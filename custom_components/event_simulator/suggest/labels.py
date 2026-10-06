"""Labeled periods and the anchors they yield.

A label is `{start, end, kind: "fire" | "no_fire", anchor: "start" | "end" | "both" | "while"}` (times ISO or POSIX seconds). A should-fire label anchored at its start and/or end yields one anchor per edge; a fire *matches* an anchor when it lands within the tolerance around it (default 2 min before to 10 min after, both ends inclusive). A trigger with id `start` or `end` (the on/off pair a `both` label asks for) only matches anchors of its own edge. A `while` label yields no anchor: it marks a period the automation may act in, whose conditions should hold throughout. A must-not label makes any fire inside it a hard failure, even one within an anchor's tolerance. The labels' `span` runs from the first labeled moment to the last: unlabeled time inside it is time the user looked at and left unlabeled, unlabeled time beyond it may simply not have been looked at.

Periods are half-open `[start, end)`.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .timeline import to_ts

KINDS = ("fire", "no_fire")
ANCHORS = ("start", "end", "both", "while")
EDGE_IDS = ("start", "end")

Kind = Literal["fire", "no_fire"]
Classification = Literal["anchor", "no_fire", "inside", "unlabeled"]


@dataclass(frozen=True, slots=True)
class Tolerance:
    """How far a fire may land from an anchor and still match it, in seconds."""

    before: float = 120
    after: float = 600


@dataclass(frozen=True, slots=True)
class Label:
    start: float
    end: float
    kind: Kind
    anchor: str | None = "start"  # None on a must-not label

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("A label must end after it starts")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        if self.kind == "fire" and self.anchor not in ANCHORS:
            raise ValueError(f"anchor must be one of {ANCHORS}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Label:
        kind = data.get("kind")
        anchor = None if kind == "no_fire" else data.get("anchor") or "start"
        try:
            start, end = to_ts(data["start"]), to_ts(data["end"])
        except (KeyError, TypeError) as err:
            raise ValueError(f"A label needs a start and an end: {err}") from err
        return cls(start, end, kind, anchor)

    def contains(self, t: float) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True, slots=True)
class Anchor:
    """A time a should-fire label wants a fire at: its start or end edge."""

    at: float
    edge: Literal["start", "end"]
    label: int  # index into Labels.labels

    def matches(self, t: float, tolerance: Tolerance, trigger_id: str | None = None) -> bool:
        if trigger_id in EDGE_IDS and trigger_id != self.edge:
            return False
        return self.at - tolerance.before <= t <= self.at + tolerance.after


class Labels:
    """A set of labels with lookups by time."""

    def __init__(self, labels: Iterable[Label], tolerance: Tolerance | None = None) -> None:
        self.labels = list(labels)
        self.tolerance = tolerance or Tolerance()
        anchors = []
        for n, label in enumerate(self.labels):
            if label.kind != "fire":
                continue
            if label.anchor in ("start", "both"):
                anchors.append(Anchor(label.start, "start", n))
            if label.anchor in ("end", "both"):
                anchors.append(Anchor(label.end, "end", n))
        self.anchors = sorted(anchors, key=lambda a: (a.at, a.label))
        self._anchor_times = [a.at for a in self.anchors]
        self.fire_periods = [label for label in self.labels if label.kind == "fire"]
        self.no_fire_periods = [label for label in self.labels if label.kind == "no_fire"]
        self.while_periods = [label for label in self.fire_periods if label.anchor == "while"]
        # The labeled stretch: from the first labeled moment to the last (anchors with their tolerance, and every period).
        edges = [(label.start, label.end) for label in self.labels] + [
            (a.at - self.tolerance.before, a.at + self.tolerance.after) for a in self.anchors
        ]
        self.span = (min(lo for lo, _ in edges), max(hi for _, hi in edges)) if edges else None

    @classmethod
    def from_dicts(cls, labels: Iterable[Mapping[str, Any]], tolerance: Tolerance | None = None) -> Labels:
        return cls([Label.from_dict(label) for label in labels], tolerance)

    def anchors_matching(self, t: float, trigger_id: str | None = None) -> list[Anchor]:
        """The anchors a fire at `t` (from a trigger with `trigger_id`) matches."""
        lo = bisect_left(self._anchor_times, t - self.tolerance.after)
        hi = bisect_right(self._anchor_times, t + self.tolerance.before)
        return [a for a in self.anchors[lo:hi] if a.matches(t, self.tolerance, trigger_id)]

    def period_at(self, t: float) -> Label | None:
        """The labeled period `t` falls in; a must-not period wins over a should-fire one."""
        found = None
        for label in self.labels:
            if label.contains(t):
                if label.kind == "no_fire":
                    return label
                found = found or label
        return found

    def classify(self, t: float, trigger_id: str | None = None) -> Classification:
        """What a fire at `t` counts as: inside a must-not period, matching an anchor, inside a should-fire period away from its anchors, or in unlabeled time."""
        if any(label.contains(t) for label in self.no_fire_periods):
            return "no_fire"
        if self.anchors_matching(t, trigger_id):
            return "anchor"
        if any(label.contains(t) for label in self.fire_periods):
            return "inside"
        return "unlabeled"

    def in_span(self, t: float) -> bool:
        """`t` is within the labeled stretch: the user looked here, so an unlabeled fire here is evidence against a candidate. Beyond it, an unlabeled fire is only a hint to label."""
        return self.span is not None and self.span[0] <= t <= self.span[1]

    def is_unlabeled(self, t: float) -> bool:
        """`t` is in no labeled period and outside every anchor's tolerance."""
        return self.classify(t) == "unlabeled"

    def unlabeled_intervals(self, start: float, end: float) -> list[tuple[float, float]]:
        """The parts of `[start, end)` that are unlabeled (see `is_unlabeled`), in order."""
        covered = [(label.start, label.end) for label in self.labels]
        covered += [(a.at - self.tolerance.before, a.at + self.tolerance.after) for a in self.anchors]
        covered.sort()
        gaps = []
        cursor = start
        for lo, hi in covered:
            if hi <= cursor:
                continue
            if lo > cursor:
                gaps.append((cursor, min(lo, end)))
            cursor = max(cursor, hi)
            if cursor >= end:
                break
        if cursor < end:
            gaps.append((cursor, end))
        return [(lo, hi) for lo, hi in gaps if hi > lo]
