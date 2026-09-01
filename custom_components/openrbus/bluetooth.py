"""Home Assistant-aware BLE client adapter for OpenRBus."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant


class HomeAssistantBleakClient:
    """Expose the Bleak methods OpenRBus needs using Home Assistant routing."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        *,
        timeout: float = 20.0,
    ) -> None:
        """Initialize a not-yet-connected client."""
        self._hass = hass
        self._address = address
        self._timeout = timeout
        self._client: BleakClient | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying client is connected."""
        return bool(self._client is not None and self._client.is_connected)

    async def connect(self) -> None:
        """Connect through the local adapter or proxy selected by Home Assistant."""
        if self.is_connected:
            return
        device = async_ble_device_from_address(
            self._hass,
            self._address,
            connectable=True,
        )
        if device is None:
            raise RuntimeError(
                "OpenRBus gateway is not currently available through Bluetooth"
            )
        self._client = await establish_connection(
            BleakClient,
            device,
            self._address,
            max_attempts=3,
            timeout=self._timeout,
        )

    async def disconnect(self) -> None:
        """Disconnect the underlying client."""
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    async def start_notify(
        self,
        characteristic: str,
        callback: Callable[[Any, bytearray], None],
    ) -> None:
        """Start notifications on one characteristic."""
        await self._require_client().start_notify(characteristic, callback)

    async def write_gatt_char(
        self,
        characteristic: str,
        data: bytes,
        *,
        response: bool,
    ) -> None:
        """Write one GATT characteristic."""
        await self._require_client().write_gatt_char(
            characteristic,
            data,
            response=response,
        )

    def _require_client(self) -> BleakClient:
        if self._client is None:
            raise RuntimeError("OpenRBus BLE client is not connected")
        return self._client


def client_factory(hass: HomeAssistant) -> Callable[..., HomeAssistantBleakClient]:
    """Return the synchronous factory expected by BleakMessageTransport."""

    def _create(address: str, *, timeout: float = 20.0) -> HomeAssistantBleakClient:
        return HomeAssistantBleakClient(hass, address, timeout=timeout)

    return _create
