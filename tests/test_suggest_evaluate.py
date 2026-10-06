"""Suggest miner: the fast approximate evaluator, against hand-built timelines."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.event_simulator.suggest.evaluate import Evaluator, Fire
from custom_components.event_simulator.suggest.model import (
    Candidate,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
)
from custom_components.event_simulator.suggest.timeline import Timeline, to_ts

UTC_START = "2026-10-04T00:00:00+00:00"
T0 = to_ts(UTC_START)
M = 60
H = 3600
DOOR = "binary_sensor.door"
HUM = "sensor.humidity"


def make(initial, changes, *, start=UTC_START, days=1, tz="UTC", sun_events=None):
    """`changes` is [(seconds after start, entity_id, state, attributes?)]."""
    t0 = to_ts(start)
    events = [
        {
            "at": datetime.fromtimestamp(t0 + c[0], UTC).isoformat(),
            "entity_id": c[1],
            "state": c[2],
            "attributes": c[3] if len(c) > 3 else {},
        }
        for c in changes
    ]
    initial = {eid: {"state": s, "attributes": {}} if isinstance(s, str) else s for eid, s in initial.items()}
    timeline = Timeline.from_scenario(initial, events, start, t0 + days * 86_400)
    return Evaluator(timeline, time_zone=tz, sun_events=sun_events)


def rel(times, t0=T0):
    return [round(t - t0, 3) for t in times]


# State trigger


def test_state_trigger_fires_on_matching_transitions_not_on_the_seeded_state():
    ev = make({DOOR: "on"}, [(10, DOOR, "off"), (20, DOOR, "on"), (30, DOOR, "unavailable"), (40, DOOR, "on")])
    assert rel(ev.trigger_fires(StateTrigger(DOOR, to=("on",)))) == [20, 40]
    assert rel(ev.trigger_fires(StateTrigger(DOOR, to=("on",), from_=("off",)))) == [20]
    assert rel(ev.trigger_fires(StateTrigger(DOOR, not_from=("unavailable",), to=("on",)))) == [20]
    assert rel(ev.trigger_fires(StateTrigger(DOOR, not_to=("unavailable",)))) == [10, 20, 40]
    assert rel(ev.trigger_fires(StateTrigger("binary_sensor.other", to=("on",)))) == []


def test_state_trigger_match_all_fires_on_attribute_writes_and_to_null_does_not():
    ev = make(
        {"light.l": {"state": "on", "attributes": {"brightness": 1}}},
        [(10, "light.l", "on", {"brightness": 50}), (20, "light.l", "off"), (30, "light.l", "off", {"x": 1})],
    )
    assert rel(ev.trigger_fires(StateTrigger("light.l"))) == [10, 20, 30]
    assert rel(ev.trigger_fires(StateTrigger("light.l", changes_only=True))) == [20]
    # An attribute trigger fires on changes of that attribute only (absent counts as None).
    assert rel(ev.trigger_fires(StateTrigger("light.l", attribute="brightness"))) == [10, 20]
    assert rel(ev.trigger_fires(StateTrigger("light.l", attribute="brightness", to=(50,)))) == [10]


def test_state_trigger_for_waits_and_is_cancelled_by_a_change():
    ev = make(
        {DOOR: "off"},
        [
            (100, DOOR, "on"),  # held 5 min -> fires at 400
            (1000, DOOR, "off"),
            (2000, DOOR, "on"),  # broken off after 4 min
            (2240, DOOR, "off"),
            (3000, DOOR, "on", {"battery": 5}),  # attribute write while waiting does not cancel
            (3100, DOOR, "on", {"battery": 4}),
            (4000, DOOR, "off"),
        ],
    )
    trigger = StateTrigger(DOOR, to=("on",), for_=5 * M)
    assert rel(ev.trigger_fires(trigger)) == [400, 3300]


def test_state_trigger_for_restarts_on_a_new_transition_and_fires_on_exact_boundary():
    ev = make({DOOR: "off"}, [(100, DOOR, "on"), (200, DOOR, "off"), (250, DOOR, "on"), (550, DOOR, "off")])
    # The second on holds exactly 300 s: the timer is due at the instant of the next write and wins.
    assert rel(ev.trigger_fires(StateTrigger(DOOR, to=("on",), for_=300))) == [550]


def test_state_trigger_with_only_from_and_for_waits_for_not_returning():
    # HA keeps a from-only `for:` alive while the value stays different from the old one.
    ev = make({DOOR: "on"}, [(100, DOOR, "off"), (200, DOOR, "unavailable"), (1000, DOOR, "on"), (1100, DOOR, "off"), (1200, DOOR, "on")])
    assert rel(ev.trigger_fires(StateTrigger(DOOR, from_=("on",), for_=300))) == [400]


def test_state_trigger_for_due_after_the_window_does_not_fire():
    ev = make({DOOR: "off"}, [(86_400 - 100, DOOR, "on")])
    assert ev.trigger_fires(StateTrigger(DOOR, to=("on",), for_=300)) == []


def test_state_trigger_on_an_entity_created_mid_window():
    ev = make({}, [(50, DOOR, "on"), (60, DOOR, "off")])
    assert rel(ev.trigger_fires(StateTrigger(DOOR, to=("on",)))) == [50]
    assert rel(ev.trigger_fires(StateTrigger(DOOR, from_=("off",)))) == []


# Numeric state trigger


def test_numeric_state_fires_on_crossing_into_range_not_while_staying():
    ev = make({HUM: "60"}, [(10, HUM, "75"), (20, HUM, "80"), (30, HUM, "65"), (40, HUM, "72"), (50, HUM, "70"), (60, HUM, "70.5")])
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70))) == [10, 40, 60]
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, below=70))) == [30]  # 60 at start: already in range; 70 is not below 70
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70, below=78))) == [10, 40, 60]


def test_numeric_state_starting_in_range_waits_for_leaving_first():
    ev = make({HUM: "75"}, [(10, HUM, "76"), (20, HUM, "60"), (30, HUM, "71")])
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70))) == [30]


def test_numeric_state_unavailable_arms_and_non_numeric_neither_arms_nor_disarms():
    ev = make({HUM: "75"}, [(10, HUM, "unavailable"), (20, HUM, "75"), (30, HUM, "garbage"), (40, HUM, "76")])
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70))) == [20]
    ev = make({HUM: "60"}, [(10, HUM, "garbage"), (20, HUM, "75")])
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70))) == [20]


def test_numeric_state_without_a_seeded_state_is_not_armed():
    ev = make({}, [(10, HUM, "75"), (20, HUM, "60"), (30, HUM, "75")])
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70))) == [30]


def test_numeric_state_attribute():
    ev = make({"climate.c": {"state": "heat", "attributes": {"temp": 18}}}, [(10, "climate.c", "heat", {"temp": 22}), (20, "climate.c", "heat", {})])
    trigger = NumericStateTrigger("climate.c", above=20, attribute="temp")
    assert rel(ev.trigger_fires(trigger)) == [10]


def test_numeric_state_for_requires_staying_in_range():
    ev = make(
        {HUM: "60"},
        [
            (100, HUM, "75"), (200, HUM, "80"),  # stays in range: fires at 100 + 300
            (1000, HUM, "60"),
            (2000, HUM, "75"), (2100, HUM, "65"),  # drops out: cancelled
            (3000, HUM, "75"), (3100, HUM, "garbage"), (3150, HUM, "75"),  # an error cancels and does not re-arm
            (4000, HUM, "50"), (5000, HUM, "71"),
        ],
    )
    assert rel(ev.trigger_fires(NumericStateTrigger(HUM, above=70, for_=300))) == [400, 5300]


# Time trigger


def berlin_ts(y, mo, d, h, mi, fold=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("Europe/Berlin"), fold=fold).timestamp()


def test_time_trigger_daily_in_local_time():
    ev = make({}, [], start="2026-10-01T22:00:00+00:00", days=3, tz="Europe/Berlin")
    fires = ev.trigger_fires(TimeTrigger(7 * H))
    assert fires == [berlin_ts(2026, 10, 2, 7, 0), berlin_ts(2026, 10, 3, 7, 0), berlin_ts(2026, 10, 4, 7, 0)]


def test_time_trigger_on_dst_days():
    # Fall back (2026-10-25, Berlin): 02:30 happens twice and Home Assistant fires on both.
    ev = make({}, [], start="2026-10-23T22:00:00+00:00", days=3, tz="Europe/Berlin")
    fires = ev.trigger_fires(TimeTrigger(2 * H + 30 * M))
    assert fires == [
        berlin_ts(2026, 10, 24, 2, 30),
        berlin_ts(2026, 10, 25, 2, 30, fold=0),
        berlin_ts(2026, 10, 25, 2, 30, fold=1),
        berlin_ts(2026, 10, 26, 2, 30),
    ]
    assert fires[2] - fires[1] == H
    # Spring forward (2026-03-29): 02:30 does not exist that day and is skipped.
    ev = make({}, [], start="2026-03-27T23:00:00+00:00", days=3, tz="Europe/Berlin")
    assert ev.trigger_fires(TimeTrigger(2 * H + 30 * M)) == [berlin_ts(2026, 3, 28, 2, 30), berlin_ts(2026, 3, 30, 2, 30)]
    # Other times keep their wall clock across the change.
    assert ev.trigger_fires(TimeTrigger(7 * H))[1] - ev.trigger_fires(TimeTrigger(7 * H))[0] == 23 * H


def test_time_trigger_weekdays():
    # 2026-10-05 is a Monday.
    ev = make({}, [], start="2026-10-03T00:00:00+00:00", days=7)
    fires = ev.trigger_fires(TimeTrigger(7 * H, weekdays=("mon", "fri")))
    assert [datetime.fromtimestamp(t, UTC).date() for t in fires] == [date(2026, 10, 5), date(2026, 10, 9)]


# Sun trigger


def fake_sun(day: date):
    """Sunrise 06:00 UTC, sunset 18:00 UTC, drifting one minute a day."""
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    drift = timedelta(minutes=day.day)
    return {"sunrise": base + timedelta(hours=6) + drift, "sunset": base + timedelta(hours=18) - drift}


def test_sun_trigger_with_offsets():
    ev = make({}, [], days=2, sun_events=fake_sun)  # 2026-10-04 and -05
    assert rel(ev.trigger_fires(SunTrigger("sunset"))) == [18 * H - 4 * M, 86_400 + 18 * H - 5 * M]
    assert rel(ev.trigger_fires(SunTrigger("sunrise", offset=-30 * M))) == [6 * H + 4 * M - 30 * M, 86_400 + 6 * H + 5 * M - 30 * M]
    # An offset can push the previous day's event into the window.
    late = rel(ev.trigger_fires(SunTrigger("sunset", offset=7 * H)))
    assert late == [1 * H - 3 * M, 86_400 + 1 * H - 4 * M]


def test_sun_trigger_without_a_sun_function_is_refused_and_polar_days_are_skipped():
    with pytest.raises(ValueError):
        make({}, []).trigger_fires(SunTrigger("sunset"))
    ev = make({}, [], days=2, sun_events=lambda d: {"sunrise": None, "sunset": None} if d.day == 4 else fake_sun(d))
    assert rel(ev.trigger_fires(SunTrigger("sunset"))) == [86_400 + 18 * H - 5 * M]


# Conditions


def test_state_condition_reads_the_state_after_writes_at_the_same_instant():
    ev = make({DOOR: "off", "person.me": "home"}, [(100, DOOR, "on"), (200, "person.me", "away")])
    cond = StateCondition("person.me", ("home",))
    assert ev.condition_holds(cond, T0 + 199)
    assert not ev.condition_holds(cond, T0 + 200)
    assert ev.condition_holds(StateCondition(DOOR, ("on", "open")), T0 + 100)
    assert not ev.condition_holds(StateCondition("sensor.nope", ("on",)), T0 + 100)  # unknown entity: error, fails


def test_state_condition_for_is_strict_and_uses_last_changed():
    ev = make(
        {DOOR: {"state": "on", "attributes": {"a": 1}, "last_changed": "2026-10-03T23:00:00+00:00"}},
        [(100, DOOR, "on", {"a": 2}), (200, DOOR, "off")],
    )
    cond = StateCondition(DOOR, ("on",), for_=H)
    assert not ev.condition_holds(cond, T0 - 1)
    assert ev.condition_holds(cond, T0 + 1)  # on since 23:00, attribute write does not reset it
    assert not ev.condition_holds(StateCondition(DOOR, ("off",), for_=60), T0 + 260)  # exactly 60 s: not longer
    assert ev.condition_holds(StateCondition(DOOR, ("off",), for_=60), T0 + 260.5)
    # attribute + for: Home Assistant still measures the state's last_changed
    assert ev.condition_holds(StateCondition(DOOR, (2,), attribute="a", for_=H), T0 + 150)


def test_numeric_state_condition():
    ev = make({HUM: "65"}, [(10, HUM, "unavailable"), (20, HUM, "x"), (30, HUM, "80")])
    cond = NumericStateCondition(HUM, above=60, below=70)
    assert ev.condition_holds(cond, T0)
    assert not ev.condition_holds(cond, T0 + 10)
    assert not ev.condition_holds(cond, T0 + 20)
    assert not ev.condition_holds(cond, T0 + 30)
    assert ev.condition_holds(NumericStateCondition(HUM, above=70), T0 + 30)
    assert not ev.condition_holds(NumericStateCondition(HUM, above=80), T0 + 30)  # strict


def test_time_condition_window_crossing_midnight_and_weekday_of_the_current_day():
    ev = make({}, [], tz="Europe/Berlin")
    night = TimeCondition(after=22 * H, before=6 * H)
    assert ev.condition_holds(night, berlin_ts(2026, 10, 4, 23, 0))
    assert ev.condition_holds(night, berlin_ts(2026, 10, 4, 22, 0))
    assert ev.condition_holds(night, berlin_ts(2026, 10, 5, 5, 59))
    assert not ev.condition_holds(night, berlin_ts(2026, 10, 5, 6, 0))
    assert not ev.condition_holds(night, berlin_ts(2026, 10, 4, 21, 59))
    day = TimeCondition(after=8 * H, before=17 * H)
    assert ev.condition_holds(day, berlin_ts(2026, 10, 4, 8, 0))
    assert not ev.condition_holds(day, berlin_ts(2026, 10, 4, 17, 0))
    assert ev.condition_holds(TimeCondition(after=12 * H), berlin_ts(2026, 10, 4, 23, 59))
    assert not ev.condition_holds(TimeCondition(before=12 * H), berlin_ts(2026, 10, 4, 12, 0))
    # 2026-10-04 is a Sunday; after midnight it is Monday, and HA checks the current day.
    sunday_night = TimeCondition(after=22 * H, before=6 * H, weekdays=("sun",))
    assert ev.condition_holds(sunday_night, berlin_ts(2026, 10, 4, 23, 0))
    assert not ev.condition_holds(sunday_night, berlin_ts(2026, 10, 5, 1, 0))
    assert ev.condition_holds(TimeCondition(weekdays=("sun",)), berlin_ts(2026, 10, 4, 0, 30))  # 22:30 UTC Saturday


def test_time_condition_on_a_dst_day_reads_local_wall_time():
    ev = make({}, [], start="2026-10-24T22:00:00+00:00", tz="Europe/Berlin")
    cond = TimeCondition(after=2 * H, before=3 * H)
    assert ev.condition_holds(cond, berlin_ts(2026, 10, 25, 2, 30, fold=0))
    assert ev.condition_holds(cond, berlin_ts(2026, 10, 25, 2, 30, fold=1))
    assert not ev.condition_holds(cond, berlin_ts(2026, 10, 25, 3, 0))


def test_sun_condition():
    ev = make({}, [], days=2, sun_events=fake_sun)  # 2026-10-04: sunrise 06:04, sunset 17:56 (UTC = local here)
    after_sunset = SunCondition(after="sunset")
    assert ev.condition_holds(after_sunset, T0 + 18 * H)
    assert not ev.condition_holds(after_sunset, T0 + 17 * H + 55 * M)
    assert not ev.condition_holds(after_sunset, T0 + 1 * H)  # after midnight: today's sunset is ahead
    dark = SunCondition(after="sunset", before="sunrise")  # the special case: either side of the night
    assert ev.condition_holds(dark, T0 + 1 * H)
    assert ev.condition_holds(dark, T0 + 23 * H)
    assert not ev.condition_holds(dark, T0 + 12 * H)
    daytime = SunCondition(after="sunrise", after_offset=30 * M, before="sunset", before_offset=-H)
    assert not ev.condition_holds(daytime, T0 + 6 * H + 30 * M)
    assert ev.condition_holds(daytime, T0 + 6 * H + 35 * M)
    assert ev.condition_holds(daytime, T0 + 16 * H + 55 * M)
    assert not ev.condition_holds(daytime, T0 + 16 * H + 57 * M)


def test_sun_condition_on_a_day_without_the_event_fails():
    ev = make({}, [], sun_events=lambda d: {"sunrise": None, "sunset": None})
    assert not ev.condition_holds(SunCondition(after="sunrise"), T0 + 12 * H)


# Candidates


def test_candidate_fires_are_trigger_fires_filtered_by_all_conditions():
    ev = make(
        {DOOR: "off", "person.me": "home"},
        [(H, DOOR, "on"), (2 * H, DOOR, "off"), (20 * H, DOOR, "on"), (21 * H, "person.me", "away"), (22 * H, DOOR, "off"), (23 * H, DOOR, "on")],
        sun_events=fake_sun,
    )
    on = StateTrigger(DOOR, to=("on",), id="start")
    off = StateTrigger(DOOR, to=("off",), id="end")
    assert ev.fires(Candidate((on, off))) == [
        Fire(T0 + H, 0), Fire(T0 + 2 * H, 1), Fire(T0 + 20 * H, 0), Fire(T0 + 22 * H, 1), Fire(T0 + 23 * H, 0)
    ]
    candidate = Candidate((on,), (SunCondition(after="sunset"), StateCondition("person.me", ("home",))))
    assert ev.fires(candidate) == [Fire(T0 + 20 * H, 0)]
    assert rel(ev.fire_times(candidate)) == [20 * H]


def test_holds_throughout_samples_every_breakpoint():
    ev = make({DOOR: "on", HUM: "50"}, [(2 * H, DOOR, "off"), (3 * H, DOOR, "on"), (5 * H, HUM, "90")], sun_events=fake_sun)
    door_on = StateCondition(DOOR, ("on",))
    assert ev.holds_throughout((door_on,), T0, T0 + 2 * H)
    assert not ev.holds_throughout((door_on,), T0 + H, T0 + 4 * H)
    assert ev.holds_throughout((door_on,), T0 + 3 * H, T0 + 4 * H)
    assert not ev.holds_throughout((NumericStateCondition(HUM, below=60),), T0 + 4 * H, T0 + 6 * H)
    assert ev.holds_throughout((TimeCondition(after=H, before=5 * H),), T0 + H, T0 + 5 * H)
    assert not ev.holds_throughout((TimeCondition(after=H, before=5 * H),), T0 + H, T0 + 5 * H + 1)
    assert not ev.holds_throughout((SunCondition(after="sunrise"),), T0 + 6 * H, T0 + 7 * H)
    assert ev.holds_throughout((StateCondition(DOOR, ("on",), for_=30 * M),), T0 + 3 * H + 31 * M, T0 + 4 * H)
    assert not ev.holds_throughout((StateCondition(DOOR, ("on",), for_=30 * M),), T0 + 3 * H, T0 + 4 * H)
    assert ev.holds_throughout((), T0, T0 + 1)


def test_trigger_fires_are_cached_and_a_month_of_chatty_sensor_is_fast():
    import time

    changes = [(i * 20, HUM, str(50 + (i % 60))) for i in range(1, 30 * 86_400 // 20)]
    ev = make({HUM: "50"}, changes, days=30)
    began = time.perf_counter()
    fires = ev.trigger_fires(NumericStateTrigger(HUM, above=100, for_=60))
    assert len(fires) > 1000
    assert time.perf_counter() - began < 2
    assert ev.trigger_fires(NumericStateTrigger(HUM, above=100, for_=60)) is fires
