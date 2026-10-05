// What goes over the websocket: packed events and the message size check. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { WS_MESSAGE_LIMIT, buildScenario, messageSizeError, packEvents, simulateMessage } from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

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
