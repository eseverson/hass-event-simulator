"""How a candidate did on the labels, and the order suggestions are ranked in.

Every fire is sorted into one bucket (see `Labels.classify`): matching an anchor, inside a must-not period (a hard failure), inside a should-fire period away from its anchors (neutral), or in unlabeled time (soft evidence against, and listed so the user can label it). The ranking follows the spec: anchor recall first (all anchors hit beats most), then `while` periods held, then fires inside must-not periods, then fires in unlabeled time, then simplicity (fewer conditions, no `for:`, round thresholds and times).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .evaluate import Evaluator, Fire
from .labels import Anchor, Labels
from .model import (
    Candidate,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
)
from .timeline import to_iso


@dataclass(frozen=True, slots=True)
class Stats:
    fires: int
    anchors_hit: int
    anchors_total: int
    missed: tuple[Anchor, ...]
    no_fire_violations: tuple[float, ...]
    outside_fires: tuple[float, ...]
    inside_fires: tuple[float, ...]
    while_held: int = 0
    while_total: int = 0
    fire_times: tuple[float, ...] = ()  # every fire, in order (the panel draws them)

    @property
    def recall(self) -> float:
        """Share of anchors hit; 1.0 when there are none."""
        return self.anchors_hit / self.anchors_total if self.anchors_total else 1.0

    @property
    def while_ratio(self) -> float:
        return self.while_held / self.while_total if self.while_total else 1.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready, times as ISO UTC (the websocket's `stats`)."""
        return {
            "fires": self.fires,
            "anchors_hit": self.anchors_hit,
            "anchors_total": self.anchors_total,
            "missed": [{"at": to_iso(a.at), "edge": a.edge, "label": a.label} for a in self.missed],
            "no_fire_violations": [to_iso(t) for t in self.no_fire_violations],
            "outside_fires": [to_iso(t) for t in self.outside_fires],
            "inside_fires": [to_iso(t) for t in self.inside_fires],
            "while_held": self.while_held,
            "while_total": self.while_total,
            "fire_times": [to_iso(t) for t in self.fire_times],
        }


def score_candidate(
    evaluator: Evaluator,
    candidate: Candidate,
    labels: Labels,
    fires: list[Fire] | None = None,
) -> Stats:
    """The candidate's stats on `labels` (pass `fires` when already computed)."""
    if fires is None:
        fires = evaluator.fires(candidate)
    return score_fires(evaluator, candidate, labels, fires)


def score_fires(evaluator: Evaluator, candidate: Candidate, labels: Labels, fires: Sequence[Fire]) -> Stats:
    """The stats of a list of fires on `labels`: what the miner's approximate fires and the sandbox's exact ones both go through.

    `fires` are time-ordered, each naming the index of the trigger that fired (its id decides which anchors it can match). `while` periods are judged on the candidate's conditions over the timeline, which needs no fire times.
    """
    hit: set[Anchor] = set()
    violations: list[float] = []
    outside: list[float] = []
    inside: list[float] = []
    for fire in fires:
        trigger_id = candidate.triggers[fire.trigger].id
        kind = labels.classify(fire.at, trigger_id)
        if kind == "anchor":
            hit.update(labels.anchors_matching(fire.at, trigger_id))
        elif kind == "no_fire":
            violations.append(fire.at)
        elif kind == "inside":
            inside.append(fire.at)
        else:
            outside.append(fire.at)
    held = sum(
        evaluator.holds_throughout(candidate.conditions, label.start, label.end) for label in labels.while_periods
    )
    return Stats(
        fires=len(fires),
        anchors_hit=len(hit),
        anchors_total=len(labels.anchors),
        missed=tuple(a for a in labels.anchors if a not in hit),
        no_fire_violations=tuple(violations),
        outside_fires=tuple(outside),
        inside_fires=tuple(inside),
        while_held=held,
        while_total=len(labels.while_periods),
        fire_times=tuple(fire.at for fire in fires),
    )


def roundness(value: float) -> int:
    """0 for a multiple of 10, then 5, 1, 0.5, 0.1; 5 for anything finer."""
    for rank, step in enumerate((10, 5, 1, 0.5, 0.1)):
        ratio = value / step
        if abs(ratio - round(ratio)) < 1e-9:
            return rank
    return 5


def clock_roundness(seconds: float) -> int:
    """0 for a multiple of 15 minutes, then 5 minutes, 1 minute; 3 for anything finer."""
    for rank, step in enumerate((900, 300, 60)):
        if abs(seconds) % step < 1e-6:
            return rank
    return 3


def simplicity(candidate: Candidate) -> tuple[int, int, int, int]:
    """Lower is simpler: (conditions, `for:` options, roughness of thresholds and times, triggers)."""
    fors = 0
    rough = 0
    for part in (*candidate.triggers, *candidate.conditions):
        match part:
            case StateTrigger() | StateCondition():
                if part.for_:
                    fors += 1
                    rough += clock_roundness(part.for_)
            case NumericStateTrigger() | NumericStateCondition():
                rough += sum(roundness(v) for v in (part.above, part.below) if v is not None)
                if getattr(part, "for_", 0):
                    fors += 1
                    rough += clock_roundness(part.for_)
            case TimeTrigger():
                rough += clock_roundness(part.at)
            case TimeCondition():
                rough += sum(clock_roundness(v) for v in (part.after, part.before) if v is not None)
            case SunTrigger():
                rough += clock_roundness(part.offset)
            case SunCondition():
                rough += clock_roundness(part.after_offset) + clock_roundness(part.before_offset)
    return (len(candidate.conditions), fors, rough, len(candidate.triggers))


def rank_key(stats: Stats, candidate: Candidate) -> tuple[Any, ...]:
    """Sort key, best first: recall, while periods held, must-not fires, unlabeled fires, simplicity."""
    return (
        -stats.recall,
        -stats.while_ratio,
        len(stats.no_fire_violations),
        len(stats.outside_fires),
        simplicity(candidate),
    )
