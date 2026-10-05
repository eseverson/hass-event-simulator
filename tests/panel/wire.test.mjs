// What goes over the websocket: packed events and the message size check. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { WS_MESSAGE_LIMIT, buildScenario, messageSizeError, packEvents, saveMessage, simulateMessage, tracksFromScenario, unpackEvents } from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const iso = (ms) => new Date(ms).toISOString();
const POWER = { unit_of_measurement: "W", device_class: "power", friendly_name: "House power" };

test("events pack into shared entity ids and attributes, in order", () => {
  const events = [
    { at: iso(T0), entity_id: "sensor.power", state: "300", attributes: { ...POWER }, last_changed: iso(T0), last_updated: iso(T0) },
    { at: iso(T0 + 20e3), entity_id: "binary_sensor.door", state: "on" },
    { at: iso(T0 + 40e3), entity_id: "sensor.power", state: "310", attributes: { friendly_name: "House power", device_class: "power", unit_of_measurement: "W" } },
    { at: iso(T0 + 60e3), entity_id: "sensor.power", state: "320", attributes: { ...POWER, icon: "mdi:flash" }, edited: true },
  ];
  const packed = packEvents(events);
  assert.deepEqual(packed.entity_ids, ["sensor.power", "binary_sensor.door"]);
  assert.deepEqual(packed.attributes, [POWER, {}, { ...POWER, icon: "mdi:flash" }]);
  assert.deepEqual(packed.rows, [
    [iso(T0), 0, "300", 0],
    [iso(T0 + 20e3), 1, "on", 1],
    [iso(T0 + 40e3), 0, "310", 0],
    [iso(T0 + 60e3), 0, "320", 2],
  ]);
});

test("simulate sends the scenario with its events packed", () => {
  const scenario = buildScenario({
    automation: { alias: "x" },
    start: T0,
    end: T0 + 3600e3,
    tracks: [{ entity_id: "a.a", initial: { state: "off", attributes: {} }, events: [{ at: iso(T0 + 1e3), entity_id: "a.a", state: "on", attributes: {} }] }],
  });
  const msg = simulateMessage(scenario);
  assert.equal(msg.type, "event_simulator/simulate");
  assert.deepEqual(msg.scenario.initial_states, scenario.initial_states);
  assert.deepEqual(msg.scenario.events, { entity_ids: ["a.a"], attributes: [{}], rows: [[iso(T0 + 1e3), 0, "on", 0]] });
});

test("a week of a chatty sensor fits in one message once packed", () => {
  const events = [];
  for (let t = 0; t < 7 * 86400e3; t += 15e3) {
    events.push({ at: new Date(T0 + t).toISOString().replace("Z", "+00:00"), entity_id: "sensor.house_power_consumption", state: (300 + (t % 997)).toFixed(1), attributes: { ...POWER, state_class: "measurement" }, last_changed: iso(T0 + t), last_updated: iso(T0 + t) });
  }
  assert.ok(messageSizeError({ type: "event_simulator/simulate", scenario: { events } }), "unpacked it is over the limit");
  assert.equal(messageSizeError(simulateMessage({ events })), null);
});

test("an oversized message is refused before it is sent, saying what to do", () => {
  const huge = { events: [{ state: "x".repeat(WS_MESSAGE_LIMIT) }] };
  const err = messageSizeError(huge);
  assert.equal(err.code, "too_large");
  assert.match(err.message, /4 MB/);
  assert.match(err.message, /shorter window|fewer tracks/);
  // Bytes, not characters: multi-byte text counts for what it weighs.
  assert.ok(messageSizeError({ s: "é".repeat(WS_MESSAGE_LIMIT / 2 + 1) }));
  assert.equal(messageSizeError({ s: "é".repeat(1000) }), null);
});

// ---- saved scenarios go out and come back packed (#15) ----

test("lossless packing round-trips recorded and edited events", () => {
  const events = [
    // recorded, state change: last_changed = last_updated = at
    { at: iso(T0), entity_id: "sensor.power", state: "300", attributes: { ...POWER }, last_changed: iso(T0), last_updated: iso(T0) },
    // recorded, attribute-only change: last_changed earlier than at
    { at: iso(T0 + 20e3), entity_id: "sensor.power", state: "300", attributes: { ...POWER, icon: "mdi:flash" }, last_changed: iso(T0), last_updated: iso(T0 + 20e3) },
    // edited: no recorded times, marked
    { at: iso(T0 + 40e3), entity_id: "binary_sensor.door", state: "on", attributes: {}, edited: true },
    // anything else rides along untouched
    { at: iso(T0 + 60e3), entity_id: "binary_sensor.door", state: "off", attributes: {}, edited: false, note: { x: 1 } },
  ];
  const packed = packEvents(events, { lossless: true });
  assert.deepEqual(packed.entity_ids, ["sensor.power", "binary_sensor.door"]);
  assert.deepEqual(packed.rows[0], [iso(T0), 0, "300", 0, 3]);
  assert.deepEqual(packed.rows[1], [iso(T0 + 20e3), 0, "300", 1, 1, { last_changed: iso(T0) }]);
  assert.deepEqual(packed.rows[2], [iso(T0 + 40e3), 1, "on", 2, 4]);
  assert.deepEqual(unpackEvents(packed), events);
});

test("unpackEvents takes the old unpacked list as is and anything else as no events", () => {
  const list = [{ at: iso(T0), entity_id: "a.a", state: "on", attributes: {} }];
  assert.equal(unpackEvents(list), list);
  assert.deepEqual(unpackEvents(null), []);
  assert.deepEqual(unpackEvents({ rows: "x" }), []);
  // a row pointing nowhere is skipped rather than drawn as an undefined entity
  assert.deepEqual(unpackEvents({ entity_ids: ["a.a"], attributes: [{}], rows: [[iso(T0), 3, "on", 0], [iso(T0), 0, "on", 0]] }), [
    { at: iso(T0), entity_id: "a.a", state: "on", attributes: {} },
  ]);
});

test("save sends the scenario with its events packed losslessly", () => {
  const events = [{ at: iso(T0 + 1e3), entity_id: "a.a", state: "on", attributes: {}, edited: true }];
  const payload = { name: "n", automation: {}, start: iso(T0), end: iso(T0 + 3600e3), entity_ids: ["a.a"], initial_states: {}, events };
  const msg = saveMessage(payload, null);
  assert.equal(msg.type, "event_simulator/scenarios/save");
  assert.equal("scenario_id" in msg, false);
  assert.equal(msg.name, "n");
  assert.deepEqual(unpackEvents(msg.events), events);
  assert.equal(saveMessage(payload, "abc").scenario_id, "abc");
});

test("a saved scenario's packed events load back into tracks", () => {
  const events = [
    { at: iso(T0 + 2e3), entity_id: "b.b", state: "x", attributes: {}, edited: true },
    { at: iso(T0 + 1e3), entity_id: "a.a", state: "on", attributes: { k: 1 }, last_changed: iso(T0 + 1e3), last_updated: iso(T0 + 1e3) },
  ];
  const tracks = tracksFromScenario({ entity_ids: ["a.a", "b.b"], initial_states: {}, events: packEvents(events, { lossless: true }) });
  assert.deepEqual(tracks.map((t) => [t.entity_id, t.events]), [["a.a", [events[1]]], ["b.b", [events[0]]]]);
});

test("the store's 50,000-change cap fits one save message once packed", () => {
  const events = [];
  for (let i = 0; i < 50000; i++) {
    const at = new Date(T0 + i * 12e3).toISOString().replace("Z", "+00:00").replace(/\.(\d{3})/, ".$1123");
    events.push({ at, entity_id: "sensor.house_power_consumption", state: (300 + (i % 997) / 10).toFixed(1), attributes: { ...POWER, state_class: "measurement" }, last_changed: at, last_updated: at });
  }
  const payload = { name: "week", automation: { alias: "x" }, start: iso(T0), end: iso(T0 + 7 * 86400e3), entity_ids: ["sensor.house_power_consumption"], initial_states: {}, events };
  assert.ok(messageSizeError({ type: "event_simulator/scenarios/save", ...payload }), "unpacked it is over the limit");
  assert.equal(messageSizeError(saveMessage(payload, null)), null);
});
