"""A fast approximate evaluator: when would a candidate automation fire over a timeline?

The miner scores thousands of candidates, so this re-implements the trigger and condition semantics on step functions instead of running Home Assistant; the sandbox validates the top few exactly afterwards. It follows Home Assistant's own code (`homeassistant.components.homeassistant.triggers.state` / `numeric_state` / `time`, `sun` trigger via `get_astral_event_next`, `helpers.condition.state` / `numeric_state` / `time`, `components.sun.condition.sun`) as of 2026.2-2026.9:

- state: a write fires when the from/to options match the old/new value; with no from/to option every write fires (attribute-only ones too); with `attribute`, only writes that change that attribute count. `for:` starts a timer that a later write of the entity cancels unless the value still equals the new value (or, from-only, still differs from the old one).
- numeric_state: fires on crossing into the range, not while staying in it. The entity is armed while out of range; `unavailable` / `unknown` / a missing attribute count as out of range, while any other non-number is an error that neither arms nor disarms (and cancels a pending `for:`). An entity in range when the automation attaches is not armed.
- time: fires at the local wall time each day, DST-correct: on a fall-back day an ambiguous time fires twice, on a spring-forward day a skipped time does not fire.
- time at an entity (`at: input_datetime.x`, a timestamp sensor, optionally `offset`): every write of the entity re-arms it at the time the new state holds plus the offset. An input_datetime with a date (local midnight without a time) adds the offset to the local wall time and arms when that is now or later; one with only a time fires every day at that time plus the offset, wrapped around midnight; a `device_class: timestamp` sensor arms when its time plus the offset is strictly later than now. A state without a time (unknown, unavailable, a sensor without the device class, a timestamp without a zone) arms nothing.
- sun: fires at every `sun_events(date)[event] + offset`.
- calendar: fires at every start (or end) of the calendar's events plus the offset (`Timeline.calendar`: the events given, else those its recorded states name), once per event, so two events starting together fire twice; an event starting after the window still fires inside it with a negative offset. An all-day event starts at local midnight, and the offset adds local wall time (as for a time at an entity).
- event: fires at every bus event of its type whose data holds each of its `(key, value)` pairs, compared as they are (the event trigger's items comparison).
- trend / derivative (#42): the helper's states are computed from its source as the helper does (`rates.py`, which lists where it differs), and the trigger on them is a state trigger to `on` (a trend: its gradient crossing `min_gradient`) or a numeric_state trigger (a derivative), as above.
- conditions read the state at the fire time; errors (unknown entity, non-numeric value) fail the condition, as a condition error stops the run in Home Assistant.

Where this knowingly differs from Home Assistant (the sandbox is the exact answer):

- Ordering at one instant: writes at the same time apply in event order, conditions see the state after all of them, and a `for:` timer due at the instant of a write fires before that write.
- Time-change triggers fire exactly on the second; Home Assistant adds a 50-500 ms random offset.
- Every fire counts as a run. The automation's `mode` only matters while an action runs, and the placeholder action finishes at once; two triggers firing at the same instant count twice.
- The automation attaches at the window start, after the initial states are seeded (as the sandbox does); fires and `for:` timers due at or after the window end do not count.
- Calendar: Home Assistant fetches the events of each 15-minute span ahead (the span shifted by the offset in local wall time), so an event whose offset time falls in the repeated or skipped hour of a DST change may fire once more or not at all there; this fires it once.
- An entity's time due at the instant of its next write does not fire (that write re-arms it); Home Assistant compares an input_datetime's time with now on the local wall clock, which differs in the repeated hour of a fall-back day.
- Not represented: templates, `value_template`, other entity-valued options (`above: sensor.y`, a time condition's `after: input_datetime.x`, a state condition's `state: input_select.x`), several entities in one trigger.
- Sun: `sun_events(date)` must return what `homeassistant.helpers.sun.get_astral_event_date(hass, event, date)` does (aware datetimes, None on a polar day); where Home Assistant's sun condition errors on a missing event, this returns False.
- `holds_throughout` samples conditions at their breakpoints; a time boundary inside a skipped DST hour lands within that hour, not exactly at the gap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, tzinfo
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

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
)
from .rates import derivative_step, trend_step
from .timeline import NON_NUMERIC, UNAVAILABLE_STATES, StepFunction, Timeline

SunEvents = Callable[[date], Mapping[str, datetime | None]]

# Breakpoints are sampled again just after, for boundaries that are strict on the left (`for:`, sun offsets).
_EPSILON = 0.001
_LAST_INSTANT_OF_DAY = DAY - 1e-6  # 23:59:59.999999, the time condition's default `before`


def entity_time(entity_id: str, state: str, attributes: Mapping[str, Any]) -> tuple[str, Any] | None:
    """The time a state holds for a time trigger at its entity, as Home Assistant's time trigger reads it: `("wall", naive local datetime)` (an input_datetime with a date), `("daily", seconds since midnight)` (one with only a time), `("instant", POSIX seconds)` (a timestamp sensor), or None."""
    domain = entity_id.split(".", 1)[0]
    if domain == "input_datetime":
        try:
            has_date, has_time = attributes["has_date"], attributes["has_time"]
            hour, minute, second = (attributes["hour"], attributes["minute"], attributes["second"]) if has_time else (0, 0, 0)
            if has_date:
                return "wall", datetime(attributes["year"], attributes["month"], attributes["day"], hour, minute, second)
        except (KeyError, TypeError, ValueError):
            return None  # Home Assistant's trigger raises on the missing attribute and arms nothing
        return ("daily", hour * 3600 + minute * 60 + second) if has_time else None
    if domain != "sensor" or attributes.get("device_class") != "timestamp" or state in UNAVAILABLE_STATES:
        return None
    try:
        parsed = datetime.fromisoformat(state)
    except ValueError:
        return None
    # A naive timestamp cannot be compared with now: Home Assistant's trigger raises and arms nothing.
    return None if parsed.tzinfo is None else ("instant", parsed.timestamp())


class Fire(NamedTuple):
    """A candidate firing: when, and which of its triggers (index into `Candidate.triggers`)."""

    at: float
    trigger: int


def _matcher(values: tuple[Any, ...] | None, excluded: tuple[Any, ...] | None) -> Callable[[Any], bool]:
    if values is not None:
        return lambda value: value in values
    if excluded is not None:
        return lambda value: value not in excluded
    return lambda value: True


class Evaluator:
    """Fire times of candidates over one timeline. Trigger fire lists are cached, since the search reuses each trigger with many condition sets."""

    def __init__(self, timeline: Timeline, *, time_zone: str | tzinfo, sun_events: SunEvents | None = None) -> None:
        self.timeline = timeline
        self.start = timeline.start
        self.end = timeline.end
        # Home Assistant passes its loaded tzinfo (`dt_util.get_default_time_zone()`); a name is for tests and offline use. All of this runs in an executor thread.
        self.tz = ZoneInfo(time_zone) if isinstance(time_zone, str) else time_zone
        self._sun_events = sun_events
        self._sun_cache: dict[date, tuple[float | None, float | None]] = {}
        self._trigger_cache: dict[Trigger, list[float]] = {}
        self._helper_cache: dict[tuple[Any, ...], StepFunction | None] = {}

    # Time helpers

    def local(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def _local_dates(self, lo: float, hi: float, margin_days: int = 1) -> list[date]:
        first = self.local(lo).date() - timedelta(days=margin_days)
        last = self.local(hi).date() + timedelta(days=margin_days)
        return [first + timedelta(days=n) for n in range((last - first).days + 1)]

    def local_instants(self, seconds: float, lo: float, hi: float, *, skip_nonexistent: bool = True) -> list[float]:
        """Every instant in `[lo, hi)` whose local wall time is `seconds` after midnight: twice for an ambiguous time, never for a skipped one (unless `skip_nonexistent` is False)."""
        whole = int(seconds)
        h, m, s = whole // 3600, whole // 60 % 60, whole % 60
        micro = round((seconds - whole) * 1_000_000)
        found = set()
        for day in self._local_dates(lo, hi):
            for fold in (0, 1):
                ts = datetime(day.year, day.month, day.day, h, m, s, micro, tzinfo=self.tz, fold=fold).timestamp()
                if skip_nonexistent:
                    back = self.local(ts)
                    if back.date() != day or (back.hour, back.minute, back.second) != (h, m, s):
                        continue
                if lo <= ts < hi:
                    found.add(ts)
        return sorted(found)

    def sun(self, day: date) -> tuple[float | None, float | None]:
        """`(sunrise, sunset)` of a local date as POSIX seconds (None on a polar day)."""
        if (cached := self._sun_cache.get(day)) is not None:
            return cached
        if self._sun_events is None:
            raise ValueError("Sun triggers and conditions need a sun_events function")
        events = self._sun_events(day)
        sunrise, sunset = events.get("sunrise"), events.get("sunset")
        cached = self._sun_cache[day] = (
            None if sunrise is None else sunrise.timestamp(),
            None if sunset is None else sunset.timestamp(),
        )
        return cached

    # Triggers

    def trigger_fires(self, trigger: Trigger) -> list[float]:
        """Sorted fire times of one trigger in the window (cached; do not mutate)."""
        if (cached := self._trigger_cache.get(trigger)) is not None:
            return cached
        match trigger:
            case StateTrigger():
                fires = self._state_fires(trigger)
            case NumericStateTrigger():
                fires = self._numeric_fires(trigger)
            case TimeTrigger():
                fires = self._time_fires(trigger)
            case EntityTimeTrigger():
                fires = self._entity_time_fires(trigger)
            case SunTrigger():
                fires = self._sun_fires(trigger)
            case CalendarTrigger():
                fires = [at for at, _ in self.calendar_fires(trigger)]
            case EventTrigger():
                fires = self._event_fires(trigger)
            case TrendTrigger():
                step = self.helper_step(trigger)
                bound = {"above": trigger.delta} if trigger.delta > 0 else {"below": trigger.delta}
                fires = [] if step is None else self._numeric_fires(NumericStateTrigger(step.entity_id, **bound), step)
            case DerivativeTrigger():
                step = self.helper_step(trigger)
                fires = [] if step is None else self._numeric_fires(NumericStateTrigger(step.entity_id, above=trigger.above, below=trigger.below), step)
            case _:
                raise TypeError(f"not a trigger: {trigger!r}")
        self._trigger_cache[trigger] = fires
        return fires

    def _state_fires(self, trigger: StateTrigger) -> list[float]:
        step = self.timeline.get(trigger.entity_id)
        if step is None:
            return []
        attribute = trigger.attribute
        values = step.states if attribute is None else [attrs.get(attribute) for attrs in step.attributes]
        match_from = _matcher(trigger.from_, trigger.not_from)
        match_to = _matcher(trigger.to, trigger.not_to)
        match_all = trigger.match_all
        # Home Assistant's same-state check: from-only waits for the value to stay away from the old one.
        from_only = trigger.from_ is not None and trigger.to is None and not trigger.changes_only
        delay = trigger.for_
        fires: list[float] = []
        pending: list[tuple[float, Any, Any]] = []  # (due, old value, new value)
        times = step.times
        for i in step.write_range():
            t = times[i]
            new = values[i]
            if pending:
                fires.extend(due for due, _, _ in pending if due <= t)
                pending = [
                    item
                    for item in pending
                    if item[0] > t and ((new != item[1]) if from_only else (new == item[2]))
                ]
            old = values[i - 1] if i else None
            if attribute is not None and old == new:
                continue
            if not match_from(old) or not match_to(new) or (not match_all and old == new):
                continue
            if delay:
                pending.append((t + delay, old, new))
            else:
                fires.append(t)
        fires.extend(due for due, _, _ in pending if due < self.end)
        fires.sort()
        return fires

    def helper_step(self, trigger: TrendTrigger | DerivativeTrigger) -> StepFunction | None:
        """The numeric view of the helper a trigger reads (`rates.py`; a trend's in its change over the window), computed once per helper; None when its source is not on the timeline."""
        # A trend's gradient does not depend on its threshold: one per window and sample count.
        key = ("trend", trigger.entity_id, trigger.window, trigger.max_samples, trigger.min_samples) if isinstance(trigger, TrendTrigger) else trigger.helper_key
        if key not in self._helper_cache:
            source = self.timeline.get(trigger.entity_id)
            if source is None:
                found = None
            elif isinstance(trigger, TrendTrigger):
                found = trend_step(source, self.start, sample_duration=trigger.window, max_samples=trigger.max_samples, min_samples=trigger.min_samples, scale=trigger.window)
            else:
                found = derivative_step(source, self.start, time_window=trigger.window, unit_time=trigger.unit_time, round_digits=trigger.round_digits)
            self._helper_cache[key] = found
        return self._helper_cache[key]

    def _numeric_fires(self, trigger: NumericStateTrigger, step: StepFunction | None = None) -> list[float]:
        if step is None:
            step = self.timeline.get(trigger.entity_id)
        if step is None:
            return []
        above = float("-inf") if trigger.above is None else trigger.above
        below = float("inf") if trigger.below is None else trigger.below
        values = step.numeric_values(trigger.attribute)
        times = step.times
        delay = trigger.for_
        fires: list[float] = []
        first = values[0] if step.seeded else NON_NUMERIC
        armed = first is None or (first is not NON_NUMERIC and not above < first < below)
        pending: float | None = None
        for i in step.write_range():
            t = times[i]
            if pending is not None and pending <= t:
                fires.append(pending)
                pending = None
            value = values[i]
            if value is NON_NUMERIC:
                pending = None
                continue
            if value is None or not above < value < below:
                pending = None
                armed = True
            elif armed:
                armed = False
                if delay:
                    pending = t + delay
                else:
                    fires.append(t)
        if pending is not None and pending < self.end:
            fires.append(pending)
        return fires

    def _time_fires(self, trigger: TimeTrigger) -> list[float]:
        fires = self.local_instants(trigger.at, self.start, self.end)
        if trigger.weekdays is not None:
            fires = [t for t in fires if WEEKDAYS[self.local(t).weekday()] in trigger.weekdays]
        return fires

    def entity_moment(self, held: tuple[str, Any], offset: float) -> float | None:
        """When a held `("wall", ...)` or `("instant", ...)` time plus `offset` is, as POSIX seconds (None for a daily time)."""
        kind, value = held
        if kind == "instant":
            return value + offset
        if kind == "wall":
            # Home Assistant adds the offset to the aware local datetime: wall-clock arithmetic, so 24 h after noon is noon the next day across a DST change.
            return (value + timedelta(seconds=offset)).replace(tzinfo=self.tz).timestamp()
        return None

    def _entity_time_fires(self, trigger: EntityTimeTrigger) -> list[float]:
        step = self.timeline.get(trigger.entity_id)
        if step is None:
            return []
        found: set[float] = set()
        times = step.times
        for i in range(len(step)):
            lo = max(times[i], self.start)
            hi = min(times[i + 1] if i + 1 < len(times) else self.end, self.end)
            if hi <= lo or (held := entity_time(trigger.entity_id, step.states[i], step.attributes[i])) is None:
                continue
            if held[0] == "daily":
                found.update(self.local_instants((held[1] + trigger.offset) % DAY, lo, hi))
                continue
            at = self.entity_moment(held, trigger.offset)
            # An input_datetime arms a time that is now or later, a sensor only a later one.
            if at is not None and (at >= lo if held[0] == "wall" else at > lo) and at < hi:
                found.add(at)
        return sorted(found)

    def _sun_fires(self, trigger: SunTrigger) -> list[float]:
        index = 0 if trigger.event == "sunrise" else 1
        margin = int(abs(trigger.offset) // DAY) + 2
        found = set()
        for day in self._local_dates(self.start, self.end, margin):
            if (at := self.sun(day)[index]) is not None and self.start <= at + trigger.offset < self.end:
                found.add(at + trigger.offset)
        return sorted(found)

    def calendar_fires(self, trigger: CalendarTrigger) -> list[tuple[float, int]]:
        """`(fire time, event index into the calendar's events)` of a calendar trigger in the window, oldest first."""
        calendar = self.timeline.calendar(trigger.entity_id)
        if calendar is None:
            return []
        # Home Assistant adds the offset to the aware local datetime: wall-clock arithmetic, so 48 h before midnight across a DST change is 49 or 47 real hours.
        offset = timedelta(seconds=trigger.offset)
        found = [((at + offset).timestamp(), n) for at, n in calendar.moments(trigger.event)]
        return sorted(item for item in found if self.start <= item[0] < self.end)

    def _event_fires(self, trigger: EventTrigger) -> list[float]:
        found = self.timeline.bus_events(trigger.event_type)
        wanted = trigger.event_data
        if not wanted:
            return list(found.times)
        return [
            t
            for t, data in zip(found.times, found.data, strict=True)
            if all(key in data and data[key] == value for key, value in wanted)
        ]

    # Conditions

    def condition_holds(self, condition: Condition, t: float) -> bool:
        match condition:
            case StateCondition():
                return self._state_holds(condition, t)
            case NumericStateCondition():
                return self._numeric_holds(condition, t)
            case TimeCondition():
                return self._time_holds(condition, t)
            case SunCondition():
                return self._sun_holds(condition, t)
        raise TypeError(f"not a condition: {condition!r}")

    def _state_holds(self, condition: StateCondition, t: float) -> bool:
        step = self.timeline.get(condition.entity_id)
        if step is None or (i := step.index_at(t)) < 0:
            return False
        if condition.attribute is None:
            value = step.states[i]
        elif condition.attribute not in step.attributes[i]:
            return False
        else:
            value = step.attributes[i][condition.attribute]
        if value not in condition.state:
            return False
        # Home Assistant measures `for:` from the state's last_changed (an attribute condition has no `for:`).
        return not condition.for_ or t - condition.for_ > step.changed[i]

    def _numeric_holds(self, condition: NumericStateCondition, t: float) -> bool:
        step = self.timeline.get(condition.entity_id)
        if step is None:
            return False
        value = step.numeric_at(t, condition.attribute)
        if value is None or value is NON_NUMERIC:
            return False
        if condition.below is not None and value >= condition.below:
            return False
        return condition.above is None or value > condition.above

    def _time_holds(self, condition: TimeCondition, t: float) -> bool:
        local = self.local(t)
        now = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1_000_000
        after = 0 if condition.after is None else condition.after
        before = _LAST_INSTANT_OF_DAY if condition.before is None else condition.before
        if after < before:
            if not after <= now < before:
                return False
        elif before <= now < after:
            return False
        return condition.weekdays is None or WEEKDAYS[local.weekday()] in condition.weekdays

    def _sun_holds(self, condition: SunCondition, t: float) -> bool:
        today = self.local(t).date()
        sunrise, sunset = self.sun(today)
        events = (condition.before, condition.after)
        if "sunrise" in events and sunrise is not None and today > self.local(sunrise).date():
            sunrise = self.sun(today + timedelta(days=1))[0]
        if "sunset" in events and sunset is not None and today > self.local(sunset).date():
            sunset = self.sun(today + timedelta(days=1))[1]
        if condition.before == "sunrise" and condition.after == "sunset":
            if sunrise is None or sunset is None:
                return False
            return t < sunrise + condition.before_offset or t > sunset + condition.after_offset
        if ("sunrise" in events and sunrise is None) or ("sunset" in events and sunset is None):
            return False
        if condition.before is not None:
            wanted = (sunrise if condition.before == "sunrise" else sunset) + condition.before_offset
            if t > wanted:
                return False
        if condition.after is not None:
            wanted = (sunrise if condition.after == "sunrise" else sunset) + condition.after_offset
            if t < wanted:
                return False
        return True

    def conditions_hold(self, conditions: Iterable[Condition], t: float) -> bool:
        return all(self.condition_holds(condition, t) for condition in conditions)

    # Candidates

    def fires(self, candidate: Candidate) -> list[Fire]:
        """Every run the candidate would start: its triggers' fires, time-ordered, where all conditions hold."""
        merged = [Fire(t, n) for n, trigger in enumerate(candidate.triggers) for t in self.trigger_fires(trigger)]
        merged.sort()
        if candidate.conditions:
            merged = [fire for fire in merged if self.conditions_hold(candidate.conditions, fire.at)]
        return merged

    def fire_times(self, candidate: Candidate) -> list[float]:
        return [fire.at for fire in self.fires(candidate)]

    def holds_throughout(self, conditions: Iterable[Condition], start: float, end: float) -> bool:
        """All conditions hold over all of `[start, end)` (what a `while` label asks for)."""
        conditions = tuple(conditions)
        points = {start}
        for condition in conditions:
            for point in self.breakpoints(condition, start, end):
                points.add(point)
                points.add(point + _EPSILON)
        return all(self.conditions_hold(conditions, p) for p in sorted(points) if start <= p < end)

    def breakpoints(self, condition: Condition, start: float, end: float) -> list[float]:
        """Instants in `(start, end)` where the condition's value can change."""
        found: list[float] = []
        match condition:
            case StateCondition() | NumericStateCondition():
                step = self.timeline.get(condition.entity_id)
                if step is not None:
                    found = self._step_breakpoints(step, condition, start, end)
            case TimeCondition():
                for seconds in (condition.after, condition.before, 0 if condition.weekdays else None):
                    if seconds is not None:
                        found += self.local_instants(seconds, start, end, skip_nonexistent=False)
            case SunCondition():
                found += self.local_instants(0, start, end, skip_nonexistent=False)  # "today" changes
                for day in self._local_dates(start, end, 2):
                    for at in self.sun(day):
                        if at is not None:
                            found += [at + condition.after_offset, at + condition.before_offset]
        return [p for p in found if start < p < end]

    @staticmethod
    def _step_breakpoints(step: StepFunction, condition: Condition, start: float, end: float) -> list[float]:
        found = [step.times[i] for i in step.write_range(start, end)]
        if isinstance(condition, StateCondition) and condition.for_:
            first = max(step.index_at(start), 0)
            last = step.index_at(end)
            found += [step.changed[i] + condition.for_ for i in range(first, last + 1)]
        return found
