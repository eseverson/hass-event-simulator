"""Candidate automations: a small internal representation, and its rendering to Home Assistant config.

Candidates are frozen dataclasses, so the miner can hash them, cache their fire times and compare them. Durations and offsets are seconds; times of day are seconds since local midnight; state values are tuples (one value renders as a scalar). Each trigger and condition watches at most one entity: multi-entity "and" triggers are a non-goal.

`render_automation` writes the modern syntax (`triggers:` / `conditions:` / `actions:`, `trigger: state`, `condition: state`, `action:`), with a placeholder action the user replaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUN_EVENTS = ("sunrise", "sunset")
DAY = 86_400

Values = tuple[Any, ...]


def _check_weekdays(weekdays: Values | None) -> None:
    if weekdays is not None and (not weekdays or any(day not in WEEKDAYS for day in weekdays)):
        raise ValueError(f"weekdays must be a non-empty subset of {WEEKDAYS}")


def _check_time_of_day(value: float | None, name: str) -> None:
    if value is not None and not 0 <= value < DAY:
        raise ValueError(f"{name} must be seconds since midnight, in [0, 86400)")


def _check_duration(value: float, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class StateTrigger:
    """`trigger: state`. `to` / `from_` / `not_to` / `not_from` are None when not given.

    With none of them and `changes_only` False, every write fires (attribute-only ones too, or, with `attribute`, every change of that attribute); `changes_only` renders `to: null`, which fires on state changes only.
    """

    entity_id: str
    to: Values | None = None
    from_: Values | None = None
    not_to: Values | None = None
    not_from: Values | None = None
    attribute: str | None = None
    for_: float = 0
    changes_only: bool = False
    id: str | None = None

    def __post_init__(self) -> None:
        if self.to is not None and self.not_to is not None:
            raise ValueError("to and not_to are exclusive")
        if self.from_ is not None and self.not_from is not None:
            raise ValueError("from and not_from are exclusive")
        if self.changes_only and (self.to is not None or self.not_to is not None):
            raise ValueError("changes_only means `to: null`; it cannot be combined with to / not_to")
        _check_duration(self.for_, "for_")

    @property
    def match_all(self) -> bool:
        """No from/to option at all: Home Assistant fires on every write."""
        return not self.changes_only and self.to is self.from_ is self.not_to is self.not_from is None


@dataclass(frozen=True, slots=True)
class NumericStateTrigger:
    """`trigger: numeric_state`: fires when the value crosses into `above < value < below`, optionally after staying there `for_`."""

    entity_id: str
    above: float | None = None
    below: float | None = None
    attribute: str | None = None
    for_: float = 0
    id: str | None = None

    def __post_init__(self) -> None:
        if self.above is None and self.below is None:
            raise ValueError("numeric_state needs above, below or both")
        _check_duration(self.for_, "for_")


@dataclass(frozen=True, slots=True)
class TimeTrigger:
    """`trigger: time` at a fixed local time of day (`at`, seconds since midnight), optionally on some weekdays."""

    at: int
    weekdays: Values | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        _check_time_of_day(self.at, "at")
        _check_weekdays(self.weekdays)


@dataclass(frozen=True, slots=True)
class SunTrigger:
    """`trigger: sun` at sunrise or sunset plus `offset` seconds (negative: before)."""

    event: str
    offset: float = 0
    id: str | None = None

    def __post_init__(self) -> None:
        if self.event not in SUN_EVENTS:
            raise ValueError(f"event must be one of {SUN_EVENTS}")


@dataclass(frozen=True, slots=True)
class StateCondition:
    """`condition: state`: the state (or attribute) is one of `state`, for longer than `for_` when given."""

    entity_id: str
    state: Values
    attribute: str | None = None
    for_: float = 0

    def __post_init__(self) -> None:
        if not self.state:
            raise ValueError("state needs at least one value")
        _check_duration(self.for_, "for_")


@dataclass(frozen=True, slots=True)
class NumericStateCondition:
    """`condition: numeric_state`: `above < value < below`."""

    entity_id: str
    above: float | None = None
    below: float | None = None
    attribute: str | None = None

    def __post_init__(self) -> None:
        if self.above is None and self.below is None:
            raise ValueError("numeric_state needs above, below or both")


@dataclass(frozen=True, slots=True)
class TimeCondition:
    """`condition: time`: local time of day in `[after, before)` (a window crossing midnight when after >= before), and/or the weekday."""

    after: int | None = None
    before: int | None = None
    weekdays: Values | None = None

    def __post_init__(self) -> None:
        if self.after is None and self.before is None and self.weekdays is None:
            raise ValueError("time needs after, before or weekdays")
        _check_time_of_day(self.after, "after")
        _check_time_of_day(self.before, "before")
        _check_weekdays(self.weekdays)


@dataclass(frozen=True, slots=True)
class SunCondition:
    """`condition: sun`: after and/or before a sun event (plus offset) of the local day."""

    after: str | None = None
    after_offset: float = 0
    before: str | None = None
    before_offset: float = 0

    def __post_init__(self) -> None:
        if self.after is None and self.before is None:
            raise ValueError("sun needs after, before or both")
        for event in (self.after, self.before):
            if event is not None and event not in SUN_EVENTS:
                raise ValueError(f"sun events are {SUN_EVENTS}")


Trigger = StateTrigger | NumericStateTrigger | TimeTrigger | SunTrigger
Condition = StateCondition | NumericStateCondition | TimeCondition | SunCondition


@dataclass(frozen=True, slots=True)
class Candidate:
    """A candidate automation: any trigger fires it, all conditions must hold at the fire time."""

    triggers: tuple[Trigger, ...]
    conditions: tuple[Condition, ...] = field(default=())

    def with_condition(self, condition: Condition) -> Candidate:
        return replace(self, conditions=(*self.conditions, condition))

    @property
    def entity_ids(self) -> set[str]:
        """The entities the candidate reads: what the sandbox needs on its timeline."""
        return {
            part.entity_id
            for part in (*self.triggers, *self.conditions)
            if isinstance(part, (StateTrigger, NumericStateTrigger, StateCondition, NumericStateCondition))
        }


# Rendering


def format_duration(seconds: float) -> str:
    """`[-]HH:MM:SS`, as Home Assistant's time period strings accept (offsets may be negative)."""
    sign = "-" if seconds < 0 else ""
    total = round(abs(seconds))
    return f"{sign}{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def format_time(seconds: float) -> str:
    """`HH:MM:SS` for seconds since midnight."""
    return format_duration(seconds)


def _period(seconds: float) -> dict[str, int]:
    """A `for:` as the dict the automation editor writes."""
    whole = int(seconds)
    period = {"hours": whole // 3600, "minutes": whole // 60 % 60, "seconds": whole % 60}
    if millis := round((seconds - whole) * 1000):
        period["milliseconds"] = millis
    return period


def _values(values: Values) -> Any:
    return values[0] if len(values) == 1 else list(values)


def render_trigger(trigger: Trigger) -> dict[str, Any]:
    """One trigger as automation config."""
    match trigger:
        case StateTrigger():
            out: dict[str, Any] = {"trigger": "state"}
            if trigger.id is not None:
                out["id"] = trigger.id
            out["entity_id"] = trigger.entity_id
            if trigger.attribute is not None:
                out["attribute"] = trigger.attribute
            for key, values in (("from", trigger.from_), ("not_from", trigger.not_from), ("to", trigger.to), ("not_to", trigger.not_to)):
                if values is not None:
                    out[key] = _values(values)
            if trigger.changes_only:
                out["to"] = None
            if trigger.for_:
                out["for"] = _period(trigger.for_)
            return out
        case NumericStateTrigger():
            out = {"trigger": "numeric_state"}
            if trigger.id is not None:
                out["id"] = trigger.id
            out["entity_id"] = trigger.entity_id
            if trigger.attribute is not None:
                out["attribute"] = trigger.attribute
            if trigger.above is not None:
                out["above"] = trigger.above
            if trigger.below is not None:
                out["below"] = trigger.below
            if trigger.for_:
                out["for"] = _period(trigger.for_)
            return out
        case TimeTrigger():
            out = {"trigger": "time"}
            if trigger.id is not None:
                out["id"] = trigger.id
            out["at"] = format_time(trigger.at)
            if trigger.weekdays is not None:
                out["weekday"] = list(trigger.weekdays)
            return out
        case SunTrigger():
            out = {"trigger": "sun"}
            if trigger.id is not None:
                out["id"] = trigger.id
            out["event"] = trigger.event
            if trigger.offset:
                out["offset"] = format_duration(trigger.offset)
            return out
    raise TypeError(f"not a trigger: {trigger!r}")


def render_condition(condition: Condition) -> dict[str, Any]:
    """One condition as automation config."""
    match condition:
        case StateCondition():
            out: dict[str, Any] = {"condition": "state", "entity_id": condition.entity_id}
            if condition.attribute is not None:
                out["attribute"] = condition.attribute
            out["state"] = _values(condition.state)
            if condition.for_:
                out["for"] = _period(condition.for_)
            return out
        case NumericStateCondition():
            out = {"condition": "numeric_state", "entity_id": condition.entity_id}
            if condition.attribute is not None:
                out["attribute"] = condition.attribute
            if condition.above is not None:
                out["above"] = condition.above
            if condition.below is not None:
                out["below"] = condition.below
            return out
        case TimeCondition():
            out = {"condition": "time"}
            if condition.after is not None:
                out["after"] = format_time(condition.after)
            if condition.before is not None:
                out["before"] = format_time(condition.before)
            if condition.weekdays is not None:
                out["weekday"] = list(condition.weekdays)
            return out
        case SunCondition():
            out = {"condition": "sun"}
            if condition.after is not None:
                out["after"] = condition.after
                if condition.after_offset:
                    out["after_offset"] = format_duration(condition.after_offset)
            if condition.before is not None:
                out["before"] = condition.before
                if condition.before_offset:
                    out["before_offset"] = format_duration(condition.before_offset)
            return out
    raise TypeError(f"not a condition: {condition!r}")


def placeholder_actions(alias: str) -> list[dict[str, Any]]:
    """A no-op the user replaces: a logbook entry saying the suggestion fired."""
    return [
        {
            "action": "logbook.log",
            "data": {"name": alias, "message": "fired (suggested automation: replace this action)"},
        }
    ]


def render_automation(
    candidate: Candidate,
    *,
    alias: str,
    description: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The candidate as a full automation config (mode `single`)."""
    config: dict[str, Any] = {"alias": alias}
    if description:
        config["description"] = description
    config["triggers"] = [render_trigger(trigger) for trigger in candidate.triggers]
    config["conditions"] = [render_condition(condition) for condition in candidate.conditions]
    config["actions"] = actions if actions is not None else placeholder_actions(alias)
    config["mode"] = "single"
    return config
