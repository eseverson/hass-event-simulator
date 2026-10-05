"""Scenario events packed with shared entity ids and attributes.

Home Assistant's websocket refuses a message over 4 MiB, and a week of a sensor that reports every 20 s is around 30,000 events whose entity id and attributes repeat in every one. So the panel sends a timeline as `{entity_ids, attributes, rows}`, each row `[at, entity index, state, attributes index]`, and the store keeps saved scenarios in the same form (several times smaller on disk too).

Simulate only needs what the replay reads, so its rows stop there. A save has to come back exactly as it went, so its rows may go on: `[at, e, state, a, flags]`, where the flags stand for the fields that almost every event repeats (`last_updated` / `last_changed` equal to `at`, `edited: true`), and `[at, e, state, a, flags, {…}]` with whatever else the event carried. The panel's `packEvents` / `unpackEvents` are the same format.
"""

from __future__ import annotations

import json
from typing import Any

import voluptuous as vol

LAST_UPDATED = 1  # last_updated equals at
LAST_CHANGED = 2  # last_changed equals at
EDITED = 4  # edited: true
_FLAGS = LAST_UPDATED | LAST_CHANGED | EDITED

_PACKED = vol.Schema(
    {
        vol.Required("entity_ids"): [str],
        vol.Required("attributes"): [dict],
        vol.Required("rows"): list,
    }
)


def _is_index(value: Any, size: int) -> bool:
    return type(value) is int and 0 <= value < size


def unpack_events(value: Any) -> list[dict[str, Any]]:
    """The events of a packed timeline; raises vol.Invalid when it is not one.

    Rows that share an attribute dict share the object, which is safe because nothing downstream mutates them.
    """
    packed = _PACKED(value)
    entity_ids, attributes = packed["entity_ids"], packed["attributes"]
    events = []
    for row in packed["rows"]:
        if not isinstance(row, list) or not 4 <= len(row) <= 6:
            raise vol.Invalid("a packed event row is [at, entity, state, attributes, flags?, extra?]")
        at, entity, state, attrs = row[:4]
        flags = row[4] if len(row) > 4 else 0
        extra = row[5] if len(row) > 5 else None
        if not (isinstance(at, str) and isinstance(state, str)):
            raise vol.Invalid("a packed event row needs a string time and state")
        if not (_is_index(entity, len(entity_ids)) and _is_index(attrs, len(attributes))):
            raise vol.Invalid("a packed event row points past entity_ids or attributes")
        if type(flags) is not int or flags & ~_FLAGS or (extra is not None and not isinstance(extra, dict)):
            raise vol.Invalid("a packed event row has bad flags or extra fields")
        event = {"at": at, "entity_id": entity_ids[entity], "state": state, "attributes": attributes[attrs]}
        if flags & LAST_CHANGED:
            event["last_changed"] = at
        if flags & LAST_UPDATED:
            event["last_updated"] = at
        if flags & EDITED:
            event["edited"] = True
        if extra:
            event.update(extra)
        events.append(event)
    return events


def pack_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Events packed losslessly: `unpack_events(pack_events(x)) == x` for events with `at`, `entity_id`, string `state` and `attributes`."""
    entity_ids: list[str] = []
    entity_index: dict[str, int] = {}
    attributes: list[dict[str, Any]] = []
    attr_index: dict[str, int] = {}
    rows = []
    for event in events:
        entity_id = event["entity_id"]
        if (e := entity_index.get(entity_id)) is None:
            e = entity_index[entity_id] = len(entity_ids)
            entity_ids.append(entity_id)
        attrs = event.get("attributes") or {}
        key = json.dumps(attrs, sort_keys=True, default=str)
        if (a := attr_index.get(key)) is None:
            a = attr_index[key] = len(attributes)
            attributes.append(attrs)
        at = event["at"]
        row: list[Any] = [at, e, event["state"], a]
        flags = 0
        extra = {}
        for name, value in event.items():
            if name in ("at", "entity_id", "state", "attributes"):
                continue
            if name == "last_updated" and value == at:
                flags |= LAST_UPDATED
            elif name == "last_changed" and value == at:
                flags |= LAST_CHANGED
            elif name == "edited" and value is True:
                flags |= EDITED
            else:
                extra[name] = value
        if extra:
            row += [flags, extra]
        elif flags:
            row.append(flags)
        rows.append(row)
    return {"entity_ids": entity_ids, "attributes": attributes, "rows": rows}


def event_count(events: Any) -> int:
    """How many events a timeline holds, packed or as a list."""
    if isinstance(events, dict):
        return len(events.get("rows") or ())
    return len(events or ())
