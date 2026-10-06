"""The suggestion miner's core: pure Python, standard library only, so it is fast and unit-testable.

See docs/superpowers/specs/2026-10-05-suggestions-design.md. Times are POSIX seconds (floats) throughout.

- `timeline`: a scenario timeline (`initial_states` + unpacked `events`) as per-entity step functions (`Timeline`, `StepFunction`).
- `labels`: labeled periods, their anchors and the match tolerance (`Labels`, `Label`, `Anchor`, `Tolerance`).
- `model`: candidate automations (`Candidate` of triggers and conditions) and `render_automation` to Home Assistant config.
- `evaluate`: `Evaluator`, the fast approximate fire-time evaluator (its docstring lists where it knowingly differs from Home Assistant).
- `score`: `score_candidate` -> `Stats`, and `rank_key` for the spec's ranking order.
- `search`: `suggest(timeline, labels, time_zone=..., sun_events=..., options=SuggestOptions())` -> `SuggestResult` (ranked `Suggestion`s and `Hint`s): candidate generation around the anchors, threshold search, `for:` inference, greedy conditions, ranking.
- `exact`: exact validation's pure half: the sandbox's runs back into fires (`fires_from_runs`), and a `SuggestResult` re-scored on them with `score.score_fires`, re-ranked and re-hinted (`apply_exact`).

Nothing here imports Home Assistant; the live side passes the time zone and a `sun_events(date)` function built on Home Assistant's astral helpers.
"""
