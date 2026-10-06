// Auto-run: when the panel replays after an edit, and which answers it keeps. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AUTO_RUN_DELAY,
  LARGE_SCENARIO_BYTES,
  MAX_AUTO_RUN_DELAY,
  ReplayScheduler,
  SLOW_RUN_MS,
  autoRunDelay,
  readAutoRunPref,
  replayOutcome,
  simulateSubscribeMessage,
  writeAutoRunPref,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

/* A scheduler on a fake clock: `advance(ms)` runs the timers that come due. */
function harness({ enabled = true, ready = true, size = 1000, skip = () => false } = {}) {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  const started = [];
  const cancelled = [];
  const scheduler = new ReplayScheduler({
    enabled,
    now: () => now,
    setTimer: (fn, ms) => {
      const id = nextId++;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    start: (seq, opts) => {
      if (skip(seq, opts)) return null;
      started.push({ seq, ...opts });
      return { size };
    },
    cancel: (seq) => cancelled.push(seq),
  });
  scheduler.setReady(ready);
  const advance = (ms) => {
    const until = now + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      now = due[1].at;
      due[1].fn();
    }
    now = until;
  };
  return { scheduler, started, cancelled, advance, timers, setNow: (t) => (now = t), getNow: () => now };
}

test("a change replays once the edits pause for the debounce delay", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY - 1);
  assert.equal(h.started.length, 0);
  h.advance(1);
  assert.deepEqual(h.started.map((s) => s.seq), [1]);
});

test("a burst of changes replays once, after the last one", () => {
  const h = harness();
  for (let i = 0; i < 10; i++) {
    h.scheduler.change();
    h.advance(AUTO_RUN_DELAY / 2);
  }
  assert.equal(h.started.length, 0);
  h.advance(AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1);
});

test("a newer replay cancels the one in flight, and the old answer is stale", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  assert.deepEqual(h.started.map((s) => s.seq), [1, 2]);
  assert.deepEqual(h.cancelled, [1]);
  assert.equal(h.scheduler.finish(1), false);
  assert.equal(h.scheduler.running, true);
  assert.equal(h.scheduler.finish(2), true);
  assert.equal(h.scheduler.running, false);
});

test("the replay counts as running while start sends it (the panel redraws from inside start)", () => {
  let runningInside = null;
  const scheduler = new ReplayScheduler({
    start: () => {
      runningInside = scheduler.running;
      return { size: 1 };
    },
    cancel: () => {},
  });
  scheduler.setReady(true);
  scheduler.runNow();
  assert.equal(runningInside, true);
});

test("a skipped start leaves nothing running", () => {
  const scheduler = new ReplayScheduler({
    start: () => null,
    cancel: () => {},
  });
  scheduler.setReady(true);
  scheduler.runNow();
  assert.equal(scheduler.running, false);
});

test("an answer arriving twice counts once", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  assert.equal(h.scheduler.finish(1), true);
  assert.equal(h.scheduler.finish(1), false);
});

test("the in-flight replay keeps running while the next change waits out its debounce", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY - 1);
  assert.deepEqual(h.cancelled, []);
  assert.equal(h.scheduler.running, true);
});

test("with auto-run off, a change only marks the result out of date", () => {
  const h = harness({ enabled: false });
  h.scheduler.change();
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 0);
  assert.equal(h.scheduler.dirty, true);
});

test("turning auto-run on replays what changed while it was off", () => {
  const h = harness({ enabled: false });
  h.scheduler.change();
  h.scheduler.setEnabled(true);
  h.advance(AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1);
});

test("turning auto-run on with nothing changed does not replay", () => {
  const h = harness({ enabled: false });
  h.scheduler.setEnabled(true);
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 0);
});

test("turning auto-run off drops the pending replay but lets the running one finish", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.scheduler.change();
  h.scheduler.setEnabled(false);
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1);
  assert.deepEqual(h.cancelled, []);
  assert.equal(h.scheduler.finish(1), true);
});

test("run now replays at once, forced, and replaces the one in flight", () => {
  const h = harness({ enabled: false });
  h.scheduler.runNow();
  assert.deepEqual(h.started, [{ seq: 1, forced: true }]);
  h.scheduler.runNow();
  assert.deepEqual(h.cancelled, [1]);
  assert.equal(h.started.length, 2);
});

test("run now clears a pending debounce", () => {
  const h = harness();
  h.scheduler.change();
  h.scheduler.runNow();
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1);
});

test("an automatic replay is not forced", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  assert.deepEqual(h.started, [{ seq: 1, forced: false }]);
});

test("while not ready (history loading) the replay waits, then runs after the debounce", () => {
  const h = harness({ ready: false });
  h.scheduler.change();
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 0);
  h.scheduler.setReady(true);
  h.advance(AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1);
});

test("run now while not ready runs as soon as it is ready", () => {
  const h = harness({ ready: false, enabled: false });
  h.scheduler.runNow();
  assert.equal(h.started.length, 0);
  h.scheduler.setReady(true);
  assert.deepEqual(h.started, [{ seq: 1, forced: true }]);
});

test("becoming ready with nothing changed does not replay", () => {
  const h = harness({ ready: false });
  h.scheduler.setReady(true);
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 0);
});

test("saying ready again does not restart the debounce", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY - 100);
  h.scheduler.setReady(true);
  h.advance(100);
  assert.equal(h.started.length, 1);
});

test("invalidate cancels the replay in flight and makes its answer stale", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.scheduler.change();
  h.scheduler.invalidate();
  assert.deepEqual(h.cancelled, [1]);
  assert.equal(h.scheduler.running, false);
  assert.equal(h.scheduler.finish(1), false);
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1, "the pending change went with it");
});

test("a start that runs nothing (unchanged inputs, too large) leaves nothing in flight", () => {
  const h = harness({ skip: () => true });
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  assert.equal(h.scheduler.running, false);
  assert.equal(h.scheduler.dirty, false);
});

test("dispose cancels everything", () => {
  const h = harness();
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.scheduler.change();
  h.scheduler.dispose();
  assert.deepEqual(h.cancelled, [1]);
  h.advance(10 * AUTO_RUN_DELAY);
  assert.equal(h.started.length, 1);
});

test("the debounce is the base delay until a large scenario replays slowly", () => {
  assert.equal(autoRunDelay(null), AUTO_RUN_DELAY);
  assert.equal(autoRunDelay({ duration: 200, size: 10 * LARGE_SCENARIO_BYTES }), AUTO_RUN_DELAY);
  assert.equal(autoRunDelay({ duration: 2 * SLOW_RUN_MS, size: 1000 }), AUTO_RUN_DELAY, "slow but small: the sandbox boot, not the input");
  assert.equal(autoRunDelay({ duration: SLOW_RUN_MS + 1000, size: LARGE_SCENARIO_BYTES + 1 }), SLOW_RUN_MS + 1000);
  assert.equal(autoRunDelay({ duration: 60e3, size: LARGE_SCENARIO_BYTES + 1 }), MAX_AUTO_RUN_DELAY);
});

test("after a slow replay of a large scenario the next one waits longer", () => {
  const h = harness({ size: LARGE_SCENARIO_BYTES * 2 });
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.advance(5000);
  assert.equal(h.scheduler.finish(1), true);
  assert.equal(h.scheduler.delay, 5000);
  h.scheduler.change();
  h.advance(4999);
  assert.equal(h.started.length, 1);
  h.advance(1);
  assert.equal(h.started.length, 2);
});

test("a cancelled replay does not teach the backoff anything", () => {
  const h = harness({ size: LARGE_SCENARIO_BYTES * 2 });
  h.scheduler.change();
  h.advance(AUTO_RUN_DELAY);
  h.advance(9000);
  h.scheduler.runNow();
  assert.equal(h.scheduler.delay, AUTO_RUN_DELAY);
});

test("the auto-run preference defaults to on and survives storage that throws", () => {
  const map = new Map();
  const storage = { getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, String(v)) };
  assert.equal(readAutoRunPref(storage), true);
  writeAutoRunPref(false, storage);
  assert.equal(readAutoRunPref(storage), false);
  writeAutoRunPref(true, storage);
  assert.equal(readAutoRunPref(storage), true);
  const broken = { getItem: () => { throw new Error("SecurityError"); }, setItem: () => { throw new Error("QuotaExceeded"); } };
  assert.equal(readAutoRunPref(broken), true);
  assert.doesNotThrow(() => writeAutoRunPref(false, broken));
  assert.equal(readAutoRunPref(undefined), true);
});

test("a replay event is a result, an error or superseded", () => {
  const ok = replayOutcome({ result: { runs: [{ index: 0, outcome: "fired", started: "2026-10-04T01:00:00Z" }] } });
  assert.equal(ok.kind, "result");
  assert.equal(ok.result.runs.length, 1);

  const crash = replayOutcome({ error: { code: "sandbox_failed", message: "The sandbox exited with exit code 1", stderr_tail: "Traceback: boom\n" } });
  assert.equal(crash.kind, "error");
  assert.equal(crash.error.code, "sandbox_failed");
  assert.equal(crash.error.stderr, "Traceback: boom");

  assert.equal(replayOutcome({ error: { code: "superseded", message: "replaced" } }).kind, "superseded");
  // A result the child answered with a structured error, as the one-shot command used to see it.
  const refused = replayOutcome({ result: { error: { code: "invalid_scenario", message: "bad" } } });
  assert.equal(refused.kind, "error");
  assert.equal(refused.error.code, "invalid_scenario");
});

test("a rejected subscribe (schema, permissions) reads like any other failure", () => {
  const out = replayOutcome({ error: { code: "invalid_format", message: "expected dict" } });
  assert.equal(out.kind, "error");
  assert.equal(out.error.title, "Home Assistant refused the request");
});

test("the subscribe request carries the packed scenario", () => {
  const msg = simulateSubscribeMessage({ start: "a", end: "b", automation: {}, events: [{ at: "2026-10-04T00:00:00Z", entity_id: "x.y", state: "1" }] });
  assert.equal(msg.type, "event_simulator/simulate/subscribe");
  assert.deepEqual(msg.scenario.events.entity_ids, ["x.y"]);
});
