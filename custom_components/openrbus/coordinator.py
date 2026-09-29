"""OpenRBus coordinator with exactly two supported transport backends."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from openrbus.catalog import catalog_for_node
from openrbus.client import WritePlan
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.errors import OpenRBusError
from openrbus.inventory import DeviceInventory, ObjectCapability, ObjectSupport
from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import AccessOperation, Registry

from .bridge import BridgeRead, GenericRead
from .const import (
    BACKEND_NATIVE,
    BACKEND_THIN_RPC,
    CONF_ACCESS_LEVEL,
    CONF_BACKEND,
    CONF_BLE_DEVICE,
    CONF_BLE_SOURCE,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_ENTITY_OVERRIDES,
    CONF_INVALID_VALUE_DISABLE_AFTER,
    CONF_LANGUAGE,
    CONF_PAIR_ACTION,
    CONF_PASSKEY,
    CONF_POLL_FAST,
    CONF_POLL_SLOW,
    CONF_POLL_STANDARD,
    CONF_READ_ACCESS_LEVEL,
    CONF_THIN_CONTROLLER,
    CONF_THIN_DIAGNOSTICS_SERVICE,
    CONF_THIN_POLL_SERVICE,
    CONF_THIN_REQUEST_HANDLE,
    CONF_THIN_REQUEST_SERVICE,
    CONF_THIN_RESPONSE_HANDLE,
    CONF_THIN_TARGET_ADDRESS_TYPE,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
    CONF_ZONE_OVERRIDES,
    DEFAULT_INVALID_VALUE_DISABLE_AFTER,
    DEFAULT_POLL_INTERVALS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .transport import (
    NativeBluetoothBackend,
    ThinRpcBackend,
    _read_error_class,
    _read_error_subtype,
    controller_prefix,
    resolve_thin_rpc_capability,
)
from .validity import RegisterValidityTracker
from .zones import (
    ZONE_FRIENDLY_NAME_INDEX,
    ZONE_FUNCTION_INDEX,
    ZoneProfile,
    normalized_overrides,
)

_LOGGER = logging.getLogger(__name__)
_COORDINATOR_INSTANCE_IDS = itertools.count(1)
_POLL_BATCH_SIZE = 32
_DIAGNOSTIC_ITEM_FAILURE_LIMIT = 16
_ERROR_CLASSES = ("abort", "item", "batch", "decode", "correlation", "session")
_DIAGNOSTIC_COUNTER_MAX = 2_147_483_647


def _tag_poll_error(
    error: HomeAssistantError, error_class: str, error_subtype: str | None = None
) -> HomeAssistantError:
    if error_class in _ERROR_CLASSES:
        error._openrbus_error_class = error_class  # type: ignore[attr-defined]
    allowed = (
        {"not_ready", "link_lost", "timeout", "not_secure", "transport"}
        if error_class == "session"
        else {"malformed", "abort", "fallback"}
    )
    if error_subtype in allowed:
        error._openrbus_error_subtype = error_subtype  # type: ignore[attr-defined]
    return error


def _should_poll_registry_entries(entries: tuple[Any, ...]) -> bool:
    """Return whether at least one projection for a row is enabled.

    Legacy sensor entries can coexist briefly with their typed replacement in
    HA's entity registry.  A disabled legacy entry must not suppress polling
    for an enabled Number/Select/Switch projection.  With no registry entry
    yet (during initial platform setup), keep the row pollable.
    """

    return not entries or any(not entry.disabled for entry in entries)


def _configured_entry_data(entry: ConfigEntry) -> dict[str, Any]:
    configured = dict(entry.data)
    configured.update(getattr(entry, "options", {}))
    return configured


class OpenRBusCoordinator(DataUpdateCoordinator[BridgeRead]):
    """Coordinate Core reads through one immutable selected backend."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        thin_key_provider=None,
        thin_profile=None,
        thin_frame_trace: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        configured = _configured_entry_data(entry)
        backend = configured.get(CONF_BACKEND)
        if backend not in {BACKEND_NATIVE, BACKEND_THIN_RPC}:
            raise HomeAssistantError(
                "Unsupported OpenRBus transport; choose Local Bluetooth or ESPHome Thin-RPC"
            )
        self._entry_id = entry.entry_id
        self._instance_id = next(_COORDINATOR_INSTANCE_IDS)
        self._diagnostic_error_counts = dict.fromkeys(_ERROR_CLASSES, 0)
        self._diagnostic_item_failures: list[dict[str, int | str]] = []
        self._diagnostic_item_failures: list[dict[str, int | str]] = []
        self._diagnostic_poll_count = 0
        self.backend_mode = backend
        def configured_level(key: str, fallback: object = 1) -> int:
            try:
                return max(1, min(3, int(configured.get(key, fallback))))
            except (TypeError, ValueError):
                return 1

        # A legacy single policy was both read and write policy.  New entries
        # carry explicit independent levels.  Core is requested at the
        # highest needed level, but this does not grant writes by itself.
        self.configured_read_access_level = configured_level(
            CONF_READ_ACCESS_LEVEL, configured.get(CONF_ACCESS_LEVEL, 1)
        )
        self.configured_write_access_level = configured_level(
            CONF_WRITE_ACCESS_LEVEL, configured.get(CONF_ACCESS_LEVEL, 1)
        )
        self.configured_access_level = max(
            self.configured_read_access_level, self.configured_write_access_level
        )
        self.write_enabled = bool(configured.get(CONF_WRITE_ENABLED, False))
        self.language = str(configured.get(CONF_LANGUAGE, "de"))
        self.poll_intervals = {
            "fast": timedelta(
                seconds=int(
                    configured.get(
                        CONF_POLL_FAST, DEFAULT_POLL_INTERVALS[CONF_POLL_FAST]
                    )
                )
            ),
            "standard": timedelta(
                seconds=int(
                    configured.get(
                        CONF_POLL_STANDARD, DEFAULT_POLL_INTERVALS[CONF_POLL_STANDARD]
                    )
                )
            ),
            "slow": timedelta(
                seconds=int(
                    configured.get(
                        CONF_POLL_SLOW, DEFAULT_POLL_INTERVALS[CONF_POLL_SLOW]
                    )
                )
            ),
        }
        try:
            invalid_after = int(
                configured.get(
                    CONF_INVALID_VALUE_DISABLE_AFTER,
                    DEFAULT_INVALID_VALUE_DISABLE_AFTER,
                )
            )
        except (TypeError, ValueError):
            invalid_after = DEFAULT_INVALID_VALUE_DISABLE_AFTER
        self.invalid_value_disable_after = timedelta(seconds=max(60, invalid_after))
        # Core/backend authorization is authoritative.  A higher configured
        # value is never treated as proof of effective access.
        self.effective_access_level: int | None = (
            1 if self.configured_access_level == 1 else None
        )
        self.devices: tuple[DeviceIdentity, ...] = ()
        self.inventories: tuple[DeviceInventory, ...] = ()
        self.effective_access_levels: dict[int, int] = {}
        # This is populated by read-only CP020/name reads after discovery.
        # Entries are keyed by protocol identity, never display names.
        self.zone_profiles: dict[tuple[int, int], ZoneProfile] = {}
        self.zone_overrides = normalized_overrides(configured.get(CONF_ZONE_OVERRIDES))
        self.entity_overrides = normalized_overrides(configured.get(CONF_ENTITY_OVERRIDES))
        if backend == BACKEND_NATIVE:
            self._backend = NativeBluetoothBackend(
                hass,
                address=str(configured.get(CONF_BLE_DEVICE) or ""),
                source=str(configured.get(CONF_BLE_SOURCE) or "") or None,
                passkey=int(configured.get(CONF_PASSKEY) or 0) or None,
                access_level=self.configured_access_level,
                write_enabled=self.write_enabled,
                key_provider=thin_key_provider,
            )
            return
        request_service = (
            configured.get(CONF_THIN_REQUEST_SERVICE) or "openrbus_gatt_rpc_request"
        )
        poll_service = (
            configured.get(CONF_THIN_POLL_SERVICE) or "openrbus_gatt_rpc_poll"
        )
        diagnostics_service = (
            configured.get(CONF_THIN_DIAGNOSTICS_SERVICE)
            or "openrbus_gatt_rpc_diagnostics"
        )
        controller = str(configured.get(CONF_THIN_CONTROLLER) or "default")
        self._backend = ThinRpcBackend(
            hass,
            controller_id=controller,
            request_handle=configured.get(CONF_THIN_REQUEST_HANDLE),
            response_handle=configured.get(CONF_THIN_RESPONSE_HANDLE),
            preferred_prefix=None
            if controller == "default"
            else controller_prefix(controller),
            request_service=request_service,
            poll_service=poll_service,
            diagnostics_service=diagnostics_service,
            pair_action=configured.get(CONF_PAIR_ACTION),
            passkey=int(configured.get(CONF_PASSKEY) or 0),
            target_address=str(configured.get(CONF_BLE_DEVICE) or "") or None,
            target_address_type=configured.get(CONF_THIN_TARGET_ADDRESS_TYPE),
            access_level=self.configured_access_level,
            write_enabled=self.write_enabled,
            capability=resolve_thin_rpc_capability(
                hass,
                request_service=request_service,
                poll_service=poll_service,
                diagnostics_service=diagnostics_service,
            ),
            key_provider=thin_key_provider,
            profile=thin_profile,
            frame_trace=thin_frame_trace,
        )

    async def _async_update_data(self) -> BridgeRead:
        self._diagnostic_poll_count = min(
            _DIAGNOSTIC_COUNTER_MAX, self._diagnostic_poll_count + 1
        )
        try:
            result = await self.async_read_object(
                ObjectAddress(0x2001, 0x02), node=0xFF
            )
        except Exception as error:
            error_class = _read_error_class(error)
            self._diagnostic_error_counts[error_class] = min(
                _DIAGNOSTIC_COUNTER_MAX,
                self._diagnostic_error_counts[error_class] + 1,
            )
            raise
        return BridgeRead(result.address, result.raw_value, result.value)

    async def async_start(self) -> None:
        started = asyncio.get_running_loop().time()
        try:
            await self._backend.async_start()
            effective = getattr(self._backend, "effective_access_level", None)
            if effective is not None:
                self.effective_access_level = int(effective)
            # Backends normally call Core discovery, but resolve once more at
            # the lifecycle boundary so custom/legacy backends cannot omit the
            # evidence-backed family metadata needed by catalog_for_node().
            # This is a pure identity projection; it does not probe or infer.
            discovered = await self._backend.async_discover_devices()
            self.devices = tuple(
                resolve_device_identity(identity)
                if isinstance(identity, DeviceIdentity)
                else identity
                for identity in discovered
            )
            self.effective_access_levels = dict(
                getattr(self._backend, "effective_access_levels", {})
            )
            self.inventories = tuple(
                self._inventory_for(identity) for identity in self.devices
            )
            await self._async_discover_zone_profiles()
        except asyncio.CancelledError:
            setup_metrics = getattr(self._backend, "setup_metrics", None)
            record_cancel = getattr(setup_metrics, "record_setup_cancellation", None)
            if callable(record_cancel):
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                record_cancel(elapsed)
            raise

    async def _async_discover_zone_profiles(self) -> None:
        """Read ZoneFunction plus custom labels for advertised zone slots.

        This is strictly read-only and best-effort.  CP020 is manufacturer
        evidence for active/inactive and heating/DHW semantics.  A transport
        error leaves that slot absent (and therefore disabled by default),
        which is safer than exposing every static SCB-10 zone array.
        """

        profiles: dict[tuple[int, int], ZoneProfile] = {}
        for runtime_node in self.inventories or self.devices:
            identity = getattr(runtime_node, "identity", runtime_node)
            node = getattr(identity, "node", None)
            if not isinstance(node, int):
                continue
            rows = catalog_for_node(runtime_node, Registry.load_default())
            slots = sorted(
                {
                    item.address.subindex
                    for item in rows
                    if item.address.index == ZONE_FUNCTION_INDEX
                }
            )
            for slot in slots:
                try:
                    function_read = await self.async_read_object(
                        ObjectAddress(ZONE_FUNCTION_INDEX, slot), node=node
                    )
                except (HomeAssistantError, OpenRBusError, TypeError, ValueError):
                    continue
                if type(function_read.value) is not int:
                    continue
                friendly_name: str | None = None
                try:
                    name_read = await self.async_read_object(
                        ObjectAddress(ZONE_FRIENDLY_NAME_INDEX, slot), node=node
                    )
                    if isinstance(name_read.value, str):
                        friendly_name = name_read.value.strip("\x00 ") or None
                except (HomeAssistantError, OpenRBusError, TypeError, ValueError):
                    pass
                profile = ZoneProfile(node, slot, function_read.value, friendly_name)
                profiles[(node, slot)] = profile
        self.zone_profiles = profiles

    @staticmethod
    def _inventory_for(identity: DeviceIdentity) -> DeviceInventory:
        """Project Core discovery evidence into its inventory/catalog model."""

        inventory = DeviceInventory(identity=identity)
        for capability in getattr(identity, "capabilities", ()) or ():
            inventory.record(
                ObjectCapability(
                    address=capability.address, status=ObjectSupport.SUPPORTED
                )
            )
        # Identity matching is intentionally conservative: without explicit
        # external evidence Core returns UNKNOWN, while capability addresses
        # remain valid node-scoped evidence for catalog_for_node().
        try:
            inventory.resolve_registry(Registry.load_default())
        except (AttributeError, TypeError, ValueError):
            pass
        return inventory

    async def async_shutdown(self) -> None:
        await self._backend.async_stop()
        await super().async_shutdown()

    async def async_read_object(
        self, address: ObjectAddress, *, node: int = 0xFF
    ) -> GenericRead:
        return await self._backend.async_read_object(address, node=node)

    async def read_raw(
        self, node: int, address: ObjectAddress, *, timeout=None
    ) -> bytes:
        return (await self.async_read_object(address, node=node)).raw_value

    async def async_read_objects(
        self, addresses: tuple[ObjectAddress, ...], *, node: int = 0xFF
    ) -> tuple[GenericRead | HomeAssistantError, ...]:
        return await self._backend.async_read_objects(addresses, node=node)

    async def async_write_object(
        self,
        address: ObjectAddress,
        value: Any,
        *,
        node: int,
        allow_unsafe: bool = False,
        verify: bool = True,
    ) -> WritePlan:
        """Run one Core-gated confirmed write through the selected backend."""

        if not self.write_enabled:
            raise HomeAssistantError("OpenRBus write access is disabled")
        # The configured role is only a request.  Never let it elevate a
        # write when Core could not establish the node's effective role (the
        # missing authorization-channel path is intentionally fail-closed).
        effective = self.effective_access_levels.get(node)
        if effective is None and self.configured_access_level > 1:
            # Discovery may not enumerate a target node (for example a
            # gateway-scoped object address). Refresh the authoritative
            # 4002:00 access object for this exact node before considering a
            # write; never infer L3 from configuration alone.
            try:
                access_read = await self._backend.async_read_object(
                    ObjectAddress(0x4002, 0x00), node=node
                )
                if (
                    type(access_read.value) is not int
                    or not 1 <= access_read.value <= 3
                ):
                    raise ValueError("invalid effective access level")
                effective = int(access_read.value)
                self.effective_access_levels[node] = effective
            except (HomeAssistantError, OpenRBusError, TypeError, ValueError) as error:
                raise HomeAssistantError(
                    "OpenRBus effective access level is unavailable; write blocked"
                ) from error
        if effective is not None:
            if effective < self.configured_write_access_level:
                raise HomeAssistantError(
                    "OpenRBus effective access level does not meet the configured write policy"
                )
            definition = Registry.load_default().find(address)
            if definition is not None:
                requirement = definition.access_requirement(
                    address, AccessOperation.WRITE, device_family=None
                )
                if (
                    requirement.required_level is not None
                    and int(requirement.required_level) > effective
                ):
                    raise HomeAssistantError(
                        "OpenRBus write requires a higher effective access level"
                    )

        return await self._backend.async_write_object(
            node,
            address,
            value,
            allow_unsafe=allow_unsafe,
            verify=verify,
        )

    async def async_reactivate_register(self, node: int, address: ObjectAddress) -> None:
        """Re-enable a lifecycle-retired projection and request a fresh poll.

        This is intentionally explicit: once polling is stopped, a sentinel
        cannot prove recovery by itself.  The service/rediscovery path keeps
        the same unique ID and entity-registry row instead of recreating it.
        """

        for coordinator in getattr(self, "_openrbus_polling_coordinators", {}).values():
            if (node, address) not in coordinator.registers:
                continue
            coordinator.async_reactivate_register(node, address)
            try:
                registry = er.async_get(self.hass)
                unique_id = (
                    f"{self.config_entry.entry_id}:node:{node}:"
                    f"object:{address.index:04x}:{address.subindex:02x}"
                )
                for platform in ("sensor", "number", "select", "switch"):
                    entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
                    entity = registry.entities.get(entity_id) if entity_id else None
                    if entity is not None and str(getattr(entity, "disabled_by", "")).casefold() == "integration":
                        registry.async_update_entity(entity_id, disabled_by=None)
            except (AttributeError, TypeError, ValueError):
                _LOGGER.debug("Could not re-enable OpenRBus entity %s", address)
            await coordinator.async_request_refresh()
            return
        raise HomeAssistantError("OpenRBus object is not part of the active poll set")


class OpenRBusPollingCoordinator(
    DataUpdateCoordinator[
        dict[tuple[int, ObjectAddress], GenericRead | HomeAssistantError]
    ]
):
    """Poll one configurable register group without duplicating transport state."""

    def __init__(
        self,
        hass: HomeAssistant,
        parent: OpenRBusCoordinator,
        group: str,
        registers: tuple[tuple[int, ObjectAddress], ...],
        interval: timedelta,
        entity_unique_ids: frozenset[str] | None = None,
        register_metadata: dict[tuple[int, ObjectAddress], object] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=parent.config_entry,
            name=f"{DOMAIN}_{group}",
            update_interval=interval,
        )
        self.parent = parent
        self.group = group
        self.registers = registers
        self.entity_unique_ids = entity_unique_ids or frozenset()
        self.register_metadata = register_metadata or {}
        self.validity = RegisterValidityTracker(parent.invalid_value_disable_after)
        self._diagnostic_poll_count = 0
        self._diagnostic_success_items = 0
        self._diagnostic_failed_items = 0
        self._diagnostic_error_counts = dict.fromkeys(_ERROR_CLASSES, 0)
        self._diagnostic_subtype_counts = dict.fromkeys(
            (
                "session.not_ready",
                "session.link_lost",
                "session.timeout",
                "session.not_secure",
                "session.transport",
                "batch.malformed",
                "batch.abort",
                "batch.fallback",
            ),
            0,
        )
        self._diagnostic_available_items = 0
        self._diagnostic_total_items = 0
        self._diagnostic_availability_delta = 0

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded, redacted poll statistics for config diagnostics."""
        return {
            "poll_count": self._diagnostic_poll_count,
            "success_items": self._diagnostic_success_items,
            "failed_items": self._diagnostic_failed_items,
            "error_counts": dict(self._diagnostic_error_counts),
            "error_subtype_counts": dict(self._diagnostic_subtype_counts),
            "available_items": self._diagnostic_available_items,
                "unavailable_items": max(
                0, self._diagnostic_total_items - self._diagnostic_available_items
            ),
            "availability_delta": self._diagnostic_availability_delta,
            "item_failures": tuple(self._diagnostic_item_failures),
        }

    @staticmethod
    def _bump(value: int, amount: int = 1) -> int:
        return min(_DIAGNOSTIC_COUNTER_MAX, value + amount)

    def add_registers(
        self,
        registers: set[tuple[int, ObjectAddress]],
        register_metadata: dict[tuple[int, ObjectAddress], object] | None = None,
    ) -> None:
        """Union registers requested by concurrently loaded HA platforms."""

        if not registers:
            return
        self.registers = tuple(
            sorted(
                set(self.registers).union(registers),
                key=lambda item: (item[0], item[1].index, item[1].subindex),
            )
        )
        if register_metadata:
            self.register_metadata.update(register_metadata)

    def is_value_available(self, node: int, address: ObjectAddress) -> bool:
        """Whether the latest object read is a publishable engineering value."""

        key = (node, address)
        return self.validity.is_valid(key, (self.data or {}).get(key))

    def async_reactivate_register(self, node: int, address: ObjectAddress) -> None:
        """Resume a retired row for explicit rediscovery/reactivation."""

        self.validity.reactivate((node, address))

    async def _async_update_data(
        self,
    ) -> dict[tuple[int, ObjectAddress], GenericRead | HomeAssistantError]:
        grouped: dict[int, list[ObjectAddress]] = defaultdict(list)
        try:
            entity_registry = er.async_get(self.hass)
        except (AttributeError, TypeError):
            # Lightweight unit-test hass objects and very early setup do not
            # have an entity registry yet; the initial poll remains complete.
            entity_registry = None
        for node, address in self.registers:
            if self.validity.is_expired((node, address)):
                continue
            unique_id = (
                f"{self.parent.config_entry.entry_id}:node:{node}:"
                f"object:{address.index:04x}:{address.subindex:02x}"
            )
            # A row may have existed as a legacy sensor before its typed
            # projection was introduced.  Do not let a disabled stale sensor
            # shadow an enabled number/select/switch with the same stable
            # unique ID.  Poll when at least one current projection is
            # enabled; suppress only when every matching registry entry is
            # disabled.
            entries = []
            if entity_registry is not None:
                # A writable row can be represented by number/select/switch;
                # disabled registry entries on any of those platforms must
                # suppress polling just like the legacy sensor projection.
                for platform in ("sensor", "number", "select", "switch"):
                    entity_id = entity_registry.async_get_entity_id(
                        platform, DOMAIN, unique_id
                    )
                    if entity_id:
                        entry = entity_registry.entities.get(entity_id)
                        if entry is not None:
                            entries.append(entry)
            if not _should_poll_registry_entries(tuple(entries)):
                continue
            grouped[node].append(address)
        result: dict[tuple[int, ObjectAddress], GenericRead | HomeAssistantError] = {}
        self._diagnostic_poll_count = self._bump(self._diagnostic_poll_count)
        success_items = 0
        failed_items = 0
        old_available = self._diagnostic_available_items
        available = 0
        for node, addresses in grouped.items():
            # Keep each HA poll transaction bounded even when one family has
            # hundreds of readable rows.  A link loss or malformed response
            # then affects only this chunk; healthy chunks (including typed
            # controls) remain publishable and are retried by the backend's
            # batch/single recovery path.
            for start in range(0, len(addresses), _POLL_BATCH_SIZE):
                chunk = tuple(addresses[start : start + _POLL_BATCH_SIZE])
                try:
                    values = await self.parent.async_read_objects(chunk, node=node)
                except Exception as error:  # noqa: BLE001 - keep one batch isolated
                    error_class = _read_error_class(error)
                    error_subtype = _read_error_subtype(error, error_class)
                    if error_class in {"abort", "item"}:
                        error_subtype = "abort" if error_class == "abort" else "fallback"
                        error_class = "batch"
                    elif error_class == "batch" and error_subtype is None:
                        error_subtype = "fallback"
                    values = tuple(
                        _tag_poll_error(
                            HomeAssistantError("OpenRBus poll batch failed"),
                            error_class,
                            error_subtype,
                        )
                        for _ in chunk
                    )
                # A backend adapter must preserve batch cardinality.  Keep
                # the coordinator total even when an older adapter violates
                # that contract: missing items become object-local errors
                # instead of taking every typed entity offline.
                values = tuple(values)
                if len(values) < len(chunk):
                    missing = len(chunk) - len(values)
                    values += tuple(
                        _tag_poll_error(
                            HomeAssistantError(
                                "OpenRBus backend returned incomplete batch"
                            ),
                            "batch",
                            "malformed",
                        )
                        for _ in range(missing)
                    )
                elif len(values) > len(chunk):
                    self._diagnostic_error_counts["batch"] = self._bump(
                        self._diagnostic_error_counts["batch"], len(values) - len(chunk)
                    )
                    values = values[: len(chunk)]
                for address, value in zip(chunk, values):
                    if isinstance(value, GenericRead) and (
                        value.node != node or value.address != address
                    ):
                        value = HomeAssistantError("OpenRBus response correlation failed")
                        value._openrbus_error_class = "correlation"  # type: ignore[attr-defined]
                    if isinstance(value, HomeAssistantError):
                        failed_items += 1
                        error_class = _read_error_class(value)
                        self._diagnostic_error_counts[error_class] = self._bump(
                            self._diagnostic_error_counts[error_class]
                        )
                        error_subtype = _read_error_subtype(value, error_class)
                        if error_class == "batch" and error_subtype is None:
                            error_subtype = "fallback"
                        subtype_key = f"{error_class}.{error_subtype}"
                        if subtype_key in self._diagnostic_subtype_counts:
                            self._diagnostic_subtype_counts[subtype_key] = self._bump(
                                self._diagnostic_subtype_counts[subtype_key]
                            )
                        options = getattr(self.parent.config_entry, "options", {})
                        if (
                            error_class in _ERROR_CLASSES
                            and bool(options.get(CONF_DIAGNOSTICS_ENABLED, False))
                        ):
                            failure = {
                                    "node": node,
                                    "index": address.index,
                                    "subindex": address.subindex,
                                    "error_class": error_class,
                                }
                            if error_subtype:
                                failure["error_subtype"] = error_subtype
                            self._diagnostic_item_failures.append(failure)
                            del self._diagnostic_item_failures[
                                : -_DIAGNOSTIC_ITEM_FAILURE_LIMIT
                            ]
                    else:
                        success_items += 1
                    result[(node, address)] = value
                    observation = self.validity.observe(
                        (node, address), value, self.register_metadata.get((node, address))
                    )
                    if observation.expired:
                        self._disable_expired_entities(node, address)
                    elif self.validity.is_valid((node, address), value):
                        available += 1
        self._diagnostic_success_items = self._bump(
            self._diagnostic_success_items, success_items
        )
        self._diagnostic_failed_items = self._bump(
            self._diagnostic_failed_items, failed_items
        )
        self._diagnostic_availability_delta = max(
            -_DIAGNOSTIC_COUNTER_MAX,
            min(_DIAGNOSTIC_COUNTER_MAX, available - old_available),
        )
        self._diagnostic_available_items = available
        self._diagnostic_total_items = len(result)
        return result

    def _disable_expired_entities(self, node: int, address: ObjectAddress) -> None:
        """Disable only this integration's stable projection; never delete it."""

        try:
            registry = er.async_get(self.hass)
        except (AttributeError, TypeError):
            return
        unique_id = (
            f"{self.parent.config_entry.entry_id}:node:{node}:"
            f"object:{address.index:04x}:{address.subindex:02x}"
        )
        for platform in ("sensor", "number", "select", "switch"):
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if not entity_id:
                continue
            entity = registry.entities.get(entity_id)
            if entity is None or getattr(entity, "disabled_by", None):
                continue
            try:
                registry.async_update_entity(entity_id, disabled_by="integration")
            except (TypeError, ValueError):
                _LOGGER.debug("Could not disable expired OpenRBus entity %s", entity_id)
