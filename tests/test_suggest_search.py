"""Suggest miner: the search (candidate generation, threshold search, `for:` inference, greedy conditions, ranking, hints).

The synthetic-rule tests build timelines from a known generating rule and check the miner recovers it (or an equivalent) from a few labels.
"""

from __future__ import annotations

import math
import random
import time
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant

from custom_components.event_simulator.suggest.evaluate import Evaluator
from custom_components.event_simulator.suggest.labels import Label, Labels, Tolerance
from custom_components.event_simulator.suggest.model import (
    Candidate,
    EntityTimeTrigger,
    EventTrigger,
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
    KIND_COVERAGE,
    MAX_CARDS,
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


def test_for_as_long_as_the_longest_lag_is_found():
    """"Turn the espresso machine off after an hour on": the automation fires a moment after the hour, a lag just past `max_lag` (1 h) that a `for: 1 h` still matches."""
    switch = "switch.espresso_machine"
    rng = random.Random(14)
    initial, changes = distractors(rng, 4)
    initial[switch] = "off"
    anchors = []
    for day in range(4):
        on = day * D + rng.uniform(7 * H, 10 * H)
        changes += [(on - 3 * H, switch, "on"), (on - 3 * H + 10 * M, switch, "off")]  # a short brew: off by hand
        changes += [(on, switch, "on"), (on + H + 0.05, switch, "off")]
        anchors.append(fire(on + H + 0.04))
    tl = timeline(initial, changes, 4)

    best = top(run(tl, Labels(anchors[:3])))

    assert best.candidate.triggers == (StateTrigger(switch, to=("on",), for_=H),)
    assert best.stats.recall == 1.0
    assert [round(t - T0 - H) for t in best.stats.outside_fires] == [round(anchors[3].start - T0 - H)]  # the fourth day, unlabeled


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


# OR of several triggers (#33)


def door_or_motion(rng, days=4):
    """The hall light comes on when the door opens (twice a day) or when the hall motion sensor sees someone (once a day, at other times)."""
    initial, changes = distractors(rng, days)
    initial.update({DOOR: "off", MOTION: "off"})
    anchors = []
    for day in range(days):
        for at in (day * D + rng.uniform(7 * H, 9 * H), day * D + rng.uniform(17 * H, 19 * H)):
            changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
            anchors.append(at + rng.uniform(0, 30))
        at = day * D + rng.uniform(12 * H, 14 * H)
        changes += [(at, MOTION, "on"), (at + 3 * M, MOTION, "off")]
        anchors.append(at + rng.uniform(0, 30))
    return initial, changes, sorted(anchors)


DOOR_ON = StateTrigger(DOOR, to=("on",))
MOTION_ON = StateTrigger(MOTION, to=("on",))


def test_anchors_explained_by_one_trigger_or_another_get_both():
    initial, changes, anchors = door_or_motion(random.Random(21))
    result = run(timeline(initial, changes, 4), Labels([fire(at) for at in anchors]))

    best = top(result)
    assert set(best.candidate.triggers) == {DOOR_ON, MOTION_ON}
    assert best.candidate.conditions == ()
    assert best.stats.recall == 1.0 and not best.stats.outside_fires
    assert [t["entity_id"] for t in best.config["triggers"]] == [DOOR, MOTION]  # the trigger covering most anchors first
    assert best.explanation.startswith(f"Fires when {DOOR} changes to on or {MOTION} changes to on; hits 12/12")
    # The single triggers are still there, below the union.
    assert any(s.candidate.triggers == (DOOR_ON,) for s in result.suggestions[1:])


def test_a_trigger_that_meets_a_stray_anchor_by_chance_is_not_added():
    """A label nothing explains, with a chatty sensor flipping a minute before it: one coincidence is no evidence, so neither that sensor nor a clock time fitted to the lone label joins the union."""
    rng = random.Random(22)
    initial, changes, anchors = door_or_motion(rng)
    stray = 3 * D + 22 * H
    chatty = "binary_sensor.chatty"
    flips = sorted([*(rng.uniform(0, 4 * D) for _ in range(4 * 24)), stray - M])
    initial[chatty] = "off"
    changes += [(at, chatty, "on" if n % 2 == 0 else "off") for n, at in enumerate(flips)]
    labels = Labels([fire(at) for at in [*anchors, stray]])

    best = top(run(timeline(initial, changes, 4), labels))

    assert set(best.candidate.triggers) == {DOOR_ON, MOTION_ON}
    assert (best.stats.anchors_hit, best.stats.anchors_total) == (12, 13)


def test_a_clock_time_fitted_to_one_burst_of_labels_is_not_added():
    """Three labels a minute apart that nothing explains: a clock time hits all three with one fire, which is still one moment."""
    initial, changes, anchors = door_or_motion(random.Random(24))
    burst = [3 * D + 22 * H + n * M for n in range(3)]
    best = top(run(timeline(initial, changes, 4), Labels([fire(at) for at in [*anchors, *burst]])))
    assert set(best.candidate.triggers) == {DOOR_ON, MOTION_ON}


def test_start_and_end_pairs_take_an_or_on_one_side():
    """The light comes on when the door opens or motion starts, and goes off 5 minutes after motion clears: the start side is a union with id `start`, the end side keeps its own id."""
    rng = random.Random(23)
    initial, changes = distractors(rng, 2)
    initial.update({DOOR: "off", MOTION: "off"})
    labels = []
    for n, begin in enumerate((8 * H, 13 * H, 19 * H, D + 8 * H, D + 13 * H, D + 19 * H)):
        if n % 2 == 0:  # through the door; the hall sees them only a quarter of an hour later
            changes += [(begin, DOOR, "on"), (begin + M, DOOR, "off"), (begin + 15 * M, MOTION, "on")]
        else:
            changes.append((begin, MOTION, "on"))
        changes.append((begin + 40 * M, MOTION, "off"))
        labels.append(fire(begin + rng.uniform(0, 30), "both", end=begin + 45 * M + rng.uniform(-30, 30)))

    best = top(run(timeline(initial, changes, 2), Labels(labels)))

    triggers = best.candidate.triggers
    starts = {t for t in triggers if t.id == "start"}
    (end,) = [t for t in triggers if t.id == "end"]
    assert starts == {StateTrigger(DOOR, to=("on",), id="start"), StateTrigger(MOTION, to=("on",), id="start")}
    assert (end.entity_id, end.to) == (MOTION, ("off",)) and 4 * M <= end.for_ <= 6 * M
    assert (best.stats.anchors_hit, best.stats.anchors_total) == (12, 12)
    assert [t["id"] for t in best.config["triggers"]] == ["start", "start", "end"]
    assert best.explanation.startswith(f"Starts when {DOOR} changes to on or {MOTION} changes to on; ends when {MOTION} changes to off")


def test_a_single_trigger_covering_every_anchor_needs_no_union():
    tl, labels, sun = door_after_sunset(random.Random(1))
    for suggestion in run(tl, labels, sun=sun).suggestions:
        assert len(suggestion.candidate.triggers) == 1


# Any change of a numeric sensor (#34)

INDOOR = "sensor.indoor_temperature"
OUTDOOR = "sensor.porch_temperature"


def wandering(rng, entity_id, days, gaps=(3 * M, 15 * M), value=70.0):
    """A temperature reporting at irregular intervals, half a degree up or down each time."""
    changes = []
    at = rng.uniform(*gaps)
    while at < days * D:
        value += rng.choice((-0.5, 0.5))
        changes.append((at, entity_id, value))
        at += rng.uniform(*gaps)
    return changes


def test_every_change_of_a_numeric_sensor():
    """A blueprint controller re-evaluates on every report of a temperature: a state trigger with no from/to."""
    rng = random.Random(31)
    initial, changes = distractors(rng, 2)
    initial[INDOOR] = "70"
    reports = wandering(rng, INDOOR, 2)
    tl = timeline(initial, [*changes, *reports], 2)
    labels = Labels([fire(at) for at, *_ in reports if at < D])

    best = top(run(tl, labels))

    assert best.candidate == Candidate((StateTrigger(INDOOR),))
    assert best.config["triggers"] == [{"trigger": "state", "entity_id": INDOOR}]
    assert best.stats.recall == 1.0
    assert len(best.stats.outside_fires) == sum(at >= D for at, *_ in reports)  # the second day, unlabeled
    assert best.explanation.startswith(f"Fires when {INDOOR} changes;")


def test_every_change_while_above_a_threshold_takes_a_numeric_condition():
    """The template-free form of "turn the fan on when it is warm": every report while the temperature is above 72."""
    rng = random.Random(32)
    initial, changes = distractors(rng, 3)
    initial[INDOOR] = "70"
    reports = wandering(rng, INDOOR, 3, gaps=(10 * M, 20 * M), value=72.0)
    tl = timeline(initial, [*changes, *reports], 3)
    warm = [at for at, _, value in reports if value > 72]
    assert len(warm) > 20
    labels = Labels([fire(at) for at in warm if at < 2 * D])

    best = top(run(tl, labels))

    assert best.candidate.triggers == (StateTrigger(INDOOR),)
    (condition,) = best.candidate.conditions
    assert condition.entity_id == INDOOR and 72 <= condition.above < 72.5 and condition.below is None
    assert best.stats.recall == 1.0
    assert best.stats.outside_nearby == 0


def test_every_change_of_either_of_two_sensors():
    """The fan blueprint watches an indoor and an outdoor temperature: an OR of two any-change triggers."""
    rng = random.Random(33)
    initial, changes = distractors(rng, 3)
    initial.update({INDOOR: "70", OUTDOOR: "60"})
    reports = wandering(rng, INDOOR, 3, gaps=(20 * M, 60 * M)) + wandering(rng, OUTDOOR, 3, gaps=(20 * M, 60 * M), value=60.0)
    tl = timeline(initial, [*changes, *reports], 3)
    labels = Labels([fire(at) for at, *_ in sorted(reports) if at < 2 * D])

    best = top(run(tl, labels))

    assert set(best.candidate.triggers) == {StateTrigger(INDOOR), StateTrigger(OUTDOOR)}
    assert best.stats.recall == 1.0 and best.stats.outside_nearby == 0


def test_every_change_of_either_of_two_chatty_sensors():
    """Both temperatures report every few minutes, so either one has a report within the tolerance of almost every fire; only the hours one of them goes quiet tell them apart, and there the other one's reports line up with the fires."""
    rng = random.Random(35)
    initial, changes = distractors(rng, 3)
    initial.update({INDOOR: "70", OUTDOOR: "60"})
    reports = [
        *((at, e, v) for at, e, v in wandering(rng, INDOOR, 3, gaps=(2 * M, 6 * M)) if not 2 * H <= at % D < 3 * H),
        *((at, e, v) for at, e, v in wandering(rng, OUTDOOR, 3, gaps=(2 * M, 6 * M), value=60.0) if not 14 * H <= at % D < 15 * H),
    ]
    tl = timeline(initial, [*changes, *reports], 3)
    labels = Labels([fire(at) for at, *_ in sorted(reports) if at < 2 * D])

    best = top(run(tl, labels))

    assert set(best.candidate.triggers) == {StateTrigger(INDOOR), StateTrigger(OUTDOOR)}
    assert best.stats.recall == 1.0


def test_every_change_of_a_numeric_attribute():
    """A thermostat's state stays `heat` while its current_temperature attribute reports: a state trigger on the attribute, with no from/to."""
    rng = random.Random(34)
    climate = "climate.living_room"
    initial, changes = distractors(rng, 2)
    initial[climate] = "heat"
    reports = [(at, climate, "heat", {"current_temperature": value}) for at, _, value in wandering(rng, climate, 2)]
    tl = timeline(initial, [*changes, *reports], 2)
    labels = Labels([fire(at) for at, *_ in reports[1:] if at < D])

    best = top(run(tl, labels))

    assert best.candidate == Candidate((StateTrigger(climate, attribute="current_temperature"),))
    assert best.stats.recall == 1.0
    assert best.explanation.startswith(f"Fires when {climate}'s current_temperature changes;")


def test_labels_that_do_not_line_up_with_reports_get_no_any_change_trigger():
    """Hand-drawn labels near a chatty sensor's reports: being within the tolerance of some report is no evidence."""
    tl, labels = humidity_episodes(random.Random(2))
    for suggestion in run(tl, labels).suggestions:
        assert not any(isinstance(t, StateTrigger) and t.entity_id == HUM for t in suggestion.candidate.triggers)


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
    """The top `top_k` cards, then only cards that cover a trigger kind with fewer than `KIND_COVERAGE` so far (tests/test_suggest_rank.py)."""
    tl, labels, sun = door_after_sunset(random.Random(1))
    result = run(tl, labels, sun=sun, top_k=5)
    assert 1 <= len(result.suggestions) <= MAX_CARDS
    for n, suggestion in enumerate(result.suggestions[5:], start=5):
        earlier = [s.trigger_kinds for s in result.suggestions[:n]]
        assert any(sum(kind in kinds for kinds in earlier) < KIND_COVERAGE for kind in suggestion.trigger_kinds)
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
    initial, changes, anchors = door_or_motion(random.Random(21))  # ORs of triggers
    configs += [s.config for s in run(timeline(initial, changes, 4), Labels([fire(at) for at in anchors])).suggestions]
    rng = random.Random(34)  # any change of a numeric attribute
    reports = [(at, "climate.x", "heat", {"current_temperature": value}) for at, _, value in wandering(rng, "climate.x", 1)]
    configs += [s.config for s in run(timeline({"climate.x": "heat"}, reports, 1), Labels([fire(at) for at, *_ in reports[1:10]])).suggestions]
    tl, anchors = boot_dryer(random.Random(38))  # time at a helper plus an offset
    configs += [s.config for s in run(tl, Labels([fire(at) for at in anchors[:3]])).suggestions]
    assert any(len(c["triggers"]) > 1 for c in configs)
    assert any(c["triggers"][0].get("attribute") and "to" not in c["triggers"][0] for c in configs)
    assert any(isinstance(c["triggers"][0].get("at"), dict) for c in configs)
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
    # Every write (attribute-only ones too) against `to: null`, state changes only.
    assert describe(Candidate((StateTrigger(HUM),))) == f"Fires when {HUM} changes"
    assert describe(Candidate((StateTrigger(HUM, changes_only=True),))) == f"Fires when {HUM} changes state"
    # A calendar entity is on while one of its events runs: on and off are an event starting and ending (#35).
    assert describe(Candidate((StateTrigger("calendar.trash", to=("on",)),))) == "Fires when an event of calendar.trash starts"
    assert describe(Candidate((StateTrigger("calendar.trash", to=("off",), id="end"), StateTrigger("calendar.trash", to=("on",), id="start")))) == (
        "Starts when an event of calendar.trash starts; ends when an event of calendar.trash ends"
    )
    assert describe(Candidate((EventTrigger("tag_scanned", (("tag_id", "front"),)),))) == "Fires when a tag_scanned event arrives with tag_id front"
    assert describe(Candidate((SunTrigger("sunset"), StateTrigger(DOOR, to=("on",)), StateTrigger(HUM)))) == f"Fires at sunset or when {DOOR} changes to on or {HUM} changes"
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


# Event triggers


def zha_remotes(rng, days):
    """Two ZHA remotes sending commands at random, every event carrying the same device keys: the bus events of a zigbee install."""
    bus = []
    for ieee, device, per_day in (("aa:aa", "dev-aa", 6), ("bb:bb", "dev-bb", 20)):
        for at in sorted(rng.uniform(0, days * D) for _ in range(per_day * days)):
            command = rng.choice(("on", "off"))
            data = {
                "device_ieee": ieee,
                "device_id": device,
                "unique_id": f"{ieee}:1:0x0006",
                "endpoint_id": 1,
                # A technical key that singles out the device as well as its id does: the id reads better.
                "cluster_id": 6 if ieee == "aa:aa" else 8,
                "command": command,
                "args": [],
                "params": {},
            }
            bus.append((at, "zha_event", data))
    return bus


def with_bus(tl, bus):
    events = [
        {"at": datetime.fromtimestamp(T0 + at, UTC).isoformat(), "event_type": event_type, "data": data}
        for at, event_type, data in bus
    ]
    return Timeline(tl.start, tl.end, tl._entities, Timeline.from_scenario({}, [], tl.start, tl.end, bus_events=events)._bus)  # noqa: SLF001


def test_event_trigger_from_the_events_at_the_labels():
    rng = random.Random(7)
    days = 7
    initial, changes = distractors(rng, days)
    bus = zha_remotes(rng, days)
    # Noise on the bus: a type with an event near every label by chance would be no evidence, and one nowhere near them is not offered.
    bus += [(rng.uniform(0, days * D), "mobile_app_notification_cleared", {"tag": "x"}) for _ in range(5)]
    tl = with_bus(timeline(initial, changes, days), bus)
    wanted = EventTrigger("zha_event", (("device_id", "dev-aa"), ("command", "on")))
    fires = Evaluator(tl, time_zone="UTC").trigger_fires(wanted)
    assert len(fires) >= 10
    result = run(tl, Labels([fire(t - T0) for t in fires[:3]]))
    best = top(result)
    assert best.candidate.triggers == (wanted,), best.explanation
    assert list(best.stats.fire_times) == fires
    assert best.config["triggers"] == [
        {"trigger": "event", "event_type": "zha_event", "event_data": {"device_id": "dev-aa", "command": "on"}}
    ]
    assert "zha_event" in best.explanation and "dev-aa" in best.explanation
    assert all(c.event_types <= {"zha_event"} for c in (s.candidate for s in result.suggestions))


def test_event_trigger_for_both_commands_leaves_the_command_out():
    rng = random.Random(8)
    days = 7
    tl = with_bus(timeline({}, [], days), zha_remotes(rng, days))
    wanted = EventTrigger("zha_event", (("device_id", "dev-aa"),))
    fires = Evaluator(tl, time_zone="UTC").trigger_fires(wanted)
    result = run(tl, Labels([fire(t - T0) for t in fires[:8]]))
    assert top(result).candidate.triggers == (wanted,), top(result).explanation


def test_event_data_values_home_assistant_would_read_differently_are_left_out():
    """A static string in `event_data` is parsed by Home Assistant's template rendering ("1" becomes 1), so it would never match the event's "1"."""
    days = 3
    bus = []
    for day in range(days):
        for hour, button in ((8, "1"), (13, "2"), (19, "1")):
            bus.append((day * D + hour * H, "deconz_event", {"id": "remote", "event": button, "unique": f"{day}-{hour}"}))
    tl = with_bus(timeline({}, [], days), bus)
    result = run(tl, Labels([fire(day * D + 13 * H) for day in range(days)]))
    assert any(isinstance(t, EventTrigger) for s in result.suggestions for t in s.candidate.triggers)
    for suggestion in result.suggestions:
        for trigger in suggestion.candidate.triggers:
            if isinstance(trigger, EventTrigger):
                assert ("event", "2") not in trigger.event_data


def test_of_event_data_picking_out_the_same_events_the_device_key_stays():
    """An IR blaster sends only `learned_ir_code` on its own cluster: its id, its radio address, its cluster and the command each single out its events. The suggestion names the device, once."""
    rng = random.Random(9)
    days = 7
    bus = zha_remotes(rng, days)
    learned = sorted(rng.uniform(0, days * D) for _ in range(12))
    data = {"device_ieee": "cc:cc", "device_id": "dev-cc", "unique_id": "cc:cc:1:0xed00", "endpoint_id": 1, "cluster_id": 60672, "command": "learned_ir_code"}
    bus += [(at, "zha_event", data) for at in learned]
    tl = with_bus(timeline({}, [], days), bus)
    result = run(tl, Labels([fire(at) for at in learned[:3]]))
    assert top(result).candidate.triggers == (EventTrigger("zha_event", (("device_id", "dev-cc"),)),), top(result).explanation


# Time triggers at an entity's time (#38)

DRYER = "input_datetime.boot_dryer_start_time"


def helper_state(at, *, date=True, time=True):
    """An input_datetime holding `at` seconds after T0 (UTC), as Home Assistant's state and attributes spell it."""
    local = datetime.fromtimestamp(T0 + at, UTC)
    attributes = {"has_date": date, "has_time": time}
    if date:
        attributes |= {"year": local.year, "month": local.month, "day": local.day}
    if time:
        attributes |= {"hour": local.hour, "minute": local.minute, "second": local.second}
    state = " ".join(part for part, wanted in ((local.strftime("%Y-%m-%d"), date), (local.strftime("%H:%M:%S"), time)) if wanted)
    return state, attributes


def with_states(tl, initial, changes):
    """`tl` plus entities whose states carry attributes: `initial` {entity_id: (state, attributes)}, `changes` [(at, entity_id, state, attributes)]."""
    events = [{"at": datetime.fromtimestamp(T0 + at, UTC).isoformat(), "entity_id": e, "state": st, "attributes": a} for at, e, st, a in changes]
    extra = Timeline.from_scenario({e: {"state": st, "attributes": a} for e, (st, a) in initial.items()}, events, tl.start, tl.end)
    return Timeline(tl.start, tl.end, {**tl._entities, **extra._entities}, tl._bus)  # noqa: SLF001


def boot_dryer(rng, days=14):
    """The user's boot dryer: started at a random time every few days (the helper records when), turned off 24 h later.

    The real automation is a template (`now() >= start + 24 h`); its template-free form is a time trigger at the helper plus 24 h. The dryer's own switch turns on at the start, so its changes are the action's effects, not the cause.
    """
    initial, changes = distractors(rng, days)
    tl = timeline({**initial, "switch.boot_dryer": "off"}, changes, days)
    helper = {DRYER: helper_state(-30 * D)}
    writes = []
    anchors = []
    day = 0.0
    while True:
        start = float(round(day * D + rng.uniform(7 * H, 21 * H)))  # the helper holds whole seconds
        if start + D >= days * D:
            break
        writes.append((start, DRYER, *helper_state(start)))
        anchors.append(start + D)
        day += rng.choice((2, 3))
    # The dryer switch is noise here (the real one turns off at the labels, the action's own effect, which the user would not track).
    switch = [(t, "switch.boot_dryer", "on", {}) for t, *_ in writes] + [(t + 2 * H, "switch.boot_dryer", "off", {}) for t, *_ in writes]
    return with_states(tl, helper, writes + switch), anchors


def test_recovers_a_time_trigger_at_a_helper_plus_an_offset():
    tl, anchors = boot_dryer(random.Random(38))
    assert len(anchors) >= 4
    result = run(tl, Labels([fire(at) for at in anchors[:3]]))
    best = top(result)
    assert best.candidate.triggers == (EntityTimeTrigger(DRYER, offset=D),), best.explanation
    assert best.config["triggers"] == [{"trigger": "time", "at": {"entity_id": DRYER, "offset": "24:00:00"}}]
    assert list(best.stats.fire_times) == [T0 + at for at in anchors]
    assert best.explanation.startswith(f"Fires 24 h after the time in {DRYER};"), best.explanation


def test_a_time_only_helper_with_an_offset():
    """Coffee 15 minutes before the alarm, whose time changes every few days: no clock time hits every morning."""
    rng = random.Random(39)
    days = 10
    initial, changes = distractors(rng, days)
    tl = timeline(initial, changes, days)
    alarm = "input_datetime.alarm"
    clock = 6.5 * H
    helper = {alarm: helper_state(clock, date=False)}
    writes, anchors = [], []
    for day in range(days):
        if day and day % 3 == 0:
            clock = rng.choice((5.75, 6, 6.25, 7, 7.5)) * H
            writes.append((day * D + 2 * H, alarm, *helper_state(clock, date=False)))
        anchors.append(day * D + clock - 15 * M)
    tl = with_states(tl, helper, writes)
    best = top(run(tl, Labels([fire(at) for at in anchors[:7]])))
    assert best.candidate.triggers == (EntityTimeTrigger(alarm, offset=-15 * M),), best.explanation
    assert list(best.stats.fire_times) == [T0 + at for at in anchors]
    assert "15 min before the time in input_datetime.alarm" in best.explanation


def test_a_chatty_timestamp_sensor_meeting_labels_by_chance_is_not_suggested():
    """A sensor stamping its own last report every 5 minutes has some time within reach of any label: no evidence."""
    rng = random.Random(40)
    days = 3
    initial, changes = distractors(rng, days)
    tl = timeline(initial, changes, days)
    seen = "sensor.phone_last_update"
    stamp = {"device_class": "timestamp"}
    reports = [(at, seen, datetime.fromtimestamp(T0 + at - 20, UTC).isoformat(), stamp) for at in range(300, days * D, 300)]
    tl = with_states(tl, {seen: (datetime.fromtimestamp(T0, UTC).isoformat(), stamp)}, reports)
    result = run(tl, Labels([fire(rng.uniform(0, days * D)) for _ in range(4)]))
    assert not any(isinstance(t, EntityTimeTrigger) for s in result.suggestions for t in s.candidate.triggers)


# Saying when a template would be needed (#38)

INDOOR_T, OUTDOOR_T = "sensor.living_room_temperature", "sensor.outdoor_temperature_2"


def test_labels_at_one_sensor_rising_above_another_name_the_template_it_needs():
    """Open the windows when it gets warmer inside than outside: both drift, so no threshold on either is the moment."""
    rng = random.Random(41)
    days = 4
    unit = {"unit_of_measurement": "°C"}
    changes = []
    inside, outside = 21.0, 15.0
    phase = rng.uniform(0, D)
    anchors = []
    previous = None
    for at in range(300, days * D, 300):
        inside = round(inside + rng.uniform(-0.15, 0.15), 2)
        outside = round(18 + 7 * math.sin(2 * math.pi * (at - phase) / D) + rng.uniform(-0.2, 0.2), 2)
        changes += [(at, INDOOR_T, str(inside), unit), (at + 1, OUTDOOR_T, str(outside), unit)]
        warmer = inside > outside
        if previous is False and warmer:
            anchors.append(at + 1)
        previous = warmer
    assert len(anchors) >= 3
    tl = with_states(timeline({}, [], days), {INDOOR_T: ("21.0", unit), OUTDOOR_T: ("15.0", unit)}, changes)
    result = run(tl, Labels([fire(at) for at in anchors]))
    note = f"The labeled moments line up with {INDOOR_T} rising above {OUTDOOR_T}; that needs a template trigger"
    assert note in top(result).explanation, top(result).explanation
    assert top(result).config["description"] == top(result).explanation
    # The note says what is needed: it is not a suggestion of its own.
    assert not any("template" in str(s.config["triggers"]) for s in result.suggestions)


def test_no_template_note_when_a_suggestion_already_fits():
    tl, anchors = boot_dryer(random.Random(38))
    result = run(tl, Labels([fire(at) for at in anchors[:3]]))
    assert "template" not in top(result).explanation
