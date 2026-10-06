"""#31: a recorded service call suspends its run as the live call does, so runs started meanwhile meet it in flight.

Live, `ServiceRegistry.async_call` fires `call_service` and then awaits the service's handler. A device's handler waits on I/O, so the calling run stays in flight for at least one pass of the loop: a run its own `event` step (or the `call_service` event) re-triggered starts meanwhile, and `mode: single` drops it, `restart` stops the caller. Core helpers (input_boolean, counter, ...) finish without suspending, so there the caller runs straight on.

Every case here runs twice: in a real Home Assistant in this process, with real services (a device stand-in whose handler awaits I/O, and a real input_boolean), and in the sandbox, which only records the calls. The run outcomes must be the same. A self-triggering loop is bounded by a template condition on a counter it passes along in the event, so live and sandbox both end it the same way.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

import pytest
from homeassistant.components.trace.const import DATA_TRACE
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

from custom_components.event_simulator.sandbox.engine import _run_record

from .test_engine import run_child, ts, utc
from .test_engine_fidelity import scenario

DOOR = "binary_sensor.door"
OPEN_AT = "2026-10-04T01:00:00+00:00"
# Each run passes its number on in the event it fires; the run that the event starts goes on only below this.
BOUND = 5
MODES = ("single", "queued", "restart", "parallel")
SERVICES = {
    # A device: its live handler waits on I/O.
    "device": {"action": "light.turn_on", "target": {"entity_id": "light.hall"}},
    # A core helper: its live handler only changes in-memory state.
    "helper": {"action": "input_boolean.turn_on", "target": {"entity_id": "input_boolean.hall"}},
}
PING = {"event": "ping", "event_data": {"n": "{{ (trigger.event.data.n | default(0)) + 1 if trigger.platform == 'event' else 1 }}"}}
BOUNDED = [{"condition": "template", "value_template": f"{{{{ trigger.platform != 'event' or trigger.event.data.n < {BOUND} }}}}"}]
OPENED = {"trigger": "state", "entity_id": DOOR, "to": "on"}
PINGED = {"trigger": "event", "event_type": "ping"}


def automation(mode: str, kind: str, shape: str) -> dict[str, Any]:
    """The automation of one case: `shape` says how it is triggered and what it does."""
    call = SERVICES[kind]
    if shape == "rapid":
        # Ordinary re-triggers: every change of the door, three at one instant.
        return {"mode": mode, "triggers": [{"trigger": "state", "entity_id": DOOR}], "actions": [call]}
    if shape == "call_service":
        # Re-triggered by its own call, through the `call_service` event.
        domain = call["action"].split(".")[0]
        listens = {"trigger": "event", "event_type": "call_service", "event_data": {"domain": domain}}
        return {"mode": mode, "max": 3, "triggers": [OPENED, listens], "actions": [call]}
    actions = {"event_then_call": [PING, call], "call_then_event": [call, PING], "event_only": [PING]}[shape]
    return {"mode": mode, "max": 3, "triggers": [OPENED, PINGED], "conditions": BOUNDED, "actions": actions}


CASES = {
    f"{shape}-{mode}-{kind}": (mode, kind, shape)
    for shape in ("event_then_call", "call_then_event", "event_only", "rapid")
    for mode in MODES
    for kind in SERVICES
    if not (shape == "event_only" and kind == "helper")
}
# A single-mode automation re-triggered by its own call: live, the run the `call_service` event starts meets the caller in flight only when the call suspends (otherwise it loops without end; see test_engine_runaway.py).
CASES["call_service-single-device"] = ("single", "device", "call_service")


async def live_outcomes(hass: HomeAssistant, mode: str, kind: str, shape: str) -> list[str]:
    """The run outcomes of one case in a real Home Assistant, as the sandbox reports them."""
    hass.states.async_set(DOOR, "off")

    async def device(call: ServiceCall) -> None:
        # A real device answers after a round trip; that wait is all that matters here.
        await asyncio.sleep(0.001)

    hass.services.async_register("light", "turn_on", device)
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"hall": {}}})
    config = {"id": "fidelity", "alias": "Fidelity", "trace": {"stored_traces": 1000}, **automation(mode, kind, shape)}
    assert await async_setup_component(hass, "automation", {"automation": [config]})
    await hass.async_block_till_done()
    for state in ("on", "off", "on") if shape == "rapid" else ("on",):
        hass.states.async_set(DOOR, state)

    def runs() -> dict[str, Any]:
        # Keyed by automation once its first trace is stored (HA 2026.2 stores a trace when its run ends).
        traces = hass.data[DATA_TRACE].get("automation.fidelity", {})
        return getattr(traces, "runs", traces)

    # An event trigger starts its run from a `call_soon`, which `async_block_till_done` does not wait for: settle until no run starts any more.
    seen = -1
    while seen != len(runs()):
        seen = len(runs())
        await asyncio.wait_for(hass.async_block_till_done(), 30)
        await asyncio.sleep(0.05)
    return [_run_record(trace, "none")["outcome"] for trace in runs().values()]


def sandbox_case(mode: str, kind: str, shape: str) -> dict[str, Any]:
    at = utc(OPEN_AT)
    states = ("on", "off", "on") if shape == "rapid" else ("on",)
    built = scenario(
        automation(mode, kind, shape),
        initial_states={DOOR: {"state": "off"}},
        events=[{"at": at.isoformat(), "entity_id": DOOR, "state": state, "attributes": {}} for state in states],
    )
    built["traces"] = "none"
    return built


@pytest.fixture(scope="module")
def sandbox() -> dict[str, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(CASES, pool.map(lambda name: run_child(sandbox_case(*CASES[name])), CASES), strict=True))
    results = {}
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None and result["error"] is None, f"{name}: {result and result['error']}\n{stderr[-4000:]}"
        results[name] = result
    return results


@pytest.mark.parametrize("name", list(CASES))
async def test_runs_interleave_as_live(hass: HomeAssistant, sandbox, name):
    mode, kind, shape = CASES[name]
    live = await live_outcomes(hass, mode, kind, shape)
    result = sandbox[name]
    assert [run["outcome"] for run in result["runs"]] == live
    assert {ts(run["started"]) for run in result["runs"]} == {utc(OPEN_AT)}
    assert not [warning for warning in result["warnings"] if warning["code"] in ("runaway_loop", "time_limit")]


async def test_single_drops_its_re_trigger_when_the_call_suspends(hass: HomeAssistant, sandbox):
    """The case of the issue, spelled out: the run its own event starts meets it still waiting on the device."""
    assert [run["outcome"] for run in sandbox["event_then_call-single-device"]["runs"]] == ["fired", "dropped"]
    assert [run["outcome"] for run in sandbox["rapid-single-device"]["runs"]] == ["fired", "dropped", "dropped"]
    # A helper's call never suspends, so its run is over before the next one starts.
    assert [run["outcome"] for run in sandbox["rapid-single-helper"]["runs"]] == ["fired"] * 3
    assert [run["outcome"] for run in sandbox["event_then_call-single-helper"]["runs"]] == ["fired"] * BOUND + ["condition_failed"]
