"""Suggest miner: a timeline as per-entity step functions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.event_simulator.suggest.timeline import (
    NON_NUMERIC,
    Timeline,
    numeric,
    to_ts,
)

START = "2026-10-04T00:00:00+00:00"
END = "2026-10-05T00:00:00+00:00"
T0 = to_ts(START)


def at(minutes: float) -> str:
    return datetime.fromtimestamp(T0 + minutes * 60, UTC).isoformat()


def build(initial=None, events=()):
    return Timeline.from_scenario(initial or {}, list(events), START, END)


def ev(minutes, entity_id, state, **attributes):
    return {"at": at(minutes), "entity_id": entity_id, "state": state, "attributes": attributes}


def test_to_ts_accepts_iso_z_naive_datetime_and_numbers():
    assert to_ts("2026-10-04T00:00:00Z") == T0
    assert to_ts("2026-10-04T00:00:00") == T0  # naive is UTC
    assert to_ts(datetime(2026, 10, 4, tzinfo=UTC)) == T0
    assert to_ts(T0) == T0


def test_state_at_is_a_right_continuous_step_function():
    timeline = build(
        {"binary_sensor.door": {"state": "off", "attributes": {}, "last_changed": "2026-10-03T21:00:00+00:00"}},
        [ev(10, "binary_sensor.door", "on"), ev(20, "binary_sensor.door", "off")],
    )
    door = timeline["binary_sensor.door"]

    assert door.state_at(T0 - 1) is None  # before the window: nothing seeded yet
    assert door.state_at(T0) == "off"
    assert door.state_at(T0 + 599) == "off"
    assert door.state_at(T0 + 600) == "on"  # the write at t is visible at t
    assert door.state_at(T0 + 1199) == "on"
    assert door.state_at(T0 + 1200) == "off"
    assert door.state_at(T0 + 86_000) == "off"
    # The seeded state keeps the recorded last_changed, so `for:` sees how long it held.
    assert door.last_changed_at(T0) == to_ts("2026-10-03T21:00:00+00:00")
    assert door.last_changed_at(T0 + 700) == T0 + 600


def test_entity_without_initial_state_starts_at_its_first_event():
    timeline = build(events=[ev(5, "sensor.t", "20")])
    assert timeline["sensor.t"].state_at(T0 + 10) is None
    assert timeline["sensor.t"].state_at(T0 + 300) == "20"
    assert "sensor.missing" not in timeline
    assert timeline.get("sensor.missing") is None


def test_attribute_only_writes_keep_last_changed_and_identical_writes_are_dropped():
    timeline = build(
        {"light.l": {"state": "on", "attributes": {"brightness": 10}}},
        [
            ev(1, "light.l", "on", brightness=10),  # identical: HA fires no state_changed
            ev(2, "light.l", "on", brightness=200),
            ev(3, "light.l", "off"),
        ],
    )
    light = timeline["light.l"]
    assert len(light) == 3
    assert light.value_at(T0 + 150, "brightness") == 200
    assert light.last_changed_at(T0 + 150) == T0  # attribute writes do not change it
    assert light.last_changed_at(T0 + 200) == T0 + 180
    assert light.value_at(T0 + 200, "brightness") is None


def test_events_outside_the_window_are_skipped_and_unsorted_input_is_sorted():
    timeline = build(
        events=[
            ev(30, "sensor.t", "2"),
            ev(10, "sensor.t", "1"),
            {"at": "2026-10-03T23:00:00+00:00", "entity_id": "sensor.t", "state": "0", "attributes": {}},
            {"at": END, "entity_id": "sensor.t", "state": "9", "attributes": {}},
        ]
    )
    # With no seeded state, the first write creates the entity (old state None).
    assert [(c.old_state, c.state) for c in timeline["sensor.t"].transitions()] == [(None, "1"), ("1", "2")]
    assert timeline["sensor.t"].state_at(T0 + 86_399) == "2"


def test_index_lookup_is_binary_search_on_large_histories():
    events = [ev(i / 6, "sensor.p", str(i)) for i in range(1, 8000)]
    power = build(events=events)["sensor.p"]
    assert power.state_at(T0 + 10 * 5000 + 5) == "5000"
    assert power.numeric_at(T0 + 10 * 5000 + 5) == 5000.0


def test_numeric_view():
    assert numeric("21.5") == 21.5
    assert numeric(3) == 3.0
    assert numeric("unavailable") is None
    assert numeric("unknown") is None
    assert numeric(None) is None
    assert numeric("open") is NON_NUMERIC
    timeline = build(
        {"sensor.h": {"state": "60", "attributes": {"battery": "88"}}},
        [ev(1, "sensor.h", "unavailable"), ev(2, "sensor.h", "71.5")],
    )
    hum = timeline["sensor.h"]
    assert hum.numeric_at(T0) == 60.0
    assert hum.numeric_at(T0, "battery") == 88.0
    assert hum.numeric_at(T0 + 61) is None
    assert hum.numeric_at(T0 + 121) == 71.5
    assert hum.numeric_values() == [60.0, None, 71.5]
    assert hum.numeric_at(T0 - 10) is None


def test_transitions_in_a_range_carry_old_and_new():
    timeline = build(
        {"binary_sensor.m": {"state": "off", "attributes": {}}},
        [ev(m, "binary_sensor.m", s) for m, s in [(10, "on"), (20, "off"), (30, "on"), (40, "off")]],
    )
    motion = timeline["binary_sensor.m"]
    changes = list(motion.transitions(T0 + 1200, T0 + 2400))
    assert [(c.at, c.old_state, c.state) for c in changes] == [
        (T0 + 1200, "on", "off"),
        (T0 + 1800, "off", "on"),
    ]  # half-open: [start, end)
    assert len(list(motion.transitions())) == 4
    assert changes[0].old_attributes == {} and changes[0].attributes == {}


def test_timeline_window_and_entities():
    timeline = build({"a.a": {"state": "1", "attributes": {}}}, [ev(1, "b.b", "x")])
    assert timeline.start == T0 and timeline.end == T0 + 86_400
    assert sorted(timeline.entity_ids) == ["a.a", "b.b"]
    with pytest.raises(ValueError):
        Timeline.from_scenario({}, [], END, START)


def test_the_suggest_core_imports_only_the_standard_library():
    """The miner core stays free of Home Assistant (and any third-party package)."""
    import ast
    from pathlib import Path
    import sys

    package = Path(__file__).parent.parent / "custom_components" / "event_simulator" / "suggest"
    modules = sorted(package.glob("*.py"))
    assert len(modules) >= 5
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                assert top in sys.stdlib_module_names or top == "__future__", f"{path.name} imports {name}"
