"""#12: registry entries snapshotted on the live side replay in the child.

Registries are built here with HA's own APIs, snapshotted with `history.async_registry_snapshot` (what the simulate command sends), and replayed through a real child process, so both halves of the format are covered together.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)

from custom_components.event_simulator import history

from .test_engine import calls, run_child, summary, utc
from .test_engine_fidelity import scenario

TEMPLATE = (
    "{{ area_entities('Kitchen') | sort | join(',') }}"
    "|{{ device_attr(device_id('binary_sensor.hall_motion'), 'model') }}"
    "|{{ label_entities('security') | sort | join(',') }}"
    "|{{ floor_areas('ground') | join(',') }}"
    "|{{ area_name('binary_sensor.hall_motion') }}"
    "|{{ config_entry_attr(config_entry_id('binary_sensor.hall_motion'), 'title') }}"
)
MOTION = {"device_class": "motion"}
MOTION_EVENTS = [
    {"at": "2026-10-04T01:00:00+00:00", "entity_id": "binary_sensor.hall_motion", "state": "on", "attributes": MOTION},
    {"at": "2026-10-04T01:05:00+00:00", "entity_id": "binary_sensor.hall_motion", "state": "off", "attributes": MOTION},
]


@pytest.fixture
async def registries(hass: HomeAssistant) -> dict[str, Any]:
    """A small home: a hue motion sensor in the hall, kitchen entities on a floor and a label, and a zha remote."""
    hue = MockConfigEntry(domain="hue", title="Hue bridge")
    hue.add_to_hass(hass)
    zha = MockConfigEntry(domain="zha", title="Zigbee")
    zha.add_to_hass(hass)
    floor = fr.async_get(hass).async_create("Ground")
    label = lr.async_get(hass).async_create("Security")
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen", floor_id=floor.floor_id)
    hall = area_reg.async_create("Hall")
    area_reg.async_create("Attic")

    dev_reg = dr.async_get(hass)
    sensor = dev_reg.async_get_or_create(
        config_entry_id=hue.entry_id, identifiers={("hue", "sensor1")}, name="Hall sensor", manufacturer="Signify", model="SML001"
    )
    dev_reg.async_update_device(sensor.id, area_id=hall.id)
    fridge = dev_reg.async_get_or_create(config_entry_id=hue.entry_id, identifiers={("hue", "plug1")}, name="Fridge plug")
    dev_reg.async_update_device(fridge.id, area_id=kitchen.id)
    remote = dev_reg.async_get_or_create(config_entry_id=zha.entry_id, identifiers={("zha", "remote1")}, name="Remote")

    ent_reg = er.async_get(hass)
    motion = ent_reg.async_get_or_create(
        "binary_sensor", "hue", "motion1", config_entry=hue, device_id=sensor.id, original_device_class="motion", suggested_object_id="hall_motion"
    )
    ceiling = ent_reg.async_get_or_create("light", "hue", "light1", config_entry=hue, suggested_object_id="kitchen_ceiling")
    ent_reg.async_update_entity(ceiling.entity_id, area_id=kitchen.id, labels={label.label_id})
    window = ent_reg.async_get_or_create("binary_sensor", "hue", "window1", config_entry=hue, suggested_object_id="kitchen_window")
    ent_reg.async_update_entity(window.entity_id, area_id=kitchen.id, labels={label.label_id})
    # In the kitchen through its device only.
    ent_reg.async_get_or_create("switch", "hue", "plug1", config_entry=hue, device_id=fridge.id, suggested_object_id="fridge")
    ent_reg.async_get_or_create("sensor", "hue", "unrelated", config_entry=hue, suggested_object_id="unrelated")
    assert motion.entity_id == "binary_sensor.hall_motion"
    return {"sensor": sensor, "motion": motion, "remote": remote, "label": label}


async def replay(hass: HomeAssistant, automation: dict[str, Any], entity_ids: list[str], **extra: Any) -> dict[str, Any]:
    built = scenario(automation, **extra)
    built["registry"] = history.async_registry_snapshot(hass, built["automation"], entity_ids)
    code, result, stderr = await asyncio.get_running_loop().run_in_executor(None, run_child, built)
    assert code == 0, stderr[-4000:]
    return result


async def test_registry_templates_see_the_snapshot(hass: HomeAssistant, registries):
    result = await replay(
        hass,
        {
            "triggers": [{"trigger": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}],
            "actions": [{"action": "notify.notify", "data": {"message": TEMPLATE}}],
        },
        ["binary_sensor.hall_motion"],
        initial_states={"binary_sensor.hall_motion": {"state": "off", "attributes": MOTION}},
        events=MOTION_EVENTS,
    )
    assert result["error"] is None, result["error"]
    assert summary(result) == [(utc("2026-10-04T01:00:00+00:00"), "fired", None)]
    assert result["service_calls"][0]["data"]["message"] == (
        "binary_sensor.kitchen_window,light.kitchen_ceiling,switch.fridge"
        "|SML001"
        "|binary_sensor.kitchen_window,light.kitchen_ceiling"
        "|kitchen"
        "|Hall"
        "|Hue bridge"
    )
    assert "registry_truncated" not in [warning["code"] for warning in result["warnings"]]


async def test_binary_sensor_device_trigger_replays(hass: HomeAssistant, registries):
    result = await replay(
        hass,
        {
            "triggers": [
                {
                    "trigger": "device",
                    "domain": "binary_sensor",
                    "device_id": registries["sensor"].id,
                    "entity_id": registries["motion"].id,
                    "type": "motion",
                }
            ],
            "conditions": [
                {
                    "condition": "device",
                    "domain": "binary_sensor",
                    "device_id": registries["sensor"].id,
                    "entity_id": registries["motion"].id,
                    "type": "is_motion",
                }
            ],
            "actions": [{"action": "light.turn_on", "target": {"area_id": "kitchen"}}],
        },
        ["binary_sensor.hall_motion"],
        initial_states={"binary_sensor.hall_motion": {"state": "off", "attributes": MOTION}},
        events=MOTION_EVENTS,
    )
    assert result["error"] is None, result["error"]
    assert summary(result) == [(utc("2026-10-04T01:00:00+00:00"), "fired", None)]
    assert calls(result) == [(utc("2026-10-04T01:00:00+00:00"), "light.turn_on")]
    assert result["runs"][0]["trigger"]["platform"] == "device"


async def test_integration_specific_device_trigger_is_refused_by_name(hass: HomeAssistant, registries):
    result = await replay(
        hass,
        {
            "triggers": [
                {"trigger": "device", "domain": "zha", "device_id": registries["remote"].id, "type": "remote_button_short_press", "subtype": "turn_on"}
            ],
            "actions": [{"action": "light.toggle", "target": {"entity_id": "light.kitchen_ceiling"}}],
        },
        [],
    )
    assert result["error"]["code"] == "unsupported_automation"
    message = result["error"]["message"]
    assert "zha" in message
    assert "triggers/0" in message
