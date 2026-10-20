"""#37: calendar events on the live side. The `calendar_events` command (the calendar's own events through `calendar.get_events`, plus those its recorded states name), calendar triggers in Suggest mode (mined, then validated in the sandbox with the events), and `calendars` in a replay."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import Context, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util

from custom_components.event_simulator import runner

from .test_websocket_suggest import setup, subscribe, until_final  # noqa: F401 - fixture

TRASH = "calendar.trash"
NOISE = "sensor.unrelated"
DAYS = 21


def attributes(day: date, summary: str) -> dict[str, Any]:
    return {
        "message": summary,
        "all_day": True,
        "start_time": f"{day} 00:00:00",
        "end_time": f"{day + timedelta(days=1)} 00:00:00",
        "location": "",
        "friendly_name": "Trash",
    }


def midnight(day: date) -> datetime:
    return dt_util.start_of_local_day(day)


@pytest.fixture
async def trash_history(hass: HomeAssistant):
    """Three weeks ending a few days ago of a trash calendar (no calendar integration: only its recorded states), a pickup every 7 days at no fixed weekday relative to the window. Returns (start, pickup days)."""
    first = dt_util.now().date() - timedelta(days=DAYS + 4)
    start = midnight(first)
    pickups = [first + timedelta(days=2 + 7 * week) for week in range(4)]
    hass.states.async_set(NOISE, "0", timestamp=(start - timedelta(seconds=30)).timestamp())
    hass.states.async_set(TRASH, "off", attributes(pickups[0], "Garbage"), timestamp=(start - timedelta(seconds=30)).timestamp())
    for n, day in enumerate(pickups):
        hass.states.async_set(TRASH, "on", attributes(day, "Garbage"), timestamp=midnight(day).timestamp())
        after = pickups[n + 1] if n + 1 < len(pickups) else day + timedelta(days=7)
        hass.states.async_set(TRASH, "off", attributes(after, "Garbage"), timestamp=midnight(day + timedelta(days=1)).timestamp())
        hass.states.async_set(NOISE, str(n + 1), timestamp=(midnight(day) + timedelta(hours=5)).timestamp())
    await async_wait_recording_done(hass)
    return start, pickups


async def calendar_events(client, start: datetime, end: datetime) -> dict[str, Any]:
    await client.send_json_auto_id(
        {"type": "event_simulator/calendar_events", "entity_ids": [TRASH, NOISE], "start": start.isoformat(), "end": end.isoformat()}
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["result"]


async def test_without_the_calendar_its_recorded_states_name_the_events(
    hass: HomeAssistant, setup, trash_history, hass_ws_client  # noqa: F811
):
    start, pickups = trash_history
    client = await hass_ws_client(hass)
    result = await calendar_events(client, start, start + timedelta(days=DAYS))
    assert list(result["calendars"]) == [TRASH]  # only calendars
    assert result["calendars"][TRASH] == [
        {"start": str(day), "end": str(day + timedelta(days=1)), "summary": "Garbage"} for day in pickups[:3]
    ]  # the fourth starts two days after the window: further than any offset reaches
    assert result["sources"][TRASH]["calendar"] == 0
    assert "not loaded" in result["sources"][TRASH]["error"]


async def test_the_calendar_answers_and_the_states_fill_in(
    hass: HomeAssistant, setup, trash_history, hass_ws_client, hass_admin_user  # noqa: F811
):
    """The calendar's own events win (with what states do not carry, a description); a past event it no longer returns still comes from the states."""
    start, pickups = trash_history
    asked: list[dict[str, Any]] = []
    contexts: list[Context] = []

    async def get_events(call: ServiceCall) -> dict[str, Any]:
        asked.append(dict(call.data))
        contexts.append(call.context)
        events = [{"start": str(day), "end": str(day + timedelta(days=1)), "summary": "Garbage", "description": "bins out"} for day in pickups[1:]]
        events.append({"start": f"{pickups[1]}T09:00:00", "end": "not a time", "summary": "broken"})
        return {TRASH: {"events": events}}

    hass.services.async_register("calendar", "get_events", get_events, supports_response=SupportsResponse.ONLY)
    client = await hass_ws_client(hass)
    end = start + timedelta(days=DAYS)
    result = await calendar_events(client, start, end)
    events = result["calendars"][TRASH]
    assert [e["start"] for e in events] == [str(day) for day in pickups[:3]]
    assert [e.get("description") for e in events] == [None, "bins out", "bins out"]
    assert result["sources"][TRASH] == {"calendar": 2, "states": 1}
    # Asked around the window, as far as a calendar trigger's offset reaches.
    assert asked[0]["start_date_time"] <= start - timedelta(days=2)
    assert asked[0]["end_date_time"] >= end + timedelta(days=2)
    # Asked as the websocket user.
    assert contexts[0].user_id == hass_admin_user.id


async def test_a_calendar_trigger_is_suggested_and_validated_with_the_events(
    hass: HomeAssistant, setup, trash_history, hass_ws_client  # noqa: F811
):
    start, pickups = trash_history
    end = start + timedelta(days=DAYS)
    reminders = [midnight(day) - timedelta(hours=7) for day in pickups if start <= midnight(day) - timedelta(hours=7) < end]
    client = await hass_ws_client(hass)
    sent: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def spy(hass, scenario, **kwargs):
        sent.append(scenario)
        return await run_scenario(hass, scenario, **kwargs)

    request = {
        "type": "event_simulator/suggest/subscribe",
        "entity_ids": [TRASH, NOISE],
        "start": start.isoformat(),
        "end": end.isoformat(),
        "labels": [{"start": reminders[0].isoformat(), "end": (reminders[0] + timedelta(minutes=10)).isoformat(), "kind": "fire", "anchor": "start"}],
        "options": {"tolerance_before": 120, "tolerance_after": 600},
    }
    with patch.object(runner, "async_run_scenario", spy):
        msg_id = await subscribe(client, request)
        _, final = await until_final(client, msg_id)

    assert "warning" not in final, final.get("warning")
    best = final["suggestions"][0]
    assert best["exact"] is True
    assert best["config"]["triggers"] == [{"trigger": "calendar", "entity_id": TRASH, "event": "start", "offset": "-07:00:00"}]
    assert [dt_util.parse_datetime(t) for t in best["stats"]["fire_times"]] == reminders
    [scenario] = sent
    assert list(scenario["calendars"]) == [TRASH]


async def test_a_replay_with_calendars(hass: HomeAssistant, setup, trash_history, hass_ws_client):  # noqa: F811
    start, pickups = trash_history
    client = await hass_ws_client(hass)
    end = start + timedelta(days=7)
    events = (await calendar_events(client, start, end))["calendars"][TRASH]
    await client.send_json_auto_id(
        {
            "type": "event_simulator/simulate",
            "scenario": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "automation": {
                    "id": "trash",
                    "triggers": [{"trigger": "calendar", "entity_id": TRASH, "event": "start", "offset": "-7:0:0"}],
                    "actions": [{"action": "notify.notify", "data": {"message": "{{ trigger.calendar_event.summary }}"}}],
                },
                "calendars": {TRASH: events},
            },
        }
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    expected = [midnight(day) - timedelta(hours=7) for day in pickups if start <= midnight(day) - timedelta(hours=7) < end]
    assert [dt_util.parse_datetime(run["started"]) for run in msg["result"]["runs"]] == expected
    assert [call["data"]["message"] for call in msg["result"]["service_calls"]] == ["Garbage"] * len(expected)
