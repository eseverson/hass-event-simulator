"""WebSocket API consumed by the sidebar panel. Every command is admin-only."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from functools import partial
import logging
import threading
from typing import Any

import astral
import astral.sun
import voluptuous as vol
import yaml

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import parse_yaml

from . import history, runner
from .const import DOMAIN
from .history import (
    MAX_ENTITIES,
    MAX_EVENTS,
    MAX_SUGGEST_EVENTS,
    MAX_SUGGEST_WINDOW,
    SimulatorError,
    check_window,
)
from .packed import unpack_events
from .runner import SandboxError
from .store import (
    DATA_STORE,
    LABEL_SCHEMA,
    MAX_LABELS,
    SCENARIO_FIELDS,
    TOLERANCE_SCHEMA,
    ScenarioStore,
)
from .suggest.exact import apply_exact, fires_from_runs, mark_approximate
from .suggest.labels import Labels, Tolerance
from .suggest.search import SuggestOptions, SuggestResult, suggest
from .suggest.timeline import Timeline, to_ts

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]], Awaitable[None]]


def _error_payload(err: Exception) -> dict[str, Any]:
    """The `error` object of a failed `simulate/subscribe` event, coded as `_wrap` codes a failed command."""
    if isinstance(err, SimulatorError):
        error = {"code": err.code, "message": str(err)}
        if isinstance(err, SandboxError):
            error["stderr_tail"] = err.stderr_tail
        return error
    _LOGGER.error("event_simulator websocket command failed", exc_info=err)
    return {"code": websocket_api.ERR_UNKNOWN_ERROR, "message": f"{type(err).__name__}: {err}"}


def _wrap(func: Handler) -> Handler:
    """Send SimulatorError as its own error code, anything else as unknown_error.

    A SandboxError also carries `stderr_tail` in the error payload; the frontend's `callWS` rejects with that whole payload, so the panel reads it as `e.stderr_tail`.
    """

    async def wrapper(hass, connection, msg):
        try:
            await func(hass, connection, msg)
        except SandboxError as err:
            # The panel shows `stderr_tail` (the child's last log lines) under the message.
            error = websocket_api.error_message(msg["id"], err.code, str(err))
            error["error"]["stderr_tail"] = err.stderr_tail
            connection.send_message(error)
        except SimulatorError as err:
            connection.send_error(msg["id"], err.code, str(err))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("event_simulator websocket command failed")
            connection.send_error(
                msg["id"], websocket_api.ERR_UNKNOWN_ERROR, f"{type(err).__name__}: {err}"
            )

    wrapper.__name__ = func.__name__
    return wrapper


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_automations)
    websocket_api.async_register_command(hass, ws_automation)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_scenarios_list)
    websocket_api.async_register_command(hass, ws_scenarios_get)
    websocket_api.async_register_command(hass, ws_scenarios_save)
    websocket_api.async_register_command(hass, ws_scenarios_delete)
    websocket_api.async_register_command(hass, ws_simulate)
    websocket_api.async_register_command(hass, ws_simulate_subscribe)
    websocket_api.async_register_command(hass, ws_suggest_subscribe)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/automations"})
@websocket_api.async_response
@_wrap
async def ws_automations(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], history.async_list_automations(hass))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/automation", vol.Required("entity_id"): cv.entity_id}
)
@websocket_api.async_response
@_wrap
async def ws_automation(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], history.async_get_automation(hass, msg["entity_id"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/history",
        vol.Required("entity_ids"): [cv.entity_id],
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
    }
)
@websocket_api.async_response
@_wrap
async def ws_history(hass, connection, msg: dict[str, Any]) -> None:
    """The recorded timeline of the entities in the window, up to a Suggest window long (the panel shows Suggest mode's longer windows too); `simulate` still refuses a replay longer than `history.MAX_WINDOW`."""
    connection.send_result(
        msg["id"],
        await history.async_get_timeline(
            hass, msg["entity_ids"], msg["start"], msg["end"], max_window=MAX_SUGGEST_WINDOW
        ),
    )


def _store(hass: HomeAssistant) -> ScenarioStore:
    if (store := hass.data.get(DATA_STORE)) is None:
        raise SimulatorError("not_loaded", "Event Simulator is not set up")
    return store


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/scenarios/list"})
@websocket_api.async_response
@_wrap
async def ws_scenarios_list(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], _store(hass).summaries())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/scenarios/get", vol.Required("scenario_id"): str}
)
@websocket_api.async_response
@_wrap
async def ws_scenarios_get(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(msg["id"], _store(hass).get(msg["scenario_id"]))


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/scenarios/save",
        vol.Optional("scenario_id"): str,
        **SCENARIO_FIELDS,
    }
)
@websocket_api.async_response
@_wrap
async def ws_scenarios_save(hass, connection, msg: dict[str, Any]) -> None:
    connection.send_result(
        msg["id"], await _store(hass).async_save_scenario(msg, msg.get("scenario_id"))
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/scenarios/delete", vol.Required("scenario_id"): str}
)
@websocket_api.async_response
@_wrap
async def ws_scenarios_delete(hass, connection, msg: dict[str, Any]) -> None:
    await _store(hass).async_delete(msg["scenario_id"])
    connection.send_result(msg["id"])


# The longest automation YAML the simulate command parses, and how many nodes it may expand to (anchors and aliases count every time they are used).
MAX_YAML_CHARS = 256 * 1024
MAX_YAML_NODES = 100_000
# Tags a plain YAML document resolves to: the safe loader's own, plus the merge key.
_SAFE_TAGS = frozenset(tag for tag in yaml.SafeLoader.yaml_constructors if tag) | {
    "tag:yaml.org,2002:merge"
}
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _aware_utc_iso(value: Any) -> str:
    """A datetime as an ISO UTC string; one without an offset is taken as local time, as `history` does."""
    return dt_util.as_utc(cv.datetime(value)).isoformat()


# The sandbox's `traces` modes (sandbox/engine.py TRACE_MODES; the engine is child-only, so not imported here).
TRACE_MODES = ("full", "summary", "none")

# Only what the live side needs is checked here; the child validates the rest and answers with a structured error.
SIMULATE_SCENARIO = vol.Schema(
    {
        vol.Required("start"): _aware_utc_iso,
        vol.Required("end"): _aware_utc_iso,
        vol.Required("automation"): vol.Any(dict, str),
        vol.Optional("core"): dict,
        vol.Optional("services"): dict,
        vol.Optional("initial_states", default=dict): dict,
        # A plain list, or packed (packed.py) to fit the websocket's 4 MiB message limit.
        vol.Optional("events", default=list): vol.Any(list, unpack_events),
        # How much each run carries (the engine's TRACE_MODES): the panel needs `full`; a long validation can ask for `summary` or `none` to stay under the output cap.
        vol.Optional("traces"): vol.In(TRACE_MODES),
    },
    extra=vol.ALLOW_EXTRA,
)


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/simulate", vol.Required("scenario"): SIMULATE_SCENARIO}
)
@websocket_api.async_response
@_wrap
async def ws_simulate(hass, connection, msg: dict[str, Any]) -> None:
    """Replay a scenario in the sandbox and send its result (see `async_simulate`).

    The replay is registered as a subscription of its message id, so the connection closing (or an `unsubscribe_events` for that id) cancels it and kills the child instead of leaving it to run out its timeout.
    """
    cancel = asyncio.current_task().cancel
    connection.subscriptions[msg["id"]] = cancel
    try:
        result = await async_simulate(hass, msg["scenario"])
    finally:
        if connection.subscriptions.get(msg["id"]) is cancel:
            del connection.subscriptions[msg["id"]]
    connection.send_result(msg["id"], result)


class _Replay:
    """A `simulate/subscribe` replay in flight; calling it (unsubscribe, connection close) cancels it."""

    def __init__(self, task: asyncio.Task[None]) -> None:
        self.task = task

    def __call__(self) -> None:
        self.task.cancel()


def _supersede(connection: websocket_api.ActiveConnection, kind: type[_Replay], message: str) -> None:
    """Cancel every unfinished subscription of exactly `kind` on the connection and tell each it was `superseded` (newest wins)."""
    for sub_id, sub in list(connection.subscriptions.items()):
        # A finished one has answered already and waits for the client's unsubscribe.
        if type(sub) is kind and not sub.task.done():
            del connection.subscriptions[sub_id]
            sub()
            connection.send_message(
                websocket_api.event_message(
                    sub_id, {"error": {"code": "superseded", "message": message}}
                )
            )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/simulate/subscribe", vol.Required("scenario"): SIMULATE_SCENARIO}
)
@callback
def ws_simulate_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replay a scenario as a cancellable subscription: what the panel's auto-run uses.

    Acknowledged at once; the outcome follows as one event, `{"result": ...}` or `{"error": {code, message[, stderr_tail]}}`. The subscription stays registered (inert) until the client unsubscribes, so the client's usual unsubscribe succeeds. Unsubscribing or closing the connection kills the child. Latest wins: a newer replay on the same connection supersedes (and kills) the one in flight, which gets a `superseded` error event, so a burst of edits holds at most one of the `runner.MAX_CONCURRENT` slots per connection.
    """
    msg_id = msg["id"]
    connection.send_result(msg_id)
    _supersede(connection, _Replay, "A newer replay replaced this one")

    async def replay() -> None:
        try:
            event: dict[str, Any] = {"result": await async_simulate(hass, msg["scenario"])}
        except Exception as err:  # noqa: BLE001
            event = {"error": _error_payload(err)}
        if connection.subscriptions.get(msg_id) is handle:  # not unsubscribed or superseded meanwhile
            connection.send_message(websocket_api.event_message(msg_id, event))

    handle = _Replay(
        hass.async_create_background_task(
            replay(), f"{DOMAIN}.simulate:{msg_id}", eager_start=False
        )
    )
    connection.subscriptions[msg_id] = handle


# Most suggestions one request may ask for: they are validated together in one sandbox child, which takes at most this many automations (sandbox/engine.py MAX_AUTOMATIONS; the engine is child-only, so not imported here).
MAX_SUGGESTIONS = 25
MAX_SUGGEST_CONDITIONS = 4

# What the panel sends in `options`: the match tolerance (seconds before / after an anchor), and optionally the search's size.
SUGGEST_OPTIONS = vol.Schema(
    {
        vol.Optional("tolerance_before"): TOLERANCE_SCHEMA,
        vol.Optional("tolerance_after"): TOLERANCE_SCHEMA,
        vol.Optional("max_conditions"): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_SUGGEST_CONDITIONS)
        ),
        vol.Optional("top_k"): vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_SUGGESTIONS)),
    }
)


class _Suggesting(_Replay):
    """A `suggest/subscribe` in flight. Cancelling it also raises `stop`, which the miner polls in its executor thread (a thread cannot be cancelled from outside)."""

    def __init__(self, task: asyncio.Task[None], stop: threading.Event) -> None:
        super().__init__(task)
        self.stop = stop

    def __call__(self) -> None:
        self.stop.set()
        super().__call__()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/suggest/subscribe",
        vol.Required("entity_ids"): vol.All([cv.entity_id], vol.Length(min=1, max=MAX_ENTITIES)),
        vol.Required("start"): cv.datetime,
        vol.Required("end"): cv.datetime,
        vol.Required("labels"): vol.All([LABEL_SCHEMA], vol.Length(max=MAX_LABELS)),
        vol.Optional("options", default=dict): SUGGEST_OPTIONS,
    }
)
@callback
def ws_suggest_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Suggest automations from labeled history, as a cancellable subscription (see `async_suggest`).

    Acknowledged at once; then progress events `{stage: "loading" | "mining" | "validating", ...counts}`, and one final event: the result `{suggestions, hints, evaluated[, warning]}` or `{error: {code, message}}`. Newest wins per connection, as with `simulate/subscribe`: a newer suggest supersedes the one in flight (`superseded` error event), and unsubscribing or closing the connection stops the miner at its next check and kills the validation child.

    Every field but `options` is required. The panel does not probe for the command (an empty request is logged as an error on every panel load); a backend without it answers `unknown_command` to the first real request.
    """
    msg_id = msg["id"]
    connection.send_result(msg_id)
    _supersede(connection, _Suggesting, "A newer suggestion request replaced this one")
    stop = threading.Event()

    def send(payload: dict[str, Any]) -> None:
        if connection.subscriptions.get(msg_id) is handle:  # not unsubscribed or superseded meanwhile
            connection.send_message(websocket_api.event_message(msg_id, payload))

    async def run() -> None:
        try:
            payload = await async_suggest(hass, msg, send, stop)
        except Exception as err:  # noqa: BLE001
            payload = {"error": _error_payload(err)}
        send(payload)

    handle = _Suggesting(
        hass.async_create_background_task(run(), f"{DOMAIN}.suggest:{msg_id}", eager_start=False),
        stop,
    )
    connection.subscriptions[msg_id] = handle


async def async_suggest(
    hass: HomeAssistant,
    msg: dict[str, Any],
    progress: Callable[[dict[str, Any]], None],
    stop: threading.Event,
) -> dict[str, Any]:
    """Load the window's history, mine suggestions from it in the executor, validate the top ones exactly in one sandbox child, and return the result dict.

    Validation failing (the child crashed, timed out, refused the scenario) is not an error: the miner's own result comes back with every suggestion `exact: false` and a `warning`.
    """
    start, end = dt_util.as_utc(msg["start"]), dt_util.as_utc(msg["end"])
    check_window(start, end, MAX_SUGGEST_WINDOW)
    entity_ids = list(dict.fromkeys(msg["entity_ids"]))
    progress({"stage": "loading", "entities": len(entity_ids)})
    timeline = await history.async_get_timeline(
        hass,
        entity_ids,
        start,
        end,
        max_window=MAX_SUGGEST_WINDOW,
        max_events=MAX_SUGGEST_EVENTS,
    )
    progress(
        {"stage": "mining", "events": len(timeline["events"]), "labels": len(msg["labels"])}
    )
    result = await hass.async_add_executor_job(
        partial(
            _mine,
            timeline,
            start,
            end,
            msg["labels"],
            msg["options"],
            hass.config.time_zone,
            async_sun_events(hass),
            stop.is_set,
        )
    )
    if result.suggestions:
        progress({"stage": "validating", "candidates": len(result.suggestions)})
        result = await _async_validate(hass, result, timeline, start, end)
    return await hass.async_add_executor_job(result.as_dict)


@callback
def async_sun_events(hass: HomeAssistant) -> Callable[[date], dict[str, datetime | None]]:
    """The miner's `sun_events(date)`: what `homeassistant.helpers.sun.get_astral_event_date` returns for sunrise and sunset at this instance's location (None when the sun does not rise or set that day). The location is looked up here, on the event loop; the returned function is pure astral math, safe in the executor."""
    # The observer `homeassistant.helpers.sun.get_astral_observer` builds (2026.9+; earlier releases use `get_astral_location`, which 2026.9 deprecates), with the same astral calls.
    observer = astral.Observer(hass.config.latitude, hass.config.longitude, hass.config.elevation)

    def sun_events(day: date) -> dict[str, datetime | None]:
        events: dict[str, datetime | None] = {}
        for event in ("sunrise", "sunset"):
            try:
                events[event] = getattr(astral.sun, event)(observer, day)
            except ValueError:
                events[event] = None
        return events

    return sun_events


def _mine(
    data: dict[str, Any],
    start: datetime,
    end: datetime,
    raw_labels: list[dict[str, Any]],
    options: dict[str, Any],
    time_zone: str,
    sun_events: Callable[[date], dict[str, datetime | None]],
    should_stop: Callable[[], bool],
) -> SuggestResult:
    """Executor: the miner over the loaded timeline (labels outside the window are left out)."""
    timeline = Timeline.from_scenario(data["initial_states"], data["events"], start, end)
    tolerance = Tolerance(
        **{
            edge: options[f"tolerance_{edge}"]
            for edge in ("before", "after")
            if f"tolerance_{edge}" in options
        }
    )
    lo, hi = start.timestamp(), end.timestamp()
    in_window = [label for label in raw_labels if to_ts(label["end"]) > lo and to_ts(label["start"]) < hi]
    return suggest(
        timeline,
        Labels.from_dicts(in_window, tolerance),
        time_zone=time_zone,
        sun_events=sun_events,
        options=SuggestOptions.from_dict(options),
        should_stop=should_stop,
    )


async def _async_validate(
    hass: HomeAssistant,
    result: SuggestResult,
    timeline: dict[str, Any],
    start: datetime,
    end: datetime,
) -> SuggestResult:
    """Replay every suggestion in one sandbox child and re-score them on what Home Assistant did (suggest/exact.py).

    Only the entities the suggestions reference are sent, with their initial states; core config, registry snapshot and services come from this instance as for `simulate`. `traces: none` keeps a month of runs small.
    """
    configs = [suggestion.config for suggestion in result.suggestions]
    entity_ids = sorted(set().union(*(s.candidate.entity_ids for s in result.suggestions)))
    wanted = set(entity_ids)
    scenario = {
        "version": 1,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "automations": configs,
        "initial_states": {
            entity_id: state
            for entity_id, state in timeline["initial_states"].items()
            if entity_id in wanted
        },
        "events": [event for event in timeline["events"] if event["entity_id"] in wanted],
        "core": history.async_core_config(hass),
        "registry": history.async_registry_snapshot(hass, {"automations": configs}, entity_ids),
        "services": {
            domain: sorted(services) for domain, services in hass.services.async_services().items()
        },
        "traces": "none",
    }

    def approximate(message: str) -> SuggestResult:
        _LOGGER.warning("Suggestions could not be validated in the sandbox: %s", message)
        return mark_approximate(
            result,
            {
                "code": "validation_failed",
                "message": f"The suggestions could not be checked in the sandbox, so their stats are the miner's estimate: {message}",
            },
        )

    try:
        replay = await runner.async_run_scenario(hass, scenario, timeout=runner.DEFAULT_TIMEOUT)
    except SimulatorError as err:
        return approximate(str(err))
    if isinstance(error := replay.get("error"), dict):
        return approximate(str(error.get("message") or error.get("code") or "the sandbox refused the scenario"))
    loaded = {
        item.get("index"): item for item in replay.get("automations") or [] if isinstance(item, dict)
    }
    fires = fires_from_runs(replay.get("runs") or [], len(configs))
    exact = [fires[n] if (loaded.get(n) or {}).get("loaded") else None for n in range(len(configs))]
    refused = [n for n, found in enumerate(exact) if found is None]
    if len(refused) == len(configs):
        return approximate("the sandbox could not run any of them")
    warning = None
    if refused:
        reason = ((loaded.get(refused[0]) or {}).get("error") or {}).get("message") or "not loaded"
        warning = {
            "code": "partly_validated",
            "message": f"{len(refused)} of {len(configs)} suggestions could not be checked in the sandbox and keep the miner's estimate ({reason})",
        }
    return await hass.async_add_executor_job(partial(apply_exact, result, exact, warning=warning))


async def async_simulate(hass: HomeAssistant, scenario: dict[str, Any]) -> dict[str, Any]:
    """Replay a scenario (validated by SIMULATE_SCENARIO) in the sandbox and return its result.

    `scenario.automation` may be a config dict or YAML text. `core` (time zone, location, units, Labs preview features), `registry` (the registry entries the automation can see) and `services` default to this instance's own, so the panel need not send them. A refusal from the child (invalid scenario or automation) raises SimulatorError with the child's code.
    """
    scenario = dict(scenario)
    check_window(dt_util.parse_datetime(scenario["start"]), dt_util.parse_datetime(scenario["end"]))
    if (count := len(scenario["events"])) > MAX_EVENTS:
        raise SimulatorError(
            "too_many_events", f"{count} events in the scenario; the limit is {MAX_EVENTS}"
        )
    entity_ids = set(scenario["initial_states"]) | {
        event.get("entity_id") for event in scenario["events"] if isinstance(event, dict)
    }
    if (count := len(entity_ids)) > MAX_ENTITIES:
        raise SimulatorError(
            "too_many_entities", f"{count} entities in the scenario; the limit is {MAX_ENTITIES}"
        )
    if isinstance(scenario["automation"], str):
        scenario["automation"] = await hass.async_add_executor_job(
            parse_automation_yaml, scenario["automation"]
        )
    scenario.setdefault("core", history.async_core_config(hass))
    if "registry" not in scenario:
        event_ids = [
            event.get("entity_id") for event in scenario["events"] if isinstance(event, dict)
        ]
        timeline_ids = [
            entity_id
            for entity_id in dict.fromkeys([*scenario["initial_states"], *event_ids])
            if isinstance(entity_id, str)
        ]
        scenario["registry"] = history.async_registry_snapshot(
            hass, scenario["automation"], timeline_ids
        )
    scenario.setdefault(
        "services",
        {domain: sorted(services) for domain, services in hass.services.async_services().items()},
    )

    result = await runner.async_run_scenario(hass, scenario, timeout=runner.DEFAULT_TIMEOUT)
    if isinstance(error := result.get("error"), dict):
        raise SimulatorError(
            str(error.get("code") or "sandbox_failed"), str(error.get("message") or "")
        )
    return result


def parse_automation_yaml(text: str) -> dict[str, Any]:
    """An automation config from YAML text, refusing anything that reaches outside the text.

    HA's loader resolves `!include` (reads a file), `!secret`, `!env_var` and friends, so the text is composed first and any non-standard tag is refused before it is loaded; anchors are fine, but every use of one counts toward MAX_YAML_NODES, so an alias bomb is refused too.
    """
    if len(text) > MAX_YAML_CHARS:
        raise SimulatorError(
            "too_large",
            f"The automation YAML is {len(text)} characters; the limit is {MAX_YAML_CHARS}",
        )
    try:
        root = yaml.compose(text, Loader=_SAFE_LOADER)
    except yaml.YAMLError as err:
        raise SimulatorError(
            "invalid_automation", f"The automation YAML does not parse: {err}"
        ) from err
    visited = 0
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        visited += 1
        if visited > MAX_YAML_NODES:
            raise SimulatorError(
                "invalid_automation",
                "The automation YAML is too large once its aliases are expanded",
            )
        if node.tag not in _SAFE_TAGS:
            raise SimulatorError(
                "invalid_automation",
                f"The automation YAML uses {node.tag}, which the simulator does not resolve; paste the value itself",
            )
        if isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                stack.extend((key, value))
    try:
        config = parse_yaml(text)
    except HomeAssistantError as err:
        raise SimulatorError(
            "invalid_automation", f"The automation YAML does not parse: {err}"
        ) from err
    if not isinstance(config, dict):
        raise SimulatorError(
            "invalid_automation",
            "The automation YAML must be a mapping (triggers, conditions, actions)",
        )
    return config
