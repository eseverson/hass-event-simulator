"""What kind of suggestion a candidate is (the panel's filter chips), and its structure (what near-duplicates share).

Kinds (the contract shared with the panel, #41/#44): `state`, `attribute`, `numeric`, `any_change`, `trend`, `time`, `weekday`, `sun`, `calendar`, `event`, `entity_time`, and `or` when two or more triggers share one id. A state trigger with an attribute is `attribute`; one with no from/to (or `to: null`) is `any_change` (both, on an attribute); any other is `state`. A numeric_state trigger or condition is `numeric`. A clock time is `time`, plus `weekday` when it names weekdays; a time condition with only weekdays is `weekday`. A time at an entity is `entity_time`. Sun, calendar and event triggers are what they say. `trend` is a trigger on a Trend or Derivative helper the suggestion needs (#42: `model.TrendTrigger`, `model.DerivativeTrigger`), or a state or numeric trigger on the entity of such a helper.

Structure: the parts of a candidate with their thresholds, durations, offsets, clock times, weekdays and helper windows left out (whether a `for:` is there stays, as does a threshold's direction and a state's values). Candidates of one structure are the same suggestion with different numbers: the miner folds them into one card (`search.suggest`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .model import (
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


def part_kinds(part: Trigger | Condition, helper_entities: Iterable[str] = ()) -> set[str]:
    """The kinds of one trigger or condition."""
    match part:
        case StateTrigger():
            if part.entity_id in helper_entities:
                return {"trend"}
            found = set()
            if part.attribute is not None:
                found.add("attribute")
            if part.to is part.from_ is part.not_to is part.not_from is None:  # every write, or `to: null`
                found.add("any_change")
            return found or {"state"}
        case NumericStateTrigger():
            return {"trend"} if part.entity_id in helper_entities else {"numeric"}
        case TimeTrigger():
            return {"time", "weekday"} if part.weekdays is not None else {"time"}
        case EntityTimeTrigger():
            return {"entity_time"}
        case SunTrigger() | SunCondition():
            return {"sun"}
        case CalendarTrigger():
            return {"calendar"}
        case EventTrigger():
            return {"event"}
        case TrendTrigger() | DerivativeTrigger():
            return {"trend"}
        case StateCondition():
            return {"attribute"} if part.attribute is not None else {"state"}
        case NumericStateCondition():
            return {"numeric"}
        case TimeCondition():
            found = set()
            if part.after is not None or part.before is not None:
                found.add("time")
            if part.weekdays is not None:
                found.add("weekday")
            return found
    raise TypeError(f"not a trigger or condition: {part!r}")


def candidate_kinds(candidate: Candidate, helper_entities: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """`(kinds, trigger_kinds)`, each sorted: the kinds of all its parts, and of its triggers only."""
    helpers = set(helper_entities)
    triggers: set[str] = set()
    for trigger in candidate.triggers:
        triggers |= part_kinds(trigger, helpers)
    ids = [trigger.id for trigger in candidate.triggers]
    if len(ids) > len(set(ids)):
        triggers.add("or")
    kinds = set(triggers)
    for condition in candidate.conditions:
        kinds |= part_kinds(condition, helpers)
    return sorted(kinds), sorted(triggers)


def _part_structure(part: Trigger | Condition) -> tuple[Any, ...]:
    match part:
        case StateTrigger():
            return ("state", part.id, part.entity_id, part.attribute, part.to, part.from_, part.not_to, part.not_from, part.changes_only, bool(part.for_))
        case NumericStateTrigger():
            return ("numeric", part.id, part.entity_id, part.attribute, part.above is not None, part.below is not None, bool(part.for_))
        case TimeTrigger():
            return ("time", part.id, part.weekdays is not None)
        case EntityTimeTrigger():
            return ("entity_time", part.id, part.entity_id)
        case SunTrigger():
            return ("sun", part.id, part.event)
        case CalendarTrigger():
            return ("calendar", part.id, part.entity_id, part.event)
        case EventTrigger():
            return ("event", part.id, part.event_type, part.event_data)
        case TrendTrigger():
            return ("trend", part.id, part.entity_id, part.delta > 0)
        case DerivativeTrigger():
            return ("derivative", part.id, part.entity_id, part.above is not None)
        case StateCondition():
            return ("state?", part.entity_id, part.attribute, part.state, bool(part.for_))
        case NumericStateCondition():
            return ("numeric?", part.entity_id, part.attribute, part.above is not None, part.below is not None)
        case TimeCondition():
            return ("time?", part.after is not None, part.before is not None, part.weekdays is not None)
        case SunCondition():
            return ("sun?", part.after, part.before)
    raise TypeError(f"not a trigger or condition: {part!r}")


def structure(candidate: Candidate) -> tuple[Any, ...]:
    """The candidate without its numbers (see the module docstring), order of parts ignored: equal for near-duplicates."""
    return (
        tuple(sorted((_part_structure(t) for t in candidate.triggers), key=repr)),
        tuple(sorted((_part_structure(c) for c in candidate.conditions), key=repr)),
    )
