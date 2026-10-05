"""Automation lookup and recorder timeline."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.event_simulator import history
from custom_components.event_simulator.history import SimulatorError

TEMPLATE_CONDITION = (
    "{{ is_state('input_boolean.guest_mode', 'on') "
    "and states('sensor.outdoor_temp') | float(0) > 20 }}"
)

AUTOMATIONS = [
    {
        "id": "door_light",
        "alias": "Door light",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"}],
        "conditions": [
            {"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"},
            {"condition": "template", "value_template": TEMPLATE_CONDITION},
        ],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.porch"}}],
    },
    {
        "alias": "No id",
        "triggers": [{"trigger": "time", "at": "07:00:00"}],
        "actions": [{"action": "notify.notify", "data": {"message": "{{ trigger.now }}"}}],
    },
]


@pytest.fixture
async def automations(hass: HomeAssistant):
    hass.states.async_set("input_boolean.guest_mode", "off")
    hass.states.async_set("sensor.outdoor_temp", "18")
    assert await async_setup_component(hass, "automation", {"automation": AUTOMATIONS})
    await hass.async_block_till_done()


async def test_list_automations(hass: HomeAssistant, automations):
    assert history.async_list_automations(hass) == [
        {"entity_id": "automation.door_light", "alias": "Door light", "id": "door_light"},
        {"entity_id": "automation.no_id", "alias": "No id", "id": None},
    ]


async def test_list_automations_before_the_component_loads(hass: HomeAssistant):
    assert history.async_list_automations(hass) == []


async def test_automation_returns_its_raw_config(hass: HomeAssistant, automations):
    result = history.async_get_automation(hass, "automation.door_light")

    assert result["config"] == AUTOMATIONS[0]


async def test_referenced_entities_include_template_entities(
    hass: HomeAssistant, automations
):
    result = history.async_get_automation(hass, "automation.door_light")

    # sensor.outdoor_temp sits behind a short-circuited `and`, so rendering alone
    # never touches it; the literal-id pass has to find it.
    assert result["referenced_entities"] == [
        "binary_sensor.door",
        "input_boolean.guest_mode",
        "light.porch",
        "sensor.outdoor_temp",
        "sun.sun",
    ]


async def test_template_variables_are_not_mistaken_for_entities(
    hass: HomeAssistant, automations
):
    result = history.async_get_automation(hass, "automation.no_id")

    assert result["referenced_entities"] == []


async def test_unknown_automation(hass: HomeAssistant, automations):
    with pytest.raises(SimulatorError) as err:
        history.async_get_automation(hass, "automation.nope")

    assert err.value.code == "not_found"


async def test_timeline_has_start_state_and_changes_in_window(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
):
    base = dt_util.utcnow().replace(microsecond=0) + timedelta(minutes=1)
    freezer.move_to(base)
    hass.states.async_set("sensor.temp", "18", {"unit_of_measurement": "°C"})
    hass.states.async_set("binary_sensor.door", "off")
    freezer.move_to(base + timedelta(minutes=5))
    hass.states.async_set("sensor.temp", "19", {"unit_of_measurement": "°C"})
    start = base + timedelta(minutes=10)
    freezer.move_to(start + timedelta(minutes=1))
    hass.states.async_set("binary_sensor.door", "on", {"device_class": "door"})
    freezer.move_to(start + timedelta(minutes=2))
    hass.states.async_set("sensor.temp", "19", {"unit_of_measurement": "°F"})
    end = start + timedelta(minutes=30)
    freezer.move_to(end + timedelta(minutes=1))
    hass.states.async_set("binary_sensor.door", "off")
    await async_wait_recording_done(hass)

    timeline = await history.async_get_timeline(
        hass, ["sensor.temp", "binary_sensor.door", "sensor.never_recorded"], start, end
    )

    # The recorder reports a start state stamped at the window start, not when it
    # really changed, so initial states carry only what the scenario format needs.
    assert timeline["initial_states"] == {
        "sensor.temp": {"state": "19", "attributes": {"unit_of_measurement": "°C"}},
        "binary_sensor.door": {"state": "off", "attributes": {}},
    }
    at_door = (start + timedelta(minutes=1)).isoformat()
    at_temp = (start + timedelta(minutes=2)).isoformat()
    assert timeline["events"] == [
        {
            "at": at_door,
            "entity_id": "binary_sensor.door",
            "state": "on",
            "attributes": {"device_class": "door"},
            "last_changed": at_door,
            "last_updated": at_door,
        },
        {
            # attribute-only change: last_changed stays at the earlier value
            "at": at_temp,
            "entity_id": "sensor.temp",
            "state": "19",
            "attributes": {"unit_of_measurement": "°F"},
            "last_changed": (base + timedelta(minutes=5)).isoformat(),
            "last_updated": at_temp,
        },
    ]


async def test_timeline_with_no_entities_is_empty(hass: HomeAssistant):
    now = dt_util.utcnow()
    timeline = await history.async_get_timeline(hass, [], now - timedelta(hours=1), now)

    assert timeline == {"initial_states": {}, "events": []}


@pytest.mark.parametrize(
    ("entity_count", "span", "code"),
    [
        (1, timedelta(0), "invalid_window"),
        (1, -timedelta(hours=1), "invalid_window"),
        (1, history.MAX_WINDOW + timedelta(seconds=1), "window_too_long"),
        (history.MAX_ENTITIES + 1, timedelta(hours=1), "too_many_entities"),
    ],
)
async def test_timeline_limits(hass: HomeAssistant, entity_count, span, code):
    start = dt_util.utcnow() - timedelta(days=30)
    entity_ids = [f"sensor.s{i}" for i in range(entity_count)]

    with pytest.raises(SimulatorError) as err:
        await history.async_get_timeline(hass, entity_ids, start, start + span)

    assert err.value.code == code


async def test_core_config(hass: HomeAssistant):
    await hass.config.async_update(
        time_zone="America/Chicago",
        latitude=41.9,
        longitude=-87.6,
        elevation=180,
        unit_system="us_customary",
    )
    assert hass.config.units is US_CUSTOMARY_SYSTEM

    assert history.async_core_config(hass) == {
        "time_zone": "America/Chicago",
        "latitude": 41.9,
        "longitude": -87.6,
        "elevation": 180,
        "unit_system": "us_customary",
    }


async def test_timeline_refuses_too_many_events(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(history, "MAX_EVENTS", 2)
    start = dt_util.utcnow().replace(microsecond=0) + timedelta(minutes=1)
    for minute, value in enumerate(("1", "2", "3"), start=1):
        freezer.move_to(start + timedelta(minutes=minute))
        hass.states.async_set("sensor.busy", value)
    await async_wait_recording_done(hass)

    with pytest.raises(SimulatorError) as err:
        await history.async_get_timeline(
            hass, ["sensor.busy"], start, start + timedelta(hours=1)
        )

    assert err.value.code == "too_many_events"
