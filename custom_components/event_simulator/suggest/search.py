"""The miner's search: candidate automations that explain the labels, ranked, with hints from outside the labels.

`suggest(timeline, labels, time_zone=..., sun_events=..., options=...)` returns a `SuggestResult`: the top-K `Suggestion`s (candidate, `Stats`, rendered config, plain-language explanation) and `Hint`s (outside fires of the top suggestions, and recurrences of the labeled transitions elsewhere in the window, each with a similarity to the labeled moments).

How it searches (see docs/superpowers/specs/2026-10-05-suggestions-design.md):

1. Sides. A label anchored at `both` asks for an on/off pair, so the start and end anchors are searched independently (trigger ids `start` / `end`) and combined. Otherwise one side covers every anchor and triggers carry no id. Labels that are only `while` periods also anchor at each period's start: the automation should act when the period begins, with conditions that hold throughout it.
2. Trigger candidates around the anchors, each evaluated over the whole window:
   - `state`: every state change near an anchor (to, and from + to), attribute changes when the state is static, and `for:` when a state consistently began a similar lag before the anchors (quantized to friendly durations). Anchors that follow changes in two or three directions also get one trigger listing them all (a toggle). A free-form entity (`FREE_FORM` or more values in the window, most seen once: a timestamp, a counter, free text) only offers values it took more than once, and a trigger on any change of its state (`to: null`).
   - rate of change (#42): for a numeric entity, a Trend helper turning on (its least-squares gradient over a sample window of 5 min to 1 h passing `min_gradient`, rising or falling) or a Derivative helper (time-weighted over the window) crossing a threshold, the helper's states computed as it does (`rates.py`) and its threshold searched as a level's is (on a rounded copy of its series). Offered only for a direction where the entity's levels fall short (they miss anchors, or fire in must-not periods or unlabeled time within the labeled stretch) and the rate does better, and more often than chance; a derivative only on the trend's best windows. Each helper costs as much simplicity as a condition, so a level that does as well stays ahead. The card's `helpers` say what to create (`model.helper_spec`), named after the source and never an existing entity (`helper_names`); a variant reads its card's helper entity with its own options.
   - `numeric_state`: `above` / `below`, the threshold searched over the full window's values. The behavior of a threshold only changes at observed values, so each gap between consecutive distinct values is one candidate, represented by its roundest number; gaps are scored by how many anchors a crossing hits and how often the value crosses elsewhere. `for:` as for state. And a state trigger with no from/to (every report, as blueprint controllers re-evaluate), with `to: null` too when some reports change attributes only, when the anchors line up with its writes (within `ALIGN` seconds, more often than chance: any chatty sensor has some write within the tolerance of every anchor). A numeric attribute that changes while the state stays put gets the same trigger on the attribute. Conditions at fire time (`numeric_state` above/below) then narrow it, the template-free form of "every report while it is warm".
   - `event`: for each type of recorded bus event (`zha_event`, `tag_scanned`, ...) with events near the anchors, the type alone and the `event_data` that picks out the anchored events: greedily, the `(key, value)` of the anchored events that removes the most other events of the type while keeping every anchor the trigger hits, from no data and from each value the anchored events share (a command, a button). Only scalar values are used, and no string Home Assistant's template rendering would read as another type ("1" becomes 1). A type is offered only when the anchors fall near its events more often than chance would.
   - `time` / `sun`: when the anchors cluster by local clock time, or by sunrise/sunset offset; whichever clusters more anchors with less spread wins. The time is chosen at or just before the cluster and rounded.
   - `calendar`: for each calendar with events (the scenario's `calendars`, else the events its recorded states name), the anchors' lag behind its events' starts and, separately, ends, clustered; the offset is chosen as a clock time is (at or just before, rounded, at most `MAX_CALENDAR_OFFSET` either way). Offered when the anchors fall near its fires more often than chance. A condition on the calendar's `message` attribute (the summary of the event it names at the fire) narrows it to some events; when that attribute cannot tell them apart (it names another event at the fire), the explanation says a template condition on the event's summary would.
   - `time` at an entity: for each input_datetime and `device_class: timestamp` sensor, the anchors' lag behind the time it holds around them (a moment, or for a time-only helper a time of day, circularly), clustered; the offset is chosen as a clock time is (at or just before, rounded, at most `MAX_ENTITY_OFFSET`). Offered when the anchors fall near its fires more often than chance (a sensor stamping its own reports meets any label).
   Triggers that hit fewer than `min_recall` of the best trigger's anchors are dropped before any condition search, and only the best `beam` go on.
   When no single trigger hits every anchor of a side, ORs of up to `max_triggers` triggers (possibly on different entities or of different kinds) join them: greedy set cover from each of the best `union_seeds` triggers, each step adding the trigger, searched again around the anchors still missed, that hits the most of them with the fewest bad fires. A trigger joins only as evidence, not chance: it hits the missed anchors more often than its fires would at random (binomial tail at `union_alpha`), and one fitted to a single moment (a clock time, a threshold, a `for:`) never joins for one anchor. The parts of an OR keep their side's trigger id, so a start/end pair stays a pair; each extra part costs as much simplicity as a condition.
3. Conditions, greedily: from each trigger (or trigger pair), add the condition that removes the most bad fires (must-not first, then unlabeled within the labeled stretch, then beyond it) while keeping every anchor hit and every `while` period held, up to `max_conditions`, branching on the best `branch` choices at each step. Condition values come from the state at the anchor fires (and throughout `while` periods) against the state at the bad fires; numeric thresholds are the roundest number between the good and the bad values.
4. Every candidate is scored exactly by the evaluator, deduplicated (candidates that fire at the same instants keep only the simplest), ranked by `score.rank_key` and `tie_break` (fewer options, fires closer to the anchors, fewer unlabeled fires overall), and folded into cards: candidates of one structure (`kinds.structure`: the same parts with other thresholds, durations, offsets or clock times) are one card, the best of them its suggestion and up to `MAX_VARIANTS` more its `variants`. Cards with clock-like parts (a time of day, a weekday, the sun) then go after cards with fewer that do about as well (`score.prefer_entities`). The result is the first `top_k` cards plus, past them, the best `KIND_COVERAGE` of each trigger kind found (`kinds.candidate_kinds`), at most `MAX_CARDS`.
   The greedy condition step (3) likewise tries a clock-like condition after an entity condition that removes nearly as many bad fires, and always keeps a place in its branch for the best entity condition.
5. When the best suggestion still misses anchors or fires where it should not, one tracked numeric entity rising above another of the same unit (what only a template trigger expresses) is checked against the anchors; one that fits them better is named in the explanations (`Relation`), never generated.
"""

from __future__ import annotations

import ast
import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta, tzinfo
from statistics import median
from typing import Any, Literal

from .evaluate import Evaluator, Fire, SunEvents, entity_time
from .labels import Anchor, Label, Labels, Tolerance
from .model import (
    DAY,
    WEEKDAYS,
    CalendarTrigger,
    Candidate,
    Condition,
    DerivativeTrigger,
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
    Trigger,
    TrendTrigger,
    helper_spec,
    render_automation,
)
from .kinds import candidate_kinds, structure
from .model import UNIT_TIMES
from .score import Stats, clock_parts, clock_roundness, prefer_entities, rank_key, roundness, score_candidate, simplicity
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
MAX_ENTITY_OFFSET = 7 * DAY  # the furthest a time trigger at an entity's time fires from that time
MAX_CALENDAR_OFFSET = 2 * DAY  # the furthest a calendar trigger fires from its events' starts or ends
RELATION_ENTITIES = 12  # numeric entities (with a unit) whose pairs are checked for a relation that needs a template trigger
RELATION_RECALL = 0.8  # share of the anchors such a relation must hit
# `event_data` keys in the order they are preferred when several pick out the same events (a device's id over its radio address).
MAX_CARDS = 25  # most suggestions one result carries: they are validated in one sandbox child, which takes at most this many automations (sandbox/engine.py MAX_AUTOMATIONS)
MAX_VARIANTS = 4  # near-duplicates folded into one card
KIND_COVERAGE = 3  # cards of each trigger kind found that the result carries, past the top-K, so a filter by kind is never empty
RATE_WINDOWS = (300, 600, 900, 1800, 3600)  # sample windows of the Trend and Derivative helpers the miner proposes (#42)
RATE_THRESHOLDS = 2  # thresholds kept per helper, window and direction
RATE_DERIVATIVE_WINDOWS = 2  # windows a Derivative helper is tried on: the Trend helper's best
RATE_BEAM = 3  # rate triggers of one helper kind, source and direction that go on to the condition search
MAX_TREND_SAMPLES = 1000
EVENT_KEYS = ("device_id", "entity_id", "tag_id", "device_ieee", "unique_id", "id", "command", "type", "subtype", "action", "event", "button")


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
class Variant:
    """A near-duplicate of a suggestion (the same structure, other numbers: `kinds.structure`), folded into its card. Always the miner's estimate: exact validation replays only the card itself."""

    candidate: Candidate
    stats: Stats
    config: dict[str, Any]
    explanation: str
    helpers: tuple[dict[str, Any], ...] = ()  # its own helpers' options, under the card's helper entities (#42)

    def as_dict(self) -> dict[str, Any]:
        return {"config": self.config, "stats": self.stats.as_dict(), "explanation": self.explanation, "exact": False, "helpers": [dict(h) for h in self.helpers]}


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
    variants: tuple[Variant, ...] = ()  # near-duplicates, best first
    helpers: tuple[dict[str, Any], ...] = ()  # helpers the suggestion needs (#42)

    @property
    def kinds(self) -> list[str]:
        return candidate_kinds(self.candidate, self._helper_entities)[0]

    @property
    def trigger_kinds(self) -> list[str]:
        return candidate_kinds(self.candidate, self._helper_entities)[1]

    @property
    def _helper_entities(self) -> set[str]:
        return {h["entity_id"] for h in self.helpers if h.get("entity_id")}

    def as_dict(self) -> dict[str, Any]:
        kinds, trigger_kinds = candidate_kinds(self.candidate, self._helper_entities)
        out = {
            "rank": self.rank,
            "config": self.config,
            "stats": self.stats.as_dict(),
            "explanation": self.explanation,
            "kinds": kinds,
            "trigger_kinds": trigger_kinds,
            "variants": [v.as_dict() for v in self.variants],
            "helpers": [dict(h) for h in self.helpers],
        }
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
        suggestions = [s.as_dict() for s in self.suggestions]
        counts: Counter[str] = Counter(kind for s in suggestions for kind in s["kinds"])
        out: dict[str, Any] = {
            "suggestions": suggestions,
            "hints": [h.as_dict() for h in self.hints],
            "evaluated": self.evaluated,
            "kind_counts": dict(sorted(counts.items())),
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


def _amount(value: float, unit: str | None) -> str:
    return f"{round(value, 6):g} {unit}" if unit else f"{round(value, 6):g}"


def describe_trigger(trigger: Trigger) -> str:
    match trigger:
        case TrendTrigger():
            verb = "rises" if trigger.delta > 0 else "drops"
            return f"{trigger.entity_id} {verb} by more than {_amount(abs(trigger.delta), trigger.unit)} within {_duration_text(trigger.window)} (Trend helper)"
        case DerivativeTrigger():
            threshold = trigger.above if trigger.above is not None else trigger.below
            per = UNIT_TIMES[trigger.unit_time]
            verb = "rises" if threshold > 0 else "drops"
            rate = f"{threshold:g} {trigger.unit or ''}/{trigger.unit_time}".replace(" /", "/")
            change = _amount(abs(threshold) * trigger.window / per, trigger.unit)
            return f"{trigger.entity_id} {verb} by more than {change} within {_duration_text(trigger.window)} (Derivative helper {'above' if trigger.above is not None else 'below'} {rate})"
        case StateTrigger():
            target = trigger.entity_id if trigger.attribute is None else f"{trigger.entity_id}'s {trigger.attribute}"
            if trigger.entity_id.startswith("calendar.") and trigger.attribute is None and trigger.from_ is None and not trigger.for_ and trigger.to in (("on",), ("off",)):
                # A calendar entity is on while one of its events runs (its attributes name the event): a calendar trigger without an offset, as a state trigger.
                return f"an event of {trigger.entity_id} {'starts' if trigger.to == ('on',) else 'ends'}"
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
        case EntityTimeTrigger():
            held = f"the time in {trigger.entity_id}"
            if not trigger.offset:
                return f"at {held}"
            return f"{_duration_text(trigger.offset)} {'before' if trigger.offset < 0 else 'after'} {held}"
        case SunTrigger():
            return f"at {trigger.event}" if not trigger.offset else _sun_text(trigger.event, trigger.offset)
        case CalendarTrigger():
            moment = f"an event of {trigger.entity_id} {'starts' if trigger.event == 'start' else 'ends'}"
            if not trigger.offset:
                return moment
            return f"{_duration_text(trigger.offset)} {'before' if trigger.offset < 0 else 'after'} {moment}"
        case EventTrigger():
            text = f"a {trigger.event_type} event arrives"
            if trigger.event_data:
                text += " with " + ", ".join(f"{key} {value}" for key, value in trigger.event_data)
            return text
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
        clock = isinstance(trigger, (TimeTrigger, EntityTimeTrigger, SunTrigger)) or (isinstance(trigger, CalendarTrigger) and bool(trigger.offset))
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


@dataclass(frozen=True, slots=True)
class Relation:
    """One tracked numeric entity rising above another (`rising` > `over`): what a template trigger would say, and how it does on the labels."""

    rising: str
    over: str
    anchors_hit: int
    violations: int  # fires in must-not periods
    outside_nearby: int  # unlabeled fires within the labeled stretch

    def beats(self, stats: Stats) -> bool:
        """Whether a suggestion that misses anchors, fires in must-not periods or fires unlabeled within the labeled stretch would do better as this relation."""
        if not (stats.missed or stats.no_fire_violations or stats.outside_nearby):
            return False
        return (-self.anchors_hit, self.violations, self.outside_nearby) < (-stats.anchors_hit, len(stats.no_fire_violations), stats.outside_nearby)

    @property
    def note(self) -> str:
        return f"The labeled moments line up with {self.rising} rising above {self.over}; that needs a template trigger"


def explain(candidate: Candidate, stats: Stats, anchor_words: str, relation: Relation | None = None, note: str | None = None) -> str:
    """`describe` plus how it did: "...; hits 4/4 marked starts, fires 2 other times". With a `relation` the labels follow better than this candidate, a sentence saying a template trigger would be needed; a `note` (`_Search.calendar_note`) is one more sentence."""
    text = f"{describe(candidate)}; hits {stats.anchors_hit}/{stats.anchors_total} {anchor_words}"
    if stats.no_fire_violations:
        text += f", fires {_plural(len(stats.no_fire_violations), 'time')} in must-not periods"
    if stats.outside_fires:
        text += f", fires {len(stats.outside_fires)} other {'time' if len(stats.outside_fires) == 1 else 'times'}"
    if stats.while_total:
        text += f", holds throughout {stats.while_held}/{stats.while_total} while periods"
    if relation is not None and relation.beats(stats):
        text += f". {relation.note}."
    if note:
        text += f". {note}."
    return text


# Option counting for ranking ties


def complexity(candidate: Candidate) -> int:
    """Options beyond the bare minimum (from, not_from, attribute, both bounds, event data keys): fewer reads simpler."""
    count = 0
    for part in (*candidate.triggers, *candidate.conditions):
        match part:
            case StateTrigger():
                count += sum(v is not None for v in (part.from_, part.not_from, part.not_to, part.attribute))
            case EventTrigger():
                # Every key beyond the first, and once more each technical one (a cluster, an endpoint; not in EVENT_KEYS): "device X, command on" reads better than "cluster 6, command on" for the same events.
                count += max(0, len(part.event_data) - 1) + sum(key not in EVENT_KEYS for key, _ in part.event_data)
            case NumericStateTrigger() | NumericStateCondition():
                count += part.above is not None and part.below is not None
            case DerivativeTrigger():
                count += 1  # a threshold in a unit per time over a window: a Trend helper saying the same reads simpler
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


RATE_LEVELS = 200  # about how many distinct values a helper's series keeps for the threshold search


def _quantized(step: StepFunction) -> StepFunction:
    """A copy of a helper's numeric series on a round grid of about `RATE_LEVELS` values over its range (a gradient takes a new value at every report, and the threshold search costs as much as the values it has to step over). The thresholds found are round anyway, and each trigger is then scored on the exact series."""
    values = step.numeric_values()
    numbers = sorted(v for v in values if isinstance(v, float))
    if len(set(numbers)) <= RATE_LEVELS:
        return step
    lo, hi = numbers[len(numbers) // 100], numbers[-1 - len(numbers) // 100]
    wanted = max(hi - lo, 1e-9) / RATE_LEVELS
    grid = 10 ** math.floor(math.log10(wanted))
    grid = next(g for g in (grid, 2 * grid, 5 * grid, 10 * grid) if g >= wanted)
    copy = StepFunction(step.entity_id)
    copy.times, copy.states, copy.attributes, copy.changed, copy.seeded = step.times, step.states, step.attributes, step.changed, step.seeded
    copy._numeric[None] = [round(round(v / grid) * grid, 10) if isinstance(v, float) else v for v in values]  # noqa: SLF001
    return copy


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


def _anchor_cluster(lags: Sequence[tuple[float, int]], width: float) -> list[tuple[float, int]]:
    """Of `(lag, anchor index)` pairs, the run of lags within `width` of each other covering the most distinct anchors (smallest spread among equals)."""
    ordered = sorted(lags)
    best: list[tuple[float, int]] = []
    best_key: tuple[int, float] = (0, 0.0)
    lo = 0
    for hi in range(len(ordered)):
        while ordered[hi][0] - ordered[lo][0] > width:
            lo += 1
        window = ordered[lo : hi + 1]
        key = (len({n for _, n in window}), window[0][0] - window[-1][0])
        if key > best_key:
            best, best_key = window, key
    return best


def _relation_crossings(a: StepFunction, b: StepFunction, lo: float, hi: float) -> tuple[list[float], list[float]]:
    """When `a` rises above `b`, and when `b` rises above `a`, in `(lo, hi)`: the moments a template trigger on `a > b` (or `b > a`) turns true, at the writes of either. Not comparable (a value missing or not a number) resets; a relation already true at `lo` has not risen."""
    va, vb = a.numeric_values(), b.numeric_values()
    writes = sorted({a.times[i] for i in a.write_range(lo, hi)} | {b.times[i] for i in b.write_range(lo, hi)})
    a_up: list[float] = []
    b_up: list[float] = []
    previous: int | None = None
    for n, t in enumerate((lo, *(t for t in writes if t > lo))):
        i, j = a.index_at(t), b.index_at(t)
        x = va[i] if i >= 0 else None
        y = vb[j] if j >= 0 else None
        state = (x > y) - (x < y) if isinstance(x, float) and isinstance(y, float) else None
        if n and previous is not None and state is not None and state != previous:
            if state == 1:
                a_up.append(t)
            elif state == -1:
                b_up.append(t)
        previous = state
    return a_up, b_up


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
    """After `score.rank_key`: fewer options, fires closer to the anchors, fewer unlabeled fires overall (beyond the labeled stretch too), a calendar trigger with the smallest offset (`_cause_rank`), then the description, so the order is stable."""
    return (complexity(candidate), _fit_error(stats, anchors, fires), len(stats.outside_fires), _cause_rank(candidate), describe(candidate))


def dedupe(evaluator: Evaluator, labels: Labels, candidates: Iterable[Candidate]) -> list[Candidate]:
    """One candidate per set of fire instants (and `while` periods held): the simplest, in order of first appearance."""
    groups: dict[tuple[Any, ...], list[Candidate]] = {}
    for candidate in candidates:
        fires = evaluator.fires(candidate)
        stats = score_candidate(evaluator, candidate, labels, fires)
        groups.setdefault(_Scored(candidate, stats, fires).signature, []).append(candidate)
    return [min(group, key=lambda c: (simplicity(c), complexity(c), _cause_rank(c), describe(c))) for group in groups.values()]


def _cause_rank(candidate: Candidate) -> tuple[int, float]:
    """Among candidates firing at the same instants: a calendar trigger before anything else (it names why; a clock time on the event's weekday drifts the first time the event moves), the smallest offset first (7 h before an all-day event starts, not 31 h before it ends)."""
    calendars = [trigger for trigger in candidate.triggers if isinstance(trigger, CalendarTrigger)]
    return (-len(calendars), sum(abs(trigger.offset) for trigger in calendars))


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


def _event_value(value: Any) -> bool:
    """Whether an event data value can go in a trigger's `event_data`: a scalar that renders back to itself. Home Assistant renders `event_data` as a template and parses a static string's result, so "1", "True" or "[1]" would compare as 1, True, [1] and never match the event's string."""
    if isinstance(value, bool | int | float):
        return True
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return True
    return isinstance(parsed, str)


def _event_pair_order(pair: tuple[str, Any]) -> tuple[Any, ...]:
    key = pair[0]
    return (EVENT_KEYS.index(key) if key in EVENT_KEYS else len(EVENT_KEYS), key, str(pair[1]))


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
        self._held: dict[str, list[tuple[str, Any] | None]] = {}
        self._samples: dict[tuple[str, float], int] = {}
        self._rate_cache: dict[tuple[Any, ...], _Crossings | None] = {}
        self._quantized: dict[int, StepFunction] = {}  # by the id of a helper step the evaluator keeps
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
        found |= self._event_triggers(anchors, trigger_id, min_hits)
        found |= self._entity_time_triggers(anchors, trigger_id, min_hits)
        found |= self._calendar_triggers(anchors, trigger_id, min_hits)
        found.update(clock_triggers(self.ev, self.labels, anchors, trigger_id, min_hits))
        return found

    def _anchors_hit(self, fires: Sequence[float], anchors: Sequence[Anchor]) -> int:
        before, after = self.tolerance.before, self.tolerance.after
        return sum(bisect_right(fires, a.at + after) > bisect_left(fires, a.at - before) for a in anchors)

    def _event_triggers(self, anchors: Sequence[Anchor], tid: str | None, min_hits: int) -> set[Trigger]:
        """Event triggers on each bus event type with events near the anchors: the type alone, and with the `event_data` that singles out the anchored events (see the module docstring)."""
        found: set[Trigger] = set()
        before, after = self.tolerance.before, self.tolerance.after
        span = max(self.timeline.end - self.timeline.start, 1.0)
        for event_type in self.timeline.event_types:
            bus = self.timeline.bus_events(event_type)
            support: dict[tuple[str, Any], set[int]] = defaultdict(set)
            near = 0
            for n, anchor in enumerate(anchors):
                lo, hi = bisect_left(bus.times, anchor.at - before), bisect_right(bus.times, anchor.at + after)
                near += hi > lo
                for i in range(lo, hi):
                    for key, value in bus.data[i].items():
                        if _event_value(value):
                            support[(key, value)].add(n)
            if near < min_hits:
                continue
            pairs = sorted((pair for pair, hits in support.items() if len(hits) >= min_hits), key=_event_pair_order)

            def trigger(data: tuple[tuple[str, Any], ...], event_type: str = event_type) -> EventTrigger:
                return EventTrigger(event_type, tuple(sorted(data, key=_event_pair_order)), id=tid)

            def specialize(data: tuple[tuple[str, Any], ...]) -> list[EventTrigger]:
                """Greedy: add the pair that drops the most other events while keeping every anchor hit, until none drops any."""
                path = [trigger(data)]
                fires = self.ev.trigger_fires(path[0])
                hits = self._anchors_hit(fires, anchors)
                while True:
                    best: tuple[int, EventTrigger, list[float]] | None = None
                    for pair in pairs:
                        if pair in data or any(key == pair[0] for key, _ in data):
                            continue
                        candidate = trigger((*data, pair))
                        narrowed = self.ev.trigger_fires(candidate)
                        if len(narrowed) >= len(fires) or self._anchors_hit(narrowed, anchors) < hits:
                            continue
                        if best is None or len(narrowed) < best[0]:
                            best = (len(narrowed), candidate, narrowed)
                    if best is None:
                        return path
                    _, chosen, fires = best
                    data = chosen.event_data
                    path.append(chosen)

            # The type alone, then each value the anchored events share as a start (a command among several of one device). Of the data picking out the same events, only the fewest, best-known keys stay (a device's id over its command or its radio address).
            best: dict[tuple[float, ...], tuple[tuple[Any, ...], EventTrigger]] = {}
            for start in ((), *((pair,) for pair in pairs)):
                for candidate in specialize(start):
                    fires = self.ev.trigger_fires(candidate)
                    hits = self._anchors_hit(fires, anchors)
                    chance = min(1.0, len(fires) * (before + after) / span)
                    if hits < min_hits or _binomial_tail(len(anchors), hits, chance) > ALIGN_ALPHA:
                        continue
                    order = (len(candidate.event_data), [_event_pair_order(pair) for pair in candidate.event_data])
                    key = tuple(fires)
                    if key not in best or order < best[key][0]:
                        best[key] = (order, candidate)
            found.update(candidate for _, candidate in best.values())
        return found

    def held_times(self, entity_id: str) -> list[tuple[str, Any] | None]:
        """The time each entry of an entity holds for a time trigger at it (`evaluate.entity_time`), computed once."""
        if (found := self._held.get(entity_id)) is None:
            step = self.timeline[entity_id]
            found = self._held[entity_id] = [entity_time(entity_id, step.states[i], step.attributes[i]) for i in range(len(step))]
        return found

    def _entity_time_triggers(self, anchors: Sequence[Anchor], tid: str | None, min_hits: int) -> set[Trigger]:
        """Time triggers at the time an input_datetime or a timestamp sensor holds, plus an offset: when the anchors lie a constant offset from the time in effect around them (a moment, or a time of day for a time-only helper), more often than chance would."""
        found: set[Trigger] = set()
        before, after = self.tolerance.before, self.tolerance.after
        span = max(self.timeline.end - self.timeline.start, 1.0)
        for entity_id in self.timeline.entity_ids:
            domain = entity_id.split(".", 1)[0]
            step = self.timeline[entity_id]
            if domain == "sensor":
                # The device class is static: a look at the first and last entries spares reading a chatty sensor's every state.
                if not any(step.attributes[i].get("device_class") == "timestamp" for i in {0, len(step) - 1} if len(step)):
                    continue
            elif domain != "input_datetime":
                continue
            held = self.held_times(entity_id)
            if not any(held):
                continue
            lags: list[tuple[float, int]] = []
            for n, anchor in enumerate(anchors):
                for i in range(max(step.index_at(anchor.at - before), 0), step.index_at(anchor.at + after) + 1):
                    if (value := held[i]) is None:
                        continue
                    if value[0] == "daily":
                        clock = self._local_clock(anchor.at)[0]
                        lag = (clock - value[1] + DAY / 2) % DAY - DAY / 2
                    else:
                        lag = anchor.at - self.ev.entity_moment(value, 0)
                    if abs(lag) <= MAX_ENTITY_OFFSET:
                        lags.append((lag, n))
            cluster = _anchor_cluster(lags, before + after)
            if len({n for _, n in cluster}) < min_hits:
                continue
            offset = _pick_moment(sorted(lag for lag, _ in cluster), self.tolerance)
            if offset is None:
                continue
            trigger = EntityTimeTrigger(entity_id, offset=offset, id=tid)
            fires = self.ev.trigger_fires(trigger)
            hits = self._anchors_hit(fires, anchors)
            chance = min(1.0, len(fires) * (before + after) / span)
            if hits >= min_hits and _binomial_tail(len(anchors), hits, chance) <= ALIGN_ALPHA:
                found.add(trigger)
        return found

    def _calendar_triggers(self, anchors: Sequence[Anchor], tid: str | None, min_hits: int) -> set[Trigger]:
        """Calendar triggers at its events' starts or ends plus an offset: when the anchors lie a constant offset from them, more often than chance would."""
        found: set[Trigger] = set()
        before, after = self.tolerance.before, self.tolerance.after
        span = max(self.timeline.end - self.timeline.start, 1.0)
        for entity_id in self.timeline.calendar_ids:
            calendar = self.timeline.calendar(entity_id)
            if calendar is None:
                continue
            for event, times in (("start", calendar.starts), ("end", calendar.ends_sorted)):
                lags: list[tuple[float, int]] = []
                for n, anchor in enumerate(anchors):
                    lo = bisect_left(times, anchor.at - MAX_CALENDAR_OFFSET - after)
                    hi = bisect_right(times, anchor.at + MAX_CALENDAR_OFFSET + before)
                    lags += [(anchor.at - at, n) for at in times[lo:hi]]
                cluster = _anchor_cluster(lags, before + after)
                if len({n for _, n in cluster}) < min_hits:
                    continue
                offset = _pick_moment(sorted(lag for lag, _ in cluster), self.tolerance)
                if offset is None or abs(offset) > MAX_CALENDAR_OFFSET:
                    continue
                trigger = CalendarTrigger(entity_id, event, offset=offset, id=tid)
                fires = self.ev.trigger_fires(trigger)
                hits = self._anchors_hit(fires, anchors)
                chance = min(1.0, len(fires) * (before + after) / span)
                if hits >= min_hits and _binomial_tail(len(anchors), hits, chance) <= ALIGN_ALPHA:
                    found.add(trigger)
        return found

    def calendar_note(self, candidate: Candidate, stats: Stats) -> str | None:
        """When a calendar trigger of the candidate fires for events that line up with the labels and for others that do not, and their summaries tell the two apart (no condition on the calendar's `message` does): what would."""
        if not (stats.outside_nearby or stats.no_fire_violations):
            return None
        for trigger in candidate.triggers:
            if not isinstance(trigger, CalendarTrigger) or (calendar := self.timeline.calendar(trigger.entity_id)) is None:
                continue
            if any(isinstance(c, StateCondition) and c.entity_id == trigger.entity_id and c.attribute == "message" for c in candidate.conditions):
                continue
            good: set[str] = set()
            bad: set[str] = set()
            for at, n in self.ev.calendar_fires(trigger):
                if not self.ev.conditions_hold(candidate.conditions, at):
                    continue
                kind = self.labels.classify(at, trigger.id)
                summary = calendar.events[n].summary
                if kind == "anchor":
                    good.add(summary)
                elif kind == "no_fire" or (kind == "unlabeled" and self.labels.in_span(at)):
                    bad.add(summary)
            if good and bad and not good & bad:
                names = " or ".join(f'"{name}"' for name in sorted(good)[:3]) + (" ..." if len(good) > 3 else "")
                return f"Only the events of {trigger.entity_id} named {names} line up with the labels; a template condition on trigger.calendar_event.summary would keep just those"
        return None

    def relation(self, anchors: Sequence[Anchor]) -> Relation | None:
        """The best "one sensor rises above another" among the tracked numeric entities that share a unit, when the anchors follow it (at least `RELATION_RECALL` of them, more often than chance): what only a template trigger could express."""
        if len(anchors) < 2:
            return None
        by_unit: dict[str, list[str]] = defaultdict(list)
        for entity_id in self.timeline.entity_ids:
            step = self.timeline[entity_id]
            unit = step.attributes[-1].get("unit_of_measurement") if len(step) else None
            if unit and self.is_numeric(entity_id):
                by_unit[str(unit)].append(entity_id)
        entities = [e for group in by_unit.values() if len(group) > 1 for e in group][:RELATION_ENTITIES]
        before, after = self.tolerance.before, self.tolerance.after
        span = max(self.timeline.end - self.timeline.start, 1.0)
        needed = max(2, math.ceil(len(anchors) * RELATION_RECALL))
        best: tuple[tuple[Any, ...], Relation] | None = None
        for group in by_unit.values():
            members = [e for e in group if e in entities]
            for k, a in enumerate(members):
                for b in members[k + 1 :]:
                    sa, sb = self.timeline[a], self.timeline[b]
                    # Around the anchors first (cheap), the whole window only for a pair crossing near enough of them.
                    near = [0, 0]
                    for anchor in anchors:
                        for side, found in enumerate(_relation_crossings(sa, sb, anchor.at - before, anchor.at + after + 1e-6)):
                            near[side] += bool(found)
                    if max(near) < needed:
                        continue
                    whole = _relation_crossings(sa, sb, self.timeline.start, self.timeline.end)
                    for rising, over, fires in zip((a, b), (b, a), whole, strict=True):
                        hits = self._anchors_hit(fires, anchors)
                        if hits < needed or _binomial_tail(len(anchors), hits, min(1.0, len(fires) * (before + after) / span)) > ALIGN_ALPHA:
                            continue
                        kinds = [self.labels.classify(t) for t in fires]
                        matched = {anchor for t in fires for anchor in self.labels.anchors_matching(t)}
                        relation = Relation(
                            rising,
                            over,
                            len(matched),
                            kinds.count("no_fire"),
                            sum(kind == "unlabeled" and self.labels.in_span(t) for kind, t in zip(kinds, fires, strict=True)),
                        )
                        key = (-relation.anchors_hit, relation.violations, relation.outside_nearby, rising, over)
                        if best is None or key < best[0]:
                            best = (key, relation)
        return None if best is None else best[1]

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
        levels: dict[str, tuple[int, int]] = {}
        for direction in ("above", "below"):
            crossings = self.crossings(entity_id, direction)
            if not crossings.times or len(crossings.values) < 2:
                continue
            direct = _gap_scores(crossings, anchors, -before, after)
            for theta, covered, _ in _ranked_thresholds(crossings, direct, 3):
                if covered >= min_hits:
                    trigger = NumericStateTrigger(entity_id, **{direction: theta}, id=tid)
                    found.add(trigger)
                    hits, must_not, near, _, _ = self.screen(trigger, anchors)
                    levels[direction] = min(levels.get(direction, (0, _INF)), (-hits, must_not + near))
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
        found |= self._rate_triggers(entity_id, anchors, tid, min_hits, levels)
        return found

    # Rate of change (#42)

    def max_samples(self, entity_id: str, window: float) -> int:
        """A Trend helper's `max_samples` for a sample window: room for every report the entity made in any span that long (the most seen, plus one), rounded up to a multiple of 5 past 10."""
        if (found := self._samples.get((entity_id, window))) is None:
            step = self.timeline[entity_id]
            times = [step.times[i] for i in step.write_range()]
            peak, lo = 1, 0
            for hi, t in enumerate(times):
                while t - times[lo] > window:
                    lo += 1
                peak = max(peak, hi - lo + 1)
            found = peak + 1
            if found > 10:
                found = -(-found // 5) * 5
            found = self._samples[(entity_id, window)] = min(max(found, 2), MAX_TREND_SAMPLES)
        return found

    def rate_crossings(self, trigger: TrendTrigger | DerivativeTrigger, direction: str) -> _Crossings | None:
        """The crossings of the helper a trigger reads (its numeric view, `Evaluator.helper_step`), computed once per helper and direction."""
        key = (trigger.entity_id, type(trigger).__name__, trigger.window, getattr(trigger, "unit_time", None), direction)
        if key not in self._rate_cache:
            step = self.ev.helper_step(trigger)
            if step is not None and (quantized := self._quantized.get(id(step))) is None:
                quantized = self._quantized[id(step)] = _quantized(step)
            self._rate_cache[key] = None if step is None else _crossings(quantized, direction)
        return self._rate_cache[key]

    def _rate_triggers(self, entity_id: str, anchors: Sequence[Anchor], tid: str | None, min_hits: int, levels: Mapping[str, tuple[int, int]]) -> set[Trigger]:
        """Triggers on a Trend or Derivative helper of a numeric entity: for each sample window and direction, the change over the window (a trend's gradient times the window) or the derivative crossing the thresholds the anchors cross. Offered only where they explain the anchors better than any level of the entity does (more anchors hit, or as many with fewer must-not and unlabeled fires within the labeled stretch), and more often than chance. A derivative says nearly what a trend says, so it is tried only on the trend's best `RATE_DERIVATIVE_WINDOWS` windows."""
        found: set[Trigger] = set()
        # A level hitting every anchor without a bad fire leaves a rate nothing to add in that direction.
        directions = [d for d in ("above", "below") if levels.get(d, (0, _INF)) > (-len(anchors), 0)]
        step = self.timeline[entity_id]
        if not directions or not len(step):
            return found
        unit = step.attributes[-1].get("unit_of_measurement")
        unit = unit if isinstance(unit, str) else None
        before, after = self.tolerance.before, self.tolerance.after
        near = sum(bisect_right(step.times, a.at + after) > bisect_left(step.times, a.at - before - max(RATE_WINDOWS)) for a in anchors)
        if near < min_hits:
            return found
        windows: list[tuple[tuple[Any, ...], float]] = []
        for window in RATE_WINDOWS:
            probe = TrendTrigger(entity_id, window, 1.0, self.max_samples(entity_id, window), unit=unit)
            best = (0, _INF)
            for direction in directions:
                kept, key = self._rate_thresholds(probe, direction, anchors, tid, min_hits, levels.get(direction, (0, _INF)))
                found |= kept
                best = min(best, key)
            windows.append((best, window))
        for _, window in sorted(windows)[:RATE_DERIVATIVE_WINDOWS]:
            trend = self.ev.helper_step(TrendTrigger(entity_id, window, 1.0, self.max_samples(entity_id, window)))
            # In the unit per minute, or per hour when the changes around the anchors are too slow to show in two decimals per minute.
            changes = [abs(v) for a in anchors if trend is not None and isinstance(v := trend.numeric_at(a.at + after), float)]
            unit_time = "min" if changes and max(changes) / (window / 60) >= 0.1 else "h"
            probe = DerivativeTrigger(entity_id, window, unit_time, above=1.0, unit=unit)
            for direction in directions:
                found |= self._rate_thresholds(probe, direction, anchors, tid, min_hits, levels.get(direction, (0, _INF)))[0]
        return found

    def _rate_thresholds(
        self, probe: TrendTrigger | DerivativeTrigger, direction: str, anchors: Sequence[Anchor], tid: str | None, min_hits: int, level: tuple[int, int]
    ) -> tuple[set[Trigger], tuple[int, int]]:
        """The best `RATE_THRESHOLDS` thresholds of one helper (`probe`'s) in one direction that beat `level` and chance, and the best `(-anchors hit, bad fires)` any threshold had."""
        found: set[Trigger] = set()
        best = (0, _INF)
        crossings = self.rate_crossings(probe, direction)
        if crossings is None or not crossings.times or len(crossings.values) < 2:
            return found, best
        before, after = self.tolerance.before, self.tolerance.after
        span = max(self.timeline.end - self.timeline.start, 1.0)
        scores = _gap_scores(crossings, anchors, -before, after)
        for theta, covered, _ in _ranked_thresholds(crossings, scores, 3 * RATE_THRESHOLDS):
            if len(found) >= RATE_THRESHOLDS or covered < min_hits or (theta <= 0 if direction == "above" else theta >= 0):
                continue
            if isinstance(probe, TrendTrigger):
                trigger: Trigger = replace(probe, delta=theta, id=tid)
            else:
                trigger = replace(probe, above=theta if direction == "above" else None, below=theta if direction == "below" else None, id=tid)
            hits, must_not, near, _, fires = self.screen(trigger, anchors)
            key = (-hits, must_not + near)
            best = min(best, key)
            if key >= level or hits < min_hits:
                continue
            if _binomial_tail(len(anchors), hits, min(1.0, len(fires) * (before + after) / span)) > ALIGN_ALPHA:
                continue
            found.add(trigger)
        return found, best

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

    def helpers(self, candidate: Candidate, ids: Mapping[tuple[Any, ...], str]) -> tuple[dict[str, Any], ...]:
        """The helpers a candidate needs (`model.helper_spec`), named by `ids`, titled after the source's friendly name."""
        found: dict[tuple[Any, ...], dict[str, Any]] = {}
        for trigger in candidate.helper_triggers:
            step = self.timeline.get(trigger.entity_id)
            name = step.attributes[-1].get("friendly_name") if step is not None and len(step) else None
            if not isinstance(name, str) or not name:
                name = trigger.entity_id.split(".", 1)[1].replace("_", " ").capitalize()
            role = ("rising" if trigger.delta > 0 else "falling") if isinstance(trigger, TrendTrigger) else "derivative"
            found.setdefault(trigger.helper_key, helper_spec(trigger, ids.get(trigger.helper_key), f"{name} {role}"))
        return tuple(found.values())

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
                key = (-hits, must_not, near, simplicity(single), complexity(single), unlabeled, _cause_rank(single), describe(single))
                screened.append((key, trigger, tuple(fires)))
        if not screened:
            return []
        most = -min(key[0] for key, *_ in screened)
        floor = max(min_hits, math.ceil(most * self.options.min_recall))
        screened.sort(key=lambda item: item[0])
        kept: list[Trigger] = []
        seen: set[tuple[float, ...]] = set()
        families: Counter[tuple[Any, ...]] = Counter()
        for key, trigger, fires in screened:
            if -key[0] < floor or fires in seen:
                continue
            if isinstance(trigger, (TrendTrigger, DerivativeTrigger)):
                # Windows and thresholds of one rate fire nearly alike and fold into one card: a few of them, not the whole beam.
                family = structure(Candidate((trigger,)))
                if families[family] >= RATE_BEAM:
                    continue
                families[family] += 1
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
        fitted = isinstance(trigger, (TimeTrigger, EntityTimeTrigger, SunTrigger, CalendarTrigger, NumericStateTrigger, TrendTrigger, DerivativeTrigger)) or bool(getattr(trigger, "for_", 0))
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
            if entity_id.startswith("calendar.") and (named := self._calendar_message_condition(entity_id, step, good, bad)) is not None:
                pool.add(named)
        pool.update(self._time_conditions(good, periods))
        if self.ev._sun_events is not None:  # noqa: SLF001
            pool.update(self._sun_conditions(good))
        return sorted(pool, key=lambda c: (type(c).__name__, describe_condition(c)))

    def _calendar_message_condition(self, entity_id: str, step: StepFunction, good: Sequence[float], bad: Sequence[float]) -> Condition | None:
        """The calendar names one of a few event summaries (its `message`) at every good fire and another at some bad one: the template-free way to keep some of its events."""
        def message(t: float) -> Any:
            attributes = step.attributes_at(t)
            return None if attributes is None else attributes.get("message")

        if self.labels.while_periods:
            return None
        messages = {message(t) for t in good}
        if not messages or None in messages or len(messages) > 3 or not all(isinstance(m, str) and self.recurs(entity_id, m, "message") for m in messages):
            return None
        if not any(message(t) not in messages for t in bad):
            return None
        return StateCondition(entity_id, tuple(sorted(messages)), attribute="message")

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
                    # A clock-like condition fits almost any few labels: it goes after an entity condition removing as many must-not fires and nearly as many (a fifth fewer) unlabeled ones within the labeled stretch, and after one alike in every way.
                    clock = clock_parts(part) > 0
                    near_credit = removed_near - removed_near // 5 if clock else removed_near
                    key = (-removed_nf, -near_credit, simplicity(part)[2], complexity(part), -removed_far, clock)
                    options.append((key, condition, nm))
                options.sort(key=lambda item: item[0])
                picked = options[: self.options.branch]
                # Clock-like conditions can fill the branch with windows a quarter hour apart: keep a place for the best entity condition.
                if picked and all(key[-1] for key, *_ in picked):
                    entity = next((item for item in options[len(picked):] if not item[0][-1]), None)
                    if entity is not None:
                        picked[-1] = entity
                for _, condition, nm in picked:
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


def _neighbors(ordered: Sequence[float], x: float, *, circular: bool = False) -> list[float]:
    """The values of sorted `ordered` that can be nearest to `x`: its neighbors, and on a circle (the clock) the first and last too."""
    i = bisect_left(ordered, x)
    picks = {i - 1, i} | ({0, len(ordered) - 1} if circular else set())
    return [ordered[j] for j in picks if 0 <= j < len(ordered)]


class _Similarity:
    """How much a moment looks like the labeled ones: the referenced entities' states, the clock time and the sun offset."""

    def __init__(self, search: _Search, anchors: Sequence[Anchor], entity_ids: Iterable[str]) -> None:
        self.search = search
        self.ev = search.ev
        times = [a.at for a in anchors]
        self.clocks = sorted(search._local_clock(t)[0] for t in times)  # noqa: SLF001
        self.sun = self.ev._sun_events is not None  # noqa: SLF001
        self.offsets = [sorted(o for t in times if (o := _sun_offset(self.ev, t, index)) is not None) for index in (0, 1)] if self.sun else []
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
            distance = min(min(abs(clock - c) % DAY, DAY - abs(clock - c) % DAY) for c in _neighbors(self.clocks, clock, circular=True))
            terms.append(1 - min(1.0, distance / 7200))
        if self.sun:
            best = 0.0
            for index, offsets in enumerate(self.offsets):
                offset = _sun_offset(self.ev, t, index)
                if offsets and offset is not None:
                    best = max(best, 1 - min(1.0, min(abs(offset - o) for o in _neighbors(offsets, offset)) / 7200))
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


def _with_coverage(cards: Sequence[list[_Scored]], top_k: int) -> list[list[_Scored]]:
    """The first `top_k` cards, then, in rank order, any card of a trigger kind that has fewer than `KIND_COVERAGE` so far (so a filter by kind is never empty), at most `MAX_CARDS` in all."""
    kept = list(cards[:top_k])
    have: Counter[str] = Counter(kind for card in kept for kind in candidate_kinds(card[0].candidate)[1])
    for card in cards[top_k:]:
        if len(kept) >= MAX_CARDS:
            break
        trigger_kinds = candidate_kinds(card[0].candidate)[1]
        if any(have[kind] < KIND_COVERAGE for kind in trigger_kinds):
            kept.append(card)
            have.update(trigger_kinds)
    return kept[:MAX_CARDS]


def helper_names(cards: Sequence[Candidate], taken: Iterable[str]) -> dict[tuple[Any, ...], str]:
    """An entity id for each helper the cards need (`model.HelperTrigger.helper_key`), in rank order: the helper's default id (`binary_sensor.<source>_rising`, ...), with `_2`, `_3`, ... when that is taken by an entity or by another helper."""
    used = set(taken)
    names: dict[tuple[Any, ...], str] = {}
    for candidate in cards:
        for trigger in candidate.helper_triggers:
            if trigger.helper_key in names:
                continue
            base = name = trigger.default_helper_id
            n = 2
            while name in used:
                name = f"{base}_{n}"
                n += 1
            used.add(name)
            names[trigger.helper_key] = name
    return names


def variant_names(card: Candidate, variant: Candidate, names: Mapping[tuple[Any, ...], str]) -> dict[tuple[Any, ...], str]:
    """A variant's helpers named as its card's: the same helper with other options (another window or sample count), so picking a variant changes the helper's options, not the automation."""
    found: dict[tuple[Any, ...], str] = {}
    for trigger in variant.helper_triggers:
        family = (type(trigger), trigger.entity_id, trigger.id)
        match = next((t for t in card.helper_triggers if (type(t), t.entity_id, t.id) == family), None)
        if match is not None and match.helper_key in names:
            found[trigger.helper_key] = names[match.helper_key]
    return found


@dataclass(frozen=True, slots=True)
class SearchContext:
    search: _Search
    anchor_words: str  # how explanations name the anchors ("marked starts", ...)
    relation: Relation | None = None  # what the labels follow that only a template trigger could express


def suggest(
    timeline: Timeline,
    labels: Labels,
    *,
    time_zone: str | tzinfo,
    sun_events: SunEvents | None = None,
    options: SuggestOptions | None = None,
    should_stop: Callable[[], bool] | None = None,
    taken: Iterable[str] = (),
) -> SuggestResult:
    """Search candidate automations that fire at the labeled anchors and nowhere they should not; see the module docstring.

    `should_stop` is polled between trigger searches; when it returns True the search raises `SuggestCancelled` (it runs in an executor thread, which cannot be cancelled from outside). `taken` are entity ids that exist already (besides the timeline's): a helper a suggestion needs never gets one (`helper_names`).
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
    # One per set of fire instants, then near-duplicates (one structure, other numbers) folded into cards, best first.
    # Of candidates firing at the same instants, the best placed, unless a later one has fewer clock-like parts and is no less simple (it fires alike and says why): it takes that place.
    unique: dict[tuple[Any, ...], tuple[int, tuple[int, ...], _Scored]] = {}
    for item in scored:
        if item.stats.anchors_hit == 0:
            continue
        clocks, simple = clock_parts(item.candidate), simplicity(item.candidate)
        found = unique.get(item.signature)
        if found is None or (clocks < found[0] and simple <= found[1]):
            unique[item.signature] = (clocks, simple, item)
    groups: dict[tuple[Any, ...], list[_Scored]] = {}
    for *_, item in unique.values():
        groups.setdefault(structure(item.candidate), []).append(item)
    cards = prefer_entities(list(groups.values()), stats=lambda g: g[0].stats, candidate=lambda g: g[0].candidate)
    kept = _with_coverage(cards, options.top_k)

    edges = {a.edge for a in anchors}
    if while_only:
        words = "while-period starts"
    elif edges == {"start"}:
        words = "marked starts"
    elif edges == {"end"}:
        words = "marked ends"
    else:
        words = "marked starts and ends"
    # Only when the best suggestion still misses (a cheap check, only then): a relation between two sensors the labels follow, which no single-entity trigger expresses.
    relation = None
    if kept and not paired and ((best := kept[0][0].stats).missed or best.no_fire_violations or best.outside_nearby):
        check()
        relation = search.relation(anchors)
    names = helper_names([card[0].candidate for card in kept], {*timeline.entity_ids, *taken})

    def rendered(item: _Scored, ids: Mapping[tuple[Any, ...], str]) -> tuple[dict[str, Any], str, tuple[dict[str, Any], ...]]:
        explanation = explain(item.candidate, item.stats, words, relation, search.calendar_note(item.candidate, item.stats))
        alias = f"Suggested: {describe(item.candidate)[:80]}"
        config = render_automation(item.candidate, alias=alias, description=explanation, helper_ids=ids)
        return config, explanation, search.helpers(item.candidate, ids)

    suggestions = []
    for rank, (item, *others) in enumerate(kept):
        config, explanation, helpers = rendered(item, names)
        variants = tuple(Variant(other.candidate, other.stats, *rendered(other, variant_names(item.candidate, other.candidate, names))) for other in others[:MAX_VARIANTS])
        suggestions.append(Suggestion(rank, item.candidate, item.stats, config, explanation, variants=variants, helpers=helpers))
    return SuggestResult(
        suggestions, _hints(search, anchors, suggestions), search.evaluated, context=SearchContext(search, words, relation)
    )
