"""How a candidate did on the labels, and the order suggestions are ranked in.

Every fire is sorted into one bucket (see `Labels.classify`): matching an anchor, inside a must-not period (a hard failure), inside a should-fire period away from its anchors (neutral), or in unlabeled time (soft evidence against, and listed so the user can label it). The ranking follows the spec: anchor recall first (all anchors hit beats most), then `while` periods held, then fires inside must-not periods, then fires in unlabeled time within the labeled stretch, then simplicity (fewer conditions, no `for:`, round thresholds and times), then unlabeled fires beyond the stretch.

Clock-like parts come last among near equals (#41): a time of day, a weekday or the sun fits almost any few labels, and says when rather than why. After the ranking, `prefer_entities` moves a candidate with fewer clock-like parts above one with more that it does `about_as_good` as. A clock time that nothing else explains about as well keeps its place.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .evaluate import Evaluator, Fire
from .labels import Anchor, Labels
from .model import (
    CalendarTrigger,
    Candidate,
    DerivativeTrigger,
    EntityTimeTrigger,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
    TrendTrigger,
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
    outside_nearby: int = 0  # outside fires within the labeled stretch (`Labels.span`), the ones that count against the candidate

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
        outside_nearby=sum(labels.in_span(t) for t in outside),
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


def extra_triggers(candidate: Candidate) -> int:
    """Triggers beyond one per trigger id: the parts of an OR (a start/end pair is one trigger per side)."""
    return len(candidate.triggers) - len({trigger.id for trigger in candidate.triggers})


def simplicity(candidate: Candidate) -> tuple[int, int, int, int]:
    """Lower is simpler: (conditions, extra triggers of an OR and helpers to create, `for:` options, roughness of thresholds and times, triggers).

    Each extra trigger of an OR costs as much as a condition: "door opens or motion" is no simpler than "door opens, only when someone is home". So does each Trend or Derivative helper the user must create first (#42): a level that explains the labels as well stays ahead of a rate.
    """
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
            case EntityTimeTrigger():
                # One more than a clock time as round: when a helper's time and a clock time fire alike (the helper never changed), the clock time reads simpler.
                rough += clock_roundness(part.offset) + 1
            case TimeCondition():
                rough += sum(clock_roundness(v) for v in (part.after, part.before) if v is not None)
            case SunTrigger() | CalendarTrigger():
                rough += clock_roundness(part.offset)
            case SunCondition():
                rough += clock_roundness(part.after_offset) + clock_roundness(part.before_offset)
            case TrendTrigger():
                rough += roundness(abs(part.delta)) + clock_roundness(part.window)
            case DerivativeTrigger():
                rough += roundness(abs(part.above if part.above is not None else part.below)) + clock_roundness(part.window)
    helpers = len({trigger.helper_key for trigger in candidate.helper_triggers})
    return (len(candidate.conditions) + extra_triggers(candidate) + helpers, fors, rough, len(candidate.triggers))


def rank_key(stats: Stats, candidate: Candidate) -> tuple[Any, ...]:
    """Sort key, best first: recall, while periods held, must-not fires, unlabeled fires within the labeled stretch, simplicity.

    Unlabeled fires beyond the labeled stretch only break ties (`search.tie_break`): a few labels say little about the rest of the window, and a condition that only excludes it fits the labels rather than the rule (the real-data benchmark, docs/benchmarks/). They are still listed as hints to label.
    """
    return (
        -stats.recall,
        -stats.while_ratio,
        len(stats.no_fire_violations),
        stats.outside_nearby,
        simplicity(candidate),
    )


SUN_ENTITIES = ("sun.sun", "sensor.sun_")  # the sun integration's entity and its sensors (next rising, next noon, ...): they move with the sun


def _follows_the_sun(part: Any) -> bool:
    entity_id = getattr(part, "entity_id", None)
    return isinstance(entity_id, str) and (entity_id == SUN_ENTITIES[0] or entity_id.startswith(SUN_ENTITIES[1]))


def clock_parts(candidate: Candidate) -> int:
    """Clock-like triggers and conditions: a time of day, a weekday, the sun (which moves like a clock), or a part on the sun integration's entities (`sun.sun` above the horizon is sunrise). A time at any other entity and a calendar's events are not: they name what the time comes from."""
    return sum(
        isinstance(part, (TimeTrigger, TimeCondition, SunTrigger, SunCondition)) or _follows_the_sun(part)
        for part in (*candidate.triggers, *candidate.conditions)
    )


def about_as_good(stats: Stats, clock: Stats) -> bool:
    """Whether a candidate with these `stats` explains the labels about as well as one with `clock`'s, so it may rank above it for having fewer clock-like parts.

    About as well: it hits as many anchors, or one fewer when there are ten or more (90 % of them); holds as many `while` periods; and fires no more often in must-not periods, nor in unlabeled time within the labeled stretch, and at most once more in unlabeled time overall (a camera's motion that happens to meet a sunrise label, and fires on a few more mornings, does not outrank the sunrise).
    """
    short = 1 if stats.anchors_total >= 10 else 0
    return (
        stats.anchors_hit >= clock.anchors_hit - short
        and stats.while_held >= clock.while_held
        and len(stats.no_fire_violations) <= len(clock.no_fire_violations)
        and stats.outside_nearby <= clock.outside_nearby
        and len(stats.outside_fires) <= len(clock.outside_fires) + 1
    )


T = TypeVar("T")
PREFER_WINDOW = 200  # how far down a ranked list `prefer_entities` looks for a candidate to move up


def prefer_entities(items: Sequence[T], *, stats: Callable[[T], Stats], candidate: Callable[[T], Candidate]) -> list[T]:
    """`items` (ranked best first) with clock-like candidates after those with fewer clock-like parts that do `about_as_good`.

    Greedy: the next item out is the first left, unless it has clock-like parts and one further down (within `PREFER_WINDOW`) with fewer does about as well and is no less simple (`simplicity`: no more conditions or extra triggers, `for:` options, or rough numbers; an entity part in place of a clock part, not two, and not "below 52.3" for "before 03:00"); then the first such goes out and the clock-like one is looked at again.
    """
    left = [(item, clock_parts(c := candidate(item)), simplicity(c)) for item in items]
    out: list[T] = []
    while left:
        first, clocks, parts = left[0]
        chosen = 0
        if clocks:
            first_stats = stats(first)
            for n in range(1, min(len(left), PREFER_WINDOW)):
                item, fewer, size = left[n]
                if fewer < clocks and size <= parts and about_as_good(stats(item), first_stats):
                    chosen = n
                    break
        out.append(left.pop(chosen)[0])
    return out
