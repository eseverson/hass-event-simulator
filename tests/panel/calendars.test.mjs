// Calendar events (#37): the calendar lane, and the events a replay of a calendar-triggered automation serves. `node --test tests/panel/`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildScenario,
  calendarEventSpan,
  calendarEventsMessage,
  calendarLane,
  calendarTriggerEntities,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const T0 = Date.parse("2026-09-06T07:00:00Z"); // Sunday 00:00 in Los Angeles
const H = 3600e3;
const TZ = "America/Los_Angeles";
const TRASH = [
  { start: "2026-09-10", end: "2026-09-11", summary: "Garbage" },
  { start: "2026-09-12T09:00:00-07:00", end: "2026-09-12T10:00:00-07:00", summary: "Sweeping", location: "Street" },
  { start: "2026-09-13T08:00:00", end: "2026-09-13T08:30:00", summary: "Local" },
  { start: "2026-10-01", end: "2026-10-02", summary: "Later" },
];
const REMINDER = { triggers: [{ trigger: "calendar", entity_id: "calendar.trash", event: "start", offset: "-7:0:0" }], actions: [] };

test("an automation's calendar triggers name the calendars a replay needs", () => {
  assert.deepEqual([...calendarTriggerEntities(REMINDER)], ["calendar.trash"]);
  const both = {
    trigger: [
      { platform: "calendar", entity_id: "calendar.a" },
      { trigger: "calendar.event_started", target: { entity_id: ["calendar.b", "light.x"] } },
      { trigger: "state", entity_id: "calendar.c" },
    ],
  };
  assert.deepEqual([...calendarTriggerEntities(both)].sort(), ["calendar.a", "calendar.b"]);
  assert.equal(calendarTriggerEntities(null).size, 0);
});

test("the calendar_events request names the calendars and the window", () => {
  assert.deepEqual(calendarEventsMessage(new Set(["calendar.trash"]), T0, T0 + 24 * H), {
    type: "event_simulator/calendar_events",
    entity_ids: ["calendar.trash"],
    start: "2026-09-06T07:00:00.000Z",
    end: "2026-09-07T07:00:00.000Z",
  });
});

test("a scenario carries the events of the calendars its triggers read, only those", () => {
  const scenario = buildScenario({
    automation: REMINDER,
    start: T0,
    end: T0 + 7 * 24 * H,
    tracks: [],
    calendars: { "calendar.trash": TRASH, "calendar.other": [] },
  });
  assert.deepEqual(scenario.calendars, { "calendar.trash": TRASH });
  const none = buildScenario({ automation: { triggers: [], actions: [] }, start: T0, end: T0 + H, tracks: [], calendars: { "calendar.trash": TRASH } });
  assert.equal("calendars" in none, false);
});

test("event spans: a date is local midnight, a time without an offset is local wall time", () => {
  assert.deepEqual(calendarEventSpan(TRASH[0], TZ), { t0: Date.parse("2026-09-10T07:00:00Z"), t1: Date.parse("2026-09-11T07:00:00Z") });
  assert.deepEqual(calendarEventSpan(TRASH[1], TZ), { t0: Date.parse("2026-09-12T16:00:00Z"), t1: Date.parse("2026-09-12T17:00:00Z") });
  assert.deepEqual(calendarEventSpan(TRASH[2], TZ), { t0: Date.parse("2026-09-13T15:00:00Z"), t1: Date.parse("2026-09-13T15:30:00Z") });
});

test("the lane draws the events in the window, with their summaries", () => {
  const bars = calendarLane(TRASH, TZ, T0, T0 + 14 * 24 * H);
  assert.equal(bars.length, 3);
  assert.equal(bars[0].title, "Garbage\n2026-09-10 (all day)");
  assert.match(bars[1].title, /^Sweeping\n.* – .*\nStreet$/);
});
