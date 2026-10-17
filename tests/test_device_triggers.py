"""#48: the live half of the device-trigger adapters — the panel's device lanes, and the `device_triggers` a replay carries.

ZHA's data is put in its trigger cache with no gateway running, the state ZHA's own device trigger falls back to; nothing of ZHA is set up.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import ANY, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.event_simulator import device_triggers, runner

from .test_websocket_suggest import setup  # noqa: F401 - fixture

pytestmark = pytest.mark.requires_zha

IEEE = "a4:c1:38:ad:af:88:68:87"
SHORT, DOUBLE, LONG = "remote_button_short_press", "remote_button_double_press", "remote_button_long_press"
TYPES = (SHORT, DOUBLE, LONG)
EXPORTED = {"ieee": IEEE, "triggers": [{"type": t, "subtype": "button_1", "data": {"endpoint_id": 1, "command": t}} for t in TYPES]}


@pytest.fixture
async def button(hass: HomeAssistant) -> dr.DeviceEntry:
    from homeassistant.components.zha.helpers import get_zha_data  # noqa: PLC0415 - needs ZHA's packages

    entry = MockConfigEntry(domain="zha", title="Zigbee")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zha", IEEE)}, name="Outdoor String Lights Button"
    )
    get_zha_data(hass).device_trigger_cache[device.id] = (IEEE, {(t, "button_1"): {"endpoint_id": 1, "command": t} for t in TYPES})
    return device


def automation(device_id: str, *kinds: str) -> dict[str, Any]:
    return {
        "triggers": [{"trigger": "device", "domain": "zha", "device_id": device_id, "type": kind, "subtype": "button_1"} for kind in kinds],
        "actions": [{"action": "light.toggle", "target": {"entity_id": "light.string_lights"}}],
    }


def hue_automation() -> dict[str, Any]:
    return {"triggers": [{"trigger": "device", "domain": "hue", "device_id": "abc", "type": "initial_press", "subtype": "1"}], "actions": []}


async def test_lanes_name_the_device_and_flag_used_kinds(hass: HomeAssistant, button):
    out = await device_triggers.async_device_lanes(hass, automation(button.id, SHORT))
    assert out["unsupported"] == []
    (lane,) = out["lanes"]
    assert lane["id"] == f"device:{button.id}"
    assert lane["domain"] == "zha"
    assert lane["device_id"] == button.id
    assert lane["name"] == "Outdoor String Lights Button"
    assert lane["event_type"] == "zha_event"
    assert [(k["type"], k["subtype"], k["used"]) for k in lane["kinds"]] == [(SHORT, "button_1", True), (DOUBLE, "button_1", False), (LONG, "button_1", False)]
    assert lane["kinds"][0]["event_type"] == "zha_event"
    assert lane["kinds"][0]["match"] == {"device_ieee": IEEE, "endpoint_id": 1, "command": SHORT}


async def test_used_follows_the_config_passed(hass: HomeAssistant, button):
    (lane,) = (await device_triggers.async_device_lanes(hass, automation(button.id, DOUBLE)))["lanes"]
    assert [k["used"] for k in lane["kinds"]] == [False, True, False]


async def test_two_triggers_on_one_device_make_one_lane(hass: HomeAssistant, button):
    lanes = (await device_triggers.async_device_lanes(hass, automation(button.id, SHORT, DOUBLE)))["lanes"]
    assert len(lanes) == 1
    assert [k["used"] for k in lanes[0]["kinds"]] == [True, True, False]


async def test_unknown_device_is_unsupported_no_data(hass: HomeAssistant, button):
    out = await device_triggers.async_device_lanes(hass, automation("gone", SHORT))
    assert out["lanes"] == []
    (entry,) = out["unsupported"]
    assert (entry["path"], entry["domain"], entry["device_id"], entry["reason"]) == ("triggers/0", "zha", "gone", "no_data")
    assert "gone" in entry["message"]


async def test_integration_without_adapter_is_unsupported(hass: HomeAssistant):
    (entry,) = (await device_triggers.async_device_lanes(hass, hue_automation()))["unsupported"]
    assert entry["reason"] == "no_adapter"
    assert "hue" in entry["message"]


async def test_entity_domain_device_triggers_need_no_lane(hass: HomeAssistant):
    config = {"triggers": [{"trigger": "device", "domain": "binary_sensor", "device_id": "x", "entity_id": "y", "type": "motion"}], "actions": []}
    assert await device_triggers.async_device_lanes(hass, config) == {"lanes": [], "unsupported": []}


async def test_changed_private_api_is_unreadable(hass: HomeAssistant, button, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delattr("homeassistant.components.zha.device_trigger._get_device_trigger_data")
    (entry,) = (await device_triggers.async_device_lanes(hass, automation(button.id, SHORT)))["unsupported"]
    assert entry["reason"] == "unreadable"


async def test_export_is_the_scenario_block(hass: HomeAssistant, button):
    assert await device_triggers.async_export(hass, automation(button.id, SHORT)) == {"zha": {button.id: EXPORTED}}
    assert await device_triggers.async_export(hass, automation("gone", SHORT)) == {}


async def test_device_lanes_command(hass: HomeAssistant, setup, button, hass_ws_client):  # noqa: F811
    client = await hass_ws_client(hass)
    yaml = f"triggers:\n  - trigger: device\n    domain: zha\n    device_id: {button.id}\n    type: {DOUBLE}\n    subtype: button_1\nactions: []\n"
    await client.send_json_auto_id({"type": "event_simulator/device_lanes", "automation": yaml})
    msg = await client.receive_json()
    assert msg["success"], msg
    assert [k["used"] for k in msg["result"]["lanes"][0]["kinds"]] == [False, True, False]


async def test_simulate_fills_device_triggers(hass: HomeAssistant, setup, button, hass_ws_client):  # noqa: F811
    captured: dict[str, Any] = {}

    async def spy(_hass: HomeAssistant, scenario: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        captured.update(scenario)
        return {"version": 1, "error": None, "runs": [], "service_calls": [], "warnings": []}

    client = await hass_ws_client(hass)
    with patch.object(runner, "async_run_scenario", spy):
        await client.send_json_auto_id(
            {
                "type": "event_simulator/simulate",
                "scenario": {"start": "2026-10-04T00:00:00+00:00", "end": "2026-10-04T06:00:00+00:00", "automation": automation(button.id, SHORT)},
            }
        )
        msg = await client.receive_json()
    assert msg["success"], msg
    assert captured["device_triggers"] == {"zha": {button.id: ANY}}


async def test_a_zha_trigger_on_a_non_zha_device_is_no_data(hass: HomeAssistant, button, monkeypatch: pytest.MonkeyPatch):
    """With a gateway running (live), ZHA's own lookup raises StopIteration for a device without a zha identifier; that is a device it has no data for, not a failed command."""
    from types import SimpleNamespace  # noqa: PLC0415

    from homeassistant.components.zha import helpers as zha_helpers  # noqa: PLC0415

    monkeypatch.setattr(zha_helpers, "get_zha_gateway_proxy", lambda _hass: SimpleNamespace(device_proxies={}))
    entry = MockConfigEntry(domain="hue")
    entry.add_to_hass(hass)
    other = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hue", "x")}, name="Hue thing")
    (unsupported,) = (await device_triggers.async_device_lanes(hass, automation(other.id, SHORT)))["unsupported"]
    assert unsupported["reason"] == "no_data"
    assert await device_triggers.async_export(hass, automation(other.id, SHORT)) == {}


async def test_a_removed_device_does_not_make_zha_log_errors(hass: HomeAssistant, button, caplog: pytest.LogCaptureFixture):
    """Every auto-run asks again, so ZHA's own 'not found in registry' error would flood the log."""
    await device_triggers.async_device_lanes(hass, automation("gone", SHORT))
    assert not [r for r in caplog.records if r.levelname == "ERROR" and "zha" in r.name]
