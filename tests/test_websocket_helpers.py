"""#43: a replay's `helpers` reach the sandbox through `simulate`, and the helper's states come back in the result."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from .test_websocket import SIMULATE, _call, setup  # noqa: F401

TEMP = "sensor.temp"
RISING = "binary_sensor.temp_rising"
TREND = {"domain": "trend", "entity_id": RISING, "title": "Temp rising", "options": {"entity_id": TEMP, "min_gradient": 0.001}}
RISE = {
    **SIMULATE,
    "automation": {
        "id": "fan",
        "alias": "Fan",
        "triggers": [{"trigger": "state", "entity_id": RISING, "to": "on"}],
        "actions": [{"action": "fan.turn_on", "target": {"entity_id": "fan.attic"}}],
    },
    "initial_states": {TEMP: {"state": "20", "attributes": {"unit_of_measurement": "°C"}}},
    "events": [
        {"at": "2026-10-04T01:00:00+00:00", "entity_id": TEMP, "state": "20.5", "attributes": {"unit_of_measurement": "°C"}},
        {"at": "2026-10-04T01:05:00+00:00", "entity_id": TEMP, "state": "21", "attributes": {"unit_of_measurement": "°C"}},
    ],
    "helpers": [TREND],
}


async def test_simulate_sets_up_the_helpers(hass: HomeAssistant, setup, hass_ws_client):  # noqa: F811
    client = await hass_ws_client(hass)

    msg = await _call(client, {"type": "event_simulator/simulate", "scenario": RISE})

    assert msg["success"], msg
    result = msg["result"]
    # 20 at the start, 20.5 an hour later: 0.5 / 3600 s is under min_gradient; 21 five minutes after that is over it.
    assert [(run["started"][:19], run["outcome"]) for run in result["runs"]] == [("2026-10-04T01:05:00", "fired")]
    assert result["helper_states"] == {
        RISING: [
            # One sample (the start's) is not enough for a gradient.
            {"at": "2026-10-04T00:00:00+00:00", "state": "unknown"},
            {"at": "2026-10-04T01:00:00+00:00", "state": "off"},
            {"at": "2026-10-04T01:05:00+00:00", "state": "on"},
        ]
    }


@pytest.mark.parametrize(
    ("helpers", "code"),
    [
        ([{**TREND, "domain": "statistics"}], "invalid_format"),
        ([{**TREND, "entity_id": "not an entity"}], "invalid_format"),
        ({"domain": "trend"}, "invalid_format"),
        # The options are the child's to check.
        ([{**TREND, "options": {"entity_id": TEMP, "min_gradient": "steep"}}], "invalid_scenario"),
    ],
)
async def test_simulate_checks_the_helpers(hass: HomeAssistant, setup, hass_ws_client, helpers, code):  # noqa: F811
    client = await hass_ws_client(hass)

    msg = await _call(client, {"type": "event_simulator/simulate", "scenario": {**RISE, "helpers": helpers}})

    assert not msg["success"]
    assert msg["error"]["code"] == code, msg["error"]
