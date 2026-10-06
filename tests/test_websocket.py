"""WebSocket API consumed by the panel."""

from __future__ import annotations

from datetime import timedelta
import sys
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
from homeassistant.helpers.json import json_bytes

from custom_components.event_simulator import history
from custom_components.event_simulator.const import DOMAIN
from custom_components.event_simulator.packed import pack_events, unpack_events

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
    assert {k: got["result"][k] for k in SCENARIO if k != "events"} == {
        k: v for k, v in SCENARIO.items() if k != "events"
    }
    assert unpack_events(got["result"]["events"]) == SCENARIO["events"]

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
    assert unpack_events(got["result"]["events"]) == SCENARIO["events"]


def _chatty_week(count: int) -> list[dict]:
    """A power sensor recorded every 20 s, as history.async_get_timeline returns it, with a few edits."""
    start = dt_util.parse_datetime("2026-10-04T00:00:00+00:00")
    events = []
    for i in range(count):
        at = (start + timedelta(seconds=20 * i, microseconds=123456)).isoformat()
        event = {
            "at": at,
            "entity_id": "sensor.power",
            "state": f"{300 + i % 997 / 10:.1f}",
            "attributes": {"unit_of_measurement": "W", "device_class": "power", "friendly_name": "House power", "state_class": "measurement"},
            "last_changed": at,
            "last_updated": at,
        }
        if i % 1000 == 1:  # an attribute-only change keeps the earlier last_changed
            event["last_changed"] = events[-1]["at"]
        if i % 5000 == 2:  # an edit has no recorded times
            event = {**{k: v for k, v in event.items() if not k.startswith("last_")}, "edited": True}
        events.append(event)
    return events


async def test_a_week_of_a_chatty_sensor_saves_packed_and_comes_back_whole(
    hass: HomeAssistant, setup, hass_ws_client
):
    """#15: a save is sent packed (it would be ~9 MB unpacked) and get returns it packed, losslessly."""
    client = await hass_ws_client(hass)
    events = _chatty_week(30_000)
    body = {**SCENARIO, "end": "2026-10-11T00:00:00+00:00", "entity_ids": ["sensor.power"], "initial_states": {}}
    packed = pack_events(events)

    saved = await _call(client, {"type": "event_simulator/scenarios/save", **body, "events": packed})
    assert saved["success"], saved
    assert saved["result"]["event_count"] == 30_000

    got = await _call(
        client, {"type": "event_simulator/scenarios/get", "scenario_id": saved["result"]["scenario_id"]}
    )
    assert got["success"]
    assert got["result"]["events"] == packed
    assert unpack_events(got["result"]["events"]) == events
    # Both directions fit under the 4 MiB message limit HA's websocket enforces on what it receives.
    assert len(json_bytes({"type": "event_simulator/scenarios/save", **body, "events": packed})) < 4 * 1024 * 1024
    assert len(json_bytes(got)) < 4 * 1024 * 1024


async def test_scenario_save_validates_packed_events(hass: HomeAssistant, setup, hass_ws_client):
    """Unpacked events go through the same checks as a plain list: here, `at` without an offset."""
    client = await hass_ws_client(hass)
    packed = {"entity_ids": ["binary_sensor.door"], "attributes": [{}], "rows": [["2026-10-04T07:12:03", 0, "on", 0]]}

    msg = await _call(client, {"type": "event_simulator/scenarios/save", **SCENARIO, "events": packed})

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


async def test_scenario_save_rejects_a_malformed_packing(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    packed = {"entity_ids": ["binary_sensor.door"], "attributes": [{}], "rows": [["2026-10-04T07:12:03+00:00", 4, "on", 0]]}

    msg = await _call(client, {"type": "event_simulator/scenarios/save", **SCENARIO, "events": packed})

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


async def test_scenario_save_counts_packed_events_against_the_limit(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    with patch("custom_components.event_simulator.store.MAX_EVENTS", 1):
        msg = await _call(
            client,
            {"type": "event_simulator/scenarios/save", **SCENARIO, "events": pack_events(SCENARIO["events"] * 2)},
        )

    assert msg["error"]["code"] == "too_large"


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


SIMULATE = {
    "version": 1,
    "start": "2026-10-04T00:00:00+00:00",
    "end": "2026-10-04T06:00:00+00:00",
    "automation": AUTOMATION,
    "initial_states": {
        "binary_sensor.door": {"state": "off", "attributes": {}},
        "sun.sun": {"state": "below_horizon", "attributes": {}},
    },
    "events": [
        {"at": "2026-10-04T01:00:00+00:00", "entity_id": "binary_sensor.door", "state": "on", "attributes": {}},
        {"at": "2026-10-04T01:05:00+00:00", "entity_id": "binary_sensor.door", "state": "off", "attributes": {}},
    ],
}

AUTOMATION_YAML = """
id: door_light
alias: Door light
triggers:
  - trigger: state
    entity_id: binary_sensor.door
    to: "on"
actions:
  - action: light.turn_on
    target:
      entity_id: light.porch
"""


async def test_simulate(hass: HomeAssistant, setup, hass_ws_client):
    """End to end through the real sandbox child."""
    client = await hass_ws_client(hass)

    msg = await _call(client, {"type": "event_simulator/simulate", "scenario": SIMULATE})

    assert msg["success"], msg
    result = msg["result"]
    assert result["error"] is None
    [run] = result["runs"]
    assert run["outcome"] == "fired"
    assert run["started"].startswith("2026-10-04T01:00:00")
    [call] = result["service_calls"]
    assert (call["domain"], call["service"]) == ("light", "turn_on")
    assert call["target"] == {"entity_id": ["light.porch"]}
    assert call["run"] == run["id"]  # how the panel ties calls to runs
    # services were filled in from the live instance, which has no light.turn_on
    assert [w["code"] for w in result["warnings"]] == ["unknown_service"]


async def test_simulate_with_trimmed_traces(hass: HomeAssistant, setup, hass_ws_client):
    """#28: `traces` reaches the child, which then leaves the step trace out."""
    client = await hass_ws_client(hass)

    msg = await _call(
        client, {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "traces": "summary"}}
    )

    assert msg["success"], msg
    [run] = msg["result"]["runs"]
    assert run["outcome"] == "fired"
    assert "trace" not in run and run["conditions"] == {"condition/0": True}
    assert len(msg["result"]["service_calls"]) == 1


async def test_simulate_yaml_automation(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    msg = await _call(
        client,
        {
            "type": "event_simulator/simulate",
            "scenario": {**SIMULATE, "automation": AUTOMATION_YAML},
        },
    )

    assert msg["success"], msg
    assert [run["outcome"] for run in msg["result"]["runs"]] == ["fired"]


async def test_simulate_fills_core_and_services(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    seen = {}

    async def fake_run(hass, scenario, **kwargs):
        seen.update(scenario)
        return {"version": 1, "error": None, "runs": [], "service_calls": [], "warnings": []}

    with patch("custom_components.event_simulator.runner.async_run_scenario", fake_run):
        msg = await _call(
            client,
            {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "automation": AUTOMATION_YAML}},
        )

    assert msg["success"], msg
    assert seen["core"] == history.async_core_config(hass)
    assert "preview_features" in seen["core"]
    assert seen["registry"] == history.async_registry_snapshot(
        hass, seen["automation"], ["binary_sensor.door", "sun.sun"]
    )
    assert "turn_on" in seen["services"]["automation"]
    assert seen["automation"]["triggers"][0]["to"] == "on"
    assert seen["start"] == "2026-10-04T00:00:00+00:00"


PACKED_EVENTS = {
    "entity_ids": ["binary_sensor.door"],
    "attributes": [{}, {"device_class": "door"}],
    "rows": [
        ["2026-10-04T01:00:00+00:00", 0, "on", 1],
        ["2026-10-04T01:05:00+00:00", 0, "off", 0],
    ],
}


async def test_simulate_accepts_packed_events(hass: HomeAssistant, setup, hass_ws_client):
    """The panel packs events (shared entity ids and attributes) to stay under the websocket's 4 MiB message limit."""
    client = await hass_ws_client(hass)
    seen = {}

    async def fake_run(hass, scenario, **kwargs):
        seen.update(scenario)
        return {"version": 1, "error": None, "runs": [], "service_calls": [], "warnings": []}

    with patch("custom_components.event_simulator.runner.async_run_scenario", fake_run):
        msg = await _call(
            client,
            {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "events": PACKED_EVENTS}},
        )

    assert msg["success"], msg
    assert seen["events"] == [
        {"at": "2026-10-04T01:00:00+00:00", "entity_id": "binary_sensor.door", "state": "on", "attributes": {"device_class": "door"}},
        {"at": "2026-10-04T01:05:00+00:00", "entity_id": "binary_sensor.door", "state": "off", "attributes": {}},
    ]
    assert seen["registry"] == history.async_registry_snapshot(
        hass, seen["automation"], ["binary_sensor.door", "sun.sun"]
    )


@pytest.mark.parametrize(
    "packed",
    [
        {**PACKED_EVENTS, "rows": [["2026-10-04T01:00:00+00:00", 1, "on", 0]]},
        {**PACKED_EVENTS, "rows": [["2026-10-04T01:00:00+00:00", 0, "on", 2]]},
        {**PACKED_EVENTS, "rows": [["2026-10-04T01:00:00+00:00", 0, "on"]]},
        {**PACKED_EVENTS, "rows": [["2026-10-04T01:00:00+00:00", 0, 5, 0]]},
        {**PACKED_EVENTS, "attributes": ["x"]},
        {"entity_ids": ["binary_sensor.door"]},
    ],
)
async def test_simulate_rejects_bad_packed_events(
    hass: HomeAssistant, setup, hass_ws_client, packed
):
    client = await hass_ws_client(hass)

    msg = await _call(
        client, {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "events": packed}}
    )

    assert not msg["success"]
    assert msg["error"]["code"] in ("invalid_scenario", "invalid_format")


async def test_simulate_counts_packed_events_against_the_limit(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    with patch("custom_components.event_simulator.websocket.MAX_EVENTS", 1):
        msg = await _call(
            client, {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "events": PACKED_EVENTS}}
        )

    assert msg["error"]["code"] == "too_many_events"


async def test_simulate_keeps_a_given_core(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    seen = {}
    core = {"time_zone": "Asia/Tokyo", "latitude": 35.0, "longitude": 139.0, "elevation": 0, "unit_system": "metric"}

    async def fake_run(hass, scenario, **kwargs):
        seen.update(scenario)
        return {"version": 1, "error": None, "runs": [], "service_calls": [], "warnings": []}

    with patch("custom_components.event_simulator.runner.async_run_scenario", fake_run):
        msg = await _call(
            client, {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "core": core}}
        )

    assert msg["success"], msg
    assert seen["core"] == core


@pytest.mark.parametrize(
    ("yaml_text", "needle"),
    [
        ("triggers: !include /etc/hostname\nactions: []", "!include"),
        ("alias: !secret door\ntriggers: []\nactions: []", "!secret"),
        ("alias: !env_var HOME\ntriggers: []\nactions: []", "!env_var"),
        ("alias: !!python/object:os.system x\ntriggers: []\nactions: []", "python/object"),
        ("triggers: [", "parse"),
        ("- just\n- a list", "mapping"),
    ],
)
async def test_simulate_rejects_bad_yaml(
    hass: HomeAssistant, setup, hass_ws_client, yaml_text, needle
):
    client = await hass_ws_client(hass)

    msg = await _call(
        client,
        {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "automation": yaml_text}},
    )

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_automation"
    assert needle in msg["error"]["message"]


async def test_simulate_rejects_a_yaml_alias_bomb(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{n}: &a{n} [{', '.join([f'*a{n - 1}'] * 10)}]" for n in range(1, 9)]

    msg = await _call(
        client,
        {"type": "event_simulator/simulate", "scenario": {**SIMULATE, "automation": "\n".join(lines)}},
    )

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_automation"
    assert "too large" in msg["error"]["message"]


async def test_simulate_invalid_automation(hass: HomeAssistant, setup, hass_ws_client):
    """The child's structured refusal comes back as a websocket error."""
    client = await hass_ws_client(hass)

    msg = await _call(
        client,
        {
            "type": "event_simulator/simulate",
            "scenario": {**SIMULATE, "automation": {"triggers": [{"trigger": "state"}], "actions": []}},
        },
    )

    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_automation"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"end": "2026-10-03T00:00:00+00:00"}, "invalid_window"),
        ({"end": "2026-10-14T00:00:00+00:00"}, "window_too_long"),
        ({"start": "yesterday"}, "invalid_format"),
        ({"automation": 5}, "invalid_format"),
        ({"events": [SIMULATE["events"][0]] * 3}, "too_many_events"),
        (
            {"initial_states": {f"sensor.s{n}": {"state": "1"} for n in range(3)}},
            "too_many_entities",
        ),
        ({"automation": "alias: " + "x" * 300}, "too_large"),
        ({"traces": "steps"}, "invalid_format"),
    ],
)
async def test_simulate_validates(
    hass: HomeAssistant, setup, hass_ws_client, change, code
):
    client = await hass_ws_client(hass)

    with (
        patch("custom_components.event_simulator.websocket.MAX_EVENTS", 2),
        patch("custom_components.event_simulator.websocket.MAX_ENTITIES", 3),
        patch("custom_components.event_simulator.websocket.MAX_YAML_CHARS", 200),
    ):
        msg = await _call(
            client, {"type": "event_simulator/simulate", "scenario": {**SIMULATE, **change}}
        )

    assert not msg["success"]
    assert msg["error"]["code"] == code


async def test_simulate_sandbox_crash(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    crash = [sys.executable, "-c", "import sys; sys.stderr.write('Traceback: boom'); sys.exit(1)"]

    with patch("custom_components.event_simulator.runner.child_command", return_value=crash):
        msg = await _call(client, {"type": "event_simulator/simulate", "scenario": SIMULATE})

    assert not msg["success"]
    assert msg["error"]["code"] == "sandbox_failed"
    assert msg["error"]["stderr_tail"] == "Traceback: boom"


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
        {"type": "event_simulator/simulate", "scenario": SIMULATE},
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


async def test_scenario_round_trip_keeps_track_shifts(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    shifts = {
        "binary_sensor.door": {
            "offset": -9000000,
            "base": {"state": "off", "attributes": {}, "last_changed": "2026-10-03T21:30:00.000Z"},
        }
    }

    saved = await _call(
        client, {"type": "event_simulator/scenarios/save", **SCENARIO, "track_shifts": shifts}
    )
    assert saved["success"], saved
    got = await _call(
        client,
        {"type": "event_simulator/scenarios/get", "scenario_id": saved["result"]["scenario_id"]},
    )

    assert got["result"]["track_shifts"] == shifts


async def test_scenario_without_track_shifts_stores_none(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    saved = await _call(client, {"type": "event_simulator/scenarios/save", **SCENARIO})
    got = await _call(
        client,
        {"type": "event_simulator/scenarios/get", "scenario_id": saved["result"]["scenario_id"]},
    )

    assert "track_shifts" not in got["result"]


async def test_scenario_save_refuses_a_malformed_track_shift(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    saved = await _call(
        client,
        {
            "type": "event_simulator/scenarios/save",
            **SCENARIO,
            "track_shifts": {"binary_sensor.door": {"base": None}},
        },
    )

    assert not saved["success"]
