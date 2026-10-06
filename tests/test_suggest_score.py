"""Suggest miner: stats against labels and the ranking order."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.event_simulator.suggest.evaluate import Evaluator
from custom_components.event_simulator.suggest.labels import Label, Labels
from custom_components.event_simulator.suggest.model import (
    Candidate,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
)
from custom_components.event_simulator.suggest.score import Stats, rank_key, roundness, score_candidate, simplicity
from custom_components.event_simulator.suggest.timeline import Timeline, to_ts

START = "2026-10-04T00:00:00+00:00"
T0 = to_ts(START)
H = 3600
DOOR = "binary_sensor.door"


def evaluator(changes, initial=None):
    events = [
        {"at": datetime.fromtimestamp(T0 + at, UTC).isoformat(), "entity_id": eid, "state": state, "attributes": {}}
        for at, eid, state in changes
    ]
    initial = {eid: {"state": s, "attributes": {}} for eid, s in (initial or {}).items()}
    return Evaluator(Timeline.from_scenario(initial, events, START, T0 + 86_400), time_zone="UTC")


def label(start_h, end_h, kind="fire", anchor="start"):
    return Label(T0 + start_h * H, T0 + end_h * H, kind, anchor)


DOOR_DAY = [(1 * H, DOOR, "on"), (2 * H, DOOR, "off"), (5 * H, DOOR, "on"), (6 * H, DOOR, "off"), (9 * H, DOOR, "on"), (9.5 * H, DOOR, "off"), (12 * H, DOOR, "on"), (12.2 * H, DOOR, "off")]


def test_stats_sort_every_fire_into_anchor_violation_inside_or_outside():
    ev = evaluator(DOOR_DAY, {DOOR: "off"})
    labels = Labels([label(1, 2, anchor="both"), label(5, 7, anchor="start"), label(9, 10, "no_fire"), label(20, 21, anchor="end")])
    on_off = Candidate((StateTrigger(DOOR, to=("on",), id="start"), StateTrigger(DOOR, to=("off",), id="end")))

    stats = score_candidate(ev, on_off, labels)

    assert stats.fires == 8
    assert (stats.anchors_hit, stats.anchors_total) == (3, 4)
    assert [(a.at - T0, a.edge) for a in stats.missed] == [(21 * H, "end")]
    assert [t - T0 for t in stats.no_fire_violations] == [9 * H, 9.5 * H]
    assert [t - T0 for t in stats.inside_fires] == [6 * H]  # the off inside a start-anchored period
    assert [t - T0 for t in stats.outside_fires] == [12 * H, 12.2 * H]
    assert stats.recall == 0.75


def test_trigger_ids_keep_start_fires_off_end_anchors():
    ev = evaluator([(1 * H, DOOR, "on"), (1 * H + 60, DOOR, "off")], {DOOR: "off"})
    labels = Labels([label(1, 1 + 1 / 60, anchor="both")])  # one minute long
    only_start = Candidate((StateTrigger(DOOR, to=("on",), id="start"),))
    stats = score_candidate(ev, only_start, labels)
    assert (stats.anchors_hit, stats.anchors_total) == (1, 2)
    unnamed = Candidate((StateTrigger(DOOR, to=("on",)),))
    assert score_candidate(ev, unnamed, labels).anchors_hit == 2  # one fire within both tolerances


def test_while_labels_count_periods_where_the_conditions_hold_throughout():
    ev = evaluator([(3 * H, DOOR, "on"), (4 * H, DOOR, "off"), (5 * H, "person.me", "away")], {DOOR: "off", "person.me": "home"})
    labels = Labels([label(3, 4, anchor="start"), label(2, 4, anchor="while"), label(4, 6, anchor="while")])
    home = Candidate((StateTrigger(DOOR, to=("on",)),), (StateCondition("person.me", ("home",)),))
    stats = score_candidate(ev, home, labels)
    assert (stats.while_held, stats.while_total) == (1, 2)


def test_as_dict_is_json_ready():
    ev = evaluator(DOOR_DAY, {DOOR: "off"})
    stats = score_candidate(ev, Candidate((StateTrigger(DOOR, to=("on",)),)), Labels([label(1, 2), label(9, 10, "no_fire"), label(22, 23)]))
    out = stats.as_dict()
    assert out["anchors_hit"] == 1 and out["anchors_total"] == 2
    assert out["missed"] == [{"at": "2026-10-04T22:00:00+00:00", "edge": "start", "label": 2}]
    assert out["no_fire_violations"] == ["2026-10-04T09:00:00+00:00"]
    assert out["outside_fires"] == ["2026-10-04T05:00:00+00:00", "2026-10-04T12:00:00+00:00"]


def test_roundness_and_simplicity():
    assert [roundness(v) for v in (70, 75, 72, 72.5, 72.3, 72.25)] == [0, 1, 2, 3, 4, 5]
    assert roundness(0) == 0
    plain = Candidate((StateTrigger(DOOR, to=("on",)),))
    with_for = Candidate((StateTrigger(DOOR, to=("on",), for_=300),))
    with_condition = plain.with_condition(TimeCondition(after=18 * H))
    assert simplicity(plain) < simplicity(with_for) < simplicity(with_condition)
    assert simplicity(Candidate((NumericStateTrigger("sensor.h", above=70),))) < simplicity(Candidate((NumericStateTrigger("sensor.h", above=71.3),)))
    assert simplicity(Candidate((TimeTrigger(7 * H),))) < simplicity(Candidate((TimeTrigger(7 * H + 7 * 60),)))
    assert simplicity(Candidate((SunTrigger("sunset", offset=-1800),))) < simplicity(Candidate((SunTrigger("sunset", offset=-1745),)))


def stats(hit=4, total=4, violations=0, outside=0, while_held=0, while_total=0):
    return Stats(
        fires=hit + violations + outside,
        anchors_hit=hit,
        anchors_total=total,
        missed=(),
        no_fire_violations=tuple(range(violations)),
        outside_fires=tuple(range(outside)),
        inside_fires=(),
        while_held=while_held,
        while_total=while_total,
    )


def test_rank_order_is_recall_then_violations_then_outside_then_simplicity():
    simple = Candidate((StateTrigger(DOOR, to=("on",)),))
    complex_ = simple.with_condition(TimeCondition(after=18 * H)).with_condition(TimeCondition(before=23 * H))
    ranked = sorted(
        [
            ("most hit, clean", stats(hit=3), simple),
            ("all hit, one violation", stats(violations=1), simple),
            ("all hit, many outside", stats(outside=9), simple),
            ("all hit, clean, complex", stats(), complex_),
            ("all hit, clean, simple", stats(), simple),
            ("all hit, one outside", stats(outside=1), simple),
        ],
        key=lambda item: rank_key(item[1], item[2]),
    )
    assert [name for name, _, _ in ranked] == [
        "all hit, clean, simple",
        "all hit, clean, complex",
        "all hit, one outside",
        "all hit, many outside",
        "all hit, one violation",
        "most hit, clean",
    ]
    # Without anchors (only while labels), recall is moot and while periods decide.
    assert rank_key(stats(0, 0, while_held=2, while_total=2), simple) < rank_key(stats(0, 0, while_held=1, while_total=2), simple)
