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
from homeassistant.components.labs.const import LABS_DATA
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import States
from homeassistant.components.recorder.history import (
    get_significant_states_with_session,
)
from homeassistant.core import HomeAssistant, State, callback, valid_entity_id
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

MAX_ENTITIES = 50
MAX_WINDOW = timedelta(days=7)
MAX_EVENTS = 50_000
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


@callback
def async_registry_snapshot(
    hass: HomeAssistant, config: dict[str, Any], entity_ids: Iterable[str]
) -> dict[str, Any]:
    """The scenario `registry` block: the registry entries the automation can see.

    Covers `entity_ids` (the timeline and referenced entities, kept first and in
    order), whatever the config names by `area_id` / `device_id` / `floor_id` /
    `label_id` (targets, device triggers and conditions) or by an entity registry
    id, and areas, floors, labels and devices that templates name in a quoted
    literal (`area_entities('Kitchen')`). Areas expand to their entities (directly
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
    uuid_refs: set[str] = set()
    _collect_registry_refs(config, refs, uuid_refs)
    for literal in _template_literals(_template_strings(config)):
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
        if len(devices) >= MAX_SNAPSHOT_DEVICES:
            truncated["devices"] += 1
            return
        devices[device.id] = device

    for entity_id in entity_ids:
        add_entity(ent_reg.async_get(entity_id))
    for uuid in sorted(uuid_refs):
        add_entity(ent_reg.async_get(uuid))
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

    snapshot: dict[str, Any] = {
        "versions": {
            "entities": [er.STORAGE_VERSION_MAJOR, er.STORAGE_VERSION_MINOR],
            "devices": [dr.STORAGE_VERSION_MAJOR, dr.STORAGE_VERSION_MINOR],
            "areas": [ar.STORAGE_VERSION_MAJOR, ar.STORAGE_VERSION_MINOR],
            "floors": [fr.STORAGE_VERSION_MAJOR, fr.STORAGE_VERSION_MINOR],
            "labels": [lr.STORAGE_VERSION_MAJOR, lr.STORAGE_VERSION_MINOR],
        },
        "entities": [_fragment(entry.as_storage_fragment) for entry in entities.values()],
        "devices": [_fragment(device.as_storage_fragment) for device in devices.values()],
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
    if any(truncated.values()):
        snapshot["truncated"] = {kind: count for kind, count in truncated.items() if count}
    return snapshot


def _fragment(fragment: Any) -> dict[str, Any]:
    """A storage `json_fragment` back as a plain dict."""
    return json_loads(json_bytes(fragment))


def _collect_registry_refs(
    node: Any, refs: dict[str, set[str]], uuid_refs: set[str]
) -> None:
    """Registry ids named anywhere in the config, plus entity registry ids (UUIDs)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in refs:
                refs[key] |= {
                    item
                    for item in (value if isinstance(value, list) else [value])
                    if isinstance(item, str) and not is_template_string(item)
                }
            elif key == "entity_id":
                uuid_refs |= {
                    item
                    for item in (value if isinstance(value, list) else [value])
                    if isinstance(item, str) and not valid_entity_id(item)
                }
            _collect_registry_refs(value, refs, uuid_refs)
    elif isinstance(node, list):
        for value in node:
            _collect_registry_refs(value, refs, uuid_refs)


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
