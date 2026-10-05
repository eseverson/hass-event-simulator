"""WebSocket API consumed by the panel."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

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


SCENARIO = {
    "name": "Door at night",
    "automation_entity_id": "automation.door_light",
    "automation": AUTOMATION,
    "start": "2026-10-04T00:00:00+00:00",
    "end": "2026-10-05T00:00:00+00:00",
    "entity_ids": ["binary_sensor.door"],
    "initial_states": {"binary_sensor.door": {"state": "off", "attributes": {}}},
    "events": [
        {
            "at": "2026-10-04T07:12:03+00:00",
            "entity_id": "binary_sensor.door",
            "state": "on",
            "attributes": {},
        }
    ],
}


async def _call(client, payload):
    await client.send_json_auto_id(payload)
    return await client.receive_json()


async def test_scenario_round_trip(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    saved = await _call(client, {"type": "event_simulator/scenarios/save", **SCENARIO})
    assert saved["success"], saved
    scenario_id = saved["result"]["scenario_id"]
    assert saved["result"]["event_count"] == 1

    listed = await _call(client, {"type": "event_simulator/scenarios/list"})
    assert listed["success"]
    assert [s["scenario_id"] for s in listed["result"]] == [scenario_id]
    assert "events" not in listed["result"][0]

    got = await _call(
        client, {"type": "event_simulator/scenarios/get", "scenario_id": scenario_id}
    )
    assert got["success"]
    assert {k: got["result"][k] for k in SCENARIO} == SCENARIO

    deleted = await _call(
        client, {"type": "event_simulator/scenarios/delete", "scenario_id": scenario_id}
    )
    assert deleted["success"]
    listed = await _call(client, {"type": "event_simulator/scenarios/list"})
    assert listed["result"] == []


async def test_scenario_save_overwrites_by_id(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    first = await _call(client, {"type": "event_simulator/scenarios/save", **SCENARIO})
    scenario_id = first["result"]["scenario_id"]

    second = await _call(
        client,
        {
            "type": "event_simulator/scenarios/save",
            **SCENARIO,
            "scenario_id": scenario_id,
            "name": "Renamed",
        },
    )

    assert second["success"]
    assert second["result"]["scenario_id"] == scenario_id
    listed = await _call(client, {"type": "event_simulator/scenarios/list"})
    assert [s["name"] for s in listed["result"]] == ["Renamed"]


async def test_scenario_save_without_automation_entity(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)
    body = {k: v for k, v in SCENARIO.items() if k != "automation_entity_id"}

    saved = await _call(client, {"type": "event_simulator/scenarios/save", **body})

    assert saved["success"]
    assert saved["result"]["automation_entity_id"] is None


async def test_scenarios_survive_a_reload(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    saved = await _call(client, {"type": "event_simulator/scenarios/save", **SCENARIO})
    entry = hass.config_entries.async_entries(DOMAIN)[0]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    got = await _call(
        client,
        {"type": "event_simulator/scenarios/get", "scenario_id": saved["result"]["scenario_id"]},
    )
    assert got["success"]
    assert got["result"]["events"] == SCENARIO["events"]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "event_simulator/scenarios/get", "scenario_id": "nope"},
        {"type": "event_simulator/scenarios/delete", "scenario_id": "nope"},
        {"type": "event_simulator/scenarios/save", **SCENARIO, "scenario_id": "nope"},
    ],
)
async def test_scenario_unknown_id(hass: HomeAssistant, setup, hass_ws_client, payload):
    client = await hass_ws_client(hass)

    msg = await _call(client, payload)

    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_scenario_save_rejects_a_bad_event(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    msg = await _call(
        client,
        {
            "type": "event_simulator/scenarios/save",
            **SCENARIO,
            # no timezone offset on `at`
            "events": [
                {"at": "2026-10-04T07:12:03", "entity_id": "binary_sensor.door", "state": "on"}
            ],
        },
    )

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


async def test_scenario_save_rejects_too_many_events(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    with patch("custom_components.event_simulator.store.MAX_EVENTS", 1):
        msg = await _call(
            client,
            {
                "type": "event_simulator/scenarios/save",
                **SCENARIO,
                "events": SCENARIO["events"] * 2,
            },
        )

    assert not msg["success"]
    assert msg["error"]["code"] == "too_large"


@pytest.mark.parametrize(
    "command",
    [
        {"type": "event_simulator/scenarios/list"},
        {"type": "event_simulator/scenarios/get", "scenario_id": "x"},
        {"type": "event_simulator/scenarios/delete", "scenario_id": "x"},
        {"type": "event_simulator/scenarios/save", **SCENARIO},
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
