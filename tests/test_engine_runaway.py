"""#30: a replay whose runs keep going without virtual time moving on ends, with the runs so far and a warning saying why.

An automation that re-triggers itself (it fires an event, or makes a service call, its own trigger listens for) can start run after run at one virtual instant: recorded service calls take no virtual time, so nothing ever lets the clock move. Where live Home Assistant loops too (`queued`, `restart`, or no call left in flight when the next run starts), the replay must end and explain. Where it ends the loop itself (`single` and `parallel` meet their re-triggered run while it waits on a device call, see test_engine_interleave.py), so does the replay. The engine turns such an automation off after `MAX_RUNS_PER_INSTANT` runs at one instant (`runaway_loop`), and a wall-clock watchdog stops a replay that still runs at `time_limit_seconds` (`time_limit`), even one stuck in code that never yields to the loop.

Same harness as test_engine.py: every scenario runs through a real child process.
"""

from __future__ import annotations

import concurrent.futures
import time
from typing import Any

import pytest

from custom_components.event_simulator.sandbox.engine import MAX_RUNS_PER_INSTANT

from .test_engine import run_child, ts, utc
from .test_engine_fidelity import scenario
from .test_engine_multi import multi, runs_of, toggles

WINDOW = "2026-10-04T{}:00+00:00"
DOOR = "binary_sensor.door"
OWN = "automation.fidelity"
LIGHT = {"action": "light.turn_on", "target": {"entity_id": "light.hall"}}
# A call whose live handler never suspends, so the run never yields to the loop on it.
HELPER = {"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.hall"}}
PING = {"event": "ping"}
OPENED = {"trigger": "state", "entity_id": DOOR, "to": "on"}
PINGED = {"trigger": "event", "event_type": "ping"}
# Real seconds a replay may take here; a hung child would run into the harness's own (much longer) timeout instead.
QUICK = 30


def looping(mode: str = "single", actions: list[Any] | None = None, triggers: list[Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Re-triggers itself through `ping` once the door opens at 01:00; the door opens again at 03:00."""
    built = scenario(
        {"mode": mode, "triggers": triggers or [OPENED, PINGED], "actions": actions or [PING, LIGHT]},
        **{"initial_states": {DOOR: {"state": "off"}}, "events": toggles(DOOR, "01:00", "03:00"), **extra},
    )
    built["traces"] = "none"
    return built


def timed(built: dict[str, Any]) -> tuple[float, tuple[int, dict[str, Any] | None, str]]:
    began = time.monotonic()
    outcome = run_child(built)
    return time.monotonic() - began, outcome


SCENARIOS: dict[str, Any] = {
    **{f"event_{mode}": (lambda mode=mode: looping(mode)) for mode in ("single", "queued", "restart", "parallel")},
    "event_only": lambda: looping(actions=[PING]),
    "service_call": lambda: looping(
        "queued", triggers=[OPENED, {"trigger": "event", "event_type": "call_service", "event_data": {"domain": "light"}}], actions=[LIGHT]
    ),
    # Its own recorded `on` at 02:30 would switch it back on; a runaway automation stays off.
    "own_entity_on": lambda: looping(
        "queued",
        initial_states={DOOR: {"state": "off"}, OWN: {"state": "on"}},
        events=sorted(
            [*toggles(DOOR, "01:00", "03:00"), {"at": WINDOW.format("02:00"), "entity_id": OWN, "state": "off"}, {"at": WINDOW.format("02:30"), "entity_id": OWN, "state": "on"}],
            key=lambda event: event["at"],
        ),
    ),
    "multi": lambda: {
        **multi(
            [
                {"alias": "Door light", "triggers": [OPENED], "actions": [LIGHT]},
                {"alias": "Ping loop", "mode": "queued", "triggers": [OPENED, PINGED], "actions": [PING, LIGHT]},
            ],
            initial_states={DOOR: {"state": "off"}},
            events=toggles(DOOR, "01:00", "03:00"),
        ),
        "traces": "none",
    },
    # Virtual time moves on (a run every second), so only the wall clock stops it: a month of runs takes far longer than the limit.
    "time_limit_yielding": lambda: {
        **scenario(
            {"triggers": [{"trigger": "time_pattern", "seconds": "/1"}], "actions": [LIGHT]},
            start="2026-09-01T00:00:00+00:00",
            end="2026-10-01T00:00:00+00:00",
        ),
        "traces": "none",
        "time_limit_seconds": 3,
    },
    # A run that never yields to the event loop (Home Assistant puts no cap on a repeat count, and a helper's call never suspends): only the watchdog's hard stop reaches it. Started by a time trigger: inside a state change's dispatch, HA 2026.9 would end it itself once it had queued 10000 events.
    "time_limit_stuck": lambda: {
        **looping(triggers=[{"trigger": "time", "at": "01:00:00"}], actions=[{"repeat": {"count": 100_000_000, "sequence": [HELPER]}}]),
        "time_limit_seconds": 3,
    },
}


@pytest.fixture(scope="module")
def results() -> dict[str, tuple[float, dict[str, Any]]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(SCENARIOS, pool.map(lambda name: timed(SCENARIOS[name]()), SCENARIOS), strict=True))
    for name, (_, (code, result, stderr)) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None and result["error"] is None, f"{name}: {result and result['error']}\n{stderr[-4000:]}"
    return {name: (elapsed, result) for name, (elapsed, (_, result, _)) in outcomes.items()}


def at(minute: str) -> Any:
    return utc(WINDOW.format(minute))


def warnings_of(result: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [warning for warning in result["warnings"] if warning["code"] == code]


@pytest.mark.parametrize("name", ["event_queued", "event_restart", "event_only", "service_call"])
def test_a_self_triggering_automation_is_stopped_at_the_instant_it_loops(results, name):
    elapsed, result = results[name]
    assert elapsed < QUICK
    (warning,) = warnings_of(result, "runaway_loop")
    assert ts(warning["at"]) == at("01:00")
    assert OWN in warning["message"]
    assert str(MAX_RUNS_PER_INSTANT) in warning["message"]
    started = {ts(run["started"]) for run in result["runs"]}
    # Every run so far is kept; it was turned off there, so the door opening at 03:00 starts nothing.
    assert started == {at("01:00")}
    assert MAX_RUNS_PER_INSTANT <= len(result["runs"]) <= MAX_RUNS_PER_INSTANT + 5
    assert result["runs"][0]["trigger"]["idx"] == "0"


@pytest.mark.parametrize(("name", "running"), [("event_single", 1), ("event_parallel", 10)])
def test_single_and_parallel_end_their_own_loop_as_live(results, name, running):
    """Each run fires `ping` and then waits on its light call: the run `ping` starts finds the caller still in flight. `single` drops it at once; `parallel` drops the one past its `max` (10). Nothing re-triggers after a dropped run, so the loop ends there, at each door opening."""
    _, result = results[name]
    assert not warnings_of(result, "runaway_loop")
    expected = [*[(at("01:00"), "fired")] * running, (at("01:00"), "dropped"), *[(at("03:00"), "fired")] * running, (at("03:00"), "dropped")]
    assert [(ts(run["started"]), run["outcome"]) for run in result["runs"]] == expected


def test_a_runaway_automation_is_not_switched_back_on_by_its_own_track(results):
    _, result = results["own_entity_on"]
    assert len(warnings_of(result, "runaway_loop")) == 1
    assert {ts(run["started"]) for run in result["runs"]} == {at("01:00")}


def test_the_runaway_is_attributed_and_the_other_automations_keep_replaying(results):
    elapsed, result = results["multi"]
    assert elapsed < QUICK
    (warning,) = warnings_of(result, "runaway_loop")
    assert warning["automation"] == 1
    assert warning["message"].startswith("automations[1]: ")
    assert "automation.ping_loop" in warning["message"]
    plain, loop = result["automations"]
    assert plain["error"] is None
    assert plain["run_count"] == 2
    assert loop["loaded"] is True
    assert loop["error"]["code"] == "runaway_loop"
    assert loop["run_count"] >= MAX_RUNS_PER_INSTANT
    assert runs_of(result, 0) == [(at("01:00"), "fired"), (at("03:00"), "fired")]
    assert {started for started, _ in runs_of(result, 1)} == {at("01:00")}


def test_a_replay_still_running_at_the_time_limit_ends_with_the_runs_so_far(results):
    elapsed, result = results["time_limit_yielding"]
    assert elapsed < QUICK
    (warning,) = warnings_of(result, "time_limit")
    stopped = ts(warning["at"])
    assert utc("2026-09-01T00:00:00+00:00") < stopped < utc("2026-10-01T00:00:00+00:00")
    assert result["runs"]
    assert all(ts(run["started"]) <= stopped for run in result["runs"])
    assert "3 s" in warning["message"]


def test_a_run_that_never_yields_is_stopped_by_the_time_limit(results):
    elapsed, result = results["time_limit_stuck"]
    assert elapsed < QUICK
    assert not warnings_of(result, "runaway_loop")
    (warning,) = warnings_of(result, "time_limit")
    assert OWN in warning["message"]
    # Time triggers fire a fraction of a second past the minute.
    assert at("01:00") <= ts(warning["at"]) < at("01:01")
    (run,) = result["runs"]
    assert at("01:00") <= ts(run["started"]) < at("01:01")
    assert run["outcome"] == "cancelled"


def test_the_time_limit_is_checked():
    for value in (0, -1, "3", True):
        code, result, _ = run_child({**scenario({"triggers": [OPENED], "actions": [LIGHT]}), "time_limit_seconds": value})
        assert code == 0
        assert result["error"]["code"] == "invalid_scenario"
        assert "time_limit_seconds" in result["error"]["message"]

