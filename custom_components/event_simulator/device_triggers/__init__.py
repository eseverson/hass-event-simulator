"""Device triggers of integration-specific platforms: the live half of their adapters (#48).

A device trigger of an integration such as ZHA reads that integration's runtime state, which the sandbox never sets up. Each adapter here reads, and only reads, what the integration already holds about a device, as plain JSON:

- `export(hass, device_id) -> payload`, what the sandbox seeds so the integration's own `device_trigger` module attaches as it does live (`sandbox/device_triggers.py` has the other half). Raises `KeyError` when the integration has no such device, `Unreadable` when its data cannot be read on this Home Assistant version.
- `kinds(payload) -> [{type, subtype, event_type, match}]`: which bus event each `(type, subtype)` of the device is (an event of `event_type` whose data contains `match`), for the panel's device lanes.

Nothing outside an adapter module knows about its integration.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import IntegrationNotFound, async_get_integration

from . import zha


class Unreadable(Exception):
    """The integration's data could not be read on this Home Assistant version."""


ADAPTERS: dict[str, Any] = {"zha": zha}
# Device automations of these integrations work from the registry alone (binary_sensor, light, ...): no adapter, no lane.
_GENERIC_TYPE = "entity"


def _device_triggers(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every device trigger in a config with its path (`triggers/0`), including ones nested in actions (`wait_for_trigger`)."""
    if isinstance(node, dict):
        if (
            "device" in (node.get("trigger"), node.get("platform"))
            and isinstance(node.get("domain"), str)
            and isinstance(node.get("device_id"), str)
            and isinstance(node.get("type"), str)
        ):
            yield path, node
            return
        for key, value in node.items():
            yield from _device_triggers(value, f"{path}/{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _device_triggers(value, f"{path}/{index}" if path else str(index))


async def _is_generic(hass: HomeAssistant, domain: str) -> bool:
    try:
        integration = await async_get_integration(hass, domain)
    except IntegrationNotFound:
        return False
    return integration.integration_type == _GENERIC_TYPE


async def _collect(hass: HomeAssistant, config: dict[str, Any]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], set[tuple[str, str]]], list[dict[str, Any]]]:
    """Each (domain, device_id) an adapter covers with its payload, the (type, subtype) pairs the config uses per device, and the device triggers that cannot replay."""
    payloads: dict[tuple[str, str], dict[str, Any]] = {}
    used: dict[tuple[str, str], set[tuple[str, str]]] = {}
    unsupported: list[dict[str, Any]] = []
    # Why a device could not be read, so a second trigger on it is refused the same way without asking again.
    failures: dict[tuple[str, str], tuple[str, str]] = {}
    for path, trigger in _device_triggers(config):
        domain, device_id = trigger["domain"], trigger["device_id"]
        key = (domain, device_id)
        if await _is_generic(hass, domain):
            continue
        if (adapter := ADAPTERS.get(domain)) is None:
            failure = ("no_adapter", f"Device triggers from '{domain}' are not supported yet")
        elif key in payloads:
            failure = None
        elif key in failures:
            failure = failures[key]
        else:
            try:
                payloads[key] = adapter.export(hass, device_id)
                failure = None
            except Unreadable as err:
                failure = failures[key] = ("unreadable", f"'{domain}' trigger data could not be read on this Home Assistant version: {err}")
            except KeyError:
                failure = failures[key] = ("no_data", f"'{domain}' has no trigger data for device {device_id}")
        if failure is not None:
            unsupported.append({"path": path, "domain": domain, "device_id": device_id, "reason": failure[0], "message": failure[1]})
            continue
        used.setdefault(key, set()).add((trigger["type"], str(trigger.get("subtype", ""))))
    return payloads, used, unsupported


async def async_device_lanes(hass: HomeAssistant, config: dict[str, Any]) -> dict[str, Any]:
    """The panel's device lanes for a config: one per device its device triggers name, with the device's kinds (the ones the config uses flagged `used`), and the device triggers that cannot replay."""
    payloads, used, unsupported = await _collect(hass, config)
    device_reg = dr.async_get(hass)
    lanes = []
    for (domain, device_id), payload in payloads.items():
        device = device_reg.async_get(device_id)
        kinds = [{**kind, "used": (kind["type"], kind["subtype"]) in used.get((domain, device_id), set())} for kind in ADAPTERS[domain].kinds(payload)]
        lanes.append(
            {
                "id": f"device:{device_id}",
                "domain": domain,
                "device_id": device_id,
                "name": (device.name_by_user or device.name) if device else device_id,
                "event_type": kinds[0]["event_type"] if kinds else None,
                "kinds": kinds,
            }
        )
    return {"lanes": lanes, "unsupported": unsupported}


async def async_export(hass: HomeAssistant, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The scenario's `device_triggers` for a config: `{domain: {device_id: payload}}` for every device an adapter could read."""
    payloads, _, _ = await _collect(hass, config)
    out: dict[str, dict[str, Any]] = {}
    for (domain, device_id), payload in payloads.items():
        out.setdefault(domain, {})[device_id] = payload
    return out
