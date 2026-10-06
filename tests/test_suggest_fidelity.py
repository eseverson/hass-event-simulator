"""The approximate evaluator agrees with the real sandbox on a mixed timeline.

Each candidate is rendered, replayed through the sandbox child as its own automation, and the runs it starts (and which of them pass the conditions) are compared with the evaluator's answer for the same timeline. Covers `for:` cancellation, numeric arming, the fall-back DST day, a time window across midnight and sun offsets.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, date, datetime
from typing import Any

from astral import LocationInfo
from astral.location import Location
import pytest

from custom_components.event_simulator.suggest.evaluate import Evaluator
from custom_components.event_simulator.suggest.model import (
    CalendarTrigger,
    Candidate,
    DerivativeTrigger,
    EntityTimeTrigger,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
    TrendTrigger,
    helper_spec,
    render_automation,
)
from custom_components.event_simulator.suggest.timeline import Timeline, to_ts

from .test_engine import run_child

CORE = {"time_zone": "Europe/Berlin", "latitude": 52.52, "longitude": 13.405, "elevation": 34, "unit_system": "metric"}
# Sunday 2026-10-25 in Berlin: clocks go back at 03:00 CEST.
START = "2026-10-24T22:00:00+00:00"
END = "2026-10-25T23:00:00+00:00"
DOOR, HUM, PERSON = "binary_sensor.door", "sensor.humidity", "person.me"
START_AT, NOON, ALARM, NEXT = "input_datetime.start", "input_datetime.noon", "input_datetime.alarm", "sensor.next_alarm"


def helper(value: str, **extra: Any) -> dict[str, Any]:
    """An input_datetime's attributes for its state ("YYYY-MM-DD HH:MM:SS", or "HH:MM:SS" for a time-only one)."""
    attributes: dict[str, Any] = {"has_date": " " in value, "has_time": True, **extra}
    if " " in value:
        day, value = value.split(" ")
        attributes |= dict(zip(("year", "month", "day"), map(int, day.split("-")), strict=True))
    attributes |= dict(zip(("hour", "minute", "second"), map(int, value.split(":")), strict=True))
    return attributes


TIMESTAMP = {"device_class": "timestamp"}
BINS, WORK = "calendar.bins", "calendar.work"


def bins(day: str, summary: str) -> dict[str, Any]:
    """The bins calendar's attributes naming an all-day pickup (as a calendar entity writes them)."""
    after = f"{day[:8]}{int(day[8:]) + 1:02d}"
    return {"message": summary, "all_day": True, "start_time": f"{day} 00:00:00", "end_time": f"{after} 00:00:00", "location": ""}


# The work calendar's events are given (`calendars`); the bins calendar's come from its recorded states (#37).
CALENDARS = {
    WORK: [
        {"start": "2026-10-25", "end": "2026-10-26", "summary": "Sunday"},
        {"start": "2026-10-25T09:00:00+01:00", "end": "2026-10-25T10:00:00+01:00", "summary": "Standup"},
        {"start": "2026-10-25T14:00:00+00:00", "end": "2026-10-25T14:30:00+00:00", "summary": "Standup"},
        {"start": "2026-10-25T20:00:00", "end": "2026-10-25T21:00:00", "summary": "Late"},
    ]
}

CHANGES = [
    ("05:00", DOOR, "on", {}), ("05:10", DOOR, "off", {}),
    ("06:00", DOOR, "on", {}), ("06:03", DOOR, "off", {}),
    ("07:00", DOOR, "on", {}), ("07:02", DOOR, "on", {"battery": 4}), ("07:20", DOOR, "off", {}),
    ("08:00", DOOR, "unavailable", {}), ("08:01", DOOR, "on", {}),
    ("09:00", HUM, "75", {}), ("09:02", HUM, "80", {}), ("09:10", HUM, "60", {}),
    ("10:00", HUM, "75", {}), ("10:02", HUM, "65", {}),
    ("11:00", HUM, "75", {}), ("11:01", HUM, "garbage", {}), ("11:02", HUM, "75", {}),
    ("12:00", HUM, "unavailable", {}), ("12:30", HUM, "72", {}), ("13:00", HUM, "50", {}), ("16:00", HUM, "71", {}),
    ("15:00", PERSON, "not_home", {}), ("19:00", PERSON, "home", {}),
    # Times held by entities (#38): replaced before they are due, re-armed by an attribute-only write, cleared.
    ("04:00", START_AT, "2026-10-25 09:00:00", helper("2026-10-25 09:00:00")),
    ("08:30", START_AT, "2026-10-25 10:00:00", helper("2026-10-25 10:00:00")),
    ("09:30", START_AT, "2026-10-25 10:00:00", helper("2026-10-25 10:00:00", friendly_name="Start")),
    ("14:00", START_AT, "unknown", {"has_date": True, "has_time": True}),
    ("12:00", ALARM, "07:15:00", helper("07:15:00")),
    ("10:00", NEXT, "2026-10-25T10:00:00+00:00", TIMESTAMP),
    ("11:00", NEXT, "2026-10-25T13:00:00+00:00", TIMESTAMP),
    ("12:55", NEXT, "unavailable", TIMESTAMP),
    # The bins calendar names its next pickup; from 12:00 a later one (glass), so the 26th's paper pickup is known only from the states before.
    ("06:00", BINS, "off", bins("2026-10-26", "Paper") | {"friendly_name": "Bins"}),
    ("12:00", BINS, "off", bins("2026-10-27", "Glass")),
]
TEMP = "sensor.temp"
CELSIUS = {"unit_of_measurement": "°C", "state_class": "measurement"}


def temp_changes() -> list[tuple[str, str, str, dict[str, Any]]]:
    """A temperature for the rate helpers (#42): a slow rise, a fast one, unavailable and garbage, a fast fall, an attribute-only write, a gap longer than any window, a jittery stretch."""
    found = []

    def at(minute: int, value: Any, attributes: dict[str, Any] = CELSIUS) -> None:
        found.append((f"{minute // 60:02d}:{minute % 60:02d}", TEMP, str(value), attributes))

    for n in range(13):
        at(60 + 5 * n, round(20 + 0.1 * n, 2))
    for n in range(11):
        at(180 + 2 * n, round(21.2 + 0.8 * n, 2))
    at(240, "unavailable")
    at(250, 22)
    at(260, "garbage")
    at(265, 21)
    for n in range(11):
        at(300 + 3 * n, round(21 - 0.7 * n, 2))
    at(360, 14, {**CELSIUS, "battery": 80})
    at(480, 25)
    at(481, 25.5)
    for n in range(31):
        at(540 + n, 25 + (0.6 if n % 3 == 0 else -0.2 * (n % 2)) + 0.05 * n)
    return found


EVENTS = [
    {"at": f"2026-10-25T{hhmm}:00+00:00", "entity_id": entity_id, "state": state, "attributes": attributes}
    for hhmm, entity_id, state, attributes in CHANGES + temp_changes()
]
INITIAL = {
    DOOR: {"state": "off", "attributes": {}, "last_changed": "2026-10-24T12:00:00+00:00", "last_updated": "2026-10-24T12:00:00+00:00"},
    HUM: {"state": "60", "attributes": {"unit_of_measurement": "%"}},
    PERSON: {"state": "home", "attributes": {}},
    START_AT: {"state": "2026-10-25 01:30:00", "attributes": helper("2026-10-25 01:30:00")},
    NOON: {"state": "2026-10-24 12:00:00", "attributes": helper("2026-10-24 12:00:00")},
    ALARM: {"state": "06:30:00", "attributes": helper("06:30:00")},
    NEXT: {"state": "2026-10-25T03:00:00+00:00", "attributes": TIMESTAMP},
    BINS: {"state": "off", "attributes": bins("2026-10-26", "Paper")},
    # Seeded twice, at last_changed and at last_updated (an attribute-only update): two samples before the window.
    TEMP: {"state": "20", "attributes": CELSIUS, "last_changed": "2026-10-24T21:00:00+00:00", "last_updated": "2026-10-24T21:55:00+00:00"},
}

H = 3600
CANDIDATES = {
    "state": Candidate(
        (
            StateTrigger(DOOR, to=("on",), id="start"),
            StateTrigger(DOOR, to=("off",), for_=300, id="end"),
            StateTrigger(DOOR, from_=("on",), for_=120),
            StateTrigger(DOOR, attribute="battery"),
        )
    ),
    # Every write (the 07:02 attribute-only one too) or every state change only, as an any-change trigger fires (#34).
    "any_change": Candidate((StateTrigger(HUM), StateTrigger(DOOR)), (NumericStateCondition(HUM, above=70),)),
    "state_change": Candidate((StateTrigger(DOOR, changes_only=True),)),
    "numeric": Candidate(
        (NumericStateTrigger(HUM, above=70, for_=180), NumericStateTrigger(HUM, below=70)),
        (StateCondition(PERSON, ("home",)),),
    ),
    "time": Candidate(
        (TimeTrigger(2 * H + 30 * 60), TimeTrigger(23 * H + 30 * 60), TimeTrigger(12 * H)),
        (TimeCondition(after=22 * H, before=3 * H), NumericStateCondition(HUM, below=65)),
    ),
    # 01:30 + 1 h is 02:30 on the fall-back night (the first, summer-time one); 12:00 on Saturday + 24 h is 25 real hours later; 07:15 - 8 h wraps to 23:15.
    "entity_time": Candidate(
        (
            EntityTimeTrigger(START_AT),
            EntityTimeTrigger(START_AT, offset=H),
            EntityTimeTrigger(NOON, offset=24 * H),
            EntityTimeTrigger(ALARM, offset=-1800),
            EntityTimeTrigger(ALARM, offset=-8 * H),
            EntityTimeTrigger(NEXT),
            EntityTimeTrigger(NEXT, offset=-600),
        )
    ),
    # The all-day event starts at the window start (local midnight); 09:00+01:00 is after the clocks went back; 20:00 is local wall time; the paper pickup starts after the window end, 7 h earlier is inside it.
    "calendar": Candidate(
        (
            CalendarTrigger(WORK, "start"),
            CalendarTrigger(WORK, "end", offset=1800),
            CalendarTrigger(WORK, "start", offset=-H),
            CalendarTrigger(BINS, "start", offset=-7 * H),
            CalendarTrigger(BINS, "end", offset=-48 * H),
        ),
        (StateCondition(BINS, ("Paper",), attribute="message"),),
    ),
    # Trend and Derivative helpers set up in the sandbox (#42, #43): the evaluator computes their states the way they do.
    "trend": Candidate(
        (
            TrendTrigger(TEMP, window=600, delta=1.0, max_samples=10),
            TrendTrigger(TEMP, window=900, delta=-1.5, max_samples=20),
            TrendTrigger(TEMP, window=300, delta=0.5, max_samples=3, min_samples=3),
            TrendTrigger(TEMP, window=7200, delta=0.4, max_samples=200),
        )
    ),
    "derivative": Candidate(
        (
            DerivativeTrigger(TEMP, 600, "min", above=0.2),
            DerivativeTrigger(TEMP, 0, "h", below=-10),
            DerivativeTrigger(TEMP, 1800, "min", below=-0.1, round_digits=1),
            DerivativeTrigger(TEMP, 300, "min", above=0.3),
        )
    ),
    "sun": Candidate(
        (SunTrigger("sunrise", offset=60), SunTrigger("sunset", offset=-1800), SunTrigger("sunset", offset=1800)),
        (SunCondition(after="sunrise", after_offset=30, before="sunset", before_offset=-60), StateCondition(PERSON, ("home",))),
    ),
}


def sun_events(day: date) -> dict[str, datetime | None]:
    """What `homeassistant.helpers.sun.get_astral_event_date` returns for the scenario's location."""
    location = Location(LocationInfo("", "", CORE["time_zone"], CORE["latitude"], CORE["longitude"]))
    return {
        event: getattr(location, event)(day, local=False, observer_elevation=CORE["elevation"])
        for event in ("sunrise", "sunset")
    }


def helper_ids(candidate: Candidate) -> dict[tuple[Any, ...], str]:
    """One helper entity per distinct helper the candidate's triggers read."""
    keys = list(dict.fromkeys(trigger.helper_key for trigger in candidate.helper_triggers))
    return {key: f"{'binary_sensor' if key[0] == 'trend' else 'sensor'}.rate_{n}" for n, key in enumerate(keys)}


def scenario(name: str, candidate: Candidate) -> dict[str, Any]:
    ids = helper_ids(candidate)
    automation = render_automation(candidate, alias=name, helper_ids=ids)
    automation["id"] = name
    helpers = {trigger.helper_key: helper_spec(trigger, ids[trigger.helper_key]) for trigger in candidate.helper_triggers}
    return {
        "helpers": list(helpers.values()),
        "version": 1,
        "core": CORE,
        "start": START,
        "end": END,
        "automation": automation,
        "initial_states": INITIAL,
        "events": EVENTS,
        "calendars": CALENDARS,
        "services": {"logbook": ["log"]},
        "seed": 0,
    }


@pytest.fixture(scope="module")
def sandbox_runs() -> dict[str, list[dict[str, Any]]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = dict(zip(CANDIDATES, pool.map(lambda item: run_child(scenario(*item)), CANDIDATES.items()), strict=True))
    runs = {}
    for name, (code, result, stderr) in outcomes.items():
        assert code == 0 and result is not None, f"{name}: exit {code}\n{stderr[-4000:]}"
        assert result["error"] is None, result["error"]
        runs[name] = result["runs"]
    return runs


@pytest.mark.parametrize("name", list(CANDIDATES))
def test_evaluator_matches_the_sandbox(name, sandbox_runs):
    timeline = Timeline.from_scenario(INITIAL, EVENTS, START, END, calendars=CALENDARS, time_zone=CORE["time_zone"])
    evaluator = Evaluator(timeline, time_zone=CORE["time_zone"], sun_events=sun_events)
    candidate = CANDIDATES[name]

    def rounded(at: float) -> int:
        return round(at)  # the sandbox's time-change triggers run up to 0.5 s late

    runs = sandbox_runs[name]
    sandbox_started = sorted((round(to_ts(run["started"])), int(run["trigger"]["idx"])) for run in runs)
    sandbox_fired = sorted((round(to_ts(run["started"])), int(run["trigger"]["idx"])) for run in runs if run["outcome"] == "fired")
    triggered = sorted((rounded(t), n) for n, trigger in enumerate(candidate.triggers) for t in evaluator.trigger_fires(trigger))
    fired = [(rounded(fire.at), fire.trigger) for fire in evaluator.fires(candidate)]

    assert sandbox_started, "the scenario should start runs"
    assert {run["outcome"] for run in runs} <= {"fired", "condition_failed"}
    assert triggered == sandbox_started, [datetime.fromtimestamp(t, UTC).isoformat() for t, _ in sandbox_started]
    assert fired == sandbox_fired
    assert 0 < len(fired) < len(triggered) or not candidate.conditions
    # Every helper trigger fires somewhere, so each one is compared.
    assert all(evaluator.trigger_fires(trigger) for trigger in candidate.helper_triggers)


def test_entity_times_fire_where_home_assistant_arms_them():
    """The `entity_time` scenario covers what it says: the sandbox agrees with these through `test_evaluator_matches_the_sandbox`."""
    evaluator = Evaluator(Timeline.from_scenario(INITIAL, EVENTS, START, END), time_zone=CORE["time_zone"])
    utc = [
        [datetime.fromtimestamp(t, UTC).strftime("%d %H:%M") for t in evaluator.trigger_fires(trigger)]
        for trigger in CANDIDATES["entity_time"].triggers
    ]
    assert utc == [
        ["24 23:30", "25 08:00", "25 09:00"],
        ["25 00:30", "25 10:00"],  # 09:00 + 1 h is replaced at 08:30
        ["25 11:00"],
        ["25 05:00"],
        ["25 22:15"],
        ["25 03:00"],
        ["25 02:50", "25 12:50"],
    ]


def test_calendar_triggers_fire_at_each_event_plus_the_offset():
    """The `calendar` scenario covers what it says: the sandbox agrees with these through `test_evaluator_matches_the_sandbox`."""
    timeline = Timeline.from_scenario(INITIAL, EVENTS, START, END, calendars=CALENDARS, time_zone=CORE["time_zone"])
    evaluator = Evaluator(timeline, time_zone=CORE["time_zone"])
    utc = [
        [datetime.fromtimestamp(t, UTC).strftime("%d %H:%M") for t in evaluator.trigger_fires(trigger)]
        for trigger in CANDIDATES["calendar"].triggers
    ]
    assert utc == [
        ["24 22:00", "25 08:00", "25 14:00", "25 19:00"],
        ["25 09:30", "25 15:00", "25 20:30"],  # the all-day event ends at the window end
        ["25 07:00", "25 13:00", "25 18:00"],
        ["25 16:00"],  # the paper pickup on the 26th, named only before 12:00
        ["24 22:00"],  # it ends on the 27th at 00:00 CET: 48 h of wall time earlier is 00:00 CEST on the 25th, 49 real hours
    ]
