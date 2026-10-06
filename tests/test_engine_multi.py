"""#22: one child replays several independent automations (`scenario.automations`).

Exact validation of suggestion candidates replays many automations against one timeline. Their actions are record-only, so they cannot interact: each runs as it would alone, and the result attributes every run and service call to its automation by index. Same harness as test_engine.py: every scenario runs through a real child process.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.event_simulator import history
from custom_components.event_simulator.sandbox.engine import MAX_AUTOMATIONS

from .test_engine import run_child, ts, utc
from .test_engine_fidelity import scenario
from .test_engine_self import CRANK_HEAT, DOOR, SELF, door, initial, own

WINDOW = "2026-10-04T{}:00+00:00"
MOTION = "binary_sensor.motion"


def multi(automations: list[Any], **extra: Any) -> dict[str, Any]:
    built = scenario({}, **extra)
    del built["automation"]
    built["automations"] = automations
    return built


def light(automation_id: str | None, entity_id: str, to: str = "on", **extra: Any) -> dict[str, Any]:
    """Turns on a light named after the automation when `entity_id` goes to `to`."""
    config = {
        "alias": f"Light {automation_id or entity_id}",
        "triggers": [{"trigger": "state", "entity_id": entity_id, "to": to}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": f"light.{automation_id or 'x'}"}}],
        **extra,
    }
    if automation_id:
        config["id"] = automation_id
    return config


def toggles(entity_id: str, *times: str) -> list[dict[str, Any]]:
    """`entity_id` on at each time and off a minute later."""
    events = []
    for at in times:
        opened = utc(WINDOW.format(at))
        events.append({"at": opened.isoformat(), "entity_id": entity_id, "state": "on", "attributes": {}})
        events.append({"at": opened.replace(minute=opened.minute + 1).isoformat(), "entity_id": entity_id, "state": "off", "attributes": {}})
    return events


def runs_of(result: dict[str, Any], index: int) -> list[tuple[Any, str]]:
    return [(ts(run["started"]), run["outcome"]) for run in result["runs"] if run["automation"] == index]


def calls_of(result: dict[str, Any], index: int) -> list[tuple[Any, str]]:
    return [(ts(call["at"]), *call["target"]["entity_id"]) for call in result["service_calls"] if call["automation"] == index]


CASES: dict[str, dict[str, Any]] = {
    "different_triggers": multi(
        [light("door_light", DOOR), light("motion_light", MOTION)],
        initial_states={DOOR: {"state": "off"}, MOTION: {"state": "off"}},
        events=sorted([*toggles(DOOR, "01:00", "03:00"), *toggles(MOTION, "02:00")], key=lambda event: event["at"]),
    ),
    "invalid_and_valid": multi(
        [
            {"id": "broken", "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on", "for": "not a duration"}], "actions": []},
            light("fine", DOOR),
            {"id": "custom", "triggers": [{"trigger": "not_a_real_integration_xyz"}], "actions": []},
            "not an object",
        ],
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, "01:00"),
    ),
    "modes": multi(
        [
            {"id": "single", "mode": "single", "max_exceeded": "silent", "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}], "actions": [{"delay": "00:05:00"}, {"action": "light.turn_on", "target": {"entity_id": "light.single"}}]},
            {"id": "queued", "mode": "queued", "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}], "actions": [{"delay": "00:05:00"}, {"action": "light.turn_on", "target": {"entity_id": "light.queued"}}]},
        ],
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, "01:00", "01:02"),
    ),
    # Neither has an id or an alias: the sandbox gives each its own, by index.
    "same_trigger": multi(
        [
            {"triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.a"}}]},
            {"triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.b"}}]},
        ],
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, "01:00", "04:00"),
    ),
    "duplicate_ids": multi(
        [light("twin", DOOR), {**light("twin", DOOR), "alias": "Second twin"}],
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, "01:00"),
    ),
    "over_the_cap": multi([light(f"a{index}", DOOR) for index in range(MAX_AUTOMATIONS + 1)]),
    "at_the_cap": multi(
        [light(f"a{index}", DOOR) for index in range(MAX_AUTOMATIONS)],
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, "01:00"),
    ),
    "both_keys": {**multi([light("a", DOOR)]), "automation": light("b", DOOR)},
    "empty_list": multi([]),
}


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(CASES, pool.map(run_child, CASES.values()), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None, f"{name}: no result\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def test_runs_and_calls_are_attributed_by_index(results):
    result = results["different_triggers"]
    assert result["error"] is None, result["error"]
    assert runs_of(result, 0) == [(utc(WINDOW.format("01:00")), "fired"), (utc(WINDOW.format("03:00")), "fired")]
    assert runs_of(result, 1) == [(utc(WINDOW.format("02:00")), "fired")]
    assert calls_of(result, 0) == [(utc(WINDOW.format("01:00")), "light.door_light"), (utc(WINDOW.format("03:00")), "light.door_light")]
    assert calls_of(result, 1) == [(utc(WINDOW.format("02:00")), "light.motion_light")]
    assert result["automations"] == [
        {"index": 0, "entity_id": "automation.light_door_light", "loaded": True, "error": None, "run_count": 2},
        {"index": 1, "entity_id": "automation.light_motion_light", "loaded": True, "error": None, "run_count": 1},
    ]
    # Runs stay in start order across automations.
    assert [run["started"] for run in result["runs"]] == sorted(run["started"] for run in result["runs"])


def test_an_invalid_automation_fails_only_itself(results):
    result = results["invalid_and_valid"]
    assert result["error"] is None, result["error"]
    broken, fine, custom, junk = result["automations"]
    assert broken["loaded"] is False and broken["error"]["code"] == "invalid_automation"
    assert "for" in broken["error"]["message"]
    assert custom["loaded"] is False and custom["error"]["code"] == "unsupported_automation"
    assert junk["loaded"] is False and junk["error"]["code"] == "invalid_automation"
    assert [item["run_count"] for item in result["automations"]] == [0, 1, 0, 0]
    assert fine == {"index": 1, "entity_id": "automation.light_fine", "loaded": True, "error": None, "run_count": 1}
    assert runs_of(result, 1) == [(utc(WINDOW.format("01:00")), "fired")]
    assert {run["automation"] for run in result["runs"]} == {1}


def test_each_automation_keeps_its_own_mode(results):
    result = results["modes"]
    assert result["error"] is None, result["error"]
    assert runs_of(result, 0) == [(utc(WINDOW.format("01:00")), "fired"), (utc(WINDOW.format("01:02")), "dropped")]
    assert runs_of(result, 1) == [(utc(WINDOW.format("01:00")), "fired"), (utc(WINDOW.format("01:02")), "fired")]
    assert calls_of(result, 0) == [(utc(WINDOW.format("01:05")), "light.single")]
    # Queued: the second run waits for the first, so its delay ends five minutes after the first one's.
    assert calls_of(result, 1) == [(utc(WINDOW.format("01:05")), "light.queued"), (utc(WINDOW.format("01:10")), "light.queued")]


def test_the_same_trigger_fires_every_automation(results):
    result = results["same_trigger"]
    assert result["error"] is None, result["error"]
    fired = [(utc(WINDOW.format("01:00")), "fired"), (utc(WINDOW.format("04:00")), "fired")]
    assert runs_of(result, 0) == fired
    assert runs_of(result, 1) == fired
    assert [call[1] for call in calls_of(result, 0)] == ["light.a", "light.a"]
    assert [call[1] for call in calls_of(result, 1)] == ["light.b", "light.b"]
    assert [item["entity_id"] for item in result["automations"]] == ["automation.automation_0", "automation.automation_1"]


def test_a_repeated_id_is_replaced_with_a_warning(results):
    result = results["duplicate_ids"]
    assert result["error"] is None, result["error"]
    assert [item["loaded"] for item in result["automations"]] == [True, True]
    assert len(runs_of(result, 0)) == 1 and len(runs_of(result, 1)) == 1
    assert [warning["code"] for warning in result["warnings"]].count("duplicate_automation_id") == 1


def test_the_count_is_capped(results):
    assert results["over_the_cap"]["error"]["code"] == "invalid_scenario"
    assert str(MAX_AUTOMATIONS) in results["over_the_cap"]["error"]["message"]
    at_cap = results["at_the_cap"]
    assert at_cap["error"] is None, at_cap["error"]
    assert [item["run_count"] for item in at_cap["automations"]] == [1] * MAX_AUTOMATIONS


@pytest.mark.parametrize("name", ["both_keys", "empty_list"])
def test_exactly_one_of_automation_and_automations(results, name):
    assert results[name]["error"]["code"] == "invalid_scenario"


def test_a_single_automation_keeps_the_original_shape():
    code, result, stderr = run_child(scenario(light("solo", DOOR), initial_states={DOOR: {"state": "off"}}, events=toggles(DOOR, "01:00")))
    assert code == 0, stderr[-4000:]
    assert result["error"] is None, result["error"]
    assert "automations" not in result
    assert "automation" not in result["runs"][0]
    assert "automation" not in result["service_calls"][0]


async def test_own_entity_handling_applies_per_automation(hass: HomeAssistant):
    """#17 per automation: a recorded off of one automation's own entity disables only that one."""
    er.async_get(hass).async_get_or_create("automation", "automation", CRANK_HEAT["id"], suggested_object_id=CRANK_HEAT["id"])
    crank = {**CRANK_HEAT, "actions": CRANK_HEAT["actions"][:1]}
    built = multi(
        [crank, light("bystander", DOOR)],
        initial_states=initial("on"),
        events=sorted([*door("01:00", "03:00"), own("02:00", "off")], key=lambda event: event["at"]),
    )
    timeline = list(dict.fromkeys([*built["initial_states"], *(event["entity_id"] for event in built["events"])]))
    built["registry"] = history.async_registry_snapshot(hass, crank, timeline)
    code, result, stderr = await asyncio.get_running_loop().run_in_executor(None, run_child, built)
    assert code == 0, stderr[-4000:]
    assert result["error"] is None, result["error"]
    assert result["automations"][0]["entity_id"] == SELF
    assert runs_of(result, 0) == [(utc(WINDOW.format("01:00")), "fired")]
    assert runs_of(result, 1) == [(utc(WINDOW.format("01:00")), "fired"), (utc(WINDOW.format("03:00")), "fired")]


def test_a_self_call_warning_names_the_calling_automation():
    calls_self = {**light("caller", DOOR), "actions": [{"action": "automation.turn_off", "target": {"entity_id": "automation.light_caller"}}]}
    calls_other = {**light("other", DOOR), "actions": [{"action": "automation.turn_off", "target": {"entity_id": "automation.light_caller"}}]}
    code, result, stderr = run_child(multi([calls_other, calls_self], initial_states={DOOR: {"state": "off"}}, events=toggles(DOOR, "01:00")))
    assert code == 0, stderr[-4000:]
    warnings = [warning for warning in result["warnings"] if warning["code"] == "self_service_call"]
    assert len(warnings) == 1
    assert "automation.light_caller" in warnings[0]["message"]
