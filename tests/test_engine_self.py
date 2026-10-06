"""#17: the replayed automation is its own entity.

An automation that references itself (it turns itself off, a template reads its state) has its own entity id on the timeline. The sandbox does not write that track as a raw state: the recorded state at the window start is the automation's last state when the sandbox boots, as live Home Assistant restores it, and recorded on/off changes in the window turn the replayed automation on and off.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.event_simulator import history

from .test_engine import calls, run_child, summary, utc
from .test_engine_fidelity import scenario

SELF = "automation.crank_heat"
DOOR = "binary_sensor.door"
# Turns itself off after it fires, as the automation in the issue does.
CRANK_HEAT = {
    "id": "crank_heat",
    "alias": "Crank heat",
    "triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}],
    "actions": [
        {"action": "climate.set_temperature", "target": {"entity_id": "climate.hall"}, "data": {"temperature": 25}},
        {"action": "automation.turn_off", "target": {"entity_id": SELF}},
    ],
}


def door(*times: str) -> list[dict[str, Any]]:
    """The door opening at each time and closing a minute later."""
    events = []
    for at in times:
        opened = utc(f"2026-10-04T{at}:00+00:00")
        events.append({"at": opened.isoformat(), "entity_id": DOOR, "state": "on", "attributes": {}})
        events.append({"at": opened.replace(minute=opened.minute + 1).isoformat(), "entity_id": DOOR, "state": "off", "attributes": {}})
    return events


def own(at: str, state: str) -> dict[str, Any]:
    return {"at": f"2026-10-04T{at}:00+00:00", "entity_id": SELF, "state": state, "attributes": {"id": "crank_heat", "friendly_name": "Crank heat"}}


async def replay(hass: HomeAssistant, automation: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Replay with a registry snapshot that carries the automation's own registry entry, as the simulate command sends it."""
    er.async_get(hass).async_get_or_create("automation", "automation", automation["id"], suggested_object_id=automation["id"])
    built = scenario(automation, **extra)
    timeline = list(dict.fromkeys([*built["initial_states"], *(event["entity_id"] for event in built["events"])]))
    built["registry"] = history.async_registry_snapshot(hass, built["automation"], timeline)
    assert SELF in [entry["entity_id"] for entry in built["registry"]["entities"]]
    code, result, stderr = await asyncio.get_running_loop().run_in_executor(None, run_child, built)
    assert code == 0, stderr[-4000:]
    return result


def initial(state: str, **attributes: Any) -> dict[str, Any]:
    return {
        DOOR: {"state": "off", "attributes": {}},
        SELF: {"state": state, "attributes": {"id": "crank_heat", "friendly_name": "Crank heat", **attributes}, "last_changed": "2026-10-03T12:00:00+00:00"},
    }


async def test_own_entity_on_the_timeline_loads_and_replays(hass: HomeAssistant):
    result = await replay(hass, CRANK_HEAT, initial_states=initial("on"), events=door("01:00"))
    assert result["error"] is None, result["error"]
    assert summary(result) == [(utc("2026-10-04T01:00:00+00:00"), "fired", None)]
    assert calls(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "climate.set_temperature"),
        (utc("2026-10-04T01:00:00+00:00"), "automation.turn_off"),
    ]


async def test_recorded_off_disables_it_until_a_recorded_on(hass: HomeAssistant):
    result = await replay(
        hass,
        CRANK_HEAT,
        initial_states=initial("on"),
        events=sorted([*door("01:00", "03:00", "05:00"), own("01:30", "off"), own("04:00", "on")], key=lambda event: event["at"]),
    )
    assert result["error"] is None, result["error"]
    assert summary(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "fired", None),
        (utc("2026-10-04T05:00:00+00:00"), "fired", None),
    ]


async def test_initial_off_loads_disabled_until_a_recorded_on(hass: HomeAssistant):
    result = await replay(hass, CRANK_HEAT, initial_states=initial("off"), events=sorted([*door("01:00", "03:00"), own("02:00", "on")], key=lambda event: event["at"]))
    assert result["error"] is None, result["error"]
    assert summary(result) == [(utc("2026-10-04T03:00:00+00:00"), "fired", None)]


def test_initial_off_without_a_registry_entry_predicts_its_entity_id():
    """No registry snapshot: the automation's entity id comes from its alias, as Home Assistant names it."""
    built = scenario(
        {**CRANK_HEAT, "actions": CRANK_HEAT["actions"][:1]},
        initial_states=initial("off"),
        events=sorted([*door("01:00", "03:00"), own("02:00", "on")], key=lambda event: event["at"]),
    )
    code, result, stderr = run_child(built)
    assert code == 0, stderr[-4000:]
    assert result["error"] is None, result["error"]
    assert summary(result) == [(utc("2026-10-04T03:00:00+00:00"), "fired", None)]


async def test_templates_read_the_automations_own_state(hass: HomeAssistant):
    """Its state and the restored `last_triggered` are the sandbox entity's, not a raw copy of the track."""
    automation = {
        **CRANK_HEAT,
        # At most once an hour, by its own last_triggered.
        "conditions": [{"condition": "template", "value_template": "{{ as_timestamp(now()) - as_timestamp(state_attr('automation.crank_heat', 'last_triggered'), 0) > 3600 }}"}],
        "actions": [{"action": "notify.notify", "data": {"message": "{{ states('automation.crank_heat') }}|{{ state_attr('automation.crank_heat', 'current') }}"}}],
    }
    result = await replay(hass, automation, initial_states=initial("on", last_triggered="2026-10-04T00:30:00+00:00"), events=door("01:00", "02:00"))
    assert result["error"] is None, result["error"]
    assert summary(result) == [
        (utc("2026-10-04T01:00:00+00:00"), "condition_failed", "condition/0"),
        (utc("2026-10-04T02:00:00+00:00"), "fired", None),
    ]
    # `current` counts the run in progress: the sandbox entity's own attribute.
    assert result["service_calls"][0]["data"]["message"] == "on|1"


async def test_calling_itself_warns_that_history_drives_its_state(hass: HomeAssistant):
    result = await replay(hass, CRANK_HEAT, initial_states=initial("on"), events=door("01:00", "03:00"))
    assert result["error"] is None, result["error"]
    warnings = [warning for warning in result["warnings"] if warning["code"] == "self_service_call"]
    assert len(warnings) == 1
    assert "automation.turn_off" in warnings[0]["message"]
    assert SELF in warnings[0]["message"]
    # Recorded only: with no recorded off on the timeline it is still on and fires again.
    assert [outcome for _, outcome, _ in summary(result)] == ["fired", "fired"]
