"""Suggest miner: the internal candidate representation and its rendering to an automation config."""

from __future__ import annotations

import pytest
import voluptuous as vol

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant

from custom_components.event_simulator.suggest.model import (
    Candidate,
    NumericStateCondition,
    NumericStateTrigger,
    StateCondition,
    StateTrigger,
    SunCondition,
    SunTrigger,
    TimeCondition,
    TimeTrigger,
    format_duration,
    render_automation,
    render_condition,
    render_trigger,
)

DOOR = "binary_sensor.door"


def test_state_trigger_renders_modern_keys_scalars_lists_and_for():
    assert render_trigger(StateTrigger(DOOR, to=("on",), from_=("off",))) == {
        "trigger": "state",
        "entity_id": DOOR,
        "from": "off",
        "to": "on",
    }
    assert render_trigger(
        StateTrigger("light.l", to=("on", "off"), for_=330, attribute="brightness", id="start")
    ) == {
        "trigger": "state",
        "id": "start",
        "entity_id": "light.l",
        "attribute": "brightness",
        "to": ["on", "off"],
        "for": {"hours": 0, "minutes": 5, "seconds": 30},
    }
    assert render_trigger(StateTrigger(DOOR, not_to=("unavailable", "unknown"))) == {
        "trigger": "state",
        "entity_id": DOOR,
        "not_to": ["unavailable", "unknown"],
    }
    # `to: null`: any change of the state itself, but not attribute-only writes.
    assert render_trigger(StateTrigger(DOOR, changes_only=True)) == {"trigger": "state", "entity_id": DOOR, "to": None}


def test_other_triggers_render():
    assert render_trigger(NumericStateTrigger("sensor.h", above=70, for_=300)) == {
        "trigger": "numeric_state",
        "entity_id": "sensor.h",
        "above": 70,
        "for": {"hours": 0, "minutes": 5, "seconds": 0},
    }
    assert render_trigger(NumericStateTrigger("sensor.h", below=20.5, attribute="t")) == {
        "trigger": "numeric_state",
        "entity_id": "sensor.h",
        "attribute": "t",
        "below": 20.5,
    }
    assert render_trigger(TimeTrigger(7 * 3600, weekdays=("mon", "tue"))) == {
        "trigger": "time",
        "at": "07:00:00",
        "weekday": ["mon", "tue"],
    }
    assert render_trigger(SunTrigger("sunset", offset=-1800)) == {
        "trigger": "sun",
        "event": "sunset",
        "offset": "-00:30:00",
    }
    assert render_trigger(SunTrigger("sunrise")) == {"trigger": "sun", "event": "sunrise"}


def test_conditions_render():
    assert render_condition(StateCondition(DOOR, ("on",), for_=60)) == {
        "condition": "state",
        "entity_id": DOOR,
        "state": "on",
        "for": {"hours": 0, "minutes": 1, "seconds": 0},
    }
    assert render_condition(NumericStateCondition("sensor.h", above=1, below=2)) == {
        "condition": "numeric_state",
        "entity_id": "sensor.h",
        "above": 1,
        "below": 2,
    }
    assert render_condition(TimeCondition(after=22 * 3600, before=6 * 3600 + 30 * 60)) == {
        "condition": "time",
        "after": "22:00:00",
        "before": "06:30:00",
    }
    assert render_condition(TimeCondition(weekdays=("sat", "sun"))) == {"condition": "time", "weekday": ["sat", "sun"]}
    assert render_condition(SunCondition(after="sunset", after_offset=-600, before="sunrise")) == {
        "condition": "sun",
        "after": "sunset",
        "after_offset": "-00:10:00",
        "before": "sunrise",
    }


def test_format_duration():
    assert format_duration(0) == "00:00:00"
    assert format_duration(3725) == "01:02:05"
    assert format_duration(-90) == "-00:01:30"


def test_invalid_candidates_are_refused():
    with pytest.raises(ValueError):
        NumericStateTrigger("sensor.h")
    with pytest.raises(ValueError):
        SunTrigger("noon")
    with pytest.raises(ValueError):
        TimeTrigger(86_400)
    with pytest.raises(ValueError):
        TimeCondition(weekdays=("monday",))
    with pytest.raises(ValueError):
        SunCondition()
    with pytest.raises(ValueError):
        StateTrigger(DOOR, to=("on",), changes_only=True)


def test_candidates_are_hashable_and_count_their_parts():
    a = Candidate((StateTrigger(DOOR, to=("on",)),), (TimeCondition(after=3600),))
    b = Candidate((StateTrigger(DOOR, to=("on",)),), (TimeCondition(after=3600),))
    assert a == b and hash(a) == hash(b)
    assert a.with_condition(SunCondition(after="sunset")).conditions[-1] == SunCondition(after="sunset")
    assert a.entity_ids == {DOOR}


EVERYTHING = Candidate(
    triggers=(
        StateTrigger(DOOR, to=("on",), from_=("off",), id="start"),
        StateTrigger(DOOR, to=("off",), for_=600, id="end"),
        StateTrigger("light.l", attribute="brightness", changes_only=False),
        StateTrigger("light.l", changes_only=True),
        StateTrigger("light.l", not_from=("unavailable",)),
        NumericStateTrigger("sensor.h", above=70, for_=300),
        NumericStateTrigger("sensor.h", above=10, below=20.5, attribute="battery"),
        TimeTrigger(7 * 3600 + 30 * 60 + 15, weekdays=("mon", "fri")),
        SunTrigger("sunset", offset=-1800),
        SunTrigger("sunrise", offset=900),
    ),
    conditions=(
        StateCondition("person.me", ("home", "work"), for_=120),
        StateCondition("light.l", (True,), attribute="on_flag"),
        NumericStateCondition("sensor.h", below=50),
        TimeCondition(after=22 * 3600, before=6 * 3600, weekdays=("sat", "sun")),
        SunCondition(after="sunset", after_offset=-600, before="sunrise", before_offset=1200),
    ),
)


def test_render_automation_shape():
    config = render_automation(EVERYTHING, alias="Suggested")
    assert config["alias"] == "Suggested"
    assert config["mode"] == "single"
    assert len(config["triggers"]) == 10 and len(config["conditions"]) == 5
    assert config["actions"][0]["action"] == "logbook.log"
    custom = render_automation(EVERYTHING, alias="x", actions=[{"action": "light.turn_on"}])
    assert custom["actions"] == [{"action": "light.turn_on"}]


async def test_rendered_automation_passes_home_assistant_validation(hass: HomeAssistant):
    config = render_automation(EVERYTHING, alias="Suggested")
    validated = await async_validate_config_item(hass, "suggested", config)
    assert validated is not None
    assert validated.validation_error is None, validated.validation_error
    assert len(validated["triggers"]) == 10
    assert len(validated["conditions"]) == 5

    # The check has teeth: a broken trigger is reported.
    broken = {**config, "triggers": [{"trigger": "numeric_state", "entity_id": "sensor.h"}]}
    with pytest.raises(vol.Invalid):
        await async_validate_config_item(hass, "broken", broken)
