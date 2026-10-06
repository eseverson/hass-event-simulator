"""Named scenarios persisted with Home Assistant's storage helper."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant

from custom_components.event_simulator.history import SimulatorError
from custom_components.event_simulator.packed import pack_events, unpack_events
from custom_components.event_simulator.store import (
    MAX_EVENTS,
    MAX_LABELS,
    MAX_SCENARIOS,
    MAX_STORED_EVENTS,
    SCENARIO_FIELDS,
    STORAGE_KEY,
    STORAGE_VERSION,
    ScenarioStore,
)

SCENARIO: dict[str, Any] = {
    "name": "Door at night",
    "automation_entity_id": "automation.door_light",
    "automation": {"id": "door_light", "alias": "Door light", "triggers": []},
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


@pytest.fixture
async def store(hass: HomeAssistant) -> ScenarioStore:
    store = ScenarioStore(hass)
    await store.async_load()
    return store


async def test_save_creates_a_scenario_with_an_id_and_timestamps(store: ScenarioStore):
    saved = await store.async_save_scenario(dict(SCENARIO))

    assert saved["scenario_id"]
    assert saved["created"] == saved["updated"]
    assert store.get(saved["scenario_id"]) == {
        **SCENARIO,
        "events": pack_events(SCENARIO["events"]),
        "scenario_id": saved["scenario_id"],
        "created": saved["created"],
        "updated": saved["updated"],
    }


async def test_get_returns_the_full_scenario_with_its_events_packed(store: ScenarioStore):
    saved = await store.async_save_scenario(dict(SCENARIO))

    scenario = store.get(saved["scenario_id"])

    assert unpack_events(scenario["events"]) == SCENARIO["events"]
    assert scenario["initial_states"] == SCENARIO["initial_states"]
    assert scenario["automation"] == SCENARIO["automation"]


async def test_get_unknown_raises_not_found(store: ScenarioStore):
    with pytest.raises(SimulatorError) as err:
        store.get("nope")
    assert err.value.code == "not_found"


async def test_summaries_leave_out_the_timeline(store: ScenarioStore):
    saved = await store.async_save_scenario(dict(SCENARIO))

    assert store.summaries() == [
        {
            "scenario_id": saved["scenario_id"],
            "name": "Door at night",
            "automation_entity_id": "automation.door_light",
            "alias": "Door light",
            "start": SCENARIO["start"],
            "end": SCENARIO["end"],
            "entity_ids": ["binary_sensor.door"],
            "event_count": 1,
            "created": saved["created"],
            "updated": saved["updated"],
        }
    ]


async def test_summaries_are_sorted_by_name(store: ScenarioStore):
    await store.async_save_scenario({**SCENARIO, "name": "beta"})
    await store.async_save_scenario({**SCENARIO, "name": "Alpha"})

    assert [s["name"] for s in store.summaries()] == ["Alpha", "beta"]


async def test_save_with_an_id_overwrites_and_keeps_created(store: ScenarioStore):
    first = await store.async_save_scenario(dict(SCENARIO))

    second = await store.async_save_scenario(
        {**SCENARIO, "name": "Renamed", "events": []}, first["scenario_id"]
    )

    assert second["scenario_id"] == first["scenario_id"]
    assert second["created"] == first["created"]
    assert len(store.summaries()) == 1
    assert store.get(first["scenario_id"])["name"] == "Renamed"
    assert unpack_events(store.get(first["scenario_id"])["events"]) == []


async def test_save_with_an_unknown_id_raises_not_found(store: ScenarioStore):
    with pytest.raises(SimulatorError) as err:
        await store.async_save_scenario(dict(SCENARIO), "nope")
    assert err.value.code == "not_found"


async def test_save_refuses_too_many_events(store: ScenarioStore):
    event = SCENARIO["events"][0]
    with pytest.raises(SimulatorError) as err:
        await store.async_save_scenario({**SCENARIO, "events": [event] * (MAX_EVENTS + 1)})
    assert err.value.code == "too_large"
    assert str(MAX_EVENTS) in str(err.value)


async def test_save_refuses_more_than_the_scenario_limit(store: ScenarioStore):
    for i in range(MAX_SCENARIOS):
        await store.async_save_scenario({**SCENARIO, "name": f"s{i}", "events": []})

    with pytest.raises(SimulatorError) as err:
        await store.async_save_scenario({**SCENARIO, "name": "one too many"})
    assert err.value.code == "too_many"


async def test_save_refuses_an_inverted_window(store: ScenarioStore):
    with pytest.raises(SimulatorError) as err:
        await store.async_save_scenario(
            {**SCENARIO, "start": SCENARIO["end"], "end": SCENARIO["start"]}
        )
    assert err.value.code == "invalid_window"


async def test_save_refuses_a_window_too_long_to_replay(store: ScenarioStore):
    """The same window cap as history and simulate, so a saved scenario can always run."""
    with pytest.raises(SimulatorError) as err:
        await store.async_save_scenario({**SCENARIO, "end": "2026-10-20T00:00:00+00:00"})
    assert err.value.code == "window_too_long"


async def test_delete_removes_it(store: ScenarioStore):
    saved = await store.async_save_scenario(dict(SCENARIO))

    await store.async_delete(saved["scenario_id"])

    assert store.summaries() == []


async def test_delete_unknown_raises_not_found(store: ScenarioStore):
    with pytest.raises(SimulatorError) as err:
        await store.async_delete("nope")
    assert err.value.code == "not_found"


async def test_scenarios_persist_to_a_new_store(
    hass: HomeAssistant, store: ScenarioStore, hass_storage
):
    saved = await store.async_save_scenario(dict(SCENARIO))
    assert STORAGE_KEY == "event_simulator.scenarios"
    assert saved["scenario_id"] in hass_storage[STORAGE_KEY]["data"]["scenarios"]

    reopened = ScenarioStore(hass)
    await reopened.async_load()

    assert reopened.get(saved["scenario_id"]) == store.get(saved["scenario_id"])


async def test_scenarios_are_stored_packed(store: ScenarioStore, hass_storage):
    saved = await store.async_save_scenario(dict(SCENARIO))

    stored = hass_storage[STORAGE_KEY]
    assert stored["version"] == STORAGE_VERSION == 2
    assert stored["data"]["scenarios"][saved["scenario_id"]]["events"] == pack_events(SCENARIO["events"])


def _week_of_power(count: int) -> list[dict[str, Any]]:
    """A chatty sensor: a recorded change every 15 s, as history.async_get_timeline returns it."""
    events = []
    for i in range(count):
        at = f"2026-10-04T{i * 15 // 3600 % 24:02d}:{i * 15 // 60 % 60:02d}:{i * 15 % 60:02d}.123456+00:00"
        events.append(
            {
                "at": at,
                "entity_id": "sensor.power",
                "state": f"{300 + i % 997 / 10:.1f}",
                "attributes": {"unit_of_measurement": "W", "device_class": "power", "friendly_name": "House power"},
                "last_changed": at,
                "last_updated": at,
            }
        )
    return events


async def test_a_version_1_store_is_migrated_to_packed_events(hass: HomeAssistant, hass_storage):
    """Scenarios saved before events were packed load, read back the same, and are rewritten packed on the next save."""
    old = {**SCENARIO, "events": [*SCENARIO["events"], *_week_of_power(3)]}
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {"scenarios": {"01OLD": {**old, "scenario_id": "01OLD", "created": "c", "updated": "u"}}},
    }
    store = ScenarioStore(hass)
    await store.async_load()

    scenario = store.get("01OLD")
    assert unpack_events(scenario["events"]) == old["events"]
    assert {k: scenario[k] for k in SCENARIO if k != "events"} == {k: SCENARIO[k] for k in SCENARIO if k != "events"}
    assert store.summaries()[0]["event_count"] == 4

    await store.async_save_scenario({**SCENARIO, "name": "new"})
    stored = hass_storage[STORAGE_KEY]
    assert stored["version"] == 2
    assert stored["data"]["scenarios"]["01OLD"]["events"] == pack_events(old["events"])


async def test_save_refuses_once_the_store_holds_too_many_events(store: ScenarioStore):
    """Every save rewrites the whole store file, so the total is capped as well as each scenario."""
    events = SCENARIO["events"] * 3
    with patch("custom_components.event_simulator.store.MAX_STORED_EVENTS", 7):
        first = await store.async_save_scenario({**SCENARIO, "events": events})
        await store.async_save_scenario({**SCENARIO, "events": events})
        # overwriting a scenario counts its new size, not old + new (3 + 3 + 3 > 7)
        await store.async_save_scenario({**SCENARIO, "events": events}, first["scenario_id"])
        with pytest.raises(SimulatorError) as err:
            await store.async_save_scenario({**SCENARIO, "events": events})
    assert err.value.code == "store_full"
    assert len(store.summaries()) == 2


def test_the_stored_event_cap_leaves_room_for_several_full_scenarios():
    assert MAX_STORED_EVENTS >= 4 * MAX_EVENTS


# ---- Suggest mode: labels and options --------------------------------------------

LABELS = [
    {"start": "2026-10-04T07:00:00+00:00", "end": "2026-10-04T07:30:00+00:00", "kind": "fire", "anchor": "both"},
    {"start": "2026-10-04T12:00:00+00:00", "end": "2026-10-04T13:00:00+00:00", "kind": "no_fire"},
]
OPTIONS = {"tolerance_before": 120.0, "tolerance_after": 600.0}

_FIELDS = vol.Schema(SCENARIO_FIELDS)


async def test_labels_and_suggest_options_round_trip(store: ScenarioStore, hass: HomeAssistant):
    body = _FIELDS({**SCENARIO, "labels": LABELS, "suggest_options": OPTIONS})
    saved = await store.async_save_scenario(body)

    scenario = store.get(saved["scenario_id"])
    assert scenario["labels"] == LABELS
    assert scenario["suggest_options"] == OPTIONS

    reopened = ScenarioStore(hass)
    await reopened.async_load()
    assert reopened.get(saved["scenario_id"])["labels"] == LABELS


async def test_a_scenario_without_labels_stores_neither_key(store: ScenarioStore):
    """Back-compatible: a replay scenario saves exactly as before."""
    saved = await store.async_save_scenario(_FIELDS({**SCENARIO, "labels": []}))

    scenario = store.get(saved["scenario_id"])
    assert "labels" not in scenario
    assert "suggest_options" not in scenario


def test_label_times_are_normalized_and_must_have_an_offset():
    body = _FIELDS({**SCENARIO, "labels": [{**LABELS[0], "start": "2026-10-04T07:00:00Z"}]})
    assert body["labels"][0]["start"] == "2026-10-04T07:00:00+00:00"

    with pytest.raises(vol.Invalid):
        _FIELDS({**SCENARIO, "labels": [{**LABELS[0], "start": "2026-10-04T07:00:00"}]})


def test_a_should_fire_label_is_anchored_at_its_start_by_default_and_a_must_not_one_never():
    body = _FIELDS(
        {
            **SCENARIO,
            "labels": [
                {k: v for k, v in LABELS[0].items() if k != "anchor"},
                {**LABELS[1], "anchor": "end"},
            ],
        }
    )
    assert body["labels"][0]["anchor"] == "start"
    assert "anchor" not in body["labels"][1]


@pytest.mark.parametrize(
    "label",
    [
        {**LABELS[0], "end": LABELS[0]["start"]},
        {**LABELS[0], "start": "2026-10-04T08:00:00+00:00"},
        {**LABELS[0], "kind": "maybe"},
        {**LABELS[0], "anchor": "middle"},
        {k: v for k, v in LABELS[0].items() if k != "kind"},
        {**LABELS[0], "extra": 1},
    ],
)
def test_save_refuses_a_malformed_label(label: dict[str, Any]):
    with pytest.raises(vol.Invalid):
        _FIELDS({**SCENARIO, "labels": [label]})


def test_save_refuses_too_many_labels():
    with pytest.raises(vol.Invalid):
        _FIELDS({**SCENARIO, "labels": [LABELS[0]] * (MAX_LABELS + 1)})


@pytest.mark.parametrize(
    "options",
    [{"tolerance_before": -1}, {"tolerance_after": 10 * 86400}, {"tolerance_before": "soon"}, {"max_conditions": 2}],
)
def test_save_refuses_malformed_suggest_options(options: dict[str, Any]):
    with pytest.raises(vol.Invalid):
        _FIELDS({**SCENARIO, "suggest_options": options})
