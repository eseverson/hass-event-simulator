// Pure-function tests for the panel: `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildScenario,
  buildSegments,
  callsForRun,
  chooseStep,
  clampView,
  classifyOutcome,
  conditionResults,
  describeTrigger,
  findNearest,
  formatDuration,
  isNumericStates,
  makeScale,
  mergeSegments,
  normalizeResult,
  numericRange,
  panView,
  segmentAt,
  stateColor,
  stepPath,
  timeTicks,
  toNumber,
  visibleSegments,
  zoomView,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const H = 3600e3;
const iso = (ms) => new Date(ms).toISOString();

test("scale maps time to x and back", () => {
  const s = makeScale(T0, T0 + 10 * H, 1000);
  assert.equal(s.toX(T0), 0);
  assert.equal(s.toX(T0 + 5 * H), 500);
  assert.equal(s.toX(T0 + 10 * H), 1000);
  assert.equal(s.toT(250), T0 + 2.5 * H);
  assert.equal(s.toT(s.toX(T0 + 1234567)), T0 + 1234567);
});

test("numbers are strict", () => {
  assert.equal(toNumber("21.5"), 21.5);
  assert.equal(toNumber("-3"), -3);
  assert.equal(toNumber("1e3"), 1000);
  assert.equal(toNumber(" 7 "), 7);
  for (const bad of ["", " ", "0x1f", "on", "12abc", null, undefined, "Infinity"]) assert.ok(Number.isNaN(toNumber(bad)), String(bad));
});

test("numeric detection ignores unavailable/unknown but needs one number", () => {
  assert.equal(isNumericStates(["1", "2.5", "unavailable", "unknown"]), true);
  assert.equal(isNumericStates(["1", "on"]), false);
  assert.equal(isNumericStates(["unavailable"]), false);
  assert.equal(isNumericStates([]), false);
});

test("segments start at the initial state and end at the window end", () => {
  const segs = buildSegments(
    { state: "off", attributes: {} },
    [
      { at: iso(T0 + 2 * H), state: "on" },
      { at: iso(T0 + H), state: "unavailable" },
    ],
    T0,
    T0 + 4 * H
  );
  assert.deepEqual(
    segs.map((s) => [s.from - T0, s.to - T0, s.state]),
    [
      [0, H, "off"],
      [H, 2 * H, "unavailable"],
      [2 * H, 4 * H, "on"],
    ]
  );
});

test("without an initial state nothing is drawn before the first event", () => {
  const segs = buildSegments(null, [{ at: iso(T0 + H), state: "5" }], T0, T0 + 2 * H);
  assert.deepEqual(segs.map((s) => [s.from - T0, s.to - T0]), [[H, 2 * H]]);
  assert.deepEqual(buildSegments(null, [], T0, T0 + H), []);
});

test("events at the same instant keep the last; events outside the window are clamped or dropped", () => {
  const segs = buildSegments(
    { state: "a" },
    [
      { at: iso(T0 - H), state: "early" },
      { at: iso(T0 + H), state: "b" },
      { at: iso(T0 + H), state: "c" },
      { at: iso(T0 + 3 * H), state: "late" },
      { at: "garbage", state: "x" },
    ],
    T0,
    T0 + 2 * H
  );
  assert.deepEqual(segs.map((s) => s.state), ["early", "c"]);
});

test("merging joins equal adjacent states only", () => {
  const merged = mergeSegments([
    { from: 0, to: 1, state: "on" },
    { from: 1, to: 2, state: "on" },
    { from: 2, to: 3, state: "off" },
    { from: 3, to: 4, state: "on" },
  ]);
  assert.deepEqual(merged.map((s) => [s.from, s.to, s.state]), [
    [0, 2, "on"],
    [2, 3, "off"],
    [3, 4, "on"],
  ]);
});

test("segment lookup and visible range", () => {
  const segs = [
    { from: 0, to: 10, state: "a" },
    { from: 10, to: 20, state: "b" },
    { from: 25, to: 30, state: "c" },
  ];
  assert.equal(segmentAt(segs, 0).state, "a");
  assert.equal(segmentAt(segs, 10).state, "b");
  assert.equal(segmentAt(segs, 22), null);
  assert.equal(segmentAt(segs, 30), null);
  assert.deepEqual(visibleSegments(segs, 12, 26).map((s) => s.state), ["b", "c"]);
  assert.deepEqual(visibleSegments(segs, 20, 25), []);
});

test("numeric range and step path break on missing values", () => {
  const segs = [
    { from: 0, to: 10, state: "1" },
    { from: 10, to: 20, state: "3" },
    { from: 20, to: 30, state: "unavailable" },
    { from: 30, to: 40, state: "2" },
  ];
  assert.deepEqual(numericRange(segs), { min: 1, max: 3 });
  assert.equal(numericRange([{ state: "unknown" }]), null);
  const d = stepPath(segs, (t) => t, (v) => 100 - v);
  assert.equal(d, "M0,99L10,99L10,97L20,97M30,98L40,98");
});

test("ticks pick a step that fits and align to whole local units", () => {
  assert.equal(chooseStep(24 * H, 1200), 3 * H);
  assert.equal(chooseStep(60e3, 1200), 5e3);
  const { step, ticks } = timeTicks(T0 + 10 * 60e3, T0 + 6 * H, 600, () => 0);
  assert.equal(step, H);
  assert.equal(ticks[0], T0 + H);
  assert.equal(ticks.length, 6);
  // A +02:00 zone: hourly ticks still land on the hour, 3-hourly on local multiples of 3.
  const shifted = timeTicks(T0, T0 + 24 * H, 600, () => 120);
  assert.equal(shifted.step, 6 * H);
  assert.equal((shifted.ticks[0] + 2 * H) % (6 * H), 0);
});

test("zoom keeps the anchor in place and stays inside the window", () => {
  const bounds = [0, 1000e3];
  assert.deepEqual(zoomView([0, 1000e3], 500e3, 0.5, bounds), [250e3, 750e3]);
  const [a, b] = zoomView([0, 1000e3], 100e3, 0.5, bounds);
  assert.equal(a, 50e3);
  assert.equal(b, 550e3);
  assert.deepEqual(zoomView([250e3, 750e3], 500e3, 4, bounds), [0, 1000e3]);
  const [c, d] = zoomView([0, 1000e3], 0, 1e-9, bounds);
  assert.equal(d - c, 10e3);
});

test("pan keeps the span and stops at the edges", () => {
  const k = 1e3;
  const bounds = [0, 100 * k];
  assert.deepEqual(panView([10 * k, 30 * k], 50 * k, bounds), [60 * k, 80 * k]);
  assert.deepEqual(panView([10 * k, 30 * k], 500 * k, bounds), [80 * k, 100 * k]);
  assert.deepEqual(panView([10 * k, 30 * k], -500 * k, bounds), [0, 20 * k]);
  assert.deepEqual(clampView(-5, 200, 0, 100, 1), [0, 100]);
});

test("nearest item within a pixel radius", () => {
  const items = [{ x: 10, id: "a" }, { x: 40, id: "b" }];
  assert.equal(findNearest(items, 14, 5).id, "a");
  assert.equal(findNearest(items, 25, 5), null);
});

test("state colors: missing is null, the rest stable", () => {
  assert.equal(stateColor("unavailable"), null);
  assert.equal(stateColor("unknown"), null);
  assert.equal(stateColor("heating"), stateColor("heating"));
  assert.match(stateColor("on"), /primary-color/);
});

test("result parsing is defensive and keeps run indexes for service calls", () => {
  const r = normalizeResult({
    runs: [
      { started: iso(T0 + H), finished: iso(T0 + H + 5000), outcome: "fired", trigger: { description: "state of door" } },
      { started: "nope", outcome: "fired" },
      { started: iso(T0 + 2 * H), outcome: "condition_failed", stopped_by: "condition/0" },
      { started: iso(T0 + 3 * H), outcome: "weird" },
      { started: iso(T0 + 4 * H), error: "boom" },
      "not an object",
    ],
    service_calls: [
      { at: iso(T0 + H + 1000), domain: "light", service: "turn_on", run: 0 },
      { at: "bad", domain: "x", service: "y", run: 0 },
      null,
    ],
    warnings: ["unknown service light.nope", { code: 1 }],
  });
  assert.deepEqual(r.runs.map((x) => [x.index, x.outcome]), [
    [0, "fired"],
    [2, "condition_failed"],
    [3, "other"],
    [4, "error"],
  ]);
  assert.equal(r.runs[0].finished - r.runs[0].started, 5000);
  assert.equal(r.runs[1].finished, r.runs[1].started);
  assert.equal(r.calls.length, 1);
  assert.deepEqual(callsForRun(r.calls, r.runs[0]).map((c) => c.service), ["turn_on"]);
  assert.deepEqual(callsForRun(r.calls, r.runs[1]), []);
  assert.deepEqual(r.warnings, [
    { code: "", message: "unknown service light.nope", at: null },
    { code: "1", message: '{"code":1}', at: null },
  ]);
  assert.deepEqual(normalizeResult(null), { runs: [], calls: [], warnings: [] });
  assert.deepEqual(normalizeResult({ runs: "x", service_calls: {}, warnings: 3 }), { runs: [], calls: [], warnings: [] });
});

test("calls can reference a run by id", () => {
  const r = normalizeResult({
    runs: [{ started: iso(T0), outcome: "fired", trace: { run_id: "abc" } }],
    service_calls: [{ at: iso(T0), domain: "a", service: "b", run: "abc" }],
  });
  assert.equal(callsForRun(r.calls, r.runs[0]).length, 1);
});

test("outcomes classify", () => {
  assert.equal(classifyOutcome("FIRED"), "fired");
  assert.equal(classifyOutcome(undefined, { error: "x" }), "error");
  assert.equal(classifyOutcome("cancelled"), "other");
});

test("conditions come out of an extended trace in order with depth", () => {
  const conds = conditionResults({
    trace: {
      "trigger/0": [{}],
      "condition/0": [{ result: { result: true } }],
      "condition/1": [{ result: { result: false } }],
      "condition/1/conditions/0": [{ result: { result: false } }],
      "condition/2": [{ error: "template error" }],
      "action/0": [{}],
    },
  });
  assert.deepEqual(conds.map((c) => [c.path, c.depth, c.result]), [
    ["condition/0", 0, true],
    ["condition/1", 0, false],
    ["condition/1/conditions/0", 1, false],
    ["condition/2", 0, null],
  ]);
  assert.equal(conds[3].error, "template error");
  assert.deepEqual(conditionResults(null), []);
  assert.deepEqual(conditionResults({ trace: [] }), []);
});

test("triggers describe themselves", () => {
  assert.equal(describeTrigger("state of x"), "state of x");
  assert.equal(describeTrigger({ description: "d" }), "d");
  assert.equal(
    describeTrigger({ platform: "state", entity_id: "binary_sensor.door", from_state: { state: "off" }, to_state: { state: "on" } }),
    "state binary_sensor.door off → on"
  );
  assert.equal(describeTrigger(null), "");
});

test("scenario merges tracks in time order and omits core", () => {
  const s = buildScenario({
    automation: { alias: "x" },
    start: T0,
    end: T0 + H,
    tracks: [
      { entity_id: "a.a", initial: { state: "off", attributes: {} }, events: [{ at: iso(T0 + 30e3), entity_id: "a.a", state: "on" }] },
      { entity_id: "b.b", initial: null, events: [{ at: iso(T0 + 10e3), entity_id: "b.b", state: "1" }] },
    ],
  });
  assert.equal(s.version, 1);
  assert.equal(s.start, "2026-10-04T00:00:00.000Z");
  assert.equal(s.end, "2026-10-04T01:00:00.000Z");
  assert.deepEqual(Object.keys(s.initial_states), ["a.a"]);
  assert.deepEqual(s.events.map((e) => e.entity_id), ["b.b", "a.a"]);
  assert.equal("core" in s, false);
});

test("durations read naturally", () => {
  assert.equal(formatDuration(850), "850 ms");
  assert.equal(formatDuration(12e3), "12 s");
  assert.equal(formatDuration(184e3), "3 min 4 s");
  assert.equal(formatDuration(2 * H + 5 * 60e3), "2 h 5 min");
  assert.equal(formatDuration(27 * H), "1 d 3 h");
});
