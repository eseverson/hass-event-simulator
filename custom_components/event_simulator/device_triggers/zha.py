"""ZHA's device triggers (#48): a press is a `zha_event` whose data identifies the device (`device_ieee`) and what happened (the quirk's `device_automation_triggers` entry for the trigger's `(type, subtype)`, e.g. `{endpoint_id: 1, command: remote_button_short_press}`).

`export` reads that map through ZHA's own lookup, `device_trigger._get_device_trigger_data` (the live device, else ZHA's trigger cache), so the live side and the sandbox see exactly what ZHA's trigger would attach with. It is private to ZHA: if it is gone or changed, the device is `Unreadable` rather than guessed at.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

EVENT_TYPE = "zha_event"


def export(hass: HomeAssistant, device_id: str) -> dict[str, Any]:
    """`{ieee, triggers: [{type, subtype, data}]}` for one device; `KeyError` when ZHA has no such device."""
    from . import Unreadable  # noqa: PLC0415 - the package imports this module

    try:
        from homeassistant.components.zha.device_trigger import _get_device_trigger_data  # noqa: PLC0415, PLC2701 - only for zha device triggers
    except ImportError as err:
        raise Unreadable(f"ZHA's device trigger lookup is not available ({err})") from err
    try:
        ieee, triggers = _get_device_trigger_data(hass, device_id)
    except (TypeError, ValueError) as err:
        raise Unreadable(f"ZHA's device trigger lookup failed ({type(err).__name__}: {err})") from err
    try:
        return {
            "ieee": str(ieee),
            "triggers": [{"type": str(kind), "subtype": str(subtype), "data": dict(data)} for (kind, subtype), data in triggers.items()],
        }
    except (AttributeError, TypeError, ValueError) as err:
        raise Unreadable(f"ZHA's device trigger data has an unexpected shape ({type(err).__name__}: {err})") from err


def kinds(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Each trigger of the device as the `zha_event` it matches: the device's IEEE plus the quirk's data, as ZHA's trigger builds its event filter."""
    return [
        {"type": t["type"], "subtype": t["subtype"], "event_type": EVENT_TYPE, "match": {"device_ieee": payload["ieee"], **t["data"]}}
        for t in payload["triggers"]
    ]
