"""Exact validation, the pure half: the sandbox's runs back into fires, and a `suggest` result re-scored, re-ranked and re-hinted on them.

The live side (websocket.py) replays the top suggestions in one sandbox child (`automations: [...]`, `traces: "none"`) and hands the runs here. Exact fires go through the same scoring as the miner's (`score.score_fires`), so the stats the user sees are what Home Assistant itself would do. A suggestion whose exact fires disagree badly with the miner's (`divergence` above `DIVERGED`) is flagged and ranked after the ones that agree: the miner's search chose it for fires it does not have.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from .evaluate import Fire
from .score import Stats, rank_key, score_fires
from .search import Suggestion, SuggestResult, _hints, explain, tie_break
from .timeline import to_ts

# Run outcomes that are not a fire: the trigger fired but a condition stopped the run, or (HA 2026.9+) the trigger evaluated a change and did not fire. Every other outcome (fired, dropped by the mode, aborted, ...) passed the conditions.
NOT_FIRES = frozenset({"condition_failed", "not_triggered"})
# An exact fire this close to an approximate one is the same fire (time triggers run 50-500 ms late in Home Assistant).
MATCH_WINDOW = 60.0
# Share of fires (approximate and exact together) left unmatched above which a suggestion counts as diverged.
DIVERGED = 0.5


def fires_from_runs(runs: Iterable[Mapping[str, Any]], count: int) -> list[list[Fire]]:
    """Each automation's fires, from the runs of a multi-automation replay (each run carries `automation`, `started`, `outcome`, `trigger.idx`)."""
    fires: list[list[Fire]] = [[] for _ in range(count)]
    for run in runs:
        index = run.get("automation")
        if not isinstance(index, int) or not 0 <= index < count:
            continue
        if run.get("outcome") in NOT_FIRES or not run.get("started"):
            continue
        trigger = run.get("trigger") or {}
        try:
            trigger_index = int(trigger.get("idx"))
        except (TypeError, ValueError):
            trigger_index = 0
        fires[index].append(Fire(to_ts(run["started"]), trigger_index))
    for found in fires:
        found.sort()
    return fires


def divergence(approx: Sequence[float], exact: Sequence[float], window: float = MATCH_WINDOW) -> float:
    """Share of all fires (both lists) that have no partner within `window` in the other list: 0 when they agree, 1 when nothing matches."""
    total = len(approx) + len(exact)
    if not total:
        return 0.0
    matched = 0
    i = j = 0
    while i < len(approx) and j < len(exact):
        if abs(approx[i] - exact[j]) <= window:
            matched += 1
            i += 1
            j += 1
        elif approx[i] < exact[j]:
            i += 1
        else:
            j += 1
    return (total - 2 * matched) / total


def _summary(stats: Stats) -> tuple[Any, ...]:
    """What the user reads off the stats; exact stats that agree with the miner's on all of it need no `approx_stats`."""
    return (
        stats.fires,
        stats.anchors_hit,
        stats.missed,
        len(stats.no_fire_violations),
        len(stats.outside_fires),
        len(stats.inside_fires),
        stats.while_held,
    )


def mark_approximate(result: SuggestResult, warning: dict[str, str]) -> SuggestResult:
    """The miner's result as is, every suggestion `exact: false`, with the reason validation did not happen."""
    suggestions = [replace(s, exact=False) for s in result.suggestions]
    return replace(result, suggestions=suggestions, warning=warning)


def apply_exact(
    result: SuggestResult,
    exact: Sequence[Sequence[Fire] | None],
    *,
    warning: dict[str, str] | None = None,
) -> SuggestResult:
    """`result` with each suggestion re-scored on its exact fires (`exact[n]` for `result.suggestions[n]`; None where the sandbox could not run it), re-ranked, and hints recomputed.

    Order: suggestions that validated and agree with the miner, then those that diverged, then any the sandbox could not run (kept with the miner's stats, `exact: false`). Within each group, `score.rank_key` on the exact stats, then the miner's tie-breaks.
    """
    context = result.context
    if context is None or not result.suggestions:
        return result if warning is None else replace(result, warning=warning)
    search = context.search
    labels = search.labels
    ranked: list[tuple[tuple[Any, ...], Suggestion]] = []
    for n, suggestion in enumerate(result.suggestions):
        candidate = suggestion.candidate
        fires = exact[n] if n < len(exact) else None
        if fires is None:
            key = (2, n)
            ranked.append((key, replace(suggestion, exact=False)))
            continue
        fires = [f if 0 <= f.trigger < len(candidate.triggers) else Fire(f.at, 0) for f in fires]
        stats = score_fires(search.ev, candidate, labels, fires)
        diverged = divergence(suggestion.stats.fire_times, stats.fire_times) > DIVERGED
        key = (
            1 if diverged else 0,
            *rank_key(stats, candidate),
            *tie_break(stats, candidate, labels.anchors, fires),
        )
        explanation = explain(candidate, stats, context.anchor_words)
        ranked.append(
            (
                key,
                replace(
                    suggestion,
                    stats=stats,
                    explanation=explanation,
                    config={**suggestion.config, "description": explanation},
                    exact=True,
                    approx_stats=None if _summary(stats) == _summary(suggestion.stats) else suggestion.stats,
                    diverged=diverged,
                ),
            )
        )
    ranked.sort(key=lambda item: item[0])
    suggestions = [replace(suggestion, rank=rank) for rank, (_, suggestion) in enumerate(ranked)]
    hints = _hints(search, labels.anchors, suggestions)
    return replace(result, suggestions=suggestions, hints=hints, warning=warning)
