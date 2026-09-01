"""Data coordinator for OpenRBus."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from openrbus import OpenRBusClient, OpenRBusError, RawObjectClient, ReadOutcome
from openrbus.transport import BleakMessageTransport, ManagedTransport

from .bluetooth import client_factory
from .const import (
    CONF_ENABLE_WRITES,
    DEFAULT_ENABLE_WRITES,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    INITIAL_NODE,
    INITIAL_REGISTERS,
)

_LOGGER = logging.getLogger(__name__)


class OpenRBusCoordinator(DataUpdateCoordinator[dict[str, ReadOutcome]]):
    """Manage the OpenRBus connection and batched polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator without opening a BLE connection."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        address = entry.data[CONF_ADDRESS]
        enable_writes = entry.options.get(
            CONF_ENABLE_WRITES,
            entry.data.get(CONF_ENABLE_WRITES, DEFAULT_ENABLE_WRITES),
        )
        ble_transport = BleakMessageTransport(
            address,
            client_factory=client_factory(hass),
        )
        self._transport = ManagedTransport(ble_transport)
        raw_client = RawObjectClient(self._transport)
        self.client = OpenRBusClient(
            raw_client,
            enable_writes=bool(enable_writes),
        )

    async def _async_update_data(self) -> dict[str, ReadOutcome]:
        """Read the first safe identification batch."""
        try:
            results = await self.client.read_many(
                INITIAL_NODE,
                INITIAL_REGISTERS,
            )
        except OpenRBusError as error:
            raise UpdateFailed("OpenRBus update failed") from error
        except (OSError, RuntimeError, TimeoutError) as error:
            raise UpdateFailed("OpenRBus Bluetooth connection failed") from error
        return {str(result.address): result for result in results}

    async def async_shutdown(self) -> None:
        """Close the managed transport."""
        await self._transport.disconnect()
