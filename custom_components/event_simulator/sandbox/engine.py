"""Replay engine: runs one scenario against one automation (or several independent ones) in a throwaway Home Assistant.

Runs in the child process (see `__main__.py`). `run_scenario(scenario)` is the whole contract: a scenario dict in, a JSON-safe result dict out.

The scenario carries either `automation` (one config) or `automations` (a list of up to `MAX_AUTOMATIONS` configs, for validating many candidates against one timeline at the cost of one boot). Every service call is only recorded, so automations replayed together cannot affect each other: each runs as it would alone, with its own mode, its own entity (the own-entity handling below applies to each) and its own id (one without an id, or repeating an earlier one, gets `DEFAULT_AUTOMATION_ID` plus its index).

Timeline of a replay (all virtual time, see `vclock.py`)
--------------------------------------------------------
1. `start - BOOT_LEAD`: a `HomeAssistant` boots on the virtual loop with a temporary config dir (deleted afterwards). The scenario's `registry` snapshot is written into that dir's `.storage` first, so the entity/device/area/floor/label registries load it through their own load path (config entries as disabled stubs, never set up); registered entities with no initial state get HA's own `unavailable` placeholder at start, as live HA gives entities whose integration did not load. It loads the base registries, sets up only the `homeassistant` and `labs` integrations (turning on the scenario's `core.preview_features`, so Labs-gated triggers and conditions validate as they do live), then runs `async_start`; the automation integration and whatever it pulls in (`trace`, `blueprint`, trigger/condition platforms) come later. No recorder, http, frontend or network-facing integrations, and `skip_pip` is on, so nothing is installed or fetched. `BOOT_LEAD` absorbs any timers boot waits on, so the window starts with everything settled.
2. `start`: initial states are seeded with their recorded `last_changed` / `last_updated` (`StateMachine.async_set(..., timestamp=...)`; an attribute-only update is reproduced by a second write at `last_updated`); states without them are stamped `start`, the same clamp the recorder applies. The automation's own entity is the exception: its recorded state is not seeded but stored as its last state before boot, so it loads enabled or disabled (and with its `last_triggered`) as live HA restores it. Then the automation is checked for platforms the sandbox cannot run (custom integrations' triggers/conditions, integration-specific device automations -> `unsupported_automation`, naming each by config path), validated with HA's own automation schema (invalid -> structured `invalid_automation` error) and set up. It attaches after `async_start`, so a `homeassistant: start` trigger does not fire, and at `start` exactly, so a time trigger at the window start does.
3. `[start, end)`: timeline events are written with `async_set` at their `at`; events at the same instant are written in list order. A recorded on/off of a replayed automation's own entity turns that automation on/off instead (`automation.turn_on` / `turn_off` semantics), so the entity's state, and every template reading it, is the sandbox's own. Every service call is intercepted at the `ServiceRegistry` (see `RecordingServiceRegistry`): recorded, answered with success (an empty response when one is asked for), and never executed, so recorded calls change no state.
4. `end`: the automation's triggers are detached (`async_turn_off(stop_actions=False)`), so nothing new starts; the window is half-open and a run triggered at `end` itself is discarded. Runs already in flight (a `delay`, a `wait_for_trigger`, a queue) keep going for up to `overrun_seconds` of virtual time (default `DEFAULT_OVERRUN`, at most `MAX_OVERRUN`); a run still going at that bound is reported with outcome `unfinished` and a warning. Then HA is stopped.

Runs are read from HA's own automation traces (captured as `trace_automation` stores them, so the stored-traces limit never drops one) and serialized with `as_extended_dict()`.

Result
------
    {
      "version": 1, "start": iso, "end": iso, "error": null | {"code", "message"},
      "runs": [{"id", "started", "finished", "trigger": {...}, "outcome", "stopped_by", "script_execution", "error", "trace"}],
      "service_calls": [{"at", "domain", "service", "data", "target", "run", "context": {"id", "parent_id", "user_id"}}],
      "warnings": [{"code", "message", "at"}]
    }

`traces` sets how much each run carries: `full` (the default) is the shape above. `summary` drops `trace` and adds `conditions`, the result of each top-level condition by trace path (`{"condition/0": false}`; empty when none was evaluated). `none` keeps only `id`, `started`, `finished`, `outcome` and `trigger` as `{"idx", "id"}`, and records no service calls (`service_calls` is empty). Trimmed runs are reduced as each one finishes, so the child never holds more than Home Assistant's own few stored traces per automation; that keeps a month of many automations well under the runner's output cap.

Under `automations`, each run and service call also carries `"automation": index`, per-automation warnings carry it too, and the result adds `"automations": [{"index", "entity_id", "loaded", "error": null | {"code", "message"}, "run_count"}]`. An automation the sandbox refuses (`invalid_automation`, `unsupported_automation`) is reported there with `loaded: false` and the others still replay; the top-level `error` is only for a problem with the scenario itself.

`outcome`: `fired` (the actions ran to the end or a `stop`), `condition_failed` (`stopped_by` = the failing condition's trace path, e.g. `condition/0`), `dropped` (the script mode refused the run; `stopped_by` = `mode_single` / `max_runs` / `recursion`), `aborted` (a condition or `stop: error` inside the actions; `stopped_by` = last action path), `cancelled` (a `restart` re-trigger or shutdown), `error` (an exception; `stopped_by` = last action path, `error` = message), `unfinished` (still running at the overrun bound).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# Home Assistant before voluptuous: from 2026.9 importing `homeassistant` aliases `voluptuous` to probatio, which then raises the validation errors. Imported the other way round, `vol` here is the real voluptuous, `vol.Invalid` misses them, and an invalid automation crashes the replay.
from homeassistant import bootstrap, config_entries, loader
from homeassistant.components import labs
from homeassistant.components.automation import DATA_COMPONENT as AUTOMATION_COMPONENT
from homeassistant.components.automation import trace as automation_trace
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    MAX_LENGTH_STATE_STATE,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceRegistry,
    SupportsResponse,
    valid_entity_id,
)
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry,
    condition as condition_helper,
    device_registry,
    entity_registry,
    floor_registry,
    label_registry,
    restore_state,
)
from homeassistant.helpers import trigger as trigger_helper
from homeassistant.helpers.json import ExtendedJSONEncoder
from homeassistant.helpers.trace import trace_id_get
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
import voluptuous as vol  # noqa: I001 - after homeassistant, see above
from voluptuous.humanize import humanize_error

from .vclock import VirtualClock, patch_homeassistant_clock

_LOGGER = logging.getLogger(__name__)

SCENARIO_VERSION = 1
RESULT_VERSION = 1

# Virtual time HA boots in before the window opens.
BOOT_LEAD = timedelta(seconds=60)
# How long runs in flight at `end` may keep going (virtual time), unless the scenario sets `overrun_seconds`.
DEFAULT_OVERRUN = timedelta(hours=1)
MAX_OVERRUN = timedelta(hours=24)
# Longest window the engine accepts.
MAX_WINDOW = timedelta(days=31)
# Poll interval (virtual) while waiting for in-flight runs after `end`.
_DRAIN_STEP = 1.0
# The id given to an automation that has none: HA only finishes traces of automations with an id. Under `automations`, the index is appended.
DEFAULT_AUTOMATION_ID = "event_simulator_replay"
# Most automations one scenario may carry under `automations`.
MAX_AUTOMATIONS = 25
# Core config keys copied from the scenario into the sandbox's `homeassistant:` section.
_CORE_KEYS = ("time_zone", "latitude", "longitude", "elevation", "unit_system", "country", "language", "currency", "radius")
# Attribute that makes the first of two seeding writes differ from the second, so the second counts as an attribute update.
_SEED_MARKER = "event_simulator_seed"
# Seed for Python's `random` in the child unless the scenario sets `seed`. HA draws on it for the 50-500 ms offset of time-change listeners (time / time_pattern triggers) and for trace run ids; a fixed seed makes a replay repeatable.
DEFAULT_SEED = 0
# Cap on HA log records surfaced as warnings.
_MAX_LOG_WARNINGS = 50
# The registry snapshot (scenario `registry`): kind -> (storage key, list key in the stored data, the registry's own deleted-items key, current (major, minor) version).
_REGISTRY_STORES: dict[str, tuple[str, str, str | None, tuple[int, int]]] = {
    "entities": (entity_registry.STORAGE_KEY, "entities", "deleted_entities", (entity_registry.STORAGE_VERSION_MAJOR, entity_registry.STORAGE_VERSION_MINOR)),
    "devices": (device_registry.STORAGE_KEY, "devices", "deleted_devices", (device_registry.STORAGE_VERSION_MAJOR, device_registry.STORAGE_VERSION_MINOR)),
    "areas": (area_registry.STORAGE_KEY, "areas", None, (area_registry.STORAGE_VERSION_MAJOR, area_registry.STORAGE_VERSION_MINOR)),
    "floors": (floor_registry.STORAGE_KEY, "floors", None, (floor_registry.STORAGE_VERSION_MAJOR, floor_registry.STORAGE_VERSION_MINOR)),
    "labels": (label_registry.STORAGE_KEY, "labels", None, (label_registry.STORAGE_VERSION_MAJOR, label_registry.STORAGE_VERSION_MINOR)),
}
# What each run carries (scenario `traces`), most first; see the module docstring.
TRACE_MODES = ("full", "summary", "none")
DEFAULT_TRACES = "full"
# Upper bound on registry entries a scenario may carry (the live side caps far lower).
MAX_REGISTRY_ITEMS = 10_000
# Device automation platforms the sandbox can run: those of entity-domain integrations (binary_sensor, light, sensor, ...), which work from the entity and device registries alone. Integration-specific ones (zha, mqtt, hue, ...) need that integration running.
_GENERIC_DEVICE_AUTOMATION_TYPE = "entity"
# Services that act on an automation entity; an automation calling one on itself gets a warning, since the call is only recorded.
_SELF_SERVICES = frozenset({"turn_on", "turn_off", "toggle", "trigger"})
# Config keys never searched for triggers/conditions: service-call payloads and variables.
_PAYLOAD_KEYS = frozenset({"data", "data_template", "target", "variables", "event_data", "event_data_template", "response_variable", "metadata", "for_each"})


class ScenarioError(Exception):
    """A scenario or automation the engine refuses, with a result error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _InitialState:
    state: str
    attributes: dict[str, Any]
    last_changed: datetime | None
    last_updated: datetime | None


@dataclass
class _Event:
    at: datetime
    entity_id: str
    state: str
    attributes: dict[str, Any]


@dataclass
class _Automation:
    """One replayed automation: its config as given, the id and entity id it gets in the sandbox, and what became of it."""

    index: int
    # The scenario's item; anything but an object is that automation's `invalid_automation` error.
    config: Any
    id: str
    # Predicted before boot (registry entry, else derived from the name as HA does); the loaded entity's own once it is set up.
    entity_id: str
    # Its own entity's recorded state at the window start (taken out of `initial_states`).
    own_initial: _InitialState | None = None
    entity: Any = None
    error: dict[str, str] | None = None
    run_count: int = 0


@dataclass
class _Scenario:
    core: dict[str, Any]
    start: datetime
    end: datetime
    automations: list[_Automation]
    initial_states: dict[str, _InitialState]
    events: list[_Event]
    services: dict[str, set[str]] | None
    overrun: timedelta
    seed: int
    preview_features: list[tuple[str, str]] = field(default_factory=list)
    registry: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # `automations` rather than `automation`: runs, calls and per-automation warnings carry the index, and the result lists each automation.
    multi: bool = False
    traces: str = DEFAULT_TRACES


def error_result(code: str, message: str, warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """A result carrying only a structured error."""
    return {
        "version": RESULT_VERSION,
        "error": {"code": code, "message": message},
        "runs": [],
        "service_calls": [],
        "warnings": warnings or [],
    }


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Replay `scenario` in a fresh virtual-time Home Assistant and return the result dict.

    Synchronous: builds its own virtual-time loop and runs everything inside `patch_homeassistant_clock`. Meant to be called once per process.
    """
    try:
        parsed = _parse_scenario(scenario)
    except ScenarioError as err:
        return error_result(err.code, str(err))

    random.seed(parsed.seed)
    clock = VirtualClock(parsed.start - BOOT_LEAD)
    config_dir = tempfile.mkdtemp(prefix="event_simulator_")
    try:
        _write_registry_storage(config_dir, parsed.registry, parsed.start - BOOT_LEAD)
        _write_own_last_state(config_dir, parsed)
        with patch_homeassistant_clock(clock):
            replay = _Replay(parsed, clock, config_dir)
            return clock.loop.run_until_complete(replay.run())
    finally:
        try:
            clock.loop.run_until_complete(clock.loop.shutdown_default_executor())
        finally:
            clock.loop.close()
            shutil.rmtree(config_dir, ignore_errors=True)


# --- scenario parsing -------------------------------------------------------------------------


def _parse_time(value: Any, what: str) -> datetime:
    if not isinstance(value, str):
        raise ScenarioError("invalid_scenario", f"{what} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise ScenarioError("invalid_scenario", f"{what} is not ISO 8601: {value!r}") from err
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ScenarioError("invalid_scenario", f"{what} must carry a UTC offset: {value!r}")
    return parsed.astimezone(UTC)


def _parse_attributes(value: Any, what: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioError("invalid_scenario", f"{what} attributes must be an object")
    return value


def _parse_entity_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not valid_entity_id(value):
        raise ScenarioError("invalid_scenario", f"{what} has an invalid entity_id: {value!r}")
    return value


def _parse_state(value: Any, what: str) -> str:
    if value is None or isinstance(value, (dict, list)):
        raise ScenarioError("invalid_scenario", f"{what} needs a state string")
    state = value if isinstance(value, str) else json.dumps(value)
    if len(state) > MAX_LENGTH_STATE_STATE:
        raise ScenarioError("invalid_scenario", f"{what} state is longer than {MAX_LENGTH_STATE_STATE} characters")
    return state


def _parse_scenario(raw: dict[str, Any]) -> _Scenario:
    if raw.get("version", SCENARIO_VERSION) != SCENARIO_VERSION:
        raise ScenarioError("invalid_scenario", f"Unsupported scenario version {raw.get('version')!r}")
    start = _parse_time(raw.get("start"), "start")
    end = _parse_time(raw.get("end"), "end")
    if end <= start:
        raise ScenarioError("invalid_scenario", "The window must end after it starts")
    if end - start > MAX_WINDOW:
        raise ScenarioError("invalid_scenario", f"The window is longer than the limit of {MAX_WINDOW}")

    core_raw = raw.get("core") or {}
    if not isinstance(core_raw, dict):
        raise ScenarioError("invalid_scenario", "core must be an object")
    core = {key: core_raw[key] for key in _CORE_KEYS if core_raw.get(key) is not None}
    preview_features = _parse_preview_features(core_raw.get("preview_features"))

    multi = "automations" in raw
    if multi:
        if "automation" in raw:
            raise ScenarioError("invalid_scenario", "Give either automation (one config) or automations (a list of configs), not both")
        items = raw["automations"]
        if not isinstance(items, list) or not items:
            raise ScenarioError("invalid_scenario", "automations must be a non-empty list of automation configs")
        if len(items) > MAX_AUTOMATIONS:
            raise ScenarioError("invalid_scenario", f"automations carries {len(items)} configs; the limit is {MAX_AUTOMATIONS}")
    else:
        if not isinstance(raw.get("automation"), dict):
            raise ScenarioError("invalid_scenario", "automation must be an object (the automation config)")
        items = [raw["automation"]]

    warnings: list[dict[str, Any]] = []

    initial_raw = raw.get("initial_states") or {}
    if not isinstance(initial_raw, dict):
        raise ScenarioError("invalid_scenario", "initial_states must be an object keyed by entity_id")
    initial_states: dict[str, _InitialState] = {}
    for entity_id, item in initial_raw.items():
        what = f"initial_states[{entity_id!r}]"
        _parse_entity_id(entity_id, what)
        if not isinstance(item, dict):
            raise ScenarioError("invalid_scenario", f"{what} must be an object")
        last_changed = _parse_time(item["last_changed"], f"{what}.last_changed") if item.get("last_changed") else None
        last_updated = _parse_time(item["last_updated"], f"{what}.last_updated") if item.get("last_updated") else None
        if last_changed and last_changed > start:
            warnings.append(_warning("initial_state_after_start", f"{entity_id}: last_changed is after the window start; using the start"))
            last_changed = start
        if last_updated and last_updated > start:
            last_updated = start
        initial_states[entity_id] = _InitialState(
            _parse_state(item.get("state"), what), _parse_attributes(item.get("attributes"), what), last_changed, last_updated
        )

    events_raw = raw.get("events") or []
    if not isinstance(events_raw, list):
        raise ScenarioError("invalid_scenario", "events must be a list")
    events: list[_Event] = []
    for index, item in enumerate(events_raw):
        what = f"events[{index}]"
        if not isinstance(item, dict):
            raise ScenarioError("invalid_scenario", f"{what} must be an object")
        event = _Event(
            _parse_time(item.get("at"), f"{what}.at"),
            _parse_entity_id(item.get("entity_id"), what),
            _parse_state(item.get("state"), what),
            _parse_attributes(item.get("attributes"), what),
        )
        if not start <= event.at < end:
            warnings.append(
                _warning("event_outside_window", f"{what} ({event.entity_id} at {event.at.isoformat()}) is outside the window and was not played", event.at)
            )
            continue
        events.append(event)
    # Stable: same-instant events keep their list order.
    events.sort(key=lambda event: event.at)

    services_raw = raw.get("services")
    services: dict[str, set[str]] | None = None
    if services_raw is not None:
        if not isinstance(services_raw, dict) or not all(isinstance(names, list) for names in services_raw.values()):
            raise ScenarioError("invalid_scenario", "services must map each domain to a list of service names")
        services = {str(domain).lower(): {str(name).lower() for name in names} for domain, names in services_raw.items()}

    overrun = DEFAULT_OVERRUN
    if (overrun_raw := raw.get("overrun_seconds")) is not None:
        if isinstance(overrun_raw, bool) or not isinstance(overrun_raw, (int, float)) or overrun_raw < 0:
            raise ScenarioError("invalid_scenario", "overrun_seconds must be a non-negative number")
        overrun = min(timedelta(seconds=overrun_raw), MAX_OVERRUN)

    seed = raw.get("seed", DEFAULT_SEED)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ScenarioError("invalid_scenario", "seed must be an integer")

    traces = raw.get("traces")
    if traces is None:
        traces = DEFAULT_TRACES
    elif not isinstance(traces, str) or traces not in TRACE_MODES:
        raise ScenarioError("invalid_scenario", f"traces must be one of {', '.join(TRACE_MODES)}, not {traces!r}")

    registry = _parse_registry(raw.get("registry"), warnings)
    automations = _automations(items, multi, registry, warnings)
    for automation in automations:
        automation.own_initial = initial_states.pop(automation.entity_id, None)

    return _Scenario(core, start, end, automations, initial_states, events, services, overrun, seed, preview_features, registry, warnings, multi, traces)


def _automations(items: list[Any], multi: bool, registry: dict[str, Any], warnings: list[dict[str, Any]]) -> list[_Automation]:
    """Each automation with the id and entity id it gets in the sandbox.

    Ids are unique, since Home Assistant sets up only the first automation with a given id: one without an id gets `DEFAULT_AUTOMATION_ID` (with its index under `automations`), and a repeated one is replaced the same way, with a warning. Entity ids are predicted as `_own_entity_id` does, numbered `_2`, `_3`, ... past one already taken, as the entity registry numbers them.
    """
    automations: list[_Automation] = []
    ids: set[str] = set()
    taken = {str(entry.get("entity_id")) for entry in registry.get("entities") or ()}
    for index, config in enumerate(items):
        given = config.get("id") if isinstance(config, dict) else None
        automation_id = str(given) if given else DEFAULT_AUTOMATION_ID if not multi else f"{DEFAULT_AUTOMATION_ID}_{index}"
        if automation_id in ids:
            fallback = f"{DEFAULT_AUTOMATION_ID}_{index}"
            while fallback in ids:
                fallback += "_"
            warnings.append({**_warning("duplicate_automation_id", f"automations[{index}] repeats the id {automation_id!r}; it replays as {fallback!r}"), "automation": index})
            automation_id = fallback
        ids.add(automation_id)
        entity_id = _own_entity_id(config if isinstance(config, dict) else {}, automation_id, index, registry)
        if entity_id in taken and not _registered(registry, automation_id, entity_id):
            base, number = entity_id, 2
            while f"{base}_{number}" in taken:
                number += 1
            entity_id = f"{base}_{number}"
        taken.add(entity_id)
        automations.append(_Automation(index, config, automation_id, entity_id))
    return automations


def _own_entity_id(config: dict[str, Any], automation_id: str, index: int, registry: dict[str, Any]) -> str:
    """The entity id a replayed automation gets: its registry entry's (platform `automation`, unique id = the config id), else the one Home Assistant derives from its name (its alias, else `automation <list position>`)."""
    for entry in registry.get("entities") or ():
        if entry.get("platform") == "automation" and entry.get("unique_id") == automation_id and str(entry.get("entity_id", "")).startswith("automation."):
            return entry["entity_id"]
    alias = config.get("alias")
    return f"automation.{slugify(alias if isinstance(alias, str) and alias else f'automation {index}')}"


def _registered(registry: dict[str, Any], automation_id: str, entity_id: Any) -> bool:
    """Whether the snapshot registers `entity_id` as the automation with this id."""
    return any(
        entry.get("platform") == "automation" and entry.get("unique_id") == automation_id and entry.get("entity_id") == entity_id
        for entry in registry.get("entities") or ()
    )


def _parse_registry(value: Any, warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """The `registry` snapshot: lists of storage-format entries per registry, config entry stubs, versions, and what the live side dropped."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ScenarioError("invalid_scenario", "registry must be an object")
    parsed: dict[str, Any] = {}
    total = 0
    for kind in (*_REGISTRY_STORES, "config_entries"):
        items = value.get(kind) or []
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ScenarioError("invalid_scenario", f"registry.{kind} must be a list of objects")
        total += len(items)
        parsed[kind] = items
    if total > MAX_REGISTRY_ITEMS:
        raise ScenarioError("invalid_scenario", f"registry carries {total} entries; the limit is {MAX_REGISTRY_ITEMS}")
    for stub in parsed["config_entries"]:
        if not all(isinstance(stub.get(key), str) for key in ("entry_id", "domain")):
            raise ScenarioError("invalid_scenario", "registry.config_entries items need string entry_id and domain")
    versions = value.get("versions") or {}
    if not isinstance(versions, dict):
        raise ScenarioError("invalid_scenario", "registry.versions must be an object")
    parsed["versions"] = {}
    for kind, (_, _, _, current) in _REGISTRY_STORES.items():
        version = versions.get(kind, list(current))
        if not (isinstance(version, list) and len(version) == 2 and all(isinstance(part, int) and not isinstance(part, bool) for part in version)):
            raise ScenarioError("invalid_scenario", f"registry.versions.{kind} must be [major, minor]")
        parsed["versions"][kind] = version
    store_data = value.get("store_data") or {}
    if not isinstance(store_data, dict) or not all(kind in _REGISTRY_STORES and isinstance(data, dict) for kind, data in store_data.items()):
        raise ScenarioError("invalid_scenario", "registry.store_data must map registry kinds to objects")
    parsed["store_data"] = store_data
    truncated = value.get("truncated") or {}
    if not isinstance(truncated, dict):
        raise ScenarioError("invalid_scenario", "registry.truncated must be an object")
    dropped = [f"{count} {kind}" for kind, count in truncated.items() if isinstance(count, int) and count > 0]
    if dropped:
        warnings.append(
            _warning("registry_truncated", f"The registry snapshot was capped: {', '.join(dropped)} left out, so area/label/device lookups may see less than live")
        )
    return parsed


def _write_registry_storage(config_dir: str, registry: dict[str, Any], stamp: datetime) -> None:
    """Seed the temp config dir's `.storage` with the snapshot, so the registries load it through their own load path (and migrations) at boot.

    Config entries are written as disabled stubs: they keep devices from being cleaned up as orphans and answer `config_entry_attr()` lookups, and being disabled they are never set up, so no integration code runs for them.
    """
    if not registry:
        return
    storage = os.path.join(config_dir, ".storage")
    os.makedirs(storage, exist_ok=True)

    def write(key: str, version: list[int] | tuple[int, int], data: dict[str, Any]) -> None:
        with open(os.path.join(storage, key), "w", encoding="utf-8") as file:
            json.dump({"version": version[0], "minor_version": version[1], "key": key, "data": data}, file)

    for kind, (key, items_key, deleted_key, _) in _REGISTRY_STORES.items():
        if not registry[kind]:
            continue
        # The rest of the live store's data first: a registry at its current version reads its keys without a migration to fill them in.
        data: dict[str, Any] = {**registry["store_data"].get(kind, {}), items_key: registry[kind]}
        if deleted_key:
            data[deleted_key] = []
        write(key, registry["versions"][kind], data)
    if registry["config_entries"]:
        now = stamp.isoformat()
        entries = [
            {
                "created_at": now,
                "data": {},
                "disabled_by": config_entries.ConfigEntryDisabler.USER.value,
                "discovery_keys": {},
                "domain": stub["domain"],
                "entry_id": stub["entry_id"],
                "minor_version": 1,
                "modified_at": now,
                "options": {},
                "pref_disable_new_entities": False,
                "pref_disable_polling": False,
                "source": config_entries.SOURCE_USER,
                "subentries": [],
                "title": str(stub.get("title") or stub["domain"]),
                "unique_id": None,
                "version": 1,
            }
            for stub in registry["config_entries"]
        ]
        write(config_entries.STORAGE_KEY, (config_entries.STORAGE_VERSION, config_entries.STORAGE_VERSION_MINOR), {"entries": entries})


def _write_own_last_state(config_dir: str, scenario: _Scenario) -> None:
    """Store each automation's recorded on/off state at the window start as its last state, so it boots enabled or disabled as live HA restores it after a restart (`last_triggered` included)."""
    states = []
    for automation in scenario.automations:
        item = automation.own_initial
        if item is None or item.state not in (STATE_ON, STATE_OFF):
            continue
        last_changed = item.last_changed or scenario.start
        states.append(
            {
                "entity_id": automation.entity_id,
                "state": item.state,
                "attributes": item.attributes,
                "last_changed": last_changed.isoformat(),
                "last_updated": max(item.last_updated or last_changed, last_changed).isoformat(),
            }
        )
    if not states:
        return
    storage = os.path.join(config_dir, ".storage")
    os.makedirs(storage, exist_ok=True)
    with open(os.path.join(storage, restore_state.STORAGE_KEY), "w", encoding="utf-8") as file:
        json.dump(
            {
                "version": restore_state.STORAGE_VERSION,
                "key": restore_state.STORAGE_KEY,
                "data": [{"state": state, "extra_data": None, "last_seen": scenario.start.isoformat()} for state in states],
            },
            file,
        )


def _parse_preview_features(value: Any) -> list[tuple[str, str]]:
    """`core.preview_features`: the Labs preview features enabled on the live instance, as `[domain, preview_feature]` pairs."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item) for item in value
    ):
        raise ScenarioError("invalid_scenario", "core.preview_features must be a list of [domain, preview_feature] pairs")
    return [(domain, feature) for domain, feature in value]


# --- platforms the sandbox cannot load --------------------------------------------------------------


async def _check_platforms(hass: HomeAssistant, config: dict[str, Any]) -> None:
    """Refuse, by name and config path, triggers/conditions/device automations the sandbox cannot run.

    - Device triggers, conditions and actions of integration-specific platforms (zha, mqtt, hue, ...): they read that integration's runtime state, which the sandbox does not have. Those of entity-domain integrations (binary_sensor, light, sensor, ...) work from the registry snapshot and are allowed.
    - Trigger and condition platforms of integrations that are not built into Home Assistant: custom integrations are never loaded in the sandbox, since that would run third-party code in it.
    """
    problems: list[str] = []
    for path, node in _walk(config, ""):
        if isinstance(node.get("domain"), str) and isinstance(node.get("type"), str) and "device_id" in node:
            kind = "trigger" if "device" in (node.get("trigger"), node.get("platform")) else "condition" if node.get("condition") == "device" else "action"
            domain = node["domain"]
            integration = await _integration(hass, domain)
            if integration is None:
                problems.append(f"{path}: device {kind} from '{domain}', an integration that is not built into Home Assistant (custom integrations are not loaded in the sandbox)")
            elif integration.integration_type != _GENERIC_DEVICE_AUTOMATION_TYPE:
                problems.append(
                    f"{path}: device {kind} from '{domain}' is specific to that integration and needs it running; only device {kind}s of entity domains (binary_sensor, light, sensor, switch, ...) replay"
                )
            continue
        for key, aliases, what in (("trigger", _TRIGGER_ALIASES, "trigger"), ("platform", _TRIGGER_ALIASES, "trigger"), ("condition", _CONDITION_ALIASES, "condition")):
            name = node.get(key)
            if not isinstance(name, str) or (key == "trigger" and "condition" in node):
                continue
            domain = name.split(".", 1)[0]
            domain = aliases.get(domain, domain)
            if domain and await _integration(hass, domain) is None:
                problems.append(
                    f"{path}: {what} '{name}' comes from '{domain}', which is not an integration built into Home Assistant: a custom integration (never loaded in the sandbox) or a typo"
                )
            break
    if problems:
        raise ScenarioError("unsupported_automation", "The sandbox cannot replay this automation: " + "; ".join(problems))


_TRIGGER_ALIASES: dict[str, str | None] = dict(getattr(trigger_helper, "_PLATFORM_ALIASES", {}))
_CONDITION_ALIASES: dict[str | None, str | None] = dict(getattr(condition_helper, "_PLATFORM_ALIASES", {}))


async def _integration(hass: HomeAssistant, domain: str) -> Any:
    try:
        return await async_get_integration(hass, domain)
    except IntegrationNotFound:
        return None


def _walk(node: Any, path: str) -> Any:
    """Every dict in the config with its slash path (`triggers/0`, `actions/1/if/0`), skipping service-call payloads."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            if key not in _PAYLOAD_KEYS:
                yield from _walk(value, f"{path}/{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}/{index}")


def _warning(code: str, message: str, at: datetime | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "at": None if at is None else at.isoformat()}


# --- the replay ---------------------------------------------------------------------------------


class _LogCollector(logging.Handler):
    """Turns HA warnings/errors logged during the window into result warnings (capped)."""

    def __init__(self, replay: _Replay) -> None:
        super().__init__(logging.WARNING)
        self._replay = replay
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if len(self._replay.log_warnings) >= _MAX_LOG_WARNINGS:
            self.dropped += 1
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log call must not break the replay
            message = str(record.msg)
        self._replay.log_warnings.append(
            {"code": "log", "message": f"{record.levelname} {record.name}: {message}", "at": self._replay.now().isoformat()}
        )


class _Replay:
    """One replay: boots HA, plays the timeline, collects the result."""

    def __init__(self, scenario: _Scenario, clock: VirtualClock, config_dir: str) -> None:
        self.scenario = scenario
        self.clock = clock
        self.config_dir = config_dir
        self.hass: HomeAssistant | None = None
        # Each run in start order as (trace key, its trace while running or under `full`, else its trimmed record).
        self.runs: list[tuple[str, Any]] = []
        self.service_calls: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = list(scenario.warnings)
        self.log_warnings: list[dict[str, Any]] = []
        self._unknown_services: set[str] = set()
        self._self_calls: set[tuple[int, str]] = set()
        # Trace key (`automation.<id>`) and own entity id -> the automation, once loaded.
        self._by_key: dict[str, _Automation] = {}
        self._by_entity_id: dict[str, _Automation] = {}

    def now(self) -> datetime:
        return self.clock.utcnow()

    async def run(self) -> dict[str, Any]:
        original_store = automation_trace.async_store_trace

        def store_trace(hass: Any, trace: Any, stored_traces: int) -> None:
            self.runs.append((trace.key, trace))
            if self.scenario.traces != "full":
                self._trim_when_finished(trace, len(self.runs) - 1)
            original_store(hass, trace, stored_traces)

        automation_trace.async_store_trace = store_trace
        collector = _LogCollector(self)
        ha_logger = logging.getLogger("homeassistant")
        try:
            try:
                try:
                    await self._boot()
                except ScenarioError as err:
                    return error_result(err.code, str(err), self.warnings)
                await self._sleep_until(self.scenario.start)
                self._seed_states()
                ha_logger.addHandler(collector)
                try:
                    await self._load_automations()
                except ScenarioError as err:
                    return error_result(err.code, str(err), self.warnings)
                if self.clock.loop.time() != self.clock.loop_time_for(self.scenario.start):
                    # Setup waited on a timer: the automation attached late, so the window's first moments were not observed.
                    what = "The automations" if self.scenario.multi else "The automation"
                    self.warnings.append(_warning("late_attach", f"{what} attached at {self.now().isoformat()}, after the window start", self.now()))
                self._schedule_events()
                await self._sleep_until(self.scenario.end)
                for automation in self._loaded():
                    await automation.entity.async_turn_off(stop_actions=False)
                await self._drain()
                return self._result(collector)
            finally:
                ha_logger.removeHandler(collector)
                await self._stop()
        finally:
            automation_trace.async_store_trace = original_store

    def _trim_when_finished(self, trace: Any, slot: int) -> None:
        """Replace the run's trace with its trimmed record as soon as it finishes, so only Home Assistant's own stored traces stay alive."""
        finished = trace.finished

        def finish() -> None:
            finished()
            del trace.finished
            self.runs[slot] = (trace.key, _run_record(trace, self.scenario.traces))

        trace.finished = finish

    async def _boot(self) -> None:
        self.hass = hass = HomeAssistant(self.config_dir)
        # Replaced before anything registers a service, so every registration lands in the recording registry.
        hass.services = RecordingServiceRegistry(hass, self._record_call)
        loader.async_setup(hass)
        hass.config.skip_pip = True
        hass.config.skip_pip_packages = []
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        try:
            await bootstrap.async_load_base_functionality(hass)
        except Exception as err:
            if not self.scenario.registry:
                raise
            raise ScenarioError("invalid_scenario", f"The registry snapshot could not be loaded: {type(err).__name__}: {err}") from err
        try:
            await async_process_ha_core_config(hass, dict(self.scenario.core))
        except Exception as err:
            raise ScenarioError("invalid_scenario", f"core config rejected: {err}") from err
        if not await async_setup_component(hass, "homeassistant", {}):
            raise RuntimeError("The homeassistant integration failed to set up")
        await self._enable_preview_features(hass)
        await hass.async_start()
        await hass.async_block_till_done()

    async def _enable_preview_features(self, hass: HomeAssistant) -> None:
        """Set up `labs` (live HA always has it) and turn on the scenario's preview features, before the automation is validated."""
        if not await async_setup_component(hass, "labs", {}):
            raise RuntimeError("The labs integration failed to set up")
        for domain, feature in self.scenario.preview_features:
            try:
                await labs.async_update_preview_feature(hass, domain, feature, True)
            except ValueError:
                self.warnings.append(
                    _warning("preview_feature_unknown", f"Preview feature {domain}.{feature} does not exist in this Home Assistant; ignored")
                )

    async def _sleep_until(self, when: datetime) -> None:
        """Wait until the virtual clock reads exactly `when`."""
        if self.clock.loop_time_for(when) <= self.clock.loop.time():
            return
        future = self.clock.loop.create_future()
        self.clock.call_at_datetime(when, lambda: future.done() or future.set_result(None))
        await future

    def _seed_states(self) -> None:
        states = self.hass.states
        start = self.scenario.start
        for entity_id, item in self.scenario.initial_states.items():
            last_changed = item.last_changed or start
            last_updated = max(item.last_updated or last_changed, last_changed)
            if last_updated > last_changed:
                # Written twice so last_changed and last_updated both match the record: the state at last_changed, then an attribute update at last_updated.
                states.async_set(entity_id, item.state, {_SEED_MARKER: True}, timestamp=last_changed.timestamp())
            states.async_set(entity_id, item.state, item.attributes, timestamp=last_updated.timestamp())

    def _loaded(self) -> list[_Automation]:
        return [automation for automation in self.scenario.automations if automation.entity is not None]

    async def _load_automations(self) -> None:
        """Validate each automation, then set them all up in one go.

        With one `automation`, a refusal is the result's error. Under `automations` it is that automation's own `error` and the rest still replay; only the automation integration failing to set up fails them all.
        """
        multi = self.scenario.multi
        configs: list[dict[str, Any]] = []
        for automation in self.scenario.automations:
            try:
                configs.append(await self._validate(automation))
            except ScenarioError as err:
                if not multi:
                    raise
                automation.error = {"code": err.code, "message": str(err)}
        if not configs:
            return
        if not await async_setup_component(self.hass, "automation", {"automation": configs}):
            raise ScenarioError("invalid_automation", "The automation integration failed to set up")
        # Setup awaits the trigger attachment itself. No async_block_till_done: it would wait out any run already started and so move virtual time.
        entities = {entity.unique_id: entity for entity in self.hass.data[AUTOMATION_COMPONENT].entities}
        for automation in self.scenario.automations:
            if automation.error is not None:
                continue
            entity = entities.get(automation.id)
            if entity is None or getattr(entity, "action_script", None) is None:
                if not multi:
                    raise ScenarioError("invalid_automation", "The automation did not load")
                automation.error = {"code": "invalid_automation", "message": "The automation did not load"}
                continue
            automation.entity = entity
            automation.entity_id = entity.entity_id
            self._by_key[f"automation.{automation.id}"] = automation
            self._by_entity_id[entity.entity_id] = automation
        self.hass.services.intercepting = True

    async def _validate(self, automation: _Automation) -> dict[str, Any]:
        """The config to set up for one automation, checked as Home Assistant checks it; refusals raise `ScenarioError`."""
        if not isinstance(automation.config, dict):
            raise ScenarioError("invalid_automation", f"automations[{automation.index}] must be an object (the automation config)")
        config = {**automation.config, "id": automation.id}
        if automation.own_initial is not None and automation.own_initial.state in (STATE_ON, STATE_OFF):
            # Its recorded state at the window start says whether it was enabled; that wins over the configured state at boot.
            config.pop("initial_state", None)
        elif config.get("initial_state") is False:
            config.pop("initial_state")
            self._warn(automation, "initial_state_ignored", "initial_state: false was ignored so the automation can run")

        await _check_platforms(self.hass, config)
        try:
            await async_validate_config_item(self.hass, config["id"], config)
        except vol.Invalid as err:
            raise ScenarioError("invalid_automation", humanize_error(config, err)) from err
        except (HomeAssistantError, ValueError, KeyError, TypeError) as err:
            raise ScenarioError("invalid_automation", str(err) or type(err).__name__) from err
        return config

    def _warn(self, automation: _Automation | None, code: str, message: str, at: datetime | None = None) -> None:
        """A warning about one automation: under `automations` it carries the index and names it."""
        if automation is None or not self.scenario.multi:
            self.warnings.append(_warning(code, message, at))
            return
        self.warnings.append({**_warning(code, f"automations[{automation.index}]: {message}", at), "automation": automation.index})

    def _schedule_events(self) -> None:
        by_instant: dict[datetime, list[_Event]] = {}
        for event in self.scenario.events:
            by_instant.setdefault(event.at, []).append(event)
        for at, events in by_instant.items():
            # One callback per instant: same-instant timers have no guaranteed order, list order does.
            self.clock.call_at_datetime(at, self._play, events)

    def _play(self, events: list[_Event]) -> None:
        for event in events:
            if (automation := self._by_entity_id.get(event.entity_id)) is not None:
                self._play_own(automation.entity, event)
            else:
                self.hass.states.async_set(event.entity_id, event.state, event.attributes, timestamp=event.at.timestamp())

    def _play_own(self, entity: Any, event: _Event) -> None:
        """A recorded state of the automation itself turns it on or off, as the `automation.turn_on` / `turn_off` services do (`stop_actions` at its default); the entity writes its own state. Attribute-only updates (`last_triggered`, `current`) and other states are its own doing, so they are not played."""
        if event.state == STATE_ON and not entity.is_on:
            self.hass.async_create_task(entity.async_turn_on())
        elif event.state == STATE_OFF and entity.is_on:
            # Eager: its triggers are detached before the next event of the same instant is written.
            self.hass.async_create_task(entity.async_turn_off())

    async def _drain(self) -> None:
        """Let runs in flight at `end` finish, up to the overrun bound."""
        bound = self.clock.loop_time_for(self.scenario.end + self.scenario.overrun)
        scripts = [automation.entity.action_script for automation in self._loaded()]
        # No async_block_till_done here: it would wait for the in-flight runs themselves and so carry virtual time past the bound.
        while any(script.is_running for script in scripts) and self.clock.loop.time() < bound:
            await asyncio.sleep(min(_DRAIN_STEP, bound - self.clock.loop.time()))

    async def _stop(self) -> None:
        if self.hass is None:
            return
        try:
            await self.hass.async_stop()
        except Exception:
            _LOGGER.exception("Stopping the sandbox Home Assistant failed")

    def _record_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        target: dict[str, Any] | None,
        context: Any,
    ) -> None:
        trace_id = trace_id_get()
        automation = self._by_key.get(trace_id[0]) if trace_id else None
        if automation is None and not self.scenario.multi:
            automation = self.scenario.automations[0]
        call = {
            "at": self.now(),
            "domain": domain,
            "service": service,
            "data": dict(data),
            "target": dict(target) if target else None,
            "run": trace_id[1] if trace_id else None,
            "context": {"id": context.id, "parent_id": context.parent_id, "user_id": context.user_id},
        }
        if self.scenario.multi:
            call["automation"] = None if automation is None else automation.index
        if self.scenario.traces != "none":
            self.service_calls.append(call)
        known = self.scenario.services
        name = f"{domain}.{service}"
        if (
            automation is not None
            and domain == "automation"
            and service in _SELF_SERVICES
            and (automation.index, name) not in self._self_calls
            and self._targets_self(automation.entity_id, data, target)
        ):
            self._self_calls.add((automation.index, name))
            own = automation.entity_id
            if service == "trigger":
                message = f"The automation calls {name} on itself ({own}). Like every service call it is only recorded, so the replay does not start that extra run"
            else:
                message = (
                    f"The automation calls {name} on itself ({own}). Like every service call it is only recorded: in the replay it is turned on and off by "
                    f"the recorded state of {own}, when that is on the timeline"
                )
            self._warn(automation, "self_service_call", message, self.now())
        if known is not None and service not in known.get(domain, ()) and name not in self._unknown_services:
            self._unknown_services.add(name)
            self.warnings.append(_warning("unknown_service", f"{name} does not exist on the live instance", self.now()))

    @staticmethod
    def _targets_self(own: str, data: dict[str, Any], target: dict[str, Any] | None) -> bool:
        for value in (data.get("entity_id"), (target or {}).get("entity_id")):
            ids = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
            if any(isinstance(entity_id, str) and entity_id.strip().lower() in (own, "all") for entity_id in ids):
                return True
        return False

    def _result(self, collector: _LogCollector) -> dict[str, Any]:
        start, end = self.scenario.start, self.scenario.end
        runs: list[dict[str, Any]] = []
        dropped_ids: set[str] = set()
        for key, item in self.runs:
            # Still running at the overrun bound, or `full`: reduced only now.
            run = item if isinstance(item, dict) else _run_record(item, self.scenario.traces)
            automation = self._by_key.get(key)
            if self.scenario.multi:
                run["automation"] = None if automation is None else automation.index
            started = run["started"]
            if started is None or not start <= started < end:
                dropped_ids.add(run["id"])
                continue
            if automation is not None:
                automation.run_count += 1
            if run["outcome"] == "unfinished":
                self._warn(automation, "run_unfinished", f"Run {run['id']} was still running at the overrun bound ({self.scenario.overrun} after the end)", started)
            runs.append(run)
        calls = [call for call in self.service_calls if call["run"] not in dropped_ids]
        warnings = self.warnings + self.log_warnings
        if collector.dropped:
            warnings.append(_warning("log_truncated", f"{collector.dropped} more log warnings were not included"))
        result = {
            "version": RESULT_VERSION,
            "start": start,
            "end": end,
            "error": None,
            "runs": runs,
            "service_calls": calls,
            "warnings": warnings,
        }
        if self.scenario.multi:
            result["automations"] = [
                {"index": item.index, "entity_id": item.entity_id, "loaded": item.entity is not None, "error": item.error, "run_count": item.run_count}
                for item in self.scenario.automations
            ]
        return _json_safe(result)


def _run_record(trace: Any, traces: str = DEFAULT_TRACES) -> dict[str, Any]:
    """One run of the result from an automation trace, carrying what `traces` asks for."""
    short = trace.as_short_dict()
    execution = short.get("script_execution")
    error = short.get("error")
    extended: dict[str, Any] | None = None
    if traces == "full":
        extended = trace.as_extended_dict()
        steps: dict[str, list[dict[str, Any]]] = extended.get("trace") or {}
    else:
        steps = _trigger_and_condition_steps(trace)
    last_step = short.get("last_step")

    stopped_by: str | None = None
    if short.get("state") == "running":
        outcome = "unfinished"
    elif execution == "failed_conditions":
        outcome = "condition_failed"
        stopped_by = _failed_condition(steps)
    elif execution in ("failed_single", "failed_max_runs", "disallowed_recursion_detected"):
        outcome = "dropped"
        stopped_by = {"failed_single": "mode_single", "failed_max_runs": "max_runs"}.get(execution, "recursion")
    elif error is not None or execution == "error":
        outcome = "error"
        stopped_by = last_step
    elif execution == "aborted":
        outcome = "aborted"
        stopped_by = last_step
    elif execution == "cancelled":
        outcome = "cancelled"
        stopped_by = last_step
    elif execution == "finished":
        outcome = "fired"
    else:
        outcome = "error"
        stopped_by = last_step

    timestamps = short.get("timestamp") or {}
    finished = timestamps.get("finish") if outcome != "unfinished" else None
    trigger = _trigger_summary(steps, short.get("trigger"))
    if traces == "none":
        return {"id": trace.run_id, "started": timestamps.get("start"), "finished": finished, "outcome": outcome, "trigger": {"idx": trigger["idx"], "id": trigger["id"]}}
    record = {
        "id": trace.run_id,
        "started": timestamps.get("start"),
        "finished": finished,
        "trigger": trigger,
        "outcome": outcome,
        "stopped_by": stopped_by,
        "script_execution": execution,
        "error": error,
    }
    if extended is not None:
        record["trace"] = extended
    else:
        record["conditions"] = {
            path: (elements[-1].get("result") or {}).get("result") for path, elements in steps.items() if path.startswith("condition/") and path.count("/") == 1 and elements
        }
    return record


def _trigger_and_condition_steps(trace: Any) -> dict[str, list[dict[str, Any]]]:
    """The trace's trigger and condition steps only, as `as_extended_dict()` would give them, without serializing the action steps."""
    elements = getattr(trace, "_trace", None)
    if isinstance(elements, dict):
        return {path: [step.as_dict() for step in steps] for path, steps in elements.items() if _is_trigger_or_condition(path)}
    # Not the ActionTrace layout this was written against: take the long way.
    return {path: steps for path, steps in (trace.as_extended_dict().get("trace") or {}).items() if _is_trigger_or_condition(path)}


def _is_trigger_or_condition(path: str) -> bool:
    return path == "trigger" or path.startswith(("trigger/", "condition/"))


def _failed_condition(steps: dict[str, list[dict[str, Any]]]) -> str | None:
    """Trace path of the top-level condition that evaluated false (first one, in evaluation order)."""
    fallback = None
    for path, elements in steps.items():
        if not path.startswith("condition/"):
            continue
        for element in elements:
            if (element.get("result") or {}).get("result") is False:
                if path.count("/") == 1:
                    return path
                fallback = fallback or path
    return fallback


def _trigger_summary(steps: dict[str, list[dict[str, Any]]], description: str | None) -> dict[str, Any]:
    """The firing trigger's essentials; the full trigger variables stay in the trace."""
    trigger: dict[str, Any] = {}
    for path, elements in steps.items():
        if path == "trigger" or path.startswith("trigger/"):
            if elements:
                trigger = (elements[0].get("changed_variables") or {}).get("trigger") or {}
            break
    summary = {
        "idx": trigger.get("idx"),
        "id": trigger.get("id"),
        "alias": trigger.get("alias"),
        "platform": trigger.get("platform"),
        "description": description if description is not None else trigger.get("description"),
        "entity_id": trigger.get("entity_id"),
    }
    for key in ("from_state", "to_state"):
        state = trigger.get(key)
        summary[key] = getattr(state, "state", None) if state is not None and not isinstance(state, dict) else (state or {}).get("state")
    return summary


def _json_safe(value: Any) -> Any:
    """Round-trip through HA's trace encoder so the result is plain JSON types."""
    return json.loads(json.dumps(value, cls=ExtendedJSONEncoder))


# --- service interception -------------------------------------------------------------------------


class RecordingServiceRegistry(ServiceRegistry):
    """Service registry that records calls instead of executing them.

    Until `intercepting` is set (once the automation under test is loaded) it behaves like the stock registry, so boot is unchanged. After that every service exists, every call is recorded and answered with success (an empty dict when a response is requested), and `call_service` is still fired on the bus so event triggers on it behave.
    """

    __slots__ = ("_on_call", "intercepting")

    def __init__(self, hass: HomeAssistant, on_call: Callable[..., None]) -> None:
        super().__init__(hass)
        self._on_call = on_call
        self.intercepting = False

    def has_service(self, domain: str, service: str) -> bool:
        return True if self.intercepting else super().has_service(domain, service)

    def supports_response(self, domain: str, service: str) -> SupportsResponse:
        handler = self._services.get(domain.lower(), {}).get(service.lower())
        if handler is None:
            # Unknown here (it may exist on the live instance): accept calls with or without a response.
            return SupportsResponse.OPTIONAL
        return handler.supports_response

    async def async_call(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = False,
        context: Context | None = None,
        target: dict[str, Any] | None = None,
        return_response: bool = False,
    ) -> Any:
        if not self.intercepting:
            return await super().async_call(domain, service, service_data, blocking, context, target, return_response)
        context = context or Context()
        domain, service = domain.lower(), service.lower()
        data = dict(service_data or {})
        self._on_call(domain, service, data, target, context)
        self._hass.bus.async_fire_internal(
            EVENT_CALL_SERVICE,
            {ATTR_DOMAIN: domain, ATTR_SERVICE: service, ATTR_SERVICE_DATA: {**data, **(target or {})}},
            context=context,
        )
        return {} if return_response else None
