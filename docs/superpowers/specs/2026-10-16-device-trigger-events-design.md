# Device-trigger and bus-event replay and editing — design

Decisions made with the user on 2026-10-16, prompted by a ZHA button (Tuya TS0041 `_TZ3000_fa9mlvja`, "Outdoor String Lights Button") whose automation ("Outdoot String Light Switch", `trigger: device, domain: zha, type: remote_button_short_press, subtype: button_1`) cannot be replayed.

## Why

Buttons and remotes are event-based: a press is a bus event (`zha_event`, `hue_event`, `deconz_event`, ...), not a state, so nothing about it shows on a state track. The simulator already records and replays bus events for plain `trigger: event` automations, but:

1. **Device triggers of integration-specific platforms are refused.** `_check_platforms` (`sandbox/engine.py`) raises `unsupported_automation` for any device trigger whose integration is not an entity domain, because those platforms read their integration's runtime state, which the sandbox does not have. Reproduced: the user's automation is refused; the same automation rewritten as a plain event trigger on `zha_event` with `{device_ieee, endpoint_id: 1, command: remote_button_short_press}` replays and fires on the short press only.
2. **The panel never sends the events a device trigger needs.** `eventTriggerTypes` (panel) only collects types from `trigger: event`, so `zha_event` is neither drawn nor put in the scenario's `bus_events`.
3. **Bus events cannot be edited.** The Events lane is read-only, and bus events are not part of undo, reset or saved scenarios. The user wants to move recorded presses and add new ones as what-ifs, as they can with state changes.

## How a ZHA device trigger works (verified)

- A press reaches HA as `zha_event` with `device_ieee`, `device_id`, `unique_id`, `endpoint_id`, `cluster_id`, `command`, `args`, `params` (`zha/helpers.py`, `ZHADeviceProxy` event handler). The quirk's `device_automation_triggers` maps `(type, subtype)` to the event data that identifies it, e.g. `(remote_button_short_press, button_1) → {endpoint_id: 1, command: remote_button_short_press}`.
- `zha/device_trigger.py` `async_attach_trigger` looks up `(ieee, triggers)` with `_get_device_trigger_data(hass, device_id)` — the live device proxy if the gateway has it, else `get_zha_data(hass).device_trigger_cache[device_id]` — and attaches core's event trigger on `zha_event` with `{device_ieee: ieee, **triggers[(type, subtype)]}`.
- `async_validate_trigger_config` skips dynamic validation when the device's ZHA config entry is not loaded (the sandbox's entries are disabled stubs), so validation passes from the registry snapshot alone.
- Spike (HA 2026.2.3, `zha==0.0.90`): in a bare `HomeAssistant` with no ZHA setup and no gateway, given the device registry entry (with its `("zha", ieee)` identifier and a ZHA config entry) and one `device_trigger_cache` entry, ZHA's unmodified `async_attach_trigger` fell back to the cache and attached; firing short, double, `attribute_updated`, short triggered exactly on the two shorts. `async_get_triggers` listed all three types. Importing `homeassistant.components.zha.device_trigger` took about 1.2 s.

## Goals

- An automation with ZHA device triggers replays: recorded presses fire it as they did live, through ZHA's own device-trigger code running in the sandbox.
- A generic mechanism for integration-specific device triggers: a small per-integration **adapter**; ZHA is the first and only one built now. Nothing outside the adapter knows about ZHA.
- A device lane per device a device trigger names (layout A, chosen by the user): every recorded event of that device that matches one of its trigger kinds, labeled with HA's own trigger names; marks for kinds this automation's triggers use are ringed, the rest dimmed.
- Plain `trigger: event` automations get the same editing on their event lanes.
- Bus events are editable like state changes: drag to move, shift a whole lane, click to edit or delete, add, with undo, per-lane reset, reset all, and saved in scenarios.

## Non-goals

- Adapters for other integrations (Hue, deCONZ, Shelly, Z-Wave JS, ...). The adapter interface must make them possible; building them waits until the user has such a device.
- Device conditions and device actions of integration-specific platforms: still refused.
- Suggest mode changes (suggesting device triggers from presses).
- Event entities (`event.*`): they are states and already replay as states.
- Integrations whose device triggers do not reduce to a bus event (mqtt, knx, homekit_controller, TV integrations): they keep a clear refusal.

## Anti-goals (a failure even if it works)

- **No code in the live process that alters behavior.** No monkeypatching of core or integration code in the live instance, and no stand-in `hass` handed to integration code there. The user explicitly rejected both. The live side may only *read* data an integration already keeps.
- **No ZHA knowledge outside the ZHA adapter.**
- **No guessing a filter.** If an adapter cannot produce a device's trigger data, the replay refuses with a message naming the device and why, rather than approximating.

## Design

### 1. Device-trigger adapters

A module per integration under `custom_components/event_simulator/` (e.g. `device_triggers/zha.py`, with a registry keyed by domain). Each adapter has two halves:

- **Live half (read-only):** `export(hass, device_ids) -> {device_id: payload}` reads what the integration already holds and returns plain JSON, plus `kinds(payload) -> [{type, subtype, event_type, match}]` describing, for the panel, which bus events each `(type, subtype)` corresponds to (`match` is the event data subset an event must contain).
- **Sandbox half:** `seed(hass, payloads)` puts the payloads where the integration's own unmodified `device_trigger` module looks for them, before the automation is set up.

**ZHA adapter.**
- Live: for each device id, the result of ZHA's `_get_device_trigger_data(hass, device_id)` (the same lookup ZHA's trigger uses: live device proxy, else the trigger cache), as `{ieee, triggers: [{type, subtype, data}]}`. This is a private ZHA function; a test pins it, and a missing or changed function makes the adapter report "ZHA's trigger data is not readable on this Home Assistant version" instead of failing obscurely.
- Kinds: `event_type: "zha_event"`, `match: {device_ieee: ieee, **data}`.
- Sandbox: `get_zha_data(hass).device_trigger_cache[device_id] = (ieee, {(type, subtype): data})`. The gateway is absent, so ZHA's lookup raises `ValueError` and falls back to the cache, as in the spike.

### 2. Scenario and sandbox

- New scenario key `device_triggers: {domain: {device_id: payload}}`, built by the live side for the device triggers in the automation config (all automations, for multi-automation scenarios). Documented in the scenario-format section of the main design doc and the engine docstring.
- `_check_platforms`: an integration-specific device **trigger** is allowed when an adapter exists for its domain and `device_triggers[domain]` has its device; otherwise it is refused as today, with the reason (no adapter for this integration / the integration had no trigger data for this device). Device conditions and actions are unchanged.
- The engine calls each adapter's `seed` after the registries load and before automation setup. The integration's own `device_trigger` module then attaches the real event trigger, so the run's `trigger` variable is what live HA gives (`platform: device`, the real event).
- The sandbox child runs on the live interpreter, so the integration's library (`zha`, `zigpy`, ...) is importable whenever the live instance uses it.

### 3. Which bus events a scenario carries

- The live side returns, with the automation (`event_simulator/automation`), the device lanes it needs: for each device a device trigger references, the device's name and the adapter's kinds (with the `(type, subtype)` each automation trigger uses flagged). Integration-specific device triggers without an adapter are listed too, so the panel can say why they have no lane.
- The panel collects the event types it needs from both plain event triggers and device lanes (replacing `eventTriggerTypes` as the source), and requests bus events for those types (`event_simulator/bus_events` already accepts `event_types`), so a busy unrelated type cannot crowd them out of the 20,000-event cap.
- A device lane holds the recorded events of its kinds' event type that match one of its kinds (subset match, as core's event trigger does). Events of that type matching no kind (the TS0041's 15-minute `attribute_updated`, the `press_type` echo) are not on the device lane and are not sent for it.
- An event lane for a plain event trigger holds every recorded event of its type, one lane per event type. Marks the trigger's `event_data` matches are ringed, the rest dimmed; when `event_data` is templated or has nested dicts the panel cannot evaluate, nothing is dimmed.
- Each event belongs to exactly one lane, so it is fired once and every edit, shift and reset has one owner: an event matching a device lane's kind is on that device lane; the plain event lane of the same type (an automation with both a device trigger and a plain `zha_event` trigger) holds only the events no device lane owns. The plain trigger still sees device-lane events in the replay, since both are fired on the same bus.
- When a needed type is over the 20,000-event cap, `history` drops it whole (`dropped_event_types`) and its lanes are empty. The lane then says so ("zha_event: too many events in this window to load; shorten the window") instead of looking like no presses happened. The user's instance logs about 570 `zha_event`s a week, well under the cap; filtering a device's events in the recorder query is a follow-up, not part of this work.

### 4. Panel: lanes and editing

- **Device lane** (layout A): header with the device's name, integration and counts ("8 presses · 5 trigger this automation"). One mark per event, labeled with HA's translated trigger name (`component.<domain>.device_automation.trigger_type.<type>` and `...trigger_subtype.<subtype>`, the strings the automation editor shows; fall back to the raw type/subtype). Marks of kinds this automation's triggers use are ringed; others dimmed. Kinds are told apart by a per-kind color within the lane.
- **Event lane**: one per event type, marks as today plus ringing/dimming above.
- **Editing**, for both lane types, mirroring state tracks:
  - Drag a mark to move it in time. Drag the lane (or Shift-drag) to shift the whole lane, with the offset chip and Unshift, and the same parking of events shifted outside the window.
  - Click a mark: an editor shows its time and data. On a device lane, the kind can be changed (a list of the device's kinds); on an event lane, the data is edited as JSON. Delete is there too.
  - Add (double-click / double-tap on the lane, as on tracks): on a device lane, pick a kind; on an event lane, the new event copies the data of the nearest event in the lane, editable before it is added.
  - Data of an added or kind-changed device event: the data of the nearest recorded event of that kind on the lane when there is one (so `args`, `cluster_id`, `unique_id` look real), else the kind's `match` alone; the editor says which.
  - Every edit is marked `edited`, goes through the same undo history (`_commit`), counts for Reset (per lane) and Reset all, and marks results stale for Auto-run.
- **Saved scenarios** store the lanes' bus events with their `edited` marks and lane shifts (store schema, `scenarioPayload`, `tracksFromScenario` equivalents), and a loaded scenario adopts recorded events as `adoptRecorded` does for states. A scenario saved before this change has no bus events and loads them from the recorder as today.

### 5. Errors and messages

- No adapter for a device trigger's integration: the existing refusal, reworded to say device triggers from that integration are not supported yet.
- Adapter has no data for the device (device removed, ZHA not loaded on the live side): refusal naming the device.
- ZHA private lookup missing or changed: refusal saying ZHA's trigger data could not be read on this HA version.

## Success criteria

- The user's automation, over the window containing 10/15 17:57–18:24, replays five runs at the five recorded short presses, with `trigger.platform == "device"`; the double presses do not fire it. (The automation's action is currently disabled, so each run fires with no service call.)
- Dragging a short press moves its run; adding a long press adds no run; changing a double press to a short press adds a run; undo restores each step; a saved and reloaded scenario keeps all of it.
- A `trigger: event` automation on `tag_scanned` (or a synthetic type in tests) gets the same editing.
- No live-side code path calls into integration code other than the adapter's read.

## Left to the builder

- Module layout and the adapter interface's exact names, within the halves above.
- How kinds are colored in a lane, how dimmed marks look, and the editor's layout, as long as they match the panel's existing look.
- Test structure: the ZHA adapter's sandbox half needs ZHA's Python package; the dev venv does not have it (the spike installed `zha==0.0.90` and a few transitive packages into a scratch dir). Whether tests install it as a test requirement or skip when it is absent is the builder's call, but a test must run it somewhere (CI included).
- Whether `event_simulator/automation` or a separate command returns the device lanes.

## Calls made by Claude (not discussed with the user)

- Adapters are triggers-only; device conditions and actions of integration-specific platforms stay refused.
- The ZHA adapter reads through ZHA's private `_get_device_trigger_data` rather than reimplementing its lookup, so live and sandbox agree by construction.
- Device lanes drop events of the type that match none of the device's kinds.
- Added events copy the nearest recorded event's data.
- Plain event lanes are split per event type (today's Events lane mixes types).
- One owner per event: device lanes take precedence over a plain event lane of the same type.
- A type over the bus-event cap leaves its lanes empty with a notice; per-device filtering in the recorder query is deferred.
