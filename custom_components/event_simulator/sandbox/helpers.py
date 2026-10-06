"""Trend and Derivative helpers for the scenario's `helpers` block: what each one is, checked and filled in as the helper's config entry holds it.

Standard library only: the sandbox child sets these helpers up, and the live side (the miner proposing one) builds the same shape.

A helper is `{domain, entity_id, options, title?}`:

- `domain`: `trend` (a `binary_sensor`, on while the source's linear gradient over its recent samples passes `min_gradient`) or `derivative` (a `sensor`, the source's rate of change).
- `entity_id`: the helper's own entity id (`binary_sensor.*` for a trend, `sensor.*` for a derivative), which automations read.
- `options`: the helper's config entry options, as the helper's own config flow stores them.
  - trend: `entity_id` (the source, required), `attribute` (read that attribute instead of the state), `invert` (default false), `max_samples` / `min_samples` (at least 2, default 2), `min_gradient` (units per second, default 0), `sample_duration` (seconds, default 0 = no age limit).
  - derivative: `source` (required), `round` (decimals, 0-6, default 2), `time_window` (default 0), `unit_prefix` (one of `UNIT_PREFIXES`, default none), `unit_time` (`s`, `min`, `h`, `d`; default `h`), `max_sub_interval` (optional). Durations are the flow's `{days, hours, minutes, seconds, milliseconds}` object or a number of seconds; they are stored as the object.
- `title`: the config entry's title, the helper's name (default: its object id).

`parse_helpers` raises `ValueError` naming the offending field; unknown option keys are refused, so a typo is not silently a default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# The helper integrations the sandbox sets up, and the entity domain of each one's entity.
ENTITY_DOMAINS = {"trend": "binary_sensor", "derivative": "sensor"}
# Most helpers one scenario may carry.
MAX_HELPERS = 20
# Most state changes the result carries per helper (`helper_states`); past that, a `helper_states_truncated` warning.
MAX_HELPER_STATES = 10_000
# The derivative's unit prefixes (its sensor's `UNIT_PREFIXES`; the config flow also offers `P`, which the sensor does not know and fails on).
UNIT_PREFIXES = ("n", "μ", "m", "k", "M", "G", "T")
UNIT_TIMES = ("s", "min", "h", "d")
_DURATION_KEYS = ("days", "hours", "minutes", "seconds", "milliseconds")


@dataclass(slots=True)
class Helper:
    """One helper of the scenario, its options filled in with the defaults its config flow stores."""

    index: int
    domain: str
    entity_id: str
    title: str
    options: dict[str, Any]
    # The entity it computes from (trend `entity_id`, derivative `source`).
    source: str
    # Its own recorded state at the window start (taken out of `initial_states`): restored as its last state, as after a restart.
    recorded: dict[str, Any] | None = field(default=None)

    @property
    def entity_domain(self) -> str:
        return ENTITY_DOMAINS[self.domain]


def parse_helpers(value: Any, valid_entity_id: Callable[[str], bool]) -> list[Helper]:
    """The scenario's `helpers`, checked; `valid_entity_id` is Home Assistant's own check."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("helpers must be a list of {domain, entity_id, options}")
    if len(value) > MAX_HELPERS:
        raise ValueError(f"helpers carries {len(value)} helpers; the limit is {MAX_HELPERS}")
    found: list[Helper] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        what = f"helpers[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{what} must be an object")
        domain = item.get("domain")
        if domain not in ENTITY_DOMAINS:
            raise ValueError(f"{what}.domain must be one of {', '.join(ENTITY_DOMAINS)}, not {domain!r}")
        entity_domain = ENTITY_DOMAINS[domain]
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not valid_entity_id(entity_id) or not entity_id.startswith(f"{entity_domain}."):
            raise ValueError(f"{what}.entity_id must be a {entity_domain} entity id (a {domain} helper is a {entity_domain}), not {entity_id!r}")
        if entity_id in seen:
            raise ValueError(f"{what}.entity_id {entity_id} is already another helper's")
        seen.add(entity_id)
        title = item.get("title")
        if title is None:
            title = entity_id.split(".", 1)[1]
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"{what}.title must be a non-empty string")
        raw = item.get("options")
        if not isinstance(raw, dict):
            raise ValueError(f"{what}.options must be an object")
        options = (_trend_options if domain == "trend" else _derivative_options)(raw, f"{what}.options", valid_entity_id)
        source = options["entity_id"] if domain == "trend" else options["source"]
        if source == entity_id:
            raise ValueError(f"{what}: a helper cannot compute from itself ({entity_id})")
        found.append(Helper(index, domain, entity_id, title.strip(), options, source))
    return found


def _trend_options(raw: dict[str, Any], what: str, valid_entity_id: Callable[[str], bool]) -> dict[str, Any]:
    _refuse_unknown(raw, ("entity_id", "attribute", "invert", "max_samples", "min_samples", "min_gradient", "sample_duration"), what)
    options: dict[str, Any] = {"entity_id": _entity(raw.get("entity_id"), f"{what}.entity_id", valid_entity_id)}
    attribute = raw.get("attribute")
    if attribute is not None:
        if not isinstance(attribute, str) or not attribute:
            raise ValueError(f"{what}.attribute must be a non-empty string")
        options["attribute"] = attribute
    invert = raw.get("invert", False)
    if not isinstance(invert, bool):
        raise ValueError(f"{what}.invert must be true or false")
    options["invert"] = invert
    options["max_samples"] = _integer(raw.get("max_samples", 2), f"{what}.max_samples", minimum=2)
    options["min_samples"] = _integer(raw.get("min_samples", 2), f"{what}.min_samples", minimum=2)
    options["min_gradient"] = _number(raw.get("min_gradient", 0.0), f"{what}.min_gradient")
    options["sample_duration"] = _number(raw.get("sample_duration", 0), f"{what}.sample_duration", minimum=0)
    return options


def _derivative_options(raw: dict[str, Any], what: str, valid_entity_id: Callable[[str], bool]) -> dict[str, Any]:
    _refuse_unknown(raw, ("source", "round", "time_window", "unit_prefix", "unit_time", "max_sub_interval"), what)
    options: dict[str, Any] = {
        "source": _entity(raw.get("source"), f"{what}.source", valid_entity_id),
        "round": _integer(raw.get("round", 2), f"{what}.round", minimum=0, maximum=6),
        "time_window": _duration(raw.get("time_window", 0), f"{what}.time_window"),
    }
    unit_prefix = raw.get("unit_prefix")
    if unit_prefix is not None:
        if unit_prefix not in UNIT_PREFIXES:
            raise ValueError(f"{what}.unit_prefix must be one of {', '.join(UNIT_PREFIXES)}, not {unit_prefix!r}")
        options["unit_prefix"] = unit_prefix
    unit_time = raw.get("unit_time", "h")
    if unit_time not in UNIT_TIMES:
        raise ValueError(f"{what}.unit_time must be one of {', '.join(UNIT_TIMES)}, not {unit_time!r}")
    options["unit_time"] = unit_time
    if (interval := raw.get("max_sub_interval")) is not None:
        options["max_sub_interval"] = _duration(interval, f"{what}.max_sub_interval")
    return options


def _refuse_unknown(raw: dict[str, Any], known: tuple[str, ...], what: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in known)
    if unknown:
        raise ValueError(f"{what} has unknown keys {', '.join(unknown)}; the known ones are {', '.join(known)}")


def _entity(value: Any, what: str, valid_entity_id: Callable[[str], bool]) -> str:
    if not isinstance(value, str) or not valid_entity_id(value):
        raise ValueError(f"{what} must be an entity id, not {value!r}")
    return value


def _number(value: Any, what: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{what} must be a number, not {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{what} must be at least {minimum:g}, not {value!r}")
    return float(value)


def _integer(value: Any, what: str, minimum: int, maximum: int | None = None) -> int:
    number = _number(value, what)
    if number != int(number):
        raise ValueError(f"{what} must be a whole number, not {value!r}")
    if number < minimum or (maximum is not None and number > maximum):
        bound = f"at least {minimum}" if maximum is None else f"from {minimum} to {maximum}"
        raise ValueError(f"{what} must be {bound}, not {value!r}")
    return int(number)


def _duration(value: Any, what: str) -> dict[str, float]:
    """A duration as the flow's duration selector stores it: `{hours, minutes, seconds, ...}`; a number is seconds."""
    if isinstance(value, dict):
        _refuse_unknown(value, _DURATION_KEYS, what)
        parts = {key: _number(part, f"{what}.{key}", minimum=0) for key, part in value.items()}
    else:
        parts = {"seconds": _number(value, what, minimum=0)}
    # Home Assistant refuses an empty one.
    return {key: int(part) if part == int(part) else part for key, part in parts.items()} or {"seconds": 0}
