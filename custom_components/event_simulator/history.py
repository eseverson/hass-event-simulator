"""Read an automation and the recorded history of what it depends on.

Everything here produces pieces of a scenario (see the design spec): the `core`
block, the automation `config`, and the `initial_states` / `events` timeline, so
the panel can feed results straight into a scenario without reshaping them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from functools import partial
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
import voluptuous as vol

from homeassistant.components.automation import DATA_COMPONENT
from homeassistant.components.labs.const import LABS_DATA
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import EventData, Events, EventTypes, States
from homeassistant.components.recorder.history import (
    get_significant_states_with_session,
)
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State, callback, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.recorder import session_scope
from homeassistant.helpers.template import Template, is_template_string
from homeassistant.util import dt as dt_util
from homeassistant.util.json import json_loads

from .sandbox.calendars import MAX_CALENDAR_EVENTS, MAX_CALENDARS, events_from_states, merge_events, parse_event

MAX_ENTITIES = 50
MAX_WINDOW = timedelta(days=7)
MAX_EVENTS = 50_000
# Suggest mode learns from repetition, so it reads a longer window (the suggestions spec); replays stay capped at MAX_WINDOW.
MAX_SUGGEST_WINDOW = timedelta(days=30)
# State changes the suggest command reads for its window: the miner holds them in memory (about 0.3 s for 60k), and only the entities the candidates reference go on to the sandbox.
MAX_SUGGEST_EVENTS = 200_000
# Recorded bus events (zha_event, tag_scanned, ...) the suggest command reads for its window, for event triggers; past this the busiest types are left out.
MAX_BUS_EVENTS = 20_000
# Recorded event types that are Home Assistant's own bookkeeping rather than something happening in the home: never offered as triggers or replayed (the sandbox fires its own). `call_service` is recorded too, but replaying one would be a service call.
SYSTEM_EVENT_TYPES = frozenset(
    {
        "automation_triggered",
        "call_service",
        "component_loaded",
        "config_entry_discovered",
        "core_config_updated",
        "data_entry_flow_progressed",
        "labs_updated",
        "logbook_entry",
        "logging_changed",
        "lovelace_updated",
        "panels_updated",
        "script_started",
        "service_registered",
        "service_removed",
        "shopping_list_updated",
        "state_changed",
        "state_reported",
        "themes_updated",
        "user_added",
        "user_removed",
        "user_updated",
    }
)
_SYSTEM_EVENT_TYPE = re.compile(r"^(homeassistant_|recorder_)|(_registry_updated|_reloaded)$")
# How far around the window a calendar's events are read: a calendar trigger with an offset fires this far from its events (the miner's `MAX_CALENDAR_OFFSET`).
CALENDAR_MARGIN = timedelta(days=2)
# Seconds `calendar.get_events` may take per calendar (a cloud calendar answers over the network) before its recorded states stand in.
CALENDAR_TIMEOUT = 10.0
# Registry snapshot caps: an area or label on a big install can pull in hundreds of
# entities, and each entry is around a kilobyte of scenario.
MAX_SNAPSHOT_ENTITIES = 500
MAX_SNAPSHOT_DEVICES = 250

# Config keys whose values name registry items (targets, device automations, ...).
_REGISTRY_REF_KEYS = ("area_id", "device_id", "floor_id", "label_id")
# A quoted literal in a template: a candidate area/floor/label id or name, or device id.
_QUOTED_LITERAL = re.compile(r"""['"]([^'"\n]{1,100})['"]""")

# A quoted literal shaped like an entity id inside a template. Rendering finds the
# entities a template touches on today's states, but a short-circuited `and`/`if`
# hides the rest, so literals naming an existing entity are added too.
_QUOTED_ENTITY_ID = re.compile(r"""['"]([a-z0-9_]+\.[a-z0-9_]+)['"]""")

# Options that may name an entity instead of a literal: a time trigger's or time condition's `at` / `after` / `before` (input_datetime, timestamp sensor), a numeric_state threshold, a zone condition's zone. HA's `referenced_entities` reads only `entity_id` (and misses even that for platform conditions such as `zone`, whose validated config nests it under `options`), so a replay would lack these states and, say, a time trigger on an input_datetime would never fire.
_ENTITY_VALUED_KEYS = frozenset({"entity_id", "at", "after", "before", "above", "below", "zone"})
# A state condition's `state` names an entity only for these domains (HA's own `INPUT_ENTITY_ID`); anything else is a literal state.
_INPUT_ENTITY_ID = re.compile(r"^input_(?:select|text|number|boolean|datetime)\.[\da-z_]+$")


class SimulatorError(Exception):
    """A request the simulator refuses, with a code for the websocket error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_window(start: datetime, end: datetime, max_window: timedelta | None = None) -> None:
    """Refuse a window that is empty, reversed, or longer than `max_window` (a replay's MAX_WINDOW unless said otherwise)."""
    max_window = MAX_WINDOW if max_window is None else max_window
    if end <= start:
        raise SimulatorError("invalid_window", "The window must end after it starts")
    if end - start > max_window:
        raise SimulatorError(
            "window_too_long", f"The window is longer than the limit of {max_window}"
        )


@callback
def async_core_config(hass: HomeAssistant) -> dict[str, Any]:
    """The scenario `core` block: what the sandbox copies from this instance.

    `preview_features` lists the enabled Labs preview features as `[domain,
    preview_feature]` pairs; the sandbox turns them on before validating the
    automation, so Labs-gated triggers and conditions behave as they do here.
    """
    config = hass.config
    return {
        "time_zone": config.time_zone,
        "latitude": config.latitude,
        "longitude": config.longitude,
        "elevation": config.elevation,
        "unit_system": config.units._name,  # noqa: SLF001 - the name core config stores
        "preview_features": _enabled_preview_features(hass),
    }


def _enabled_preview_features(hass: HomeAssistant) -> list[list[str]]:
    if (labs_data := hass.data.get(LABS_DATA)) is None:
        return []
    return sorted(
        [domain, feature] for domain, feature in labs_data.data.preview_feature_status
    )


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
    referenced |= _entity_valued_options(config)
    referenced |= _device_automation_entities(config)
    return {"config": config, "referenced_entities": sorted(_entity_ids(hass, referenced))}


def _entity_ids(hass: HomeAssistant, references: Iterable[str]) -> set[str]:
    """Entity ids for `references`: device triggers and conditions name their entity by its registry id, which HA's `referenced_entities` passes on as is. One that resolves to no entity is dropped; the history and simulate commands take entity ids only."""
    ent_reg = er.async_get(hass)
    found = set()
    for reference in references:
        if valid_entity_id(reference):
            found.add(reference)
        elif (entity_id := er.async_resolve_entity_id(ent_reg, reference)) is not None:
            found.add(entity_id)
    return found


async def async_get_timeline(
    hass: HomeAssistant,
    entity_ids: list[str],
    start: datetime,
    end: datetime,
    *,
    max_window: timedelta | None = None,
    max_events: int | None = None,
) -> dict[str, Any]:
    """The state of each entity at `start`, plus every state write in [start, end).

    Returned as the scenario's `initial_states` and `events`. An initial state is
    `{state, attributes, last_changed, last_updated}`, the timestamps ISO UTC and
    taken from the recorded row itself: the recorder's own start state is stamped
    with the window start, which would make a `for:` early in the window see a
    state that has held for hours as brand new. Entities with nothing recorded
    before `start` are left out of `initial_states`; the recorder is the only
    source, and a made-up start state would present a guess as history.

    The window and the number of state changes are capped at `max_window` / `max_events` (a replay's limits unless the caller is Suggest mode).
    """
    start, end = dt_util.as_utc(start), dt_util.as_utc(end)
    max_window = MAX_WINDOW if max_window is None else max_window
    max_events = MAX_EVENTS if max_events is None else max_events
    if len(entity_ids) > MAX_ENTITIES:
        raise SimulatorError(
            "too_many_entities",
            f"{len(entity_ids)} entities requested; the limit is {MAX_ENTITIES}",
        )
    check_window(start, end, max_window)
    if not entity_ids:
        return {"initial_states": {}, "events": []}

    states, stamps = await get_instance(hass).async_add_executor_job(
        partial(_read_recorder, hass, list(entity_ids), start, end)
    )

    unrecorded = {entity_id: _unrecorded_attributes(hass, entity_id) for entity_id in states}
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
                    "attributes": _with_unrecorded(state, unrecorded[entity_id]),
                    "last_changed": last_changed.isoformat(),
                    "last_updated": last_updated.isoformat(),
                }
    if len(changes) > max_events:
        raise SimulatorError(
            "too_many_events",
            f"{len(changes)} state changes in the window; the limit is {max_events}."
            " Shorten the window or drop the busiest entities.",
        )
    changes.sort(key=lambda state: state.last_updated)
    events = [
        {
            "at": state.last_updated.isoformat(),
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": _with_unrecorded(state, unrecorded[state.entity_id]),
            "last_changed": state.last_changed.isoformat(),
            "last_updated": state.last_updated.isoformat(),
        }
        for state in changes
    ]
    return {"initial_states": initial_states, "events": events}


def is_system_event_type(event_type: str) -> bool:
    """Whether a recorded event type is Home Assistant's own bookkeeping (see SYSTEM_EVENT_TYPES)."""
    return event_type in SYSTEM_EVENT_TYPES or bool(_SYSTEM_EVENT_TYPE.search(event_type))


async def async_get_bus_events(
    hass: HomeAssistant,
    start: datetime,
    end: datetime,
    *,
    event_types: Iterable[str] | None = None,
    max_events: int = MAX_BUS_EVENTS,
) -> dict[str, Any]:
    """The recorder's bus events in [start, end) (the scenario's `bus_events`), oldest first: `{bus_events: [{at, event_type, data}], dropped_event_types}`.

    Every recorded type (or only `event_types`) except Home Assistant's own bookkeeping (`is_system_event_type`). When there are more than `max_events`, whole types are left out, the busiest first, and named in `dropped_event_types`.
    """
    start, end = dt_util.as_utc(start), dt_util.as_utc(end)
    check_window(start, end, MAX_SUGGEST_WINDOW)
    wanted = None if event_types is None else set(event_types)
    rows, dropped = await get_instance(hass).async_add_executor_job(
        partial(_read_bus_events, hass, start, end, wanted, max_events)
    )
    return {
        "bus_events": [
            {"at": dt_util.utc_from_timestamp(at).isoformat(), "event_type": event_type, "data": data}
            for at, event_type, data in rows
        ],
        "dropped_event_types": dropped,
    }


async def async_get_calendar_events(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    start: datetime,
    end: datetime,
    *,
    timeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The events of each calendar around [start, end) (`CALENDAR_MARGIN` either side), for calendar triggers: the scenario's `calendars`.

    Returns `{calendars: {entity_id: [{start, end, summary, description?, location?}]}, sources: {entity_id: {calendar, states, error?}}}`. Each calendar is asked with `calendar.get_events` (a read-only service call on this instance), and the events its recorded states name (`timeline`, the window's `initial_states` / `events`, read here when not given) are added where the calendar does not return them: an event deleted since, or a past one it no longer serves. When the calendar cannot answer (its integration is not loaded, it is unavailable, it timed out), the recorded states alone stand in and `error` says why. `sources` counts the events from each. Only `calendar.` entities are read, at most `MAX_CALENDARS`, each capped at `MAX_CALENDAR_EVENTS`.
    """
    start, end = dt_util.as_utc(start), dt_util.as_utc(end)
    check_window(start, end, MAX_SUGGEST_WINDOW)
    ids = [entity_id for entity_id in dict.fromkeys(entity_ids) if entity_id.startswith("calendar.")][:MAX_CALENDARS]
    if not ids:
        return {"calendars": {}, "sources": {}}
    if timeline is None:
        timeline = await async_get_timeline(hass, ids, start, end, max_window=MAX_SUGGEST_WINDOW)
    tz = dt_util.get_default_time_zone()
    lo, hi = start - CALENDAR_MARGIN, end + CALENDAR_MARGIN
    calendars: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, dict[str, Any]] = {}
    for entity_id in ids:
        states = [item.get("attributes") or {} for item in timeline.get("events") or () if item.get("entity_id") == entity_id]
        if (initial := (timeline.get("initial_states") or {}).get(entity_id)) is not None:
            states.append(initial.get("attributes") or {})
        recorded = events_from_states(states, tz)
        live, error = await _async_calendar_get_events(hass, entity_id, lo, hi, tz)
        merged = [event for event in merge_events(live, recorded, tz) if event.start_local(tz) < hi and event.end_local(tz) >= lo]
        from_calendar = sum(any(event is mine for mine in live) for event in merged)
        source: dict[str, Any] = {"calendar": from_calendar, "states": len(merged) - from_calendar}
        if error is not None:
            source["error"] = error
        sources[entity_id] = source
        calendars[entity_id] = [event.as_dict() for event in merged[:MAX_CALENDAR_EVENTS]]
    return {"calendars": calendars, "sources": sources}


async def _async_calendar_get_events(hass: HomeAssistant, entity_id: str, start: datetime, end: datetime, tz: Any) -> tuple[list[Any], str | None]:
    """The calendar's own events in [start, end) through `calendar.get_events`, and why there are none when it could not answer."""
    if not hass.services.has_service("calendar", "get_events"):
        return [], "the calendar integration is not loaded"
    try:
        async with asyncio.timeout(CALENDAR_TIMEOUT):
            response = await hass.services.async_call(
                "calendar",
                "get_events",
                {"entity_id": entity_id, "start_date_time": start, "end_date_time": end},
                blocking=True,
                return_response=True,
            )
    except TimeoutError:
        return [], f"calendar.get_events did not answer within {CALENDAR_TIMEOUT:g} s"
    except (HomeAssistantError, vol.Invalid) as err:
        return [], str(err) or type(err).__name__
    items = ((response or {}).get(entity_id) or {}).get("events")
    if not isinstance(items, list):
        return [], "calendar.get_events returned no events list"
    events = []
    for item in items:
        try:
            events.append(parse_event(item, tz))
        except ValueError:
            continue
    return events, None


def _read_bus_events(
    hass: HomeAssistant, start: datetime, end: datetime, wanted: set[str] | None, max_events: int
) -> tuple[list[tuple[float, str, dict[str, Any]]], list[str]]:
    """Recorder executor: count the window's events by type, keep the quietest types up to `max_events`, and read those."""
    start_ts, end_ts = start.timestamp(), end.timestamp()
    in_window = (Events.time_fired_ts >= start_ts) & (Events.time_fired_ts < end_ts)
    with session_scope(hass=hass, read_only=True) as session:
        counts = session.execute(
            select(EventTypes.event_type, func.count())
            .select_from(Events)
            .join(EventTypes, Events.event_type_id == EventTypes.event_type_id)
            .where(in_window)
            .group_by(EventTypes.event_type)
        ).all()
        eligible = sorted(
            (count, event_type)
            for event_type, count in counts
            if event_type and not is_system_event_type(event_type) and (wanted is None or event_type in wanted)
        )
        kept: list[str] = []
        dropped: list[str] = []
        total = 0
        for count, event_type in eligible:
            if total + count <= max_events:
                kept.append(event_type)
                total += count
            else:
                dropped.append(event_type)
        if not kept:
            return [], sorted(dropped)
        rows = session.execute(
            select(Events.time_fired_ts, EventTypes.event_type, EventData.shared_data, Events.event_data)
            .select_from(Events)
            .join(EventTypes, Events.event_type_id == EventTypes.event_type_id)
            .outerjoin(EventData, Events.data_id == EventData.data_id)
            .where(in_window, EventTypes.event_type.in_(kept))
            .order_by(Events.time_fired_ts, Events.event_id)
        ).all()
    found = []
    for at, event_type, shared, legacy in rows:
        try:
            data = json_loads(shared or legacy or "{}")
        except ValueError:
            continue
        found.append((at, event_type, data if isinstance(data, dict) else {}))
    return found, sorted(dropped)


def _unrecorded_attributes(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """The live state's attributes that its entity keeps out of the recorder (`_unrecorded_attributes`).

    These are static capabilities (an input_datetime's `has_date` / `has_time`, a light's `supported_color_modes`, ...), yet triggers and templates read them: a time trigger on an input_datetime fails to attach without `has_date`. The live values stand in for the recorded rows that lack them.
    """
    if (live := hass.states.get(entity_id)) is None or live.state_info is None:
        return {}
    keys = live.state_info["unrecorded_attributes"]
    return {key: value for key, value in live.attributes.items() if key in keys}


def _with_unrecorded(state: State, unrecorded: dict[str, Any]) -> dict[str, Any]:
    """A recorded state's attributes, with the unrecorded ones put back unless the row is `unavailable` (which carries no capabilities)."""
    if not unrecorded or state.state == STATE_UNAVAILABLE:
        return dict(state.attributes)
    return {**unrecorded, **state.attributes}


@callback
def async_registry_snapshot(
    hass: HomeAssistant, config: dict[str, Any], entity_ids: Iterable[str]
) -> dict[str, Any]:
    """The scenario `registry` block: the registry entries the automation can see.

    Covers `entity_ids` (the timeline, kept first and in order), the entities the
    config names by entity id or entity registry id (also as a quoted literal in a
    template), whatever it names by `area_id` / `device_id` / `floor_id` /
    `label_id` (targets, device triggers and conditions), and areas, floors, labels
    and devices that templates name in a quoted literal (`area_entities('Kitchen')`). Areas expand to their entities (directly
    or through a device, as `area_entities()` does), floors to their areas, devices
    to their entities, labels to what carries them. Then every entity's device and
    area, every area's floor, and every label any of them carries come along.

    Entries are in the registries' own storage format, with `versions` saying which
    one, so the sandbox loads them through the registries' normal load path. Config
    entries travel as `{entry_id, domain, title}` stubs, never their data. Past
    `MAX_SNAPSHOT_ENTITIES` / `MAX_SNAPSHOT_DEVICES` the rest is dropped and counted
    in `truncated`, which the sandbox reports as a warning.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    floor_reg = fr.async_get(hass)
    label_reg = lr.async_get(hass)

    refs: dict[str, set[str]] = {key: set() for key in _REGISTRY_REF_KEYS}
    entity_refs: set[str] = set()
    _collect_registry_refs(config, refs, entity_refs)
    for literal in _template_literals(_template_strings(config)):
        if valid_entity_id(literal):
            entity_refs.add(literal)
            continue
        if dev_reg.async_get(literal) is not None:
            refs["device_id"].add(literal)
        if area := (area_reg.async_get_area(literal) or area_reg.async_get_area_by_name(literal)):
            refs["area_id"].add(area.id)
        if floor := (floor_reg.async_get_floor(literal) or floor_reg.async_get_floor_by_name(literal)):
            refs["floor_id"].add(floor.floor_id)
        if label := (label_reg.async_get_label(literal) or label_reg.async_get_label_by_name(literal)):
            refs["label_id"].add(label.label_id)

    entities: dict[str, er.RegistryEntry] = {}
    devices: dict[str, dr.DeviceEntry] = {}
    area_ids: set[str] = set()
    truncated = {"entities": 0, "devices": 0}

    def add_entity(entry: er.RegistryEntry | None) -> None:
        if entry is None or entry.entity_id in entities:
            return
        if len(entities) >= MAX_SNAPSHOT_ENTITIES:
            truncated["entities"] += 1
            return
        entities[entry.entity_id] = entry

    def add_device(device: dr.DeviceEntry | None) -> None:
        if device is None or device.id in devices:
            return
        # A child device (Home Assistant 2026.9+) is stored apart from devices and cannot load without its parent.
        if (parent_id := getattr(device, "parent_device_id", None)) is not None:
            add_device(dev_reg.async_get(parent_id))
        if len(devices) >= MAX_SNAPSHOT_DEVICES:
            truncated["devices"] += 1
            return
        devices[device.id] = device

    for entity_id in entity_ids:
        add_entity(ent_reg.async_get(entity_id))
    for entity_id_or_uuid in sorted(entity_refs):
        add_entity(ent_reg.async_get(entity_id_or_uuid))
    for device_id in sorted(refs["device_id"]):
        if (device := dev_reg.async_get(device_id)) is not None:
            add_device(device)
            for entry in er.async_entries_for_device(ent_reg, device_id, True):
                add_entity(entry)
    expanded_areas = set(refs["area_id"])
    for floor_id in sorted(refs["floor_id"]):
        expanded_areas |= {area.id for area in ar.async_entries_for_floor(area_reg, floor_id)}
    for area_id in sorted(expanded_areas):
        if area_reg.async_get_area(area_id) is None:
            continue
        area_ids.add(area_id)
        for entry in er.async_entries_for_area(ent_reg, area_id):
            add_entity(entry)
        for device in dr.async_entries_for_area(dev_reg, area_id):
            add_device(device)
            for entry in er.async_entries_for_device(ent_reg, device.id):
                if entry.area_id is None:
                    add_entity(entry)
    for label_id in sorted(refs["label_id"]):
        for entry in er.async_entries_for_label(ent_reg, label_id):
            add_entity(entry)
        for device in dr.async_entries_for_label(dev_reg, label_id):
            add_device(device)
        area_ids |= {area.id for area in ar.async_entries_for_label(area_reg, label_id)}

    # Closure: what the chosen entries point at.
    for entry in list(entities.values()):
        if entry.device_id:
            add_device(dev_reg.async_get(entry.device_id))
        if entry.area_id:
            area_ids.add(entry.area_id)
    for device in devices.values():
        if device.area_id:
            area_ids.add(device.area_id)
    areas = [area for area_id in sorted(area_ids) if (area := area_reg.async_get_area(area_id))]
    floor_ids = set(refs["floor_id"]) | {area.floor_id for area in areas if area.floor_id}
    label_ids = set(refs["label_id"])
    for item in (*entities.values(), *devices.values(), *areas):
        label_ids |= item.labels
    entry_ids = {entry.config_entry_id for entry in entities.values() if entry.config_entry_id}
    for device in devices.values():
        entry_ids |= device.config_entries

    children = [device for device in devices.values() if getattr(device, "parent_device_id", None)]
    snapshot: dict[str, Any] = {
        "versions": {
            "entities": [er.STORAGE_VERSION_MAJOR, er.STORAGE_VERSION_MINOR],
            "devices": [dr.STORAGE_VERSION_MAJOR, dr.STORAGE_VERSION_MINOR],
            "areas": [ar.STORAGE_VERSION_MAJOR, ar.STORAGE_VERSION_MINOR],
            "floors": [fr.STORAGE_VERSION_MAJOR, fr.STORAGE_VERSION_MINOR],
            "labels": [lr.STORAGE_VERSION_MAJOR, lr.STORAGE_VERSION_MINOR],
        },
        "entities": [_fragment(entry.as_storage_fragment) for entry in entities.values()],
        "devices": [
            _fragment(device.as_storage_fragment)
            for device in devices.values()
            if not getattr(device, "parent_device_id", None)
        ],
        # Areas, floors and labels have no per-entry storage form; their registries'
        # save data is small and filtered here.
        "areas": [
            area
            for area in area_reg._data_to_save()["areas"]  # noqa: SLF001
            if area["id"] in area_ids
        ],
        "floors": [
            floor
            for floor in floor_reg._data_to_save()["floors"]  # noqa: SLF001
            if floor["floor_id"] in floor_ids
        ],
        "labels": [
            label
            for label in label_reg._data_to_save()["labels"]  # noqa: SLF001
            if label["label_id"] in label_ids
        ],
        "config_entries": [
            {"entry_id": entry.entry_id, "domain": entry.domain, "title": entry.title}
            for entry_id in sorted(entry_ids)
            if (entry := hass.config_entries.async_get_entry(entry_id)) is not None
        ],
    }
    # The rest of each store's data (the entity registry's `settings`, the device registry's `child_devices`, ...), so the child writes stores in exactly the shape this Home Assistant loads: its registries read these keys unconditionally once the store is at the current version.
    snapshot["store_data"] = {
        "entities": _store_data(ent_reg, "entities", "deleted_entities"),
        "devices": _store_data(dev_reg, "devices", "deleted_devices"),
    }
    if "child_devices" in snapshot["store_data"]["devices"]:
        snapshot["store_data"]["devices"]["child_devices"] = [
            _fragment(device.as_storage_fragment) for device in children
        ]
    if any(truncated.values()):
        snapshot["truncated"] = {kind: count for kind, count in truncated.items() if count}
    return snapshot


def _store_data(registry: Any, *skip: str) -> dict[str, Any]:
    """A registry's save data other than the `skip` keys (its entry lists, which the snapshot filters)."""
    data = registry._data_to_save()  # noqa: SLF001
    return {key: _fragment(value) for key, value in data.items() if key not in skip}


def _fragment(fragment: Any) -> dict[str, Any]:
    """A storage `json_fragment` back as a plain dict."""
    return json_loads(json_bytes(fragment))


def _collect_registry_refs(
    node: Any, refs: dict[str, set[str]], entity_refs: set[str]
) -> None:
    """Registry ids named anywhere in the config, plus entity ids and entity registry ids."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in refs:
                refs[key] |= {
                    item
                    for item in (value if isinstance(value, list) else [value])
                    if isinstance(item, str) and not is_template_string(item)
                }
            elif key == "entity_id":
                entity_refs |= {
                    item
                    for item in (value if isinstance(value, list) else [value])
                    if isinstance(item, str) and not is_template_string(item)
                }
            _collect_registry_refs(value, refs, entity_refs)
    elif isinstance(node, list):
        for value in node:
            _collect_registry_refs(value, refs, entity_refs)


def _template_literals(templates: Iterable[str]) -> set[str]:
    return {
        literal.strip()
        for text in templates
        for literal in _QUOTED_LITERAL.findall(text)
        if literal.strip()
    }


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


def _entity_valued_options(node: Any) -> set[str]:
    """Entity ids given as the value of an option that also takes a literal (see `_ENTITY_VALUED_KEYS`), anywhere in the config."""
    found: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key in _ENTITY_VALUED_KEYS or key == "state":
                values = value if isinstance(value, list) else [value]
                for candidate in values:
                    if isinstance(candidate, dict):  # `at: {entity_id: ..., offset: ...}`
                        candidate = candidate.get("entity_id")
                    if not isinstance(candidate, str) or not valid_entity_id(candidate):
                        continue
                    if key != "state" or _INPUT_ENTITY_ID.match(candidate):
                        found.add(candidate)
            if isinstance(value, (dict, list)):
                stack.append(value)
    return found


def _device_automation_entities(node: Any) -> set[str]:
    """The `entity_id` of every device trigger, condition and action (any mapping with a `device_id`), usually a registry id.

    Home Assistant's `referenced_entities` leaves a device trigger's entity out unless validation turned it into an entity id, which it does not, so the entity that fires the automation was missing.
    """
    found: set[str] = set()
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            if "device_id" in item:
                value = item.get("entity_id")
                found.update(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str))
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return found


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
