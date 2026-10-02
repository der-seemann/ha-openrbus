"""Coordinator coverage for the two supported OpenRBus transports."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.protocol.canip import ObjectAddress

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
)
from custom_components.openrbus.coordinator import (
    OpenRBusCoordinator,
    _should_poll_registry_entries,
)
from custom_components.openrbus.transport import ThinRpcCapability, select_backend_mode


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
