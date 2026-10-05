"""Config flow and panel lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.event_simulator.const import (
    DOMAIN,
    PANEL_JS_FILE,
    PANEL_STATIC_URL,
    PANEL_URL_PATH,
)


@pytest.fixture
async def setup(hass: HomeAssistant, tmp_path: Path):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_user_flow_creates_the_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Event Simulator"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_user_flow_refuses_a_second_entry(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_setup_registers_an_admin_only_panel(hass: HomeAssistant, setup):
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]

    assert panel.require_admin is True
    config = panel.config["_panel_custom"]
    assert config["name"] == "event-simulator-panel"
    assert config["module_url"].startswith(f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v=0.1.0-")


async def test_unload_removes_the_panel(hass: HomeAssistant, setup):
    assert await hass.config_entries.async_unload(setup.entry_id)
    await hass.async_block_till_done()

    assert setup.state is ConfigEntryState.NOT_LOADED
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]


def test_module_url_changes_when_the_panel_file_changes(tmp_path: Path):
    from custom_components.event_simulator import _module_url

    panel = tmp_path / PANEL_JS_FILE
    panel.write_text("console.log(1)")
    first = _module_url("0.1.0", panel)
    panel.write_text("console.log(2)")
    second = _module_url("0.1.0", panel)

    assert first.startswith(f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}?v=0.1.0-")
    assert first != second


async def test_panel_module_is_served_with_revalidation(hass: HomeAssistant, setup, hass_client):
    client = await hass_client()

    resp = await client.get(f"{PANEL_STATIC_URL}/{PANEL_JS_FILE}")

    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    assert "event-simulator-panel" in await resp.text()
