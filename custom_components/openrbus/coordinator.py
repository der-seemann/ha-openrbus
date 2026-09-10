"""Data coordinator for the verified read-only ESPHome bridge."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bridge import BridgeRead, decode_device_type
from .const import (
    CONF_REFRESH_ACTION,
    CONF_RESPONSE_ENTITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class OpenRBusCoordinator(DataUpdateCoordinator[BridgeRead]):
    """Decode live responses published by the authenticated Phase-1A proxy."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self.response_entity = entry.data[CONF_RESPONSE_ENTITY]
        configured_action = entry.data.get(CONF_REFRESH_ACTION)
        available_actions = hass.services.async_services().get("esphome", {})
        candidates = sorted(
            name for name in available_actions if name.endswith("openrbus_gateway_auth")
        )
        self.refresh_action = configured_action or (
            candidates[0] if len(candidates) == 1 else None
        )

    async def _async_update_data(self) -> BridgeRead:
        state = await self._async_request_live_read()
        if state is None or state.state in {"", "unknown", "unavailable"}:
            raise UpdateFailed("ESPHome OpenRBus response is unavailable")
        try:
            return decode_device_type(state.state)
        except (ValueError, TypeError) as error:
            raise UpdateFailed("ESPHome OpenRBus response is invalid") from error

    async def _async_request_live_read(self):
        if not self.refresh_action:
            return self.hass.states.get(self.response_entity)
        response_ready = asyncio.Event()
        received_state = None

        async def response_changed(event: Event[EventStateChangedData]) -> None:
            nonlocal received_state
            candidate = event.data["new_state"]
            if candidate is not None and candidate.state not in {
                "",
                "unknown",
                "unavailable",
            }:
                received_state = candidate
                response_ready.set()

        remove = async_track_state_change_event(
            self.hass, [self.response_entity], response_changed
        )
        try:
            async with asyncio.timeout(50):
                await self.hass.services.async_call(
                    "esphome",
                    self.refresh_action,
                    {"passkey": 0},
                    blocking=False,
                )
                await response_ready.wait()
            return received_state
        except TimeoutError as error:
            raise UpdateFailed("ESPHome OpenRBus read timed out") from error
        finally:
            remove()

    async def async_shutdown(self) -> None:
        """Release coordinator resources."""
