"""#28: `scenario.traces` trims what each run carries, so long multi-automation validations fit the runner's output cap.

`full` (the default) is the original shape. `summary` keeps the run's times, trigger, outcome, condition results and the service calls, without the step-by-step trace. `none` keeps only times, outcome and the trigger's id and index, and no service calls. Same harness as test_engine.py: every scenario runs through a real child process.
"""

from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from .test_engine import CHILD_TIMEOUT, run_child, ts, utc
from .test_engine_fidelity import scenario
from .test_engine_multi import MOTION, WINDOW, multi, toggles
from .test_engine_self import DOOR

REPO = Path(__file__).parent.parent
MODES = ("full", "summary", "none")


def _case(traces: str | None) -> dict[str, Any]:
    events = [*toggles(DOOR, "01:00", "03:00", "05:00"), {"at": utc(WINDOW.format("02:30")).isoformat(), "entity_id": MOTION, "state": "on", "attributes": {}}]
    built = multi(
        [
            {
                "id": "conditional",
                "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}],
                "conditions": [{"condition": "state", "entity_id": MOTION, "state": "on"}],
                "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.conditional"}}],
            },
            {
                "id": "slow",
                "mode": "parallel",
                "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on", "id": "opened"}],
                "actions": [{"delay": "02:00:00"}, {"action": "light.turn_on", "target": {"entity_id": "light.slow"}}],
            },
        ],
        initial_states={DOOR: {"state": "off"}, MOTION: {"state": "off"}},
        events=sorted(events, key=lambda event: event["at"]),
        overrun_seconds=60,
    )
    if traces is not None:
        built["traces"] = traces
    return built


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    cases = {"default": _case(None), **{mode: _case(mode) for mode in MODES}}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(cases, pool.map(run_child, cases.values()), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None and result["error"] is None, f"{name}: {result and result['error']}\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def _timeline(result: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [(run["id"], run["automation"], ts(run["started"]), ts(run["finished"]), run["outcome"]) for run in result["runs"]]


def test_full_is_the_default_and_keeps_the_trace(results):
    default, full = results["default"], results["full"]
    # Whole runs differ between children only in the trace's context ids (ULIDs).
    assert [{key: value for key, value in run.items() if key != "trace"} for run in default["runs"]] == [
        {key: value for key, value in run.items() if key != "trace"} for run in full["runs"]
    ]
    assert all(run["trace"]["trace"] for run in (*default["runs"], *full["runs"]))


@pytest.mark.parametrize("mode", ["summary", "none"])
def test_every_mode_reports_the_same_runs(results, mode):
    full = results["full"]
    assert _timeline(results[mode]) == _timeline(full)
    assert results[mode]["automations"] == full["automations"]
    # The case covers every kind of ending the trimming has to carry over.
    assert {run["outcome"] for run in full["runs"]} == {"fired", "condition_failed", "unfinished"}


def test_summary_drops_only_the_step_trace(results):
    full, summary = results["full"], results["summary"]
    for full_run, run in zip(full["runs"], summary["runs"], strict=True):
        assert "trace" not in run
        assert {key: value for key, value in run.items() if key != "conditions"} == {key: value for key, value in full_run.items() if key != "trace"}
    # Context ids are ULIDs, so they differ between children.
    assert [{**call, "context": None} for call in summary["service_calls"]] == [{**call, "context": None} for call in full["service_calls"]]
    assert summary["warnings"] == full["warnings"]


def test_summary_keeps_condition_results(results):
    conditional = [run for run in results["summary"]["runs"] if run["automation"] == 0]
    assert [(ts(run["started"]), run["outcome"], run["stopped_by"], run["conditions"]) for run in conditional] == [
        (utc(WINDOW.format("01:00")), "condition_failed", "condition/0", {"condition/0": False}),
        (utc(WINDOW.format("03:00")), "fired", None, {"condition/0": True}),
        (utc(WINDOW.format("05:00")), "fired", None, {"condition/0": True}),
    ]
    assert all(run["conditions"] == {} for run in results["summary"]["runs"] if run["automation"] == 1)


def test_none_keeps_times_outcome_and_trigger_only(results):
    full, bare = results["full"], results["none"]
    for full_run, run in zip(full["runs"], bare["runs"], strict=True):
        assert set(run) == {"id", "automation", "started", "finished", "outcome", "trigger"}
        assert run["trigger"] == {"idx": full_run["trigger"]["idx"], "id": full_run["trigger"]["id"]}
    assert {run["trigger"]["id"] for run in bare["runs"] if run["automation"] == 1} == {"opened"}
    assert full["service_calls"] and bare["service_calls"] == []
    assert bare["warnings"] == full["warnings"]


@pytest.mark.parametrize("value", ["bogus", True, False])
def test_an_unknown_mode_is_refused(value):
    code, result, stderr = run_child({**_case(None), "traces": value})
    assert code == 0, stderr[-4000:]
    assert result["error"]["code"] == "invalid_scenario"
    assert "traces" in result["error"]["message"]


def test_a_single_automation_takes_the_option_too():
    built = scenario(
        {"triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.x"}}]},
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, "01:00"),
        traces="summary",
    )
    code, result, stderr = run_child(built)
    assert code == 0, stderr[-4000:]
    [run] = result["runs"]
    assert run["outcome"] == "fired" and "trace" not in run and "automation" not in run
    assert [call["service"] for call in result["service_calls"]] == ["turn_on"]


# Counts the automation traces still alive when the result is built: a trimmed run must not keep its trace object around until then.
_HELD_TRACES = """
import gc, json, sys
from homeassistant.components.automation.trace import AutomationTrace
from custom_components.event_simulator.sandbox import engine

held = []
original = engine._Replay._result

def spy(self, collector):
    gc.collect()
    held.append(sum(isinstance(item, AutomationTrace) for item in gc.get_objects()))
    return original(self, collector)

engine._Replay._result = spy
result = engine.run_scenario(json.load(sys.stdin))
print(json.dumps({"held": held[0], "runs": len(result["runs"]), "error": result["error"]}))
"""


@pytest.mark.parametrize(("mode", "bound"), [("full", None), ("summary", 5), ("none", 5)])
def test_trimmed_runs_do_not_hold_their_traces(mode, bound):
    times = [f"{hour:02d}:{minute:02d}" for hour in range(1, 5) for minute in range(0, 60, 10)]
    built = scenario(
        {"triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.x"}}]},
        initial_states={DOOR: {"state": "off"}},
        events=toggles(DOOR, *times),
        traces=mode,
    )
    proc = subprocess.run(
        [sys.executable, "-c", _HELD_TRACES], input=json.dumps(built).encode(), capture_output=True, timeout=CHILD_TIMEOUT, cwd=REPO, check=False
    )
    assert proc.returncode == 0, proc.stderr.decode()[-4000:]
    report = json.loads(proc.stdout.decode().strip().splitlines()[-1])
    assert report["error"] is None and report["runs"] == len(times)
    if bound is None:
        assert report["held"] >= len(times)
    else:
        # Only Home Assistant's own trace store (stored_traces, 5 by default) keeps any.
        assert report["held"] <= bound
