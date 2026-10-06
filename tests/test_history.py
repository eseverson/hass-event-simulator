"""Automation lookup and recorder timeline."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components import labs
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


@pytest.fixture(autouse=True)
async def detach_automations(hass: HomeAssistant):
    """Turn every automation off after the test, so a time trigger leaves no timer behind (the harness fails a test on one from 2026.9)."""
    yield
    if hass.services.has_service("automation", "turn_off"):
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": "all"}, blocking=True
        )


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

    # Entities with nothing recorded before the window are left out.
    assert timeline["initial_states"] == {
        "sensor.temp": {
            "state": "19",
            "attributes": {"unit_of_measurement": "°C"},
            "last_changed": (base + timedelta(minutes=5)).isoformat(),
            "last_updated": (base + timedelta(minutes=5)).isoformat(),
        },
        "binary_sensor.door": {
            "state": "off",
            "attributes": {},
            "last_changed": base.isoformat(),
            "last_updated": base.isoformat(),
        },
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



async def test_initial_states_carry_when_the_state_really_changed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
):
    """Not the window start the recorder clamps a start state to (issue #10)."""
    start = dt_util.utcnow().replace(microsecond=0) + timedelta(hours=3)
    freezer.move_to(start - timedelta(hours=2))
    hass.states.async_set("binary_sensor.door", "off")
    hass.states.async_set("sensor.temp", "18", {"unit_of_measurement": "°C"})
    freezer.move_to(start - timedelta(hours=1))
    hass.states.async_set("sensor.temp", "18", {"unit_of_measurement": "°F"})
    freezer.move_to(start + timedelta(minutes=1))
    await async_wait_recording_done(hass)

    timeline = await history.async_get_timeline(
        hass, ["binary_sensor.door", "sensor.temp"], start, start + timedelta(hours=1)
    )

    two_hours_before = (start - timedelta(hours=2)).isoformat()
    assert timeline["initial_states"]["binary_sensor.door"] == {
        "state": "off",
        "attributes": {},
        "last_changed": two_hours_before,
        "last_updated": two_hours_before,
    }
    # attribute-only change: last_updated moves, last_changed stays put
    assert timeline["initial_states"]["sensor.temp"] == {
        "state": "18",
        "attributes": {"unit_of_measurement": "°F"},
        "last_changed": two_hours_before,
        "last_updated": (start - timedelta(hours=1)).isoformat(),
    }
    assert timeline["events"] == []


async def test_single_entity_initial_state_carries_its_timestamps(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
):
    start = dt_util.utcnow().replace(microsecond=0) + timedelta(hours=3)
    freezer.move_to(start - timedelta(hours=2))
    hass.states.async_set("binary_sensor.door", "off")
    freezer.move_to(start + timedelta(minutes=1))
    await async_wait_recording_done(hass)

    timeline = await history.async_get_timeline(
        hass, ["binary_sensor.door"], start, start + timedelta(hours=1)
    )

    assert timeline["initial_states"]["binary_sensor.door"]["last_changed"] == (
        start - timedelta(hours=2)
    ).isoformat()

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
        "preview_features": [],
    }


async def test_core_config_carries_enabled_preview_features(hass: HomeAssistant):
    assert await async_setup_component(hass, "labs", {})
    await labs.async_update_preview_feature(hass, "frontend", "winter_mode", True)
    await labs.async_update_preview_feature(hass, "kitchen_sink", "special_repair", True)
    await labs.async_update_preview_feature(
        hass, "kitchen_sink", "special_repair", False
    )

    assert history.async_core_config(hass)["preview_features"] == [
        ["frontend", "winter_mode"]
    ]


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


async def test_referenced_entities_include_entity_valued_options(hass: HomeAssistant):
    """Entities named as a time, a threshold, a zone or a state to match are timeline inputs too.

    HA's own `referenced_entities` only reads `entity_id`, so without these a time trigger on an input_datetime never fires in a replay.
    """
    config = {
        "id": "entity_options",
        "alias": "Entity options",
        "triggers": [
            {"trigger": "time", "at": ["input_datetime.alarm", "07:00:00"]},
            {"trigger": "time", "at": {"entity_id": "sensor.next_alarm", "offset": "-00:05:00"}},
            {"trigger": "numeric_state", "entity_id": "sensor.temp", "above": "input_number.high"},
        ],
        "conditions": [
            {"condition": "time", "after": "input_datetime.wake", "before": "sensor.bedtime"},
            {"condition": "numeric_state", "entity_id": "sensor.temp", "below": "number.cap"},
            {"condition": "state", "entity_id": "input_select.mode", "state": "input_select.wanted"},
            {"condition": "zone", "entity_id": "person.me", "zone": "zone.work"},
            {"condition": "sun", "after": "sunrise"},
        ],
        "actions": [
            {
                "wait_for_trigger": [{"trigger": "time", "at": "input_datetime.deadline"}],
                "timeout": 60,
            },
            {"action": "notify.notify", "data": {"message": "at 07:00"}},
        ],
    }
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_block_till_done()

    result = history.async_get_automation(hass, "automation.entity_options")

    assert result["referenced_entities"] == [
        "input_datetime.alarm",
        "input_datetime.deadline",
        "input_datetime.wake",
        "input_number.high",
        "input_select.mode",
        "input_select.wanted",
        "number.cap",
        "person.me",
        "sensor.bedtime",
        "sensor.next_alarm",
        "sensor.temp",
        "zone.work",
    ]


async def test_timeline_restores_unrecorded_attributes_from_the_live_state(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
):
    """The recorder drops an entity's `_unrecorded_attributes`; a replay still needs them.

    A time trigger on an input_datetime reads `has_date` / `has_time`, which input_datetime keeps out of the recorder, so without them the trigger fails to attach in the sandbox. They are static capabilities, so the live state's values stand in.
    """
    assert await async_setup_component(
        hass, "input_datetime", {"input_datetime": {"alarm": {"has_time": True}}}
    )
    await hass.async_block_till_done()
    start = dt_util.utcnow().replace(microsecond=0) + timedelta(minutes=1)
    freezer.move_to(start + timedelta(minutes=1))
    await hass.services.async_call(
        "input_datetime",
        "set_datetime",
        {"entity_id": "input_datetime.alarm", "time": "07:30:00"},
        blocking=True,
    )
    hass.states.async_set("sensor.plain", "1")
    end = start + timedelta(minutes=30)
    freezer.move_to(end + timedelta(minutes=1))
    await async_wait_recording_done(hass)

    timeline = await history.async_get_timeline(
        hass, ["input_datetime.alarm", "sensor.plain"], start, end
    )

    initial = timeline["initial_states"]["input_datetime.alarm"]["attributes"]
    assert initial["has_date"] is False
    assert initial["has_time"] is True
    [changed] = [e for e in timeline["events"] if e["entity_id"] == "input_datetime.alarm"]
    assert changed["state"] == "07:30:00"
    assert changed["attributes"]["has_time"] is True
    assert changed["attributes"]["has_date"] is False
    [plain] = [e for e in timeline["events"] if e["entity_id"] == "sensor.plain"]
    assert plain["attributes"] == {}


async def test_referenced_entities_resolve_registry_ids(hass: HomeAssistant):
    """A device trigger or condition names its entity by registry id; the panel gets the entity id, which history and simulate accept."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "door")})
    door = er.async_get(hass).async_get_or_create(
        "binary_sensor", "test", "door", config_entry=entry, device_id=device.id, suggested_object_id="side_door"
    )
    config = {
        "id": "registry_ids",
        "alias": "Registry ids",
        "triggers": [{"trigger": "state", "entity_id": "binary_sensor.motion", "to": "on"}],
        "actions": [
            {
                "if": [
                    {"condition": "device", "device_id": device.id, "domain": "binary_sensor", "entity_id": door.id, "type": "is_open"},
                ],
                "then": [{"action": "notify.notify", "data": {"message": "open"}}],
            },
        ],
    }
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_block_till_done()

    result = history.async_get_automation(hass, "automation.registry_ids")

    assert result["referenced_entities"] == ["binary_sensor.motion", "binary_sensor.side_door"]


async def test_referenced_entities_include_device_trigger_entities(hass: HomeAssistant):
    """A device trigger's entity is in the referenced entities: Home Assistant 2026.9 leaves a registry id out of `referenced_entities` rather than passing it on, so the panel lost the entity that fires the automation."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "door")})
    door = er.async_get(hass).async_get_or_create(
        "binary_sensor", "test", "door", config_entry=entry, device_id=device.id, suggested_object_id="side_door"
    )
    config = {
        "id": "device_trigger",
        "alias": "Device trigger",
        "triggers": [
            {"trigger": "device", "device_id": device.id, "domain": "binary_sensor", "entity_id": door.id, "type": "opened"},
        ],
        "actions": [{"action": "notify.notify", "data": {"message": "open"}}],
    }
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_block_till_done()

    result = history.async_get_automation(hass, "automation.device_trigger")

    assert result["referenced_entities"] == ["binary_sensor.side_door"]


async def test_bus_events_in_the_window_without_home_assistant_bookkeeping(hass: HomeAssistant):
    """#35: the recorder's events of the window, oldest first, with their data; Home Assistant's own (automation_triggered, call_service, service_registered, *_registry_updated, ...) left out."""
    start = dt_util.utcnow().replace(microsecond=0) - timedelta(days=2)
    fired = [
        (start - timedelta(minutes=5), "zha_event", {"command": "before"}),
        (start + timedelta(minutes=10), "zha_event", {"device_ieee": "aa", "command": "on", "args": {"k": 1}}),
        (start + timedelta(minutes=20), "tag_scanned", {"tag_id": "front"}),
        (start + timedelta(minutes=30), "automation_triggered", {"name": "x"}),
        (start + timedelta(minutes=31), "call_service", {"domain": "light"}),
        (start + timedelta(minutes=32), "entity_registry_updated", {"action": "update"}),
        (start + timedelta(minutes=33), "recorder_5min_statistics_generated", {}),
        (start + timedelta(minutes=34), "script_reloaded", {}),
        (start + timedelta(minutes=40), "zha_event", {"device_ieee": "aa", "command": "off"}),
        (start + timedelta(hours=2), "zha_event", {"command": "after"}),
    ]
    for at, event_type, data in fired:
        hass.bus.async_fire(event_type, data, time_fired=at.timestamp())
    await async_wait_recording_done(hass)

    found = await history.async_get_bus_events(hass, start, start + timedelta(hours=1))
    assert found["dropped_event_types"] == []
    assert [(e["event_type"], e["data"]) for e in found["bus_events"]] == [
        ("zha_event", {"device_ieee": "aa", "command": "on", "args": {"k": 1}}),
        ("tag_scanned", {"tag_id": "front"}),
        ("zha_event", {"device_ieee": "aa", "command": "off"}),
    ]
    assert dt_util.parse_datetime(found["bus_events"][0]["at"]) == start + timedelta(minutes=10)

    # Over the cap, the busiest types go first.
    capped = await history.async_get_bus_events(hass, start, start + timedelta(hours=1), max_events=1)
    assert capped["dropped_event_types"] == ["zha_event"]
    assert [e["event_type"] for e in capped["bus_events"]] == ["tag_scanned"]
    # Only some types.
    only = await history.async_get_bus_events(hass, start, start + timedelta(hours=1), event_types=["tag_scanned", "call_service"])
    assert [e["event_type"] for e in only["bus_events"]] == ["tag_scanned"]
