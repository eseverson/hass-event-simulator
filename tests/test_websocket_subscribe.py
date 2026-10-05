"""event_simulator/simulate/subscribe: the cancellable replay the panel's auto-run uses.

The panel replays on every edit and abandons the replay in flight when a newer one starts, so the command is subscription-style: unsubscribing, a newer replay on the same connection, or the connection closing each kill the sandbox child, and a burst of edits can never hold the concurrency cap.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import textwrap
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.event_simulator import runner
from custom_components.event_simulator.const import DOMAIN

from .test_websocket import AUTOMATION, SIMULATE

SUBSCRIBE = "event_simulator/simulate/subscribe"


@pytest.fixture
async def setup(hass: HomeAssistant):
    assert await async_setup_component(hass, "automation", {"automation": [AUTOMATION]})
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def fake_child(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


def sleeper(pid_dir: Path) -> list[str]:
    """A child that records its pid in `pid_dir` and then hangs until killed."""
    return fake_child(
        f"""
        import os, sys, time
        sys.stdin.read()
        with open(os.path.join({str(pid_dir)!r}, str(os.getpid())), "w"):
            pass
        time.sleep(60)
        """
    )


ECHO_RUNS = fake_child(
    """
    import json, sys
    json.load(sys.stdin)
    json.dump({"version": 1, "error": None, "runs": [], "service_calls": [], "warnings": []}, sys.stdout)
    """
)


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def wait_for_pids(pid_dir: Path, count: int) -> list[int]:
    for _ in range(400):
        pids = [int(name) for name in os.listdir(pid_dir)]
        if len(pids) >= count:
            return pids
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {count} children, saw {os.listdir(pid_dir)}")


async def wait_gone(pid: int) -> bool:
    for _ in range(200):
        if gone(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def subscribe(client, scenario=SIMULATE) -> int:
    await client.send_json_auto_id({"type": SUBSCRIBE, "scenario": scenario})
    ack = await client.receive_json()
    assert ack["type"] == "result" and ack["success"], ack
    return ack["id"]


async def test_sends_the_result_as_an_event(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=ECHO_RUNS):
        msg_id = await subscribe(client)
        event = await client.receive_json()

    assert event["id"] == msg_id
    assert event["type"] == "event"
    assert event["event"]["result"]["runs"] == []


async def test_the_client_can_unsubscribe_after_the_outcome(
    hass: HomeAssistant, setup, hass_ws_client
):
    """The subscription lives until the client ends it, so its usual unsubscribe succeeds."""
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=ECHO_RUNS):
        msg_id = await subscribe(client)
        await client.receive_json()

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
    msg = await client.receive_json()
    assert msg["success"], msg


async def test_a_finished_replay_is_not_reported_superseded(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=ECHO_RUNS):
        first = await subscribe(client)
        assert (await client.receive_json())["id"] == first
        second = await subscribe(client)
        event = await client.receive_json()

    assert event["id"] == second
    assert "result" in event["event"]


async def test_a_failure_arrives_as_an_error_event(hass: HomeAssistant, setup, hass_ws_client):
    client = await hass_ws_client(hass)
    crash = fake_child("import sys; sys.stderr.write('Traceback: boom'); sys.exit(1)")

    with patch.object(runner, "child_command", return_value=crash):
        msg_id = await subscribe(client)
        event = await client.receive_json()

    assert event["id"] == msg_id
    assert event["event"]["error"]["code"] == "sandbox_failed"
    assert event["event"]["error"]["stderr_tail"] == "Traceback: boom"


async def test_a_refused_scenario_arrives_as_an_error_event(
    hass: HomeAssistant, setup, hass_ws_client
):
    client = await hass_ws_client(hass)

    msg_id = await subscribe(client, {**SIMULATE, "automation": "triggers: [unclosed"})
    event = await client.receive_json()

    assert event["id"] == msg_id
    assert event["event"]["error"]["code"] == "invalid_automation"


async def test_unsubscribing_kills_the_child(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path: Path
):
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        msg_id = await subscribe(client)
        [pid] = await wait_for_pids(tmp_path, 1)
        assert not gone(pid)

        await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": msg_id})
        msg = await client.receive_json()
        assert msg["success"], msg

        assert await wait_gone(pid)


async def test_a_newer_replay_on_the_same_connection_supersedes_the_old_one(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path: Path
):
    """Latest wins even if the panel never got to unsubscribe the old one."""
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        first = await subscribe(client)
        [pid] = await wait_for_pids(tmp_path, 1)
        await subscribe(client)

        superseded = await client.receive_json()
        assert superseded["id"] == first
        assert superseded["event"]["error"]["code"] == "superseded"
        assert await wait_gone(pid)


async def test_closing_the_connection_kills_the_child(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path: Path
):
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        await subscribe(client)
        [pid] = await wait_for_pids(tmp_path, 1)

        await client.close()

        assert await wait_gone(pid)


async def test_rapid_edits_cannot_exhaust_the_concurrency_cap(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path: Path
):
    """A burst of replays, each replacing the last, leaves room for the final one to finish.

    Each hanging child is let start (so it holds a slot) before the next replay replaces it; without the supersede the first two would hold both slots for a minute.
    """
    client = await hass_ws_client(hass)
    messages: list[dict] = []

    async def send(scenario=SIMULATE) -> int:
        await client.send_json_auto_id({"type": SUBSCRIBE, "scenario": scenario})
        while True:
            msg = await asyncio.wait_for(client.receive_json(), 30)
            if msg["type"] == "result":
                assert msg["success"], msg
                return msg["id"]
            messages.append(msg)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        for count in range(1, 4):
            await send()
            await wait_for_pids(tmp_path, count)
    with patch.object(runner, "child_command", return_value=ECHO_RUNS):
        last = await send()
        while not any(msg["id"] == last for msg in messages):
            messages.append(await asyncio.wait_for(client.receive_json(), 30))

    *superseded, final = messages
    assert [msg["event"]["error"]["code"] for msg in superseded] == ["superseded"] * 3
    assert final["id"] == last
    assert final["event"]["result"]["runs"] == []
    for pid in (int(name) for name in os.listdir(tmp_path)):
        assert await wait_gone(pid)


async def test_non_admin_is_refused(
    hass: HomeAssistant, setup, hass_ws_client, hass_read_only_access_token
):
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json_auto_id({"type": SUBSCRIBE, "scenario": SIMULATE})
    msg = await client.receive_json()

    assert not msg["success"]
    assert msg["error"]["code"] == "unauthorized"


async def test_a_one_shot_simulate_is_killed_when_the_connection_closes(
    hass: HomeAssistant, setup, hass_ws_client, tmp_path: Path
):
    """The request/response command used to outlive its client until the 5 minute timeout."""
    client = await hass_ws_client(hass)

    with patch.object(runner, "child_command", return_value=sleeper(tmp_path)):
        await client.send_json_auto_id({"type": "event_simulator/simulate", "scenario": SIMULATE})
        [pid] = await wait_for_pids(tmp_path, 1)

        await client.close()

        assert await wait_gone(pid)
