// Panning with the scroll bar, and shifting a whole track in time. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EditHistory,
  adoptRecorded,
  buildScenario,
  formatOffset,
  pageView,
  parkedCounts,
  restoreShift,
  scenarioFingerprint,
  scenarioPayload,
  scrollThumb,
  setInitial,
  shiftEvent,
  shiftTrack,
  snapDelta,
  timelineEdited,
  trackOffset,
  tracksFromScenario,
  unpackEvents,
  updateEvent,
  viewFromThumb,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const M = 60e3;
const H = 3600e3;
const iso = (ms) => new Date(ms).toISOString();
const WIN = { start: T0, end: T0 + 4 * H };

const event = (t, state) => ({ at: iso(t), entity_id: "binary_sensor.door", state, attributes: {}, last_changed: iso(t), last_updated: iso(t) });
const recorded = () => ({
  initial: { state: "off", attributes: { friendly_name: "Door" }, last_changed: iso(T0 - 5 * H), last_updated: iso(T0 - H) },
  events: [event(T0 + H, "on"), event(T0 + 2 * H, "off"), event(T0 + 3 * H, "on")],
});
const times = (tl) => tl.events.map((e) => (Date.parse(e.at) - T0) / M);

// ---- scroll bar ------------------------------------------------------------

// Times in minutes, so spans stay above clampView's 10 s minimum.
const mins = (pair) => pair.map((t) => t * M);

test("the thumb is the visible share of the window, placed by the view start", () => {
  const bounds = mins([0, 1000]);
  assert.deepEqual(scrollThumb(mins([0, 1000]), bounds, 500), { left: 0, width: 500 });
  assert.deepEqual(scrollThumb(mins([0, 250]), bounds, 500), { left: 0, width: 125 });
  assert.deepEqual(scrollThumb(mins([750, 1000]), bounds, 500), { left: 375, width: 125 });
  assert.deepEqual(scrollThumb(mins([375, 625]), bounds, 500), { left: 187.5, width: 125 });
});

test("a thumb never gets thinner than the minimum, and still reaches both ends", () => {
  const bounds = mins([0, 1e6]);
  const atStart = scrollThumb(mins([0, 10]), bounds, 400, 24);
  const atEnd = scrollThumb(mins([1e6 - 10, 1e6]), bounds, 400, 24);
  assert.equal(atStart.width, 24);
  assert.equal(atStart.left, 0);
  assert.equal(atEnd.left + atEnd.width, 400);
});

test("dragging the thumb moves the view and keeps the zoom", () => {
  const bounds = mins([0, 1000]);
  const view = mins([0, 250]);
  const { width } = scrollThumb(view, bounds, 500);
  assert.deepEqual(viewFromThumb(187.5, view, bounds, 500, width), mins([375, 625]));
  // Past either end the view stops at the window edge.
  assert.deepEqual(viewFromThumb(-50, view, bounds, 500, width), mins([0, 250]));
  assert.deepEqual(viewFromThumb(9999, view, bounds, 500, width), mins([750, 1000]));
  // Round trip with a minimum-width thumb.
  const tiny = mins([400_000, 400_010]);
  const thumb = scrollThumb(tiny, mins([0, 1e6]), 400, 24);
  const back = viewFromThumb(thumb.left, tiny, mins([0, 1e6]), 400, thumb.width);
  assert.ok(Math.abs(back[0] - tiny[0]) < 1 && Math.abs(back[1] - back[0] - 10 * M) < 1);
});

test("the whole window visible: the thumb fills the bar and dragging does nothing", () => {
  assert.deepEqual(viewFromThumb(100, mins([0, 1000]), mins([0, 1000]), 500, 500), mins([0, 1000]));
});

test("a click beside the thumb pages toward it, clamped to the window", () => {
  assert.deepEqual(pageView(mins([100, 200]), 1, mins([0, 1000])), mins([190, 290]));
  assert.deepEqual(pageView(mins([100, 200]), -1, mins([0, 1000])), mins([10, 110]));
  assert.deepEqual(pageView(mins([850, 950]), 1, mins([0, 1000])), mins([900, 1000]));
});

// ---- track shift -----------------------------------------------------------

test("a shift moves every event and the base state's timestamps together", () => {
  const tl = recorded();
  const out = shiftTrack(tl, 20 * M, WIN);
  assert.deepEqual(times(out), [80, 140, 200]);
  assert.ok(out.events.every((e) => e.edited && !e.last_changed && !e.last_updated));
  assert.equal(trackOffset(out), 20 * M);
  assert.equal(out.initial.state, "off");
  assert.equal(out.initial.last_changed, iso(T0 - 5 * H + 20 * M));
  assert.ok(out.initial.edited);
  // The input is untouched, and a zero shift is no edit at all.
  assert.deepEqual(times(tl), [60, 120, 180]);
  assert.equal(shiftTrack(tl, 0, WIN), tl);
});

test("shifts add up, and shifting back hands back the recorded items", () => {
  const tl = recorded();
  const once = shiftTrack(tl, 20 * M, WIN);
  const twice = shiftTrack(once, 15 * M, WIN);
  assert.equal(trackOffset(twice), 35 * M);
  assert.deepEqual(times(twice), [95, 155, 215]);
  const back = shiftTrack(twice, -35 * M, WIN);
  assert.equal(back.shift, undefined);
  assert.equal(back.initial, tl.initial);
  back.events.forEach((ev, i) => assert.equal(ev, tl.events[i]));
  assert.equal(timelineEdited(back, tl), false);
  assert.equal(timelineEdited(once, tl), true);
});

test("events shifted past the end are kept and come back", () => {
  const tl = recorded();
  const out = shiftTrack(tl, 90 * M, WIN); // 3 h -> 4 h 30: outside [0, 4 h)
  assert.deepEqual(times(out), [150, 210]);
  assert.deepEqual(parkedCounts(out), [0, 1]);
  assert.equal(buildScenario({ automation: {}, start: WIN.start, end: WIN.end, tracks: [{ entity_id: "binary_sensor.door", ...out }] }).events.length, 2);
  const back = shiftTrack(out, -90 * M, WIN);
  assert.deepEqual(times(back), [60, 120, 180]);
  assert.deepEqual(parkedCounts(back), [0, 0]);
});

test("events shifted before the start become the initial state, and back again", () => {
  const tl = recorded();
  const out = shiftTrack(tl, -150 * M, WIN); // 1 h, 2 h -> before the start; 3 h -> 30 min
  assert.deepEqual(times(out), [30]);
  assert.deepEqual(parkedCounts(out), [2, 0]);
  // In effect at the start: the last parked event ("off", from 2 h - 150 min).
  assert.equal(out.initial.state, "off");
  assert.equal(out.initial.last_changed, iso(T0 - 30 * M));
  const scenario = buildScenario({ automation: {}, start: WIN.start, end: WIN.end, tracks: [{ entity_id: "binary_sensor.door", ...out }] });
  assert.equal(scenario.initial_states["binary_sensor.door"].state, "off");
  assert.equal(scenario.initial_states["binary_sensor.door"].edited, undefined);
  assert.equal(scenario.events.length, 1);
  const back = shiftTrack(out, 150 * M, WIN);
  assert.equal(back.initial, tl.initial);
  assert.equal(timelineEdited(back, tl), false);
});

test("a run of equal parked states dates the initial state from its first", () => {
  const tl = {
    initial: { state: "off", attributes: {} },
    events: [event(T0 + 10 * M, "on"), event(T0 + 20 * M, "on"), event(T0 + 3 * H, "off")],
  };
  const out = shiftTrack(tl, -30 * M, WIN);
  assert.equal(out.initial.state, "on");
  assert.equal(out.initial.last_changed, iso(T0 - 20 * M));
  assert.equal(out.initial.last_updated, iso(T0 - 10 * M));
});

test("a base state shifted past the window start is clamped to it", () => {
  const tl = { initial: { state: "off", attributes: {}, last_changed: iso(T0 - 10 * M), last_updated: iso(T0 - 10 * M) }, events: [] };
  const out = shiftTrack(tl, 30 * M, WIN);
  assert.equal(out.initial.last_changed, iso(T0));
  // The unclamped time is kept, so shifting back restores it.
  assert.equal(shiftTrack(out, -30 * M, WIN).initial, tl.initial);
  assert.equal(shiftTrack(out, -25 * M, WIN).initial.last_changed, iso(T0 - 5 * M));
});

test("a track without an initial state shifts its events alone", () => {
  const tl = { initial: null, events: [event(T0 + H, "on")] };
  const out = shiftTrack(tl, 5 * M, WIN);
  assert.equal(out.initial, null);
  assert.deepEqual(times(out), [65]);
});

test("single-event edits after a shift keep the shift, and shifting again moves them too", () => {
  const tl = recorded();
  const shifted = shiftTrack(tl, 10 * M, WIN);
  const moved = shiftEvent(shifted, 1, T0 + 2 * H + 30 * M, WIN).timeline;
  assert.equal(trackOffset(moved), 10 * M);
  const edited = updateEvent(moved, 0, { state: "open" });
  const again = shiftTrack(edited, -10 * M, WIN);
  assert.deepEqual(times(again), [60, 140, 180]);
  assert.equal(again.events[0].state, "open");
  assert.equal(again.shift, undefined);
  assert.equal(timelineEdited(again, tl), true);
});

test("an initial state edited while events are parked before the start survives the next shift", () => {
  const tl = recorded();
  const out = shiftTrack(tl, -150 * M, WIN);
  const edited = setInitial(out, { state: "jammed", attributes: {} }, WIN.start);
  const next = shiftTrack(edited, 10 * M, WIN);
  assert.equal(next.initial.state, "jammed");
  // Shifting the parked events back in brings the edit with them.
  const back = shiftTrack(next, 140 * M, WIN);
  assert.equal(back.events[1].state, "jammed");
  assert.equal(back.initial.state, "off");
});

test("an initial state edited with nothing parked becomes the new base", () => {
  const tl = recorded();
  const out = shiftTrack(tl, 10 * M, WIN);
  const edited = setInitial(out, { state: "on", attributes: {} }, WIN.start);
  assert.equal(shiftTrack(edited, 5 * M, WIN).initial.state, "on");
});

test("an undo snapshot of a shift is the previous timeline, shift included", () => {
  const tl = recorded();
  const history = new EditHistory(5);
  const shifted = shiftTrack(tl, 90 * M, WIN);
  history.push({ label: "shift track", snapshots: [{ entity_id: "binary_sensor.door", timeline: { initial: tl.initial, events: tl.events, shift: tl.shift } }] });
  const twice = shiftTrack(shifted, 30 * M, WIN);
  history.push({ label: "shift track", snapshots: [{ entity_id: "binary_sensor.door", timeline: { initial: shifted.initial, events: shifted.events, shift: shifted.shift } }] });
  assert.deepEqual(parkedCounts(twice), [0, 2]);
  const prev = history.pop().snapshots[0].timeline;
  assert.deepEqual(parkedCounts(prev), [0, 1]);
  assert.equal(trackOffset(prev), 90 * M);
  assert.equal(history.pop().snapshots[0].timeline.shift, undefined);
});

test("offsets read with a sign and snap to whole steps", () => {
  assert.equal(formatOffset(200e3), "+3 min 20 s");
  assert.equal(formatOffset(-H), "−1 h");
  assert.equal(formatOffset(0), "0 s");
  assert.equal(snapDelta(204e3, 15e3), 210e3);
  assert.equal(snapDelta(-37e3, 15e3), -30e3);
  assert.equal(snapDelta(1234.4, 0), 1234);
});

// ---- saved scenarios -------------------------------------------------------

test("a saved shifted track keeps its parked events, offset and base, and loads back the same", () => {
  const tl = recorded();
  const shifted = shiftTrack(shiftTrack(tl, -150 * M, WIN), 0, WIN);
  const tracks = [{ entity_id: "binary_sensor.door", ...shifted }];
  const payload = scenarioPayload({ name: "x", entityId: "automation.a", automation: {}, start: WIN.start, end: WIN.end, tracks });
  assert.equal(payload.events.length, 3);
  assert.deepEqual(payload.track_shifts, { "binary_sensor.door": { offset: -150 * M, base: shifted.shift.base } });
  const [loaded] = tracksFromScenario(JSON.parse(JSON.stringify(payload)));
  assert.deepEqual(times(loaded), [30]);
  assert.deepEqual(parkedCounts(loaded), [2, 0]);
  assert.equal(trackOffset(loaded), -150 * M);
  assert.equal(loaded.initial.state, "off");
  // Same fingerprint, so a loaded scenario is not dirty.
  const again = scenarioPayload({ name: "x", entityId: "automation.a", automation: {}, start: WIN.start, end: WIN.end, tracks: [loaded] });
  assert.equal(scenarioFingerprint(again), scenarioFingerprint(payload));
  // Shifting it back after the load brings the events in, back where they were recorded.
  const back = shiftTrack(loaded, 150 * M, WIN);
  assert.deepEqual(times(back), [60, 120, 180]);
  assert.equal(back.initial.state, "off");
  assert.equal(back.initial.last_changed, tl.initial.last_changed);
  assert.equal(back.shift, undefined);
});

test("a scenario without shifts saves no track_shifts and loads as before", () => {
  const tl = recorded();
  const payload = scenarioPayload({ name: "x", entityId: null, automation: {}, start: WIN.start, end: WIN.end, tracks: [{ entity_id: "binary_sensor.door", ...tl }] });
  assert.equal("track_shifts" in payload, false);
  const [loaded] = tracksFromScenario(payload);
  assert.equal(loaded.shift, undefined);
  assert.equal(loaded.events.length, 3);
});

test("loading adopts recorded items and keeps the shift", () => {
  const tl = recorded();
  const loaded = restoreShift({ initial: tl.initial, events: [tl.events[0]] }, { offset: 60 * M, base: tl.initial }, WIN);
  const adopted = adoptRecorded(loaded, tl);
  assert.equal(adopted.shift, loaded.shift);
  assert.equal(adopted.events[0], tl.events[0]);
});

test("restoreShift leaves an unshifted timeline alone and parks out-of-window events", () => {
  const tl = recorded();
  assert.equal(restoreShift(tl, undefined, WIN), tl);
  const late = { initial: null, events: [...tl.events, event(T0 + 5 * H, "off")] };
  const out = restoreShift(late, undefined, WIN);
  assert.deepEqual(parkedCounts(out), [0, 1]);
  assert.equal(trackOffset(out), 0);
  assert.equal(unpackEvents(tl.events).length, 3);
});
