"""#12: registry entries snapshotted on the live side replay in the child.

Registries are built here with HA's own APIs, snapshotted with `history.async_registry_snapshot` (what the simulate command sends), and replayed through a real child process, so both halves of the format are covered together.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
from custom_components.event_simulator.sandbox import engine

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


async def test_stores_carry_every_key_the_live_registry_saves(
    hass: HomeAssistant, registries, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A registry at its current storage version reads every key of its save data with no migration to fill them in.

    Home Assistant 2026.9 added `settings` to the entity registry's store and `child_devices` to the device registry's; a store written with only the entry lists failed to load (`KeyError: 'child_devices'`), so every scenario with a registry snapshot came back `invalid_scenario`. Stand-ins for such keys here, so the check holds on any version.
    """
    added = {
        er.EntityRegistry: {"settings": {"entity_id_parts": None}, "future_key": {"x": 1}},
        dr.DeviceRegistry: {"child_devices": []},
    }
    for registry_class, extra in added.items():
        original = registry_class._data_to_save
        monkeypatch.setattr(
            registry_class, "_data_to_save", lambda self, original=original, extra=extra: {**original(self), **extra}
        )

    snapshot = history.async_registry_snapshot(hass, {}, ["binary_sensor.hall_motion"])
    assert snapshot["store_data"] == {
        "entities": {"settings": {"entity_id_parts": None}, "future_key": {"x": 1}},
        "devices": {"child_devices": []},
    }

    registry = engine._parse_registry(json.loads(json.dumps(snapshot)), [])
    engine._write_registry_storage(str(tmp_path), registry, utc("2026-10-04T00:00:00+00:00"))
    entities = json.loads((tmp_path / ".storage" / er.STORAGE_KEY).read_text())["data"]
    devices = json.loads((tmp_path / ".storage" / dr.STORAGE_KEY).read_text())["data"]
    assert entities["settings"] == {"entity_id_parts": None}
    assert entities["future_key"] == {"x": 1}
    assert [entity["entity_id"] for entity in entities["entities"]] == ["binary_sensor.hall_motion"]
    assert entities["deleted_entities"] == []
    assert devices["child_devices"] == []
    assert [device["id"] for device in devices["devices"]] == [registries["sensor"].id]


@pytest.mark.skipif(not hasattr(dr, "ChildDeviceEntry"), reason="child devices arrived in Home Assistant 2026.9")
async def test_child_device_replays_with_its_parent(hass: HomeAssistant, registries):
    """A child device travels in the device store's `child_devices`, its parent along with it, and templates in the replay see both."""
    dev_reg = dr.async_get(hass)
    hue = registries["motion"].config_entry_id
    child = dev_reg.async_get_or_create_child(
        config_entry_id=hue, identifiers={("hue", "sensor1-porch")}, name="Porch sensor", parent_device_id=registries["sensor"].id
    )
    porch = er.async_get(hass).async_get_or_create(
        "binary_sensor", "hue", "porch1", config_entry=MockConfigEntry(domain="hue", entry_id=hue), device_id=child.id, suggested_object_id="porch_motion"
    )
    snapshot = history.async_registry_snapshot(hass, {}, [porch.entity_id])
    assert [device["id"] for device in snapshot["devices"]] == [registries["sensor"].id]
    assert [device["id"] for device in snapshot["store_data"]["devices"]["child_devices"]] == [child.id]

    result = await replay(
        hass,
        {
            "triggers": [{"trigger": "state", "entity_id": porch.entity_id, "to": "on"}],
            "actions": [{"action": "notify.notify", "data": {"message": "{{ device_attr(device_id('binary_sensor.porch_motion'), 'name') }}"}}],
        },
        [porch.entity_id],
        initial_states={porch.entity_id: {"state": "off", "attributes": MOTION}},
        events=[{**event, "entity_id": porch.entity_id} for event in MOTION_EVENTS],
    )
    assert result["error"] is None, result["error"]
    assert result["service_calls"][0]["data"]["message"] == "Porch sensor"


async def test_store_data_must_be_objects_per_registry():
    for bad in ("x", {"entities": []}, {"nonsense": {}}):
        with pytest.raises(engine.ScenarioError, match="store_data"):
            engine._parse_registry({"store_data": bad}, [])
