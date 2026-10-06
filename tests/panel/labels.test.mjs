// Suggest mode labels: CRUD, overlap rules, brush math, hit testing, serialization. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_TOLERANCE,
  EditHistory,
  MAX_LABELS,
  brushPeriod,
  checkToleranceMinutes,
  labelHit,
  labelText,
  labelsFromScenario,
  labelsInWindow,
  labelsPayload,
  makeScale,
  moveLabel,
  normalizeLabel,
  placeLabel,
  readLabelDefault,
  removeLabel,
  resizeLabel,
  scenarioFingerprint,
  scenarioPayload,
  suggestAvailable,
  suggestMessage,
  suggestOptions,
  suggestOutcome,
  toleranceFromOptions,
  writeLabelDefault,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const M = 60e3;
const H = 3600e3;
const WIN = { start: T0, end: T0 + 24 * H };
const utc = () => 0;

const fire = (start, end, anchor = "start", id = undefined) => ({ id, start: T0 + start, end: T0 + end, kind: "fire", anchor });
const nofire = (start, end, id = undefined) => ({ id, start: T0 + start, end: T0 + end, kind: "no_fire", anchor: "start" });

function place(labels, label) {
  const r = placeLabel(labels, label);
  assert.ok(r.ok, r.error);
  return r;
}

test("placing a label gives it an id and keeps the list sorted by start", () => {
  const { labels: a } = place([], fire(5 * H, 6 * H));
  const { labels: b, label } = place(a, nofire(1 * H, 2 * H));
  assert.ok(label.id);
  assert.deepEqual(b.map((l) => l.kind), ["no_fire", "fire"]);
  assert.notEqual(a, b);
  assert.equal(a.length, 1, "the input list is not mutated");
});

test("same-kind overlaps merge, keeping the placed label's id and anchor", () => {
  let { labels } = place([], fire(1 * H, 2 * H, "start", "a"));
  ({ labels } = place(labels, fire(3 * H, 4 * H, "start", "b")));
  // Bridges both: everything merges into the placed one.
  const r = place(labels, fire(1.5 * H, 3.5 * H, "both", "c"));
  assert.equal(r.labels.length, 1);
  assert.deepEqual(r.labels[0], { id: "c", start: T0 + 1 * H, end: T0 + 4 * H, kind: "fire", anchor: "both" });
  assert.deepEqual(r.merged.sort(), ["a", "b"]);
});

test("touching periods do not merge: periods are half-open", () => {
  const { labels } = place([], nofire(1 * H, 2 * H));
  assert.equal(place(labels, nofire(2 * H, 3 * H)).labels.length, 2);
  assert.equal(place(labels, fire(2 * H, 3 * H)).labels.length, 2);
});

test("a should-fire period may not overlap a must-not-fire one", () => {
  const { labels } = place([], nofire(1 * H, 2 * H, "n"));
  const r = placeLabel(labels, fire(90 * M, 3 * H));
  assert.equal(r.ok, false);
  assert.match(r.error, /must-not-fire/);
  const back = placeLabel(place([], fire(0, H)).labels, nofire(30 * M, 2 * H));
  assert.equal(back.ok, false);
  assert.match(back.error, /should-fire/);
});

test("a merge that would grow into the other kind is refused", () => {
  let { labels } = place([], fire(0, 2 * H, "start", "f"));
  ({ labels } = place(labels, nofire(2 * H, 3 * H, "n")));
  // Overlaps f only, but merged with f it still ends at 2 h, so this is fine...
  assert.equal(place(labels, fire(H, 2 * H)).labels.length, 2);
  // ...while extending past 2 h reaches n.
  assert.equal(placeLabel(labels, fire(H, 150 * M)).ok, false);
});

test("editing a label in place replaces it; it does not merge with its old self", () => {
  const { labels, label } = place([], fire(H, 2 * H, "start", "x"));
  const r = place(labels, { ...label, start: T0 + 30 * M, anchor: "end" });
  assert.deepEqual(r.labels, [{ id: "x", start: T0 + 30 * M, end: T0 + 2 * H, kind: "fire", anchor: "end" }]);
  assert.deepEqual(r.merged, []);
});

test("changing a label's kind is checked against the other kind", () => {
  let { labels } = place([], fire(0, H, "start", "a"));
  ({ labels } = place(labels, nofire(2 * H, 3 * H, "b")));
  const r = place(labels, { ...labels[0], kind: "no_fire" });
  assert.equal(r.labels.length, 2);
  assert.equal(placeLabel(labels, { ...labels[0], kind: "no_fire", end: T0 + 150 * M }).ok, true, "merges with b");
});

test("placing refuses an empty or reversed period and caps the count", () => {
  assert.equal(placeLabel([], fire(H, H)).ok, false);
  assert.equal(placeLabel([], fire(2 * H, H)).ok, false);
  const many = Array.from({ length: MAX_LABELS }, (_, i) => ({ id: `m${i}`, start: T0 + i * 2 * M, end: T0 + i * 2 * M + M, kind: "fire", anchor: "start" }));
  assert.equal(placeLabel(many, fire(23 * H, 24 * H)).ok, false);
});

test("removeLabel drops one by id", () => {
  const { labels } = place(place([], fire(0, H, "start", "a")).labels, nofire(2 * H, 3 * H, "b"));
  assert.deepEqual(removeLabel(labels, "a").map((l) => l.id), ["b"]);
});

test("moving keeps the length and stays inside the window", () => {
  const l = fire(H, 2 * H, "start", "a");
  assert.deepEqual([moveLabel(l, 30 * M, WIN).start - T0, moveLabel(l, 30 * M, WIN).end - T0], [90 * M, 150 * M]);
  const early = moveLabel(l, -5 * H, WIN);
  assert.deepEqual([early.start, early.end], [T0, T0 + H]);
  const late = moveLabel(l, 30 * H, WIN);
  assert.deepEqual([late.start, late.end], [WIN.end - H, WIN.end]);
});

test("resizing an edge never crosses the other edge or the window", () => {
  const l = fire(H, 2 * H, "start", "a");
  assert.equal(resizeLabel(l, "start", T0 + 30 * M, WIN).start, T0 + 30 * M);
  assert.equal(resizeLabel(l, "start", T0 + 3 * H, WIN).start, T0 + 2 * H - 1e3);
  assert.equal(resizeLabel(l, "start", T0 - H, WIN).start, T0);
  assert.equal(resizeLabel(l, "end", T0, WIN, M).end, T0 + H + M);
  assert.equal(resizeLabel(l, "end", T0 + 30 * H, WIN).end, WIN.end);
});

test("a brush snaps both ends, orders them, and clamps to the window", () => {
  const opts = { ...WIN, step: 5 * M, offsetMinutes: utc };
  assert.deepEqual(brushPeriod(T0 + 62 * M, T0 + 9 * M, opts), { start: T0 + 10 * M, end: T0 + 60 * M });
  assert.deepEqual(brushPeriod(T0 - H, T0 + 61 * M, opts), { start: T0, end: T0 + 60 * M });
  assert.deepEqual(brushPeriod(WIN.end - 7 * M, WIN.end + H, opts), { start: WIN.end - 5 * M, end: WIN.end });
});

test("a brush shorter than a snap step is a click, not a period", () => {
  assert.equal(brushPeriod(T0 + 10 * M, T0 + 11 * M, { ...WIN, step: 5 * M, offsetMinutes: utc }), null);
  assert.equal(brushPeriod(T0 + 10 * M, T0 + 10 * M + 500, { ...WIN }), null);
  assert.deepEqual(brushPeriod(T0 + 10 * M, T0 + 10 * M + 2e3, { ...WIN }), { start: T0 + 10 * M, end: T0 + 10 * M + 2e3 });
});

test("hit testing finds edges before the body, and only the selected label's edges by touch", () => {
  const scale = makeScale(T0, T0 + 10 * H, 1000); // 100 px per hour
  const labels = [fire(H, 3 * H, "start", "a"), nofire(5 * H, 6 * H, "b")];
  assert.deepEqual(labelHit(labels, 102, scale), { id: "a", part: "start" });
  assert.deepEqual(labelHit(labels, 296, scale), { id: "a", part: "end" });
  assert.deepEqual(labelHit(labels, 200, scale), { id: "a", part: "body" });
  assert.equal(labelHit(labels, 400, scale), null);
  assert.deepEqual(labelHit(labels, 102, scale, { touch: true, reach: 14 }), { id: "a", part: "body" });
  assert.deepEqual(labelHit(labels, 102, scale, { touch: true, reach: 14, selected: "a" }), { id: "a", part: "start" });
  assert.deepEqual(labelHit(labels, 498, scale, { touch: true, reach: 14, selected: "a" }), null, "outside b by touch: nothing");
});

test("hit testing prefers the edge the pointer is inside of when two labels touch", () => {
  const scale = makeScale(T0, T0 + 10 * H, 1000);
  const labels = [fire(H, 2 * H, "start", "a"), nofire(2 * H, 3 * H, "b")];
  assert.deepEqual(labelHit(labels, 201, scale), { id: "b", part: "start" });
  assert.deepEqual(labelHit(labels, 199, scale), { id: "a", part: "end" });
});

test("labels serialize sorted, as ISO, with an anchor only on should-fire periods", () => {
  const out = labelsPayload([fire(3 * H, 4 * H, "both", "b"), nofire(H, 2 * H, "a")]);
  assert.deepEqual(out, [
    { start: "2026-10-04T01:00:00.000Z", end: "2026-10-04T02:00:00.000Z", kind: "no_fire" },
    { start: "2026-10-04T03:00:00.000Z", end: "2026-10-04T04:00:00.000Z", kind: "fire", anchor: "both" },
  ]);
});

test("saved labels load back, skipping malformed ones", () => {
  const saved = labelsPayload([fire(3 * H, 4 * H, "while"), nofire(H, 2 * H)]);
  const loaded = labelsFromScenario([...saved, { start: "x", end: "y", kind: "fire" }, { ...saved[0], kind: "maybe" }, null]);
  assert.deepEqual(labelsPayload(loaded), saved);
  assert.ok(loaded.every((l) => l.id));
  assert.deepEqual(labelsFromScenario(undefined), []);
  assert.equal(normalizeLabel({ start: T0, end: T0 + H, kind: "fire", anchor: "middle" }).anchor, "start");
});

test("labelText names the kind and anchor", () => {
  assert.equal(labelText(fire(0, H, "both")), "Should fire at start and end");
  assert.equal(labelText(fire(0, H, "while")), "Should fire while");
  assert.equal(labelText(nofire(0, H)), "Must not fire");
});

test("the suggest request sends the labels inside the window and the tolerance in seconds", () => {
  const labels = [fire(-2 * H, -H), fire(H, 2 * H, "end"), nofire(23 * H, 25 * H)];
  const msg = suggestMessage({ entityIds: ["sensor.a"], ...WIN, labels, tolerance: DEFAULT_TOLERANCE });
  assert.equal(msg.type, "event_simulator/suggest/subscribe");
  assert.deepEqual(msg.entity_ids, ["sensor.a"]);
  assert.equal(msg.start, "2026-10-04T00:00:00.000Z");
  assert.deepEqual(msg.labels.map((l) => l.kind), ["fire", "no_fire"]);
  assert.deepEqual(msg.options, { tolerance_before: 120, tolerance_after: 600 });
  assert.equal(labelsInWindow(labels, WIN.start, WIN.end).length, 2);
});

test("tolerance converts between options and ms, and is checked as typed", () => {
  assert.deepEqual(toleranceFromOptions(suggestOptions({ before: 30e3, after: 5 * M })), { before: 30e3, after: 5 * M });
  assert.deepEqual(toleranceFromOptions(undefined), DEFAULT_TOLERANCE);
  assert.deepEqual(toleranceFromOptions({ tolerance_before: -1, tolerance_after: null }), DEFAULT_TOLERANCE);
  assert.deepEqual(checkToleranceMinutes("2.5"), { ok: true, value: 150e3 });
  assert.equal(checkToleranceMinutes("0").ok, true);
  for (const bad of ["", "-1", "soon", "100000"]) assert.equal(checkToleranceMinutes(bad).ok, false, bad);
});

test("the default kind for a new period is remembered, and survives broken storage", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.deepEqual(readLabelDefault(storage), { kind: "fire", anchor: "start" });
  writeLabelDefault({ kind: "no_fire", anchor: "end" }, storage);
  assert.deepEqual(readLabelDefault(storage), { kind: "no_fire", anchor: "end" });
  store.set("event_simulator.label_default", "{not json");
  assert.deepEqual(readLabelDefault(storage), { kind: "fire", anchor: "start" });
  const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.deepEqual(readLabelDefault(broken), { kind: "fire", anchor: "start" });
  writeLabelDefault({ kind: "fire", anchor: "both" }, broken);
});

test("suggest availability comes from how the backend refuses an empty request", () => {
  assert.equal(suggestAvailable({ code: "unknown_command" }), false);
  assert.equal(suggestAvailable({ code: "invalid_format" }), true);
  assert.equal(suggestAvailable(undefined), true);
});

test("suggest events read as progress, a result, unavailable, or an error", () => {
  assert.deepEqual(suggestOutcome({ error: { code: "unknown_command", message: "Unknown command." } }), { kind: "unavailable" });
  assert.deepEqual(suggestOutcome({ stage: "mining" }), { kind: "progress", text: "Mining…" });
  assert.deepEqual(suggestOutcome({ stage: "validating", done: 2, total: 5 }), { kind: "progress", text: "Validating 2/5…" });
  assert.deepEqual(suggestOutcome({ result: { suggestions: [{ config: {} }], hints: [1] } }), { kind: "done", suggestions: [{ config: {} }], hints: [1] });
  assert.deepEqual(suggestOutcome({ suggestions: [] }), { kind: "done", suggestions: [], hints: [] });
  const err = suggestOutcome({ error: { code: "timeout", message: "slow" } });
  assert.equal(err.kind, "error");
  assert.equal(err.error.code, "timeout");
});

test("a scenario saves its labels and Suggest options only when there are some", () => {
  const base = { name: "n", entityId: "", automation: {}, ...WIN, tracks: [] };
  const plain = scenarioPayload(base);
  assert.equal("labels" in plain, false);
  assert.equal("suggest_options" in plain, false);
  const labels = [fire(H, 2 * H, "both", "a")];
  const full = scenarioPayload({ ...base, labels, suggestOptions: suggestOptions(DEFAULT_TOLERANCE) });
  assert.deepEqual(full.labels, labelsPayload(labels));
  assert.deepEqual(full.suggest_options, { tolerance_before: 120, tolerance_after: 600 });
  // A label edit is an unsaved change.
  assert.notEqual(scenarioFingerprint(full), scenarioFingerprint(scenarioPayload({ ...base, labels: [fire(H, 3 * H, "both", "a")], suggestOptions: suggestOptions() })));
});

test("label edits undo through the same history as timeline edits", () => {
  const undo = new EditHistory(5);
  const before = place([], fire(H, 2 * H, "start", "a")).labels;
  undo.push({ label: "add period", snapshots: [], labels: [] });
  undo.push({ label: "move period", snapshots: [], labels: before });
  assert.equal(undo.pop().labels, before);
  assert.deepEqual(undo.pop().labels, []);
});
