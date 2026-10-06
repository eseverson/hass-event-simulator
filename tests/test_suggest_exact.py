"""Exact validation, pure half: runs back into fires, divergence, and re-scoring / re-ranking / re-hinting a suggest result on exact fires."""

from __future__ import annotations

import random
import threading
from types import SimpleNamespace

import pytest

from custom_components.event_simulator.suggest.evaluate import Evaluator, Fire
from custom_components.event_simulator.suggest.exact import (
    apply_exact,
    divergence,
    estimated_runs,
    fires_from_runs,
    mark_approximate,
    plan_validation,
)
from custom_components.event_simulator.suggest.labels import Labels
from custom_components.event_simulator.suggest.model import Candidate, StateTrigger
from custom_components.event_simulator.suggest.score import score_candidate, score_fires
from custom_components.event_simulator.suggest.search import SuggestCancelled, suggest
from custom_components.event_simulator.suggest.timeline import to_iso

from .test_suggest_search import DOOR, H, M, PERSON, T0, fire, no_fire, run, timeline


def door_day():
    """The door opens four times; three are labeled, one (12:00) is not."""
    changes = []
    for at in (8 * H, 12 * H, 18 * H, 21 * H):
        changes += [(at, DOOR, "on"), (at + M, DOOR, "off")]
    changes += [(11 * H, PERSON, "not_home"), (14 * H, PERSON, "home")]
    tl = timeline({DOOR: "off", PERSON: "home"}, changes, 1)
    return tl, Labels([fire(8 * H), fire(18 * H), fire(21 * H)])


def test_score_fires_is_what_score_candidate_does_on_the_evaluator_fires():
    tl, labels = door_day()
    ev = Evaluator(tl, time_zone="UTC")
    candidate = Candidate((StateTrigger(DOOR, to=("on",)),))
    fires = ev.fires(candidate)
    stats = score_fires(ev, candidate, labels, fires)
    assert stats == score_candidate(ev, candidate, labels)
    assert stats.fire_times == tuple(T0 + t for t in (8 * H, 12 * H, 18 * H, 21 * H))
    assert stats.as_dict()["fire_times"] == [to_iso(t) for t in stats.fire_times]
    assert (stats.anchors_hit, len(stats.outside_fires)) == (3, 1)


def test_fires_from_runs_keeps_runs_past_the_conditions_per_automation():
    runs = [
        {"automation": 1, "started": to_iso(T0 + 20), "outcome": "fired", "trigger": {"idx": "1", "id": "end"}},
        {"automation": 0, "started": to_iso(T0 + 10), "outcome": "fired", "trigger": {"idx": "0", "id": None}},
        {"automation": 0, "started": to_iso(T0 + 5), "outcome": "dropped", "trigger": {"idx": "0"}},
        {"automation": 0, "started": to_iso(T0 + 30), "outcome": "condition_failed", "trigger": {"idx": "0"}},
        {"automation": 1, "started": to_iso(T0 + 40), "outcome": "not_triggered", "trigger": {"idx": "0"}},
        {"automation": 7, "started": to_iso(T0 + 50), "outcome": "fired", "trigger": {"idx": "0"}},
        {"automation": 1, "started": None, "outcome": "fired", "trigger": {}},
    ]
    assert fires_from_runs(runs, 2) == [[Fire(T0 + 5, 0), Fire(T0 + 10, 0)], [Fire(T0 + 20, 1)]]


def test_divergence():
    assert divergence([], []) == 0
    assert divergence([0, 100, 200], [0.3, 100.4, 200.2]) == 0
    assert divergence([0, 100], []) == 1
    assert divergence([0, 100], [0, 5000]) == 0.5


def test_exact_fires_that_agree_keep_the_ranking_and_say_exact():
    tl, labels = door_day()
    result = run(tl, labels)
    exact = [[Fire(f + 0.2, 0) for f in s.stats.fire_times] for s in result.suggestions]
    # The pair test has no paired triggers, so trigger 0 is right for every suggestion here.
    assert all(len(s.candidate.triggers) == 1 for s in result.suggestions)

    validated = apply_exact(result, exact)

    assert [s.explanation for s in validated.suggestions] == [s.explanation for s in result.suggestions]
    assert all(s.exact is True and s.approx_stats is None and not s.diverged for s in validated.suggestions)
    out = validated.as_dict()["suggestions"][0]
    assert out["exact"] is True and "approx_stats" not in out
    assert out["config"]["description"] == out["explanation"]


def test_exact_fires_re_rank_flag_divergence_and_re_hint():
    tl, labels = door_day()
    result = run(tl, labels)
    assert len(result.suggestions) >= 2
    first, second = result.suggestions[0], result.suggestions[1]
    exact: list[list[Fire] | None] = [[Fire(f, 0) for f in s.stats.fire_times] for s in result.suggestions]
    # The sandbox says the top suggestion never fires; the second also fires at 03:00 (unlabeled).
    exact[0] = []
    exact[1] = sorted([*exact[1], Fire(T0 + 3 * H, 0)])

    validated = apply_exact(result, exact)

    ranks = [s.rank for s in validated.suggestions]
    assert ranks == list(range(len(ranks)))
    moved = next(s for s in validated.suggestions if s.candidate == first.candidate)
    assert moved.rank == len(validated.suggestions) - 1  # diverged: ranked last
    assert moved.diverged and moved.exact
    assert moved.stats.anchors_hit == 0 and moved.approx_stats == first.stats
    assert "hits 0/3" in moved.explanation and moved.config["description"] == moved.explanation
    assert moved.as_dict()["diverged"] is True and "approx_stats" in moved.as_dict()
    promoted = next(s for s in validated.suggestions if s.candidate == second.candidate)
    assert T0 + 3 * H in promoted.stats.outside_fires
    if promoted.rank < 3:
        assert any(h.kind == "outside_fire" and h.at == T0 + 3 * H for h in validated.hints)


def test_a_suggestion_the_sandbox_could_not_run_keeps_the_miner_stats_last():
    tl, labels = door_day()
    result = run(tl, labels)
    exact = [[Fire(f, 0) for f in s.stats.fire_times] for s in result.suggestions]
    exact[0] = None
    validated = apply_exact(result, exact, warning={"code": "partly_validated", "message": "x"})
    last = validated.suggestions[-1]
    assert last.candidate == result.suggestions[0].candidate
    assert last.exact is False and last.stats == result.suggestions[0].stats
    assert validated.as_dict()["warning"] == {"code": "partly_validated", "message": "x"}


def test_estimated_runs_count_trigger_fires_before_conditions():
    tl, labels = door_day()
    result = run(tl, labels)
    ev = result.context.search.ev
    for suggestion in result.suggestions:
        # A run a condition stops costs as much as one that fires.
        assert estimated_runs(ev, suggestion.candidate) == sum(len(ev.trigger_fires(t)) for t in suggestion.candidate.triggers)
        assert estimated_runs(ev, suggestion.candidate) >= suggestion.stats.fires
    assert any(estimated_runs(ev, s.candidate) > s.stats.fires for s in result.suggestions)


def test_plan_validation_takes_the_best_suggestions_whose_runs_fit_the_budget():
    """Best first; one that does not fit is passed over and the ones after it still get their turn."""
    runs = {"a": 30_000, "b": 30_000, "c": 5_000, "d": 4_000}
    ev = SimpleNamespace(trigger_fires=lambda trigger: [0.0] * runs[trigger])
    result = SimpleNamespace(
        suggestions=[SimpleNamespace(candidate=SimpleNamespace(triggers=(name,)), helpers=()) for name in runs],
        context=SimpleNamespace(search=SimpleNamespace(ev=ev)),
    )
    assert plan_validation(result, budget=40_000) == ([0, 2, 3], 30_000)
    assert plan_validation(result, budget=8_000) == ([2], 64_000)
    assert plan_validation(result, budget=0) == ([], 69_000)
    assert plan_validation(result, budget=10**6) == ([0, 1, 2, 3], 0)


def test_plan_validation_keeps_the_helpers_within_what_one_child_sets_up():
    """The sandbox sets up at most 20 helpers (#42): a suggestion needing more is passed over; one sharing a helper already chosen still fits."""
    ev = SimpleNamespace(trigger_fires=lambda trigger: [0.0])

    def needing(*ids):
        return SimpleNamespace(candidate=SimpleNamespace(triggers=("t",)), helpers=tuple({"entity_id": i} for i in ids))

    many = [f"binary_sensor.h{n}" for n in range(19)]
    result = SimpleNamespace(
        suggestions=[needing(*many), needing("sensor.a", "sensor.b"), needing("sensor.a"), needing("binary_sensor.h3"), needing("sensor.c")],
        context=SimpleNamespace(search=SimpleNamespace(ev=ev)),
    )
    assert plan_validation(result) == ([0, 2, 3], 2)


def test_suggestions_left_unchecked_keep_the_miner_stats_and_their_rank():
    """Not replayed for their cost, a suggestion is not suspect: it ranks on the miner's stats among the ones that validated, not after them."""
    tl, labels = door_day()
    result = run(tl, labels)
    exact: list[list[Fire] | None] = [[Fire(f + 0.2, 0) for f in s.stats.fire_times] for s in result.suggestions]
    exact[0] = None

    validated = apply_exact(result, exact, unchecked=[0], warning={"code": "validation_capped", "message": "x"})

    assert [s.candidate for s in validated.suggestions] == [s.candidate for s in result.suggestions]
    top = validated.suggestions[0]
    assert top.exact is False and top.stats == result.suggestions[0].stats and top.approx_stats is None
    assert all(s.exact is True for s in validated.suggestions[1:])
    assert validated.as_dict()["warning"]["code"] == "validation_capped"


def test_mark_approximate():
    tl, labels = door_day()
    result = mark_approximate(run(tl, labels), {"code": "validation_failed", "message": "boom"})
    out = result.as_dict()
    assert out["warning"]["code"] == "validation_failed"
    assert all(s["exact"] is False for s in out["suggestions"])


def test_must_not_fires_found_only_by_the_sandbox_count():
    tl, _ = door_day()
    labels = Labels([fire(8 * H), fire(18 * H), fire(21 * H), no_fire(2 * H, 4 * H)])
    result = run(tl, labels)
    exact = [[Fire(f, 0) for f in s.stats.fire_times] for s in result.suggestions]
    exact[0] = sorted([*exact[0], Fire(T0 + 3 * H, 0)])
    validated = apply_exact(result, exact)
    moved = next(s for s in validated.suggestions if s.candidate == result.suggestions[0].candidate)
    assert moved.stats.no_fire_violations == (T0 + 3 * H,)
    assert "in must-not periods" in moved.explanation


def test_suggest_stops_when_asked():
    tl, labels = door_day()
    stop = threading.Event()
    stop.set()
    with pytest.raises(SuggestCancelled):
        suggest(tl, labels, time_zone="UTC", should_stop=stop.is_set)
    assert suggest(tl, labels, time_zone="UTC", should_stop=lambda: False).suggestions


def test_random_noise_does_not_break_apply_exact():
    rng = random.Random(3)
    tl, labels = door_day()
    result = run(tl, labels)
    exact = [[Fire(f + rng.uniform(-1, 1), 0) for f in s.stats.fire_times] for s in result.suggestions]
    exact = [sorted(e) for e in exact]
    assert len(apply_exact(result, exact).suggestions) == len(result.suggestions)
