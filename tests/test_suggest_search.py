"""Suggest miner: the search (candidate generation, threshold search, `for:` inference, greedy conditions, ranking, hints).

The synthetic-rule tests build timelines from a known generating rule and check the miner recovers it (or an equivalent) from a few labels.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant

from custom_components.event_simulator.suggest.evaluate import Evaluator
from custom_components.event_simulator.suggest.labels import Label, Labels, Tolerance
from custom_components.event_simulator.suggest.model import (
    Candidate,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
)
from custom_components.event_simulator.suggest.score import score_candidate
from custom_components.event_simulator.suggest.search import (
    SuggestOptions,
    clock_triggers,
    dedupe,
    describe,
    friendly_durations,
    infer_for,
    numeric_thresholds,
    round_in,
    suggest,
)
from custom_components.event_simulator.suggest.timeline import Timeline, to_ts

START = "2026-10-05T00:00:00+00:00"  # a Monday
T0 = to_ts(START)
S = 1
M = 60
H = 3600
D = 86_400
DOOR = "binary_sensor.door"
HUM = "sensor.bathroom_humidity"
MOTION = "binary_sensor.hall_motion"
PERSON = "person.me"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")


def timeline(initial, changes, days):
    """`changes` is [(seconds after T0, entity_id, state, attributes?)]."""
    events = [
        {
            "at": datetime.fromtimestamp(T0 + c[0], UTC).isoformat(),
            "entity_id": c[1],
            "state": str(c[2]),
            "attributes": c[3] if len(c) > 3 else {},
        }
        for c in changes
    ]
    initial = {eid: {"state": str(s), "attributes": {}} for eid, s in initial.items()}
    return Timeline.from_scenario(initial, events, START, T0 + days * D)


def drifting_sun(minutes_per_day):
    """Sunrise 06:00 UTC and sunset 18:00 UTC on 2026-10-05, sunset moving earlier by `minutes_per_day` a day."""

    def sun_events(day: date):
        base = datetime(day.year, day.month, day.day, tzinfo=UTC)
        n = (day - date(2026, 10, 5)).days
        return {
            "sunrise": base + timedelta(hours=6, minutes=n * minutes_per_day / 2),
            "sunset": base + timedelta(hours=18, minutes=-n * minutes_per_day),
        }

    return sun_events


TWENTY_MINUTES_A_DAY = drifting_sun(20)


def sunset(day, sun=TWENTY_MINUTES_A_DAY):
    return sun(date(2026, 10, 5) + timedelta(days=day))["sunset"].timestamp() - T0


def fire(at, anchor="start", end=None):
    return Label(T0 + at, T0 + (end if end is not None else at + 10 * M), "fire", anchor)


def no_fire(start, end):
    return Label(T0 + start, T0 + end, "no_fire", None)


def distractors(rng, days, *, count=4, flips_per_day=12):
    """Binary sensors flipping at random and a random-walk temperature: noise the miner must see through."""
    changes = []
    initial = {}
    for n in range(count):
        eid = f"binary_sensor.noise_{n}"
        initial[eid] = "off"
        state = "off"
        for at in sorted(rng.uniform(0, days * D) for _ in range(flips_per_day * days)):
            state = "on" if state == "off" else "off"
            changes.append((at, eid, state))
    initial["sensor.outdoor_temperature"] = "12.0"
    value = 12.0
    for at in range(600, days * D, 600):
        value = round(min(25, max(0, value + rng.uniform(-0.4, 0.4))), 1)
        changes.append((at, "sensor.outdoor_temperature", value))
    return initial, changes


def top(result):
    return result.suggestions[0]


def run(tl, labels, *, sun=None, tz="UTC", **options):
    return suggest(tl, labels, time_zone=tz, sun_events=sun, options=SuggestOptions(**options))


# Synthetic-rule recovery


def door_after_sunset(rng, *, jitter=0):
    """The door opens four times a day for three days; the light comes on only for the opening after sunset.

    Sunset moves 20 minutes earlier every day, so no clock time separates the evening openings from the late-afternoon ones.
    """
    sun = drifting_sun(20)
    initial, changes = distractors(rng, 3)
    initial[DOOR] = "off"
    anchors = []
    for day, after in enumerate((10 * M, 45 * M, 25 * M)):
        for at in (day * D + 7.5 * H, day * D + 12 * H, sunset(day, sun) - 15 * M, sunset(day, sun) + after):
            changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
        anchors.append(sunset(day, sun) + after + rng.uniform(0, jitter))
    return timeline(initial, changes, 3), Labels([fire(a) for a in anchors]), sun


def test_recovers_a_state_trigger_with_a_sun_condition():
    tl, labels, sun = door_after_sunset(random.Random(1))
    result = run(tl, labels, sun=sun)

    best = top(result)
    assert best.candidate.triggers[0].entity_id == DOOR
    assert best.candidate.triggers[0].to == ("on",)
    assert best.candidate.conditions == (SunCondition(after="sunset"),)
    assert (best.stats.anchors_hit, best.stats.anchors_total) == (3, 3)
    assert best.stats.outside_fires == ()
    assert best.explanation.startswith("Fires when binary_sensor.door changes to on, only after sunset; hits 3/3 marked starts")
    assert best.config["triggers"][0]["trigger"] == "state"
    assert best.config["conditions"] == [{"condition": "sun", "after": "sunset"}]


def test_noise_robustness_jittered_anchors_and_distractor_entities():
    rng = random.Random(7)
    tl, labels, sun = door_after_sunset(rng, jitter=90)  # labels drawn up to 90 s after the door opened
    result = run(tl, labels, sun=sun)
    best = top(result)
    assert best.candidate.triggers[0].entity_id == DOOR
    assert SunCondition(after="sunset") in best.candidate.conditions
    assert best.stats.recall == 1.0
    assert not best.stats.outside_fires


def humidity_episodes(rng, days=3):
    """Showers push humidity above 70 for half an hour once a day; the fan comes on 5 minutes after it crosses 70.

    Brief spikes to 85 (two minutes) and a wandering baseline below 70 rule out every threshold that does not wait.
    """
    initial, changes = distractors(rng, days, count=2)
    initial[HUM] = "60"
    anchors = []
    showers = {day * D + (7 + 4 * day) * H for day in range(days)}
    spikes = {day * D + h * H for day in range(days) for h in (2, 13, 20)}
    value = 60.0
    for at in range(60, days * D, 60):
        shower = next((s for s in showers if 0 <= at - s < 40 * M), None)
        spike = next((s for s in spikes if 0 <= at - s < 2 * M), None)
        if shower is not None:
            value = min(80.0, 63 + 8 * (at - shower) / 60)  # 63, 71 (the crossing), 79, 80, ...
            if at == shower + 60:
                anchors.append(at + 5 * M + rng.uniform(-30, 30))
        elif spike is not None:
            value = 85.0
        else:
            value = round(min(68.0, max(52.0, (value if value < 69 else 62) + rng.uniform(-1.5, 1.5))), 1)
        changes.append((at, HUM, value))
    return timeline(initial, changes, days), Labels([fire(a) for a in anchors])


def test_recovers_a_numeric_threshold_with_for():
    tl, labels = humidity_episodes(random.Random(2))
    result = run(tl, labels)
    best = top(result)
    (trigger,) = best.candidate.triggers
    assert isinstance(trigger, NumericStateTrigger)
    assert trigger.entity_id == HUM
    assert 68 <= trigger.above <= 71 and trigger.below is None
    assert 4 * M <= trigger.for_ <= 6 * M
    assert best.stats.recall == 1.0
    assert not best.stats.outside_fires
    assert "rises above" in best.explanation


def test_recovers_a_weekday_time_trigger():
    rng = random.Random(3)
    initial, changes = distractors(rng, 14)
    tl = timeline(initial, changes, 14)
    # Mon-Fri of two weeks: the weekend between them is inside the labeled stretch, so its 07:00 counts against "every day".
    labels = Labels([fire(day * D + 7 * H + rng.uniform(0, 100)) for day in range(12) if day % 7 < 5])
    result = run(tl, labels, sun=drifting_sun(4))
    best = top(result)
    assert best.candidate.triggers == (TimeTrigger(7 * H),)
    assert best.candidate.conditions == (TimeCondition(weekdays=WEEKDAYS),)
    assert best.stats.recall == 1.0 and not best.stats.outside_fires
    assert "only on Mon–Fri" in best.explanation


def test_a_few_labels_generalize_instead_of_fitting_conditions_to_them():
    """The light follows every door opening; the user labels only the first three. The rest of the window is unlabeled, not
    negative: conditions that merely exclude the later openings (a weekday, a narrow time window, a noise sensor's state) must
    not beat the plain trigger, which lists the later openings as outside fires to label."""
    rng = random.Random(11)
    initial, changes = distractors(rng, 5)
    initial[DOOR] = "off"
    openings = sorted(day * D + rng.uniform(6 * H, 23 * H) for day in range(5) for _ in range(4))
    for at in openings:
        changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
    tl = timeline(initial, changes, 5)
    labels = Labels([fire(at) for at in openings[:3]])

    best = top(run(tl, labels, sun=drifting_sun(4)))

    assert best.candidate == Candidate((StateTrigger(DOOR, to=("on",)),))
    assert len(best.stats.outside_fires) == len(openings) - 3


def test_one_label_suggests_the_plain_trigger():
    rng = random.Random(12)
    initial, changes = distractors(rng, 3)
    initial[DOOR] = "off"
    openings = sorted(day * D + rng.uniform(6 * H, 23 * H) for day in range(3) for _ in range(3))
    for at in openings:
        changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
    best = top(run(timeline(initial, changes, 3), Labels([fire(openings[4])]), sun=drifting_sun(4)))
    assert best.candidate == Candidate((StateTrigger(DOOR, to=("on",)),))


def test_values_seen_once_are_not_trigger_or_condition_values():
    """A timestamp-like state (an input_datetime, a scene, a button) takes a new value every time: a trigger or condition on
    one of those values can never fire again. Such an entity triggers on any change instead."""
    stamp = "input_datetime.last_arrival"
    initial = {stamp: "2026-10-01 08:00:00", DOOR: "off"}
    changes = []
    arrivals = [day * D + (17 + day) * H for day in range(4)]
    for at in arrivals:
        changes.append((at, stamp, datetime.fromtimestamp(T0 + at, UTC).strftime("%Y-%m-%d %H:%M:%S")))
    for day in range(4):
        changes += [(day * D + 9 * H, DOOR, "on"), (day * D + 9 * H + M, DOOR, "off")]
    tl = timeline(initial, changes, 4)

    best = top(run(tl, Labels([fire(at) for at in arrivals[:3]])))
    assert best.candidate.triggers == (StateTrigger(stamp, changes_only=True),)
    assert best.stats.recall == 1.0

    for result in (run(tl, Labels([fire(arrivals[1])])), run(tl, Labels([fire(at) for at in arrivals[:3]]))):
        for suggestion in result.suggestions:
            text = repr(suggestion.config)
            assert not any(value in text for value in ("2026-10-0", "08:00:00")), suggestion.config


def test_a_toggle_fires_on_both_directions():
    switch = "light.kitchen_switch"
    rng = random.Random(13)
    initial, changes = distractors(rng, 2)
    initial[switch] = "off"
    state = "off"
    toggles = sorted(rng.uniform(0, 2 * D) for _ in range(12))
    for at in toggles:
        state = "on" if state == "off" else "off"
        changes.append((at, switch, state))
    tl = timeline(initial, changes, 2)

    best = top(run(tl, Labels([fire(at) for at in toggles[:6]])))

    (trigger,) = best.candidate.triggers
    assert (trigger.entity_id, trigger.to, trigger.from_) == (switch, ("off", "on"), None)
    assert best.candidate.conditions == ()
    assert best.stats.recall == 1.0 and len(best.stats.outside_fires) == 6


def motion_episodes(rng):
    """Hall motion: three occupancy episodes with a short flicker in each; the light is on from motion until 5 minutes after it clears."""
    initial, changes = distractors(rng, 1)
    initial[MOTION] = "off"
    labels = []
    for begin in (8 * H, 13 * H, 19 * H):
        changes += [(begin, MOTION, "on"), (begin + 10 * M, MOTION, "off"), (begin + 11 * M, MOTION, "on"), (begin + 30 * M, MOTION, "off")]
        labels.append(fire(begin + rng.uniform(0, 60), "both", end=begin + 35 * M + rng.uniform(-60, 60)))
    return timeline(initial, changes, 1), Labels(labels)


def test_start_and_end_labels_yield_paired_triggers_with_ids():
    tl, labels = motion_episodes(random.Random(4))
    result = run(tl, labels)
    best = top(result)
    start, end = best.candidate.triggers
    assert (start.id, end.id) == ("start", "end")
    assert start.entity_id == end.entity_id == MOTION
    assert start.to == ("on",) and start.for_ == 0
    assert end.to == ("off",) and 4 * M <= end.for_ <= 6 * M
    assert (best.stats.anchors_hit, best.stats.anchors_total) == (6, 6)
    assert not best.stats.outside_fires
    assert [t["id"] for t in best.config["triggers"]] == ["start", "end"]
    assert best.explanation.startswith("Starts when binary_sensor.hall_motion changes to on; ends when")


def test_a_must_not_period_forces_a_condition_that_excludes_it():
    rng = random.Random(5)
    initial, changes = distractors(rng, 1, count=0)
    initial.update({DOOR: "off", PERSON: "home"})
    for at in (8 * H, 12 * H, 18 * H, 21 * H):
        changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
    changes += [(11 * H, PERSON, "not_home"), (14 * H, PERSON, "home")]
    tl = timeline(initial, changes, 1)
    labels = Labels([fire(8 * H), fire(18 * H), fire(21 * H), no_fire(11.5 * H, 13 * H)])

    result = run(tl, labels)
    best = top(result)
    assert best.stats.recall == 1.0
    assert best.stats.no_fire_violations == ()
    assert best.candidate.conditions == (StateCondition(PERSON, ("home",)),)
    assert "only when person.me is home" in best.explanation


def test_while_labels_prefer_conditions_that_hold_throughout():
    rng = random.Random(6)
    initial, changes = distractors(rng, 2, count=1)
    initial.update({DOOR: "off", PERSON: "not_home"})
    labels = []
    for day, home in ((0, 17 * H), (1, 19 * H)):  # home late on the second day
        changes += [(day * D + home, PERSON, "home"), (day * D + 23 * H, PERSON, "not_home")]
        for at in (9 * H, 14 * H, 18 * H, 21 * H):
            changes += [(day * D + at, DOOR, "on"), (day * D + at + M, DOOR, "off")]
        labels += [fire(day * D + 21 * H), Label(T0 + day * D + home, T0 + day * D + 23 * H, "fire", "while")]
    labels.append(fire(18 * H))
    tl = timeline(initial, changes, 2)
    result = run(tl, Labels(labels))
    best = top(result)
    assert (best.stats.while_held, best.stats.while_total) == (2, 2)
    assert best.stats.recall == 1.0 and not best.stats.outside_fires
    assert StateCondition(PERSON, ("home",)) in best.candidate.conditions
    assert "holds throughout 2/2 while periods" in best.explanation


def test_while_only_labels_act_when_the_period_begins():
    rng = random.Random(8)
    initial, changes = distractors(rng, 2, count=2)
    initial["media_player.tv"] = "off"
    labels = []
    for day, evening in ((0, 20 * H), (1, 19 * H + 20 * M)):  # no clock time explains both evenings
        for begin, end in ((10 * H, 11 * H), (evening, evening + 2.5 * H)):
            changes += [(day * D + begin, "media_player.tv", "playing"), (day * D + end, "media_player.tv", "off")]
        labels.append(Label(T0 + day * D + evening, T0 + day * D + evening + 2.5 * H, "fire", "while"))
    result = run(timeline(initial, changes, 2), Labels(labels))
    best = top(result)
    assert best.candidate.triggers[0].entity_id == "media_player.tv"
    assert best.stats.while_held == best.stats.while_total == 2
    assert best.stats.recall == 1.0


def test_no_should_fire_labels_means_no_suggestions():
    tl = timeline({DOOR: "off"}, [(H, DOOR, "on")], 1)
    assert run(tl, Labels([no_fire(0, 2 * H)])).suggestions == []


def test_results_are_ranked_deduped_and_capped_at_top_k():
    tl, labels, sun = door_after_sunset(random.Random(1))
    result = run(tl, labels, sun=sun, top_k=5)
    assert 1 <= len(result.suggestions) <= 5
    keys = [s.rank for s in result.suggestions]
    assert keys == sorted(keys)
    signatures = [(tuple(s.stats.no_fire_violations), s.stats.outside_fires, s.stats.anchors_hit, s.stats.fires, s.explanation) for s in result.suggestions]
    assert len(set(signatures)) == len(signatures)
    out = result.as_dict()
    assert set(out) >= {"suggestions", "hints"}
    assert set(out["suggestions"][0]) >= {"config", "stats", "explanation"}


def test_attribute_changes_when_the_state_is_static():
    tv = "media_player.living_room"
    changes = []
    anchors = []
    for day in range(3):
        changes += [(day * D + 8 * H, tv, "on", {"source": "Radio"}), (day * D + (18 + day) * H, tv, "on", {"source": "TV"})]
        anchors.append(fire(day * D + (18 + day) * H + 30))
    tl = timeline({tv: "on"}, changes, 3)
    best = top(run(tl, Labels(anchors)))
    assert best.candidate.triggers == (StateTrigger(tv, attribute="source", to=("TV",)),)
    assert best.stats.recall == 1.0 and not best.stats.outside_fires
    assert best.explanation.startswith("Fires when media_player.living_room's source changes to TV")


def test_sampling_bad_fires_still_scores_exactly():
    tl, labels, sun = door_after_sunset(random.Random(1))
    sampled = run(tl, labels, sun=sun, max_bad_sample=2)  # the door fires 9 times outside the labels
    assert top(sampled).candidate.conditions == (SunCondition(after="sunset"),)
    ev = Evaluator(tl, time_zone="UTC", sun_events=sun)
    for suggestion in sampled.suggestions:
        assert suggestion.stats == score_candidate(ev, suggestion.candidate, labels)


def test_options_from_the_websocket():
    assert SuggestOptions.from_dict(None) == SuggestOptions()
    options = SuggestOptions.from_dict({"max_conditions": "1", "top_k": 3, "max_lag": 600, "unknown": True})
    assert (options.max_conditions, options.top_k, options.max_lag) == (1, 3, 600.0)


async def test_every_suggestion_passes_home_assistant_validation(hass: HomeAssistant):
    configs = []
    tl, labels, sun = door_after_sunset(random.Random(1))
    configs += [s.config for s in run(tl, labels, sun=sun).suggestions]
    tl, labels = humidity_episodes(random.Random(2))
    configs += [s.config for s in run(tl, labels).suggestions]
    tl, labels = motion_episodes(random.Random(4))
    configs += [s.config for s in run(tl, labels).suggestions]
    assert len(configs) > 10
    for n, config in enumerate(configs):
        validated = await async_validate_config_item(hass, f"suggested_{n}", config)
        assert validated is not None and validated.validation_error is None, (config, validated.validation_error)


# Hints


def test_hints_list_outside_fires_and_recurrences_ranked_by_similarity():
    tl, labels, sun = door_after_sunset(random.Random(1))
    # Only two of the three evening openings are labeled: the third looks like them, and the hints should say so.
    two = Labels(labels.labels[:2])
    third = labels.labels[2].start
    result = run(tl, two, sun=sun, hint_suggestions=8)
    assert all(0 <= h.similarity <= 1 for h in result.hints)

    outside = [h for h in result.hints if h.kind == "outside_fire"]
    assert outside
    for hint in outside:
        assert hint.suggestions
        for rank in hint.suggestions:
            assert any(abs(t - hint.at) <= 60 for t in result.suggestions[rank].stats.outside_fires)
    evening = [h for h in outside if abs(h.at - third) < 2 * M]
    assert evening and len(evening[0].suggestions) > 1  # several suggestions fire there: one hint lists them all
    assert [h.similarity for h in outside] == sorted((h.similarity for h in outside), reverse=True)

    recurrences = [h for h in result.hints if h.kind == "recurrence"]
    assert any(h.entity_id == DOOR and (h.from_, h.to) == ("off", "on") for h in recurrences)
    assert all(abs(h.at - o.at) > 60 for h in recurrences for o in outside)  # each moment is listed once

    # With only the top suggestion's outside fires (none: "after 18:00" excludes the third evening), the third evening
    # is the most similar recurrence of the labeled door opening.
    best = run(tl, two, sun=sun, hint_suggestions=1)
    first = best.hints[0]
    assert first.kind == "recurrence" and abs(first.at - third) < 2 * M
    assert (first.entity_id, first.from_, first.to) == (DOOR, "off", "on")
    assert {"kind", "at", "similarity", "suggestions", "description", "entity_id", "from", "to"} <= set(first.as_dict())


# Units


def test_round_in_picks_the_roundest_number_in_a_half_open_interval():
    assert round_in(68.4, 71.0) == 70
    assert round_in(70.0, 70.6) == 70  # closed on the left
    assert round_in(69.2, 70.0) == 69.5  # 70 itself is excluded
    assert round_in(69.2, 70.0, closed="hi") == 70
    assert round_in(1234.0, 1301.0) == 1300
    assert round_in(0.12, 0.13) == 0.12
    assert round_in(61.0, 79.0) == 70  # the most round, nearest the middle


def test_numeric_thresholds_search_the_full_window_distribution():
    # Crossings near the anchors go 68.9 -> 70.4; elsewhere the value wanders 69.1 <-> 69.6, crossing 69.5 often.
    changes = []
    anchors = []
    for day in range(3):
        base = day * D
        for k in range(20):
            changes.append((base + k * 10 * M, HUM, 69.1 if k % 2 else 69.6))
        changes += [(base + 12 * H, HUM, 68.9), (base + 13 * H, HUM, 70.4), (base + 14 * H, HUM, 69.1)]
        anchors.append(fire(base + 13 * H))
    tl = timeline({HUM: "69"}, changes, 3)
    labels = Labels(anchors)
    found = numeric_thresholds(tl[HUM], labels.anchors, labels.tolerance, "above")
    assert found[0] == 70  # every anchor crossing, no wandering crossing, and round
    assert 69.5 not in found[:1]


def test_numeric_thresholds_below():
    # Light level falls from 400 to 20 every evening, 49 -> 31 at the anchor and 31 -> 20 ten minutes later; noon clouds dip to 60.
    changes = []
    anchors = []
    for day in range(3):
        base = day * D
        changes += [(base + 12 * H, "sensor.lux", 60.0), (base + 12.5 * H, "sensor.lux", 400.0)]
        for k, value in enumerate((300.0, 150.0, 80.0, 49.0, 31.0, 20.0)):
            changes.append((base + 18 * H + k * 10 * M, "sensor.lux", value))
        changes.append((base + 23 * H, "sensor.lux", 400.0))
        anchors.append(fire(base + 18 * H + 40 * M))
    tl = timeline({"sensor.lux": "400"}, changes, 3)
    labels = Labels(anchors)
    # (31, 49] and (20, 31] both hit every anchor without firing at noon; 40 crosses right at the anchors, 30 ten minutes late.
    assert numeric_thresholds(tl["sensor.lux"], labels.anchors, labels.tolerance, "below")[:2] == [40, 30]
    best = top(run(tl, labels))
    assert best.candidate.triggers == (NumericStateTrigger("sensor.lux", below=40),)


def test_friendly_durations_and_for_inference():
    assert friendly_durations(240, 840)[:2] == [300, 600]
    assert friendly_durations(61, 62) == []
    lags = [290.0, 310.0, 330.0, 305.0]
    assert infer_for(lags, Tolerance())[0] == 300  # [330 - 120, 290 + 600] contains 5 min, the closest friendly value
    assert infer_for([3700.0, 3705.0], Tolerance(before=0, after=30)) == [3720]  # no friendly value fits: whole minutes
    assert infer_for([100.0, 2000.0], Tolerance())  # inconsistent lags still yield the nearest friendly value


def test_clock_vs_sun_choice_follows_the_smaller_spread():
    sun = drifting_sun(20)
    tl = timeline({}, [], 4)
    ev = Evaluator(tl, time_zone="UTC", sun_events=sun)
    # 10 minutes after a sunset that moves 20 minutes a day: the sun explains it, the clock does not.
    sunset_labels = Labels([fire(sunset(day) + 10 * M) for day in range(4)])
    found = clock_triggers(ev, sunset_labels, sunset_labels.anchors, None)
    assert found == [SunTrigger("sunset", offset=10 * M)]
    # 07:00 every day: the clock explains it.
    clock_labels = Labels([fire(day * D + 7 * H + 30 * day) for day in range(4)])  # 07:00..07:01:30
    assert clock_triggers(ev, clock_labels, clock_labels.anchors, None) == [TimeTrigger(7 * H)]
    # Trigger ids are carried through.
    assert clock_triggers(ev, clock_labels, clock_labels.anchors, "start") == [TimeTrigger(7 * H, id="start")]


def test_dedupe_keeps_the_simplest_of_candidates_with_the_same_fires():
    tl = timeline({DOOR: "off"}, [(H, DOOR, "on"), (2 * H, DOOR, "off"), (3 * H, DOOR, "on")], 1)
    ev = Evaluator(tl, time_zone="UTC")
    labels = Labels([fire(H), fire(3 * H)])
    plain = Candidate((StateTrigger(DOOR, to=("on",)),))
    from_off = Candidate((StateTrigger(DOOR, from_=("off",), to=("on",)),))
    other = Candidate((StateTrigger(DOOR, to=("off",)),))
    kept = dedupe(ev, labels, [from_off, plain, other])
    assert kept == [plain, other]


def test_describe_reads_like_a_sentence():
    candidate = Candidate(
        (NumericStateTrigger(HUM, above=70, for_=5 * M),),
        (TimeCondition(after=17 * H, before=22 * H), StateCondition(PERSON, ("home",))),
    )
    assert describe(candidate) == f"Fires when {HUM} rises above 70 and stays there for 5 min, only between 17:00 and 22:00, only when person.me is home"
    assert describe(Candidate((SunTrigger("sunset", offset=-30 * M),))) == "Fires 30 min before sunset"
    assert describe(Candidate((TimeTrigger(7 * H),), (SunCondition(after="sunset", before="sunrise"),))) == "Fires at 07:00, only after sunset or before sunrise"
    assert describe(Candidate((TimeTrigger(7 * H),), (SunCondition(before="sunset", before_offset=H),))) == "Fires at 07:00, only before sunset + 1 h"
    assert describe(Candidate((TimeTrigger(7 * H),), (SunCondition(after="sunrise", after_offset=-30 * M, before="sunset"),))) == "Fires at 07:00, only between sunrise − 30 min and sunset"


# Performance


def test_a_month_of_twenty_chatty_entities_is_fast():
    rng = random.Random(9)
    days = 30
    initial = {}
    changes = []
    for n in range(10):
        eid = f"binary_sensor.chatty_{n}"
        initial[eid] = "off"
        state = "off"
        for at in sorted(rng.uniform(0, days * D) for _ in range(3000)):
            state = "on" if state == "off" else "off"
            changes.append((at, eid, state))
    for n in range(10):
        eid = f"sensor.level_{n}"
        initial[eid] = "50"
        value = 50.0
        for at in sorted(rng.uniform(0, days * D) for _ in range(3000)):
            value = round(min(100, max(0, value + rng.uniform(-3, 3))), 1)
            changes.append((at, eid, value))
    initial[DOOR] = "off"
    labels = []
    for day in range(days):
        at = day * D + 19 * H + rng.uniform(0, 3600)
        changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
        if day % 7 == 0:
            labels.append(fire(at + 20))
    labels.append(no_fire(3 * D, 3 * D + 6 * H))
    tl = timeline(initial, changes, days)

    began = time.perf_counter()
    result = run(tl, Labels(labels), sun=drifting_sun(2))
    elapsed = time.perf_counter() - began
    assert top(result).candidate.triggers[0].entity_id == DOOR
    assert elapsed < 10, elapsed  # the budget is ~2 s; generous for slow CI runners
