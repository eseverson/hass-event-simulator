// Device lanes and event-trigger lanes (#48): which recorded bus events each lane holds, how its marks read, new presses, and what a replay sends. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildLanes,
  buildScenario,
  eventTriggerFilters,
  insertBusEvent,
  kindIndex,
  laneTypes,
  laneView,
  matchesData,
  newDeviceEvent,
  shiftLane,
  timelineEdited,
  updateBusEvent,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const IEEE = "a4:c1:38:ad:af:88:68:87";
const SHORT = "remote_button_short_press";
const DOUBLE = "remote_button_double_press";
const LONG = "remote_button_long_press";
const start = Date.parse("2026-10-16T00:50:00Z");
const end = Date.parse("2026-10-16T01:30:00Z");
const win = { start, end };

const kind = (type, used) => ({ type, subtype: "button_1", event_type: "zha_event", match: { device_ieee: IEEE, endpoint_id: 1, command: type }, used });
const BUTTON = {
  id: "device:dev1",
  domain: "zha",
  device_id: "dev1",
  name: "Outdoor String Lights Button",
  event_type: "zha_event",
  kinds: [kind(SHORT, true), kind(DOUBLE, false), kind(LONG, false)],
};
const zha = (at, command, ieee = IEEE) => ({
  at,
  event_type: "zha_event",
  data: { device_ieee: ieee, device_id: "dev1", unique_id: `${ieee}:1:0x0006`, endpoint_id: 1, cluster_id: 6, command, args: [], params: {} },
});
// The user's presses of 2026-10-15 (local), plus what else the button and another device sent.
const PRESSES = [
  zha("2026-10-16T00:57:51.566678+00:00", SHORT),
  zha("2026-10-16T00:57:57.693908+00:00", SHORT),
  zha("2026-10-16T00:58:03.961630+00:00", SHORT),
  zha("2026-10-16T00:58:07.055341+00:00", DOUBLE),
  zha("2026-10-16T00:58:10.343343+00:00", DOUBLE),
  zha("2026-10-16T00:58:13.465173+00:00", DOUBLE),
  zha("2026-10-16T01:23:22.709938+00:00", SHORT),
  zha("2026-10-16T01:23:36.455500+00:00", SHORT),
];
const NOISE = [
  zha("2026-10-16T00:57:51.566733+00:00", "press_type"),
  zha("2026-10-16T01:11:40+00:00", "attribute_updated"),
  zha("2026-10-16T01:15:00+00:00", "on", "00:11:22:33:44:55:66:77"),
];
const EVENTS = [...PRESSES, ...NOISE].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
const lanesOf = (deviceLanes, filters = new Map(), busEvents = EVENTS) => buildLanes({ deviceLanes, filters, busEvents, start, end });

test("a device lane holds only its kinds' events", () => {
  const [lane] = lanesOf([BUTTON]);
  assert.equal(lane.kind, "device");
  assert.deepEqual(lane.events, PRESSES);
  assert.deepEqual(lane.recorded, { initial: null, events: lane.events });
  assert.equal(timelineEdited(lane, lane.recorded), false);
});

test("used kinds are ringed, the others dimmed", () => {
  const [lane] = lanesOf([BUTTON]);
  const view = laneView(lane, { dropped: [] });
  assert.deepEqual(view.marks.map((m) => m.used), [true, true, true, false, false, false, true, true]);
  assert.deepEqual(view.marks.map((m) => m.kind), [0, 0, 0, 1, 1, 1, 0, 0]);
  assert.equal(view.notice, null);
});

test("two used kinds on one device lane", () => {
  const both = { ...BUTTON, kinds: [kind(SHORT, true), kind(DOUBLE, true), kind(LONG, false)] };
  const [lane] = lanesOf([both]);
  assert.ok(laneView(lane, { dropped: [] }).marks.every((m) => m.used));
});

test("an empty window still gives the device lane", () => {
  const lanes = lanesOf([BUTTON], new Map(), []);
  assert.equal(lanes.length, 1);
  assert.deepEqual(lanes[0].events, []);
});

test("a dropped type shows a notice", () => {
  const [lane] = lanesOf([BUTTON], new Map(), []);
  assert.equal(laneView(lane, { dropped: ["zha_event"] }).notice, "zha_event: too many events in this window to load; shorten the window");
});

test("a plain zha_event lane gets only what no device lane took", () => {
  const lanes = lanesOf([BUTTON], new Map([["zha_event", [{ command: "x" }]]]));
  assert.deepEqual(lanes.map((l) => [l.id, l.kind, l.events.length]), [["device:dev1", "device", 8], ["event:zha_event", "event", 3]]);
  assert.deepEqual(laneView(lanes[1], { dropped: [] }).marks.map((m) => m.used), [false, false, false]);
});

test("an event lane rings what its trigger's event_data matches", () => {
  const [lane] = lanesOf([], new Map([["zha_event", [{ command: DOUBLE }]]]));
  assert.equal(lane.events.length, 11);
  assert.equal(laneView(lane, { dropped: [] }).marks.filter((m) => m.used).length, 3);
});

test("a trigger without event_data, or with templated data, dims nothing", () => {
  const [lane] = lanesOf([], new Map([["zha_event", null]]));
  assert.ok(laneView(lane, { dropped: [] }).marks.every((m) => m.used));
});

test("eventTriggerFilters: per type, null when any trigger of it cannot be evaluated", () => {
  const filters = eventTriggerFilters({
    triggers: [
      { trigger: "event", event_type: "zha_event", event_data: { command: "on" } },
      { platform: "event", event_type: ["tag_scanned", "zha_event"], event_data: { tag_id: "front" } },
      { trigger: "event", event_type: "x", event_data: { v: "{{ states('a.b') }}" } },
      { trigger: "event", event_type: "y" },
      { trigger: "state", entity_id: "a.b" },
      { trigger: "event", event_type: "{{ 'z' }}" },
    ],
  });
  assert.deepEqual([...filters.keys()].sort(), ["tag_scanned", "x", "y", "zha_event"]);
  assert.deepEqual(filters.get("zha_event"), [{ command: "on" }, { tag_id: "front" }]);
  assert.equal(filters.get("x"), null);
  assert.equal(filters.get("y"), null);
});

test("matchesData: subset, nested subset, equal values only", () => {
  assert.ok(matchesData({ a: 1, b: { c: 2, d: 3 } }, { b: { c: 2 } }));
  assert.ok(matchesData({ a: [1, 2] }, { a: [1, 2] }));
  assert.ok(!matchesData({ a: 1 }, { a: "1" }));
  assert.ok(!matchesData({ a: 1 }, { b: 1 }));
  assert.ok(matchesData({ a: 1 }, {}));
});

test("kindIndex finds the kind a press is", () => {
  assert.equal(kindIndex(BUTTON.kinds, PRESSES[3]), 1);
  assert.equal(kindIndex(BUTTON.kinds, NOISE[1]), -1);
});

test("a new press copies the nearest recorded press of its kind", () => {
  const [lane] = lanesOf([BUTTON]);
  const at = Date.parse("2026-10-16T01:20:00Z");
  const { event, copiedFrom } = newDeviceEvent(lane, 0, at);
  assert.equal(copiedFrom, "2026-10-16T01:23:22.709938+00:00");
  assert.equal(event.at, "2026-10-16T01:20:00.000Z");
  assert.equal(event.event_type, "zha_event");
  assert.equal(event.data.cluster_id, 6);
  assert.equal(event.data.command, SHORT);
  assert.equal(event.edited, true);
});

test("a kind never pressed gets its match only", () => {
  const [lane] = lanesOf([BUTTON]);
  const { event, copiedFrom } = newDeviceEvent(lane, 2, Date.parse("2026-10-16T01:00:00Z"));
  assert.equal(copiedFrom, null);
  assert.deepEqual(event.data, BUTTON.kinds[2].match);
});

test("insertBusEvent keeps the lane in time order; updateBusEvent marks the event", () => {
  const [lane] = lanesOf([BUTTON]);
  const { event } = newDeviceEvent(lane, 2, Date.parse("2026-10-16T01:00:00Z"));
  const { timeline, index } = insertBusEvent(lane, event, win);
  assert.equal(index, 6);
  assert.equal(timeline.events.length, 9);
  const updated = updateBusEvent(timeline, 0, { command: "x" });
  assert.deepEqual(updated.events[0].data, { command: "x" });
  assert.equal(updated.events[0].edited, true);
  assert.equal(lane.events.length, 8);
});

test("a lane shifted out of the window and back is recorded again", () => {
  const [lane] = lanesOf([BUTTON]);
  const out = shiftLane(lane, 3600e3, win);
  assert.equal(out.events.length, 0);
  assert.equal(out.initial, null);
  const back = shiftLane(out, -3600e3, win);
  assert.equal(back.initial, null);
  assert.equal(timelineEdited(back, lane.recorded), false);
  assert.equal(back.events[0], lane.events[0]);
});

test("buildScenario sends every lane's events, sorted, marks stripped", () => {
  const lanes = lanesOf([BUTTON], new Map([["zha_event", null]]));
  const { event } = newDeviceEvent(lanes[0], 2, Date.parse("2026-10-16T01:00:00Z"));
  lanes[0] = { ...lanes[0], ...insertBusEvent(lanes[0], event, win).timeline };
  const s = buildScenario({ automation: { triggers: [], actions: [] }, start, end, tracks: [], busEvents: [], lanes });
  assert.equal(s.bus_events.length, 12);
  assert.ok(s.bus_events.every((e, i, all) => i === 0 || Date.parse(all[i - 1].at) <= Date.parse(e.at)));
  assert.ok(s.bus_events.every((e) => !("edited" in e)));
});

test("buildScenario without lanes keeps sending the automation's event types", () => {
  const automation = { triggers: [{ trigger: "event", event_type: "zha_event" }], actions: [] };
  assert.equal(buildScenario({ automation, start, end, tracks: [], busEvents: EVENTS }).bus_events.length, 11);
});

test("laneTypes asks for device and plain types once", () => {
  assert.deepEqual(laneTypes([BUTTON], new Map([["tag_scanned", null], ["zha_event", null]])), ["tag_scanned", "zha_event"]);
  assert.deepEqual(laneTypes([], new Map()), []);
});
