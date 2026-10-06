"""Suggest miner: clock-like parts ranked after entity-based ones that do about as well, near-duplicates folded into variants, and kinds (#41)."""

from __future__ import annotations

import random

from custom_components.event_simulator.suggest.evaluate import Fire
from custom_components.event_simulator.suggest.exact import apply_exact, mark_approximate
from custom_components.event_simulator.suggest.kinds import candidate_kinds, structure
from custom_components.event_simulator.suggest.labels import Labels
from custom_components.event_simulator.suggest.model import (
    CalendarTrigger,
    Candidate,
    EntityTimeTrigger,
    EventTrigger,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
)
from custom_components.event_simulator.suggest.score import Stats, about_as_good, clock_parts, prefer_entities
from custom_components.event_simulator.suggest.search import MAX_CARDS, MAX_VARIANTS

from .test_suggest_search import (
    DOOR,
    HUM,
    PERSON,
    D,
    H,
    M,
    distractors,
    door_after_sunset,
    drifting_sun,
    fire,
    humidity_episodes,
    run,
    timeline,
    top,
)

KETTLE = "switch.kettle"


def morning(rng, *, kettle_days, days=10):
    """Labels at 07:00 every day; the kettle switches on half a minute before them on `kettle_days` of the days (and off ten minutes later)."""
    initial, changes = distractors(rng, days)
    initial[KETTLE] = "off"
    anchors = []
    for day in range(days):
        at = day * D + 7 * H
        if day in kettle_days:
            on = at - 30 - rng.uniform(0, 20)
            changes += [(on, KETTLE, "on"), (on + 10 * M, KETTLE, "off")]
        anchors.append(at + rng.uniform(0, 60))
    return timeline(initial, changes, days), Labels([fire(a) for a in anchors])


# Ranking: clock-like parts after entity-based ones that do about as well


def test_clock_parts_count_time_weekday_and_sun():
    assert clock_parts(Candidate((StateTrigger(DOOR, to=("on",)),))) == 0
    assert clock_parts(Candidate((TimeTrigger(7 * H),), (TimeCondition(weekdays=("mon",)),))) == 2
    assert clock_parts(Candidate((SunTrigger("sunset"),), (SunCondition(after="sunset"),))) == 2
    # A helper's time and a calendar's events are entities: they say why, not when.
    assert clock_parts(Candidate((EntityTimeTrigger("input_datetime.wake"), CalendarTrigger("calendar.trash")))) == 0
    # The sun integration's entities move with the sun.
    assert clock_parts(Candidate((StateTrigger("sun.sun", to=("above_horizon",)),), (StateCondition("sun.sun", ("below_horizon",)),))) == 2
    assert clock_parts(Candidate((EntityTimeTrigger("sensor.sun_next_noon", offset=-4 * H),))) == 1


def stats(hits, total, *, violations=0, nearby=0, outside=0, while_held=0, while_total=0):
    return Stats(
        fires=hits + violations + outside,
        anchors_hit=hits,
        anchors_total=total,
        missed=(),
        no_fire_violations=tuple(float(n) for n in range(violations)),
        outside_fires=tuple(float(n) for n in range(outside)),
        inside_fires=(),
        while_held=while_held,
        while_total=while_total,
        outside_nearby=nearby,
    )


def test_about_as_good_is_one_anchor_short_at_most_and_no_more_bad_fires():
    clock = stats(10, 10, outside=20)
    assert about_as_good(stats(10, 10, outside=20), clock)
    assert about_as_good(stats(9, 10, outside=20), clock)  # one anchor short, of ten or more
    assert not about_as_good(stats(8, 10), clock)
    assert not about_as_good(stats(6, 7), stats(7, 7))  # one of seven is too many
    assert not about_as_good(stats(10, 10, violations=1), clock)  # must-not fires are hard failures
    assert not about_as_good(stats(10, 10, nearby=1, outside=20), clock)  # nor more unlabeled fires within the labeled stretch
    assert about_as_good(stats(10, 10, outside=21), clock)  # beyond the stretch, at most one more
    assert not about_as_good(stats(10, 10, outside=22), clock)
    assert not about_as_good(stats(10, 10, while_held=1, while_total=2), stats(10, 10, while_held=2, while_total=2))


def test_prefer_entities_moves_an_entity_card_above_a_clock_card_it_does_about_as_well_as():
    clock = Candidate((TimeTrigger(7 * H),))
    kettle = Candidate((StateTrigger(KETTLE, to=("on",)),))
    noise = Candidate((StateTrigger("binary_sensor.noise", to=("on",)),))
    items = [(clock, stats(10, 10)), (noise, stats(6, 10)), (kettle, stats(9, 10))]
    ordered = prefer_entities(items, stats=lambda i: i[1], candidate=lambda i: i[0])
    assert [c for c, _ in ordered] == [kettle, clock, noise]
    # An entity card needing two conditions where the clock card needs one is not moved up.
    after_six = Candidate((StateTrigger(DOOR, to=("on",)),), (TimeCondition(after=18 * H),))
    two_noises = Candidate((StateTrigger(DOOR, to=("on",)),), (StateCondition("binary_sensor.a", ("off",)), StateCondition("binary_sensor.b", ("on",))))
    items = [(after_six, stats(10, 10)), (two_noises, stats(10, 10))]
    assert prefer_entities(items, stats=lambda i: i[1], candidate=lambda i: i[0]) == items
    # Nor one with a rougher number: "below 52.3" does not replace "before 03:00".
    before_three = Candidate((StateTrigger(DOOR, to=("on",)),), (TimeCondition(before=3 * H),))
    cold = Candidate((StateTrigger(DOOR, to=("on",)),), (NumericStateCondition("sensor.outdoor", below=52.3),))
    items = [(before_three, stats(10, 10, outside=3)), (cold, stats(10, 10))]
    assert prefer_entities(items, stats=lambda i: i[1], candidate=lambda i: i[0]) == items
    # Nothing entity-based comes close: the clock stays first.
    items = [(clock, stats(10, 10)), (noise, stats(6, 10))]
    assert prefer_entities(items, stats=lambda i: i[1], candidate=lambda i: i[0]) == items


def test_an_entity_trigger_ranks_above_a_clock_time_that_fits_as_well():
    tl, labels = morning(random.Random(41), kettle_days=set(range(10)))
    result = run(tl, labels, sun=drifting_sun(4))
    assert top(result).candidate == Candidate((StateTrigger(KETTLE, to=("on",)),))
    # The clock time is still offered.
    assert any(s.candidate.triggers == (TimeTrigger(7 * H),) for s in result.suggestions)


def test_an_entity_trigger_one_anchor_short_of_ten_still_ranks_above_the_clock():
    tl, labels = morning(random.Random(42), kettle_days=set(range(10)) - {4})
    best = top(run(tl, labels, sun=drifting_sun(4)))
    assert best.candidate == Candidate((StateTrigger(KETTLE, to=("on",)),))
    assert (best.stats.anchors_hit, best.stats.anchors_total) == (9, 10)


def test_a_clock_rule_wins_when_no_entity_explains_it():
    tl, labels = morning(random.Random(43), kettle_days={0, 2, 5, 7, 8, 9})
    best = top(run(tl, labels, sun=drifting_sun(4)))
    assert best.candidate.triggers == (TimeTrigger(7 * H),)
    assert best.stats.recall == 1.0


def evening_door(rng, days=6):
    """The door opens five times a day; the light follows it only while someone is home, which is 17:00-23:00 every day."""
    initial, changes = distractors(rng, days, count=2)
    initial.update({DOOR: "off", PERSON: "not_home"})
    anchors = []
    for day in range(days):
        changes += [(day * D + 17 * H, PERSON, "home"), (day * D + 23 * H, PERSON, "not_home")]
        for hour in (8, 11.5, 14, 18, 21):
            at = day * D + hour * H + rng.uniform(0, 20 * M)
            changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
            if hour > 17:
                anchors.append(at + rng.uniform(0, 30))
    return timeline(initial, changes, days), Labels([fire(a) for a in anchors])


def test_an_entity_condition_is_chosen_over_a_clock_window_that_does_as_well():
    tl, labels = evening_door(random.Random(44))
    result = run(tl, labels, sun=drifting_sun(4))
    best = top(result)
    assert best.candidate == Candidate((StateTrigger(DOOR, to=("on",)),), (StateCondition(PERSON, ("home",)),))
    assert best.stats.recall == 1.0 and not best.stats.outside_fires


# Folding near-duplicates


def test_structure_leaves_out_thresholds_durations_offsets_and_clock_values():
    assert structure(Candidate((NumericStateTrigger(HUM, above=70, for_=5 * M),))) == structure(Candidate((NumericStateTrigger(HUM, above=68.5, for_=4 * M),)))
    assert structure(Candidate((NumericStateTrigger(HUM, above=70),))) != structure(Candidate((NumericStateTrigger(HUM, below=70),)))
    assert structure(Candidate((NumericStateTrigger(HUM, above=70),))) != structure(Candidate((NumericStateTrigger(HUM, above=70, for_=5 * M),)))
    assert structure(Candidate((NumericStateTrigger(HUM, above=70),))) != structure(Candidate((NumericStateTrigger("sensor.other", above=70),)))
    assert structure(Candidate((TimeTrigger(7 * H),), (TimeCondition(weekdays=("mon", "tue")),))) == structure(Candidate((TimeTrigger(7 * H + 15 * M),), (TimeCondition(weekdays=("sat",)),)))
    assert structure(Candidate((SunTrigger("sunset", -30 * M),))) == structure(Candidate((SunTrigger("sunset"),)))
    assert structure(Candidate((SunTrigger("sunset"),))) != structure(Candidate((SunTrigger("sunrise"),)))
    door = StateTrigger(DOOR, to=("on",))
    assert structure(Candidate((door,), (NumericStateCondition(HUM, above=60),))) == structure(Candidate((door,), (NumericStateCondition(HUM, above=65),)))
    assert structure(Candidate((door,), (StateCondition(PERSON, ("home",)),))) != structure(Candidate((door,), (StateCondition(PERSON, ("not_home",)),)))
    assert structure(Candidate((door,), (TimeCondition(after=17 * H),))) != structure(Candidate((door,), (TimeCondition(after=17 * H, before=23 * H),)))
    assert structure(Candidate((door, StateTrigger(PERSON, to=("home",))))) == structure(Candidate((StateTrigger(PERSON, to=("home",)), door)))


def test_threshold_variants_fold_into_one_card():
    tl, labels = humidity_episodes(random.Random(2))
    result = run(tl, labels)
    structures = [structure(s.candidate) for s in result.suggestions]
    assert len(set(structures)) == len(structures)
    best = top(result)
    assert best.variants, "the numeric trigger's other thresholds and durations are variants of the top card"
    assert len(best.variants) <= MAX_VARIANTS
    for variant in best.variants:
        assert structure(variant.candidate) == structure(best.candidate)
        assert variant.candidate != best.candidate
    out = result.as_dict()["suggestions"][0]["variants"]
    assert [set(v) for v in out] == [{"config", "stats", "explanation", "exact"}] * len(out)
    assert all(v["exact"] is False for v in out)
    assert out[0]["config"]["triggers"][0]["trigger"] == "numeric_state"


def test_variants_stay_estimated_through_exact_validation():
    tl, labels = humidity_episodes(random.Random(2))
    result = run(tl, labels)
    exact = [[Fire(f, 0) for f in s.stats.fire_times] for s in result.suggestions]
    validated = apply_exact(result, exact)
    best = top(validated)
    assert best.exact is True and best.variants
    assert all(v["exact"] is False for v in best.as_dict()["variants"])
    assert top(mark_approximate(result, {"code": "x", "message": "y"})).variants == top(result).variants


# Kinds


def test_kinds_of_triggers_and_conditions():
    door = StateTrigger(DOOR, to=("on",))
    assert candidate_kinds(Candidate((door,))) == (["state"], ["state"])
    assert candidate_kinds(Candidate((StateTrigger("media_player.tv", attribute="source", to=("TV",)),))) == (["attribute"], ["attribute"])
    assert candidate_kinds(Candidate((StateTrigger(HUM),))) == (["any_change"], ["any_change"])
    assert candidate_kinds(Candidate((StateTrigger(HUM, changes_only=True),))) == (["any_change"], ["any_change"])
    assert candidate_kinds(Candidate((StateTrigger("climate.x", attribute="current_temperature"),))) == (["any_change", "attribute"], ["any_change", "attribute"])
    assert candidate_kinds(Candidate((NumericStateTrigger(HUM, above=70),), (TimeCondition(after=17 * H),))) == (["numeric", "time"], ["numeric"])
    assert candidate_kinds(Candidate((TimeTrigger(7 * H, weekdays=("mon",)),))) == (["time", "weekday"], ["time", "weekday"])
    assert candidate_kinds(Candidate((TimeTrigger(7 * H),), (TimeCondition(weekdays=("mon",)),))) == (["time", "weekday"], ["time"])
    assert candidate_kinds(Candidate((EntityTimeTrigger("input_datetime.wake"),))) == (["entity_time"], ["entity_time"])
    assert candidate_kinds(Candidate((SunTrigger("sunset"),), (StateCondition(PERSON, ("home",)),))) == (["state", "sun"], ["sun"])
    assert candidate_kinds(Candidate((CalendarTrigger("calendar.trash"),), (SunCondition(after="sunset"),))) == (["calendar", "sun"], ["calendar"])
    assert candidate_kinds(Candidate((EventTrigger("zha_event"),), (NumericStateCondition(HUM, below=50),))) == (["event", "numeric"], ["event"])
    # Two triggers with one id are an OR; a start/end pair is not.
    assert candidate_kinds(Candidate((door, StateTrigger(PERSON, to=("home",))))) == (["or", "state"], ["or", "state"])
    pair = Candidate((StateTrigger(DOOR, to=("on",), id="start"), StateTrigger(DOOR, to=("off",), id="end")))
    assert candidate_kinds(pair) == (["state"], ["state"])


def test_every_suggestion_carries_kinds_helpers_and_the_result_counts_them():
    tl, labels, sun = door_after_sunset(random.Random(1))
    result = run(tl, labels, sun=sun)
    out = result.as_dict()
    counts: dict[str, int] = {}
    for suggestion in out["suggestions"]:
        assert suggestion["helpers"] == []
        assert set(suggestion["trigger_kinds"]) <= set(suggestion["kinds"])
        assert suggestion["kinds"] == sorted(suggestion["kinds"])
        for kind in suggestion["kinds"]:
            counts[kind] = counts.get(kind, 0) + 1
    assert out["kind_counts"] == counts


def test_every_trigger_kind_found_gets_a_few_cards_beyond_the_top_k():
    tl, labels, sun = door_after_sunset(random.Random(1))
    wide = run(tl, labels, sun=sun, top_k=MAX_CARDS)
    found = {kind for s in wide.suggestions for kind in s.trigger_kinds}
    assert len(found) >= 2
    narrow = run(tl, labels, sun=sun, top_k=1)
    assert narrow.suggestions[0].candidate == wide.suggestions[0].candidate
    for kind in found:
        have = [s for s in narrow.suggestions if kind in s.trigger_kinds]
        assert have, kind
        assert len(have) >= min(2, sum(kind in s.trigger_kinds for s in wide.suggestions))
    assert len(narrow.suggestions) <= MAX_CARDS
    assert [s.rank for s in narrow.suggestions] == list(range(len(narrow.suggestions)))
