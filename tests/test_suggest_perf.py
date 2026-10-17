"""Suggest on chatty sensors stays fast (issue #36).

The shape of the user's fan controllers: two temperature sensors reporting every minute and a half for 30 days, and labels on their first 500 reports, so the top suggestions are any-change triggers firing ~30 000 times each. Validating all of them replayed ~300 000 runs, 25 s, while holding a sandbox slot other replays queued behind. The history is handed to the command directly (no recorder: writing 60 000 states would be the slow part of the test).
"""

from __future__ import annotations

import random
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.event_simulator import runner, websocket
from custom_components.event_simulator.suggest.exact import MAX_VALIDATION_RUNS


OUTDOOR = "sensor.outdoor_temperature"
INDOOR = "sensor.indoor_temperature"
START = datetime(2026, 8, 1, tzinfo=UTC)
DAYS = 30
LABELS = 500
# Generous: the whole request (mining included) takes about 10 s on a fast desktop and 23 s on a GitHub-hosted runner;
# before the fix the validation alone took 25 s on the desktop. The run budget asserted below is the hardware-independent check.
BOUND = 45.0


def fan_history() -> tuple[dict[str, Any], list[datetime]]:
    """Both sensors reporting a new value every 60-110 s; returns the timeline and every report time."""
    rng = random.Random(36)
    end = START + timedelta(days=DAYS)
    attributes = {"unit_of_measurement": "°F", "device_class": "temperature", "state_class": "measurement"}
    events = []
    for entity_id in (OUTDOOR, INDOOR):
        at, value = START + timedelta(seconds=rng.uniform(0, 60)), 70.0
        while at < end:
            value = round(value + rng.choice((-0.2, -0.1, 0.1, 0.2)), 1)
            events.append({"at": at.isoformat(), "entity_id": entity_id, "state": str(value), "attributes": attributes})
            at += timedelta(seconds=rng.uniform(60, 110))
    events.sort(key=lambda event: event["at"])
    initial = {
        entity_id: {"state": "70.0", "attributes": attributes, "last_changed": START.isoformat(), "last_updated": START.isoformat()}
        for entity_id in (OUTDOOR, INDOOR)
    }
    return {"initial_states": initial, "events": events}, [datetime.fromisoformat(event["at"]) for event in events]


async def test_validation_of_chatty_any_change_suggestions_is_bounded(hass: HomeAssistant):
    timeline, reports = fan_history()
    replayed: list[dict[str, Any]] = []
    run_scenario = runner.async_run_scenario

    async def counting(hass, scenario, **kwargs):
        result = await run_scenario(hass, scenario, **kwargs)
        replayed.append({"automations": len(scenario["automations"]), "runs": len(result.get("runs") or [])})
        return result

    # The command's message after its schema (the final event is a few MiB of fire times, more than the test client takes).
    message = {
        "entity_ids": [OUTDOOR, INDOOR],
        "start": START,
        "end": START + timedelta(days=DAYS),
        "labels": [
            {"start": at.isoformat(), "end": (at + timedelta(seconds=1)).isoformat(), "kind": "fire", "anchor": "start"}
            for at in reports[:LABELS]
        ],
        "options": {"tolerance_before": 120, "tolerance_after": 600},
    }
    progress: list[dict[str, Any]] = []
    with (
        patch.object(websocket.history, "async_get_timeline", AsyncMock(return_value=timeline)),
        patch.object(runner, "async_run_scenario", counting),
    ):
        began = time.monotonic()
        final = await websocket.async_suggest(hass, message, progress.append, threading.Event())
        took = time.monotonic() - began

    suggestions = final["suggestions"]
    assert suggestions
    # The best suggestion is still the any-change trigger the labels sit on, whether or not it was replayed.
    top = suggestions[0]["config"]["triggers"]
    assert {trigger.get("entity_id") for trigger in top} <= {OUTDOOR, INDOOR}
    # What the sandbox replayed stays within the budget (a run or two over: the estimate is the miner's).
    assert sum(item["runs"] for item in replayed) <= MAX_VALIDATION_RUNS * 1.1
    unchecked = [s for s in suggestions if s.get("exact") is False]
    if unchecked:
        assert final["warning"]["code"] in ("validation_capped", "partly_validated")
    assert took < BOUND, f"suggest took {took:.1f} s; progress {progress}"
