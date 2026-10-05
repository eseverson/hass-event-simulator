"""WebSocket API consumed by the sidebar panel. Every command is admin-only."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from . import history
from .const import DOMAIN
from .history import SimulatorError

_LOGGER = logging.getLogger(__name__)

Handler = Callable[[HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]], Awaitable[None]]


def _wrap(func: Handler) -> Handler:
    """Send SimulatorError as its own error code, anything else as unknown_error."""

    async def wrapper(hass, connection, msg):
        try:
            await func(hass, connection, msg)
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
    # event_simulator/simulate is registered here once the runner exists.


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
