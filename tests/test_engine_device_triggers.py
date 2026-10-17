"""#48: integration-specific device triggers replay through the integration's own trigger code, seeded from `device_triggers`.

The registry is snapshotted from a live-side `hass` (as the simulate command sends it) and every scenario runs through a real child process. ZHA's device trigger falls back to its trigger cache when no gateway is running, which is what the sandbox seeds.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.event_simulator import history

from .test_engine import run_child
from .test_engine_fidelity import scenario

pytestmark = pytest.mark.requires_zha

IEEE = "a4:c1:38:ad:af:88:68:87"
WINDOW = "2026-10-04T{}:00+00:00"
TYPES = ("remote_button_short_press", "remote_button_double_press", "remote_button_long_press")
PAYLOAD = {"ieee": IEEE, "triggers": [{"type": t, "subtype": "button_1", "data": {"endpoint_id": 1, "command": t}} for t in TYPES]}


@pytest.fixture
async def button(hass: HomeAssistant) -> dr.DeviceEntry:
    entry = MockConfigEntry(domain="zha", title="Zigbee")
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zha", IEEE)}, name="Outdoor String Lights Button"
    )


def press(at: str, command: str, device_id: str) -> dict[str, Any]:
    return {
        "at": WINDOW.format(at),
        "event_type": "zha_event",
        "data": {
            "device_ieee": IEEE,
            "device_id": device_id,
            "unique_id": f"{IEEE}:1:0x0006",
            "endpoint_id": 1,
            "cluster_id": 6,
            "command": command,
            "args": [],
            "params": {},
        },
    }


def presses(device_id: str) -> list[dict[str, Any]]:
    return [
        press("01:00", "remote_button_short_press", device_id),
        press("02:00", "remote_button_double_press", device_id),
        press("03:00", "attribute_updated", device_id),
        press("04:00", "remote_button_short_press", device_id),
    ]


def device_trigger(device_id: str, kind: str, domain: str = "zha") -> dict[str, Any]:
    return {"trigger": "device", "domain": domain, "device_id": device_id, "type": kind, "subtype": "button_1"}


async def replay(hass: HomeAssistant, automation: dict[str, Any], **extra: Any) -> dict[str, Any]:
    built = scenario({"actions": [], **automation}, **extra)
    built["registry"] = history.async_registry_snapshot(hass, built["automation"], [])
    code, result, stderr = await asyncio.get_running_loop().run_in_executor(None, run_child, built)
    assert code == 0, stderr[-4000:]
    return result


def started(result: dict[str, Any]) -> list[str]:
    return [run["started"][11:16] for run in result["runs"]]


async def test_zha_device_trigger_fires_on_its_presses(hass: HomeAssistant, button):
    result = await replay(
        hass,
        {"triggers": [device_trigger(button.id, "remote_button_short_press")]},
        bus_events=presses(button.id),
        device_triggers={"zha": {button.id: PAYLOAD}},
    )
    assert result["error"] is None, result["error"]
    assert [(run["started"][11:16], run["outcome"]) for run in result["runs"]] == [("01:00", "fired"), ("04:00", "fired")]
    assert {run["trigger"]["platform"] for run in result["runs"]} == {"device"}


async def test_two_triggers_on_one_device_fire_once_each(hass: HomeAssistant, button):
    result = await replay(
        hass,
        {"triggers": [device_trigger(button.id, "remote_button_short_press"), device_trigger(button.id, "remote_button_double_press")]},
        bus_events=presses(button.id),
        device_triggers={"zha": {button.id: PAYLOAD}},
    )
    assert result["error"] is None, result["error"]
    assert started(result) == ["01:00", "02:00", "04:00"]


async def test_no_payload_for_the_device_is_refused_naming_it(hass: HomeAssistant, button):
    result = await replay(hass, {"triggers": [device_trigger(button.id, "remote_button_short_press")]}, bus_events=presses(button.id))
    error = result["error"]
    assert error["code"] == "unsupported_automation"
    assert button.id in error["message"]
    assert "no trigger data" in error["message"]


async def test_integration_without_adapter_is_refused(hass: HomeAssistant, button):
    result = await replay(
        hass,
        {"triggers": [device_trigger(button.id, "initial_press", domain="hue")]},
        device_triggers={"zha": {button.id: PAYLOAD}},
    )
    assert result["error"]["code"] == "unsupported_automation"
    assert "device triggers from 'hue' are not supported yet" in result["error"]["message"]


async def test_device_condition_from_zha_is_still_refused(hass: HomeAssistant, button):
    result = await replay(
        hass,
        {
            "triggers": [{"trigger": "event", "event_type": "zha_event"}],
            "conditions": [{"condition": "device", "domain": "zha", "device_id": button.id, "type": "is_on"}],
        },
        device_triggers={"zha": {button.id: PAYLOAD}},
    )
    assert result["error"]["code"] == "unsupported_automation"
    assert "device condition" in result["error"]["message"]


async def test_malformed_device_triggers_is_invalid_scenario(hass: HomeAssistant, button):
    result = await replay(
        hass,
        {"triggers": [device_trigger(button.id, "remote_button_short_press")]},
        device_triggers={"zha": {button.id: {"ieee": 1}}},
    )
    assert result["error"]["code"] == "invalid_scenario"
