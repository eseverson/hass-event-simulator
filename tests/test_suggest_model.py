"""Suggest miner: the internal candidate representation and its rendering to an automation config."""

from __future__ import annotations

import pytest
import voluptuous as vol

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant

from custom_components.event_simulator.suggest.model import (
    Candidate,
    EntityTimeTrigger,
    EventTrigger,
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


def test_event_trigger_renders_type_and_data_subset():
    assert render_trigger(EventTrigger("zha_event", (("device_ieee", "b0:e8"), ("command", "on")), id="start")) == {
        "trigger": "event",
        "id": "start",
        "event_type": "zha_event",
        "event_data": {"device_ieee": "b0:e8", "command": "on"},
    }
    assert render_trigger(EventTrigger("tag_scanned")) == {"trigger": "event", "event_type": "tag_scanned"}
    with pytest.raises(ValueError):
        EventTrigger("")
    candidate = Candidate((EventTrigger("tag_scanned"), StateTrigger(DOOR, to=("on",))))
    assert candidate.entity_ids == {DOOR}
    assert candidate.event_types == {"tag_scanned"}


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


def test_entity_time_trigger_renders_the_entity_alone_or_with_its_offset():
    # Without an offset, the plain entity id: Home Assistant 2026.2 takes `{entity_id}` without `offset` for a clock time and fails to attach it.
    assert render_trigger(EntityTimeTrigger("input_datetime.wake_up")) == {"trigger": "time", "at": "input_datetime.wake_up"}
    assert render_trigger(EntityTimeTrigger("input_datetime.boot_dryer_start", offset=86_400, id="end")) == {
        "trigger": "time",
        "id": "end",
        "at": {"entity_id": "input_datetime.boot_dryer_start", "offset": "24:00:00"},
    }
    assert render_trigger(EntityTimeTrigger("sensor.next_alarm", offset=-1800))["at"] == {"entity_id": "sensor.next_alarm", "offset": "-00:30:00"}
    # The entity is read: the sandbox needs its states.
    assert Candidate((EntityTimeTrigger("sensor.next_alarm"),)).entity_ids == {"sensor.next_alarm"}
    with pytest.raises(ValueError):
        EntityTimeTrigger("binary_sensor.door")


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
        EventTrigger("zha_event", (("device_ieee", "b0:e8:e8:ff:fe:48:0b:83"), ("command", "learned_ir_code"), ("endpoint_id", 1))),
        EntityTimeTrigger("input_datetime.boot_dryer_start", offset=86_400),
        EntityTimeTrigger("sensor.next_alarm", offset=-1800),
        EntityTimeTrigger("input_datetime.wake_up"),
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
    assert len(config["triggers"]) == 14 and len(config["conditions"]) == 5
    assert config["actions"][0]["action"] == "logbook.log"
    custom = render_automation(EVERYTHING, alias="x", actions=[{"action": "light.turn_on"}])
    assert custom["actions"] == [{"action": "light.turn_on"}]


def test_a_state_condition_held_in_any_of_several_states_renders_one_per_state():
    """Home Assistant 2026.10 refuses `for` with a list of states (core #174083); `or` over the states, each with its own `for`, is what the list meant: in one of them, unchanged for that long."""
    assert render_condition(StateCondition("person.me", ("home", "work"), for_=120)) == {
        "condition": "or",
        "conditions": [
            {"condition": "state", "entity_id": "person.me", "state": "home", "for": {"hours": 0, "minutes": 2, "seconds": 0}},
            {"condition": "state", "entity_id": "person.me", "state": "work", "for": {"hours": 0, "minutes": 2, "seconds": 0}},
        ],
    }


def test_an_attribute_state_condition_cannot_have_a_duration():
    """Home Assistant measures `for` from the entity's last_changed, not the attribute's, and 2026.10 refuses it."""
    with pytest.raises(ValueError, match="attribute"):
        StateCondition("calendar.x", ("Trash",), attribute="message", for_=60)


async def test_rendered_automation_passes_home_assistant_validation(hass: HomeAssistant):
    config = render_automation(EVERYTHING, alias="Suggested")
    validated = await async_validate_config_item(hass, "suggested", config)
    assert validated is not None
    assert validated.validation_error is None, validated.validation_error
    assert len(validated["triggers"]) == 14
    assert len(validated["conditions"]) == 5

    # The check has teeth: a broken trigger is reported.
    broken = {**config, "triggers": [{"trigger": "numeric_state", "entity_id": "sensor.h"}]}
    with pytest.raises(vol.Invalid):
        await async_validate_config_item(hass, "broken", broken)
