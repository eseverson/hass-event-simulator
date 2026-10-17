// The window fields: typed text is checked as a whole before it applies. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { makeClock, windowFromInputs } from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const clock = makeClock({ timeZone: "UTC", serverTimeZone: "UTC" });
const DAY = 86400e3;

test("a typed window applies when it is whole and fits", () => {
  assert.deepEqual(windowFromInputs("2026-10-15T00:00", "2026-10-16T00:00", clock, { maxDays: 7 }), {
    ok: true,
    start: Date.parse("2026-10-15T00:00:00Z"),
    end: Date.parse("2026-10-16T00:00:00Z"),
  });
});

test("a field left blank or half typed is not a window", () => {
  assert.deepEqual(windowFromInputs("", "2026-10-16T00:00", clock, { maxDays: 7 }), { ok: false, error: "Enter a full start and end date and time." });
});

test("an inverted or too long window says why", () => {
  assert.equal(windowFromInputs("2026-10-16T00:00", "2026-10-15T00:00", clock, { maxDays: 7 }).error, "The window must end after it starts.");
  assert.equal(windowFromInputs("2026-10-01T00:00", "2026-10-16T00:00", clock, { maxDays: 7 }).error, "The window can be at most 7 days long (30 in Suggest mode).");
  assert.equal(windowFromInputs("2026-09-01T00:00", "2026-10-16T00:00", clock, { maxDays: 30, suggest: true }).error, "The window can be at most 30 days long in Suggest mode.");
  assert.equal(windowFromInputs("2026-10-09T00:00", "2026-10-16T00:00", clock, { maxDays: 7 }).ok, true);
  assert.equal(7 * DAY, Date.parse("2026-10-16T00:00:00Z") - Date.parse("2026-10-09T00:00:00Z"));
});
