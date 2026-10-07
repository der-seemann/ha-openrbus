"""Focused coverage for the local Native Bluetooth backend."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.authorization import AuthorizationCorrelationError
from openrbus.errors import AuthorizationKeyError, ProtocolError, RequestTimeoutError

from custom_components.openrbus.transport import NativeBluetoothBackend


class _Transport:
    def __init__(self, address: str) -> None:
        self.address = address
        self.is_connected = False
        self.disconnects = 0

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False

    async def request(self, message: bytes, *, timeout: float) -> bytes:
        raise AssertionError("discovery/read is not part of this lifecycle test")


def _backend_with_factory(factory, monkeypatch, authorizer):
    monkeypatch.setattr(
        "custom_components.openrbus.transport.CanIpGatewayAuthorizer", authorizer
    )
    return NativeBluetoothBackend(
        SimpleNamespace(),
        address="AA:BB:CC:DD:EE:FF",
        access_level=3,
        key_provider=lambda _purpose: b"1234",
        transport_factory=factory,
    )


@pytest.mark.asyncio
async def test_native_backend_owns_local_transport_lifecycle() -> None:
    created: list[_Transport] = []

    def factory(address: str) -> _Transport:
        transport = _Transport(address)
        created.append(transport)
        return transport

    backend = NativeBluetoothBackend(
        SimpleNamespace(), address="AA:BB:CC:DD:EE:FF", transport_factory=factory
    )
    await backend.async_start()
    assert backend.started
    assert backend.transport is created[0]
    await backend.async_stop()
    assert not backend.started
    assert created[0].disconnects == 1


@pytest.mark.asyncio
async def test_native_backend_retains_transport_when_disconnect_is_unproven() -> None:
    class _StickyTransport(_Transport):
        async def disconnect(self) -> None:
            self.disconnects += 1

    transport = _StickyTransport("AA:BB:CC:DD:EE:FF")
    backend = NativeBluetoothBackend(
        SimpleNamespace(),
        address="AA:BB:CC:DD:EE:FF",
        transport_factory=lambda _address: transport,
    )
    await backend.async_start()
    client = backend.client

    with pytest.raises(HomeAssistantError, match="did not prove the link is down"):
        await backend.async_stop()

    assert backend.transport is transport
    assert backend.client is client
    assert backend.started


@pytest.mark.asyncio
async def test_native_backend_retries_one_fresh_transport_on_correlation_error(
    monkeypatch,
) -> None:
    created: list[_Transport] = []

    def factory(address: str) -> _Transport:
        transport = _Transport(address)
        created.append(transport)
        return transport

    class _Authorizer:
        calls = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def authorize(self, *_args, **_kwargs):
            type(self).calls += 1
            if self.calls == 1:
                raise AuthorizationCorrelationError("stale confirmation")
            return "authenticated"

    backend = _backend_with_factory(factory, monkeypatch, _Authorizer)
    await backend.async_start()
    assert backend.started
    assert len(created) == 2
    assert created[0].disconnects == 1
    assert created[1].disconnects == 0


@pytest.mark.asyncio
async def test_native_backend_stops_after_second_correlation_error(monkeypatch) -> None:
    created: list[_Transport] = []

    def factory(address: str) -> _Transport:
        transport = _Transport(address)
        created.append(transport)
        return transport

    class _Authorizer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def authorize(self, *_args, **_kwargs):
            raise AuthorizationCorrelationError("stale confirmation")

    backend = _backend_with_factory(factory, monkeypatch, _Authorizer)
    with pytest.raises(AuthorizationCorrelationError):
        await backend.async_start()
    assert len(created) == 2
    assert [item.disconnects for item in created] == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RequestTimeoutError("timeout"), ProtocolError("malformed")]
)
async def test_native_backend_does_not_retry_non_correlation_failures(
    monkeypatch, failure
) -> None:
    created: list[_Transport] = []

    def factory(address: str) -> _Transport:
        transport = _Transport(address)
        created.append(transport)
        return transport

    class _Authorizer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def authorize(self, *_args, **_kwargs):
            raise failure

    backend = _backend_with_factory(factory, monkeypatch, _Authorizer)
    with pytest.raises(type(failure)):
        await backend.async_start()
    assert len(created) == 1
    assert created[0].disconnects == 1


@pytest.mark.asyncio
async def test_native_backend_does_not_retry_key_provider_failure(monkeypatch) -> None:
    created: list[_Transport] = []

    def factory(address: str) -> _Transport:
        transport = _Transport(address)
        created.append(transport)
        return transport

    def key_provider(_purpose: bytes):
        raise AuthorizationKeyError("bad key")

    backend = NativeBluetoothBackend(
        SimpleNamespace(),
        address="AA:BB:CC:DD:EE:FF",
        access_level=3,
        key_provider=key_provider,
        transport_factory=factory,
    )
    with pytest.raises(AuthorizationKeyError):
        await backend.async_start()
    assert len(created) == 1
    assert created[0].disconnects == 1


def test_native_backend_passes_configured_pin_to_core_transport(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _CoreTransport:
        def __init__(self, address: str, **kwargs) -> None:
            captured["address"] = address
            captured.update(kwargs)

    monkeypatch.setattr(
        "custom_components.openrbus.transport.BleakMessageTransport",
        _CoreTransport,
    )
    backend = NativeBluetoothBackend(
        SimpleNamespace(), address="AA:BB:CC:DD:EE:FF", source="hci1", passkey=123456
    )
    backend.transport_factory("AA:BB:CC:DD:EE:FF")
    assert captured == {
        "address": "AA:BB:CC:DD:EE:FF",
        "adapter": "hci1",
        "pairing_pin": 123456,
        "gateway_auth": True,
    }


def test_native_backend_resolves_persisted_ha_source_to_adapter(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _CoreTransport:
        def __init__(self, address: str, **kwargs) -> None:
            captured["address"] = address
            captured.update(kwargs)

    monkeypatch.setattr(
        "custom_components.openrbus.transport.BleakMessageTransport",
        _CoreTransport,
    )
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_scanner_by_source",
        lambda _hass, source: SimpleNamespace(adapter="hci1", source=source),
    )
    backend = NativeBluetoothBackend(
        SimpleNamespace(),
        address="AA:BB:CC:DD:EE:FF",
        source="02:00:00:00:00:02",
    )
    backend.transport_factory("AA:BB:CC:DD:EE:FF")
    assert captured["adapter"] == "hci1"


def test_native_backend_rejects_legacy_entry_without_resolved_source() -> None:
    backend = NativeBluetoothBackend(SimpleNamespace(), address="AA:BB:CC:DD:EE:FF")
    with pytest.raises(HomeAssistantError, match="adapter source unavailable"):
        backend.transport_factory("AA:BB:CC:DD:EE:FF")


@pytest.mark.asyncio
async def test_native_backend_write_policy_is_explicit() -> None:
    backend = NativeBluetoothBackend(SimpleNamespace(), address="target")
    with pytest.raises(HomeAssistantError, match="write access is disabled"):
        await backend.async_write_object(4, "346a:04", 1)


def test_native_backend_requires_target_and_valid_pairing_pin() -> None:
    with pytest.raises(HomeAssistantError, match="requires a BLE target"):
        NativeBluetoothBackend(SimpleNamespace(), address="")
    with pytest.raises(HomeAssistantError, match="pairing PIN is invalid"):
        NativeBluetoothBackend(SimpleNamespace(), address="target", passkey=1_000_000)
