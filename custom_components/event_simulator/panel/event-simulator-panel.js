/* Event Simulator sidebar panel.
 * Plain web component: no build step, no framework, no external dependencies. Uses
 * Home Assistant's CSS variables so it matches the active theme, light or dark.
 *
 * Flow: pick an automation -> pick a window -> tracks load from the recorder for the
 * entities the automation references -> Run sends the scenario to the sandbox and the
 * runs it reports land on a results row under the shared time axis.
 *
 * Editing: change marks on a track drag in time (snapped, never past a neighbor), a
 * click opens an inline editor for the state and attributes in effect there (or the
 * initial state), and changes can be added and deleted. Edits are undoable, marked as
 * edited against the recorded history, and resettable per track or all at once. The
 * automation config can be edited too (see loadYamlEditor); none of it is ever saved
 * to Home Assistant, it only shapes the scenario Run sends.
 *
 * The first half of this file is pure functions (time scaling, numeric detection,
 * segment building, result parsing), exported so `node --test tests/panel/` can check
 * them without a browser. Home Assistant loads the file as an ES module, so the
 * exports are harmless there.
 */

// ---- pure helpers ---------------------------------------------------------

/* States that mean "no reading", drawn hatched rather than as a value. */
export const MISSING_STATES = new Set(["unavailable", "unknown"]);
export const isMissing = (state) => state == null || MISSING_STATES.has(String(state));

const NUMBER_RE = /^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i;

/* A state as a number, or NaN. Stricter than Number(): "", "0x1f" and " " are not numbers. */
export function toNumber(state) {
  if (typeof state === "number") return Number.isFinite(state) ? state : NaN;
  if (typeof state !== "string") return NaN;
  const text = state.trim();
  return NUMBER_RE.test(text) ? Number(text) : NaN;
}

/* A track is numeric when every state that is not unavailable/unknown parses as a number,
 * and there is at least one such state. */
export function isNumericStates(states) {
  let seen = false;
  for (const state of states) {
    if (isMissing(state)) continue;
    if (Number.isNaN(toNumber(state))) return false;
    seen = true;
  }
  return seen;
}

/* Epoch milliseconds from an ISO string (or a number passed through); NaN if unparseable. */
export function parseTime(value) {
  if (typeof value === "number") return value;
  if (typeof value !== "string" || !value) return NaN;
  return Date.parse(value);
}

/* Linear mapping between time [t0, t1] and pixels [0, width]. */
export function makeScale(t0, t1, width) {
  const k = width / Math.max(1, t1 - t0);
  return { t0, t1, width, toX: (t) => (t - t0) * k, toT: (x) => t0 + x / k };
}

/* One entity's history as contiguous segments [from, to) inside [start, end).
 * `initial` is the state at `start` (may be absent: then nothing is drawn until the
 * first event). Events at the same instant collapse to the last one. */
export function buildSegments(initial, events, start, end) {
  const segments = [];
  let cur = initial ? { from: start, state: initial.state, attributes: initial.attributes || {} } : null;
  const sorted = [...events]
    .map((ev) => ({ ev, t: parseTime(ev.at) }))
    .filter(({ t }) => Number.isFinite(t) && t < end)
    .sort((a, b) => a.t - b.t);
  for (const { ev, t: raw } of sorted) {
    const t = Math.max(raw, start);
    if (cur && t > cur.from) segments.push({ ...cur, to: t });
    cur = { from: t, state: ev.state, attributes: ev.attributes || {} };
  }
  if (cur && end > cur.from) segments.push({ ...cur, to: end });
  return segments;
}

/* Adjacent segments with the same state become one (attribute-only writes vanish).
 * Used for discrete tracks, where only the state is drawn. */
export function mergeSegments(segments) {
  const out = [];
  for (const seg of segments) {
    const last = out[out.length - 1];
    if (last && last.to === seg.from && String(last.state) === String(seg.state)) last.to = seg.to;
    else out.push({ ...seg });
  }
  return out;
}

/* Index of the first segment whose `to` is after t (binary search; segments sorted). */
function firstEndingAfter(segments, t) {
  let lo = 0;
  let hi = segments.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (segments[mid].to > t) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

/* The segment covering time t, or null. */
export function segmentAt(segments, t) {
  const i = firstEndingAfter(segments, t);
  const seg = segments[i];
  return seg && seg.from <= t ? seg : null;
}

/* Segments overlapping [t0, t1). */
export function visibleSegments(segments, t0, t1) {
  const out = [];
  for (let i = firstEndingAfter(segments, t0); i < segments.length && segments[i].from < t1; i++) out.push(segments[i]);
  return out;
}

/* Min and max of the numeric states, ignoring missing ones; null if there are none. */
export function numericRange(segments) {
  let min = Infinity;
  let max = -Infinity;
  for (const seg of segments) {
    const v = toNumber(seg.state);
    if (Number.isNaN(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
  }
  return min === Infinity ? null : { min, max };
}

/* SVG path `d` for a step plot of the numeric segments. A missing or non-numeric state
 * breaks the line, so gaps read as gaps rather than as a held value. */
export function stepPath(segments, toX, toY) {
  const r = (n) => Math.round(n * 10) / 10;
  let d = "";
  let prevEnd = null;
  for (const seg of segments) {
    const v = toNumber(seg.state);
    if (Number.isNaN(v)) {
      prevEnd = null;
      continue;
    }
    const x1 = r(toX(seg.from));
    const x2 = r(toX(seg.to));
    const y = r(toY(v));
    d += prevEnd === seg.from ? `L${x1},${y}L${x2},${y}` : `M${x1},${y}L${x2},${y}`;
    prevEnd = seg.to;
  }
  return d;
}

/* Tick steps the axis may use, smallest first. */
export const TICK_STEPS = [
  1e3, 5e3, 15e3, 30e3, 60e3, 5 * 60e3, 15 * 60e3, 30 * 60e3,
  3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 864e5, 2 * 864e5, 7 * 864e5,
];

/* The smallest step that keeps ticks at least `minPx` apart. */
export function chooseStep(span, width, minPx = 90) {
  const maxTicks = Math.max(1, Math.floor(width / minPx));
  return TICK_STEPS.find((step) => span / step <= maxTicks) ?? TICK_STEPS[TICK_STEPS.length - 1];
}

const localOffsetMinutes = (t) => -new Date(t).getTimezoneOffset();

/* Tick times in [t0, t1], aligned to whole steps of local time (so hourly ticks land on
 * the hour and daily ticks at midnight). `offsetMinutes(t)` is the zone's UTC offset. */
export function timeTicks(t0, t1, width, offsetMinutes = localOffsetMinutes) {
  const step = chooseStep(t1 - t0, width);
  const off = offsetMinutes(t0) * 60e3;
  const ticks = [];
  for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1 && ticks.length < 500; t += step) ticks.push(t);
  return { step, ticks };
}

export const MIN_SPAN = 10e3;

/* Keep a view inside [lo, hi] and no narrower than MIN_SPAN (or the whole range). */
export function clampView(t0, t1, lo, hi, minSpan = MIN_SPAN) {
  const full = hi - lo;
  const span = Math.min(Math.max(t1 - t0, Math.min(minSpan, full)), full);
  let start = Math.max(t0, lo);
  if (start + span > hi) start = hi - span;
  return [start, start + span];
}

/* Zoom about `anchor`: factor < 1 zooms in, > 1 zooms out. */
export function zoomView([t0, t1], anchor, factor, [lo, hi]) {
  return clampView(anchor - (anchor - t0) * factor, anchor + (t1 - anchor) * factor, lo, hi);
}

/* Shift the view by dt milliseconds. clampView keeps the span, so a pan never zooms. */
export function panView([t0, t1], dt, [lo, hi]) {
  return clampView(t0 + dt, t1 + dt, lo, hi);
}

/* The item whose x is nearest to `x`, within `maxPx`; items are {x, ...}. */
export function findNearest(items, x, maxPx) {
  let best = null;
  let bestDist = maxPx;
  for (const item of items) {
    const dist = Math.abs(item.x - x);
    if (dist <= bestDist) {
      best = item;
      bestDist = dist;
    }
  }
  return best;
}

const ON_STATES = new Set(["on", "open", "home", "detected", "playing", "active", "true", "heat", "cool", "unlocked", "armed_away", "armed_home"]);
const OFF_STATES = new Set(["off", "closed", "not_home", "idle", "false", "clear", "paused", "standby", "locked", "disarmed"]);
const PALETTE = ["#039be5", "#43a047", "#fb8c00", "#8e24aa", "#e53935", "#00897b", "#3949ab", "#c0ca33", "#6d4c41", "#d81b60"];

export function hashString(text) {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (Math.imul(h, 31) + text.charCodeAt(i)) | 0;
  return Math.abs(h);
}

/* Fill for a discrete state. null means "missing": drawn with the hatch pattern. */
export function stateColor(state) {
  if (isMissing(state)) return null;
  const s = String(state).toLowerCase();
  if (ON_STATES.has(s)) return "var(--primary-color, #03a9f4)";
  if (OFF_STATES.has(s)) return "var(--disabled-text-color, #9e9e9e)";
  return PALETTE[hashString(s) % PALETTE.length];
}

export const OUTCOMES = ["fired", "condition_failed", "error", "other"];

export function classifyOutcome(outcome, run = {}) {
  const o = String(outcome ?? "").toLowerCase();
  if (o === "fired" || o === "condition_failed" || o === "error") return o;
  if (!o && run.error) return "error";
  return "other";
}

const isObject = (v) => v != null && typeof v === "object" && !Array.isArray(v);

/* The simulate result in a shape the panel can draw, whatever arrives. Runs without a
 * parseable start are dropped (nowhere to put them); each run keeps its index in the
 * original list, because service calls refer to runs by that index (or by run id). */
export function normalizeResult(raw) {
  const r = isObject(raw) ? raw : {};
  const runs = (Array.isArray(r.runs) ? r.runs : [])
    .map((run, index) => {
      const o = isObject(run) ? run : {};
      const trace = isObject(o.trace) ? o.trace : null;
      const started = parseTime(o.started);
      const finished = parseTime(o.finished);
      return {
        index,
        raw: o,
        started,
        finished: Number.isFinite(finished) && finished >= started ? finished : started,
        outcome: classifyOutcome(o.outcome, o),
        outcomeLabel: String(o.outcome ?? "unknown"),
        stoppedBy: o.stopped_by ?? null,
        error: o.error ?? trace?.error ?? null,
        trigger: o.trigger ?? trace?.trigger ?? null,
        trace,
        id: o.id ?? o.run_id ?? trace?.run_id ?? null,
      };
    })
    .filter((run) => Number.isFinite(run.started));
  const calls = (Array.isArray(r.service_calls) ? r.service_calls : [])
    .filter(isObject)
    .map((c) => ({
      t: parseTime(c.at),
      domain: String(c.domain ?? "?"),
      service: String(c.service ?? "?"),
      data: c.data ?? {},
      target: c.target ?? null,
      run: c.run ?? null,
      raw: c,
    }))
    .filter((c) => Number.isFinite(c.t));
  const warnings = (Array.isArray(r.warnings) ? r.warnings : []).map((w) => (typeof w === "string" ? w : JSON.stringify(w)));
  return { runs, calls, warnings };
}

/* Service calls a run made: matched by the run's index, or by its id when it has one. */
export function callsForRun(calls, run) {
  return calls.filter((c) => c.run != null && (c.run === run.index || (run.id != null && c.run === run.id)));
}

/* One readable line for a trigger, which may be a description string or a trigger dict. */
export function describeTrigger(trigger) {
  if (trigger == null) return "";
  if (typeof trigger === "string") return trigger;
  if (!isObject(trigger)) return String(trigger);
  if (trigger.description) return String(trigger.description);
  const parts = [trigger.platform ?? trigger.trigger, trigger.entity_id].filter(Boolean);
  const from = trigger.from_state?.state;
  const to = trigger.to_state?.state;
  if (from !== undefined || to !== undefined) parts.push(`${from ?? "∅"} → ${to ?? "∅"}`);
  return parts.join(" ") || JSON.stringify(trigger);
}

/* The condition steps of an extended trace, in trace order: {path, depth, result, error}.
 * result is true/false, or null when the trace did not record one. */
export function conditionResults(trace) {
  const steps = trace?.trace;
  if (!isObject(steps)) return [];
  return Object.entries(steps)
    .filter(([path]) => path.startsWith("condition/"))
    .map(([path, list]) => {
      const last = Array.isArray(list) && list.length ? list[list.length - 1] : {};
      const result = last?.result?.result;
      return {
        path,
        depth: Math.max(0, Math.floor(path.split("/").length / 2) - 1),
        result: typeof result === "boolean" ? result : null,
        error: last?.error ?? null,
      };
    });
}

/* An event or initial state without the panel's own bookkeeping (the `edited` mark). */
function stripMarks(item) {
  if (!item || !("edited" in item)) return item;
  const { edited, ...rest } = item;
  return rest;
}

/* The scenario sent to event_simulator/simulate (see the design spec). Events from all
 * tracks merged into one list ordered by time, as the child replays them. `automation`
 * is always the config as an object, edited or not (see the config editor below). */
export function buildScenario({ automation, start, end, tracks }) {
  const initial_states = {};
  const events = [];
  for (const track of tracks) {
    if (track.initial) initial_states[track.entity_id] = stripMarks(track.initial);
    for (const ev of track.events) events.push(stripMarks(ev));
  }
  events.sort((a, b) => parseTime(a.at) - parseTime(b.at));
  return {
    version: 1,
    // No `core` block: no websocket command hands the panel the instance's time zone,
    // location and units, so simulate fills it in server side from hass.config
    // (history.async_core_config) when the scenario arrives without one.
    start: new Date(start).toISOString(),
    end: new Date(end).toISOString(),
    automation,
    initial_states,
    events,
  };
}

/* `YYYY-MM-DDTHH:MM` in local time, the value format of <input type="datetime-local">. */
export function toLocalInput(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fromLocalInput(value) {
  return value ? new Date(value).getTime() : NaN;
}

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/* Short duration text: 850 ms, 12 s, 3 min 4 s, 2 h 5 min, 1 d 3 h. */
export function formatDuration(ms) {
  if (!Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

/* `YYYY-MM-DDTHH:MM:SS` in local time, for <input type="datetime-local" step="1">. */
export function toLocalInputSeconds(ms) {
  const d = new Date(ms);
  return `${toLocalInput(ms)}:${String(d.getSeconds()).padStart(2, "0")}`;
}

// ---- timeline editing (pure) ------------------------------------------------
//
// A track's editable data is a timeline `{initial, events}`: the state at the window
// start (or null) and the state writes in the window, sorted by `at`. Every function
// here returns a new timeline and never mutates its input; event objects are treated
// as immutable and replaced when edited, so an undo snapshot is just the old
// `{initial, events}` pair. Edited and inserted items carry `edited: true`, which the
// panel draws and buildScenario strips.

/* Number of events whose time is <= t (binary search; events sorted by `at`). */
export function countAtOrBefore(events, t) {
  let lo = 0;
  let hi = events.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (parseTime(events[mid].at) <= t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/* Index of the event in effect at t (the last one at or before it), or -1 for the
 * initial state. */
export function eventIndexAt(events, t) {
  return countAtOrBefore(events, t) - 1;
}

/* The {state, attributes} in effect at t, or null when nothing is known yet. */
export function stateAt(timeline, t) {
  const i = eventIndexAt(timeline.events, t);
  const item = i >= 0 ? timeline.events[i] : timeline.initial;
  return item ? { state: item.state, attributes: item.attributes || {} } : null;
}

/* Snap steps for dragging, smallest first. */
export const SNAP_STEPS = [1e3, 5e3, 15e3, 30e3, 60e3, 5 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3];

/* The smallest snap step worth at least `minPx` pixels at this zoom, so a drag moves
 * in whole seconds when zoomed in and whole minutes or quarter hours when zoomed out. */
export function snapStep(msPerPx, minPx = 8) {
  return SNAP_STEPS.find((step) => step >= msPerPx * minPx) ?? SNAP_STEPS[SNAP_STEPS.length - 1];
}

/* t rounded to a whole step of local time (whole minutes, quarter hours on the clock). */
export function snapTime(t, step, offsetMinutes = localOffsetMinutes) {
  const off = offsetMinutes(t) * 60e3;
  return Math.round((t + off) / step) * step - off;
}

const isoAt = (t) => new Date(t).toISOString();

/* An edited event: marked, and without the recorded last_changed/last_updated, which
 * no longer describe it (the sandbox stamps events with their `at` when it plays them). */
function editedEvent(ev, changes) {
  const { last_changed, last_updated, ...rest } = ev;
  return { ...rest, ...changes, edited: true };
}

/* The times event `index` may be dragged to without passing a neighbor on its track,
 * inside the window [start, end). */
export function shiftBounds(events, index, start, end) {
  const prev = events[index - 1];
  const next = events[index + 1];
  const lo = Math.max(start, prev ? parseTime(prev.at) + 1 : start);
  const hi = Math.min(end - 1, next ? parseTime(next.at) - 1 : end - 1);
  return [lo, hi];
}

/* Drag event `index` to about time t. It snaps to `step` when the snapped time still
 * fits between its neighbors, and otherwise stops just short of the neighbor, so a
 * drag never reorders a track. Returns {timeline, index, at}; the timeline is the
 * input itself when the event did not move. */
export function shiftEvent(timeline, index, t, { start, end, step = 0, offsetMinutes = localOffsetMinutes }) {
  const ev = timeline.events[index];
  if (!ev) return { timeline, index, at: NaN };
  const [lo, hi] = shiftBounds(timeline.events, index, start, end);
  const old = parseTime(ev.at);
  if (lo > hi) return { timeline, index, at: old };
  let at = step ? snapTime(t, step, offsetMinutes) : Math.round(t);
  if (at < lo || at > hi) at = Math.min(hi, Math.max(lo, Math.round(t)));
  if (at === old) return { timeline, index, at };
  const events = timeline.events.slice();
  events[index] = editedEvent(ev, { at: isoAt(at) });
  return { timeline: { ...timeline, events }, index, at };
}

/* Insert `event` (which has an `at`) after every event at or before its time, so it is
 * the one in effect from then on. Returns {timeline, index}. */
function insertSorted(timeline, event) {
  const events = timeline.events.slice();
  const index = countAtOrBefore(events, parseTime(event.at));
  events.splice(index, 0, event);
  return { timeline: { ...timeline, events }, index };
}

/* Move event `index` to exactly t (typed in, so it may pass its neighbors: the list is
 * re-sorted). t must be inside the window. Returns {timeline, index}. */
export function moveEvent(timeline, index, t, { start, end }) {
  const ev = timeline.events[index];
  if (!ev) throw new Error("No such change");
  if (!(t >= start && t < end)) throw new Error("The time must be inside the window");
  if (t === parseTime(ev.at)) return { timeline, index };
  const rest = { ...timeline, events: timeline.events.filter((_, i) => i !== index) };
  return insertSorted(rest, editedEvent(ev, { at: isoAt(t) }));
}

/* A new state write on a track at t (clamped into the window). The state and attributes
 * default to whatever is in effect there, so an added change starts as a no-op the user
 * then edits. Returns {timeline, index}. */
export function insertEvent(timeline, entityId, t, { start, end }, value = null) {
  const at = Math.min(end - 1, Math.max(start, Math.round(t)));
  const current = value || stateAt(timeline, at) || { state: "unknown", attributes: {} };
  return insertSorted(timeline, {
    at: isoAt(at),
    entity_id: entityId,
    state: String(current.state),
    attributes: { ...(current.attributes || {}) },
    edited: true,
  });
}

/* Change the state and/or attributes of event `index`. */
export function updateEvent(timeline, index, { state, attributes }) {
  const ev = timeline.events[index];
  if (!ev) throw new Error("No such change");
  const events = timeline.events.slice();
  events[index] = editedEvent(ev, {
    ...(state !== undefined ? { state: String(state) } : {}),
    ...(attributes !== undefined ? { attributes } : {}),
  });
  return { ...timeline, events };
}

export function deleteEvent(timeline, index) {
  if (!timeline.events[index]) throw new Error("No such change");
  return { ...timeline, events: timeline.events.filter((_, i) => i !== index) };
}

/* Set the state at the window start. A changed state drops the recorded timestamps
 * (the sandbox then treats it as set at the window start, the recorder's own clamp);
 * an attribute-only edit keeps last_changed and moves last_updated to the start. */
export function setInitial(timeline, { state, attributes }, start) {
  const old = timeline.initial;
  const next = { state: String(state), attributes: attributes || {}, edited: true };
  if (old && String(old.state) === next.state && old.last_changed) {
    next.last_changed = old.last_changed;
    next.last_updated = isoAt(start);
  }
  return { ...timeline, initial: next };
}

/* JSON with object keys sorted, so equal data compares equal whatever its key order. */
export function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (isObject(value)) {
    return `{${Object.keys(value)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/* Whether a timeline differs from what was recorded. */
export function timelineEdited(timeline, recorded) {
  if (!recorded) return false;
  if (timeline.initial !== recorded.initial || timeline.events.length !== recorded.events.length) return true;
  return timeline.events.some((ev, i) => ev !== recorded.events[i]);
}

/* Attributes typed as JSON: {ok, value} or {ok: false, error}. Blank means {}. */
export function parseAttributes(text) {
  const src = String(text ?? "").trim();
  if (!src) return { ok: true, value: {} };
  try {
    const value = JSON.parse(src);
    if (!isObject(value)) return { ok: false, error: "Attributes must be a JSON object, like {\"brightness\": 128}." };
    return { ok: true, value };
  } catch (e) {
    return { ok: false, error: `Attributes are not valid JSON: ${e.message}` };
  }
}

/* An automation config from the editor: {ok, value} or {ok: false, error}. */
export function checkAutomationConfig(value) {
  if (!isObject(value)) return { ok: false, error: "The automation config must be a mapping (triggers, conditions, actions…)." };
  return { ok: true, value };
}

/* A bounded undo stack. Entries are {label, snapshots: [{entity_id, timeline}]}: the
 * timelines of the tracks an edit touched, as they were before it. */
export class EditHistory {
  constructor(limit = 30) {
    this.limit = limit;
    this.entries = [];
  }
  push(entry) {
    this.entries.push(entry);
    if (this.entries.length > this.limit) this.entries.splice(0, this.entries.length - this.limit);
  }
  pop() {
    return this.entries.pop() ?? null;
  }
  peek() {
    return this.entries[this.entries.length - 1] ?? null;
  }
  clear() {
    this.entries = [];
  }
  get size() {
    return this.entries.length;
  }
}

// ---- panel ----------------------------------------------------------------

const HOUR = 3600e3;
const DAY = 24 * HOUR;
const MAX_WINDOW = 7 * DAY; // history.MAX_WINDOW
const MAX_ENTITIES = 50; // history.MAX_ENTITIES
const PRESETS = [
  { id: "1h", label: "1 h", span: HOUR },
  { id: "6h", label: "6 h", span: 6 * HOUR },
  { id: "24h", label: "24 h", span: DAY },
  { id: "3d", label: "3 d", span: 3 * DAY },
  { id: "7d", label: "7 d", span: 7 * DAY },
  { id: "today", label: "Today" },
  { id: "yesterday", label: "Yesterday" },
];
const AXIS_H = 24;
const DISCRETE_H = 24;
const NUMERIC_H = 56;
const RESULTS_H = 38;
const OUTCOME_LABEL = { fired: "Fired", condition_failed: "Stopped by condition", error: "Error", other: "Other" };
const OUTCOME_COLOR = {
  fired: "var(--success-color, #43a047)",
  condition_failed: "var(--warning-color, #ff9800)",
  error: "var(--error-color, #db4437)",
  other: "var(--secondary-text-color, #727272)",
};

function presetWindow(id, now = Date.now()) {
  const end = Math.ceil(now / 60e3) * 60e3;
  const preset = PRESETS.find((p) => p.id === id);
  if (preset?.span) return { start: end - preset.span, end };
  const midnight = new Date(now);
  midnight.setHours(0, 0, 0, 0);
  if (id === "today") return { start: midnight.getTime(), end };
  const before = new Date(midnight);
  before.setDate(before.getDate() - 1);
  return { start: before.getTime(), end: midnight.getTime() };
}

/* Make Home Assistant's own `ha-yaml-editor` available, resolving true once it is.
 *
 * YAML choice: the automation config is edited as YAML in `ha-yaml-editor` (CodeMirror
 * with HA's YAML schema and entity autocomplete). It parses with the frontend's bundled
 * js-yaml and hands back an object (`value-changed` -> {value, isValid}), so the panel
 * sends `scenario.automation` as an object and the simulate command never sees YAML
 * text. The frontend only defines the element once a page that uses it has loaded, and
 * there is no import path to it without a bundler, so this loads the developer tools'
 * Actions page (which uses it) through the panel resolver, the way custom cards load
 * HA's editors. That leans on frontend internals (`partial-panel-resolver._getRoutes`,
 * the developer-tools router's routes), so any failure, or no element within the
 * timeout, falls back to a monospace textarea holding the config as JSON (also valid
 * YAML), parsed with JSON.parse: still an object, so the contract does not change. */
export async function loadYamlEditor(timeoutMs = 8000) {
  const registry = globalThis.customElements;
  if (!registry) return false;
  if (registry.get("ha-yaml-editor")) return true;
  const defined = registry.whenDefined("ha-yaml-editor").then(() => true);
  const load = async () => {
    const ppr = document.createElement("partial-panel-resolver");
    const routes = ppr._getRoutes([{ component_name: "developer-tools", url_path: "event-simulator-yaml" }]);
    await routes.routes["event-simulator-yaml"].load();
    await registry.whenDefined("developer-tools-router");
    const router = document.createElement("developer-tools-router");
    await router.routerOptions.routes.action.load();
  };
  load().catch((e) => console.debug("event-simulator: ha-yaml-editor not loadable, using a textarea", e));
  return Promise.race([defined, new Promise((resolve) => setTimeout(() => resolve(false), timeoutMs))]);
}

const Base = globalThis.HTMLElement ?? class {};

class EventSimulatorPanel extends Base {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._automations = null; // [{entity_id, alias, id}] once loaded
    this._autoFilter = "";
    this._entityId = ""; // selected automation
    this._automation = null; // {config, referenced_entities}
    this._preset = "24h";
    this._window = presetWindow("24h");
    this._view = [this._window.start, this._window.end];
    this._tracks = []; // [{entity_id, initial, events, segments, merged, numeric, range}]
    this._loading = null; // text while automations/automation/history load
    this._error = null; // load error shown above the timeline
    this._notice = null;
    this._running = false;
    this._result = null; // normalizeResult() of the last run
    this._resultStale = false;
    this._runError = null; // {message, code, stderr}
    this._selectedRun = null; // run index
    this._requestToken = 0; // drops responses to requests a newer one superseded
    this._width = 600;
    this._drag = null; // pan (or click) in progress
    this._evDrag = null; // event drag in progress: {entity_id, index, preview, ...}
    this._frame = 0;
    this._rendered = false;
    // Timeline editing.
    this._sel = null; // {entity_id, index}: the event being edited, index -1 = initial state
    this._selT = null; // the time last clicked on the selected track, for "Add change"
    this._edDraft = null; // unapplied editor input: {key, time, state, attrs}
    this._edError = null;
    this._undo = new EditHistory(30);
    // Automation config editing. `_cfgDraft` is the edited config object (null = the
    // saved one); it only ever reaches the simulate call, never Home Assistant.
    this._yamlState = "loading"; // "loading" | "ready" | "unavailable"
    this._cfgEl = null; // the ha-yaml-editor or textarea
    this._cfgDraft = null;
    this._cfgError = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
    if (this._cfgEl && this._yamlState === "ready") this._cfgEl.hass = hass;
    if (first) this._connect();
  }
  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    const changed = this._narrow !== !!narrow;
    this._narrow = !!narrow;
    if (changed && this._rendered) this._render();
  }
  set panel(panel) {
    this._panel = panel;
  }

  connectedCallback() {
    if (this._rendered && this._resizeObserver) this._observeWidth();
    window.addEventListener("keydown", this._onShortcut);
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this._onShortcut);
    this._resizeObserver?.disconnect();
    cancelAnimationFrame(this._frame);
  }

  // ---- data ---------------------------------------------------------------

  async _connect() {
    this._loading = "Loading automations…";
    this._render();
    loadYamlEditor().then((ok) => {
      this._yamlState = ok ? "ready" : "unavailable";
      this._renderConfig();
    });
    try {
      this._automations = await this._hass.callWS({ type: "event_simulator/automations" });
      this._loading = null;
      if (this._automations.length === 1) return this._selectAutomation(this._automations[0].entity_id);
    } catch (e) {
      this._loading = null;
      this._error = `Could not list automations: ${e.message || e}`;
    }
    this._render();
  }

  async _selectAutomation(entityId) {
    this._entityId = entityId;
    this._automation = null;
    this._tracks = [];
    this._clearResult();
    this._clearEdits();
    this._cfgDraft = null;
    this._cfgError = null;
    this._error = null;
    this._notice = null;
    this._renderConfig();
    if (!entityId) return this._render();
    const token = ++this._requestToken;
    this._loading = "Loading automation…";
    this._render();
    try {
      const automation = await this._hass.callWS({ type: "event_simulator/automation", entity_id: entityId });
      if (token !== this._requestToken) return;
      this._automation = automation;
      this._renderConfig();
      let ids = automation.referenced_entities || [];
      if (ids.length > MAX_ENTITIES) {
        this._notice = `This automation references ${ids.length} entities; only the first ${MAX_ENTITIES} were loaded.`;
        ids = ids.slice(0, MAX_ENTITIES);
      }
      this._tracks = ids.map((entity_id) => ({ entity_id, initial: null, events: [] }));
      await this._loadHistory();
    } catch (e) {
      if (token !== this._requestToken) return;
      this._loading = null;
      this._error = `Could not load ${entityId}: ${e.message || e}`;
      this._render();
    }
  }

  /* Fetch history for `entityIds` (default: every track) in the current window and
   * replace those tracks' data. A response is dropped if the automation or the window
   * changed while it was in flight (both bump _requestToken and reload everything). */
  async _loadHistory(entityIds = null) {
    const token = this._requestToken;
    const ids = entityIds || this._tracks.map((t) => t.entity_id);
    this._error = null;
    if (!ids.length) {
      this._loading = null;
      return this._render();
    }
    this._loading = `Loading history for ${ids.length} ${ids.length === 1 ? "entity" : "entities"}…`;
    this._render();
    try {
      const timeline = await this._hass.callWS({
        type: "event_simulator/history",
        entity_ids: ids,
        start: new Date(this._window.start).toISOString(),
        end: new Date(this._window.end).toISOString(),
      });
      if (token !== this._requestToken) return;
      const byEntity = new Map(ids.map((id) => [id, []]));
      for (const ev of timeline.events || []) byEntity.get(ev.entity_id)?.push(ev);
      for (const track of this._tracks) {
        if (!byEntity.has(track.entity_id)) continue;
        track.initial = timeline.initial_states?.[track.entity_id] || null;
        // Stable sort: the editing helpers rely on time order, same-instant writes keep theirs.
        track.events = byEntity.get(track.entity_id).sort((a, b) => parseTime(a.at) - parseTime(b.at));
        track.recorded = { initial: track.initial, events: track.events };
        this._prepareTrack(track);
      }
    } catch (e) {
      if (token !== this._requestToken) return;
      this._error = `Could not load history: ${e.message || e}`;
    }
    this._loading = null;
    this._render();
  }

  _prepareTrack(track) {
    Object.assign(track, this._derive(track));
    track.edited = timelineEdited(track, track.recorded);
  }

  /* What a track draws, from a timeline (the track's own, or a drag preview). */
  _derive(timeline) {
    const { start, end } = this._window;
    const segments = buildSegments(timeline.initial, timeline.events, start, end);
    const numeric = isNumericStates(segments.map((s) => s.state));
    return {
      segments,
      numeric,
      merged: numeric ? segments : mergeSegments(segments),
      range: numeric ? numericRange(segments) : null,
    };
  }

  _setWindow(start, end, preset = null) {
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      this._error = "The window must end after it starts.";
      return this._render();
    }
    if (end - start > MAX_WINDOW) {
      this._error = "The window can be at most 7 days long.";
      return this._render();
    }
    this._preset = preset;
    this._window = { start, end };
    this._view = [start, end];
    this._clearResult();
    if (this._tracks.some((t) => t.edited)) {
      this._notice = "The window changed, so every track was reloaded from history and the timeline edits were dropped.";
    }
    this._clearEdits();
    this._requestToken++;
    if (this._automation) this._loadHistory();
    else this._render();
  }

  _addTrack(entityId) {
    const id = String(entityId || "").trim().toLowerCase();
    if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test(id)) {
      this._notice = `“${entityId}” is not an entity id.`;
      return this._render();
    }
    if (this._tracks.some((t) => t.entity_id === id)) {
      this._notice = `${id} already has a track.`;
      return this._render();
    }
    if (this._tracks.length >= MAX_ENTITIES) {
      this._notice = `At most ${MAX_ENTITIES} tracks.`;
      return this._render();
    }
    this._notice = null;
    const input = this.shadowRoot.getElementById("add-track");
    if (input) input.value = "";
    this._tracks.push({ entity_id: id, initial: null, events: [] });
    this._markStale();
    this._loadHistory([id]);
  }

  _removeTrack(entityId) {
    this._tracks = this._tracks.filter((t) => t.entity_id !== entityId);
    if (this._sel?.entity_id === entityId) this._select(null);
    this._markStale();
    this._render();
  }

  // ---- timeline edits -------------------------------------------------------

  _clearEdits() {
    this._undo.clear();
    this._sel = null;
    this._selT = null;
    this._edDraft = null;
    this._edError = null;
    this._evDrag = null;
  }

  _track(entityId) {
    return this._tracks.find((t) => t.entity_id === entityId) || null;
  }

  _select(sel, t = null) {
    const key = sel ? `${sel.entity_id}#${sel.index}` : null;
    if (key !== this._edDraft?.key) {
      this._edDraft = null;
      this._edError = null;
    }
    this._sel = sel;
    this._selT = sel ? t : null;
  }

  /* Replace tracks' timelines as one undoable edit. `changes` is [{entity_id, timeline}];
   * `select` is what to show in the editor afterwards. */
  _commit(label, changes, select = undefined) {
    const snapshots = [];
    for (const { entity_id, timeline } of changes) {
      const track = this._track(entity_id);
      if (!track || (timeline.initial === track.initial && timeline.events === track.events)) continue;
      snapshots.push({ entity_id, timeline: { initial: track.initial, events: track.events } });
      track.initial = timeline.initial;
      track.events = timeline.events;
      this._prepareTrack(track);
    }
    if (!snapshots.length) return false;
    this._undo.push({ label, snapshots });
    this._markStale();
    if (select !== undefined) this._select(select, select ? this._selT : null);
    this._edDraft = null;
    this._edError = null;
    this._render();
    return true;
  }

  _undoEdit() {
    const entry = this._undo.pop();
    if (!entry) return;
    for (const { entity_id, timeline } of entry.snapshots) {
      const track = this._track(entity_id);
      if (!track) continue;
      track.initial = timeline.initial;
      track.events = timeline.events;
      this._prepareTrack(track);
    }
    // Indexes may point elsewhere now; keep the selection only if it still exists.
    const sel = this._sel && this._track(this._sel.entity_id);
    if (sel && this._sel.index >= sel.events.length) this._select(null);
    this._edDraft = null;
    this._markStale();
    this._notice = `Undid: ${entry.label}.`;
    this._render();
  }

  _resetTracks(entityIds) {
    const changes = entityIds
      .map((id) => this._track(id))
      .filter((t) => t?.edited)
      .map((t) => ({ entity_id: t.entity_id, timeline: t.recorded }));
    const label = entityIds.length === 1 ? `reset ${entityIds[0]}` : "reset all tracks";
    const notice = this._notice;
    this._notice = `Back to recorded history (${label}). Undo brings the edits back.`;
    if (!this._commit(label, changes, null)) this._notice = notice;
  }

  _editOpts() {
    return { start: this._window.start, end: this._window.end };
  }

  _addChange(entityId, t) {
    const track = this._track(entityId);
    if (!track?.segments) return;
    const { timeline, index } = insertEvent(track, entityId, t, this._editOpts());
    this._commit("add change", [{ entity_id: entityId, timeline }], { entity_id: entityId, index });
  }

  _deleteSelected() {
    const sel = this._sel;
    const track = sel && this._track(sel.entity_id);
    if (!track || sel.index < 0 || !track.events[sel.index]) return;
    const timeline = deleteEvent(track, sel.index);
    // Select whatever is in effect where the deleted change was, so Delete can repeat.
    this._commit("delete change", [{ entity_id: sel.entity_id, timeline }], { entity_id: sel.entity_id, index: sel.index - 1 });
  }

  /* Apply the editor's fields to the selected event or initial state. */
  _applyEditor() {
    const sel = this._sel;
    const track = sel && this._track(sel.entity_id);
    if (!track) return;
    const root = this.shadowRoot;
    const state = root.getElementById("ed-state")?.value ?? "";
    const attrs = parseAttributes(root.getElementById("ed-attrs")?.value);
    const fail = (message) => {
      this._edError = message;
      this._render();
    };
    if (!state.trim()) return fail("The state cannot be empty.");
    if (!attrs.ok) return fail(attrs.error);
    if (sel.index < 0) {
      const timeline = setInitial(track, { state: state.trim(), attributes: attrs.value }, this._window.start);
      return this._commit("edit initial state", [{ entity_id: sel.entity_id, timeline }], sel);
    }
    const ev = track.events[sel.index];
    let timeline = track;
    let index = sel.index;
    const timeInput = root.getElementById("ed-time");
    // The field shows whole seconds; only a changed value moves the event.
    if (timeInput && timeInput.value !== toLocalInputSeconds(parseTime(ev.at))) {
      try {
        ({ timeline, index } = moveEvent(timeline, index, fromLocalInput(timeInput.value), this._editOpts()));
      } catch (e) {
        return fail(`${e.message} (${this._fullTime(this._window.start)} to ${this._fullTime(this._window.end)}).`);
      }
    }
    const moved = timeline.events[index];
    if (String(moved.state) !== state.trim() || stableStringify(moved.attributes || {}) !== stableStringify(attrs.value)) {
      timeline = updateEvent(timeline, index, { state: state.trim(), attributes: attrs.value });
    }
    if (timeline === track) {
      this._edDraft = null;
      return this._render();
    }
    this._commit("edit change", [{ entity_id: sel.entity_id, timeline }], { entity_id: sel.entity_id, index });
  }

  /* Step the selection to the previous/next change on the same track. */
  _stepChange(delta) {
    const sel = this._sel;
    const track = sel && this._track(sel.entity_id);
    if (!track) return;
    const index = sel.index + delta;
    if (index < -1 || index >= track.events.length) return;
    this._select({ entity_id: sel.entity_id, index });
    const t = index >= 0 ? parseTime(track.events[index].at) : this._window.start;
    const [t0, t1] = this._view;
    if (t < t0 || t > t1) {
      const half = (t1 - t0) / 2;
      this._view = clampView(t - half, t + half, ...this._bounds());
    }
    this._render();
  }

  // ---- automation config ------------------------------------------------------

  _savedConfig() {
    return this._automation?.config || {};
  }

  /* The config Run sends: the edited one when there is a valid edit. */
  _runConfig() {
    return this._cfgDraft ?? this._savedConfig();
  }

  _onConfigValue(value, valid, error) {
    const before = [this._cfgDraft !== null, !!this._cfgError];
    if (!valid) {
      this._cfgError = error || "The YAML does not parse.";
    } else {
      const check = checkAutomationConfig(value);
      this._cfgError = check.ok ? null : check.error;
      if (check.ok) this._cfgDraft = stableStringify(value) === stableStringify(this._savedConfig()) ? null : value;
    }
    this._markStale();
    this._updateConfigStatus();
    // Run's enabled state and hint live in the main area; redraw it only on a change of state.
    if (before[0] !== (this._cfgDraft !== null) || before[1] !== !!this._cfgError) this._render();
  }

  _revertConfig() {
    this._cfgDraft = null;
    this._cfgError = null;
    this._renderConfig();
    this._markStale();
    this._render();
  }

  /* The config card is drawn apart from the rest so the editor (and its undo history,
   * cursor and scroll) survives every other re-render. Redrawn only when the automation
   * changes, the editor finishes loading, or the config is reverted. */
  _renderConfig() {
    const host = this.shadowRoot.getElementById("config");
    if (!host) return;
    this._cfgEl = null;
    if (!this._automation) {
      host.innerHTML = "";
      return;
    }
    const mode =
      this._yamlState === "ready"
        ? "YAML"
        : this._yamlState === "loading"
          ? "Loading the YAML editor…"
          : "JSON (Home Assistant's YAML editor could not be loaded here, so the config is shown as JSON, which is also valid YAML)";
    host.innerHTML = `
      <ha-card>
        <div class="card-content">
          <div class="cfg-head">
            <h2>Automation config</h2>
            <span id="cfg-badge"></span>
          </div>
          <div class="alert info"><div><strong>For what-ifs only.</strong> Edits here change what the simulator runs and nothing else. They are never saved to Home Assistant; the real automation stays as it is.</div></div>
          <div class="muted small cfg-mode">${esc(mode)}</div>
          <div id="cfg-editor"></div>
          <div id="cfg-error" class="err small" hidden></div>
          <div class="cfg-actions">
            <button data-action="cfg-revert" id="cfg-revert">Revert to saved automation</button>
            <span class="muted small" id="cfg-status"></span>
          </div>
        </div>
      </ha-card>`;
    const slot = host.querySelector("#cfg-editor");
    if (this._yamlState === "ready") {
      const el = document.createElement("ha-yaml-editor");
      el.hass = this._hass;
      el.defaultValue = this._runConfig();
      el.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        this._onConfigValue(ev.detail?.value, ev.detail?.isValid !== false, ev.detail?.errorMsg);
      });
      slot.appendChild(el);
      this._cfgEl = el;
    } else if (this._yamlState === "unavailable") {
      const el = document.createElement("textarea");
      el.id = "cfg-text";
      el.className = "mono cfg-text";
      el.spellcheck = false;
      el.value = JSON.stringify(this._runConfig(), null, 2);
      el.rows = Math.min(30, Math.max(8, el.value.split("\n").length + 1));
      el.addEventListener("input", () => {
        let value;
        try {
          value = JSON.parse(el.value);
        } catch (e) {
          return this._onConfigValue(null, false, `Not valid JSON: ${e.message}`);
        }
        this._onConfigValue(value, true);
      });
      slot.appendChild(el);
      this._cfgEl = el;
    }
    this._updateConfigStatus();
  }

  _updateConfigStatus() {
    const root = this.shadowRoot;
    const badge = root.getElementById("cfg-badge");
    if (!badge) return;
    const edited = this._cfgDraft !== null;
    badge.className = `chip ${this._cfgError ? "fail" : edited ? "edited" : "none"}`;
    badge.textContent = this._cfgError ? "invalid" : edited ? "edited" : "as saved";
    const error = root.getElementById("cfg-error");
    error.hidden = !this._cfgError;
    error.textContent = this._cfgError || "";
    root.getElementById("cfg-revert").disabled = !edited && !this._cfgError;
    root.getElementById("cfg-status").textContent = this._cfgError
      ? "Run is off until the config parses."
      : edited
        ? "Run uses this edited config."
        : "Run uses the automation as saved in Home Assistant.";
  }

  _clearResult() {
    this._result = null;
    this._runError = null;
    this._selectedRun = null;
    this._resultStale = false;
  }

  _markStale() {
    if (this._result) this._resultStale = true;
  }

  async _run() {
    if (!this._automation || this._running || this._cfgError) return;
    const scenario = buildScenario({
      automation: this._runConfig(),
      start: this._window.start,
      end: this._window.end,
      tracks: this._tracks,
    });
    this._running = true;
    this._runError = null;
    this._render();
    try {
      const raw = await this._hass.callWS({ type: "event_simulator/simulate", scenario });
      if (raw && raw.error && !Array.isArray(raw.runs)) {
        this._runError = { message: String(raw.error), stderr: raw.stderr_tail || "" };
      } else {
        this._result = normalizeResult(raw);
        this._resultStale = false;
        this._selectedRun = this._result.runs[0]?.index ?? null;
      }
    } catch (e) {
      this._runError =
        e?.code === "unknown_command"
          ? {
              message:
                "This build of the integration cannot run simulations yet: the backend has no event_simulator/simulate command. Update the integration and restart Home Assistant.",
              code: e.code,
            }
          : { message: e?.message || String(e), code: e?.code || null, stderr: e?.stderr_tail || "" };
    }
    this._running = false;
    this._render();
  }

  // ---- rendering ----------------------------------------------------------

  _render() {
    if (!this._rendered) {
      this.shadowRoot.innerHTML = `<style>${this._css()}</style><div class="page"><div id="root"></div><div id="detail"></div><div id="config"></div></div>`;
      this._rendered = true;
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
      this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
      this.shadowRoot.addEventListener("keydown", (ev) => this._onKeydown(ev));
    }
    const root = this.shadowRoot.getElementById("root");
    const addValue = root.querySelector("#add-track")?.value || "";
    // The timeline keeps keyboard focus across a redraw, so Delete and Ctrl+Z keep working.
    const timelineFocused = this.shadowRoot.activeElement?.id === "tl-wrap";
    root.innerHTML = this._html();
    this.shadowRoot.getElementById("detail").innerHTML = this._detailCard();
    const add = root.querySelector("#add-track");
    if (add) add.value = addValue;
    const menu = root.querySelector("ha-menu-button");
    if (menu) {
      menu.hass = this._hass;
      menu.narrow = this._narrow;
    }
    this._bindTimeline();
    this._renderTimeline();
    if (timelineFocused) this.shadowRoot.getElementById("tl-wrap")?.focus({ preventScroll: true });
  }

  _html() {
    return `
      <div class="header">
        ${this._narrow ? `<ha-menu-button></ha-menu-button>` : ""}
        <h1>Event Simulator</h1>
      </div>
      ${this._setupCard()}
      ${this._timelineCard()}`;
  }

  _setupCard() {
    const autos = this._automations;
    const picker =
      autos === null
        ? `<div class="muted">Loading automations…</div>`
        : autos.length === 0
          ? `<div class="muted">No automations found on this instance.</div>`
          : `
            <input id="auto-filter" type="search" placeholder="Search automations…" value="${esc(this._autoFilter)}" autocomplete="off">
            <select id="automation" size="1">${this._automationOptions()}</select>`;
    const w = this._window;
    const presets = PRESETS.map(
      (p) => `<button class="${this._preset === p.id ? "on" : ""}" data-action="preset" data-preset="${p.id}">${p.label}</button>`
    ).join("");
    return `
      <ha-card>
        <div class="card-content setup">
          <div class="field grow">
            <span class="label">Automation</span>
            <span class="picker">${picker}</span>
          </div>
          <div class="field">
            <span class="label">Window</span>
            <div class="window">
              <input id="win-start" type="datetime-local" value="${toLocalInput(w.start)}" aria-label="Window start">
              <span class="muted">to</span>
              <input id="win-end" type="datetime-local" value="${toLocalInput(w.end)}" aria-label="Window end">
            </div>
            <div class="segmented">${presets}</div>
          </div>
          <div class="run-row">
            <button class="primary" data-action="run" ${!this._automation || this._running || this._loading || this._cfgError ? "disabled" : ""}>
              ${this._running ? "Running…" : "Run"}
            </button>
            <span class="muted small">${esc(this._runHint())}</span>
          </div>
        </div>
      </ha-card>`;
  }

  _runHint() {
    if (this._running) return "Replaying the window in the sandbox…";
    if (!this._entityId) return "Pick an automation to load its entities.";
    if (!this._automation) return "";
    if (this._cfgError) return "The edited automation config does not parse; fix it or revert it below.";
    const events = this._tracks.reduce((n, t) => n + t.events.length, 0);
    const edited = this._tracks.filter((t) => t.edited).length;
    const what = [
      edited ? `${edited} edited track${edited === 1 ? "" : "s"}` : "",
      this._cfgDraft ? "the edited automation config" : "",
    ].filter(Boolean);
    return `Replays ${events} state change${events === 1 ? "" : "s"} on ${this._tracks.length} track${this._tracks.length === 1 ? "" : "s"}${what.length ? `, with ${what.join(" and ")}` : ""}. Service calls are recorded, never made.`;
  }

  _automationOptions() {
    const needle = this._autoFilter.trim().toLowerCase();
    const list = (this._automations || []).filter(
      (a) => !needle || a.alias.toLowerCase().includes(needle) || a.entity_id.includes(needle) || a.entity_id === this._entityId
    );
    const opts = list
      .slice()
      .sort((a, b) => a.alias.localeCompare(b.alias))
      .map(
        (a) =>
          `<option value="${esc(a.entity_id)}" ${a.entity_id === this._entityId ? "selected" : ""}>${esc(a.alias)}${a.alias !== a.entity_id ? ` — ${esc(a.entity_id)}` : ""}</option>`
      )
      .join("");
    const placeholder = `<option value="" ${this._entityId ? "" : "selected"}>${list.length ? "Choose an automation…" : "No match"}</option>`;
    return placeholder + opts;
  }

  _timelineCard() {
    const banner = [
      this._error ? `<div class="alert error">${esc(this._error)}</div>` : "",
      this._notice ? `<div class="alert info">${esc(this._notice)}</div>` : "",
      this._loading ? `<div class="alert info"><span class="spinner"></span>${esc(this._loading)}</div>` : "",
      this._runErrorHtml(),
    ].join("");
    if (!this._entityId) {
      return `<ha-card><div class="card-content">${banner}<div class="empty">Choose an automation above. Its triggers, conditions and templates decide which entities get a track.</div></div></ha-card>`;
    }
    const entityOptions = this._entityDatalist();
    return `
      <ha-card>
        <div class="card-content">
          ${banner}
          <div class="tl-toolbar">
            <div class="zoom">
              <button data-action="zoom-out" title="Zoom out" aria-label="Zoom out">−</button>
              <button data-action="zoom-in" title="Zoom in" aria-label="Zoom in">+</button>
              <button data-action="zoom-reset" title="Show the whole window">Fit</button>
              <span class="muted small" id="span-label"></span>
            </div>
            <div class="edits">
              <button data-action="undo" ${this._undo.size ? "" : "disabled"} title="${esc(this._undo.peek() ? `Undo: ${this._undo.peek().label}` : "Nothing to undo")}">Undo</button>
              <button data-action="reset-all" ${this._tracks.some((t) => t.edited) ? "" : "disabled"} title="Put every track back to its recorded history">Reset all</button>
            </div>
            <div class="add">
              <input id="add-track" list="entity-ids" placeholder="Add track: entity id" autocomplete="off" spellcheck="false">
              <datalist id="entity-ids">${entityOptions}</datalist>
              <button data-action="add-track">Add</button>
            </div>
          </div>
          <div class="muted small hint">Drag to pan; Ctrl/⌘ + wheel or wheel over the axis zooms. Drag a change mark to move it in time. Click (tap) a state to edit it or add a change there; double-click (double-tap) adds one directly.</div>
          <div id="tl-wrap" tabindex="0">
            <div id="tl"></div>
            <div id="cursor" hidden></div>
            <div id="tip" hidden></div>
          </div>
          ${this._editorHtml()}
          ${this._tracks.length === 0 && !this._loading
            ? `<div class="empty">This automation references no entities (a time pattern, say). Add tracks above, or run it as is.</div>`
            : ""}
        </div>
      </ha-card>`;
  }

  _entityDatalist() {
    const ids = Object.keys(this._hass?.states || {}).sort();
    const have = new Set(this._tracks.map((t) => t.entity_id));
    return ids
      .filter((id) => !have.has(id))
      .map((id) => {
        const name = this._hass.states[id]?.attributes?.friendly_name;
        return `<option value="${esc(id)}">${name ? esc(name) : ""}</option>`;
      })
      .join("");
  }

  _runErrorHtml() {
    const e = this._runError;
    if (!e) return "";
    const stderr = e.stderr ? `<details><summary>Sandbox output</summary><pre>${esc(e.stderr)}</pre></details>` : "";
    return `<div class="alert error"><div><strong>Run failed.</strong> ${esc(e.message)}${e.code && e.code !== "unknown_command" ? ` <span class="mono small">(${esc(e.code)})</span>` : ""}${stderr}</div></div>`;
  }

  /* The inline editor for the selected event (or initial state) of a track. */
  _editorHtml() {
    const sel = this._sel;
    const track = sel && this._track(sel.entity_id);
    if (!track?.segments) return "";
    const isInitial = sel.index < 0;
    const item = isInitial ? track.initial : track.events[sel.index];
    if (!isInitial && !item) return "";
    const key = `${sel.entity_id}#${sel.index}`;
    const draft = this._edDraft?.key === key ? this._edDraft : null;
    const t = isInitial ? this._window.start : parseTime(item.at);
    const timeValue = draft?.time ?? (isInitial ? "" : toLocalInputSeconds(t));
    const stateValue = draft?.state ?? (item ? String(item.state) : "");
    const attrsValue = draft?.attrs ?? (item && Object.keys(item.attributes || {}).length ? JSON.stringify(item.attributes, null, 2) : "");
    const known = [...new Set([track.initial, ...track.events].filter(Boolean).map((e) => String(e.state)))].slice(0, 200);
    const name = this._friendlyName(sel.entity_id);
    const what = isInitial
      ? item
        ? "State at the window start"
        : "No recorded state at the window start. Set one to seed it."
      : `Change ${sel.index + 1} of ${track.events.length}`;
    const tag = item ? (item.edited ? `<span class="chip edited">edited</span>` : `<span class="chip none">recorded</span>`) : "";
    const addT = this._selT ?? (isInitial ? null : t);
    return `
      <div class="editor" id="editor">
        <div class="editor-head">
          <button class="icon" data-action="ed-prev" ${sel.index <= -1 ? "disabled" : ""} aria-label="Previous change" title="Previous change">‹</button>
          <button class="icon" data-action="ed-next" ${sel.index >= track.events.length - 1 ? "disabled" : ""} aria-label="Next change" title="Next change">›</button>
          <strong>${esc(name || sel.entity_id)}</strong>
          <span class="muted small">${esc(what)}</span>
          ${tag}
          <button class="icon close" data-action="ed-close" aria-label="Close editor" title="Close (Esc)">×</button>
        </div>
        <div class="editor-fields">
          ${isInitial
            ? `<label class="fld"><span class="label">Time</span><span class="mono small fixed">${esc(this._fullTime(t))} (window start)</span></label>`
            : `<label class="fld"><span class="label">Time</span><input id="ed-time" type="datetime-local" step="1" value="${esc(timeValue)}"></label>`}
          <label class="fld"><span class="label">State</span><input id="ed-state" class="mono" list="ed-states" value="${esc(stateValue)}" autocomplete="off" spellcheck="false"></label>
          <datalist id="ed-states">${known.map((s) => `<option value="${esc(s)}"></option>`).join("")}</datalist>
          <label class="fld grow"><span class="label">Attributes (JSON)</span><textarea id="ed-attrs" class="mono" rows="${Math.min(8, Math.max(2, attrsValue.split("\n").length))}" spellcheck="false" placeholder="{}">${esc(attrsValue)}</textarea></label>
        </div>
        ${this._edError ? `<div class="err small">${esc(this._edError)}</div>` : ""}
        <div class="editor-actions">
          <button class="primary small-btn" data-action="ed-apply">${isInitial && !item ? "Set initial state" : "Apply"}</button>
          ${isInitial ? "" : `<button data-action="ed-delete" title="Delete this change (Del)">Delete change</button>`}
          ${Number.isFinite(addT) ? `<button data-action="ed-add" data-t="${addT}">Add change at ${esc(this._clockTime(addT))}</button>` : ""}
          ${track.edited ? `<button data-action="reset-track" data-entity="${esc(sel.entity_id)}">Reset track to recorded</button>` : ""}
        </div>
      </div>`;
  }

  _clockTime(t) {
    return new Date(t).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  _friendlyName(entityId) {
    return this._hass?.states?.[entityId]?.attributes?.friendly_name || "";
  }

  _unit(track) {
    const attrs = track.segments?.[track.segments.length - 1]?.attributes || this._hass?.states?.[track.entity_id]?.attributes || {};
    return attrs.unit_of_measurement || "";
  }

  /* Only the timeline area: called on every zoom/pan/resize without rebuilding the form. */
  _renderTimeline() {
    const tl = this.shadowRoot.getElementById("tl");
    if (!tl) return;
    const wrap = this.shadowRoot.getElementById("tl-wrap");
    this._width = Math.max(200, Math.floor(wrap.clientWidth || this._width));
    const [t0, t1] = this._view;
    const scale = makeScale(t0, t1, this._width);
    const label = this.shadowRoot.getElementById("span-label");
    if (label) label.textContent = `${formatDuration(t1 - t0)} visible`;
    const parts = [this._axisSvg(scale)];
    if (this._result || this._running) parts.push(this._resultsRow(scale));
    for (const track of this._tracks) parts.push(this._trackRow(track, scale));
    tl.innerHTML = parts.join("");
  }

  _axisSvg(scale) {
    const { step, ticks } = timeTicks(scale.t0, scale.t1, scale.width);
    const marks = ticks
      .map((t) => {
        const x = scale.toX(t);
        const text = this._tickLabel(t, step);
        // A label that would run off the right edge is left out rather than cut in half.
        const label = x + 3 + text.length * 6 <= scale.width ? `<text x="${x + 3}" y="${AXIS_H - 9}" class="axis-label">${esc(text)}</text>` : "";
        return `<line x1="${x}" x2="${x}" y1="${AXIS_H - 6}" y2="${AXIS_H}" class="tick"/>${label}`;
      })
      .join("");
    return `<div class="row axis-row"><svg class="axis" data-row="axis" width="${scale.width}" height="${AXIS_H}">${marks}
      <line x1="0" x2="${scale.width}" y1="${AXIS_H - 0.5}" y2="${AXIS_H - 0.5}" class="baseline"/></svg></div>`;
  }

  _tickLabel(t, step) {
    const d = new Date(t);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0;
    if (step >= DAY || (midnight && step >= HOUR)) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const opts = { hour: "2-digit", minute: "2-digit" };
    if (step < 60e3) opts.second = "2-digit";
    return d.toLocaleTimeString(undefined, opts);
  }

  _gridLines(scale, height) {
    const { ticks } = timeTicks(scale.t0, scale.t1, scale.width);
    return ticks.map((t) => `<line x1="${scale.toX(t)}" x2="${scale.toX(t)}" y1="0" y2="${height}" class="grid"/>`).join("");
  }

  _hatch(id) {
    return `<defs><pattern id="${id}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <rect width="6" height="6" class="missing-bg"/><line x1="0" y1="0" x2="0" y2="6" class="missing-line"/></pattern></defs>`;
  }

  _trackRow(track, scale) {
    const name = this._friendlyName(track.entity_id);
    const unit = this._unit(track);
    // While a change mark is dragged, the track draws the would-be timeline.
    const preview = this._evDrag?.entity_id === track.entity_id ? this._evDrag.preview : null;
    const view = preview?.timeline && track.segments ? { ...track, ...preview.timeline, ...this._derive(preview.timeline) } : track;
    let meta = "";
    if (!track.segments) meta = "loading…";
    else if (!track.segments.length) meta = "no history in this window";
    else if (track.range) meta = `min ${track.range.min} · max ${track.range.max}${unit ? ` ${esc(unit)}` : ""}`;
    else meta = `${track.events.length} change${track.events.length === 1 ? "" : "s"}`;
    const editedCount = track.edited ? track.events.filter((e) => e.edited).length + (track.initial?.edited ? 1 : 0) : 0;
    const height = view.numeric ? NUMERIC_H : DISCRETE_H;
    const body = view.segments
      ? `${view.numeric ? this._numericSvg(view, scale) : this._discreteSvg(view, scale)}${this._marksSvg(view, scale, height)}`
      : "";
    return `
      <div class="row">
        <div class="track-head">
          <span class="name">${esc(name || track.entity_id)}</span>
          ${name ? `<span class="eid mono">${esc(track.entity_id)}</span>` : ""}
          ${track.edited ? `<span class="chip edited" title="${editedCount} edited item${editedCount === 1 ? "" : "s"}; deleted changes are not counted">edited</span>
            <button class="link" data-action="reset-track" data-entity="${esc(track.entity_id)}" title="Put this track back to its recorded history">Reset</button>` : ""}
          <span class="meta">${meta}</span>
          <button class="icon" data-action="remove-track" data-entity="${esc(track.entity_id)}" title="Remove track" aria-label="Remove ${esc(track.entity_id)}">×</button>
        </div>
        <svg class="track${track.edited ? " is-edited" : ""}" data-row="track" data-entity="${esc(track.entity_id)}" width="${scale.width}" height="${height}">
          ${this._hatch(`h-${hashString(track.entity_id)}`)}
          ${this._gridLines(scale, height)}
          ${body}
        </svg>
      </div>`;
  }

  /* Change marks: a tick per state write (the drag handles), edited ones in the accent
   * color with a flag on top, and an outline around the selected change's span. On a
   * crowded track only edited and selected marks are drawn; dragging still finds the
   * nearest change. */
  _marksSvg(track, scale, height) {
    const { start, end } = this._window;
    const events = track.events;
    const sel = this._sel?.entity_id === track.entity_id ? this._sel.index : null;
    let out = "";
    if (sel !== null) {
      const from = sel < 0 ? start : parseTime(events[sel]?.at);
      const to = sel + 1 < events.length ? parseTime(events[sel + 1].at) : end;
      if (Number.isFinite(from) && to > scale.t0 && from < scale.t1) {
        const x1 = Math.max(0, scale.toX(from));
        const x2 = Math.min(scale.width, scale.toX(to));
        out += `<rect x="${x1 + 0.75}" y="0.75" width="${Math.max(1, x2 - x1 - 1.5)}" height="${height - 1.5}" rx="2" class="sel-span"/>`;
      }
    }
    if (track.initial?.edited && start >= scale.t0) out += `<path d="M0,0L8,0L0,8Z" class="ev-flag"/>`;
    const i0 = countAtOrBefore(events, scale.t0 - 1);
    const i1 = countAtOrBefore(events, scale.t1);
    const crowded = i1 - i0 > scale.width / 6;
    let lastX = -Infinity;
    for (let i = i0; i < i1; i++) {
      const ev = events[i];
      const selected = i === sel;
      if (crowded && !ev.edited && !selected) continue;
      const x = Math.round(scale.toX(parseTime(ev.at)) * 10) / 10;
      if (!ev.edited && !selected && x - lastX < 2) continue;
      lastX = x;
      out += `<line x1="${x}" x2="${x}" y1="0" y2="${height}" class="ev${ev.edited ? " edited" : ""}${selected ? " sel" : ""}"/>`;
      if (ev.edited) out += `<path d="M${x - 4},0L${x + 4},0L${x},6Z" class="ev-flag"/>`;
    }
    return out;
  }

  _discreteSvg(track, scale) {
    const hatch = `url(#h-${hashString(track.entity_id)})`;
    let out = "";
    let lastX = -Infinity;
    for (const seg of visibleSegments(track.merged, scale.t0, scale.t1)) {
      const x1 = Math.max(0, scale.toX(seg.from));
      const x2 = Math.min(scale.width, scale.toX(seg.to));
      const w = x2 - x1;
      // A run of sub-pixel segments would paint the same column many times; one is enough.
      if (w < 1 && x1 - lastX < 1) continue;
      lastX = x1;
      const color = stateColor(seg.state);
      const fill = color ? `fill="${color}" class="seg"` : `fill="${hatch}" class="seg missing"`;
      out += `<rect x="${x1}" y="2" width="${Math.max(w, 1)}" height="${DISCRETE_H - 4}" rx="2" ${fill}/>`;
      const text = String(seg.state);
      const room = Math.floor((w - 8) / 6.5);
      if (room >= 3) {
        const shown = text.length <= room ? text : `${text.slice(0, room - 1)}…`;
        out += `<text x="${x1 + 4}" y="${DISCRETE_H / 2 + 4}" class="seg-label">${esc(shown)}</text>`;
      }
    }
    return out;
  }

  _numericSvg(track, scale) {
    const hatch = `url(#h-${hashString(track.entity_id)})`;
    const { min, max } = track.range;
    const pad = 6;
    const span = max - min;
    const toY = (v) => (span === 0 ? NUMERIC_H / 2 : pad + (1 - (v - min) / span) * (NUMERIC_H - 2 * pad));
    const visible = visibleSegments(track.segments, scale.t0, scale.t1);
    const clampX = (t) => Math.min(scale.width + 1, Math.max(-1, scale.toX(t)));
    let missing = "";
    for (const seg of visible) {
      if (!Number.isNaN(toNumber(seg.state))) continue;
      const x1 = clampX(seg.from);
      const w = Math.max(1, clampX(seg.to) - x1);
      missing += `<rect x="${x1}" y="0" width="${w}" height="${NUMERIC_H}" fill="${hatch}" class="missing"/>`;
    }
    const d = stepPath(visible, clampX, toY);
    return `${missing}
      <text x="4" y="11" class="range-label">${esc(max)}</text>
      <text x="4" y="${NUMERIC_H - 3}" class="range-label">${esc(min)}</text>
      <path d="${d}" class="line"/>`;
  }

  _resultsRow(scale) {
    if (!this._result) {
      return `<div class="row"><div class="track-head"><span class="name">Runs</span><span class="meta">running…</span></div>
        <svg class="results" width="${scale.width}" height="${RESULTS_H}"></svg></div>`;
    }
    const { runs, calls } = this._result;
    const counts = OUTCOMES.map((o) => [o, runs.filter((r) => r.outcome === o).length]).filter(([, n]) => n);
    const summary = runs.length
      ? counts.map(([o, n]) => `<span class="dot" style="background:${OUTCOME_COLOR[o]}"></span>${n} ${OUTCOME_LABEL[o].toLowerCase()}`).join(" · ")
      : "no runs";
    const stale = this._resultStale ? ` <span class="stale">inputs changed since this run</span>` : "";
    let marks = "";
    for (const run of runs) {
      if (run.finished < scale.t0 || run.started > scale.t1) continue;
      const x = scale.toX(run.started);
      const x2 = scale.toX(run.finished);
      const color = OUTCOME_COLOR[run.outcome];
      if (x2 - x > 2) marks += `<rect x="${x}" y="9" width="${x2 - x}" height="6" rx="3" fill="${color}" opacity=".35"/>`;
      if (run.index === this._selectedRun) marks += `<circle cx="${x}" cy="12" r="9" class="selected"/>`;
      marks += this._marker(run.outcome, x, 12, color);
    }
    for (const call of calls) {
      if (call.t < scale.t0 || call.t > scale.t1) continue;
      const x = scale.toX(call.t);
      marks += `<line x1="${x}" x2="${x}" y1="25" y2="${RESULTS_H - 2}" class="call-tick"/>`;
    }
    return `
      <div class="row results-row">
        <div class="track-head"><span class="name">Runs</span><span class="meta">${summary} · ${calls.length} service call${calls.length === 1 ? "" : "s"}${stale}</span></div>
        <svg class="results" data-row="results" width="${scale.width}" height="${RESULTS_H}">
          ${this._gridLines(scale, RESULTS_H)}${marks}
        </svg>
      </div>`;
  }

  /* Shape as well as color, so outcomes stay distinguishable without color vision. */
  _marker(outcome, x, y, color) {
    if (outcome === "fired") return `<circle cx="${x}" cy="${y}" r="5" fill="${color}" class="marker"/>`;
    if (outcome === "condition_failed") return `<circle cx="${x}" cy="${y}" r="4.5" fill="var(--card-background-color, #fff)" stroke="${color}" stroke-width="2" class="marker"/>`;
    if (outcome === "error") return `<path d="M${x},${y - 6}L${x + 6},${y}L${x},${y + 6}L${x - 6},${y}Z" fill="${color}" class="marker"/>`;
    return `<rect x="${x - 4}" y="${y - 4}" width="8" height="8" fill="none" stroke="${color}" stroke-width="2" class="marker"/>`;
  }

  _detailCard() {
    const result = this._result;
    if (!result) return "";
    const warnings = result.warnings.length
      ? `<div class="alert warning"><div><strong>Warnings</strong><ul>${result.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div></div>`
      : "";
    const run = result.runs.find((r) => r.index === this._selectedRun);
    if (!run) {
      const text = result.runs.length ? "Click a run marker on the Runs row to see what happened." : "The automation did not run in this window.";
      return `<ha-card><div class="card-content">${warnings}<div class="empty">${text}</div></div></ha-card>`;
    }
    const pos = result.runs.indexOf(run);
    const conditions = conditionResults(run.trace);
    const calls = callsForRun(result.calls, run);
    const condHtml = conditions.length
      ? `<ul class="conds">${conditions
          .map(
            (c) => `<li style="padding-left:${c.depth * 16}px">
              <span class="chip ${c.result === true ? "pass" : c.result === false ? "fail" : "none"}">${c.result === true ? "pass" : c.result === false ? "fail" : "—"}</span>
              <span class="mono">${esc(c.path)}</span>${c.error ? ` <span class="err">${esc(c.error)}</span>` : ""}</li>`
          )
          .join("")}</ul>`
      : `<div class="muted small">${run.trace ? "No conditions were evaluated." : "The result has no trace for this run."}</div>`;
    const callsHtml = calls.length
      ? `<div class="calls">${calls
          .map(
            (c) => `<div class="call">
              <span class="mono small muted">${esc(this._fullTime(c.t))}</span>
              <span class="mono"><strong>${esc(c.domain)}.${esc(c.service)}</strong></span>
              ${c.target ? `<span class="mono small">target ${esc(JSON.stringify(c.target))}</span>` : ""}
              ${c.data && Object.keys(c.data).length ? `<span class="mono small">data ${esc(JSON.stringify(c.data))}</span>` : ""}
            </div>`
          )
          .join("")}</div>`
      : `<div class="muted small">No service calls recorded for this run.</div>`;
    const raw = run.trace ?? run.raw;
    return `
      <ha-card>
        <div class="card-content detail">
          ${warnings}
          <div class="detail-head">
            <button class="icon" data-action="prev-run" ${pos <= 0 ? "disabled" : ""} aria-label="Previous run">‹</button>
            <strong>Run ${pos + 1} of ${result.runs.length}</strong>
            <button class="icon" data-action="next-run" ${pos >= result.runs.length - 1 ? "disabled" : ""} aria-label="Next run">›</button>
            <span class="chip" style="background:${OUTCOME_COLOR[run.outcome]};color:var(--text-primary-color, #fff)">${esc(run.outcomeLabel)}</span>
          </div>
          <dl>
            <dt>Started</dt><dd class="mono">${esc(this._fullTime(run.started))}</dd>
            <dt>Duration</dt><dd>${run.finished > run.started ? esc(formatDuration(run.finished - run.started)) : "instant"}</dd>
            <dt>Trigger</dt><dd>${esc(describeTrigger(run.trigger)) || `<span class="muted">not reported</span>`}</dd>
            ${run.stoppedBy ? `<dt>Stopped by</dt><dd class="mono">${esc(typeof run.stoppedBy === "string" ? run.stoppedBy : JSON.stringify(run.stoppedBy))}</dd>` : ""}
            ${run.error ? `<dt>Error</dt><dd class="err">${esc(typeof run.error === "string" ? run.error : JSON.stringify(run.error))}</dd>` : ""}
          </dl>
          <h3>Conditions</h3>
          ${condHtml}
          <h3>Recorded service calls</h3>
          ${callsHtml}
          <details class="raw">
            <summary>${run.trace ? "Raw trace" : "Raw run"} JSON</summary>
            <pre>${esc(JSON.stringify(raw, null, 2))}</pre>
          </details>
        </div>
      </ha-card>`;
  }

  _fullTime(t) {
    if (!Number.isFinite(t)) return "?";
    return new Date(t).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  }

  // ---- interaction ----------------------------------------------------------

  _bindTimeline() {
    const wrap = this.shadowRoot.getElementById("tl-wrap");
    if (!wrap) return;
    wrap.addEventListener("wheel", (ev) => this._onWheel(ev), { passive: false });
    wrap.addEventListener("pointerdown", (ev) => this._onPointerDown(ev));
    wrap.addEventListener("pointermove", (ev) => this._onPointerMove(ev));
    wrap.addEventListener("pointerup", (ev) => this._onPointerUp(ev));
    wrap.addEventListener("pointercancel", () => {
      this._drag = null;
      if (this._evDrag) {
        this._evDrag = null;
        this._scheduleTimeline();
      }
    });
    wrap.addEventListener("pointerleave", () => {
      if (!this._evDrag?.moved) this._hideHover();
    });
    this._observeWidth();
  }

  _observeWidth() {
    this._resizeObserver?.disconnect();
    const wrap = this.shadowRoot.getElementById("tl-wrap");
    if (!wrap || !globalThis.ResizeObserver) return;
    this._resizeObserver = new ResizeObserver(() => {
      if (Math.floor(wrap.clientWidth) !== this._width) this._scheduleTimeline();
    });
    this._resizeObserver.observe(wrap);
  }

  _scheduleTimeline() {
    cancelAnimationFrame(this._frame);
    this._frame = requestAnimationFrame(() => this._renderTimeline());
  }

  _setView(view) {
    this._view = view;
    this._scheduleTimeline();
  }

  _bounds() {
    return [this._window.start, this._window.end];
  }

  _localX(ev) {
    const rect = this.shadowRoot.getElementById("tl-wrap").getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  _onWheel(ev) {
    const overAxis = ev.target.closest?.("[data-row=axis]");
    const scale = makeScale(this._view[0], this._view[1], this._width);
    if (ev.ctrlKey || ev.metaKey || overAxis) {
      ev.preventDefault();
      const anchor = scale.toT(this._localX(ev).x);
      const factor = Math.exp(Math.max(-1, Math.min(1, ev.deltaY / 300)));
      this._setView(zoomView(this._view, anchor, factor, this._bounds()));
    } else if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
      ev.preventDefault();
      this._setView(panView(this._view, (ev.deltaX / this._width) * (this._view[1] - this._view[0]), this._bounds()));
    }
  }

  _scale() {
    return makeScale(this._view[0], this._view[1], this._width);
  }

  /* The timeline row SVG under a client point, found by geometry. Not elementFromPoint:
   * pointer capture retargets events to the wrapper, and inside Home Assistant's nested
   * shadow roots ShadowRoot.elementFromPoint answers with the document's <html>. */
  _rowAt(clientX, clientY) {
    for (const svg of this.shadowRoot.querySelectorAll("#tl svg[data-row]")) {
      const r = svg.getBoundingClientRect();
      if (clientX >= r.left && clientX < r.right && clientY >= r.top && clientY < r.bottom) return svg;
    }
    return null;
  }

  /* The track SVG under a pointer event, the track, and the time there. */
  _trackAt(ev) {
    const svg = this._rowAt(ev.clientX, ev.clientY);
    const track = svg?.dataset.row === "track" && this._track(svg.dataset.entity);
    if (!track?.segments) return null;
    const x = ev.clientX - svg.getBoundingClientRect().left;
    return { svg, track, x, t: this._scale().toT(x) };
  }

  /* The change mark within reach of the pointer on that track, as an event index. */
  _markHit(hit, ev) {
    const scale = this._scale();
    const { track, x, t } = hit;
    const reach = ev.pointerType === "touch" ? 14 : 6;
    const crowded = countAtOrBefore(track.events, scale.t1) - countAtOrBefore(track.events, scale.t0 - 1) > scale.width / 6;
    const i = eventIndexAt(track.events, t);
    const candidates = [i, i + 1]
      .filter((j) => j >= 0 && j < track.events.length)
      .filter((j) => !crowded || track.events[j].edited || (this._sel?.entity_id === track.entity_id && this._sel.index === j))
      .map((j) => ({ x: scale.toX(parseTime(track.events[j].at)), index: j }));
    return findNearest(candidates, x, reach)?.index ?? null;
  }

  _onPointerDown(ev) {
    if (ev.button !== 0 || !ev.target.closest?.("svg")) return;
    const hit = ev.target.closest("svg[data-row=track]") ? this._trackAt(ev) : null;
    const index = hit ? this._markHit(hit, ev) : null;
    if (index !== null) {
      const at = parseTime(hit.track.events[index].at);
      this._evDrag = { id: ev.pointerId, entity_id: hit.track.entity_id, index, x: ev.clientX, from: at, moved: false, preview: null };
    } else {
      this._drag = { x: ev.clientX, view: this._view, moved: false, id: ev.pointerId };
    }
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
  }

  _onPointerMove(ev) {
    const moving = this._evDrag;
    if (moving && moving.id === ev.pointerId) {
      const dx = ev.clientX - moving.x;
      if (Math.abs(dx) > 3) moving.moved = true;
      if (moving.moved) {
        const track = this._track(moving.entity_id);
        if (!track) return;
        const msPerPx = (this._view[1] - this._view[0]) / this._width;
        moving.preview = shiftEvent(track, moving.index, moving.from + dx * msPerPx, { ...this._editOpts(), step: snapStep(msPerPx) });
        this._showDragTip(ev, moving);
        this._scheduleTimeline();
      }
      return;
    }
    const drag = this._drag;
    if (drag && drag.id === ev.pointerId) {
      const dx = ev.clientX - drag.x;
      if (Math.abs(dx) > 3) drag.moved = true;
      if (drag.moved) {
        this._hideHover();
        const span = drag.view[1] - drag.view[0];
        this._setView(panView(drag.view, (-dx / this._width) * span, this._bounds()));
        return;
      }
    }
    this._showHover(ev);
  }

  _onPointerUp(ev) {
    ev.currentTarget.releasePointerCapture?.(ev.pointerId);
    const moving = this._evDrag;
    if (moving && moving.id === ev.pointerId) {
      this._evDrag = null;
      this._hideHover();
      const sel = { entity_id: moving.entity_id, index: moving.index };
      if (!moving.moved) {
        this._select(sel, moving.from);
        this._render();
        return this.shadowRoot.getElementById("tl-wrap")?.focus({ preventScroll: true });
      }
      const preview = moving.preview;
      const track = this._track(moving.entity_id);
      if (!preview || !track || preview.timeline === track || !Number.isFinite(preview.at)) return this._scheduleTimeline();
      this._selT = preview.at;
      this._commit("move change", [{ entity_id: moving.entity_id, timeline: preview.timeline }], sel);
      return;
    }
    const drag = this._drag;
    this._drag = null;
    if (!drag || drag.moved) return;
    const hit = this._resultsHit(ev);
    if (hit?.run) {
      this._selectedRun = hit.run.index;
      return this._render();
    }
    // A click (tap) on a track selects the state there, and remembers the time for "Add change".
    // A second one on the same spot adds a change there. Detected here rather than with
    // dblclick, which never fires: the first click redraws the timeline, and pointer
    // capture plus a replaced target means the browser sends no click events at all.
    const at = this._trackAt(ev);
    if (at) {
      const msPerPx = (this._view[1] - this._view[0]) / this._width;
      const t = Math.min(this._window.end - 1, Math.max(this._window.start, snapTime(at.t, snapStep(msPerPx))));
      const last = this._lastTap;
      const now = ev.timeStamp;
      this._lastTap = { entity_id: at.track.entity_id, x: ev.clientX, y: ev.clientY, at: now };
      if (last && last.entity_id === at.track.entity_id && now - last.at < 450 && Math.abs(ev.clientX - last.x) < 8 && Math.abs(ev.clientY - last.y) < 8) {
        this._lastTap = null;
        this._selT = t;
        return this._addChange(at.track.entity_id, t);
      }
      this._select({ entity_id: at.track.entity_id, index: eventIndexAt(at.track.events, at.t) }, t);
      this._render();
      this.shadowRoot.getElementById("tl-wrap")?.focus({ preventScroll: true });
    }
  }

  /* The time a dragged change would land on, next to the pointer. */
  _showDragTip(ev, moving) {
    const tip = this.shadowRoot.getElementById("tip");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (!tip || !cursor || !moving.preview) return;
    const at = moving.preview.at;
    const delta = at - moving.from;
    const x = this._scale().toX(at);
    cursor.hidden = false;
    cursor.style.left = `${x}px`;
    tip.hidden = false;
    tip.innerHTML = `<div class="mono"><strong>${esc(this._fullTime(at))}</strong></div>
      <div class="muted small">${delta === 0 ? "unchanged" : `${delta > 0 ? "+" : "−"}${esc(formatDuration(Math.abs(delta)))} from ${esc(this._clockTime(moving.from))}`}</div>`;
    const { y } = this._localX(ev);
    const wrapWidth = this.shadowRoot.getElementById("tl-wrap").clientWidth;
    const tipWidth = tip.offsetWidth;
    tip.style.left = `${x + 12 + tipWidth > wrapWidth ? Math.max(0, x - 12 - tipWidth) : x + 12}px`;
    tip.style.top = `${y + 14}px`;
  }

  /* The run or service call under the pointer on the results row. */
  _resultsHit(ev) {
    if (!this._result) return null;
    const svg = this._rowAt(ev.clientX, ev.clientY);
    if (svg?.dataset.row !== "results") return null;
    const x = ev.clientX - svg.getBoundingClientRect().left;
    const scale = makeScale(this._view[0], this._view[1], this._width);
    const run = findNearest(this._result.runs.map((r) => ({ x: scale.toX(r.started), run: r })), x, 10);
    if (run) return run;
    return findNearest(this._result.calls.map((c) => ({ x: scale.toX(c.t), call: c })), x, 4);
  }

  _showHover(ev) {
    const svg = this._rowAt(ev.clientX, ev.clientY);
    const tip = this.shadowRoot.getElementById("tip");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (!svg || !tip || svg.dataset.row === "axis") return this._hideHover();
    const { x, y } = this._localX(ev);
    let overMark = false;
    const svgX = ev.clientX - svg.getBoundingClientRect().left;
    const scale = makeScale(this._view[0], this._view[1], this._width);
    const t = scale.toT(svgX);
    let html = "";
    if (svg.dataset.row === "track") {
      const track = this._tracks.find((tr) => tr.entity_id === svg.dataset.entity);
      const seg = track?.segments && segmentAt(track.segments, t);
      const unit = track ? this._unit(track) : "";
      const hit = this._trackAt(ev);
      const mark = hit ? this._markHit(hit, ev) : null;
      if (mark !== null) {
        overMark = true;
        const change = track.events[mark];
        html = `<div class="mono small">${esc(this._fullTime(parseTime(change.at)))}</div>
          <div>→ <strong>${esc(change.state)}</strong> <span class="muted small">${change.edited ? "edited" : "recorded"}</span></div>
          <div class="muted small">drag to move · click to edit</div>`;
      } else {
        html = `<div class="mono small">${esc(this._fullTime(t))}</div>
          <div><strong>${seg ? esc(seg.state) : "no data"}</strong>${seg && track.numeric && !isMissing(seg.state) && unit ? ` ${esc(unit)}` : ""}</div>
          ${seg ? `<div class="muted small">since ${esc(this._fullTime(seg.from))}</div>` : ""}`;
      }
    } else if (svg.dataset.row === "results") {
      const hit = this._resultsHit(ev);
      if (hit?.run) {
        const r = hit.run;
        html = `<div class="mono small">${esc(this._fullTime(r.started))}</div>
          <div><strong>${esc(OUTCOME_LABEL[r.outcome])}</strong>${r.outcome === "other" ? ` (${esc(r.outcomeLabel)})` : ""}</div>
          ${r.trigger ? `<div class="small">${esc(describeTrigger(r.trigger))}</div>` : ""}
          <div class="muted small">click for details</div>`;
      } else if (hit?.call) {
        const c = hit.call;
        html = `<div class="mono small">${esc(this._fullTime(c.t))}</div><div><strong>${esc(c.domain)}.${esc(c.service)}</strong></div>`;
      } else {
        html = `<div class="mono small">${esc(this._fullTime(t))}</div>`;
      }
    }
    this.shadowRoot.getElementById("tl-wrap")?.classList.toggle("over-mark", overMark);
    cursor.hidden = false;
    cursor.style.left = `${x}px`;
    tip.hidden = false;
    tip.innerHTML = html;
    const wrapWidth = this.shadowRoot.getElementById("tl-wrap").clientWidth;
    const tipWidth = tip.offsetWidth;
    tip.style.left = `${x + 12 + tipWidth > wrapWidth ? Math.max(0, x - 12 - tipWidth) : x + 12}px`;
    tip.style.top = `${y + 14}px`;
  }

  _hideHover() {
    const tip = this.shadowRoot.getElementById("tip");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (tip) tip.hidden = true;
    if (cursor) cursor.hidden = true;
    this.shadowRoot.getElementById("tl-wrap")?.classList.remove("over-mark");
  }

  _onClick(ev) {
    const el = ev.target.closest?.("[data-action]");
    if (!el || el.disabled) return;
    const action = el.dataset.action;
    const span = this._view[1] - this._view[0];
    const mid = this._view[0] + span / 2;
    if (action === "preset") {
      const { start, end } = presetWindow(el.dataset.preset);
      this._setWindow(start, end, el.dataset.preset);
    } else if (action === "run") this._run();
    else if (action === "zoom-in") this._setView(zoomView(this._view, mid, 0.5, this._bounds()));
    else if (action === "zoom-out") this._setView(zoomView(this._view, mid, 2, this._bounds()));
    else if (action === "zoom-reset") this._setView(this._bounds());
    else if (action === "add-track") this._addTrack(this.shadowRoot.getElementById("add-track")?.value);
    else if (action === "remove-track") this._removeTrack(el.dataset.entity);
    else if (action === "prev-run" || action === "next-run") this._stepRun(action === "next-run" ? 1 : -1);
    else if (action === "undo") this._undoEdit();
    else if (action === "reset-all") this._resetTracks(this._tracks.map((t) => t.entity_id));
    else if (action === "reset-track") this._resetTracks([el.dataset.entity]);
    else if (action === "ed-apply") this._applyEditor();
    else if (action === "ed-delete") this._deleteSelected();
    else if (action === "ed-add" && this._sel) this._addChange(this._sel.entity_id, Number(el.dataset.t));
    else if (action === "ed-prev" || action === "ed-next") this._stepChange(action === "ed-next" ? 1 : -1);
    else if (action === "ed-close") {
      this._select(null);
      this._render();
    } else if (action === "cfg-revert") this._revertConfig();
  }

  _stepRun(delta) {
    const runs = this._result?.runs || [];
    const pos = runs.findIndex((r) => r.index === this._selectedRun);
    const next = runs[pos + delta];
    if (!next) return;
    this._selectedRun = next.index;
    // Bring the run into view if it is off screen, keeping the zoom.
    const [t0, t1] = this._view;
    if (next.started < t0 || next.started > t1) {
      const half = (t1 - t0) / 2;
      this._view = clampView(next.started - half, next.started + half, ...this._bounds());
    }
    this._render();
  }

  _onChange(ev) {
    const id = ev.target.id;
    if (id === "automation") this._selectAutomation(ev.target.value);
    else if (id === "win-start" || id === "win-end") {
      const start = fromLocalInput(this.shadowRoot.getElementById("win-start").value);
      const end = fromLocalInput(this.shadowRoot.getElementById("win-end").value);
      this._setWindow(start, end, null);
    }
  }

  _onInput(ev) {
    const id = ev.target.id;
    if ((id === "ed-time" || id === "ed-state" || id === "ed-attrs") && this._sel) {
      // Kept so a re-render (a history load, a run finishing) does not wipe unapplied input.
      const value = (name) => this.shadowRoot.getElementById(name)?.value;
      this._edDraft = { key: `${this._sel.entity_id}#${this._sel.index}`, time: value("ed-time"), state: value("ed-state"), attrs: value("ed-attrs") };
      return;
    }
    if (id !== "auto-filter") return;
    this._autoFilter = ev.target.value;
    // Only the options change, so the search box keeps focus and caret.
    const select = this.shadowRoot.getElementById("automation");
    if (select) select.innerHTML = this._automationOptions();
  }

  /* Timeline shortcuts (Delete, Ctrl/⌘+Z, Escape), listened for on the window: a click
   * on the timeline redraws it, which drops keyboard focus to the page, so a listener
   * on the panel alone would miss them. They apply when the key comes from inside the
   * panel or from nowhere in particular, and never while typing in a field. */
  _onShortcut = (ev) => {
    const path = ev.composedPath?.() || [ev.target];
    const origin = path[0];
    if (!path.includes(this) && origin !== document.body && origin !== document.documentElement) return;
    const typing = path.some((el) => /^(INPUT|TEXTAREA|SELECT|HA-YAML-EDITOR|HA-CODE-EDITOR)$/.test(el.tagName || "") || el.isContentEditable);
    const inEditor = ["ed-time", "ed-state", "ed-attrs"].includes(origin.id);
    if (this._sel && ev.key === "Escape" && (!typing || inEditor)) {
      this._select(null);
      return this._render();
    }
    if (typing) return;
    if ((ev.key === "Delete" || ev.key === "Backspace") && this._sel?.index >= 0) {
      ev.preventDefault();
      return this._deleteSelected();
    }
    if (ev.key.toLowerCase() === "z" && (ev.ctrlKey || ev.metaKey) && !ev.shiftKey && this._undo.size) {
      ev.preventDefault();
      return this._undoEdit();
    }
  };

  _onKeydown(ev) {
    if (["ed-time", "ed-state"].includes(ev.target.id) && ev.key === "Enter") {
      ev.preventDefault();
      return this._applyEditor();
    }
    if (ev.key !== "Enter") return;
    if (ev.target.id === "add-track") {
      ev.preventDefault();
      this._addTrack(ev.target.value);
    } else if (ev.target.id === "auto-filter") {
      // Enter picks the first match, so search-then-Enter is enough on its own.
      const first = (this._automations || []).find((a) => {
        const needle = this._autoFilter.trim().toLowerCase();
        return a.alias.toLowerCase().includes(needle) || a.entity_id.includes(needle);
      });
      if (first) this._selectAutomation(first.entity_id);
    }
  }

  _css() {
    return `
      :host { display: block; background: var(--primary-background-color); min-height: 100vh; }
      .page { max-width: 1400px; margin: 0 auto; padding: 16px; color: var(--primary-text-color); box-sizing: border-box; }
      .header { display: flex; align-items: center; gap: 8px; }
      h1 { font-size: 24px; font-weight: 400; margin: 8px 0 16px; }
      h3 { font-size: 0.95em; font-weight: 500; margin: 16px 0 6px; }
      ha-card { display: block; margin-bottom: 16px; }
      .card-content { padding: 16px; }
      .muted { color: var(--secondary-text-color); }
      .small { font-size: 0.85em; }
      .mono { font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); }
      input, select { font: inherit; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 7px 8px; min-width: 0; box-sizing: border-box; }
      input[type=datetime-local] { color-scheme: light dark; }
      button { font: inherit; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 6px 12px; cursor: pointer; }
      button:hover:not([disabled]) { background: var(--secondary-background-color); }
      button[disabled] { opacity: .5; cursor: default; }
      button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); font-weight: 500; padding: 8px 24px; }
      button.primary:hover:not([disabled]) { filter: brightness(1.1); background: var(--primary-color); }
      button.icon { border: none; background: none; padding: 0 6px; font-size: 1.2em; line-height: 1; color: var(--secondary-text-color); }
      .setup { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; }
      .field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
      .field.grow { flex: 1 1 320px; }
      .label { font-size: 0.85em; color: var(--secondary-text-color); }
      .picker { display: flex; gap: 8px; flex-wrap: wrap; }
      .picker input { flex: 1 1 140px; }
      .picker select { flex: 3 1 220px; }
      .window { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
      .segmented { display: inline-flex; flex-wrap: wrap; border: 1px solid var(--divider-color); border-radius: 6px; overflow: hidden; align-self: flex-start; }
      .segmented button { border: 0; border-radius: 0; font-size: 0.85em; padding: 3px 10px; color: var(--secondary-text-color); }
      .segmented button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .run-row { display: flex; align-items: center; gap: 12px; flex: 1 1 100%; flex-wrap: wrap; }
      .alert { display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-radius: 8px; margin-bottom: 12px; font-size: 0.92em; }
      .alert.error { background: rgba(219, 68, 55, .15); color: var(--error-color, #db4437); }
      .alert.warning { background: rgba(255, 152, 0, .15); color: var(--primary-text-color); border: 1px solid rgba(255,152,0,.4); }
      .alert.info { background: var(--secondary-background-color); color: var(--primary-text-color); }
      .alert ul { margin: 4px 0 0; padding-left: 20px; }
      .alert pre, .raw pre { white-space: pre-wrap; word-break: break-word; max-height: 360px; overflow: auto; font-size: 0.8em;
        background: var(--secondary-background-color); color: var(--primary-text-color); padding: 8px; border-radius: 6px; }
      .spinner { width: 14px; height: 14px; border: 2px solid var(--divider-color); border-top-color: var(--primary-color); border-radius: 50%; animation: spin .8s linear infinite; flex: none; }
      @keyframes spin { to { transform: rotate(360deg); } }
      .empty { padding: 24px 8px; text-align: center; color: var(--secondary-text-color); }
      .tl-toolbar { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: center; }
      .zoom { display: flex; gap: 4px; align-items: center; }
      .zoom button { padding: 3px 10px; }
      .zoom .muted { margin-left: 6px; }
      .add { display: flex; gap: 6px; flex: 0 1 380px; min-width: 0; }
      .add input { flex: 1; }
      .hint { margin: 6px 0 4px; }
      .edits { display: flex; gap: 4px; }
      .edits button { padding: 3px 10px; }
      #tl-wrap { position: relative; user-select: none; -webkit-user-select: none; outline: none; }
      #tl-wrap:focus-visible { box-shadow: 0 0 0 2px var(--primary-color); border-radius: 4px; }
      #tl-wrap.over-mark svg.track { cursor: ew-resize; }
      .ev { stroke: var(--primary-text-color); stroke-width: 1; opacity: .35; }
      .ev.edited { stroke: var(--accent-color, #ff9800); stroke-width: 2; opacity: 1; }
      .ev.sel { stroke: var(--primary-text-color); stroke-width: 2.5; opacity: 1; }
      .ev.edited.sel { stroke: var(--accent-color, #ff9800); }
      .ev-flag { fill: var(--accent-color, #ff9800); }
      .sel-span { fill: none; stroke: var(--primary-text-color); stroke-width: 1.5; stroke-dasharray: 4 2; }
      button.link { border: none; background: none; padding: 0 2px; color: var(--primary-color); font-size: inherit; }
      button.link:hover:not([disabled]) { background: none; text-decoration: underline; }
      .chip.edited { background: rgba(255,152,0,.2); color: var(--accent-color, #ff9800); }
      .editor { margin-top: 12px; padding: 12px; border: 1px solid var(--divider-color); border-left: 3px solid var(--primary-color); border-radius: 8px; display: flex; flex-direction: column; gap: 10px; }
      .editor-head { display: flex; align-items: center; gap: 6px 8px; flex-wrap: wrap; }
      .editor-head .close { margin-left: auto; }
      .editor-fields { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start; }
      .fld { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
      .fld.grow { flex: 1 1 260px; }
      .fld .fixed { padding: 8px 0; }
      .fld textarea, .cfg-text { font: inherit; font-family: var(--code-font-family, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 0.85em; color: var(--primary-text-color); background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; padding: 7px 8px; box-sizing: border-box; width: 100%; resize: vertical; }
      .editor-actions { display: flex; flex-wrap: wrap; gap: 8px; }
      button.primary.small-btn { padding: 6px 18px; }
      .cfg-head { display: flex; align-items: center; gap: 10px; }
      h2 { font-size: 1.1em; font-weight: 500; margin: 0 0 12px; }
      .cfg-head h2 { margin: 0; }
      .cfg-head { margin-bottom: 12px; }
      .cfg-mode { margin-bottom: 6px; }
      #cfg-editor ha-yaml-editor { display: block; }
      #cfg-error { margin-top: 6px; white-space: pre-wrap; }
      .cfg-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
      #tl svg { display: block; touch-action: pan-y; cursor: grab; overflow: hidden; }
      #tl svg:active { cursor: grabbing; }
      .row { margin-top: 6px; }
      .axis-row { position: sticky; top: 0; z-index: 2; background: var(--card-background-color, var(--primary-background-color)); margin-top: 0; }
      .track-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 8px; font-size: 0.85em; padding: 2px 0; min-width: 0; }
      .track-head .name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .track-head .eid { color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
      .track-head .meta { color: var(--secondary-text-color); margin-left: auto; text-align: right; }
      .track-head .stale { color: var(--warning-color, #ff9800); }
      .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
      svg.track, svg.results { background: var(--secondary-background-color); border-radius: 4px; }
      .tick { stroke: var(--secondary-text-color); }
      .baseline { stroke: var(--divider-color); }
      .grid { stroke: var(--divider-color); stroke-dasharray: 2 3; }
      .axis-label { fill: var(--secondary-text-color); font-size: 11px; }
      .seg { fill-opacity: .55; }
      .seg.missing { fill-opacity: 1; }
      .missing-bg { fill: var(--card-background-color, #fff); }
      .missing-line { stroke: var(--secondary-text-color); stroke-width: 2; opacity: .45; }
      .seg-label { fill: var(--primary-text-color); font-size: 11px; pointer-events: none; }
      .range-label { fill: var(--secondary-text-color); font-size: 10px; pointer-events: none; }
      .line { fill: none; stroke: var(--primary-color); stroke-width: 1.5; stroke-linejoin: round; }
      .call-tick { stroke: var(--info-color, #039be5); stroke-width: 2; }
      .selected { fill: none; stroke: var(--primary-text-color); stroke-width: 1.5; }
      #cursor { position: absolute; top: ${AXIS_H}px; bottom: 0; width: 1px; background: var(--primary-text-color); opacity: .35; pointer-events: none; }
      #tip { position: absolute; z-index: 3; pointer-events: none; max-width: 280px; padding: 6px 10px; border-radius: 6px;
        background: var(--card-background-color, #fff); color: var(--primary-text-color); border: 1px solid var(--divider-color);
        box-shadow: 0 2px 8px rgba(0,0,0,.25); font-size: 0.9em; }
      .detail-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .chip { padding: 2px 10px; border-radius: 12px; font-size: 0.8em; font-weight: 500; white-space: nowrap; }
      .chip.pass { background: rgba(67,160,71,.2); color: var(--success-color, #43a047); }
      .chip.fail { background: rgba(219,68,55,.2); color: var(--error-color, #db4437); }
      .chip.none { background: var(--secondary-background-color); color: var(--secondary-text-color); }
      dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; margin: 12px 0 0; font-size: 0.92em; }
      dt { color: var(--secondary-text-color); }
      dd { margin: 0; word-break: break-word; }
      .err { color: var(--error-color, #db4437); }
      .conds { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; font-size: 0.9em; }
      .calls { display: flex; flex-direction: column; gap: 6px; }
      .call { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; padding: 6px 8px; border-radius: 6px; background: var(--secondary-background-color); word-break: break-all; }
      .raw { margin-top: 16px; }
      .raw summary { cursor: pointer; color: var(--secondary-text-color); }
      @media (max-width: 600px) {
        .page { padding: 8px; }
        .card-content { padding: 12px; }
        .add { flex: 1 1 100%; }
        .fld, .fld.grow { flex: 1 1 100%; }
        .fld input { width: 100%; }
        .track-head .eid { display: none; }
        dl { grid-template-columns: 1fr; gap: 0; }
        dt { margin-top: 6px; }
      }
    `;
  }
}

if (globalThis.customElements && !customElements.get("event-simulator-panel")) {
  customElements.define("event-simulator-panel", EventSimulatorPanel);
}
