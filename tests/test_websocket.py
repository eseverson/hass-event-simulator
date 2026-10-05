"""WebSocket API consumed by the panel."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.event_simulator.const import DOMAIN

AUTOMATION = {
    "id": "door_light",
    "alias": "Door light",
    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"}],
    "conditions": [
        {"condition": "template", "value_template": "{{ is_state('sun.sun', 'below_horizon') }}"}
    ],
    "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}],
}


@pytest.fixture
async def setup(hass: HomeAssistant):
    assert await async_setup_component(hass, "automation", {"automation": [AUTOMATION]})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_automations(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "event_simulator/automations"})
    msg = await client.receive_json()

    assert msg["success"]
    assert msg["result"] == [
        {"entity_id": "automation.door_light", "alias": "Door light", "id": "door_light"}
    ]


async def test_automation(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "event_simulator/automation", "entity_id": "automation.door_light"}
    )
    msg = await client.receive_json()

    assert msg["success"]
    assert msg["result"] == {
        "config": AUTOMATION,
        "referenced_entities": ["binary_sensor.door", "light.porch", "sun.sun"],
    }


async def test_automation_not_found(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": "event_simulator/automation", "entity_id": "automation.nope"}
    )
    msg = await client.receive_json()

    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_history(
    hass: HomeAssistant, setup, hass_ws_client, freezer: FrozenDateTimeFactory
):
    start = dt_util.utcnow().replace(microsecond=0) + timedelta(minutes=1)
    freezer.move_to(start - timedelta(seconds=30))
    hass.states.async_set("binary_sensor.door", "off")
    at = start + timedelta(minutes=2)
    freezer.move_to(at)
    hass.states.async_set("binary_sensor.door", "on")
    await async_wait_recording_done(hass)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": "event_simulator/history",
            "entity_ids": ["binary_sensor.door"],
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
        }
    )
    msg = await client.receive_json()

    assert msg["success"]
    assert msg["result"] == {
        "initial_states": {
            "binary_sensor.door": {
                "state": "off",
                "attributes": {},
                "last_changed": (start - timedelta(seconds=30)).isoformat(),
                "last_updated": (start - timedelta(seconds=30)).isoformat(),
            }
        },
        "events": [
            {
                "at": at.isoformat(),
                "entity_id": "binary_sensor.door",
                "state": "on",
                "attributes": {},
                "last_changed": at.isoformat(),
                "last_updated": at.isoformat(),
            }
        ],
    }


async def test_history_bad_window(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    now = dt_util.utcnow()

    await client.send_json_auto_id(
        {
            "type": "event_simulator/history",
            "entity_ids": ["binary_sensor.door"],
            "start": now.isoformat(),
            "end": (now - timedelta(hours=1)).isoformat(),
        }
    )
    msg = await client.receive_json()

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_window"


async def test_history_rejects_an_unparseable_time(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {
            "type": "event_simulator/history",
            "entity_ids": ["binary_sensor.door"],
            "start": "yesterday",
            "end": dt_util.utcnow().isoformat(),
        }
    )
    msg = await client.receive_json()

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


@pytest.mark.parametrize(
    "command",
    [
        {"type": "event_simulator/automations"},
        {"type": "event_simulator/automation", "entity_id": "automation.door_light"},
        {
            "type": "event_simulator/history",
            "entity_ids": ["binary_sensor.door"],
            "start": "2026-10-04T00:00:00+00:00",
            "end": "2026-10-05T00:00:00+00:00",
        },
    ],
)
async def test_non_admin_is_refused(
    hass: HomeAssistant, setup, hass_ws_client, hass_read_only_access_token, command
):
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json_auto_id(command)
    msg = await client.receive_json()

    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"
