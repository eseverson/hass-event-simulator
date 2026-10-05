"""Tests for the virtual-time event loop (sandbox/vclock.py).

These drive the loop directly with run_until_complete; no HA fixture is needed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.event_simulator.sandbox.vclock import (
    VirtualClock,
    VirtualTimeEventLoop,
    patch_homeassistant_clock,
)

START = 1_000.0


@pytest.fixture
def loop():
    loop = VirtualTimeEventLoop(start=START)
    yield loop
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()


def test_time_starts_at_start_and_stands_still(loop):
    assert loop.time() == START
    loop.run_until_complete(asyncio.sleep(0))
    assert loop.time() == START


def test_long_sleep_completes_instantly_in_wall_time(loop):
    wall = time.monotonic()
    loop.run_until_complete(asyncio.sleep(3600))
    assert loop.time() == START + 3600
    assert time.monotonic() - wall < 0.5


def test_timers_fire_in_order_at_exact_virtual_times(loop):
    fired = []

    def record(name):
        fired.append((name, loop.time()))

    loop.call_later(5, record, "c")
    loop.call_later(1, record, "a")
    loop.call_at(START + 3, record, "b")
    loop.call_later(7.25, record, "d")
    loop.run_until_complete(asyncio.sleep(10))

    assert fired == [("a", START + 1), ("b", START + 3), ("c", START + 5), ("d", START + 7.25)]


def test_nested_scheduling_and_call_soon_do_not_advance_time(loop):
    fired = []

    def record(name):
        fired.append((name, loop.time()))

    def outer():
        record("outer")
        loop.call_soon(record, "soon")
        loop.call_later(2, inner)

    def inner():
        record("inner")
        loop.call_later(0, record, "zero-delay")

    loop.call_later(1, outer)
    loop.run_until_complete(asyncio.sleep(10))

    assert fired == [
        ("outer", START + 1),
        ("soon", START + 1),
        ("inner", START + 3),
        ("zero-delay", START + 3),
    ]


def test_tasks_interleave_and_wake_in_virtual_order(loop):
    log = []

    async def spinner(name):
        for i in range(3):
            log.append(f"{name}{i}")
            await asyncio.sleep(0)

    async def sleeper(name, delay):
        await asyncio.sleep(delay)
        log.append((name, loop.time()))

    async def main():
        await asyncio.gather(spinner("a"), spinner("b"), sleeper("slow", 10), sleeper("fast", 5))

    loop.run_until_complete(main())
    assert log[:6] == ["a0", "b0", "a1", "b1", "a2", "b2"]
    assert log[6:] == [("fast", START + 5), ("slow", START + 10)]


def test_asyncio_timeout_expires_at_virtual_deadline(loop):
    async def main():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(60):
                await asyncio.sleep(3600)
        return loop.time()

    assert loop.run_until_complete(main()) == START + 60


def test_executor_job_is_waited_for_without_jumping_time(loop):
    """Outstanding executor work blocks the time jump until it completes."""
    events = []

    async def sleeper():
        await asyncio.sleep(3600)
        events.append(("slept", loop.time()))

    async def worker():
        result = await loop.run_in_executor(None, _slow_job, 0.2)
        events.append((result, loop.time()))

    async def main():
        await asyncio.gather(sleeper(), worker())

    wall = time.monotonic()
    loop.run_until_complete(main())
    assert time.monotonic() - wall >= 0.2
    assert events == [("done", START), ("slept", START + 3600)]


def test_executor_job_calling_back_into_loop(loop):
    """A thread that hands work to the loop via call_soon_threadsafe is serviced."""
    seen = []

    def job():
        done = threading.Event()
        loop.call_soon_threadsafe(lambda: (seen.append(loop.time()), done.set()))
        assert done.wait(5)
        return "ok"

    async def main():
        timer = asyncio.create_task(asyncio.sleep(100))
        result = await loop.run_in_executor(None, job)
        await timer
        return result

    assert loop.run_until_complete(main()) == "ok"
    assert seen == [START]
    assert loop.time() == START + 100


def test_day_of_periodic_timers_runs_fast(loop):
    counts = {1: 0, 60: 0, 3600: 0}

    def periodic(period):
        counts[period] += 1
        loop.call_later(period, periodic, period)

    for period in counts:
        loop.call_later(period, periodic, period)

    wall = time.monotonic()
    loop.run_until_complete(asyncio.sleep(24 * 3600 + 0.5))
    elapsed = time.monotonic() - wall

    assert counts == {1: 86400, 60: 1440, 3600: 24}
    assert elapsed < 1.0, f"24 virtual hours took {elapsed:.3f}s of wall time"


# --- VirtualClock: wall-clock mapping -------------------------------------------------

SCENARIO_START = datetime(2026, 10, 4, 0, 0, tzinfo=UTC)


def test_clock_requires_aware_start():
    with pytest.raises(ValueError):
        VirtualClock(datetime(2026, 10, 4))  # noqa: DTZ001


def test_clock_maps_virtual_elapsed_onto_wall_clock():
    clock = VirtualClock(SCENARIO_START)
    try:
        assert clock.utcnow() == SCENARIO_START
        assert clock.timestamp() == SCENARIO_START.timestamp()
        assert clock.elapsed() == 0

        clock.loop.run_until_complete(asyncio.sleep(90.5))

        assert clock.elapsed() == 90.5
        assert clock.utcnow() == SCENARIO_START + timedelta(seconds=90.5)
        assert clock.utcnow().tzinfo is UTC
        assert clock.timestamp() == SCENARIO_START.timestamp() + 90.5
        berlin = ZoneInfo("Europe/Berlin")
        assert clock.now(berlin) == clock.utcnow()
        assert clock.now(berlin).tzinfo is berlin
    finally:
        clock.loop.close()


def test_clock_schedules_at_wall_datetimes():
    clock = VirtualClock(SCENARIO_START)
    seen = []
    try:
        target = SCENARIO_START + timedelta(hours=7, minutes=12, seconds=3)
        clock.call_at_datetime(target, lambda: seen.append(clock.utcnow()))
        clock.call_at_datetime(
            datetime(2026, 10, 4, 1, 0, tzinfo=ZoneInfo("Europe/Berlin")),
            lambda: seen.append(clock.utcnow()),
        )
        clock.loop.run_until_complete(asyncio.sleep(24 * 3600))
        # 01:00 Berlin (CEST, UTC+2) on Oct 4 is 23:00 UTC on Oct 3: in the past, runs immediately
        assert seen == [SCENARIO_START, target]
        assert clock.loop_time_for(target) == target.timestamp()
    finally:
        clock.loop.close()


# --- Patching Home Assistant's clock --------------------------------------------------


def test_patch_homeassistant_clock_follows_virtual_time():
    import homeassistant.helpers.event as ha_event
    import homeassistant.helpers.script as ha_script
    import homeassistant.util as ha_util
    from homeassistant.util import dt as dt_util

    clock = VirtualClock(SCENARIO_START)
    real_utcnow = dt_util.utcnow
    real_time = time.time

    async def main():
        with patch_homeassistant_clock(clock):
            first = (
                dt_util.utcnow(),
                ha_util.utcnow(),
                ha_event.time_tracker_utcnow(),
                ha_event.time_tracker_timestamp(),
                time.time(),
                ha_script.utcnow(),  # bound at import via `from homeassistant.util.dt import utcnow`
            )
            await asyncio.sleep(3600)
            later = dt_util.utcnow()
            local = dt_util.now(ZoneInfo("America/Chicago"))
            default_local = dt_util.now()
        return first, later, local, default_local

    try:
        first, later, local, default_local = clock.loop.run_until_complete(main())
    finally:
        clock.loop.close()

    ts = SCENARIO_START.timestamp()
    assert first == (SCENARIO_START, SCENARIO_START, SCENARIO_START, ts, ts, SCENARIO_START)
    assert later == SCENARIO_START + timedelta(hours=1)
    assert local == later and local.tzinfo == ZoneInfo("America/Chicago")
    assert default_local == later and default_local.tzinfo == dt_util.get_default_time_zone()

    # Everything is restored on exit.
    assert dt_util.utcnow is real_utcnow
    assert ha_event.time_tracker_utcnow is real_utcnow
    assert time.time is real_time
    assert abs(dt_util.utcnow().timestamp() - real_time()) < 5


def test_ha_point_in_time_tracker_fires_at_exact_virtual_time(tmp_path):
    """HA's own timer helper (call_at + early-fire rearm) converges on the virtual clock."""
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.event import (
        async_track_point_in_utc_time,
        async_track_time_interval,
    )

    clock = VirtualClock(SCENARIO_START)
    fired = []
    ticks = []

    async def main():
        hass = HomeAssistant(str(tmp_path))
        points = [SCENARIO_START + timedelta(hours=h, seconds=0.123456) for h in (1, 5, 23)]
        for point in points:
            async_track_point_in_utc_time(hass, lambda now: fired.append((now, clock.utcnow())), point)
        cancel = async_track_time_interval(hass, lambda now: ticks.append(now), timedelta(minutes=5))
        await asyncio.sleep(24 * 3600 + 1)
        cancel()
        await hass.async_block_till_done()
        return points

    wall = time.monotonic()
    try:
        with patch_homeassistant_clock(clock):
            points = clock.loop.run_until_complete(main())
    finally:
        clock.loop.run_until_complete(clock.loop.shutdown_default_executor())
        clock.loop.close()

    assert fired == [(p, p) for p in points]
    assert len(ticks) == 24 * 12
    assert ticks[0] == SCENARIO_START + timedelta(minutes=5)
    assert ticks[-1] == SCENARIO_START + timedelta(hours=24)
    assert time.monotonic() - wall < 2.0


def _slow_job(seconds: float) -> str:
    time.sleep(seconds)
    return "done"
