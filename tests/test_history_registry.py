"""`history.async_registry_snapshot`: which registry entries a scenario carries (#12)."""

from __future__ import annotations

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


@pytest.fixture
def home(hass: HomeAssistant) -> dict[str, str]:
    entry = MockConfigEntry(domain="hue", title="Hue bridge", data={"api_key": "secret"})
    entry.add_to_hass(hass)
    other = MockConfigEntry(domain="met", title="Weather")
    other.add_to_hass(hass)
    floor = fr.async_get(hass).async_create("Upstairs")
    fr.async_get(hass).async_create("Basement")
    label = lr.async_get(hass).async_create("Night")
    lr.async_get(hass).async_create("Unused")
    area_reg = ar.async_get(hass)
    bedroom = area_reg.async_create("Bedroom", floor_id=floor.floor_id, labels={label.label_id})
    office = area_reg.async_create("Office")
    area_reg.async_create("Garage")
    dev_reg = dr.async_get(hass)
    lamp_dev = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hue", "lamp")}, name="Lamp")
    dev_reg.async_update_device(lamp_dev.id, area_id=bedroom.id)
    desk_dev = dev_reg.async_get_or_create(config_entry_id=entry.entry_id, identifiers={("hue", "desk")}, name="Desk")
    dev_reg.async_update_device(desk_dev.id, area_id=office.id, labels={label.label_id})
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create("light", "hue", "lamp", config_entry=entry, device_id=lamp_dev.id, suggested_object_id="lamp")
    ent_reg.async_get_or_create("light", "hue", "desk", config_entry=entry, device_id=desk_dev.id, suggested_object_id="desk")
    door = ent_reg.async_get_or_create("binary_sensor", "hue", "door", config_entry=entry, suggested_object_id="door")
    ent_reg.async_update_entity(door.entity_id, labels={label.label_id})
    ent_reg.async_get_or_create("sensor", "met", "temp", config_entry=other, suggested_object_id="outside")
    return {"lamp_dev": lamp_dev.id, "desk_dev": desk_dev.id, "door_uuid": door.id, "hue": entry.entry_id}


def ids(snapshot: dict, kind: str, key: str) -> list[str]:
    return sorted(item[key] for item in snapshot[kind])


async def test_snapshot_follows_entities_to_devices_areas_floors_labels(hass: HomeAssistant, home):
    snapshot = history.async_registry_snapshot(hass, {"triggers": []}, ["light.lamp", "sensor.nowhere"])

    assert ids(snapshot, "entities", "entity_id") == ["light.lamp"]
    assert ids(snapshot, "devices", "id") == [home["lamp_dev"]]
    assert ids(snapshot, "areas", "id") == ["bedroom"]
    assert ids(snapshot, "floors", "floor_id") == ["upstairs"]
    assert ids(snapshot, "labels", "label_id") == ["night"]
    # Config entries travel as stubs: never their data or options.
    assert snapshot["config_entries"] == [{"entry_id": home["hue"], "domain": "hue", "title": "Hue bridge"}]
    assert set(snapshot["versions"]) == {"entities", "devices", "areas", "floors", "labels"}
    assert "truncated" not in snapshot


async def test_snapshot_expands_targets_and_template_references(hass: HomeAssistant, home):
    config = {
        "triggers": [{"trigger": "device", "domain": "binary_sensor", "device_id": home["desk_dev"], "entity_id": home["door_uuid"], "type": "opened"}],
        "actions": [
            {"action": "light.turn_on", "target": {"area_id": ["bedroom"], "label_id": "night"}},
            {"action": "notify.notify", "data": {"message": "{{ area_entities('Office') }} {{ floor_areas('upstairs') }}"}},
        ],
    }
    snapshot = history.async_registry_snapshot(hass, config, [])

    # Bedroom via area_id (its lamp through the device), Office by name in a template (desk via device),
    # the door by registry id, and everything labelled `night`.
    assert ids(snapshot, "entities", "entity_id") == ["binary_sensor.door", "light.desk", "light.lamp"]
    assert ids(snapshot, "devices", "id") == sorted([home["lamp_dev"], home["desk_dev"]])
    assert ids(snapshot, "areas", "id") == ["bedroom", "office"]
    assert ids(snapshot, "floors", "floor_id") == ["upstairs"]
    assert ids(snapshot, "labels", "label_id") == ["night"]


async def test_snapshot_is_size_capped(hass: HomeAssistant, home, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(history, "MAX_SNAPSHOT_ENTITIES", 2)
    snapshot = history.async_registry_snapshot(hass, {}, ["sensor.outside", "light.lamp", "light.desk", "binary_sensor.door"])

    # Directly named entities come first, in the order given.
    assert [item["entity_id"] for item in snapshot["entities"]] == ["sensor.outside", "light.lamp"]
    assert snapshot["truncated"] == {"entities": 2}


async def test_empty_registries_give_an_empty_snapshot(hass: HomeAssistant):
    snapshot = history.async_registry_snapshot(hass, {"actions": [{"action": "x.y", "target": {"area_id": "nowhere"}}]}, ["light.x"])
    assert {kind: snapshot[kind] for kind in ("entities", "devices", "areas", "floors", "labels", "config_entries")} == {
        "entities": [], "devices": [], "areas": [], "floors": [], "labels": [], "config_entries": []
    }
