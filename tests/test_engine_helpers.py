"""#43: the scenario's `helpers` (core Trend and Derivative helpers) are set up in the sandbox, compute from the replayed source states on virtual time, and their states come back in `helper_states`.

Sandbox cases run through a real child process (same harness as test_engine.py). The fidelity cases also run the same helper, with the same options, in a real Home Assistant in this process (frozen time moved to each recorded instant), and its state changes must be the sandbox's.
"""

from __future__ import annotations

import concurrent.futures
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.event_simulator.sandbox.helpers import parse_helpers

from .test_engine import run_child, ts, utc
from .test_engine_fidelity import END, START, scenario

TEMP = "sensor.temp"
RISING = "binary_sensor.temp_rising"
NOT_RISING = "binary_sensor.temp_not_rising"
FALLING = "binary_sensor.temp_falling"
RATE = "sensor.temp_rate"
UNIT = {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}


def at(minutes: float) -> str:
    return (utc(START) + timedelta(minutes=minutes)).isoformat()


# 20 °C until 01:00, then +0.5 °C every 5 min to 25 °C at 01:45 (6 °C/h), flat until 03:00, then -0.5 °C every 5 min to 20 °C at 03:45, a last reading at 05:00.
RAMP = (
    [(60 + 5 * n, 20.5 + 0.5 * n) for n in range(10)]
    + [(180 + 5 * n, 24.5 - 0.5 * n) for n in range(10)]
    + [(300, 20.5)]
)
EVENTS = [{"at": at(minutes), "entity_id": TEMP, "state": f"{value:g}", "attributes": UNIT} for minutes, value in RAMP]
INITIAL = {TEMP: {"state": "20", "attributes": UNIT}}

TREND_RISING = {"domain": "trend", "entity_id": RISING, "title": "Temp rising", "options": {"entity_id": TEMP, "min_gradient": 0.001, "max_samples": 3, "sample_duration": 900}}
TREND_FALLING = {"domain": "trend", "entity_id": FALLING, "options": {"entity_id": TEMP, "min_gradient": -0.001, "max_samples": 3, "sample_duration": 900}}
TREND_INVERTED = {"domain": "trend", "entity_id": NOT_RISING, "options": {"entity_id": TEMP, "min_gradient": 0.001, "invert": True, "sample_duration": 900}}
DERIVATIVE = {"domain": "derivative", "entity_id": RATE, "title": "Temp rate", "options": {"source": TEMP, "time_window": {"minutes": 10}, "unit_time": "h", "round": 2}}


def on(entity_id: str, to: str = "on") -> dict[str, Any]:
    return {"triggers": [{"trigger": "state", "entity_id": entity_id, "to": to}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.fan"}}]}


def case(automation: dict[str, Any], helpers: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return scenario(automation, **{"initial_states": dict(INITIAL), "events": list(EVENTS), "helpers": helpers, **extra})


CASES: dict[str, dict[str, Any]] = {
    "rising": case(on(RISING), [TREND_RISING]),
    "falling": case(on(FALLING), [TREND_FALLING]),
    "inverted": case(on(NOT_RISING), [TREND_INVERTED]),
    "derivative": case(
        {"triggers": [{"trigger": "numeric_state", "entity_id": RATE, "above": 5}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.fan"}}]},
        [DERIVATIVE],
    ),
    # With max_sub_interval, a timer recomputes it while the source holds still, so it falls back to 0 after the rise. 7 min, so no timer is due at the same instant as a reading (every 5 min): which of the two runs first is arbitrary, live as well.
    "decay": case(
        {"triggers": [{"trigger": "numeric_state", "entity_id": RATE, "below": 1}], "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.fan"}}]},
        [{**DERIVATIVE, "options": {**DERIVATIVE["options"], "max_sub_interval": {"minutes": 7}}}],
    ),
    # A trend on an attribute, its source's initial state recorded with an attribute update after its last change (seeded as two writes).
    "attribute": scenario(
        on("binary_sensor.living_warming"),
        helpers=[{"domain": "trend", "entity_id": "binary_sensor.living_warming", "options": {"entity_id": "climate.living", "attribute": "current_temperature", "min_gradient": 0.001}}],
        initial_states={
            "climate.living": {
                "state": "heat",
                "attributes": {"current_temperature": 20, "temperature": 21},
                "last_changed": "2026-10-03T20:00:00+00:00",
                "last_updated": "2026-10-03T23:00:00+00:00",
            }
        },
        events=[{"at": at(60 + 5 * n), "entity_id": "climate.living", "state": "heat", "attributes": {"current_temperature": 20.5 + 0.5 * n, "temperature": 21}} for n in range(3)],
    ),
    # The helper exists live too: its recorded states give way to the computed ones, and the one at the start is its restored state.
    "recorded": case(
        on(RISING),
        [TREND_RISING],
        initial_states={**INITIAL, RISING: {"state": "on", "attributes": {"friendly_name": "Temp rising"}}},
        events=[*EVENTS, {"at": at(30), "entity_id": RISING, "state": "off"}],
    ),
    "no_source": scenario(on(RISING), helpers=[TREND_RISING]),
    "unknown_domain": case(on(RISING), [{"domain": "statistics", "entity_id": "sensor.x", "options": {}}]),
    "wrong_entity_domain": case(on(RISING), [{**TREND_RISING, "entity_id": "sensor.temp_rising"}]),
    "unknown_option": case(on(RISING), [{**TREND_RISING, "options": {**TREND_RISING["options"], "min_gradiant": 1}}]),
    "bad_option": case(on(RISING), [{**DERIVATIVE, "options": {**DERIVATIVE["options"], "unit_time": "fortnight"}}]),
    "not_a_list": case(on(RISING), {"domain": "trend"}),
}


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(CASES, pool.map(run_child, CASES.values()), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None, f"{name}: no result\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def fired(result: dict[str, Any]) -> list[Any]:
    return [ts(run["started"]) for run in result["runs"] if run["outcome"] == "fired"]


def changes(result: dict[str, Any], entity_id: str) -> list[tuple[Any, str]]:
    return [(ts(item["at"]), item["state"]) for item in result["helper_states"][entity_id]]


def ok(result: dict[str, Any]) -> None:
    assert result["error"] is None, result["error"]
    assert not [w for w in result["warnings"] if w["code"] in ("late_attach", "log")], result["warnings"]


def test_a_trend_turns_on_once_two_recent_samples_rise_fast_enough(results):
    result = results["rising"]
    ok(result)
    # 01:00 is the ramp's first sample; the 20 °C one (00:00) is older than sample_duration by then. 01:05 brings the second: 0.5 °C / 300 s > 0.001.
    assert fired(result) == [utc(at(65))]
    assert result["runs"][0]["trigger"]["entity_id"] == RISING
    states = changes(result, RISING)
    assert states[0] == (utc(START), "unknown")
    assert (utc(at(65)), "on") in states
    # Flat from 01:45: no new sample, so it stays on. The fall's first sample (03:00) is alone within sample_duration; the second (03:05) takes it off.
    assert states[-2:] == [(utc(at(65)), "on"), (utc(at(185)), "off")]


def test_a_trend_with_a_negative_gradient_turns_on_falling(results):
    result = results["falling"]
    ok(result)
    assert fired(result) == [utc(at(185))]
    assert (utc(at(185)), "on") in changes(result, FALLING)


def test_an_inverted_trend_is_on_while_it_is_not_rising(results):
    result = results["inverted"]
    ok(result)
    states = changes(result, NOT_RISING)
    assert states[:3] == [(utc(START), "unknown"), (utc(at(65)), "off"), (utc(at(185)), "on")]
    assert fired(result) == [utc(at(185))]


def test_a_derivative_crossing_a_threshold_fires_a_numeric_state_trigger(results):
    result = results["derivative"]
    ok(result)
    # 6 °C/h over a 10 min window: 0.5 at 01:00 (the hour since 00:00), 3.25 at 01:05 (half the window still on that hour), 6 at 01:10.
    states = changes(result, RATE)
    assert states[:4] == [(utc(START), "0.00"), (utc(at(60)), "0.50"), (utc(at(65)), "3.25"), (utc(at(70)), "6.00")]
    assert fired(result) == [utc(at(70))]
    assert any(state.startswith("-") for _, state in states)


def test_a_derivative_with_max_sub_interval_decays_on_virtual_time(results):
    result = results["decay"]
    ok(result)
    states = changes(result, RATE)
    # The rise ends at 01:45 (6 °C/h); at 01:52 its last segment still has 3 of the window's 10 min, at 01:59 none.
    assert [(when, state) for when, state in states if utc(at(105)) < when < utc(at(180))] == [(utc(at(112)), "1.80"), (utc(at(119)), "0.00")]
    assert fired(result) == [utc(at(119))]


def test_a_trend_on_an_attribute_reads_it_from_every_seeded_write(results):
    result = results["attribute"]
    ok(result)
    # The two seeded samples (20 at 20:00 and 23:00) are flat: off at the start; 20.5 at 01:00 after 20 at 23:00 is too slow, 21 five minutes later is not.
    assert changes(result, "binary_sensor.living_warming") == [(utc(START), "off"), (utc(at(65)), "on")]
    assert fired(result) == [utc(at(65))]


def test_a_helper_recorded_live_is_computed_instead_and_starts_from_its_recorded_state(results):
    result = results["recorded"]
    assert result["error"] is None, result["error"]
    states = changes(result, RISING)
    assert states[0] == (utc(START), "on")
    # The recorded `off` at 00:30 is not played.
    assert utc(at(30)) not in [when for when, _ in states]
    assert [w["code"] for w in result["warnings"]] == ["helper_replaces_recorded"]
    assert RISING in result["warnings"][0]["message"]


def test_a_helper_whose_source_is_not_on_the_timeline_says_so(results):
    result = results["no_source"]
    assert result["error"] is None, result["error"]
    assert result["runs"] == []
    assert [w["code"] for w in result["warnings"]] == ["helper_source_missing"]
    assert TEMP in result["warnings"][0]["message"]


def test_without_helpers_the_result_has_no_helper_states():
    code, result, stderr = run_child(scenario(on("binary_sensor.door")))
    assert code == 0, stderr[-4000:]
    assert result["helper_states"] == {}


@pytest.mark.parametrize(
    ("name", "names"),
    [
        ("unknown_domain", "helpers[0].domain"),
        ("wrong_entity_domain", "helpers[0].entity_id"),
        ("unknown_option", "min_gradiant"),
        ("bad_option", "helpers[0].options.unit_time"),
        ("not_a_list", "helpers must be a list"),
    ],
)
def test_malformed_helpers_are_refused(results, name, names):
    assert results[name]["error"]["code"] == "invalid_scenario", results[name]["error"]
    assert names in results[name]["error"]["message"]


def test_options_are_checked_and_filled_in_as_the_config_flow_stores_them():
    trend, derivative = parse_helpers([TREND_INVERTED, DERIVATIVE], valid_entity_id)
    assert trend.options == {"entity_id": TEMP, "invert": True, "max_samples": 2, "min_samples": 2, "min_gradient": 0.001, "sample_duration": 900.0}
    assert trend.title == "temp_not_rising"
    assert derivative.options == {"source": TEMP, "round": 2, "time_window": {"minutes": 10}, "unit_time": "h"}
    assert parse_helpers([{**DERIVATIVE, "options": {"source": TEMP, "time_window": 90}}], valid_entity_id)[0].options["time_window"] == {"seconds": 90}
    for bad in (
        {**TREND_RISING, "options": {"entity_id": TEMP, "max_samples": 1}},
        {**TREND_RISING, "options": {"entity_id": TEMP, "invert": "yes"}},
        {**TREND_RISING, "options": {"entity_id": RISING}},
        {**DERIVATIVE, "options": {"source": TEMP, "unit_prefix": "P"}},
        {**DERIVATIVE, "options": {"source": TEMP, "round": 7}},
        {**DERIVATIVE, "options": {"source": TEMP, "time_window": {"weeks": 1}}},
    ):
        with pytest.raises(ValueError, match=r"helpers\[0\]"):
            parse_helpers([bad], valid_entity_id)
    with pytest.raises(ValueError, match="already another helper"):
        parse_helpers([TREND_RISING, TREND_RISING], valid_entity_id)


# --- fidelity: the same helper in a real Home Assistant ---------------------------------------------


async def live_helper_states(hass: HomeAssistant, freezer: FrozenDateTimeFactory, built: dict[str, Any]) -> list[tuple[Any, str]]:
    """The helper's state changes in a real Home Assistant, fed the scenario's timeline at its times; time moves on a minute at a time, firing the timers due (a derivative's max_sub_interval)."""
    (helper,) = parse_helpers(built["helpers"], valid_entity_id)
    freezer.move_to(utc(START))
    entry = MockConfigEntry(domain=helper.domain, title=helper.title, options=helper.options, version=1, minor_version={"trend": 2, "derivative": 4}[helper.domain])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    (registered,) = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    for entity_id, item in built["initial_states"].items():
        hass.states.async_set(entity_id, item["state"], item.get("attributes"))
    await hass.async_block_till_done()
    seen = [(utc(START), hass.states.get(registered.entity_id).state)]
    by_minute: dict[Any, list[dict[str, Any]]] = {}
    for event in built["events"]:
        by_minute.setdefault(utc(event["at"]), []).append(event)
    now = utc(START)
    while now < utc(END):
        now += timedelta(minutes=1)
        freezer.move_to(now)
        async_fire_time_changed(hass, now)
        for event in by_minute.get(now, ()):
            hass.states.async_set(event["entity_id"], event["state"], event.get("attributes"))
        await hass.async_block_till_done()
        state = hass.states.get(registered.entity_id).state
        if state != seen[-1][1]:
            seen.append((now, state))
    return seen


@pytest.mark.parametrize("name", ["rising", "falling", "inverted", "derivative", "decay"])
async def test_helper_states_are_those_of_the_same_helper_live(hass: HomeAssistant, freezer: FrozenDateTimeFactory, results, name):
    built = CASES[name]
    live = await live_helper_states(hass, freezer, built)
    entity_id = built["helpers"][0]["entity_id"]
    assert changes(results[name], entity_id) == live
    assert utc(END) > live[-1][0]
