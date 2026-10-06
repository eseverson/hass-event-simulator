// Suggest mode filters (#44): kinds (sent or derived), chips, filtering, variants and helpers. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  KIND_FILTER_KEY,
  activeKinds,
  buildScenario,
  deriveKinds,
  durationText,
  filterSuggestions,
  helperLane,
  helperView,
  hintsFor,
  kindChips,
  kindName,
  normalizeResult,
  readKindFilter,
  suggestionView,
  variantDiff,
  writeKindFilter,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const door = { trigger: "state", entity_id: "binary_sensor.door", to: "on" };
const temp = { trigger: "numeric_state", entity_id: "sensor.temp", above: 20, for: { minutes: 5 } };

test("derives trigger kinds from each trigger platform", () => {
  const k = (t) => deriveKinds({ triggers: [t] }).triggerKinds;
  assert.deepEqual(k(door), ["state"]);
  assert.deepEqual(k({ trigger: "state", entity_id: "light.x", attribute: "brightness", to: 100 }), ["attribute"]);
  assert.deepEqual(k({ trigger: "state", entity_id: "light.x", attribute: "brightness" }), ["attribute", "any_change"]);
  assert.deepEqual(k({ trigger: "state", entity_id: "sensor.x", to: null }), ["any_change"]);
  assert.deepEqual(k({ platform: "state", entity_id: "sensor.x" }), ["any_change"]);
  assert.deepEqual(k(temp), ["numeric"]);
  assert.deepEqual(k({ trigger: "time", at: "07:30:00" }), ["time"]);
  assert.deepEqual(k({ trigger: "time", at: "07:30", weekday: ["mon"] }), ["time", "weekday"]);
  assert.deepEqual(k({ trigger: "time", at: "sensor.alarm" }), ["entity_time"]);
  assert.deepEqual(k({ trigger: "time", at: { entity_id: "sensor.alarm", offset: "-00:05:00" } }), ["entity_time"]);
  assert.deepEqual(k({ trigger: "sun", event: "sunset" }), ["sun"]);
  assert.deepEqual(k({ trigger: "calendar", entity_id: "calendar.x", event: "start" }), ["calendar"]);
  assert.deepEqual(k({ trigger: "event", event_type: "zha_event" }), ["event"]);
});

test("a trigger on a helper's entity is a trend trigger", () => {
  const helpers = [{ domain: "trend", entity_id: "binary_sensor.temp_rising", options: {} }];
  const d = deriveKinds({ triggers: [{ trigger: "state", entity_id: "binary_sensor.temp_rising", to: "on" }] }, helpers);
  assert.deepEqual(d.triggerKinds, ["trend"]);
});

test("or when two triggers share a side; a start/end pair is not an OR", () => {
  assert.deepEqual(deriveKinds({ triggers: [door, temp] }).triggerKinds, ["state", "numeric", "or"]);
  const pair = deriveKinds({ triggers: [{ ...door, id: "start" }, { ...door, to: "off", id: "end" }] });
  assert.deepEqual(pair.triggerKinds, ["state"]);
});

test("kinds add the conditions' kinds, through and/or/not", () => {
  const d = deriveKinds({
    trigger: [door],
    condition: [
      { condition: "time", after: "18:00:00", weekday: ["sat", "sun"] },
      { condition: "or", conditions: [{ condition: "sun", after: "sunset" }, { condition: "numeric_state", entity_id: "sensor.lux", below: 10 }] },
    ],
  });
  assert.deepEqual(d.triggerKinds, ["state"]);
  assert.deepEqual(d.kinds, ["state", "numeric", "time", "weekday", "sun"]);
  assert.deepEqual(deriveKinds({ triggers: [door], conditions: [{ condition: "time", weekday: ["mon"] }] }).kinds, ["state", "weekday"]);
});

test("suggestionView prefers the kinds the backend sent", () => {
  const v = suggestionView({ config: { triggers: [door] }, kinds: ["sun", "state"], trigger_kinds: ["state"] });
  assert.deepEqual(v.kinds, ["state", "sun"]);
  assert.deepEqual(v.triggerKinds, ["state"]);
  const derived = suggestionView({ config: { triggers: [temp], conditions: [{ condition: "sun", after: "sunset" }] } });
  assert.deepEqual(derived.kinds, ["numeric", "sun"]);
  assert.deepEqual(derived.triggerKinds, ["numeric"]);
  assert.deepEqual(derived.variants, []);
  assert.deepEqual(derived.helpers, []);
});

const views = [
  suggestionView({ rank: 0, config: { triggers: [door], conditions: [{ condition: "sun", after: "sunset" }] } }),
  suggestionView({ rank: 1, config: { triggers: [temp] } }),
  suggestionView({ rank: 2, config: { triggers: [{ trigger: "sun", event: "sunset" }] } }),
];

test("chips count the suggestions of each kind, in a fixed order", () => {
  assert.deepEqual(kindChips(views), [
    { kind: "state", name: "State", count: 1 },
    { kind: "numeric", name: "Threshold", count: 1 },
    { kind: "sun", name: "Sun", count: 1 },
  ]);
  assert.deepEqual(kindChips(views, { conditions: true }).find((c) => c.kind === "sun").count, 2);
  assert.equal(kindName("entity_time"), "Entity time");
  assert.equal(kindName("brand_new"), "Brand new");
});

test("filtering shows the suggestions with any selected kind; none selected shows all", () => {
  assert.equal(filterSuggestions(views, []).length, 3);
  assert.deepEqual(filterSuggestions(views, ["sun"]).map((v) => v.rank), [3]);
  assert.deepEqual(filterSuggestions(views, ["sun"], { conditions: true }).map((v) => v.rank), [1, 3]);
  assert.deepEqual(filterSuggestions(views, ["state", "numeric"]).map((v) => v.rank), [1, 2]);
  // A remembered kind no suggestion has drops out instead of hiding everything.
  assert.deepEqual(activeKinds(["calendar", "sun"], kindChips(views)), ["sun"]);
  assert.deepEqual(activeKinds(["calendar"], kindChips(views)), []);
});

test("hints follow the shown suggestions", () => {
  const hints = [{ suggestions: [1] }, { suggestions: [2, 3] }, { suggestions: [] }];
  assert.deepEqual(hintsFor(hints, [3]), [hints[1], hints[2]]);
});

test("the kind filter is remembered, and survives broken storage", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  assert.deepEqual(readKindFilter(storage), { kinds: [], conditions: false });
  writeKindFilter({ kinds: new Set(["sun"]), conditions: true }, storage);
  assert.equal(store.get(KIND_FILTER_KEY), '{"kinds":["sun"],"conditions":true}');
  assert.deepEqual(readKindFilter(storage), { kinds: ["sun"], conditions: true });
  const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
  assert.deepEqual(readKindFilter(broken), { kinds: [], conditions: false });
  writeKindFilter({ kinds: ["sun"] }, broken);
  assert.deepEqual(readKindFilter({ getItem: () => "{nope" }), { kinds: [], conditions: false });
  assert.deepEqual(readKindFilter(undefined), { kinds: [], conditions: false });
});

test("durations read as H:MM:SS", () => {
  assert.equal(durationText({ minutes: 5 }), "0:05:00");
  assert.equal(durationText({ hours: 1, seconds: 30 }), "1:00:30");
  assert.equal(durationText(90), "0:01:30");
  assert.equal(durationText("-00:05:00"), "-00:05:00");
});

test("variant diff names threshold and duration differences first", () => {
  const base = { triggers: [temp], conditions: [{ condition: "state", entity_id: "input_boolean.away", state: "off" }] };
  const variant = { triggers: [{ ...temp, above: 22, for: { minutes: 10 } }], conditions: [{ condition: "state", entity_id: "input_boolean.away", state: "on" }] };
  assert.deepEqual(variantDiff(base, variant), [
    { path: "triggers[0].above", field: "above", label: "above", from: "20", to: "22", kind: "threshold" },
    { path: "triggers[0].for", field: "for", label: "for", from: "0:05:00", to: "0:10:00", kind: "duration" },
    { path: "conditions[0].state", field: "state", label: "condition state", from: "off", to: "on", kind: "other" },
  ]);
  const added = variantDiff({ triggers: [door] }, { triggers: [door, temp] });
  assert.ok(added.some((d) => d.label === "trigger 2 above" && d.from === null && d.to === "20"));
  assert.deepEqual(variantDiff(base, base), []);
});

test("a variant that only changes its helper's options says which (#42)", () => {
  const trigger = { trigger: "state", entity_id: "binary_sensor.hum_rising", to: "on" };
  const helper = (minGradient, sampleDuration) => ({
    domain: "trend",
    entity_id: "binary_sensor.hum_rising",
    options: { entity_id: "sensor.hum", invert: false, max_samples: 25, min_gradient: minGradient, min_samples: 2, sample_duration: sampleDuration },
  });
  assert.deepEqual(variantDiff({ triggers: [trigger] }, { triggers: [trigger] }, [helper(0.011111111111111112, 900)], [helper(0.022222222222222223, 600)]), [
    { path: "helpers.binary_sensor.hum_rising.min_gradient", field: "min_gradient", label: "helper min_gradient", from: "0.01111", to: "0.02222", kind: "threshold" },
    { path: "helpers.binary_sensor.hum_rising.sample_duration", field: "sample_duration", label: "helper sample_duration", from: "0:15:00", to: "0:10:00", kind: "duration" },
  ]);
  const derivative = (window) => ({ domain: "derivative", entity_id: "sensor.hum_derivative", options: { source: "sensor.hum", round: 2, time_window: window, unit_time: "min" } });
  assert.deepEqual(variantDiff({}, {}, [derivative({ minutes: 15 })], [derivative({ minutes: 5 })]).map((d) => [d.label, d.from, d.to, d.kind]), [["helper time_window", "0:15:00", "0:05:00", "duration"]]);
  assert.deepEqual(variantDiff({}, {}, [helper(0.01, 900)], [helper(0.01, 900)]), []);
  // Through the views: the variant's own helpers against the card's.
  const card = suggestionView({ rank: 0, config: { triggers: [trigger] }, helpers: [helper(0.01, 900)], variants: [{ config: { triggers: [trigger] }, helpers: [helper(0.02, 900)], stats: {} }], stats: {} });
  assert.deepEqual(card.variants[0].diff.map((d) => d.label), ["helper min_gradient"]);
  assert.equal(card.variants[0].helpers[0].options.min_gradient, 0.02);
});

test("variants become views with their own stats and a diff against the card", () => {
  const v = suggestionView({
    rank: 1,
    config: { triggers: [temp] },
    stats: { anchors_hit: 3, anchors_total: 4, fires: 5 },
    variants: [{ config: { triggers: [{ ...temp, above: 21 }] }, stats: { anchors_hit: 4, anchors_total: 4, fires: 4 }, exact: true, explanation: "alt" }, "junk"],
  });
  assert.equal(v.variants.length, 1);
  const [a] = v.variants;
  assert.equal(a.rank, 2);
  assert.equal(a.variant, 1);
  assert.equal(a.hits, 4);
  assert.equal(a.exact, true);
  assert.equal(a.explanation, "alt");
  assert.deepEqual(a.triggerKinds, ["numeric"]);
  assert.deepEqual(a.diff.map((d) => d.label), ["above"]);
  assert.deepEqual(a.variants, []);
});

test("helpers: view, option rows, YAML and scenario field", () => {
  const raw = { domain: "trend", title: "Temp rising", entity_id: "binary_sensor.temp_rising", options: { entity_id: "sensor.temp", min_gradient: 0.001, sample_duration: { minutes: 30 } } };
  const h = helperView(raw);
  assert.equal(h.name, "Trend");
  assert.deepEqual(h.rows, [["entity_id", "sensor.temp"], ["min_gradient", "0.001"], ["sample_duration", "0:30:00"]]);
  assert.match(h.yaml, /^binary_sensor:\n- platform: trend\n {2}sensors:\n {4}temp_rising:\n {6}friendly_name: Temp rising\n {6}entity_id: sensor.temp/);
  const d = helperView({ domain: "derivative", title: "Temp rate", entity_id: "sensor.temp_rate", options: { source: "sensor.temp", unit_time: "min" } });
  assert.equal(d.name, "Derivative sensor");
  assert.match(d.yaml, /^sensor:\n- platform: derivative\n {2}name: Temp rate\n {2}source: sensor.temp/);
  assert.equal(helperView({}), null);
  const v = suggestionView({ config: { triggers: [{ trigger: "state", entity_id: "binary_sensor.temp_rising", to: "on" }] }, helpers: [raw] });
  assert.deepEqual(v.triggerKinds, ["trend"]);
  const scenario = buildScenario({ automation: v.config, start: 0, end: 1000, tracks: [], helpers: v.helpers });
  assert.deepEqual(scenario.helpers, [{ domain: "trend", options: raw.options, entity_id: "binary_sensor.temp_rising" }]);
  assert.equal("helpers" in buildScenario({ automation: v.config, start: 0, end: 1000, tracks: [] }), false);
});

test("helper states from a replay result become bars", () => {
  const r = normalizeResult({ runs: [], helper_states: { "binary_sensor.t": [{ at: "1970-01-01T00:00:00.500Z", state: "on" }, { at: "1970-01-01T00:00:00.100Z", state: "off" }], bad: "x" } });
  assert.deepEqual(Object.keys(r.helperStates), ["binary_sensor.t"]);
  assert.deepEqual(helperLane(r.helperStates["binary_sensor.t"], 0, 1000), [
    { t0: 100, t1: 500, state: "off" },
    { t0: 500, t1: 1000, state: "on" },
  ]);
  assert.deepEqual(normalizeResult({}).helperStates, {});
});
