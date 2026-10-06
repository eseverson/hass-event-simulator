# Event Simulator — design

Origin: SiYuan note `Software/Ideas/hass event simulator`. Decisions made with the user on 2026-10-05: HACS integration with a sidebar panel; v1 is replay/simulate (generating triggers from history comes later); action calls are recorded only, never applied to state; the sandbox runs in a worker subprocess (approach A below).

## Goal

Pick an automation, pull the recorded history of the entities it depends on onto a timeline, edit that timeline, and replay it against the automation in a sandbox that cannot touch the live instance. The answer is: did it fire, when, which trigger, which condition stopped it, and which actions it would have called.

Success: for a real automation and a real day of history, the simulator reports the same runs the live instance's traces show for that day (where the automation was unchanged), and an edited timeline produces the run/no-run outcome HA itself would.

## Non-goals (v1)

- Generating triggers/conditions from history (v2; will reuse the timeline and engine to validate its suggestions).
- Applying action side effects to state, or several automations interacting.
- Scripts, blueprints-as-such (a blueprint-based automation works, since its config is resolved), and `automation.reload`-style lifecycle events.

## Why a subprocess

HA's trigger machinery reads the clock through module-level functions (`homeassistant.helpers.event.time_tracker_utcnow`, `dt_util.utcnow`, `time.time`) and loop timer handles; its own tests fake time by patching those globals. Doing that in the live process would skew time for every integration. A second `HomeAssistant` object in the live process would also fight singletons and context vars. So the replay runs in a child process started from the live interpreter (`sys.executable`), importing the same `homeassistant` package — real semantics, full isolation, and virtual time is ours to own.

## Components

Integration domain `event_simulator`, layout mirroring `hass-integration-pins` (vanilla-JS panel, websocket API, pytest-homeassistant-custom-component tests).

1. **`vclock.py` — virtual-time event loop.** An asyncio event loop whose `time()` is virtual. When nothing is ready to run, it jumps virtual time to the earliest of the next scheduled timer and the next scenario event, instead of sleeping. Provides a wall-clock mapping (`utcnow()` = scenario start + elapsed virtual seconds) that the engine patches into `dt_util.utcnow`, `time_tracker_utcnow`, `time_tracker_timestamp` and `time.time`. Pure asyncio, no HA import; unit-tested alone.

2. **`sandbox/engine.py` — the replay engine (runs in the child).** Input: a scenario JSON (below). Boots a throwaway `HomeAssistant` on the virtual loop with a temp config dir and no recorder/http/network: core config copied from the live instance (time zone, location, elevation, units), `homeassistant` + `automation` set up with just the automation under test. Seeds initial states, then plays each timeline event at its virtual time via `hass.states.async_set`. Service calls are intercepted at `ServiceRegistry` — every call is recorded (time, domain, service, data, target, triggering run) and returns success/empty response; nothing executes. Automation traces are captured from HA's own `trace` data and serialized with `as_extended_dict()`. Output: result JSON. Runs until the scenario end plus any in-flight run finishing or a bounded overrun.

3. **`sandbox/__main__.py` + `runner.py` — process boundary.** Child reads one scenario JSON on stdin, writes one result JSON on stdout, logs on stderr. Live side (`runner.py`) spawns it with `asyncio.create_subprocess_exec(sys.executable, ...)`, enforces a wall-clock timeout, kills on timeout/cancel, and turns a crash into a structured error carrying the child's stderr tail. One replay per child; no long-lived worker in v1.

4. **`history.py` — timeline from the recorder.** Given entity ids and a window, read state changes through the recorder's history API on the recorder executor, and the state of each entity at the window start. Also: the automation list (entity id, alias, config via the automation entity's `raw_config`), and the entities an automation references (triggers, conditions, templates — HA's own `referenced_entities` plus template entity extraction) so tracks auto-populate.

5. **`websocket.py` — API.** Admin-only commands:
   - `event_simulator/automations` → `[{entity_id, alias, id}]`
   - `event_simulator/automation` `{entity_id}` → `{config, referenced_entities}`
   - `event_simulator/history` `{entity_ids, start, end}` → timeline tracks
   - `event_simulator/simulate` `{scenario}` → result. `scenario.automation` may be a config object or YAML text (custom tags such as `!include` / `!secret` are refused); `scenario.events` may be sent packed as `{entity_ids, attributes, rows}` with each row `[at, entity index, state, attributes index]`, which the panel always does: Home Assistant's websocket drops the connection on a message over 4 MiB, and a week of a chatty sensor is several times that unpacked; `core` (with Labs preview features), `registry` (the registry snapshot) and `services` default to the live instance's. Window and size caps match `history`. Failures are websocket errors `{code, message}`: the child's own refusal (`invalid_scenario`, `invalid_automation`, `unsupported_automation`), or a runner failure (`timeout`, `sandbox_failed`, `output_too_large`) whose error also carries `stderr_tail`. At most two replays run at once; a replay is killed after 300 s or when the request goes away.
   - `event_simulator/simulate/subscribe` `{scenario}`: the same replay as a cancellable subscription, which the panel's auto-run uses. It is acknowledged at once and answered by one event, `{result}` or `{error: {code, message[, stderr_tail]}}`; unsubscribing or closing the connection kills the child, and a newer replay on the same connection supersedes the one in flight (killed, answered `superseded`), so a burst of edits holds at most one of the two slots. The one-shot command's replay is also killed when its connection closes.

6. **Panel (`panel/event-simulator-panel.js`).** Single vanilla web component, no build step. Flow: choose automation → window (default: last 24 h) → tracks load from history for referenced entities (add/remove tracks by entity id) → Run. Timeline: one row per entity (state segments for discrete, a line for numeric), a shared time axis with zoom/pan, editable events (drag to shift, click to edit value/attributes, add, delete), a results row with run markers, one shape and color per engine outcome (fired / stopped by condition / aborted / cancelled / dropped, by which mode limit / still running after the overrun / errored; `other` only for an unknown value), counted by name in the legend, and service-call ticks. Selecting a run shows its trigger, condition results and the recorded calls; the raw trace is expandable. The automation config can be edited in a YAML textarea for what-ifs, without saving to the live instance.

7. **Scenario store (`store.py`).** Save/load named scenarios (automation config + timeline + window) via `helpers.storage.Store`. `scenarios/save` takes `events` as a list or packed, and the store keeps and `scenarios/get` returns them packed (`packed.py`); unlike simulate's, the packing is lossless: a row may go on with flags for `last_updated`/`last_changed` equal to `at` and `edited`, then an object of any other fields. Storage version 2 (version 1 kept plain lists and is migrated on load). At most 500,000 events across all scenarios, since every save rewrites the file.

## Scenario format (live ↔ child)

```json
{
  "version": 1,
  "core": {"time_zone": "...", "latitude": 0, "longitude": 0, "elevation": 0, "unit_system": "metric", "preview_features": [["automation", "new_triggers_conditions"]]},
  "start": "2026-10-04T00:00:00+00:00",
  "end":   "2026-10-05T00:00:00+00:00",
  "automation": { "...": "automation config dict" },
  "initial_states": {"binary_sensor.door": {"state": "off", "attributes": {}, "last_changed": "2026-10-03T21:40:00+00:00", "last_updated": "2026-10-03T21:40:00+00:00"}},
  "events": [{"at": "2026-10-04T07:12:03+00:00", "entity_id": "binary_sensor.door", "state": "on", "attributes": {}}],
  "services": {"light": ["turn_on", "turn_off"]},
  "overrun_seconds": 3600,
  "seed": 0,
  "registry": {"versions": {"entities": [1, 20], "...": "..."}, "entities": [], "devices": [], "areas": [], "floors": [], "labels": [],
               "config_entries": [{"entry_id": "...", "domain": "hue", "title": "Hue bridge"}], "truncated": {"entities": 12}}
}
```

The window is half-open, `[start, end)`. `last_changed` / `last_updated` on an initial state are optional (absent: the state is stamped `start`, as the recorder clamps it); they let a `for:` early in the window see how long the state really held (#10). Events outside the window are skipped with a warning; extra event keys (`last_changed`, ...) are ignored, since HA derives them from the write. `registry` (optional, #12) is the slice of the live registries the automation can see, built by `history.async_registry_snapshot(hass, automation_config, entity_ids)`: the entities given (timeline + referenced), whatever the config names by `area_id` / `device_id` / `floor_id` / `label_id` or entity registry id, and areas/floors/labels/devices a template names in a quoted literal; areas expand to their entities (directly or via a device), floors to areas, devices to entities, labels to what carries them; then each entity's device and area, each area's floor and every label in play. Entries are in the registries' storage format with `versions`, and the child writes them into its temp `.storage` so the registries load (and migrate) them themselves; config entries are `{entry_id, domain, title}` stubs (no data), loaded disabled so they are never set up. Capped at 500 entities / 250 devices, the overflow counted in `truncated` and reported as a `registry_truncated` warning. With it, `area_entities()` / `device_attr()` / `label_entities()` templates and device triggers/conditions/actions of entity-domain integrations (binary_sensor, light, sensor, ...) replay. Integration-specific device automations (zha, mqtt, hue, ...) and trigger/condition platforms that are not built into HA (a custom integration, or a typo) are refused with an `unsupported_automation` error naming each one by config path; custom integrations are never loaded in the child (#14). The `simulate` command fills `registry` from the live registries when the panel does not send one, as it does `core`. `core.preview_features` (optional) lists the Labs preview features enabled on the live instance as `[domain, preview_feature]` pairs (`history.async_core_config` fills it); the child sets up `labs` and turns them on before validating the automation, so Labs-gated purpose-specific triggers and conditions (#11) validate as they do live, and a feature this HA does not know is a `preview_feature_unknown` warning. Optional: `services` (the live instance's services, so calls to anything else are flagged), `overrun_seconds` (how long runs in flight at `end` may continue, default 1 h, max 24 h), `seed` (Python `random` seed; HA uses it for the 50–500 ms offset of time-change triggers and for run ids, so a fixed seed makes replays repeatable). `traces` (#28) sets how much each run carries: `full` (default, what the panel shows), `summary` (no step-by-step `trace`; adds `conditions`, each top-level condition's result by path) or `none` (only `id`, times, `outcome` and the trigger's `idx`/`id`, and no `service_calls`); trimmed runs are reduced as each finishes, so 25 automations over 30 days come to about 16 MiB in `summary` and 6 MiB in `none`, against about 88 MiB in `full` (over the runner's 64 MiB output cap).

Result:

```json
{
  "version": 1, "start": "...", "end": "...", "error": null,
  "runs": [{"id": "run id", "started": "...", "finished": "... | null", "trigger": {"idx", "id", "alias", "platform", "description", "entity_id", "from_state", "to_state"},
            "outcome": "fired|condition_failed|dropped|aborted|cancelled|error|unfinished|not_triggered", "stopped_by": "condition/0 | mode_single | max_runs | recursion | action/N | <not-triggered reason> | null",
            "script_execution": "HA's value", "error": "message | null", "trace": {"...": "as_extended_dict()"}}],
  "service_calls": [{"at", "domain", "service", "data", "target", "run", "context": {"id", "parent_id", "user_id"}}],
  "warnings": [{"code": "event_outside_window|unknown_service|preview_feature_unknown|registry_truncated|run_unfinished|initial_state_after_start|initial_state_ignored|late_attach|log|log_truncated", "message", "at"}]
}
```

A refused scenario or automation yields the same shape with `error: {"code": "invalid_scenario" | "invalid_automation" | "unsupported_automation", "message"}` and empty lists. The child is started with `sandbox.child_command()` (`[sys.executable, <sandbox dir>]`): exit 0 whenever a result was written (including those errors), 2 when stdin is not a JSON object, 1 on a crash (an `internal_error` result plus the traceback on stderr). Engine details (boot lead, seeding, end-of-window handling) are in `sandbox/engine.py`'s module docstring.

## Error handling

- Invalid automation config → validated in the child with HA's own schema; returned as a structured error the panel shows inline.
- Child crash/timeout → runner error with stderr tail; panel shows it, never hangs.
- Unknown services/entities in actions → still recorded (the registry intercept accepts anything); a warning lists services that do not exist on the live instance.
- History gaps / unavailable states → replayed as-is (`unavailable` is a real state automations see).

## Testing

- `vclock`: pure unit tests (timers fire in order at virtual times; a 24 h scenario completes in well under a second of wall time).
- Engine: end-to-end through the real child process with fixture scenarios — state trigger, `numeric_state` with `for:`, `time` trigger, `sun` condition, template condition, `delay` + `wait_for_trigger`, `mode: single` dropping a re-trigger. Assert runs and recorded calls.
- Runner: timeout kill, crash surfacing.
- History/websocket: pytest-homeassistant-custom-component with the recorder fixture.
- Panel: kept thin; logic that can live in Python does.
