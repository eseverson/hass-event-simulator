"""#37: calendar triggers replay against a stub calendar serving the scenario's `calendars` (or the events the calendar's recorded states name).

Same harness as test_engine.py: every scenario runs through a real child process.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

import pytest

from .test_engine import run_child, ts, utc

TZ = "America/Los_Angeles"
START = "2026-09-06T00:00:00+00:00"
END = "2026-09-20T01:00:00+00:00"
TRASH = "calendar.trash"


def scenario(automation: dict[str, Any], **extra: Any) -> dict[str, Any]:
    base = {
        "version": 1,
        "core": {"time_zone": TZ, "latitude": 47.6, "longitude": -122.3, "elevation": 0, "unit_system": "metric"},
        "start": START,
        "end": END,
        "automation": {"id": "calendar", "alias": "Calendar", **automation},
        "initial_states": {},
        "events": [],
    }
    base.update(extra)
    return base


def all_day(day: str, summary: str) -> dict[str, Any]:
    nxt = f"{day[:8]}{int(day[8:]) + 1:02d}"
    return {"start": day, "end": nxt, "summary": summary}


# The user's own trash reminder (#37): 7 h before each pickup (an all-day event), with the event in the action.
REMINDER = {
    "triggers": [{"trigger": "calendar", "entity_id": TRASH, "event": "start", "offset": "-7:0:0"}],
    "actions": [{"action": "notify.notify", "data": {"title": "Trash", "message": "{{ trigger.calendar_event.summary }}"}}],
}
PICKUPS = [
    all_day("2026-09-10", "Garbage and food & yard"),
    all_day("2026-09-17", "Garbage, recycling, and food & yard"),
    # Starts after the window end, but 7 h before it is inside the window.
    all_day("2026-09-20", "Garbage"),
    # Before the window: never fires.
    all_day("2026-09-03", "Garbage"),
]


def calendar_state(at: str, state: str, event: dict[str, Any]) -> dict[str, Any]:
    """A recorded state of calendar.trash naming `event` (all day), as the calendar entity writes it."""
    return {
        "at": at,
        "entity_id": TRASH,
        "state": state,
        "attributes": {
            "message": event["summary"],
            "all_day": True,
            "start_time": f"{event['start']} 00:00:00",
            "end_time": f"{event['end']} 00:00:00",
            "location": "",
            "friendly_name": "Trash",
        },
    }


CASES: dict[str, dict[str, Any]] = {
    "reminder": scenario(REMINDER, calendars={TRASH: PICKUPS}),
    # Timed events, a trigger at each end plus 30 min, and a condition on the event the trigger carries.
    "timed_end": scenario(
        {
            "triggers": [{"trigger": "calendar", "entity_id": "calendar.work", "event": "end", "offset": "0:30:0"}],
            "conditions": [{"condition": "template", "value_template": "{{ 'standup' in trigger.calendar_event.summary | lower }}"}],
            "actions": [{"action": "logbook.log", "data": {"name": "x", "message": "{{ trigger.calendar_event.location }}"}}],
        },
        calendars={
            "calendar.work": [
                {"start": "2026-09-08T09:00:00-07:00", "end": "2026-09-08T09:15:00-07:00", "summary": "Standup", "location": "Room 1"},
                # Local wall time when the offset is left out.
                {"start": "2026-09-09T09:00:00", "end": "2026-09-09T09:15:00", "summary": "Retro"},
                {"start": "2026-09-10T16:00:00+00:00", "end": "2026-09-10T16:15:00+00:00", "summary": "standup"},
            ]
        },
    ),
    # No `calendars`: the events come from the calendar's recorded states, which are still played as recorded.
    "from_states": scenario(
        REMINDER,
        initial_states={TRASH: calendar_state(START, "off", PICKUPS[0]) | {"state": "off"}},
        events=[
            calendar_state("2026-09-10T07:00:00+00:00", "on", PICKUPS[0]),
            calendar_state("2026-09-11T07:00:00+00:00", "off", PICKUPS[1]),
            calendar_state("2026-09-17T07:00:00+00:00", "on", PICKUPS[1]),
            calendar_state("2026-09-18T07:00:00+00:00", "off", PICKUPS[2]),
        ],
    ),
    "no_events": scenario({**REMINDER, "triggers": [{"trigger": "calendar", "entity_id": "calendar.none", "event": "start"}]}),
    "not_an_object": scenario(REMINDER, calendars=[1]),
    "ends_first": scenario(REMINDER, calendars={TRASH: [{"start": "2026-09-10T10:00:00+00:00", "end": "2026-09-10T09:00:00+00:00", "summary": "x"}]}),
    "mixed": scenario(REMINDER, calendars={TRASH: [{"start": "2026-09-10", "end": "2026-09-10T09:00:00+00:00", "summary": "x"}]}),
    "not_a_calendar": scenario(REMINDER, calendars={"sensor.trash": []}),
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


def test_a_calendar_trigger_with_a_negative_offset_fires_ahead_of_each_event(results):
    result = results["reminder"]
    assert result["error"] is None, result["error"]
    # 17:00 the evening before each all-day pickup (PDT), including the one starting after the window end.
    assert runs(result) == [
        (utc("2026-09-09T17:00:00-07:00"), "fired"),
        (utc("2026-09-16T17:00:00-07:00"), "fired"),
        (utc("2026-09-19T17:00:00-07:00"), "fired"),
    ]
    assert [call["data"]["message"] for call in result["service_calls"]] == [event["summary"] for event in PICKUPS[:3]]
    assert result["runs"][0]["trigger"]["platform"] == "calendar"
    assert not [w for w in result["warnings"] if w["code"].startswith("calendar")]


def test_a_calendar_trigger_at_event_ends_reads_the_event(results):
    result = results["timed_end"]
    assert result["error"] is None, result["error"]
    assert runs(result) == [
        (utc("2026-09-08T09:45:00-07:00"), "fired"),
        (utc("2026-09-09T09:45:00-07:00"), "condition_failed"),
        (utc("2026-09-10T16:45:00+00:00"), "fired"),
    ]
    assert [call["data"]["message"] for call in result["service_calls"]] == ["Room 1", ""]


def test_without_calendars_the_recorded_states_name_the_events(results):
    result = results["from_states"]
    assert result["error"] is None, result["error"]
    assert runs(result) == [
        (utc("2026-09-09T17:00:00-07:00"), "fired"),
        (utc("2026-09-16T17:00:00-07:00"), "fired"),
        (utc("2026-09-19T17:00:00-07:00"), "fired"),
    ]
    assert [w["code"] for w in result["warnings"]] == ["calendar_from_states"]
    assert TRASH in result["warnings"][0]["message"]


def test_a_calendar_without_events_attaches_and_says_so(results):
    result = results["no_events"]
    assert result["error"] is None, result["error"]
    assert result["runs"] == []
    assert [w["code"] for w in result["warnings"]] == ["calendar_no_events"]
    assert "calendar.none" in result["warnings"][0]["message"]


@pytest.mark.parametrize("name", ["not_an_object", "ends_first", "mixed", "not_a_calendar"])
def test_malformed_calendars_are_refused(results, name):
    assert results[name]["error"]["code"] == "invalid_scenario", results[name]["error"]
    assert "calendars" in results[name]["error"]["message"]
