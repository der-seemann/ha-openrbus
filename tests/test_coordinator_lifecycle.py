"""Coordinator coverage for the two supported OpenRBus transports."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.protocol.canip import ObjectAddress

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
from custom_components.openrbus.zones import ZoneProfile


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
    assert not _zone_selection_changed({}, {(4, 1): inactive})
    assert not _zone_selection_changed(
        {(4, 1): active}, {(4, 1): same_selection_new_label}
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
    coordinator_module._ZONE_PROFILE_CACHE.pop(key, None)
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)
    calls: list[tuple[int, int]] = []
    nodes = tuple(
        SimpleNamespace(
            identity=SimpleNamespace(node=node, model=f"Node {node}"),
        )
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
    ]
    assert parent.zone_profiles[(4, 1)].active
    assert parent.zone_profiles[(4, 1)].node_name == "Node 4"
    coordinator_module._ZONE_PROFILE_CACHE.pop(key, None)
    coordinator_module._ZONE_DISCOVERY_CURSOR.pop(key, None)


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
async def test_coordinator_stops_pollers_before_releasing_backend(monkeypatch) -> None:
    events: list[str] = []

    class _OrderedBackend(_Backend):
        async def async_stop(self) -> None:
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
    coordinator._openrbus_polling_coordinators = {"fast": _Poller()}

    async def finish_parent_shutdown(_coordinator) -> None:
        events.append("parent_shutdown")

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.DataUpdateCoordinator.async_shutdown",
        finish_parent_shutdown,
    )

    await coordinator.async_shutdown()

    assert events == ["poller_shutdown", "backend_stop", "parent_shutdown"]
    assert coordinator._shutting_down is True


@pytest.mark.asyncio
async def test_polling_coordinator_does_not_start_reads_after_parent_shutdown() -> None:
    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller.parent = SimpleNamespace(_shutting_down=True)

    assert await poller._async_poll_data() == {}


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

    assert backend.started is True
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
