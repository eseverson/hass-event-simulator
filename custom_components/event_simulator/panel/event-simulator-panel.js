/* Event Simulator sidebar panel.
 * Plain web component: no build step, no framework, no external dependencies. Uses
 * Home Assistant's CSS variables so it matches the active theme, light or dark.
 *
 * Flow: pick an automation -> pick a window -> tracks load from the recorder for the
 * entities the automation references -> Run sends the scenario to the sandbox and the
 * runs it reports land on a results row under the shared time axis.
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

/* The scenario sent to event_simulator/simulate (see the design spec). Events from all
 * tracks merged into one list ordered by time, as the child replays them. */
export function buildScenario({ automation, start, end, tracks }) {
  const initial_states = {};
  const events = [];
  for (const track of tracks) {
    if (track.initial) initial_states[track.entity_id] = track.initial;
    for (const ev of track.events) events.push(ev);
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
    this._drag = null;
    this._frame = 0;
    this._rendered = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
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
  }

  disconnectedCallback() {
    this._resizeObserver?.disconnect();
    cancelAnimationFrame(this._frame);
  }

  // ---- data ---------------------------------------------------------------

  async _connect() {
    this._loading = "Loading automations…";
    this._render();
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
    this._error = null;
    this._notice = null;
    if (!entityId) return this._render();
    const token = ++this._requestToken;
    this._loading = "Loading automation…";
    this._render();
    try {
      const automation = await this._hass.callWS({ type: "event_simulator/automation", entity_id: entityId });
      if (token !== this._requestToken) return;
      this._automation = automation;
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
        track.events = byEntity.get(track.entity_id);
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
    const { start, end } = this._window;
    track.segments = buildSegments(track.initial, track.events, start, end);
    track.numeric = isNumericStates(track.segments.map((s) => s.state));
    track.merged = track.numeric ? track.segments : mergeSegments(track.segments);
    track.range = track.numeric ? numericRange(track.segments) : null;
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
    this._markStale();
    this._render();
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
    if (!this._automation || this._running) return;
    const scenario = buildScenario({
      automation: this._automation.config,
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
      this.shadowRoot.innerHTML = `<style>${this._css()}</style><div id="root"></div>`;
      this._rendered = true;
      this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
      this.shadowRoot.addEventListener("change", (ev) => this._onChange(ev));
      this.shadowRoot.addEventListener("input", (ev) => this._onInput(ev));
      this.shadowRoot.addEventListener("keydown", (ev) => this._onKeydown(ev));
    }
    const root = this.shadowRoot.getElementById("root");
    const addValue = root.querySelector("#add-track")?.value || "";
    root.innerHTML = this._html();
    const add = root.querySelector("#add-track");
    if (add) add.value = addValue;
    const menu = root.querySelector("ha-menu-button");
    if (menu) {
      menu.hass = this._hass;
      menu.narrow = this._narrow;
    }
    this._bindTimeline();
    this._renderTimeline();
  }

  _html() {
    return `
      <div class="page">
        <div class="header">
          ${this._narrow ? `<ha-menu-button></ha-menu-button>` : ""}
          <h1>Event Simulator</h1>
        </div>
        ${this._setupCard()}
        ${this._timelineCard()}
        ${this._detailCard()}
      </div>`;
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
            <button class="primary" data-action="run" ${!this._automation || this._running || this._loading ? "disabled" : ""}>
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
    const events = this._tracks.reduce((n, t) => n + t.events.length, 0);
    return `Replays ${events} state change${events === 1 ? "" : "s"} on ${this._tracks.length} track${this._tracks.length === 1 ? "" : "s"}. Service calls are recorded, never made.`;
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
            <div class="add">
              <input id="add-track" list="entity-ids" placeholder="Add track: entity id" autocomplete="off" spellcheck="false">
              <datalist id="entity-ids">${entityOptions}</datalist>
              <button data-action="add-track">Add</button>
            </div>
          </div>
          <div class="muted small hint">Drag to pan. Ctrl/⌘ + wheel or wheel over the axis zooms.</div>
          <div id="tl-wrap">
            <div id="tl"></div>
            <div id="cursor" hidden></div>
            <div id="tip" hidden></div>
          </div>
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
    let meta = "";
    if (!track.segments) meta = "loading…";
    else if (!track.segments.length) meta = "no history in this window";
    else if (track.range) meta = `min ${track.range.min} · max ${track.range.max}${unit ? ` ${esc(unit)}` : ""}`;
    else meta = `${track.events.length} change${track.events.length === 1 ? "" : "s"}`;
    const height = track.numeric ? NUMERIC_H : DISCRETE_H;
    const body = track.segments ? (track.numeric ? this._numericSvg(track, scale) : this._discreteSvg(track, scale)) : "";
    return `
      <div class="row">
        <div class="track-head">
          <span class="name">${esc(name || track.entity_id)}</span>
          ${name ? `<span class="eid mono">${esc(track.entity_id)}</span>` : ""}
          <span class="meta">${meta}</span>
          <button class="icon" data-action="remove-track" data-entity="${esc(track.entity_id)}" title="Remove track" aria-label="Remove ${esc(track.entity_id)}">×</button>
        </div>
        <svg class="track" data-row="track" data-entity="${esc(track.entity_id)}" width="${scale.width}" height="${height}">
          ${this._hatch(`h-${hashString(track.entity_id)}`)}
          ${this._gridLines(scale, height)}
          ${body}
        </svg>
      </div>`;
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
    const stale = this._resultStale ? ` <span class="stale">timeline changed since this run</span>` : "";
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
    wrap.addEventListener("pointercancel", () => (this._drag = null));
    wrap.addEventListener("pointerleave", () => this._hideHover());
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

  _onPointerDown(ev) {
    if (ev.button !== 0 || !ev.target.closest?.("svg")) return;
    this._drag = { x: ev.clientX, view: this._view, moved: false, id: ev.pointerId };
    ev.currentTarget.setPointerCapture?.(ev.pointerId);
  }

  _onPointerMove(ev) {
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
    const drag = this._drag;
    this._drag = null;
    ev.currentTarget.releasePointerCapture?.(ev.pointerId);
    if (!drag || drag.moved) return;
    const hit = this._resultsHit(ev);
    if (hit?.run) {
      this._selectedRun = hit.run.index;
      this._render();
    }
  }

  /* The run or service call under the pointer on the results row. Uses elementFromPoint
   * rather than ev.target because pointer capture retargets events to the wrapper. */
  _resultsHit(ev) {
    if (!this._result) return null;
    const el = this.shadowRoot.elementFromPoint?.(ev.clientX, ev.clientY);
    const svg = el?.closest?.("svg[data-row=results]");
    if (!svg) return null;
    const x = ev.clientX - svg.getBoundingClientRect().left;
    const scale = makeScale(this._view[0], this._view[1], this._width);
    const run = findNearest(this._result.runs.map((r) => ({ x: scale.toX(r.started), run: r })), x, 10);
    if (run) return run;
    return findNearest(this._result.calls.map((c) => ({ x: scale.toX(c.t), call: c })), x, 4);
  }

  _showHover(ev) {
    const el = this.shadowRoot.elementFromPoint?.(ev.clientX, ev.clientY) || ev.target;
    const svg = el?.closest?.("svg[data-row]");
    const tip = this.shadowRoot.getElementById("tip");
    const cursor = this.shadowRoot.getElementById("cursor");
    if (!svg || !tip || svg.dataset.row === "axis") return this._hideHover();
    const { x, y } = this._localX(ev);
    const svgX = ev.clientX - svg.getBoundingClientRect().left;
    const scale = makeScale(this._view[0], this._view[1], this._width);
    const t = scale.toT(svgX);
    let html = "";
    if (svg.dataset.row === "track") {
      const track = this._tracks.find((tr) => tr.entity_id === svg.dataset.entity);
      const seg = track?.segments && segmentAt(track.segments, t);
      const unit = track ? this._unit(track) : "";
      html = `<div class="mono small">${esc(this._fullTime(t))}</div>
        <div><strong>${seg ? esc(seg.state) : "no data"}</strong>${seg && track.numeric && !isMissing(seg.state) && unit ? ` ${esc(unit)}` : ""}</div>
        ${seg ? `<div class="muted small">since ${esc(this._fullTime(seg.from))}</div>` : ""}`;
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
    if (ev.target.id !== "auto-filter") return;
    this._autoFilter = ev.target.value;
    // Only the options change, so the search box keeps focus and caret.
    const select = this.shadowRoot.getElementById("automation");
    if (select) select.innerHTML = this._automationOptions();
  }

  _onKeydown(ev) {
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
      #tl-wrap { position: relative; user-select: none; -webkit-user-select: none; }
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
