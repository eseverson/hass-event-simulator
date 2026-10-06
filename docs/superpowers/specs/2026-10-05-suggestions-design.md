# Suggestions — design

Second half of the SiYuan idea note ("view tracked state of sensor and automatically generate automation triggers/conditions"). Requested by the user on 2026-10-05, to be built unattended like v1:

> select a period in the history as guidance and the integration would have selections for when an event starts/ends/etc. integration should peek outside the selected area for other hints at what would work. user should also be able to select periods in which the integration should not trigger.

## What the user does

1. Switch the panel to **Suggest** mode. Pick the entities to learn from (tracks, as in replay) and a context window, which can be longer than a replay window (up to 30 days) because patterns need repetition.
2. Brush periods on the timeline and label each one:
   - **Should fire** — with an anchor: *at start*, *at end*, or *at start and end* (two triggers with trigger ids `start`/`end`, the classic on/off pair). Optionally *while* — "conditions should hold throughout this period" — for automations that should only act inside such periods.
   - **Must not fire** — any fire inside the period is a hard failure.
   One labeled period is enough to start; more make the suggestions better.
3. Press Suggest (or let it auto-run, as replay does). Get a ranked list of suggestions. Each shows the trigger + conditions as YAML, how it did on the labels (hits / misses / fires inside must-not periods), and every time it would fire *outside* the labeled periods.
4. Act on what it found outside: each outside fire can be quick-labeled "yes, this too" (becomes a should-fire period) or "no, not here" (becomes a must-not period), and suggestions recompute. This is the "peek outside" loop: unlabeled history is evidence, and the user turns it into labels with one click.
5. **Open in simulator** loads a suggestion (with a placeholder action the user replaces) into replay mode on the same window, with the labels shown as an overlay row.

## What "peek outside" means

- **Unlabeled time is soft evidence.** A candidate that fires often where nothing is labeled is penalized (less than inside a must-not period), and those fires are listed for the user to label.
- **The whole context window informs candidate shape.** Thresholds are chosen from the value distribution over the full window, not just the labeled periods; time-of-day vs sun-relative timing is decided by which explains the labeled anchors with less spread across days; recurrence elsewhere (the same transition happening on other days at the same time) is shown as "looks like the same event" hints, ranked by similarity to the labeled ones.
- **Neighbor entities (later, opt-in).** Entities in the same area/device as the tracked ones can be scanned for better triggers; capped and clearly marked. Not in the first cut.

## Architecture

Two stages, so the search is fast and the answer is exact.

1. **Miner (live side, pure Python, `suggest/`).** Input: timeline (`initial_states` + `events`, the same shape replay uses), labels, core config (time zone, location for sun), options (tolerance, max conditions). No HA objects; sun positions through an injected function (the live side passes one built on HA's `astral` helpers) so it is unit-testable.
   - **Step functions**: each entity's history as a sorted list of (time, state, attributes) with O(log n) "state at t".
   - **Anchors**: each should-fire label yields anchor times (start and/or end). A fire *matches* an anchor if it is within the tolerance (default: 2 min before to 10 min after; user-adjustable).
   - **Trigger candidates** generated around anchors, then evaluated over the whole window by a fast approximate evaluator:
     - `state` — the transitions (from → to) on each entity near anchors; `for:` inferred when the transition consistently precedes the anchor by about the same duration; attribute triggers when the state is static but an attribute changes.
     - `numeric_state` — `above`/`below` crossings near anchors; the threshold is searched over observed values to maximize anchor hits and minimize fires elsewhere; `for:` as above.
     - `time` — when anchors cluster by clock time.
     - `sun` — `sunrise`/`sunset` with offset, when anchors cluster by sun event better than by clock.
   - **Conditions** (to remove fires outside / inside must-not): `state` and `numeric_state` at fire time, `time` window (`after`/`before`), weekday, `sun` before/after. Greedy: start from each trigger, add the condition that removes the most bad fires while keeping all (or the most) anchor hits, up to N (default 2) conditions.
   - **Score**: anchor recall first (all anchors hit beats most), then fires inside must-not periods (hard), then fires in unlabeled time (soft), then simplicity (fewer conditions, round thresholds, no `for:`).
   - **Output**: top-K candidates as automation configs (`triggers`/`conditions` + placeholder action `logbook.log`-style no-op the user replaces), with approximate stats.
   - Also produces **hints**: the outside fires of the top candidates and recurrences of the labeled transition elsewhere, each with a similarity score.

2. **Exact validation (sandbox).** The top-K candidates are replayed through the existing sandbox in **one child**: the scenario gains `automations: [...]` (many independent automations; actions are record-only so they cannot interact). The validated fire times replace the approximate ones, so the stats the user sees are what HA itself would do. Candidates whose exact behavior diverges badly from the approximation are demoted. Only the entities a candidate references are sent, which keeps 30-day windows inside the event caps.

`event_simulator/suggest/subscribe` (admin): `{entity_ids, start, end, labels: [{start, end, kind: "fire"|"no_fire", anchor: "start"|"end"|"both"|"while"}], options}` → progress messages (`mining`, `validating`, counts) then `{suggestions: [{config, stats: {anchors_hit, anchors_total, missed: [...], no_fire_violations: [...], outside_fires: [...]}, explanation}], hints: [...]}`. Cancelled the same way replay subscriptions are (newest wins; unsubscribing kills the child).

Labels persist in saved scenarios (store schema bump, migrated).

## Panel

Mode switch Replay / Suggest sharing the same timeline. Suggest mode adds: brush-to-label on the time axis (drag across the axis or any track), a labels row (green should-fire with start/end anchor handles, red must-not), a label editor (anchor choice, delete), a suggestions list (rank, plain-language explanation, YAML, stats, copy YAML, Open in simulator), and outside-fire markers on a hints row with "yes, this too" / "no, not here" actions. Auto-suggest after label changes with the same debounce/newest-wins machinery as auto-run. The panel file is already large: split suggest UI into its own ES module served from the panel dir if registration allows; otherwise keep pure logic in importable modules.

## Non-goals (this cut)

Actions (the user writes them), template triggers, multi-entity "and" triggers, learning from logbook manual actions (the Danm72 approach), neighbor-entity scanning, ML models. Deterministic search only — every suggestion is explainable by the stats shown.

## Testing

- Miner: synthetic timelines with a known generating rule (door opens after sunset → light on; humidity > 70 for 5 min → fan on; weekday 07:00) — the miner must recover the rule (or an equivalent) from a few labels, and avoid must-not periods. Threshold search, `for:` inference, clock vs sun choice, greedy conditions, scoring order: unit tests.
- Engine: multi-automation scenarios replay each automation independently; results attribute runs per automation.
- Websocket: suggest subscription end to end with the recorder fixture; cancellation kills the child.
- Real-data evaluation (read-only copy of the user's recorder, as in the v1 sweep): for existing automations with live traces, use their actual fire times as should-fire labels and check that a suggestion reproduces them (report recall/extra-fire rates per automation; this is the benchmark to improve against).
- Panel: pure logic (label edits, brush math, hint actions) under `node --test`; headless smoke against a disposable HA.
