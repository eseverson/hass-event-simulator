"""The live side of the process boundary: spawn, timeout, crash surfacing, cancellation.

Most tests swap the sandbox for a tiny fake child (`python -c ...`) through the runner's `command` argument, so they exercise only the process handling; the real child runs end to end in test_websocket.py's simulate tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import textwrap
import time
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.event_simulator import runner
from custom_components.event_simulator.history import SimulatorError
from custom_components.event_simulator.runner import SandboxError, async_run_scenario

SCENARIO = {"version": 1, "start": "2026-10-04T00:00:00+00:00", "end": "2026-10-04T01:00:00+00:00"}


def fake_child(code: str) -> list[str]:
    """A child that runs `code` instead of the sandbox."""
    return [sys.executable, "-c", textwrap.dedent(code)]


# Echoes the scenario back as the result, so a test can see what reached the child.
ECHO = fake_child(
    """
    import json, sys
    scenario = json.load(sys.stdin)
    json.dump({"version": 1, "error": None, "runs": [], "echo": scenario}, sys.stdout)
    """
)


def gone(pid: int) -> bool:
    """True once `pid` no longer exists (killed and reaped)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def test_returns_the_child_result(hass: HomeAssistant):
    result = await async_run_scenario(hass, SCENARIO, timeout=30, command=ECHO)

    # The child is told to stop itself before the runner would kill it.
    assert result["echo"] == {**SCENARIO, "time_limit_seconds": 24.0}
    assert result["runs"] == []


async def test_a_time_limit_the_scenario_sets_is_kept(hass: HomeAssistant):
    result = await async_run_scenario(hass, {**SCENARIO, "time_limit_seconds": 5}, timeout=30, command=ECHO)

    assert result["echo"]["time_limit_seconds"] == 5


async def test_timeout_kills_the_child(hass: HomeAssistant):
    child = fake_child(
        """
        import os, sys, time
        print("pid", os.getpid(), file=sys.stderr, flush=True)
        time.sleep(60)
        """
    )
    started = time.monotonic()

    with pytest.raises(SandboxError) as caught:
        await async_run_scenario(hass, SCENARIO, timeout=0.5, command=child)

    assert time.monotonic() - started < 10
    assert caught.value.code == "timeout"
    assert "0.5" in str(caught.value)
    pid = int(caught.value.stderr_tail.split()[1])
    assert gone(pid)


async def test_crash_carries_the_stderr_tail(hass: HomeAssistant):
    child = fake_child(
        """
        import sys
        for n in range(500):
            print(f"noise {n}", file=sys.stderr)
        print("Traceback: boom", file=sys.stderr)
        sys.exit(1)
        """
    )

    with pytest.raises(SandboxError) as caught:
        await async_run_scenario(hass, SCENARIO, timeout=30, command=child)

    assert caught.value.code == "sandbox_failed"
    assert "exit code 1" in str(caught.value)
    assert caught.value.stderr_tail.rstrip().endswith("Traceback: boom")
    assert "noise 0\n" not in caught.value.stderr_tail  # a tail, not the whole log
    assert len(caught.value.stderr_tail) <= runner.STDERR_TAIL_CHARS


async def test_crash_names_the_engine_error(hass: HomeAssistant):
    """Exit 1 with an internal_error result: the result's message is the useful part."""
    child = fake_child(
        """
        import json, sys
        print("Traceback ...", file=sys.stderr)
        json.dump({"error": {"code": "internal_error", "message": "KeyError: 'x'"}, "runs": []}, sys.stdout)
        sys.exit(1)
        """
    )

    with pytest.raises(SandboxError) as caught:
        await async_run_scenario(hass, SCENARIO, timeout=30, command=child)

    assert caught.value.code == "sandbox_failed"
    assert "KeyError: 'x'" in str(caught.value)


async def test_killed_by_a_signal(hass: HomeAssistant):
    child = fake_child("import os, signal; os.kill(os.getpid(), signal.SIGKILL)")

    with pytest.raises(SandboxError) as caught:
        await async_run_scenario(hass, SCENARIO, timeout=30, command=child)

    assert caught.value.code == "sandbox_failed"
    assert "signal 9" in str(caught.value)


@pytest.mark.parametrize("stdout", ["not json", "", "[1, 2]"])
async def test_unparseable_stdout(hass: HomeAssistant, stdout: str):
    child = fake_child(f"import sys; sys.stdin.read(); sys.stdout.write({stdout!r})")

    with pytest.raises(SandboxError) as caught:
        await async_run_scenario(hass, SCENARIO, timeout=30, command=child)

    assert caught.value.code == "sandbox_failed"


async def test_cancellation_kills_the_child(hass: HomeAssistant, tmp_path: Path):
    pid_file = tmp_path / "pid"
    child = fake_child(
        f"""
        import os, time
        with open({str(pid_file)!r}, "w") as f:
            f.write(str(os.getpid()))
        time.sleep(60)
        """
    )
    task = hass.async_create_task(async_run_scenario(hass, SCENARIO, timeout=60, command=child))
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    assert not gone(pid)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gone(pid)


async def test_large_output_does_not_deadlock(hass: HomeAssistant):
    """Megabytes on stdin, stdout and stderr at once: every pipe is drained concurrently."""
    child = fake_child(
        """
        import json, sys
        scenario = json.load(sys.stdin)
        sys.stderr.write("e" * 3_000_000)
        json.dump({"runs": [], "size": len(scenario["events"]), "pad": "x" * 3_000_000}, sys.stdout)
        """
    )
    scenario = {**SCENARIO, "events": [{"pad": "y" * 100}] * 30_000}

    result = await async_run_scenario(hass, scenario, timeout=60, command=child)

    assert result["size"] == 30_000
    assert len(result["pad"]) == 3_000_000


async def test_output_cap(hass: HomeAssistant):
    child = fake_child(
        """
        import sys, time
        sys.stdout.write("x" * 100_000)
        sys.stdout.flush()
        time.sleep(60)
        """
    )

    with patch.object(runner, "MAX_OUTPUT_BYTES", 10_000), pytest.raises(SandboxError) as caught:
        await async_run_scenario(hass, SCENARIO, timeout=30, command=child)

    assert caught.value.code == "output_too_large"


async def test_input_cap(hass: HomeAssistant):
    with patch.object(runner, "MAX_INPUT_BYTES", 100), pytest.raises(SimulatorError) as caught:
        await async_run_scenario(hass, {**SCENARIO, "pad": "x" * 200}, timeout=30, command=ECHO)

    assert caught.value.code == "too_large"


async def test_child_gets_a_minimal_environment(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "secret")
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    child = fake_child(
        """
        import json, os, sys
        sys.stdin.read()
        json.dump({"runs": [], "env": dict(os.environ), "cwd": os.getcwd()}, sys.stdout)
        """
    )

    result = await async_run_scenario(hass, SCENARIO, timeout=30, command=child)

    assert "SUPERVISOR_TOKEN" not in result["env"]
    assert result["env"]["TZ"] == "Europe/Amsterdam"
    assert set(result["env"]) <= set(runner.ENV_PASSTHROUGH) | {"LC_CTYPE"}
    assert result["cwd"] != os.getcwd()
    assert not os.path.exists(result["cwd"])  # the scratch directory is removed afterwards


async def test_concurrent_replays_are_capped(hass: HomeAssistant, tmp_path: Path):
    """With room for one replay, a second waits until the first is done."""
    child = fake_child(
        f"""
        import json, os, sys, time
        sys.stdin.read()
        marker = os.path.join({str(tmp_path)!r}, str(os.getpid()))
        open(marker, "w").close()
        time.sleep(0.5)
        active = len(os.listdir({str(tmp_path)!r}))
        os.remove(marker)
        json.dump({{"runs": [], "active": active}}, sys.stdout)
        """
    )

    with patch.object(runner, "MAX_CONCURRENT", 1):
        results = await asyncio.gather(
            *(async_run_scenario(hass, SCENARIO, timeout=30, command=child) for _ in range(2))
        )

    assert [result["active"] for result in results] == [1, 1]


async def test_real_child_round_trip(hass: HomeAssistant):
    """The default command is the real sandbox."""
    scenario = json.loads((Path(__file__).parent / "scenarios" / "state_trigger.json").read_text())

    result = await async_run_scenario(hass, scenario, timeout=120)

    assert result["error"] is None
    assert [run["outcome"] for run in result["runs"]] == ["fired", "fired"]


def held_child(directory: Path) -> list[str]:
    """A child that marks itself started (a file named after its pid in `directory`) and answers once the file `release` appears there."""
    return fake_child(
        f"""
        import json, os, sys, time
        sys.stdin.read()
        open(os.path.join({str(directory)!r}, str(os.getpid())), "w").close()
        while not os.path.exists(os.path.join({str(directory)!r}, "release")):
            time.sleep(0.02)
        json.dump({{"runs": []}}, sys.stdout)
        """
    )


async def wait_started(directory: Path, count: int) -> None:
    for _ in range(400):
        if len([p for p in directory.iterdir() if p.name != "release"]) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{count} children did not start")


async def test_background_replays_leave_a_slot_for_the_others(hass: HomeAssistant, tmp_path: Path):
    """A second background replay (suggest's validation) waits for the first, saying so; a foreground one (the panel's) still starts at once."""
    child = held_child(tmp_path)
    queued: list[str] = []
    first = hass.async_create_task(
        async_run_scenario(hass, SCENARIO, timeout=30, command=child, background=True, on_queued=lambda: queued.append("first"))
    )
    await wait_started(tmp_path, 1)
    second = hass.async_create_task(
        async_run_scenario(hass, SCENARIO, timeout=30, command=child, background=True, on_queued=lambda: queued.append("second"))
    )
    panel = hass.async_create_task(
        async_run_scenario(hass, SCENARIO, timeout=30, command=child, on_queued=lambda: queued.append("panel"))
    )
    await wait_started(tmp_path, 2)
    await asyncio.sleep(0.3)
    assert queued == ["second"]
    assert len(list(tmp_path.iterdir())) == 2  # the second background replay has not started
    (tmp_path / "release").touch()
    assert [result["runs"] for result in await asyncio.gather(first, second, panel)] == [[], [], []]
    assert len(list(tmp_path.iterdir())) == 4


async def test_cancelling_a_queued_replay_gives_up_its_place_at_once(hass: HomeAssistant, tmp_path: Path):
    child = held_child(tmp_path)
    with patch.object(runner, "MAX_CONCURRENT", 1):
        running = hass.async_create_task(async_run_scenario(hass, SCENARIO, timeout=30, command=child))
        await wait_started(tmp_path, 1)
        queued = asyncio.Event()
        waiting = hass.async_create_task(
            async_run_scenario(hass, SCENARIO, timeout=30, command=child, on_queued=queued.set)
        )
        await asyncio.wait_for(queued.wait(), 5)
        began = time.monotonic()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert time.monotonic() - began < 1
        # It never started a child, and the slot goes to the next one when the running replay ends.
        after = hass.async_create_task(async_run_scenario(hass, SCENARIO, timeout=30, command=child))
        (tmp_path / "release").touch()
        assert (await running)["runs"] == [] and (await after)["runs"] == []
    assert len(list(tmp_path.iterdir())) == 3  # two children and the release file
