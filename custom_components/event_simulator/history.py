"""Read an automation and the recorded history of what it depends on.

Everything here produces pieces of a scenario (see the design spec): the `core`
block, the automation `config`, and the `initial_states` / `events` timeline, so
the panel can feed results straight into a scenario without reshaping them.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from functools import partial
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from homeassistant.components.automation import DATA_COMPONENT
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import States
from homeassistant.components.recorder.history import (
    get_significant_states_with_session,
)
from homeassistant.core import HomeAssistant, State, callback, valid_entity_id
from homeassistant.helpers.recorder import session_scope
from homeassistant.helpers.template import Template, is_template_string
from homeassistant.util import dt as dt_util

MAX_ENTITIES = 50
MAX_WINDOW = timedelta(days=7)
MAX_EVENTS = 50_000

# A quoted literal shaped like an entity id inside a template. Rendering finds the
# entities a template touches on today's states, but a short-circuited `and`/`if`
# hides the rest, so literals naming an existing entity are added too.
_QUOTED_ENTITY_ID = re.compile(r"""['"]([a-z0-9_]+\.[a-z0-9_]+)['"]""")


class SimulatorError(Exception):
    """A request the simulator refuses, with a code for the websocket error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_window(start: datetime, end: datetime) -> None:
    """Refuse a window that is empty, reversed, or longer than MAX_WINDOW."""
    if end <= start:
        raise SimulatorError("invalid_window", "The window must end after it starts")
    if end - start > MAX_WINDOW:
        raise SimulatorError(
            "window_too_long", f"The window is longer than the limit of {MAX_WINDOW}"
        )


@callback
def async_core_config(hass: HomeAssistant) -> dict[str, Any]:
    """The scenario `core` block: what the sandbox copies from this instance."""
    config = hass.config
    return {
        "time_zone": config.time_zone,
        "latitude": config.latitude,
        "longitude": config.longitude,
        "elevation": config.elevation,
        "unit_system": config.units._name,  # noqa: SLF001 - the name core config stores
    }


@callback
def async_list_automations(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Every automation entity as `{entity_id, alias, id}`, sorted by entity id."""
    if (component := hass.data.get(DATA_COMPONENT)) is None:
        return []
    return sorted(
        (
            {
                "entity_id": entity.entity_id,
                "alias": _alias(entity),
                "id": entity.unique_id,
            }
            for entity in component.entities
        ),
        key=lambda item: item["entity_id"],
    )


@callback
def async_get_automation(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """The automation's config as written, and every entity it references."""
    component = hass.data.get(DATA_COMPONENT)
    if component is None or (entity := component.get_entity(entity_id)) is None:
        raise SimulatorError("not_found", f"No automation {entity_id}")
    config = entity.raw_config or {}
    referenced = set(entity.referenced_entities)
    referenced |= _template_entities(hass, _template_strings(config))
    return {"config": config, "referenced_entities": sorted(referenced)}


async def async_get_timeline(
    hass: HomeAssistant, entity_ids: list[str], start: datetime, end: datetime
) -> dict[str, Any]:
    """The state of each entity at `start`, plus every state write in [start, end).

    Returned as the scenario's `initial_states` and `events`. An initial state is
    `{state, attributes, last_changed, last_updated}`, the timestamps ISO UTC and
    taken from the recorded row itself: the recorder's own start state is stamped
    with the window start, which would make a `for:` early in the window see a
    state that has held for hours as brand new. Entities with nothing recorded
    before `start` are left out of `initial_states`; the recorder is the only
    source, and a made-up start state would present a guess as history.
    """
    start, end = dt_util.as_utc(start), dt_util.as_utc(end)
    if len(entity_ids) > MAX_ENTITIES:
        raise SimulatorError(
            "too_many_entities",
            f"{len(entity_ids)} entities requested; the limit is {MAX_ENTITIES}",
        )
    check_window(start, end)
    if not entity_ids:
        return {"initial_states": {}, "events": []}

    states, stamps = await get_instance(hass).async_add_executor_job(
        partial(_read_recorder, hass, list(entity_ids), start, end)
    )

    initial_states: dict[str, dict[str, Any]] = {}
    changes: list[State] = []
    for entity_id, entity_states in states.items():
        for state in entity_states:
            assert isinstance(state, State)
            if state.last_updated > start:
                changes.append(state)
            elif (stamp := stamps.get(entity_id)) is not None:
                last_changed, last_updated = stamp
                initial_states[entity_id] = {
                    "state": state.state,
                    "attributes": dict(state.attributes),
                    "last_changed": last_changed.isoformat(),
                    "last_updated": last_updated.isoformat(),
                }
    if len(changes) > MAX_EVENTS:
        raise SimulatorError(
            "too_many_events",
            f"{len(changes)} state changes in the window; the limit is {MAX_EVENTS}."
            " Shorten the window or drop the busiest entities.",
        )
    changes.sort(key=lambda state: state.last_updated)
    events = [
        {
            "at": state.last_updated.isoformat(),
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
        }
        for state in changes
    ]
    return {"initial_states": initial_states, "events": events}


def _read_recorder(
    hass: HomeAssistant, entity_ids: list[str], start: datetime, end: datetime
) -> tuple[dict[str, list[Any]], dict[str, tuple[datetime, datetime]]]:
    """Recorder executor: the window's states, and when each start state was set.

    Both reads share one session so the timestamps describe the same rows the
    start states came from.
    """
    with session_scope(hass=hass, read_only=True) as session:
        states = get_significant_states_with_session(
            hass,
            session,
            start,
            end,
            entity_ids,
            include_start_time_state=True,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=False,
        )
        starting = [
            entity_id
            for entity_id, entity_states in states.items()
            if entity_states and entity_states[0].last_updated <= start
        ]
        return states, _start_state_stamps(hass, session, starting, start)


def _start_state_stamps(
    hass: HomeAssistant, session: Session, entity_ids: list[str], start: datetime
) -> dict[str, tuple[datetime, datetime]]:
    """`(last_changed, last_updated)` of each entity's last row before `start`.

    The same row the recorder picks for a start state (latest `last_updated_ts`
    strictly before `start`), read for the timestamps the recorder replaces with
    `start`. One limit-1 reverse scan of the (metadata_id, last_updated_ts) index
    per entity, the lookup the recorder itself uses for a single entity; the
    recorder stores `last_changed_ts` as NULL when it equals `last_updated_ts`.
    """
    metadata_ids = get_instance(hass).states_meta_manager.get_many(
        entity_ids, session, False
    )
    start_ts = start.timestamp()
    stamps: dict[str, tuple[datetime, datetime]] = {}
    for entity_id, metadata_id in metadata_ids.items():
        if metadata_id is None:
            continue
        row = session.execute(
            select(States.last_updated_ts, States.last_changed_ts)
            .where(
                States.metadata_id == metadata_id,
                States.last_updated_ts < start_ts,
            )
            .order_by(States.last_updated_ts.desc())
            .limit(1)
        ).first()
        if row is None:
            continue
        last_updated_ts, last_changed_ts = row
        stamps[entity_id] = (
            dt_util.utc_from_timestamp(last_changed_ts or last_updated_ts),
            dt_util.utc_from_timestamp(last_updated_ts),
        )
    return stamps


def _alias(entity: Any) -> str:
    if alias := (entity.raw_config or {}).get("alias"):
        return str(alias)
    return entity.name if isinstance(entity.name, str) else entity.entity_id


def _template_strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        if is_template_string(node):
            yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _template_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _template_strings(value)


def _template_entities(hass: HomeAssistant, templates: Iterable[str]) -> set[str]:
    """Entities the templates read: HA's render-time tracking plus quoted literals.

    Rendering runs against the live states with no run variables, so templates
    that need `trigger` fail partway; whatever they touched before failing still
    counts, and the failure itself is irrelevant here.
    """
    found: set[str] = set()
    for text in templates:
        info = Template(text, hass).async_render_to_info(log_fn=_ignore_log)
        found |= info.entities
        found |= {
            entity_id
            for entity_id in _QUOTED_ENTITY_ID.findall(text)
            if valid_entity_id(entity_id) and hass.states.get(entity_id) is not None
        }
    return found


def _ignore_log(level: int, message: str) -> None:
    """Swallow undefined-variable warnings from rendering without run variables."""
