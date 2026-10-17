// Saved scenarios, replay warnings and run failures: `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  adoptRecorded,
  checkScenarioName,
  describeRunError,
  groupWarnings,
  helperView,
  helpersFromScenario,
  insertEvent,
  normalizeResult,
  normalizeWarning,
  scenarioErrorMessage,
  scenarioFingerprint,
  scenarioPayload,
  setInitial,
  timelineEdited,
  tracksFromScenario,
  warningLabel,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const H = 3600e3;
const iso = (ms) => new Date(ms).toISOString();
// What the store hands back: the same instants with a +00:00 offset instead of Z.
const pyIso = (ms) => iso(ms).replace(/\.000Z$/, "+00:00");
const WIN = { start: T0, end: T0 + 4 * H };

const ev = (t, entity_id, state) => ({
  at: pyIso(t),
  entity_id,
  state,
  attributes: {},
  last_changed: pyIso(t),
  last_updated: pyIso(t),
});

const recorded = () => ({
  "binary_sensor.door": {
    initial: { state: "off", attributes: { friendly_name: "Door" }, last_changed: pyIso(T0 - H), last_updated: pyIso(T0 - H) },
    events: [ev(T0 + H, "binary_sensor.door", "on"), ev(T0 + 3 * H, "binary_sensor.door", "off")],
  },
  "sensor.temp": {
    initial: null,
    events: [ev(T0 + 2 * H, "sensor.temp", "21.5"), ev(T0 + 2 * H, "sensor.temp", "21.6")],
  },
});

const panelTracks = () => {
  const r = recorded();
  return Object.entries(r).map(([entity_id, tl]) => ({ entity_id, ...tl, recorded: tl, segments: [], merged: [] }));
};

const AUTOMATION = { alias: "Door light", triggers: [{ trigger: "state", entity_id: "binary_sensor.door" }], actions: [] };

test("the save payload carries the window with an offset, the tracks in order and the edited marks", () => {
  const tracks = panelTracks();
  const door = tracks[0];
  Object.assign(door, insertEvent(door, "binary_sensor.door", T0 + 2 * H, WIN, { state: "on", attributes: {} }).timeline);
  const p = scenarioPayload({ name: "  Evening  ", entityId: "automation.door_light", automation: AUTOMATION, ...WIN, tracks });
  assert.equal(p.name, "Evening");
  assert.equal(p.automation_entity_id, "automation.door_light");
  assert.equal(p.start, "2026-10-04T00:00:00.000Z");
  assert.equal(p.end, "2026-10-04T04:00:00.000Z");
  assert.deepEqual(p.entity_ids, ["binary_sensor.door", "sensor.temp"]);
  assert.deepEqual(Object.keys(p.initial_states), ["binary_sensor.door"]);
  assert.equal(p.events.length, 5);
  assert.deepEqual(p.events.map((e) => Date.parse(e.at)), [T0 + H, T0 + 2 * H, T0 + 2 * H, T0 + 2 * H, T0 + 3 * H].sort((a, b) => a - b));
  assert.equal(p.events.filter((e) => e.edited).length, 1);
  // No panel bookkeeping leaks into the store.
  assert.ok(p.events.every((e) => !("segments" in e)));
  assert.ok(!("recorded" in p) && !JSON.stringify(p).includes("merged"));
  assert.equal(scenarioPayload({ name: "x", entityId: "", automation: {}, ...WIN, tracks: [] }).automation_entity_id, null);
});

test("a saved scenario loads back into the same tracks, order of same-time writes kept", () => {
  const tracks = panelTracks();
  tracks[1] = { ...tracks[1], ...setInitial(tracks[1], { state: "20", attributes: { unit_of_measurement: "°C" } }, T0) };
  const payload = scenarioPayload({ name: "a", entityId: "automation.x", automation: AUTOMATION, ...WIN, tracks });
  // The store round trip: JSON, and times re-serialized with an offset.
  const stored = JSON.parse(JSON.stringify({ ...payload, scenario_id: "01J", start: pyIso(T0), end: pyIso(T0 + 4 * H) }));
  const loaded = tracksFromScenario(stored);
  assert.deepEqual(
    loaded,
    tracks.map((t) => ({ entity_id: t.entity_id, initial: t.initial, events: t.events }))
  );
  assert.deepEqual(loaded[1].events.map((e) => e.state), ["21.5", "21.6"]);
  assert.equal(loaded[1].initial.edited, true);
});

test("entities only in the timeline still get a track, after the listed ones", () => {
  const loaded = tracksFromScenario({
    entity_ids: ["light.a"],
    initial_states: { "light.b": { state: "on", attributes: {} } },
    events: [ev(T0, "light.c", "off"), "junk", { at: pyIso(T0) }],
  });
  assert.deepEqual(loaded.map((t) => t.entity_id), ["light.a", "light.b", "light.c"]);
  assert.deepEqual(tracksFromScenario(null), []);
});

test("loaded tracks adopt the recorded items, so only real edits count as edited", () => {
  const history = recorded()["binary_sensor.door"];
  // Same data, freshly parsed objects with a different time format.
  const saved = JSON.parse(JSON.stringify(history));
  saved.events = saved.events.map((e) => ({ ...e, at: iso(Date.parse(e.at)) }));
  const plain = adoptRecorded(saved, history);
  assert.equal(plain.initial, history.initial);
  assert.ok(plain.events.every((e, i) => e === history.events[i]));
  assert.equal(timelineEdited(plain, history), false);

  const edited = { ...saved, events: [...saved.events, { ...ev(T0 + 3.5 * H, "binary_sensor.door", "on"), edited: true }] };
  const adopted = adoptRecorded(edited, history);
  assert.equal(adopted.events[0], history.events[0]);
  assert.equal(adopted.events[2].edited, true);
  assert.equal(timelineEdited(adopted, history), true);

  // History that no longer has an item (purged): the saved one stays, and counts as an edit.
  const purged = { initial: null, events: [history.events[1]] };
  const kept = adoptRecorded(saved, purged);
  assert.notEqual(kept.events[0], history.events[0]);
  assert.equal(kept.events[1], history.events[1]);
  assert.equal(timelineEdited(kept, purged), true);
  assert.equal(adoptRecorded(saved, null), saved);
});

test("the fingerprint ignores the name and key order, and sees every other change", () => {
  const tracks = panelTracks();
  const base = { entityId: "automation.x", automation: AUTOMATION, ...WIN, tracks };
  const a = scenarioFingerprint(scenarioPayload({ ...base, name: "one" }));
  assert.equal(a, scenarioFingerprint(scenarioPayload({ ...base, name: "two" })));
  const reordered = { actions: [], triggers: AUTOMATION.triggers, alias: "Door light" };
  assert.equal(a, scenarioFingerprint(scenarioPayload({ ...base, automation: reordered, name: "one" })));
  assert.notEqual(a, scenarioFingerprint(scenarioPayload({ ...base, automation: { ...AUTOMATION, mode: "queued" }, name: "one" })));
  assert.notEqual(a, scenarioFingerprint(scenarioPayload({ ...base, end: WIN.end + 1000, name: "one" })));
  assert.notEqual(a, scenarioFingerprint(scenarioPayload({ ...base, tracks: tracks.slice(0, 1), name: "one" })));
  const moved = panelTracks();
  moved[0] = { ...moved[0], ...insertEvent(moved[0], "binary_sensor.door", T0 + 30 * 60e3, WIN).timeline };
  assert.notEqual(a, scenarioFingerprint(scenarioPayload({ ...base, tracks: moved, name: "one" })));
});

test("scenario names are trimmed and bounded", () => {
  assert.deepEqual(checkScenarioName("  Night run "), { ok: true, value: "Night run" });
  assert.equal(checkScenarioName("   ").ok, false);
  assert.equal(checkScenarioName(null).ok, false);
  assert.equal(checkScenarioName("x".repeat(100)).ok, true);
  assert.match(checkScenarioName("x".repeat(101)).error, /100/);
});

test("scenario errors read as sentences per code", () => {
  assert.match(scenarioErrorMessage({ code: "not_found", message: "No saved scenario 1" }), /no longer exists/);
  assert.match(scenarioErrorMessage({ code: "invalid_window" }), /7 days/);
  assert.match(scenarioErrorMessage({ code: "too_large", message: "this one has 60000" }), /too large.*60000/);
  assert.match(scenarioErrorMessage({ code: "too_many" }), /100 saved scenarios/);
  assert.match(scenarioErrorMessage({ code: "store_full", message: "the others hold 480000" }), /Delete.*saved scenarios.*480000/s);
  assert.match(scenarioErrorMessage({ code: "invalid_format", message: "expected str for dictionary value @ data['name']" }), /did not accept.*data\['name'\]/);
  assert.match(scenarioErrorMessage({ code: "unknown_command" }), /Update/);
  assert.equal(scenarioErrorMessage({ code: "home_assistant_error", message: "boom" }, "delete"), "Could not delete the scenario: boom");
  assert.equal(scenarioErrorMessage(new Error("offline"), "list"), "Could not list the scenarios: offline");
});

test("warnings normalize from the engine's objects and from anything else", () => {
  assert.deepEqual(normalizeWarning({ code: "unknown_service", message: "light.nope does not exist", at: pyIso(T0 + H) }), {
    code: "unknown_service",
    message: "light.nope does not exist",
    at: T0 + H,
  });
  assert.deepEqual(normalizeWarning({ code: "late_attach", message: "late", at: null }), { code: "late_attach", message: "late", at: null });
  assert.deepEqual(normalizeWarning("plain text"), { code: "", message: "plain text", at: null });
  assert.deepEqual(normalizeWarning({ weird: true }), { code: "", message: '{"weird":true}', at: null });
  assert.deepEqual(normalizeWarning(7), { code: "", message: "7", at: null });
});

test("warnings group by code in first-seen order, each group in time order", () => {
  const raw = [
    { code: "unknown_service", message: "b", at: pyIso(T0 + 2 * H) },
    { code: "log", message: "WARNING x: y", at: pyIso(T0 + H) },
    { code: "unknown_service", message: "a", at: pyIso(T0 + H) },
    { code: "event_outside_window", message: "skipped", at: null },
    { code: "unknown_service", message: "untimed", at: null },
    "loose string",
  ];
  const groups = groupWarnings(normalizeResult({ warnings: raw }).warnings);
  assert.deepEqual(groups.map((g) => [g.code, g.items.length]), [
    ["unknown_service", 3],
    ["log", 1],
    ["event_outside_window", 1],
    ["", 1],
  ]);
  assert.deepEqual(groups[0].items.map((w) => w.message), ["untimed", "a", "b"]);
  assert.equal(groups[0].label, "Service calls to services this instance does not have");
  assert.equal(groups[3].label, "Other warnings");
  // Raw engine warnings group the same way without normalizing first.
  assert.deepEqual(groupWarnings(raw).map((g) => g.code), groups.map((g) => g.code));
  assert.deepEqual(groupWarnings(undefined), []);
  assert.equal(warningLabel("some_new-code"), "Some new code");
});

test("run failures keep their code, message and the child's last log lines", () => {
  const crash = describeRunError({ code: "sandbox_failed", message: "The sandbox exited with exit code 1", stderr_tail: "Traceback\n  boom\n\n" });
  assert.deepEqual(crash, {
    title: "The sandbox crashed",
    message: "The sandbox exited with exit code 1",
    code: "sandbox_failed",
    stderr: "Traceback\n  boom",
  });
  assert.equal(describeRunError({ code: "timeout", message: "x" }).title, "The replay took too long and was stopped");
  assert.equal(describeRunError({ code: "invalid_automation", message: "bad trigger" }).title, "The automation config is not valid");
  const structured = describeRunError({ error: { code: "invalid_scenario", message: "no start" }, stderr_tail: "" });
  assert.equal(structured.code, "invalid_scenario");
  assert.equal(structured.message, "no start");
  assert.equal(describeRunError({ code: "unknown_command", message: "Unknown command." }).code, "unknown_command");
  assert.match(describeRunError({ code: "unknown_command" }).message, /Update the integration/);
  const plain = describeRunError(new Error("socket closed"));
  assert.deepEqual([plain.title, plain.message, plain.code, plain.stderr], ["The run failed", "socket closed", null, ""]);
  assert.equal(describeRunError({ code: "brand_new", message: "m" }).title, "The run failed");
});

// A scenario opened from a suggestion that needs a Trend or Derivative helper (#42) replays
// with it; saving keeps it, and loading brings it back for the next replay.
const TREND = {
  domain: "trend",
  entity_id: "binary_sensor.bathroom_humidity_rising",
  title: "Bathroom humidity rising",
  options: { entity_id: "sensor.bathroom_humidity", invert: false, max_samples: 25, min_gradient: 0.0111, min_samples: 2, sample_duration: 900 },
};

test("the save payload carries the helpers the replay sets up, and loading brings them back", () => {
  const base = { name: "Shower", entityId: "", automation: AUTOMATION, ...WIN, tracks: panelTracks() };
  assert.equal("helpers" in scenarioPayload(base), false);
  assert.equal("helpers" in scenarioPayload({ ...base, helpers: [] }), false);
  const p = scenarioPayload({ ...base, helpers: [helperView(TREND)] });
  assert.deepEqual(p.helpers, [TREND]);
  assert.notEqual(scenarioFingerprint(p), scenarioFingerprint(scenarioPayload(base)));

  const loaded = helpersFromScenario({ ...p, scenario_id: "x" });
  assert.equal(loaded.length, 1);
  assert.equal(loaded[0].entityId, TREND.entity_id);
  assert.deepEqual(loaded[0].options, TREND.options);
  assert.deepEqual(scenarioPayload({ ...base, helpers: loaded }).helpers, [TREND]);
  assert.deepEqual(helpersFromScenario({}), []);
  assert.deepEqual(helpersFromScenario({ helpers: [{ title: "no domain" }, "junk"] }), []);
});

// ---- lanes (#48) ---------------------------------------------------------------

import { buildLanes as laneBuild, insertBusEvent as laneInsert, lanesFromScenario, restoreLanes, scenarioPayload as payloadOf, shiftLane as laneShift, timelineEdited as laneEdited } from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const L0 = Date.parse("2026-10-04T00:00:00Z");
const LWIN = { start: L0, end: L0 + 24 * 3600e3 };
const KINDS = ["short", "double"].map((c, i) => ({ type: c, subtype: "b1", event_type: "zha_event", match: { device_ieee: "aa", command: c }, used: i === 0 }));
const DEVICE = { id: "device:d1", domain: "zha", device_id: "d1", name: "Remote", event_type: "zha_event", kinds: KINDS };
const press = (h, command) => ({ at: new Date(L0 + h * 3600e3).toISOString(), event_type: "zha_event", data: { device_ieee: "aa", command } });
const RECORDED = [press(1, "short"), press(2, "double"), press(3, "short")];
const freshLanes = () => laneBuild({ deviceLanes: [DEVICE], filters: new Map(), busEvents: RECORDED, ...LWIN });
const base = { name: "x", entityId: "automation.a", automation: {}, start: LWIN.start, end: LWIN.end, tracks: [] };

test("a scenario with an edited lane saves every lane's events and shifts", () => {
  const [lane] = freshLanes();
  const added = { ...lane, ...laneInsert(lane, press(5, "double"), LWIN).timeline };
  added.edited = laneEdited(added, added.recorded);
  const shifted = laneShift(added, 3600e3, LWIN);
  shifted.edited = true;
  const payload = payloadOf({ ...base, lanes: [shifted] });
  assert.equal(payload.bus_events.length, 4);
  assert.equal(payload.bus_events.filter((e) => e.edited).length, 4);
  assert.deepEqual(payload.lane_shifts, { "device:d1": { offset: 3600e3 } });
});

test("a scenario without lane edits saves no lane keys", () => {
  const payload = payloadOf({ ...base, lanes: freshLanes() });
  assert.ok(!("bus_events" in payload));
  assert.ok(!("lane_shifts" in payload));
  assert.equal(lanesFromScenario({}), null);
});

test("a loaded lane adopts its recorded events and keeps the edited ones", () => {
  const [lane] = freshLanes();
  const edited = { ...lane, ...laneInsert(lane, press(5, "double"), LWIN).timeline, edited: true };
  const saved = lanesFromScenario(JSON.parse(JSON.stringify(payloadOf({ ...base, lanes: [edited] }))));
  const [restored] = restoreLanes(freshLanes(), saved, LWIN);
  assert.equal(restored.events.length, 4);
  assert.equal(restored.events[0], restored.recorded.events[0]);
  assert.equal(restored.events.filter((e) => e.edited).length, 1);
  assert.equal(laneEdited(restored, restored.recorded), true);
});

test("a loaded shifted lane is parked again", () => {
  const [lane] = freshLanes();
  const shifted = { ...laneShift(lane, 22 * 3600e3, LWIN), edited: true };
  const saved = lanesFromScenario(JSON.parse(JSON.stringify(payloadOf({ ...base, lanes: [shifted] }))));
  const [restored] = restoreLanes(freshLanes(), saved, LWIN);
  assert.equal(restored.shift.offset, 22 * 3600e3);
  assert.equal(restored.events.length, 1);
  assert.equal(restored.shift.after.length, 2);
});
