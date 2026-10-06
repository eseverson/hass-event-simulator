"""event_simulator/suggest/subscribe: mine suggestions from recorded history, validate them exactly in one sandbox child.

End to end with the recorder: a door that opens at the same two times every day for nine days (longer than a replay window), three evening openings labeled. The command streams progress, then answers with suggestions whose stats come from the sandbox.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.event_simulator import runner, websocket
from custom_components.event_simulator.suggest import exact
from custom_components.event_simulator.const import DOMAIN

from .test_runner import held_child, wait_started
from .test_websocket import AUTOMATION, SIMULATE
from .test_websocket_subscribe import fake_child, gone, sleeper, wait_for_pids, wait_gone

SUGGEST = "event_simulator/suggest/subscribe"
DOOR = "binary_sensor.door"
NOISE = "sensor.unrelated"
DAYS = 9


@pytest.fixture
async def setup(hass: HomeAssistant):
    assert await async_setup_component(hass, "automation", {"automation": [AUTOMATION]})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def door_history(hass: HomeAssistant):
    """Nine days, ending ten days ago, of a door opening at 08:00 and 19:00 (window-relative) for two minutes, and an unrelated sensor counting up every six hours (its first write repeats the start state, so it records no change). Returns (start, evening openings).

    Written with past timestamps rather than a frozen clock: the cancellation tests wait on real time.
    """
    start = dt_util.utcnow().replace(second=0, microsecond=0) - timedelta(days=DAYS + 1)
    begin = (start - timedelta(seconds=30)).timestamp()
    hass.states.async_set(DOOR, "off", timestamp=begin)
    hass.states.async_set(NOISE, "0", timestamp=begin)
    evenings = []
    changes = []
    for day in range(DAYS):
        for hour in (8, 19):
            at = start + timedelta(days=day, hours=hour)
            changes += [(at, DOOR, "on"), (at + timedelta(minutes=2), DOOR, "off")]
            if hour == 19:
                evenings.append(at)
        for quarter in range(4):
            changes.append((start + timedelta(days=day, hours=6 * quarter + 3), NOISE, str(day * 4 + quarter)))
    for at, entity_id, state in sorted(changes):
        hass.states.async_set(entity_id, state, timestamp=at.timestamp())
    await async_wait_recording_done(hass)
    return start, evenings


def request(start, evenings, **extra) -> dict[str, Any]:
    return {
        "type": SUGGEST,
        "entity_ids": [DOOR, NOISE],
        "start": start.isoformat(),
        "end": (start + timedelta(days=DAYS)).isoformat(),
        "labels": [
            {"start": at.isoformat(), "end": (at + timedelta(minutes=30)).isoformat(), "kind": "fire", "anchor": "start"}
            for at in evenings[:6:2]
        ],
        "options": {"tolerance_before": 120, "tolerance_after": 600},
        **extra,
    }


async def subscribe(client, message) -> int:
    await client.send_json_auto_id(message)
    ack = await client.receive_json()
    assert ack["type"] == "result" and ack["success"], ack
    return ack["id"]


async def until_final(client, msg_id) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Progress events, then the final one (the first without a `stage`)."""
    progress = []
    while True:
        event = await client.receive_json()
        assert event["id"] == msg_id and event["type"] == "event", event
        if "stage" not in event["event"]:
            return progress, event["event"]
        progress.append(event["event"])


async def test_a_request_without_fields_is_refused_as_malformed(hass: HomeAssistant, setup, hass_ws_client):
    """A request without its fields fails validation (invalid_format), not as an unknown command."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": SUGGEST})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"


async def test_suggests_from_recorded_history_and_validates_exactly(
    hass: HomeAssistant, setup, door_history, hass_ws_client
):
    start, evenings = door_history
    client = await hass_ws_client(hass)
    sent: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def spy(hass, scenario, **kwargs):
        sent.append(scenario)
        return await run_scenario(hass, scenario, **kwargs)

    with patch.object(runner, "async_run_scenario", spy):
        msg_id = await subscribe(client, request(start, evenings))
        progress, final = await until_final(client, msg_id)

    assert [p["stage"] for p in progress] == ["loading", "mining", "validating"]
    assert progress[1]["events"] == DAYS * 8 - 1 and progress[2]["candidates"] == len(final["suggestions"])
    assert "warning" not in final, final.get("warning")
    best = final["suggestions"][0]
    assert best["rank"] == 0 and best["exact"] is True
    stats = best["stats"]
    assert (stats["anchors_hit"], stats["anchors_total"]) == (3, 3)
    assert not stats["missed"]
    # The sandbox's own fire times: one at each labeled opening (a time trigger runs a fraction of a second late in HA).
    fired = [dt_util.parse_datetime(t) for t in stats["fire_times"]]
    for at in evenings[:6:2]:
        assert any(-120 <= (t - at).total_seconds() <= 600 for t in fired), at
    assert best["config"]["description"] == best["explanation"]
    assert final["evaluated"] > 0
    assert all(s["exact"] is True for s in final["suggestions"])
    # Three labels leave the other evenings to the hints ("peek outside"): fires of the top suggestions, or the door opening again.
    assert final["hints"]

    # One child validated them all, with only the entities they read.
    [scenario] = sent
    assert scenario["traces"] == "none"
    assert len(scenario["automations"]) == len(final["suggestions"])
    # Replayed without the placeholder action: whether a run fires is up to its trigger and conditions (#36).
    assert all(automation["actions"] == [] for automation in scenario["automations"])
    assert all(s["config"]["actions"] for s in final["suggestions"])
    assert "automation" not in scenario
    referenced = set()
    for suggestion in final["suggestions"]:
        for part in (*suggestion["config"]["triggers"], *suggestion["config"]["conditions"]):
            if "entity_id" in part:
                referenced.add(part["entity_id"])
    timeline_ids = set(scenario["initial_states"]) | {e["entity_id"] for e in scenario["events"]}
    assert timeline_ids <= referenced
    if NOISE not in referenced:
        assert NOISE not in timeline_ids


async def test_the_suggest_window_is_longer_than_a_replay(
    hass: HomeAssistant, setup, door_history, hass_ws_client
):
    """Nine days loads for history and suggest; a replay of it is still refused, and suggest stops at 30 days."""
    start, evenings = door_history
    client = await hass_ws_client(hass)
    end = start + timedelta(days=DAYS)

    await client.send_json_auto_id(
        {"type": "event_simulator/history", "entity_ids": [DOOR], "start": start.isoformat(), "end": end.isoformat()}
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    assert len(msg["result"]["events"]) == DAYS * 4

    await client.send_json_auto_id(
        {
            "type": "event_simulator/simulate",
            "scenario": {"start": start.isoformat(), "end": end.isoformat(), "automation": {"triggers": [], "actions": []}},
        }
    )
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "window_too_long"

    too_long = request(start, evenings, end=(start + timedelta(days=30, seconds=1)).isoformat())
    msg_id = await subscribe(client, too_long)
    progress, final = await until_final(client, msg_id)
    assert progress == []
    assert final["error"]["code"] == "window_too_long"


async def test_unsubscribing_kills_the_validation_child(
    hass: HomeAssistant, setup, door_history, hass_ws_client, tmp_path: Path
):
    start, evenings = door_history
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        msg_id = await subscribe(client, request(start, evenings))
        [pid] = await wait_for_pids(tmp_path, 1)
        await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
        while (msg := await client.receive_json())["type"] == "event":
            assert "stage" in msg["event"]  # progress already on its way; never a result
        assert msg["success"]
        assert await wait_gone(pid)


async def test_a_newer_request_supersedes_the_one_in_flight(
    hass: HomeAssistant, setup, door_history, hass_ws_client, tmp_path: Path
):
    start, evenings = door_history
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        first = await subscribe(client, request(start, evenings))
        [pid] = await wait_for_pids(tmp_path, 1)
        await client.send_json_auto_id(request(start, evenings))
        superseded = None
        while superseded is None:
            msg = await client.receive_json()
            if msg["id"] == first and "error" in msg.get("event", {}):
                superseded = msg["event"]["error"]
        assert superseded["code"] == "superseded"
        assert await wait_gone(pid)
        await wait_for_pids(tmp_path, 2)  # the newer one got as far as its own child
    # Leaving the block closes the connection, which kills the newer child.


async def test_a_simulate_subscription_is_not_superseded_by_a_suggestion(
    hass: HomeAssistant, setup, door_history, hass_ws_client, tmp_path: Path
):
    """Newest wins per kind: a suggestion does not cancel the panel's replay, nor the other way round."""
    start, evenings = door_history
    client = await hass_ws_client(hass)
    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        await client.send_json_auto_id({"type": "event_simulator/simulate/subscribe", "scenario": SIMULATE})
        replay = (await client.receive_json())["id"]
        await wait_for_pids(tmp_path, 1)
        await subscribe(client, request(start, evenings))
        pids = await wait_for_pids(tmp_path, 2)
        await asyncio.sleep(0.3)
        assert not any(gone(pid) for pid in pids)
        # Nothing was superseded: only the suggestion's progress has arrived.
        while (msg := await asyncio.wait_for(client.receive_json(), 1))["id"] != replay:
            assert "stage" in msg["event"]
            if msg["event"]["stage"] == "validating":
                break
        else:
            raise AssertionError(f"the replay got {msg}")


async def test_a_failed_validation_falls_back_to_the_miner(
    hass: HomeAssistant, setup, door_history, hass_ws_client
):
    start, evenings = door_history
    client = await hass_ws_client(hass)
    crash = fake_child(
        """
        import sys
        sys.stdin.read()
        print("boom", file=sys.stderr)
        sys.exit(3)
        """
    )

    with patch.object(runner, "child_command", return_value=crash):
        msg_id = await subscribe(client, request(start, evenings))
        progress, final = await until_final(client, msg_id)

    assert [p["stage"] for p in progress] == ["loading", "mining", "validating"]
    assert final["warning"]["code"] == "validation_failed"
    assert "exit code 3" in final["warning"]["message"]
    assert final["suggestions"]
    assert all(s["exact"] is False for s in final["suggestions"])
    assert final["suggestions"][0]["stats"]["anchors_hit"] == 3


async def test_sun_events_come_from_the_instance_location(
    hass: HomeAssistant, setup, caplog: pytest.LogCaptureFixture
):
    sun_events = websocket.async_sun_events(hass)
    day = dt_util.now().date()
    events = sun_events(day)
    assert "deprecated" not in caplog.text  # get_astral_location is, from 2026.9 (removed in 2027.7)
    assert events["sunrise"] == get_astral_event_date(hass, "sunrise", day)
    assert events["sunset"] == get_astral_event_date(hass, "sunset", day)
    # A polar day (no sunset) is None, as Home Assistant's helper says.
    hass.config.latitude = 78.2
    polar = websocket.async_sun_events(hass)(day.replace(month=6, day=21))
    assert polar["sunset"] is None
    assert polar["sunset"] == get_astral_event_date(hass, "sunset", day.replace(month=6, day=21))


def same_rule(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["triggers"] == b["triggers"] and a.get("conditions") == b.get("conditions")


async def validate_with(hass, hass_ws_client, door_history, alter) -> tuple[dict[str, Any], dict[str, Any]]:
    """Suggest with the real validation child, its result changed by `alter(scenario, replay)` before it is scored. Returns (scenario sent, final event)."""
    start, evenings = door_history
    client = await hass_ws_client(hass)
    sent: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def spy(hass, scenario, **kwargs):
        sent.append(scenario)
        replay = await run_scenario(hass, scenario, **kwargs)
        alter(scenario, replay)
        return replay

    with patch.object(runner, "async_run_scenario", spy):
        msg_id = await subscribe(client, request(start, evenings))
        _, final = await until_final(client, msg_id)
    [scenario] = sent
    return scenario, final


@pytest.mark.parametrize("code", ["runaway_loop", "time_limit"])
async def test_a_suggestion_stopped_mid_replay_is_not_exact(
    hass: HomeAssistant, setup, door_history, hass_ws_client, code
):
    """#31: the sandbox stopped one automation partway (it kept re-triggering itself, or was still running at the time limit after the window): its fires are only those up to there, so it keeps the miner's estimate."""

    def stop_first(scenario, replay):
        entry = next(item for item in replay["automations"] if item["index"] == 0)
        assert entry["loaded"] is True
        entry["error"] = {"code": code, "message": f"automations[0] was stopped ({code})"}
        replay["runs"] = [run for run in replay["runs"] if run["automation"] != 0 or run["started"] < scenario["start"]]
        if code == "time_limit":
            # Reached while runs in flight after the window end were waited for: the window itself was replayed in full.
            replay["warnings"].append({"code": code, "message": "automations[0]: stopped", "at": scenario["end"], "automation": 0})

    scenario, final = await validate_with(hass, hass_ws_client, door_history, stop_first)
    stopped = [s for s in final["suggestions"] if same_rule(s["config"], scenario["automations"][0])]
    assert len(stopped) == 1 and stopped[0]["exact"] is False
    # Kept with the miner's estimate rather than scored on no fires at all.
    assert stopped[0]["stats"]["fire_times"]
    assert stopped[0] is final["suggestions"][-1]
    others = [s for s in final["suggestions"] if s is not stopped[0]]
    assert others and all(s["exact"] is True for s in others)
    assert final["warning"]["code"] == "partly_validated"
    assert f"({code})" in final["warning"]["message"]


async def test_a_replay_cut_short_by_the_time_limit_validates_nothing(
    hass: HomeAssistant, setup, door_history, hass_ws_client
):
    """#31: the time limit ended the window early: every suggestion's later fires are missing, so none is exact."""

    def cut(scenario, replay):
        at = dt_util.parse_datetime(scenario["start"]) + timedelta(days=2)
        replay["runs"] = [run for run in replay["runs"] if dt_util.parse_datetime(run["started"]) <= at]
        replay["warnings"].append({"code": "time_limit", "message": "The replay was stopped after its time limit", "at": at.isoformat()})

    _, final = await validate_with(hass, hass_ws_client, door_history, cut)
    assert final["suggestions"]
    assert all(s["exact"] is False for s in final["suggestions"])
    assert final["warning"]["code"] == "validation_failed"
    assert "time limit" in final["warning"]["message"]
    assert final["suggestions"][0]["stats"]["anchors_hit"] == 3


async def test_a_validation_waits_its_turn_visibly_and_never_holds_up_a_replay(
    hass: HomeAssistant, setup, door_history, hass_ws_client, tmp_path: Path
):
    """#36: validations share one sandbox slot. One that has to wait says `queued`; a panel replay still starts at once."""
    start, evenings = door_history
    client = await hass_ws_client(hass)
    panel = await hass_ws_client(hass)
    held = held_child(tmp_path)
    blocker = hass.async_create_task(
        runner.async_run_scenario(hass, SIMULATE, timeout=60, command=held, background=True)
    )
    await wait_started(tmp_path, 1)

    msg_id = await subscribe(client, request(start, evenings))
    progress = []
    while not progress or progress[-1].get("stage") != "queued":
        event = await client.receive_json()
        assert event["id"] == msg_id and "stage" in event["event"], event
        progress.append(event["event"])
    assert [p["stage"] for p in progress] == ["loading", "mining", "validating", "queued"]

    await panel.send_json_auto_id({"type": "event_simulator/simulate", "scenario": SIMULATE})
    replay = await panel.receive_json()
    assert replay["success"] and replay["result"]["error"] is None, replay

    (tmp_path / "release").touch()
    await blocker
    _, final = await until_final(client, msg_id)
    assert final["suggestions"] and all(s["exact"] is True for s in final["suggestions"])


async def test_suggestions_too_costly_to_replay_keep_the_miner_estimate(
    hass: HomeAssistant, setup, door_history, hass_ws_client
):
    """#36: only the best suggestions whose runs fit `MAX_VALIDATION_RUNS` are replayed; the rest are not suspect, just unchecked."""
    start, evenings = door_history
    client = await hass_ws_client(hass)
    sent: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def spy(hass, scenario, **kwargs):
        sent.append(scenario)
        return await run_scenario(hass, scenario, **kwargs)

    # Room for the top suggestion's runs (the door opens twice a day) and not much more.
    with patch.object(runner, "async_run_scenario", spy), patch.object(exact, "MAX_VALIDATION_RUNS", DAYS * 2):
        msg_id = await subscribe(client, request(start, evenings))
        progress, final = await until_final(client, msg_id)

    [scenario] = sent
    suggestions = final["suggestions"]
    checked = [s for s in suggestions if s["exact"] is True]
    unchecked = [s for s in suggestions if s["exact"] is False]
    assert checked and unchecked
    assert len(scenario["automations"]) == len(checked) == progress[-1]["candidates"]
    assert final["warning"]["code"] == "validation_capped"
    assert f"{len(unchecked)} of {len(suggestions)} suggestions fire too often" in final["warning"]["message"]
    assert all(s["stats"]["fire_times"] and "approx_stats" not in s for s in unchecked)

    # Nothing fits: no child at all, every suggestion keeps the miner's estimate.
    sent.clear()
    with patch.object(runner, "async_run_scenario", spy), patch.object(exact, "MAX_VALIDATION_RUNS", 0):
        msg_id = await subscribe(client, request(start, evenings))
        progress, final = await until_final(client, msg_id)
    assert sent == []
    assert [p["stage"] for p in progress] == ["loading", "mining"]
    assert final["warning"]["code"] == "validation_capped"
    assert all(s["exact"] is False for s in final["suggestions"])
