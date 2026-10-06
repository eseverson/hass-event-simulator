"""#35: recorded bus events (`scenario.bus_events`) are fired at their times, so event triggers replay.

Same harness as test_engine.py: every scenario runs through a real child process.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

import pytest

from .test_engine import run_child, ts, utc
from .test_engine_fidelity import scenario

WINDOW = "2026-10-04T{}:00+00:00"
DOOR = "binary_sensor.door"
IEEE = "b0:e8:e8:ff:fe:48:0b:83"


def bus(at: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"at": WINDOW.format(at), "event_type": event_type, "data": data}


def ir(at: str, key_code: str = "", command: str = "learned_ir_code", ieee: str = IEEE) -> dict[str, Any]:
    return bus(at, "zha_event", {"device_ieee": ieee, "command": command, "args": {"key_code": key_code}})


# The user's own automation (#35): an event trigger on one device's command, and a template condition on the event's nested data.
CAPTURE = {
    "triggers": [{"trigger": "event", "event_type": "zha_event", "event_data": {"device_ieee": IEEE, "command": "learned_ir_code"}}],
    "conditions": [{"condition": "template", "value_template": "{{ trigger.event.data.args.key_code | default('') != '' }}"}],
    "actions": [{"action": "logbook.log", "data": {"name": "x", "message": "{{ trigger.event.data.args.key_code }}"}}],
}

CASES: dict[str, dict[str, Any]] = {
    "capture": scenario(
        CAPTURE,
        bus_events=[
            ir("01:00", "AAA"),
            ir("02:00", ""),
            ir("03:00", "BBB", command="other"),
            ir("04:00", "CCC", ieee="00:00"),
            bus("05:00", "tag_scanned", {"tag_id": "t"}),
            ir("05:30", "DDD"),
        ],
    ),
    # A state write and a bus event at one instant: the state is written first, so a condition reads it.
    "same_instant": scenario(
        {
            "triggers": [{"trigger": "event", "event_type": "tag_scanned"}],
            "conditions": [{"condition": "state", "entity_id": DOOR, "state": "on"}],
            "actions": [],
        },
        initial_states={DOOR: {"state": "off"}},
        events=[{"at": WINDOW.format("01:00"), "entity_id": DOOR, "state": "on", "attributes": {}}],
        bus_events=[bus("01:00", "tag_scanned", {"tag_id": "t"}), bus("00:30", "tag_scanned", {})],
    ),
    "outside": scenario(
        {"triggers": [{"trigger": "event", "event_type": "tag_scanned"}], "actions": []},
        bus_events=[bus("01:00", "tag_scanned", {}), {"at": "2026-10-03T23:00:00+00:00", "event_type": "tag_scanned", "data": {}}],
    ),
    "not_a_list": scenario({"triggers": [{"trigger": "event", "event_type": "x"}], "actions": []}, bus_events={"x": 1}),
    "no_type": scenario({"triggers": [{"trigger": "event", "event_type": "x"}], "actions": []}, bus_events=[{"at": WINDOW.format("01:00"), "data": {}}]),
    "bad_data": scenario({"triggers": [{"trigger": "event", "event_type": "x"}], "actions": []}, bus_events=[{"at": WINDOW.format("01:00"), "event_type": "x", "data": [1]}]),
    "core_event": scenario({"triggers": [{"trigger": "event", "event_type": "x"}], "actions": []}, bus_events=[bus("01:00", "state_changed", {})]),
}


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(CASES, pool.map(run_child, CASES.values()), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None, f"{name}: no result\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def runs(result: dict[str, Any]) -> list[tuple[Any, str]]:
    return [(ts(run["started"]), run["outcome"]) for run in result["runs"]]


def test_event_triggers_fire_on_matching_recorded_events_with_their_data(results):
    result = results["capture"]
    assert result["error"] is None, result["error"]
    assert runs(result) == [
        (utc(WINDOW.format("01:00")), "fired"),
        (utc(WINDOW.format("02:00")), "condition_failed"),
        (utc(WINDOW.format("05:30")), "fired"),
    ]
    assert [call["data"]["message"] for call in result["service_calls"]] == ["AAA", "DDD"]
    assert result["runs"][0]["trigger"]["platform"] == "event"


def test_a_bus_event_follows_the_state_writes_of_its_instant(results):
    result = results["same_instant"]
    assert result["error"] is None, result["error"]
    assert runs(result) == [(utc(WINDOW.format("00:30")), "condition_failed"), (utc(WINDOW.format("01:00")), "fired")]


def test_bus_events_outside_the_window_are_skipped_with_a_warning(results):
    result = results["outside"]
    assert result["error"] is None, result["error"]
    assert runs(result) == [(utc(WINDOW.format("01:00")), "fired")]
    assert [w["code"] for w in result["warnings"]].count("event_outside_window") == 1


@pytest.mark.parametrize("name", ["not_a_list", "no_type", "bad_data", "core_event"])
def test_malformed_bus_events_are_refused(results, name):
    assert results[name]["error"]["code"] == "invalid_scenario", results[name]["error"]
    assert "bus_events" in results[name]["error"]["message"]
