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
    CONF_GENERATION_ENTITY,
    CONF_REFRESH_ACTION,
    CONF_RESPONSE_ENTITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .freshness import is_new_generation

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
        self.generation_entity = entry.data.get(CONF_GENERATION_ENTITY) or (
            self.response_entity.removesuffix("openrbus_read_raw_response")
            + "openrbus_read_generation"
        )
        self._poll_lock = asyncio.Lock()
        self._cycle_id = 0
        configured_action = entry.data.get(CONF_REFRESH_ACTION)
        available_actions = hass.services.async_services().get("esphome", {})
        candidates = sorted(
            name for name in available_actions if name.endswith("openrbus_gateway_auth")
        )
        self.refresh_action = configured_action or (
            candidates[0] if len(candidates) == 1 else None
        )

    async def _async_update_data(self) -> BridgeRead:
        self._cycle_id += 1
        cycle_id = self._cycle_id
        async with self._poll_lock:
            _LOGGER.debug("poll cycle=%s start", cycle_id)
            state = await self._async_request_live_read(cycle_id)
        if state is None or state.state in {"", "unknown", "unavailable"}:
            raise UpdateFailed("ESPHome OpenRBus response is unavailable")
        try:
            return decode_device_type(state.state)
        except (ValueError, TypeError) as error:
            raise UpdateFailed("ESPHome OpenRBus response is invalid") from error

    async def _async_request_live_read(self, cycle_id: int):
        if not self.refresh_action:
            return self.hass.states.get(self.response_entity)
        response_ready = asyncio.Event()
        received_state = None
        previous_generation = self._generation_value(
            self.hass.states.get(self.generation_entity)
        )
        _LOGGER.debug(
            "poll cycle=%s arm listener entity=%s previous_generation=%s",
            cycle_id,
            self.generation_entity,
            previous_generation,
        )

        async def response_changed(event: Event[EventStateChangedData]) -> None:
            nonlocal received_state
            candidate = event.data.get("new_state")
            generation = self._generation_value(candidate)
            if is_new_generation(previous_generation, generation):
                received_state = self.hass.states.get(self.response_entity)
                _LOGGER.debug(
                    "poll cycle=%s freshness event generation=%s response=%s",
                    cycle_id,
                    generation,
                    received_state.state if received_state else None,
                )
                response_ready.set()
            elif candidate is not None and candidate.state not in {
                "",
                "unknown",
                "unavailable",
            }:
                _LOGGER.debug(
                    "poll cycle=%s ignored non-fresh state entity=%s state=%s",
                    cycle_id,
                    self.generation_entity,
                    candidate.state,
                )

        remove = async_track_state_change_event(
            self.hass, [self.generation_entity], response_changed
        )
        try:
            _LOGGER.debug(
                "poll cycle=%s service_call esphome.%s", cycle_id, self.refresh_action
            )
            async with asyncio.timeout(50):
                await self.hass.services.async_call(
                    "esphome",
                    self.refresh_action,
                    {"passkey": 0},
                    blocking=False,
                )
                await response_ready.wait()
            _LOGGER.debug("poll cycle=%s complete", cycle_id)
            return received_state
        except TimeoutError as error:
            _LOGGER.warning("poll cycle=%s timeout after 50s", cycle_id)
            raise UpdateFailed("ESPHome OpenRBus read timed out") from error
        finally:
            remove()

    @staticmethod
    def _generation_value(state) -> int | None:
        if state is None or state.state in {"", "unknown", "unavailable"}:
            return None
        try:
            return int(float(state.state))
        except (TypeError, ValueError):
            return None

    async def async_shutdown(self) -> None:
        """Release coordinator resources."""
