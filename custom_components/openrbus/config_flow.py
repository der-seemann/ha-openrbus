"""Config flow for the ESPHome OpenRBus proxy."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_DYNAMIC_DISABLE_ACTION,
    CONF_DYNAMIC_ENABLE_ACTION,
    CONF_GENERATION_ENTITY,
    CONF_RAW_READ_ACTION,
    CONF_REFRESH_ACTION,
    CONF_RESPONSE_ENTITY,
    DOMAIN,
)


class OpenRBusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure the verified HA -> ESPHome -> OpenRBus path."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select the two ESPHome entities used by the read-only coordinator."""
        actions = self.hass.services.async_services().get("esphome", {})
        refresh_actions = sorted(
            name for name in actions if name.endswith("openrbus_gateway_auth")
        )
        raw_actions = sorted(name for name in actions if name.endswith("openrbus_raw_read"))
        enable_actions = sorted(
            name for name in actions if name.endswith("openrbus_enable_dynamic_transport")
        )
        disable_actions = sorted(
            name for name in actions if name.endswith("openrbus_disable_dynamic_transport")
        )
        response_entities = {
            state.entity_id: state.name or state.entity_id
            for state in self.hass.states.async_all("sensor")
            if state.entity_id.endswith("openrbus_read_raw_response")
        }
        generation_entities = {
            state.entity_id: state.name or state.entity_id
            for state in self.hass.states.async_all("sensor")
            if state.entity_id.endswith("openrbus_read_generation")
        }
        if user_input is not None:
            if not refresh_actions:
                return self.async_abort(reason="no_esphome_gateway")
            await self.async_set_unique_id(
                f"esphome_bridge:{user_input[CONF_RESPONSE_ENTITY]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="OpenRBus ESPHome Gateway",
                data={
                    CONF_RESPONSE_ENTITY: user_input[CONF_RESPONSE_ENTITY],
                    CONF_GENERATION_ENTITY: user_input[CONF_GENERATION_ENTITY],
                    CONF_REFRESH_ACTION: refresh_actions[0],
                    CONF_RAW_READ_ACTION: raw_actions[0] if raw_actions else None,
                    CONF_DYNAMIC_ENABLE_ACTION: enable_actions[0] if enable_actions else None,
                    CONF_DYNAMIC_DISABLE_ACTION: disable_actions[0] if disable_actions else None,
                },
            )

        if not response_entities or not generation_entities:
            return self.async_abort(reason="no_esphome_entities")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RESPONSE_ENTITY): vol.In(response_entities),
                    vol.Required(CONF_GENERATION_ENTITY): vol.In(generation_entities),
                }
            ),
        )
