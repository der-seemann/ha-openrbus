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

from openrbus.errors import ProtocolError
from openrbus.protocol.ble_segments import BleSegmentCodec
from openrbus.protocol.canip import ObjectAddress, build_read
from openrbus.protocol.selector import wrap_canip

from .bridge import BridgeRead, GenericRead, decode_device_type, decode_read_response
from .const import (
    CONF_DYNAMIC_ENABLE_ACTION,
    CONF_GENERATION_ENTITY,
    CONF_PASSKEY,
    CONF_RAW_READ_ACTION,
    CONF_REFRESH_ACTION,
    CONF_RESPONSE_ENTITY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .freshness import is_new_generation, parse_generation

_LOGGER = logging.getLogger(__name__)


def _runtime_frame(node: int, address: ObjectAddress) -> str:
    """Encode one read as the BLE-segmented ESPHome runtime payload."""

    return BleSegmentCodec().encode(wrap_canip(build_read(node, address).encode()))[0].hex()


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
        self.pairing_status_entity = self.response_entity.removesuffix(
            "openrbus_read_raw_response"
        ) + "openrbus_pairing_status"
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
        self.raw_read_action = entry.data.get(CONF_RAW_READ_ACTION)
        self.dynamic_enable_action = entry.data.get(CONF_DYNAMIC_ENABLE_ACTION)
        self.passkey = int(entry.data.get(CONF_PASSKEY) or 0)
        self._runtime_request_id = 10000
        self._dynamic_enabled = False
        self._runtime_authenticated = False
        self._reauth_required = False

    _AUTH_READY_PREFIXES = (
        "gateway_authenticated", "dynamic_session_enabled",
        "openrbus_dynamic_response", "openrbus_read_received", "ready",
    )
    _AUTH_FAILURE_PREFIXES = (
        "gateway_auth_failed", "gateway_connect_failed",
        "gateway_ident_characteristic_missing", "security_request_failed",
        "openrbus_read_timeout", "failed_connect", "failed_pairing",
        "timeout", "unavailable", "invalid_passkey", "unpaired",
    )

    @classmethod
    def _lifecycle_failure(cls, state) -> str | None:
        if state is None:
            return None
        value = str(state.state)
        return value if value.startswith(cls._AUTH_FAILURE_PREFIXES) else None

    async def _async_update_data(self) -> BridgeRead:
        self._cycle_id += 1
        cycle_id = self._cycle_id
        _LOGGER.debug("poll cycle=%s start", cycle_id)
        if getattr(self, "raw_read_action", None):
            result = await self.async_read_object(
                ObjectAddress(0x2001, 0x02), node=0xFF
            )
            return BridgeRead(result.address, result.raw_value, result.value)
        async with self._poll_lock:
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
        lifecycle_error: str | None = None
        service_started = False
        reconnect_seen = False
        previous_generation = self._generation_value(
            self.hass.states.get(self.generation_entity)
        )
        previous_uptime = self._float_state(self.hass.states.get(self.uptime_entity))
        initial_failure = self._lifecycle_failure(
            self.hass.states.get(self.pairing_status_entity)
        )
        if initial_failure is not None:
            self._runtime_authenticated = False
            self._reauth_required = True
            raise UpdateFailed(f"ESPHome OpenRBus lifecycle failed: {initial_failure}")
        _LOGGER.debug(
            "poll cycle=%s arm listener entity=%s previous_generation=%s",
            cycle_id,
            self.generation_entity,
            previous_generation,
        )

        async def response_changed(event: Event[EventStateChangedData]) -> None:
            nonlocal received_state, reconnect_seen, previous_uptime, lifecycle_error
            entity_id = event.data.get("entity_id")
            candidate = event.data.get("new_state")
            if entity_id == self.pairing_status_entity:
                status = str(candidate.state) if candidate is not None else "unavailable"
                if status.startswith(self._AUTH_FAILURE_PREFIXES):
                    lifecycle_error = status
                    self._runtime_authenticated = False
                    self._reauth_required = True
                    response_ready.set()
                return
            if entity_id == self.uptime_entity:
                uptime = self._float_state(candidate)
                if (
                    uptime is not None
                    and previous_uptime is not None
                    and uptime < previous_uptime
                ):
                    reconnect_seen = True
                    self._runtime_authenticated = False
                    self._reauth_required = True
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
            self.hass,
            [self.generation_entity, self.uptime_entity, self.pairing_status_entity],
            response_changed,
        )
        try:
            _LOGGER.debug(
                "poll cycle=%s service_call esphome.%s", cycle_id, self.refresh_action
            )
            async with asyncio.timeout(50):
                service_started = True
                try:
                    await self.hass.services.async_call(
                        "esphome",
                        self.refresh_action,
                        {"passkey": getattr(self, "passkey", 0)},
                        blocking=False,
                    )
                except HomeAssistantError as error:
                    _LOGGER.warning("poll cycle=%s service call failed: %s", cycle_id, error)
                # A response timeout is deliberately not retried in the same
                # cycle.  Gateway auth is an ESPHome automation; triggering a
                # second action while the first is still unwinding can create
                # duplicate CCCD/IdentInfo state and the observed
                # ``gateway_ident_sent`` deadlock.  The next coordinator cycle
                # is the safe retry boundary.
                await asyncio.wait_for(response_ready.wait(), timeout=50)
            if lifecycle_error is not None:
                self._runtime_authenticated = False
                raise UpdateFailed(
                    f"ESPHome OpenRBus lifecycle failed: {lifecycle_error}"
                )
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
            self._runtime_authenticated = False
            self._reauth_required = True
            raise UpdateFailed("ESPHome OpenRBus read timed out") from error
        finally:
            remove()

    async def async_read_object(self, address: ObjectAddress, *, node: int = 0xFF) -> GenericRead:
        """Read one arbitrary raw object through the verified ESPHome bridge.

        The coordinator lock is shared with scheduled polls, so the ESPHome
        transport never receives concurrent requests.  This is intentionally
        read-only and does not add registry semantics to the firmware.
        """

        if not self.raw_read_action:
            raise HomeAssistantError("ESPHome runtime raw-read action is unavailable")
        async with self._poll_lock:
            self._runtime_request_id += 1
            request_id = self._runtime_request_id
            generation_before = self._generation_value(
                self.hass.states.get(self.generation_entity)
            )
            uptime_before = self._float_state(self.hass.states.get(self.uptime_entity))
            reconnect_seen = False
            reconnect_error = False
            event = asyncio.Event()
            received: dict[str, object] = {}

            async def changed(change: Event[EventStateChangedData]) -> None:
                nonlocal reconnect_seen, reconnect_error
                entity_id = change.data.get("entity_id")
                new_state = change.data.get("new_state")
                if entity_id == self.uptime_entity:
                    uptime = self._float_state(new_state)
                    if (
                        uptime is not None
                        and uptime_before is not None
                        and uptime < uptime_before
                    ):
                        reconnect_seen = True
                        reconnect_error = True
                        received.clear()
                        self._runtime_authenticated = False
                        self._reauth_required = True
                        event.set()
                    return
                if (
                    entity_id == self.response_entity
                    and new_state is not None
                    and new_state.state not in {"", "unknown", "unavailable"}
                ):
                    received["response"] = new_state
                if entity_id == self.generation_entity:
                    candidate = self._generation_value(new_state)
                    if candidate is not None and is_new_generation(
                        generation_before, candidate, reset_allowed=False
                    ):
                        received["generation"] = candidate
                if "response" in received and "generation" in received:
                    event.set()

            remove = async_track_state_change_event(
                self.hass,
                [self.generation_entity, self.response_entity, self.uptime_entity],
                changed,
            )
            try:
                if self.dynamic_enable_action and not self._dynamic_enabled:
                    await self.hass.services.async_call(
                        "esphome", self.dynamic_enable_action, {}, blocking=False
                    )
                    self._dynamic_enabled = True
                if self.refresh_action and (
                    not self._runtime_authenticated or self._reauth_required
                ):
                    status = self.hass.states.get(self.pairing_status_entity)
                    initial_failure = self._lifecycle_failure(status)
                    if initial_failure is not None:
                        raise HomeAssistantError(
                            f"ESPHome OpenRBus lifecycle failed: {initial_failure}"
                        )
                    if (
                        self._reauth_required
                        or status is None
                        or not str(status.state).startswith(self._AUTH_READY_PREFIXES)
                    ):
                        auth_ready = asyncio.Event()
                        auth_error: str | None = None

                        async def auth_changed(
                            change: Event[EventStateChangedData],
                        ) -> None:
                            nonlocal auth_error
                            new_state = change.data.get("new_state")
                            state = str(new_state.state) if new_state is not None else "unavailable"
                            if state.startswith(self._AUTH_READY_PREFIXES):
                                auth_ready.set()
                            elif state.startswith(self._AUTH_FAILURE_PREFIXES):
                                auth_error = state
                                auth_ready.set()

                        remove_auth = async_track_state_change_event(
                            self.hass, [self.pairing_status_entity], auth_changed
                        )
                        try:
                            await self.hass.services.async_call(
                                "esphome",
                                self.refresh_action,
                                {"passkey": self.passkey},
                                blocking=False,
                            )
                            current = self.hass.states.get(self.pairing_status_entity)
                            if (
                                not self._reauth_required
                                and current is not None
                                and str(current.state).startswith(self._AUTH_READY_PREFIXES)
                            ):
                                auth_ready.set()
                            async with asyncio.timeout(20):
                                await auth_ready.wait()
                            if auth_error is not None:
                                raise HomeAssistantError(
                                    f"ESPHome OpenRBus lifecycle failed: {auth_error}"
                                )
                            self._reauth_required = False
                        finally:
                            remove_auth()
                    self._runtime_authenticated = True
                await self.hass.services.async_call(
                    "esphome",
                    self.raw_read_action,
                    {
                        "request_id": request_id,
                        "frame": _runtime_frame(node, address),
                    },
                    blocking=False,
                )
                async with asyncio.timeout(25):
                    await event.wait()
                if reconnect_error:
                    raise HomeAssistantError(
                        "ESPHome reconnected during OpenRBus raw read; retry required"
                    )
                state = received.get("response")
                if state is None or state.state in {"", "unknown", "unavailable"}:
                    raise HomeAssistantError("ESPHome raw response is unavailable")
                try:
                    return decode_read_response(state.state, node=node, address=address)
                except (ProtocolError, ValueError) as error:
                    raise HomeAssistantError(str(error)) from error
            except (TimeoutError, HomeAssistantError):
                self._runtime_authenticated = False
                raise
            finally:
                remove()

    async def async_read_objects(
        self, addresses: tuple[ObjectAddress, ...], *, node: int = 0xFF
    ) -> tuple[GenericRead | HomeAssistantError, ...]:
        """Read a bounded batch sequentially and retain per-object failures."""

        results: list[GenericRead | HomeAssistantError] = []
        for address in addresses:
            try:
                results.append(await self.async_read_object(address, node=node))
            except HomeAssistantError as error:
                results.append(error)
        return tuple(results)

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
