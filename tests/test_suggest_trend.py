"""Suggest miner: rising / falling / rate-of-change suggestions through Trend and Derivative helpers (#42).

Synthetic timelines where a level threshold cannot tell the labeled moments from others but the rate of change can (a shower against a humid day, an open window against the evening cooling), and one where the level alone explains them and stays on top.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant

from custom_components.event_simulator.suggest.labels import Label, Labels, Tolerance
from custom_components.event_simulator.suggest.model import DerivativeTrigger, NumericStateTrigger, TrendTrigger
from custom_components.event_simulator.suggest.search import suggest
from custom_components.event_simulator.suggest.timeline import Timeline, to_ts

START = "2026-10-05T00:00:00+00:00"  # a Monday
T0 = to_ts(START)
M = 60
H = 3600
D = 86_400
HUM = "sensor.bathroom_humidity"
TEMP = "sensor.living_room_temperature"
PERCENT = {"unit_of_measurement": "%", "device_class": "humidity", "friendly_name": "Bathroom humidity"}
CELSIUS = {"unit_of_measurement": "°C", "device_class": "temperature", "friendly_name": "Living room temperature"}


class Sensor:
    """A sensor reporting on change at a resolution, from a function of time sampled every `step` seconds."""

    def __init__(self, entity_id: str, attributes: dict[str, Any], resolution: float, digits: int) -> None:
        self.entity_id, self.attributes, self.resolution, self.digits = entity_id, attributes, resolution, digits
        self.events: list[dict[str, Any]] = []
        self.last: float | None = None

    def run(self, value_at, days: int, step: float = 30) -> None:
        t = 0.0
        while t < days * D:
            value = round(round(value_at(t) / self.resolution) * self.resolution, self.digits)
            if value != self.last:
                self.events.append(
                    {"at": datetime.fromtimestamp(T0 + t, UTC).isoformat(), "entity_id": self.entity_id, "state": str(value), "attributes": self.attributes}
                )
                self.last = value
            t += step

    def timeline(self, days: int, first: float) -> Timeline:
        initial = {self.entity_id: {"state": str(first), "attributes": self.attributes}}
        return Timeline.from_scenario(initial, self.events, START, T0 + days * D)


def bump(t: float, at: float, rise: float, height: float, fall: float) -> float:
    """0 before `at`, up `height` linearly over `rise` seconds, then back down linearly over `fall` seconds."""
    if t < at:
        return 0.0
    if t < at + rise:
        return height * (t - at) / rise
    if t < at + rise + fall:
        return height * (1 - (t - at - rise) / fall)
    return 0.0


DAYS = 10
# Showers (day, hour): humidity jumps from 50 % to 85 % within 8 minutes, starting a minute after the shower is turned on, and dries out over an hour.
SHOWERS = [(0, 7.0), (1, 21.5), (2, 7.25), (3, 13.0), (4, 7.5), (6, 20.0), (7, 7.0), (8, 21.0)]
# Humid days (day, hour of the rise's start): from 50 % to 88 % over four hours, high for three hours, and back over four; on the showers' weekdays, crossing every level at the showers' times of day.
HUMID = [(1, 8.0), (3, 15.0), (6, 6.0), (8, 9.0)]


def humidity(t: float) -> float:
    rng = random.Random(int(t // 1800))
    value = 50 + rng.choice((-1, 0, 0, 0, 1))  # a step now and then, half an hour at a time
    for day, hour in SHOWERS:
        value += bump(t, day * D + hour * H + 60, 8 * M, 35, H)
    for day, hour in HUMID:
        at = day * D + hour * H
        if at <= t < at + 4 * H:
            value += 38 * (t - at) / (4 * H)
        elif at + 4 * H <= t < at + 7 * H:
            value += 38
        elif at + 7 * H <= t < at + 11 * H:
            value += 38 * (1 - (t - at - 7 * H) / (4 * H))
    return min(value, 99)


def shower_case() -> tuple[Timeline, Labels]:
    sensor = Sensor(HUM, PERCENT, 1, 0)
    sensor.run(humidity, DAYS)
    labels = Labels(
        [Label(T0 + day * D + hour * H, T0 + day * D + hour * H + 20 * M, "fire", "start") for day, hour in SHOWERS],
        Tolerance(120, 600),
    )
    return sensor.timeline(DAYS, 50), labels


def test_a_shower_is_found_by_how_fast_the_humidity_rises_not_how_high():
    timeline, labels = shower_case()
    result = suggest(timeline, labels, time_zone="UTC")
    best = result.suggestions[0]
    assert best.kinds == ["trend"] and best.trigger_kinds == ["trend"]
    assert (best.stats.anchors_hit, best.stats.anchors_total) == (len(SHOWERS), len(SHOWERS))
    assert not best.stats.outside_fires
    # A level threshold cannot do it: one low enough for the showers fires on every humid day too.
    levels = [s for s in result.suggestions if s.trigger_kinds == ["numeric"]]
    assert levels and all(s.stats.outside_nearby or s.stats.missed for s in levels)
    [trigger] = best.candidate.triggers
    assert isinstance(trigger, TrendTrigger | DerivativeTrigger)
    # The helper it needs, named after the source, with the options its config flow stores.
    [helper] = best.helpers
    if isinstance(trigger, TrendTrigger):
        assert trigger.delta > 0
        assert helper["domain"] == "trend" and helper["entity_id"] == "binary_sensor.bathroom_humidity_rising"
        options = helper["options"]
        assert options["entity_id"] == HUM and options["invert"] is False
        assert options["sample_duration"] == trigger.window and options["min_gradient"] == float(f"{trigger.delta / trigger.window:.6g}")
        assert options["max_samples"] >= 2
        assert best.config["triggers"] == [{"trigger": "state", "entity_id": "binary_sensor.bathroom_humidity_rising", "to": "on"}]
        assert helper["title"] == "Bathroom humidity rising"
    else:
        assert helper["domain"] == "derivative" and helper["entity_id"] == "sensor.bathroom_humidity_derivative"
        assert helper["options"]["source"] == HUM
    # "rises by Δ within T" in plain words.
    assert "Bathroom humidity" in best.explanation or HUM in best.explanation
    assert "rises by more than" in best.explanation and "within" in best.explanation
    as_dict = best.as_dict()
    assert as_dict["helpers"] == [helper] and as_dict["kinds"] == ["trend"]
    assert result.as_dict()["kind_counts"]["trend"] >= 1


def test_variants_of_a_trend_card_carry_their_own_helper_options_under_the_same_entity():
    timeline, labels = shower_case()
    result = suggest(timeline, labels, time_zone="UTC")
    card = next(s for s in result.suggestions if s.variants and s.helpers)
    for variant in card.variants:
        [helper] = variant.helpers
        assert helper["entity_id"] == card.helpers[0]["entity_id"]
        assert variant.as_dict()["helpers"] == [helper]
        entity = variant.config["triggers"][0]["entity_id"]
        assert entity == helper["entity_id"]
    assert any(v.helpers[0]["options"] != card.helpers[0]["options"] for v in card.variants)


def test_a_helper_never_takes_the_id_of_an_existing_entity():
    timeline, labels = shower_case()
    taken = {"binary_sensor.bathroom_humidity_rising", "sensor.bathroom_humidity_derivative"}
    result = suggest(timeline, labels, time_zone="UTC", taken=taken)
    ids = {h["entity_id"] for s in result.suggestions for h in s.helpers}
    assert ids and not ids & taken
    # Cards whose helpers differ get different entities; the same helper keeps one.
    by_id: dict[str, dict[str, Any]] = {}
    for suggestion in result.suggestions:
        for helper in suggestion.helpers:
            assert by_id.setdefault(helper["entity_id"], helper["options"]) == helper["options"]


async def test_trend_suggestions_are_valid_automations(hass: HomeAssistant):
    timeline, labels = shower_case()
    result = suggest(timeline, labels, time_zone="UTC")
    configs = [s.config for s in result.suggestions if s.helpers] + [v.config for s in result.suggestions for v in s.variants if v.helpers]
    assert configs
    for config in configs:
        await async_validate_config_item(hass, "suggestion", config)


# An open window: the temperature drops 3 °C within ten minutes and comes back over an hour once it is closed; every evening it cools 4 °C over five hours.
WINDOWS = [(0, 9.0), (1, 19.5), (3, 8.5), (4, 20.0), (6, 9.5), (8, 18.75)]


def temperature(t: float) -> float:
    day, clock = divmod(t, D)
    value = 21.0
    if 17 * H <= clock < 22 * H:
        value -= 4 * (clock - 17 * H) / (5 * H)
    elif clock >= 22 * H or clock < 6 * H:
        value -= 4 * (1 - ((clock - 22 * H) % D) / (8 * H))
    for d, hour in WINDOWS:
        value -= bump(t, d * D + hour * H + 30, 10 * M, 3, H)
    return value


def test_an_open_window_is_found_by_how_fast_the_temperature_falls():
    sensor = Sensor(TEMP, CELSIUS, 0.1, 1)
    sensor.run(temperature, DAYS)
    labels = Labels([Label(T0 + d * D + hour * H, T0 + d * D + hour * H + 30 * M, "fire", "start") for d, hour in WINDOWS], Tolerance(120, 600))
    result = suggest(sensor.timeline(DAYS, 21), labels, time_zone="UTC")
    best = result.suggestions[0]
    assert best.trigger_kinds == ["trend"]
    assert (best.stats.anchors_hit, best.stats.outside_nearby) == (len(WINDOWS), 0)
    [trigger] = best.candidate.triggers
    if isinstance(trigger, TrendTrigger):
        assert trigger.delta < 0
        assert best.helpers[0]["entity_id"] == "binary_sensor.living_room_temperature_falling"
        assert best.helpers[0]["options"]["min_gradient"] < 0
    else:
        assert trigger.below is not None and trigger.below < 0
    assert "drops by more than" in best.explanation


def test_a_level_that_explains_the_labels_stays_ahead_of_a_trend():
    """The heating turns off when the room passes 23 °C: it warms fast some days and slowly others, so only the level fits all of them."""
    warmups = [(0, 2 * H), (1, 20 * M), (2, 3 * H), (3, 30 * M), (5, 90 * M), (6, 15 * M), (8, 4 * H)]

    def warm(t: float) -> float:
        value = 20.0
        for day, rise in warmups:
            at = day * D + 12 * H
            value += bump(t, at, rise, 4, 2 * H)
        return value

    sensor = Sensor(TEMP, CELSIUS, 0.1, 1)
    sensor.run(warm, DAYS)
    crossings = [T0 + day * D + 12 * H + 0.75 * rise for day, rise in warmups]  # 23 °C is 3/4 of the way up
    labels = Labels([Label(at, at + 10 * M, "fire", "start") for at in crossings], Tolerance(120, 600))
    result = suggest(sensor.timeline(DAYS, 20), labels, time_zone="UTC")
    best = result.suggestions[0]
    [trigger] = best.candidate.triggers
    assert isinstance(trigger, NumericStateTrigger) and trigger.above is not None
    assert best.helpers == () and "trend" not in best.kinds
