# Event Simulator for Home Assistant

[![Release](https://img.shields.io/github/v/release/eseverson/hass-event-simulator?style=flat-square)](https://github.com/eseverson/hass-event-simulator/releases)
[![License](https://img.shields.io/github/license/eseverson/hass-event-simulator?style=flat-square)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square)](https://hacs.xyz)
[![Validate](https://img.shields.io/github/actions/workflow/status/eseverson/hass-event-simulator/validate.yml?branch=main&label=validate&style=flat-square)](https://github.com/eseverson/hass-event-simulator/actions/workflows/validate.yml)
[![Tests](https://img.shields.io/github/actions/workflow/status/eseverson/hass-event-simulator/test.yml?branch=main&label=tests&style=flat-square)](https://github.com/eseverson/hass-event-simulator/actions/workflows/test.yml)

Replay recorded history against one automation, in a sandbox, and see what it would have done.

> **Event Simulator is not affiliated with, endorsed, recommended, or supported by the Home Assistant project.**
>
> This custom integration is provided as-is, without any warranty. A replay is a model of what your automation does, not a guarantee: it runs Home Assistant's own trigger, condition and script code, but against recorded states, on a virtual clock, with every service call faked.

Testing an automation usually means waiting for the real event, or flipping devices by hand and reading traces afterwards. This lets you pick an automation, pull the recorded history of the entities it depends on onto a timeline, change that timeline, and replay it. For each window you get:

- whether the automation fired, and when;
- which trigger started each run;
- which condition stopped a run that did not get through, or whether the run was dropped by its `mode`;
- which service calls it would have made, with their data and target.

Nothing it does touches the live instance: the replay runs in a separate process, and its service calls are recorded, not made.

![The Event Simulator panel](docs/panel.png)

## Install

Through HACS, add this repository as a custom repository with category **Integration**, then install it. Or copy `custom_components/event_simulator/` into your config directory yourself.

Either way:

1. Restart Home Assistant.
2. Settings → Devices & services → Add integration → **Event Simulator**.

The **Event Simulator** panel appears in the sidebar for admin users. It needs Home Assistant 2026.2.3 or newer and the recorder, which is where the history comes from; there is nothing else to install and no network access is needed.

## Using it

**Choose an automation.** The panel lists every automation entity. Picking one loads its config and gives a track to each entity its triggers, conditions and templates reference.

**Choose a window.** The default is the last 24 hours; presets and the start/end fields set any window up to 7 days. Each track is filled from the recorder: the state at the window start, then every recorded change inside it. Numeric entities draw as a line, everything else as state segments; `unavailable` and `unknown` are hatched, and replayed as the real states they are.

**Pan and zoom.** Every track shares one time axis. Pan with the scroll bar under the axis (drag its thumb, or click beside it to page), the horizontal wheel or Shift + wheel; zoom with the − / + / **Fit** buttons, Ctrl/⌘ + wheel, or the wheel over the axis. Dragging on the timeline itself never pans.

**Edit the timeline** for a what-if. Two drags, told apart by where they start:

- **Drag a track** (anywhere on it but a change mark) to shift that entity's whole history in time, so its changes line up differently against the other tracks'. The offset shows while you drag (`+3 min 20 s`) and stays on the track as a chip afterwards, with **Unshift** to put it back. The state in effect at the window start moves with it: shifted earlier, the last change pushed before the start becomes the initial state. Changes shifted outside the window are left out of the replay but kept, and come back when you shift the track back. Hold Shift to shift the track even when the drag starts on a mark.
- **Drag a change mark** to move just that change (it snaps, and never passes its neighbors). On a touch screen only the selected change has a handle: tap a change, then drag its handle; a swipe anywhere else on the track shifts the whole track.

Click a mark (or anywhere on a track) to edit the state and attributes in effect there, add changes, or delete them. Add a track for any other entity by its id, or remove one. Every edit, a track shift included, is marked, undoable and saved with a scenario; **Undo**, **Reset** per track and **Reset all** take you back to the recorded history.

**Edit the automation config** in the YAML editor under the timeline to try a different trigger, condition or action. The edit only changes what the simulator runs. It is never saved to Home Assistant, and **Revert to saved automation** puts the real config back. As you type, lines that differ from the saved automation are marked in the editor (a bar beside added and changed lines, a red marker where lines were removed), and a summary under it opens a compact diff. The saved config is dumped the same way the editor shows it, and a config that parses to the saved one marks nothing, so reordered keys or added comments alone are not flagged. `!include`, `!secret` and other custom YAML tags are refused.

**Results follow your edits.** With **Auto-run** on (the default), the panel replays the window by itself whenever what it depends on changes: picking an automation, changing the window, adding or removing a track, any timeline edit (on drop for a drag), undo and reset, an automation config edit once it parses, loading a scenario. It waits for half a second of quiet first, so a burst of edits replays once, and for any history still loading; the last results stay on screen, dimmed, until the new ones arrive, and a newer replay abandons (and stops) the one in flight. After a replay of a large timeline took more than 3 seconds, it waits about that long instead. Turn Auto-run off (the choice is remembered in this browser) to replay only on **Run now**, which also re-runs on demand when nothing changed.

Runs land on a results row under the shared time axis, each with its own marker shape and color: fired, stopped by a condition, aborted inside the actions, cancelled (by a `restart` re-trigger), dropped by the automation's `mode` (the legend says which: `single`, `max_runs`, or recursive), still running after the overrun, or errored. On Home Assistant 2026.9 and newer, a trigger that looked at a change and decided not to fire (a non-numeric reading for a threshold, say) shows as **not triggered**, with the reason in the run details. The legend above the row counts each. Selecting a run shows when it started and how long it took, the trigger, each condition's result, the step that stopped it, any error, and the service calls it recorded. Warnings from the replay (an event outside the window, a call to a service your instance does not have, a run still going when the replay ended) are listed with the results.

**Suggest mode (labeling).** Switch the mode from **Replay** to **Suggest** to label history instead of replaying it. Suggest mode keeps the same timeline, tracks and window, and the window can be longer, up to 30 days, because patterns need repetition (a replay is still limited to 7 days). You don't need an automation: add the entities to learn from as tracks, or pick an automation to add the entities it references. To label a period, drag across the **Labels** row or the time axis (on a touch screen, long-press first, then drag). Then say what the period is:

- **Should fire** at its start, at its end, at both (the classic on/off pair), or **while** (conditions should hold throughout). Should-fire periods are drawn green, with a tick at each anchor.
- **Must not fire**: any fire inside it is a failure. These periods are drawn red and hatched.

The panel remembers the kind you picked last and offers it first next time. Labels also show faintly across every track. Click a period to change its kind or anchor, or to type its exact start and end. Drag its edges to resize it, or its body to move it. **Delete** removes the selected period, and **Undo** brings it back. Overlapping periods of the same kind merge into one. A should-fire period cannot overlap a must-not-fire one. **Tolerance** sets how far a fire may land from an anchor and still count: 2 minutes before to 10 minutes after by default. Labels and tolerance are saved with a scenario.

**Suggestions.** With at least one should-fire period, the panel asks for suggestions by itself after each change to the labels, tracks, window or tolerance and after loading a scenario (**Auto-suggest**, debounced like Auto-run, newest request wins; turn it off to ask only on **Suggest now**). The status line shows each stage: loading history, searching for triggers and conditions, checking the best ones in Home Assistant's sandbox. Each suggestion is a card, best first:

- a sentence saying what it does, then how it did on your labels: anchors hit (each missed anchor is a link that jumps the timeline there), fires inside must-not periods in red (also links), and fires in unlabeled time;
- **checked in HA's sandbox** when those numbers come from replaying it in Home Assistant itself, **estimated** when they come from the fast search only;
- its triggers and conditions as YAML, the full automation under **Full automation YAML**, and **Copy YAML**;
- **Open in simulator**, which switches to Replay on the same window and tracks with the suggestion loaded as the automation config (as an edit of the automation the tracks came from, if any). Its action is a placeholder `logbook.log`: replace it with yours. The labels stay on their row as an overlay.

The selected suggestion's fires show on a **Fires** row: green on an anchor, red inside a must-not period, grey in unlabeled time, a dashed ring where an anchor was missed. A **Hints** row marks where the top suggestions fire outside your labels (circles) and where the labeled change happens again elsewhere (diamonds), larger and more solid the more alike they look. Click one for **Yes, this too** (labels a should-fire period there with your usual anchor) or **No, not here** (labels a must-not-fire period around it); either asks for suggestions again.

**Save** the setup as a named scenario to come back to it: the automation config (edited or not) and the automation it came from, the window, the tracked entities and the timeline, edits included. Scenarios are kept in `.storage/event_simulator.scenarios`, up to 100 of them. Loading one puts it back on screen as saved; it does not re-read the recorder, so a scenario still replays after the history behind it has been purged.

## Suggest mode

Switch the panel to **Suggest** mode to have the simulator analyze your history and propose automation triggers and conditions. See the [Suggest design spec](docs/superpowers/specs/2026-10-05-suggestions-design.md) for details.

**How it works.** Label periods in the history as should-fire or must-not-fire, then the simulator searches for trigger + condition combinations that match your labels. It generates candidates from state transitions, numeric thresholds, time-of-day, sun events and recorded bus events (a ZHA remote's `zha_event`, a `tag_scanned`, a phone's notification action: the event type plus the `event_data` keys that single out the labeled ones), then adds conditions (greedily) to remove unwanted fires. Top candidates are validated by replaying them in the sandbox so the stats are exact.

**Understanding suggestion stats.** Each suggestion shows:

- **anchors hit / total** — how many should-fire anchor times the automation fires within the tolerance window (default: 2 min before to 10 min after). Missed anchors link to the time so you can see the gap.
- **no-fire violations** — fires inside labeled must-not-fire periods. Each is linked.
- **outside fires** — fires in unlabeled time. If the candidate fires often where nothing is labeled, it is penalized.

Click **Open in simulator** to load the suggestion into Replay mode on the same window and tracks, with your labels shown as an overlay row, so you can verify it by hand.

**Limits.** Suggest mode is a deterministic search — no ML models, no randomness. Every suggestion is explainable from the stats shown. It works within the following constraints:

- **No actions.** The simulator generates only triggers and conditions; the action is a placeholder (`logbook.log`) that you replace. Template triggers are not generated ([#38](https://github.com/eseverson/hass-event-simulator/issues/38)), and neither are calendar triggers with an offset ([#37](https://github.com/eseverson/hass-event-simulator/issues/37)); a calendar event starting or ending at the labeled moment comes back as the calendar entity turning on or off ("an event of calendar.x starts").
- **No multi-entity "and" triggers.** Each candidate uses a single-entity trigger. Conditions across multiple entities are supported.
- **Supported trigger platforms:** `state`, `numeric_state`, `time`, `sun`, `event` (event types the recorder stores, except Home Assistant's own bookkeeping such as `automation_triggered`, `call_service` or `*_registry_updated`; the panel shows them on an **Events** row, and up to 20,000 a window are read, the busiest types left out first).
- **Supported condition platforms:** `state`, `numeric_state`, `time` (after/before, weekday), `sun` (before/after).
- **Window:** up to 30 days (vs 7 for replay mode), because patterns need repetition.
- **Entities:** same 50-track limit as replay mode.

## How it works

Each replay starts a child process with the same Python interpreter Home Assistant runs on, importing the same `homeassistant` package, so triggers, conditions, templates and script steps behave as they do live, version for version. The child boots a throwaway Home Assistant in a temporary config directory with no recorder, HTTP server, frontend or network-facing integrations, sets up only the automation under test, and deletes the directory when it is done. It gets a minimal environment, so tokens and credentials in the live process's environment do not reach it. It copies the live instance's time zone, location, elevation and unit system, any Labs preview features you have turned on, and a slice of the entity, device, area, floor and label registries — the entries the automation and its tracks can see — so `area_entities()`, `device_attr()` and similar templates, and device triggers of entity-domain integrations such as `binary_sensor` and `light`, resolve as they do live.

Time in the child is virtual. Its event loop never sleeps: when nothing is ready to run it jumps straight to the next timer or timeline change, so a `for:` of an hour, a `delay`, a `time` trigger and a `sun` condition all see the time they would have seen, and a day replays in seconds. The initial states keep their recorded `last_changed`, so a `for:` early in the window counts from when the state really changed.

The replay attaches the automation at the window start, writes each timeline change at its time, and intercepts every service call at Home Assistant's service registry: the call is recorded with its time, data, target and the run that made it, answered with success, and never executed. The window is half-open, so a run triggered exactly at the end is discarded. Runs still in progress at the end (in a `delay`, a `wait_for_trigger`, a queue) get up to an hour of virtual time to finish; one still going after that is reported as unfinished. Results come from Home Assistant's own automation traces, and the full trace of each run is in the panel.

## Limitations

**Actions have no effect.** A recorded `light.turn_on` does not turn the light on in the replay, so anything later in the run that waits on the result — a `wait_template` on that light's state, a condition on it — sees the timeline, not the call. A `script.turn_on` or `automation.trigger` is recorded, not run. If the result matters, add the change it would have caused to the timeline yourself.

**One automation at a time.** The automation under test is the only one in the sandbox, so automations that trigger each other cannot be replayed together.

**Only state changes, recorded events and time drive a replay.** The timeline holds state changes, and an automation with `event` triggers also gets the recorded events of the types it listens to, fired at their times (shown on the **Events** row; a tag scan is the `tag_scanned` event). Events the recorder does not store (it skips none by default, but `recorder: exclude: event_types` can) and triggers that wait for something else — a webhook, an MQTT message, a calendar — have nothing to fire on, and a `homeassistant: start` trigger does not fire because the automation is attached after the sandbox has started.

**Some trigger and condition platforms cannot run.** Custom integrations are never loaded in the sandbox, so a trigger or condition from one is refused, as are device triggers, conditions and actions specific to an integration such as ZHA, MQTT or Hue (they need that integration's runtime state, not just its code). The error names each one by its place in the config. Device automations of entity domains (`binary_sensor`, `light`, `sensor`, ...) work. Whether custom platforms should ever be loaded is an open question, [#14](https://github.com/eseverson/hass-event-simulator/issues/14).

**History is what the recorder kept.** Entities excluded from the recorder have no history to load, and nothing older than the recorder's `purge_keep_days` is available. Attributes an integration keeps out of the recorder (an input_datetime's `has_date`, a light's `supported_color_modes`, ...) are filled in from the entity's current state, so a replay sees today's value for them.

**Clock caveats.** Home Assistant reads the time through a few functions the sandbox patches. Code that calls `datetime.now()` directly — some third-party libraries do — sees real time, as do context ids, log timestamps and anything measured with `time.monotonic` in a worker thread. Work handed to Home Assistant's executor takes zero virtual time. None of this affects Home Assistant's own trigger, condition and script paths.

**Size limits:**

| Limit | Value |
| --- | --- |
| Window | 7 days (Suggest mode: 30 days) |
| Tracked entities | 50 |
| Timeline changes | 50,000 (Suggest mode reads up to 200,000) |
| One request to Home Assistant (replays and saves send the timeline packed, so a week of a sensor reporting every 15 s fits) | 4 MB |
| Registry slice sent to the sandbox | 500 entities, 250 devices (the rest is dropped with a warning) |
| Edited automation YAML | 256 KiB, 100,000 nodes |
| Overrun after the window for runs in progress | 1 hour |
| Wall-clock time for one replay | 300 seconds |
| Replays at once | 2 (more wait their turn); one per panel, a newer replay stops the one in flight |
| Saved scenarios | 100, names up to 100 characters, 500,000 timeline changes in all |

## Development

```
uv venv --python 3.13 .venv && . .venv/bin/activate
uv pip install -r tests/requirements.txt
uv pip install "home-assistant-frontend==<version from homeassistant/components/frontend/manifest.json>"
pytest
node --test tests/panel/*.test.mjs
```

```
custom_components/event_simulator/
  __init__.py       entry setup, panel registration
  history.py        automations, recorded timelines, core config and registry snapshot
  runner.py         spawns the sandbox child: timeout, kill, crash surfacing
  store.py          saved scenarios (.storage/event_simulator.scenarios)
  websocket.py      event_simulator/* commands used by the panel
  config_flow.py    single-instance config entry
  sandbox/
    __main__.py     child entry point: scenario JSON on stdin, result JSON on stdout
    engine.py       boots the throwaway Home Assistant and replays the scenario
    vclock.py       virtual-time asyncio event loop and the clock patch
  panel/            the sidebar panel (plain web component, no build step)
```

The scenario and result formats are in [`docs/superpowers/specs/2026-10-05-event-simulator-design.md`](docs/superpowers/specs/2026-10-05-event-simulator-design.md); the replay's timeline (boot lead, seeding, end-of-window handling) is in `sandbox/engine.py`'s module docstring and the clock patch in `sandbox/vclock.py`'s.

Engine tests run scenarios through the real child process; the panel's pure functions are tested with `node --test` without a browser.

## License

[MIT](LICENSE).
