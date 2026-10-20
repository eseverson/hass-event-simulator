"""Named scenarios, persisted with Home Assistant's storage helper.

A saved scenario is what the panel needs to put a simulation back on screen: the
automation config (and the entity it came from, if any), the window, the entities
being tracked, and the editable `initial_states` / `events` timeline in the same
shape `history.async_get_timeline` produces. The events are kept, and sent back by
`get`, packed (see packed.py): the websocket refuses a message over 4 MiB, and a week of
a chatty sensor is several times that as a plain list. A scenario saved in Suggest mode
also keeps its labeled periods and options (`labels`, `suggest_options`); both are
optional and stored only when present, so older scenarios load unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import voluptuous as vol
from voluptuous.humanize import humanize_error

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.ulid import ulid_now

from .const import DOMAIN
from .history import MAX_BUS_EVENTS, MAX_ENTITIES, MAX_EVENTS, MAX_SUGGEST_WINDOW, MAX_WINDOW, SimulatorError, check_window
from .packed import event_count, pack_events, unpack_events
from .suggest.labels import ANCHORS, KINDS

STORAGE_KEY = f"{DOMAIN}.scenarios"
# 2: events packed (version 1 kept them as a plain list).
STORAGE_VERSION = 2

MAX_SCENARIOS = 100
MAX_NAME_LENGTH = 100
# Events across every saved scenario. Each save rewrites the whole store file, so this
# bounds that file (about 25 MB packed) rather than letting 100 week-long scenarios of a
# chatty sensor make it hundreds of megabytes.
MAX_STORED_EVENTS = 500_000
# Labeled periods in one scenario (Suggest mode).
MAX_LABELS = 500
# The longest match tolerance either side of an anchor, in seconds.
MAX_TOLERANCE = 24 * 3600
# The sandbox's helper domains and cap (sandbox/helpers.py ENTITY_DOMAINS, MAX_HELPERS; the sandbox is child-only, so not imported here).
HELPER_DOMAINS = ("trend", "derivative")
MAX_HELPERS = 20

__all__ = [
    "HELPERS_SCHEMA",
    "LABEL_SCHEMA",
    "MAX_EVENTS",
    "MAX_LABELS",
    "MAX_SCENARIOS",
    "MAX_STORED_EVENTS",
    "SCENARIO_FIELDS",
    "STORAGE_KEY",
    "TOLERANCE_SCHEMA",
    "EventSimulatorConfigEntry",
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

# A bus event of a lane (#48): a recorded or edited press or event, `edited` marking the panel's own.
_BUS_EVENT = vol.Schema(
    {
        vol.Required("at"): _aware_iso,
        vol.Required("event_type"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("data", default=dict): dict,
        vol.Optional("edited"): bool,
    }
)

# A lane the panel shifted in time: its offset in ms. Its events, wherever they landed, are in `bus_events`.
_LANE_SHIFT = vol.Schema({vol.Required("offset"): vol.Coerce(int)})

# A scenario's events and bus events, checked one by one in the executor when it is saved (`_checked_timeline`): there can be tens of thousands.
_EVENTS = vol.Schema([_EVENT])
_BUS_EVENTS = vol.Schema([_BUS_EVENT])
# Only the shape of a packed timeline (packed.py) is checked in the websocket schema, which runs on the event loop; its rows are checked when it is unpacked.
_PACKED_SHAPE = vol.Schema(
    {vol.Required("entity_ids"): list, vol.Required("attributes"): list, vol.Required("rows"): list}
)


def _check_label(label: dict[str, Any]) -> dict[str, Any]:
    """A period that ends after it starts; a should-fire one anchored (at its start by default), a must-not one not."""
    if _parse(label["end"]) <= _parse(label["start"]):
        raise vol.Invalid("a label must end after it starts")
    if label["kind"] == "fire":
        return {**label, "anchor": label.get("anchor") or "start"}
    return {key: value for key, value in label.items() if key != "anchor"}


# A labeled period for Suggest mode, in the shape `suggest/subscribe` takes (suggest/labels.py).
LABEL_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("start"): _aware_iso,
            vol.Required("end"): _aware_iso,
            vol.Required("kind"): vol.In(KINDS),
            vol.Optional("anchor"): vol.Any(None, vol.In(ANCHORS)),
        }
    ),
    _check_label,
)

TOLERANCE_SCHEMA = vol.All(vol.Coerce(float), vol.Range(min=0, max=MAX_TOLERANCE))

# Suggest mode's options: how far (seconds) a fire may land before / after an anchor and still match it.
_SUGGEST_OPTIONS = vol.Schema(
    {
        vol.Optional("tolerance_before"): TOLERANCE_SCHEMA,
        vol.Optional("tolerance_after"): TOLERANCE_SCHEMA,
    }
)

# The body of a scenario, shared with the websocket `save` command's schema.
# Trend / Derivative helpers a scenario replays with (`[{domain, entity_id, options, title?}]`, sandbox/helpers.py): the sandbox checks the options.
HELPERS_SCHEMA = vol.All(
    [
        vol.Schema(
            {
                vol.Required("domain"): vol.In(HELPER_DOMAINS),
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("options"): dict,
            },
            extra=vol.ALLOW_EXTRA,
        )
    ],
    vol.Length(max=MAX_HELPERS),
)

SCENARIO_FIELDS: dict[vol.Marker, Any] = {
    vol.Required("name"): vol.All(cv.string, vol.Strip, vol.Length(min=1, max=MAX_NAME_LENGTH)),
    vol.Optional("automation_entity_id", default=None): vol.Any(None, cv.entity_id),
    vol.Required("automation"): dict,
    vol.Required("start"): _aware_iso,
    vol.Required("end"): _aware_iso,
    vol.Optional("entity_ids", default=list): [cv.entity_id],
    vol.Optional("initial_states", default=dict): {cv.entity_id: _STATE},
    # A plain list, or packed as the panel sends it. Each event is checked when it is saved, in the executor (`_checked_timeline`).
    vol.Optional("events", default=list): vol.Any(list, _PACKED_SHAPE),
    # Only sent when a track is shifted, and only stored then.
    vol.Optional("track_shifts"): {cv.entity_id: _TRACK_SHIFT},
    # Suggest mode: the labeled periods and the options, stored only when sent non-empty.
    vol.Optional("labels"): vol.All([LABEL_SCHEMA], vol.Length(max=MAX_LABELS)),
    vol.Optional("suggest_options"): _SUGGEST_OPTIONS,
    # The helpers it replays with (a scenario opened from a suggestion that needs one, #42), stored only when sent non-empty.
    vol.Optional("helpers"): HELPERS_SCHEMA,
    # The lanes' bus events and shifts (#48), sent and stored only when a lane is edited. Each bus event is checked when it is saved, as the events are.
    vol.Optional("bus_events"): list,
    vol.Optional("lane_shifts"): {cv.string: _LANE_SHIFT},
}

_OPTIONAL_KEYS = ("track_shifts", "labels", "suggest_options", "helpers", "bus_events", "lane_shifts")
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
        self._hass = hass
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
        """Create a scenario, or overwrite `scenario_id`; return its summary.

        Refused `invalid_format` when an event or bus event is malformed (they are checked here rather than in the websocket schema, so the work runs in the executor).
        """
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
            _stored_count(other)
            for other_id, other in self._scenarios.items()
            if other_id != scenario_id
        )
        if stored + (count := _stored_count(body)) > MAX_STORED_EVENTS:
            raise SimulatorError(
                "store_full",
                f"Saved scenarios hold at most {MAX_STORED_EVENTS} state changes in all; "
                f"the others hold {stored} and this one has {count}, so delete some first",
            )

        events, bus_events = await self._hass.async_add_executor_job(
            _checked_timeline, body.get("events") or [], body.get("bus_events") or []
        )
        scenario = {
            "scenario_id": scenario_id,
            **{key: body.get(key) for key in _BODY_KEYS},
            **{key: body[key] for key in _OPTIONAL_KEYS if body.get(key)},
            **({"bus_events": bus_events} if bus_events else {}),
            "events": events,
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


# The config entry carries the scenario store as its runtime data.
type EventSimulatorConfigEntry = ConfigEntry[ScenarioStore]


def _check_limits(body: dict[str, Any]) -> None:
    # A scenario saved in Suggest mode (it carries `suggest_options`) may span a Suggest window.
    max_window = MAX_SUGGEST_WINDOW if body.get("suggest_options") else MAX_WINDOW
    check_window(_parse(body["start"]), _parse(body["end"]), max_window)
    if (count := event_count(body.get("events"))) > MAX_EVENTS:
        raise SimulatorError(
            "too_large", f"A scenario holds at most {MAX_EVENTS} events; this one has {count}"
        )
    if (count := len(body.get("bus_events") or ())) > MAX_BUS_EVENTS:
        raise SimulatorError(
            "too_large", f"A scenario holds at most {MAX_BUS_EVENTS} bus events; this one has {count}"
        )
    if (count := len(body.get("entity_ids") or ())) > MAX_ENTITIES:
        raise SimulatorError(
            "too_large",
            f"A scenario tracks at most {MAX_ENTITIES} entities; this one has {count}",
        )


def _checked_timeline(
    events: list[Any] | dict[str, Any], bus_events: list[Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Executor: a scenario's events checked (`_EVENT`, unpacked first when packed) and packed, and its bus events checked (`_BUS_EVENT`)."""
    try:
        if isinstance(events, dict):
            events = unpack_events(events)
        checked = _EVENTS(events)
    except vol.Invalid as err:
        raise SimulatorError("invalid_format", f"Invalid events: {humanize_error(events, err)}") from err
    try:
        checked_bus = _BUS_EVENTS(bus_events)
    except vol.Invalid as err:
        raise SimulatorError("invalid_format", f"Invalid bus_events: {humanize_error(bus_events, err)}") from err
    return pack_events(checked), checked_bus


def _stored_count(scenario: dict[str, Any]) -> int:
    """What a scenario adds to the store file: its state changes and its lanes' bus events."""
    return event_count(scenario.get("events")) + len(scenario.get("bus_events") or ())


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
