"""Coordinator coverage for the two supported OpenRBus transports."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import (
    ConfigEntries,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from openrbus.protocol.canip import ObjectAddress

import custom_components.openrbus as integration
import custom_components.openrbus.coordinator as coordinator_module
from custom_components.openrbus import _async_validate_transport_migration
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.const import (
    BACKEND_NATIVE,
    BACKEND_THIN_RPC,
    CONF_BACKEND,
    CONF_EXPERIMENTAL_WRITES,
    CONF_LANGUAGE,
    CONF_POLL_FAST,
    CONF_POLL_SLOW,
    CONF_POLL_STANDARD,
    CONF_READ_ACCESS_LEVEL,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
    DEFAULT_INVALID_VALUE_DISABLE_AFTER,
)
from custom_components.openrbus.coordinator import (
    OpenRBusCoordinator,
    OpenRBusPollingCoordinator,
    _invalid_value_retirement_period,
    _should_poll_registry_entries,
    _zone_selection_changed,
    schedule_background_task,
    schedule_first_refresh_in_background,
)
from custom_components.openrbus.transport import ThinRpcCapability, select_backend_mode
from custom_components.openrbus.zones import ZoneProfile, ZoneReadState


def _entry(backend: str) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=f"entry-{backend}",
        data={CONF_BACKEND: backend, "ble_device": "target"},
        options={},
        async_on_unload=lambda *_: None,
    )


def _hass() -> SimpleNamespace:
    return SimpleNamespace(
        services=SimpleNamespace(async_services=lambda: {"esphome": {}})
    )


def _confirm_zone_active(coordinator, node: int = 4, slot: int = 4) -> None:
    coordinator.zone_profiles[(node, slot)] = ZoneProfile(node, slot, 2)
    coordinator.zone_profile_states[(node, slot)] = ZoneReadState.CONFIRMED_ACTIVE


class _StartupHarness:
    """Small lifecycle surface for testing staged HA startup retries."""

    async_wait_for_initial_startup = OpenRBusCoordinator.async_wait_for_initial_startup
    _async_run_initial_startup = OpenRBusCoordinator._async_run_initial_startup
    _consume_startup_exception = OpenRBusCoordinator._consume_startup_exception
    ensure_startup_lifecycle = OpenRBusCoordinator.ensure_startup_lifecycle
    async_cancel_initial_startup = OpenRBusCoordinator.async_cancel_initial_startup

    def __init__(self, *, hass, backend, start) -> None:
        self.hass = hass
        self._backend = backend
        self._entry_id = "startup-test"
        self._startup_task = None
        self._startup_retryable = False
        self._startup_cleanup_failed = False
        self._startup_lifecycle_unsubscribe = None
        self.async_start = start


def _startup_hass() -> SimpleNamespace:
    def create_background_task(coroutine, _name, eager_start=False):
        assert not eager_start
        return asyncio.create_task(coroutine)

    return SimpleNamespace(async_create_background_task=create_background_task)


@pytest.mark.asyncio
async def test_initial_setup_retry_reuses_pending_discovery_task(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "_INITIAL_SETUP_WAIT_SLICE", 0.01)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def start() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    coordinator = _StartupHarness(
        hass=_startup_hass(), backend=SimpleNamespace(), start=start
    )
    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_wait_for_initial_startup()
    await started.wait()
    pending = coordinator._startup_task
    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_wait_for_initial_startup()
    assert coordinator._startup_task is pending
    assert calls == 1

    release.set()
    await coordinator.async_wait_for_initial_startup()
    assert coordinator._startup_task is pending
    assert calls == 1


@pytest.mark.asyncio
async def test_entry_state_preserves_setup_retry_and_cancels_on_removal(
    monkeypatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "_INITIAL_SETUP_WAIT_SLICE", 0.01)
    started = asyncio.Event()
    backend = SimpleNamespace(_owns_controller=False, started=False)

    async def stop() -> None:
        backend._owns_controller = False

    backend.async_stop = stop

    async def start() -> None:
        backend._owns_controller = True
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await backend.async_stop()
            raise

    coordinator = _StartupHarness(hass=_startup_hass(), backend=backend, start=start)
    callbacks = []
    entry = SimpleNamespace(
        state=ConfigEntryState.SETUP_IN_PROGRESS,
        async_on_state_change=lambda callback: (
            callbacks.append(callback) or (lambda: callbacks.remove(callback))
        ),
    )
    coordinator.ensure_startup_lifecycle(entry)

    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_wait_for_initial_startup()
    await started.wait()
    task = coordinator._startup_task
    entry.state = ConfigEntryState.SETUP_RETRY
    callbacks[0]()
    assert task.cancelling() == 0
    assert backend._owns_controller is True

    entry.state = ConfigEntryState.NOT_LOADED
    callbacks[0]()
    await coordinator.async_cancel_initial_startup()
    assert task.cancelled()
    assert backend._owns_controller is False


@pytest.mark.asyncio
async def test_initial_startup_preserves_authentication_errors(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "_INITIAL_SETUP_WAIT_SLICE", 0.2)

    async def start() -> None:
        raise HomeAssistantError("invalid key provider")

    coordinator = _StartupHarness(
        hass=_startup_hass(), backend=SimpleNamespace(), start=start
    )
    with pytest.raises(HomeAssistantError, match="invalid key provider"):
        await coordinator.async_wait_for_initial_startup()


@pytest.mark.asyncio
async def test_initial_setup_total_deadline_cleans_up_then_becomes_retryable(
    monkeypatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "_INITIAL_SETUP_TOTAL_BUDGET", 0.01)
    monkeypatch.setattr(coordinator_module, "_INITIAL_SETUP_WAIT_SLICE", 0.2)
    backend = SimpleNamespace(_owns_controller=False, started=False)

    async def stop() -> None:
        backend._owns_controller = False

    backend.async_stop = stop

    async def start() -> None:
        backend._owns_controller = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await backend.async_stop()
            raise

    coordinator = _StartupHarness(hass=_startup_hass(), backend=backend, start=start)
    with pytest.raises(ConfigEntryNotReady, match="300 second budget"):
        await coordinator.async_wait_for_initial_startup()

    assert coordinator._startup_retryable
    assert backend._owns_controller is False


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_thin_owner_fenced(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "_INITIAL_SETUP_CLEANUP_BUDGET", 0.01)
    backend = SimpleNamespace(_owns_controller=True, started=False)

    async def stop() -> None:
        await asyncio.sleep(1)
        backend._owns_controller = False

    backend.async_stop = stop
    coordinator = _StartupHarness(
        hass=_startup_hass(), backend=backend, start=lambda: asyncio.sleep(0)
    )

    with pytest.raises(HomeAssistantError, match="cleanup did not prove disconnect"):
        await coordinator.async_cancel_initial_startup()

    assert coordinator._startup_cleanup_failed
    assert backend._owns_controller is True


@pytest.mark.asyncio
async def test_entry_setup_keeps_coordinator_until_retry_then_forwards_once(
    monkeypatch,
) -> None:
    created = []
    forwarded = []

    class _Coordinator:
        def __init__(self, _hass, entry, **_kwargs) -> None:
            self._startup_config_snapshot = (
                dict(entry.data),
                dict(entry.options),
            )
            self.wait_calls = 0
            self.entry = entry
            created.append(self)

        def ensure_startup_lifecycle(self, entry) -> None:
            assert entry is self.entry

        def detach_startup_lifecycle(self) -> None:
            return

        async def async_wait_for_initial_startup(self) -> None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise ConfigEntryNotReady("discovery remains active")

    async def forward(_entry, platforms) -> None:
        forwarded.append(tuple(platforms))

    class _Services:
        @staticmethod
        def has_service(_domain, _service) -> bool:
            return True

    callbacks = []
    entry = SimpleNamespace(
        entry_id="staged-startup-entry",
        data={CONF_BACKEND: BACKEND_NATIVE, "ble_device": "target"},
        options={},
        async_on_unload=callbacks.append,
        add_update_listener=lambda _listener: lambda: None,
    )
    hass = SimpleNamespace(
        services=_Services(),
        data={},
        config_entries=SimpleNamespace(async_forward_entry_setups=forward),
    )

    monkeypatch.setattr(integration, "OpenRBusCoordinator", _Coordinator)
    monkeypatch.setattr(integration, "_thin_runtime", lambda *_args: (None, None))
    monkeypatch.setattr(
        integration, "async_load_access_profile", AsyncMock(return_value={})
    )
    monkeypatch.setattr(integration, "async_save_access_profile", AsyncMock())
    monkeypatch.setattr(
        integration,
        "schedule_first_refresh_in_background",
        lambda *_args, **_kwargs: asyncio.create_task(asyncio.sleep(0)),
    )
    monkeypatch.setattr(
        "custom_components.openrbus.register_entities.migrate_stable_registry_ids",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.register_entities.cleanup_legacy_sensor_entities",
        lambda *_args: None,
    )

    with pytest.raises(ConfigEntryNotReady, match="discovery remains active"):
        await integration.async_setup_entry(hass, entry)
    first_coordinator = entry.runtime_data
    assert created == [first_coordinator]
    assert forwarded == []

    assert await integration.async_setup_entry(hass, entry)
    assert entry.runtime_data is first_coordinator
    assert first_coordinator.wait_calls == 2
    assert created == [first_coordinator]
    assert forwarded == [tuple(integration.PLATFORMS)]


@pytest.mark.asyncio
async def test_home_assistant_config_entry_add_saves_after_not_ready(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(ConfigEntry, "async_migrate", AsyncMock(return_value=True))
    hass = HomeAssistant(str(tmp_path))
    saves = []
    calls = 0

    class _Entries:
        def __init__(self) -> None:
            self.data = {}

        def __setitem__(self, key, value) -> None:
            self.data[key] = value

    class _Component:
        async def async_setup_entry(self, _hass, _entry) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConfigEntryNotReady("initial discovery still running")
            return True

    class _Integration:
        domain = integration.DOMAIN
        logger = logging.getLogger(integration.DOMAIN)

        async def async_get_component(self):
            return _Component()

        async def async_get_platform(self, _platform):
            return object()

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=integration.DOMAIN,
        title="staged setup",
        data=MappingProxyType({}),
        options=MappingProxyType({}),
        source="user",
        unique_id="staged-setup-test",
        entry_id="staged-setup-test",
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    entry.supports_unload = True
    entry.supports_remove_device = True
    entry._integration_for_domain = _Integration()

    async def setup(_entry_id):
        async with entry.setup_lock:
            await entry.async_setup(hass, integration=entry._integration_for_domain)

    manager = object.__new__(ConfigEntries)
    manager.hass = hass
    manager._entries = _Entries()
    manager.async_update_issues = lambda: None
    manager._async_dispatch = lambda *_args: None
    manager.async_setup = setup
    manager._async_schedule_save = lambda: saves.append("saved")

    await ConfigEntries.async_add(manager, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert manager._entries.data[entry.entry_id] is entry
    assert saves == ["saved"]


@pytest.mark.asyncio
async def test_real_config_entry_retry_unload_reload_and_poll_io(
    tmp_path, monkeypatch
) -> None:
    """Exercise setup retry callbacks and replacement through HA's entry API."""
    from datetime import timedelta

    from custom_components.openrbus.validity import RegisterValidityTracker

    monkeypatch.setattr(ConfigEntry, "async_migrate", AsyncMock(return_value=True))
    monkeypatch.setattr(coordinator_module.er, "async_get", lambda _hass: None)
    monkeypatch.setattr(integration, "_thin_runtime", lambda *_args: (None, None))
    monkeypatch.setattr(
        integration, "async_load_access_profile", AsyncMock(return_value={})
    )
    monkeypatch.setattr(integration, "async_save_access_profile", AsyncMock())
    monkeypatch.setattr(
        "custom_components.openrbus.register_entities.migrate_stable_registry_ids",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.register_entities.cleanup_inactive_zone_entities",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.register_entities.cleanup_legacy_sensor_entities",
        lambda *_args: None,
    )

    hass = HomeAssistant(str(tmp_path))
    hass.config.components.add(integration.DOMAIN)
    startup_release = asyncio.Event()
    created = []
    read_calls: list[tuple[ObjectAddress, ...]] = []

    class _Coordinator:
        def __init__(self, _hass, entry, **_kwargs) -> None:
            self.config_entry = entry
            self._startup_config_snapshot = (dict(entry.data), dict(entry.options))
            self._shutting_down = False
            self._shutdown_complete = False
            self.effective_access_levels = {1: 3}
            self.backend_mode = BACKEND_NATIVE
            self._startup_task = None
            self.startup_waits = 0
            self.shutdown_calls = 0
            self._openrbus_polling_coordinators = {}
            address = ObjectAddress(0x2001, 0x02)

            async def read_objects(addresses, *, node, trace_failure=False):
                del node, trace_failure
                read_calls.append(tuple(addresses))
                return tuple(GenericRead(1, item, b"\x01", 1) for item in addresses)

            self.async_read_objects = read_objects
            self.poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
            self.poller.parent = self
            self.poller.hass = _hass
            self.poller.registers = ((1, address),)
            self.poller.register_metadata = {}
            self.poller.group = "standard"
            self.poller.validity = RegisterValidityTracker(timedelta(days=1))
            self.poller._diagnostic_poll_count = 0
            self.poller._diagnostic_success_items = 0
            self.poller._diagnostic_failed_items = 0
            self.poller._diagnostic_error_counts = {}
            self.poller._diagnostic_subtype_counts = {}
            self.poller._diagnostic_item_failures = []
            self.poller._diagnostic_available_items = 0
            self.poller._diagnostic_total_items = 0
            self.poller._diagnostic_availability_delta = 0
            self.poller._diagnostic_registry_disabled_count = 0
            self._openrbus_polling_coordinators = {"standard": self.poller}
            created.append(self)

        def ensure_startup_lifecycle(self, _entry) -> None:
            return

        def detach_startup_lifecycle(self) -> None:
            return

        async def _run_startup(self) -> None:
            await startup_release.wait()
            values = await self.poller._async_poll_data()
            assert values

        async def async_wait_for_initial_startup(self) -> None:
            self.startup_waits += 1
            if self._startup_task is None:
                self._startup_task = asyncio.create_task(self._run_startup())
            done, _ = await asyncio.wait(
                {self._startup_task}, timeout=0.1 if startup_release.is_set() else 0
            )
            if not done:
                raise ConfigEntryNotReady("discovery remains active")
            self._startup_task.result()

        async def async_shutdown(self) -> None:
            self.shutdown_calls += 1
            self._shutting_down = True
            if self._startup_task is not None and not self._startup_task.done():
                self._startup_task.cancel()
                await asyncio.gather(self._startup_task, return_exceptions=True)
            self.effective_access_levels.clear()
            self._shutdown_complete = True

    monkeypatch.setattr(integration, "OpenRBusCoordinator", _Coordinator)

    class _Component:
        async def async_setup_entry(self, setup_hass, entry):
            return await integration.async_setup_entry(setup_hass, entry)

        async def async_unload_entry(self, setup_hass, entry):
            return await integration.async_unload_entry(setup_hass, entry)

        async def async_remove_entry(self, setup_hass, entry):
            return await integration.async_remove_entry(setup_hass, entry)

    class _Integration:
        domain = integration.DOMAIN
        logger = logging.getLogger(integration.DOMAIN)

        async def async_get_component(self):
            return _Component()

        async def async_get_platform(self, _platform):
            return object()

    class _Entries:
        def __init__(self) -> None:
            self.data = {}

        def __getitem__(self, entry_id):
            return self.data[entry_id]

        def __setitem__(self, entry_id, entry):
            self.data[entry_id] = entry

        def __contains__(self, entry_id):
            return entry_id in self.data

    manager = object.__new__(ConfigEntries)
    manager.hass = hass
    manager._entries = _Entries()
    manager.flow = SimpleNamespace(async_progress_by_handler=lambda *_a, **_k: ())
    manager.async_update_issues = lambda: None
    manager._async_dispatch = lambda *_args: None
    manager._async_schedule_save = lambda: None
    manager.async_forward_entry_setups = AsyncMock()
    manager.async_unload_platforms = AsyncMock(return_value=True)
    manager.async_entries = lambda _domain: [entry]
    manager.async_get_entry = lambda entry_id: manager._entries.data.get(entry_id)
    manager._hass_config = {}
    hass.config_entries = manager

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=integration.DOMAIN,
        title="lifecycle test",
        data=MappingProxyType({CONF_BACKEND: BACKEND_NATIVE, "ble_device": "target"}),
        options=MappingProxyType({}),
        source="user",
        unique_id="lifecycle-test",
        entry_id="lifecycle-test",
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    entry.supports_unload = True
    entry.supports_remove_device = True
    entry._integration_for_domain = _Integration()

    def no_refresh(*_args, **_kwargs):
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(integration, "schedule_first_refresh_in_background", no_refresh)
    await manager.async_add(entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    initial = entry.runtime_data
    pending_task = initial._startup_task
    assert initial.shutdown_calls == 0

    # A changed options snapshot fences the pending owner and starts a new
    # coordinator; retries with the same snapshot continue that new task.
    manager.async_update_entry(
        entry, options=MappingProxyType({"updated_during_retry": True})
    )
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=entry._integration_for_domain)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    first = entry.runtime_data
    assert first is not initial
    assert initial._shutdown_complete
    assert pending_task.cancelled()
    assert first.shutdown_calls == 0

    startup_release.set()
    async with entry.setup_lock:
        await entry.async_setup(hass, integration=entry._integration_for_domain)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is first
    assert first._startup_task is not pending_task
    assert first.effective_access_levels == {1: 3}
    assert read_calls == [(ObjectAddress(0x2001, 0x02),)]
    assert first.poller._diagnostic_poll_count == 1

    assert await manager.async_reload(entry.entry_id)
    second = entry.runtime_data
    assert second is not first
    assert first._shutdown_complete
    assert second.effective_access_levels == {1: 3}
    assert len(read_calls) == 2

    entry.disabled_by = "user"
    assert await manager.async_reload(entry.entry_id)
    assert not hasattr(entry, "runtime_data")
    entry.disabled_by = None
    assert await manager.async_reload(entry.entry_id)
    third = entry.runtime_data
    assert third is not second
    assert third._shutdown_complete is False
    assert len(read_calls) == 3

    async with entry.setup_lock:
        await entry.async_unload(hass, integration=entry._integration_for_domain)
        await entry.async_remove(hass)
    assert third.shutdown_calls >= 1


@pytest.mark.asyncio
async def test_setup_retains_runtime_owner_when_bounded_shutdown_is_unproven(
    monkeypatch,
) -> None:
    monkeypatch.setattr(integration, "_INITIAL_SETUP_CLEANUP_BUDGET", 0.01)
    monkeypatch.setattr(integration, "_thin_runtime", lambda *_args: (None, None))
    monkeypatch.setattr(
        integration, "async_load_access_profile", AsyncMock(return_value={})
    )
    monkeypatch.setattr(integration, "async_save_access_profile", AsyncMock())
    cleanup_cancelled = asyncio.Event()
    created = []

    class _Coordinator:
        def __init__(self, *_args, **_kwargs) -> None:
            created.append(self)

        async def async_shutdown(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

        def detach_startup_lifecycle(self) -> None:
            raise AssertionError("unproven shutdown must retain the owner")

    old = _Coordinator()
    old._shutting_down = True
    old._shutdown_complete = False
    monkeypatch.setattr(integration, "OpenRBusCoordinator", _Coordinator)

    entry = SimpleNamespace(
        entry_id="cleanup-fence",
        data={CONF_BACKEND: BACKEND_NATIVE, "ble_device": "target"},
        options={},
        runtime_data=old,
    )
    hass = SimpleNamespace(data={})

    with pytest.raises(ConfigEntryNotReady, match="cleanup is still pending"):
        await integration.async_setup_entry(hass, entry)

    assert cleanup_cancelled.is_set()
    assert entry.runtime_data is old
    assert created == [old]


def test_transport_selection_has_exactly_two_modes() -> None:
    capability = ThinRpcCapability("request", "poll", "diagnostics")
    assert select_backend_mode(BACKEND_NATIVE, capability) == BACKEND_NATIVE
    assert select_backend_mode(BACKEND_THIN_RPC, capability) == BACKEND_THIN_RPC
    for value in (None, "auto", "legacy", "custom_runtime"):
        with pytest.raises(HomeAssistantError, match="Unknown OpenRBus transport"):
            select_backend_mode(value, capability)


@pytest.mark.asyncio
async def test_transport_migration_validation_is_read_only_and_always_stops(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class _Candidate:
        def __init__(self, _hass, entry, **_kwargs) -> None:
            assert entry.entry_id == "migration"

        async def async_start(self) -> None:
            calls.append("start")

        async def async_shutdown(self) -> None:
            calls.append("stop")

    monkeypatch.setattr("custom_components.openrbus.OpenRBusCoordinator", _Candidate)
    monkeypatch.setattr(
        "custom_components.openrbus._thin_runtime", lambda *_args: (None, None)
    )
    await _async_validate_transport_migration(
        _hass(),
        SimpleNamespace(entry_id="migration"),
        {CONF_BACKEND: BACKEND_NATIVE, "ble_device": "AA:BB:CC:DD:EE:FF"},
    )
    assert calls == ["start", "stop"]


def test_enabled_typed_projection_is_not_shadowed_by_disabled_legacy_sensor() -> None:
    disabled = SimpleNamespace(disabled=True)
    enabled = SimpleNamespace(disabled=False)

    assert _should_poll_registry_entries(())
    assert not _should_poll_registry_entries((disabled,))
    assert _should_poll_registry_entries((disabled, enabled))


def test_invalid_value_retirement_option_is_interpreted_as_minutes() -> None:
    assert _invalid_value_retirement_period(120) == timedelta(hours=2)
    assert _invalid_value_retirement_period(1) == timedelta(minutes=1)
    assert _invalid_value_retirement_period(0) == timedelta(minutes=1)
    assert _invalid_value_retirement_period("invalid") == timedelta(
        minutes=DEFAULT_INVALID_VALUE_DISABLE_AFTER
    )


def test_deferred_zone_discovery_only_reloads_when_slot_eligibility_changes() -> None:
    active = ZoneProfile(4, 1, 2)
    inactive = ZoneProfile(4, 1, 0)
    same_selection_new_label = ZoneProfile(4, 1, 2, friendly_name="Wohnzimmer")

    assert _zone_selection_changed({}, {(4, 1): active})
    assert _zone_selection_changed({(4, 1): active}, {(4, 1): inactive})
    assert _zone_selection_changed({}, {(4, 1): inactive})
    assert not _zone_selection_changed({(4, 1): inactive}, {(4, 1): inactive})
    assert not _zone_selection_changed(
        {(4, 1): active}, {(4, 1): same_selection_new_label}
    )
    assert not _zone_selection_changed(
        {(4, 1): active},
        {},
        {(4, 1): ZoneReadState.CONFIRMED_ACTIVE},
        {(4, 1): ZoneReadState.UNKNOWN},
    )
    assert _zone_selection_changed(
        {},
        {},
        {(4, 1): ZoneReadState.UNKNOWN},
        {(4, 1): ZoneReadState.CONFIRMED_DISABLED},
    )
    assert _zone_selection_changed(
        {},
        {},
        {(4, 1): ZoneReadState.UNKNOWN},
        {(4, 1): ZoneReadState.CONFIRMED_ACTIVE},
    )


@pytest.mark.asyncio
async def test_deferred_zone_discovery_reloads_once_for_new_active_slot(
    monkeypatch,
) -> None:
    reloads: list[str] = []
    refreshes: list[str] = []

    class _ZoneEntity:
        def async_refresh_zone_name(self) -> None:
            refreshes.append("refreshed")

    async def _no_wait(_seconds: float) -> None:
        return None

    async def _discover(self) -> bool:
        self.zone_profiles = {(4, 1): ZoneProfile(4, 1, 2)}
        return False

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.sleep", _no_wait
    )
    coordinator = SimpleNamespace(
        _shutting_down=False,
        _entry_id="entry",
        zone_profiles={},
        zone_profile_states={},
        _zone_entities={_ZoneEntity()},
        _async_discover_zone_profiles=lambda: _discover(coordinator),
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_schedule_reload=reloads.append)
        ),
    )

    await OpenRBusCoordinator._async_retry_zone_discovery(coordinator)

    assert refreshes == ["refreshed"]
    assert reloads == ["entry"]


@pytest.mark.asyncio
async def test_zone_discovery_continues_after_an_earlier_node_read_failure(
    monkeypatch,
) -> None:
    key = ("zone-discovery-fairness-test", "AA:BB")
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)
    calls: list[tuple[int, int]] = []
    nodes = tuple(
        SimpleNamespace(identity=SimpleNamespace(node=node, model=f"Node {node}"))
        for node in (1, 4)
    )

    async def _read(address, *, node, **_kwargs):
        calls.append((node, address.index))
        if node == 1:
            raise HomeAssistantError("CP020 unavailable")
        if address.index == coordinator_module.ZONE_FUNCTION_INDEX:
            return SimpleNamespace(value=2)
        return SimpleNamespace(value="")

    monkeypatch.setattr(coordinator_module, "catalog_for_node", lambda *_a, **_k: ())
    monkeypatch.setattr(coordinator_module, "zone_function_slots", lambda _rows: (1,))
    parent = SimpleNamespace(
        inventories=nodes,
        devices=nodes,
        zone_profiles={},
        zone_profile_states={},
        _zone_profile_cache_key=key,
        write_enabled=False,
        experimental_writes=False,
        async_read_object=_read,
    )

    complete = await OpenRBusCoordinator._async_discover_zone_profiles(parent)

    assert not complete
    assert calls == [
        (1, coordinator_module.ZONE_FUNCTION_INDEX),
        (4, coordinator_module.ZONE_FUNCTION_INDEX),
        (4, coordinator_module.ZONE_FRIENDLY_NAME_INDEX),
        (4, coordinator_module.ZONE_SHORT_NAME_INDEX),
    ]
    assert parent.zone_profiles[(4, 1)].active
    assert parent.zone_profile_states[(1, 1)] is ZoneReadState.UNKNOWN
    assert parent.zone_profile_states[(4, 1)] is ZoneReadState.CONFIRMED_ACTIVE
    assert parent.zone_profiles[(4, 1)].node_name == "Node 4"
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)


@pytest.mark.asyncio
async def test_selector_refresh_retains_profile_but_marks_read_unknown(
    monkeypatch,
) -> None:
    key = ("zone-stale-profile-test", "AA:CC")
    node = SimpleNamespace(identity=SimpleNamespace(node=4, model="Node 4"))

    async def _read(_address, *, node, **_kwargs):
        raise HomeAssistantError("CP020 unavailable")

    monkeypatch.setattr(coordinator_module, "catalog_for_node", lambda *_a, **_k: ())
    monkeypatch.setattr(coordinator_module, "zone_function_slots", lambda _rows: (1,))
    parent = SimpleNamespace(
        inventories=(node,),
        devices=(node,),
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.CONFIRMED_ACTIVE},
        _zone_confirmed_active_slots={(4, 1)},
        _zone_profile_cache_key=key,
        write_enabled=False,
        experimental_writes=False,
        async_read_object=_read,
    )

    assert not await OpenRBusCoordinator._async_discover_zone_profiles(parent)
    assert parent.zone_profiles[(4, 1)].active
    assert parent.zone_profile_states[(4, 1)] is ZoneReadState.UNKNOWN
    from custom_components.openrbus.zones import zone_is_active

    assert not zone_is_active(parent, 4, 1)
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)


@pytest.mark.asyncio
async def test_successful_zero_read_confirms_disabled_instead_of_unknown(
    monkeypatch,
) -> None:
    key = ("zone-confirmed-zero-read-test", "AA:CF")
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)
    node = SimpleNamespace(identity=SimpleNamespace(node=4, model="Node 4"))

    async def _read(address, *, node, **_kwargs):
        assert address == ObjectAddress(coordinator_module.ZONE_FUNCTION_INDEX, 1)
        return GenericRead(node, address, b"\x00", 0)

    monkeypatch.setattr(coordinator_module, "catalog_for_node", lambda *_a, **_k: ())
    monkeypatch.setattr(coordinator_module, "zone_function_slots", lambda _rows: (1,))
    coordinator = SimpleNamespace(
        inventories=(node,),
        devices=(node,),
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.CONFIRMED_ACTIVE},
        _zone_confirmed_active_slots={(4, 1)},
        _zone_profile_cache_key=key,
        write_enabled=False,
        experimental_writes=False,
        async_read_object=_read,
    )

    assert await OpenRBusCoordinator._async_discover_zone_profiles(coordinator)
    assert coordinator.zone_profiles[(4, 1)].function == 0
    assert coordinator.zone_profile_states[(4, 1)] is ZoneReadState.CONFIRMED_DISABLED
    assert (4, 1) not in coordinator._zone_confirmed_active_slots
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("was_active", [False, True])
async def test_unrecognized_selector_retains_only_in_session_active_history(
    monkeypatch, was_active: bool
) -> None:
    key = ("zone-unrecognized-function-test", str(was_active))
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)
    node = SimpleNamespace(identity=SimpleNamespace(node=4, model="Node 4"))

    async def _read(address, *, node, **_kwargs):
        assert address == ObjectAddress(coordinator_module.ZONE_FUNCTION_INDEX, 1)
        return GenericRead(node, address, b"\xff", 255)

    monkeypatch.setattr(coordinator_module, "catalog_for_node", lambda *_a, **_k: ())
    monkeypatch.setattr(coordinator_module, "zone_function_slots", lambda _rows: (1,))
    active_slots = {(4, 1)} if was_active else set()
    coordinator = SimpleNamespace(
        inventories=(node,),
        devices=(node,),
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)} if was_active else {},
        zone_profile_states={(4, 1): ZoneReadState.CONFIRMED_ACTIVE}
        if was_active
        else {},
        _zone_confirmed_active_slots=active_slots,
        _zone_profile_cache_key=key,
        write_enabled=False,
        experimental_writes=False,
        async_read_object=_read,
    )

    assert not await OpenRBusCoordinator._async_discover_zone_profiles(coordinator)
    assert coordinator.zone_profiles[(4, 1)].function == 255
    assert coordinator.zone_profile_states[(4, 1)] is ZoneReadState.UNKNOWN
    assert ((4, 1) in coordinator._zone_confirmed_active_slots) is was_active
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)


@pytest.mark.asyncio
async def test_selector_refresh_does_not_reload_or_clear_on_unknown_read(
    monkeypatch,
) -> None:
    reloads: list[str] = []

    async def _no_wait(_seconds: float) -> None:
        return None

    async def _discover(self) -> bool:
        self.zone_profile_states = {(4, 1): ZoneReadState.UNKNOWN}
        coordinator._shutting_down = True
        return False

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.sleep", _no_wait
    )
    coordinator = SimpleNamespace(
        _shutting_down=False,
        _entry_id="entry",
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.CONFIRMED_ACTIVE},
        _zone_entities=set(),
        _async_discover_zone_profiles=lambda: _discover(coordinator),
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_schedule_reload=reloads.append)
        ),
    )

    await OpenRBusCoordinator._async_monitor_zone_profiles(coordinator)

    assert reloads == []
    assert coordinator.zone_profiles[(4, 1)].active


@pytest.mark.asyncio
async def test_unknown_zone_recovers_active_and_reloads_once(monkeypatch) -> None:
    reloads: list[str] = []
    refreshes: list[str] = []

    class _ZoneEntity:
        def async_refresh_zone_name(self) -> None:
            refreshes.append("refreshed")

    async def _no_wait(_seconds: float) -> None:
        return None

    async def _discover(self) -> bool:
        self.zone_profile_states[(4, 1)] = ZoneReadState.CONFIRMED_ACTIVE
        self.zone_profiles[(4, 1)] = ZoneProfile(4, 1, 2)
        return False

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.sleep", _no_wait
    )
    coordinator = SimpleNamespace(
        _shutting_down=False,
        _entry_id="entry",
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.UNKNOWN},
        _zone_entities={_ZoneEntity()},
        _async_discover_zone_profiles=lambda: _discover(coordinator),
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_schedule_reload=reloads.append)
        ),
    )

    await OpenRBusCoordinator._async_retry_zone_discovery(coordinator)

    assert reloads == ["entry"]
    assert refreshes == ["refreshed"]


@pytest.mark.asyncio
async def test_exhausted_unknown_retries_fall_back_to_slow_monitor(monkeypatch) -> None:
    attempts = 0
    monitors: list[str] = []

    async def _no_wait(_seconds: float) -> None:
        return None

    async def _discover(self) -> bool:
        nonlocal attempts
        attempts += 1
        self.zone_profile_states[(4, 1)] = ZoneReadState.UNKNOWN
        return False

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.sleep", _no_wait
    )
    coordinator = SimpleNamespace(
        _shutting_down=False,
        zone_profiles={},
        zone_profile_states={(4, 1): ZoneReadState.UNKNOWN},
        _zone_entities=set(),
        _async_discover_zone_profiles=lambda: _discover(coordinator),
        _schedule_zone_profile_monitor=lambda: monitors.append("scheduled"),
    )

    await OpenRBusCoordinator._async_retry_zone_discovery(coordinator)

    assert attempts == coordinator_module._ZONE_DISCOVERY_MAX_RETRIES
    assert monitors == ["scheduled"]


@pytest.mark.asyncio
async def test_selector_refresh_confirms_zero_and_reloads_once(monkeypatch) -> None:
    reloads: list[str] = []

    async def _no_wait(_seconds: float) -> None:
        return None

    async def _discover(self) -> bool:
        self.zone_profiles[(4, 1)] = ZoneProfile(4, 1, 0)
        self.zone_profile_states[(4, 1)] = ZoneReadState.CONFIRMED_DISABLED
        return True

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.sleep", _no_wait
    )
    coordinator = SimpleNamespace(
        _shutting_down=False,
        _entry_id="entry",
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.CONFIRMED_ACTIVE},
        _zone_entities=set(),
        _async_discover_zone_profiles=lambda: _discover(coordinator),
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_schedule_reload=reloads.append)
        ),
    )

    await OpenRBusCoordinator._async_monitor_zone_profiles(coordinator)

    assert reloads == ["entry"]
    assert coordinator.zone_profile_states[(4, 1)] is ZoneReadState.CONFIRMED_DISABLED


@pytest.mark.asyncio
async def test_polling_stops_zone_rows_while_function_state_is_unknown(
    monkeypatch,
) -> None:
    address = ObjectAddress(0x346A, 0x01)
    identity = SimpleNamespace(node=4, family="Ehc-16")
    calls: list[tuple[ObjectAddress, ...]] = []

    async def _read_objects(addresses, *, node, trace_failure=False):
        del node, trace_failure
        calls.append(tuple(addresses))
        return ()

    parent = SimpleNamespace(
        _shutting_down=False,
        backend_mode=BACKEND_NATIVE,
        effective_access_levels={},
        config_entry=SimpleNamespace(options={}),
        inventories=(SimpleNamespace(identity=identity),),
        devices=(),
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.UNKNOWN},
        async_read_objects=_read_objects,
    )
    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller.parent = parent
    poller.hass = _hass()
    poller.registers = ((4, address),)
    poller.register_metadata = {
        (4, address): SimpleNamespace(address=address, readable=True)
    }
    poller.group = "standard"
    poller.validity = SimpleNamespace(
        is_expired=lambda _key: False,
        is_quarantined=lambda _key, _scope: False,
        observe=lambda *_args: SimpleNamespace(expired=False),
        is_valid=lambda *_args: True,
        quarantine_deterministic_failure=lambda *_args: None,
    )
    poller._diagnostic_poll_count = 0
    poller._diagnostic_success_items = 0
    poller._diagnostic_failed_items = 0
    poller._diagnostic_error_counts = {}
    poller._diagnostic_subtype_counts = {}
    poller._diagnostic_item_failures = []
    poller._diagnostic_available_items = 0
    poller._diagnostic_total_items = 0
    poller._diagnostic_availability_delta = 0
    poller._diagnostic_registry_disabled_count = 0

    result = await poller._async_poll_data()

    assert result == {}
    assert calls == []


@pytest.mark.asyncio
async def test_initial_refresh_is_owned_background_task() -> None:
    calls: list[tuple[object, str]] = []

    class _Entry:
        def async_create_background_task(self, hass, coroutine, *, name):
            calls.append((hass, name))
            return asyncio.create_task(coroutine)

    class _Coordinator:
        async def async_config_entry_first_refresh(self):
            return "refreshed"

    hass = object()
    entry = _Entry()
    task = schedule_first_refresh_in_background(
        hass, entry, _Coordinator(), name="test initial refresh"
    )
    background_task = schedule_background_task(
        hass,
        entry,
        asyncio.sleep(0, result="completed"),
        name="test deferred work",
    )

    assert calls == [(hass, "test initial refresh"), (hass, "test deferred work")]
    assert await task == "refreshed"
    assert await background_task == "completed"


class _Backend:
    def __init__(self, *_args, **_kwargs) -> None:
        self.started = False
        self.stopped = False
        self.reads: list[tuple[ObjectAddress, int]] = []

    async def async_start(self) -> None:
        self.started = True

    async def async_stop(self) -> None:
        self.stopped = True
        self.started = False

    async def async_discover_devices(self):
        return ("device",)

    async def async_read_object(self, address, *, node=0xFF):
        self.reads.append((address, node))
        return GenericRead(node, address, b"\x01", 1)

    async def async_read_objects(self, addresses, *, node=0xFF):
        return tuple(
            await self.async_read_object(address, node=node) for address in addresses
        )

    async def async_write_object(
        self, node, address, value, *, allow_unsafe=False, verify=True
    ):
        return SimpleNamespace(
            node=node,
            address=address,
            value=value,
            allow_unsafe=allow_unsafe,
            verify=verify,
        )


@pytest.mark.asyncio
async def test_coordinator_dispatches_native_and_thin_without_fallback(
    monkeypatch,
) -> None:
    native = _Backend()
    thin = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: native,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.ThinRpcBackend", lambda *a, **k: thin
    )
    native_coordinator = OpenRBusCoordinator(_hass(), _entry(BACKEND_NATIVE))
    thin_coordinator = OpenRBusCoordinator(_hass(), _entry(BACKEND_THIN_RPC))
    assert native_coordinator.backend_mode == BACKEND_NATIVE
    assert thin_coordinator.backend_mode == BACKEND_THIN_RPC
    await native_coordinator.async_start()
    await thin_coordinator.async_start()
    address = ObjectAddress(0x2001, 0x02)
    assert (await native_coordinator.async_read_object(address, node=1)).value == 1
    assert (await thin_coordinator.async_read_object(address, node=2)).value == 1
    assert native_coordinator.devices == ("device",)
    assert thin_coordinator.devices == ("device",)
    await native_coordinator.async_shutdown()
    await thin_coordinator.async_shutdown()
    assert native.stopped and thin.stopped


@pytest.mark.asyncio
async def test_coordinator_stops_zone_monitor_and_pollers_before_backend(
    monkeypatch,
) -> None:
    events: list[str] = []
    monitor_started = asyncio.Event()
    monitor_cancelled = asyncio.Event()

    async def zone_monitor() -> None:
        events.append("zone_monitor_started")
        monitor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("zone_monitor_cancelled")
            monitor_cancelled.set()
            raise

    class _OrderedBackend(_Backend):
        async def async_stop(self) -> None:
            assert monitor_cancelled.is_set()
            events.append("backend_stop")
            await super().async_stop()

    class _Poller:
        async def async_shutdown(self) -> None:
            events.append("poller_shutdown")

    backend = _OrderedBackend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    coordinator = OpenRBusCoordinator(_hass(), _entry(BACKEND_NATIVE))
    coordinator._zone_confirmed_active_slots.add((4, 1))
    coordinator._openrbus_polling_coordinators = {"fast": _Poller()}
    coordinator._zone_profile_monitor_task = asyncio.create_task(zone_monitor())
    await monitor_started.wait()

    async def finish_parent_shutdown(_coordinator) -> None:
        events.append("parent_shutdown")

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.DataUpdateCoordinator.async_shutdown",
        finish_parent_shutdown,
    )

    await coordinator.async_shutdown()

    assert events == [
        "zone_monitor_started",
        "zone_monitor_cancelled",
        "poller_shutdown",
        "backend_stop",
        "parent_shutdown",
    ]
    assert coordinator._shutting_down is True
    assert coordinator._zone_confirmed_active_slots == set()


@pytest.mark.asyncio
async def test_coordinator_does_not_mark_shutdown_complete_with_live_backend(
    monkeypatch,
) -> None:
    class _BackendThatDidNotStop(_Backend):
        async def async_stop(self) -> None:
            self.stopped = True

    backend = _BackendThatDidNotStop()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    coordinator = OpenRBusCoordinator(_hass(), _entry(BACKEND_NATIVE))
    await coordinator.async_start()

    with pytest.raises(HomeAssistantError, match="before its owner or link"):
        await coordinator.async_shutdown()

    assert coordinator._shutting_down
    assert not coordinator._shutdown_complete
    assert backend.started


@pytest.mark.asyncio
async def test_polling_coordinator_does_not_start_reads_after_parent_shutdown() -> None:
    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller.parent = SimpleNamespace(_shutting_down=True)

    assert await poller._async_poll_data() == {}


@pytest.mark.asyncio
async def test_polling_coordinator_shutdown_joins_inflight_refresh(monkeypatch) -> None:
    async def stop_schedule(_poller) -> None:
        return

    monkeypatch.setattr(
        coordinator_module.DataUpdateCoordinator, "async_shutdown", stop_schedule
    )
    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller._poll_in_progress_count = 1

    shutdown = asyncio.create_task(poller.async_shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()

    poller._poll_in_progress_count = 0
    await shutdown


@pytest.mark.asyncio
async def test_inflight_poll_stops_before_starting_next_batch(monkeypatch) -> None:
    parent = SimpleNamespace(
        _shutting_down=False,
        backend_mode=BACKEND_NATIVE,
        effective_access_levels={},
        config_entry=SimpleNamespace(options={}),
    )
    calls: list[tuple[ObjectAddress, ...]] = []

    async def read_objects(addresses, *, node, trace_failure=False):
        del trace_failure
        calls.append(tuple(addresses))
        # Model unload arriving while one physical transaction is in flight.
        parent._shutting_down = True
        return tuple(GenericRead(node, address, b"\x01", 1) for address in addresses)

    parent.async_read_objects = read_objects
    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller.parent = parent
    poller.hass = _hass()
    poller.registers = tuple(
        (1, ObjectAddress(0x2001, subindex)) for subindex in range(1, 34)
    )
    poller.group = "standard"
    poller.register_metadata = {}
    poller._diagnostic_poll_count = 0
    poller._diagnostic_success_items = 0
    poller._diagnostic_failed_items = 0
    poller._diagnostic_available_items = 0
    poller._diagnostic_total_items = 0
    poller._diagnostic_availability_delta = 0
    poller._diagnostic_error_counts = {}
    poller._diagnostic_subtype_counts = {}
    poller._diagnostic_item_failures = []
    poller._diagnostic_registry_disabled_count = 0
    poller.validity = SimpleNamespace(
        is_expired=lambda _key: False,
        is_quarantined=lambda _key, _scope: False,
        observe=lambda *_args: SimpleNamespace(expired=False),
        is_valid=lambda *_args: True,
        quarantine_deterministic_failure=lambda *_args: None,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.stable_object_id",
        lambda *_args: "test-row",
    )

    await poller._async_poll_data()

    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("discovery failed"), asyncio.CancelledError()]
)
async def test_coordinator_stops_backend_when_startup_discovery_fails(
    monkeypatch, failure
) -> None:
    backend = _Backend()

    async def fail_discovery():
        raise failure

    backend.async_discover_devices = fail_discovery
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    coordinator = OpenRBusCoordinator(_hass(), _entry(BACKEND_NATIVE))

    with pytest.raises(type(failure)):
        await coordinator.async_start()

    assert backend.started is False
    assert backend.stopped is True
    assert coordinator._discovery_attempted is True
    assert coordinator.discovery_error is failure


@pytest.mark.asyncio
async def test_coordinator_dispatches_core_gated_write(monkeypatch) -> None:
    backend = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.data.update({CONF_WRITE_ENABLED: True, CONF_EXPERIMENTAL_WRITES: True})
    coordinator = OpenRBusCoordinator(_hass(), entry)
    _confirm_zone_active(coordinator)
    address = ObjectAddress(0x346A, 0x04)
    result = await coordinator.async_write_object(
        address, 1, node=4, allow_unsafe=True, verify=True
    )
    assert result.node == 4
    assert result.address == address
    assert result.value == 1
    assert result.allow_unsafe is True
    assert result.verify is True


@pytest.mark.asyncio
async def test_coordinator_blocks_zone_write_until_cp020_is_confirmed_active(
    monkeypatch,
) -> None:
    backend = _Backend()
    writes: list[ObjectAddress] = []

    async def _write(node, address, value, **kwargs):
        writes.append(address)
        return SimpleNamespace(node=node, address=address, value=value, **kwargs)

    backend.async_write_object = _write
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.data.update({CONF_WRITE_ENABLED: True, CONF_EXPERIMENTAL_WRITES: True})
    coordinator = OpenRBusCoordinator(_hass(), entry)

    with pytest.raises(HomeAssistantError, match="not confirmed active"):
        await coordinator.async_write_object(
            ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
        )

    assert writes == []


@pytest.mark.asyncio
async def test_coordinator_blocks_write_when_preference_is_off(monkeypatch) -> None:
    backend = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    coordinator = OpenRBusCoordinator(_hass(), _entry(BACKEND_NATIVE))
    with pytest.raises(HomeAssistantError, match="write access is disabled"):
        await coordinator.async_write_object(
            ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
        )


@pytest.mark.asyncio
async def test_coordinator_blocks_requested_elevated_write_without_effective_level(
    monkeypatch,
) -> None:
    backend = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.data.update(
        {CONF_WRITE_ENABLED: True, CONF_EXPERIMENTAL_WRITES: True, "access_level": 3}
    )
    coordinator = OpenRBusCoordinator(_hass(), entry)
    with pytest.raises(HomeAssistantError, match="effective access level"):
        await coordinator.async_write_object(
            ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
        )


@pytest.mark.asyncio
async def test_higher_read_policy_does_not_raise_write_policy(monkeypatch) -> None:
    backend = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.data.update(
        {
            CONF_WRITE_ENABLED: True,
            CONF_EXPERIMENTAL_WRITES: True,
            CONF_READ_ACCESS_LEVEL: 3,
            CONF_WRITE_ACCESS_LEVEL: 1,
        }
    )

    async def read_access(address, *, node=0xFF):
        return GenericRead(node, address, b"\x03", 3)

    backend.async_read_object = read_access
    coordinator = OpenRBusCoordinator(_hass(), entry)
    assert coordinator.configured_access_level == 3
    assert coordinator.configured_write_access_level == 1
    _confirm_zone_active(coordinator)
    result = await coordinator.async_write_object(
        ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
    )
    assert result.value == 1


@pytest.mark.asyncio
async def test_coordinator_refreshes_node_access_before_elevated_write(
    monkeypatch,
) -> None:
    backend = _Backend()

    async def read_access(address, *, node=0xFF):
        backend.reads.append((address, node))
        return GenericRead(node, address, b"\x03", 3)

    backend.async_read_object = read_access
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.data.update(
        {CONF_WRITE_ENABLED: True, CONF_EXPERIMENTAL_WRITES: True, "access_level": 3}
    )
    coordinator = OpenRBusCoordinator(_hass(), entry)
    _confirm_zone_active(coordinator)

    await coordinator.async_write_object(
        ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
    )

    assert backend.reads == [(ObjectAddress(0x4002, 0x00), 4)]
    assert coordinator.effective_access_levels == {4: 3}


@pytest.mark.asyncio
async def test_independent_level_three_write_policy_reaches_backend(
    monkeypatch,
) -> None:
    backend = _Backend()

    async def read_access(address, *, node=0xFF):
        backend.reads.append((address, node))
        return GenericRead(node, address, b"\x03", 3)

    backend.async_read_object = read_access
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.data.update(
        {
            CONF_READ_ACCESS_LEVEL: 1,
            CONF_WRITE_ACCESS_LEVEL: 3,
            CONF_WRITE_ENABLED: True,
            CONF_EXPERIMENTAL_WRITES: True,
        }
    )
    coordinator = OpenRBusCoordinator(_hass(), entry)
    _confirm_zone_active(coordinator)

    result = await coordinator.async_write_object(
        ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
    )

    assert coordinator.configured_read_access_level == 1
    assert coordinator.configured_write_access_level == 3
    assert coordinator.configured_access_level == 3
    assert backend.reads == [(ObjectAddress(0x4002, 0x00), 4)]
    assert result.value == 1


def test_invalid_coordinator_backend_fails_closed() -> None:
    with pytest.raises(HomeAssistantError, match="Unsupported OpenRBus transport"):
        OpenRBusCoordinator(_hass(), _entry("legacy"))


def test_entry_options_override_stale_data(monkeypatch) -> None:
    backend = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = SimpleNamespace(
        data={CONF_BACKEND: "legacy"},
        options={CONF_BACKEND: BACKEND_NATIVE},
        entry_id="options",
        async_on_unload=lambda *_: None,
    )
    coordinator = OpenRBusCoordinator(_hass(), entry)
    assert coordinator.backend_mode == BACKEND_NATIVE


@pytest.mark.asyncio
async def test_no_write_level_is_preserved_independently_of_read_authorization(
    monkeypatch,
) -> None:
    backend = _Backend()

    backend_options = {}

    def make_backend(*args, **kwargs):
        backend_options.update(kwargs)
        return backend

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        make_backend,
    )
    entry = _entry(BACKEND_NATIVE)
    entry.options = {
        CONF_READ_ACCESS_LEVEL: 3,
        CONF_WRITE_ACCESS_LEVEL: 0,
        CONF_WRITE_ENABLED: True,
    }

    coordinator = OpenRBusCoordinator(_hass(), entry)

    assert coordinator.configured_read_access_level == 3
    assert coordinator.configured_write_access_level == 0
    # The Core transport still requests enough access for reads, while the
    # local write policy retains its distinct no-write value.
    assert coordinator.configured_access_level == 3
    assert backend_options["access_level"] == 3
    with pytest.raises(HomeAssistantError, match="set to no write"):
        await coordinator.async_write_object(
            ObjectAddress(0x346A, 0x04), 1, node=4, allow_unsafe=True
        )


def test_coordinator_persists_language_write_policy_and_poll_groups(
    monkeypatch,
) -> None:
    backend = _Backend()
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.NativeBluetoothBackend",
        lambda *a, **k: backend,
    )
    entry = SimpleNamespace(
        data={
            CONF_BACKEND: BACKEND_NATIVE,
            "ble_device": "target",
            CONF_LANGUAGE: "en",
            CONF_WRITE_ENABLED: True,
            CONF_POLL_FAST: 11,
            CONF_POLL_STANDARD: 22,
            CONF_POLL_SLOW: 33,
        },
        options={},
        entry_id="configured",
        async_on_unload=lambda *_: None,
    )
    coordinator = OpenRBusCoordinator(_hass(), entry)
    assert coordinator.language == "en"
    assert coordinator.write_enabled is True
    assert coordinator.poll_intervals["fast"].total_seconds() == 11
    assert coordinator.poll_intervals["standard"].total_seconds() == 22
    assert coordinator.poll_intervals["slow"].total_seconds() == 33
