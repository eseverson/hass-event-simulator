"""WebSocket API consumed by the sidebar panel. Every command is admin-only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

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
from .history import MAX_ENTITIES, MAX_EVENTS, SimulatorError, check_window
from .packed import unpack_events
from .runner import SandboxError
from .store import DATA_STORE, SCENARIO_FIELDS, ScenarioStore

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]], Awaitable[None]]


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
    connection.send_result(
        msg["id"],
        await history.async_get_timeline(hass, msg["entity_ids"], msg["start"], msg["end"]),
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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/simulate",
        # Only what the live side needs is checked here; the child validates the rest and answers with a structured error.
        vol.Required("scenario"): vol.Schema(
            {
                vol.Required("start"): _aware_utc_iso,
                vol.Required("end"): _aware_utc_iso,
                vol.Required("automation"): vol.Any(dict, str),
                vol.Optional("core"): dict,
                vol.Optional("services"): dict,
                vol.Optional("initial_states", default=dict): dict,
                # A plain list, or packed (packed.py) to fit the websocket's 4 MiB message limit.
                vol.Optional("events", default=list): vol.Any(list, unpack_events),
            },
            extra=vol.ALLOW_EXTRA,
        ),
    }
)
@websocket_api.async_response
@_wrap
async def ws_simulate(hass, connection, msg: dict[str, Any]) -> None:
    """Replay a scenario in the sandbox and send its result.

    `scenario.automation` may be a config dict or YAML text. `core` (time zone, location, units, Labs preview features), `registry` (the registry entries the automation can see) and `services` default to this instance's own, so the panel need not send them. A refusal from the child (invalid scenario or automation) is sent as an error with the child's code.
    """
    scenario = dict(msg["scenario"])
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
    connection.send_result(msg["id"], result)


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
