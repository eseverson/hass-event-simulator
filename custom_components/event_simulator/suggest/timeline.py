"""A scenario timeline as per-entity step functions.

Input is the timeline replay uses (`history.async_get_timeline`, unpacked): `initial_states` `{entity_id: {state, attributes, last_changed?, last_updated?}}` and `events` `[{at, entity_id, state, attributes, ...}]`. Times are POSIX seconds (floats) throughout the miner; `to_ts` converts.

Bus events (`bus_events` `[{at, event_type, data}]`, the scenario's optional list of recorded events such as `zha_event` or `tag_scanned`) are kept per event type as `BusEvents`, time-ordered, inside the window.

Calendars (`calendars` `{entity_id: [{start, end, summary, ...}]}`, the scenario's optional block, see `sandbox/calendars.py`) are kept per calendar as `CalendarEvents`; a calendar entity on the timeline without an entry gets the events its own recorded states name. Both need the time zone (`time_zone`): an all-day event begins at local midnight, and the states name local wall times.

Each entity becomes a `StepFunction`: the entries it held, sorted by when each began, so "state at t" is one binary search. The seeded state begins at the window start (the sandbox seeds it there) but keeps its recorded `last_changed`. Like the sandbox, events outside `[start, end)` are skipped, and a write identical to the previous state and attributes is dropped, since Home Assistant fires no `state_changed` for it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from ..sandbox.calendars import Event, events_from_states, parse_calendars

UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})


class _NonNumeric:
    """A value that is there but is not a number: Home Assistant's numeric_state raises on it."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "NON_NUMERIC"


NON_NUMERIC: Any = _NonNumeric()


def to_ts(value: str | datetime | float) -> float:
    """POSIX seconds for an ISO string, a datetime (naive means UTC) or a number."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.timestamp()


def to_iso(ts: float) -> str:
    """ISO UTC string for POSIX seconds."""
    return datetime.fromtimestamp(ts, UTC).isoformat()


def numeric(value: Any) -> float | None | Any:
    """The numeric view of a state or attribute value, as Home Assistant's numeric_state reads it.

    A number; `None` for a missing, `unavailable` or `unknown` value (never in range, no error); `NON_NUMERIC` for anything else (an error: the condition fails, a trigger neither arms nor disarms).
    """
    if value is None or value in UNAVAILABLE_STATES:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return NON_NUMERIC


@dataclass(frozen=True, slots=True)
class Transition:
    """One state write: entry `index` of its step function replacing entry `index - 1`."""

    at: float
    index: int
    old_state: str | None
    state: str
    old_attributes: Mapping[str, Any] | None
    attributes: Mapping[str, Any]

    def value(self, attribute: str | None = None) -> Any:
        return self.state if attribute is None else self.attributes.get(attribute)

    def old_value(self, attribute: str | None = None) -> Any:
        if attribute is None:
            return self.old_state
        return None if self.old_attributes is None else self.old_attributes.get(attribute)


class StepFunction:
    """One entity's history: entry i holds from `times[i]` until `times[i + 1]`."""

    __slots__ = ("_numeric", "attributes", "changed", "entity_id", "seeded", "states", "times")

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.times: list[float] = []
        self.states: list[str] = []
        self.attributes: list[Mapping[str, Any]] = []
        self.changed: list[float] = []  # last_changed of each entry
        self.seeded = False  # entry 0 is the initial state, not a write
        self._numeric: dict[str | None, list[Any]] = {}

    def __len__(self) -> int:
        return len(self.times)

    def _append(self, at: float, state: str, attributes: Mapping[str, Any], changed: float | None) -> None:
        if self.states:
            if state == self.states[-1] and attributes == self.attributes[-1]:
                return
            if changed is None:
                changed = self.changed[-1] if state == self.states[-1] else at
        self.times.append(at)
        self.states.append(state)
        self.attributes.append(attributes)
        self.changed.append(at if changed is None else changed)

    def index_at(self, t: float) -> int:
        """Index of the entry holding at `t` (a write at exactly `t` included), or -1."""
        return bisect_right(self.times, t) - 1

    def state_at(self, t: float) -> str | None:
        i = self.index_at(t)
        return None if i < 0 else self.states[i]

    def attributes_at(self, t: float) -> Mapping[str, Any] | None:
        i = self.index_at(t)
        return None if i < 0 else self.attributes[i]

    def value_at(self, t: float, attribute: str | None = None) -> Any:
        """The state, or the attribute's value (None when absent), at `t`."""
        i = self.index_at(t)
        if i < 0:
            return None
        return self.states[i] if attribute is None else self.attributes[i].get(attribute)

    def last_changed_at(self, t: float) -> float | None:
        i = self.index_at(t)
        return None if i < 0 else self.changed[i]

    def numeric_values(self, attribute: str | None = None) -> list[Any]:
        """The numeric view (`numeric`) of every entry, computed once per attribute."""
        if (values := self._numeric.get(attribute)) is None:
            source = self.states if attribute is None else [a.get(attribute) for a in self.attributes]
            values = self._numeric[attribute] = [numeric(v) for v in source]
        return values

    def numeric_at(self, t: float, attribute: str | None = None) -> Any:
        i = self.index_at(t)
        return None if i < 0 else self.numeric_values(attribute)[i]

    def write_range(self, start: float | None = None, end: float | None = None) -> range:
        """Indices of the writes (entries that replaced another, or created the entity) in `[start, end)`."""
        lo = 1 if self.seeded else 0
        if start is not None:
            lo = max(lo, bisect_left(self.times, start))
        hi = len(self.times) if end is None else bisect_left(self.times, end)
        return range(lo, max(lo, hi))

    def transitions(self, start: float | None = None, end: float | None = None) -> Iterator[Transition]:
        """The writes in `[start, end)`, oldest first."""
        for i in self.write_range(start, end):
            yield Transition(
                at=self.times[i],
                index=i,
                old_state=self.states[i - 1] if i else None,
                state=self.states[i],
                old_attributes=self.attributes[i - 1] if i else None,
                attributes=self.attributes[i],
            )


@dataclass(frozen=True, slots=True)
class BusEvents:
    """The recorded events of one type: `times[i]` and the event's `data[i]`, oldest first."""

    event_type: str
    times: list[float]
    data: list[Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class CalendarEvents:
    """One calendar's events (`events`, sorted by start) and when each starts and ends, as POSIX seconds (`starts[i]` / `ends[i]` are `events[i]`'s; `end_order` lists the indexes by end, `ends_sorted` their ends) and as aware local datetimes (`local_starts` / `local_ends`), which Home Assistant adds a trigger's offset to."""

    entity_id: str
    events: list[Event]
    starts: list[float]
    ends: list[float]
    end_order: list[int]
    ends_sorted: list[float]
    local_starts: list[datetime]
    local_ends: list[datetime]

    @classmethod
    def build(cls, entity_id: str, events: list[Event], tz: tzinfo) -> CalendarEvents:
        events = sorted(events, key=lambda event: (event.start_local(tz), event.end_local(tz), event.summary))
        local_starts = [event.start_local(tz) for event in events]
        local_ends = [event.end_local(tz) for event in events]
        starts = [at.timestamp() for at in local_starts]
        ends = [at.timestamp() for at in local_ends]
        order = sorted(range(len(events)), key=lambda n: (ends[n], n))
        return cls(entity_id, events, starts, ends, order, [ends[n] for n in order], local_starts, local_ends)

    def moments(self, event: str) -> list[tuple[datetime, int]]:
        """`(aware local datetime, event index)` of every start (`event` "start") or end, oldest first."""
        if event == "start":
            return list(zip(self.local_starts, range(len(self.events)), strict=True))
        return [(self.local_ends[n], n) for n in self.end_order]


class Timeline:
    """Step functions for every entity in a scenario window `[start, end)`, the window's bus events by type, and the events of its calendars."""

    def __init__(
        self,
        start: float,
        end: float,
        entities: dict[str, StepFunction],
        bus_events: dict[str, BusEvents] | None = None,
        calendars: Mapping[str, list[Event]] | None = None,
        time_zone: str | tzinfo | None = None,
    ) -> None:
        if end <= start:
            raise ValueError("The window must end after it starts")
        self.start = start
        self.end = end
        self._entities = entities
        self._bus = bus_events or {}
        self.tz = ZoneInfo(time_zone) if isinstance(time_zone, str) else time_zone or UTC
        self._calendars: dict[str, CalendarEvents | None] = {
            entity_id: CalendarEvents.build(entity_id, events, self.tz) for entity_id, events in (calendars or {}).items()
        }

    @classmethod
    def from_scenario(
        cls,
        initial_states: Mapping[str, Mapping[str, Any]],
        events: list[Mapping[str, Any]],
        start: str | datetime | float,
        end: str | datetime | float,
        bus_events: list[Mapping[str, Any]] | None = None,
        calendars: Mapping[str, Any] | None = None,
        time_zone: str | tzinfo | None = None,
    ) -> Timeline:
        start_ts, end_ts = to_ts(start), to_ts(end)
        if end_ts <= start_ts:
            raise ValueError("The window must end after it starts")
        entities: dict[str, StepFunction] = {}
        for entity_id, initial in initial_states.items():
            step = entities[entity_id] = StepFunction(entity_id)
            changed = initial.get("last_changed")
            step._append(  # noqa: SLF001
                start_ts,
                str(initial["state"]),
                initial.get("attributes") or {},
                min(to_ts(changed), start_ts) if changed else start_ts,
            )
            step.seeded = True
        timed = sorted(
            ((to_ts(event["at"]), n, event) for n, event in enumerate(events)),
            key=lambda item: (item[0], item[1]),
        )
        for at, _, event in timed:
            if not start_ts <= at < end_ts:
                continue
            entity_id = event["entity_id"]
            if (step := entities.get(entity_id)) is None:
                step = entities[entity_id] = StepFunction(entity_id)
            step._append(at, str(event["state"]), event.get("attributes") or {}, None)  # noqa: SLF001
        bus: dict[str, BusEvents] = {}
        for at, _, event in sorted(
            ((to_ts(event["at"]), n, event) for n, event in enumerate(bus_events or ())),
            key=lambda item: (item[0], item[1]),
        ):
            if not start_ts <= at < end_ts:
                continue
            event_type = str(event["event_type"])
            if (found := bus.get(event_type)) is None:
                found = bus[event_type] = BusEvents(event_type, [], [])
            found.times.append(at)
            found.data.append(event.get("data") or {})
        tz = ZoneInfo(time_zone) if isinstance(time_zone, str) else time_zone or UTC
        return cls(start_ts, end_ts, entities, bus, parse_calendars(calendars, tz), tz)

    def __getitem__(self, entity_id: str) -> StepFunction:
        return self._entities[entity_id]

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._entities

    def get(self, entity_id: str) -> StepFunction | None:
        return self._entities.get(entity_id)

    @property
    def entity_ids(self) -> list[str]:
        return list(self._entities)

    @property
    def event_types(self) -> list[str]:
        """The types of the bus events in the window, sorted."""
        return sorted(self._bus)

    def bus_events(self, event_type: str) -> BusEvents:
        """The window's events of one type (none: empty lists)."""
        return self._bus.get(event_type) or BusEvents(event_type, [], [])

    @property
    def calendar_ids(self) -> list[str]:
        """Every calendar with events to read: those given, and the calendar entities on the timeline, sorted."""
        return sorted(set(self._calendars) | {entity_id for entity_id in self._entities if entity_id.startswith("calendar.")})

    def calendar(self, entity_id: str) -> CalendarEvents | None:
        """A calendar's events: the given ones, else those its recorded states name (computed once); None for a calendar with neither."""
        if entity_id not in self._calendars:
            step = self._entities.get(entity_id)
            found = None
            if step is not None and entity_id.startswith("calendar."):
                events = events_from_states(step.attributes, self.tz)
                found = CalendarEvents.build(entity_id, events, self.tz) if events else None
            self._calendars[entity_id] = found
        return self._calendars[entity_id]
