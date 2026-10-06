"""Rate-of-change suggestions end to end (#42): recorded history, the miner proposing a Trend or Derivative helper, and the sandbox validating it with the helper set up.

The shower case of test_suggest_trend.py, written to the recorder: a level threshold cannot tell the showers from humid days, the humidity's rise can.
"""

from __future__ import annotations

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

from .test_suggest_trend import DAYS, HUM, PERCENT, SHOWERS, T0, Sensor, humidity
from .test_websocket_suggest import SUGGEST, setup, subscribe, until_final  # noqa: F401


@pytest.fixture
async def shower_history(hass: HomeAssistant):
    """Ten days of bathroom humidity, ending a day ago. Returns (start, shower times)."""
    start = dt_util.utcnow().replace(second=0, microsecond=0) - timedelta(days=DAYS + 1)
    sensor = Sensor(HUM, PERCENT, 1, 0)
    sensor.run(humidity, DAYS)
    hass.states.async_set(HUM, "50", PERCENT, timestamp=(start - timedelta(minutes=5)).timestamp())
    shift = start.timestamp() - T0
    for event in sensor.events:
        at = dt_util.parse_datetime(event["at"]).timestamp() + shift
        hass.states.async_set(HUM, event["state"], event["attributes"], timestamp=at)
    await async_wait_recording_done(hass)
    showers = [start + timedelta(days=day, hours=hour) for day, hour in SHOWERS]
    return start, showers


async def test_a_trend_suggestion_validates_exactly_with_its_helper_set_up(hass: HomeAssistant, setup, shower_history, hass_ws_client):  # noqa: F811
    start, showers = shower_history
    # An entity that already has the helper's default id: the suggestion's helper takes another.
    hass.states.async_set("binary_sensor.bathroom_humidity_rising", "off")
    client = await hass_ws_client(hass)
    sent: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def spy(hass, scenario, **kwargs):
        sent.append(scenario)
        return await run_scenario(hass, scenario, **kwargs)

    message = {
        "type": SUGGEST,
        "entity_ids": [HUM],
        "start": start.isoformat(),
        "end": (start + timedelta(days=DAYS)).isoformat(),
        "labels": [{"start": at.isoformat(), "end": (at + timedelta(minutes=20)).isoformat(), "kind": "fire", "anchor": "start"} for at in showers],
        "options": {"tolerance_before": 120, "tolerance_after": 600},
    }
    with patch.object(runner, "async_run_scenario", spy):
        msg_id = await subscribe(client, message)
        _, final = await until_final(client, msg_id)

    assert "warning" not in final, final.get("warning")
    best = final["suggestions"][0]
    assert best["trigger_kinds"] == ["trend"] and best["exact"] is True
    assert (best["stats"]["anchors_hit"], best["stats"]["anchors_total"]) == (len(SHOWERS), len(SHOWERS))
    assert not best["stats"]["outside_fires"]
    [helper] = best["helpers"]
    assert helper["entity_id"] != "binary_sensor.bathroom_humidity_rising"
    assert best["config"]["triggers"][0]["entity_id"] == helper["entity_id"]
    assert final["kind_counts"]["trend"] >= 1

    # The validation child set up the helpers of every replayed suggestion (variants are not replayed), once each.
    [scenario] = sent
    helpers = scenario["helpers"]
    ids = [h["entity_id"] for h in helpers]
    assert len(ids) == len(set(ids))
    wanted = {h["entity_id"]: h for s in final["suggestions"] for h in s["helpers"]}
    assert {h["entity_id"]: h["options"] for h in helpers} == {k: v["options"] for k, v in wanted.items()}
    # The helpers' source is on the replayed timeline.
    assert HUM in scenario["initial_states"] or any(e["entity_id"] == HUM for e in scenario["events"])
