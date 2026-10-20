"""ZHA's device triggers (#48): a press is a `zha_event` whose data identifies the device (`device_ieee`) and what happened (the quirk's `device_automation_triggers` entry for the trigger's `(type, subtype)`, e.g. `{endpoint_id: 1, command: remote_button_short_press}`).

`export` reads that map the way ZHA's own device trigger does (the live device through `helpers.async_get_zha_device_proxy`, else the trigger cache in `helpers.get_zha_data`), so the live side and the sandbox see exactly what ZHA's trigger would attach with. Both are public functions of `homeassistant.components.zha.helpers`; it only reads, and if they are gone or return something else, the device is `Unreadable` rather than guessed at.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

DOMAIN = "zha"
EVENT_TYPE = "zha_event"


def export(hass: HomeAssistant, device_id: str) -> dict[str, Any]:
    """`{ieee, triggers: [{type, subtype, data}]}` for one device; `KeyError` when ZHA has no such device."""
    from . import Unreadable  # noqa: PLC0415 - the package imports this module

    # Answered before asking ZHA: its lookup logs an error for a device the registry lacks and raises StopIteration for one without a zha identifier, and this is asked on every replay.
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not any(domain == DOMAIN for domain, _ in device.identifiers):
        raise KeyError(device_id)
    # ZHA not loaded: it holds no data for any device, so its packages are not imported at all.
    if DOMAIN not in hass.config.components:
        raise KeyError(device_id)
    try:
        from homeassistant.components.zha.helpers import async_get_zha_device_proxy, get_zha_data  # noqa: PLC0415 - only for zha device triggers, only once ZHA is loaded
    except ImportError as err:
        raise Unreadable(f"ZHA's device lookup is not available ({err})") from err
    # No public API exposes the `(type, subtype) -> zha_event data` map (`device_automation`'s `async_get_triggers` returns only type/subtype). This is the lookup ZHA's own device trigger does (`components/zha/device_trigger.py`, `_get_device_trigger_data`), through the public helpers its logbook platform also uses to describe the same events (`components/zha/logbook.py`, `async_describe_zha_event`). Read-only; any failure degrades to `Unreadable`.
    try:
        try:
            zha_device = async_get_zha_device_proxy(hass, device_id).device
        except ValueError:
            # No gateway running (radio unreachable): ZHA's trigger falls back to the cache its `async_setup_entry` fills from the registry; a device missing there is a `KeyError`, as in ZHA.
            ieee, triggers = get_zha_data(hass).device_trigger_cache[device_id]
        else:
            ieee, triggers = zha_device.ieee, zha_device.device_automation_triggers
    except (AttributeError, TypeError, ValueError, StopIteration, RuntimeError) as err:
        raise Unreadable(f"ZHA's device lookup failed ({type(err).__name__}: {err})") from err
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
