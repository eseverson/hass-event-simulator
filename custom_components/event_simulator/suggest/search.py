"""The miner's search: candidate automations that explain the labels, ranked, with hints from outside the labels.

`suggest(timeline, labels, time_zone=..., sun_events=..., options=...)` returns a `SuggestResult`: the top-K `Suggestion`s (candidate, `Stats`, rendered config, plain-language explanation) and `Hint`s (outside fires of the top suggestions, and recurrences of the labeled transitions elsewhere in the window, each with a similarity to the labeled moments).

How it searches (see docs/superpowers/specs/2026-10-05-suggestions-design.md):

1. Sides. A label anchored at `both` asks for an on/off pair, so the start and end anchors are searched independently (trigger ids `start` / `end`) and combined. Otherwise one side covers every anchor and triggers carry no id. Labels that are only `while` periods also anchor at each period's start: the automation should act when the period begins, with conditions that hold throughout it.
2. Trigger candidates around the anchors, each evaluated over the whole window:
   - `state`: every state change near an anchor (to, and from + to), attribute changes when the state is static, and `for:` when a state consistently began a similar lag before the anchors (quantized to friendly durations). Anchors that follow changes in two or three directions also get one trigger listing them all (a toggle). A free-form entity (`FREE_FORM` or more values in the window, most seen once: a timestamp, a counter, free text) only offers values it took more than once, and a trigger on any change of its state (`to: null`).
   - `numeric_state`: `above` / `below`, the threshold searched over the full window's values. The behavior of a threshold only changes at observed values, so each gap between consecutive distinct values is one candidate, represented by its roundest number; gaps are scored by how many anchors a crossing hits and how often the value crosses elsewhere. `for:` as for state. And a state trigger with no from/to (every report, as blueprint controllers re-evaluate), with `to: null` too when some reports change attributes only, when the anchors line up with its writes (within `ALIGN` seconds, more often than chance: any chatty sensor has some write within the tolerance of every anchor). A numeric attribute that changes while the state stays put gets the same trigger on the attribute. Conditions at fire time (`numeric_state` above/below) then narrow it, the template-free form of "every report while it is warm".
   - `time` / `sun`: when the anchors cluster by local clock time, or by sunrise/sunset offset; whichever clusters more anchors with less spread wins. The time is chosen at or just before the cluster and rounded.
   Triggers that hit fewer than `min_recall` of the best trigger's anchors are dropped before any condition search, and only the best `beam` go on.
   When no single trigger hits every anchor of a side, ORs of up to `max_triggers` triggers (possibly on different entities or of different kinds) join them: greedy set cover from each of the best `union_seeds` triggers, each step adding the trigger, searched again around the anchors still missed, that hits the most of them with the fewest bad fires. A trigger joins only as evidence, not chance: it hits the missed anchors more often than its fires would at random (binomial tail at `union_alpha`), and one fitted to a single moment (a clock time, a threshold, a `for:`) never joins for one anchor. The parts of an OR keep their side's trigger id, so a start/end pair stays a pair; each extra part costs as much simplicity as a condition.
3. Conditions, greedily: from each trigger (or trigger pair), add the condition that removes the most bad fires (must-not first, then unlabeled within the labeled stretch, then beyond it) while keeping every anchor hit and every `while` period held, up to `max_conditions`, branching on the best `branch` choices at each step. Condition values come from the state at the anchor fires (and throughout `while` periods) against the state at the bad fires; numeric thresholds are the roundest number between the good and the bad values.
4. Every candidate is scored exactly by the evaluator, deduplicated (candidates that fire at the same instants keep only the simplest), ranked by `score.rank_key` and `tie_break` (fewer options, fires closer to the anchors, fewer unlabeled fires overall) and cut to `top_k`.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta, tzinfo
from statistics import median
from typing import Any, Literal

from .evaluate import Evaluator, Fire, SunEvents
from .labels import Anchor, Label, Labels, Tolerance
from .model import (
    DAY,
    WEEKDAYS,
    Candidate,
    Condition,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
    Trigger,
    render_automation,
)
from .score import Stats, clock_roundness, rank_key, roundness, score_candidate, simplicity
from .timeline import NON_NUMERIC, UNAVAILABLE_STATES, StepFunction, Timeline, to_iso

FRIENDLY_DURATIONS = (5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600, 900, 1200, 1800, 2700, 3600, 5400, 7200, 10800)
THRESHOLD_STEPS = (1000, 500, 100, 50, 10, 5, 1, 0.5, 0.1, 0.05, 0.01)
CLOCK_STEPS = (3600, 900, 300, 60, 10, 1)
WORKDAYS = WEEKDAYS[:5]
WEEKEND = WEEKDAYS[5:]
_INF = math.inf
FREE_FORM = 4  # an entity (or attribute) with at least this many values in the window, most of them seen once, is free-form: a timestamp, a counter, free text
MAX_SUN_OFFSET = 3 * 3600  # a sun trigger or condition further from the event reads as a clock time
_MERGE = 60  # outside fires of several suggestions closer than this are one hint
ALIGN = 5.0  # an any-change trigger is offered when anchors fall this close (seconds) to the entity's writes ...
ALIGN_ALPHA = 0.05  # ... more often than chance would, at this level


@dataclass(frozen=True, slots=True)
class SuggestOptions:
    max_conditions: int = 2
    top_k: int = 8
    max_lag: float = 3600  # the longest `for:` inferred (a cause may begin up to this plus the tolerance before an anchor)
    min_recall: float = 0.5  # triggers hitting less than this share of the best trigger's anchors are dropped
    beam: int = 12  # triggers that go on to the condition search
    side_beam: int = 4  # per side, for start/end pairs
    branch: int = 3  # conditions tried at each greedy step
    max_hints: int = 20  # per kind
    hint_suggestions: int = 3  # whose outside fires become hints
    max_bad_sample: int = 600  # bad fires the condition search looks at (all of them are scored)
    max_triggers: int = 3  # per side: an OR of up to this many triggers when no single one hits every anchor
    union_seeds: int = 3  # triggers an OR is grown from
    union_min_recall: float = 0.1  # a part of an OR hits at least this share of the anchors still missed
    union_alpha: float = 0.01  # a part of an OR must hit the missed anchors more often than chance would, at this level

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> SuggestOptions:
        """Options from the websocket's `options` (unknown keys are ignored)."""
        if not data:
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        values = {}
        for key, value in data.items():
            if key in known:
                values[key] = float(value) if known[key] == "float" else int(value)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Suggestion:
    rank: int
    candidate: Candidate
    stats: Stats
    config: dict[str, Any]
    explanation: str
    # Set by exact validation (exact.py): whether `stats` are the sandbox's, the miner's stats when they differ, and whether the two disagree badly.
    exact: bool | None = None
    approx_stats: Stats | None = None
    diverged: bool = False

    def as_dict(self) -> dict[str, Any]:
        out = {"rank": self.rank, "config": self.config, "stats": self.stats.as_dict(), "explanation": self.explanation}
        if self.exact is not None:
            out["exact"] = self.exact
        if self.approx_stats is not None:
            out["approx_stats"] = self.approx_stats.as_dict()
        if self.diverged:
            out["diverged"] = True
        return out


@dataclass(frozen=True, slots=True)
class Hint:
    """Evidence outside the labels: an outside fire of the top suggestions, or the labeled transition happening elsewhere."""

    kind: Literal["outside_fire", "recurrence"]
    at: float
    similarity: float  # 0..1, how much the moment looks like the labeled ones
    suggestions: tuple[int, ...] = ()  # ranks of the suggestions that fire here
    entity_id: str | None = None  # recurrence: the transition
    from_: Any = None
    to: Any = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "at": to_iso(self.at),
            "similarity": self.similarity,
            "suggestions": list(self.suggestions),
            "description": self.description,
        }
        if self.kind == "recurrence":
            out.update({"entity_id": self.entity_id, "from": self.from_, "to": self.to})
        return out


@dataclass(slots=True)
class SuggestResult:
    suggestions: list[Suggestion] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    evaluated: int = 0  # candidates scored
    warning: dict[str, str] | None = None  # {code, message}: exact validation fell back to the miner's stats
    # What the search knew (its labels, evaluator, anchors), so exact validation can re-score and re-hint; not serialized.
    context: SearchContext | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "suggestions": [s.as_dict() for s in self.suggestions],
            "hints": [h.as_dict() for h in self.hints],
            "evaluated": self.evaluated,
        }
        if self.warning is not None:
            out["warning"] = self.warning
        return out


class SuggestCancelled(Exception):
    """`suggest`'s `should_stop` said to stop."""


# Rounding and quantizing


def round_in(lo: float, hi: float, closed: Literal["lo", "hi"] = "lo") -> float:
    """The roundest number in `[lo, hi)` (or `(lo, hi]`), nearest the middle among equally round ones."""
    def inside(v: float) -> bool:
        return lo <= v < hi if closed == "lo" else lo < v <= hi

    mid = (lo + hi) / 2
    for step in THRESHOLD_STEPS:
        first, last = math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9)
        if last - first > 50:
            continue  # a coarser step already had a candidate; this only guards odd inputs
        found = [v for k in range(first, last + 1) if inside(v := round(k * step, 10))]
        if found:
            return min(found, key=lambda v: (abs(v - mid), v))
    value = round(mid, 3)
    return value if inside(value) else (lo if closed == "lo" else hi)


def _clock_pick(lo: float, hi: float, prefer: Literal["late", "early"]) -> float | None:
    """The roundest instant in `[lo, hi]` (seconds; may be negative), latest or earliest among equally round."""
    for step in CLOCK_STEPS:
        first, last = math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9)
        if first <= last:
            return float((last if prefer == "late" else first) * step)
    return None


def friendly_durations(lo: float, hi: float) -> list[float]:
    """Friendly `for:` durations in `[lo, hi]`, roundest first."""
    found = [float(v) for v in FRIENDLY_DURATIONS if lo <= v <= hi]
    return sorted(found, key=lambda v: (clock_roundness(v), v))


def infer_for(lags: Sequence[float], tolerance: Tolerance, limit: int = 2) -> list[float]:
    """`for:` durations that put fires inside the anchors' tolerance, given how long before each anchor its cause began.

    A fire at `cause + for` matches when `for` is within `[lag - before, lag + after]`; the intersection over all lags is searched for friendly durations (closest to the median lag among equally round ones), else whole minutes or seconds. Inconsistent lags still yield the friendly duration nearest their median, for the evaluator to judge.
    """
    lo = max(max(lags) - tolerance.before, 1.0)
    hi = min(lags) + tolerance.after
    mid = median(lags)
    if lo <= hi:
        found = [v for v in FRIENDLY_DURATIONS if lo <= v <= hi]
        if found:
            return [float(v) for v in sorted(found, key=lambda v: (clock_roundness(v), abs(v - mid)))[:limit]]
        for step in (60, 10, 1):
            first, last = math.ceil(lo / step), math.floor(hi / step)
            if first <= last:
                k = min(max(round(mid / step), first), last)
                return [float(k * step)]
    return [float(min(FRIENDLY_DURATIONS, key=lambda v: (abs(v - mid), v)))]


# Plain language


def _duration_text(seconds: float) -> str:
    seconds = round(abs(seconds))
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    parts = [f"{h} h" if h else "", f"{m} min" if m else "", f"{s} s" if s else ""]
    return " ".join(p for p in parts if p) or "0 s"


def _clock_text(seconds: float) -> str:
    seconds = round(seconds) % DAY
    text = f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}"
    return text + (f":{seconds % 60:02d}" if seconds % 60 else "")


def _values_text(values: Iterable[Any]) -> str:
    return " or ".join(str(v) for v in values)


def _weekdays_text(days: Sequence[str]) -> str:
    indexes = sorted(WEEKDAYS.index(d) for d in days)
    names = [WEEKDAYS[i].capitalize() for i in indexes]
    if len(indexes) > 2 and indexes == list(range(indexes[0], indexes[-1] + 1)):
        return f"{names[0]}–{names[-1]}"
    return ", ".join(names)


def _sun_text(event: str, offset: float) -> str:
    if not offset:
        return event
    return f"{_duration_text(offset)} {'before' if offset < 0 else 'after'} {event}"


def _sun_point(event: str, offset: float) -> str:
    """A sun event as a point in time for a condition: "sunset", "sunset + 1 h", "sunrise − 30 min"."""
    if not offset:
        return event
    return f"{event} {'−' if offset < 0 else '+'} {_duration_text(offset)}"


def describe_trigger(trigger: Trigger) -> str:
    match trigger:
        case StateTrigger():
            target = trigger.entity_id if trigger.attribute is None else f"{trigger.entity_id}'s {trigger.attribute}"
            if trigger.to is not None and trigger.from_ is not None:
                text = f"{target} changes from {_values_text(trigger.from_)} to {_values_text(trigger.to)}"
            elif trigger.to is not None:
                text = f"{target} changes to {_values_text(trigger.to)}"
            elif trigger.from_ is not None:
                text = f"{target} changes from {_values_text(trigger.from_)}"
            elif trigger.not_to is not None:
                text = f"{target} changes to anything but {_values_text(trigger.not_to)}"
            elif trigger.changes_only:
                text = f"{target} changes state"
            else:
                text = f"{target} changes"
            if trigger.not_from is not None:
                text += f" (not from {_values_text(trigger.not_from)})"
            if trigger.for_:
                text += f" and stays that way for {_duration_text(trigger.for_)}"
            return text
        case NumericStateTrigger():
            target = trigger.entity_id if trigger.attribute is None else f"{trigger.entity_id}'s {trigger.attribute}"
            if trigger.above is not None and trigger.below is not None:
                text = f"{target} goes between {trigger.above:g} and {trigger.below:g}"
            elif trigger.above is not None:
                text = f"{target} rises above {trigger.above:g}"
            else:
                text = f"{target} drops below {trigger.below:g}"
            if trigger.for_:
                text += f" and stays there for {_duration_text(trigger.for_)}"
            return text
        case TimeTrigger():
            text = f"at {_clock_text(trigger.at)}"
            if trigger.weekdays:
                text += f" on {_weekdays_text(trigger.weekdays)}"
            return text
        case SunTrigger():
            return f"at {trigger.event}" if not trigger.offset else _sun_text(trigger.event, trigger.offset)
    raise TypeError(f"not a trigger: {trigger!r}")


def describe_condition(condition: Condition) -> str:
    match condition:
        case StateCondition():
            target = condition.entity_id if condition.attribute is None else f"{condition.entity_id}'s {condition.attribute}"
            text = f"only when {target} is {_values_text(condition.state)}"
            if condition.for_:
                text += f" for {_duration_text(condition.for_)}"
            return text
        case NumericStateCondition():
            target = condition.entity_id if condition.attribute is None else f"{condition.entity_id}'s {condition.attribute}"
            if condition.above is not None and condition.below is not None:
                return f"only when {target} is between {condition.above:g} and {condition.below:g}"
            if condition.above is not None:
                return f"only when {target} is above {condition.above:g}"
            return f"only when {target} is below {condition.below:g}"
        case TimeCondition():
            parts = []
            if condition.after is not None and condition.before is not None:
                parts.append(f"only between {_clock_text(condition.after)} and {_clock_text(condition.before)}")
            elif condition.after is not None:
                parts.append(f"only after {_clock_text(condition.after)}")
            elif condition.before is not None:
                parts.append(f"only before {_clock_text(condition.before)}")
            if condition.weekdays:
                parts.append(f"only on {_weekdays_text(condition.weekdays)}")
            return ", ".join(parts)
        case SunCondition():
            after = condition.after and _sun_point(condition.after, condition.after_offset)
            before = condition.before and _sun_point(condition.before, condition.before_offset)
            if after and before:
                if condition.after == "sunset" and condition.before == "sunrise":
                    return f"only after {after} or before {before}"
                return f"only between {after} and {before}"
            return f"only after {after}" if after else f"only before {before}"
    raise TypeError(f"not a condition: {condition!r}")


def _either(triggers: Sequence[Trigger]) -> str:
    """Triggers joined with "or": "at 07:00 or when X changes", "when X changes or Y changes"."""
    phrases = []
    after_clock = True
    for trigger in triggers:
        phrase = describe_trigger(trigger)
        clock = isinstance(trigger, (TimeTrigger, SunTrigger))
        phrases.append(phrase if clock or not after_clock else f"when {phrase}")
        after_clock = clock
    return " or ".join(phrases)


def describe(candidate: Candidate) -> str:
    """What the candidate does, as one sentence (without its stats)."""
    triggers = candidate.triggers
    if {t.id for t in triggers} == {"start", "end"}:
        starts = " or ".join(describe_trigger(t) for t in triggers if t.id == "start")
        ends = " or ".join(describe_trigger(t) for t in triggers if t.id == "end")
        text = f"Starts when {starts}; ends when {ends}"
    else:
        text = f"Fires {_either(triggers)}"
    for condition in candidate.conditions:
        text += f", {describe_condition(condition)}"
    return text


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def explain(candidate: Candidate, stats: Stats, anchor_words: str) -> str:
    """`describe` plus how it did: "...; hits 4/4 marked starts, fires 2 other times"."""
    text = f"{describe(candidate)}; hits {stats.anchors_hit}/{stats.anchors_total} {anchor_words}"
    if stats.no_fire_violations:
        text += f", fires {_plural(len(stats.no_fire_violations), 'time')} in must-not periods"
    if stats.outside_fires:
        text += f", fires {len(stats.outside_fires)} other {'time' if len(stats.outside_fires) == 1 else 'times'}"
    if stats.while_total:
        text += f", holds throughout {stats.while_held}/{stats.while_total} while periods"
    return text


# Option counting for ranking ties


def complexity(candidate: Candidate) -> int:
    """Options beyond the bare minimum (from, not_from, attribute, both bounds): fewer reads simpler."""
    count = 0
    for part in (*candidate.triggers, *candidate.conditions):
        match part:
            case StateTrigger():
                count += sum(v is not None for v in (part.from_, part.not_from, part.not_to, part.attribute))
            case NumericStateTrigger() | NumericStateCondition():
                count += part.above is not None and part.below is not None
            case StateCondition():
                count += len(part.state) - 1
            case TimeCondition():
                count += (part.after is not None) + (part.before is not None) + (part.weekdays is not None) - 1
            case SunCondition():
                count += part.after is not None and part.before is not None
    return count


# Numeric threshold search


@dataclass(slots=True)
class _Crossings:
    """Every write of a numeric entity that crosses some threshold in one direction.

    A crossing at `times[i]` fires `above θ` for θ in `[lo[i], hi[i])` (or `below θ` for θ in `(lo[i], hi[i]]`). The fire count of any θ is then two binary searches.
    """

    direction: str
    times: list[float]
    lo: list[float]
    hi: list[float]
    values: list[float]  # distinct finite values, sorted: thresholds behave alike between consecutive ones
    _sorted_lo: list[float] = field(default_factory=list)
    _sorted_hi: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._sorted_lo = sorted(self.lo)
        self._sorted_hi = sorted(self.hi)

    def count(self, theta: float) -> int:
        """Fires of `above θ` / `below θ` (without `for:`) over the window."""
        if self.direction == "above":
            return bisect_right(self._sorted_lo, theta) - bisect_right(self._sorted_hi, theta)
        return bisect_left(self._sorted_lo, theta) - bisect_left(self._sorted_hi, theta)

    def gaps(self, i: int) -> range:
        """Indices k of the gaps between `values[k]` and `values[k + 1]` that crossing `i` crosses."""
        last = len(self.values) - 1
        first = 0 if self.lo[i] == -_INF else bisect_left(self.values, self.lo[i])
        end = last if self.hi[i] == _INF else bisect_left(self.values, self.hi[i])
        return range(first, min(end, last))

    def representative(self, k: int) -> float:
        closed = "lo" if self.direction == "above" else "hi"
        return round_in(self.values[k], self.values[k + 1], closed)

    def in_range(self, theta: float, value: Any) -> bool:
        return isinstance(value, float) and (value > theta if self.direction == "above" else value < theta)


def _crossings(step: StepFunction, direction: str, attribute: str | None = None) -> _Crossings:
    values = step.numeric_values(attribute)
    blocked = _INF if direction == "above" else -_INF  # not armed for any threshold
    prev: Any = blocked
    if step.seeded and values and values[0] is not NON_NUMERIC:
        prev = values[0]
    times: list[float] = []
    los: list[float] = []
    his: list[float] = []
    for i in step.write_range():
        value = values[i]
        if value is NON_NUMERIC:
            continue
        if value is None:
            prev = None
            continue
        if direction == "above":
            lo = -_INF if prev is None else prev
            if value > lo:
                times.append(step.times[i])
                los.append(lo)
                his.append(value)
        else:
            hi = _INF if prev is None else prev
            if value < hi:
                times.append(step.times[i])
                los.append(value)
                his.append(hi)
        prev = value
    distinct = sorted({v for v in values if isinstance(v, float)})
    return _Crossings(direction, times, los, his, distinct)


def _gap_scores(crossings: _Crossings, anchors: Sequence[Anchor], lo_offset: float, hi_offset: float) -> dict[int, tuple[int, float]]:
    """For each gap: how many anchors have a crossing of it within `[anchor + lo_offset, anchor + hi_offset]`, and the total distance from those anchors to their nearest such crossing."""
    scores: dict[int, tuple[int, float]] = {}
    times = crossings.times
    for anchor in anchors:
        nearest: dict[int, float] = {}
        for i in range(bisect_left(times, anchor.at + lo_offset), bisect_right(times, anchor.at + hi_offset)):
            distance = abs(times[i] - anchor.at)
            for k in crossings.gaps(i):
                if distance < nearest.get(k, _INF):
                    nearest[k] = distance
        for k, distance in nearest.items():
            covered, total = scores.get(k, (0, 0.0))
            scores[k] = (covered + 1, total + distance)
    return scores


def _ranked_thresholds(crossings: _Crossings, scores: Mapping[int, tuple[int, float]], limit: int) -> list[tuple[float, int, int]]:
    """`(θ, anchors covered, window fires)`, best first: most anchors, fewest other fires, roundest, crossing closest to the anchors."""
    ranked = []
    for k, (covered, distance) in scores.items():
        theta = crossings.representative(k)
        fires = crossings.count(theta)
        ranked.append((-covered, fires - covered, roundness(theta), distance / covered, theta, fires))
    ranked.sort()
    return [(theta, -neg, fires) for neg, _, _, _, theta, fires in ranked[:limit]]


def numeric_thresholds(
    step: StepFunction,
    anchors: Sequence[Anchor],
    tolerance: Tolerance,
    direction: Literal["above", "below"],
    limit: int = 3,
) -> list[float]:
    """The best thresholds for a `numeric_state` trigger that fires at the anchors (no `for:`), best first."""
    crossings = _crossings(step, direction)
    coverage = _gap_scores(crossings, anchors, -tolerance.before, tolerance.after)
    return [theta for theta, _, _ in _ranked_thresholds(crossings, coverage, limit)]


# Clock and sun


def _circular(values: Sequence[float], period: float = DAY) -> list[float]:
    """Values aligned around the first one, so a cluster across midnight is contiguous."""
    ref = values[0]
    return [ref + ((v - ref + period / 2) % period) - period / 2 for v in values]


def _best_cluster(values: Sequence[float], width: float) -> list[float]:
    """The largest subset of `values` within `width` of each other (smallest spread among equals)."""
    ordered = sorted(values)
    best: list[float] = []
    lo = 0
    for hi in range(len(ordered)):
        while ordered[hi] - ordered[lo] > width:
            lo += 1
        window = ordered[lo : hi + 1]
        if len(window) > len(best) or (len(window) == len(best) and window[-1] - window[0] < best[-1] - best[0]):
            best = window
    return best


def _pick_moment(cluster: Sequence[float], tolerance: Tolerance) -> float | None:
    """A round moment that matches every value in `cluster`: at or just before them if possible."""
    lo, first = cluster[-1] - tolerance.before, cluster[0]
    if lo <= first and (at := _clock_pick(lo, first, "late")) is not None:
        return at
    return _clock_pick(lo, first + tolerance.after, "early")


def clock_triggers(
    evaluator: Evaluator,
    labels: Labels,
    anchors: Sequence[Anchor],
    trigger_id: str | None,
    min_hits: int = 1,
) -> list[Trigger]:
    """A `time` or `sun` trigger for anchors that cluster by local clock or by sunrise/sunset: whichever clusters more of them with less spread."""
    if not anchors:
        return []
    tolerance = labels.tolerance
    width = tolerance.before + tolerance.after
    options: list[tuple[int, float, str, list[float]]] = []
    clocks = []
    for anchor in anchors:
        local = evaluator.local(anchor.at)
        clocks.append(local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6)
    cluster = _best_cluster(_circular(clocks), width)
    options.append((len(cluster), cluster[-1] - cluster[0], "time", cluster))
    if evaluator._sun_events is not None:  # noqa: SLF001
        for index, event in enumerate(("sunrise", "sunset")):
            offsets = []
            for anchor in anchors:
                offset = _sun_offset(evaluator, anchor.at, index)
                if offset is not None and abs(offset) <= MAX_SUN_OFFSET:
                    offsets.append(offset)
            if offsets:
                cluster = _best_cluster(offsets, width)
                options.append((len(cluster), cluster[-1] - cluster[0], event, cluster))
    best = max(n for n, *_ in options)
    if best < min_hits:
        return []
    least = min(spread for n, spread, *_ in options if n == best)
    found: list[Trigger] = []
    for n, spread, kind, cluster in options:
        if n != best or spread > least + 60:
            continue
        at = _pick_moment(cluster, tolerance)
        if at is None:
            continue
        if kind == "time":
            found.append(TimeTrigger(int(at % DAY), id=trigger_id))
        else:
            found.append(SunTrigger(kind, offset=at, id=trigger_id))
    return found


def _sun_offset(evaluator: Evaluator, t: float, index: int) -> float | None:
    """`t` minus the nearest sunrise (index 0) or sunset (1), over the local day before, of and after `t`."""
    day = evaluator.local(t).date()
    best = None
    for delta in (-1, 0, 1):
        event = evaluator.sun(day + timedelta(days=delta))[index]
        if event is not None and (best is None or abs(t - event) < abs(best)):
            best = t - event
    return best


# The search


@dataclass(slots=True)
class _Scored:
    candidate: Candidate
    stats: Stats
    fires: list[Fire]
    key: tuple[Any, ...] = ()

    @property
    def signature(self) -> tuple[Any, ...]:
        triggers = self.candidate.triggers
        return (tuple((f.at, triggers[f.trigger].id) for f in self.fires), self.stats.while_held)


def _fit_error(stats: Stats, anchors: Sequence[Anchor], fires: Sequence[Fire]) -> float:
    """Mean distance from each hit anchor to its nearest fire: the tie-break among otherwise equal candidates."""
    if not fires:
        return 0.0
    missed = set(stats.missed)
    times = [f.at for f in fires]
    errors = []
    for anchor in anchors:
        if anchor in missed:
            continue
        i = bisect_left(times, anchor.at)
        errors.append(min(abs(times[j] - anchor.at) for j in (i - 1, i) if 0 <= j < len(times)))
    return round(sum(errors) / len(errors), 1) if errors else 0.0


def tie_break(stats: Stats, candidate: Candidate, anchors: Sequence[Anchor], fires: Sequence[Fire]) -> tuple[Any, ...]:
    """After `score.rank_key`: fewer options, fires closer to the anchors, fewer unlabeled fires overall (beyond the labeled stretch too), then the description, so the order is stable."""
    return (complexity(candidate), _fit_error(stats, anchors, fires), len(stats.outside_fires), describe(candidate))


def dedupe(evaluator: Evaluator, labels: Labels, candidates: Iterable[Candidate]) -> list[Candidate]:
    """One candidate per set of fire instants (and `while` periods held): the simplest, in order of first appearance."""
    groups: dict[tuple[Any, ...], list[Candidate]] = {}
    for candidate in candidates:
        fires = evaluator.fires(candidate)
        stats = score_candidate(evaluator, candidate, labels, fires)
        groups.setdefault(_Scored(candidate, stats, fires).signature, []).append(candidate)
    return [min(group, key=lambda c: (simplicity(c), complexity(c), describe(c))) for group in groups.values()]


@dataclass(frozen=True, slots=True)
class _Part:
    """A trigger as a candidate part of an OR (see `_Search.unions`)."""

    hits: frozenset[int]  # anchors hit
    nearest: dict[int, int]  # each hit anchor's nearest fire (index into the trigger's fires)
    chance: float  # share of the window within reach of its fires: the chance it hits an anchor placed at random
    rank: tuple[Any, ...]  # bad fires, simplicity, then how close its fires are to the anchors
    aligned: frozenset[int] | None = None  # an any-change trigger: the anchors within `ALIGN` of its fires ...
    align_chance: float = 0.0  # ... and the chance of that at random


def _binomial_tail(n: int, k: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0 or p >= 1:
        return 1.0
    if p <= 0:
        return 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = sum(
        math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
        for i in range(k, n + 1)
    )
    return min(1.0, total)


class _Search:
    def __init__(self, timeline: Timeline, labels: Labels, evaluator: Evaluator, options: SuggestOptions) -> None:
        self.timeline = timeline
        self.labels = labels
        self.ev = evaluator
        self.options = options
        self.tolerance = labels.tolerance
        self._numeric: dict[str, bool] = {}
        self._crossing_cache: dict[tuple[str, str], _Crossings] = {}
        self._while_cache: dict[Condition, bool] = {}
        self._entries: dict[tuple[str, str | None], Counter[Any]] = {}
        self._unlabeled = labels.unlabeled_intervals(timeline.start, timeline.end)
        lo, hi = labels.span or (0.0, 0.0)
        self._unlabeled_near = [(max(a, lo), min(b, hi)) for a, b in self._unlabeled if a < hi and b > lo]
        self.evaluated = 0

    # Entities

    def is_numeric(self, entity_id: str) -> bool:
        if (known := self._numeric.get(entity_id)) is not None:
            return known
        values = self.timeline[entity_id].numeric_values()
        numbers = [v for v in values if isinstance(v, float)]
        present = sum(v is not None for v in values)
        result = self._numeric[entity_id] = bool(present) and len(numbers) >= 0.8 * present and len(set(numbers)) > 3
        return result

    def entries(self, entity_id: str, attribute: str | None = None) -> Counter[Any]:
        """How many times the state (or attribute) took each value in the window: a change into it, or the first entry."""
        key = (entity_id, attribute)
        if (found := self._entries.get(key)) is None:
            found = self._entries[key] = Counter()
            step = self.timeline[entity_id]
            previous: Any = object()
            for i in range(len(step)):
                value = step.states[i] if attribute is None else step.attributes[i].get(attribute)
                if value != previous and isinstance(value, (str, int, float, bool)):
                    found[value] += 1
                previous = value
        return found

    def free_form(self, entity_id: str, attribute: str | None = None) -> bool:
        entries = self.entries(entity_id, attribute)
        return len(entries) >= FREE_FORM and 2 * sum(n == 1 for n in entries.values()) > len(entries)

    def recurs(self, entity_id: str, value: Any, attribute: str | None = None) -> bool:
        """Whether `value` can be a trigger's `to` / `from` or a condition's state: any value of an entity with few values, but of a free-form one only a value it took more than once. A value seen once (a timestamp, a "9 days ago") can never match again."""
        return not self.free_form(entity_id, attribute) or self.entries(entity_id, attribute)[value] > 1

    def crossings(self, entity_id: str, direction: str) -> _Crossings:
        key = (entity_id, direction)
        if (found := self._crossing_cache.get(key)) is None:
            found = self._crossing_cache[key] = _crossings(self.timeline[entity_id], direction)
        return found

    # Trigger candidates

    def triggers(self, anchors: Sequence[Anchor], trigger_id: str | None, min_hits: int) -> set[Trigger]:
        found: set[Trigger] = set()
        for entity_id in self.timeline.entity_ids:
            if self.is_numeric(entity_id):
                found |= self._numeric_triggers(entity_id, anchors, trigger_id, min_hits)
            else:
                found |= self._state_triggers(entity_id, anchors, trigger_id, min_hits)
        found.update(clock_triggers(self.ev, self.labels, anchors, trigger_id, min_hits))
        return found

    def _state_triggers(self, entity_id: str, anchors: Sequence[Anchor], tid: str | None, min_hits: int) -> set[Trigger]:
        step = self.timeline[entity_id]
        before, after, max_lag = self.tolerance.before, self.tolerance.after, self.options.max_lag
        support: dict[Trigger, set[int]] = defaultdict(set)
        lagged: dict[Any, dict[int, tuple[float, Any]]] = defaultdict(dict)
        free_form = self.free_form(entity_id)
        tos: set[str] = set()
        froms: set[str] = set()
        changed_attributes: set[str] = set()
        for n, anchor in enumerate(anchors):
            for tr in step.transitions(anchor.at - before, anchor.at + after + 1e-6):
                if tr.old_state != tr.state:
                    if tr.state in UNAVAILABLE_STATES:
                        continue
                    if free_form:
                        support[StateTrigger(entity_id, changes_only=True, id=tid)].add(n)
                    if not self.recurs(entity_id, tr.state):
                        continue
                    tos.add(tr.state)
                    support[StateTrigger(entity_id, to=(tr.state,), id=tid)].add(n)
                    if tr.old_state is not None and self.recurs(entity_id, tr.old_state):
                        support[StateTrigger(entity_id, from_=(tr.old_state,), to=(tr.state,), id=tid)].add(n)
                        if tr.old_state not in UNAVAILABLE_STATES:
                            froms.add(tr.old_state)
                elif tr.old_attributes is not None:
                    for key, value in tr.attributes.items():
                        if value is None or not isinstance(value, (str, int, float, bool)):
                            continue
                        if tr.old_attributes.get(key) == value:
                            continue
                        changed_attributes.add(key)
                        if self.recurs(entity_id, value, key):
                            support[StateTrigger(entity_id, attribute=key, to=(value,), id=tid)].add(n)
            # The state at the anchor began a while before it: a `for:` candidate.
            i = step.index_at(anchor.at)
            if i < 0:
                continue
            began = step.changed[i]
            j = bisect_left(step.times, began)
            if j >= len(step.times) or step.times[j] != began or (j == 0 and step.seeded) or began < self.timeline.start:
                continue
            lag = anchor.at - began
            state = step.states[i]
            # A `for:` of at most `max_lag` still matches an anchor up to `before` later than that.
            if before < lag <= max_lag + before and state not in UNAVAILABLE_STATES and self.recurs(entity_id, state):
                lagged[state][n] = (lag, step.states[j - 1] if j else None)
        if 2 <= len(tos) <= 3:
            # A toggle: some anchors follow a change one way and some another, so one trigger listing them all hits more than either.
            single = [support[StateTrigger(entity_id, to=(state,), id=tid)] for state in tos]
            either = set().union(*single)
            if len(either) > max(len(hits) for hits in single):
                support[StateTrigger(entity_id, to=tuple(sorted(tos)), id=tid)] = either
                if froms:
                    support[StateTrigger(entity_id, from_=tuple(sorted(froms)), to=tuple(sorted(tos)), id=tid)] = either
        found = {trigger for trigger, hits in support.items() if len(hits) >= min_hits}
        for key in changed_attributes:
            # Every change of the attribute (a thermostat's current_temperature), when the anchors follow its changes.
            changes = [step.times[i] for i in step.write_range() if i and step.attributes[i].get(key) != step.attributes[i - 1].get(key)]
            if self.lines_up(changes, anchors, min_hits):
                found.add(StateTrigger(entity_id, attribute=key, id=tid))
        for state, per_anchor in lagged.items():
            if len(per_anchor) < min_hits:
                continue
            for duration in infer_for([lag for lag, _ in per_anchor.values()], self.tolerance):
                found.add(StateTrigger(entity_id, to=(state,), for_=duration, id=tid))
        return found

    def lines_up(self, times: Sequence[float], anchors: Sequence[Anchor], min_hits: int) -> bool:
        """Whether at least `min_hits` anchors fall within `ALIGN` seconds of `times` (an entity's writes), more often than chance would. A trigger on every write of a chatty entity has some write within the tolerance of any anchor, so only this tighter fit says the anchors follow its writes."""
        if not times:
            return False
        aligned = sum(bisect_right(times, a.at + ALIGN) > bisect_left(times, a.at - ALIGN) for a in anchors)
        if aligned < min_hits:
            return False
        chance = min(1.0, len(times) * 2 * ALIGN / max(self.timeline.end - self.timeline.start, 1.0))
        return _binomial_tail(len(anchors), aligned, chance) <= ALIGN_ALPHA

    def _numeric_triggers(self, entity_id: str, anchors: Sequence[Anchor], tid: str | None, min_hits: int) -> set[Trigger]:
        step = self.timeline[entity_id]
        before, after, max_lag = self.tolerance.before, self.tolerance.after, self.options.max_lag
        found: set[Trigger] = set()
        # Every report (the blueprint controller's `trigger: state` with no from/to), and every state change (`to: null`) when some reports change attributes only.
        writes = [step.times[i] for i in step.write_range()]
        if self.lines_up(writes, anchors, min_hits):
            found.add(StateTrigger(entity_id, id=tid))
            changes = [step.times[i] for i in step.write_range() if i and step.states[i] != step.states[i - 1]]
            if len(changes) < len(writes) and self.lines_up(changes, anchors, min_hits):
                found.add(StateTrigger(entity_id, changes_only=True, id=tid))
        for direction in ("above", "below"):
            crossings = self.crossings(entity_id, direction)
            if not crossings.times or len(crossings.values) < 2:
                continue
            direct = _gap_scores(crossings, anchors, -before, after)
            for theta, covered, _ in _ranked_thresholds(crossings, direct, 3):
                if covered >= min_hits:
                    found.add(NumericStateTrigger(entity_id, **{direction: theta}, id=tid))
            lagged = _gap_scores(crossings, anchors, -max_lag - before, -before)
            ranked = sorted(
                (-covered, roundness(theta := crossings.representative(k)), crossings.count(theta), theta)
                for k, (covered, _) in lagged.items()
                if covered >= min_hits
            )[:4]
            for _, _, _, theta in ranked:
                lags = [lag for anchor in anchors if (lag := self._numeric_lag(step, crossings, theta, anchor.at)) is not None]
                if len(lags) < min_hits:
                    continue
                for duration in infer_for(lags, self.tolerance):
                    found.add(NumericStateTrigger(entity_id, **{direction: theta}, for_=duration, id=tid))
        return found

    def _numeric_lag(self, step: StepFunction, crossings: _Crossings, theta: float, at: float) -> float | None:
        """How long before `at` the value last entered the range (it must still be in it at `at`), within `max_lag`."""
        values = step.numeric_values()
        i = step.index_at(at)
        if i < 0 or not crossings.in_range(theta, values[i]):
            return None
        floor = at - self.options.max_lag - self.tolerance.before
        began = None
        j = i
        while j >= 0 and step.times[j] >= floor:
            value = values[j]
            if value is NON_NUMERIC:
                j -= 1
                continue
            if not crossings.in_range(theta, value):
                break
            began = j
            j -= 1
        else:
            return None  # in range since before the lag window (or since the seeded state)
        if began is None or (began == 0 and step.seeded):
            return None
        lag = at - step.times[began]
        return lag if lag > self.tolerance.before else None

    # Screening

    def screen(self, trigger: Trigger, anchors: Sequence[Anchor]) -> tuple[int, int, int, int, list[float]]:
        """`(anchors hit, must-not fires, unlabeled fires within the labeled stretch, all unlabeled fires, fires)` of one trigger, by binary search."""
        fires = self.ev.trigger_fires(trigger)
        self.evaluated += 1
        before, after = self.tolerance.before, self.tolerance.after
        hits = sum(bisect_right(fires, a.at + after) > bisect_left(fires, a.at - before) for a in anchors)
        must_not = sum(bisect_left(fires, p.end) - bisect_left(fires, p.start) for p in self.labels.no_fire_periods)
        unlabeled = sum(bisect_left(fires, hi) - bisect_left(fires, lo) for lo, hi in self._unlabeled)
        near = sum(bisect_right(fires, hi) - bisect_left(fires, lo) for lo, hi in self._unlabeled_near)
        return hits, must_not, near, unlabeled, fires

    def best_triggers(self, anchors: Sequence[Anchor], trigger_id: str | None, beam: int) -> list[Trigger]:
        min_hits = max(1, math.ceil(len(anchors) * self.options.min_recall))
        screened = []
        for trigger in self.triggers(anchors, trigger_id, min_hits):
            hits, must_not, near, unlabeled, fires = self.screen(trigger, anchors)
            if hits:
                single = Candidate((trigger,))
                key = (-hits, must_not, near, simplicity(single), complexity(single), unlabeled, describe(single))
                screened.append((key, trigger, tuple(fires)))
        if not screened:
            return []
        most = -min(key[0] for key, *_ in screened)
        floor = max(min_hits, math.ceil(most * self.options.min_recall))
        screened.sort(key=lambda item: item[0])
        kept: list[Trigger] = []
        seen: set[tuple[float, ...]] = set()
        for key, trigger, fires in screened:
            if -key[0] < floor or fires in seen:
                continue
            seen.add(fires)
            kept.append(trigger)
            if len(kept) >= beam:
                break
        return kept

    # OR of several triggers

    def side(self, anchors: Sequence[Anchor], trigger_id: str | None, beam: int) -> list[tuple[Trigger, ...]]:
        """The trigger sets one side (all anchors, or the starts or ends of a pair) is searched from: the best single triggers, then ORs of several (`unions`)."""
        singles = self.best_triggers(anchors, trigger_id, beam)
        return [(trigger,) for trigger in singles] + self.unions(anchors, trigger_id, singles)

    def _part(self, trigger: Trigger, anchors: Sequence[Anchor]) -> _Part:
        """A trigger's anchor hits (indexes into `anchors`, the whole side), its bad fires, and how much of the window its fires would cover by chance."""
        _, must_not, near, unlabeled, fires = self.screen(trigger, anchors)
        before, after = self.tolerance.before, self.tolerance.after
        nearest: dict[int, int] = {}
        distance = 0.0
        for n, a in enumerate(anchors):
            lo, hi = bisect_left(fires, a.at - before), bisect_right(fires, a.at + after)
            if hi > lo:
                j = min((lo, hi - 1), key=lambda j: abs(fires[j] - a.at))
                nearest[n] = j
                distance += abs(fires[j] - a.at)
        # An anchor at `a` is hit by a fire in [a - before, a + after]: the anchors a fire could explain lie in [fire - after, fire + before].
        covered = 0.0
        reach = self.timeline.start
        for t in fires:
            lo, hi = max(t - after, reach), min(t + before, self.timeline.end)
            if hi > lo:
                covered += hi - lo
                reach = hi
        span = max(self.timeline.end - self.timeline.start, 1.0)
        single = Candidate((trigger,))
        fit = round(distance / len(nearest), 1) if nearest else 0.0
        rank = (must_not, near, simplicity(single), complexity(single), fit, unlabeled, describe(single))
        if not (isinstance(trigger, StateTrigger) and trigger.to is trigger.not_to is trigger.from_ is trigger.not_from is None):
            return _Part(frozenset(nearest), nearest, covered / span, rank)
        # A chatty sensor has a report within the tolerance of any anchor, so its evidence is the anchors that land on its reports.
        aligned = frozenset(n for n, a in enumerate(anchors) if bisect_right(fires, a.at + ALIGN) > bisect_left(fires, a.at - ALIGN))
        return _Part(frozenset(nearest), nearest, covered / span, rank, aligned, min(1.0, len(fires) * 2 * ALIGN / span))

    def _evidence(self, trigger: Trigger, part: _Part, new: Iterable[int], missed: int) -> bool:
        """Whether a trigger hitting the `new` anchors (of `missed` ones) is evidence, not chance. What counts is the moments it fires at (one fire near a burst of labels is one moment): one fitted to a single moment (a clock time, a threshold, a `for:`) never is evidence, and anything else must fire near more of them than its fires would at random (binomial tail at `union_alpha`). An any-change trigger on a chatty sensor fires near every anchor, so for it the anchors landing on its reports (within `ALIGN`) count instead."""
        moments = len({part.nearest[n] for n in new})
        fitted = isinstance(trigger, (TimeTrigger, SunTrigger, NumericStateTrigger)) or bool(getattr(trigger, "for_", 0))
        if fitted and moments < 2:
            return False
        alpha = self.options.union_alpha
        # Landing on the reports of an any-change trigger is the stronger claim (a sensor polled on the minute meets labels drawn on the minute), so it needs a tenth of the level.
        if part.aligned is not None and _binomial_tail(missed, len(part.aligned.intersection(new)), part.align_chance) <= alpha / 10:
            return True
        return _binomial_tail(missed, moments, part.chance) <= alpha

    def unions(self, anchors: Sequence[Anchor], trigger_id: str | None, singles: Sequence[Trigger]) -> list[tuple[Trigger, ...]]:
        """ORs of up to `max_triggers` triggers, when no single trigger hits every anchor: greedy set cover from each of the best `union_seeds` triggers, each step adding the trigger (searched again around the anchors still missed) that hits the most of them, fewest bad fires first. A trigger joins only when it hits the missed anchors more often than chance would."""
        options = self.options
        everything = frozenset(range(len(anchors)))
        if options.max_triggers < 2 or len(anchors) < 2:
            return []
        memo: dict[Trigger, _Part] = {}

        def info(trigger: Trigger) -> _Part:
            if (found := memo.get(trigger)) is None:
                found = memo[trigger] = self._part(trigger, anchors)
            return found

        if any(info(t).hits == everything for t in singles):
            return []

        def pool(around: Sequence[Anchor]) -> list[Trigger]:
            min_hits = max(1, math.ceil(len(around) * options.union_min_recall))
            return sorted(self.triggers(around, trigger_id, min_hits), key=lambda t: info(t).rank)

        first = pool(anchors)
        seeds = [
            t
            for t in sorted(first, key=lambda t: (-len(info(t).hits), info(t).rank))
            if self._evidence(t, info(t), info(t).hits, len(anchors))
        ][: options.union_seeds]
        found: list[tuple[Trigger, ...]] = []
        seen: set[frozenset[Trigger]] = set()
        for seed in seeds:
            parts = [seed]
            covered = info(seed).hits
            while len(parts) < options.max_triggers and covered != everything:
                missed = sorted(everything - covered)
                around = [anchors[n] for n in missed]
                best: tuple[Any, ...] | None = None
                for trigger in dict.fromkeys([*first, *pool(around)]):
                    if trigger in parts:
                        continue
                    part = info(trigger)
                    new = part.hits - covered
                    if not new or not self._evidence(trigger, part, new, len(missed)):
                        continue
                    key = (-len(new), part.rank)
                    if best is None or key < best[0]:
                        best = (key, trigger)
                if best is None:
                    break
                parts.append(best[1])
                covered |= info(best[1]).hits
            if len(parts) > 1 and frozenset(parts) not in seen:
                seen.add(frozenset(parts))
                found.append(tuple(parts))
        return found

    # Conditions

    def _while_ok(self, condition: Condition) -> bool:
        if (known := self._while_cache.get(condition)) is None:
            known = self._while_cache[condition] = all(
                self.ev.holds_throughout((condition,), period.start, period.end) for period in self.labels.while_periods
            )
        return known

    def _local_clock(self, t: float) -> tuple[float, str]:
        local = self.ev.local(t)
        return local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6, WEEKDAYS[local.weekday()]

    def condition_pool(self, good: Sequence[float], bad: Sequence[float]) -> list[Condition]:
        """Conditions that hold at every good fire (and throughout every `while` period) and fail at some bad one."""
        periods = self.labels.while_periods
        pool: set[Condition] = set()
        for entity_id in self.timeline.entity_ids:
            step = self.timeline[entity_id]
            if self.is_numeric(entity_id):
                pool.update(self._numeric_conditions(entity_id, step, good, bad, periods))
                continue
            states = {step.state_at(t) for t in good}
            for period in periods:
                states.add(step.state_at(period.start))
                states.update(step.states[i] for i in step.write_range(period.start, period.end))
            if None in states or len(states) > 3 or states & UNAVAILABLE_STATES:
                continue
            if not all(self.recurs(entity_id, state) for state in states):
                continue
            if any(step.state_at(t) not in states for t in bad):
                pool.add(StateCondition(entity_id, tuple(sorted(states))))
        pool.update(self._time_conditions(good, periods))
        if self.ev._sun_events is not None:  # noqa: SLF001
            pool.update(self._sun_conditions(good))
        return sorted(pool, key=lambda c: (type(c).__name__, describe_condition(c)))

    def _numeric_conditions(self, entity_id: str, step: StepFunction, good: Sequence[float], bad: Sequence[float], periods: Sequence[Label]) -> list[Condition]:
        values = step.numeric_values()
        good_values = [step.numeric_at(t) for t in good]
        for period in periods:
            good_values.append(step.numeric_at(period.start))
            good_values += [values[i] for i in step.write_range(period.start, period.end)]
        if not good_values or not all(isinstance(v, float) for v in good_values):
            return []
        lo, hi = min(good_values), max(good_values)
        bad_values = [v for t in bad if isinstance(v := step.numeric_at(t), float)]
        found: list[Condition] = []
        under = [v for v in bad_values if v < lo]
        if under:
            found.append(NumericStateCondition(entity_id, above=round_in(max(under), lo, "lo")))
        over = [v for v in bad_values if v > hi]
        if over:
            found.append(NumericStateCondition(entity_id, below=round_in(hi, min(over), "hi")))
        return found

    def _time_conditions(self, good: Sequence[float], periods: Sequence[Label]) -> list[Condition]:
        moments = list(good)
        for period in periods:
            moments += [period.start, period.end - 1]
        clocks_days = [self._local_clock(t) for t in moments]
        found: list[Condition] = []
        days = {day for _, day in clocks_days}
        if len(days) < 7:
            found.append(TimeCondition(weekdays=tuple(d for d in WEEKDAYS if d in days)))
            if days < set(WORKDAYS):
                found.append(TimeCondition(weekdays=WORKDAYS))
            if days < set(WEEKEND):
                found.append(TimeCondition(weekdays=WEEKEND))
        clocks = sorted(clock for clock, _ in clocks_days)
        if len(clocks) == 1:
            first = last = clocks[0]
        else:
            # The window is everything but the largest gap between consecutive clock times (around midnight too).
            gaps = [(clocks[(i + 1) % len(clocks)] - clocks[i]) % DAY for i in range(len(clocks))]
            widest = max(range(len(gaps)), key=gaps.__getitem__)
            if gaps[widest] < 4 * 3600:
                return found
            first, last = clocks[(widest + 1) % len(clocks)], clocks[widest]
        for step in (3600, 900, 300):
            after = int(math.floor(first / step) * step) % DAY
            before = int(math.floor(last / step + 1) * step) % DAY
            found.append(TimeCondition(after=after))
            if before:  # `before: 00:00` would hold all day
                found.append(TimeCondition(before=before))
            if after != before:
                found.append(TimeCondition(after=after, before=before))
        return found

    def _sun_conditions(self, good: Sequence[float]) -> list[Condition]:
        found: list[Condition] = [
            SunCondition(after="sunset"),
            SunCondition(before="sunrise"),
            SunCondition(after="sunrise"),
            SunCondition(before="sunset"),
            SunCondition(after="sunset", before="sunrise"),
            SunCondition(after="sunrise", before="sunset"),
        ]
        offsets: dict[str, list[float]] = {"sunrise": [], "sunset": []}
        for t in good:
            sunrise, sunset = self.ev.sun(self.ev.local(t).date())
            if sunrise is not None:
                offsets["sunrise"].append(t - sunrise)
            if sunset is not None:
                offsets["sunset"].append(t - sunset)
        for event, values in offsets.items():
            if not values or len(values) < len(good):
                continue
            earliest = math.floor(min(values) / 900) * 900
            latest = math.ceil(max(values) / 900) * 900
            if earliest and abs(earliest) <= MAX_SUN_OFFSET:
                found.append(SunCondition(after=event, after_offset=earliest))
            if latest and abs(latest) <= MAX_SUN_OFFSET:
                found.append(SunCondition(before=event, before_offset=latest))
        return found

    def grow(self, base: Candidate) -> list[_Scored]:
        """The base and the condition sets the greedy search adds to it, each scored exactly."""
        labels = self.labels
        fires = self.ev.fires(base)
        ids = [base.triggers[f.trigger].id for f in fires]
        kinds = [labels.classify(f.at, tid) for f, tid in zip(fires, ids, strict=True)]
        good = [j for j, k in enumerate(kinds) if k == "anchor"]
        must_not = [j for j, k in enumerate(kinds) if k == "no_fire"]
        unlabeled = [j for j, k in enumerate(kinds) if k == "unlabeled"]
        near = [j for j in unlabeled if labels.in_span(fires[j].at)]
        results = [self.score(base, fires)]
        if not must_not and not unlabeled:
            return results
        cap = self.options.max_bad_sample
        sampled = len(must_not) + len(unlabeled) > cap
        if sampled:
            stride = (len(must_not) + len(unlabeled)) / cap
            bad = sorted(must_not + unlabeled)
            bad = [bad[int(n * stride)] for n in range(cap)]
            chosen = sorted(set(good) | set(bad))
        else:
            chosen = list(range(len(fires)))
        times = [fires[j].at for j in chosen]
        position = {j: p for p, j in enumerate(chosen)}

        def mask_of(indexes: Iterable[int]) -> int:
            m = 0
            for j in indexes:
                if j in position:
                    m |= 1 << position[j]
            return m

        must_not_mask, near_mask = mask_of(must_not), mask_of(near)
        far_mask = mask_of(unlabeled) & ~near_mask
        anchor_masks: dict[Anchor, int] = defaultdict(int)
        for j in good:
            for anchor in labels.anchors_matching(fires[j].at, ids[j]):
                anchor_masks[anchor] |= 1 << position[j]
        required = list(anchor_masks.values())
        pool = []
        for condition in self.condition_pool(
            [fires[j].at for j in good], [fires[j].at for j in chosen if kinds[j] in ("no_fire", "unlabeled")]
        ):
            if labels.while_periods and not self._while_ok(condition):
                continue
            bits = "".join("1" if self.ev.condition_holds(condition, t) else "0" for t in reversed(times))
            m = int(bits, 2) if bits else 0
            if all(m & r for r in required):
                pool.append((condition, m))
        full = (1 << len(times)) - 1
        frontier: list[tuple[tuple[Condition, ...], int]] = [((), full)]
        seen: set[frozenset[Condition]] = set()
        for _ in range(self.options.max_conditions):
            grown = []
            for conditions, m in frontier:
                bad_nf, bad_near, bad_far = ((m & mask).bit_count() for mask in (must_not_mask, near_mask, far_mask))
                options = []
                for condition, cm in pool:
                    if condition in conditions:
                        continue
                    nm = m & cm
                    if not all(nm & r for r in required):
                        continue
                    removed_nf = bad_nf - (nm & must_not_mask).bit_count()
                    removed_near = bad_near - (nm & near_mask).bit_count()
                    removed_far = bad_far - (nm & far_mask).bit_count()
                    if removed_nf + removed_near + removed_far == 0:
                        continue
                    part = Candidate((), (condition,))
                    key = (-removed_nf, -removed_near, simplicity(part)[2], complexity(part), -removed_far)
                    options.append((key, condition, nm))
                options.sort(key=lambda item: item[0])
                for _, condition, nm in options[: self.options.branch]:
                    chosen_set = frozenset((*conditions, condition))
                    if chosen_set in seen:
                        continue
                    seen.add(chosen_set)
                    grown.append(((*conditions, condition), nm))
            for conditions, nm in grown:
                candidate = replace(base, conditions=conditions)
                if sampled:
                    kept = [f for f in fires if self.ev.conditions_hold(conditions, f.at)]
                else:
                    kept = [fires[j] for j in range(len(fires)) if nm >> j & 1]
                results.append(self.score(candidate, kept))
            frontier = grown
        return results

    def score(self, candidate: Candidate, fires: list[Fire]) -> _Scored:
        self.evaluated += 1
        stats = score_candidate(self.ev, candidate, self.labels, fires)
        key = (*rank_key(stats, candidate), *tie_break(stats, candidate, self.labels.anchors, fires))
        return _Scored(candidate, stats, fires, key)


# Similarity, for hints


class _Similarity:
    """How much a moment looks like the labeled ones: the referenced entities' states, the clock time and the sun offset."""

    def __init__(self, search: _Search, anchors: Sequence[Anchor], entity_ids: Iterable[str]) -> None:
        self.search = search
        self.ev = search.ev
        times = [a.at for a in anchors]
        self.clocks = [search._local_clock(t)[0] for t in times]  # noqa: SLF001
        self.sun = self.ev._sun_events is not None  # noqa: SLF001
        self.offsets = [[o for t in times if (o := _sun_offset(self.ev, t, index)) is not None] for index in (0, 1)] if self.sun else []
        self.entities: list[tuple[str, Any]] = []
        for entity_id in sorted(entity_ids):
            step = search.timeline.get(entity_id)
            if step is None:
                continue
            if search.is_numeric(entity_id):
                at_anchors = [v for t in times if isinstance(v := step.numeric_at(t), float)]
                everything = [v for v in step.numeric_values() if isinstance(v, float)]
                if at_anchors and everything:
                    scale = max((max(everything) - min(everything)) / 4, 1e-9)
                    self.entities.append((entity_id, ("numeric", median(at_anchors), scale)))
            else:
                self.entities.append((entity_id, ("state", Counter(step.state_at(t) for t in times), len(times))))

    def __call__(self, t: float) -> float:
        terms = []
        clock = self.search._local_clock(t)[0]  # noqa: SLF001
        if self.clocks:
            distance = min(min(abs(clock - c) % DAY, DAY - abs(clock - c) % DAY) for c in self.clocks)
            terms.append(1 - min(1.0, distance / 7200))
        if self.sun:
            best = 0.0
            for index, offsets in enumerate(self.offsets):
                offset = _sun_offset(self.ev, t, index)
                if offsets and offset is not None:
                    best = max(best, 1 - min(1.0, min(abs(offset - o) for o in offsets) / 7200))
            terms.append(best)
        if self.entities:
            parts = []
            for entity_id, profile in self.entities:
                step = self.search.timeline[entity_id]
                if profile[0] == "numeric":
                    value = step.numeric_at(t)
                    parts.append(1 - min(1.0, abs(value - profile[1]) / profile[2]) if isinstance(value, float) else 0.0)
                else:
                    parts.append(profile[1][step.state_at(t)] / profile[2])
            terms.append(sum(parts) / len(parts))
        return round(sum(terms) / len(terms), 3) if terms else 0.0


def _hints(search: _Search, anchors: Sequence[Anchor], suggestions: Sequence[Suggestion]) -> list[Hint]:
    options = search.options
    top = suggestions[: options.hint_suggestions]
    if not top or not anchors:
        return []
    entity_ids: set[str] = set()
    for suggestion in top:
        entity_ids |= suggestion.candidate.entity_ids
    similarity = _Similarity(search, anchors, entity_ids)
    # Outside fires of the top suggestions, merged when they coincide.
    fired = sorted((t, s.rank) for s in top for t in s.stats.outside_fires)
    groups: list[tuple[float, set[int]]] = []
    for t, rank in fired:
        if groups and t - groups[-1][0] <= _MERGE:
            groups[-1][1].add(rank)
        else:
            groups.append((t, {rank}))
    outside = []
    for t, ranks in groups:
        ordered = tuple(sorted(ranks))
        which = ", ".join(str(r + 1) for r in ordered)
        outside.append(Hint("outside_fire", t, similarity(t), ordered, description=f"Suggestion {which} fires here, outside the labels"))
    outside.sort(key=lambda h: (-h.similarity, h.at))
    outside = outside[: options.max_hints]
    # The transitions seen at most anchors, happening elsewhere.
    before, after = search.tolerance.before, search.tolerance.after
    support: dict[tuple[str, Any, Any], set[int]] = defaultdict(set)
    for entity_id in search.timeline.entity_ids:
        if search.is_numeric(entity_id):
            continue
        step = search.timeline[entity_id]
        for n, anchor in enumerate(anchors):
            for tr in step.transitions(anchor.at - before, anchor.at + after + 1e-6):
                if tr.old_state != tr.state and tr.old_state is not None:
                    support[(entity_id, tr.old_state, tr.state)].add(n)
    needed = max(1, math.ceil(len(anchors) / 2))
    taken = [h.at for h in outside]
    recurrences = []
    for (entity_id, old, new), hits in support.items():
        if len(hits) < needed:
            continue
        for tr in search.timeline[entity_id].transitions():
            if tr.old_state != old or tr.state != new or not search.labels.is_unlabeled(tr.at):
                continue
            if any(abs(tr.at - t) <= _MERGE for t in taken):
                continue
            recurrences.append(
                Hint(
                    "recurrence",
                    tr.at,
                    similarity(tr.at),
                    entity_id=entity_id,
                    from_=old,
                    to=new,
                    description=f"{entity_id} changed from {old} to {new}, as at {len(hits)} of {len(anchors)} labeled moments",
                )
            )
    recurrences.sort(key=lambda h: (-h.similarity, h.at))
    return outside + recurrences[: options.max_hints]


# Entry point


@dataclass(frozen=True, slots=True)
class SearchContext:
    search: _Search
    anchor_words: str  # how explanations name the anchors ("marked starts", ...)


def suggest(
    timeline: Timeline,
    labels: Labels,
    *,
    time_zone: str | tzinfo,
    sun_events: SunEvents | None = None,
    options: SuggestOptions | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SuggestResult:
    """Search candidate automations that fire at the labeled anchors and nowhere they should not; see the module docstring.

    `should_stop` is polled between trigger searches; when it returns True the search raises `SuggestCancelled` (it runs in an executor thread, which cannot be cancelled from outside).
    """
    options = options or SuggestOptions()

    def check() -> None:
        if should_stop is not None and should_stop():
            raise SuggestCancelled

    while_only = not labels.anchors and bool(labels.while_periods)
    if while_only:
        starts = [Label(p.start, p.end, "fire", "start") for p in labels.while_periods]
        labels = Labels([*labels.labels, *starts], labels.tolerance)
    if not labels.anchors:
        return SuggestResult()
    evaluator = Evaluator(timeline, time_zone=time_zone, sun_events=sun_events)
    search = _Search(timeline, labels, evaluator, options)
    anchors = labels.anchors
    paired = any(label.anchor == "both" for label in labels.fire_periods)
    starts = [a for a in anchors if a.edge == "start"]
    ends = [a for a in anchors if a.edge == "end"]
    check()
    if paired and starts and ends:
        start_side = search.side(starts, "start", options.side_beam)
        end_side = search.side(ends, "end", options.side_beam)
        if start_side and end_side:
            bases = [Candidate((*s, *e)) for s in start_side for e in end_side]
        else:
            bases = [Candidate(t) for t in start_side or end_side]
    else:
        bases = [Candidate(t) for t in search.side(anchors, None, options.beam)]

    scored: list[_Scored] = []
    for base in bases:
        check()
        scored += search.grow(base)
    check()
    scored.sort(key=lambda s: s.key)
    kept: list[_Scored] = []
    seen: set[tuple[Any, ...]] = set()
    for item in scored:
        if item.stats.anchors_hit == 0 or item.signature in seen:
            continue
        seen.add(item.signature)
        kept.append(item)
        if len(kept) >= options.top_k:
            break

    edges = {a.edge for a in anchors}
    if while_only:
        words = "while-period starts"
    elif edges == {"start"}:
        words = "marked starts"
    elif edges == {"end"}:
        words = "marked ends"
    else:
        words = "marked starts and ends"
    suggestions = []
    for rank, item in enumerate(kept):
        explanation = explain(item.candidate, item.stats, words)
        alias = f"Suggested: {describe(item.candidate)[:80]}"
        config = render_automation(item.candidate, alias=alias, description=explanation)
        suggestions.append(Suggestion(rank, item.candidate, item.stats, config, explanation))
    return SuggestResult(
        suggestions, _hints(search, anchors, suggestions), search.evaluated, context=SearchContext(search, words)
    )
