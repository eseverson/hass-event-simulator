// Marking the config editor's lines that differ from the saved automation. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { configDiffHunks, configLineMarks, configLines, lineDiff } from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const SAVED = `alias: Door light
triggers:
  - trigger: state
    entity_id: binary_sensor.door
    to: "on"
conditions: []
actions:
  - action: light.turn_on
    target:
      entity_id: light.hall
mode: single
`;

const edit = (fn) => fn(SAVED.split("\n")).join("\n");

/* Apply an edit script to `a` and check it yields `b`. */
function replay(a, b, ops) {
  const out = [];
  for (const op of ops) {
    if (op.op === "equal") out.push(...a.slice(op.a, op.a + op.count));
    if (op.op === "insert") out.push(...b.slice(op.b, op.b + op.count));
  }
  return out;
}

test("the edit script turns one list into the other", () => {
  const cases = [
    [[], []],
    [["a"], []],
    [[], ["a"]],
    [["a", "b", "c"], ["a", "b", "c"]],
    [["a", "b", "c"], ["a", "x", "c"]],
    [["a", "b", "c", "d"], ["b", "c", "d", "e"]],
    [["x", "a", "y", "b"], ["a", "b", "z"]],
  ];
  for (const [a, b] of cases) assert.deepEqual(replay(a, b, lineDiff(a, b)), b, JSON.stringify([a, b]));
});

test("too large a middle gives up on the table but stays a valid script", () => {
  const a = ["h", ...Array.from({ length: 50 }, (_, i) => `a${i}`), "t"];
  const b = ["h", ...Array.from({ length: 50 }, (_, i) => `b${i}`), "t"];
  const ops = lineDiff(a, b, 10);
  assert.deepEqual(replay(a, b, ops), b);
  assert.deepEqual(ops.map((o) => o.op), ["equal", "delete", "insert", "equal"]);
});

test("the saved config itself marks nothing", () => {
  const marks = configLineMarks(SAVED, SAVED);
  assert.ok(marks.lines.every((k) => k === null));
  assert.deepEqual(marks.removed, []);
  assert.deepEqual(marks.counts, { added: 0, changed: 0, removed: 0 });
});

test("trailing whitespace and a final newline are not changes", () => {
  const marks = configLineMarks(SAVED, SAVED.replace("mode: single\n", "mode: single   ").replace("alias: Door light", "alias: Door light  "));
  assert.deepEqual(marks.counts, { added: 0, changed: 0, removed: 0 });
  assert.deepEqual(configLines("a\r\nb\n\n"), ["a", "b"]);
});

test("a changed value marks just that line", () => {
  const text = SAVED.replace('to: "on"', 'to: "off"');
  const marks = configLineMarks(SAVED, text);
  assert.equal(marks.lines[4], "changed");
  assert.equal(marks.lines.filter(Boolean).length, 1);
  assert.deepEqual(marks.counts, { added: 0, changed: 1, removed: 0 });
});

test("added lines are added, and a replaced block pairs line for line", () => {
  const added = edit((l) => [...l.slice(0, 5), '    for: "00:05:00"', ...l.slice(5)]);
  const marks = configLineMarks(SAVED, added);
  assert.equal(marks.lines[5], "added");
  assert.deepEqual(marks.counts, { added: 1, changed: 0, removed: 0 });

  const replaced = edit((l) => [...l.slice(0, 7), "  - action: notify.phone", "    data:", "      message: Door opened", "      title: Door", ...l.slice(10)]);
  const r = configLineMarks(SAVED, replaced);
  assert.deepEqual(r.lines.slice(7, 11), ["changed", "changed", "changed", "added"]);
  assert.deepEqual(r.counts, { added: 1, changed: 3, removed: 0 });
});

test("removed lines leave a marker above the line that followed them", () => {
  const text = edit((l) => [...l.slice(0, 2), ...l.slice(5)]); // the trigger's three lines
  const marks = configLineMarks(SAVED, text);
  assert.ok(marks.lines.every((k) => k === null));
  assert.equal(marks.removed.length, 1);
  assert.equal(marks.removed[0].before, 2);
  assert.deepEqual(marks.removed[0].lines, ["  - trigger: state", "    entity_id: binary_sensor.door", '    to: "on"']);
  assert.deepEqual(marks.counts, { added: 0, changed: 0, removed: 3 });
  // Removed at the very end: the marker sits past the last line.
  const end = configLineMarks(SAVED, edit((l) => l.slice(0, 10)));
  assert.deepEqual(end.removed, [{ before: 10, lines: ["mode: single"] }]);
});

test("a shrunk block is changed lines then a removal marker", () => {
  const text = edit((l) => [...l.slice(0, 7), "  - action: light.toggle", ...l.slice(10)]);
  const marks = configLineMarks(SAVED, text);
  assert.equal(marks.lines[7], "changed");
  assert.deepEqual(marks.removed, [{ before: 8, lines: ["    target:", "      entity_id: light.hall"] }]);
});

test("the compact diff keeps a little context around each change", () => {
  const text = SAVED.replace('to: "on"', 'to: "off"');
  const hunks = configDiffHunks(SAVED, text, 1);
  assert.equal(hunks.length, 1);
  assert.deepEqual(
    hunks[0].map((r) => `${r.kind}${r.n ?? "-"} ${r.text.trim()}`),
    [" 4 entity_id: binary_sensor.door", "-- to: \"on\"", "+5 to: \"off\"", " 6 conditions: []"]
  );
  assert.deepEqual(configDiffHunks(SAVED, SAVED), []);
  // Two changes far apart are two hunks.
  const two = SAVED.replace("alias: Door light", "alias: Door lamp").replace("mode: single", "mode: restart");
  assert.equal(configDiffHunks(SAVED, two, 1).length, 2);
});
