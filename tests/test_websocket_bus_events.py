"""#35: recorded bus events in Suggest mode (event triggers mined from them, validated in the sandbox with the events replayed), the `bus_events` command the panel's events lane reads, and `bus_events` in a replay."""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.event_simulator import runner

from .test_websocket_suggest import setup, subscribe, until_final  # noqa: F401 - fixture

DAYS = 5
NOISE = "sensor.unrelated"


def zha(ieee: str, command: str) -> dict[str, Any]:
    return {"device_ieee": ieee, "unique_id": f"{ieee}:1:0x0006", "endpoint_id": 1, "cluster_id": 6, "command": command, "args": [], "params": {}}


@pytest.fixture
async def remote_history(hass: HomeAssistant):
    """Five days ending a day ago: remote aa sends `on` once every evening at no fixed time (so no clock trigger stands in) and `off` at 23:00, remote bb sends commands at random. Returns (start, the `on` presses)."""
    start = dt_util.utcnow().replace(second=0, microsecond=0) - timedelta(days=DAYS + 1)
    hass.states.async_set(NOISE, "0", timestamp=(start - timedelta(seconds=30)).timestamp())
    rng = random.Random(3)
    fired = []
    presses = []
    for day in range(DAYS):
        evening = start + timedelta(days=day, hours=17, minutes=47 * day)
        presses.append(evening)
        fired += [(evening, zha("aa", "on")), (start + timedelta(days=day, hours=23), zha("aa", "off"))]
        fired += [(start + timedelta(days=day, seconds=rng.uniform(0, 86_400)), zha("bb", rng.choice(("on", "off")))) for _ in range(8)]
        fired.append((start + timedelta(days=day, hours=12), {"name": "x"}))  # automation_triggered: bookkeeping
    for at, data in sorted(fired, key=lambda item: item[0]):
        hass.bus.async_fire("automation_triggered" if "name" in data else "zha_event", data, time_fired=at.timestamp())
    await async_wait_recording_done(hass)
    return start, presses


async def test_event_triggers_are_suggested_and_validated_with_the_events_replayed(
    hass: HomeAssistant, setup, remote_history, hass_ws_client  # noqa: F811
):
    start, presses = remote_history
    client = await hass_ws_client(hass)
    sent: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def spy(hass, scenario, **kwargs):
        sent.append(scenario)
        return await run_scenario(hass, scenario, **kwargs)

    request = {
        "type": "event_simulator/suggest/subscribe",
        "entity_ids": [NOISE],
        "start": start.isoformat(),
        "end": (start + timedelta(days=DAYS)).isoformat(),
        "labels": [
            {"start": at.isoformat(), "end": (at + timedelta(minutes=30)).isoformat(), "kind": "fire", "anchor": "start"}
            for at in presses[:3]
        ],
        "options": {"tolerance_before": 120, "tolerance_after": 600},
    }
    with patch.object(runner, "async_run_scenario", spy):
        msg_id = await subscribe(client, request)
        progress, final = await until_final(client, msg_id)

    assert progress[1]["bus_events"] == DAYS * 10
    assert "warning" not in final, final.get("warning")
    best = final["suggestions"][0]
    assert best["exact"] is True
    assert best["config"]["triggers"] == [
        {"trigger": "event", "event_type": "zha_event", "event_data": {"device_ieee": "aa", "command": "on"}}
    ]
    # The sandbox replayed the presses: it fired at every one, the held-out ones too, and nowhere else.
    fired = [dt_util.parse_datetime(t) for t in best["stats"]["fire_times"]]
    assert fired == presses
    # Only the event types the suggestions listen to went to the child.
    [scenario] = sent
    listened = {t["event_type"] for s in final["suggestions"] for t in s["config"]["triggers"] if t["trigger"] == "event"}
    assert {e["event_type"] for e in scenario["bus_events"]} == listened
    assert len(scenario["bus_events"]) <= DAYS * 10


async def test_the_bus_events_command_and_a_replay_with_bus_events(
    hass: HomeAssistant, setup, remote_history, hass_ws_client  # noqa: F811
):
    start, presses = remote_history
    client = await hass_ws_client(hass)
    end = start + timedelta(days=DAYS)
    await client.send_json_auto_id({"type": "event_simulator/bus_events", "start": start.isoformat(), "end": end.isoformat()})
    msg = await client.receive_json()
    assert msg["success"], msg
    events = msg["result"]["bus_events"]
    assert len(events) == DAYS * 10 and {e["event_type"] for e in events} == {"zha_event"}
    assert msg["result"]["dropped_event_types"] == []

    window_end = start + timedelta(days=1)
    await client.send_json_auto_id(
        {
            "type": "event_simulator/simulate",
            "scenario": {
                "start": start.isoformat(),
                "end": window_end.isoformat(),
                "automation": {
                    "id": "remote",
                    "triggers": [{"trigger": "event", "event_type": "zha_event", "event_data": {"device_ieee": "aa", "command": "on"}}],
                    "actions": [],
                },
                "bus_events": [e for e in events if dt_util.parse_datetime(e["at"]) < window_end],
            },
        }
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    assert [dt_util.parse_datetime(run["started"]) for run in msg["result"]["runs"]] == presses[:1]
