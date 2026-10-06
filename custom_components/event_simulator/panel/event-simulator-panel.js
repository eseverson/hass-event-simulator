/* Event Simulator sidebar panel.
 * Plain web component: no build step, no framework, no external dependencies. Uses
 * Home Assistant's CSS variables so it matches the active theme, light or dark.
 *
 * Flow: pick an automation -> pick a window -> tracks load from the recorder for the
 * entities the automation references -> Run sends the scenario to the sandbox and the
 * runs it reports land on a results row under the shared time axis.
 *
 * Panning is the scroll bar under the shared axis (every track pans together); a drag on
 * a track shifts that track's whole history in time (shiftTrack).
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

/* The scroll bar that pans the shared time axis: where its thumb sits for `view` inside
 * [lo, hi] on a bar `width` px wide, as {left, width} in px. The thumb is as wide as the
 * visible share of the window, but never thinner than `minThumb`, so it stays grabbable
 * when zoomed far in; positions map over the bar's free room, so the ends still line up. */
export function scrollThumb([t0, t1], [lo, hi], width, minThumb = 24) {
  const full = hi - lo;
  if (!(full > 0) || !(width > 0)) return { left: 0, width: Math.max(0, width) };
  const span = t1 - t0;
  const w = Math.min(width, Math.max(Math.min(minThumb, width), (span / full) * width));
  const room = full - span;
  const left = room > 0 ? ((t0 - lo) / room) * (width - w) : 0;
  return { left: Math.min(width - w, Math.max(0, left)), width: w };
}

/* The view a thumb `thumbWidth` px wide shows when dragged to `left` px: the span stays,
 * the start follows the thumb (the inverse of scrollThumb). */
export function viewFromThumb(left, [t0, t1], [lo, hi], width, thumbWidth) {
  const span = t1 - t0;
  const free = width - thumbWidth;
  const room = hi - lo - span;
  const start = free > 0 && room > 0 ? lo + (Math.min(free, Math.max(0, left)) / free) * room : lo;
  return clampView(start, start + span, lo, hi);
}

/* A click on the bar beside the thumb pages the view toward the click by most of a span. */
export function pageView([t0, t1], direction, bounds) {
  return panView([t0, t1], Math.sign(direction) * (t1 - t0) * 0.9, bounds);
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

/* Every run outcome sandbox/engine.py reports, in legend order; `other` is only for a
 * value the panel does not know. Each has its own marker shape as well as color, so
 * they stay apart without color vision. */
export const OUTCOMES = ["fired", "condition_failed", "not_triggered", "aborted", "cancelled", "dropped", "unfinished", "error", "other"];

export const OUTCOME_STYLE = {
  fired: { label: "Fired", color: "var(--success-color, #43a047)", shape: "dot" },
  condition_failed: { label: "Stopped by condition", color: "var(--warning-color, #ff9800)", shape: "ring" },
  // HA 2026.9+: a trigger looked at a change and decided not to fire (stopped_by: why).
  not_triggered: { label: "Not triggered", color: "#78909c", shape: "dash" },
  aborted: { label: "Aborted", color: "#c2185b", shape: "open-diamond" },
  cancelled: { label: "Cancelled", color: "#5c6bc0", shape: "cross" },
  dropped: { label: "Dropped", color: "#8d6e63", shape: "open-triangle" },
  unfinished: { label: "Still running after the overrun", color: "#0288d1", shape: "play" },
  error: { label: "Error", color: "var(--error-color, #db4437)", shape: "diamond" },
  other: { label: "Other", color: "var(--secondary-text-color, #727272)", shape: "open-square" },
};

export function classifyOutcome(outcome, run = {}) {
  const o = String(outcome ?? "").toLowerCase();
  if (o in OUTCOME_STYLE && o !== "other") return o;
  if (!o && run.error) return "error";
  return "other";
}

/* Why the script mode refused a run (engine.py `stopped_by` for `dropped`). */
const DROPPED_BY = { mode_single: "Dropped by mode: single", max_runs: "Dropped at max_runs", recursion: "Dropped as recursive" };

/* What happened to a normalized run, in words: its outcome, why it was dropped, or the
 * unknown outcome's own name. */
export function outcomeText(run) {
  if (run.outcome === "dropped" && DROPPED_BY[run.stoppedBy]) return DROPPED_BY[run.stoppedBy];
  if (run.outcome === "other") return `Other (${run.outcomeLabel})`;
  return OUTCOME_STYLE[run.outcome]?.label ?? OUTCOME_STYLE.other.label;
}

/* The Runs legend: [{outcome, text, count}] for each distinct outcomeText, in OUTCOMES
 * order, then first appearance. */
export function outcomeSummary(runs) {
  const groups = new Map();
  for (const run of runs) {
    const text = outcomeText(run);
    const g = groups.get(text) || groups.set(text, { outcome: run.outcome, text: text[0].toLowerCase() + text.slice(1), count: 0 }).get(text);
    g.count++;
  }
  return [...groups.values()].sort((a, b) => OUTCOMES.indexOf(a.outcome) - OUTCOMES.indexOf(b.outcome));
}

/* The SVG marker for an outcome centered on (x, y). */
export function outcomeMarker(outcome, x, y, color = (OUTCOME_STYLE[outcome] || OUTCOME_STYLE.other).color) {
  const open = `fill="var(--card-background-color, #fff)" stroke="${color}" stroke-width="2"`;
  switch ((OUTCOME_STYLE[outcome] || OUTCOME_STYLE.other).shape) {
    case "dot":
      return `<circle cx="${x}" cy="${y}" r="5" fill="${color}" class="marker"/>`;
    case "ring":
      return `<circle cx="${x}" cy="${y}" r="4.5" ${open} class="marker"/>`;
    case "dash":
      return `<path d="M${x - 5},${y}L${x + 5},${y}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round" class="marker"/>`;
    case "diamond":
      return `<path d="M${x},${y - 6}L${x + 6},${y}L${x},${y + 6}L${x - 6},${y}Z" fill="${color}" class="marker"/>`;
    case "open-diamond":
      return `<path d="M${x},${y - 5}L${x + 5},${y}L${x},${y + 5}L${x - 5},${y}Z" ${open} class="marker"/>`;
    case "cross":
      return `<path d="M${x - 4},${y - 4}L${x + 4},${y + 4}M${x + 4},${y - 4}L${x - 4},${y + 4}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" class="marker"/>`;
    case "open-triangle":
      return `<path d="M${x},${y - 5}L${x + 5},${y + 4}L${x - 5},${y + 4}Z" ${open} stroke-linejoin="round" class="marker"/>`;
    case "play":
      return `<path d="M${x - 4},${y - 5}L${x + 5},${y}L${x - 4},${y + 5}Z" fill="${color}" class="marker"/>`;
    default:
      return `<rect x="${x - 4}" y="${y - 4}" width="8" height="8" fill="none" stroke="${color}" stroke-width="2" class="marker"/>`;
  }
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
  const warnings = (Array.isArray(r.warnings) ? r.warnings : []).map(normalizeWarning);
  return { runs, calls, warnings };
}

/* A result warning as {code, message, at}: `at` in ms or null. The engine sends
 * {code, message, at}; a bare string or any other shape still comes out readable. */
export function normalizeWarning(w) {
  if (typeof w === "string") return { code: "", message: w, at: null };
  if (!isObject(w)) return { code: "", message: JSON.stringify(w) ?? String(w), at: null };
  const at = parseTime(w.at);
  const message = w.message == null ? JSON.stringify(w) : String(w.message);
  return { code: w.code == null ? "" : String(w.code), message, at: Number.isFinite(at) ? at : null };
}

/* What each engine warning code means, as a group heading (see sandbox/engine.py). */
export const WARNING_LABELS = {
  event_outside_window: "Events outside the window were skipped",
  initial_state_after_start: "Initial states recorded after the window start",
  initial_state_ignored: "The automation's initial_state was ignored",
  late_attach: "The automation attached late",
  run_unfinished: "Runs still going when the replay stopped",
  self_service_call: "The automation calls itself; its own track decides when it is on",
  unknown_service: "Service calls to services this instance does not have",
  log: "Home Assistant logged warnings or errors",
  log_truncated: "More log messages were left out",
};

/* A heading for a warning code nobody listed: `some_code` -> "Some code". */
export function warningLabel(code) {
  if (!code) return "Other warnings";
  if (WARNING_LABELS[code]) return WARNING_LABELS[code];
  const words = String(code).replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* Warnings grouped by code, groups in order of first appearance, each group's items in
 * time order (untimed ones first, as they come from checking the scenario before the
 * replay starts). [{code, label, items: [{code, message, at}]}] */
export function groupWarnings(warnings) {
  const groups = new Map();
  for (const raw of warnings || []) {
    const w = isObject(raw) && "code" in raw && "message" in raw && (raw.at === null || typeof raw.at === "number") ? raw : normalizeWarning(raw);
    if (!groups.has(w.code)) groups.set(w.code, { code: w.code, label: warningLabel(w.code), items: [] });
    groups.get(w.code).items.push(w);
  }
  const key = (w) => (w.at == null ? -Infinity : w.at);
  return [...groups.values()].map((g) => ({ ...g, items: g.items.slice().sort((a, b) => key(a) - key(b)) }));
}

/* Headings for the error codes a simulate call can fail with (runner.py, websocket.py,
 * the child's structured refusals). */
const RUN_ERROR_TITLES = {
  timeout: "The replay took too long and was stopped",
  sandbox_failed: "The sandbox crashed",
  output_too_large: "The replay produced too much output",
  invalid_scenario: "The sandbox refused the scenario",
  invalid_automation: "The automation config is not valid",
  internal_error: "The sandbox hit an internal error",
  too_many_events: "Too many state changes to replay",
  too_many_entities: "Too many entities to replay",
  too_large: "The scenario is too large to replay",
  invalid_window: "The window is not valid",
  invalid_format: "Home Assistant refused the request",
  unknown_command: "Simulation is not available",
};

/* A failed simulate call, from the rejection callWS hands back (or a structured `error`
 * in a result): {title, message, code, stderr}. */
export function describeRunError(err) {
  if (err?.code === "unknown_command") {
    return {
      title: RUN_ERROR_TITLES.unknown_command,
      message:
        "This build of the integration cannot run simulations: the backend has no event_simulator/simulate/subscribe command. Update the integration and restart Home Assistant.",
      code: "unknown_command",
      stderr: "",
    };
  }
  const nested = isObject(err?.error) ? err.error : null;
  const code = String(err?.code ?? nested?.code ?? "") || null;
  let message = err?.message ?? nested?.message ?? (typeof err?.error === "string" ? err.error : null);
  if (message == null) message = typeof err === "string" ? err : err == null ? "" : String(err);
  const stderr = String(err?.stderr_tail ?? nested?.stderr_tail ?? err?.stderr ?? "").replace(/\s+$/, "");
  return { title: (code && RUN_ERROR_TITLES[code]) || "The run failed", message: String(message), code, stderr };
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

/* Home Assistant's websocket closes the connection on a message over 4 MiB (aiohttp's
 * default), so every request the panel sends is measured against this first, with room
 * left for the envelope. */
export const WS_MESSAGE_LIMIT = 4 * 1024 * 1024 - 16 * 1024;

/* Scenario events as `{entity_ids, attributes, rows}`, each row `[at, entity index,
 * state, attributes index]` (packed.py unpacks it). Entity ids and attribute dicts
 * repeat in nearly every recorded event, so this is several times smaller.
 *
 * Simulate only needs what the replay reads, so by default nothing else is kept. A save
 * must come back exactly as it went (adoptRecorded matches loaded items to history by
 * their full key), so `lossless` adds `flags` and, when something is left over, an object
 * of the remaining fields: `[at, e, state, a, flags, {…}]`. */
export const PACK_LAST_UPDATED = 1; // last_updated equals at
export const PACK_LAST_CHANGED = 2; // last_changed equals at
export const PACK_EDITED = 4; // edited: true

export function packEvents(events, { lossless = false } = {}) {
  const entityIds = [];
  const entityIndex = new Map();
  const attributes = [];
  const attrIndex = new Map();
  const rows = events.map((ev) => {
    let e = entityIndex.get(ev.entity_id);
    if (e === undefined) {
      e = entityIds.push(ev.entity_id) - 1;
      entityIndex.set(ev.entity_id, e);
    }
    const attrs = isObject(ev.attributes) ? ev.attributes : {};
    const key = stableStringify(attrs);
    let a = attrIndex.get(key);
    if (a === undefined) {
      a = attributes.push(attrs) - 1;
      attrIndex.set(key, a);
    }
    const row = [ev.at, e, String(ev.state), a];
    if (!lossless) return row;
    const { at, entity_id, state, attributes: _a, ...rest } = ev;
    let flags = 0;
    const extra = {};
    for (const [k, v] of Object.entries(rest)) {
      if (k === "last_updated" && v === at) flags |= PACK_LAST_UPDATED;
      else if (k === "last_changed" && v === at) flags |= PACK_LAST_CHANGED;
      else if (k === "edited" && v === true) flags |= PACK_EDITED;
      else extra[k] = v;
    }
    if (Object.keys(extra).length) row.push(flags, extra);
    else if (flags) row.push(flags);
    return row;
  });
  return { entity_ids: entityIds, attributes, rows };
}

/* The events of packEvents back as a list (a list is taken as it is: scenarios saved
 * before they were packed). Rows that do not fit the packed shape are skipped. */
export function unpackEvents(packed) {
  if (Array.isArray(packed)) return packed;
  if (!isObject(packed) || !Array.isArray(packed.rows)) return [];
  const ids = Array.isArray(packed.entity_ids) ? packed.entity_ids : [];
  const attrs = Array.isArray(packed.attributes) ? packed.attributes : [];
  const events = [];
  for (const row of packed.rows) {
    if (!Array.isArray(row) || row.length < 4) continue;
    const [at, e, state, a, flags = 0, extra] = row;
    if (!(e in ids) || !(a in attrs) || !Number.isInteger(e) || !Number.isInteger(a)) continue;
    const ev = { at, entity_id: ids[e], state, attributes: attrs[a] };
    if (flags & PACK_LAST_CHANGED) ev.last_changed = at;
    if (flags & PACK_LAST_UPDATED) ev.last_updated = at;
    if (flags & PACK_EDITED) ev.edited = true;
    events.push(isObject(extra) ? { ...ev, ...extra } : ev);
  }
  return events;
}

/* The event_simulator/simulate request for a scenario from buildScenario. */
export function simulateMessage(scenario) {
  return { type: "event_simulator/simulate", scenario: { ...scenario, events: packEvents(scenario.events || []) } };
}

/* The cancellable event_simulator/simulate/subscribe request auto-run sends: acknowledged at
 * once, answered by one event (see replayOutcome); unsubscribing kills the replay. */
export function simulateSubscribeMessage(scenario) {
  return { ...simulateMessage(scenario), type: "event_simulator/simulate/subscribe" };
}

/* What a simulate/subscribe event (or a rejected subscribe, as {error}) means for the panel:
 * {kind: "result", result} (raw), {kind: "error", error: describeRunError(...)}, or
 * {kind: "superseded"} when a newer replay replaced it. */
export function replayOutcome(event) {
  const raw = isObject(event?.result) ? event.result : null;
  const failure = raw ? (raw.error && !Array.isArray(raw.runs) ? raw : null) : event;
  if (!failure) return { kind: "result", result: raw };
  const err = isObject(failure.error) ? { ...failure.error, stderr_tail: failure.error.stderr_tail ?? failure.stderr_tail } : failure.error ?? failure;
  if (err?.code === "superseded") return { kind: "superseded" };
  return { kind: "error", error: describeRunError(err) };
}

/* The event_simulator/scenarios/save request for a scenarioPayload, overwriting
 * `scenarioId` when given. Events go packed losslessly, so a save is as large as Run's. */
export function saveMessage(payload, scenarioId) {
  return {
    type: "event_simulator/scenarios/save",
    ...payload,
    events: packEvents(payload.events || [], { lossless: true }),
    ...(scenarioId ? { scenario_id: scenarioId } : {}),
  };
}

/* null when a request fits in one websocket message, else a {code, message} error to show
 * instead of sending it (sending it would drop the whole frontend connection). */
export function messageSizeError(message, bytes = messageBytes(message)) {
  if (bytes <= WS_MESSAGE_LIMIT) return null;
  const mb = (n) => (n / (1024 * 1024)).toFixed(1);
  return {
    code: "too_large",
    message: `This request is ${mb(bytes)} MB, and Home Assistant accepts at most 4 MB in one websocket message. Try a shorter window or fewer tracks.`,
  };
}

/* The size of a websocket request on the wire, in bytes. */
export function messageBytes(message) {
  return new TextEncoder().encode(JSON.stringify(message)).length;
}

// ---- auto-run ---------------------------------------------------------------
// The panel replays by itself after every change to what a replay depends on: debounced,
// latest wins. ReplayScheduler decides when to start a replay and which answer counts;
// the panel supplies `start` (send it) and `cancel` (abandon it, which kills the child).

export const AUTO_RUN_DELAY = 500; // ms after the last change
export const SLOW_RUN_MS = 3000; // a replay slower than this, of ...
export const LARGE_SCENARIO_BYTES = 512 * 1024; // ... a request larger than this, backs off
export const MAX_AUTO_RUN_DELAY = 10e3;
const AUTO_RUN_KEY = "event_simulator.auto_run";

/* The debounce after a replay that took `last.duration` ms for a `last.size` byte request.
 * A slow replay of a big scenario (days of a chatty sensor) waits about as long as that
 * replay took, so a burst of edits does not restart it over and over; a slow small one is
 * just the sandbox booting, which a longer wait would not help. */
export function autoRunDelay(last) {
  if (!last || last.duration <= SLOW_RUN_MS || last.size <= LARGE_SCENARIO_BYTES) return AUTO_RUN_DELAY;
  return Math.min(MAX_AUTO_RUN_DELAY, Math.max(AUTO_RUN_DELAY, last.duration));
}

/* The remembered Auto-run toggle (default on). Storage can be missing or throw (private
 * windows, blocked site data); then it is just on. */
export function readAutoRunPref(storage = globalThis.localStorage) {
  try {
    return storage?.getItem(AUTO_RUN_KEY) !== "off";
  } catch {
    return true;
  }
}

export function writeAutoRunPref(on, storage = globalThis.localStorage) {
  try {
    storage?.setItem(AUTO_RUN_KEY, on ? "on" : "off");
  } catch {
    // not remembered; nothing else depends on it
  }
}

/* When to replay, and which answer is current.
 *
 * `change()` after an edit (re)arms the debounce while auto-run is on; otherwise it only
 * marks the inputs dirty. A replay starts only while `setReady(true)` (an automation is
 * loaded, no history load is pending, the config parses); one due meanwhile waits for it.
 * Starting a replay cancels the one in flight, and `finish(seq)` says whether an answer is
 * still the current one. `start(seq, {forced})` returns `{size}` (request bytes) when it
 * sent something, or null when it ran nothing (inputs unchanged, request too large). */
export class ReplayScheduler {
  constructor({ start, cancel, enabled = true, now = () => Date.now(), setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id) }) {
    Object.assign(this, { _start: start, _cancel: cancel, _now: now, _setTimer: setTimer, _clearTimer: clearTimer });
    this.enabled = enabled;
    this.ready = false;
    this.dirty = false; // inputs changed since the last replay started
    this._forced = false; // Run now pressed, waiting to be ready
    this._timer = null;
    this._seq = 0;
    this._inflight = null; // {seq, startedAt, size}
    this._last = null; // {duration, size} of the last replay that finished
  }

  get running() {
    return this._inflight !== null;
  }

  get scheduled() {
    return this._timer !== null;
  }

  get delay() {
    return autoRunDelay(this._last);
  }

  change() {
    this.dirty = true;
    if (this.enabled) this._arm();
  }

  setEnabled(on) {
    this.enabled = !!on;
    if (!this.enabled) this._disarm();
    else if (this.dirty) this._arm();
  }

  setReady(ready) {
    const was = this.ready;
    this.ready = !!ready;
    if (!this.ready || was) return;
    if (this._forced) this._launch();
    else if (this.dirty && this.enabled && this._timer === null) this._arm();
  }

  runNow() {
    this._disarm();
    this._forced = true;
    if (this.ready) this._launch();
  }

  /* The answer for `seq` arrived: true when it is the current one (and so ends it). */
  finish(seq) {
    if (this._inflight?.seq !== seq) return false;
    this._last = { duration: this._now() - this._inflight.startedAt, size: this._inflight.size };
    this._inflight = null;
    return true;
  }

  /* What a replay would answer no longer applies (another automation, window or
   * scenario): drop the one in flight and anything pending. */
  invalidate() {
    this._disarm();
    this.dirty = false;
    this._forced = false;
    this._seq++;
    this._abandon();
  }

  dispose() {
    this.invalidate();
  }

  _arm() {
    this._disarm();
    this._timer = this._setTimer(() => {
      this._timer = null;
      if (this.ready) this._launch();
    }, this.delay);
  }

  _disarm() {
    if (this._timer !== null) this._clearTimer(this._timer);
    this._timer = null;
  }

  _abandon() {
    const prev = this._inflight;
    this._inflight = null;
    if (prev) this._cancel(prev.seq);
  }

  _launch() {
    const forced = this._forced;
    this._forced = false;
    this.dirty = false;
    const seq = ++this._seq;
    this._abandon();
    // In flight from before `start`, which may redraw and ask `running`.
    const inflight = (this._inflight = { seq, startedAt: this._now(), size: 0 });
    const sent = this._start(seq, { forced });
    if (this._inflight !== inflight) return; // start answered or replaced it already
    if (sent) inflight.size = sent.size ?? 0;
    else this._inflight = null;
  }
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

// ---- track shift (pure) -----------------------------------------------------
//
// Dragging a track moves its whole history in time, so events of different entities can
// be realigned against each other. A shifted timeline keeps the window invariant of the
// others (`events` are inside [start, end), `initial` is the state at the window start)
// and adds `shift: {offset, base, before, after}`: the total offset in ms, the state in
// effect before every event (its timestamps shifted too), and the events pushed out of
// the window on either side. Those are kept, not dropped, so shifting back restores them;
// only the replay leaves them out (buildScenario reads `events` and `initial` alone).
// While events are parked before the start, the last of them is the initial state.

/* Each shifted copy remembers the item it came from and the net offset, so shifting
 * back to where it started hands back the very same object: the identity checks of
 * timelineEdited then see it as recorded again. Not serialized; a WeakMap lets go. */
const SHIFT_ORIGIN = new WeakMap();

function shiftItem(item, dt, retime) {
  const origin = SHIFT_ORIGIN.get(item) ?? { src: item, net: 0 };
  const net = origin.net + dt;
  if (net === 0) return origin.src;
  const out = retime(origin.src, net);
  SHIFT_ORIGIN.set(out, { src: origin.src, net });
  return out;
}

const retimeEvent = (ev, net) => editedEvent(ev, { at: isoAt(parseTime(ev.at) + net) });

function retimeState(state, net) {
  const out = { ...state, edited: true };
  for (const key of ["last_changed", "last_updated"]) if (state[key]) out[key] = isoAt(parseTime(state[key]) + net);
  return out;
}

/* A state whose timestamps are past the window start is clamped to it: whatever is in
 * effect at the start cannot have begun after it. */
function clampToStart(state, start) {
  const late = (key) => state[key] && parseTime(state[key]) > start;
  if (!late("last_changed") && !late("last_updated")) return state;
  const out = { ...state };
  for (const key of ["last_changed", "last_updated"]) if (late(key)) out[key] = isoAt(start);
  return out;
}

/* The state at the window start of a shifted track: the last event parked before it
 * (changed when its run of equal states began), or else the base state. */
function shiftedInitial(before, base, start) {
  if (!before.length) return base ? clampToStart(base, start) : null;
  const last = before[before.length - 1];
  let since = parseTime(last.at);
  for (let i = before.length - 2; i >= 0 && String(before[i].state) === String(last.state); i--) since = parseTime(before[i].at);
  return { state: last.state, attributes: last.attributes || {}, last_changed: isoAt(since), last_updated: isoAt(parseTime(last.at)), edited: true };
}

const sameValue = (a, b) => !!a === !!b && (!a || (String(a.state) === String(b.state) && stableStringify(a.attributes || {}) === stableStringify(b.attributes || {})));

/* The track's offset in ms (0 when it was never shifted or is back in place). */
export function trackOffset(timeline) {
  return timeline?.shift?.offset ?? 0;
}

/* How many of the track's events are shifted out of the window, [before, after]. */
export function parkedCounts(timeline) {
  return [timeline?.shift?.before?.length ?? 0, timeline?.shift?.after?.length ?? 0];
}

/* Shift every event of a timeline by dt ms (and the base state's timestamps with them).
 * Events that leave [start, end) are parked on `shift`, and come back when a later shift
 * brings them inside again. Shifted events are marked edited; a track shifted back to
 * offset 0 with nothing parked drops `shift` and gets its own items back. Returns the
 * input itself for dt = 0. */
export function shiftTrack(timeline, dt, { start, end }) {
  const delta = Math.round(dt);
  if (!delta) return timeline;
  const sh = timeline.shift;
  let before = sh?.before ?? [];
  let base = sh ? sh.base : timeline.initial;
  // The initial state edited since the last shift: keep the edit rather than re-deriving over it.
  if (sh && !sameValue(timeline.initial, shiftedInitial(before, base, start))) {
    if (before.length && timeline.initial) {
      const last = before[before.length - 1];
      before = [...before.slice(0, -1), editedEvent(last, { state: String(timeline.initial.state), attributes: timeline.initial.attributes || {} })];
    } else if (!before.length) {
      base = timeline.initial;
    }
  }
  const all = [...before, ...timeline.events, ...(sh?.after ?? [])].map((ev) => shiftItem(ev, delta, retimeEvent));
  const nextBase = base ? shiftItem(base, delta, retimeState) : null;
  const lo = countAtOrBefore(all, start - 1);
  const hi = countAtOrBefore(all, end - 1);
  const parkedBefore = all.slice(0, lo);
  const parkedAfter = all.slice(hi);
  const offset = (sh?.offset ?? 0) + delta;
  const { shift, ...rest } = timeline;
  const next = { ...rest, initial: shiftedInitial(parkedBefore, nextBase, start), events: all.slice(lo, hi) };
  if (offset !== 0 || parkedBefore.length || parkedAfter.length) {
    next.shift = { offset, base: nextBase, before: parkedBefore, after: parkedAfter };
  }
  return next;
}

/* A loaded timeline whose events may lie outside the window (a saved shifted track):
 * parked again as shiftTrack left them, with the saved offset and base state. */
export function restoreShift(timeline, saved, { start, end }) {
  if (!Number.isFinite(start) || !Number.isFinite(end)) return timeline;
  const lo = countAtOrBefore(timeline.events, start - 1);
  const hi = countAtOrBefore(timeline.events, end - 1);
  const offset = Number(saved?.offset) || 0;
  if (!offset && lo === 0 && hi === timeline.events.length) return timeline;
  return {
    ...timeline,
    events: timeline.events.slice(lo, hi),
    shift: {
      offset,
      base: isObject(saved) && "base" in saved ? saved.base : timeline.initial,
      before: timeline.events.slice(0, lo),
      after: timeline.events.slice(hi),
    },
  };
}

/* An offset as `+3 min 20 s` or `−1 h`. */
export function formatOffset(ms) {
  if (!ms) return "0 s";
  return `${ms > 0 ? "+" : "−"}${formatDuration(Math.abs(ms))}`;
}

/* A drag distance in ms rounded to whole snap steps (an offset, so not to the clock). */
export function snapDelta(dt, step) {
  return step ? Math.round(dt / step) * step : Math.round(dt);
}

// ---- config line diff (pure) ---------------------------------------------------
//
// The config editor marks the lines that differ from the saved automation: added and
// changed lines get a bar, and a marker shows where saved lines were removed.

/* The edit script turning `a` into `b` (arrays of lines) as runs of
 * {op: "equal" | "delete" | "insert", a, b, count}: positions in each array. Common
 * head and tail are matched first, so a local edit to a long config costs only the
 * changed middle; the middle is an LCS table, given up past `maxCells` for one
 * delete-all, insert-all hunk. */
export function lineDiff(a, b, maxCells = 4e6) {
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail++;
  const ops = [];
  const push = (op, ai, bi) => {
    const last = ops[ops.length - 1];
    if (last && last.op === op) last.count++;
    else ops.push({ op, a: ai, b: bi, count: 1 });
  };
  for (let i = 0; i < head; i++) push("equal", i, i);
  const n = a.length - head - tail;
  const m = b.length - head - tail;
  if (n * m > maxCells) {
    for (let i = 0; i < n; i++) push("delete", head + i, head);
    for (let j = 0; j < m; j++) push("insert", head + n, head + j);
  } else if (n || m) {
    // lcs[i][j]: the longest common run of a[head+i..] and b[head+j..].
    const w = m + 1;
    const lcs = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        lcs[i * w + j] = a[head + i] === b[head + j] ? lcs[(i + 1) * w + j + 1] + 1 : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && a[head + i] === b[head + j]) push("equal", head + i++, head + j++);
      // Deletes before inserts, so a changed line reads old then new.
      else if (i < n && (j >= m || lcs[(i + 1) * w + j] >= lcs[i * w + j + 1])) push("delete", head + i++, head + j);
      else push("insert", head + i, head + j++);
    }
  }
  for (let k = 0; k < tail; k++) push("equal", a.length - tail + k, b.length - tail + k);
  return ops;
}

/* Lines for comparison: split, trailing whitespace dropped, a final empty line ignored. */
export function configLines(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n").map((l) => l.trimEnd());
  while (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/* What the editor marks, from the saved text and the edited one:
 * {lines: [kind per edited line: "added" | "changed" | null], removed: [{before, lines}],
 *  counts: {added, changed, removed}}. In each hunk the first deleted lines pair with
 * the first inserted ones as changed; the rest are added, or removed (`before` is the
 * edited line they were removed above, lines.length for the end). */
export function configLineMarks(savedText, text) {
  const a = configLines(savedText);
  const b = configLines(text);
  const lines = new Array(b.length).fill(null);
  const removed = [];
  const counts = { added: 0, changed: 0, removed: 0 };
  const ops = lineDiff(a, b);
  for (let k = 0; k < ops.length; k++) {
    if (ops[k].op === "equal") continue;
    // One hunk: the deletes and inserts up to the next equal run.
    let del = null;
    let ins = null;
    for (; k < ops.length && ops[k].op !== "equal"; k++) {
      if (ops[k].op === "delete") del = del ? { ...del, count: del.count + ops[k].count } : { ...ops[k] };
      else ins = ins ? { ...ins, count: ins.count + ops[k].count } : { ...ops[k] };
    }
    k--;
    const changed = Math.min(del?.count ?? 0, ins?.count ?? 0);
    for (let j = 0; j < (ins?.count ?? 0); j++) lines[ins.b + j] = j < changed ? "changed" : "added";
    counts.changed += changed;
    counts.added += (ins?.count ?? 0) - changed;
    const gone = (del?.count ?? 0) - changed;
    if (gone > 0) {
      counts.removed += gone;
      removed.push({ before: ins ? ins.b + ins.count : del.b, lines: a.slice(del.a + changed, del.a + del.count) });
    }
  }
  return { lines, removed, counts };
}

/* A unified diff for the compact view under the editor: hunks of rows
 * {kind: " " | "+" | "-", text, n} (n = the edited line number, 1-based, for " " and "+"),
 * with `context` unchanged lines around each change. */
export function configDiffHunks(savedText, text, context = 2) {
  const a = configLines(savedText);
  const b = configLines(text);
  const rows = [];
  for (const op of lineDiff(a, b)) {
    for (let i = 0; i < op.count; i++) {
      if (op.op === "equal") rows.push({ kind: " ", text: b[op.b + i], n: op.b + i + 1 });
      else if (op.op === "insert") rows.push({ kind: "+", text: b[op.b + i], n: op.b + i + 1 });
      else rows.push({ kind: "-", text: a[op.a + i], n: null });
    }
  }
  const keep = rows.map(() => false);
  rows.forEach((row, i) => {
    if (row.kind === " ") return;
    for (let j = Math.max(0, i - context); j <= Math.min(rows.length - 1, i + context); j++) keep[j] = true;
  });
  const hunks = [];
  let cur = null;
  rows.forEach((row, i) => {
    if (!keep[i]) {
      cur = null;
      return;
    }
    if (!cur) hunks.push((cur = []));
    cur.push(row);
  });
  return hunks;
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
  if (timeline.shift) return true;
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

// ---- saved scenarios (pure) -------------------------------------------------
//
// A saved scenario (store.py) is the panel's state on the server: the automation config
// Run would send (edited or not) and the entity it came from, the window, the tracks
// in order, and each track's timeline. Timelines are saved with their `edited` marks so
// a loaded scenario draws its edited changes exactly as they were.

export const MAX_SCENARIO_NAME = 100; // store.MAX_NAME_LENGTH

/* The body of event_simulator/scenarios/save for the panel's current state. Times go
 * out as UTC ISO strings with a `Z`, the offset the store insists on. */
export function scenarioPayload({ name, entityId, automation, start, end, tracks, labels = [], suggestOptions = null }) {
  const initial_states = {};
  const events = [];
  const track_shifts = {};
  for (const track of tracks) {
    if (track.initial) initial_states[track.entity_id] = track.initial;
    for (const ev of track.events) events.push(ev);
    // A shifted track saves the events it parked outside the window too (restoreShift
    // parks them again on load), and its offset and base state.
    const sh = track.shift;
    if (sh) {
      for (const ev of [...sh.before, ...sh.after]) events.push(ev);
      track_shifts[track.entity_id] = { offset: sh.offset, base: sh.base ?? null };
    }
  }
  events.sort((a, b) => parseTime(a.at) - parseTime(b.at));
  return {
    name: String(name ?? "").trim(),
    automation_entity_id: entityId || null,
    automation,
    start: new Date(start).toISOString(),
    end: new Date(end).toISOString(),
    entity_ids: tracks.map((t) => t.entity_id),
    initial_states,
    events,
    ...(Object.keys(track_shifts).length ? { track_shifts } : {}),
    // Suggest mode: labeled periods (also kept in Replay, where they are an overlay), and
    // the options, which are only sent from Suggest mode and reopen a scenario in it.
    ...(labels.length ? { labels: labelsPayload(labels) } : {}),
    ...(suggestOptions ? { suggest_options: suggestOptions } : {}),
  };
}

/* The tracks a saved scenario describes: [{entity_id, initial, events}] in its
 * `entity_ids` order, then any entity that only appears in its timeline. Each track's
 * events keep their saved order among equal times. */
export function tracksFromScenario(scenario) {
  const ids = [...(Array.isArray(scenario?.entity_ids) ? scenario.entity_ids : [])];
  const initials = isObject(scenario?.initial_states) ? scenario.initial_states : {};
  const events = unpackEvents(scenario?.events).filter((ev) => isObject(ev) && ev.entity_id);
  const shifts = isObject(scenario?.track_shifts) ? scenario.track_shifts : {};
  const window = { start: parseTime(scenario?.start), end: parseTime(scenario?.end) };
  for (const id of [...Object.keys(initials), ...events.map((ev) => ev.entity_id)]) if (!ids.includes(id)) ids.push(id);
  return ids.map((entity_id) =>
    restoreShift(
      {
        entity_id,
        initial: initials[entity_id] || null,
        events: events.filter((ev) => ev.entity_id === entity_id).sort((a, b) => parseTime(a.at) - parseTime(b.at)),
      },
      shifts[entity_id],
      window
    )
  );
}

/* A comparable key for a state write, whatever format its time strings arrived in. */
function itemKey(item) {
  return stableStringify({ ...item, at: parseTime(item.at) });
}

/* A loaded timeline with every unedited item swapped for the equal item of the recorded
 * history, so the identity checks of timelineEdited and the editor treat it as recorded.
 * Items marked `edited`, and recorded ones history no longer has, stay as loaded. */
export function adoptRecorded(loaded, recorded) {
  if (!recorded) return loaded;
  const pool = new Map();
  for (const ev of recorded.events) {
    const key = itemKey(ev);
    if (!pool.has(key)) pool.set(key, []);
    pool.get(key).push(ev);
  }
  const events = loaded.events.map((ev) => (ev.edited ? ev : pool.get(itemKey(ev))?.shift() ?? ev));
  const initial =
    loaded.initial && recorded.initial && !loaded.initial.edited && stableStringify(loaded.initial) === stableStringify(recorded.initial)
      ? recorded.initial
      : loaded.initial;
  return { initial, events, ...(loaded.shift ? { shift: loaded.shift } : {}) };
}

/* What "unsaved changes" compares: everything a save would write except the name. */
export function scenarioFingerprint(payload) {
  const { name, ...rest } = payload;
  return stableStringify(rest);
}

/* A scenario name as typed: {ok, value} or {ok: false, error}. */
export function checkScenarioName(text) {
  const value = String(text ?? "").trim();
  if (!value) return { ok: false, error: "Give the scenario a name." };
  if (value.length > MAX_SCENARIO_NAME) return { ok: false, error: `Names can be at most ${MAX_SCENARIO_NAME} characters.` };
  return { ok: true, value };
}

/* Friendly text for a failed scenarios/* command, by its error code. */
export function scenarioErrorMessage(err, action = "save") {
  const code = err?.code;
  const detail = err?.message ? ` (${err.message})` : "";
  switch (code) {
    case "not_found":
      return "That scenario no longer exists; it may have been deleted in another window. The list is refreshed.";
    case "invalid_window":
      return "The window is not valid: it must end after it starts and be at most 7 days long.";
    case "window_too_long":
      return "Saved scenarios are limited to windows of at most 7 days for now. Shorten the window to save it.";
    case "too_large":
      return `This scenario is too large to save: at most 50,000 state changes and 50 tracks${detail}.`;
    case "too_many":
      return "There are already 100 saved scenarios. Delete one before saving another.";
    case "store_full":
      return `Saved scenarios are limited to 500,000 state changes in all. Delete some saved scenarios before saving this one${detail}.`;
    case "invalid_format":
      return `Home Assistant did not accept the scenario's data${detail}.`;
    case "unknown_command":
      return "This build of the integration cannot save scenarios. Update it and restart Home Assistant.";
    default: {
      const verb = { save: "save", load: "load", delete: "delete", list: "list" }[action] || action;
      return `Could not ${verb} the scenario${action === "list" ? "s" : ""}: ${err?.message || err || "unknown error"}`;
    }
  }
}

// ---- suggest labels (pure) --------------------------------------------------
//
// Suggest mode learns an automation from labeled periods of history. A label is
// {id, start, end, kind, anchor} with times in ms: kind "fire" (should fire, anchored at
// its start, its end, both, or "while" = conditions hold throughout) or "no_fire" (must not
// fire anywhere in it). Periods are half-open [start, end). Lists are kept sorted by start
// and never mutated; every edit returns a new list, so an undo snapshot is the old list.
//
// Overlaps: periods of the same kind that overlap merge into one (the period being placed
// keeps its id and anchor); a should-fire period may not overlap a must-not-fire one.

export const LABEL_KINDS = ["fire", "no_fire"];
export const LABEL_ANCHORS = ["start", "end", "both", "while"];
export const ANCHOR_TEXT = { start: "at start", end: "at end", both: "at start and end", while: "while" };
export const MAX_LABELS = 500; // store.MAX_LABELS
export const DEFAULT_TOLERANCE = { before: 2 * 60e3, after: 10 * 60e3 };
export const MAX_TOLERANCE_MIN = 24 * 60; // store.MAX_TOLERANCE
export const MIN_LABEL_SPAN = 1e3;
const LABEL_DEFAULT_KEY = "event_simulator.label_default";

let labelSeq = 0;
export const nextLabelId = () => `l${++labelSeq}`;

const byStart = (a, b) => a.start - b.start || a.end - b.end;
const overlaps = (a, b) => a.start < b.end && b.start < a.end;

/* A label in words: "Should fire at start", "Must not fire". */
export function labelText(label) {
  return label.kind === "no_fire" ? "Must not fire" : `Should fire ${ANCHOR_TEXT[label.anchor] ?? ANCHOR_TEXT.start}`;
}

/* A label from loose input (saved, or typed), or null when it is not one. Times may be ISO
 * strings or ms. A should-fire label without a known anchor is anchored at its start. */
export function normalizeLabel(raw, id = undefined) {
  if (!isObject(raw)) return null;
  const start = parseTime(raw.start);
  const end = parseTime(raw.end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || !(end > start) || !LABEL_KINDS.includes(raw.kind)) return null;
  const anchor = LABEL_ANCHORS.includes(raw.anchor) ? raw.anchor : "start";
  return { id: id ?? raw.id ?? nextLabelId(), start, end, kind: raw.kind, anchor };
}

/* Put `label` into `labels`, replacing the one with its id (a new one gets an id):
 * {ok: true, labels, label, merged: [ids absorbed]} or {ok: false, error}. Same-kind
 * periods it overlaps (directly or through another) merge into it; a should-fire and a
 * must-not-fire period may not overlap. */
export function placeLabel(labels, label) {
  if (!(label.end - label.start >= MIN_LABEL_SPAN)) return { ok: false, error: "A period must end at least a second after it starts." };
  if (!LABEL_KINDS.includes(label.kind)) return { ok: false, error: "Unknown kind of period." };
  const placed = { ...label, id: label.id ?? nextLabelId(), anchor: LABEL_ANCHORS.includes(label.anchor) ? label.anchor : "start" };
  const others = labels.filter((l) => l.id !== placed.id);
  let merged = placed;
  const absorbed = new Set();
  for (let grew = true; grew; ) {
    grew = false;
    for (const l of others) {
      if (absorbed.has(l) || l.kind !== merged.kind || !overlaps(l, merged)) continue;
      merged = { ...merged, start: Math.min(merged.start, l.start), end: Math.max(merged.end, l.end) };
      absorbed.add(l);
      grew = true;
    }
  }
  if (others.some((l) => l.kind !== merged.kind && overlaps(l, merged))) {
    const other = merged.kind === "fire" ? "must-not-fire" : "should-fire";
    return {
      ok: false,
      error: `That period overlaps a ${other} period${absorbed.size ? " (once merged with the overlapping ones of its own kind)" : ""}. Should-fire and must-not-fire periods cannot overlap; shorten or move one first.`,
    };
  }
  const next = [...others.filter((l) => !absorbed.has(l)), merged].sort(byStart);
  if (next.length > MAX_LABELS) return { ok: false, error: `At most ${MAX_LABELS} labeled periods.` };
  return { ok: true, labels: next, label: merged, merged: [...absorbed].map((l) => l.id) };
}

export function removeLabel(labels, id) {
  return labels.filter((l) => l.id !== id);
}

/* The label moved by dt ms, its length kept, never leaving the window [start, end). */
export function moveLabel(label, dt, { start, end }) {
  const span = label.end - label.start;
  const from = Math.min(Math.max(label.start + Math.round(dt), start), Math.max(start, end - span));
  return { ...label, start: from, end: from + Math.min(span, end - start) };
}

/* The label with one edge ("start" | "end") dragged to t, kept at least `minSpan` long and
 * inside the window. */
export function resizeLabel(label, edge, t, { start, end }, minSpan = MIN_LABEL_SPAN) {
  const at = Math.round(t);
  if (edge === "start") return { ...label, start: Math.max(start, Math.min(at, label.end - minSpan)) };
  return { ...label, end: Math.min(end, Math.max(at, label.start + minSpan)) };
}

/* The period a brush from t0 to t1 covers: ordered, each end snapped to `step` of local
 * time, clamped to the window; null when it is too short to be a period (a click). */
export function brushPeriod(t0, t1, { start, end, step = 0, offsetMinutes = localOffsetMinutes }) {
  const snap = (t) => (step ? snapTime(t, step, offsetMinutes) : Math.round(t));
  const lo = Math.max(start, snap(Math.min(t0, t1)));
  const hi = Math.min(end, snap(Math.max(t0, t1)));
  return hi - lo >= Math.max(step, MIN_LABEL_SPAN) ? { start: lo, end: hi } : null;
}

/* What the pointer at `x` px is on: {id, part: "start" | "end" | "body"} or null. Edges
 * within `reach` px win over the body, the nearest edge first; with a finger (`touch`) only
 * the selected label has edge handles, so a drag on any other one moves it. */
export function labelHit(labels, x, scale, { reach = 6, selected = null, touch = false } = {}) {
  let edge = null;
  let best = reach;
  let body = null;
  for (const l of labels) {
    if (l.end < scale.t0 || l.start > scale.t1) continue;
    const x1 = scale.toX(l.start);
    const x2 = scale.toX(l.end);
    if (!touch || l.id === selected) {
      for (const [part, ex] of [["start", x1], ["end", x2]]) {
        const d = Math.abs(ex - x);
        // Ties go to the selected label, then to the edge the pointer is inside of.
        if (d < best || (d === best && edge && (l.id === selected || (x >= x1 && x <= x2)))) {
          best = d;
          edge = { id: l.id, part };
        }
      }
    }
    if (x >= x1 && x <= x2 && (!body || l.id === selected)) body = { id: l.id, part: "body" };
  }
  return edge ?? body;
}

export function labelsInWindow(labels, start, end) {
  return labels.filter((l) => l.start < end && l.end > start);
}

/* Labels as event_simulator/suggest/subscribe and the store take them: sorted, ISO times,
 * an anchor only on should-fire periods. */
export function labelsPayload(labels) {
  return [...labels].sort(byStart).map((l) => ({
    start: new Date(l.start).toISOString(),
    end: new Date(l.end).toISOString(),
    kind: l.kind,
    ...(l.kind === "fire" ? { anchor: LABEL_ANCHORS.includes(l.anchor) ? l.anchor : "start" } : {}),
  }));
}

/* The labels of a saved scenario, sorted; malformed ones are skipped. */
export function labelsFromScenario(raw) {
  return (Array.isArray(raw) ? raw : []).map((l) => normalizeLabel(l, nextLabelId())).filter(Boolean).sort(byStart);
}

/* Suggest options from the tolerance (ms) as the store and suggest/subscribe take them (s). */
export function suggestOptions(tolerance = DEFAULT_TOLERANCE) {
  return { tolerance_before: tolerance.before / 1000, tolerance_after: tolerance.after / 1000 };
}

export function toleranceFromOptions(options) {
  const pick = (v, d) => (Number.isFinite(Number(v)) && Number(v) >= 0 && v !== null && v !== "" ? Number(v) * 1000 : d);
  return { before: pick(options?.tolerance_before, DEFAULT_TOLERANCE.before), after: pick(options?.tolerance_after, DEFAULT_TOLERANCE.after) };
}

/* A tolerance typed in minutes: {ok, value (ms)} or {ok: false, error}. */
export function checkToleranceMinutes(text) {
  const src = String(text ?? "").trim();
  const n = toNumber(src);
  if (!src || Number.isNaN(n) || n < 0 || n > MAX_TOLERANCE_MIN) return { ok: false, error: `The tolerance is a number of minutes from 0 to ${MAX_TOLERANCE_MIN}.` };
  return { ok: true, value: Math.round(n * 60e3) };
}

/* The remembered kind and anchor for a new period (default: should fire, at start). */
export function readLabelDefault(storage = globalThis.localStorage) {
  try {
    const v = JSON.parse(storage?.getItem(LABEL_DEFAULT_KEY) ?? "null");
    if (isObject(v) && LABEL_KINDS.includes(v.kind)) return { kind: v.kind, anchor: LABEL_ANCHORS.includes(v.anchor) ? v.anchor : "start" };
  } catch {
    // unreadable or blocked: the default
  }
  return { kind: "fire", anchor: "start" };
}

export function writeLabelDefault(value, storage = globalThis.localStorage) {
  try {
    storage?.setItem(LABEL_DEFAULT_KEY, JSON.stringify({ kind: value.kind, anchor: value.anchor }));
  } catch {
    // not remembered
  }
}

/* The event_simulator/suggest/subscribe request (see the suggestions spec): the entities to
 * learn from, the window, the labels inside it, and the options. */
export function suggestMessage({ entityIds, start, end, labels, tolerance }) {
  return {
    type: "event_simulator/suggest/subscribe",
    entity_ids: [...entityIds],
    start: new Date(start).toISOString(),
    end: new Date(end).toISOString(),
    labels: labelsPayload(labelsInWindow(labels, start, end)),
    options: suggestOptions(tolerance),
  };
}

/* Whether the backend has suggest/subscribe, from how it answered a request with no fields:
 * an unknown command is not there; any other refusal (invalid_format) means it is. */
export function suggestAvailable(err) {
  return err?.code !== "unknown_command";
}

/* A suggest/subscribe event (or a rejected subscribe, as {error}) for the panel:
 * {kind: "done", suggestions, hints}, {kind: "progress", text}, {kind: "unavailable"} or
 * {kind: "error", error: describeRunError(...)}. */
export function suggestOutcome(event) {
  const e = isObject(event) ? event : {};
  const err = e.error ?? (isObject(e.result) && e.result.error && !Array.isArray(e.result.suggestions) ? e.result.error : null);
  if (err) {
    if (err?.code === "unknown_command") return { kind: "unavailable" };
    return { kind: "error", error: describeRunError(err) };
  }
  const body = isObject(e.result) ? e.result : e;
  if (Array.isArray(body.suggestions)) return { kind: "done", suggestions: body.suggestions, hints: Array.isArray(body.hints) ? body.hints : [] };
  const stage = body.stage ?? body.progress ?? body.status ?? body.type;
  const counts = [body.done ?? body.count, body.total].filter((n) => Number.isFinite(n));
  const text = stage ? `${String(stage).charAt(0).toUpperCase()}${String(stage).slice(1)}${counts.length === 2 ? ` ${counts[0]}/${counts[1]}` : ""}…` : "Working…";
  return { kind: "progress", text };
}

// ---- panel ----------------------------------------------------------------

const HOUR = 3600e3;
const DAY = 24 * HOUR;
const MAX_WINDOW = 7 * DAY; // history.MAX_WINDOW
// Suggest mode learns from repetition, so its window may be longer (the suggestions spec).
// The history command still caps a load at MAX_WINDOW until the backend raises it for
// suggest; a longer window then fails to load with `window_too_long`, said in words.
const MAX_SUGGEST_WINDOW = 30 * DAY;
const MAX_ENTITIES = 50; // history.MAX_ENTITIES
const PRESETS = [
  { id: "1h", label: "1 h", span: HOUR },
  { id: "6h", label: "6 h", span: 6 * HOUR },
  { id: "24h", label: "24 h", span: DAY },
  { id: "3d", label: "3 d", span: 3 * DAY },
  { id: "7d", label: "7 d", span: 7 * DAY },
  { id: "14d", label: "14 d", span: 14 * DAY, suggest: true },
  { id: "30d", label: "30 d", span: 30 * DAY, suggest: true },
  { id: "today", label: "Today" },
  { id: "yesterday", label: "Yesterday" },
];
const AXIS_H = 24;
const LABELS_H = 30;
const LONG_PRESS_MS = 450; // a touch held this long on the labels row starts a brush
const SCROLL_MIN_THUMB = 28; // px
const DISCRETE_H = 24;
const NUMERIC_H = 56;
const RESULTS_H = 38;

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
    this._result = null; // normalizeResult() of the last run
    this._resultStale = false;
    this._runError = null; // {message, code, stderr}
    this._selectedRun = null; // run index
    this._requestToken = 0; // drops responses to requests a newer one superseded
    this._width = 600;
    this._drag = null; // click, or whole-track shift, in progress: {entity_id, dt, preview, ...}
    this._thumbDrag = null; // scroll bar thumb drag in progress
    this._evDrag = null; // event drag in progress: {entity_id, index, preview, ...}
    this._frame = 0;
    this._heldRender = 0;
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
    this._cfgMarks = null; // configLineMarks of the editor's text, while it differs
    this._savedText = null; // {config, yaml, text}: _savedConfigText's cache
    // Saved scenarios. `_scenario` is the one loaded or last saved ({scenario_id, name});
    // `_savedPrint` the scenarioFingerprint of the panel state it was saved or loaded as.
    this._scenarios = null; // summaries from scenarios/list, once loaded
    this._scenariosOpen = false;
    this._scenario = null;
    this._savedPrint = null;
    this._saveForm = null; // {name, asNew}: the name prompt, while open
    this._scError = null;
    this._scInfo = null;
    this._scBusy = false;
    this._confirm = null; // {kind: "delete" | "load", id}
    this._printCache = null;
    this._focusName = false; // focus the name prompt on the next render
    this._open = {}; // open state of <details data-key>, kept across redraws
    // Auto-run: replay after every change (debounced, latest wins); see ReplayScheduler.
    this._autoRun = readAutoRunPref();
    this._replay = null; // the replay in flight: {seq, print, unsub, done}
    this._resultPrint = null; // _fingerprint() of the inputs the shown result came from
    this._renderDeferred = false; // a render held back until a drag ends
    this._resumeReplay = false;
    this._scheduler = new ReplayScheduler({
      enabled: this._autoRun,
      start: (seq, opts) => this._startReplay(seq, opts),
      cancel: (seq) => this._cancelReplay(seq),
    });
    // Suggest mode: the same timeline, tracks and window, plus labeled periods. Label edits
    // share the undo history (entries with `labels`: the list as it was before).
    this._mode = "replay"; // "replay" | "suggest"
    this._labels = []; // sorted, immutable: see placeLabel
    this._labelSel = null; // id of the label in the editor
    this._labelDraft = null; // {start, end}: a brushed period waiting for its kind
    this._labelDrag = null; // brush, move or resize in progress
    this._labelEdDraft = null; // unapplied label editor times: {id, start, end}
    this._labelError = null;
    this._labelDefault = readLabelDefault();
    this._tolerance = { ...DEFAULT_TOLERANCE };
    this._tolError = null;
    this._suggestAvail = null; // null until probed, then true / false
    this._suggestRun = null; // {done, unsub}
    this._suggestState = null; // {running, text, error, result}
  }

  get _running() {
    return this._scheduler.running;
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
    if (this._resumeReplay) {
      this._resumeReplay = false;
      this._scheduler.change();
      this._render();
    }
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this._onShortcut);
    // Off screen nothing shows a replay, so none runs; one owed is made up on return.
    this._resumeReplay = this._scheduler.running || this._scheduler.dirty;
    this._scheduler.invalidate();
    this._resizeObserver?.disconnect();
    cancelAnimationFrame(this._frame);
  }

  // ---- data ---------------------------------------------------------------

  async _connect() {
    // A reconnect drops the replay subscription without an answer; ask again.
    this._hass.connection?.addEventListener?.("ready", () => {
      if (this._scheduler.running) this._scheduler.change();
    });
    this._loading = "Loading automations…";
    this._render();
    loadYamlEditor().then((ok) => {
      this._yamlState = ok ? "ready" : "unavailable";
      this._renderConfig();
    });
    this._listScenarios();
    this._probeSuggest();
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
    // In Suggest mode an automation only seeds tracks: it adds its entities to the ones there.
    if (this._mode === "suggest" && this._tracks.length) {
      if (entityId) return this._seedFromAutomation(entityId);
      this._entityId = "";
      this._automation = null;
      this._cfgDraft = null;
      this._cfgError = null;
      this._clearResult();
      this._renderConfig();
      return this._render();
    }
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

  /* Suggest mode: load an automation and add a track for each entity it references that
   * has none yet, keeping the tracks, their edits and the labels already there. */
  async _seedFromAutomation(entityId) {
    this._entityId = entityId;
    this._clearResult();
    this._cfgDraft = null;
    this._cfgError = null;
    this._error = null;
    this._notice = null;
    const token = this._requestToken;
    this._loading = "Loading automation…";
    this._render();
    try {
      const automation = await this._hass.callWS({ type: "event_simulator/automation", entity_id: entityId });
      if (token !== this._requestToken) return;
      this._automation = automation;
      this._renderConfig();
      const have = new Set(this._tracks.map((t) => t.entity_id));
      const fresh = (automation.referenced_entities || []).filter((id) => !have.has(id));
      const room = Math.max(0, MAX_ENTITIES - this._tracks.length);
      if (fresh.length > room) this._notice = `Only ${room} of the ${fresh.length} entities ${entityId} references were added: at most ${MAX_ENTITIES} tracks.`;
      const add = fresh.slice(0, room);
      for (const id of add) this._tracks.push({ entity_id: id, initial: null, events: [] });
      if (!add.length && !this._notice) this._notice = `Every entity ${entityId} references already has a track.`;
      this._markStale();
      await this._loadHistory(add.length ? add : []);
    } catch (e) {
      if (token !== this._requestToken) return;
      this._loading = null;
      this._error = `Could not load ${entityId}: ${e.message || e}`;
      this._render();
    }
  }

  /* A failed history load in words. A window longer than the history command takes says
   * so plainly, since Suggest mode offers windows the backend may not load yet. */
  _historyError(e) {
    if (e?.code === "window_too_long") {
      const span = formatDuration(this._window.end - this._window.start);
      return `This window is ${span}, and this version of the integration loads at most ${MAX_WINDOW / DAY} days of history at a time, so it cannot load yet. Pick ${MAX_WINDOW / DAY} days or less for now.`;
    }
    return `Could not load history: ${e.message || e}`;
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
      const byEntity = await this._fetchHistory(ids);
      if (token !== this._requestToken) return;
      for (const track of this._tracks) {
        const recorded = byEntity.get(track.entity_id);
        if (!recorded) continue;
        track.initial = recorded.initial;
        track.events = recorded.events;
        delete track.shift;
        track.recorded = recorded;
        this._prepareTrack(track);
      }
    } catch (e) {
      if (token !== this._requestToken) return;
      this._error = this._historyError(e);
      // Whatever these tracks showed belongs to another window (or none): show them empty.
      for (const track of this._tracks) {
        if (!ids.includes(track.entity_id)) continue;
        Object.assign(track, { initial: null, events: [], recorded: { initial: null, events: [] } });
        delete track.shift;
        this._prepareTrack(track);
      }
    }
    this._loading = null;
    this._render();
  }

  /* Recorded timelines of `ids` in the current window: Map entity_id -> {initial, events}. */
  async _fetchHistory(ids) {
    const timeline = await this._hass.callWS({
      type: "event_simulator/history",
      entity_ids: ids,
      start: new Date(this._window.start).toISOString(),
      end: new Date(this._window.end).toISOString(),
    });
    const byEntity = new Map(ids.map((id) => [id, []]));
    for (const ev of timeline.events || []) byEntity.get(ev.entity_id)?.push(ev);
    return new Map(
      ids.map((id) => [
        id,
        {
          initial: timeline.initial_states?.[id] || null,
          // Stable sort: the editing helpers rely on time order, same-instant writes keep theirs.
          events: byEntity.get(id).sort((a, b) => parseTime(a.at) - parseTime(b.at)),
        },
      ])
    );
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
    if (end - start > this._maxWindow()) {
      this._error =
        this._mode === "suggest"
          ? `The window can be at most ${MAX_SUGGEST_WINDOW / DAY} days long in Suggest mode.`
          : `The window can be at most ${MAX_WINDOW / DAY} days long (${MAX_SUGGEST_WINDOW / DAY} in Suggest mode).`;
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
    if (this._hasTimeline()) this._loadHistory();
    else this._render();
  }

  _maxWindow() {
    return this._mode === "suggest" ? MAX_SUGGEST_WINDOW : MAX_WINDOW;
  }

  /* Whether there is a timeline to show: an automation's, or Suggest mode's own tracks. */
  _hasTimeline() {
    return !!this._automation || this._mode === "suggest";
  }

  /* Switch between Replay and Suggest. Both share the window, tracks, edits and labels; a
   * window longer than Replay allows is cut to its last 7 days on the way back. */
  _setMode(mode) {
    if (mode === this._mode || !["replay", "suggest"].includes(mode)) return;
    this._mode = mode;
    this._labelDraft = null;
    this._labelDrag = null;
    this._labelError = null;
    this._notice = null;
    if (mode === "replay") {
      this._labelSel = null;
      const { start, end } = this._window;
      if (end - start > MAX_WINDOW) {
        this._setWindow(end - MAX_WINDOW, end, "7d");
        this._notice = [`Replay windows are at most ${MAX_WINDOW / DAY} days, so the window is now the last ${MAX_WINDOW / DAY} days of the Suggest one.`, this._notice].filter(Boolean).join(" ");
      }
    }
    this._render();
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
    this._drag = null;
    this._labelSel = null;
    this._labelDraft = null;
    this._labelDrag = null;
    this._labelEdDraft = null;
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
    if (sel) this._labelSel = null;
  }

  /* Replace tracks' timelines as one undoable edit. `changes` is [{entity_id, timeline}];
   * `select` is what to show in the editor afterwards. */
  _commit(label, changes, select = undefined) {
    const snapshots = [];
    for (const { entity_id, timeline } of changes) {
      const track = this._track(entity_id);
      if (!track || (timeline.initial === track.initial && timeline.events === track.events && timeline.shift === track.shift)) continue;
      snapshots.push({ entity_id, timeline: { initial: track.initial, events: track.events, shift: track.shift } });
      this._setTimeline(track, timeline);
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

  /* A track's timeline replaced: its state at the start, its events, and its shift. */
  _setTimeline(track, timeline) {
    track.initial = timeline.initial;
    track.events = timeline.events;
    if (timeline.shift) track.shift = timeline.shift;
    else delete track.shift;
    this._prepareTrack(track);
  }

  _undoEdit() {
    const entry = this._undo.pop();
    if (!entry) return;
    if (entry.labels) {
      this._labels = entry.labels;
      if (!this._labels.some((l) => l.id === this._labelSel)) this._labelSel = null;
      this._labelDraft = null;
      this._labelEdDraft = null;
      this._labelError = null;
      this._labelsChanged();
      this._notice = `Undid: ${entry.label}.`;
      return this._render();
    }
    for (const { entity_id, timeline } of entry.snapshots) {
      const track = this._track(entity_id);
      if (track) this._setTimeline(track, timeline);
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

  // ---- labels (Suggest mode) ----------------------------------------------------

  _label(id) {
    return this._labels.find((l) => l.id === id) || null;
  }

  /* Place a new or edited label as one undoable edit (see placeLabel for the overlap rules).
   * A refusal leaves the labels as they were and says why. */
  _placeLabel(undoLabel, label) {
    const r = placeLabel(this._labels, label);
    if (!r.ok) {
      this._labelError = r.error;
      return this._render();
    }
    this._undo.push({ label: undoLabel, snapshots: [], labels: this._labels });
    this._labels = r.labels;
    this._labelSel = r.label.id;
    this._sel = null;
    this._labelEdDraft = null;
    this._labelError = null;
    const merged = r.merged.length;
    this._notice = merged ? `Merged with ${merged} overlapping ${label.kind === "fire" ? "should-fire" : "must-not-fire"} period${merged === 1 ? "" : "s"}.` : null;
    this._labelsChanged();
    this._render();
  }

  _deleteLabel(id) {
    if (!this._label(id)) return;
    this._undo.push({ label: "delete period", snapshots: [], labels: this._labels });
    this._labels = removeLabel(this._labels, id);
    if (this._labelSel === id) this._labelSel = null;
    this._labelEdDraft = null;
    this._labelError = null;
    this._labelsChanged();
    this._render();
  }

  _selectLabel(id) {
    this._select(null);
    this._labelSel = id;
    this._labelEdDraft = null;
    this._labelError = null;
  }

  /* The kind picked for a brushed period: create it and remember the choice for next time. */
  _createLabel(kind, anchor) {
    const draft = this._labelDraft;
    if (!draft) return;
    this._labelDraft = null;
    this._labelDefault = { kind, anchor: kind === "fire" ? anchor : this._labelDefault.anchor };
    writeLabelDefault(this._labelDefault);
    this._placeLabel("add period", { start: draft.start, end: draft.end, kind, anchor: this._labelDefault.anchor });
    this.shadowRoot.getElementById("tl-wrap")?.focus({ preventScroll: true });
  }

  /* Kind and anchor apply as soon as they are picked in the label editor. */
  _updateLabel(id, changes) {
    const label = this._label(id);
    if (!label) return;
    const next = { ...label, ...changes };
    if (changes.kind === "fire" && !LABEL_ANCHORS.includes(label.anchor)) next.anchor = this._labelDefault.anchor;
    this._placeLabel(changes.kind ? "change period kind" : "change anchor", next);
  }

  /* The label editor's exact start and end. */
  _applyLabelEditor() {
    const label = this._label(this._labelSel);
    if (!label) return;
    const root = this.shadowRoot;
    const startText = root.getElementById("lb-start")?.value;
    const endText = root.getElementById("lb-end")?.value;
    const start = startText === toLocalInputSeconds(label.start) ? label.start : fromLocalInput(startText);
    const end = endText === toLocalInputSeconds(label.end) ? label.end : fromLocalInput(endText);
    const fail = (message) => {
      this._labelError = message;
      this._render();
    };
    if (!Number.isFinite(start) || !Number.isFinite(end)) return fail("Enter a start and an end.");
    if (end <= start) return fail("The period must end after it starts.");
    if (start < this._window.start || end > this._window.end) {
      return fail(`The period must be inside the window (${this._fullTime(this._window.start)} to ${this._fullTime(this._window.end)}).`);
    }
    if (start === label.start && end === label.end) {
      this._labelEdDraft = null;
      return this._render();
    }
    this._placeLabel("edit period", { ...label, start, end });
  }

  /* Labels changed: the save state and anything suggested from the old ones are out of date. */
  _labelsChanged() {
    if (this._suggestState?.result) this._suggestState = { ...this._suggestState, stale: true };
    this._updateDirty();
  }

  // ---- suggest ------------------------------------------------------------------

  /* Ask the backend whether it has suggest/subscribe: a request with no fields is refused
   * as unknown when it does not, and as malformed when it does. */
  async _probeSuggest() {
    try {
      await this._hass.callWS({ type: "event_simulator/suggest/subscribe" });
      this._suggestAvail = true;
    } catch (e) {
      this._suggestAvail = suggestAvailable(e);
    }
    if (this._mode === "suggest") this._render();
  }

  _canSuggest() {
    return (
      this._suggestAvail !== false &&
      !this._loading &&
      this._tracks.length > 0 &&
      labelsInWindow(this._labels, this._window.start, this._window.end).some((l) => l.kind === "fire")
    );
  }

  /* Press Suggest: subscribe to suggest/subscribe (newest wins: a running one is dropped).
   * The results UI is the next panel unit; this shows progress, errors and a count. */
  _runSuggest() {
    if (!this._canSuggest()) return;
    this._cancelSuggest();
    const message = suggestMessage({
      entityIds: this._tracks.map((t) => t.entity_id),
      start: this._window.start,
      end: this._window.end,
      labels: this._labels,
      tolerance: this._tolerance,
    });
    const tooLarge = messageSizeError(message);
    if (tooLarge) {
      this._suggestState = { running: false, error: describeRunError(tooLarge) };
      return this._render();
    }
    const run = { done: false, unsub: null };
    this._suggestRun = run;
    this._suggestState = { running: true, text: "Starting…" };
    this._hass.connection
      .subscribeMessage((event) => this._onSuggestEvent(run, event), message, { resubscribe: false })
      .then((unsub) => {
        run.unsub = unsub;
        if (run.done) this._unsubscribe(run);
      })
      .catch((err) => this._onSuggestEvent(run, { error: err }));
    this._render();
  }

  _cancelSuggest() {
    const run = this._suggestRun;
    if (!run) return;
    run.done = true;
    this._suggestRun = null;
    this._unsubscribe(run);
  }

  _onSuggestEvent(run, event) {
    if (run.done || run !== this._suggestRun) return;
    const outcome = suggestOutcome(event);
    if (outcome.kind === "progress") {
      this._suggestState = { ...this._suggestState, running: true, text: outcome.text };
      return this._renderSoon();
    }
    this._cancelSuggest();
    if (outcome.kind === "unavailable") {
      this._suggestAvail = false;
      this._suggestState = null;
    } else if (outcome.kind === "error") {
      this._suggestState = { running: false, error: outcome.error };
    } else {
      this._suggestState = { running: false, result: { suggestions: outcome.suggestions, hints: outcome.hints } };
    }
    this._renderSoon();
  }

  _setTolerance(which, text) {
    const check = checkToleranceMinutes(text);
    if (!check.ok) {
      this._tolError = check.error;
      return this._render();
    }
    this._tolError = null;
    if (this._tolerance[which] === check.value) return this._render();
    this._tolerance = { ...this._tolerance, [which]: check.value };
    this._labelsChanged();
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
    this._updateConfigDiff();
    this._updateDirty();
    // Run's enabled state and hint live in the main area; redraw it only on a change of state.
    if (before[0] !== (this._cfgDraft !== null) || before[1] !== !!this._cfgError) this._render();
    else this._syncReady();
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
          <div id="cfg-editor"><div id="cfg-marks" aria-hidden="true"></div></div>
          <div id="cfg-error" class="err small" hidden></div>
          <div id="cfg-diff"></div>
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
      this._watchConfigEditor(el);
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
    this._updateConfigDiff();
  }

  /* The saved automation's config as the editor would show it: dumped by a detached
   * ha-yaml-editor (the same js-yaml dump and schema the visible one used for its text),
   * or as the fallback textarea's JSON. Cached per config. */
  _savedConfigText() {
    const saved = this._savedConfig();
    const yaml = this._yamlState === "ready";
    if (this._savedText?.config === saved && this._savedText.yaml === yaml) return this._savedText.text;
    let text = null;
    if (yaml) {
      try {
        const dumper = document.createElement("ha-yaml-editor");
        dumper.setValue(saved);
        text = typeof dumper.yaml === "string" ? dumper.yaml : null;
      } catch (e) {
        console.debug("event-simulator: could not dump the saved config as YAML", e);
      }
    }
    if (text === null) text = JSON.stringify(saved, null, 2);
    this._savedText = { config: saved, yaml, text };
    return text;
  }

  /* The editor's text as typed. */
  _configText() {
    const el = this._cfgEl;
    if (!el) return null;
    if (el.tagName === "TEXTAREA") return el.value;
    if (typeof el.yaml === "string") return el.yaml;
    return this._codeMirror()?.state.doc.toString() ?? null;
  }

  /* The CodeMirror view inside ha-yaml-editor's ha-code-editor (its public `codemirror`),
   * or null where this frontend does not have one to give. */
  _codeMirror() {
    const view = this._cfgEl?.shadowRoot?.querySelector("ha-code-editor")?.codemirror;
    return view && typeof view.lineBlockAt === "function" && view.state?.doc ? view : null;
  }

  /* Keep the line marks on the editor's lines as it lays out, resizes and scrolls. */
  _watchConfigEditor(el) {
    this._cfgObserver?.disconnect();
    if (globalThis.ResizeObserver) {
      this._cfgObserver = new ResizeObserver(() => this._placeConfigMarks());
      this._cfgObserver.observe(el);
    }
    // The code editor renders a moment after it is attached.
    let tries = 0;
    const hook = () => {
      if (this._cfgEl !== el) return;
      const view = this._codeMirror();
      if (!view) {
        if (tries++ < 40) setTimeout(hook, 100);
        return;
      }
      view.scrollDOM.addEventListener("scroll", () => this._placeConfigMarks(), { passive: true });
      this._updateConfigDiff();
    };
    hook();
  }

  /* The lines the edited config adds or changes against the saved one, and where saved
   * lines were removed: the summary and compact diff under the editor, and the marks on
   * the editor's own lines. A config that parses to the saved one marks nothing, however
   * it is laid out (keys reordered, comments added). */
  _updateConfigDiff() {
    const host = this.shadowRoot.getElementById("cfg-diff");
    if (!host) return;
    const text = this._configText();
    const differs = this._cfgDraft !== null || !!this._cfgError;
    const savedText = this._savedConfigText();
    const marks = differs && text !== null ? configLineMarks(savedText, text) : null;
    this._cfgMarks = marks;
    const { added = 0, changed = 0, removed = 0 } = marks?.counts || {};
    if (!added && !changed && !removed) {
      host.innerHTML = "";
      this._placeConfigMarks();
      return;
    }
    const lines = (n, what) => `${n} line${n === 1 ? "" : "s"} ${what}`;
    const summary = [changed && lines(changed, "changed"), added && lines(added, "added"), removed && lines(removed, "removed")].filter(Boolean).join(" · ");
    const hunks = configDiffHunks(savedText, text);
    const rows = hunks
      .map((hunk) =>
        hunk
          .map((r) => `<div class="d-row d-${r.kind === "+" ? "add" : r.kind === "-" ? "del" : "ctx"}"><span class="d-n">${r.n ?? ""}</span><span class="d-k">${r.kind}</span><span class="d-t">${esc(r.text) || " "}</span></div>`)
          .join("")
      )
      .join(`<div class="d-gap">⋯</div>`);
    host.innerHTML = `<details class="cfg-diff" data-key="cfg-diff" ${this._openAttr("cfg-diff")}>
        <summary><span class="d-legend"><i class="d-bar changed"></i><i class="d-bar added"></i></span>${esc(summary)} against the saved automation</summary>
        <div class="d-body mono">${rows}</div>
      </details>`;
    this._placeConfigMarks();
  }

  /* Bars beside the editor's added and changed lines and a marker where lines were
   * removed, laid over the editor (CodeMirror owns its own DOM, and this frontend hands
   * out no way to decorate it from outside). Positions come from the view's own line
   * geometry, clipped to its scroller. */
  _placeConfigMarks() {
    const layer = this.shadowRoot.getElementById("cfg-marks");
    if (!layer) return;
    const marks = this._cfgMarks;
    const view = marks && this._codeMirror();
    if (!view) {
      layer.innerHTML = "";
      return;
    }
    cancelAnimationFrame(this._cfgFrame);
    // After CodeMirror has measured the change that triggered this.
    this._cfgFrame = requestAnimationFrame(() => {
      const host = layer.parentElement.getBoundingClientRect();
      const scroller = view.scrollDOM.getBoundingClientRect();
      const doc = view.state.doc;
      const top = view.documentTop - scroller.top;
      const out = [];
      marks.lines.forEach((kind, i) => {
        if (!kind || i + 1 > doc.lines) return;
        const block = view.lineBlockAt(doc.line(i + 1).from);
        out.push(`<div class="m-line ${kind}" style="top:${top + block.top}px;height:${block.height}px"></div>`);
      });
      for (const { before, lines } of marks.removed) {
        const y =
          before + 1 <= doc.lines
            ? view.lineBlockAt(doc.line(before + 1).from).top
            : view.lineBlockAt(doc.line(doc.lines).from).bottom;
        out.push(`<div class="m-removed" style="top:${top + y}px" title="${lines.length} line${lines.length === 1 ? "" : "s"} removed"></div>`);
      }
      layer.style.cssText = `left:${scroller.left - host.left}px;top:${scroller.top - host.top}px;width:${scroller.width}px;height:${scroller.height}px`;
      layer.innerHTML = out.join("");
    });
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

  /* The result no longer applies (another automation, window or scenario): drop it and
   * anything in flight, and replay afresh once the new inputs have loaded. */
  _clearResult() {
    this._result = null;
    this._runError = null;
    this._selectedRun = null;
    this._resultStale = false;
    this._resultPrint = null;
    this._scheduler.invalidate();
    this._scheduler.change();
  }

  /* Something a replay depends on changed: the shown result is out of date, and auto-run
   * replays once the changes pause. */
  _markStale() {
    if (this._result) this._resultStale = true;
    this._scheduler.change();
  }

  /* Whether a replay can start now: an automation, no load pending, a config that parses. */
  _syncReady() {
    this._scheduler.setReady(this._mode === "replay" && !!this._automation && !this._loading && !this._cfgError);
  }

  _run() {
    this._scheduler.runNow();
  }

  _setAutoRun(on) {
    this._autoRun = !!on;
    writeAutoRunPref(this._autoRun);
    this._scheduler.setEnabled(this._autoRun);
    this._render();
  }

  /* ReplayScheduler's `start`: send replay `seq`, or skip it when nothing changed since the
   * shown result (unless forced by Run now) or the request is too large to send. */
  _startReplay(seq, { forced }) {
    const print = this._replayPrint();
    if (!forced && this._result && !this._runError && print === this._resultPrint) {
      this._resultStale = false;
      this._renderSoon();
      return null;
    }
    const message = simulateSubscribeMessage(
      buildScenario({ automation: this._runConfig(), start: this._window.start, end: this._window.end, tracks: this._tracks })
    );
    const size = messageBytes(message);
    const tooLarge = messageSizeError(message, size);
    if (tooLarge) {
      this._runError = describeRunError(tooLarge);
      this._renderSoon();
      return null;
    }
    const replay = { seq, print, unsub: null, done: false };
    this._replay = replay;
    this._hass.connection
      .subscribeMessage((event) => this._onReplayEvent(replay, event), message, { resubscribe: false })
      .then((unsub) => {
        replay.unsub = unsub;
        if (replay.done) this._unsubscribe(replay);
      })
      .catch((err) => this._onReplayEvent(replay, { error: err }));
    this._renderSoon();
    return { size };
  }

  /* ReplayScheduler's `cancel`: abandon replay `seq`; unsubscribing kills its child. */
  _cancelReplay(seq) {
    const replay = this._replay;
    if (!replay || replay.seq !== seq) return;
    replay.done = true;
    this._replay = null;
    this._unsubscribe(replay);
  }

  _unsubscribe(replay) {
    const unsub = replay.unsub;
    replay.unsub = null;
    // Rejects when the connection dropped meanwhile, which ended the replay anyway.
    if (unsub) Promise.resolve().then(unsub).catch(() => {});
  }

  _onReplayEvent(replay, event) {
    if (replay.done) return;
    replay.done = true;
    this._unsubscribe(replay);
    if (this._replay === replay) this._replay = null;
    if (!this._scheduler.finish(replay.seq)) return; // a newer replay replaced it
    const outcome = replayOutcome(event);
    if (outcome.kind === "error") {
      this._runError = outcome.error;
      if (this._result) this._resultStale = true;
    } else if (outcome.kind === "result") {
      const selected = this._selectedRun;
      this._result = normalizeResult(outcome.result);
      this._runError = null;
      this._resultPrint = replay.print;
      this._resultStale = this._scheduler.dirty || this._replayPrint() !== replay.print;
      // Keep the run being inspected across replays when it is still there.
      const runs = this._result.runs;
      this._selectedRun = runs.some((r) => r.index === selected) ? selected : (runs[0]?.index ?? null);
    }
    this._renderSoon();
  }

  /* Render now, or once the drag in progress ends: a redraw replaces the timeline the
   * pointer is captured by. */
  _renderSoon() {
    if (this._evDrag || this._drag || this._thumbDrag || this._labelDrag) {
      this._renderDeferred = true;
      return;
    }
    this._render();
  }

  _flushRender() {
    if (!this._renderDeferred || this._evDrag || this._drag || this._thumbDrag || this._labelDrag) return;
    this._renderDeferred = false;
    this._render();
  }


  // ---- saved scenarios --------------------------------------------------------

  _payload(name = "") {
    return scenarioPayload({
      name,
      entityId: this._entityId,
      automation: this._runConfig(),
      start: this._window.start,
      end: this._window.end,
      tracks: this._tracks,
      labels: this._labels,
      suggestOptions: this._mode === "suggest" ? suggestOptions(this._tolerance) : null,
    });
  }

  /* scenarioFingerprint of the current state, labels and Suggest options included.
   * Timelines and label lists are immutable and replaced on every edit, so the costly
   * part is cached on their identity. */
  _fingerprint() {
    return this._prints().full;
  }

  /* The same without labels and Suggest options: what a replay depends on. */
  _replayPrint() {
    return this._prints().replay;
  }

  _prints() {
    const parts = [this._entityId, this._window.start, this._window.end, this._labels, this._mode, this._tolerance, ...this._tracks.flatMap((t) => [t.entity_id, t.initial, t.events, t.shift])];
    const c = this._printCache;
    if (!c || c.parts.length !== parts.length || c.parts.some((x, i) => x !== parts[i]) || c.config !== this._runConfig()) {
      const payload = this._payload();
      const { labels, suggest_options, ...replay } = payload;
      this._printCache = { parts, config: this._runConfig(), full: scenarioFingerprint(payload), replay: scenarioFingerprint(replay) };
    }
    return this._printCache;
  }

  /* "saved", "dirty", "new" (never saved) or null (nothing to save yet). */
  _saveState() {
    if (!this._hasTimeline()) return null;
    if (!this._scenario) return "new";
    return this._fingerprint() === this._savedPrint ? "saved" : "dirty";
  }

  _canSave() {
    return this._hasTimeline() && !this._loading && !this._cfgError && !this._scBusy;
  }

  /* The save-state chip, redrawn on its own while the config editor is typed in. */
  _updateDirty() {
    const chip = this.shadowRoot.getElementById("sc-state");
    if (!chip) return;
    const state = this._saveState();
    const [cls, text, title] = {
      saved: ["pass", "saved", "Matches the saved scenario"],
      dirty: ["edited", "unsaved changes", "Differs from the saved scenario; Save to keep the changes"],
      new: ["none", "not saved", "Save keeps this automation, window, tracks and edits as a named scenario"],
    }[state] || ["", "", ""];
    chip.hidden = !state;
    chip.className = `chip ${cls}`;
    chip.textContent = text;
    chip.title = title;
  }

  async _listScenarios() {
    try {
      this._scenarios = await this._hass.callWS({ type: "event_simulator/scenarios/list" });
    } catch (e) {
      this._scenarios = this._scenarios || [];
      this._scError = scenarioErrorMessage(e, "list");
    }
    this._render();
  }

  _defaultName() {
    const alias = this._automation?.config?.alias || this._entityId || (this._mode === "suggest" ? "Suggest" : "Scenario");
    const d = new Date(this._window.start);
    return `${alias} — ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`.slice(0, MAX_SCENARIO_NAME);
  }

  /* Save: overwrite the loaded scenario, or ask for a name when there is none. */
  _onSave(asNew) {
    this._scError = null;
    this._scInfo = null;
    this._confirm = null;
    if (!asNew && this._scenario) return this._saveScenario(false, this._scenario.name);
    const base = this._scenario ? `${this._scenario.name} (copy)` : this._defaultName();
    this._saveForm = { name: base.slice(0, MAX_SCENARIO_NAME), asNew };
    this._focusName = true;
    this._render();
  }

  async _saveScenario(asNew, name) {
    if (!this._canSave()) return;
    const check = checkScenarioName(name);
    if (!check.ok) {
      this._scError = check.error;
      return this._render();
    }
    const payload = this._payload(check.value);
    const overwrite = !asNew && this._scenario ? this._scenario.scenario_id : null;
    const message = saveMessage(payload, overwrite);
    const tooLarge = messageSizeError(message);
    if (tooLarge) {
      this._scError = `This scenario is too large to save. ${tooLarge.message}`;
      return this._render();
    }
    this._scBusy = true;
    this._scError = null;
    this._render();
    try {
      const summary = await this._hass.callWS(message);
      this._scenario = { scenario_id: summary.scenario_id, name: summary.name };
      this._savedPrint = scenarioFingerprint(payload);
      this._saveForm = null;
      this._scInfo = `Saved “${summary.name}”.`;
    } catch (e) {
      this._scError = scenarioErrorMessage(e, "save");
      if (e?.code === "not_found" && overwrite) {
        // Deleted elsewhere: what is on screen is now an unsaved scenario.
        this._scenario = null;
        this._savedPrint = null;
        this._scError = "The loaded scenario was deleted elsewhere, so there is nothing to overwrite. Use Save as to keep this one.";
      }
    }
    this._scBusy = false;
    await this._listScenarios();
  }

  async _loadScenario(id, confirmed = false) {
    if (!confirmed && this._saveState() === "dirty") {
      this._confirm = { kind: "load", id };
      return this._render();
    }
    this._confirm = null;
    this._scError = null;
    this._scInfo = null;
    this._saveForm = null;
    this._scBusy = true;
    this._render();
    let record;
    try {
      record = await this._hass.callWS({ type: "event_simulator/scenarios/get", scenario_id: id });
    } catch (e) {
      this._scBusy = false;
      this._scError = scenarioErrorMessage(e, "load");
      if (e?.code === "not_found") return this._listScenarios();
      return this._render();
    }
    const token = ++this._requestToken;
    const start = parseTime(record.start);
    const end = parseTime(record.end);
    const entityId = record.automation_entity_id || "";
    this._entityId = entityId;
    this._automation = null;
    this._tracks = [];
    this._clearResult();
    this._clearEdits();
    this._cancelSuggest();
    this._suggestState = null;
    // Saved from Suggest mode (it has options) or not; labels load either way.
    this._mode = isObject(record.suggest_options) ? "suggest" : "replay";
    this._labels = labelsFromScenario(record.labels);
    this._tolerance = toleranceFromOptions(record.suggest_options);
    this._tolError = null;
    this._labelError = null;
    this._cfgDraft = null;
    this._cfgError = null;
    this._error = null;
    this._notice = null;
    this._preset = null;
    this._window = { start, end };
    this._view = [start, end];
    this._scenario = { scenario_id: record.scenario_id, name: record.name };
    this._savedPrint = null;
    this._loading = `Loading scenario “${record.name}”…`;
    this._renderConfig();
    this._render();
    const notices = [];
    let live = null;
    if (entityId) {
      try {
        live = await this._hass.callWS({ type: "event_simulator/automation", entity_id: entityId });
      } catch (e) {
        notices.push(`${entityId} could not be loaded (${e.message || e}), so the scenario runs its saved config.`);
      }
    }
    if (token !== this._requestToken) return;
    const saved = isObject(record.automation) ? record.automation : {};
    // A Suggest scenario saved without an automation has none to replay.
    this._automation = live || (entityId || Object.keys(saved).length ? { config: saved, referenced_entities: [] } : null);
    // The saved config is what Run sent; against the live automation it is an edit or not.
    this._cfgDraft = !this._automation || stableStringify(saved) === stableStringify(this._automation.config) ? null : saved;
    this._renderConfig();
    const loaded = tracksFromScenario(record);
    let recorded = new Map();
    if (loaded.length) {
      try {
        recorded = await this._fetchHistory(loaded.map((t) => t.entity_id));
      } catch (e) {
        notices.push(`The recorded history could not be loaded (${e.message || e}), so the tracks cannot be compared with it or reset to it.`);
      }
    }
    if (token !== this._requestToken) return;
    this._tracks = loaded.map((track) => {
      const history = recorded.get(track.entity_id) || null;
      const timeline = adoptRecorded(track, history);
      const out = { entity_id: track.entity_id, ...timeline, recorded: history || timeline };
      this._prepareTrack(out);
      return out;
    });
    this._savedPrint = this._fingerprint();
    this._loading = null;
    this._scBusy = false;
    this._scInfo = `Loaded “${record.name}”.`;
    this._notice = notices.join(" ") || null;
    this._render();
  }

  async _deleteScenario(id, confirmed = false) {
    if (!confirmed) {
      this._confirm = { kind: "delete", id };
      return this._render();
    }
    this._confirm = null;
    this._scError = null;
    this._scInfo = null;
    this._scBusy = true;
    this._render();
    const name = this._scenarios?.find((s) => s.scenario_id === id)?.name || "the scenario";
    try {
      await this._hass.callWS({ type: "event_simulator/scenarios/delete", scenario_id: id });
      this._scInfo = `Deleted “${name}”.`;
    } catch (e) {
      if (e?.code !== "not_found") this._scError = scenarioErrorMessage(e, "delete");
    }
    if (this._scenario?.scenario_id === id) {
      // What is on screen stays; it is just no longer saved anywhere.
      this._scenario = null;
      this._savedPrint = null;
    }
    this._scBusy = false;
    await this._listScenarios();
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
      // `toggle` does not bubble; capture still sees it. Keeps <details> open across redraws.
      this.shadowRoot.addEventListener("toggle", (ev) => {
        const key = ev.target?.dataset?.key;
        if (key) this._open[key] = ev.target.open;
      }, true);
    }
    const root = this.shadowRoot.getElementById("root");
    const addValue = root.querySelector("#add-track")?.value || "";
    // The timeline keeps keyboard focus across a redraw, so Delete and Ctrl+Z keep working.
    const timelineFocused = this.shadowRoot.activeElement?.id === "tl-wrap";
    const nameInput = this.shadowRoot.activeElement?.id === "sc-name" ? this.shadowRoot.activeElement : null;
    const caret = nameInput ? [nameInput.selectionStart, nameInput.selectionEnd] : null;
    // Replays finish whenever they finish, so a redraw can land while a field is typed in:
    // keep that field's focus, unsent text and caret.
    const typing = this._focusedField();
    // Each redraw makes new ha-cards, which have no height until they render their slot a
    // microtask later; the layout reads below would see the page collapse meanwhile and the
    // browser would clamp a scrolled page back toward the top. Hold the height until then.
    const page = this.shadowRoot.querySelector(".page");
    page.style.minHeight = `${page.offsetHeight}px`;
    root.innerHTML = this._html();
    this.shadowRoot.getElementById("detail").innerHTML = this._detailCard();
    const held = ++this._heldRender;
    Promise.all([...page.querySelectorAll("ha-card")].map((card) => card.updateComplete))
      .catch(() => {})
      .then(() => {
        if (held === this._heldRender) page.style.minHeight = "";
      });
    // Suggest mode has no automation to edit; the config card keeps its editor, hidden.
    this.shadowRoot.getElementById("config").hidden = this._mode === "suggest";
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
    // A just-brushed period: its kind popover takes the keyboard, the remembered kind first.
    if (this._focusPop) {
      const pop = this.shadowRoot.getElementById("label-pop");
      // A new ha-card renders its slot asynchronously; until then its content cannot take focus.
      Promise.resolve(pop?.closest("ha-card")?.updateComplete).then(() => pop?.querySelector("[data-default]")?.focus({ preventScroll: true }));
    }
    this._focusPop = false;
    const name = this.shadowRoot.getElementById("sc-name");
    if (name && (caret || this._focusName)) {
      const scroll = this._focusName;
      // A new ha-card renders its slot asynchronously; until then its content cannot take focus.
      Promise.resolve(name.closest("ha-card")?.updateComplete).then(() => {
        if (!name.isConnected) return;
        name.focus({ preventScroll: !scroll });
        if (caret) name.setSelectionRange(...caret);
        else name.select();
      });
    }
    this._focusName = false;
    if (typing) this._restoreField(typing);
    this._updateDirty();
    this._syncReady();
  }

  _focusedField() {
    const el = this.shadowRoot.activeElement;
    if (!el?.id || el.id === "sc-name" || !/^(INPUT|TEXTAREA)$/.test(el.tagName) || el.type === "checkbox") return null;
    let sel = null;
    try {
      sel = el.selectionStart == null ? null : [el.selectionStart, el.selectionEnd];
    } catch {
      // datetime-local has no text selection
    }
    return { id: el.id, value: el.value, sel };
  }

  _restoreField({ id, value, sel }) {
    const el = this.shadowRoot.getElementById(id);
    if (!el) return;
    if (el.value !== value) el.value = value;
    el.focus({ preventScroll: true });
    try {
      if (sel) el.setSelectionRange(...sel);
    } catch {
      // not a text field
    }
  }

  /* `open` for a <details data-key>, remembered across redraws. */
  _openAttr(key, byDefault = false) {
    return (this._open[key] ?? byDefault) ? "open" : "";
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
    const suggest = this._mode === "suggest";
    const presets = PRESETS.filter((p) => suggest || !p.suggest).map(
      (p) => `<button class="${this._preset === p.id ? "on" : ""}" data-action="preset" data-preset="${p.id}">${p.label}</button>`
    ).join("");
    const modes = [
      ["replay", "Replay", "Replay recorded history against an automation"],
      ["suggest", "Suggest", "Label periods of history and get a trigger and conditions suggested"],
    ]
      .map(([id, text, title]) => `<button class="${this._mode === id ? "on" : ""}" data-action="mode" data-mode="${id}" title="${esc(title)}" aria-pressed="${this._mode === id}">${text}</button>`)
      .join("");
    return `
      <ha-card>
        <div class="card-content setup">
          <div class="field">
            <span class="label">Mode</span>
            <div class="segmented mode" role="group" aria-label="Mode">${modes}</div>
          </div>
          <div class="field grow">
            <span class="label">${suggest ? "Seed tracks from an automation (optional)" : "Automation"}</span>
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
          ${suggest ? this._suggestRowHtml() : `<div class="run-row">
            <label class="auto-run" title="Replay automatically after each change">
              <input type="checkbox" id="auto-run" ${this._autoRun ? "checked" : ""}> Auto-run
            </label>
            <button class="${this._autoRun ? "" : "primary"}" data-action="run" ${!this._automation || this._loading || this._cfgError ? "disabled" : ""} title="Replay now, even when nothing changed">
              Run now
            </button>
            ${this._running ? `<span class="spinner" aria-label="Replaying"></span>` : ""}
            <span class="muted small">${esc(this._runHint())}</span>
          </div>`}
          ${this._scenarioHtml()}
        </div>
      </ha-card>`;
  }

  /* Suggest mode's row in place of Run: the match tolerance, the Suggest button and what it
   * is doing. The suggestions list itself is the next panel unit. */
  _suggestRowHtml() {
    const tol = this._tolerance;
    const minutes = (ms) => String(Math.round((ms / 60e3) * 100) / 100);
    const st = this._suggestState;
    const inWindow = labelsInWindow(this._labels, this._window.start, this._window.end);
    const fires = inWindow.filter((l) => l.kind === "fire").length;
    let status;
    if (this._suggestAvail === false) status = `<span class="muted small" id="suggest-status">Suggestions are not available yet.</span>`;
    else if (st?.running) status = `<span class="spinner" aria-label="Suggesting"></span><span class="muted small" id="suggest-status">${esc(st.text || "Working…")}</span>`;
    else if (st?.error) status = `<span class="err small" id="suggest-status" role="alert">${esc(st.error.title)}: ${esc(st.error.message)}</span>`;
    else if (st?.result) {
      const n = st.result.suggestions.length;
      status = `<span class="muted small" id="suggest-status">${n} suggestion${n === 1 ? "" : "s"} found${st.stale ? " (labels changed since)" : ""}.</span>`;
    } else if (!this._tracks.length) status = `<span class="muted small" id="suggest-status">Add the entities to learn from as tracks.</span>`;
    else if (!fires) status = `<span class="muted small" id="suggest-status">Label at least one period where the automation should fire.</span>`;
    else status = `<span class="muted small" id="suggest-status">${fires} should-fire and ${inWindow.length - fires} must-not-fire period${inWindow.length - fires === 1 ? "" : "s"} labeled.</span>`;
    const title = this._suggestAvail === false ? "Suggestions are not available yet" : "Suggest a trigger and conditions from the labeled periods";
    return `
      <div class="run-row suggest-row">
        <div class="tolerance" role="group" aria-label="Match tolerance">
          <span class="label">Tolerance</span>
          <span class="small">a fire counts for an anchor from</span>
          <input id="tol-before" type="number" min="0" max="${MAX_TOLERANCE_MIN}" step="0.5" value="${minutes(tol.before)}" aria-label="Minutes before an anchor">
          <span class="small">min before to</span>
          <input id="tol-after" type="number" min="0" max="${MAX_TOLERANCE_MIN}" step="0.5" value="${minutes(tol.after)}" aria-label="Minutes after an anchor">
          <span class="small">min after it</span>
        </div>
        <button class="primary" data-action="suggest" ${this._canSuggest() && !st?.running ? "" : "disabled"} title="${esc(title)}">Suggest</button>
        ${status}
        ${this._tolError ? `<span class="err small" role="alert">${esc(this._tolError)}</span>` : ""}
      </div>`;
  }

  /* The saved-scenario controls: current name and save state, Save / Save as, the name
   * prompt, inline errors, and the list to load or delete from. */
  _scenarioHtml() {
    const list = this._scenarios;
    const busy = this._scBusy;
    const canSave = this._canSave();
    const current = this._scenario;
    const saveTitle = this._cfgError
      ? "Fix or revert the automation config first"
      : current
        ? `Overwrite “${current.name}”`
        : "Save as a named scenario";
    const form = this._saveForm
      ? `<div class="sc-form">
          <input id="sc-name" maxlength="${MAX_SCENARIO_NAME}" value="${esc(this._saveForm.name)}" placeholder="Scenario name" aria-label="Scenario name" autocomplete="off">
          <button class="primary small-btn" data-action="sc-save-name" ${canSave ? "" : "disabled"}>${this._saveForm.asNew && current ? "Save as new" : "Save"}</button>
          <button data-action="sc-cancel">Cancel</button>
        </div>`
      : "";
    const confirmLoad =
      this._confirm?.kind === "load"
        ? `<div class="alert warning sc-confirm"><div>Loading replaces what is on screen, and it has unsaved changes${current ? ` to “${esc(current.name)}”` : ""}.</div>
            <button data-action="sc-load-confirm" data-id="${esc(this._confirm.id)}">Discard and load</button>
            <button data-action="sc-confirm-cancel">Keep editing</button></div>`
        : "";
    const messages = [
      this._scError ? `<div class="err small sc-msg" role="alert">${esc(this._scError)}</div>` : "",
      this._scInfo ? `<div class="muted small sc-msg">${esc(this._scInfo)}</div>` : "",
    ].join("");
    const count = list ? ` (${list.length})` : "";
    return `
      <div class="sc">
        <div class="sc-row">
          <span class="label">Scenario</span>
          <strong class="sc-name">${current ? esc(current.name) : `<span class="muted">Unsaved</span>`}</strong>
          <span id="sc-state" class="chip" hidden></span>
          <span class="sc-actions">
            <button data-action="sc-save" ${canSave ? "" : "disabled"} title="${esc(saveTitle)}">Save</button>
            <button data-action="sc-save-as" ${canSave ? "" : "disabled"} title="Save under a new name">Save as…</button>
            <button data-action="sc-toggle" aria-expanded="${this._scenariosOpen}">${this._scenariosOpen ? "Hide" : "Saved scenarios"}${count}</button>
            ${busy ? `<span class="spinner"></span>` : ""}
          </span>
        </div>
        ${form}${messages}${confirmLoad}
        ${this._scenariosOpen ? this._scenarioListHtml() : ""}
      </div>`;
  }

  _scenarioListHtml() {
    const list = this._scenarios;
    if (list === null) return `<div class="muted small">Loading saved scenarios…</div>`;
    if (!list.length) return `<div class="muted small sc-empty">No saved scenarios yet. Save keeps the automation config (edited or not), the window, the tracks and every edit to them.</div>`;
    const rows = list
      .map((sc) => {
        const id = sc.scenario_id;
        const isCurrent = this._scenario?.scenario_id === id;
        const deleting = this._confirm?.kind === "delete" && this._confirm.id === id;
        const start = parseTime(sc.start);
        const end = parseTime(sc.end);
        const auto = sc.alias || sc.automation_entity_id || "no automation";
        const actions = deleting
          ? `<span class="err small">Delete “${esc(sc.name)}”?</span>
             <button class="danger" data-action="sc-delete-confirm" data-id="${esc(id)}">Delete</button>
             <button data-action="sc-confirm-cancel">Cancel</button>`
          : `<button data-action="sc-load" data-id="${esc(id)}" ${this._scBusy ? "disabled" : ""}>${isCurrent ? "Reload" : "Load"}</button>
             <button data-action="sc-delete" data-id="${esc(id)}" ${this._scBusy ? "disabled" : ""} title="Delete this saved scenario">Delete</button>`;
        return `
          <li class="${isCurrent ? "current" : ""}">
            <div class="sc-info">
              <strong>${esc(sc.name)}</strong>${isCurrent ? ` <span class="chip none">loaded</span>` : ""}
              <span class="muted small">${esc(auto)} · ${esc(this._fullTime(start))} – ${esc(this._fullTime(end))} (${esc(formatDuration(end - start))}) · ${sc.entity_ids?.length ?? 0} track${sc.entity_ids?.length === 1 ? "" : "s"}, ${sc.event_count} change${sc.event_count === 1 ? "" : "s"}</span>
              <span class="muted small">Saved ${esc(this._fullTime(parseTime(sc.updated)))}</span>
            </div>
            <div class="sc-row-actions">${actions}</div>
          </li>`;
      })
      .join("");
    return `<ul class="sc-list">${rows}</ul>`;
  }

  _runHint() {
    if (this._running) return "Replaying the window in the sandbox…";
    if (!this._autoRun && this._automation && (this._resultStale || !this._result)) {
      if (this._cfgError) return "The edited automation config does not parse; fix it or revert it below.";
      return "Auto-run is off: Run now replays the window with the changes so far.";
    }
    if (!this._entityId && !this._automation) return "Pick an automation to load its entities.";
    if (!this._automation) return "";
    if (this._cfgError) return "The edited automation config does not parse; fix it or revert it below.";
    const events = this._tracks.reduce((n, t) => n + t.events.length, 0);
    const edited = this._tracks.filter((t) => t.edited).length;
    const what = [
      edited ? `${edited} edited track${edited === 1 ? "" : "s"}` : "",
      this._cfgDraft ? "the edited automation config" : "",
    ].filter(Boolean);
    return `Replays ${events} state change${events === 1 ? "" : "s"} on ${this._tracks.length} track${this._tracks.length === 1 ? "" : "s"}${what.length ? `, with ${what.join(" and ")}` : ""}${this._autoRun ? ", after every change" : ""}. Service calls are recorded, never made.`;
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
    // A loaded scenario may name an automation this instance no longer has.
    const gone =
      this._entityId && !(this._automations || []).some((a) => a.entity_id === this._entityId)
        ? `<option value="${esc(this._entityId)}" selected>${esc(this._entityId)} (not on this instance)</option>`
        : "";
    return placeholder + gone + opts;
  }

  _timelineCard() {
    const banner = [
      this._error ? `<div class="alert error">${esc(this._error)}</div>` : "",
      this._notice ? `<div class="alert info">${esc(this._notice)}</div>` : "",
      this._loading ? `<div class="alert info"><span class="spinner"></span>${esc(this._loading)}</div>` : "",
      this._mode === "replay" ? this._runErrorHtml() : "",
      this._labelError && !this._labelSel ? `<div class="alert error" role="alert">${esc(this._labelError)}</div>` : "",
    ].join("");
    if (!this._entityId && !this._hasTimeline()) {
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
          <div class="muted small hint">${this._hintText()}</div>
          <div id="tl-wrap" tabindex="0" class="${this._mode === "suggest" ? "suggest-mode" : ""}">
            <div id="tl"></div>
            <div id="cursor" hidden></div>
            <div id="tip" hidden></div>
            <div id="label-pop" class="label-pop" hidden></div>
          </div>
          ${this._labelSel ? this._labelEditorHtml() : this._editorHtml()}
          ${this._tracks.length === 0 && !this._loading
            ? this._mode === "suggest"
              ? `<div class="empty">Add the entities to learn from with Add track above, or seed them from an automation.</div>`
              : `<div class="empty">This automation references no entities (a time pattern, say). Add tracks above, or run it as is.</div>`
            : ""}
        </div>
      </ha-card>`;
  }

  _hintText() {
    const touch = this._narrow;
    const base = touch
      ? "Pan with the scroll bar under the axis. Drag a track to shift its whole history; to move one change, tap it, then drag its handle. Tap a state to edit it or add a change there; double-tap adds one directly."
      : "Pan with the scroll bar under the axis (or the horizontal wheel); Ctrl/⌘ + wheel or wheel over the axis zooms. Drag a track to shift its whole history in time; drag a change mark to move just that change (Shift + drag shifts the track even from a mark). Click a state to edit it or add a change there; double-click adds one directly.";
    if (this._mode !== "suggest") return base;
    return touch
      ? `Label periods: long-press the Labels row or the time axis, then drag across a period. Tap a period to edit it; drag it to move it, or its edges once selected to resize it. ${base}`
      : `Label periods: drag across the Labels row or the time axis. Click a period to edit it; drag its edges to resize it or its body to move it; Delete removes the selected one. ${base}`;
  }

  /* The editor for the selected label: kind and anchor (applied as picked), exact start
   * and end (applied with Apply or Enter), delete. */
  _labelEditorHtml() {
    const label = this._label(this._labelSel);
    if (!label) return "";
    const draft = this._labelEdDraft?.id === label.id ? this._labelEdDraft : null;
    const fire = label.kind === "fire";
    const anchors = LABEL_ANCHORS.map((a) => `<option value="${a}" ${label.anchor === a ? "selected" : ""}>${esc(ANCHOR_TEXT[a])}</option>`).join("");
    const where = label.start < this._window.start || label.end > this._window.end ? `<span class="chip none" title="Only the part inside the window is used">reaches outside the window</span>` : "";
    return `
      <div class="editor label-editor ${fire ? "fire" : "nofire"}" id="label-editor">
        <div class="editor-head">
          <strong>Labeled period</strong>
          <span class="chip ${fire ? "pass" : "fail"}">${esc(labelText(label))}</span>
          <span class="muted small">${esc(formatDuration(label.end - label.start))}</span>
          ${where}
          <button class="icon close" data-action="label-close" aria-label="Close editor" title="Close (Esc)">×</button>
        </div>
        <div class="editor-fields">
          <label class="fld"><span class="label">Kind</span>
            <select id="lb-kind"><option value="fire" ${fire ? "selected" : ""}>Should fire</option><option value="no_fire" ${fire ? "" : "selected"}>Must not fire</option></select></label>
          <label class="fld"><span class="label">Anchor</span>
            <select id="lb-anchor" ${fire ? "" : "disabled"} title="${fire ? "Where in the period the automation should fire; while: its conditions should hold throughout" : "A must-not-fire period has no anchor"}">${anchors}</select></label>
          <label class="fld"><span class="label">Start</span><input id="lb-start" type="datetime-local" step="1" value="${esc(draft?.start ?? toLocalInputSeconds(label.start))}"></label>
          <label class="fld"><span class="label">End</span><input id="lb-end" type="datetime-local" step="1" value="${esc(draft?.end ?? toLocalInputSeconds(label.end))}"></label>
        </div>
        ${this._labelError ? `<div class="err small" role="alert">${esc(this._labelError)}</div>` : ""}
        <div class="editor-actions">
          <button class="primary small-btn" data-action="label-apply">Apply times</button>
          <button data-action="label-delete" title="Delete this period (Del)">Delete period</button>
        </div>
      </div>`;
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

  /* A failed run: what went wrong in words, and the code and the child's last log lines
   * in a collapsible block. */
  _runErrorHtml() {
    const e = this._runError;
    if (!e) return "";
    const lines = e.stderr ? e.stderr.split("\n").length : 0;
    const details =
      e.code && e.code !== "unknown_command"
        ? `<details class="failure" data-key="run-error" ${this._openAttr("run-error")}>
            <summary>Details: error code <span class="mono">${esc(e.code)}</span>${lines ? `, last ${lines} line${lines === 1 ? "" : "s"} the sandbox logged` : ""}</summary>
            ${e.stderr ? `<pre class="stderr">${esc(e.stderr)}</pre>` : `<div class="small muted">The sandbox logged nothing before it stopped.</div>`}
          </details>`
        : "";
    return `<div class="alert error run-error${this._running ? " updating" : ""}"><div class="alert-body"><strong>${esc(e.title)}</strong><div class="msg">${esc(e.message)}</div>${details}</div></div>`;
  }

  /* Result warnings, grouped by code, each with the virtual time it happened at. */
  _warningsHtml(warnings) {
    if (!warnings.length) return "";
    const LIMIT = 50;
    const groups = groupWarnings(warnings)
      .map((g) => {
        const items = g.items
          .slice(0, LIMIT)
          .map((w) => `<li>${w.at != null ? `<span class="mono small w-at">${esc(this._fullTime(w.at))}</span> ` : ""}<span class="w-msg">${esc(w.message)}</span></li>`)
          .join("");
        const more = g.items.length > LIMIT ? `<li class="muted">…and ${g.items.length - LIMIT} more</li>` : "";
        const key = `warn:${g.code}`;
        return `<details class="wgroup" data-key="${esc(key)}" ${this._openAttr(key, g.items.length <= 3)}>
            <summary><span class="w-label">${esc(g.label)}</span> <span class="chip none">${g.items.length}</span></summary>
            <ul>${items}${more}</ul>
          </details>`;
      })
      .join("");
    return `<div class="alert warning"><div class="alert-body"><strong>${warnings.length} warning${warnings.length === 1 ? "" : "s"} from the replay</strong>${groups}</div></div>`;
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
    if (this._mode === "suggest" || this._labels.length) parts.push(this._labelsRow(scale));
    if (this._mode === "replay" && (this._result || this._running)) parts.push(this._resultsRow(scale));
    for (const track of this._tracks) parts.push(this._trackRow(track, scale));
    tl.innerHTML = parts.join("");
    // The hover line starts under the axis and its scroll bar.
    const axis = tl.querySelector(".axis-row");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (axis && cursor) cursor.style.top = `${axis.offsetHeight}px`;
    this._placeLabelPopover(scale);
  }

  /* The labels to draw: the list, with a label being moved or resized drawn where it would land. */
  _drawnLabels() {
    const d = this._labelDrag;
    if (!d?.preview || d.brush) return this._labels;
    return this._labels.map((l) => (l.id === d.labelId ? d.preview : l));
  }

  /* The Labels row: should-fire periods green with a tick at each anchor, must-not-fire
   * periods red hatched; the selected one outlined with edge handles. In Suggest mode it is
   * where periods are brushed; in Replay it is an overlay of the labels a scenario has. */
  _labelsRow(scale) {
    const labels = this._drawnLabels();
    const suggest = this._mode === "suggest";
    const W = scale.width;
    const clip = (t) => Math.min(W + 2, Math.max(-2, scale.toX(t)));
    let out = `<defs><pattern id="nofire-hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <rect width="7" height="7" class="nofire-bg"/><line x1="0" y1="0" x2="0" y2="7" class="nofire-line"/></pattern></defs>${this._gridLines(scale, LABELS_H)}`;
    for (const l of labels) {
      if (l.end < scale.t0 || l.start > scale.t1) continue;
      const x1 = clip(l.start);
      const x2 = clip(l.end);
      const w = Math.max(1, x2 - x1);
      const sel = l.id === this._labelSel;
      const fire = l.kind === "fire";
      out += `<rect x="${x1}" y="3" width="${w}" height="${LABELS_H - 6}" rx="3" class="lb ${fire ? "fire" : "nofire"}${fire && l.anchor === "while" ? " while" : ""}${sel ? " sel" : ""}"/>`;
      if (fire) {
        const ticks = [];
        if (l.anchor === "start" || l.anchor === "both") ticks.push([scale.toX(l.start), 1]);
        if (l.anchor === "end" || l.anchor === "both") ticks.push([scale.toX(l.end), -1]);
        for (const [x, dir] of ticks) {
          if (x < -4 || x > W + 4) continue;
          out += `<line x1="${x}" x2="${x}" y1="0" y2="${LABELS_H}" class="lb-anchor"/><path d="M${x},0L${x + 7 * dir},0L${x},7Z" class="lb-anchor-flag"/>`;
        }
      }
      const text = fire ? `fire ${ANCHOR_TEXT[l.anchor]}` : "must not fire";
      const room = Math.floor((w - 10) / 6.2);
      if (room >= 4) out += `<text x="${Math.max(x1, 0) + 6}" y="${LABELS_H / 2 + 4}" class="lb-text">${esc(text.length <= room ? text : `${text.slice(0, room - 1)}…`)}</text>`;
      if (sel && suggest) {
        for (const x of [scale.toX(l.start), scale.toX(l.end)]) {
          if (x >= -4 && x <= W + 4) out += `<rect x="${x - 3.5}" y="${LABELS_H / 2 - 8}" width="7" height="16" rx="3" class="lb-handle"/>`;
        }
      }
    }
    const brush = this._labelDrag?.brush ? this._labelDrag.preview : this._labelDraft;
    if (brush) {
      const x1 = clip(brush.start);
      out += `<rect x="${x1}" y="1.5" width="${Math.max(1, clip(brush.end) - x1)}" height="${LABELS_H - 3}" rx="3" class="lb-brush"/>`;
    }
    if (suggest && !labels.length && !brush) {
      out += `<text x="8" y="${LABELS_H / 2 + 4}" class="lb-empty">${this._narrow ? "Long-press here, then drag across a period to label it" : "Drag across a period here (or on the time axis) to label it"}</text>`;
    }
    const inView = labelsInWindow(this._labels, this._window.start, this._window.end);
    const fires = inView.filter((l) => l.kind === "fire").length;
    const meta = inView.length ? `${fires} should fire · ${inView.length - fires} must not fire` : suggest ? "no periods labeled yet" : "";
    return `
      <div class="row labels-row">
        <div class="track-head"><span class="name">Labels</span>${suggest ? "" : `<span class="chip none" title="Labeled in Suggest mode">from Suggest</span>`}<span class="meta">${meta}</span></div>
        <svg class="labels${suggest ? " editable" : ""}" data-row="labels" width="${W}" height="${LABELS_H}">${out}</svg>
      </div>`;
  }

  /* Labels drawn faintly across a track, so periods line up with what the track did. */
  _labelOverlay(scale, height) {
    let out = "";
    for (const l of this._drawnLabels()) {
      if (l.end < scale.t0 || l.start > scale.t1) continue;
      const x1 = Math.max(-1, scale.toX(l.start));
      const x2 = Math.min(scale.width + 1, scale.toX(l.end));
      out += `<rect x="${x1}" y="0" width="${Math.max(1, x2 - x1)}" height="${height}" class="lb-overlay ${l.kind === "fire" ? "fire" : "nofire"}"/>`;
      if (l.kind === "fire") {
        if (l.anchor === "start" || l.anchor === "both") out += `<line x1="${scale.toX(l.start)}" x2="${scale.toX(l.start)}" y1="0" y2="${height}" class="lb-overlay-anchor"/>`;
        if (l.anchor === "end" || l.anchor === "both") out += `<line x1="${scale.toX(l.end)}" x2="${scale.toX(l.end)}" y1="0" y2="${height}" class="lb-overlay-anchor"/>`;
      }
    }
    return out;
  }

  /* The popover asking what a just-brushed period is, placed under the Labels row. */
  _placeLabelPopover(scale) {
    const pop = this.shadowRoot.getElementById("label-pop");
    if (!pop) return;
    const draft = this._labelDraft;
    const row = this.shadowRoot.querySelector("#tl .labels-row");
    if (!draft || !row || this._mode !== "suggest") {
      pop.hidden = true;
      pop.innerHTML = "";
      return;
    }
    const def = this._labelDefault;
    const btn = (kind, anchor, text) => {
      const isDefault = def.kind === kind && (kind === "no_fire" || def.anchor === anchor);
      return `<button class="${isDefault ? "primary small-btn" : ""}${kind === "no_fire" ? " nofire-btn" : ""}" data-action="label-create" data-kind="${kind}" data-anchor="${anchor}" ${isDefault ? 'data-default="1"' : ""}>${text}</button>`;
    };
    pop.hidden = false;
    pop.innerHTML = `
      <div class="small"><strong>${esc(this._fullTime(draft.start))} – ${esc(this._clockTime(draft.end))}</strong> <span class="muted">(${esc(formatDuration(draft.end - draft.start))})</span></div>
      <div class="pop-group"><span class="label">Should fire</span>
        ${btn("fire", "start", "at start")}${btn("fire", "end", "at end")}${btn("fire", "both", "at start and end")}${btn("fire", "while", "while")}</div>
      <div class="pop-group">${btn("no_fire", "start", "Must not fire")}<button data-action="label-cancel" class="link">Cancel</button></div>`;
    // Placed once laid out: right after a full redraw the card around it has not rendered yet.
    const place = () => {
      const wrap = this.shadowRoot.getElementById("tl-wrap");
      if (!pop.isConnected || !row.isConnected || !wrap) return;
      const top = row.getBoundingClientRect().bottom - wrap.getBoundingClientRect().top + 4;
      const left = Math.min(Math.max(0, scale.toX(draft.start)), Math.max(0, wrap.clientWidth - pop.offsetWidth));
      pop.style.top = `${top}px`;
      pop.style.left = `${left}px`;
    };
    place();
    requestAnimationFrame(place);
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
      <line x1="0" x2="${scale.width}" y1="${AXIS_H - 0.5}" y2="${AXIS_H - 0.5}" class="baseline"/></svg>${this._scrollbarHtml(scale)}</div>`;
  }

  /* The scroll bar that pans every track together (one shared time axis); the zoom
   * buttons and Ctrl/⌘ + wheel set how much of the window it shows. */
  _scrollbarHtml(scale) {
    const thumb = scrollThumb(this._view, this._bounds(), scale.width, SCROLL_MIN_THUMB);
    const all = thumb.width >= scale.width - 0.5;
    const pct = (v) => `${((v / scale.width) * 100).toFixed(3)}%`;
    return `<div class="scrollbar${all ? " all" : ""}" data-part="scrollbar" title="${all ? "The whole window is visible; zoom in to pan" : "Drag to pan; click beside the thumb to page"}">
      <div class="thumb${this._thumbDrag ? " dragging" : ""}" data-part="thumb" style="left:${pct(thumb.left)};width:${pct(thumb.width)}"></div></div>`;
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
    // The automation's own track sets whether it is enabled in the replay, not a raw state.
    const name = track.entity_id === this._entityId ? "This automation (enabled state)" : this._friendlyName(track.entity_id);
    const unit = this._unit(track);
    // While a change mark or the whole track is dragged, the track draws the would-be timeline.
    const preview =
      this._evDrag?.entity_id === track.entity_id
        ? this._evDrag.preview?.timeline
        : this._drag?.entity_id === track.entity_id && this._drag.moved
          ? this._drag.preview
          : null;
    const view = preview && track.segments ? { ...track, ...preview, ...this._derive(preview) } : track;
    const shifting = !!preview && this._drag?.entity_id === track.entity_id;
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
          ${this._shiftChip(shifting ? view : track, shifting)}
          <span class="meta">${meta}</span>
          <button class="icon" data-action="remove-track" data-entity="${esc(track.entity_id)}" title="Remove track" aria-label="Remove ${esc(track.entity_id)}">×</button>
        </div>
        <svg class="track${track.edited ? " is-edited" : ""}${shifting ? " shifting" : ""}" data-row="track" data-entity="${esc(track.entity_id)}" width="${scale.width}" height="${height}">
          ${this._hatch(`h-${hashString(track.entity_id)}`)}
          ${this._gridLines(scale, height)}
          ${this._labelOverlay(scale, height)}
          ${body}
        </svg>
      </div>`;
  }

  /* The offset of a shifted track and how many of its changes it pushed out of the
   * window, with a reset; drawn live while the track is dragged. */
  _shiftChip(timeline, live) {
    const offset = trackOffset(timeline);
    const [before, after] = parkedCounts(timeline);
    if (!offset && !before && !after && !live) return "";
    const parked = before + after;
    const title = `Every change on this track moved by ${formatOffset(offset)}${parked ? `; ${parked} now outside the window, left out of the replay but kept (shift back to bring them in)` : ""}`;
    return `<span class="chip shift${live ? " live" : ""}" title="${esc(title)}">shifted ${esc(formatOffset(offset))}${parked ? ` · ${parked} outside` : ""}</span>
      ${live || !offset ? "" : `<button class="link" data-action="reset-shift" data-entity="${esc(timeline.entity_id)}" title="Shift the track back by ${esc(formatOffset(-offset))}">Unshift</button>`}`;
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
      if (selected) out += `<rect x="${x - 4}" y="${height / 2 - 6}" width="8" height="12" rx="3" class="ev-knob"/>`;
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
    const summary = runs.length
      ? outcomeSummary(runs)
          .map((g) => `<span class="legend-item"><svg class="legend-mark" width="14" height="14" aria-hidden="true">${outcomeMarker(g.outcome, 7, 7)}</svg>${g.count} ${esc(g.text)}</span>`)
          .join(" · ")
      : "no runs";
    const stale = this._running
      ? ` <span class="updating-note"><span class="spinner small"></span>updating…</span>`
      : this._resultStale
        ? ` <span class="stale">inputs changed since this run</span>`
        : "";
    let marks = "";
    for (const run of runs) {
      if (run.finished < scale.t0 || run.started > scale.t1) continue;
      const x = scale.toX(run.started);
      const x2 = scale.toX(run.finished);
      const color = OUTCOME_STYLE[run.outcome].color;
      if (x2 - x > 2) marks += `<rect x="${x}" y="9" width="${x2 - x}" height="6" rx="3" fill="${color}" opacity=".35"/>`;
      if (run.index === this._selectedRun) marks += `<circle cx="${x}" cy="12" r="9" class="selected"/>`;
      marks += outcomeMarker(run.outcome, x, 12, color);
    }
    for (const call of calls) {
      if (call.t < scale.t0 || call.t > scale.t1) continue;
      const x = scale.toX(call.t);
      marks += `<line x1="${x}" x2="${x}" y1="25" y2="${RESULTS_H - 2}" class="call-tick"/>`;
    }
    return `
      <div class="row results-row${this._running ? " updating" : ""}">
        <div class="track-head"><span class="name">Runs</span><span class="meta">${summary} · ${calls.length} service call${calls.length === 1 ? "" : "s"}${stale}</span></div>
        <svg class="results" data-row="results" width="${scale.width}" height="${RESULTS_H}">
          ${this._gridLines(scale, RESULTS_H)}${marks}
        </svg>
      </div>`;
  }

  _detailCard() {
    const result = this._result;
    if (!result || this._mode !== "replay") return "";
    return `<div class="detail${this._running ? " updating" : ""}">${this._detailHtml(result)}</div>`;
  }

  _detailHtml(result) {
    const warnings = this._warningsHtml(result.warnings);
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
            <span class="chip" style="background:${OUTCOME_STYLE[run.outcome].color};color:var(--text-primary-color, #fff)">${esc(outcomeText(run))}</span>
          </div>
          <dl>
            <dt>Started</dt><dd class="mono">${esc(this._fullTime(run.started))}</dd>
            <dt>Duration</dt><dd>${run.finished > run.started ? esc(formatDuration(run.finished - run.started)) : "instant"}</dd>
            <dt>Trigger</dt><dd>${esc(describeTrigger(run.trigger)) || `<span class="muted">not reported</span>`}</dd>
            ${run.stoppedBy ? `<dt>${run.outcome === "not_triggered" ? "Why not" : "Stopped by"}</dt><dd class="mono">${esc(typeof run.stoppedBy === "string" ? run.stoppedBy : JSON.stringify(run.stoppedBy))}</dd>` : ""}
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
    wrap.addEventListener("pointerup", (ev) => {
      this._onPointerUp(ev);
      this._flushRender();
    });
    wrap.addEventListener("pointercancel", () => {
      // A cancelled gesture (the browser took it for a page scroll, say) changes nothing.
      const redraw = this._evDrag || this._drag?.moved || this._thumbDrag || this._labelDrag;
      clearTimeout(this._labelDrag?.timer);
      this._drag = null;
      this._evDrag = null;
      this._thumbDrag = null;
      this._labelDrag = null;
      this._hideHover();
      if (redraw) this._scheduleTimeline();
      this._flushRender();
    });
    wrap.addEventListener("pointerleave", () => {
      if (!this._evDrag?.moved && !this._labelDrag) this._hideHover();
    });
    // A long press on the labels row or the axis starts a brush, not the context menu.
    wrap.addEventListener("contextmenu", (ev) => {
      if (this._mode === "suggest" && ev.target.closest?.("svg[data-row=labels], svg[data-row=axis]")) ev.preventDefault();
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
    } else if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY) || (ev.shiftKey && ev.deltaY)) {
      // A horizontal wheel (or Shift + wheel) pans, as the scroll bar does.
      ev.preventDefault();
      const delta = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      this._setView(panView(this._view, (delta / this._width) * (this._view[1] - this._view[0]), this._bounds()));
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

  /* The change mark within reach of the pointer on that track, as an event index: the
   * handle that moves one change instead of shifting the whole track. With a finger only
   * the selected change has a handle (tap a change first), so a swipe on a busy track
   * still shifts the track rather than whichever mark it happened to start on.
   * `anyMark` finds any mark in reach, for a tap that selects one. */
  _markHit(hit, ev, anyMark = false) {
    const scale = this._scale();
    const { track, x, t } = hit;
    const touch = ev.pointerType === "touch";
    const reach = touch ? 14 : 6;
    const crowded = countAtOrBefore(track.events, scale.t1) - countAtOrBefore(track.events, scale.t0 - 1) > scale.width / 6;
    const selected = (j) => this._sel?.entity_id === track.entity_id && this._sel.index === j;
    const i = eventIndexAt(track.events, t);
    const candidates = [i, i + 1]
      .filter((j) => j >= 0 && j < track.events.length)
      .filter((j) => (touch && !anyMark ? selected(j) : !crowded || track.events[j].edited || selected(j)))
      .map((j) => ({ x: scale.toX(parseTime(track.events[j].at)), index: j }));
    return findNearest(candidates, x, reach)?.index ?? null;
  }

  _onPointerDown(ev) {
    if (ev.button !== 0) return;
    const bar = ev.target.closest?.("[data-part=scrollbar]");
    if (bar) return this._onScrollbarDown(ev, bar);
    if (!ev.target.closest?.("svg")) return;
    const row = ev.target.closest("svg[data-row]")?.dataset.row;
    if (this._mode === "suggest" && (row === "labels" || row === "axis")) return this._onLabelDown(ev, row);
    const hit = ev.target.closest("svg[data-row=track]") ? this._trackAt(ev) : null;
    // A mark is a handle for that one change; Shift + drag shifts the track from anywhere.
    const index = hit && !ev.shiftKey ? this._markHit(hit, ev) : null;
    if (index !== null) {
      const at = parseTime(hit.track.events[index].at);
      this._evDrag = { id: ev.pointerId, entity_id: hit.track.entity_id, index, x: ev.clientX, from: at, moved: false, preview: null, touch: ev.pointerType === "touch" };
    } else {
      // On a track: a click, or a drag that shifts the whole track. On the axis or the
      // runs row: a click only (the axis drag is left free for labeling periods).
      this._drag = { id: ev.pointerId, x: ev.clientX, moved: false, entity_id: hit ? hit.track.entity_id : null, dt: 0, preview: null, touch: ev.pointerType === "touch" };
    }
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
  }

  /* Press on the Labels row or the axis in Suggest mode: grab a label's edge (resize) or
   * body (move), or start brushing a new period. A finger has to rest for LONG_PRESS_MS
   * before a brush starts, so a swipe meant to scroll the page labels nothing. */
  _onLabelDown(ev, row) {
    const svg = ev.target.closest("svg[data-row]");
    const x = ev.clientX - svg.getBoundingClientRect().left;
    const scale = this._scale();
    const touch = ev.pointerType === "touch";
    const base = { pid: ev.pointerId, x0: ev.clientX, y0: ev.clientY, moved: false, touch, preview: null };
    const hit = row === "labels" ? labelHit(this._labels, x, scale, { reach: touch ? 14 : 6, selected: this._labelSel, touch }) : null;
    if (hit) {
      this._labelDrag = { ...base, labelId: hit.id, part: hit.part, orig: this._label(hit.id) };
    } else {
      const drag = { ...base, brush: true, t0: scale.toT(x), armed: !touch };
      if (touch) {
        drag.timer = setTimeout(() => {
          if (this._labelDrag !== drag) return;
          drag.armed = true;
          navigator.vibrate?.(12);
          this._showBrushTip(drag.x0, drag.y0, null);
        }, LONG_PRESS_MS);
      }
      this._labelDrag = drag;
    }
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
  }

  /* A label move, resize or brush following the pointer. */
  _onLabelMove(ev, d) {
    const dx = ev.clientX - d.x0;
    const msPerPx = (this._view[1] - this._view[0]) / this._width;
    const step = snapStep(msPerPx);
    if (d.brush) {
      if (!d.armed) {
        // A finger that moves before the long press is up is scrolling or swiping: no brush.
        if (Math.hypot(dx, ev.clientY - d.y0) > 8) {
          clearTimeout(d.timer);
          this._labelDrag = null;
        }
        return;
      }
      if (Math.abs(dx) > 3) d.moved = true;
      if (!d.moved) return;
      const rect = this.shadowRoot.getElementById("tl-wrap").getBoundingClientRect();
      d.preview = brushPeriod(d.t0, this._scale().toT(ev.clientX - rect.left), { ...this._editOpts(), step });
      this._showBrushTip(ev.clientX, ev.clientY, d.preview);
      return this._scheduleTimeline();
    }
    if (Math.abs(dx) > (d.touch ? 6 : 3)) d.moved = true;
    if (!d.moved || !d.orig) return;
    const dt = dx * msPerPx;
    d.preview =
      d.part === "body"
        ? moveLabel(d.orig, snapDelta(dt, step), this._editOpts())
        : resizeLabel(d.orig, d.part, snapTime(d.orig[d.part] + dt, step), this._editOpts(), Math.max(step, MIN_LABEL_SPAN));
    this._showBrushTip(ev.clientX, ev.clientY, d.preview, d.part === "body" ? "move" : "resize");
    this._scheduleTimeline();
  }

  _onLabelUp(d) {
    clearTimeout(d.timer);
    this._labelDrag = null;
    this._hideHover();
    if (d.brush) {
      if (d.moved && d.preview) {
        // Ask what the period is (the popover); Escape or Cancel drops it.
        this._labelDraft = d.preview;
        this._labelSel = null;
        this._labelError = null;
        this._focusPop = true;
        return this._render();
      }
      // A click on an empty part of the row (or the axis) drops the selection.
      if (this._labelSel || this._labelDraft) {
        this._labelSel = null;
        this._labelDraft = null;
        return this._render();
      }
      return this._scheduleTimeline();
    }
    if (!d.moved) {
      this._labelDraft = null;
      this._selectLabel(d.labelId);
      this._render();
      return this.shadowRoot.getElementById("tl-wrap")?.focus({ preventScroll: true });
    }
    const p = d.preview;
    if (!p || (p.start === d.orig.start && p.end === d.orig.end)) return this._scheduleTimeline();
    this._placeLabel(d.part === "body" ? "move period" : "resize period", p);
  }

  /* The period a brush, move or resize would give, next to the pointer. */
  _showBrushTip(clientX, clientY, period, what = "new") {
    const tip = this.shadowRoot.getElementById("tip");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (!tip || !cursor) return;
    const wrap = this.shadowRoot.getElementById("tl-wrap");
    const rect = wrap.getBoundingClientRect();
    const x = clientX - rect.left;
    cursor.hidden = false;
    cursor.style.left = `${x}px`;
    tip.hidden = false;
    tip.innerHTML = period
      ? `<div class="mono small"><strong>${esc(this._fullTime(period.start))} – ${esc(this._clockTime(period.end))}</strong></div>
        <div class="muted small">${esc(formatDuration(period.end - period.start))}${what === "new" ? " · release to label it" : what === "move" ? " · moving the period" : " · resizing the period"}</div>`
      : `<div class="muted small">Drag across the period to label</div>`;
    const tipWidth = tip.offsetWidth;
    tip.style.left = `${x + 12 + tipWidth > wrap.clientWidth ? Math.max(0, x - 12 - tipWidth) : x + 12}px`;
    tip.style.top = `${clientY - rect.top + 14}px`;
  }

  /* Press on the scroll bar: drag the thumb, or page toward a press beside it. */
  _onScrollbarDown(ev, bar) {
    ev.preventDefault();
    const rect = bar.getBoundingClientRect();
    const thumb = scrollThumb(this._view, this._bounds(), rect.width, SCROLL_MIN_THUMB);
    const x = ev.clientX - rect.left;
    if (x < thumb.left || x > thumb.left + thumb.width) {
      this._setView(pageView(this._view, x < thumb.left ? -1 : 1, this._bounds()));
      return;
    }
    this._thumbDrag = { id: ev.pointerId, x: ev.clientX, left: thumb.left, thumb: thumb.width, bar: rect.width, view: this._view };
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
    this._hideHover();
    this._scheduleTimeline();
  }

  _onPointerMove(ev) {
    const thumb = this._thumbDrag;
    if (thumb && thumb.id === ev.pointerId) {
      const left = thumb.left + ev.clientX - thumb.x;
      this._setView(viewFromThumb(left, thumb.view, this._bounds(), thumb.bar, thumb.thumb));
      return;
    }
    const ld = this._labelDrag;
    if (ld && ld.pid === ev.pointerId) return this._onLabelMove(ev, ld);
    const moving = this._evDrag;
    if (moving && moving.id === ev.pointerId) {
      const dx = ev.clientX - moving.x;
      if (Math.abs(dx) > (moving.touch ? 6 : 3)) moving.moved = true;
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
      if (Math.abs(dx) > (drag.touch ? 6 : 3)) drag.moved = true;
      if (!drag.moved) return this._showHover(ev);
      const track = drag.entity_id && this._track(drag.entity_id);
      if (!track?.segments) return this._hideHover();
      const msPerPx = (this._view[1] - this._view[0]) / this._width;
      const dt = snapDelta(dx * msPerPx, snapStep(msPerPx));
      if (dt !== drag.dt || !drag.preview) {
        drag.dt = dt;
        drag.preview = shiftTrack(track, dt, this._editOpts());
        this._scheduleTimeline();
      }
      this._showShiftTip(ev, drag, track);
      return;
    }
    this._showHover(ev);
  }

  _onPointerUp(ev) {
    ev.currentTarget.releasePointerCapture?.(ev.pointerId);
    const thumb = this._thumbDrag;
    if (thumb && thumb.id === ev.pointerId) {
      this._thumbDrag = null;
      return this._scheduleTimeline();
    }
    const ld = this._labelDrag;
    if (ld && ld.pid === ev.pointerId) return this._onLabelUp(ld);
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
    if (!drag) return;
    if (drag.moved) {
      this._hideHover();
      if (drag.entity_id && drag.preview && drag.dt) return this._shiftTrackBy(drag.entity_id, drag.dt, drag.preview);
      return this._scheduleTimeline();
    }
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
      // A tap on a mark selects that change (with a finger no mark is a drag handle until selected).
      const mark = ev.pointerType === "touch" ? this._markHit(at, ev, true) : null;
      const index = mark ?? eventIndexAt(at.track.events, at.t);
      this._select({ entity_id: at.track.entity_id, index }, mark !== null ? parseTime(at.track.events[mark].at) : t);
      this._render();
      this.shadowRoot.getElementById("tl-wrap")?.focus({ preventScroll: true });
    }
  }

  /* Shift a whole track by dt ms as one undoable edit (`timeline` when already worked
   * out by the drag preview). A selection on that track follows its change. */
  _shiftTrackBy(entityId, dt, timeline = null) {
    const track = this._track(entityId);
    if (!track?.segments || !dt) return this._scheduleTimeline();
    const next = timeline ?? shiftTrack(track, dt, this._editOpts());
    let select;
    if (this._sel?.entity_id === entityId && this._sel.index >= 0) {
      const index = this._sel.index - (parkedCounts(next)[0] - parkedCounts(track)[0]);
      select = index >= 0 && index < next.events.length ? { entity_id: entityId, index } : null;
      if (Number.isFinite(this._selT)) this._selT += dt;
    }
    const label = trackOffset(next) === 0 ? "unshift track" : "shift track";
    this._commit(label, [{ entity_id: entityId, timeline: next }], select);
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

  /* The offset a dragged track would get, next to the pointer. */
  _showShiftTip(ev, drag, track) {
    const tip = this.shadowRoot.getElementById("tip");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (!tip || !cursor) return;
    cursor.hidden = true;
    const total = trackOffset(drag.preview);
    const [before, after] = parkedCounts(drag.preview);
    const parked = before + after;
    tip.hidden = false;
    tip.innerHTML = `<div class="mono"><strong>${esc(formatOffset(drag.dt))}</strong></div>
      <div class="muted small">${esc(this._friendlyName(track.entity_id) || track.entity_id)}: whole track${total !== drag.dt ? `, ${esc(formatOffset(total))} in all` : ""}</div>
      ${parked ? `<div class="muted small">${parked} change${parked === 1 ? "" : "s"} outside the window (left out of the replay, kept)</div>` : ""}`;
    const { x, y } = this._localX(ev);
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
    if (!svg || !tip || (svg.dataset.row === "axis" && this._mode !== "suggest")) return this._hideHover();
    const { x, y } = this._localX(ev);
    let overMark = false;
    let overEdge = false;
    let overLabel = false;
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
          <div class="muted small">drag to move this change · click to edit</div>`;
      } else {
        html = `<div class="mono small">${esc(this._fullTime(t))}</div>
          <div><strong>${seg ? esc(seg.state) : "no data"}</strong>${seg && track.numeric && !isMissing(seg.state) && unit ? ` ${esc(unit)}` : ""}</div>
          ${seg ? `<div class="muted small">since ${esc(this._fullTime(seg.from))}</div>` : ""}
          ${track?.segments ? `<div class="muted small">drag to shift the whole track</div>` : ""}`;
      }
    } else if (svg.dataset.row === "labels" || svg.dataset.row === "axis") {
      const suggest = this._mode === "suggest";
      const hit = svg.dataset.row === "labels" ? labelHit(this._labels, svgX, scale, { reach: 6, selected: this._labelSel }) : null;
      const label = hit && this._label(hit.id);
      if (label) {
        overEdge = suggest && hit.part !== "body";
        overLabel = suggest && hit.part === "body";
        html = `<div class="mono small">${esc(this._fullTime(label.start))} – ${esc(this._clockTime(label.end))}</div>
          <div><strong>${esc(labelText(label))}</strong> <span class="muted small">${esc(formatDuration(label.end - label.start))}</span></div>
          ${suggest ? `<div class="muted small">${overEdge ? "drag to resize" : "drag to move · click to edit"}</div>` : ""}`;
      } else {
        html = `<div class="mono small">${esc(this._fullTime(t))}</div>${suggest ? `<div class="muted small">drag across a period to label it</div>` : ""}`;
      }
    } else if (svg.dataset.row === "results") {
      const hit = this._resultsHit(ev);
      if (hit?.run) {
        const r = hit.run;
        html = `<div class="mono small">${esc(this._fullTime(r.started))}</div>
          <div><strong>${esc(outcomeText(r))}</strong></div>
          ${r.trigger ? `<div class="small">${esc(describeTrigger(r.trigger))}</div>` : ""}
          <div class="muted small">click for details</div>`;
      } else if (hit?.call) {
        const c = hit.call;
        html = `<div class="mono small">${esc(this._fullTime(c.t))}</div><div><strong>${esc(c.domain)}.${esc(c.service)}</strong></div>`;
      } else {
        html = `<div class="mono small">${esc(this._fullTime(t))}</div>`;
      }
    }
    const wrapEl = this.shadowRoot.getElementById("tl-wrap");
    wrapEl?.classList.toggle("over-mark", overMark);
    wrapEl?.classList.toggle("over-edge", overEdge);
    wrapEl?.classList.toggle("over-label", overLabel);
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
    this.shadowRoot.getElementById("tl-wrap")?.classList.remove("over-mark", "over-edge", "over-label");
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
    else if (action === "reset-shift") this._shiftTrackBy(el.dataset.entity, -trackOffset(this._track(el.dataset.entity)));
    else if (action === "ed-apply") this._applyEditor();
    else if (action === "ed-delete") this._deleteSelected();
    else if (action === "ed-add" && this._sel) this._addChange(this._sel.entity_id, Number(el.dataset.t));
    else if (action === "ed-prev" || action === "ed-next") this._stepChange(action === "ed-next" ? 1 : -1);
    else if (action === "ed-close") {
      this._select(null);
      this._render();
    } else if (action === "mode") this._setMode(el.dataset.mode);
    else if (action === "label-create") this._createLabel(el.dataset.kind, el.dataset.anchor);
    else if (action === "label-cancel") {
      this._labelDraft = null;
      this._render();
    } else if (action === "label-close") {
      this._labelSel = null;
      this._labelError = null;
      this._render();
    } else if (action === "label-delete") this._deleteLabel(this._labelSel);
    else if (action === "label-apply") this._applyLabelEditor();
    else if (action === "suggest") this._runSuggest();
    else if (action === "cfg-revert") this._revertConfig();
    else if (action === "sc-save") this._onSave(false);
    else if (action === "sc-save-as") this._onSave(true);
    else if (action === "sc-save-name") this._saveScenario(!!this._saveForm?.asNew, this.shadowRoot.getElementById("sc-name")?.value);
    else if (action === "sc-cancel") {
      this._saveForm = null;
      this._scError = null;
      this._render();
    } else if (action === "sc-toggle") {
      this._scenariosOpen = !this._scenariosOpen;
      this._confirm = null;
      if (this._scenariosOpen) this._listScenarios();
      else this._render();
    } else if (action === "sc-load") this._loadScenario(el.dataset.id);
    else if (action === "sc-load-confirm") this._loadScenario(el.dataset.id, true);
    else if (action === "sc-delete") this._deleteScenario(el.dataset.id);
    else if (action === "sc-delete-confirm") this._deleteScenario(el.dataset.id, true);
    else if (action === "sc-confirm-cancel") {
      this._confirm = null;
      this._render();
    }
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
    else if (id === "auto-run") this._setAutoRun(ev.target.checked);
    else if (id === "lb-kind" && this._labelSel) this._updateLabel(this._labelSel, { kind: ev.target.value });
    else if (id === "lb-anchor" && this._labelSel) this._updateLabel(this._labelSel, { anchor: ev.target.value });
    else if (id === "tol-before" || id === "tol-after") this._setTolerance(id === "tol-before" ? "before" : "after", ev.target.value);
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
    if ((id === "lb-start" || id === "lb-end") && this._labelSel) {
      const value = (name) => this.shadowRoot.getElementById(name)?.value;
      this._labelEdDraft = { id: this._labelSel, start: value("lb-start"), end: value("lb-end") };
      return;
    }
    if (id === "sc-name" && this._saveForm) {
      this._saveForm.name = ev.target.value;
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
    const inEditor = ["ed-time", "ed-state", "ed-attrs", "lb-start", "lb-end", "lb-kind", "lb-anchor"].includes(origin.id);
    if (ev.key === "Escape" && (!typing || inEditor) && (this._labelDraft || this._labelSel || this._labelDrag)) {
      clearTimeout(this._labelDrag?.timer);
      this._labelDrag = null;
      if (this._labelDraft) this._labelDraft = null;
      else this._labelSel = null;
      this._labelError = null;
      return this._render();
    }
    if (this._sel && ev.key === "Escape" && (!typing || inEditor)) {
      this._select(null);
      return this._render();
    }
    if (typing) return;
    if ((ev.key === "Delete" || ev.key === "Backspace") && this._labelSel && this._mode === "suggest") {
      ev.preventDefault();
      return this._deleteLabel(this._labelSel);
    }
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
    if (["lb-start", "lb-end"].includes(ev.target.id) && ev.key === "Enter") {
      ev.preventDefault();
      return this._applyLabelEditor();
    }
    if (ev.target.id === "sc-name") {
      if (ev.key === "Enter") {
        ev.preventDefault();
        this._saveScenario(!!this._saveForm?.asNew, ev.target.value);
      } else if (ev.key === "Escape") {
        this._saveForm = null;
        this._render();
      }
      return;
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
      .auto-run { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; user-select: none; }
      .updating { opacity: .55; transition: opacity .2s; }
      .updating-note { display: inline-flex; align-items: center; gap: 4px; }
      .spinner.small { width: 10px; height: 10px; border-width: 2px; }
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
      #cfg-editor { position: relative; }
      #cfg-editor ha-yaml-editor { display: block; }
      #cfg-marks { position: absolute; left: 0; top: 0; width: 0; height: 0; overflow: hidden; pointer-events: none; z-index: 1; }
      .m-line { position: absolute; left: 0; right: 0; border-left: 3px solid; }
      .m-line.changed { border-color: var(--accent-color, #ff9800); background: rgba(255,152,0,.10); }
      .m-line.added { border-color: var(--success-color, #43a047); background: rgba(67,160,71,.12); }
      .m-removed { position: absolute; left: 0; width: 0; height: 0; margin-top: -5px; border-style: solid; border-width: 5px 0 5px 7px; border-color: transparent transparent transparent var(--error-color, #db4437); }
      .cfg-diff { margin-top: 8px; font-size: 0.85em; }
      .cfg-diff summary { cursor: pointer; color: var(--secondary-text-color); }
      .d-legend { display: inline-flex; gap: 2px; margin-right: 6px; vertical-align: -1px; }
      .d-bar { display: inline-block; width: 3px; height: 11px; }
      .d-bar.changed { background: var(--accent-color, #ff9800); }
      .d-bar.added { background: var(--success-color, #43a047); }
      .d-body { margin-top: 6px; border: 1px solid var(--divider-color); border-radius: 6px; overflow: auto; max-height: 320px; font-size: 0.95em; }
      .d-row { display: flex; white-space: pre; line-height: 1.45; }
      .d-row.d-add { background: rgba(67,160,71,.14); }
      .d-row.d-del { background: rgba(219,68,55,.14); }
      .d-n { flex: none; width: 3.5em; text-align: right; padding-right: 6px; color: var(--secondary-text-color); user-select: none; }
      .d-k { flex: none; width: 1.2em; color: var(--secondary-text-color); user-select: none; }
      .d-gap { color: var(--secondary-text-color); padding-left: 3.5em; border-top: 1px dashed var(--divider-color); border-bottom: 1px dashed var(--divider-color); }
      #cfg-error { margin-top: 6px; white-space: pre-wrap; }
      .cfg-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
      #tl svg { display: block; touch-action: pan-y; overflow: hidden; }
      #tl svg.track { cursor: grab; }
      #tl svg.track:active, #tl svg.track.shifting { cursor: grabbing; }
      svg.results { cursor: pointer; }
      .scrollbar { position: relative; height: 12px; margin: 3px 0 2px; border-radius: 6px; background: var(--secondary-background-color); touch-action: none; cursor: pointer; }
      .scrollbar .thumb { position: absolute; top: 1px; bottom: 1px; border-radius: 5px; background: var(--secondary-text-color); opacity: .45; cursor: grab; min-width: ${SCROLL_MIN_THUMB}px; box-sizing: border-box; }
      .scrollbar .thumb:hover, .scrollbar .thumb.dragging { opacity: .7; }
      .scrollbar .thumb.dragging { cursor: grabbing; }
      .scrollbar.all { cursor: default; }
      .scrollbar.all .thumb { opacity: .18; cursor: default; }
      @media (pointer: coarse) { .scrollbar { height: 22px; border-radius: 11px; } .scrollbar .thumb { border-radius: 10px; } }
      .chip.shift { background: rgba(3,155,229,.15); color: var(--info-color, #039be5); }
      .chip.shift.live { box-shadow: inset 0 0 0 1px var(--info-color, #039be5); }
      svg.track.shifting { outline: 1px dashed var(--info-color, #039be5); outline-offset: -1px; }
      .ev-knob { fill: var(--card-background-color, #fff); stroke: var(--primary-text-color); stroke-width: 1.5; }
      .row { margin-top: 6px; }
      .axis-row { position: sticky; top: 0; z-index: 2; background: var(--card-background-color, var(--primary-background-color)); margin-top: 0; }
      .track-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 8px; font-size: 0.85em; padding: 2px 0; min-width: 0; }
      .track-head .name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .track-head .eid { color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
      .track-head .meta { color: var(--secondary-text-color); margin-left: auto; text-align: right; }
      .track-head .stale { color: var(--warning-color, #ff9800); }
      .legend-item { white-space: nowrap; }
      .legend-mark { vertical-align: -2px; margin-right: 3px; overflow: visible; }
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
      .alert-body { display: flex; flex-direction: column; gap: 6px; min-width: 0; flex: 1; }
      .run-error .msg { color: var(--primary-text-color); }
      .run-error details summary, .wgroup summary { cursor: pointer; }
      .run-error details summary { color: var(--secondary-text-color); font-size: 0.9em; }
      .run-error pre.stderr { margin: 6px 0 0; }
      .wgroup ul { margin: 4px 0 4px; padding-left: 20px; }
      .wgroup li { margin: 2px 0; word-break: break-word; }
      .w-at { color: var(--secondary-text-color); margin-right: 8px; white-space: nowrap; }
      .w-label { font-weight: 500; }
      .sc { flex: 1 1 100%; display: flex; flex-direction: column; gap: 8px; border-top: 1px solid var(--divider-color); padding-top: 12px; min-width: 0; }
      .sc-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; min-width: 0; }
      .sc-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; max-width: 100%; }
      .sc-actions { display: inline-flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-left: auto; }
      .sc-actions button { padding: 4px 12px; }
      .sc-form { display: flex; gap: 6px; flex-wrap: wrap; }
      .sc-form input { flex: 1 1 220px; }
      .sc-confirm { margin: 0; flex-wrap: wrap; }
      .sc-confirm > div { flex: 1 1 240px; }
      .sc-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; max-height: 360px; overflow: auto; }
      .sc-list li { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; justify-content: space-between; padding: 8px 10px; border-radius: 6px; background: var(--secondary-background-color); }
      .sc-list li.current { box-shadow: inset 3px 0 0 var(--primary-color); }
      .sc-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 260px; word-break: break-word; }
      .sc-row-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
      .sc-row-actions button { padding: 3px 10px; }
      button.danger { background: var(--error-color, #db4437); border-color: var(--error-color, #db4437); color: var(--text-primary-color, #fff); }
      button.danger:hover:not([disabled]) { background: var(--error-color, #db4437); filter: brightness(1.1); }
      .segmented.mode button { font-size: 0.95em; padding: 5px 16px; }
      .suggest-row { gap: 10px 12px; }
      .tolerance { display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap; }
      .tolerance input { width: 5em; padding: 4px 6px; }
      #tl-wrap { -webkit-touch-callout: none; }
      svg.labels { background: var(--secondary-background-color); border-radius: 4px; }
      svg.labels.editable { cursor: crosshair; }
      .suggest-mode svg.axis { cursor: crosshair; }
      #tl-wrap.over-edge svg.labels { cursor: ew-resize; }
      #tl-wrap.over-label svg.labels { cursor: grab; }
      .lb.fire { fill: var(--success-color, #43a047); fill-opacity: .28; stroke: var(--success-color, #43a047); stroke-width: 1; }
      .lb.fire.while { stroke-dasharray: 5 3; fill-opacity: .18; }
      .lb.nofire { fill: url(#nofire-hatch); stroke: var(--error-color, #db4437); stroke-width: 1; }
      .lb.sel { stroke-width: 2.5; stroke: var(--primary-text-color); }
      .nofire-bg { fill: rgba(219, 68, 55, .12); }
      .nofire-line { stroke: var(--error-color, #db4437); stroke-width: 2.5; opacity: .55; }
      .lb-anchor { stroke: var(--success-color, #43a047); stroke-width: 3; }
      .lb-anchor-flag { fill: var(--success-color, #43a047); }
      .lb-text { fill: var(--primary-text-color); font-size: 11px; pointer-events: none; }
      .lb-empty { fill: var(--secondary-text-color); font-size: 11px; font-style: italic; pointer-events: none; }
      .lb-handle { fill: var(--card-background-color, #fff); stroke: var(--primary-text-color); stroke-width: 1.5; }
      .lb-brush { fill: var(--primary-color); fill-opacity: .18; stroke: var(--primary-color); stroke-width: 1.5; stroke-dasharray: 4 3; }
      .lb-overlay { pointer-events: none; }
      .lb-overlay.fire { fill: var(--success-color, #43a047); fill-opacity: .08; }
      .lb-overlay.nofire { fill: var(--error-color, #db4437); fill-opacity: .08; }
      .lb-overlay-anchor { stroke: var(--success-color, #43a047); stroke-width: 1.5; opacity: .45; pointer-events: none; }
      .label-pop { position: absolute; z-index: 4; display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; max-width: min(440px, 100%); box-sizing: border-box;
        background: var(--card-background-color, #fff); color: var(--primary-text-color); border: 1px solid var(--divider-color); border-radius: 8px; box-shadow: 0 4px 14px rgba(0,0,0,.3); }
      .label-pop[hidden] { display: none; }
      .pop-group { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
      .pop-group button { padding: 4px 10px; }
      .pop-group button.primary.small-btn { padding: 4px 12px; }
      .pop-group .nofire-btn { border-color: var(--error-color, #db4437); }
      .pop-group .nofire-btn.primary { background: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
      .label-editor.fire { border-left-color: var(--success-color, #43a047); }
      .label-editor.nofire { border-left-color: var(--error-color, #db4437); }
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
