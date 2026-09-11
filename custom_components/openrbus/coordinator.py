"""Data coordinator for the verified read-only ESPHome bridge."""

from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
from .freshness import is_new_generation, parse_generation

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
        self.uptime_entity = (
            self.response_entity.removesuffix("openrbus_read_raw_response") + "uptime"
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
        service_started = False
        reconnect_seen = False
        previous_generation = self._generation_value(
            self.hass.states.get(self.generation_entity)
        )
        previous_uptime = self._float_state(self.hass.states.get(self.uptime_entity))
        _LOGGER.debug(
            "poll cycle=%s arm listener entity=%s previous_generation=%s",
            cycle_id,
            self.generation_entity,
            previous_generation,
        )

        async def response_changed(event: Event[EventStateChangedData]) -> None:
            nonlocal received_state, reconnect_seen, previous_uptime
            entity_id = event.data.get("entity_id")
            candidate = event.data.get("new_state")
            if entity_id == self.uptime_entity:
                uptime = self._float_state(candidate)
                if (
                    uptime is not None
                    and previous_uptime is not None
                    and uptime < previous_uptime
                ):
                    reconnect_seen = True
                    _LOGGER.debug(
                        "poll cycle=%s detected ESP uptime reset %.0f -> %.0f",
                        cycle_id,
                        previous_uptime,
                        uptime,
                    )
                if uptime is not None:
                    previous_uptime = uptime
                return
            generation = self._generation_value(candidate)
            if generation is None:
                reconnect_seen = True
                return
            if not service_started:
                return
            if is_new_generation(
                previous_generation, generation, reset_allowed=reconnect_seen
            ):
                received_state = self.hass.states.get(self.response_entity)
                _LOGGER.debug(
                    "poll cycle=%s freshness event generation=%s reset=%s response=%s",
                    cycle_id,
                    generation,
                    reconnect_seen,
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
            self.hass, [self.generation_entity, self.uptime_entity], response_changed
        )
        try:
            _LOGGER.debug(
                "poll cycle=%s service_call esphome.%s", cycle_id, self.refresh_action
            )
            async with asyncio.timeout(50):
                service_started = True
                for attempt in range(2):
                    try:
                        await self.hass.services.async_call(
                            "esphome",
                            self.refresh_action,
                            {"passkey": 0},
                            blocking=False,
                        )
                    except HomeAssistantError as error:
                        _LOGGER.warning(
                            "poll cycle=%s service call failed attempt=%s: %s",
                            cycle_id,
                            attempt + 1,
                            error,
                        )
                    try:
                        await asyncio.wait_for(response_ready.wait(), timeout=25)
                        break
                    except TimeoutError:
                        if attempt == 0:
                            _LOGGER.debug(
                                "poll cycle=%s retrying service call after response timeout",
                                cycle_id,
                            )
                            continue
                        raise
            _LOGGER.debug("poll cycle=%s complete", cycle_id)
            return received_state
        except TimeoutError as error:
            # ESPHome can deliver the state update while HA is reconnecting;
            # use the monotonic marker as a final, race-safe completion check.
            current_generation = self._generation_value(
                self.hass.states.get(self.generation_entity)
            )
            current_uptime = self._float_state(self.hass.states.get(self.uptime_entity))
            if (
                current_uptime is not None
                and previous_uptime is not None
                and current_uptime < previous_uptime
            ):
                reconnect_seen = True
            if is_new_generation(
                previous_generation,
                current_generation,
                reset_allowed=reconnect_seen,
            ):
                _LOGGER.debug(
                    "poll cycle=%s recovered completion generation=%s reset=%s",
                    cycle_id,
                    current_generation,
                    reconnect_seen,
                )
                return self.hass.states.get(self.response_entity)
            _LOGGER.warning("poll cycle=%s timeout after 50s", cycle_id)
            raise UpdateFailed("ESPHome OpenRBus read timed out") from error
        finally:
            remove()

    @staticmethod
    def _generation_value(state) -> int | None:
        if state is None or state.state in {"", "unknown", "unavailable"}:
            return None
        return parse_generation(state.state)

    @staticmethod
    def _float_state(state) -> float | None:
        if state is None or state.state in {"", "unknown", "unavailable"}:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
