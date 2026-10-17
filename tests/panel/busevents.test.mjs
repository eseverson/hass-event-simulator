// Recorded bus events (#35): the Events lane, and the events a replay of an event-triggered automation fires. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  busEventLane,
  busEventsFor,
  busEventsMessage,
  buildScenario,
  deviceLanesMessage,
  eventTriggerTypes,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const H = 3600e3;
const iso = (ms) => new Date(T0 + ms).toISOString();
const EVENTS = [
  { at: iso(1 * H), event_type: "zha_event", data: { device_ieee: "aa", command: "on" } },
  { at: iso(2 * H), event_type: "tag_scanned", data: { tag_id: "front" } },
  { at: iso(3 * H), event_type: "zha_event", data: { device_ieee: "bb", command: "off" } },
  { at: iso(30 * H), event_type: "zha_event", data: { command: "late" } },
];

test("the bus_events request covers the window", () => {
  assert.deepEqual(busEventsMessage(T0, T0 + 24 * H), {
    type: "event_simulator/bus_events",
    start: "2026-10-04T00:00:00.000Z",
    end: "2026-10-05T00:00:00.000Z",
  });
});

test("the bus_events request can ask for only the types the lanes need (#48)", () => {
  assert.deepEqual(busEventsMessage(T0, T0 + 24 * H, ["zha_event"]), {
    type: "event_simulator/bus_events",
    start: "2026-10-04T00:00:00.000Z",
    end: "2026-10-05T00:00:00.000Z",
    event_types: ["zha_event"],
  });
});

test("the device_lanes request carries the run config", () => {
  const automation = { triggers: [], actions: [] };
  assert.deepEqual(deviceLanesMessage(automation), { type: "event_simulator/device_lanes", automation });
});

test("an automation's event triggers name the types a replay needs", () => {
  assert.deepEqual(
    [...eventTriggerTypes({ triggers: [{ trigger: "event", event_type: "zha_event" }, { trigger: "state", entity_id: "x.y" }] })],
    ["zha_event"]
  );
  // Legacy keys, lists, several triggers.
  assert.deepEqual(
    [...eventTriggerTypes({ trigger: [{ platform: "event", event_type: ["a", "b"] }, { platform: "event", event_type: "c" }] })].sort(),
    ["a", "b", "c"]
  );
  assert.equal(eventTriggerTypes({ triggers: [{ trigger: "time", at: "07:00" }] }).size, 0);
  assert.equal(eventTriggerTypes(null).size, 0);
  // A templated type could be anything: every type.
  assert.equal(eventTriggerTypes({ triggers: [{ trigger: "event", event_type: "{{ states('input_text.t') }}" }] }), null);
});

test("the events a replay fires: the automation's types, inside the window", () => {
  const types = new Set(["zha_event"]);
  assert.deepEqual(
    busEventsFor(EVENTS, types, T0, T0 + 24 * H).map((e) => e.data.command),
    ["on", "off"]
  );
  assert.equal(busEventsFor(EVENTS, null, T0, T0 + 24 * H).length, 3);
  assert.deepEqual(busEventsFor(EVENTS, new Set(), T0, T0 + 24 * H), []);
});

test("a scenario carries bus_events only for an event-triggered automation", () => {
  const automation = { triggers: [{ trigger: "event", event_type: "tag_scanned" }], actions: [] };
  const s = buildScenario({ automation, start: T0, end: T0 + 24 * H, tracks: [], busEvents: EVENTS });
  assert.deepEqual(s.bus_events, [EVENTS[1]]);
  const plain = buildScenario({ automation: { triggers: [] }, start: T0, end: T0 + 24 * H, tracks: [], busEvents: EVENTS });
  assert.equal("bus_events" in plain, false);
  assert.equal("bus_events" in buildScenario({ automation, start: T0, end: T0 + 24 * H, tracks: [] }), false);
});

test("the lane: one mark per event, a class per type (busiest first), and a readable title", () => {
  const lane = busEventLane(EVENTS);
  assert.deepEqual(
    lane.types.map((t) => [t.event_type, t.count, t.index]),
    [
      ["zha_event", 3, 0],
      ["tag_scanned", 1, 1],
    ]
  );
  assert.equal(lane.marks.length, 4);
  assert.deepEqual(lane.marks[0], {
    t: T0 + H,
    index: 0,
    title: `zha_event ${iso(H)}\ndevice_ieee: aa\ncommand: on`,
  });
  // Nested data reads as JSON.
  assert.match(busEventLane([{ at: iso(0), event_type: "x", data: { args: { k: 1 } } }]).marks[0].title, /args: \{"k":1\}/);
  assert.deepEqual(busEventLane([]), { types: [], marks: [] });
});
