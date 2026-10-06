"""A scenario timeline as per-entity step functions.

Input is the timeline replay uses (`history.async_get_timeline`, unpacked): `initial_states` `{entity_id: {state, attributes, last_changed?, last_updated?}}` and `events` `[{at, entity_id, state, attributes, ...}]`. Times are POSIX seconds (floats) throughout the miner; `to_ts` converts.

Each entity becomes a `StepFunction`: the entries it held, sorted by when each began, so "state at t" is one binary search. The seeded state begins at the window start (the sandbox seeds it there) but keeps its recorded `last_changed`. Like the sandbox, events outside `[start, end)` are skipped, and a write identical to the previous state and attributes is dropped, since Home Assistant fires no `state_changed` for it.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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


class Timeline:
    """Step functions for every entity in a scenario window `[start, end)`."""

    def __init__(self, start: float, end: float, entities: dict[str, StepFunction]) -> None:
        if end <= start:
            raise ValueError("The window must end after it starts")
        self.start = start
        self.end = end
        self._entities = entities

    @classmethod
    def from_scenario(
        cls,
        initial_states: Mapping[str, Mapping[str, Any]],
        events: list[Mapping[str, Any]],
        start: str | datetime | float,
        end: str | datetime | float,
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
        return cls(start_ts, end_ts, entities)

    def __getitem__(self, entity_id: str) -> StepFunction:
        return self._entities[entity_id]

    def __contains__(self, entity_id: object) -> bool:
        return entity_id in self._entities

    def get(self, entity_id: str) -> StepFunction | None:
        return self._entities.get(entity_id)

    @property
    def entity_ids(self) -> list[str]:
        return list(self._entities)
