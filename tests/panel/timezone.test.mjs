// Display time zone and clock (#32): the panel follows Home Assistant's profile setting
// (local = browser zone, server = hass.config.time_zone) and its 12/24-hour format.
// Fixed zones throughout, so the result does not depend on the machine running it:
// the "browser" is America/Los_Angeles and the server Europe/Berlin.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  annotateClockYaml,
  brushPeriod,
  displaySettings,
  fromLocalInput,
  hasClockTerms,
  localizeTimes,
  makeClock,
  offsetIn,
  presetWindow,
  resolveTimeZone,
  snapTime,
  startOfDay,
  timeTicks,
  toLocalInput,
  toLocalInputSeconds,
  useAmPm,
  wallParts,
  wallToTime,
  zoneOffset,
} from "../../custom_components/event_simulator/panel/event-simulator-panel.js";

const LA = "America/Los_Angeles";
const BER = "Europe/Berlin";
const H = 3600e3;
const M = 60e3;
const DAY = 24 * H;
const at = (iso) => Date.parse(iso);
const plain = (s) => s.replace(/[  ]/g, " ");

const hass = (time_zone, time_format = "language", language = "en") => ({
  locale: { language, time_format, time_zone, number_format: "language", date_format: "language", first_weekday: "language" },
  config: { time_zone: BER },
});

test("the profile option picks the browser's zone or the server's, like HA's resolveTimeZone", () => {
  assert.equal(resolveTimeZone("local", BER, LA), LA);
  assert.equal(resolveTimeZone("server", BER, LA), BER);
  assert.equal(resolveTimeZone("local", BER, null), BER); // browser zone unknown (null): server's
  assert.equal(resolveTimeZone("server", undefined, LA), LA); // no server zone either way
  assert.equal(displaySettings(hass("local"), LA).timeZone, LA);
  assert.equal(displaySettings(hass("server"), LA).timeZone, BER);
  assert.equal(displaySettings(hass("server"), LA).serverTimeZone, BER);
  // No locale (an old core): the profile default, the browser's zone.
  assert.equal(displaySettings({ config: { time_zone: BER } }, LA).timeZone, LA);
  assert.equal(displaySettings(null, LA).timeZone, LA);
});

test("12/24 hours follow time_format, and the language's own clock for 'language'", () => {
  assert.equal(useAmPm({ time_format: "12", language: "de" }), true);
  assert.equal(useAmPm({ time_format: "24", language: "en" }), false);
  assert.equal(useAmPm({ time_format: "language", language: "en-US" }), true);
  assert.equal(useAmPm({ time_format: "language", language: "de" }), false);
  assert.equal(displaySettings(hass("local", "24"), LA).hour12, false);
});

test("zone offsets and wall clocks, both sides of DST", () => {
  assert.equal(zoneOffset(at("2026-01-15T12:00:00Z"), LA), -480);
  assert.equal(zoneOffset(at("2026-07-15T12:00:00Z"), LA), -420);
  assert.equal(zoneOffset(at("2026-01-15T12:00:00Z"), BER), 60);
  assert.equal(zoneOffset(at("2026-07-15T12:00:00Z"), BER), 120);
  // The same instant is Monday in Berlin and still Sunday in Los Angeles, which is how a
  // weekday condition can disagree with a date shown in the browser's zone.
  const t = at("2026-10-05T03:00:00Z");
  assert.equal(wallParts(t, BER).weekday, 1);
  assert.equal(wallParts(t, LA).weekday, 0);
  assert.deepEqual(
    { ...wallParts(t, BER), weekday: undefined },
    { year: 2026, month: 10, day: 5, hour: 5, minute: 0, second: 0, weekday: undefined }
  );
});

test("datetime-local values are written and read in the display zone", () => {
  const t = at("2026-10-04T12:00:07Z");
  assert.equal(toLocalInput(t, LA), "2026-10-04T05:00");
  assert.equal(toLocalInput(t, BER), "2026-10-04T14:00");
  assert.equal(toLocalInputSeconds(t, BER), "2026-10-04T14:00:07");
  assert.equal(fromLocalInput("2026-10-04T14:00:07", BER), t);
  assert.equal(fromLocalInput("2026-10-04T05:00:07", LA), t);
  assert.equal(fromLocalInput("2026-10-04T14:00", BER), t - 7e3);
  for (const bad of ["", null, "2026-10-04", "2026-02-30T10:00", "2026-10-04T25:00", "soon"]) assert.ok(Number.isNaN(fromLocalInput(bad, BER)), String(bad));
  // Round trip through every zone the panel might show.
  for (const tz of [LA, BER, "Asia/Kolkata", "Australia/Lord_Howe", "UTC"]) assert.equal(fromLocalInput(toLocalInputSeconds(t, tz), tz), t, tz);
});

test("typed times in a DST gap land after it, and in an overlap on the first occurrence", () => {
  // Spring forward: 02:30 does not exist, read as 03:30 summer time (as browsers do).
  assert.equal(fromLocalInput("2026-03-08T02:30", LA), at("2026-03-08T10:30:00Z"));
  assert.equal(fromLocalInput("2026-03-29T02:30", BER), at("2026-03-29T01:30:00Z"));
  // Fall back: 01:30 (LA) and 02:30 (Berlin) happen twice; the first one, summer time.
  assert.equal(fromLocalInput("2026-11-01T01:30", LA), at("2026-11-01T08:30:00Z"));
  assert.equal(fromLocalInput("2026-10-25T02:30", BER), at("2026-10-25T00:30:00Z"));
  // Both instants of the repeated hour still show as that wall time.
  assert.equal(toLocalInput(at("2026-10-25T01:30:00Z"), BER), "2026-10-25T02:30");
  // A plain offset function works too (the form the snapping helpers take).
  assert.equal(wallToTime(at("2026-10-04T14:00:00Z"), () => 120), at("2026-10-04T12:00:00Z"));
});

test("day ticks stay on the display zone's midnight across a DST change", () => {
  const t0 = at("2026-10-30T00:00:00Z");
  for (const tz of [LA, BER]) {
    const { step, ticks } = timeTicks(t0, t0 + 5 * DAY, 500, offsetIn(tz));
    assert.equal(step, DAY);
    assert.ok(ticks.length >= 4, tz);
    for (const t of ticks) assert.match(toLocalInput(t, tz), /T00:00$/, tz);
  }
  // Hourly ticks over Berlin's fall-back night stay on whole hours and never repeat.
  const night = at("2026-10-24T22:00:00Z");
  const { ticks } = timeTicks(night, night + 6 * H, 600, offsetIn(BER));
  assert.ok(ticks.every((t) => t % H === 0));
  assert.equal(new Set(ticks).size, ticks.length);
});

test("snapping and brushes use the display zone's quarter hours", () => {
  const kolkata = offsetIn("Asia/Kolkata"); // +05:30: whole hours there are :30 in UTC
  assert.equal(snapTime(at("2026-10-04T10:20:00Z"), H, kolkata), at("2026-10-04T10:30:00Z"));
  assert.equal(snapTime(at("2026-10-04T10:20:00Z"), H, offsetIn(BER)), at("2026-10-04T10:00:00Z"));
  const win = { start: at("2026-10-04T00:00:00Z"), end: at("2026-10-05T00:00:00Z") };
  assert.deepEqual(brushPeriod(at("2026-10-04T10:20:00Z"), at("2026-10-04T12:10:00Z"), { ...win, step: H, offsetMinutes: kolkata }), {
    start: at("2026-10-04T10:30:00Z"),
    end: at("2026-10-04T12:30:00Z"),
  });
});

test("Today and Yesterday are calendar days in the display zone, 23 or 25 hours at DST", () => {
  const now = at("2026-10-26T12:00:00Z"); // the day after Berlin falls back
  const today = presetWindow("today", now, BER);
  assert.equal(today.start, at("2026-10-25T23:00:00Z"));
  const yesterday = presetWindow("yesterday", now, BER);
  assert.deepEqual(yesterday, { start: at("2026-10-24T22:00:00Z"), end: at("2026-10-25T23:00:00Z") });
  assert.equal(yesterday.end - yesterday.start, 25 * H);
  // The same instant in Los Angeles is still the 26th, from its midnight.
  assert.equal(presetWindow("today", now, LA).start, at("2026-10-26T07:00:00Z"));
  assert.equal(presetWindow("yesterday", at("2026-03-09T12:00:00Z"), LA).end - presetWindow("yesterday", at("2026-03-09T12:00:00Z"), LA).start, 23 * H);
  assert.equal(startOfDay(at("2026-10-05T03:00:00Z"), BER), at("2026-10-04T22:00:00Z"));
  // Spans do not depend on the zone.
  assert.deepEqual(presetWindow("24h", now, BER), { start: now - DAY, end: now });
});

test("the clock formats in its zone with the profile's 12/24 hours", () => {
  const t = at("2026-10-04T17:05:09Z");
  const ber24 = makeClock({ timeZone: BER, serverTimeZone: BER, hour12: false, language: "en" });
  const la12 = makeClock({ timeZone: LA, serverTimeZone: BER, hour12: true, language: "en" });
  assert.equal(plain(ber24.time(t)), "19:05:09");
  assert.equal(plain(ber24.time(t, false)), "19:05");
  assert.equal(plain(la12.time(t)), "10:05:09 AM");
  assert.equal(plain(la12.full(t)), "Oct 4, 10:05:09 AM");
  assert.equal(plain(ber24.full(t)), "Oct 4, 19:05:09");
  assert.equal(la12.date(at("2026-10-05T03:00:00Z")), "Oct 4");
  assert.equal(ber24.date(at("2026-10-05T03:00:00Z")), "Oct 5");
  assert.equal(la12.toInput(t), "2026-10-04T10:05");
  assert.equal(la12.fromInput("2026-10-04T10:05:09"), t);
  assert.equal(ber24.full(NaN), "?");
  // Whether times need a zone named next to clock conditions.
  assert.equal(la12.foreign, true);
  assert.equal(ber24.foreign, false);
  assert.equal(makeClock({ timeZone: "US/Pacific", serverTimeZone: LA }).foreign, false);
  assert.equal(makeClock({ timeZone: LA }).foreign, false); // server zone unknown: nothing to compare
});

test("clock-dependent suggestions are detected and their YAML times annotated", () => {
  assert.equal(hasClockTerms({ triggers: [{ trigger: "state", entity_id: "a.b" }], conditions: [{ condition: "sun", after: "sunset" }] }), false);
  assert.equal(hasClockTerms({ conditions: [{ condition: "and", conditions: [{ condition: "time", weekday: ["mon"] }] }] }), true);
  assert.equal(hasClockTerms({ triggers: [{ platform: "time_pattern", minutes: "/5" }] }), true);
  assert.equal(hasClockTerms({ triggers: [{ trigger: "time", at: "07:00:00" }] }), true);
  const yaml = [
    "conditions:",
    "- condition: time",
    '  after: "17:30:00"',
    '  before: "20:30:00"',
    "  weekday:",
    "  - mon",
    "- condition: sun",
    "  after: sunset",
  ].join("\n");
  assert.equal(
    annotateClockYaml(yaml, "Europe/Berlin time"),
    [
      "conditions:",
      "- condition: time",
      '  after: "17:30:00"  # Europe/Berlin time',
      '  before: "20:30:00"  # Europe/Berlin time',
      "  weekday:  # Europe/Berlin time",
      "  - mon",
      "- condition: sun",
      "  after: sunset",
    ].join("\n")
  );
});

test("times inside a warning's message read in the display zone", () => {
  const fmt = (ms) => toLocalInput(ms, BER);
  const msg = "automation.x started 1000 runs at 2026-10-05T17:40:42+00:00 without virtual time moving on";
  assert.equal(localizeTimes(msg, fmt), "automation.x started 1000 runs at 2026-10-05T19:40 without virtual time moving on");
  assert.equal(localizeTimes("until 2026-10-05T17:40:42.250Z, then 2026-10-05T13:00:00-04:00.", fmt), "until 2026-10-05T19:40, then 2026-10-05T19:00.");
  // No offset, a bare date, or nothing like a time: left as is.
  for (const text of ["at 2026-10-05T17:40:42 local", "on 2026-10-05", "no time here"]) assert.equal(localizeTimes(text, fmt), text);
});
