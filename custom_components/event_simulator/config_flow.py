"""Config flow: single instance (the manifest's `single_config_entry`), no options."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class EventSimulatorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(title="Event Simulator", data={})
