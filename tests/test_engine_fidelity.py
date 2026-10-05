"""Replays that depend on live-instance context beyond states: Labs preview features (#11) and registries (#12).

Same harness as test_engine.py: every scenario runs through a real child process.
"""

from __future__ import annotations

import concurrent.futures
import copy
from collections.abc import Callable
from typing import Any

import pytest

from .test_engine import calls, run_child, summary, utc

START = "2026-10-04T00:00:00+00:00"
END = "2026-10-04T06:00:00+00:00"
NEW_TRIGGERS = ["automation", "new_triggers_conditions"]


def scenario(automation: dict[str, Any], **extra: Any) -> dict[str, Any]:
    base = {
        "version": 1,
        "core": {"time_zone": "UTC", "latitude": 52.37, "longitude": 4.89, "elevation": 0, "unit_system": "metric"},
        "start": START,
        "end": END,
        "automation": {"id": "fidelity", "alias": "Fidelity", **automation},
        "initial_states": {},
        "events": [],
    }
    base.update(extra)
    return base


def occupancy(preview_features: list[list[str]] | None) -> dict[str, Any]:
    """Experimental purpose-specific trigger (binary_sensor.occupancy_detected), behind the Labs flag in HA 2026.2."""
    attrs = {"device_class": "occupancy"}
    built = scenario(
        {
            "triggers": [{"trigger": "binary_sensor.occupancy_detected", "target": {"entity_id": "binary_sensor.hall_motion"}, "options": {"behavior": "any"}}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.hall"}}],
        },
        initial_states={"binary_sensor.hall_motion": {"state": "off", "attributes": attrs}},
        events=[
            {"at": "2026-10-04T01:00:00+00:00", "entity_id": "binary_sensor.hall_motion", "state": "on", "attributes": attrs},
            {"at": "2026-10-04T01:10:00+00:00", "entity_id": "binary_sensor.hall_motion", "state": "off", "attributes": attrs},
        ],
    )
    if preview_features is not None:
        built["core"]["preview_features"] = preview_features
    return built


SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "labs_enabled": lambda: occupancy([NEW_TRIGGERS]),
    "labs_disabled": lambda: occupancy(None),
    "labs_unknown_feature": lambda: occupancy([NEW_TRIGGERS, ["nope", "not_a_feature"]]),
}


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(SCENARIOS, pool.map(lambda name: run_child(SCENARIOS[name]()), SCENARIOS), strict=True))
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result is not None, f"{name}: no result\n{stderr[-4000:]}"
    return {name: result for name, (_, result, _) in outcomes.items()}


def codes(result: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in result["warnings"]]


# --- #11 Labs preview features -------------------------------------------------------------------


def test_preview_feature_in_the_scenario_enables_experimental_triggers(results):
    result = results["labs_enabled"]
    assert result["error"] is None, result["error"]
    assert summary(result) == [(utc("2026-10-04T01:00:00+00:00"), "fired", None)]
    assert calls(result) == [(utc("2026-10-04T01:00:00+00:00"), "light.turn_on")]
    assert result["runs"][0]["trigger"]["entity_id"] == "binary_sensor.hall_motion"


def test_experimental_trigger_without_the_feature_is_invalid(results):
    result = results["labs_disabled"]
    assert result["error"]["code"] == "invalid_automation"
    assert "new_triggers_conditions" in result["error"]["message"]


def test_unknown_preview_feature_is_a_warning(results):
    result = results["labs_unknown_feature"]
    assert result["error"] is None, result["error"]
    assert len(result["runs"]) == 1
    unknown = [warning for warning in result["warnings"] if warning["code"] == "preview_feature_unknown"]
    assert len(unknown) == 1
    assert "nope.not_a_feature" in unknown[0]["message"]


def test_malformed_preview_features_are_refused():
    bad = occupancy(None)
    bad["core"]["preview_features"] = ["automation.new_triggers_conditions"]
    code, result, _ = run_child(bad)
    assert code == 0
    assert result["error"]["code"] == "invalid_scenario"
    assert "preview_features" in result["error"]["message"]
