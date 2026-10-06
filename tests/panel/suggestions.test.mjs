// Suggest mode results: card view-model, fires overlay, hints and their quick labels, YAML. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_TOLERANCE,
  HINT_SPAN_MIN,
  fireMarks,
  hintLabel,
  hintMarkStyle,
  hintView,
  jumpView,
  labelAnchors,
  placeLabel,
  suggestEmptyState,
  suggestionView,
  toYaml,
  triggerYaml,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-10-04T00:00:00Z");
const M = 60e3;
const H = 3600e3;
const WIN = { start: T0, end: T0 + 24 * H };
const iso = (ms) => new Date(T0 + ms).toISOString();

const LABELS = [
  { id: "a", start: T0 + 7 * H, end: T0 + 8 * H, kind: "fire", anchor: "start" },
  { id: "b", start: T0 + 12 * H, end: T0 + 13 * H, kind: "no_fire", anchor: "start" },
  { id: "c", start: T0 + 19 * H, end: T0 + 20 * H, kind: "fire", anchor: "both" },
];

// The shape suggest/subscribe answers with (suggest/score.py Stats.as_dict, search.py hints).
const CONFIG = {
  alias: "Suggested: door opens after sunset",
  description: "Fires when binary_sensor.door changes to on, only after sunset",
  triggers: [{ trigger: "state", entity_id: "binary_sensor.door", to: "on" }],
  conditions: [{ condition: "sun", after: "sunset" }],
  actions: [{ action: "logbook.log", data: { name: "Suggested automation", message: "fired" } }],
  mode: "single",
};
const RESULT = {
  suggestions: [
    {
      rank: 1,
      config: CONFIG,
      explanation: "Fires when binary_sensor.door changes to on, only after sunset; hits 2/3 marked starts, fires 2 other times",
      exact: true,
      stats: {
        fires: 5,
        anchors_hit: 2,
        anchors_total: 3,
        missed: [{ at: iso(20 * H), edge: "end", label: 1 }],
        no_fire_violations: [iso(12 * H + 30 * M)],
        outside_fires: [iso(22 * H)],
        inside_fires: [],
        fire_times: [iso(7 * H + M), iso(12 * H + 30 * M), iso(19 * H), iso(22 * H)],
        while_held: 0,
        while_total: 0,
      },
      approx_stats: { fires: 6 },
    },
    { rank: 2, config: { triggers: [{ trigger: "time", at: "07:00:00" }] }, stats: { anchors_hit: 1, anchors_total: 3, missed: [{ at: iso(19 * H), edge: "start" }, { at: iso(20 * H), edge: "end" }] } },
  ],
  hints: [
    { kind: "outside_fire", at: iso(22 * H), similarity: 0.8, suggestions: [1], description: "Fires at 22:00 on Oct 4" },
    { kind: "recurrence", at: iso(3 * H), similarity: 1.7, suggestions: [], description: "binary_sensor.door went off → on", entity_id: "binary_sensor.door", from: "off", to: "on" },
    { kind: "outside_fire", at: "not a time", similarity: 0.5 },
  ],
  evaluated: 42,
};

test("a suggestion reads as a card: explanation, stats in ms, the exact badge", () => {
  const v = suggestionView(RESULT.suggestions[0]);
  assert.equal(v.rank, 1);
  assert.equal(v.exact, true);
  assert.match(v.explanation, /^Fires when binary_sensor.door/);
  assert.deepEqual([v.hits, v.total, v.fires], [2, 3, 5]);
  assert.deepEqual(v.missed, [{ at: T0 + 20 * H, edge: "end" }]);
  assert.deepEqual(v.violations, [T0 + 12 * H + 30 * M]);
  assert.deepEqual(v.outside, [T0 + 22 * H]);
  assert.equal(v.fireTimes.length, 4);
  assert.deepEqual(v.approx, { fires: 6 });
});

test("a sparse or malformed suggestion still makes a card", () => {
  const v = suggestionView(RESULT.suggestions[1], 1);
  assert.equal(v.exact, false, "estimated unless the sandbox checked it");
  assert.equal(v.explanation, "");
  assert.deepEqual([v.hits, v.total, v.fires], [1, 3, 1]);
  assert.equal(v.fireTimes, null);
  const empty = suggestionView(null, 4);
  assert.equal(empty.rank, 5);
  assert.deepEqual([empty.hits, empty.total, empty.violations, empty.missed], [0, 0, [], []]);
  // The explanation falls back to the config's description; the hit count to total - missed.
  const desc = suggestionView({ config: { description: "From the description" }, stats: { anchors_total: 2, missed: [{ at: iso(H) }] } });
  assert.equal(desc.explanation, "From the description");
  assert.equal(desc.hits, 1);
});

test("fires sort into anchor hits, must-not violations, unlabeled and missed anchors", () => {
  const marks = fireMarks(suggestionView(RESULT.suggestions[0]), LABELS);
  assert.deepEqual(
    marks.map((m) => [(m.t - T0) / M, m.kind]),
    [
      [7 * 60 + 1, "anchor"],
      [12 * 60 + 30, "no_fire"],
      [19 * 60, "anchor"],
      [20 * 60, "missed"],
      [22 * 60, "unlabeled"],
    ]
  );
});

test("without fire times, the hit anchors stand in for their fires", () => {
  const marks = fireMarks(suggestionView(RESULT.suggestions[1]), LABELS);
  assert.deepEqual(
    marks.map((m) => [(m.t - T0) / H, m.kind, !!m.approx]),
    [
      [7, "anchor", true],
      [19, "missed", false],
      [20, "missed", false],
    ]
  );
});

test("two fires at the same instant in different buckets are both kept", () => {
  const view = suggestionView({ stats: { outside_fires: [iso(H)], fire_times: [iso(H), iso(H)] } });
  assert.deepEqual(fireMarks(view, []).map((m) => m.kind).sort(), ["anchor", "unlabeled"]);
});

test("labels yield their anchors in time order", () => {
  assert.deepEqual(
    labelAnchors(LABELS).map((a) => [(a.at - T0) / H, a.edge, a.label]),
    [
      [7, "start", "a"],
      [19, "start", "c"],
      [20, "end", "c"],
    ]
  );
});

test("hints read with their similarity clamped, timeless ones dropped", () => {
  const hints = RESULT.hints.map(hintView);
  assert.equal(hints[2], null);
  assert.equal(hints[0].kind, "outside_fire");
  assert.equal(hints[0].at, T0 + 22 * H);
  assert.equal(hints[1].similarity, 1);
  assert.deepEqual([hints[1].entityId, hints[1].from, hints[1].to], ["binary_sensor.door", "off", "on"]);
  assert.equal(hintView({ at: iso(H) }).similarity, 0.5);
});

test("likelier hints are larger and more opaque", () => {
  const lo = hintMarkStyle(0);
  const hi = hintMarkStyle(1);
  assert.ok(hi.r > lo.r && hi.opacity > lo.opacity);
  assert.deepEqual(hintMarkStyle(5), hi);
  assert.deepEqual(hintMarkStyle("x"), lo);
});

test("'yes, this too' places a should-fire period whose anchor is on the hint", () => {
  const hint = { at: T0 + 22 * H };
  const opts = { labels: LABELS, tolerance: DEFAULT_TOLERANCE, window: WIN };
  // Start anchor: as long as the other start-anchored periods (1 h).
  const start = hintLabel(hint, "yes", { ...opts, anchor: "start" });
  assert.deepEqual(start.label, { start: T0 + 22 * H, end: T0 + 23 * H, kind: "fire", anchor: "start" });
  // End anchor with no other end-anchored period: the tolerance window, ending on the hint.
  const end = hintLabel(hint, "yes", { ...opts, anchor: "end" });
  assert.deepEqual(end.label, { start: T0 + 22 * H - 10 * M, end: T0 + 22 * H, kind: "fire", anchor: "end" });
  // It fits next to the others.
  assert.ok(placeLabel(LABELS, start.label).ok);
});

test("'yes' stops short of a must-not period and of the window", () => {
  const opts = { labels: LABELS, tolerance: DEFAULT_TOLERANCE, window: WIN, anchor: "start" };
  const r = hintLabel({ at: T0 + 11 * H + 30 * M }, "yes", opts);
  assert.equal(r.label.end, T0 + 12 * H);
  assert.ok(placeLabel(LABELS, r.label).ok);
  assert.equal(hintLabel({ at: T0 + 23 * H + 50 * M }, "yes", opts).label.end, WIN.end);
  assert.equal(hintLabel({ at: T0 + 12 * H + 10 * M }, "yes", opts).ok, false, "inside a must-not period");
  assert.equal(hintLabel({ at: T0 - H }, "yes", opts).ok, false, "outside the window");
});

test("'no, not here' covers the tolerance around the hint, clear of should-fire periods", () => {
  const opts = { labels: LABELS, tolerance: DEFAULT_TOLERANCE, window: WIN };
  const r = hintLabel({ at: T0 + 22 * H }, "no", opts);
  assert.deepEqual(r.label, { start: T0 + 22 * H - 2 * M, end: T0 + 22 * H + 10 * M, kind: "no_fire", anchor: "start" });
  // Right after a should-fire period: starts where it ends.
  const after = hintLabel({ at: T0 + 8 * H + M }, "no", opts);
  assert.equal(after.label.start, T0 + 8 * H);
  assert.ok(placeLabel(LABELS, after.label).ok);
  assert.equal(hintLabel({ at: T0 + 7 * H + 5 * M }, "no", opts).ok, false, "inside a should-fire period");
  // A zero tolerance still makes a period of HINT_SPAN_MIN each side.
  const tight = hintLabel({ at: T0 + 3 * H }, "no", { ...opts, tolerance: { before: 0, after: 0 } });
  assert.equal(tight.label.end - tight.label.start, 2 * HINT_SPAN_MIN);
});

test("jumping centers the spot and zooms in to at most two hours", () => {
  assert.deepEqual(jumpView([WIN.start, WIN.end], T0 + 12 * H, [WIN.start, WIN.end]), [T0 + 11 * H, T0 + 13 * H]);
  assert.deepEqual(jumpView([T0, T0 + 30 * M], T0 + 12 * H, [WIN.start, WIN.end]), [T0 + 12 * H - 15 * M, T0 + 12 * H + 15 * M]);
  assert.deepEqual(jumpView([WIN.start, WIN.end], T0 + 10 * M, [WIN.start, WIN.end]), [T0, T0 + 2 * H], "kept inside the window");
});

test("the empty states say what to do next", () => {
  const base = { available: true, tracks: 2, fireLabels: 1, state: null };
  assert.equal(suggestEmptyState({ ...base, available: false }).kind, "unavailable");
  assert.equal(suggestEmptyState({ ...base, tracks: 0 }).kind, "no_tracks");
  assert.match(suggestEmptyState({ ...base, fireLabels: 0 }).text, /^Mark at least one period where it should fire/);
  assert.equal(suggestEmptyState({ ...base, state: { result: { suggestions: [] } } }).kind, "none_found");
  assert.equal(suggestEmptyState({ ...base, state: { running: true } }).kind, "waiting");
  assert.equal(suggestEmptyState({ ...base, state: { error: { title: "x" } } }), null, "the error says it");
  assert.equal(suggestEmptyState({ ...base, state: { result: { suggestions: [{}] } } }), null);
});

test("YAML reads like a hand-written automation", () => {
  assert.equal(
    toYaml(CONFIG),
    [
      'alias: "Suggested: door opens after sunset"',
      "description: Fires when binary_sensor.door changes to on, only after sunset",
      "triggers:",
      "- trigger: state",
      "  entity_id: binary_sensor.door",
      '  to: "on"',
      "conditions:",
      "- condition: sun",
      "  after: sunset",
      "actions:",
      "- action: logbook.log",
      "  data:",
      "    name: Suggested automation",
      "    message: fired",
      "mode: single",
    ].join("\n")
  );
  assert.equal(toYaml({ at: "07:00:00", above: 70, for: { minutes: 5 }, weekday: ["mon", "tue"], x: [], obj: {}, z: null, b: false }),
    ['at: "07:00:00"', "above: 70", "for:", "  minutes: 5", "weekday:", "- mon", "- tue", "x: []", "obj: {}", "z: null", "b: false"].join("\n"));
  assert.equal(toYaml({ s: "a: b", t: "tail ", u: "#x", v: "yes", w: "" }), ['s: "a: b"', 't: "tail "', 'u: "#x"', 'v: "yes"', 'w: ""'].join("\n"));
  assert.equal(toYaml([[1, 2], { a: 1 }]), ["- - 1", "  - 2", "- a: 1"].join("\n"));
});

test("a card's short YAML is its triggers and conditions", () => {
  assert.equal(triggerYaml(CONFIG), ["triggers:", "- trigger: state", "  entity_id: binary_sensor.door", '  to: "on"', "conditions:", "- condition: sun", "  after: sunset"].join("\n"));
  assert.equal(triggerYaml({ trigger: [{ platform: "time", at: "07:00:00" }], condition: [] }), ["triggers:", "- platform: time", '  at: "07:00:00"'].join("\n"));
});
