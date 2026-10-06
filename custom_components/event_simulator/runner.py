"""Live side of the process boundary: run one scenario in a sandbox child and return its result.

The child (`sandbox/`, started with `sandbox.child_command()`) reads the scenario JSON on stdin and writes one result JSON on stdout; see the design spec. This module owns everything that can go wrong around that: a child that hangs (wall-clock timeout, then killed), crashes (nonzero exit, surfaced with the tail of its stderr), prints something that is not a result, or floods its output. A cancelled caller (the websocket client went away, Home Assistant is stopping) kills the child too, so no replay outlives the request that started it.

The child gets a minimal environment and a scratch working directory, so nothing the live process holds in its environment (supervisor tokens, credentials) reaches it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
import os
import shutil
import signal
import sys
import tempfile
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_bytes
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .history import SimulatorError
from .sandbox import child_command

# Wall-clock budget for one replay. A day of busy history replays in seconds; this only catches a child that is stuck.
DEFAULT_TIMEOUT = 300.0
# Share of `timeout` the child gets as its own `time_limit_seconds` (unless the scenario sets one): it stops the replay there and answers with the runs so far, leaving the rest for it to boot, stop and write before it would be killed.
TIME_LIMIT_SHARE = 0.8
# Replays running at once; more wait their turn. Each child is a whole Home Assistant boot.
MAX_CONCURRENT = 2
# Caps on what crosses the pipes.
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
# How much of the child's stderr is kept while it runs, and how much of that an error carries.
STDERR_KEEP_BYTES = 256 * 1024
STDERR_TAIL_CHARS = 4000
# The only environment variables the child inherits: enough to find Python and its packages, and to keep the locale and time zone.
ENV_PASSTHROUGH = ("PATH", "PYTHONPATH", "HOME", "TMPDIR", "TZ", "LANG", "LC_ALL")

_READ_CHUNK = 64 * 1024
_DATA_SEMAPHORE: HassKey[asyncio.Semaphore] = HassKey(f"{DOMAIN}_runner_semaphore")


class SandboxError(SimulatorError):
    """The child failed to produce a result; `stderr_tail` is the end of what it logged."""

    def __init__(self, code: str, message: str, stderr_tail: str = "") -> None:
        super().__init__(code, message)
        self.stderr_tail = stderr_tail


async def async_run_scenario(
    hass: HomeAssistant,
    scenario: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    command: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Replay `scenario` in a fresh sandbox child and return the result dict it wrote.

    Raises `SandboxError` when the child times out, crashes, floods its output or writes no result, and `SimulatorError("too_large")` for a scenario over the input cap. A result carrying a structured `error` (invalid scenario or automation) is returned as is; it is an answer, not a failure. The child is told to stop by itself at `TIME_LIMIT_SHARE` of `timeout` (`time_limit_seconds`, unless the scenario sets it), so a replay that runs long still ends with the runs so far. `command` replaces `child_command()`, for tests.
    """
    if "time_limit_seconds" not in scenario:
        scenario = {**scenario, "time_limit_seconds": round(timeout * TIME_LIMIT_SHARE, 3)}
    payload = await hass.async_add_executor_job(json_bytes, scenario)
    if len(payload) > MAX_INPUT_BYTES:
        raise SimulatorError(
            "too_large",
            f"The scenario is {len(payload) // 1024} KiB; the limit is {MAX_INPUT_BYTES // 1024} KiB",
        )
    if (semaphore := hass.data.get(_DATA_SEMAPHORE)) is None:
        semaphore = hass.data[_DATA_SEMAPHORE] = asyncio.Semaphore(MAX_CONCURRENT)
    argv = list(command) if command is not None else child_command()
    async with semaphore:
        cwd = await hass.async_add_executor_job(tempfile.mkdtemp, None, f"{DOMAIN}-")
        try:
            return await _async_run(hass, argv, payload, timeout, cwd)
        finally:
            await hass.async_add_executor_job(shutil.rmtree, cwd, True)


def child_env(hass: HomeAssistant) -> dict[str, str]:
    """The child's environment: the passthrough variables, plus Home Assistant's `deps` dir when packages live there."""
    env = {key: os.environ[key] for key in ENV_PASSTHROUGH if key in os.environ}
    deps = hass.config.path("deps")
    if deps in sys.path:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (deps, env.get("PYTHONPATH"))))
    return env


async def _async_run(
    hass: HomeAssistant, argv: list[str], payload: bytes, timeout: float, cwd: str
) -> dict[str, Any]:
    stderr = bytearray()
    too_large = False

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=child_env(hass),
        # Its own process group, so a kill reaches anything it started and a Ctrl-C meant for Home Assistant does not reach it.
        start_new_session=True,
    )

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except ConnectionError:
            pass  # the child exited without reading it all; its exit code tells the story
        finally:
            proc.stdin.close()

    async def read_stdout() -> bytes:
        nonlocal too_large
        assert proc.stdout is not None
        chunks: list[bytes] = []
        size = 0
        while chunk := await proc.stdout.read(_READ_CHUNK):
            size += len(chunk)
            if size > MAX_OUTPUT_BYTES:
                too_large = True
                _kill(proc)
                break
            chunks.append(chunk)
        return b"".join(chunks)

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(_READ_CHUNK):
            stderr.extend(chunk)
            if len(stderr) > STDERR_KEEP_BYTES:
                del stderr[:-STDERR_KEEP_BYTES]

    def tail() -> str:
        text = stderr.decode(errors="replace").rstrip()
        return text[-STDERR_TAIL_CHARS:]

    try:
        async with asyncio.timeout(timeout):
            stdout, _, _, returncode = await asyncio.gather(
                read_stdout(), read_stderr(), feed(), proc.wait()
            )
    except TimeoutError:
        raise SandboxError(
            "timeout", f"The replay did not finish within {timeout:g} s and was stopped", tail()
        ) from None
    finally:
        if proc.returncode is None:
            _kill(proc)
            await proc.wait()

    if too_large:
        raise SandboxError(
            "output_too_large",
            f"The replay produced more than {MAX_OUTPUT_BYTES // (1024 * 1024)} MiB of results and was stopped",
            tail(),
        )
    try:
        result = await hass.async_add_executor_job(json.loads, stdout) if stdout.strip() else None
    except ValueError:
        result = None
    if returncode != 0:
        if returncode < 0:
            reason = f"was killed by signal {-returncode}"
        else:
            reason = f"exited with exit code {returncode}"
        message = f"The sandbox {reason}"
        if isinstance(result, dict) and isinstance(error := result.get("error"), dict):
            message += f": {error.get('message')}"
        raise SandboxError("sandbox_failed", message, tail())
    if not isinstance(result, dict):
        raise SandboxError("sandbox_failed", "The sandbox exited without writing a result", tail())
    return result


def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
