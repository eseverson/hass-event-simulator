"""Named scenarios, persisted with Home Assistant's storage helper.

A saved scenario is what the panel needs to put a simulation back on screen: the
automation config (and the entity it came from, if any), the window, the entities
being tracked, and the editable `initial_states` / `events` timeline in the same
shape `history.async_get_timeline` produces.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.ulid import ulid_now

from .const import DOMAIN
from .history import MAX_ENTITIES, MAX_EVENTS, SimulatorError

STORAGE_KEY = f"{DOMAIN}.scenarios"
STORAGE_VERSION = 1
DATA_STORE: HassKey[ScenarioStore] = HassKey(f"{DOMAIN}_scenarios")

MAX_SCENARIOS = 100
MAX_NAME_LENGTH = 100

__all__ = [
    "DATA_STORE",
    "MAX_EVENTS",
    "MAX_SCENARIOS",
    "SCENARIO_FIELDS",
    "STORAGE_KEY",
    "ScenarioStore",
]


def _aware_iso(value: Any) -> str:
    """A timezone-aware datetime, normalized to an ISO 8601 string."""
    parsed = cv.datetime(value)
    if parsed.tzinfo is None:
        raise vol.Invalid("datetime needs a timezone offset")
    return parsed.isoformat()


_STATE = vol.Schema(
    {vol.Required("state"): cv.string, vol.Optional("attributes", default=dict): dict},
    extra=vol.ALLOW_EXTRA,  # last_changed / last_updated ride along untouched
)

_EVENT = _STATE.extend({vol.Required("at"): _aware_iso, vol.Required("entity_id"): cv.entity_id})

# The body of a scenario, shared with the websocket `save` command's schema.
SCENARIO_FIELDS: dict[vol.Marker, Any] = {
    vol.Required("name"): vol.All(cv.string, vol.Strip, vol.Length(min=1, max=MAX_NAME_LENGTH)),
    vol.Optional("automation_entity_id", default=None): vol.Any(None, cv.entity_id),
    vol.Required("automation"): dict,
    vol.Required("start"): _aware_iso,
    vol.Required("end"): _aware_iso,
    vol.Optional("entity_ids", default=list): [cv.entity_id],
    vol.Optional("initial_states", default=dict): {cv.entity_id: _STATE},
    vol.Optional("events", default=list): [_EVENT],
}

_BODY_KEYS = tuple(marker.schema for marker in SCENARIO_FIELDS)


class ScenarioStore:
    """Load, save and delete named scenarios, keyed by a generated id."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._scenarios: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        self._scenarios = dict(data.get("scenarios", {})) if data else {}

    async def _async_write(self) -> None:
        await self._store.async_save({"scenarios": self._scenarios})

    def summaries(self) -> list[dict[str, Any]]:
        """Every scenario without its timeline, sorted by name."""
        return sorted(
            (_summary(scenario) for scenario in self._scenarios.values()),
            key=lambda item: (item["name"].casefold(), item["created"]),
        )

    def get(self, scenario_id: str) -> dict[str, Any]:
        if (scenario := self._scenarios.get(scenario_id)) is None:
            raise SimulatorError("not_found", f"No saved scenario {scenario_id}")
        return scenario

    async def async_save_scenario(
        self, body: dict[str, Any], scenario_id: str | None = None
    ) -> dict[str, Any]:
        """Create a scenario, or overwrite `scenario_id`; return its summary."""
        _check_limits(body)
        now = dt_util.utcnow().isoformat()
        if scenario_id is None:
            if len(self._scenarios) >= MAX_SCENARIOS:
                raise SimulatorError(
                    "too_many",
                    f"At most {MAX_SCENARIOS} scenarios can be saved; delete one first",
                )
            scenario_id = ulid_now()
            created = now
        else:
            created = self.get(scenario_id)["created"]

        scenario = {
            "scenario_id": scenario_id,
            **{key: body.get(key) for key in _BODY_KEYS},
            "created": created,
            "updated": now,
        }
        self._scenarios[scenario_id] = scenario
        await self._async_write()
        return _summary(scenario)

    async def async_delete(self, scenario_id: str) -> None:
        self.get(scenario_id)
        del self._scenarios[scenario_id]
        await self._async_write()


def _check_limits(body: dict[str, Any]) -> None:
    if _parse(body["end"]) <= _parse(body["start"]):
        raise SimulatorError("invalid_window", "The window must end after it starts")
    if (count := len(body.get("events") or ())) > MAX_EVENTS:
        raise SimulatorError(
            "too_large", f"A scenario holds at most {MAX_EVENTS} events; this one has {count}"
        )
    if (count := len(body.get("entity_ids") or ())) > MAX_ENTITIES:
        raise SimulatorError(
            "too_large",
            f"A scenario tracks at most {MAX_ENTITIES} entities; this one has {count}",
        )


def _parse(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return dt_util.parse_datetime(value, raise_on_error=True)


def _summary(scenario: dict[str, Any]) -> dict[str, Any]:
    return {
        "scenario_id": scenario["scenario_id"],
        "name": scenario["name"],
        "automation_entity_id": scenario["automation_entity_id"],
        "alias": (scenario["automation"] or {}).get("alias"),
        "start": scenario["start"],
        "end": scenario["end"],
        "entity_ids": scenario["entity_ids"],
        "event_count": len(scenario["events"] or ()),
        "created": scenario["created"],
        "updated": scenario["updated"],
    }
