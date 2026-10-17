# Device-trigger and bus-event replay and editing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay automations whose device triggers come from ZHA (through ZHA's own trigger code in the sandbox), and make recorded bus events — device presses and plain event-trigger events — editable lanes on the timeline.

**Architecture:** A per-integration adapter has a live half (reads data the integration already holds; never calls into it to change behavior) and a sandbox half (seeds that data where the integration's unmodified `device_trigger` module looks). The scenario carries the data as `device_triggers`. The panel gets per-device "kinds" (which bus event each `(type, subtype)` is) from a new websocket command and draws one editable lane per device and one per plain event-trigger type; lanes reuse the state tracks' timeline functions, undo and scenario saving.

**Tech Stack:** Python 3.13+ / Home Assistant 2026.2.3+ custom integration, pytest + pytest-homeassistant-custom-component, vanilla-JS web component tested with `node --test`.

**Spec:** `docs/superpowers/specs/2026-10-16-device-trigger-events-design.md` (read it first; this plan argues from it).

**Tracking issue:** me/hass-event-simulator#48 — comment progress there as tasks land.

**Status (2026-10-16):** Tasks 1–6 done (66b39fd, b558724, ebdfb53, cf90b98, 9e8075c, 7edb1d9) plus the final-review fix pass (a0e4add). Task 6 deviates toward the spec: lanes' events are always saved, not only when edited. Task 7: the user deployed it to the live instance; #48 closed.

## Global Constraints

- No monkeypatching of core or integration code in the live process, and no stand-in `hass` handed to integration code there. The live half may only read data an integration already keeps.
- No ZHA knowledge outside the ZHA adapter (`device_triggers/zha.py` live, the `zha` entry of `sandbox/device_triggers.py`).
- No guessed filters: an adapter that cannot produce a device's data makes the replay refuse, naming the device.
- Device conditions and device actions of integration-specific platforms stay refused.
- Sandbox modules import nothing from the parent integration package (`sandbox/__init__.py` rule).
- Floor: Home Assistant 2026.2.3 (`hacs.json`); CI runs `minimum` and `latest`.
- Every edit to a lane goes through the panel's one undo history and marks results stale for Auto-run.
- Conventional Commits (`feat(sandbox): ...`, `feat(panel): ...`); docs updated in the task whose behavior they describe.

## Review Focus

- **Trigger edited in the YAML editor** (short → double press): the lanes' ringing and the replay follow the edited config, not the saved one. Pinned in Task 2 (`used` follows the config passed) and Task 4 (lanes refetched when the device-trigger part of the run config changes).
- **Two triggers on one device** (short and double press of the same remote): one lane, both kinds ringed, each press fired once. Pinned in Task 2 and Task 3.
- **A window with no presses:** the device lane is still drawn, empty, and offers the device's kinds to add. Pinned in Task 3.
- **`zha_event` dropped by the 20,000-event cap:** the lane shows the "too many events" notice instead of looking empty. Pinned in Task 3.
- **A lane shifted out of the window and back:** events return as the recorded objects, so the lane is not marked edited. Pinned in Task 3.

## File Structure

- Create `custom_components/event_simulator/sandbox/device_triggers.py` — sandbox half: `SEEDERS` by domain, payload parsing, seeding.
- Modify `custom_components/event_simulator/sandbox/engine.py` — parse `device_triggers`, seed before automation setup, adapter-aware `_check_platforms`, docstring.
- Create `custom_components/event_simulator/device_triggers/__init__.py` — live half registry: `async_device_lanes`, `async_export`.
- Create `custom_components/event_simulator/device_triggers/zha.py` — ZHA live adapter.
- Modify `custom_components/event_simulator/websocket.py` — `event_simulator/device_lanes` command; `async_simulate` fills `device_triggers`.
- Modify `custom_components/event_simulator/store.py` — scenarios keep `bus_events` and `lane_shifts`.
- Modify `custom_components/event_simulator/panel/event-simulator-panel.js` — lane model (pure), lane rendering, editing, saving.
- Create `scripts/integration_requirements.py` — prints an integration's requirements and those of its manifest dependencies, for CI and local test setup.
- Modify `.github/workflows/test.yml` — install ZHA's requirements; require the ZHA tests to run.
- Tests: `tests/test_engine_device_triggers.py`, `tests/test_device_triggers.py`, `tests/test_store.py`, `tests/panel/lanes.test.mjs`, `tests/panel/scenarios.test.mjs`, `tests/panel/busevents.test.mjs`.
- Docs: `README.md` (Using it), `docs/superpowers/specs/2026-10-05-event-simulator-design.md` (scenario format).

## Shared shapes

- **Adapter payload, ZHA:** `{"ieee": str, "triggers": [{"type": str, "subtype": str, "data": dict}]}`.
- **Scenario key:** `device_triggers: {domain: {device_id: payload}}`.
- **Kind:** `{"type": str, "subtype": str, "event_type": str, "match": dict, "used": bool}`.
- **Device lane (from `event_simulator/device_lanes`):** `{"id": "device:<device_id>", "domain": str, "device_id": str, "name": str, "event_type": str, "kinds": [Kind]}`.
- **Unsupported entry:** `{"path": "triggers/0", "domain": str, "device_id": str, "reason": "no_adapter" | "no_data" | "unreadable", "message": str}`.
- **Panel lane:** `{id, kind: "device" | "event", event_type, name, kinds?, filters?, initial: null, events: [BusEvent], recorded, shift?}`; a BusEvent is `{at, event_type, data, edited?}`.

---

### Task 1: Sandbox runs ZHA device triggers from seeded data

**Files:**
- Create: `custom_components/event_simulator/sandbox/device_triggers.py`
- Modify: `custom_components/event_simulator/sandbox/engine.py` (`_Scenario`, `_parse_scenario`, `_check_platforms`, `_Replay.run` setup order, module docstring)
- Create: `scripts/integration_requirements.py`
- Modify: `.github/workflows/test.yml`, `tests/conftest.py`
- Test: `tests/test_engine_device_triggers.py`

**Interfaces:**
- Produces: `sandbox/device_triggers.py`: `SEEDERS: dict[str, Callable[[HomeAssistant, dict[str, Any]], None]]`; `parse_device_triggers(value: Any) -> dict[str, dict[str, Any]]` (raises `ValueError` with a message the engine turns into `invalid_scenario`); `seed(hass, device_triggers) -> None`. Engine: `_Scenario.device_triggers: dict[str, dict[str, Any]]`.
- Produces: `scripts/integration_requirements.py <domain>` prints one requirement per line.
- Produces: pytest fixture/marker `requires_zha` in `tests/conftest.py`: skips when `zha` is not importable, fails instead when `EVENT_SIMULATOR_REQUIRE_ZHA=1`.

- [x] **Step 1: Test setup.** Write `scripts/integration_requirements.py`: load the installed HA's `homeassistant/components/<domain>/manifest.json`, collect `requirements` of it and, recursively, of its `dependencies` and `after_dependencies`, print them deduplicated. Install into the venv: `.venv/bin/pip install $(.venv/bin/python scripts/integration_requirements.py zha)`. Check: `.venv/bin/python -c "import homeassistant.components.zha.device_trigger"` exits 0. In `test.yml`, after installing the frontend, add the same install with `$(python scripts/integration_requirements.py zha)`, and set `EVENT_SIMULATOR_REQUIRE_ZHA: "1"` on the pytest step.

- [x] **Step 2: Write the failing tests** in `tests/test_engine_device_triggers.py`, all marked `requires_zha`, each scenario run through `run_child` (`tests/test_engine.py`) with `scenario()` (`tests/test_engine_fidelity.py`). Build the `registry` with `history.async_registry_snapshot` in a pytest-homeassistant `hass` holding a `MockConfigEntry(domain="zha")` and a device with `identifiers={("zha", IEEE)}` (pattern: `tests/test_engine_registry.py` `registries`), `IEEE = "a4:c1:38:ad:af:88:68:87"`. `PAYLOAD = {"ieee": IEEE, "triggers": [{"type": t, "subtype": "button_1", "data": {"endpoint_id": 1, "command": t}} for t in ("remote_button_short_press", "remote_button_double_press", "remote_button_long_press")]}`. Presses: `zha_event` with `{device_ieee: IEEE, device_id, unique_id: IEEE + ":1:0x0006", endpoint_id: 1, cluster_id: 6, command, args: [], params: {}}`.

```python
def test_zha_device_trigger_fires_on_its_presses(results):
    # short 01:00, double 02:00, attribute_updated 03:00, short 04:00; trigger: short press
    r = results["short"]
    assert r["error"] is None
    assert [(run["started"][11:16], run["outcome"]) for run in r["runs"]] == [("01:00", "fired"), ("04:00", "fired")]
    assert {run["trigger"]["platform"] for run in r["runs"]} == {"device"}

def test_two_triggers_on_one_device_fire_once_each(results):
    # triggers: short and double press of the same device; same presses
    assert [run["started"][11:16] for run in results["short_and_double"]["runs"]] == ["01:00", "02:00", "04:00"]

def test_no_payload_for_the_device_is_refused_naming_it(results):
    e = results["no_payload"]["error"]
    assert e["code"] == "unsupported_automation" and DEVICE_ID in e["message"] and "no trigger data" in e["message"]

def test_integration_without_adapter_is_refused(results):
    # trigger: device, domain: hue
    assert "device triggers from 'hue' are not supported yet" in results["hue"]["error"]["message"]

def test_device_condition_from_zha_is_still_refused(results):
    assert results["zha_condition"]["error"]["code"] == "unsupported_automation"

def test_malformed_device_triggers_is_invalid_scenario(results):
    assert results["malformed"]["error"]["code"] == "invalid_scenario"   # device_triggers = {"zha": {DEVICE_ID: {"ieee": 1}}}
```

- [x] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_engine_device_triggers.py -v`
Expected: FAIL (`short` refused with `unsupported_automation`).

- [x] **Step 4: Implement.**
  - `sandbox/device_triggers.py`: `parse_device_triggers` accepts `None` (→ `{}`) or `{domain: {device_id: payload}}`; a domain without a seeder is kept out with no error; the ZHA payload is validated (`ieee` str, `triggers` list of `{type: str, subtype: str, data: dict}`). The ZHA seeder: `from homeassistant.components.zha.helpers import get_zha_data` imported inside the function; `get_zha_data(hass).device_trigger_cache[device_id] = (ieee, {(t["type"], t["subtype"]): t["data"] for t in triggers})`.
  - Engine: `_parse_scenario` stores `device_triggers=parse_device_triggers(raw.get("device_triggers"))`. In `run`, call `device_triggers.seed(self.hass, self.scenario.device_triggers)` after `_serve_calendars()` and before `_load_automations()`. `_check_platforms(hass, config, device_triggers)`: for a node with `trigger: device` / `platform: device` whose integration is not an entity domain, allow it when `domain in SEEDERS and node["device_id"] in device_triggers.get(domain, {})`; else refuse with `"{path}: device triggers from '{domain}' are not supported yet"` (no seeder) or `"{path}: '{domain}' had no trigger data for device {device_id}"` (seeder, no payload). Conditions and actions keep today's message. Add `device_triggers` to the module docstring's timeline step 2 and to the scenario format in `docs/superpowers/specs/2026-10-05-event-simulator-design.md`.

- [x] **Step 5: Run to verify they pass**, plus the whole suite (the old refusal message test in `tests/test_engine_registry.py` may need the new wording).

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add custom_components/event_simulator/sandbox scripts/integration_requirements.py .github/workflows/test.yml tests/conftest.py tests/test_engine_device_triggers.py tests/test_engine_registry.py docs/superpowers/specs/2026-10-05-event-simulator-design.md
git commit -m "feat(sandbox): replay zha device triggers from their trigger data"
```

### Task 2: Live adapter and the `device_lanes` command

**Files:**
- Create: `custom_components/event_simulator/device_triggers/__init__.py`, `custom_components/event_simulator/device_triggers/zha.py`
- Modify: `custom_components/event_simulator/websocket.py` (register `ws_device_lanes`; `async_simulate`)
- Test: `tests/test_device_triggers.py`, `tests/test_websocket_bus_events.py` (simulate fills `device_triggers`; same capture pattern as its existing simulate tests)

**Interfaces:**
- Consumes: payload shape from Task 1.
- Produces: `device_triggers.async_device_lanes(hass, config: dict) -> {"lanes": [DeviceLane], "unsupported": [Unsupported]}`; `device_triggers.async_export(hass, config: dict) -> dict[str, dict[str, Any]]` (the scenario's `device_triggers`); `device_triggers.ADAPTERS: dict[str, Adapter]`, where an adapter module exposes `export(hass, device_id) -> payload` (raises `KeyError` for no data, `device_triggers.Unreadable(Exception)` when ZHA's private lookup is missing) and `kinds(payload) -> list[dict]` (Kind without `used`). Websocket: `event_simulator/device_lanes` `{automation: dict | str}` → the `async_device_lanes` result (YAML parsed with `parse_automation_yaml`).

- [x] **Step 1: Write the failing tests** (`requires_zha`). Seed live ZHA data without a gateway: `get_zha_data(hass).device_trigger_cache[device.id] = (IEEE, {(…): {...}})` with a zha `MockConfigEntry` and registry device named "Outdoor String Lights Button".

```python
async def test_lanes_name_the_device_and_flag_used_kinds(hass, button):
    out = await device_triggers.async_device_lanes(hass, automation(short=True))
    lane, = out["lanes"]
    assert lane["id"] == f"device:{button.id}" and lane["name"] == "Outdoor String Lights Button" and lane["event_type"] == "zha_event"
    assert [(k["type"], k["used"]) for k in lane["kinds"]] == [("remote_button_short_press", True), ("remote_button_double_press", False), ("remote_button_long_press", False)]
    assert lane["kinds"][0]["match"] == {"device_ieee": IEEE, "endpoint_id": 1, "command": "remote_button_short_press"}

async def test_used_follows_the_config_passed(hass, button):          # Review Focus: edited trigger
    lane, = (await device_triggers.async_device_lanes(hass, automation(double=True)))["lanes"]
    assert [k["used"] for k in lane["kinds"]] == [False, True, False]

async def test_two_triggers_on_one_device_make_one_lane(hass, button):  # Review Focus
    lanes = (await device_triggers.async_device_lanes(hass, automation(short=True, double=True)))["lanes"]
    assert len(lanes) == 1 and [k["used"] for k in lanes[0]["kinds"]] == [True, True, False]

async def test_unknown_device_is_unsupported_no_data(hass):
    out = await device_triggers.async_device_lanes(hass, automation(short=True, device_id="gone"))
    assert out["lanes"] == [] and out["unsupported"][0]["reason"] == "no_data"

async def test_integration_without_adapter_is_unsupported(hass):
    assert (await device_triggers.async_device_lanes(hass, hue_automation()))["unsupported"][0]["reason"] == "no_adapter"

async def test_changed_private_api_is_unreadable(hass, button, monkeypatch):
    monkeypatch.delattr("homeassistant.components.zha.device_trigger._get_device_trigger_data")
    assert (await device_triggers.async_device_lanes(hass, automation(short=True)))["unsupported"][0]["reason"] == "unreadable"

async def test_export_is_the_scenario_block(hass, button):
    assert await device_triggers.async_export(hass, automation(short=True)) == {"zha": {button.id: {"ieee": IEEE, "triggers": [...]}}}

async def test_simulate_fills_device_triggers(hass, hass_ws_client, button):  # through async_simulate, runner mocked to capture the scenario
    assert captured["device_triggers"] == {"zha": {button.id: ANY}}
```

- [x] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_device_triggers.py -v`
Expected: FAIL (`ModuleNotFoundError: device_triggers`).

- [x] **Step 3: Implement.** Device triggers are found by walking the config for mappings with `trigger`/`platform` == `"device"` and a `domain` that is not an entity domain (reuse the engine's notion: integration type `"entity"` is generic; use `async_get_integration`). `zha.export` calls `homeassistant.components.zha.device_trigger._get_device_trigger_data(hass, device_id)` (imported inside the function; `AttributeError`/`ImportError` → `Unreadable`), returning `{"ieee": ieee, "triggers": [{"type": t, "subtype": s, "data": d} ...]}` in the dict's order. `zha.kinds` gives `event_type: "zha_event"`, `match: {"device_ieee": ieee, **data}`. Lane `name` is the device registry's `name_by_user or name`. `async_simulate` does `scenario.setdefault("device_triggers", await device_triggers.async_export(hass, scenario["automation"]))` after the YAML parse.

- [x] **Step 4: Run to verify they pass**, then the full suite.

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add custom_components/event_simulator/device_triggers custom_components/event_simulator/websocket.py tests/test_device_triggers.py
git commit -m "feat(device_triggers): read zha trigger data for lanes and replays"
```

### Task 3: Panel lane model (pure functions)

**Files:**
- Modify: `custom_components/event_simulator/panel/event-simulator-panel.js` (next to `busEventsFor` / `busEventLane`)
- Test: `tests/panel/lanes.test.mjs`, `tests/panel/busevents.test.mjs`

**Interfaces:**
- Consumes: DeviceLane / Kind shapes from Task 2.
- Produces (all exported):
  - `matchesData(data: object, match: object) -> boolean` — every key of `match` present in `data` with an equal value; a plain-object value in `match` is matched recursively as a subset (core's schema path); other values by `stableStringify` equality.
  - `kindIndex(kinds: Kind[], event: BusEvent) -> number` — first kind whose `event_type` and `match` fit, else −1.
  - `eventTriggerFilters(automation) -> Map<string, object[] | null>` — per plain event-trigger type, its `event_data` objects; `null` for a type with a trigger without `event_data`, or one whose data is templated (then nothing is dimmed); replaces `eventTriggerTypes` as the source of plain lanes (keep `eventTriggerTypes` for Suggest mode).
  - `buildLanes({deviceLanes, filters, busEvents, start, end}) -> Lane[]` — device lanes first (events of their `event_type` matching one of their kinds), then one event lane per type in `filters` holding that type's events no device lane took. Each lane: `{..., initial: null, events, recorded: {initial: null, events}}`.
  - `laneTypes(deviceLanes, filters) -> string[]` — event types to request from `event_simulator/bus_events`.
  - `laneView(lane, {dropped: string[]}) -> {marks: [{t, index, kind: number, used: boolean, title}], notice: string | null}` — device marks `used` = their kind's `used`; event-lane marks `used` = any filter matches, or `true` when filters is `null`; `notice` = `` `${lane.event_type}: too many events in this window to load; shorten the window` `` when the type is in `dropped`.
  - `newDeviceEvent(lane, kind: number, t: number) -> {event: BusEvent, copiedFrom: string | null}` — data copied from the lane's recorded event of that kind nearest to `t`, else the kind's `match`; `edited: true`.
  - `insertBusEvent(timeline, event, {start, end}) -> {timeline, index}`, `updateBusEvent(timeline, index, data: object) -> timeline` (marks `edited`).
  - Existing `shiftEvent`, `moveEvent`, `deleteEvent`, `shiftTrack`, `restoreShift`, `timelineEdited`, `adoptRecorded` are used unchanged on lanes.
  - `busEventsMessage(start, end, types?: string[])` adds `event_types` when given; `deviceLanesMessage(automation) -> {type: "event_simulator/device_lanes", automation}`.
  - `buildScenario({..., lanes = null})`: when `lanes` is given, `bus_events` = every lane's events (marks stripped, sorted by time); otherwise today's behavior.

- [x] **Step 1: Write the failing tests** in `tests/panel/lanes.test.mjs` with the user's presses (short ×3 at 17:57:51/57/58:03, double ×3 at 58:07/10/13, short ×2 at 18:23:22/36, plus `attribute_updated` and `press_type` events of the same device, and a `zha_event` of another device):

```js
test("a device lane holds only its kinds' events", () => {
  const [lane] = buildLanes({ deviceLanes: [BUTTON], filters: new Map(), busEvents: EVENTS, start, end });
  assert.equal(lane.events.length, 8);                 // no attribute_updated, press_type, other device
});
test("used kinds are ringed, others dimmed", () => {
  const v = laneView(lane, { dropped: [] });
  assert.deepEqual(v.marks.map((m) => m.used), [true, true, true, false, false, false, true, true]);
});
test("two used kinds on one device lane", () => { /* BUTTON with short and double used → all 8 used */ });
test("an empty window still gives the device lane", () => {
  const lanes = buildLanes({ deviceLanes: [BUTTON], filters: new Map(), busEvents: [], start, end });
  assert.equal(lanes.length, 1); assert.equal(lanes[0].events.length, 0);
});
test("a dropped type shows a notice", () => {
  assert.match(laneView(lanes[0], { dropped: ["zha_event"] }).notice, /too many events/);
});
test("a plain zha_event lane gets only what no device lane took", () => { /* filters zha_event → [{command: "x"}]: lane has the 3 non-kind events of the button + other device */ });
test("templated event_data dims nothing", () => { /* filters.get(type) === null → all used */ });
test("matchesData: subset, nested subset, different value", () => {
  assert.ok(matchesData({ a: 1, b: { c: 2, d: 3 } }, { b: { c: 2 } }));
  assert.ok(!matchesData({ a: 1 }, { a: "1" }));
});
test("a new press copies the nearest recorded press of its kind", () => {
  const { event, copiedFrom } = newDeviceEvent(lane, 0, Date.parse("2026-10-16T01:20:00Z"));
  assert.equal(copiedFrom, "2026-10-16T01:23:22.709938+00:00"); assert.equal(event.data.cluster_id, 6); assert.equal(event.edited, true);
});
test("a kind never pressed gets its match only", () => { /* kind 2 (long) → event.data deepEqual kinds[2].match, copiedFrom null */ });
test("a lane shifted out of the window and back is recorded again", () => {   // Review Focus
  const out = shiftTrack(shiftTrack(lane, 3600e3, win), -3600e3, win);
  assert.equal(timelineEdited(out, lane.recorded), false);
});
test("buildScenario sends every lane's events, sorted, marks stripped", () => { /* bus_events from two lanes */ });
test("laneTypes asks for device and plain types once", () => { /* ["tag_scanned", "zha_event"] */ });
```

Add to `busevents.test.mjs`: `busEventsMessage(T0, T1, ["zha_event"])` carries `event_types: ["zha_event"]`.

- [x] **Step 2: Run to verify they fail**

Run: `node --test tests/panel/lanes.test.mjs tests/panel/busevents.test.mjs`
Expected: FAIL (`buildLanes` is not exported).

- [x] **Step 3: Implement** the functions above.

- [x] **Step 4: Run to verify they pass**, plus the whole panel suite and `node --check`.

Run: `node --check custom_components/event_simulator/panel/event-simulator-panel.js && node --test tests/panel/*.test.mjs`
Expected: all pass.

- [x] **Step 5: Commit**

```bash
git add custom_components/event_simulator/panel/event-simulator-panel.js tests/panel/lanes.test.mjs tests/panel/busevents.test.mjs
git commit -m "feat(panel): lane model for device presses and event-trigger events"
```

### Task 4: Panel draws lanes and replays them

**Files:**
- Modify: `custom_components/event_simulator/panel/event-simulator-panel.js` (`_loadBusEvents`, `_busLaneEvents`, `_busEventsRow`, the timeline render that calls it, `_runConfig` change handling, `buildScenario` call at the replay)
- Test: `tests/panel/lanes.test.mjs` (pure helper below)

**Interfaces:**
- Consumes: Task 3 functions; `event_simulator/device_lanes` from Task 2.
- Produces: component state `this._deviceLanes` (last `device_lanes` answer), `this._lanes` (Lane[]), `this._lane(id)`; exported `deviceTriggerKey(automation) -> string` (stable string of the config's device triggers, `""` when none) used to refetch lanes only when it changes.

- [x] **Step 1: Write the failing test**: `deviceTriggerKey` is equal for configs differing only outside device triggers, and differs when a device trigger's `type` changes (Review Focus: edited trigger).

- [x] **Step 2: Run to verify it fails** (`node --test tests/panel/lanes.test.mjs`).

- [x] **Step 3: Implement.**
  - After an automation loads and whenever `deviceTriggerKey(this._runConfig())` changes, call `device_lanes` (latest answer wins, as `_busKey` does) and store `lanes`/`unsupported`.
  - `_loadBusEvents` requests `laneTypes(...)` (skip the request when empty in Replay mode; Suggest mode keeps requesting all types) and keeps `dropped_event_types`.
  - In Replay mode, rebuild `this._lanes` with `buildLanes` when lanes, filters, bus events or the window change, keeping edited lanes (match by `id`) unless the window changed. Draw one row per lane in place of the single Events row: device lane header = name, integration, `"8 presses · 5 trigger this automation"`; event lane header = event type and count. Marks: per-kind color within the lane (`bus-mark b{index}` classes), ringed when `used`, dimmed otherwise; tooltip = kind label, time, data. Kind labels: `hass.localize(`component.${domain}.device_automation.trigger_type.${type}`)` and `...trigger_subtype.${subtype}` after `hass.loadBackendTranslation("device_automation", domain)`, falling back to the raw strings. Show `laneView(...).notice` in the lane. List `unsupported` entries with the results' warnings as "Device trigger at triggers/0 (hue) can't be replayed: …".
  - Suggest mode keeps today's read-only all-types Events row.
  - The replay sends `buildScenario({..., lanes: this._lanes})`.

- [x] **Step 4: Verify.** `node --check` and `node --test tests/panel/*.test.mjs` pass. Then in a browser against a dev HA (the `run` skill, or the project's usual dev instance) with the Task 1 scenario data, confirm the lane draws and Run gives runs at the short presses. Note what was checked in the commit body if anything could not be.

- [x] **Step 5: Commit**

```bash
git add custom_components/event_simulator/panel/event-simulator-panel.js tests/panel/lanes.test.mjs
git commit -m "feat(panel): draw device and event-trigger lanes and replay them"
```

### Task 5: Editing lanes

**Files:**
- Modify: `custom_components/event_simulator/panel/event-simulator-panel.js` (`_commit`, `_setTimeline`, `_undoEdit`, `_resetTracks`, `_deleteSelected`, `_applyEditor`, pointer handlers for `svg[data-row]`, editor render), `README.md`
- Test: `tests/panel/lanes.test.mjs`, `tests/panel/edit.test.mjs`

**Interfaces:**
- Consumes: Tasks 3–4.
- Produces: lane rows are `svg[data-row="lane"][data-lane="<id>"]`; `_commit(label, changes, select)` accepts changes `{lane_id, timeline}` as well as `{entity_id, timeline}` and snapshots both in one undo entry; selection `{lane_id, index}`; exported `deviceEventKind(lane, index, kind) -> timeline` (replaces the event's data with `newDeviceEvent`'s data for that kind, keeping `at`).

- [x] **Step 1: Write the failing tests**: `deviceEventKind` changes a double press to a short press (data `command` becomes `remote_button_short_press`, `edited: true`, same `at`); `updateBusEvent` with invalid JSON is refused by the editor's parser (reuse `parseAttributes`-style `{ok:false, error}` for data text).

- [x] **Step 2: Run to verify they fail.**

- [x] **Step 3: Implement**, mirroring state tracks exactly:
  - Drag a mark → `shiftEvent` (snaps, never passes a neighbor). Drag the lane / Shift-drag → `shiftTrack`, with the offset chip and Unshift. Double-click / double-tap on a lane → device lane: a kind picker then `newDeviceEvent` + `insertBusEvent`; event lane: copy of the nearest event's data via `insertBusEvent`, opened in the editor.
  - Click a mark → editor: time (typed → `moveEvent`), device lane kind select (→ `deviceEventKind`) with a note "data copied from the press at …" or "only the trigger's match data", event lane data as JSON (→ `updateBusEvent`), Delete (→ `deleteEvent`).
  - Reset lane / Reset all include lanes (`timeline: lane.recorded`); every commit marks results stale (Auto-run).
  - README "Edit the timeline": a paragraph on device lanes and event lanes, what ringed/dimmed means, and that only device triggers with an adapter (ZHA today) get a device lane.

- [x] **Step 4: Verify.** `node --test tests/panel/*.test.mjs` passes. In the browser: drag a short press (its run moves), add a long press (no new run), change a double to short (a new run), undo each, Reset all.

- [x] **Step 5: Commit**

```bash
git add custom_components/event_simulator/panel/event-simulator-panel.js tests/panel README.md
git commit -m "feat(panel): move, add, change and delete presses and events"
```

### Task 6: Saved scenarios keep lane edits

**Files:**
- Modify: `custom_components/event_simulator/store.py` (scenario schema, size checks), `custom_components/event_simulator/panel/event-simulator-panel.js` (`scenarioPayload`, `_loadScenario`, new `lanesFromScenario`)
- Test: `tests/test_store.py`, `tests/panel/scenarios.test.mjs`

**Interfaces:**
- Consumes: Lane shape; `restoreShift`, `adoptRecorded`.
- Produces: saved scenario keys `bus_events: [{at, event_type, data, edited?}]` (plain list, at most `MAX_BUS_EVENTS`) and `lane_shifts: {lane_id: {offset}}`; `scenarioPayload({..., lanes = []})` writes them only when some lane is edited; exported `lanesFromScenario(scenario) -> {events: BusEvent[], shifts: object} | null` (`null` for a scenario saved before this change); bus events count toward the store's 500,000-event total.

- [x] **Step 1: Write the failing tests.**
  - `test_store.py`: a save with `bus_events` and `lane_shifts` round-trips through `scenarios/get`; a save with more than `MAX_BUS_EVENTS` bus events is `too_large`; a stored scenario without the keys still loads.
  - `scenarios.test.mjs`: `scenarioPayload` with an edited lane includes `bus_events` with `edited` marks and `lane_shifts`; without edits it has neither; `lanesFromScenario({})` is `null`; after load, unedited events are adopted from the recorded lane (`adoptRecorded`) so the lane is not marked edited.

- [x] **Step 2: Run to verify they fail** (`.venv/bin/pytest tests/test_store.py -q`, `node --test tests/panel/scenarios.test.mjs`).

- [x] **Step 3: Implement.** On load, when `lanesFromScenario` is not `null`, each rebuilt lane takes the saved events of its id's ownership (re-run `buildLanes` over the saved events), `restoreShift` with its saved offset, then `adoptRecorded` against the lane built from the recorder.

- [x] **Step 4: Run to verify they pass**, then `.venv/bin/pytest -q` and `node --test tests/panel/*.test.mjs`.

- [x] **Step 5: Commit**

```bash
git add custom_components/event_simulator/store.py custom_components/event_simulator/panel/event-simulator-panel.js tests/test_store.py tests/panel/scenarios.test.mjs
git commit -m "feat(store): save edited presses and event lanes with a scenario"
```

### Task 7: Check against the user's instance

**Files:** none (verification only).

- [x] **Step 1:** Ask the user before copying `custom_components/event_simulator/` into `~/.config/homeassistant/custom_components/` and restarting Home Assistant: it is outside this repo and restarts their live instance.
- [x] **Step 2:** With their go-ahead, open "Outdoot String Light Switch", window 2026-10-15 17:50–18:30 local. Expected (spec success criteria): one device lane "Outdoor String Lights Button" with 8 presses, 5 ringed; Run gives five `fired` runs at the short presses, trigger platform `device`, none at the double presses. Drag, add long, change double→short, undo, save, reload: as in Task 5 and 6.
- [x] **Step 3:** Record the outcome on #48 and in this plan; close #48.
