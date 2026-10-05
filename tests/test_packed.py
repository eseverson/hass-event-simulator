"""Packed scenario events: what the panel sends for simulate and save, and what the store keeps."""

from __future__ import annotations

import pytest
import voluptuous as vol

from custom_components.event_simulator.packed import pack_events, unpack_events

T = "2026-10-04T00:00:{:02d}.000123+00:00"
POWER = {"unit_of_measurement": "W", "device_class": "power"}

EVENTS = [
    {"at": T.format(0), "entity_id": "sensor.power", "state": "300", "attributes": POWER, "last_changed": T.format(0), "last_updated": T.format(0)},
    {"at": T.format(20), "entity_id": "sensor.power", "state": "300", "attributes": {**POWER, "icon": "mdi:flash"}, "last_changed": T.format(0), "last_updated": T.format(20)},
    {"at": T.format(40), "entity_id": "binary_sensor.door", "state": "on", "attributes": {}, "edited": True},
    {"at": T.format(50), "entity_id": "binary_sensor.door", "state": "off", "attributes": {}, "edited": False, "note": {"x": 1}},
]


def test_pack_is_lossless_and_shares_ids_and_attributes():
    packed = pack_events(EVENTS)

    assert packed["entity_ids"] == ["sensor.power", "binary_sensor.door"]
    assert packed["attributes"] == [POWER, {**POWER, "icon": "mdi:flash"}, {}]
    assert packed["rows"][0] == [T.format(0), 0, "300", 0, 3]
    assert packed["rows"][1] == [T.format(20), 0, "300", 1, 1, {"last_changed": T.format(0)}]
    assert packed["rows"][2] == [T.format(40), 1, "on", 2, 4]
    assert unpack_events(packed) == EVENTS


def test_unpack_matches_the_panel_format():
    """The rows the panel's packEvents writes (wire.test.mjs) unpack to the same events."""
    packed = {
        "entity_ids": ["a.a"],
        "attributes": [{}],
        "rows": [[T.format(1), 0, "on", 0], [T.format(2), 0, "off", 0, 7]],
    }

    assert unpack_events(packed) == [
        {"at": T.format(1), "entity_id": "a.a", "state": "on", "attributes": {}},
        {"at": T.format(2), "entity_id": "a.a", "state": "off", "attributes": {}, "last_changed": T.format(2), "last_updated": T.format(2), "edited": True},
    ]


@pytest.mark.parametrize(
    "packed",
    [
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 1, "on", 0]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 0, "on", -1]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 0, "on"]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 0, 5, 0]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), True, "on", 0]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 0, "on", 0, "x"]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 0, "on", 0, 0, []]]},
        {"entity_ids": ["a.a"], "attributes": [{}], "rows": [[T.format(1), 0, "on", 0, 0, {}, 1]]},
        {"entity_ids": ["a.a"], "attributes": ["x"], "rows": []},
        {"entity_ids": ["a.a"]},
        "rows",
    ],
)
def test_unpack_refuses_a_malformed_packing(packed):
    with pytest.raises(vol.Invalid):
        unpack_events(packed)
