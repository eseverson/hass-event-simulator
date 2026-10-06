"""#29: every way Home Assistant can record an automation trace maps to its own run outcome.

The replays cover each outcome a scenario can reach through a real child, as test_engine.py does. `_run_record` is also fed a stand-in trace for every `script_execution` value core sets (helpers/script.py, components/automation), including the ones a sandboxed replay cannot produce and HA 2026.9's `not_triggered`, which older releases never record.
"""

from __future__ import annotations

import concurrent.futures
import inspect
from typing import Any

from homeassistant.components.automation.trace import trace_automation
import pytest

from custom_components.event_simulator.sandbox.engine import _run_record

from .test_engine import calls, run_child, summary, utc
from .test_engine_fidelity import scenario

# HA 2026.9 records a trace when a trigger evaluated a change but did not fire (`AutomationEntity._handle_not_triggered`).
RECORDS_NOT_TRIGGERED = "not_triggered" in inspect.signature(trace_automation).parameters
needs_not_triggered = pytest.mark.skipif(not RECORDS_NOT_TRIGGERED, reason="this Home Assistant records no not_triggered traces")

WINDOW = "2026-10-04T{}:00+00:00"
DOOR = "binary_sensor.door"
HUMIDITY = "sensor.bath_humidity"
HUMIDITY_ATTRS = {"device_class": "humidity", "unit_of_measurement": "%"}
OPENED = [{"trigger": "state", "entity_id": DOOR, "to": "on"}]
LIGHT = {"action": "light.turn_on", "target": {"entity_id": "light.hall"}}


def door(automation: dict[str, Any], *times: str) -> dict[str, Any]:
    """The door opens at each of `times` (HH:MM) and closes a minute later."""
    events = []
    for at in times or ("01:00", "01:02"):
        hour, minute = at.split(":")
        events += [
            {"at": WINDOW.format(at), "entity_id": DOOR, "state": "on"},
            {"at": WINDOW.format(f"{hour}:{int(minute) + 1:02d}"), "entity_id": DOOR, "state": "off"},
        ]
    return scenario({"triggers": OPENED, **automation}, initial_states={DOOR: {"state": "off"}}, events=events)


def humidity(traces: str | None = None) -> dict[str, Any]:
    """A purpose-specific threshold trigger: a non-numeric reading is reported as not triggered, a reading above it fires."""
    built = scenario(
        {
            "triggers": [{"trigger": "humidity.crossed_threshold", "target": {"entity_id": HUMIDITY}, "options": {"threshold": {"type": "above", "value": {"number": 60}}}}],
            "actions": [{"action": "fan.turn_on", "target": {"entity_id": "fan.bath"}}],
        },
        initial_states={HUMIDITY: {"state": "50", "attributes": HUMIDITY_ATTRS}},
        events=[
            {"at": WINDOW.format("01:00"), "entity_id": HUMIDITY, "state": "abc", "attributes": HUMIDITY_ATTRS},
            {"at": WINDOW.format("02:00"), "entity_id": HUMIDITY, "state": "70", "attributes": HUMIDITY_ATTRS},
        ],
    )
    if traces is not None:
        built["traces"] = traces
    return built


SLOW = [{"delay": "00:05:00"}, LIGHT]
SCENARIOS: dict[str, Any] = {
    "action_condition": lambda: door({"actions": [LIGHT, {"condition": "template", "value_template": "{{ false }}"}, LIGHT]}),
    "stop_error": lambda: door({"actions": [LIGHT, {"stop": "nope", "error": True}]}),
    "stop": lambda: door({"actions": [LIGHT, {"stop": "done"}]}),
    "restart": lambda: door({"mode": "restart", "actions": SLOW}),
    "parallel_max": lambda: door({"mode": "parallel", "max": 2, "actions": SLOW}, "01:00", "01:02", "01:04"),
    "queued_max": lambda: door({"mode": "queued", "max": 2, "actions": SLOW}, "01:00", "01:02", "01:04"),
    "variables_error": lambda: door({"variables": {"level": "{{ states('sensor.missing') | float }}"}, "actions": [LIGHT]}),
    "condition_error": lambda: door({"conditions": [{"condition": "numeric_state", "entity_id": DOOR, "above": 1}], "actions": [LIGHT]}),
}
if RECORDS_NOT_TRIGGERED:
    SCENARIOS |= {f"not_triggered_{mode}": (lambda mode=mode: humidity(mode)) for mode in ("full", "summary", "none")}


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(SCENARIOS, pool.map(lambda name: run_child(SCENARIOS[name]()), SCENARIOS), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None and result["error"] is None, f"{name}: {result and result['error']}\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def at(minute: str) -> Any:
    return utc(WINDOW.format(minute))


# --- replays --------------------------------------------------------------------------------------


@needs_not_triggered
def test_a_trigger_that_did_not_fire_is_not_an_error(results):
    result = results["not_triggered_full"]
    assert summary(result) == [(at("01:00"), "not_triggered", "entity_value_not_numeric"), (at("02:00"), "fired", None)]
    run = result["runs"][0]
    assert run["script_execution"] == "not_triggered"
    assert run["error"] is None
    assert run["trigger"]["platform"] == "humidity.crossed_threshold"
    assert calls(result) == [(at("02:00"), "fan.turn_on")]


@needs_not_triggered
@pytest.mark.parametrize("mode", ["summary", "none"])
def test_trimmed_traces_keep_the_not_triggered_outcome(results, mode):
    runs = results[f"not_triggered_{mode}"]["runs"]
    assert [run["outcome"] for run in runs] == ["not_triggered", "fired"]
    if mode == "summary":
        assert runs[0]["stopped_by"] == "entity_value_not_numeric"
        assert runs[0]["conditions"] == {}


def test_a_failing_condition_in_the_actions_aborts(results):
    assert summary(results["action_condition"]) == [(at("01:00"), "aborted", "action/1"), (at("01:02"), "aborted", "action/1")]
    assert len(results["action_condition"]["service_calls"]) == 2


def test_stop_with_error_aborts(results):
    assert summary(results["stop_error"]) == [(at("01:00"), "aborted", "action/1"), (at("01:02"), "aborted", "action/1")]


def test_a_plain_stop_is_a_finished_run(results):
    assert summary(results["stop"]) == [(at("01:00"), "fired", None), (at("01:02"), "fired", None)]


def test_restart_cancels_the_run_in_flight(results):
    assert summary(results["restart"]) == [(at("01:00"), "cancelled", "action/0"), (at("01:02"), "fired", None)]
    assert calls(results["restart"]) == [(at("01:07"), "light.turn_on")]


@pytest.mark.parametrize("name", ["parallel_max", "queued_max"])
def test_a_run_past_max_is_dropped(results, name):
    assert [(started, outcome, stopped_by) for started, outcome, stopped_by in summary(results[name])][-1] == (at("01:04"), "dropped", "max_runs")
    assert [outcome for _, outcome, _ in summary(results[name])] == ["fired", "fired", "dropped"]


def test_a_variables_error_is_an_error_without_a_step(results):
    run = results["variables_error"]["runs"][0]
    assert (run["outcome"], run["stopped_by"], run["script_execution"]) == ("error", None, None)
    assert "float" in run["error"]
    assert results["variables_error"]["service_calls"] == []


def test_a_condition_that_errors_is_the_failing_condition(results):
    # HA logs the error and treats the condition as false; the trace step carries the error instead of a result.
    assert summary(results["condition_error"]) == [(at("01:00"), "condition_failed", "condition/0"), (at("01:02"), "condition_failed", "condition/0")]


# --- every script_execution value -----------------------------------------------------------------


class _Trace:
    """Just enough of an AutomationTrace for `_run_record`."""

    run_id = "run"

    def __init__(self, execution: str | None, *, state: str = "stopped", error: str | None = None, trace: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._short = {
            "state": state,
            "script_execution": execution,
            "error": error,
            "last_step": "action/0",
            "timestamp": {"start": "2026-10-04T01:00:00+00:00", "finish": "2026-10-04T01:00:01+00:00"},
            "trigger": "state of binary_sensor.door",
        }
        self._steps = trace or {"trigger/0": [{"path": "trigger/0", "changed_variables": {"trigger": {"idx": "0", "id": "0", "platform": "state"}}}]}

    def as_short_dict(self) -> dict[str, Any]:
        return dict(self._short)

    def as_extended_dict(self) -> dict[str, Any]:
        return {**self._short, "trace": self._steps}


NOT_TRIGGERED_STEP = {
    "trigger/0": [
        {
            "path": "trigger/0",
            "changed_variables": {"trigger": {"idx": "0", "id": "0", "platform": "humidity.crossed_threshold"}},
            "result": {"reason": "entity_value_not_numeric", "data": {"entity_id": HUMIDITY, "value": "abc"}},
        }
    ]
}


@pytest.mark.parametrize(
    ("execution", "kwargs", "outcome", "stopped_by"),
    [
        ("finished", {}, "fired", None),
        ("failed_conditions", {"trace": {"condition/0": [{"path": "condition/0", "result": {"result": False}}]}}, "condition_failed", "condition/0"),
        ("failed_single", {}, "dropped", "mode_single"),
        ("failed_max_runs", {}, "dropped", "max_runs"),
        ("disallowed_recursion_detected", {}, "dropped", "recursion"),
        ("aborted", {}, "aborted", "action/0"),
        ("cancelled", {}, "cancelled", "action/0"),
        ("error", {"error": "boom"}, "error", "action/0"),
        (None, {"error": "boom"}, "error", "action/0"),
        ("not_triggered", {"trace": NOT_TRIGGERED_STEP}, "not_triggered", "entity_value_not_numeric"),
        ("finished", {"state": "running"}, "unfinished", None),
        # A value a later release adds is passed through, not reported as an error.
        ("some_new_state", {}, "some_new_state", "action/0"),
    ],
)
@pytest.mark.parametrize("traces", ["full", "summary", "none"])
def test_every_script_execution_has_its_outcome(execution, kwargs, outcome, stopped_by, traces):
    record = _run_record(_Trace(execution, **kwargs), traces)
    assert record["outcome"] == outcome
    if traces != "none":
        assert record["stopped_by"] == stopped_by
        assert record["script_execution"] == execution
