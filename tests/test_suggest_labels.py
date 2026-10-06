"""Suggest miner: labeled periods, anchors and tolerance."""

from __future__ import annotations

import pytest

from custom_components.event_simulator.suggest.labels import Anchor, Label, Labels, Tolerance
from custom_components.event_simulator.suggest.timeline import to_ts

T0 = to_ts("2026-10-04T00:00:00+00:00")
H = 3600


def lbl(start_h, end_h, kind="fire", anchor="start"):
    return Label(T0 + start_h * H, T0 + end_h * H, kind, anchor)


def test_label_from_dict_parses_times_and_validates():
    label = Label.from_dict({"start": "2026-10-04T01:00:00Z", "end": "2026-10-04T02:00:00+00:00", "kind": "fire", "anchor": "both"})
    assert label == Label(T0 + H, T0 + 2 * H, "fire", "both")
    assert Label.from_dict({"start": T0, "end": T0 + 1, "kind": "no_fire"}).anchor is None
    assert Label.from_dict({"start": T0, "end": T0 + 1, "kind": "fire"}).anchor == "start"
    for bad in (
        {"start": T0, "end": T0, "kind": "fire"},
        {"start": T0, "end": T0 + 1, "kind": "maybe"},
        {"start": T0, "end": T0 + 1, "kind": "fire", "anchor": "middle"},
    ):
        with pytest.raises(ValueError):
            Label.from_dict(bad)


def test_anchors_from_each_anchor_kind():
    labels = Labels([lbl(1, 2, anchor="start"), lbl(3, 4, anchor="end"), lbl(5, 6, anchor="both"), lbl(7, 8, anchor="while"), lbl(9, 10, "no_fire")])
    assert [(a.at, a.edge, a.label) for a in labels.anchors] == [
        (T0 + 1 * H, "start", 0),
        (T0 + 4 * H, "end", 1),
        (T0 + 5 * H, "start", 2),
        (T0 + 6 * H, "end", 2),
    ]
    assert labels.while_periods == [labels.labels[3]]
    assert labels.no_fire_periods == [labels.labels[4]]


def test_tolerance_is_two_minutes_before_ten_after_inclusive_and_configurable():
    anchor = Anchor(T0, "start", 0)
    tolerance = Tolerance()
    assert (tolerance.before, tolerance.after) == (120, 600)
    assert anchor.matches(T0 - 120, tolerance)
    assert not anchor.matches(T0 - 121, tolerance)
    assert anchor.matches(T0 + 600, tolerance)
    assert not anchor.matches(T0 + 601, tolerance)
    wide = Tolerance(before=600, after=1800)
    assert anchor.matches(T0 - 500, wide) and anchor.matches(T0 + 1700, wide)


def test_start_and_end_trigger_ids_only_match_their_own_edge():
    labels = Labels([lbl(1, 1.1, anchor="both")])  # 6 minutes long: tolerances overlap
    t = T0 + H + 300
    assert {a.edge for a in labels.anchors_matching(t)} == {"start", "end"}
    assert {a.edge for a in labels.anchors_matching(t, trigger_id="start")} == {"start"}
    assert {a.edge for a in labels.anchors_matching(t, trigger_id="end")} == {"end"}
    # Any other id (or none) matches either edge.
    assert len(labels.anchors_matching(t, trigger_id="door")) == 2


def test_period_at_prefers_must_not_over_should_fire():
    labels = Labels([lbl(1, 3), lbl(2, 4, "no_fire")])
    assert labels.period_at(T0) is None
    assert labels.period_at(T0 + 1 * H).kind == "fire"
    assert labels.period_at(T0 + 2.5 * H).kind == "no_fire"
    assert labels.period_at(T0 + 3.5 * H).kind == "no_fire"
    assert labels.period_at(T0 + 4 * H) is None  # half-open


def test_classify_a_fire():
    labels = Labels([lbl(1, 2, anchor="start"), lbl(5, 6, "no_fire"), lbl(8, 9, anchor="while")])
    assert labels.classify(T0 + H + 30) == "anchor"
    assert labels.classify(T0 + H - 60) == "anchor"  # before the period, inside tolerance
    assert labels.classify(T0 + 1.5 * H) == "inside"  # in a should-fire period, not near its anchor
    assert labels.classify(T0 + 8.5 * H) == "inside"
    assert labels.classify(T0 + 5.5 * H) == "no_fire"
    assert labels.classify(T0 + 3 * H) == "unlabeled"
    assert labels.is_unlabeled(T0 + 3 * H)
    assert not labels.is_unlabeled(T0 + H - 60)
    # A must-not period wins even over an anchor's tolerance.
    clash = Labels([lbl(1, 2), lbl(0.9, 1, "no_fire")])
    assert clash.classify(T0 + H - 60) == "no_fire"


def test_unlabeled_intervals_are_the_complement_of_periods_and_anchor_tolerance():
    labels = Labels([lbl(1, 2, anchor="end"), lbl(5, 6, "no_fire")])
    assert labels.unlabeled_intervals(T0, T0 + 10 * H) == [
        (T0, T0 + H),
        (T0 + 2 * H + 600, T0 + 5 * H),
        (T0 + 6 * H, T0 + 10 * H),
    ]
    assert Labels([]).unlabeled_intervals(T0, T0 + H) == [(T0, T0 + H)]


def test_labels_from_dicts_with_tolerance_and_lookup_on_many_anchors():
    labels = Labels.from_dicts(
        [{"start": T0 + d * 86_400 + 7 * H, "end": T0 + d * 86_400 + 8 * H, "kind": "fire"} for d in range(30)],
        Tolerance(before=0, after=60),
    )
    assert len(labels.anchors) == 30
    assert labels.anchors_matching(T0 + 17 * 86_400 + 7 * H + 30)[0].label == 17
    assert labels.anchors_matching(T0 + 17 * 86_400 + 7 * H - 1) == []


def test_classify_on_many_overlapping_periods_agrees_with_a_scan():
    """`classify` looks periods up by binary search (the benchmark labels 500 fires, and candidates fire 30 000 times); overlapping and nested periods must still be found."""
    import random

    rng = random.Random(5)
    raw = []
    for _ in range(300):
        start = rng.uniform(0, 48) * H
        raw.append(Label(T0 + start, T0 + start + rng.choice((1, 60, 600, 3 * H)), rng.choice(("fire", "no_fire")), "start"))
    labels = Labels(raw)

    def scan(t):
        if any(p.contains(t) for p in labels.no_fire_periods):
            return "no_fire"
        if labels.anchors_matching(t):
            return "anchor"
        return "inside" if any(p.contains(t) for p in labels.fire_periods) else "unlabeled"

    probes = [T0 + rng.uniform(-H, 52 * H) for _ in range(2000)] + [p.start for p in raw] + [p.end for p in raw]
    assert [labels.classify(t) for t in probes] == [scan(t) for t in probes]
