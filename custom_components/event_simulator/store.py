"""Named scenarios, persisted with Home Assistant's storage helper.

A saved scenario is what the panel needs to put a simulation back on screen: the
automation config (and the entity it came from, if any), the window, the entities
being tracked, and the editable `initial_states` / `events` timeline in the same
shape `history.async_get_timeline` produces. The events are kept, and sent back by
`get`, packed (see packed.py): the websocket refuses a message over 4 MiB, and a week of
a chatty sensor is several times that as a plain list.
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
from .history import MAX_ENTITIES, MAX_EVENTS, SimulatorError, check_window
from .packed import event_count, pack_events, unpack_events

STORAGE_KEY = f"{DOMAIN}.scenarios"
# 2: events packed (version 1 kept them as a plain list).
STORAGE_VERSION = 2
DATA_STORE: HassKey[ScenarioStore] = HassKey(f"{DOMAIN}_scenarios")

MAX_SCENARIOS = 100
MAX_NAME_LENGTH = 100
# Events across every saved scenario. Each save rewrites the whole store file, so this
# bounds that file (about 25 MB packed) rather than letting 100 week-long scenarios of a
# chatty sensor make it hundreds of megabytes.
MAX_STORED_EVENTS = 500_000

__all__ = [
    "DATA_STORE",
    "MAX_EVENTS",
    "MAX_SCENARIOS",
    "MAX_STORED_EVENTS",
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

# A track the panel shifted in time: its offset in ms and the state in effect before all
# of its events (None when there was none). Its events, wherever they landed, are in
# `events`; the panel parks the ones outside the window again on load.
_TRACK_SHIFT = vol.Schema(
    {
        vol.Required("offset"): vol.Coerce(int),
        vol.Optional("base", default=None): vol.Any(None, _STATE),
    }
)

# The body of a scenario, shared with the websocket `save` command's schema.
SCENARIO_FIELDS: dict[vol.Marker, Any] = {
    vol.Required("name"): vol.All(cv.string, vol.Strip, vol.Length(min=1, max=MAX_NAME_LENGTH)),
    vol.Optional("automation_entity_id", default=None): vol.Any(None, cv.entity_id),
    vol.Required("automation"): dict,
    vol.Required("start"): _aware_iso,
    vol.Required("end"): _aware_iso,
    vol.Optional("entity_ids", default=list): [cv.entity_id],
    vol.Optional("initial_states", default=dict): {cv.entity_id: _STATE},
    # A plain list, or packed as the panel sends it.
    vol.Optional("events", default=list): vol.All(vol.Any(list, unpack_events), [_EVENT]),
    # Only sent when a track is shifted, and only stored then.
    vol.Optional("track_shifts"): {cv.entity_id: _TRACK_SHIFT},
}

_OPTIONAL_KEYS = ("track_shifts",)
_BODY_KEYS = tuple(
    marker.schema for marker in SCENARIO_FIELDS if marker.schema not in _OPTIONAL_KEYS
)


class _ScenarioFile(Store[dict[str, Any]]):
    """The store file, migrated forward on load."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        scenarios = dict(old_data.get("scenarios") or {})
        if old_major_version < 2:
            for scenario_id, scenario in scenarios.items():
                events = scenario.get("events")
                if isinstance(events, list):
                    scenarios[scenario_id] = {**scenario, "events": pack_events(events)}
        return {**old_data, "scenarios": scenarios}


class ScenarioStore:
    """Load, save and delete named scenarios, keyed by a generated id.

    `get` returns a scenario with its events packed, as they are stored and as the panel reads them.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = _ScenarioFile(hass, STORAGE_VERSION, STORAGE_KEY)
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
        stored = sum(
            event_count(other["events"])
            for other_id, other in self._scenarios.items()
            if other_id != scenario_id
        )
        if stored + (count := event_count(body.get("events"))) > MAX_STORED_EVENTS:
            raise SimulatorError(
                "store_full",
                f"Saved scenarios hold at most {MAX_STORED_EVENTS} state changes in all; "
                f"the others hold {stored} and this one has {count}, so delete some first",
            )

        scenario = {
            "scenario_id": scenario_id,
            **{key: body.get(key) for key in _BODY_KEYS},
            **{key: body[key] for key in _OPTIONAL_KEYS if body.get(key)},
            "events": pack_events(body.get("events") or []),
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
    check_window(_parse(body["start"]), _parse(body["end"]))
    if (count := event_count(body.get("events"))) > MAX_EVENTS:
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
        "event_count": event_count(scenario["events"]),
        "created": scenario["created"],
        "updated": scenario["updated"],
    }
