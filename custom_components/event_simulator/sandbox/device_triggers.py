"""Device triggers of integration-specific platforms, from data the live side read (the scenario's `device_triggers`).

An integration's device trigger reads that integration's runtime state, which the sandbox never sets up. For the integrations listed in `SEEDERS`, the live side ships the little that state holds about each device (`device_triggers.<domain>.export` there) and the seeder puts it where the integration's own, unmodified `device_trigger` module looks for it, so the trigger attaches as it does live.

`device_triggers`: `{domain: {device_id: payload}}`. A domain with no seeder here is left out; `engine._check_platforms` then refuses its device triggers by name.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant


def _parse_zha(payload: Any, what: str) -> dict[str, Any]:
    """`{ieee, triggers: [{type, subtype, data}]}`: what ZHA's device trigger looks up for one device."""
    if not isinstance(payload, dict) or not isinstance(payload.get("ieee"), str):
        raise ValueError(f"{what}.ieee must be a string")
    triggers = payload.get("triggers")
    if not isinstance(triggers, list):
        raise ValueError(f"{what}.triggers must be a list")
    for index, trigger in enumerate(triggers):
        if not (
            isinstance(trigger, dict)
            and isinstance(trigger.get("type"), str)
            and isinstance(trigger.get("subtype"), str)
            and isinstance(trigger.get("data"), dict)
        ):
            raise ValueError(f"{what}.triggers[{index}] must be {{type, subtype, data}} with string type and subtype and an object data")
    return {"ieee": payload["ieee"], "triggers": triggers}


def _seed_zha(hass: HomeAssistant, devices: dict[str, dict[str, Any]]) -> None:
    """ZHA's trigger cache, its fallback when the gateway has no such device (here: no gateway at all)."""
    from homeassistant.components.zha.helpers import get_zha_data  # noqa: PLC0415 - only when a scenario has zha device triggers

    # Only the sandbox child's own throwaway Home Assistant, never the live one. This is the cache ZHA's own `async_setup_entry` (`components/zha/__init__.py`, "Load and cache device trigger information early") fills from the device registry so device triggers attach before, or without, a radio; ZHA's `device_trigger` reads it unmodified.
    cache = get_zha_data(hass).device_trigger_cache
    for device_id, payload in devices.items():
        cache[device_id] = (payload["ieee"], {(t["type"], t["subtype"]): t["data"] for t in payload["triggers"]})


_PARSERS: dict[str, Callable[[Any, str], dict[str, Any]]] = {"zha": _parse_zha}
SEEDERS: dict[str, Callable[[HomeAssistant, dict[str, dict[str, Any]]], None]] = {"zha": _seed_zha}


def parse_device_triggers(value: Any) -> dict[str, dict[str, Any]]:
    """The scenario's `device_triggers`, checked; `ValueError` says what is wrong."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("device_triggers must map integration domains to {device_id: data}")
    parsed: dict[str, dict[str, Any]] = {}
    for domain, devices in value.items():
        if not isinstance(devices, dict):
            raise ValueError(f"device_triggers.{domain} must map device ids to their data")
        if (parse := _PARSERS.get(domain)) is None:
            continue
        parsed[domain] = {str(device_id): parse(payload, f"device_triggers.{domain}.{device_id}") for device_id, payload in devices.items()}
    return parsed


def seed(hass: HomeAssistant, device_triggers: dict[str, dict[str, Any]]) -> None:
    """Hand each integration its devices' data, before any automation attaches."""
    for domain, devices in device_triggers.items():
        if devices:
            SEEDERS[domain](hass, devices)
