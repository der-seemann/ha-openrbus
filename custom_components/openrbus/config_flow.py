"""Config flow for OpenRBus."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import format_mac

from .const import (
    BDR_THERMEA_MANUFACTURER_ID,
    CONF_ENABLE_WRITES,
    CONF_PAIRING_PIN,
    DEFAULT_ENABLE_WRITES,
    DOMAIN,
    TRANSPARENT_SERVICE_UUID,
)


def _supports_openrbus(info: BluetoothServiceInfoBleak) -> bool:
    """Return whether an advertisement is a plausible OpenRBus gateway."""
    if any(
        str(service_uuid).lower() == TRANSPARENT_SERVICE_UUID
        for service_uuid in (info.service_uuids or ())
    ):
        return True

    if BDR_THERMEA_MANUFACTURER_ID in (info.manufacturer_data or {}):
        return True

    # Some BDR Thermea gateways advertise their controller family in the
    # local name but omit the transparent service UUID until GATT discovery.
    name = (info.name or "").casefold()
    return name.startswith("ehc-") or name.startswith("ehc_")


def _unique_id(address: str) -> str:
    """Normalize a Linux BLE address while retaining platform identifiers."""
    try:
        return format_mac(address)
    except ValueError:
        return address.casefold()


def _pairing_pin(value: Any) -> str:
    """Validate the six-digit BLE pairing PIN without converting away zeros."""
    pin = str(value).strip()
    if len(pin) != 6 or not pin.isdecimal():
        raise vol.Invalid("Pairing PIN must contain exactly six digits")
    return pin


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
        self._pairing_pin: str | None = None

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
        """Select a gateway discovered by Home Assistant Bluetooth."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(_unique_id(address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._set_device(self._discovered_devices[address])
            return await self.async_step_credentials()

        # Ask every available HA Bluetooth scanner, including ESPHome proxies,
        # for an active scan before evaluating the current discovery cache.
        await bluetooth.async_request_active_scan(self.hass)

        current_ids = self._async_current_ids(include_ignore=False)
        for info in bluetooth.async_discovered_service_info(
            self.hass,
            connectable=True,
        ):
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
        """Collect the owner-supplied pairing PIN transiently."""
        assert self._address is not None
        if user_input is not None:
            self._pairing_pin = user_input[CONF_PAIRING_PIN]
            return await self.async_step_access_policy()

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({vol.Required(CONF_PAIRING_PIN): _pairing_pin}),
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
            # The pairing PIN deliberately never enters the config entry. The
            # next roadmap step consumes it through the selected ESPHome proxy
            # action and clears this transient field after pairing succeeds.
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
