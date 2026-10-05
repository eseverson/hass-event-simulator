"""Virtual-time asyncio event loop for the replay sandbox.

Standard library only; Home Assistant is imported lazily inside `patch_homeassistant_clock`, so this module loads in the child process before (and without) HA.

How time works here
-------------------
`VirtualTimeEventLoop.time()` returns a virtual clock that never moves on its own. Each loop iteration the stock asyncio machinery asks the selector to wait until the next timer is due; our selector first polls real I/O without blocking, and if nothing is ready it jumps virtual time straight to the earliest scheduled `TimerHandle` instead of sleeping. Timers therefore fire in order, at exactly their scheduled virtual time, and a day of timers takes milliseconds. `call_soon` callbacks run before any jump, so interleaving is unchanged.

Real work is never skipped over: while a job submitted through `loop.run_in_executor` (which is how `hass.async_add_executor_job` and the import executor dispatch) is outstanding, the selector blocks on real I/O until the job's completion wakes the loop. Virtual time stands still meanwhile, so executor work takes zero virtual time and the replay stays deterministic. Registered sockets/pipes do not hold time back (they are polled every iteration but may be idle forever); a thread that calls `call_soon_threadsafe` unprompted lands at whatever virtual time the loop has reached. With nothing scheduled and nothing outstanding the loop blocks on real I/O exactly like a stock loop.

The loop's virtual time is the scenario's wall-clock Unix timestamp (`VirtualClock` starts the loop at `start.timestamp()`), so `loop.time()` and the patched `time.time()` are the same number. That matters for HA's `async_track_point_in_utc_time`, which schedules `call_at(loop.time() + target - time.time())` and re-arms if `time_tracker_timestamp()` says it fired early: with one shared scale the re-arm converges onto the exact target instead of chasing float rounding between two clocks.

What `patch_homeassistant_clock` patches, and why
------------------------------------------------
HA reads wall time through a handful of module-level functions (the same seams HA's own tests patch in `tests/common.py::_async_fire_time_changed` and `tests/patch_time.py`):

- `homeassistant.util.dt.utcnow` — the main "now" for triggers, conditions, templates (`utcnow()`), traces, the sun helpers and `Script` (which binds it at import via `from homeassistant.util.dt import utcnow`).
- `homeassistant.util.dt.now` — calls `datetime.now(tz)` directly rather than `utcnow`, so it needs its own patch; used by time triggers, `time` conditions and the `now()` template.
- `homeassistant.helpers.event.time_tracker_utcnow` / `time_tracker_timestamp` — what time-pattern/interval trackers report and what the point-in-time tracker uses to detect early firing.
- `time.time` (the `time` module attribute) — `State` timestamps (`StateMachine.async_set`), `Event.time_fired`, rate limiting and entity timing all call `time.time()`.

Names already bound to any of these at import time (`from homeassistant.util.dt import utcnow` in `helpers/script.py` and the registries, `timer = time.time` in `helpers/entity.py`, `homeassistant.util.utcnow`) are found by scanning loaded `homeassistant*` / `custom_components*` modules and rebound; modules imported while the patch is active pick up the patched names naturally. On exit every rebinding is undone, including ones in modules imported during the patch.

Deliberately not patched:

- `time.monotonic` and `loop.time()` — the loop's own `time()` is already virtual and is what asyncio timers, `asyncio.timeout`, HA `async_call_later`, `Script` delays and `wait_for_trigger` timeouts use. Patching `time.monotonic` process-wide would freeze timeouts inside executor threads and the stdlib (queues, `threading` waits) while virtual time stands still. HA reads it only for slow-shutdown / block-till-done diagnostics.
- `datetime.datetime.now()` called directly (C type, cannot be patched) — HA's automation paths go through `dt_util`, but a third-party library calling it directly sees real time.
- ULID context ids, `time.time_ns`, `time.perf_counter` and logging timestamps — real time; they only label things.
- `from time import time` bindings outside `homeassistant*` / `custom_components*` modules.

Typical use (engine)
--------------------
    clock = VirtualClock(scenario_start)
    with patch_homeassistant_clock(clock):
        clock.call_at_datetime(event_time, play_event, event)  # timeline events
        result = clock.loop.run_until_complete(replay())
    clock.loop.run_until_complete(clock.loop.shutdown_default_executor())
    clock.loop.close()
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import selectors
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, tzinfo
from typing import Any

__all__ = ["VirtualClock", "VirtualTimeEventLoop", "patch_homeassistant_clock"]

# Timers due within this many virtual seconds of "now" run in the current iteration. It must exceed the float spacing of loop.time() at Unix-epoch magnitude (~2.4e-7 s), otherwise a timer due exactly now is never popped and the loop spins.
_CLOCK_RESOLUTION = 1e-6

# Module name prefixes whose import-time bindings of the clock functions are rebound.
_PATCH_MODULE_PREFIXES = ("homeassistant", "custom_components")


class _VirtualSelector(selectors.DefaultSelector):
    """Selector that turns an idle wait into a virtual-time jump."""

    def __init__(self) -> None:
        super().__init__()
        self.loop: VirtualTimeEventLoop | None = None

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        loop = self.loop
        if loop is None or (timeout is not None and timeout <= 0):
            return super().select(timeout)
        if timeout is None or loop.pending_real_work:
            # Nothing scheduled, or an executor job is in flight: wait for real I/O. A finished job wakes us through the loop's self-pipe; virtual time stands still meanwhile.
            return super().select(None)
        ready = super().select(0)
        if ready:
            return ready
        loop._jump(timeout)
        return []


class VirtualTimeEventLoop(asyncio.SelectorEventLoop):
    """Selector event loop whose `time()` is virtual and jumps forward when idle."""

    def __init__(self, start: float = 0.0) -> None:
        selector = _VirtualSelector()
        super().__init__(selector)
        selector.loop = self
        self._now = float(start)
        self._clock_resolution = _CLOCK_RESOLUTION
        self._real_work = 0
        self._real_work_lock = threading.Lock()
        self._vt_default_executor: concurrent.futures.ThreadPoolExecutor | None = None

    def time(self) -> float:
        """Return the current virtual time."""
        return self._now

    @property
    def pending_real_work(self) -> int:
        """Number of executor jobs submitted through this loop and not yet finished."""
        return self._real_work

    def _jump(self, timeout: float) -> None:
        """Advance virtual time to the earliest scheduled timer (or by `timeout`)."""
        target = self._now + timeout
        scheduled = getattr(self, "_scheduled", None)
        if scheduled:
            # Land exactly on the timer's `when` rather than now + (when - now), which can be off by a rounding step.
            target = scheduled[0].when()
        self._now = max(self._now, target)

    def set_default_executor(self, executor: Any) -> None:
        super().set_default_executor(executor)
        self._vt_default_executor = executor

    def run_in_executor(self, executor: Any, func: Callable[..., Any], *args: Any) -> asyncio.Future[Any]:
        self._check_closed()
        if executor is None:
            if self._vt_default_executor is None:
                self.set_default_executor(concurrent.futures.ThreadPoolExecutor(thread_name_prefix="asyncio"))
            executor = self._vt_default_executor
        return super().run_in_executor(_TrackingExecutor(self, executor), func, *args)

    def _real_work_started(self) -> None:
        with self._real_work_lock:
            self._real_work += 1

    def _real_work_finished(self, _future: object = None) -> None:
        with self._real_work_lock:
            self._real_work -= 1


class _TrackingExecutor:
    """Executor proxy that counts a job as outstanding real work until it finishes."""

    __slots__ = ("_executor", "_loop")

    def __init__(self, loop: VirtualTimeEventLoop, executor: Any) -> None:
        self._loop = loop
        self._executor = executor

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> concurrent.futures.Future[Any]:
        self._loop._real_work_started()
        try:
            future = self._executor.submit(fn, *args, **kwargs)
        except BaseException:
            self._loop._real_work_finished()
            raise
        # Added before asyncio's wrap_future hooks its own callback, so the count drops before the loop is woken to see the result.
        future.add_done_callback(self._loop._real_work_finished)
        return future


class VirtualClock:
    """Maps the loop's virtual time onto the scenario's wall clock.

    Creates the `VirtualTimeEventLoop` (`self.loop`) with virtual time equal to `start`'s Unix timestamp, so `timestamp()` is `loop.time()` and `utcnow()` is `start` plus the virtual seconds elapsed.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("VirtualClock start must be a timezone-aware datetime")
        self.start = start.astimezone(UTC)
        self._start_ts = self.start.timestamp()
        self.loop = VirtualTimeEventLoop(start=self._start_ts)

    def timestamp(self) -> float:
        """Virtual Unix timestamp (stands in for `time.time()`)."""
        return self.loop.time()

    def utcnow(self) -> datetime:
        """Virtual now as an aware UTC datetime (stands in for `dt_util.utcnow()`)."""
        return datetime.fromtimestamp(self.loop.time(), UTC)

    def now(self, time_zone: tzinfo) -> datetime:
        """Virtual now in `time_zone`."""
        return self.utcnow().astimezone(time_zone)

    def elapsed(self) -> float:
        """Virtual seconds since the scenario start."""
        return self.loop.time() - self._start_ts

    def loop_time_for(self, when: datetime) -> float:
        """Loop time at which the virtual wall clock reads `when` (aware datetime)."""
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("when must be a timezone-aware datetime")
        return when.timestamp()

    def call_at_datetime(self, when: datetime, callback: Callable[..., object], *args: Any) -> asyncio.TimerHandle:
        """Schedule `callback(*args)` for the virtual instant `when`; a past `when` runs on the next iteration."""
        return self.loop.call_at(self.loop_time_for(when), callback, *args)


@contextlib.contextmanager
def patch_homeassistant_clock(clock: VirtualClock) -> Iterator[None]:
    """Point Home Assistant's wall-clock reads (and `time.time`) at `clock` for the duration.

    See the module docstring for exactly what is patched and what is not. Not reentrant; meant to wrap one replay in a dedicated child process.
    """
    # Lazy: the sandbox must import without HA, and only this helper needs it.
    from homeassistant.helpers import event as ha_event
    from homeassistant.util import dt as dt_util

    def make_utcnow() -> Callable[[], datetime]:
        def utcnow() -> datetime:
            return clock.utcnow()

        return utcnow

    def make_now() -> Callable[..., datetime]:
        def now(time_zone: tzinfo | None = None) -> datetime:
            return clock.utcnow().astimezone(time_zone or dt_util.get_default_time_zone())

        return now

    def make_timestamp() -> Callable[[], float]:
        def timestamp() -> float:
            return clock.loop.time()

        return timestamp

    # original object -> replacement; each original gets its own replacement so restore is unambiguous.
    originals = (
        (dt_util.utcnow, make_utcnow),
        (dt_util.now, make_now),
        (ha_event.time_tracker_utcnow, make_utcnow),
        (ha_event.time_tracker_timestamp, make_timestamp),
        (time.time, make_timestamp),
    )
    forward: dict[int, tuple[object, object]] = {}
    for original, factory in originals:
        if id(original) not in forward:
            forward[id(original)] = (original, factory())
    backward = {id(new): (new, old) for old, new in forward.values()}

    _rebind(forward)
    try:
        yield
    finally:
        _rebind(backward)


def _rebind(mapping: dict[int, tuple[object, object]]) -> None:
    """Replace every module global that *is* a mapped object, in `time` and the HA/custom modules."""
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "time" or name.startswith(_PATCH_MODULE_PREFIXES)):
            continue
        namespace = getattr(module, "__dict__", None)
        if namespace is None:
            continue
        for attr, value in list(namespace.items()):
            entry = mapping.get(id(value))
            if entry is not None and entry[0] is value:
                setattr(module, attr, entry[1])
