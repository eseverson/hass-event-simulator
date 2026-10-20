"""Replay engine: runs one scenario against one automation (or several independent ones) in a throwaway Home Assistant.

Runs in the child process (see `__main__.py`). `run_scenario(scenario)` is the whole contract: a scenario dict in, a JSON-safe result dict out.

The scenario carries either `automation` (one config) or `automations` (a list of up to `MAX_AUTOMATIONS` configs, for validating many candidates against one timeline at the cost of one boot). Every service call is only recorded, so automations replayed together cannot affect each other: each runs as it would alone, with its own mode, its own entity (the own-entity handling below applies to each) and its own id (one without an id, or repeating an earlier one, gets `DEFAULT_AUTOMATION_ID` plus its index).

Timeline of a replay (all virtual time, see `vclock.py`)
--------------------------------------------------------
1. `start - BOOT_LEAD`: a `HomeAssistant` boots on the virtual loop with a temporary config dir (deleted afterwards). The scenario's `registry` snapshot is written into that dir's `.storage` first, so the entity/device/area/floor/label registries load it through their own load path (config entries as disabled stubs, never set up); registered entities with no initial state get HA's own `unavailable` placeholder at start, as live HA gives entities whose integration did not load. It loads the base registries, sets up only the `homeassistant` and `labs` integrations (turning on the scenario's `core.preview_features`, so Labs-gated triggers and conditions validate as they do live), then runs `async_start`; the automation integration and whatever it pulls in (`trace`, `blueprint`, trigger/condition platforms) come later. No recorder, http, frontend or network-facing integrations, and `skip_pip` is on, so nothing is installed or fetched. `BOOT_LEAD` absorbs any timers boot waits on, so the window starts with everything settled.
2. `start`: the scenario's optional `helpers` (`[{domain, entity_id, options, title?}]`, core Trend and Derivative helpers; see `helpers.py` and `_Replay._setup_helpers`) are set up first, as config entries of those integrations with the requested entity ids, so they take in the seeded states and compute from the replayed source states on virtual time; a helper's own recorded states are not played (its state at the start is restored as its last state). Then initial states are seeded with their recorded `last_changed` / `last_updated` (`StateMachine.async_set(..., timestamp=...)`; an attribute-only update is reproduced by a second write at `last_updated`); states without them are stamped `start`, the same clamp the recorder applies. The automation's own entity is the exception: its recorded state is not seeded but stored as its last state before boot, so it loads enabled or disabled (and with its `last_triggered`) as live HA restores it. Then the scenario's optional `device_triggers` (`{domain: {device_id: data}}`, read on the live side) is seeded where those integrations' own `device_trigger` modules look for it (see `device_triggers.py`; ZHA's trigger cache), so their device triggers attach as live. Then the automation is checked for platforms the sandbox cannot run (custom integrations' triggers/conditions, integration-specific device automations other than the seeded device triggers -> `unsupported_automation`, naming each by config path), validated with HA's own automation schema (invalid -> structured `invalid_automation` error) and set up. It attaches after `async_start`, so a `homeassistant: start` trigger does not fire, and at `start` exactly, so a time trigger at the window start does.
3. `[start, end)`: timeline events are written with `async_set` at their `at`; events at the same instant are written in list order. The optional `bus_events` (`[{at, event_type, data}]`, recorded events such as `zha_event` or `tag_scanned`) are fired on the bus at their `at` (`time_fired` set to it), after the state writes of the same instant, so event triggers replay and a condition reads the states of that instant. Home Assistant's own lifecycle and bookkeeping events (`state_changed`, `call_service`, `homeassistant_start`, ...) are refused: the sandbox's Home Assistant fires those itself. Calendar triggers ask a stub calendar per calendar entity for its events (see `_Replay._serve_calendars`): the scenario's optional `calendars` (`{entity_id: [{start, end, summary, description?, location?}]}`, the shape `calendar.get_events` answers with; see `calendars.py`), else the events the calendar's own recorded states name (a `calendar_from_states` warning, or `calendar_no_events` when there are none); the calendar's recorded states still replay as recorded. A recorded on/off of a replayed automation's own entity turns that automation on/off instead (`automation.turn_on` / `turn_off` semantics), so the entity's state, and every template reading it, is the sandbox's own. Every service call is intercepted at the `ServiceRegistry` (see `RecordingServiceRegistry`): recorded, answered with success (an empty response when one is asked for), and never executed, so recorded calls change no state.
4. `end`: the automation's triggers are detached (`async_turn_off(stop_actions=False)`), so nothing new starts; the window is half-open and a run triggered at `end` itself is discarded. Runs already in flight (a `delay`, a `wait_for_trigger`, a queue) keep going for up to `overrun_seconds` of virtual time (default `DEFAULT_OVERRUN`, at most `MAX_OVERRUN`); a run still going at that bound is reported with outcome `unfinished` and a warning. Then HA is stopped.

Runs that never let time move on
--------------------------------
Recorded service calls take no virtual time, so an automation that re-triggers itself (it fires an event, or makes a service call, that one of its own triggers listens for) can start run after run at one virtual instant and the clock never moves again. Where live Home Assistant stops such a loop itself the replay does too: a recorded call suspends its run as a live device call does (see `RecordingServiceRegistry`), so in `mode: single` the re-triggered run meets the first still running and is dropped. Where live Home Assistant would loop without end (`queued`, `restart`, or a run that re-triggers itself after its last call), the replay ends it and says so: an automation that starts `MAX_RUNS_PER_INSTANT` runs at one instant is turned off there, as `automation.turn_off` would (its runs in flight are stopped and its own recorded `on` no longer turns it back on), with a `runaway_loop` warning naming it, the instant and the count; its runs so far are kept and the rest of the scenario replays on. Independently, a wall-clock watchdog (scenario `time_limit_seconds`, default `DEFAULT_TIME_LIMIT`; the runner sets it under its own timeout) ends a replay still going at the limit, wherever virtual time stands: the window ends there, runs in flight are stopped, and a `time_limit` warning names each automation still running. A run stuck in code that never yields to the loop (a huge `repeat: count`) is cancelled to get there (see `_Watchdog`). Both are answers with the runs so far, not errors. `repeat: while` / `until` without a delay need neither: Home Assistant itself aborts them after 10000 iterations.

Runs are read from HA's own automation traces (captured as `trace_automation` stores them, so the stored-traces limit never drops one) and serialized with `as_extended_dict()`.

Result
------
    {
      "version": 1, "start": iso, "end": iso, "error": null | {"code", "message"},
      "runs": [{"id", "started", "finished", "trigger": {...}, "outcome", "stopped_by", "script_execution", "error", "trace"}],
      "service_calls": [{"at", "domain", "service", "data", "target", "run", "context": {"id", "parent_id", "user_id"}}],
      "warnings": [{"code", "message", "at"}],
      "helper_states": {entity_id: [{"at", "state"}]}
    }

`helper_states` has each helper's state at the window start, then every change of its state in the window (attribute-only updates left out), at most `helpers.MAX_HELPER_STATES` per helper (`helper_states_truncated` warning past that); `{}` without helpers.

`traces` sets how much each run carries: `full` (the default) is the shape above. `summary` drops `trace` and adds `conditions`, the result of each top-level condition by trace path (`{"condition/0": false}`; empty when none was evaluated). `none` keeps only `id`, `started`, `finished`, `outcome` and `trigger` as `{"idx", "id"}`, and records no service calls (`service_calls` is empty). Trimmed runs are reduced as each one finishes, so the child never holds more than Home Assistant's own few stored traces per automation; that keeps a month of many automations well under the runner's output cap.

Under `automations`, each run and service call also carries `"automation": index`, per-automation warnings carry it too, and the result adds `"automations": [{"index", "entity_id", "loaded", "error": null | {"code", "message"}, "run_count"}]`. An automation the sandbox refuses (`invalid_automation`, `unsupported_automation`) is reported there with `loaded: false` and the others still replay; one stopped mid-replay (`runaway_loop`, `time_limit`, see below) keeps `loaded: true` and gets that `error`. The top-level `error` is only for a problem with the scenario itself.

`outcome`: `fired` (the actions ran to the end or a `stop`), `condition_failed` (`stopped_by` = the failing condition's trace path, e.g. `condition/0`), `dropped` (the script mode refused the run; `stopped_by` = `mode_single` / `max_runs` / `recursion`), `aborted` (a condition or `stop: error` inside the actions; `stopped_by` = last action path), `cancelled` (a `restart` re-trigger or shutdown), `error` (an exception; `stopped_by` = last action path, `error` = message), `unfinished` (still running at the overrun bound), `not_triggered` (HA 2026.9+: a trigger evaluated a change but did not fire, e.g. a non-numeric value for a threshold trigger, and HA recorded why; `stopped_by` = that reason, e.g. `entity_value_not_numeric`; nothing ran). Any other value is a `script_execution` this engine does not know yet, passed through as is.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import signal
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    EVENT_COMPONENT_LOADED,
    EVENT_CORE_CONFIG_UPDATE,
    EVENT_HOMEASSISTANT_CLOSE,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_START,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_SERVICE_REGISTERED,
    EVENT_SERVICE_REMOVED,
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    MAX_LENGTH_EVENT_EVENT_TYPE,
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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.json import ExtendedJSONEncoder
from homeassistant.helpers.trace import script_execution_set, trace_id_get
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
import voluptuous as vol  # noqa: I001 - after homeassistant, see above
from voluptuous.humanize import humanize_error

from . import calendars as calendar_events, device_triggers as scenario_device_triggers, helpers as scenario_helpers
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
# Runs one automation may start at a single virtual instant before it counts as re-triggering itself without end and is turned off (`runaway_loop`). Recorded events of one instant start a handful; a loop starts them until something stops it.
MAX_RUNS_PER_INSTANT = 1000
# Real seconds a replay may take before the watchdog stops it with the runs so far (`time_limit`), unless the scenario sets `time_limit_seconds`. Under the runner's own timeout, so the child answers before it is killed.
DEFAULT_TIME_LIMIT = 240.0
# Real seconds the watchdog waits for the loop to take its stop before it cancels whatever code is running and keeps it from yielding (repeated until the loop answers).
_HARD_STOP_GRACE = 2.0
# Upper bound on registry entries a scenario may carry (the live side caps far lower).
MAX_REGISTRY_ITEMS = 10_000
# Device automation platforms the sandbox can run: those of entity-domain integrations (binary_sensor, light, sensor, ...), which work from the entity and device registries alone. Integration-specific ones (zha, mqtt, hue, ...) need that integration running.
_GENERIC_DEVICE_AUTOMATION_TYPE = "entity"
# Services that act on an automation entity; an automation calling one on itself gets a warning, since the call is only recorded.
_SELF_SERVICES = frozenset({"turn_on", "turn_off", "toggle", "trigger"})
# Services whose live handlers finish without ever suspending (they only change in-memory state), measured on Home Assistant 2026.2.3 and 2026.9.4: an automation calling one runs straight on. Every other call (a device's, which waits on I/O; `scene` / `homeassistant.*`, which call those; a script run) suspends its run until the answer comes back, so the recorded call waits too (see `RecordingServiceRegistry.async_call`).
_INSTANT_DOMAINS = frozenset(
    {"input_boolean", "input_button", "input_datetime", "input_number", "input_select", "input_text", "counter", "timer", "group", "persistent_notification", "logbook", "system_log"}
)
_INSTANT_SERVICES = frozenset({"automation.turn_on", "automation.turn_off", "automation.toggle", "automation.trigger", "script.turn_on", "notify.persistent_notification"})
# Bus events a scenario may not fire: the sandbox's Home Assistant fires these itself, and a replayed one would act on it (a state machine write, a restart, a service call).
_RESERVED_EVENT_TYPES = frozenset(
    {
        EVENT_STATE_CHANGED,
        EVENT_STATE_REPORTED,
        EVENT_CALL_SERVICE,
        EVENT_SERVICE_REGISTERED,
        EVENT_SERVICE_REMOVED,
        EVENT_COMPONENT_LOADED,
        EVENT_CORE_CONFIG_UPDATE,
        EVENT_HOMEASSISTANT_START,
        EVENT_HOMEASSISTANT_STARTED,
        EVENT_HOMEASSISTANT_STOP,
        EVENT_HOMEASSISTANT_FINAL_WRITE,
        EVENT_HOMEASSISTANT_CLOSE,
    }
)
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
class _BusEvent:
    at: datetime
    event_type: str
    data: dict[str, Any]


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
    # Loop time of the last run it started, and how many it started at that instant.
    instant: float | None = None
    instant_runs: int = 0
    # Turned off for re-triggering itself without end; its own recorded `on` no longer turns it back on.
    runaway: bool = False


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
    time_limit: float = DEFAULT_TIME_LIMIT
    bus_events: list[_BusEvent] = field(default_factory=list)
    # Every calendar the sandbox serves (see `_calendars`): entity_id -> its events, sorted by start.
    calendars: dict[str, list[calendar_events.Event]] = field(default_factory=dict)
    time_zone: Any = UTC
    # The Trend / Derivative helpers set up before the automations (see `_helpers`).
    helpers: list[scenario_helpers.Helper] = field(default_factory=list)
    # Data for integration-specific device triggers, by domain and device id (see `device_triggers.py`).
    device_triggers: dict[str, dict[str, Any]] = field(default_factory=dict)


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
    # In `TMPDIR`, which the runner points at its scratch dir: removed below, and with that dir if the child is killed before this cleanup runs.
    config_dir = tempfile.mkdtemp(prefix="event_simulator_")
    try:
        _write_registry_storage(config_dir, parsed.registry, parsed.start - BOOT_LEAD)
        _write_own_last_state(config_dir, parsed)
        with patch_homeassistant_clock(clock):
            replay = _Replay(parsed, clock, config_dir)
            task = clock.loop.create_task(replay.run())
            with _Watchdog(clock.loop, parsed.time_limit, replay.time_limit_reached, replay.hard_stopping):
                while True:
                    try:
                        return clock.loop.run_until_complete(task)
                    except asyncio.CancelledError:
                        # The hard stop landed in the loop's own code rather than in a run: carry on, the stop is queued.
                        if task.done():
                            return error_result("time_limit", f"The replay did not finish within its time limit of {parsed.time_limit:g} s", replay.warnings)
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
    bus_events = _parse_bus_events(raw.get("bus_events"), start, end, warnings)

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

    time_limit = DEFAULT_TIME_LIMIT
    if (limit_raw := raw.get("time_limit_seconds")) is not None:
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, (int, float)) or limit_raw <= 0:
            raise ScenarioError("invalid_scenario", "time_limit_seconds must be a positive number")
        time_limit = float(limit_raw)

    registry = _parse_registry(raw.get("registry"), warnings)
    automations = _automations(items, multi, registry, warnings)
    time_zone = _time_zone(core.get("time_zone"))
    calendars = _calendars(raw.get("calendars"), items, initial_states, events, time_zone, warnings)
    helpers = _helpers(raw.get("helpers"), initial_states, events, warnings)
    try:
        device_triggers = scenario_device_triggers.parse_device_triggers(raw.get("device_triggers"))
    except ValueError as err:
        raise ScenarioError("invalid_scenario", str(err)) from err
    for automation in automations:
        automation.own_initial = initial_states.pop(automation.entity_id, None)

    return _Scenario(
        core, start, end, automations, initial_states, events, services, overrun, seed, preview_features, registry, warnings, multi, traces, time_limit, bus_events, calendars, time_zone, helpers, device_triggers
    )


def _helpers(value: Any, initial_states: dict[str, _InitialState], events: list[_Event], warnings: list[dict[str, Any]]) -> list[scenario_helpers.Helper]:
    """The scenario's `helpers` (see `helpers.py`), each one's own recorded states taken off the timeline.

    A helper computes its own state, so a recorded one of its entity (the helper exists live too) is not played: the one at the window start becomes its restored last state, as after a restart, and the rest are dropped with a `helper_replaces_recorded` warning. A helper whose source has no state on the timeline gets a `helper_source_missing` warning: it never changes.
    """
    try:
        helpers = scenario_helpers.parse_helpers(value, valid_entity_id)
    except ValueError as err:
        raise ScenarioError("invalid_scenario", str(err)) from err
    timeline = set(initial_states) | {event.entity_id for event in events}
    for helper in helpers:
        if (initial := initial_states.pop(helper.entity_id, None)) is not None:
            helper.recorded = {"state": initial.state, "attributes": initial.attributes, "last_changed": initial.last_changed, "last_updated": initial.last_updated}
        dropped = [event for event in events if event.entity_id == helper.entity_id]
        if dropped:
            events[:] = [event for event in events if event.entity_id != helper.entity_id]
            count = len(dropped)
            warnings.append(
                _warning(
                    "helper_replaces_recorded",
                    f"{helper.entity_id}: the {helper.domain} helper computes it from {helper.source}, so its {count} recorded state{'' if count == 1 else 's'} in the window {'was' if count == 1 else 'were'} not played",
                )
            )
        if helper.source not in timeline and helper.source not in {other.entity_id for other in helpers}:
            warnings.append(_warning("helper_source_missing", f"{helper.entity_id}: its source {helper.source} has no state in the scenario, so the {helper.domain} helper never changes"))
    return helpers


def _time_zone(name: Any) -> Any:
    """The scenario's time zone (UTC when it has none, or one Home Assistant will refuse anyway)."""
    try:
        return ZoneInfo(name) if isinstance(name, str) and name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _calendars(
    value: Any, items: list[Any], initial_states: dict[str, _InitialState], events: list[_Event], tz: Any, warnings: list[dict[str, Any]]
) -> dict[str, list[calendar_events.Event]]:
    """The calendars the sandbox serves to calendar triggers: those of the scenario's `calendars`, every calendar a calendar trigger names, and every calendar entity on the timeline.

    A calendar not in `calendars` serves the events its recorded states name (`calendars.events_from_states`), a lower bound. A calendar trigger on a calendar served that way gets a `calendar_from_states` warning, and one on a calendar with no events at all a `calendar_no_events` warning: it attaches and never fires.
    """
    try:
        given = calendar_events.parse_calendars(value, tz)
    except ValueError as err:
        raise ScenarioError("invalid_scenario", str(err)) from err
    named = _calendar_trigger_entities(items)
    attributes: dict[str, list[dict[str, Any]]] = {}
    for entity_id, item in initial_states.items():
        if entity_id.startswith("calendar."):
            attributes.setdefault(entity_id, []).append(item.attributes)
    for event in events:
        if event.entity_id.startswith("calendar."):
            attributes.setdefault(event.entity_id, []).append(event.attributes)
    served = dict(given)
    for entity_id in sorted(set(named) | set(attributes)):
        if entity_id in served:
            continue
        served[entity_id] = calendar_events.events_from_states(attributes.get(entity_id, ()), tz)
        if entity_id not in named:
            continue
        if served[entity_id]:
            count = len(served[entity_id])
            warnings.append(
                _warning(
                    "calendar_from_states",
                    f"{entity_id}: no events given, so its {count} event{'' if count == 1 else 's'} come from its recorded states (the event each one names): "
                    "events at the same time as another, or that it never showed as the next one, are missing",
                )
            )
        else:
            warnings.append(_warning("calendar_no_events", f"{entity_id}: no events given and none in its recorded states, so its calendar trigger never fires"))
    return served


def _calendar_trigger_entities(items: list[Any]) -> list[str]:
    """The calendars the automations' calendar triggers name (`trigger: calendar`, and the target of `calendar.event_started` / `event_ended`)."""
    found: list[str] = []
    for config in items:
        for _, node in _walk(config, ""):
            platform = node.get("trigger", node.get("platform"))
            if not isinstance(platform, str) or "condition" in node:
                continue
            if platform == "calendar":
                named = node.get("entity_id")
            elif platform.startswith("calendar."):
                named = (node.get("target") or {}).get("entity_id") if isinstance(node.get("target"), dict) else None
            else:
                continue
            for entity_id in named if isinstance(named, list) else [named]:
                if isinstance(entity_id, str) and entity_id.startswith("calendar.") and valid_entity_id(entity_id) and entity_id not in found:
                    found.append(entity_id)
    return found


def _parse_bus_events(value: Any, start: datetime, end: datetime, warnings: list[dict[str, Any]]) -> list[_BusEvent]:
    """The scenario's `bus_events`, time-ordered (same-instant ones in list order); those outside the window are skipped with a warning."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ScenarioError("invalid_scenario", "bus_events must be a list")
    found: list[_BusEvent] = []
    for index, item in enumerate(value):
        what = f"bus_events[{index}]"
        if not isinstance(item, dict):
            raise ScenarioError("invalid_scenario", f"{what} must be an object")
        event_type = item.get("event_type")
        if not isinstance(event_type, str) or not event_type or len(event_type) > MAX_LENGTH_EVENT_EVENT_TYPE:
            raise ScenarioError("invalid_scenario", f"{what}.event_type must be a non-empty string of at most {MAX_LENGTH_EVENT_EVENT_TYPE} characters")
        if event_type in _RESERVED_EVENT_TYPES:
            raise ScenarioError("invalid_scenario", f"{what}: {event_type} is fired by Home Assistant itself and cannot be replayed")
        data = item.get("data") or {}
        if not isinstance(data, dict):
            raise ScenarioError("invalid_scenario", f"{what}.data must be an object")
        event = _BusEvent(_parse_time(item.get("at"), f"{what}.at"), event_type, data)
        if not start <= event.at < end:
            warnings.append(_warning("event_outside_window", f"{what} ({event_type} at {event.at.isoformat()}) is outside the window and was not fired", event.at))
            continue
        found.append(event)
    found.sort(key=lambda event: event.at)
    return found


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
    """Store each automation's recorded on/off state at the window start as its last state, so it boots enabled or disabled as live HA restores it after a restart (`last_triggered` included); likewise each helper's recorded state (a derivative's value with its sensor data, as the sensor restores it)."""
    states = []
    extra: dict[str, Any] = {}
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
    for helper in scenario.helpers:
        recorded = helper.recorded
        if recorded is None or recorded["state"] in ("unknown", "unavailable"):
            continue
        last_changed = recorded["last_changed"] or scenario.start
        states.append(
            {
                "entity_id": helper.entity_id,
                "state": recorded["state"],
                "attributes": recorded["attributes"],
                "last_changed": last_changed.isoformat(),
                "last_updated": max(recorded["last_updated"] or last_changed, last_changed).isoformat(),
            }
        )
        if helper.domain == "derivative":
            extra[helper.entity_id] = {
                "native_value": {"__type": "<class 'decimal.Decimal'>", "decimal_str": recorded["state"]},
                "native_unit_of_measurement": recorded["attributes"].get("unit_of_measurement"),
            }
    if not states:
        return
    storage = os.path.join(config_dir, ".storage")
    os.makedirs(storage, exist_ok=True)
    with open(os.path.join(storage, restore_state.STORAGE_KEY), "w", encoding="utf-8") as file:
        json.dump(
            {
                "version": restore_state.STORAGE_VERSION,
                "key": restore_state.STORAGE_KEY,
                "data": [{"state": state, "extra_data": extra.get(state["entity_id"]), "last_seen": scenario.start.isoformat()} for state in states],
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


async def _check_platforms(hass: HomeAssistant, config: dict[str, Any], device_triggers: dict[str, dict[str, Any]]) -> None:
    """Refuse, by name and config path, triggers/conditions/device automations the sandbox cannot run.

    - Device triggers, conditions and actions of integration-specific platforms (zha, mqtt, hue, ...): they read that integration's runtime state, which the sandbox does not have. Those of entity-domain integrations (binary_sensor, light, sensor, ...) work from the registry snapshot and are allowed. So is a device trigger of an integration `device_triggers.SEEDERS` covers when the scenario's `device_triggers` carries its device: the seeded data stands in for the runtime state.
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
            elif integration.integration_type == _GENERIC_DEVICE_AUTOMATION_TYPE:
                pass
            elif kind == "trigger" and domain in scenario_device_triggers.SEEDERS:
                if node["device_id"] not in device_triggers.get(domain, {}):
                    problems.append(f"{path}: '{domain}' had no trigger data for device {node['device_id']} (the device is gone, or {domain} is not running on the live instance)")
            elif kind == "trigger":
                problems.append(f"{path}: device triggers from '{domain}' are not supported yet; only {', '.join(sorted(scenario_device_triggers.SEEDERS))} and entity domains (binary_sensor, light, sensor, switch, ...) replay")
            else:
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


async def _skip_dump() -> None:
    """Stands in for `RestoreStateData.async_dump_states` in the sandbox: its config dir is thrown away."""


def _warning(code: str, message: str, at: datetime | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "at": None if at is None else at.isoformat()}


# --- the replay ---------------------------------------------------------------------------------


class _Overtime(asyncio.CancelledError):
    """Raised by the watchdog's hard stop into code that keeps the loop from running: to Home Assistant it is a cancellation of that run."""


class _Watchdog:
    """Stops a replay that is still running after `limit` real seconds.

    At the limit it queues `on_limit` on the loop, which ends the replay gracefully (see `_Replay.time_limit_reached`). That only runs once the loop gets control back; a run stuck in code that never yields (Home Assistant 2026.2 builds a schema per iteration of a repeat count, without awaiting) would keep it waiting until the runner kills the child. So, in the main thread, a `SIGALRM` every `_HARD_STOP_GRACE` seconds after the limit raises `_Overtime` into whatever is running until `on_limit` has run (calling `on_hard_stop` first, in the interrupted code's context, so it can tell whose run that is): inside a run that cancels the run, and the loop then takes the queued stop.

    Both timers (`ITIMER_REAL`, `threading.Timer`) run on real time, which the virtual clock leaves alone. Off the main thread (signals are main-thread only) only the graceful stop is armed.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, limit: float, on_limit: Callable[[], None], on_hard_stop: Callable[[], None]) -> None:
        self._loop = loop
        self._limit = limit
        self._on_limit = on_limit
        self._on_hard_stop = on_hard_stop
        self._answered = False
        self._queued = False
        self._timer: threading.Timer | None = None
        self._previous: Any = None
        self._signals = threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer")

    def __enter__(self) -> _Watchdog:
        if self._signals:
            self._previous = signal.signal(signal.SIGALRM, self._alarm)
            signal.setitimer(signal.ITIMER_REAL, self._limit)
        else:
            self._timer = threading.Timer(self._limit, self._queue_stop)
            self._timer.daemon = True
            self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._signals:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._previous)
        elif self._timer is not None:
            self._timer.cancel()

    def _queue_stop(self) -> None:
        self._loop.call_soon_threadsafe(self._stop)

    def _stop(self) -> None:
        self._answered = True
        self._on_limit()

    def _alarm(self, _signum: int, _frame: Any) -> None:
        if self._answered:
            return
        signal.setitimer(signal.ITIMER_REAL, _HARD_STOP_GRACE)
        if not self._queued:
            self._queued = True
            self._queue_stop()
            return
        self._on_hard_stop()
        raise _Overtime("Stopped by the replay's time limit")


class _StubCalendars:
    """What the calendar trigger reads from the calendar integration's entity component: the calendar entity of an entity id (see `_Replay._serve_calendars`)."""

    def __init__(self, entities: dict[str, Any]) -> None:
        self._entities = entities

    def get_entity(self, entity_id: str) -> Any:
        return self._entities.get(entity_id)


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
        # Resolved at `end`, or early when the time limit stops the replay.
        self._window_end: asyncio.Future[None] = clock.loop.create_future()
        self._halted = False
        self._event_handles: list[asyncio.TimerHandle] = []
        # Automations whose run the watchdog's hard stop cancelled.
        self._hard_stopped: set[str] = set()
        # Each helper's state changes in the window (`helper_states`), and how many past the cap were left out.
        self.helper_states: dict[str, list[dict[str, Any]]] = {}
        self._helper_states_dropped: dict[str, int] = {}

    def now(self) -> datetime:
        return self.clock.utcnow()

    async def run(self) -> dict[str, Any]:
        original_store = automation_trace.async_store_trace

        def store_trace(hass: Any, trace: Any, stored_traces: int) -> None:
            self.runs.append((trace.key, trace))
            if self.scenario.traces != "full":
                self._trim_when_finished(trace, len(self.runs) - 1)
            original_store(hass, trace, stored_traces)
            self._count_run(trace.key)

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
                ha_logger.addHandler(collector)
                try:
                    # Helpers first, so the seeded states reach them as they reached the live ones: a trend's latest sample is its source's state at the start.
                    await self._setup_helpers()
                    self._seed_states()
                    await self._settle_helpers()
                    self._serve_calendars()
                    scenario_device_triggers.seed(self.hass, self.scenario.device_triggers)
                    await self._load_automations()
                except ScenarioError as err:
                    return error_result(err.code, str(err), self.warnings)
                if self.clock.loop.time() != self.clock.loop_time_for(self.scenario.start):
                    # Setup waited on a timer: the automation attached late, so the window's first moments were not observed.
                    what = "The automations" if self.scenario.multi else "The automation"
                    self.warnings.append(_warning("late_attach", f"{what} attached at {self.now().isoformat()}, after the window start", self.now()))
                self._schedule_events()
                self.clock.call_at_datetime(self.scenario.end, self._end_window)
                await self._window_end
                for automation in self._loaded():
                    # Stopped by the time limit, runs in flight stop too: there is no time left to let them finish.
                    await automation.entity.async_turn_off(stop_actions=self._halted)
                if not self._halted:
                    await self._drain()
                return self._result(collector)
            finally:
                ha_logger.removeHandler(collector)
                await self._stop()
        finally:
            automation_trace.async_store_trace = original_store

    def _end_window(self) -> None:
        if not self._window_end.done():
            self._window_end.set_result(None)

    def _count_run(self, key: str) -> None:
        """Count a run that just started against its automation's runs at this virtual instant; past `MAX_RUNS_PER_INSTANT`, it is re-triggering itself without end."""
        automation = self._by_key.get(key)
        if automation is None or automation.runaway or automation.entity is None:
            # A run started while setup was still going (see `_load_automations`) is not counted: there is no entity to turn off yet.
            return
        instant = self.clock.loop.time()
        if automation.instant != instant:
            automation.instant, automation.instant_runs = instant, 0
        automation.instant_runs += 1
        if automation.instant_runs >= MAX_RUNS_PER_INSTANT:
            self._stop_runaway(automation)

    def _stop_runaway(self, automation: _Automation) -> None:
        """Turn off an automation that keeps starting runs at one instant (stopping its runs in flight), and say so. The rest of the replay goes on."""
        automation.runaway = True
        message = (
            f"{automation.entity_id} started {automation.instant_runs} runs at {self.now().isoformat()} without virtual time moving on: it keeps "
            "triggering itself (an event it fires or a service it calls is one of its own triggers), which live Home Assistant would also "
            "repeat without end. It was turned off there; its runs up to then are kept"
        )
        self._warn(automation, "runaway_loop", message, self.now())
        automation.error = {"code": "runaway_loop", "message": message}
        self.hass.async_create_task(automation.entity.async_turn_off())

    def hard_stopping(self) -> None:
        """The watchdog is about to cancel the code that keeps the loop from running; note whose run that is (called in that code's context).

        Home Assistant records a run only as cancelled when it stopped it itself, so the run's trace is marked cancelled here: a cancellation raised from inside a step would otherwise leave the run looking finished.
        """
        trace_id = trace_id_get()
        if trace_id:
            self._hard_stopped.add(trace_id[0])
            script_execution_set("cancelled")

    def time_limit_reached(self) -> None:
        """The wall-clock time limit is up: end the window here, with the runs so far, and name the automations still running."""
        if self._halted:
            return
        self._halted = True
        for handle in self._event_handles:
            handle.cancel()
        limit = self.scenario.time_limit
        rest = "the rest of the window was not replayed" if self.now() < self.scenario.end else "runs still in flight after the window end were not waited for"
        message = f"The replay was stopped after its time limit of {limit:g} s of real time, at {self.now().isoformat()}; {rest}"
        running = [
            automation
            for automation in self._loaded()
            if automation.entity.action_script.is_running or f"automation.{automation.id}" in self._hard_stopped
        ]
        if not running:
            self.warnings.append(_warning("time_limit", message, self.now()))
        for automation in running:
            text = f"{message}. {automation.entity_id} was still running"
            self._warn(automation, "time_limit", text, self.now())
            automation.error = automation.error or {"code": "time_limit", "message": text}
        self._end_window()

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
        # Restore state is saved every 15 minutes (virtual), for a restart that never comes: thousands of file writes over a month, each an executor round trip.
        restore_state.async_get(hass).async_dump_states = _skip_dump

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
                # Written twice so last_changed and last_updated both match the record: the state at last_changed, then an attribute update at last_updated. Both carry the recorded attributes, so a helper reading one (a trend's `attribute`) finds it in either.
                states.async_set(entity_id, item.state, {**item.attributes, _SEED_MARKER: True}, timestamp=last_changed.timestamp())
            states.async_set(entity_id, item.state, item.attributes, timestamp=last_updated.timestamp())

    async def _setup_helpers(self) -> None:
        """Set up the scenario's helpers (`_Scenario.helpers`) as config entries of Home Assistant's own Trend and Derivative integrations, each with its options and its requested entity id.

        A config entry is what the helper's config flow creates live, so the helper runs the same code (`async_setup_entry`) on the same options. Its entity id is reserved in the entity registry first (unique id = the entry id, as the helper registers it), replacing a registry entry the snapshot holds for that entity id. Both integrations compute from state timestamps and `utcnow` (a trend's `sample_duration`) or loop timers (a derivative's `max_sub_interval`), all of which run on virtual time.
        """
        if not self.scenario.helpers:
            return
        hass = self.hass
        registry = entity_registry.async_get(hass)
        for helper in self.scenario.helpers:
            what = f"helpers[{helper.index}] ({helper.domain} {helper.entity_id})"
            try:
                integration = await async_get_integration(hass, helper.domain)
                await integration.async_get_platform("config_flow")
            except Exception as err:
                raise ScenarioError("helper_failed", f"{what}: the {helper.domain} integration could not be loaded: {err}") from err
            handler = config_entries.HANDLERS[helper.domain]
            entry = config_entries.ConfigEntry(
                data={},
                discovery_keys=MappingProxyType({}),
                domain=helper.domain,
                entry_id=f"event_simulator_helper_{helper.index}",
                minor_version=handler.MINOR_VERSION,
                options=helper.options,
                source=config_entries.SOURCE_USER,
                subentries_data=None,
                title=helper.title,
                unique_id=None,
                version=handler.VERSION,
            )
            if registry.async_get(helper.entity_id) is not None:
                registry.async_remove(helper.entity_id)
            hass.states.async_remove(helper.entity_id)
            registry.async_get_or_create(helper.entity_domain, helper.domain, entry.entry_id, suggested_object_id=helper.entity_id.split(".", 1)[1])
            await hass.config_entries.async_add(entry)
            if entry.state is not config_entries.ConfigEntryState.LOADED or hass.states.get(helper.entity_id) is None:
                reason = entry.reason or "see the log warnings"
                raise ScenarioError("helper_failed", f"{what} did not set up ({entry.state.value}): {reason}")

    async def _settle_helpers(self) -> None:
        """Let the helpers take in the seeded states, then record each one's state from here on (`helper_states`)."""
        if not self.scenario.helpers:
            return
        # Nothing runs yet but the helpers' own updates (a trend's gradient is computed in the executor, which takes no virtual time).
        await self.hass.async_block_till_done()
        entity_ids = [helper.entity_id for helper in self.scenario.helpers]
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            self.helper_states[entity_id] = [{"at": self.now(), "state": state.state if state is not None else None}]

        def changed(event: Any) -> None:
            new_state = event.data["new_state"]
            if new_state is None or self.now() >= self.scenario.end:
                return
            entity_id = event.data["entity_id"]
            seen = self.helper_states[entity_id]
            if seen[-1]["state"] == new_state.state:
                return
            if len(seen) >= scenario_helpers.MAX_HELPER_STATES:
                self._helper_states_dropped[entity_id] = self._helper_states_dropped.get(entity_id, 0) + 1
                return
            seen.append({"at": self.now(), "state": new_state.state})

        async_track_state_change_event(self.hass, entity_ids, changed)

    def _serve_calendars(self) -> None:
        """Stand in for the calendar integration: each calendar the scenario serves (`_Scenario.calendars`) answers calendar triggers with its events.

        Home Assistant's calendar trigger looks its calendar up in the calendar integration's entity component and asks it for the events of each 15-minute span ahead. That lookup gets a stub calendar entity per calendar, never added to the state machine, so the calendar's recorded states still replay as recorded and nothing the stub does writes one. The calendar integration itself (its HTTP views, frontend panel and services) is not set up.
        """
        if not self.scenario.calendars:
            return
        from homeassistant.components.calendar import CalendarEntity, CalendarEvent  # noqa: PLC0415 - only when there is a calendar to serve
        from homeassistant.components.calendar.const import DATA_COMPONENT as CALENDAR_COMPONENT  # noqa: PLC0415

        tz = self.scenario.time_zone

        class _StubCalendar(CalendarEntity):
            def __init__(self, entity_id: str, events: list[calendar_events.Event]) -> None:
                self.entity_id = entity_id
                self._events = events
                self._ha_events = [
                    CalendarEvent(event.start, event.end, event.summary, description=event.description, location=event.location) for event in events
                ]

            @property
            def event(self) -> Any:
                return None

            async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[Any]:
                return [self._ha_events[n] for n in calendar_events.active(self._events, start_date, end_date, tz)]

        stubs = {}
        for entity_id, events in self.scenario.calendars.items():
            try:
                stubs[entity_id] = _StubCalendar(entity_id, events)
            except HomeAssistantError as err:
                raise ScenarioError("invalid_scenario", f"calendars[{entity_id!r}]: {err}") from err
        self.hass.data[CALENDAR_COMPONENT] = _StubCalendars(stubs)

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
        # Record calls from here on, not once setup returns: a trigger can fire as it attaches (a calendar event at the window start) while setup still awaits real work, such as loading translations, that took longer than the attach (#45).
        for automation in self.scenario.automations:
            if automation.error is None:
                self._by_key[f"automation.{automation.id}"] = automation
        self.hass.services.intercepting = True
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
                self._by_key.pop(f"automation.{automation.id}", None)
                continue
            automation.entity = entity
            automation.entity_id = entity.entity_id
            self._by_entity_id[entity.entity_id] = automation

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

        await _check_platforms(self.hass, config, self.scenario.device_triggers)
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
        by_instant: dict[datetime, tuple[list[_Event], list[_BusEvent]]] = {}
        for event in self.scenario.events:
            by_instant.setdefault(event.at, ([], []))[0].append(event)
        for bus_event in self.scenario.bus_events:
            by_instant.setdefault(bus_event.at, ([], []))[1].append(bus_event)
        for at, (events, bus_events) in sorted(by_instant.items()):
            # One callback per instant: same-instant timers have no guaranteed order, list order does.
            self._event_handles.append(self.clock.call_at_datetime(at, self._play, events, bus_events))

    def _play(self, events: list[_Event], bus_events: list[_BusEvent]) -> None:
        self._write_states(events)
        for bus_event in bus_events:
            self.hass.bus.async_fire(bus_event.event_type, dict(bus_event.data), time_fired=bus_event.at.timestamp())

    def _write_states(self, events: list[_Event]) -> None:
        for event in events:
            if (automation := self._by_entity_id.get(event.entity_id)) is not None:
                if not automation.runaway:
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
        while any(script.is_running for script in scripts) and self.clock.loop.time() < bound and not self._halted:
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
        for entity_id, count in self._helper_states_dropped.items():
            self.warnings.append(
                _warning("helper_states_truncated", f"{entity_id}: only its first {scenario_helpers.MAX_HELPER_STATES} state changes are in helper_states; {count} more were left out")
            )
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
            "helper_states": self.helper_states,
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
    if traces == "none":
        return _bare_run_record(trace, short)
    execution = short.get("script_execution")
    error = short.get("error")
    extended: dict[str, Any] | None = None
    if traces == "full":
        extended = trace.as_extended_dict()
        steps: dict[str, list[dict[str, Any]]] = extended.get("trace") or {}
    else:
        steps = _trigger_and_condition_steps(trace)

    outcome, stopped_by = _outcome(short)
    if outcome == "not_triggered":
        stopped_by = _not_triggered_reason(steps)
    elif outcome == "condition_failed":
        stopped_by = _failed_condition(steps)

    timestamps = short.get("timestamp") or {}
    finished = timestamps.get("finish") if outcome != "unfinished" else None
    trigger = _trigger_summary(steps, short.get("trigger"))
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


def _bare_run_record(trace: Any, short: dict[str, Any]) -> dict[str, Any]:
    """A run under `traces: none`: when, how it ended and which trigger, read straight off the trace without serializing any step (a month of a chatty trigger is hundreds of thousands of these)."""
    outcome, _ = _outcome(short)
    timestamps = short.get("timestamp") or {}
    trigger = _first_trigger_variables(trace)
    return {
        "id": trace.run_id,
        "started": timestamps.get("start"),
        "finished": timestamps.get("finish") if outcome != "unfinished" else None,
        "outcome": outcome,
        "trigger": {"idx": trigger.get("idx"), "id": trigger.get("id")},
    }


def _first_trigger_variables(trace: Any) -> dict[str, Any]:
    """The `trigger` variable the run started with (from its trigger step), as `_trigger_summary` reads it."""
    elements = getattr(trace, "_trace", None)
    if not isinstance(elements, dict):
        return _trigger_variables(_trigger_and_condition_steps(trace))
    for path, steps in elements.items():
        if path == "trigger" or path.startswith("trigger/"):
            if not steps:
                return {}
            variables = getattr(steps[0], "_variables", None)
            if not isinstance(variables, dict):
                variables = steps[0].as_dict().get("changed_variables") or {}
            return variables.get("trigger") or {}
    return {}


def _outcome(short: dict[str, Any]) -> tuple[str, str | None]:
    """The run's outcome and what stopped it, from the trace's short dict (the step that stopped a `not_triggered` or `condition_failed` run is left to the caller, which has the steps)."""
    execution = short.get("script_execution")
    error = short.get("error")
    last_step = short.get("last_step")
    stopped_by: str | None = None
    if short.get("state") == "running":
        outcome = "unfinished"
    elif execution == "not_triggered":
        # HA 2026.9+: a trigger evaluated a change but did not fire; its step's result says why.
        outcome = "not_triggered"
    elif execution == "failed_conditions":
        outcome = "condition_failed"
    elif execution in ("failed_single", "failed_max_runs", "disallowed_recursion_detected"):
        outcome = "dropped"
        stopped_by = {"failed_single": "mode_single", "failed_max_runs": "max_runs"}.get(execution, "recursion")
    elif error is not None or execution == "error":
        outcome = "error"
        stopped_by = last_step
    elif execution == "aborted":
        outcome = "aborted"
        stopped_by = last_step
    elif execution == "cancelled" or (execution is None and error is None):
        # A run stopped while it waits on a service call (`restart`, turned off) ends with no `script_execution` at all; one stopped between steps or in a delay says `cancelled`.
        outcome = "cancelled"
        stopped_by = last_step
    elif execution == "finished":
        outcome = "fired"
    elif isinstance(execution, str) and execution:
        # A value a later release adds: passed through rather than reported as an error.
        outcome = execution
        stopped_by = last_step
    else:
        outcome = "error"
        stopped_by = last_step
    return outcome, stopped_by


def _trigger_and_condition_steps(trace: Any) -> dict[str, list[dict[str, Any]]]:
    """The trace's trigger and condition steps only, as `as_extended_dict()` would give them, without serializing the action steps."""
    elements = getattr(trace, "_trace", None)
    if isinstance(elements, dict):
        return {path: [step.as_dict() for step in steps] for path, steps in elements.items() if _is_trigger_or_condition(path)}
    # Not the ActionTrace layout this was written against: take the long way.
    return {path: steps for path, steps in (trace.as_extended_dict().get("trace") or {}).items() if _is_trigger_or_condition(path)}


def _is_trigger_or_condition(path: str) -> bool:
    return path == "trigger" or path.startswith(("trigger/", "condition/"))


def _not_triggered_reason(steps: dict[str, list[dict[str, Any]]]) -> str | None:
    """The reason a not-triggered trace's trigger step gives (`entity_value_not_numeric`, ...)."""
    for path, elements in steps.items():
        if (path == "trigger" or path.startswith("trigger/")) and elements:
            return (elements[0].get("result") or {}).get("reason")
    return None


def _failed_condition(steps: dict[str, list[dict[str, Any]]]) -> str | None:
    """Trace path of the top-level condition that evaluated false or raised (HA counts an error as false), first one in evaluation order."""
    fallback = None
    for path, elements in steps.items():
        if not path.startswith("condition/"):
            continue
        for element in elements:
            if (element.get("result") or {}).get("result") is False or "error" in element:
                if path.count("/") == 1:
                    return path
                fallback = fallback or path
    return fallback


def _trigger_summary(steps: dict[str, list[dict[str, Any]]], description: str | None) -> dict[str, Any]:
    """The firing trigger's essentials; the full trigger variables stay in the trace."""
    trigger = _trigger_variables(steps)
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


def _trigger_variables(steps: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The `trigger` variable of the first trigger step, from serialized steps."""
    for path, elements in steps.items():
        if path == "trigger" or path.startswith("trigger/"):
            return ((elements[0].get("changed_variables") or {}).get("trigger") or {}) if elements else {}
    return {}


def _json_safe(value: Any) -> Any:
    """Round-trip through HA's trace encoder so the result is plain JSON types."""
    return json.loads(json.dumps(value, cls=ExtendedJSONEncoder))


# --- service interception -------------------------------------------------------------------------


class RecordingServiceRegistry(ServiceRegistry):
    """Service registry that records calls instead of executing them.

    Until `intercepting` is set (just before the automations are set up) it behaves like the stock registry, so boot is unchanged. After that every service exists, every call is recorded and answered with success (an empty dict when a response is requested), and `call_service` is still fired on the bus so event triggers on it behave.

    A blocking call, after firing `call_service`, waits as a live call to a device does for its I/O round trip: until nothing else is left to run at this virtual instant (`VirtualTimeEventLoop.when_idle`; no virtual time passes). Services whose live handler never suspends (`_INSTANT_DOMAINS` / `_INSTANT_SERVICES`) answer at once. The calling run is thus still in flight while everything already started at this instant runs: a run its own `event` step or `call_service` re-triggered, or the next of several changes recorded at one instant, meets it running, so `mode: single` drops that run (`failed_single`), `restart` stops the caller and `max` runs fill up, as they do live.
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
        if blocking and domain not in _INSTANT_DOMAINS and f"{domain}.{service}" not in _INSTANT_SERVICES:
            when_idle = getattr(self._hass.loop, "when_idle", None)
            await (when_idle() if when_idle is not None else asyncio.sleep(0))
        return {} if return_response else None
