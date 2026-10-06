"""Rate of change: what a Trend or Derivative helper on a numeric entity would report, step by step over a timeline (#42).

The miner proposes triggers on these helpers (`model.TrendTrigger`, `model.DerivativeTrigger`), so it needs their states without running Home Assistant. Each function here follows the helper's own code as of 2026.2-2026.9 and returns a `StepFunction` of the helper's numeric view, so the evaluator's numeric crossing logic (and the search's threshold search) runs on it unchanged:

- Trend (`homeassistant.components.trend.binary_sensor.SensorTrend`): every write of the source (`state_changed`, attribute-only ones too) appends a sample `(last_updated, value)` to a deque of at most `max_samples`; then samples older than `now - sample_duration` are dropped, and with at least `min_samples` left the gradient is the least-squares slope through them (numpy's `polyfit`, degree 1). The helper is on while the gradient passes `min_gradient` in its sign's direction. With fewer samples nothing is recomputed: the state stays as it was. An `unknown` / `unavailable` source makes the helper unavailable (the update still runs); a value that is not a number is an error that changes nothing. `trend_step`'s values are the gradient times `scale` (the miner uses the window, so a value is "the change over the window"), or None while the helper is unknown or unavailable: on for `min_gradient` g is then crossing above `g * scale` (rising) or below it (falling).
- Derivative (`homeassistant.components.derivative.sensor.DerivativeSensor`): on each write after the first valid one, the segment from the previous state's `last_reported` to this write gets the derivative `Δvalue / Δt` (in the unit per `unit_time`); segments ending more than `time_window` ago are dropped; the state is the newest segment's derivative when it alone spans more than the window, else the time-weighted sum of the segments over the window, rounded to `round` digits. The first valid write only sets the unit (and the state to 0). A source that is unavailable makes it unavailable; one that is not a number makes it unknown; a `total_increasing` source going down is dropped. `derivative_step`'s values are the rounded derivative, or None while unknown or unavailable.

Both start the way the sandbox starts them (sandbox/engine.py): set up before the initial states are seeded, so the seeded state is a write (two, at `last_changed` and at `last_updated`, when they differ) at the window start, and the automation attaches after.

Where this knowingly differs from Home Assistant (the sandbox is the exact answer): floating point instead of numpy and `Decimal`, so a value within rounding of a threshold may land on the other side; two writes at one instant (a zero-length segment, which Home Assistant's derivative fails on) are skipped; a source unit change (the derivative resets) is not followed.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .timeline import UNAVAILABLE_STATES, StepFunction, numeric

UNIT_SECONDS = {"s": 1, "min": 60, "h": 3600, "d": 86_400}


def seed_writes(step: StepFunction, start: float) -> list[float]:
    """When the sandbox writes the seeded state: at its `last_changed`, and again at its `last_updated` when that is later (an attribute-only update). Empty for an entity first written inside the window."""
    if not step.seeded:
        return []
    changed = min(step.changed[0], start)
    updated = max(min(step.seed_updated if step.seed_updated is not None else changed, start), changed)
    return [changed, updated] if updated > changed else [changed]


def _writes(step: StepFunction, start: float) -> list[tuple[float, float, int, bool]]:
    """`(now, last_updated, entry index, seeding)` of every write a helper sees, oldest first: the seeding ones (all at `now` = the window start, before the automation attaches), then the window's."""
    found = [(start, at, 0, True) for at in seed_writes(step, start)]
    found += [(step.times[i], step.times[i], i, False) for i in step.write_range()]
    return found


_NO_ATTRIBUTES: dict[str, Any] = {}


def _helper_step(entity_id: str, start: float, first: float | None, changes: list[tuple[float, float | None]]) -> StepFunction:
    """A step function of a helper's numeric view: `first` at the window start (after seeding), then each change. Only the numeric view is meant to be read (its states are blank)."""
    step = StepFunction(entity_id)
    times: list[float] = [start]
    values: list[Any] = [first]
    for at, value in changes:
        if value != values[-1]:
            times.append(at)
            values.append(value)
    step.times = times
    step.changed = times
    step.states = [""] * len(times)
    step.attributes = [_NO_ATTRIBUTES] * len(times)
    step.seeded = True
    step._numeric[None] = values  # noqa: SLF001
    return step


class _Fit:
    """A least-squares line through a sliding run of samples, `samples[lo:]`: O(1) per sample added or dropped. Times are kept relative to a reference near the run (re-summed when the run moves far from it), so the sums stay precise."""

    __slots__ = ("lo", "n", "ref", "samples", "st", "stt", "stv", "sv")

    def __init__(self) -> None:
        self.samples: list[tuple[float, float]] = []
        self.lo = 0
        self.ref = 0.0
        self.n = 0
        self.st = self.sv = self.stt = self.stv = 0.0

    def _resum(self) -> None:
        if self.lo > 4096:
            del self.samples[: self.lo]
            self.lo = 0
        run = self.samples[self.lo :]
        self.ref = run[-1][0] if run else 0.0
        self.n = len(run)
        self.st = sum(t - self.ref for t, _ in run)
        self.sv = sum(v for _, v in run)
        self.stt = sum((t - self.ref) ** 2 for t, _ in run)
        self.stv = sum((t - self.ref) * v for t, v in run)

    def add(self, t: float, v: float) -> None:
        if not self.n:
            self.ref = t
        self.samples.append((t, v))
        x = t - self.ref
        self.n += 1
        self.st += x
        self.sv += v
        self.stt += x * x
        self.stv += x * v

    def drop(self) -> None:
        t, v = self.samples[self.lo]
        self.lo += 1
        x = t - self.ref
        self.n -= 1
        self.st -= x
        self.sv -= v
        self.stt -= x * x
        self.stv -= x * v

    def first_time(self) -> float | None:
        return self.samples[self.lo][0] if self.n else None

    def slope(self, span: float) -> float:
        """The slope (0 when every sample shares one instant); `span` is how far the run may move from the reference before the sums are redone."""
        if self.n and abs(self.samples[-1][0] - self.ref) > 4 * span:
            self._resum()
        denominator = self.n * self.stt - self.st * self.st
        if denominator <= 1e-9 * max(self.n * self.stt, 1e-300):
            return 0.0
        return (self.n * self.stv - self.st * self.sv) / denominator


def trend_step(
    step: StepFunction,
    start: float,
    *,
    sample_duration: float,
    max_samples: int,
    min_samples: int = 2,
    attribute: str | None = None,
    scale: float = 1.0,
) -> StepFunction:
    """The gradient a Trend helper on `step` computes, times `scale`, as a step function (see the module docstring): None while it is unknown or unavailable."""
    fit = _Fit()
    available = True
    gradient: float | None = None  # None until computed once (the helper is unknown)
    first: float | None = None
    changes: list[tuple[float, float | None]] = []
    span = sample_duration or 3600.0
    for now, updated, i, seeding in _writes(step, start):
        raw = step.states[i] if attribute is None else step.attributes[i].get(attribute)
        if raw in UNAVAILABLE_STATES:
            available = False
        else:
            available = True
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue  # an error: no sample, no update
            fit.add(updated, value)
            while fit.n > max_samples:
                fit.drop()
        if sample_duration > 0:
            cutoff = now - sample_duration
            while fit.n and fit.first_time() < cutoff:
                fit.drop()
        if fit.n >= min_samples:
            gradient = fit.slope(span) * scale
        out = gradient if available else None
        if seeding:
            first = out
        else:
            changes.append((now, out))
    return _helper_step(f"trend:{step.entity_id}", start, first, changes)


def derivative_step(
    step: StepFunction,
    start: float,
    *,
    time_window: float,
    unit_time: str = "h",
    round_digits: int = 2,
    unit_prefix: float = 1.0,
) -> StepFunction:
    """The state a Derivative helper on `step` reports, as a step function (see the module docstring): None while it is unknown or unavailable."""
    per = UNIT_SECONDS[unit_time]
    segments: deque[tuple[float, float, float]] = deque()  # (start, end, derivative), oldest first: ends and starts only grow
    total = 0.0  # sum of derivative * (end - start) over the segments
    unit_set = False
    value: float | None = None  # the helper's native value (unavailable is tracked apart)
    available = False  # its source has no state when it is set up
    last_valid: tuple[float, float] | None = None
    previous: tuple[Any, float] | None = None  # the source's previous state and its last_reported
    first: float | None = None
    changes: list[tuple[float, float | None]] = []

    def prune(now: float) -> None:
        nonlocal total
        while segments and now - segments[0][1] >= time_window:
            begin, end, d = segments.popleft()
            total -= d * (end - begin)
        if not segments:
            total = 0.0

    def add(begin: float, end: float, d: float) -> None:
        nonlocal total
        segments.append((begin, end, d))
        total += d * (end - begin)

    def weighted(now: float) -> float:
        """Each segment's derivative weighted by its share of the window (the part before the window left out)."""
        window_start = now - time_window
        found = total
        for begin, end, d in segments:
            if begin >= window_start:
                break
            found -= d * (min(window_start, end) - begin)
        return found / time_window

    def reset() -> None:
        nonlocal total
        segments.clear()
        total = 0.0

    # Writes identical to the state in effect fire `state_reported`: the derivative takes a flat segment up to each (unless it reads 0), and the next segment starts there.
    writes = [(*write, False) for write in _writes(step, start)]
    writes += [(at, at, step.index_at(at), False, True) for at in step.reports]
    writes.sort(key=lambda item: (item[0], item[4]))
    for now, updated, i, seeding, reported in writes:
        state = step.states[i]
        old = previous
        previous = (state, updated)
        if reported:
            if state == "unavailable":
                available = False
            elif not isinstance(numeric(state), float) or state in UNAVAILABLE_STATES:
                available, value = True, None
            elif value != 0 and old is not None:
                available = True
                prune(updated)
                if updated > old[1]:
                    add(old[1], updated, 0.0)
                    last_valid = (float(state), updated)
                    value = round(0.0 if updated - old[1] > time_window else weighted(updated), round_digits)
            else:
                available = True
        elif state == "unavailable":
            available = False
        elif not isinstance(numeric(state), float) or state in UNAVAILABLE_STATES:
            available, value = True, None
        else:
            available = True
            if not unit_set:
                # The first valid state sets the unit (from none), which resets the helper to 0.
                unit_set, value = True, 0.0
                reset()
            elif old is not None:
                old_state, old_at = old
                if not isinstance(numeric(old_state), float) or old_state in UNAVAILABLE_STATES:
                    if last_valid is not None:
                        old_state, old_at = last_valid
                    else:
                        old_state = None
                if old_state is not None and updated > old_at:
                    prune(updated)
                    elapsed = updated - old_at
                    rate = (float(state) - float(old_state)) / elapsed / unit_prefix * per
                    if rate < 0 and step.attributes[i].get("state_class") == "total_increasing":
                        if old is not None and (not isinstance(numeric(old[0]), float) or old[0] in UNAVAILABLE_STATES):
                            last_valid, value = (float(state), updated), 0.0
                            reset()
                    else:
                        add(old_at, updated, rate)
                        last_valid = (float(state), updated)
                        value = round(rate if elapsed > time_window else weighted(updated), round_digits)
        out = value if available else None
        if seeding:
            first = out
        else:
            changes.append((now, out))
    return _helper_step(f"derivative:{step.entity_id}", start, first, changes)
