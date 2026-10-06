"""Suggest miner: calendar triggers with an offset (#37), from the model to the search.

The trash reminder: 7 h before each pickup (an all-day event) starts. The calendar entity's recorded states name each pickup days ahead, so its events are known before the reminder fires; a clock time ("17:00 on Wed") stands in only until a pickup moves.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant

from custom_components.event_simulator.sandbox.calendars import events_from_states, merge_events, parse_event
from custom_components.event_simulator.suggest.evaluate import Evaluator
from custom_components.event_simulator.suggest.labels import Label, Labels
from custom_components.event_simulator.suggest.model import CalendarTrigger, Candidate, StateCondition, TimeTrigger, render_trigger
from custom_components.event_simulator.suggest.search import SuggestOptions, describe, suggest
from custom_components.event_simulator.suggest.timeline import Timeline, to_ts

TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)
START = "2026-09-06T07:00:00+00:00"  # Sunday 00:00 PDT
T0 = to_ts(START)
M, H, D = 60, 3600, 86_400
TRASH = "calendar.trash"


def local(day: date, hour: float = 0) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=LA).timestamp() + hour * H


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def attributes(event: tuple[date, str]) -> dict:
    day, summary = event
    return {
        "message": summary,
        "all_day": True,
        "start_time": f"{day} 00:00:00",
        "end_time": f"{day + timedelta(days=1)} 00:00:00",
        "location": "",
        "friendly_name": "Trash",
    }


def calendar_timeline(pickups: list[tuple[date, str]], days: int, rng: random.Random, *, track_states: bool = True, given: bool = False) -> Timeline:
    """The calendar's recorded states as the calendar entity writes them (off naming the next pickup, on through it, off naming the one after), with attribute rewrites at random, plus noise sensors; or, `given`, the events as `calendars` with no calendar states at all."""
    end = T0 + days * D
    initial: dict = {}
    events: list[dict] = []
    for n in range(3):
        eid = f"binary_sensor.noise_{n}"
        initial[eid] = {"state": "off", "attributes": {}}
        state = "off"
        for at in sorted(rng.uniform(T0, end) for _ in range(10 * days)):
            state = "on" if state == "off" else "off"
            events.append({"at": iso(at), "entity_id": eid, "state": state, "attributes": {}})
    if track_states:
        upcoming = [p for p in pickups if local(p[0]) + D > T0]
        initial[TRASH] = {"state": "off", "attributes": attributes(upcoming[0])}
        for n, pickup in enumerate(upcoming):
            begin = local(pickup[0])
            if begin >= end:
                break
            events.append({"at": iso(begin), "entity_id": TRASH, "state": "on", "attributes": attributes(pickup)})
            nxt = upcoming[n + 1] if n + 1 < len(upcoming) else pickup
            events.append({"at": iso(begin + D), "entity_id": TRASH, "state": "off", "attributes": attributes(nxt)})
            # The integration rewrites its attributes now and then (a friendly name, a refresh): no new event.
            for at in sorted(rng.uniform(begin + D, begin + 4 * D) for _ in range(3)):
                events.append({"at": iso(at), "entity_id": TRASH, "state": "off", "attributes": {**attributes(nxt), "friendly_name": rng.choice(("Trash", "Waste"))}})
    calendars = {TRASH: [{"start": str(day), "end": str(day + timedelta(days=1)), "summary": summary} for day, summary in pickups]} if given else None
    return Timeline.from_scenario(initial, events, START, end, calendars=calendars, time_zone=TZ)


def thursdays(weeks: int, *, holiday: int | None = None, summaries: tuple[str, ...] = ("Garbage",)) -> list[tuple[date, str]]:
    """A pickup every Thursday from 2026-09-03 (the one in week `holiday` a day late)."""
    first = date(2026, 9, 3)
    return [(first + timedelta(days=7 * w + (w == holiday)), summaries[w % len(summaries)]) for w in range(weeks)]


def reminders(pickups: list[tuple[date, str]], tl: Timeline, rng: random.Random, wanted=None) -> list[float]:
    """When the reminder fires: 17:00 the day before each pickup (7 h before it starts), a moment late as Home Assistant runs it."""
    times = [local(day, -7) + rng.uniform(0, 0.5) for day, summary in pickups if wanted is None or summary in wanted]
    return [t for t in times if tl.start <= t < tl.end]


def fire(at: float) -> Label:
    return Label(at, at + 10 * M, "fire", "start")


def run(tl: Timeline, labels: Labels, **options):
    return suggest(tl, labels, time_zone=TZ, sun_events=None, options=SuggestOptions(**options))


# Model


async def test_a_calendar_trigger_renders_as_home_assistant_reads_it(hass: HomeAssistant) -> None:
    trigger = CalendarTrigger(TRASH, "start", offset=-7 * H)
    assert render_trigger(trigger) == {"trigger": "calendar", "entity_id": TRASH, "event": "start", "offset": "-07:00:00"}
    assert render_trigger(CalendarTrigger(TRASH, "end", id="end")) == {"trigger": "calendar", "id": "end", "entity_id": TRASH, "event": "end"}
    config = {"id": "x", "triggers": [render_trigger(trigger)], "actions": []}
    validated = await async_validate_config_item(hass, "x", config)
    assert validated is not None
    assert describe(Candidate((trigger,))) == f"Fires 7 h before an event of {TRASH} starts"
    assert describe(Candidate((CalendarTrigger(TRASH, "end"),))) == f"Fires when an event of {TRASH} ends"
    assert Candidate((trigger,)).calendars == {TRASH}


# Events from the scenario and from recorded states


def test_events_from_states_are_the_distinct_events_they_name():
    states = [
        attributes((date(2026, 9, 10), "Garbage")),
        attributes((date(2026, 9, 10), "Garbage")) | {"friendly_name": "Waste"},
        attributes((date(2026, 9, 17), "Recycling")),
        {"friendly_name": "Trash"},  # unavailable: no event
        {"message": "Standup", "all_day": False, "start_time": "2026-09-08 09:00:00", "end_time": "2026-09-08 09:15:00", "location": "Room 1"},
    ]
    events = events_from_states(states, LA)
    assert [(e.start, e.end, e.summary, e.location) for e in events] == [
        (datetime(2026, 9, 8, 9, tzinfo=LA), datetime(2026, 9, 8, 9, 15, tzinfo=LA), "Standup", "Room 1"),
        (date(2026, 9, 10), date(2026, 9, 11), "Garbage", None),
        (date(2026, 9, 17), date(2026, 9, 18), "Recycling", None),
    ]
    live = [parse_event({"start": "2026-09-10", "end": "2026-09-11", "summary": "Garbage", "description": "bins out"}, LA)]
    merged = merge_events(live, events, LA)
    assert [e.summary for e in merged] == ["Standup", "Garbage", "Recycling"]
    assert merged[1].description == "bins out"  # the live one wins


def test_the_evaluator_fires_at_each_event_plus_the_offset():
    rng = random.Random(1)
    pickups = thursdays(4)
    tl = calendar_timeline(pickups, 21, rng)
    ev = Evaluator(tl, time_zone=TZ)
    # Sep 10, 17 and 24 start inside the window; Sep 24 at 17:00 the day before is still inside it, Sep 3's is before the window.
    assert ev.trigger_fires(CalendarTrigger(TRASH, "start", -7 * H)) == [local(date(2026, 9, d), -7) for d in (10, 17, 24)]
    assert ev.trigger_fires(CalendarTrigger(TRASH, "end", 30 * M)) == [local(date(2026, 9, d), 0.5) for d in (4, 11, 18, 25) if local(date(2026, 9, d)) >= T0]
    # Given events win over the recorded states; an entity with neither has none.
    given = calendar_timeline([(date(2026, 9, 9), "Moved")], 21, rng, given=True)
    assert Evaluator(given, time_zone=TZ).trigger_fires(CalendarTrigger(TRASH, "start")) == [local(date(2026, 9, 9))]
    assert ev.trigger_fires(CalendarTrigger("calendar.none", "start")) == []


# Search


def test_recovers_seven_hours_before_a_pickup_from_one_label():
    """One label: a daily 17:00 hits it too, but fires every evening; the calendar trigger fires only before a pickup."""
    rng = random.Random(37)
    pickups = thursdays(6, holiday=3)
    tl = calendar_timeline(pickups, 35, rng)
    anchors = reminders(pickups, tl, rng)
    best = run(tl, Labels([fire(anchors[0])])).suggestions[0]
    assert best.candidate == Candidate((CalendarTrigger(TRASH, "start", -7 * H),)), best.explanation
    assert best.config["triggers"] == [{"trigger": "calendar", "entity_id": TRASH, "event": "start", "offset": "-07:00:00"}]
    assert all(abs(f - a) < 1 for f, a in zip(best.stats.fire_times, anchors, strict=True))
    assert best.explanation.startswith(f"Fires 7 h before an event of {TRASH} starts;"), best.explanation


def test_recovers_seven_hours_before_a_pickup_from_three_labels_across_a_holiday():
    """Three labels, a pickup a day late in the fourth week: "17:00 on Wed" misses it, the calendar does not."""
    rng = random.Random(38)
    pickups = thursdays(6, holiday=3)
    tl = calendar_timeline(pickups, 35, rng)
    anchors = reminders(pickups, tl, rng)
    result = run(tl, Labels([fire(at) for at in anchors[:3]]))
    best = result.suggestions[0]
    assert best.candidate == Candidate((CalendarTrigger(TRASH, "start", -7 * H),)), best.explanation
    assert len(best.stats.fire_times) == len(anchors)
    assert all(abs(f - a) < 1 for f, a in zip(best.stats.fire_times, anchors, strict=True))
    # A clock time is no longer the best answer anywhere it ranks.
    clocks = [s for s in result.suggestions if any(isinstance(t, TimeTrigger) for t in s.candidate.triggers)]
    assert all(s.rank > best.rank for s in clocks)


def test_a_clock_time_firing_alike_gives_way_to_the_calendar():
    """No holiday: "17:00 on Wed" and the calendar fire at the same instants; the calendar names why."""
    rng = random.Random(39)
    pickups = thursdays(6)
    tl = calendar_timeline(pickups, 35, rng)
    anchors = reminders(pickups, tl, rng)
    best = run(tl, Labels([fire(at) for at in anchors])).suggestions[0]
    assert best.candidate == Candidate((CalendarTrigger(TRASH, "start", -7 * H),)), best.explanation


def test_only_some_events_keep_a_condition_on_the_calendar_message():
    """Garbage and recycling alternate; the reminder is for garbage only. At 17:00 the day before, the calendar names the coming pickup."""
    rng = random.Random(40)
    pickups = thursdays(6, summaries=("Garbage", "Recycling"))
    tl = calendar_timeline(pickups, 35, rng)
    anchors = reminders(pickups, tl, rng, wanted={"Garbage"})
    best = run(tl, Labels([fire(at) for at in anchors])).suggestions[0]
    assert best.candidate == Candidate(
        (CalendarTrigger(TRASH, "start", -7 * H),), (StateCondition(TRASH, ("Garbage",), attribute="message"),)
    ), best.explanation
    assert best.stats.anchors_hit == len(anchors) and not best.stats.outside_fires
    assert best.config["conditions"] == [{"condition": "state", "entity_id": TRASH, "attribute": "message", "state": "Garbage"}]


def test_without_the_calendar_state_the_explanation_names_the_template_condition():
    """The events come from the calendar itself (no recorded states to condition on), each weekday with both kinds, so no weekday stands in."""
    rng = random.Random(41)
    monday = date(2026, 9, 7)
    garbage = (0, 2, 4, 1, 3)  # weekday of each week's garbage pickup
    sweeping = (2, 0, 1, 4, 0)
    pickups = sorted(
        [(monday + timedelta(days=7 * week + day), "Garbage") for week, day in enumerate(garbage)]
        + [(monday + timedelta(days=7 * week + day), "Street sweeping") for week, day in enumerate(sweeping)]
    )
    tl = calendar_timeline(pickups, 35, rng, track_states=False, given=True)
    anchors = reminders(pickups, tl, rng, wanted={"Garbage"})
    best = run(tl, Labels([fire(at) for at in anchors])).suggestions[0]
    assert best.candidate == Candidate((CalendarTrigger(TRASH, "start", -7 * H),)), best.explanation
    assert best.stats.outside_nearby
    assert f'Only the events of {TRASH} named "Garbage" line up with the labels; a template condition on trigger.calendar_event.summary would keep just those.' in best.explanation


def test_no_calendar_no_calendar_trigger():
    rng = random.Random(42)
    pickups = thursdays(6)
    with_calendar = calendar_timeline(pickups, 35, rng)
    tl = Timeline(with_calendar.start, with_calendar.end, {e: with_calendar[e] for e in with_calendar.entity_ids if e != TRASH}, time_zone=TZ)
    anchors = reminders(pickups, tl, rng)
    result = run(tl, Labels([fire(at) for at in anchors[:3]]))
    assert result.suggestions
    assert not any(isinstance(t, CalendarTrigger) for s in result.suggestions for t in s.candidate.triggers)
