"""Event Simulator: replay recorded history against an automation in a sandbox.

Pick an automation, pull the history of the entities it depends on onto an
editable timeline, and replay it in a child process running on virtual time, so
nothing touches the live instance. The panel reports which runs fired, which
condition stopped them, and which service calls they would have made.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from . import websocket
from .const import (
    DOMAIN,
    PANEL_ICON,
    PANEL_JS_FILE,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PANEL_WEBCOMPONENT,
)
from .store import DATA_STORE, ScenarioStore

_DATA_STATIC_REGISTERED = f"{DOMAIN}_static_registered"


def _module_url(version: str, panel_js: Path) -> str:
    """Cache-bust on the panel's own bytes, so every edit gets a fresh URL: the static path caches the module for a month."""
    digest = hashlib.sha256(panel_js.read_bytes()).hexdigest()[:8]
    return f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v={version}-{digest}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = ScenarioStore(hass)
    await store.async_load()
    hass.data[DATA_STORE] = store
    websocket.async_register(hass)
    await _async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
    hass.data.pop(DATA_STORE, None)
    return True


async def _async_register_panel(hass: HomeAssistant) -> None:
    panel_js = Path(__file__).parent / "panel" / PANEL_JS_FILE
    # Static paths cannot be unregistered, so a reload must not add a second one.
    if not hass.data.get(_DATA_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}", str(panel_js), cache_headers=True)]
        )
        hass.data[_DATA_STATIC_REGISTERED] = True

    integration = await async_get_integration(hass, DOMAIN)
    module_url = await hass.async_add_executor_job(
        _module_url, str(integration.version), panel_js
    )

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_WEBCOMPONENT,
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=module_url,
        embed_iframe=False,
        require_admin=True,
    )
