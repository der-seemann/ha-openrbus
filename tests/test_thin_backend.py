"""Focused tests for additive Thin-RPC backend selection and privacy."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.authorization import AuthorizationCorrelationError
from openrbus.discovery import DeviceIdentity
from openrbus.errors import (
    CanOpenAbortError,
    ProtocolError,
    RequestTimeoutError,
    TransportError,
)
from openrbus.protocol.canip import ObjectAddress
from openrbus.transport.thin_gatt import (
    CAPABILITY_MARKER,
    ConnectionIdentity,
    ThinGattCorrelationError,
    ThinGattLink,
    ThinGattProfile,
    ThinGattRpcServices,
    ThinGattSession,
)

from custom_components.openrbus import (
    _default_thin_profile,
    _json_safe_read_value,
    _thin_profile,
    _thin_runtime,
)
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.const import (
    BACKEND_THIN_RPC,
    CONF_BACKEND,
    CONF_THIN_KEY_SECRET,
    CONF_THIN_PROFILE,
)
from custom_components.openrbus.coordinator import _configured_entry_data
from custom_components.openrbus.setup_observability import (
    RecoveryFenceMetrics,
    SetupResponseMetrics,
)
from custom_components.openrbus.transport import (
    _THIN_SECURE_TIMEOUT,
    _THIN_SUBSCRIPTIONS,
    MAX_FRAME_TRACE_BYTES,
    MAX_FRAME_TRACE_ENTRIES,
    HomeAssistantThinGattChannel,
    ThinRpcBackend,
    ThinRpcCapability,
    _discover_capabilities_batched,
    _PreparedThinGattMessageTransport,
    _read_effective_access_levels,
    _read_thin_access_level,
    _safe_recovery_error_chain,
    _thin_attach_mode,
    _validate_scan_frame,
    async_scan_thin_rpc_devices,
    detect_thin_rpc_capability,
    resolve_thin_rpc_capability,
    select_backend_mode,
    thin_rpc_controller_choices,
)


def test_read_service_value_serializes_scaled_decimal() -> None:
    value = _json_safe_read_value(Decimal("-5.00"))
    assert value == -5.0
    assert json.loads(json.dumps({"value": value})) == {"value": -5.0}


class _Services:
    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def async_services(self):
        return {"esphome": {name: object() for name in self.names}}


def _hass(*names: str):
    return SimpleNamespace(services=_Services(*names))


async def _async_noop() -> None:
    return None


@pytest.mark.asyncio
async def test_effective_access_level_recovers_once_after_thin_transport_timeout() -> (
    None
):
    identity = DeviceIdentity(4, 7702, 3, "SCB-10")

    class _Client:
        calls = 0

        async def read_raw(self, node, address):
            assert node == 4
            assert address == ObjectAddress(0x4002, 0x00)
            self.calls += 1
            if self.calls == 1:
                raise RequestTimeoutError("Thin-RPC response timed out")
            return b"\x03"

    client = _Client()
    recoveries = 0

    async def recover():
        nonlocal recoveries
        recoveries += 1

    assert await _read_effective_access_levels(
        client, (identity,), recover_on_transport_error=recover
    ) == {4: 3}
    assert client.calls == 2
    assert recoveries == 1


@pytest.mark.asyncio
async def test_effective_access_level_stays_fail_closed_after_retry_fails() -> None:
    identity = DeviceIdentity(4, 7702, 3, "SCB-10")

    class _Client:
        async def read_raw(self, node, address):
            raise RequestTimeoutError("Thin-RPC response timed out")

    async def recover():
        return None

    assert (
        await _read_effective_access_levels(
            _Client(), (identity,), recover_on_transport_error=recover
        )
        == {}
    )


@pytest.mark.asyncio
async def test_thin_access_proof_retries_one_transient_transport_error() -> None:
    backend = SimpleNamespace(timeout=10.0)
    attempts = 0

    async def read_object(address, *, node, timeout):
        nonlocal attempts
        assert address == ObjectAddress(0x4002, 0x00)
        assert node == 1
        assert timeout == 3.0
        attempts += 1
        if attempts == 1:
            raise RequestTimeoutError("Thin-RPC response timed out")
        return GenericRead(1, address, b"\x03", 3)

    backend.async_read_object = read_object
    assert await _read_thin_access_level(backend, 1) == 3
    assert attempts == 2


def test_thin_capability_requires_request_poll_and_diagnostics() -> None:
    capability = detect_thin_rpc_capability(
        _hass("openrbus_gatt_rpc_request", "openrbus_gatt_rpc_poll")
    )
    assert not capability.available
    assert capability.missing == ("diagnostics",)


@pytest.mark.asyncio
async def test_thin_batch_reprepares_after_link_loss(monkeypatch) -> None:
    """A dropped secure link must not strand typed poll results unavailable."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.channel = None
    backend.client = object()
    backend.session = SimpleNamespace(connected=False)
    address = ObjectAddress(0x500F, 0x00)
    recovered = GenericRead(5, address, b"\x00", 0)
    calls = 0

    async def ensure_session_ready() -> None:
        nonlocal calls
        calls += 1
        backend.session.connected = True

    async def read_batch(_client, addresses, *, node, **_kwargs):
        assert addresses == (address,)
        assert node == 5
        if calls == 1:
            backend.session.connected = False
            return (HomeAssistantError("Thin-RPC link lost; reprepare required"),)
        return (recovered,)

    backend._ensure_session_ready = ensure_session_ready
    monkeypatch.setattr(
        "custom_components.openrbus.transport._read_objects_batched", read_batch
    )

    result = await backend.async_read_objects((address,), node=5)

    assert calls == 2
    assert result == (recovered,)


@pytest.mark.asyncio
async def test_thin_batch_reprepares_after_session_error_without_link_text(
    monkeypatch,
) -> None:
    """Bounded recovery must not depend on parsing a transport error message."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = object()
    backend.session = SimpleNamespace(connected=True)
    address = ObjectAddress(0x500F, 0x00)
    recovered = GenericRead(5, address, b"\x00", 0)
    values = 0
    recoveries = 0

    async def ensure_session_ready() -> None:
        return None

    async def recover_after_transport_loss() -> None:
        nonlocal recoveries
        recoveries += 1

    async def read_batch(_client, addresses, *, node, **_kwargs):
        nonlocal values
        assert addresses == (address,)
        assert node == 5
        values += 1
        if values == 1:
            failure = HomeAssistantError("request timed out")
            failure._openrbus_error_class = "session"
            return (failure,)
        return (recovered,)

    backend._ensure_session_ready = ensure_session_ready
    backend._recover_after_transport_loss = recover_after_transport_loss
    monkeypatch.setattr(
        "custom_components.openrbus.transport._read_objects_batched", read_batch
    )

    result = await backend.async_read_objects((address,), node=5)

    assert result == (recovered,)
    assert values == 2
    assert recoveries == 1


@pytest.mark.asyncio
async def test_thin_batch_preserves_session_error_after_one_retry(monkeypatch) -> None:
    """A failed bounded retry stays visible as an object-local session error."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = object()
    backend.session = SimpleNamespace(connected=True)
    address = ObjectAddress(0x500F, 0x00)
    attempts = 0
    recoveries = 0

    async def ensure_session_ready() -> None:
        return None

    async def recover_after_transport_loss() -> None:
        nonlocal recoveries
        recoveries += 1

    async def read_batch(_client, addresses, *, node, **_kwargs):
        nonlocal attempts
        attempts += 1
        failure = HomeAssistantError("request timed out")
        failure._openrbus_error_class = "session"
        return (failure,)

    backend._ensure_session_ready = ensure_session_ready
    backend._recover_after_transport_loss = recover_after_transport_loss
    monkeypatch.setattr(
        "custom_components.openrbus.transport._read_objects_batched", read_batch
    )

    result = await backend.async_read_objects((address,), node=5)

    assert attempts == 2
    assert recoveries == 1
    assert isinstance(result[0], HomeAssistantError)
    assert result[0]._openrbus_error_class == "session"


@pytest.mark.asyncio
async def test_transport_recovery_waits_and_drains_before_reprepare() -> None:
    """Do not let a delayed physical disconnect retire the replacement epoch."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    events: list[str] = []
    backend._recovery_fence_metrics = RecoveryFenceMetrics()

    class _Session:
        identity = object()
        epoch = 1

        def retire(self) -> None:
            events.append("retire")

    backend.session = _Session()
    backend.channel = object()

    async def force_disconnect() -> bool:
        events.append("disconnect")
        return True

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        events.append("physical_disconnect")
        assert recovery_fence is backend._recovery_fence_metrics
        recovery_fence.record_state(False, False)

    async def drain_stale_frames() -> None:
        events.append("drain")

    async def ensure_session_ready() -> None:
        events.append("reprepare")

    backend._force_disconnect_current_session = force_disconnect
    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._drain_stale_frames = drain_stale_frames
    backend._ensure_session_ready = ensure_session_ready

    await backend._recover_after_transport_loss()

    assert events == [
        "disconnect",
        "retire",
        "physical_disconnect",
        "drain",
        "reprepare",
    ]
    fence = backend._recovery_fence_metrics.diagnostics()
    assert fence["attempts"] == 1
    assert fence["dispatch_acknowledged"] is True
    assert fence["disconnect_attempt"] == {
        "attempt_id": 1,
        "session_epoch": 1,
        "outcome": "service_completed",
        "exception_class": None,
    }
    assert fence["link_active"] is False
    assert fence["parent_connected"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "identity", "epoch", "expected"),
    [
        (None, object(), 1, "missing_channel"),
        (object(), object(), None, "invalid_epoch"),
        (object(), None, 1, "missing_identity"),
    ],
)
async def test_transport_recovery_records_exact_disconnect_guard(
    channel, identity, epoch, expected
) -> None:
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend.channel = channel
    backend.session = SimpleNamespace(
        identity=identity, epoch=epoch, retire=lambda: None
    )
    backend._recovery_fence_metrics = RecoveryFenceMetrics()
    dispatches: list[bool] = []

    async def force_disconnect() -> bool:
        dispatches.append(True)
        return True

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        recovery_fence.record_state(False, False)

    backend._force_disconnect_current_session = force_disconnect
    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._drain_stale_frames = _async_noop
    backend._ensure_session_ready = _async_noop

    if expected in {"missing_identity", "invalid_epoch"}:
        with pytest.raises(HomeAssistantError, match="no scoped ESPHome disconnect"):
            await backend._recover_after_transport_loss()
    else:
        await backend._recover_after_transport_loss()

    attempt = backend._recovery_fence_metrics.diagnostics()["disconnect_attempt"]
    assert attempt["outcome"] == expected
    assert dispatches == []


@pytest.mark.asyncio
async def test_transport_recovery_records_missing_session_guard() -> None:
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend.channel = object()
    backend.session = None
    backend._recovery_fence_metrics = RecoveryFenceMetrics()

    async def unexpected_dispatch() -> bool:
        raise AssertionError("guard must prevent DISCONNECT dispatch")

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        recovery_fence.record_state(False, False)

    backend._force_disconnect_current_session = unexpected_dispatch
    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._drain_stale_frames = _async_noop
    backend._ensure_session_ready = _async_noop

    with pytest.raises(HomeAssistantError, match="no scoped ESPHome disconnect"):
        await backend._recover_after_transport_loss()

    attempt = backend._recovery_fence_metrics.diagnostics()["disconnect_attempt"]
    assert attempt["outcome"] == "missing_session"


def test_safe_recovery_error_chain_redacts_unrecognized_messages() -> None:
    cause = RuntimeError("private-token 123456789abcdef")
    error = HomeAssistantError(
        "Thin-RPC physical disconnect did not complete before reload"
    )
    error.__cause__ = cause

    assert _safe_recovery_error_chain(error) == (
        "home_assistant_error:Thin-RPC physical disconnect did not complete before reload"
        " <- runtime_error:redacted"
    )


@pytest.mark.asyncio
async def test_transport_recovery_logs_safe_stage_and_fence_state(caplog) -> None:
    caplog.set_level("DEBUG", logger="custom_components.openrbus.transport")
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend.channel = object()
    backend.session = SimpleNamespace(
        identity=SimpleNamespace(gattc_if=17, conn_id=23),
        epoch=4,
        retire=lambda: None,
    )
    backend._recovery_fence_metrics = RecoveryFenceMetrics()
    backend._last_disconnect_snapshot = {
        "link_active": True,
        "parent_connected": True,
        "epoch": 4,
    }

    async def force_disconnect() -> bool:
        return True

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        recovery_fence.record_state(True, True)

    async def prepare() -> None:
        raise HomeAssistantError("Thin-RPC backend is not started")

    backend._force_disconnect_current_session = force_disconnect
    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._drain_stale_frames = _async_noop
    backend._ensure_session_ready = prepare

    with pytest.raises(HomeAssistantError, match="backend is not started"):
        await backend._recover_after_transport_loss()

    record = next(
        record.message
        for record in caplog.records
        if "THIN_RECOVERY event=failed" in record.message
    )
    assert "stage=session_prepare" in record
    assert "guard=identity_fenced" in record
    assert "expected_identity_present=True" in record
    assert "expected_epoch_valid=True" in record
    assert "observed_link=True" in record
    assert "observed_parent=True" in record
    assert "observed_epoch=4" in record
    assert "Thin-RPC backend is not started" in record


@pytest.mark.asyncio
async def test_transport_recovery_uses_scoped_disconnect_and_rearms_before_reprepare() -> (
    None
):
    events: list[str] = []

    class _RecoveryServices:
        def async_services(self):
            return {
                "esphome": {
                    name: object()
                    for name in ("proxy_openrbus_pair", "proxy_openrbus_disconnect")
                }
            }

        async def async_call(self, domain, service, data, *, blocking):
            events.append(service)

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend.hass = SimpleNamespace(services=_RecoveryServices())
    backend.pair_action = "proxy_openrbus_pair"
    backend.capability = SimpleNamespace(
        request_service="proxy_openrbus_gatt_rpc_request"
    )
    backend.channel = object()
    backend.session = SimpleNamespace(
        identity=None, epoch=13, retire=lambda: events.append("retire")
    )
    backend._recovery_fence_metrics = RecoveryFenceMetrics()

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        events.append("physical_down")
        recovery_fence.record_state(False, False)

    async def arm_pairing() -> bool:
        events.append("pair_rearm")
        return False

    async def drain() -> None:
        events.append("drain")

    async def reprepare() -> None:
        events.append("reprepare")

    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._arm_pairing_if_configured = arm_pairing
    backend._drain_stale_frames = drain
    backend._ensure_session_ready = reprepare

    await backend._recover_after_transport_loss()

    assert events == [
        "retire",
        "proxy_openrbus_disconnect",
        "physical_down",
        "drain",
        "pair_rearm",
        "reprepare",
    ]
    assert backend._recovery_fence_metrics.diagnostics()["link_active"] is False


@pytest.mark.asyncio
async def test_transport_recovery_scoped_disconnect_timeout_never_reprepares() -> None:
    events: list[str] = []

    class _RecoveryServices:
        def async_services(self):
            return {
                "esphome": {
                    name: object()
                    for name in ("proxy_openrbus_pair", "proxy_openrbus_disconnect")
                }
            }

        async def async_call(self, domain, service, data, *, blocking):
            events.append(service)

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend.hass = SimpleNamespace(services=_RecoveryServices())
    backend.pair_action = "proxy_openrbus_pair"
    backend.capability = SimpleNamespace(
        request_service="proxy_openrbus_gatt_rpc_request"
    )
    backend.channel = object()
    backend.session = None
    backend._recovery_fence_metrics = RecoveryFenceMetrics()

    async def timeout(*, recovery_fence=None) -> None:
        events.append("physical_down")
        recovery_fence.record_state(True, True)
        recovery_fence.record_timeout()
        raise HomeAssistantError("physical disconnect timeout")

    backend._wait_for_physical_disconnect = timeout
    backend._drain_stale_frames = _async_noop
    backend._ensure_session_ready = lambda: pytest.fail("must not reprepare")

    with pytest.raises(HomeAssistantError, match="physical disconnect timeout"):
        await backend._recover_after_transport_loss()
    assert events == ["proxy_openrbus_disconnect", "physical_down"]


@pytest.mark.asyncio
async def test_transport_recovery_scoped_disconnect_cancellation_propagates() -> None:
    events: list[str] = []

    class _RecoveryServices:
        def async_services(self):
            return {
                "esphome": {
                    name: object()
                    for name in ("proxy_openrbus_pair", "proxy_openrbus_disconnect")
                }
            }

        async def async_call(self, domain, service, data, *, blocking):
            events.append(service)
            raise asyncio.CancelledError

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend.hass = SimpleNamespace(services=_RecoveryServices())
    backend.pair_action = "proxy_openrbus_pair"
    backend.capability = SimpleNamespace(
        request_service="proxy_openrbus_gatt_rpc_request"
    )
    backend.channel = object()
    backend.session = None
    backend._recovery_fence_metrics = RecoveryFenceMetrics()
    backend._wait_for_physical_disconnect = lambda **kwargs: pytest.fail(
        "must not fence"
    )
    backend._drain_stale_frames = _async_noop
    backend._ensure_session_ready = _async_noop

    with pytest.raises(asyncio.CancelledError):
        await backend._recover_after_transport_loss()
    assert events == ["proxy_openrbus_disconnect"]


@pytest.mark.asyncio
async def test_transport_recovery_does_not_reprepare_after_disconnect_timeout() -> None:
    """A missing physical down boundary must fail closed before reconnect."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    events: list[str] = []
    backend._recovery_fence_metrics = RecoveryFenceMetrics()

    async def force_disconnect() -> bool:
        events.append("disconnect")
        return False

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        events.append("physical_disconnect")
        assert recovery_fence is backend._recovery_fence_metrics
        recovery_fence.record_state(True, False)
        recovery_fence.record_timeout()
        raise HomeAssistantError("Thin-RPC physical disconnect did not complete")

    async def drain_stale_frames() -> None:
        events.append("drain")

    async def ensure_session_ready() -> None:
        events.append("reprepare")

    backend.channel = object()
    backend.session = SimpleNamespace(identity=object(), epoch=9, retire=lambda: None)
    backend._force_disconnect_current_session = force_disconnect
    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._drain_stale_frames = drain_stale_frames
    backend._ensure_session_ready = ensure_session_ready

    with pytest.raises(HomeAssistantError, match="physical disconnect"):
        await backend._recover_after_transport_loss()

    assert events == ["disconnect", "physical_disconnect"]
    fence = backend._recovery_fence_metrics.diagnostics()
    assert fence["dispatch_acknowledged"] is False
    assert fence["disconnect_attempt"]["outcome"] == "service_not_acknowledged"
    assert fence["timeout_count"] == 1
    assert fence["last_timeout_class"] == "link_active"


@pytest.mark.asyncio
async def test_transport_recovery_records_disconnect_exception_class() -> None:
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._recovery_fence_metrics = RecoveryFenceMetrics()
    backend.channel = object()
    backend.session = SimpleNamespace(identity=object(), epoch=23, retire=lambda: None)

    async def force_disconnect() -> bool:
        raise HomeAssistantError("private detail must not be retained")

    async def wait_for_disconnect(*, recovery_fence=None) -> None:
        recovery_fence.record_state(False, False)

    backend._force_disconnect_current_session = force_disconnect
    backend._wait_for_physical_disconnect = wait_for_disconnect
    backend._drain_stale_frames = _async_noop
    backend._ensure_session_ready = _async_noop

    await backend._recover_after_transport_loss()

    attempt = backend._recovery_fence_metrics.diagnostics()["disconnect_attempt"]
    assert attempt == {
        "attempt_id": 1,
        "session_epoch": 23,
        "outcome": "service_exception",
        "exception_class": "HomeAssistantError",
    }
    assert "private detail" not in repr(attempt)


@pytest.mark.asyncio
async def test_wait_for_physical_disconnect_times_out(monkeypatch) -> None:
    class _Channel:
        async def diagnostics(self):
            return {"link_active": True, "parent_connected": True}

    backend = ThinRpcBackend(_hass(), controller_id="controller", channel=_Channel())
    monkeypatch.setattr(
        "custom_components.openrbus.transport._THIN_DISCONNECT_TIMEOUT", 0
    )

    with pytest.raises(HomeAssistantError, match="physical disconnect"):
        await backend._wait_for_physical_disconnect()


@pytest.mark.asyncio
async def test_wait_for_physical_disconnect_requires_both_live_states_down(
    monkeypatch,
) -> None:
    """A GATT close alone is not the physical BLE disconnect boundary."""

    class _Channel:
        def __init__(self) -> None:
            self.snapshots = [
                # The Thin GATT wrapper can process CLOSE_EVT before the
                # ESPHome BLEClient has reported its own disconnected state.
                {"link_active": False, "parent_connected": True, "epoch": 7},
                {"link_active": True, "parent_connected": False, "epoch": 7},
                # Epoch is a monotonically increasing connection identifier;
                # it is retained after disconnect and is not a down sentinel.
                {"link_active": False, "parent_connected": False, "epoch": 7},
            ]

        async def diagnostics(self):
            return self.snapshots.pop(0)

    channel = _Channel()
    backend = ThinRpcBackend(_hass(), controller_id="controller", channel=channel)
    monkeypatch.setattr(
        "custom_components.openrbus.transport._THIN_DISCONNECT_TIMEOUT", 0.5
    )

    await backend._wait_for_physical_disconnect()

    assert channel.snapshots == []


@pytest.mark.asyncio
async def test_thin_single_read_retries_once_after_transport_loss() -> None:
    """An idempotent single read gets one fresh secure-session attempt."""

    address = ObjectAddress(0x500F, 0x00)

    class _Client:
        calls = 0

        async def read_raw(self, _node, _address):
            self.calls += 1
            if self.calls == 1:
                raise TransportError("link lost during read")
            return b"\x00"

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = _Client()
    ready_calls = 0
    recoveries = 0

    async def ensure_session_ready() -> None:
        nonlocal ready_calls
        ready_calls += 1

    async def recover_after_transport_loss() -> None:
        nonlocal recoveries
        recoveries += 1

    backend._ensure_session_ready = ensure_session_ready
    backend._recover_after_transport_loss = recover_after_transport_loss

    result = await backend.async_read_object(address, node=5)

    assert result.raw_value == b"\x00"
    assert backend.client.calls == 2
    assert ready_calls == 1
    assert recoveries == 1


@pytest.mark.asyncio
async def test_single_read_capture_tags_frames_and_brackets_proxy_counters() -> None:
    trace: list[dict[str, object]] = []
    channel = HomeAssistantThinGattChannel(
        object(), ThinRpcCapability("request", "poll", "diagnostics"), trace
    )
    snapshots = iter(
        [
            {
                "epoch": 8,
                "rpc_requests": 4,
                "att_write_char_calls": 2,
                "event_queue_depth": 2,
                "poll_frame_calls": 20,
                "poll_nonempty_returns": 8,
                "poll_empty_returns": 12,
                "poll_queue_depth_before": 1,
                "poll_queue_depth_after": 2,
                "total_frames_enqueued": 10,
                "private_snapshot_field": "must not escape",
            },
            {
                "epoch": 8,
                "rpc_requests": 5,
                "att_write_char_calls": 3,
                "att_write_char_callbacks": 3,
                "last_att_write_request_id": 19,
                "last_att_write_status": "success",
                "notification_callbacks": 7,
                "last_notification_epoch": 8,
                "last_notification_seq": 12,
                "last_notification_nonempty": True,
                "event_queue_depth": 1,
                "poll_frame_calls": 21,
                "poll_nonempty_returns": 9,
                "poll_empty_returns": 12,
                "poll_queue_depth_before": 2,
                "poll_queue_depth_after": 1,
                "total_frames_enqueued": 11,
                "private_snapshot_field": "must not escape",
            },
        ]
    )

    async def diagnostics():
        return next(snapshots)

    channel.diagnostics = diagnostics
    address = ObjectAddress(0x500F, 0x00)

    class _Client:
        async def read_raw(self, _node, _address):
            channel._record_frame(
                "request",
                {
                    "kind": "request",
                    "op": "WRITE_CHAR",
                    "epoch": 8,
                    "request_id": 19,
                },
            )
            channel._record_frame(
                "response",
                {
                    "kind": "event",
                    "op": "NOTIFICATION",
                    "epoch": 8,
                    "seq": 12,
                    "request_id": 0,
                },
            )
            return b"\x00"

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.channel = channel
    backend.client = _Client()
    backend.session = SimpleNamespace(epoch=8)

    async def ensure_ready():
        return None

    backend._ensure_session_ready = ensure_ready
    result = await backend.async_read_object_with_transport_capture(address, node=5)
    assert result.raw_value == b"\x00"
    assert [frame["read_operation_id"] for frame in trace] == [1, 1]
    assert backend._read_operation_trace == [
        {"operation_id": 1, "kind": "single", "outcome": "success", "epoch": 8}
    ]
    capture = backend._last_read_transport_capture
    assert capture["before"]["rpc_requests"] == 4
    assert capture["before"]["total_frames_enqueued"] == 10
    assert capture["before"]["poll_frame_calls"] == 20
    assert capture["after"]["total_frames_enqueued"] == 11
    assert capture["after"]["poll_frame_calls"] == 21
    assert capture["after"]["poll_nonempty_returns"] == 9
    assert capture["after"]["poll_empty_returns"] == 12
    assert capture["after"]["poll_queue_depth_before"] == 2
    assert capture["after"]["poll_queue_depth_after"] == 1
    assert "private_snapshot_field" not in repr(capture)
    assert capture["after"]["last_att_write_request_id"] == 19
    assert capture["after"]["last_notification_seq"] == 12


@pytest.mark.asyncio
async def test_thin_discovery_retries_once_after_transport_loss(monkeypatch) -> None:
    """Read-only discovery gets the same bounded recovery as register reads."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = object()
    backend.effective_access_levels = {}
    backend.async_start = _async_noop
    attempts = 0
    recoveries = 0

    async def discover(_client, *, include_serial):
        nonlocal attempts
        assert include_serial is False
        attempts += 1
        if attempts == 1:
            raise RequestTimeoutError("Thin-GATT message response timed out")
        return ()

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    async def read_effective_access(_client, identities, **_kwargs):
        assert identities == ()
        return {}

    backend._recover_after_transport_loss = recover
    monkeypatch.setattr(
        "custom_components.openrbus.transport.discover_devices", discover
    )
    monkeypatch.setattr(
        "custom_components.openrbus.transport._read_effective_access_levels",
        read_effective_access,
    )

    assert await backend.async_discover_devices() == ()
    assert attempts == 2
    assert recoveries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_fails", (False, True))
async def test_capability_timeout_recovers_before_probing_next_node(
    monkeypatch, retry_fails
) -> None:
    """Recover one lost session, and stop probing if its retry also fails."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = object()
    backend.timeout = 10.0
    backend.effective_access_levels = {}
    backend.session = SimpleNamespace(epoch=1)
    backend.async_start = _async_noop
    reads: list[int] = []
    identities = (
        DeviceIdentity(1, None, None, None),
        DeviceIdentity(3, None, None, None),
    )
    recoveries = 0

    async def discover(_client, *, include_serial):
        assert include_serial is False
        return identities

    async def read_capabilities(_client, node, *, timeout, batch_timeout):
        assert timeout == 5.0
        assert batch_timeout == 10.0
        reads.append(node)
        if node == 1 and reads.count(1) == 1:
            raise RequestTimeoutError("Thin-GATT message response timed out")
        if node == 1 and retry_fails:
            raise RequestTimeoutError("Thin-GATT message response timed out")
        return ()

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1
        backend.session.epoch += 1

    async def read_access(_backend, _node):
        return None

    backend._recover_after_transport_loss = recover
    monkeypatch.setattr(
        "custom_components.openrbus.transport.discover_devices", discover
    )
    monkeypatch.setattr(
        "custom_components.openrbus.transport._discover_capabilities_batched",
        read_capabilities,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.transport._read_thin_access_level", read_access
    )

    result = await backend.async_discover_devices()

    assert tuple(identity.node for identity in result) == (1, 3)
    assert reads == ([1, 1] if retry_fails else [1, 1, 3])
    assert recoveries == 1
    assert backend.session.epoch == 2


@pytest.mark.asyncio
async def test_capability_abort_does_not_recover_or_block_next_node(
    monkeypatch,
) -> None:
    """An object-local abort is not treated as a lost Thin session."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = object()
    backend.timeout = 10.0
    backend.effective_access_levels = {}
    backend.session = SimpleNamespace(epoch=1)
    backend.async_start = _async_noop
    reads: list[int] = []
    identities = (
        DeviceIdentity(1, None, None, None),
        DeviceIdentity(3, None, None, None),
    )
    recoveries = 0

    async def discover(_client, *, include_serial):
        assert include_serial is False
        return identities

    async def read_capabilities(_client, node, *, timeout, batch_timeout):
        assert timeout == 5.0
        assert batch_timeout == 10.0
        reads.append(node)
        if node == 1:
            raise CanOpenAbortError(0x06020000)
        return ()

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    async def read_access(_backend, _node):
        return None

    backend._recover_after_transport_loss = recover
    monkeypatch.setattr(
        "custom_components.openrbus.transport.discover_devices", discover
    )
    monkeypatch.setattr(
        "custom_components.openrbus.transport._discover_capabilities_batched",
        read_capabilities,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.transport._read_thin_access_level", read_access
    )

    result = await backend.async_discover_devices()

    assert tuple(identity.node for identity in result) == (1, 3)
    assert reads == [1, 3]
    assert recoveries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_count", (88, 102))
@pytest.mark.asyncio
async def test_capability_discovery_batches_large_directories(directory_count) -> None:
    """Large 0x5826 tables use Core's bounded GetList partitioning."""

    class _Client:
        async def read_raw(self, node, address, *, timeout):
            assert node == 4
            assert timeout == 5.0
            assert address == ObjectAddress(0x5826, 0x00)
            return bytes((directory_count,))

        async def read_many_raw(self, items, *, timeout):
            assert timeout == 10.0
            assert len(items) == directory_count
            return tuple(
                SimpleNamespace(
                    address=item.address,
                    raw=bytes((0x12, 0x34, item.address.subindex, 0x56)),
                )
                for item in items
            )

    capabilities = await _discover_capabilities_batched(
        _Client(), 4, timeout=5.0, batch_timeout=10.0
    )
    assert len(capabilities) == directory_count
    assert capabilities[0].address == ObjectAddress(0x5634, 1)
    assert capabilities[-1].address == ObjectAddress(0x5634, directory_count)


@pytest.mark.asyncio
async def test_batch_failure_trace_falls_back_to_same_window_proxy_request_id() -> None:
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._batch_failure_trace_captured = False
    backend.frame_trace = []

    class _Channel:
        async def diagnostics(self):
            return {"epoch": 8, "last_rpc_request_id": 1141}

    backend.channel = _Channel()
    await backend._finish_batch_failure_trace(
        176,
        {"stage": "recovery_dispatch", "exception_class": "home_assistant_error"},
        {"epoch": 8, "last_rpc_request_id": 1135},
    )

    assert backend._last_batch_failure_trace["operation_id"] == 176
    assert backend._last_batch_failure_trace["request_id"] == 1141
    assert backend._last_batch_failure_trace["epoch"] == 8


@pytest.mark.asyncio
async def test_thin_discovery_does_not_retry_protocol_failure(monkeypatch) -> None:
    """Only transport loss enters recovery; discovery protocol errors remain exact."""

    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.client = object()
    backend.async_start = _async_noop
    recoveries = 0

    async def discover(_client, *, include_serial):
        raise ProtocolError("invalid assignment directory response")

    async def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    backend._recover_after_transport_loss = recover
    monkeypatch.setattr(
        "custom_components.openrbus.transport.discover_devices", discover
    )

    with pytest.raises(ProtocolError, match="invalid assignment directory response"):
        await backend.async_discover_devices()
    assert recoveries == 0


@pytest.mark.asyncio
async def test_failed_thin_reprepare_releases_backend_lifecycle_for_next_poll() -> None:
    """A failed reconnect must not strand the backend in a false started state."""

    controller_id = "test-thin-reprepare-lifecycle"
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._started = True
    backend.controller_id = controller_id
    backend._active_controllers.add(controller_id)
    backend._owns_controller = True
    backend._owner_generation = 1
    backend._controller_owner_generations[controller_id] = 1
    backend.session = SimpleNamespace(connected=False, identity=None, epoch=0)
    backend.link = object()
    backend.channel = SimpleNamespace(
        diagnostics=lambda: asyncio.sleep(
            0, result={"link_active": False, "parent_connected": False}
        )
    )
    backend._lifecycle_lock = asyncio.Lock()
    backend.timeout = 10.0
    backend.authentication = object()
    backend.transport = object()
    backend.client = object()

    async def async_start() -> None:
        return None

    async def establish_session(*, attach: bool) -> None:
        assert attach is True
        raise TransportError("reconnect failed")

    backend.async_start = async_start
    backend._establish_session = establish_session

    with pytest.raises(TransportError, match="reconnect failed"):
        await backend._ensure_session_ready()

    assert backend._started is False
    assert controller_id not in backend._active_controllers
    assert backend.session is None
    assert backend.link is None
    assert backend.authentication is None
    assert backend.transport is None
    assert backend.client is None


@pytest.mark.asyncio
async def test_thin_write_reprepares_before_first_attempt_but_never_retries(
    monkeypatch,
) -> None:
    """A write after a disconnect is safe to start, not safe to duplicate."""

    address = ObjectAddress(0x500F, 0x00)
    backend = ThinRpcBackend.__new__(ThinRpcBackend)
    backend._read_lock = asyncio.Lock()
    backend.write_enabled = True
    backend.access_level = 1
    backend.client = object()
    ensured = 0

    async def ensure_session_ready() -> None:
        nonlocal ensured
        ensured += 1

    backend._ensure_session_ready = ensure_session_ready
    writes = 0

    class _Writer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def write(self, *_args, **_kwargs):
            nonlocal writes
            writes += 1
            raise TransportError("response lost after write")

    monkeypatch.setattr("custom_components.openrbus.transport.OpenRBusClient", _Writer)

    with pytest.raises(TransportError, match="response lost"):
        await backend.async_write_object(5, address, 0)

    assert ensured == 1
    assert writes == 1


def test_options_flow_pairing_pin_overrides_original_entry_data() -> None:
    entry = SimpleNamespace(data={"passkey": "0"}, options={"passkey": "123456"})
    configured = _configured_entry_data(entry)
    assert int(configured["passkey"]) == 123456


def test_missing_or_auto_backend_is_rejected_without_fallback() -> None:
    capability = detect_thin_rpc_capability(
        _hass(
            "openrbus_gatt_rpc_request",
            "openrbus_gatt_rpc_poll",
            "openrbus_gatt_rpc_diagnostics",
        )
    )
    with pytest.raises(HomeAssistantError, match="Unknown OpenRBus transport"):
        select_backend_mode(None, capability)
    with pytest.raises(HomeAssistantError, match="Unknown OpenRBus transport"):
        select_backend_mode("auto", capability)


def test_thin_capability_is_complete_only_at_lifecycle_boundary() -> None:
    hass = _hass(
        "openrbus_gatt_rpc_request",
        "openrbus_gatt_rpc_poll",
        "openrbus_gatt_rpc_diagnostics",
    )
    backend = ThinRpcBackend(hass, controller_id="controller", channel=object())
    hass.services.names.clear()
    assert backend.capability.available


def test_prefixed_services_bind_to_one_controller() -> None:
    capability = detect_thin_rpc_capability(
        _hass(
            "boiler_openrbus_gatt_rpc_request",
            "boiler_openrbus_gatt_rpc_poll",
            "boiler_openrbus_gatt_rpc_diagnostics",
            "other_openrbus_gatt_rpc_request",
        )
    )
    assert capability.available
    assert capability.request_service.startswith("boiler_")
    assert capability.poll_service == "boiler_openrbus_gatt_rpc_poll"


def test_prefixed_services_from_different_controllers_are_rejected() -> None:
    capability = detect_thin_rpc_capability(
        _hass(
            "boiler_openrbus_gatt_rpc_request",
            "radiator_openrbus_gatt_rpc_poll",
            "boiler_openrbus_gatt_rpc_diagnostics",
        )
    )
    assert not capability.available
    assert capability.missing == ("same_controller",)


def test_preferred_controller_selects_matching_trio() -> None:
    capability = detect_thin_rpc_capability(
        _hass(
            "boiler_openrbus_gatt_rpc_request",
            "boiler_openrbus_gatt_rpc_poll",
            "boiler_openrbus_gatt_rpc_diagnostics",
            "radiator_openrbus_gatt_rpc_request",
            "radiator_openrbus_gatt_rpc_poll",
            "radiator_openrbus_gatt_rpc_diagnostics",
        ),
        preferred_prefix="radiator",
    )
    assert capability.available
    assert capability.request_service == "radiator_openrbus_gatt_rpc_request"


def test_preferred_controller_missing_is_rejected() -> None:
    capability = detect_thin_rpc_capability(
        _hass(
            "boiler_openrbus_gatt_rpc_request",
            "boiler_openrbus_gatt_rpc_poll",
            "boiler_openrbus_gatt_rpc_diagnostics",
        ),
        preferred_prefix="radiator",
    )
    assert not capability.available
    assert capability.missing == ("preferred_controller",)


def test_multiple_complete_controllers_without_preference_are_rejected() -> None:
    capability = detect_thin_rpc_capability(
        _hass(
            "boiler_openrbus_gatt_rpc_request",
            "boiler_openrbus_gatt_rpc_poll",
            "boiler_openrbus_gatt_rpc_diagnostics",
            "radiator_openrbus_gatt_rpc_request",
            "radiator_openrbus_gatt_rpc_poll",
            "radiator_openrbus_gatt_rpc_diagnostics",
        )
    )
    assert not capability.available
    assert capability.missing == ("same_controller",)


def test_controller_choices_are_complete_and_same_prefix() -> None:
    choices = thin_rpc_controller_choices(
        _hass(
            "boiler_openrbus_gatt_rpc_request",
            "boiler_openrbus_gatt_rpc_poll",
            "boiler_openrbus_gatt_rpc_diagnostics",
            "radiator_openrbus_gatt_rpc_request",
            "radiator_openrbus_gatt_rpc_poll",
        )
    )
    assert set(choices) == {"boiler"}
    assert choices["boiler"].diagnostics_service.endswith(
        "boiler_openrbus_gatt_rpc_diagnostics"
    )


def test_persisted_bare_trio_wins_over_prefixed_candidates() -> None:
    hass = _hass(
        "openrbus_gatt_rpc_request",
        "openrbus_gatt_rpc_poll",
        "openrbus_gatt_rpc_diagnostics",
        "boiler_openrbus_gatt_rpc_request",
        "boiler_openrbus_gatt_rpc_poll",
        "boiler_openrbus_gatt_rpc_diagnostics",
    )
    capability = resolve_thin_rpc_capability(
        hass,
        request_service="openrbus_gatt_rpc_request",
        poll_service="openrbus_gatt_rpc_poll",
        diagnostics_service="openrbus_gatt_rpc_diagnostics",
    )
    assert capability.available
    assert capability.request_service == "openrbus_gatt_rpc_request"


def test_persisted_mixed_prefix_trio_fails_closed() -> None:
    hass = _hass(
        "boiler_openrbus_gatt_rpc_request",
        "radiator_openrbus_gatt_rpc_poll",
        "boiler_openrbus_gatt_rpc_diagnostics",
    )
    capability = resolve_thin_rpc_capability(
        hass,
        request_service="boiler_openrbus_gatt_rpc_request",
        poll_service="radiator_openrbus_gatt_rpc_poll",
        diagnostics_service="boiler_openrbus_gatt_rpc_diagnostics",
    )
    assert not capability.available
    assert capability.missing == ("same_controller",)


def test_runtime_factory_builds_core_profile_and_secret_provider(
    monkeypatch, tmp_path
) -> None:
    hass = SimpleNamespace(config=SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(
        "custom_components.openrbus._thin_key_provider", lambda *_args: object()
    )
    entry = SimpleNamespace(
        data={
            CONF_THIN_PROFILE: {
                "service": "service",
                "identity": "identity",
                "auth": "auth",
                "roles": {
                    role: ["service", role]
                    for role in (
                        "identity",
                        "identity_notify",
                        "identity_cccd",
                        "auth",
                        "auth_notify",
                        "auth_cccd",
                        "request",
                        "response",
                        "response_cccd",
                    )
                },
            },
            CONF_THIN_KEY_SECRET: "openrbus_ehc_key",
        },
        options={},
    )
    profile, provider = _thin_runtime(hass, entry)
    assert isinstance(profile, ThinGattProfile)
    assert provider is not None


def test_fresh_thin_entry_gets_internal_profile_and_default_ehc_secret(
    monkeypatch,
) -> None:
    hass = SimpleNamespace(config=SimpleNamespace(config_dir="/tmp/openrbus-test"))
    seen: list[object] = []
    monkeypatch.setattr(
        "custom_components.openrbus._thin_key_provider",
        lambda _hass, name: seen.append(name) or object(),
    )
    entry = SimpleNamespace(data={CONF_BACKEND: BACKEND_THIN_RPC}, options={})

    profile, provider = _thin_runtime(hass, entry)

    assert profile == _default_thin_profile()
    assert provider is not None
    assert seen == ["openrbus_ehc_key"]
    assert set(profile.roles) == {
        "identity",
        "identity_notify",
        "identity_cccd",
        "auth",
        "auth_notify",
        "auth_cccd",
        "request",
        "response",
        "response_cccd",
    }


def test_incomplete_thin_profile_is_rejected_before_connection() -> None:
    assert (
        _thin_profile(
            {"service": "service", "identity": "identity", "auth": "auth", "roles": {}}
        )
        is None
    )


def test_thin_backend_wires_all_required_notification_subscriptions() -> None:
    assert _THIN_SUBSCRIPTIONS == (
        ("identity_notify", "identity_cccd"),
        ("auth_notify", "auth_cccd"),
        ("response", "response_cccd"),
    )


def test_attach_mode_uses_liveness_only_for_stream_baseline() -> None:
    assert not _thin_attach_mode(True, {})
    assert not _thin_attach_mode(
        True, {"parent_connected": False, "link_active": False}
    )
    assert not _thin_attach_mode(True, {"parent_connected": True})
    assert _thin_attach_mode(True, {"link_active": True})
    assert not _thin_attach_mode(False, {"parent_connected": True, "link_active": True})


@pytest.mark.asyncio
async def test_matching_connect_seq_one_is_valid_for_fresh_or_attach_baseline() -> None:
    """An auto-connected link can still have sequence one when HA attaches."""

    identity = {"gattc_if": 2, "conn_id": 0}
    frames = [
        {
            "kind": "event",
            "op": "CAPABILITY",
            "epoch": 0,
            "seq": 0,
            "payload": {"state": CAPABILITY_MARKER},
        },
        {
            "kind": "response",
            "op": "CONNECT",
            "epoch": 4,
            "seq": 0,
            "request_id": 1,
            "status": "OK",
            **identity,
        },
        {
            "kind": "event",
            "op": "CONNECTION_STATE",
            "epoch": 4,
            "seq": 1,
            "payload": {"state": "connected"},
            **identity,
        },
    ]

    class _Frames:
        def __init__(self) -> None:
            self.frames = list(frames)

        async def action(self, _name, _payload, *, timeout):
            del timeout

        async def poll(self, *, timeout):
            del timeout
            return self.frames.pop(0) if self.frames else None

        async def diagnostics(self):
            return {}

    assert await ThinGattSession(_Frames(), poll_timeout=0.01).prepare(
        timeout=1, attach=True
    )

    # The same exact stream is valid when HA selects the fresh baseline.
    assert await ThinGattSession(_Frames(), poll_timeout=0.01).prepare(
        timeout=1, attach=_thin_attach_mode(True, {})
    )


@pytest.mark.asyncio
async def test_backend_uses_fresh_baseline_when_liveness_is_not_active() -> None:
    identity = {"gattc_if": 2, "conn_id": 0}

    class _Channel:
        def __init__(self) -> None:
            self.frames = [
                {
                    "kind": "event",
                    "op": "CAPABILITY",
                    "epoch": 0,
                    "seq": 0,
                    "payload": {"state": CAPABILITY_MARKER},
                },
                {
                    "kind": "response",
                    "op": "CONNECT",
                    "epoch": 4,
                    "seq": 0,
                    "request_id": 1,
                    "status": "OK",
                    **identity,
                },
                {
                    "kind": "event",
                    "op": "CONNECTION_STATE",
                    "epoch": 4,
                    "seq": 1,
                    "payload": {"state": "connected"},
                    **identity,
                },
            ]

        async def action(self, _name, _payload, *, timeout):
            del timeout

        async def poll(self, *, timeout):
            del timeout
            return self.frames.pop(0) if self.frames else None

        async def diagnostics(self):
            return {
                "rpc_schema_version": 3,
                "pair_contract": "pair_terminal_v3",
                "parent_connected": False,
                "link_active": False,
            }

    class _Link:
        def __init__(self, session):
            self.session = session
            self.is_ready = True
            self.is_connected = True

        async def prepare(self, *, timeout):
            del timeout

    class _Auth:
        is_authenticated = True

        async def authenticate(self, *, timeout):
            del timeout
            return object()

    backend = ThinRpcBackend(
        _hass("request", "poll", "diagnostics"),
        controller_id="controller",
        request_service="request",
        poll_service="poll",
        diagnostics_service="diagnostics",
        request_handle=7,
        response_handle=42,
        channel=_Channel(),
        link_factory=_Link,
        authenticator_factory=lambda _link: _Auth(),
    )
    await backend._establish_session(attach=True)
    assert backend.session is not None and backend.session.connected


@pytest.mark.asyncio
async def test_thin_setup_arms_existing_pairing_wrapper_before_rpc_pair() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _ServiceRegistry(_Services):
        async def async_call(self, domain, service, data, *, blocking):
            assert domain == "esphome"
            assert blocking is True
            calls.append((service, data))

    class _Channel:
        samples = iter(
            ({"link_active": True}, {"link_active": False, "parent_connected": False})
        )

        async def diagnostics(self):
            return next(self.samples)

    hass = SimpleNamespace(
        services=_ServiceRegistry("openrbus_pair"),
    )
    backend = ThinRpcBackend(
        hass,
        controller_id="controller",
        channel=_Channel(),
        pair_action="openrbus_pair",
        passkey=123456,
    )
    await backend._arm_pairing_if_configured()
    assert calls == [("openrbus_pair", {"passkey": 123456})]


@pytest.mark.asyncio
async def test_thin_setup_does_not_accept_stale_already_secure_terminal(
    monkeypatch,
) -> None:
    """An old RPC pair terminal is not proof of this arm's disconnect boundary."""

    calls: list[tuple[str, dict[str, object]]] = []

    class _ServiceRegistry(_Services):
        async def async_call(self, domain, service, data, *, blocking):
            assert domain == "esphome"
            assert blocking is True
            calls.append((service, data))

    class _Channel:
        async def diagnostics(self):
            return {
                "pair_terminal_status": "already_secure_success",
                "link_active": True,
                "parent_connected": True,
            }

    hass = SimpleNamespace(services=_ServiceRegistry("openrbus_pair"))
    backend = ThinRpcBackend(
        hass,
        controller_id="controller",
        channel=_Channel(),
        pair_action="openrbus_pair",
        passkey=123456,
    )

    monkeypatch.setattr(
        "custom_components.openrbus.transport._THIN_DISCONNECT_TIMEOUT", 0.01
    )
    with pytest.raises(
        HomeAssistantError,
        match="pairing arm did not reach the disconnect boundary",
    ):
        await backend._arm_pairing_if_configured()
    assert calls == [("openrbus_pair", {"passkey": 123456})]


@pytest.mark.asyncio
async def test_thin_pair_arm_resets_stale_state_once_and_rearms(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _ServiceRegistry(_Services):
        async def async_call(self, domain, service, data, *, blocking):
            assert domain == "esphome"
            assert blocking is True
            calls.append((service, data))

    class _Channel:
        samples = iter(
            (
                {"link_active": True, "parent_connected": True},
                {"link_active": False, "parent_connected": False},
                {"link_active": False, "parent_connected": False},
            )
        )

        async def diagnostics(self):
            return next(self.samples)

    hass = SimpleNamespace(
        services=_ServiceRegistry("openrbus_pair", "openrbus_disconnect")
    )
    backend = ThinRpcBackend(
        hass,
        controller_id="controller",
        channel=_Channel(),
        pair_action="openrbus_pair",
        passkey=123456,
    )
    backend._drain_stale_frames = _async_noop
    monkeypatch.setattr(
        "custom_components.openrbus.transport._THIN_DISCONNECT_TIMEOUT", 0
    )

    await backend._arm_pairing_if_configured()

    assert calls == [
        ("openrbus_pair", {"passkey": 123456}),
        ("openrbus_disconnect", {}),
        ("openrbus_pair", {"passkey": 123456}),
    ]
    assert (
        backend.setup_metrics.diagnostics()["pairing_arm"]["outcomes"][
            "recovery_succeeded"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_thin_pair_arm_stale_state_retry_is_bounded(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _ServiceRegistry(_Services):
        async def async_call(self, domain, service, data, *, blocking):
            calls.append((service, data))

    class _Channel:
        samples = iter(
            (
                {"link_active": True, "parent_connected": True},
                {"link_active": False, "parent_connected": False},
                {"link_active": True, "parent_connected": True},
            )
        )

        async def diagnostics(self):
            return next(self.samples)

    hass = SimpleNamespace(
        services=_ServiceRegistry("openrbus_pair", "openrbus_disconnect")
    )
    backend = ThinRpcBackend(
        hass,
        controller_id="controller",
        channel=_Channel(),
        pair_action="openrbus_pair",
        passkey=123456,
    )
    backend._drain_stale_frames = _async_noop
    monkeypatch.setattr(
        "custom_components.openrbus.transport._THIN_DISCONNECT_TIMEOUT", 0
    )

    with pytest.raises(HomeAssistantError, match="stale pairing reset/rearm failed"):
        await backend._arm_pairing_if_configured()

    assert [service for service, _data in calls] == [
        "openrbus_pair",
        "openrbus_disconnect",
        "openrbus_pair",
    ]
    assert (
        backend.setup_metrics.diagnostics()["pairing_arm"]["outcomes"][
            "recovery_failed"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_thin_pair_arm_disconnect_action_failure_does_not_rearm(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class _ServiceRegistry(_Services):
        async def async_call(self, domain, service, data, *, blocking):
            del domain, data, blocking
            calls.append(service)
            if service == "openrbus_disconnect":
                raise RuntimeError("raw service detail")

    class _Channel:
        async def diagnostics(self):
            return {"link_active": True, "parent_connected": True}

    hass = SimpleNamespace(
        services=_ServiceRegistry("openrbus_pair", "openrbus_disconnect")
    )
    backend = ThinRpcBackend(
        hass,
        controller_id="controller",
        channel=_Channel(),
        pair_action="openrbus_pair",
        passkey=123456,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.transport._THIN_DISCONNECT_TIMEOUT", 0
    )

    with pytest.raises(
        HomeAssistantError, match="stale pairing reset/rearm failed"
    ) as error:
        await backend._arm_pairing_if_configured()

    assert calls == ["openrbus_pair", "openrbus_disconnect"]
    assert "raw service detail" not in str(error.value)
    assert (
        backend.setup_metrics.diagnostics()["pairing_arm"]["outcomes"][
            "recovery_failed"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_setup_response_metrics_distinguish_empty_poll_and_lookup_frame() -> None:
    metrics = SetupResponseMetrics()
    lookup = {"kind": "response", "op": "HANDLE_LOOKUP", "request_id": 7}

    class _Client:
        hass = _hass("openrbus_gatt_rpc_poll")

        def __init__(self):
            self.responses = iter(({"frame": ""}, {"frame": json.dumps(lookup)}))

        async def execute_service(self, *_args, **_kwargs):
            return next(self.responses)

    channel = HomeAssistantThinGattChannel(
        _Client(),
        ThinRpcCapability(
            "openrbus_gatt_rpc_request",
            "openrbus_gatt_rpc_poll",
            "openrbus_gatt_rpc_diagnostics",
        ),
        setup_metrics=metrics,
    )
    channel._setup_handle_lookup_request_ids.add(7)

    assert await channel.poll(timeout=1) is None
    assert (await channel.poll(timeout=1))["op"] == "HANDLE_LOOKUP"
    snapshot = metrics.diagnostics()
    assert snapshot["poll_request"]["outcomes"]["no_esp_response"] == 1
    assert snapshot["poll_request"]["outcomes"]["esp_response"] == 1
    assert snapshot["handle_lookup"]["outcomes"]["no_esp_response"] == 1
    assert snapshot["handle_lookup"]["outcomes"]["response"] == 1
    assert snapshot["handle_lookup"]["outcomes"]["call_not_sent"] == 0
    assert "request_id" not in repr(snapshot)


@pytest.mark.asyncio
async def test_secure_link_deadline_exceeds_firmware_pair_watchdog() -> None:
    """Allow a delayed pair terminal event to beat the host deadline."""

    class _Channel:
        def __init__(self):
            identity = {"gattc_if": 1, "conn_id": 1}
            self.frames = [
                {
                    "kind": "event",
                    "op": "CAPABILITY",
                    "epoch": 0,
                    "seq": 0,
                    "payload": {"state": CAPABILITY_MARKER},
                },
                {
                    "kind": "response",
                    "op": "CONNECT",
                    "epoch": 1,
                    "seq": 0,
                    "request_id": 1,
                    "status": "OK",
                    **identity,
                },
                {
                    "kind": "event",
                    "op": "CONNECTION_STATE",
                    "epoch": 1,
                    "seq": 1,
                    "request_id": 1,
                    "payload": {"state": "connected"},
                    **identity,
                },
            ]

        async def diagnostics(self):
            return {
                "parent_connected": False,
                "link_active": False,
                "rpc_schema_version": 3,
                "pair_contract": "pair_terminal_v3",
            }

        async def action(self, _name, _payload, *, timeout):
            del timeout

        async def poll(self, *, timeout):
            del timeout
            return self.frames.pop(0) if self.frames else None

    class _Link:
        is_ready = True
        is_connected = True

        def __init__(self, session):
            self.session = session
            self.timeout = None

        async def prepare(self, *, timeout):
            self.timeout = timeout
            assert timeout > 10.0
            await asyncio.wait_for(asyncio.sleep(0.02), timeout=timeout)

    class _Auth:
        is_authenticated = True

        async def authenticate(self, *, timeout):
            assert timeout > 10.0
            return object()

    backend = ThinRpcBackend(
        _hass("request", "poll", "diagnostics"),
        controller_id="controller",
        request_service="request",
        poll_service="poll",
        diagnostics_service="diagnostics",
        request_handle=7,
        response_handle=42,
        channel=_Channel(),
        link_factory=_Link,
        authenticator_factory=lambda _link: _Auth(),
        timeout=0.1,
    )

    await backend._establish_session(attach=True)
    assert _THIN_SECURE_TIMEOUT > 10.0


@pytest.mark.asyncio
async def test_ha_channel_reproduces_pair_timeout_after_discovery() -> None:
    """Reproduce the live ordering when pairing emits no terminal frame."""

    class _Client:
        def __init__(self) -> None:
            self.frames = []
            self.operations = []

        async def execute_service(self, service, data, **kwargs):
            del kwargs
            if service == "rpc_request":
                request = json.loads(data["frame"])
                operation = request["op"]
                self.operations.append(operation)
                request_id = request["request_id"]
                identity = {"gattc_if": 1, "conn_id": 1}
                if operation == "CONNECT":
                    self.frames.extend(
                        [
                            {
                                "kind": "event",
                                "op": "CAPABILITY",
                                "epoch": 0,
                                "seq": 0,
                                "payload": {"state": CAPABILITY_MARKER},
                            },
                            {
                                "kind": "response",
                                "op": "CONNECT",
                                "epoch": 1,
                                "seq": 0,
                                "request_id": request_id,
                                "status": "OK",
                                **identity,
                            },
                            {
                                "kind": "event",
                                "op": "CONNECTION_STATE",
                                "epoch": 1,
                                "seq": 1,
                                "request_id": request_id,
                                "payload": {"state": "connected"},
                                **identity,
                            },
                        ]
                    )
                elif operation == "DISCOVER":
                    self.frames.extend(
                        [
                            {
                                "kind": "response",
                                "op": "DISCOVER",
                                "epoch": 1,
                                "seq": 0,
                                "request_id": request_id,
                                "status": "OK",
                                **identity,
                            },
                            {
                                "kind": "event",
                                "op": "CONNECTION_STATE",
                                "epoch": 1,
                                "seq": 2,
                                "request_id": request_id,
                                "payload": {"state": "discovered"},
                                **identity,
                            },
                        ]
                    )
                # PAIR_ENCRYPT deliberately receives no callback, matching
                # the live timeout where the device emitted no terminal pair
                # response or ENCRYPTION_STATE event.
                return None
            if service == "rpc_poll":
                frame = self.frames.pop(0) if self.frames else None
                return {"frame": json.dumps(frame) if frame is not None else ""}
            if service == "rpc_diag":
                return {"snapshot": json.dumps({"link_active": False})}
            raise AssertionError(f"unexpected service {service}")

    client = _Client()
    channel = HomeAssistantThinGattChannel(
        client, ThinRpcCapability("rpc_request", "rpc_poll", "rpc_diag")
    )
    session = ThinGattSession(
        channel,
        services=ThinGattRpcServices(
            request="rpc_request", poll="rpc_poll", diagnostics="rpc_diag"
        ),
        poll_timeout=0.001,
    )
    link = ThinGattLink(session, roles={})

    with pytest.raises(RequestTimeoutError, match="PAIR_ENCRYPT timed out"):
        await link.prepare(timeout=0.02)
    assert client.operations == ["CONNECT", "DISCOVER", "PAIR_ENCRYPT", "CANCEL"]


@pytest.mark.asyncio
async def test_frame_trace_captures_seq_one_attach_without_private_fields() -> None:
    identity = {"gattc_if": 1, "conn_id": 1}
    frames = [
        {
            "kind": "event",
            "op": "CAPABILITY",
            "epoch": 0,
            "seq": 0,
            "payload": {"state": CAPABILITY_MARKER},
        },
        {
            "kind": "response",
            "op": "CONNECT",
            "epoch": 3,
            "seq": 0,
            "request_id": 1,
            "status": "OK",
            **identity,
        },
        {
            "kind": "event",
            "op": "CONNECTION_STATE",
            "epoch": 3,
            "seq": 1,
            "request_id": 1,
            "payload": {"state": "connected"},
            **identity,
        },
    ]
    trace: list[dict[str, object]] = []

    class _Client:
        async def execute_service(self, service, data, **kwargs):
            del data, kwargs
            if service == "request":
                return None
            frame = frames.pop(0) if frames else None
            return {"frame": json.dumps(frame) if frame is not None else ""}

    channel = HomeAssistantThinGattChannel(
        _Client(),
        ThinRpcCapability("request", "poll", "diagnostics"),
        frame_trace=trace,
    )
    channel.trace_context = "discovery"
    session = ThinGattSession(
        channel,
        services=ThinGattRpcServices(
            request="request", poll="poll", diagnostics="diagnostics"
        ),
        poll_timeout=0.001,
    )
    await session.prepare(timeout=1, attach=True)

    assert [
        (
            item["kind"],
            item["op"],
            item["seq"],
            item["epoch"],
            item["request_id"],
            item["context"],
            item["status"],
            item["state_category"],
        )
        for item in trace
    ] == [
        ("request", "CONNECT", None, 0, 1, "discovery", None, None),
        ("event", "CAPABILITY", 0, 0, None, "discovery", None, None),
        ("response", "CONNECT", 0, 3, 1, "discovery", "OK", None),
        ("event", "CONNECTION_STATE", 1, 3, 1, "discovery", None, "connected"),
    ]
    assert all(
        set(item)
        == {
            "direction",
            "kind",
            "op",
            "seq",
            "epoch",
            "request_id_present",
            "request_id",
            "context",
            "status",
            "state_category",
        }
        and isinstance(item["request_id_present"], bool)
        for item in trace
    )
    assert all("payload" not in item and "gattc_if" not in item for item in trace)


@pytest.mark.asyncio
async def test_frame_trace_supports_direct_and_nested_poll_responses() -> None:
    trace: list[dict[str, object]] = []
    frames = [
        {"kind": "response", "op": "DISCOVER", "epoch": 2, "seq": 4, "status": "OK"},
        {
            "kind": "event",
            "op": "ENCRYPTION_STATE",
            "epoch": 2,
            "seq": 5,
            "payload": {"state": "encrypted"},
        },
    ]

    class _Client:
        calls = 0

        async def execute_service(self, service, data, **kwargs):
            del data, kwargs
            frame = frames.pop(0)
            body = {"frame": json.dumps(frame)}
            self.calls += 1
            return body if self.calls == 1 else {"response": body}

    channel = HomeAssistantThinGattChannel(
        _Client(), ThinRpcCapability("request", "poll", "diagnostics"), trace
    )
    assert (await channel.poll(timeout=1))["op"] == "DISCOVER"
    assert (await channel.poll(timeout=1))["op"] == "ENCRYPTION_STATE"
    assert trace[0]["request_id_present"] is False
    assert trace[1]["state_category"] == "encrypted"


def test_frame_trace_is_disabled_and_bounded(monkeypatch) -> None:
    channel = HomeAssistantThinGattChannel(
        _Client({"frame": ""}), ThinRpcCapability("request", "poll", "diagnostics")
    )
    assert channel.frame_trace is None
    trace: list[dict[str, object]] = []
    channel = HomeAssistantThinGattChannel(
        _Client({"frame": ""}),
        ThinRpcCapability("request", "poll", "diagnostics"),
        trace,
    )
    for _ in range(MAX_FRAME_TRACE_ENTRIES + 1):
        channel._record_frame(
            "response",
            {"kind": "event", "op": "CONNECT", "epoch": 1, "seq": 1},
        )
    assert len(trace) == MAX_FRAME_TRACE_ENTRIES
    monkeypatch.setattr("custom_components.openrbus.transport.MAX_FRAME_TRACE_BYTES", 1)
    blocked: list[dict[str, object]] = []
    channel = HomeAssistantThinGattChannel(
        _Client({"frame": ""}),
        ThinRpcCapability("request", "poll", "diagnostics"),
        blocked,
    )
    channel._record_frame(
        "response", {"kind": "event", "op": "CONNECT", "epoch": 1, "seq": 1}
    )
    assert blocked == []
    assert MAX_FRAME_TRACE_BYTES > 1


@pytest.mark.asyncio
async def test_frame_trace_failure_cannot_mask_transport_success() -> None:
    class _BrokenTrace(list):
        def append(self, item) -> None:
            del item
            raise RuntimeError("diagnostic sink unavailable")

    channel = HomeAssistantThinGattChannel(
        _Client(None),
        ThinRpcCapability("request", "poll", "diagnostics"),
        _BrokenTrace(),
    )
    await channel.action("request", {"op": "CONNECT", "request_id": 1}, timeout=1)


@pytest.mark.asyncio
async def test_lost_message_transport_cannot_bypass_link_reprepare() -> None:
    transport = _PreparedThinGattMessageTransport.__new__(
        _PreparedThinGattMessageTransport
    )
    with pytest.raises(Exception, match="must reprepare"):
        await transport.connect()


@pytest.mark.asyncio
async def test_forced_thin_without_capability_fails_closed() -> None:
    backend = ThinRpcBackend(_hass(), controller_id="controller")
    with pytest.raises(HomeAssistantError, match="missing ESPHome service"):
        await backend.async_start()


@pytest.mark.asyncio
async def test_thin_backend_retries_once_after_stale_authorization(monkeypatch) -> None:
    backend = ThinRpcBackend(
        _hass(),
        controller_id="controller",
        channel=object(),
        profile=object(),
        key_provider=lambda _purpose: b"1234",
        access_level=3,
    )
    attempts: list[bool] = []

    async def arm_pairing() -> None:
        return None

    async def establish(*, attach: bool) -> None:
        attempts.append(attach)
        if len(attempts) == 1:
            raise AuthorizationCorrelationError("stale confirmation")

    async def wait_for_disconnect() -> None:
        return None

    monkeypatch.setattr(backend, "_arm_pairing_if_configured", arm_pairing)
    monkeypatch.setattr(backend, "_establish_session", establish)
    monkeypatch.setattr(backend, "_wait_for_physical_disconnect", wait_for_disconnect)

    await backend.async_start()

    assert backend.started
    assert attempts == [True, True]


@pytest.mark.asyncio
async def test_stop_waits_for_physical_disconnect_before_releasing_controller() -> None:
    class _Link:
        is_connected = True

        async def disconnect(self, *, timeout):
            del timeout

    class _Channel:
        def __init__(self):
            self.snapshots = [
                {"link_active": True, "parent_connected": True},
                {"link_active": False, "parent_connected": False},
            ]

        async def diagnostics(self):
            return self.snapshots.pop(0)

    channel = _Channel()
    backend = ThinRpcBackend(_hass(), controller_id="controller", channel=channel)
    backend.link = _Link()
    backend._active_controllers.add("controller")
    backend._owns_controller = True
    backend._controller_owner_generations["controller"] = backend._owner_generation

    await backend.async_stop()

    assert channel.snapshots == []
    assert "controller" not in backend._active_controllers


@pytest.mark.asyncio
async def test_stop_rejects_normal_cleanup_return_with_owner_still_fenced(
    monkeypatch,
) -> None:
    backend = ThinRpcBackend(_hass(), controller_id="controller")
    backend._owns_controller = True
    backend._active_controllers.add(backend.controller_id)
    backend._controller_owner_generations[backend.controller_id] = (
        backend._owner_generation
    )

    async def incomplete_cleanup() -> None:
        return

    monkeypatch.setattr(backend, "_cleanup_owned_session", incomplete_cleanup)

    with pytest.raises(HomeAssistantError, match="did not release this session"):
        await backend.async_stop()

    assert backend._owns_controller
    assert backend.controller_id in backend._active_controllers


@pytest.mark.asyncio
async def test_stop_uses_scoped_proxy_disconnect_if_rpc_disconnect_stalls(
    monkeypatch,
) -> None:
    calls = []

    class _Services:
        def async_services(self):
            return {
                "esphome": {
                    "proxy_openrbus_gatt_rpc_request": object(),
                    "proxy_openrbus_disconnect": object(),
                }
            }

        async def async_call(self, domain, service, data, *, blocking):
            calls.append((domain, service, data, blocking))

    hass = SimpleNamespace(services=_Services())
    backend = ThinRpcBackend(
        hass,
        controller_id="controller",
        pair_action="proxy_openrbus_pair",
        capability=ThinRpcCapability("proxy_openrbus_gatt_rpc_request", "poll", "diag"),
        channel=object(),
    )

    class _Session:
        connected = True
        identity = object()
        epoch = 8

        def retire(self):
            self.connected = False

    class _Link:
        is_connected = True

        async def disconnect(self, *, timeout):
            assert timeout == 5.0

    backend.session = _Session()
    backend.link = _Link()
    backend._started = True
    backend._owns_controller = True
    backend._active_controllers.add("controller")
    backend._controller_owner_generations["controller"] = backend._owner_generation
    wait_calls = 0

    async def wait_for_disconnect(*, recovery_fence=None):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise HomeAssistantError("physical disconnect pending")
        assert calls == [("esphome", "proxy_openrbus_disconnect", {}, True)]

    monkeypatch.setattr(backend, "_wait_for_physical_disconnect", wait_for_disconnect)
    await backend.async_stop()

    assert wait_calls == 2
    assert backend._owns_controller is False
    assert "controller" not in backend._active_controllers


@pytest.mark.asyncio
async def test_start_failure_retains_controller_until_safe_stop_then_allows_setup(
    monkeypatch,
) -> None:
    controller_id = "test-start-failure-owner-fence"

    class _Channel:
        disconnected = False

        async def diagnostics(self):
            return {
                "link_active": not self.disconnected,
                "parent_connected": not self.disconnected,
            }

    class _Link:
        is_connected = True

        async def disconnect(self, *, timeout):
            del timeout
            raise TransportError("disconnect not confirmed")

    channel = _Channel()
    first = ThinRpcBackend(
        _hass(),
        controller_id=controller_id,
        channel=channel,
        profile=object(),
        capability=ThinRpcCapability("request", "poll", "diagnostics"),
    )
    first.link = _Link()

    async def wait_for_disconnect(*, recovery_fence=None):
        if not channel.disconnected:
            raise HomeAssistantError("physical disconnect timeout")

    monkeypatch.setattr(first, "_wait_for_physical_disconnect", wait_for_disconnect)

    async def fail_establish(*, attach):
        assert attach is True
        raise HomeAssistantError("startup probe failed")

    monkeypatch.setattr(first, "_establish_session", fail_establish)
    with pytest.raises(HomeAssistantError, match="startup probe failed"):
        await first.async_start()

    assert first._owns_controller is True
    assert controller_id in first._active_controllers
    with pytest.raises(HomeAssistantError, match="physical disconnect timeout"):
        await first.async_stop()
    assert first._owns_controller is True
    assert controller_id in first._active_controllers

    second = ThinRpcBackend(
        _hass(),
        controller_id=controller_id,
        channel=channel,
        profile=object(),
        capability=ThinRpcCapability("request", "poll", "diagnostics"),
    )
    with pytest.raises(HomeAssistantError, match="already owned"):
        await second.async_start()
    assert second._owns_controller is False
    assert controller_id in second._active_controllers

    first.link.is_connected = False
    channel.disconnected = True
    await first.async_stop()
    assert first._owns_controller is False
    assert controller_id not in first._active_controllers

    async def succeed_establish(*, attach):
        assert attach is True

    monkeypatch.setattr(second, "_establish_session", succeed_establish)
    await second.async_start()
    assert second.started is True
    assert second._owns_controller is True
    await second.async_stop()


@pytest.mark.asyncio
async def test_same_thin_owner_can_retry_start_without_releasing_claim(
    monkeypatch,
) -> None:
    """A retained fail-closed claim may be resumed by its owning backend."""

    controller_id = "test-thin-same-owner-retry"

    class _Channel:
        async def diagnostics(self):
            return {"link_active": False, "parent_connected": False}

    backend = ThinRpcBackend(
        _hass(),
        controller_id=controller_id,
        channel=_Channel(),
        profile=object(),
        capability=ThinRpcCapability("request", "poll", "diagnostics"),
    )
    backend._active_controllers.add(controller_id)
    backend._owns_controller = True

    async def establish_session(*, attach: bool) -> None:
        assert attach is True

    monkeypatch.setattr(backend, "_establish_session", establish_session)
    await backend.async_start()

    assert backend.started is True
    assert backend._owns_controller is True
    assert controller_id in backend._active_controllers
    await backend.async_stop()


@pytest.mark.asyncio
async def test_cancelled_thin_start_releases_claim_after_idle_proof(
    monkeypatch,
) -> None:
    controller_id = "test-thin-cancel-release"
    entered = asyncio.Event()

    class _Channel:
        async def diagnostics(self):
            return {"link_active": False, "parent_connected": False}

    backend = ThinRpcBackend(
        _hass(),
        controller_id=controller_id,
        channel=_Channel(),
        profile=object(),
        capability=ThinRpcCapability("request", "poll", "diagnostics"),
    )

    async def establish_session(*, attach: bool) -> None:
        assert attach is True
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(backend, "_establish_session", establish_session)
    task = asyncio.create_task(backend.async_start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend._owns_controller is False
    assert controller_id not in backend._active_controllers


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def execute_service(self, service, data, **kwargs):
        self.calls.append((service, data, kwargs))
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"frame": '{"kind":"event","op":"CAPABILITY"}'},
        {"response": {"frame": '{"kind":"event","op":"CAPABILITY"}'}},
        {"service_response": {"frame": '{"kind":"event","op":"CAPABILITY"}'}},
        SimpleNamespace(
            response_data=b'{"frame":"{\\"kind\\":\\"event\\",\\"op\\":\\"CAPABILITY\\"}"}'
        ),
    ],
)
async def test_native_response_shapes_are_unwrapped(response) -> None:
    client = _Client(response)
    channel = HomeAssistantThinGattChannel(
        client,
        ThinRpcCapability("request", "poll", "diagnostics"),
    )
    frame = await channel.poll(timeout=1)
    assert frame == {"kind": "event", "op": "CAPABILITY"}


@pytest.mark.asyncio
async def test_batch_poll_drains_more_than_firmware_ring_in_order() -> None:
    frames = [
        {
            "kind": "event",
            "op": "NOTIFICATION",
            "epoch": 7,
            "seq": seq,
            "request_id": 0,
            "gattc_if": 1,
            "conn_id": 0,
            "payload": {"handle": 3, "value": ""},
        }
        for seq in range(1, 41)
    ]
    frames.append(
        {
            "kind": "response",
            "op": "WRITE_CHAR",
            "epoch": 7,
            "seq": 0,
            "request_id": 12,
            "gattc_if": 1,
            "conn_id": 0,
            "status": "OK",
        }
    )
    expected_frames = [dict(frame) for frame in frames]
    for frame in expected_frames[:-1]:
        frame["payload"] = dict(frame["payload"], value=b"")
        frame["handle"] = 3

    class _BatchClient(_Client):
        hass = _hass("openrbus_gatt_rpc_poll", "openrbus_gatt_rpc_poll_batch")

        def __init__(self):
            super().__init__({"frames": [json.dumps(frame) for frame in frames]})
            self.poll_calls = 0

        async def execute_service(self, service, data, **kwargs):
            self.calls.append((service, data, kwargs))
            self.poll_calls += 1
            start = (self.poll_calls - 1) * 8
            return {
                "frames": [json.dumps(frame) for frame in frames[start : start + 8]]
            }

    client = _BatchClient()
    channel = HomeAssistantThinGattChannel(
        client, ThinRpcCapability("request", "openrbus_gatt_rpc_poll", "diagnostics")
    )
    session = ThinGattSession(channel)
    session.connected = True
    session.epoch = 7
    session.identity = ConnectionIdentity(1, 0)
    session.last_seq = 0
    session.register_request("WRITE_CHAR", request_id=12)

    received = []
    for _ in frames:
        frame = await channel.poll(timeout=1)
        assert frame is not None
        session.ingest(frame)
        received.append(frame)

    assert received == expected_frames
    assert session.last_seq == 40
    assert not session._pending_operations
    assert client.poll_calls == 6
    assert {call[0] for call in client.calls} == {"openrbus_gatt_rpc_poll_batch"}


@pytest.mark.asyncio
async def test_batch_poll_malformed_item_fails_before_buffering_any_frame() -> None:
    class _BatchClient(_Client):
        hass = _hass("openrbus_gatt_rpc_poll_batch")

    client = _BatchClient(
        {
            "frames": [
                json.dumps({"kind": "event", "op": "NOTIFICATION"}),
                "not-json",
            ]
        }
    )
    channel = HomeAssistantThinGattChannel(
        client, ThinRpcCapability("request", "openrbus_gatt_rpc_poll", "diagnostics")
    )

    with pytest.raises(TransportError, match="invalid Thin-RPC poll response"):
        await channel.poll(timeout=1)
    assert not channel._batch_poll_frames
    assert client.calls[0][0] == "openrbus_gatt_rpc_poll_batch"


@pytest.mark.asyncio
async def test_connect_discards_frames_buffered_from_prior_epoch() -> None:
    frames = [
        json.dumps(
            {
                "kind": "event",
                "op": "NOTIFICATION",
                "epoch": 7,
                "seq": i,
                "request_id": 0,
                "gattc_if": 1,
                "conn_id": 0,
                "payload": {"handle": 3, "value": ""},
            }
        )
        for i in range(1, 4)
    ]

    class _BatchClient(_Client):
        hass = _hass("openrbus_gatt_rpc_poll_batch")

    client = _BatchClient({"frames": frames})
    channel = HomeAssistantThinGattChannel(
        client, ThinRpcCapability("request", "openrbus_gatt_rpc_poll", "diagnostics")
    )
    assert (await channel.poll(timeout=1))["seq"] == 1
    assert len(channel._batch_poll_frames) == 2

    await channel.action("request", {"op": "CONNECT", "request_id": 1}, timeout=1)

    assert not channel._batch_poll_frames


@pytest.mark.asyncio
async def test_old_firmware_uses_single_frame_poll_service() -> None:
    class _OldFirmwareClient(_Client):
        hass = _hass("openrbus_gatt_rpc_poll")

    client = _OldFirmwareClient({"frame": '{"kind":"event","op":"CAPABILITY"}'})
    channel = HomeAssistantThinGattChannel(
        client, ThinRpcCapability("request", "openrbus_gatt_rpc_poll", "diagnostics")
    )

    assert (await channel.poll(timeout=1))["op"] == "CAPABILITY"
    assert client.calls[0][0] == "openrbus_gatt_rpc_poll"


@pytest.mark.asyncio
async def test_nested_diagnostics_mapping_is_unwrapped() -> None:
    client = _Client(
        {
            "response": {
                "snapshot": json.dumps(
                    {
                        "rpc_schema_version": 2,
                        "pair_contract": "pair_terminal_v2",
                        "pair_requests": 3,
                        "pair_last_status": "terminal_success",
                    }
                )
            }
        }
    )
    channel = HomeAssistantThinGattChannel(
        client,
        ThinRpcCapability("request", "poll", "diagnostics"),
    )
    assert await channel.diagnostics() == {
        "rpc_schema_version": 2,
        "pair_contract": "pair_terminal_v2",
        "pair_requests": 3,
        "pair_last_status": "terminal_success",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"response": {"frame": "{}"}, "service_response": {"frame": "{}"}},
        {"service_response": []},
        {"service_response": {"other": "{}"}},
        {"response": {"service_response": {"frame": "{}"}}},
        {},
    ],
)
async def test_native_response_envelopes_fail_closed(response) -> None:
    channel = HomeAssistantThinGattChannel(
        _Client(response),
        ThinRpcCapability("request", "poll", "diagnostics"),
    )
    with pytest.raises(TransportError):
        await channel.poll(timeout=1)


@pytest.mark.asyncio
async def test_service_response_diagnostics_envelope_is_unwrapped() -> None:
    channel = HomeAssistantThinGattChannel(
        _Client({"service_response": {"snapshot": '{"state":"ready"}'}}),
        ThinRpcCapability("request", "poll", "diagnostics"),
    )
    assert await channel.diagnostics() == {"state": "ready"}


@pytest.mark.asyncio
async def test_configured_services_are_called_exactly() -> None:
    client = _Client({"frame": "", "snapshot": "{}"})
    channel = HomeAssistantThinGattChannel(
        client,
        ThinRpcCapability("boiler_request", "boiler_poll", "boiler_diagnostics"),
    )
    await channel.action(
        "boiler_request", {"op": "CONNECT", "request_id": 1}, timeout=1
    )
    await channel.poll(timeout=1)
    await channel.diagnostics()
    assert [item[0] for item in client.calls] == [
        "boiler_request",
        "boiler_poll",
        "boiler_diagnostics",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", ["", "   ", b"", b"\t\n"])
async def test_empty_thin_rpc_poll_frames_are_no_message(frame) -> None:
    """An ESPHome poll can be empty while the remote scan is still running."""

    channel = HomeAssistantThinGattChannel(
        _Client({"frame": frame}),
        ThinRpcCapability("request", "poll", "diagnostics"),
    )
    assert await channel.poll(timeout=1) is None


@pytest.mark.asyncio
async def test_nonempty_malformed_thin_rpc_poll_frame_fails_closed() -> None:
    channel = HomeAssistantThinGattChannel(
        _Client({"frame": "not-json"}),
        ThinRpcCapability("request", "poll", "diagnostics"),
    )
    with pytest.raises(TransportError, match="invalid Thin-RPC poll response"):
        await channel.poll(timeout=1)


@pytest.mark.asyncio
async def test_connect_carries_selected_remote_ble_target() -> None:
    client = _Client({"frame": ""})
    channel = HomeAssistantThinGattChannel(
        client,
        ThinRpcCapability("request", "poll", "diagnostics"),
        target_address="AA:BB:CC:DD:EE:FF",
    )
    await channel.action("request", {"op": "CONNECT", "request_id": 1}, timeout=1)
    frame = json.loads(client.calls[0][1]["frame"])
    assert frame["payload"]["target_address"] == "AA:BB:CC:DD:EE:FF"


@pytest.mark.asyncio
async def test_remote_scan_collects_all_visible_devices_until_done() -> None:
    class ScanClient:
        def __init__(self):
            self.calls = []
            self.frames = [
                {
                    "frame": json.dumps(
                        {
                            "kind": "event",
                            "op": "CONNECTION_STATE",
                            "request_id": 0,
                            "epoch": 3,
                            "gattc_if": 3,
                            "conn_id": 0,
                            "payload": {"state": "disconnected"},
                        }
                    )
                },
                {
                    "frame": json.dumps(
                        {
                            "kind": "response",
                            "op": "SCAN",
                            "request_id": 1,
                            "epoch": 0,
                            "gattc_if": 0,
                            "conn_id": 0,
                            "status": "ACCEPTED",
                        }
                    )
                },
                {
                    "frame": json.dumps(
                        {
                            "kind": "event",
                            "op": "SCAN_RESULT",
                            "request_id": 1,
                            "epoch": 0,
                            "gattc_if": 0,
                            "conn_id": 0,
                            "payload": {"address": "AA", "name": "one", "rssi": -40},
                        }
                    )
                },
                {
                    "frame": json.dumps(
                        {
                            "kind": "event",
                            "op": "SCAN_RESULT",
                            "request_id": 1,
                            "epoch": 0,
                            "gattc_if": 0,
                            "conn_id": 0,
                            "payload": {"address": "BB", "name": "two", "rssi": -60},
                        }
                    )
                },
                {
                    "frame": json.dumps(
                        {
                            "kind": "event",
                            "op": "SCAN_DONE",
                            "request_id": 1,
                            "epoch": 0,
                            "gattc_if": 0,
                            "conn_id": 0,
                            "payload": {"count": 2},
                        }
                    )
                },
            ]

        async def execute_service(
            self,
            _domain,
            service,
            data,
            *,
            return_response=False,
            timeout=None,
            **_kwargs,
        ):
            self.calls.append((service, data))
            if service.endswith("_poll"):
                return self.frames.pop(0)
            return {"success": True}

    client = ScanClient()
    hass = SimpleNamespace(
        services=SimpleNamespace(
            async_call=client.execute_service,
            async_services=lambda: {"esphome": {"openrbus_gatt_rpc_poll": object()}},
        )
    )
    devices = await async_scan_thin_rpc_devices(
        hass,
        ThinRpcCapability(
            "request", "openrbus_gatt_rpc_poll", "openrbus_gatt_rpc_diagnostics"
        ),
        duration=1,
    )
    assert [item["address"] for item in devices] == ["AA", "BB"]
    scan_request = json.loads(
        [data["frame"] for service, data in client.calls if service == "request"][-1]
    )
    # The ESP scan server validates the v1 envelope before dispatching SCAN;
    # duration_ms belongs in the nested payload, while routing metadata stays
    # at the frame root.  Keep this contract explicit so a bare/flattened
    # request cannot regress to INVALID_REQUEST/invalid_payload.
    assert scan_request["v"] == 1
    assert scan_request["kind"] == "request"
    assert scan_request["op"] == "SCAN"
    assert scan_request["request_id"] == 1
    assert scan_request["epoch"] == 0
    assert scan_request["gattc_if"] == 0
    assert scan_request["conn_id"] == 0
    assert scan_request["payload"] == {"duration_ms": 1000}
    assert [service for service, _data in client.calls].count(
        "openrbus_gatt_rpc_diagnostics"
    ) == 0
    assert all(
        json.loads(data["frame"])["op"] == "SCAN"
        for service, data in client.calls
        if service == "request"
    )


def test_scan_frame_accepts_zero_identity_without_gatt_session() -> None:
    _validate_scan_frame(
        {
            "kind": "event",
            "op": "SCAN_RESULT",
            "request_id": 7,
            "epoch": 0,
            "gattc_if": 0,
            "conn_id": 0,
            "payload": {"address": "AA"},
        },
        7,
    )


def test_scan_frame_accepts_canonical_no_session_interface_sentinel() -> None:
    _validate_scan_frame(
        {
            "kind": "event",
            "op": "SCAN_DONE",
            "request_id": 7,
            "epoch": 0,
            "gattc_if": 255,
            "conn_id": 0,
            "payload": {"count": 0},
        },
        7,
    )


@pytest.mark.parametrize("request_id", [6, 8])
def test_scan_frame_rejects_stale_or_wrong_request(request_id: int) -> None:
    with pytest.raises(ThinGattCorrelationError, match="stale or mismatched"):
        _validate_scan_frame(
            {
                "kind": "event",
                "op": "SCAN_DONE",
                "request_id": request_id,
                "epoch": 0,
                "gattc_if": 0,
                "conn_id": 0,
            },
            7,
        )


def test_scan_frame_rejects_nonzero_identity_but_normal_gatt_is_untouched() -> None:
    with pytest.raises(ThinGattCorrelationError, match="identity"):
        _validate_scan_frame(
            {
                "kind": "event",
                "op": "SCAN_DONE",
                "request_id": 7,
                "epoch": 1,
                "gattc_if": 2,
                "conn_id": 3,
            },
            7,
        )


def test_thin_diagnostics_redacts_identity_and_credentials() -> None:
    backend = ThinRpcBackend(_hass(), controller_id="controller", channel=object())
    backend._diagnostics = {
        "state": "ready",
        "identity": "private-identity",
        "passkey": "123456",
        "payload": "secret-payload",
    }
    diagnostics = backend.diagnostics()
    text = repr(diagnostics)
    assert "private-identity" not in text
    assert "123456" not in text
    assert "secret-payload" not in text
    assert "snapshot" not in diagnostics


def test_thin_only_policy_does_not_treat_diagnostics_as_auth_proof() -> None:
    backend = ThinRpcBackend(_hass(), controller_id="controller", channel=object())
    backend._diagnostics = {
        "encrypted": True,
        "gateway_auth_ready": True,
        "current_boot_id": "boot-1",
    }
    assert backend.authentication is None
