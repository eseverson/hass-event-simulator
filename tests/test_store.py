"""Named scenarios persisted with Home Assistant's storage helper."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.core import HomeAssistant

from custom_components.event_simulator.history import SimulatorError
from custom_components.event_simulator.store import (
    MAX_EVENTS,
    MAX_SCENARIOS,
    STORAGE_KEY,
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
        "scenario_id": saved["scenario_id"],
        "created": saved["created"],
        "updated": saved["updated"],
    }


async def test_get_returns_the_full_scenario(store: ScenarioStore):
    saved = await store.async_save_scenario(dict(SCENARIO))

    scenario = store.get(saved["scenario_id"])

    assert scenario["events"] == SCENARIO["events"]
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
    assert store.get(first["scenario_id"])["events"] == []


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
