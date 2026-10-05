// Timeline and config editing logic of the panel: `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EditHistory,
  buildScenario,
  checkAutomationConfig,
  countAtOrBefore,
  deleteEvent,
  eventIndexAt,
  insertEvent,
  moveEvent,
  parseAttributes,
  setInitial,
  shiftBounds,
  shiftEvent,
  snapStep,
  snapTime,
  stableStringify,
  stateAt,
  timelineEdited,
  updateEvent,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const M = 60e3;
const H = 3600e3;
const iso = (ms) => new Date(ms).toISOString();
const utc = () => 0;
const WIN = { start: T0, end: T0 + 4 * H };

const recorded = () => {
  const initial = { state: "off", attributes: { friendly_name: "Door" }, last_changed: iso(T0 - 5 * H), last_updated: iso(T0 - H) };
  const events = [
    { at: iso(T0 + H), entity_id: "binary_sensor.door", state: "on", attributes: {}, last_changed: iso(T0 + H), last_updated: iso(T0 + H) },
    { at: iso(T0 + 2 * H), entity_id: "binary_sensor.door", state: "off", attributes: {}, last_changed: iso(T0 + 2 * H), last_updated: iso(T0 + 2 * H) },
    { at: iso(T0 + 3 * H), entity_id: "binary_sensor.door", state: "on", attributes: {}, last_changed: iso(T0 + 3 * H), last_updated: iso(T0 + 3 * H) },
  ];
  return { initial, events };
};
const times = (tl) => tl.events.map((e) => Date.parse(e.at) - T0);

test("lookup finds the event in effect, or the initial state before the first", () => {
  const tl = recorded();
  assert.equal(countAtOrBefore(tl.events, T0), 0);
  assert.equal(countAtOrBefore(tl.events, T0 + H), 1);
  assert.equal(eventIndexAt(tl.events, T0 + 30 * M), -1);
  assert.equal(eventIndexAt(tl.events, T0 + H), 0);
  assert.equal(eventIndexAt(tl.events, T0 + 2.5 * H), 1);
  assert.equal(stateAt(tl, T0).state, "off");
  assert.equal(stateAt(tl, T0 + 3.5 * H).state, "on");
  assert.equal(stateAt({ initial: null, events: [] }, T0), null);
});

test("snap step grows with zoom and snapping follows local time", () => {
  assert.equal(snapStep(10), 1e3); // 10 ms per px: whole seconds
  assert.equal(snapStep(1000), 15e3); // 8 px = 8 s -> 15 s
  assert.equal(snapStep(144e3), 30 * M); // a day on 600 px
  assert.equal(snapStep(1e9), H);
  assert.equal(snapTime(T0 + 7 * M + 20e3, 5 * M, utc), T0 + 5 * M);
  assert.equal(snapTime(T0 + 8 * M, 5 * M, utc), T0 + 10 * M);
  // +05:30: quarter hours land on the local clock, not on UTC.
  const ist = () => 330;
  const snapped = snapTime(T0 + 7 * M, 15 * M, ist);
  assert.equal((snapped + 330 * M) % (15 * M), 0);
});

test("shift snaps, marks the event edited and drops its recorded stamps", () => {
  const tl = recorded();
  const r = shiftEvent(tl, 1, T0 + 2 * H + 7 * M, { ...WIN, step: 5 * M, offsetMinutes: utc });
  assert.equal(r.at, T0 + 2 * H + 5 * M);
  assert.deepEqual(times(r.timeline), [H, 2 * H + 5 * M, 3 * H]);
  const ev = r.timeline.events[1];
  assert.equal(ev.edited, true);
  assert.equal(ev.state, "off");
  assert.equal("last_changed" in ev, false);
  assert.equal("last_updated" in ev, false);
  // The input is untouched and the other events are the same objects.
  assert.deepEqual(times(tl), [H, 2 * H, 3 * H]);
  assert.equal(r.timeline.events[0], tl.events[0]);
  assert.equal(r.timeline.initial, tl.initial);
});

test("shift never passes a neighbor or leaves the window", () => {
  const tl = recorded();
  const past = shiftEvent(tl, 1, T0 + 3.5 * H, { ...WIN, step: 5 * M, offsetMinutes: utc });
  assert.equal(past.at, T0 + 3 * H - 1);
  assert.deepEqual(eventIndexAt(past.timeline.events, T0 + 3 * H - 1), 1);
  const before = shiftEvent(tl, 0, T0 - H, { ...WIN, step: M, offsetMinutes: utc });
  assert.equal(before.at, T0);
  const after = shiftEvent(tl, 2, T0 + 9 * H, { ...WIN, step: M, offsetMinutes: utc });
  assert.equal(after.at, WIN.end - 1);
  assert.deepEqual(shiftBounds(tl.events, 1, WIN.start, WIN.end), [T0 + H + 1, T0 + 3 * H - 1]);
});

test("a shift that lands where it started changes nothing", () => {
  const tl = recorded();
  const r = shiftEvent(tl, 0, T0 + H + 20e3, { ...WIN, step: 5 * M, offsetMinutes: utc });
  assert.equal(r.timeline, tl);
  assert.equal(r.at, T0 + H);
});

test("typed move re-sorts and rejects times outside the window", () => {
  const tl = recorded();
  const r = moveEvent(tl, 0, T0 + 3.5 * H, WIN);
  assert.equal(r.index, 2);
  assert.deepEqual(r.timeline.events.map((e) => e.state), ["off", "on", "on"]);
  assert.equal(r.timeline.events[2].edited, true);
  assert.throws(() => moveEvent(tl, 0, WIN.end, WIN), /inside the window/);
  assert.throws(() => moveEvent(tl, 0, NaN, WIN), /inside the window/);
  assert.equal(moveEvent(tl, 0, T0 + H, WIN).timeline, tl);
});

test("insert defaults to the state in effect and lands after same-time events", () => {
  const tl = recorded();
  const r = insertEvent(tl, "binary_sensor.door", T0 + 2.5 * H, WIN);
  assert.equal(r.index, 2);
  assert.deepEqual(r.timeline.events.map((e) => e.state), ["on", "off", "off", "on"]);
  assert.deepEqual(r.timeline.events[2], { at: iso(T0 + 2.5 * H), entity_id: "binary_sensor.door", state: "off", attributes: {}, edited: true });
  // Before the first event the initial state is in effect, attributes included.
  const first = insertEvent(tl, "binary_sensor.door", T0 + M, WIN);
  assert.equal(first.index, 0);
  assert.deepEqual(first.timeline.events[0].attributes, { friendly_name: "Door" });
  assert.notEqual(first.timeline.events[0].attributes, tl.initial.attributes);
  // At an existing event's time the new one follows it, so it is the one in effect.
  assert.equal(insertEvent(tl, "binary_sensor.door", T0 + H, WIN).index, 1);
  // Nothing known: unknown. Out of window: clamped.
  const empty = insertEvent({ initial: null, events: [] }, "sensor.t", T0 - H, WIN);
  assert.deepEqual([empty.index, empty.timeline.events[0].state, empty.timeline.events[0].at], [0, "unknown", iso(T0)]);
  assert.equal(insertEvent(tl, "x.y", T0 + 9 * H, WIN, { state: "5", attributes: {} }).timeline.events[3].at, iso(WIN.end - 1));
});

test("update and delete", () => {
  const tl = recorded();
  const u = updateEvent(tl, 1, { state: "unavailable" });
  assert.equal(u.events[1].state, "unavailable");
  assert.equal(u.events[1].at, tl.events[1].at);
  assert.equal(u.events[1].edited, true);
  assert.deepEqual(updateEvent(tl, 0, { attributes: { a: 1 } }).events[0].attributes, { a: 1 });
  assert.equal(updateEvent(tl, 0, { state: 21.5 }).events[0].state, "21.5");
  const d = deleteEvent(tl, 0);
  assert.deepEqual(times(d), [2 * H, 3 * H]);
  assert.equal(tl.events.length, 3);
  assert.throws(() => deleteEvent(tl, 9), /No such change/);
  assert.throws(() => updateEvent(tl, -1, { state: "x" }), /No such change/);
});

test("initial state edits keep the stamps consistent", () => {
  const tl = recorded();
  const changed = setInitial(tl, { state: "on", attributes: {} }, T0).initial;
  assert.deepEqual(changed, { state: "on", attributes: {}, edited: true });
  const attrsOnly = setInitial(tl, { state: "off", attributes: { x: 1 } }, T0).initial;
  assert.equal(attrsOnly.last_changed, tl.initial.last_changed);
  assert.equal(attrsOnly.last_updated, iso(T0));
  const fresh = setInitial({ initial: null, events: [] }, { state: "5" }, T0).initial;
  assert.deepEqual(fresh, { state: "5", attributes: {}, edited: true });
});

test("edited detection compares to the recorded timeline", () => {
  const tl = recorded();
  assert.equal(timelineEdited(tl, tl), false);
  assert.equal(timelineEdited({ ...tl }, tl), false);
  assert.equal(timelineEdited(updateEvent(tl, 0, { state: "x" }), tl), true);
  assert.equal(timelineEdited(deleteEvent(tl, 0), tl), true);
  assert.equal(timelineEdited(setInitial(tl, { state: "off" }, T0), tl), true);
  assert.equal(timelineEdited(tl, null), false);
});

test("scenario leaves out the edited marks", () => {
  const tl = setInitial(updateEvent(recorded(), 0, { state: "open" }), { state: "on" }, T0);
  const s = buildScenario({ automation: {}, start: WIN.start, end: WIN.end, tracks: [{ entity_id: "binary_sensor.door", ...tl }] });
  assert.equal(s.events[0].state, "open");
  assert.equal(s.events.some((e) => "edited" in e), false);
  assert.equal("edited" in s.initial_states["binary_sensor.door"], false);
});

test("undo history is bounded and last in, first out", () => {
  const h = new EditHistory(3);
  assert.equal(h.pop(), null);
  for (const label of ["a", "b", "c", "d"]) h.push({ label, snapshots: [] });
  assert.equal(h.size, 3);
  assert.equal(h.peek().label, "d");
  assert.deepEqual([h.pop().label, h.pop().label, h.pop().label, h.pop()], ["d", "c", "b", null]);
  h.push({ label: "x", snapshots: [] });
  h.clear();
  assert.equal(h.size, 0);
});

test("attributes parse as a JSON object", () => {
  assert.deepEqual(parseAttributes(""), { ok: true, value: {} });
  assert.deepEqual(parseAttributes(' {"a": 1} '), { ok: true, value: { a: 1 } });
  assert.equal(parseAttributes("[1]").ok, false);
  assert.equal(parseAttributes("{a: 1}").ok, false);
  assert.match(parseAttributes("{a: 1}").error, /not valid JSON/);
});

test("config check and stable comparison", () => {
  assert.equal(checkAutomationConfig({ triggers: [] }).ok, true);
  for (const bad of [null, [], "x", 3]) assert.equal(checkAutomationConfig(bad).ok, false);
  assert.equal(stableStringify({ b: 1, a: [{ d: 2, c: null }] }), stableStringify({ a: [{ c: null, d: 2 }], b: 1 }));
  assert.notEqual(stableStringify({ a: 1 }), stableStringify({ a: "1" }));
  assert.equal(stableStringify(undefined), "null");
});
