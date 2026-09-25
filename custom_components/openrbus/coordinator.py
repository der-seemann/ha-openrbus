"""OpenRBus coordinator with exactly two supported transport backends."""

from __future__ import annotations

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
    CONF_LANGUAGE,
    CONF_PAIR_ACTION,
    CONF_PASSKEY,
    CONF_POLL_FAST,
    CONF_POLL_SLOW,
    CONF_POLL_STANDARD,
    CONF_THIN_CONTROLLER,
    CONF_THIN_DIAGNOSTICS_SERVICE,
    CONF_THIN_POLL_SERVICE,
    CONF_THIN_REQUEST_HANDLE,
    CONF_THIN_REQUEST_SERVICE,
    CONF_THIN_RESPONSE_HANDLE,
    CONF_THIN_TARGET_ADDRESS_TYPE,
    CONF_WRITE_ENABLED,
    DEFAULT_POLL_INTERVALS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .transport import (
    NativeBluetoothBackend,
    ThinRpcBackend,
    controller_prefix,
    resolve_thin_rpc_capability,
)

_LOGGER = logging.getLogger(__name__)
_COORDINATOR_INSTANCE_IDS = itertools.count(1)
_POLL_BATCH_SIZE = 32


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
        self.backend_mode = backend
        self.configured_access_level = max(
            1, min(3, int(configured.get(CONF_ACCESS_LEVEL, 1)))
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
        # Core/backend authorization is authoritative.  A higher configured
        # value is never treated as proof of effective access.
        self.effective_access_level: int | None = (
            1 if self.configured_access_level == 1 else None
        )
        self.devices: tuple[DeviceIdentity, ...] = ()
        self.inventories: tuple[DeviceInventory, ...] = ()
        self.effective_access_levels: dict[int, int] = {}
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
        result = await self.async_read_object(ObjectAddress(0x2001, 0x02), node=0xFF)
        return BridgeRead(result.address, result.raw_value, result.value)

    async def async_start(self) -> None:
        await self._backend.async_start()
        effective = getattr(self._backend, "effective_access_level", None)
        if effective is not None:
            self.effective_access_level = int(effective)
        # Backends normally call Core discovery, but resolve once more at the
        # lifecycle boundary so custom/legacy backends cannot omit the
        # evidence-backed family metadata needed by catalog_for_node().  This
        # is a pure identity projection; it does not probe or infer a node.
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

    def add_registers(self, registers: set[tuple[int, ObjectAddress]]) -> None:
        """Union registers requested by concurrently loaded HA platforms."""

        if not registers:
            return
        self.registers = tuple(
            sorted(
                set(self.registers).union(registers),
                key=lambda item: (item[0], item[1].index, item[1].subindex),
            )
        )

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
                    values = tuple(HomeAssistantError(str(error)) for _ in chunk)
                # A backend adapter must preserve batch cardinality.  Keep
                # the coordinator total even when an older adapter violates
                # that contract: missing items become object-local errors
                # instead of taking every typed entity offline.
                values = tuple(values)
                if len(values) < len(chunk):
                    values += tuple(
                        HomeAssistantError("OpenRBus backend returned incomplete batch")
                        for _ in range(len(chunk) - len(values))
                    )
                elif len(values) > len(chunk):
                    values = values[: len(chunk)]
                for address, value in zip(chunk, values):
                    if isinstance(value, GenericRead) and (
                        value.node != node or value.address != address
                    ):
                        value = HomeAssistantError(
                            "OpenRBus backend returned a mismatched node/object address"
                        )
                    result[(node, address)] = value
        return result
