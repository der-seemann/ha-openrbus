"""OpenRBus coordinator with exactly two supported transport backends."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import defaultdict
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from openrbus.catalog import catalog_for_node
from openrbus.client import WritePlan
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.errors import (
    CanOpenAbortError,
    OpenRBusError,
)
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
    CONF_EXPERIMENTAL_WRITES,
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
from .identity import migrate_legacy_unique_id, stable_object_id
from .transport import (
    NativeBluetoothBackend,
    ThinRpcBackend,
    _abort_category,
    _read_error_class,
    _read_error_subtype,
    controller_prefix,
    resolve_thin_rpc_capability,
    safe_batch_exception_type,
)
from .validity import RegisterValidityTracker
from .zones import (
    ZONE_FRIENDLY_NAME_INDEX,
    ZONE_FUNCTION_INDEX,
    ZONE_SHORT_NAME_INDEX,
    ZoneProfile,
    normalized_overrides,
    normalized_selection_overrides,
    zone_function_slots,
)

_LOGGER = logging.getLogger(__name__)
_COORDINATOR_INSTANCE_IDS = itertools.count(1)
_POLL_BATCH_SIZE = 32
_DIAGNOSTIC_ITEM_FAILURE_LIMIT = 16
_ZONE_DISCOVERY_STARTUP_BUDGET = 20.0
_ZONE_DISCOVERY_READ_TIMEOUT = 1.5
_ZONE_DISCOVERY_MAX_RETRIES = 3
_INITIAL_SETUP_TOTAL_BUDGET = 300.0
_INITIAL_SETUP_CLEANUP_BUDGET = 8.0
_INITIAL_SETUP_WAIT_SLICE = 15.0
_ZONE_PROFILE_CACHE: dict[tuple[str, str], dict[tuple[int, int], ZoneProfile]] = {}
_ZONE_DISCOVERY_CURSOR: dict[tuple[str, str], int] = {}
_ZONE_PROFILE_REFRESH_INTERVAL = 300
_REGISTRY = Registry.load_default()
_ERROR_CLASSES = ("abort", "item", "batch", "decode", "correlation", "session")
_ABORT_CATEGORIES = frozenset(
    {
        "unsupported_access",
        "read_not_supported",
        "write_not_supported",
        "object_missing",
        "subindex_missing",
        "type_length_mismatch",
        "other_abort",
    }
)
_DECODE_DETAILS = frozenset(
    {"visible_string_non_ascii", "visible_string_overlength", "visible_string_other"}
)


def _invalid_value_retirement_period(value: object) -> timedelta:
    """Convert the user-facing invalid-value threshold, expressed in minutes."""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = DEFAULT_INVALID_VALUE_DISABLE_AFTER
    return timedelta(minutes=max(1, minutes))


def _zone_selection_changed(
    previous: dict[tuple[int, int], ZoneProfile],
    current: dict[tuple[int, int], ZoneProfile],
) -> bool:
    """Whether late zone evidence changes the set of eligible zone slots."""

    return any(
        bool(previous.get(key) and previous[key].active)
        != bool(current.get(key) and current[key].active)
        for key in previous.keys() | current.keys()
    )


def schedule_background_task(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    target: Any,
    *,
    name: str,
) -> asyncio.Task[Any]:
    """Create an entry-owned task without holding up HA startup.

    The ConfigEntry owns the task and cancels it when the entry is unloaded.
    """

    return config_entry.async_create_background_task(hass, target, name=name)


def schedule_first_refresh_in_background(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: DataUpdateCoordinator[Any],
    *,
    name: str,
) -> asyncio.Task[Any]:
    """Start initial data fetching without holding up Home Assistant startup.

    Config-entry setup already verifies transport and access. Register polling
    coordinators before this refresh is scheduled; their first device reads can
    span many batches, so they must be lifecycle-owned background tasks rather
    than setup tasks tracked by HA's startup watchdog.
    """

    return schedule_background_task(
        hass,
        config_entry,
        coordinator.async_config_entry_first_refresh(),
        name=name,
    )


_SAFE_BATCH_EXCEPTION_TYPES = frozenset(
    {
        "canopen_abort",
        "request_timeout",
        "transport_error",
        "protocol_error",
        "registry_error",
        "validation_error",
        "timeout",
        "home_assistant_error",
        "value_error",
        "type_error",
        "assertion_error",
        "attribute_error",
        "index_error",
        "key_error",
        "lookup_error",
        "not_implemented_error",
        "os_error",
        "runtime_error",
        "unicode_error",
        "other_error",
    }
)
_DIAGNOSTIC_COUNTER_MAX = 2_147_483_647


def _tag_poll_error(
    error: HomeAssistantError,
    error_class: str,
    error_subtype: str | None = None,
    *,
    abort_category: str | None = None,
    decode_subtype: str | None = None,
    batch_exception_type: str | None = None,
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
    if abort_category in _ABORT_CATEGORIES:
        error._openrbus_abort_category = abort_category  # type: ignore[attr-defined]
    if decode_subtype in _DECODE_DETAILS:
        error._openrbus_decode_subtype = decode_subtype  # type: ignore[attr-defined]
    if batch_exception_type in _SAFE_BATCH_EXCEPTION_TYPES:
        error._openrbus_batch_exception_type = batch_exception_type  # type: ignore[attr-defined]
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
        self._zone_profile_cache_key = (
            entry.entry_id,
            str(configured.get(CONF_BLE_DEVICE, "")).strip().upper(),
        )
        backend = configured.get(CONF_BACKEND)
        if backend not in {BACKEND_NATIVE, BACKEND_THIN_RPC}:
            raise HomeAssistantError(
                "Unsupported OpenRBus transport; choose Local Bluetooth or ESPHome Thin-RPC"
            )
        self._entry_id = entry.entry_id
        self._startup_config_snapshot = (
            dict(getattr(entry, "data", {})),
            dict(getattr(entry, "options", {})),
        )
        self._instance_id = next(_COORDINATOR_INSTANCE_IDS)
        self._diagnostic_error_counts = dict.fromkeys(_ERROR_CLASSES, 0)
        self._diagnostic_item_failures: list[dict[str, int | str]] = []
        self._diagnostic_poll_count = 0
        self._discovery_attempted = False
        self.discovery_error: BaseException | None = None
        self.backend_mode = backend

        def configured_level(
            key: str, fallback: object = 1, *, allow_no_write: bool = False
        ) -> int:
            try:
                minimum = 0 if allow_no_write else 1
                return max(minimum, min(3, int(configured.get(key, fallback))))
            except (TypeError, ValueError):
                return 0 if allow_no_write else 1

        # A legacy single policy was both read and write policy.  New entries
        # carry explicit independent levels.  Core is requested at the
        # highest needed level, but this does not grant writes by itself.
        self.configured_read_access_level = configured_level(
            CONF_READ_ACCESS_LEVEL, configured.get(CONF_ACCESS_LEVEL, 1)
        )
        self.configured_write_access_level = configured_level(
            CONF_WRITE_ACCESS_LEVEL,
            configured.get(CONF_ACCESS_LEVEL, 1),
            allow_no_write=True,
        )
        self.configured_access_level = max(
            self.configured_read_access_level, self.configured_write_access_level
        )
        self.write_enabled = bool(configured.get(CONF_WRITE_ENABLED, False))
        self.experimental_writes = bool(configured.get(CONF_EXPERIMENTAL_WRITES, False))
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
        self.invalid_value_disable_after = _invalid_value_retirement_period(
            configured.get(
                CONF_INVALID_VALUE_DISABLE_AFTER,
                DEFAULT_INVALID_VALUE_DISABLE_AFTER,
            )
        )
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
        # CP020 activity is live device evidence. Never reuse it across a
        # coordinator reload; the new instance must positively reread selectors.
        self.zone_profiles: dict[tuple[int, int], ZoneProfile] = {}
        self._zone_entities: set[Any] = set()
        self._zone_discovery_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._startup_retryable = False
        self._startup_cleanup_failed = False
        self._startup_lifecycle_unsubscribe = None
        self._zone_profile_monitor_task: asyncio.Task[None] | None = None
        # Set before child polling coordinators shut down.  An in-flight poll
        # may finish its current request, but must not start another batch
        # after the parent has begun releasing the physical controller.
        self._shutting_down = False
        self.zone_overrides = normalized_overrides(configured.get(CONF_ZONE_OVERRIDES))
        self.entity_overrides = {
            migrate_legacy_unique_id(self, key): value
            for key, value in normalized_selection_overrides(
                configured.get(CONF_ENTITY_OVERRIDES)
            ).items()
        }
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
        backend_start_invoked = False
        try:
            backend_start_invoked = True
            await self._backend.async_start()
            effective = getattr(self._backend, "effective_access_level", None)
            if effective is not None:
                self.effective_access_level = int(effective)
            # Backends normally call Core discovery, but resolve once more at
            # the lifecycle boundary so custom/legacy backends cannot omit the
            # evidence-backed family metadata needed by catalog_for_node().
            # This is a pure identity projection; it does not probe or infer.
            self._discovery_attempted = True
            try:
                discovered = await self._backend.async_discover_devices()
            except BaseException as error:
                self.discovery_error = error
                raise
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
            if not await self._async_discover_zone_profiles():
                self._zone_discovery_task = schedule_background_task(
                    self.hass,
                    self.config_entry,
                    self._async_retry_zone_discovery(),
                    name="OpenRBus deferred zone discovery",
                )
            else:
                self._schedule_zone_profile_monitor()
        except BaseException as error:
            # Backend setup can succeed before the remaining discovery and
            # catalog projection steps fail. Stop the partially initialized
            # backend before Home Assistant retries this entry. Thin-RPC stop
            # retains controller ownership unless its physical disconnect
            # fence succeeds, so a cleanup failure remains fail-closed.
            if backend_start_invoked:
                try:
                    async with asyncio.timeout(_INITIAL_SETUP_CLEANUP_BUDGET):
                        await self._backend.async_stop()
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation while reporting cleanup failure
                    self._startup_cleanup_failed = True
                    error.add_note(
                        "OpenRBus backend cleanup failed after startup error: "
                        f"{type(cleanup_error).__name__}"
                    )
            if isinstance(error, asyncio.CancelledError):
                setup_metrics = getattr(self._backend, "setup_metrics", None)
                record_cancel = getattr(
                    setup_metrics, "record_setup_cancellation", None
                )
                if callable(record_cancel):
                    elapsed = (asyncio.get_running_loop().time() - started) * 1000
                    record_cancel(elapsed)
            raise

    def ensure_startup_lifecycle(self, entry: ConfigEntry) -> None:
        """Keep one initial discovery attempt alive across HA setup retries."""

        if self._startup_lifecycle_unsubscribe is not None:
            return

        def state_changed() -> None:
            # A normal ConfigEntryNotReady transition must retain the same
            # session/task. Explicit unload, removal, fatal setup errors, and
            # external cancellation terminate it instead.
            if entry.state in {
                ConfigEntryState.NOT_LOADED,
                ConfigEntryState.UNLOAD_IN_PROGRESS,
                ConfigEntryState.SETUP_ERROR,
                ConfigEntryState.FAILED_UNLOAD,
            }:
                self._startup_retryable = True
                task = self._startup_task
                if task is not None and not task.done():
                    task.cancel("OpenRBus config entry is no longer setting up")

        self._startup_lifecycle_unsubscribe = entry.async_on_state_change(state_changed)

    def detach_startup_lifecycle(self) -> None:
        """Remove the state listener when the entry is unloaded or removed."""

        unsubscribe = self._startup_lifecycle_unsubscribe
        self._startup_lifecycle_unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()

    async def async_wait_for_initial_startup(self) -> None:
        """Wait briefly for one shared, bounded initial transport/discovery task."""

        task = self._startup_task
        if task is not None and (task.cancelled() or task.cancelling()):
            # Explicit reload/removal cancels the old attempt. A later setup
            # may start over only after that task's cleanup has completed.
            try:
                async with asyncio.timeout(_INITIAL_SETUP_CLEANUP_BUDGET + 1.0):
                    await asyncio.gather(task, return_exceptions=True)
            except TimeoutError as error:
                raise ConfigEntryNotReady(
                    "OpenRBus startup cancellation cleanup is still pending"
                ) from error
            if self._startup_cleanup_failed or getattr(
                self._backend, "_owns_controller", False
            ):
                try:
                    async with asyncio.timeout(_INITIAL_SETUP_CLEANUP_BUDGET):
                        await self._backend.async_stop()
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    self._startup_cleanup_failed = True
                    raise ConfigEntryNotReady(
                        "OpenRBus startup cleanup has not proven disconnect"
                    ) from error
                self._startup_cleanup_failed = False
            self._startup_task = None
            task = None
        if task is not None and task.done():
            if self._startup_retryable:
                # A previous total-budget timeout is retryable only after its
                # cleanup proved safe. If not, retry that physical fence first.
                if self._startup_cleanup_failed:
                    try:
                        async with asyncio.timeout(_INITIAL_SETUP_CLEANUP_BUDGET):
                            await self._backend.async_stop()
                    except asyncio.CancelledError:
                        raise
                    except BaseException as error:
                        self._startup_cleanup_failed = True
                        raise ConfigEntryNotReady(
                            "OpenRBus startup cleanup has not proven disconnect"
                        ) from error
                    self._startup_cleanup_failed = False
                self._startup_task = None
                task = None
            else:
                task.result()

        if task is None:
            self._startup_retryable = False
            self._startup_cleanup_failed = False
            task = self.hass.async_create_background_task(
                self._async_run_initial_startup(),
                f"OpenRBus initial setup {self._entry_id}",
                eager_start=False,
            )
            self._startup_task = task
            task.add_done_callback(self._consume_startup_exception)

        done, _pending = await asyncio.wait({task}, timeout=_INITIAL_SETUP_WAIT_SLICE)
        if not done:
            raise ConfigEntryNotReady(
                "OpenRBus is still connecting and discovering devices"
            )
        task.result()

    async def _async_run_initial_startup(self) -> None:
        """Run initial discovery once with a hard total budget."""

        try:
            async with asyncio.timeout(_INITIAL_SETUP_TOTAL_BUDGET):
                await self.async_start()
        except TimeoutError as error:
            self._startup_retryable = True
            raise ConfigEntryNotReady(
                "OpenRBus initial discovery exceeded its 300 second budget"
            ) from error

    @staticmethod
    def _consume_startup_exception(task: asyncio.Task[None]) -> None:
        """Retrieve late startup errors when HA's setup retry is sleeping."""

        if not task.cancelled():
            task.exception()

    async def async_cancel_initial_startup(self) -> None:
        """Cancel and join startup before unloading/removing this coordinator."""

        task = self._startup_task
        if task is not None and not task.done():
            task.cancel("OpenRBus initial setup is unloading")
            try:
                async with asyncio.timeout(_INITIAL_SETUP_CLEANUP_BUDGET + 1.0):
                    await asyncio.gather(task, return_exceptions=True)
            except TimeoutError as error:
                raise HomeAssistantError(
                    "OpenRBus initial startup did not stop before unload"
                ) from error

        backend = self._backend
        if getattr(backend, "started", False) or getattr(
            backend, "_owns_controller", False
        ):
            try:
                async with asyncio.timeout(_INITIAL_SETUP_CLEANUP_BUDGET):
                    await backend.async_stop()
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self._startup_cleanup_failed = True
                raise HomeAssistantError(
                    "OpenRBus backend cleanup did not prove disconnect"
                ) from error

    async def _async_discover_zone_profiles(self) -> bool:
        """Read ZoneFunction plus custom labels for advertised zone slots.

        This is strictly read-only and best-effort.  CP020 is manufacturer
        evidence for active/inactive and heating/DHW semantics.  A transport
        error leaves that slot absent (and therefore disabled by default),
        which is safer than exposing every static SCB-10 zone array.
        """

        # Build from this read cycle only. Failed/unread selectors must remove
        # old positive evidence so they cannot keep child rows eligible.
        profiles: dict[tuple[int, int], ZoneProfile] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ZONE_DISCOVERY_STARTUP_BUDGET
        complete = True
        candidates: list[tuple[Any, Any, int, int]] = []
        for runtime_node in self.inventories or self.devices:
            identity = getattr(runtime_node, "identity", runtime_node)
            node = getattr(identity, "node", None)
            if not isinstance(node, int):
                continue
            rows = catalog_for_node(
                runtime_node,
                _REGISTRY,
                experimental_writes=self.write_enabled and self.experimental_writes,
            )
            candidates.extend(
                (runtime_node, identity, node, slot)
                for slot in zone_function_slots(rows)
            )

        if not candidates:
            self._has_zone_function_slots = False
            return True
        self._has_zone_function_slots = True

        cursor = _ZONE_DISCOVERY_CURSOR.get(self._zone_profile_cache_key, 0)
        cursor %= len(candidates)
        processed = 0
        for offset in range(len(candidates)):
            if loop.time() >= deadline:
                complete = False
                break
            index = (cursor + offset) % len(candidates)
            _runtime_node, identity, node, slot = candidates[index]
            processed += 1
            try:
                function_read = await self.async_read_object(
                    ObjectAddress(ZONE_FUNCTION_INDEX, slot),
                    node=node,
                    timeout=_ZONE_DISCOVERY_READ_TIMEOUT,
                    recover_on_transport_error=False,
                )
            except (HomeAssistantError, OpenRBusError, TypeError, ValueError):
                complete = False
                continue
            if type(function_read.value) is not int:
                complete = False
                continue
            if function_read.value == 0:
                profile = ZoneProfile(
                    node,
                    slot,
                    function_read.value,
                    node_name=(
                        getattr(identity, "model", None)
                        or getattr(identity, "family", None)
                        or getattr(identity, "name", None)
                    ),
                )
                profiles[(node, slot)] = profile
                self.zone_profiles = dict(profiles)
                continue
            friendly_name: str | None = None
            short_name: str | None = None
            try:
                name_read = await self.async_read_object(
                    ObjectAddress(ZONE_FRIENDLY_NAME_INDEX, slot),
                    node=node,
                    timeout=_ZONE_DISCOVERY_READ_TIMEOUT,
                    recover_on_transport_error=False,
                )
                if isinstance(name_read.value, str):
                    friendly_name = name_read.value.strip("\x00").strip() or None
            except (HomeAssistantError, OpenRBusError, TypeError, ValueError):
                # CP020 already proved that this slot is active. A missing
                # optional label must not block discovery of other slots.
                _LOGGER.debug("Zone label unavailable for node %s slot %s", node, slot)
            if not friendly_name:
                try:
                    short_name_read = await self.async_read_object(
                        ObjectAddress(ZONE_SHORT_NAME_INDEX, slot),
                        node=node,
                        timeout=_ZONE_DISCOVERY_READ_TIMEOUT,
                        recover_on_transport_error=False,
                    )
                    if isinstance(short_name_read.value, str):
                        short_name = short_name_read.value.strip("\x00").strip() or None
                except (
                    HomeAssistantError,
                    OpenRBusError,
                    TypeError,
                    ValueError,
                ):
                    _LOGGER.debug(
                        "Short zone label unavailable for node %s slot %s",
                        node,
                        slot,
                    )
            # The device model/family is manufacturer identity evidence.
            # Keep it on each profile so both the Options Flow and HA's
            # zone child device show which bus node owns the slot.
            node_name = (
                getattr(identity, "model", None)
                or getattr(identity, "family", None)
                or getattr(identity, "name", None)
            )
            profile = ZoneProfile(
                node,
                slot,
                function_read.value,
                friendly_name,
                node_name,
                short_name,
            )
            profiles[(node, slot)] = profile
            self.zone_profiles = dict(profiles)
        self.zone_profiles = profiles
        _ZONE_DISCOVERY_CURSOR[self._zone_profile_cache_key] = (
            cursor + processed
        ) % len(candidates)
        return complete

    async def _async_retry_zone_discovery(self) -> None:
        """Complete best-effort zone discovery after the entry is available."""

        try:
            for attempt in range(_ZONE_DISCOVERY_MAX_RETRIES):
                await asyncio.sleep(15)
                if self._shutting_down:
                    return
                previous = dict(self.zone_profiles)
                complete = await self._async_discover_zone_profiles()
                for entity in tuple(self._zone_entities):
                    entity.async_refresh_zone_name()
                if self._shutting_down:
                    return
                if _zone_selection_changed(previous, self.zone_profiles):
                    # A late positive CP020 profile changes which zone
                    # entities and poller rows are safe to expose. One managed
                    # reload rebuilds both from cached evidence.
                    self.hass.config_entries.async_schedule_reload(self._entry_id)
                    return
                if complete:
                    self._schedule_zone_profile_monitor()
                    return
                _LOGGER.debug(
                    "Zone discovery remains incomplete after retry %s/%s",
                    attempt + 1,
                    _ZONE_DISCOVERY_MAX_RETRIES,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Deferred OpenRBus zone discovery failed", exc_info=True)

    def _schedule_zone_profile_monitor(self) -> None:
        """Periodically refresh CP020 so child projection follows device state."""

        if (
            not getattr(self, "_has_zone_function_slots", False)
            or self._zone_profile_monitor_task is not None
        ):
            return
        self._zone_profile_monitor_task = schedule_background_task(
            self.hass,
            self.config_entry,
            self._async_monitor_zone_profiles(),
            name="OpenRBus zone selector refresh",
        )

    async def _async_monitor_zone_profiles(self) -> None:
        """Reconcile activity changes from fresh, read-only CP020 selector data."""

        while not self._shutting_down:
            await asyncio.sleep(_ZONE_PROFILE_REFRESH_INTERVAL)
            if self._shutting_down:
                return
            previous = dict(self.zone_profiles)
            await self._async_discover_zone_profiles()
            for entity in tuple(self._zone_entities):
                entity.async_refresh_zone_name()
            if self._shutting_down:
                return
            if _zone_selection_changed(previous, self.zone_profiles):
                self.hass.config_entries.async_schedule_reload(self._entry_id)
                return

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
            inventory.resolve_registry(_REGISTRY)
        except (AttributeError, TypeError, ValueError):
            pass
        return inventory

    async def async_shutdown(self) -> None:
        backend = self._backend
        self._shutting_down = True
        await self.async_cancel_initial_startup()
        zone_task = self._zone_discovery_task
        if zone_task is not None and not zone_task.done():
            zone_task.cancel()
            await asyncio.gather(zone_task, return_exceptions=True)
        # These coordinators own the periodic register reads. Stop their
        # schedules before stopping the shared backend so an already-running
        # poll can finish its current transaction and observe _shutting_down
        # before it tries the next batch. Otherwise a multi-batch refresh can
        # restart Thin-RPC immediately after async_stop releases its claim.
        polling_coordinators = tuple(
            getattr(self, "_openrbus_polling_coordinators", {}).values()
        )
        for polling_coordinator in polling_coordinators:
            await polling_coordinator.async_shutdown()
        session = getattr(backend, "session", None)
        epoch = getattr(session, "epoch", None)
        if type(epoch) is not int or not 0 <= epoch <= _DIAGNOSTIC_COUNTER_MAX:
            epoch = None
        _LOGGER.warning(
            "UNLOAD_TRACE event=coordinator_shutdown_begin controller_owned=%s "
            "backend_started=%s session_epoch=%s link_connected=%s",
            bool(getattr(backend, "_owns_controller", False)),
            bool(getattr(backend, "started", False)),
            epoch,
            bool(getattr(getattr(backend, "link", None), "is_connected", False)),
        )
        try:
            await backend.async_stop()
        except BaseException as error:
            flags = getattr(backend, "_last_disconnect_snapshot", {})
            _LOGGER.warning(
                "UNLOAD_TRACE event=coordinator_shutdown_error error_type=%s "
                "controller_owned=%s physical_link_active=%s "
                "physical_parent_connected=%s physical_epoch=%s",
                type(error).__name__,
                bool(getattr(backend, "_owns_controller", False)),
                flags.get("link_active") if isinstance(flags, dict) else None,
                flags.get("parent_connected") if isinstance(flags, dict) else None,
                flags.get("epoch") if isinstance(flags, dict) else None,
            )
            raise
        flags = getattr(backend, "_last_disconnect_snapshot", {})
        _LOGGER.warning(
            "UNLOAD_TRACE event=coordinator_shutdown_complete controller_owned=%s "
            "physical_link_active=%s physical_parent_connected=%s physical_epoch=%s",
            bool(getattr(backend, "_owns_controller", False)),
            flags.get("link_active") if isinstance(flags, dict) else None,
            flags.get("parent_connected") if isinstance(flags, dict) else None,
            flags.get("epoch") if isinstance(flags, dict) else None,
        )
        await super().async_shutdown()

    async def async_read_object(
        self,
        address: ObjectAddress,
        *,
        node: int = 0xFF,
        timeout: float | None = None,
        recover_on_transport_error: bool = True,
    ) -> GenericRead:
        read = self._backend.async_read_object
        options: dict[str, Any] = {}
        if timeout is not None:
            options["timeout"] = timeout
        if not recover_on_transport_error and self.backend_mode == BACKEND_THIN_RPC:
            options["recover_on_transport_error"] = False
        return await read(address, node=node, **options)

    async def async_read_object_with_transport_capture(
        self, address: ObjectAddress, *, node: int = 0xFF
    ) -> GenericRead:
        capture = getattr(
            self._backend, "async_read_object_with_transport_capture", None
        )
        if not callable(capture):
            raise HomeAssistantError("Transport capture requires Thin-RPC")
        return await capture(address, node=node)

    async def read_raw(
        self, node: int, address: ObjectAddress, *, timeout=None
    ) -> bytes:
        return (await self.async_read_object(address, node=node)).raw_value

    async def async_read_objects(
        self,
        addresses: tuple[ObjectAddress, ...],
        *,
        node: int = 0xFF,
        trace_failure: bool = False,
    ) -> tuple[GenericRead | HomeAssistantError, ...]:
        if trace_failure and isinstance(self._backend, ThinRpcBackend):
            return await self._backend.async_read_objects(
                addresses, node=node, trace_failure=True
            )
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

        if self.configured_write_access_level not in (1, 2, 3):
            raise HomeAssistantError("OpenRBus write access level is set to no write")
        if not self.write_enabled:
            raise HomeAssistantError("OpenRBus write access is disabled")
        if allow_unsafe and not self.experimental_writes:
            raise HomeAssistantError("Experimental OpenRBus writes are disabled")
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
            definition = _REGISTRY.find(address)
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

    async def async_reactivate_register(
        self, node: int, address: ObjectAddress
    ) -> None:
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
                unique_id = stable_object_id(
                    self, node, address.index, address.subindex
                )
                for platform in ("sensor", "number", "select", "switch"):
                    entity_id = registry.async_get_entity_id(
                        platform, DOMAIN, unique_id
                    )
                    entity = registry.entities.get(entity_id) if entity_id else None
                    if (
                        entity is not None
                        and str(getattr(entity, "disabled_by", "")).casefold()
                        == "integration"
                    ):
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
        self._diagnostic_item_failures: list[dict[str, int | str]] = []
        self._diagnostic_available_items = 0
        self._diagnostic_total_items = 0
        self._diagnostic_availability_delta = 0
        self._poll_in_progress_count = 0
        self._diagnostic_registry_disabled_count = 0

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded, redacted poll statistics for config diagnostics."""
        return {
            "poll_count": self._diagnostic_poll_count,
            "poll_in_progress": getattr(self, "_poll_in_progress_count", 0) > 0,
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
            "quarantined_count": self.validity.quarantined_count,
            "quarantined_items": self.validity.quarantined_snapshot(),
            "registry_disabled_count": self._diagnostic_registry_disabled_count,
        }

    def _object_failure_scope(self, node: int) -> object | None:
        """Scope deterministic failures to identity, access, and session epoch."""

        if getattr(self.parent, "backend_mode", None) != BACKEND_THIN_RPC:
            return None
        identity = next(
            (
                getattr(inventory, "identity", None)
                for inventory in getattr(self.parent, "inventories", ())
                if getattr(getattr(inventory, "identity", None), "node", None) == node
            ),
            None,
        )
        access = getattr(self.parent, "effective_access_levels", {}).get(node)
        backend = getattr(self.parent, "_backend", None)
        session = getattr(backend, "session", None)
        epoch = getattr(session, "epoch", None)
        generation = getattr(backend, "_session_generation", None)
        if (
            identity is None
            or type(access) is not int
            or not 1 <= access <= 3
            or type(epoch) is not int
            or epoch <= 0
            or type(generation) is not int
            or generation <= 0
        ):
            return None
        scope = (access, identity, generation, epoch)
        try:
            hash(scope)
        except TypeError:
            return None
        return scope

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
        self._poll_in_progress_count = getattr(self, "_poll_in_progress_count", 0) + 1
        try:
            return await self._async_poll_data()
        finally:
            self._poll_in_progress_count = max(
                0, getattr(self, "_poll_in_progress_count", 1) - 1
            )

    async def _async_poll_data(
        self,
    ) -> dict[tuple[int, ObjectAddress], GenericRead | HomeAssistantError]:
        if getattr(self.parent, "_shutting_down", False):
            return {}
        grouped: dict[int, list[ObjectAddress]] = defaultdict(list)
        scopes: dict[tuple[int, ObjectAddress], object | None] = {}
        quarantined_keys: set[tuple[int, ObjectAddress]] = set()
        try:
            entity_registry = er.async_get(self.hass)
        except (AttributeError, TypeError):
            # Lightweight unit-test hass objects and very early setup do not
            # have an entity registry yet; the initial poll remains complete.
            entity_registry = None
        for node, address in self.registers:
            if self.validity.is_expired((node, address)):
                continue
            unique_id = stable_object_id(
                self.parent, node, address.index, address.subindex
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
                self._diagnostic_registry_disabled_count = min(
                    _DIAGNOSTIC_COUNTER_MAX,
                    self._diagnostic_registry_disabled_count + 1,
                )
                continue
            key = (node, address)
            scope = self._object_failure_scope(node)
            scopes[key] = scope
            if self.validity.is_quarantined(key, scope):
                quarantined_keys.add(key)
                continue
            grouped[node].append(address)
        result: dict[tuple[int, ObjectAddress], GenericRead | HomeAssistantError] = {}
        self._diagnostic_poll_count = self._bump(self._diagnostic_poll_count)
        success_items = 0
        failed_items = 0
        old_available = self._diagnostic_available_items
        available = 0
        for node, addresses in grouped.items():
            if getattr(self.parent, "_shutting_down", False):
                break
            # Keep each HA poll transaction bounded even when one family has
            # hundreds of readable rows.  A link loss or malformed response
            # then affects only this chunk; healthy chunks (including typed
            # controls) remain publishable and are retried by the backend's
            # batch/single recovery path.
            for start in range(0, len(addresses), _POLL_BATCH_SIZE):
                if getattr(self.parent, "_shutting_down", False):
                    break
                chunk = tuple(addresses[start : start + _POLL_BATCH_SIZE])
                try:
                    if self.group == "fast":
                        values = await self.parent.async_read_objects(
                            chunk, node=node, trace_failure=True
                        )
                    else:
                        values = await self.parent.async_read_objects(chunk, node=node)
                except Exception as error:  # noqa: BLE001 - keep one batch isolated
                    error_class = _read_error_class(error)
                    error_subtype = _read_error_subtype(error, error_class)
                    abort_category = (
                        _abort_category(error)
                        if isinstance(error, CanOpenAbortError)
                        else None
                    )
                    if error_class in {"abort", "item"}:
                        error_subtype = (
                            "abort" if error_class == "abort" else "fallback"
                        )
                        error_class = "batch"
                    elif error_class == "batch" and error_subtype is None:
                        error_subtype = "fallback"
                    values = tuple(
                        _tag_poll_error(
                            HomeAssistantError("OpenRBus poll batch failed"),
                            error_class,
                            error_subtype,
                            abort_category=abort_category,
                            batch_exception_type=safe_batch_exception_type(error),
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
                        value = HomeAssistantError(
                            "OpenRBus response correlation failed"
                        )
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
                        if error_class in _ERROR_CLASSES and bool(
                            options.get(CONF_DIAGNOSTICS_ENABLED, False)
                        ):
                            failure = {
                                "node": node,
                                "index": address.index,
                                "subindex": address.subindex,
                                "error_class": error_class,
                            }
                            if error_subtype:
                                failure["error_subtype"] = error_subtype
                            abort_category = getattr(
                                value, "_openrbus_abort_category", None
                            )
                            if abort_category in _ABORT_CATEGORIES:
                                failure["abort_category"] = abort_category
                            decode_subtype = getattr(
                                value, "_openrbus_decode_subtype", None
                            )
                            if decode_subtype in _DECODE_DETAILS:
                                failure["decode_subtype"] = decode_subtype
                            batch_exception_type = getattr(
                                value, "_openrbus_batch_exception_type", None
                            )
                            if batch_exception_type in _SAFE_BATCH_EXCEPTION_TYPES:
                                failure["batch_exception_type"] = batch_exception_type
                            self._diagnostic_item_failures.append(failure)
                            del self._diagnostic_item_failures[
                                :-_DIAGNOSTIC_ITEM_FAILURE_LIMIT
                            ]
                    else:
                        success_items += 1
                    result[(node, address)] = value
                    observation = self.validity.observe(
                        (node, address),
                        value,
                        self.register_metadata.get((node, address)),
                    )
                    self.validity.quarantine_deterministic_failure(
                        (node, address), value, scopes.get((node, address))
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
        self._diagnostic_total_items = len(result) + len(quarantined_keys)
        return result

    def _disable_expired_entities(self, node: int, address: ObjectAddress) -> None:
        """Disable only this integration's stable projection; never delete it."""

        try:
            registry = er.async_get(self.hass)
        except (AttributeError, TypeError):
            return
        unique_id = stable_object_id(self.parent, node, address.index, address.subindex)
        for platform in ("sensor", "number", "select", "switch"):
            entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if not entity_id:
                continue
            entity = registry.entities.get(entity_id)
            if entity is None or getattr(entity, "disabled_by", None):
                continue
            try:
                registry.async_update_entity(
                    entity_id,
                    disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                )
            except (TypeError, ValueError):
                _LOGGER.debug("Could not disable expired OpenRBus entity %s", entity_id)
