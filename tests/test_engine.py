"""End-to-end tests of the replay engine through the real child process.

Every scenario runs in a fresh child launched with `sandbox.child_command()`, the same command the live runner uses, so these cover the launcher, the stdin/stdout contract and the engine together. The fixture scenarios live in tests/scenarios/; the children run concurrently once per module to keep the suite quick.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custom_components.event_simulator.sandbox import child_command

SCENARIOS = Path(__file__).parent / "scenarios"
CHILD_TIMEOUT = 120


def run_child(payload: dict[str, Any] | bytes) -> tuple[int, dict[str, Any] | None, str]:
    """Run one scenario through a child process: (exit code, parsed stdout, stderr)."""
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    proc = subprocess.run(child_command(), input=data, capture_output=True, timeout=CHILD_TIMEOUT, check=False)
    stdout = proc.stdout.decode()
    result = json.loads(stdout) if stdout.strip() else None
    return proc.returncode, result, proc.stderr.decode()


def load(name: str) -> dict[str, Any]:
    return json.loads((SCENARIOS / f"{name}.json").read_text())


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    """Every fixture scenario's result, keyed by file stem; each child must exit 0."""
    names = sorted(path.stem for path in SCENARIOS.glob("*.json"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(names, pool.map(lambda name: run_child(load(name)), names), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None, f"{name}: no result\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value).astimezone(UTC)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def summary(result: dict[str, Any]) -> list[tuple[datetime | None, str, str | None]]:
    return [(ts(run["started"]), run["outcome"], run["stopped_by"]) for run in result["runs"]]


def calls(result: dict[str, Any]) -> list[tuple[datetime | None, str]]:
    return [(ts(call["at"]), f"{call['domain']}.{call['service']}") for call in result["service_calls"]]


def test_state_trigger_fires_and_records_calls_without_executing(results):
    result = results["state_trigger"]
    assert result["error"] is None
    assert summary(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "fired", None),
        (utc("2026-10-04T02:00:00+00:00"), "fired", None),
    ]
    first = result["runs"][0]
    assert first["finished"] == first["started"]
    assert first["trigger"]["platform"] == "state"
    assert first["trigger"]["entity_id"] == "binary_sensor.door"
    assert first["trigger"]["from_state"] == "off"
    assert first["trigger"]["to_state"] == "on"
    assert first["trace"]["item_id"] == "door_light"
    assert "action/0" in first["trace"]["trace"]

    assert calls(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "light.turn_on"),
        (utc("2026-10-04T01:00:00+00:00"), "script.not_on_live_instance"),
        (utc("2026-10-04T02:00:00+00:00"), "light.turn_on"),
        (utc("2026-10-04T02:00:00+00:00"), "script.not_on_live_instance"),
    ]
    light = result["service_calls"][0]
    assert light["data"] == {"brightness": 128}
    assert light["target"] == {"entity_id": ["light.hall"]}
    assert light["run"] == first["id"]
    assert light["context"]["id"] == first["trace"]["context"]["id"]

    # The event after `end` is not played, and the unknown service is flagged once.
    codes = [warning["code"] for warning in result["warnings"]]
    assert codes.count("event_outside_window") == 1
    assert "late_attach" not in codes
    unknown = [warning for warning in result["warnings"] if warning["code"] == "unknown_service"]
    assert len(unknown) == 1
    assert "script.not_on_live_instance" in unknown[0]["message"]


def test_numeric_state_for_needs_the_full_hold(results):
    result = results["numeric_state_for"]
    # 01:00 -> 26 drops back below at 01:03, so the 5 min hold never completes; 02:00 -> 27 holds (28 at 02:02 stays above).
    assert summary(result) == [(utc("2026-10-04T02:05:00+00:00"), "fired", None)]
    assert calls(result) == [(utc("2026-10-04T02:05:00+00:00"), "notify.notify")]
    assert result["service_calls"][0]["data"] == {"message": "Temp is 28"}
    assert result["runs"][0]["trigger"]["platform"] == "numeric_state"


def test_time_trigger_uses_local_time_and_a_half_open_window(results):
    result = results["time_trigger"]
    started = [ts(run["started"]) for run in result["runs"]]
    # Window is [2026-10-04 00:00, 2026-10-06 00:00) Chicago (CDT, UTC-5): the trigger at the start instant fires, the one at the end does not.
    assert [moment.replace(microsecond=0) for moment in started] == [
        utc("2026-10-04T05:00:00+00:00"),
        utc("2026-10-04T11:30:00+00:00"),
        utc("2026-10-05T05:00:00+00:00"),
        utc("2026-10-05T11:30:00+00:00"),
    ]
    # HA fires time-change listeners 50-500 ms past the second on purpose; the replay keeps that.
    assert all(50_000 <= moment.microsecond <= 500_000 for moment in started)
    assert all(run["outcome"] == "fired" for run in result["runs"])
    assert [run["trigger"]["platform"] for run in result["runs"]] == ["time"] * 4
    assert len(result["service_calls"]) == 4


def test_replay_is_repeatable(results):
    code, again, stderr = run_child(load("time_trigger"))
    assert code == 0, stderr
    assert [run["started"] for run in again["runs"]] == [run["started"] for run in results["time_trigger"]["runs"]]
    assert [run["id"] for run in again["runs"]] == [run["id"] for run in results["time_trigger"]["runs"]]


def test_sun_condition_uses_the_scenario_location(results):
    result = results["sun_condition"]
    # Chicago on 2026-06-21: sunrise 05:15:47 CDT, sunset 20:28:59 CDT.
    assert summary(result) == [
        (utc("2026-06-21T04:00:00-05:00"), "condition_failed", "condition/0"),
        (utc("2026-06-21T12:00:00-05:00"), "fired", None),
        (utc("2026-06-21T21:00:00-05:00"), "condition_failed", "condition/0"),
    ]
    assert calls(result) == [(utc("2026-06-21T12:00:00-05:00"), "light.turn_on")]


def test_template_condition_sees_state_at_trigger_time(results):
    result = results["template_condition"]
    assert summary(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "condition_failed", "condition/0"),
        (utc("2026-10-04T02:00:00+00:00"), "fired", None),
    ]
    assert result["service_calls"][0]["data"] == {"brightness_pct": 10}


def test_delay_and_wait_for_trigger_advance_virtual_time(results):
    result = results["delay_wait"]
    runs = result["runs"]
    assert [(ts(run["started"]), ts(run["finished"]), run["outcome"]) for run in runs] == [
        (utc("2026-10-04T01:00:00+00:00"), utc("2026-10-04T01:05:00+00:00"), "fired"),
        # No door-off after 03:00: the wait times out after 10 minutes and the run continues.
        (utc("2026-10-04T03:00:00+00:00"), utc("2026-10-04T03:12:00+00:00"), "fired"),
    ]
    assert calls(result) == [
        (utc("2026-10-04T01:02:00+00:00"), "light.turn_on"),
        (utc("2026-10-04T01:05:00+00:00"), "light.turn_off"),
        (utc("2026-10-04T03:02:00+00:00"), "light.turn_on"),
        (utc("2026-10-04T03:12:00+00:00"), "light.turn_off"),
    ]
    assert [call["data"] for call in result["service_calls"] if call["service"] == "turn_off"] == [
        {"transition": 1},
        {"transition": 5},
    ]
    assert [call["run"] for call in result["service_calls"]] == [runs[0]["id"]] * 2 + [runs[1]["id"]] * 2


def test_mode_single_drops_a_retrigger(results):
    result = results["mode_single"]
    assert summary(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "fired", None),
        (utc("2026-10-04T01:02:00+00:00"), "dropped", "mode_single"),
        (utc("2026-10-04T02:00:00+00:00"), "fired", None),
    ]
    assert result["runs"][1]["script_execution"] == "failed_single"
    assert calls(result) == [
        (utc("2026-10-04T01:05:00+00:00"), "light.turn_on"),
        (utc("2026-10-04T02:05:00+00:00"), "light.turn_on"),
    ]


def test_invalid_automation_is_a_structured_error(results):
    result = results["invalid_config"]
    assert result["error"]["code"] == "invalid_automation"
    assert "above" in result["error"]["message"] or "below" in result["error"]["message"]
    assert result["runs"] == []
    assert result["service_calls"] == []


def test_initial_state_keeps_its_recorded_last_changed(results):
    """#10: a `for:` condition early in the window sees how long the state has really held."""
    result = results["initial_last_changed"]
    assert summary(result) == [(utc("2026-10-04T00:05:00+00:00"), "fired", None)]
    assert result["service_calls"][0]["data"] == {
        "message": "2026-10-03T08:00:00+00:00|2026-10-03T20:00:00+00:00"
    }


def test_initial_state_without_last_changed_starts_at_the_window():
    scenario = load("initial_last_changed")
    for state in scenario["initial_states"].values():
        state.pop("last_changed", None)
        state.pop("last_updated", None)
    code, result, stderr = run_child(scenario)
    assert code == 0, stderr
    # Seeded at the window start, the door has only been off for 5 minutes at the trigger.
    assert summary(result) == [(utc("2026-10-04T00:05:00+00:00"), "condition_failed", "condition/0")]


def test_run_in_flight_at_end_finishes_within_the_overrun():
    scenario = load("delay_wait")
    scenario["end"] = "2026-10-04T03:05:00+00:00"
    code, result, stderr = run_child(scenario)
    assert code == 0, stderr
    last = result["runs"][-1]
    assert (ts(last["started"]), ts(last["finished"]), last["outcome"]) == (
        utc("2026-10-04T03:00:00+00:00"),
        utc("2026-10-04T03:12:00+00:00"),
        "fired",
    )
    assert calls(result)[-1] == (utc("2026-10-04T03:12:00+00:00"), "light.turn_off")


def test_run_still_going_past_the_overrun_is_reported_unfinished():
    scenario = load("delay_wait")
    scenario["end"] = "2026-10-04T03:05:00+00:00"
    scenario["overrun_seconds"] = 60
    code, result, stderr = run_child(scenario)
    assert code == 0, stderr
    last = result["runs"][-1]
    assert last["outcome"] == "unfinished"
    assert last["finished"] is None
    assert "run_unfinished" in [warning["code"] for warning in result["warnings"]]
    assert calls(result)[-1] == (utc("2026-10-04T03:02:00+00:00"), "light.turn_on")


def test_unreadable_input_exits_nonzero_with_an_error_result():
    code, result, _ = run_child(b"{not json")
    assert code == 2
    assert result["error"]["code"] == "invalid_scenario"


def test_malformed_scenario_is_a_structured_error():
    scenario = load("state_trigger")
    scenario["end"] = scenario["start"]
    code, result, _ = run_child(scenario)
    assert code == 0
    assert result["error"]["code"] == "invalid_scenario"
    assert result["runs"] == []


def test_day_long_busy_scenario_finishes_in_seconds():
    start = datetime(2026, 10, 4, tzinfo=UTC)
    events = []
    for minute in range(0, 24 * 60, 10):
        at = start + timedelta(minutes=minute)
        events.append({"at": at.isoformat(), "entity_id": "binary_sensor.door", "state": "on" if minute % 20 == 0 else "off", "attributes": {}})
    scenario = {
        "version": 1,
        "core": {"time_zone": "UTC", "latitude": 52.37, "longitude": 4.89, "elevation": 0, "unit_system": "metric"},
        "start": start.isoformat(),
        "end": (start + timedelta(days=1)).isoformat(),
        "automation": {
            "id": "busy",
            "alias": "Every minute and every door change",
            "mode": "queued",
            "triggers": [
                {"trigger": "time_pattern", "minutes": "/1"},
                {"trigger": "state", "entity_id": "binary_sensor.door"},
            ],
            "conditions": [{"condition": "template", "value_template": "{{ now().minute % 2 == 0 }}"}],
            "actions": [{"delay": "00:00:20"}, {"action": "light.toggle", "target": {"entity_id": "light.hall"}}],
        },
        "initial_states": {"binary_sensor.door": {"state": "off", "attributes": {}}},
        "events": events,
    }
    wall = time.monotonic()
    code, result, stderr = run_child(scenario)
    elapsed = time.monotonic() - wall
    assert code == 0, stderr
    assert len([run for run in result["runs"] if run["trigger"]["platform"] == "time_pattern"]) == 24 * 60
    fired = [run for run in result["runs"] if run["outcome"] == "fired"]
    assert len(fired) == len(result["service_calls"]) == 24 * 30 + 144
    assert elapsed < 10, f"24 h replay took {elapsed:.1f} s"


def test_failing_action_is_an_error_outcome():
    scenario = load("state_trigger")
    scenario["automation"]["actions"] = [
        {"action": "light.turn_on", "target": {"entity_id": "light.hall"}},
        {"action": "notify.notify", "data": {"message": "{{ states('sensor.missing') | float }}"}},
    ]
    code, result, stderr = run_child(scenario)
    assert code == 0, stderr
    assert summary(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "error", "action/1"),
        (utc("2026-10-04T02:00:00+00:00"), "error", "action/1"),
    ]
    assert "float" in result["runs"][0]["error"]
    assert calls(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "light.turn_on"),
        (utc("2026-10-04T02:00:00+00:00"), "light.turn_on"),
    ]
    # HA logs the failure; the replay surfaces it as a warning.
    assert any(warning["code"] == "log" and "float" in warning["message"] for warning in result["warnings"])


def test_run_started_at_the_window_start_does_not_delay_the_timeline():
    scenario = load("delay_wait")
    scenario["automation"]["triggers"].append({"trigger": "time", "at": "00:00:00"})
    scenario["automation"]["actions"] = [{"delay": "02:00:00"}, {"action": "light.turn_on", "target": {"entity_id": "light.hall"}}]
    code, result, stderr = run_child(scenario)
    assert code == 0, stderr
    assert [(ts(run["started"]).replace(microsecond=0), run["trigger"]["platform"]) for run in result["runs"]] == [
        (utc("2026-10-04T00:00:00+00:00"), "time"),
        (utc("2026-10-04T01:00:00+00:00"), "state"),
        (utc("2026-10-04T03:00:00+00:00"), "state"),
    ]
    assert "late_attach" not in [warning["code"] for warning in result["warnings"]]
