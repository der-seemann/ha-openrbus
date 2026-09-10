"""Config flow for OpenRBus."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_ENABLE_WRITES,
    CONF_PAIRING_PIN,
    CONF_REFRESH_ACTION,
    CONF_RESPONSE_ENTITY,
    DEFAULT_ENABLE_WRITES,
    DOMAIN,
    TRANSPARENT_SERVICE_UUID,
)


def _supports_openrbus(info: BluetoothServiceInfoBleak) -> bool:
    """Return whether an advertisement exposes the transparent service."""
    return any(
        str(service_uuid).lower() == TRANSPARENT_SERVICE_UUID
        for service_uuid in (info.service_uuids or ())
    )


def _unique_id(address: str) -> str:
    """Normalize a Linux BLE address while retaining platform identifiers."""
    try:
        return format_mac(address)
    except ValueError:
        return address.casefold()


class OpenRBusOptionsFlow(OptionsFlow):
    """Handle OpenRBus options."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Configure the integration-level write gate."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        enabled = self.config_entry.options.get(
            CONF_ENABLE_WRITES,
            self.config_entry.data.get(CONF_ENABLE_WRITES, DEFAULT_ENABLE_WRITES),
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_ENABLE_WRITES, default=enabled): bool}
            ),
        )


class OpenRBusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an OpenRBus config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient flow state."""
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._title = "OpenRBus"
        self._pending_data: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OpenRBusOptionsFlow:
        """Return the options flow handler."""
        return OpenRBusOptionsFlow()

    async def async_step_bluetooth(
        self,
        discovery_info: BluetoothServiceInfoBleak,
    ) -> ConfigFlowResult:
        """Handle automatic Bluetooth discovery."""
        if not _supports_openrbus(discovery_info):
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(_unique_id(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._set_device(discovery_info)
        self.context["title_placeholders"] = {
            "name": self._title,
            "address": discovery_info.address,
        }
        return await self.async_step_credentials()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select the verified ESPHome bridge or a discovered BLE gateway."""
        if user_input is not None:
            if CONF_RESPONSE_ENTITY in user_input:
                entity_id = user_input[CONF_RESPONSE_ENTITY]
                actions = self.hass.services.async_services().get("esphome", {})
                refresh_actions = sorted(
                    name for name in actions if name.endswith("openrbus_gateway_auth")
                )
                await self.async_set_unique_id(f"esphome_bridge:{entity_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="OpenRBus ESPHome Gateway",
                    data={
                        CONF_RESPONSE_ENTITY: entity_id,
                        CONF_ENABLE_WRITES: False,
                        CONF_REFRESH_ACTION: refresh_actions[0]
                        if len(refresh_actions) == 1
                        else "",
                    },
                )
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(_unique_id(address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._set_device(self._discovered_devices[address])
            return await self.async_step_credentials()

        response_entities = {
            state.entity_id: state.name or state.entity_id
            for state in self.hass.states.async_all("sensor")
            if state.entity_id.endswith("openrbus_read_raw_response")
        }
        if response_entities:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {vol.Required(CONF_RESPONSE_ENTITY): vol.In(response_entities)}
                ),
            )

        current_ids = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if _unique_id(info.address) in current_ids or not _supports_openrbus(info):
                continue
            self._discovered_devices[info.address] = info

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        labels = {
            address: f"{info.name or 'OpenRBus'} ({address})"
            for address, info in self._discovered_devices.items()
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(labels)}),
        )

    async def async_step_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Collect the owner-supplied pairing PIN."""
        assert self._address is not None
        if user_input is not None:
            self._pending_data[CONF_PAIRING_PIN] = user_input[CONF_PAIRING_PIN]
            return await self.async_step_access_policy()

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIRING_PIN): vol.All(
                        str,
                        vol.Length(min=1, max=64),
                    )
                }
            ),
            description_placeholders={
                "name": self._title,
                "address": self._address,
            },
        )

    async def async_step_access_policy(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose read-only or explicitly enable the client write gate."""
        if user_input is not None:
            self._pending_data[CONF_ENABLE_WRITES] = bool(
                user_input[CONF_ENABLE_WRITES]
            )
            return self.async_create_entry(
                title=self._title,
                data=self._pending_data,
            )

        return self.async_show_form(
            step_id="access_policy",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_WRITES,
                        default=DEFAULT_ENABLE_WRITES,
                    ): bool
                }
            ),
        )

    def _set_device(self, info: BluetoothServiceInfoBleak) -> None:
        """Retain one selected Bluetooth advertisement."""
        self._address = info.address
        self._title = info.name or f"OpenRBus {info.address}"
        self._pending_data = {CONF_ADDRESS: info.address}
