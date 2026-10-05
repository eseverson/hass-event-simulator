"""Replay engine: runs one scenario against one automation in a throwaway Home Assistant.

Runs in the child process (see `__main__.py`). `run_scenario(scenario)` is the whole contract: a scenario dict in, a JSON-safe result dict out.

Timeline of a replay (all virtual time, see `vclock.py`)
--------------------------------------------------------
1. `start - BOOT_LEAD`: a `HomeAssistant` boots on the virtual loop with a temporary config dir (deleted afterwards). It loads the base registries and sets up only the `homeassistant` integration, then runs `async_start`; the automation integration and whatever it pulls in (`trace`, `blueprint`, trigger/condition platforms) come later. No recorder, http, frontend or network-facing integrations, and `skip_pip` is on, so nothing is installed or fetched. `BOOT_LEAD` absorbs any timers boot waits on, so the window starts with everything settled.
2. `start`: initial states are seeded with their recorded `last_changed` / `last_updated` (`StateMachine.async_set(..., timestamp=...)`; an attribute-only update is reproduced by a second write at `last_updated`); states without them are stamped `start`, the same clamp the recorder applies. Then the automation is validated with HA's own automation schema (invalid -> structured `invalid_automation` error) and set up. It attaches after `async_start`, so a `homeassistant: start` trigger does not fire, and at `start` exactly, so a time trigger at the window start does.
3. `[start, end)`: timeline events are written with `async_set` at their `at`; events at the same instant are written in list order. Every service call is intercepted at the `ServiceRegistry` (see `RecordingServiceRegistry`): recorded, answered with success (an empty response when one is asked for), and never executed, so recorded calls change no state.
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

`outcome`: `fired` (the actions ran to the end or a `stop`), `condition_failed` (`stopped_by` = the failing condition's trace path, e.g. `condition/0`), `dropped` (the script mode refused the run; `stopped_by` = `mode_single` / `max_runs` / `recursion`), `aborted` (a condition or `stop: error` inside the actions; `stopped_by` = last action path), `cancelled` (a `restart` re-trigger or shutdown), `error` (an exception; `stopped_by` = last action path, `error` = message), `unfinished` (still running at the overrun bound).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant import bootstrap, config_entries, loader
from homeassistant.components.automation import DATA_COMPONENT as AUTOMATION_COMPONENT
from homeassistant.components.automation import trace as automation_trace
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    MAX_LENGTH_STATE_STATE,
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
from homeassistant.helpers.json import ExtendedJSONEncoder
from homeassistant.helpers.trace import trace_id_get
from homeassistant.setup import async_setup_component
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
# The id given to an automation that has none: HA only finishes traces of automations with an id.
DEFAULT_AUTOMATION_ID = "event_simulator_replay"
# Core config keys copied from the scenario into the sandbox's `homeassistant:` section.
_CORE_KEYS = ("time_zone", "latitude", "longitude", "elevation", "unit_system", "country", "language", "currency", "radius")
# Attribute that makes the first of two seeding writes differ from the second, so the second counts as an attribute update.
_SEED_MARKER = "event_simulator_seed"
# Seed for Python's `random` in the child unless the scenario sets `seed`. HA draws on it for the 50-500 ms offset of time-change listeners (time / time_pattern triggers) and for trace run ids; a fixed seed makes a replay repeatable.
DEFAULT_SEED = 0
# Cap on HA log records surfaced as warnings.
_MAX_LOG_WARNINGS = 50


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
class _Scenario:
    core: dict[str, Any]
    start: datetime
    end: datetime
    automation: dict[str, Any]
    initial_states: dict[str, _InitialState]
    events: list[_Event]
    services: dict[str, set[str]] | None
    overrun: timedelta
    seed: int
    warnings: list[dict[str, Any]] = field(default_factory=list)


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

    automation = raw.get("automation")
    if not isinstance(automation, dict):
        raise ScenarioError("invalid_scenario", "automation must be an object (the automation config)")

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

    return _Scenario(core, start, end, automation, initial_states, events, services, overrun, seed, warnings)


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
        self.traces: list[Any] = []
        self.service_calls: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = list(scenario.warnings)
        self.log_warnings: list[dict[str, Any]] = []
        self._unknown_services: set[str] = set()
        self._entity: Any = None

    def now(self) -> datetime:
        return self.clock.utcnow()

    async def run(self) -> dict[str, Any]:
        original_store = automation_trace.async_store_trace

        def store_trace(hass: Any, trace: Any, stored_traces: int) -> None:
            self.traces.append(trace)
            original_store(hass, trace, stored_traces)

        automation_trace.async_store_trace = store_trace
        collector = _LogCollector(self)
        ha_logger = logging.getLogger("homeassistant")
        try:
            self.hass = await self._boot()
            try:
                await self._sleep_until(self.scenario.start)
                self._seed_states()
                ha_logger.addHandler(collector)
                try:
                    await self._load_automation()
                except ScenarioError as err:
                    return error_result(err.code, str(err), self.warnings)
                if self.clock.loop.time() != self.clock.loop_time_for(self.scenario.start):
                    # Setup waited on a timer: the automation attached late, so the window's first moments were not observed.
                    self.warnings.append(_warning("late_attach", f"The automation attached at {self.now().isoformat()}, after the window start", self.now()))
                self._schedule_events()
                await self._sleep_until(self.scenario.end)
                await self._entity.async_turn_off(stop_actions=False)
                await self._drain()
                return self._result(collector)
            finally:
                ha_logger.removeHandler(collector)
                await self._stop()
        finally:
            automation_trace.async_store_trace = original_store

    async def _boot(self) -> HomeAssistant:
        hass = HomeAssistant(self.config_dir)
        # Replaced before anything registers a service, so every registration lands in the recording registry.
        hass.services = RecordingServiceRegistry(hass, self._record_call)
        loader.async_setup(hass)
        hass.config.skip_pip = True
        hass.config.skip_pip_packages = []
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await bootstrap.async_load_base_functionality(hass)
        try:
            await async_process_ha_core_config(hass, dict(self.scenario.core))
        except Exception as err:
            raise ScenarioError("invalid_scenario", f"core config rejected: {err}") from err
        if not await async_setup_component(hass, "homeassistant", {}):
            raise RuntimeError("The homeassistant integration failed to set up")
        await hass.async_start()
        await hass.async_block_till_done()
        return hass

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

    async def _load_automation(self) -> None:
        config = dict(self.scenario.automation)
        if not config.get("id"):
            config["id"] = DEFAULT_AUTOMATION_ID
        if config.get("initial_state") is False:
            config.pop("initial_state")
            self.warnings.append(_warning("initial_state_ignored", "initial_state: false was ignored so the automation can run"))

        try:
            await async_validate_config_item(self.hass, config["id"], config)
        except vol.Invalid as err:
            raise ScenarioError("invalid_automation", humanize_error(config, err)) from err
        except (HomeAssistantError, ValueError, KeyError, TypeError) as err:
            raise ScenarioError("invalid_automation", str(err) or type(err).__name__) from err

        if not await async_setup_component(self.hass, "automation", {"automation": [config]}):
            raise ScenarioError("invalid_automation", "The automation integration failed to set up")
        # Setup awaits the trigger attachment itself. No async_block_till_done: it would wait out any run already started and so move virtual time.
        entities = list(self.hass.data[AUTOMATION_COMPONENT].entities)
        if len(entities) != 1 or getattr(entities[0], "action_script", None) is None:
            raise ScenarioError("invalid_automation", "The automation did not load")
        self._entity = entities[0]
        self.hass.services.intercepting = True

    def _schedule_events(self) -> None:
        by_instant: dict[datetime, list[_Event]] = {}
        for event in self.scenario.events:
            by_instant.setdefault(event.at, []).append(event)
        for at, events in by_instant.items():
            # One callback per instant: same-instant timers have no guaranteed order, list order does.
            self.clock.call_at_datetime(at, self._play, events)

    def _play(self, events: list[_Event]) -> None:
        for event in events:
            self.hass.states.async_set(event.entity_id, event.state, event.attributes, timestamp=event.at.timestamp())

    async def _drain(self) -> None:
        """Let runs in flight at `end` finish, up to the overrun bound."""
        bound = self.clock.loop_time_for(self.scenario.end + self.scenario.overrun)
        script = self._entity.action_script
        # No async_block_till_done here: it would wait for the in-flight runs themselves and so carry virtual time past the bound.
        while script.is_running and self.clock.loop.time() < bound:
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
        self.service_calls.append(
            {
                "at": self.now(),
                "domain": domain,
                "service": service,
                "data": dict(data),
                "target": dict(target) if target else None,
                "run": trace_id[1] if trace_id else None,
                "context": {"id": context.id, "parent_id": context.parent_id, "user_id": context.user_id},
            }
        )
        known = self.scenario.services
        name = f"{domain}.{service}"
        if known is not None and service not in known.get(domain, ()) and name not in self._unknown_services:
            self._unknown_services.add(name)
            self.warnings.append(_warning("unknown_service", f"{name} does not exist on the live instance", self.now()))

    def _result(self, collector: _LogCollector) -> dict[str, Any]:
        start, end = self.scenario.start, self.scenario.end
        runs: list[dict[str, Any]] = []
        dropped_ids: set[str] = set()
        for trace in self.traces:
            run = _run_record(trace)
            started = run["started"]
            if started is None or not start <= started < end:
                dropped_ids.add(run["id"])
                continue
            if run["outcome"] == "unfinished":
                self.warnings.append(
                    _warning("run_unfinished", f"Run {run['id']} was still running at the overrun bound ({self.scenario.overrun} after the end)", started)
                )
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
        return _json_safe(result)


def _run_record(trace: Any) -> dict[str, Any]:
    """One run of the result from an automation trace."""
    extended = trace.as_extended_dict()
    short = trace.as_short_dict()
    execution = short.get("script_execution")
    error = short.get("error")
    steps: dict[str, list[dict[str, Any]]] = extended.get("trace") or {}
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
    return {
        "id": trace.run_id,
        "started": timestamps.get("start"),
        "finished": finished,
        "trigger": _trigger_summary(steps, short.get("trigger")),
        "outcome": outcome,
        "stopped_by": stopped_by,
        "script_execution": execution,
        "error": error,
        "trace": extended,
    }


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
