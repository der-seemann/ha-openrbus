"""Privacy and stability coverage for aggregate transport diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.discovery import DeviceIdentity
from openrbus.errors import (
    CanOpenAbortError,
    RequestTimeoutError,
    TransportError,
    ValidationError,
)
from openrbus.protocol.canip import ObjectAddress
from openrbus.transport.thin_gatt import (
    ThinGattCorrelationError,
    ThinGattFlowControlError,
    ThinGattSessionStateError,
)

from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.coordinator import (
    OpenRBusCoordinator,
    OpenRBusPollingCoordinator,
)
from custom_components.openrbus.diagnostics import (
    _safe_batch_failure_trace,
    _safe_poll_selection_snapshot,
    _safe_poll_snapshot,
    _safe_read_operation_trace,
    _safe_read_transport_capture,
    _safe_recovery_fence,
    _safe_setup_response,
    _safe_thin_rpc_frame_trace,
    async_get_config_entry_diagnostics,
)
from custom_components.openrbus.setup_observability import (
    RecoveryFenceMetrics,
    SetupResponseMetrics,
)
from custom_components.openrbus.transport import (
    _abort_category,
    _read_error_class,
    _read_error_subtype,
    _tag_read_error,
    _visible_string_decode_subtype,
    safe_batch_exception_type,
)
from custom_components.openrbus.validity import RegisterValidityTracker


def test_thin_rpc_frame_trace_allows_only_bounded_categorical_fields() -> None:
    trace = _safe_thin_rpc_frame_trace(
        [
            {
                "direction": "request",
                "kind": "request",
                "op": "WRITE_CHAR",
                "seq": 4,
                "epoch": 2,
                "request_id_present": True,
                "request_id": 17,
                "context": "discovery",
                "read_operation_id": 9,
                "status": "OK",
                "state_category": "connected",
                "payload": "private bytes",
                "peer": "private identity",
            },
            {
                "direction": "private",
                "kind": "private",
                "op": "private",
                "request_id": -1,
                "context": "private",
            },
        ]
    )

    assert trace == [
        {
            "direction": "request",
            "kind": "request",
            "op": "WRITE_CHAR",
            "request_id_present": True,
            "seq": 4,
            "epoch": 2,
            "request_id": 17,
            "status": "OK",
            "state_category": "connected",
            "context": "discovery",
            "read_operation_id": 9,
        },
        {
            "direction": "other",
            "kind": "other",
            "op": "other",
            "request_id_present": False,
        },
    ]


def test_poll_selection_diagnostics_allow_only_bounded_aggregate_counts() -> None:
    assert _safe_poll_selection_snapshot(
        {
            "runtime_nodes": 5,
            "catalog_rows": 17,
            "read_access_excluded": 17,
            "private_address": "must not escape",
            "negative": -1,
        }
    ) == {
        "runtime_nodes": 5,
        "catalog_rows": 17,
        "read_access_excluded": 17,
    }


def test_read_transport_diagnostics_retain_only_safe_join_fields() -> None:
    operations = _safe_read_operation_trace(
        [
            {
                "operation_id": 7,
                "kind": "single",
                "outcome": "success",
                "epoch": 12,
                "origin": "bridge_health",
                "address": "private object",
            },
            {
                "operation_id": -1,
                "origin": "manual_read_service",
                "address": "private object",
            },
            {"operation_id": 8, "kind": "single", "origin": "private origin"},
        ]
    )
    capture = _safe_read_transport_capture(
        {
            "operation_id": 7,
            "outcome": "success",
            "before": {
                "epoch": 12,
                "rpc_requests": 25,
                "att_write_char_calls": 10,
                "last_notification_seq": 4,
                "notification_callbacks": 40,
                "total_frames_enqueued": 39,
                "poll_frame_calls": 75,
                "poll_nonempty_returns": 38,
                "poll_empty_returns": 37,
                "event_queue_depth": 1,
                "last_enqueued_kind": "response",
                "last_enqueued_op": "WRITE_CHAR",
                "peer": "private device",
            },
            "after": {
                "epoch": 12,
                "rpc_requests": 26,
                "last_rpc_request_id": 26,
                "last_att_write_request_id": 26,
                "last_att_write_status": "success",
                "last_notification_epoch": 12,
                "last_notification_seq": 5,
                "last_notification_request_id": 0,
                "last_notification_nonempty": True,
                "last_notification_matched_request": False,
                "notification_callbacks": 41,
                "total_frames_enqueued": 40,
                "poll_frame_calls": 76,
                "poll_nonempty_returns": 39,
                "poll_empty_returns": 37,
                "event_queue_depth": 0,
                "last_enqueued_kind": "response",
                "last_enqueued_op": "WRITE_CHAR",
                "value": "private payload",
                "raw_frame": "private payload",
                "last_enqueued_op_with_payload": "private payload",
            },
        }
    )
    assert operations == [
        {
            "operation_id": 7,
            "kind": "single",
            "outcome": "success",
            "origin": "bridge_health",
            "epoch": 12,
        },
        {"operation_id": 8, "kind": "single"},
    ]
    assert capture["operation_id"] == 7
    assert capture["before"]["epoch"] == 12
    assert capture["after"]["last_att_write_request_id"] == 26
    assert capture["after"]["last_notification_seq"] == 5
    assert capture["before"]["total_frames_enqueued"] == 39
    assert capture["after"]["poll_nonempty_returns"] == 39
    assert capture["after"]["last_enqueued_kind"] == "response"
    assert capture["after"]["last_enqueued_op"] == "WRITE_CHAR"
    assert "peer" not in repr(capture)
    assert "value" not in repr(capture)
    assert "raw_frame" not in repr(capture)
    assert "last_enqueued_op_with_payload" not in repr(capture)


def test_safe_batch_failure_trace_is_payload_free_and_bounded() -> None:
    safe = _safe_batch_failure_trace(
        {
            "operation_id": 23,
            "stage": "single_fallback",
            "exception_class": "transport_error",
            "request_id": 81,
            "epoch": 7,
            "before": {"notification_callbacks": 5, "payload": "secret"},
            "after": {"notification_callbacks": 6, "last_notification_request_id": 81},
            "message": "private details",
        }
    )
    assert safe == {
        "operation_id": 23,
        "stage": "single_fallback",
        "exception_class": "transport_error",
        "request_id": 81,
        "epoch": 7,
        "before": {"notification_callbacks": 5},
        "after": {
            "notification_callbacks": 6,
            "last_notification_request_id": 81,
        },
    }
    assert (
        _safe_batch_failure_trace(
            {
                "operation_id": 23,
                "stage": "private_stage",
                "exception_class": "transport_error",
            }
        )
        == {}
    )


@pytest.mark.asyncio
async def test_diagnostics_reads_pollers_from_hass_entry_registry_fallback() -> None:
    poller = SimpleNamespace(
        diagnostics=lambda: {
            "poll_count": 2,
            "failed_items": 3,
            "availability_delta": -3,
            "error_counts": {"item": 3},
        }
    )
    coordinator = SimpleNamespace(
        devices=(),
        inventories=(),
        discovery_error=None,
        _backend=None,
        _cycle_id=0,
        last_update_success=True,
        _diagnostic_poll_count=0,
    )
    entry = SimpleNamespace(
        runtime_data=coordinator,
        entry_id="entry-1",
        domain="openrbus",
        data={"backend": "esphome_thin_rpc"},
        options={},
    )
    hass = SimpleNamespace(
        data={"openrbus_polling_coordinators": {"entry-1": {"fast": poller}}}
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["coordinator"]["poll_groups"]["fast"]["error_counts"]["item"] == 3
    assert result["coordinator"]["poll_groups"]["fast"]["availability_delta"] == -3


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ThinGattCorrelationError("secret frame payload"), "correlation"),
        (ThinGattSessionStateError("private session identity"), "session"),
        (RequestTimeoutError("private address"), "session"),
        (ValidationError("private object details"), "decode"),
        (CanOpenAbortError(0x06010000), "abort"),
        (HomeAssistantError("private abort text"), "item"),
    ],
)
def test_read_error_class_is_fixed_and_never_uses_error_text(error, expected) -> None:
    tagged = _tag_read_error(HomeAssistantError(str(error)), _read_error_class(error))
    assert _read_error_class(tagged) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ThinGattSessionStateError("private state"), "not_ready"),
        (ThinGattSessionStateError("private link disconnected"), "link_lost"),
        (ThinGattSessionStateError("private link not securely prepared"), "not_secure"),
        (RequestTimeoutError("private target"), "timeout"),
        (TransportError("private transport text"), "transport"),
        (ThinGattFlowControlError("queue_full"), "flow_control_queue_full"),
        (ThinGattFlowControlError("private frame payload"), "flow_control_unknown"),
    ],
)
def test_session_error_subtype_is_fixed_and_redacted(error, expected) -> None:
    assert _read_error_subtype(error, "session") == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0x06010000, "unsupported_access"),
        (0x06010001, "read_not_supported"),
        (0x06020000, "object_missing"),
        (0x06090011, "subindex_missing"),
        (0xDEADBEEF, "other_abort"),
    ],
)
def test_abort_code_is_reduced_to_allowlisted_category(code, expected) -> None:
    assert _abort_category(CanOpenAbortError(code, "private message")) == expected


def test_visible_string_decode_subtype_uses_only_shape_and_exception_class() -> None:
    with pytest.raises(UnicodeDecodeError) as caught:
        b"\xff".decode("ascii")
    non_ascii = ValidationError("private address and value")
    non_ascii.__cause__ = caught.value
    assert (
        _visible_string_decode_subtype(non_ascii, b"\xff", 20)
        == "visible_string_non_ascii"
    )
    assert (
        _visible_string_decode_subtype(ValidationError("private"), b"x" * 21, 20)
        == "visible_string_overlength"
    )
    assert (
        _visible_string_decode_subtype(ValidationError("private"), b"x", 20)
        == "visible_string_other"
    )
    assert (
        safe_batch_exception_type(RequestTimeoutError("private target"))
        == "request_timeout"
    )
    assert (
        safe_batch_exception_type(HomeAssistantError("private details"))
        == "home_assistant_error"
    )
    assert safe_batch_exception_type(RuntimeError("private details")) == "runtime_error"
    assert (
        safe_batch_exception_type(AttributeError("private details"))
        == "attribute_error"
    )
    assert (
        safe_batch_exception_type(NotImplementedError("private details"))
        == "not_implemented_error"
    )


def test_poll_snapshot_keeps_only_bounded_redacted_numbers() -> None:
    snapshot = _safe_poll_snapshot(
        {
            "poll_count": 4,
            "success_items": 10,
            "failed_items": 2,
            "available_items": 8,
            "unavailable_items": 2,
            "availability_delta": -1,
            "error_counts": {
                "item": 1,
                "batch": 1,
                "decode": 0,
                "correlation": 0,
                "session": 0,
                "abort": 0,
                "secret frame": "private payload",
            },
            "item_failures": [
                {
                    "node": 4,
                    "index": 0x1234,
                    "subindex": 2,
                    "error_class": "item",
                    "error": "private abort text",
                    "payload": "private bytes",
                    "device": "private identity",
                    "abort_category": "object_missing",
                    "decode_subtype": "visible_string_non_ascii",
                    "batch_exception_type": "request_timeout",
                }
            ],
            "message": "private controller identifier",
        }
    )
    assert snapshot == {
        "poll_count": 4,
        "success_items": 10,
        "failed_items": 2,
        "available_items": 8,
        "unavailable_items": 2,
        "availability_delta": -1,
        "error_counts": {
            "item": 1,
            "batch": 1,
            "decode": 0,
            "correlation": 0,
            "session": 0,
            "abort": 0,
        },
    }
    opted_in = _safe_poll_snapshot(
        {
            "quarantined_count": 1,
            "quarantined_items": [
                {
                    "node": 1,
                    "index": 0x430E,
                    "subindex": 0,
                    "error_class": "abort",
                    "subtype": "unsupported_access",
                    "scope": "private identity/access/session",
                }
            ],
            "item_failures": [
                {
                    "node": 4,
                    "index": 0x1234,
                    "subindex": 2,
                    "error_class": "item",
                    "error": "private abort text",
                    "abort_category": "object_missing",
                    "decode_subtype": "visible_string_non_ascii",
                    "batch_exception_type": "request_timeout",
                },
            ],
        },
        include_item_failures=True,
    )
    assert opted_in == {
        "quarantined_count": 1,
        "item_failures": [
            {
                "node": 4,
                "index": 0x1234,
                "subindex": 2,
                "error_class": "item",
                "abort_category": "object_missing",
                "decode_subtype": "visible_string_non_ascii",
                "batch_exception_type": "request_timeout",
            }
        ],
        "quarantined_items": [
            {
                "node": 1,
                "index": 0x430E,
                "subindex": 0,
                "error_class": "abort",
                "subtype": "unsupported_access",
            }
        ],
    }
    assert "private" not in repr(opted_in)


def test_setup_response_metrics_are_fixed_bounded_and_redacted() -> None:
    metrics = SetupResponseMetrics()
    metrics.record("pairing_arm", "call_not_sent", 31)
    metrics.record("handle_lookup", "call_completed", 500)
    metrics.record("poll_request", "no_esp_response", 1500)
    metrics.mark_handle_lookup_response()
    metrics.record_setup_cancellation(12000)
    snapshot = metrics.diagnostics()
    assert snapshot["pairing_arm"]["outcomes"]["call_not_sent"] == 1
    assert snapshot["handle_lookup"]["outcomes"]["call_completed"] == 1
    assert snapshot["handle_lookup"]["outcomes"]["cancelled_after_response"] == 1
    assert snapshot["handle_lookup"]["elapsed_ms_buckets"]["gte_10s"] == 1
    assert snapshot["scope"] == "backend_lifetime"
    assert snapshot["setup_attempts"] == 0
    metrics.begin_attempt()
    assert metrics.diagnostics()["setup_attempts"] == 1
    metrics.mark_handle_lookup_response()
    metrics.record_setup_cancellation(100)
    assert (
        metrics.diagnostics()["handle_lookup"]["outcomes"]["cancelled_after_response"]
        == 2
    )
    assert snapshot["poll_request"]["outcomes"]["no_esp_response"] == 1
    assert "private" not in repr(snapshot)
    assert _safe_setup_response({**snapshot, "private": "secret"}) == snapshot


def test_safe_setup_response_rejects_unbounded_fields_and_values() -> None:
    safe = _safe_setup_response(
        {
            "handle_lookup": {
                "outcomes": {
                    "response": 2_147_483_648,
                    "call_not_sent": -5,
                    "private": 2,
                },
                "elapsed_ms_buckets": {"gte_10s": 2_147_483_648, "private": 9},
                "error": "secret payload",
            },
            "private_phase": {"outcomes": {"response": 99}},
        }
    )
    assert safe == {
        "handle_lookup": {
            "outcomes": {"call_not_sent": 0, "response": 2_147_483_647},
            "elapsed_ms_buckets": {"gte_10s": 2_147_483_647},
        }
    }


def test_recovery_fence_metrics_are_bounded_and_privacy_safe() -> None:
    metrics = RecoveryFenceMetrics()
    metrics.begin_attempt()
    metrics.record_dispatch(True)
    metrics.record_state(False, True)
    metrics.record_timeout()
    snapshot = metrics.diagnostics()

    assert snapshot == {
        "attempts": 1,
        "dispatch_acknowledged": True,
        "link_active": False,
        "parent_connected": True,
        "timeout_count": 1,
        "timeout_classes": {
            "both_connected": 0,
            "link_active": 0,
            "parent_connected": 1,
            "state_unavailable": 0,
        },
        "last_timeout_class": "parent_connected",
        "disconnect_attempt": {
            "attempt_id": 1,
            "session_epoch": None,
            "outcome": None,
            "exception_class": None,
            "origin": "unspecified",
        },
    }
    assert _safe_recovery_fence({**snapshot, "identity": "private"}) == snapshot

    metrics.begin_attempt(17, "bridge_health")
    assert metrics.diagnostics()["disconnect_attempt"]["origin"] == "bridge_health"
    metrics.begin_attempt(18, "startup_discovery")
    assert (
        _safe_recovery_fence(metrics.diagnostics())["disconnect_attempt"]["origin"]
        == "startup_discovery"
    )
    assert (
        _safe_recovery_fence(
            {
                "disconnect_attempt": {
                    "attempt_id": 2,
                    "session_epoch": 17,
                    "outcome": "missing_identity",
                    "origin": "bridge_health",
                    "address": "private object",
                }
            }
        )["disconnect_attempt"]["origin"]
        == "bridge_health"
    )

    unsafe = _safe_recovery_fence(
        {
            "attempts": 2_147_483_648,
            "dispatch_acknowledged": 1,
            "link_active": "true",
            "parent_connected": False,
            "timeout_count": -1,
            "timeout_classes": {
                "both_connected": 2_147_483_648,
                "parent_connected": -1,
                "private": 3,
            },
            "last_timeout_class": "secret",
            "raw_frame": "must not export",
        }
    )
    assert unsafe == {
        "parent_connected": False,
        "timeout_classes": {
            "both_connected": 2_147_483_647,
            "parent_connected": 0,
        },
    }


@pytest.mark.asyncio
async def test_entry_diagnostics_add_only_redacted_transport_metrics() -> None:
    poller = SimpleNamespace(
        diagnostics=lambda: {
            "poll_count": 3,
            "available_items": 7,
            "unavailable_items": 1,
            "availability_delta": -1,
            "registry_disabled_count": 2,
            "poll_in_progress": True,
            "error_counts": {"session": 2},
            "item_failures": [
                {"node": 4, "index": 0x1234, "subindex": 2, "error_class": "item"}
            ],
            "private": "must not escape",
        }
    )
    coordinator = SimpleNamespace(
        devices=(),
        inventories=(),
        discovery_error=None,
        _cycle_id=3,
        last_update_success=False,
        _discovery_attempted=True,
        _diagnostic_poll_count=4,
        _diagnostic_error_counts={"correlation": 2},
        _backend=SimpleNamespace(
            diagnostics=lambda: {
                "session_generation": 5,
                "session_epoch": 17,
                "recovery_fence": {
                    "attempts": 2,
                    "dispatch_acknowledged": False,
                    "link_active": True,
                    "parent_connected": False,
                    "timeout_count": 1,
                    "timeout_classes": {
                        "both_connected": 0,
                        "link_active": 1,
                        "parent_connected": 0,
                        "state_unavailable": 0,
                    },
                    "last_timeout_class": "link_active",
                    "disconnect_attempt": {
                        "attempt_id": 2,
                        "session_epoch": 17,
                        "outcome": "service_exception",
                        "exception_class": "TimeoutError",
                        "origin": "private caller string",
                        "exception_message": "private detail",
                    },
                    "private": "must not escape",
                },
                "target_address": "AA:BB:CC:DD:EE:FF",
                "token": "credential",
            }
        ),
        _openrbus_polling_coordinators={"standard": poller},
    )
    entry = SimpleNamespace(
        runtime_data=coordinator,
        data={"backend": "esphome_thin_rpc", "private": "secret"},
    )
    diagnostics = await async_get_config_entry_diagnostics(None, entry)
    rendered = repr(diagnostics)
    assert diagnostics["coordinator"]["transport_session"] == {
        "session_generation": 5,
        "session_epoch": 17,
        "recovery_fence": {
            "attempts": 2,
            "dispatch_acknowledged": False,
            "link_active": True,
            "parent_connected": False,
            "timeout_count": 1,
            "timeout_classes": {
                "both_connected": 0,
                "link_active": 1,
                "parent_connected": 0,
                "state_unavailable": 0,
            },
            "last_timeout_class": "link_active",
            "disconnect_attempt": {
                "attempt_id": 2,
                "session_epoch": 17,
                "outcome": "service_exception",
                "exception_class": "TimeoutError",
            },
        },
    }
    assert diagnostics["coordinator"]["coordinator_error_counts"]["correlation"] == 2
    assert diagnostics["coordinator"]["poll_in_progress"] is True
    assert "poll_lock_locked" not in diagnostics["coordinator"]
    assert diagnostics["coordinator"]["coordinator_poll_count"] == 4
    assert (
        diagnostics["coordinator"]["poll_groups"]["standard"]["error_counts"]["session"]
        == 2
    )
    assert (
        diagnostics["coordinator"]["poll_groups"]["standard"]["registry_disabled_count"]
        == 2
    )
    assert "item_failures" not in diagnostics["coordinator"]["poll_groups"]["standard"]
    assert "AA:BB:CC:DD:EE:FF" not in rendered
    assert "credential" not in rendered
    assert "must not escape" not in rendered
    assert "secret" not in rendered


@pytest.mark.asyncio
async def test_poll_group_tracks_fixed_error_classes_and_availability_delta() -> None:
    addresses = tuple(ObjectAddress(0x5000 + index, 0) for index in range(6))
    classes = ("item", "batch", "decode", "correlation", "session")
    values = []
    for error_class in classes:
        error = HomeAssistantError("private test detail")
        error._openrbus_error_class = error_class
        values.append(error)
    values.append(GenericRead(1, addresses[-1], b"\x01", 1))
    poller_ref = {}

    class _Parent:
        config_entry = SimpleNamespace(
            entry_id="entry",
            data={"ble_device": "00:11:22:33:44:55"},
            options={"diagnostics_enabled": True},
        )

        async def async_read_objects(self, _addresses, *, node):
            assert node == 1
            assert poller_ref["poller"].diagnostics()["poll_in_progress"] is True
            return tuple(values)

    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller.hass = SimpleNamespace()
    poller.parent = _Parent()
    poller.group = "standard"
    poller.registers = tuple((1, address) for address in addresses)
    poller.register_metadata = {}
    poller.validity = RegisterValidityTracker(
        __import__("datetime").timedelta(minutes=5)
    )
    poller._diagnostic_poll_count = 0
    poller._diagnostic_success_items = 0
    poller._diagnostic_failed_items = 0
    poller._diagnostic_error_counts = dict.fromkeys(classes, 0)
    poller._diagnostic_subtype_counts = dict.fromkeys(
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
    poller._diagnostic_item_failures = []
    poller._diagnostic_available_items = 0
    poller._diagnostic_total_items = 0
    poller._diagnostic_availability_delta = 0
    poller._diagnostic_registry_disabled_count = 0
    poller_ref["poller"] = poller

    await poller._async_update_data()
    metrics = poller.diagnostics()
    assert metrics["poll_count"] == 1
    assert metrics["poll_in_progress"] is False
    assert metrics["success_items"] == 1
    assert metrics["failed_items"] == 5
    assert metrics["available_items"] == 1
    assert metrics["unavailable_items"] == 5
    assert metrics["availability_delta"] == 1
    assert metrics["error_counts"] == {
        "item": 1,
        "batch": 1,
        "decode": 1,
        "correlation": 1,
        "session": 1,
    }
    assert metrics["error_subtype_counts"]["session.transport"] == 1
    assert metrics["error_subtype_counts"]["batch.fallback"] == 1
    assert metrics["item_failures"] == tuple(
        {
            "node": 1,
            "index": addresses[index].index,
            "subindex": addresses[index].subindex,
            "error_class": error_class,
            **(
                {"error_subtype": "transport"}
                if error_class == "session"
                else {"error_subtype": "fallback"}
                if error_class == "batch"
                else {}
            ),
        }
        for index, error_class in enumerate(classes)
    )


@pytest.mark.asyncio
async def test_poll_group_quarantines_object_failures_until_scope_changes() -> None:
    address = ObjectAddress(0x430E, 0)
    abort = HomeAssistantError("private detail")
    abort._openrbus_error_class = "abort"
    abort._openrbus_abort_category = "unsupported_access"
    identity = DeviceIdentity(
        node=1,
        device_code=4,
        parameter_number=7,
        name="private identity label",
    )
    backend = SimpleNamespace(_session_generation=2, session=SimpleNamespace(epoch=7))

    class _Parent:
        config_entry = SimpleNamespace(
            entry_id="entry",
            data={"ble_device": "00:11:22:33:44:55"},
            options={"diagnostics_enabled": True},
        )
        backend_mode = "esphome_thin_rpc"
        inventories = (SimpleNamespace(identity=identity),)
        _backend = backend

        def __init__(self):
            self.effective_access_levels = {1: 1}
            self.read_calls = 0

        async def async_read_objects(self, addresses, *, node):
            assert node == 1
            assert addresses == (address,)
            self.read_calls += 1
            return (abort,)

    parent = _Parent()
    poller = OpenRBusPollingCoordinator.__new__(OpenRBusPollingCoordinator)
    poller.hass = SimpleNamespace()
    poller.parent = parent
    poller.group = "standard"
    poller.registers = ((1, address),)
    poller.register_metadata = {}
    poller.validity = RegisterValidityTracker(__import__("datetime").timedelta(hours=1))
    poller._diagnostic_poll_count = 0
    poller._diagnostic_success_items = 0
    poller._diagnostic_failed_items = 0
    poller._diagnostic_error_counts = dict.fromkeys(
        ("abort", "item", "batch", "decode", "correlation", "session"), 0
    )
    poller._diagnostic_subtype_counts = dict.fromkeys(
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
    poller._diagnostic_item_failures = []
    poller._diagnostic_available_items = 0
    poller._diagnostic_total_items = 0
    poller._diagnostic_availability_delta = 0
    poller._diagnostic_registry_disabled_count = 0
    poller._poll_in_progress_count = 0

    first = await poller._async_poll_data()
    assert isinstance(first[(1, address)], HomeAssistantError)
    assert parent.read_calls == 1
    assert poller.diagnostics()["error_counts"]["abort"] == 1
    assert poller.diagnostics()["quarantined_items"] == (
        {
            "node": 1,
            "index": 0x430E,
            "subindex": 0,
            "error_class": "abort",
            "subtype": "unsupported_access",
        },
    )

    # An unchanged access, identity, and epoch leaves the object unavailable
    # without issuing another bus read or adding another poll error.
    second = await poller._async_poll_data()
    assert second == {}
    assert parent.read_calls == 1
    assert poller.diagnostics()["error_counts"]["abort"] == 1
    assert poller.diagnostics()["unavailable_items"] == 1

    # A new transport epoch forces one fresh read and records any repeat.
    backend.session.epoch = 8
    third = await poller._async_poll_data()
    assert isinstance(third[(1, address)], HomeAssistantError)
    assert parent.read_calls == 2
    assert poller.diagnostics()["error_counts"]["abort"] == 2


@pytest.mark.asyncio
async def test_base_poll_counts_typed_correlation_failure(monkeypatch) -> None:
    coordinator = OpenRBusCoordinator.__new__(OpenRBusCoordinator)
    coordinator._diagnostic_poll_count = 0
    coordinator._diagnostic_error_counts = dict.fromkeys(
        ("item", "batch", "decode", "correlation", "session"), 0
    )

    async def fail_read(*_args, **_kwargs):
        raise ThinGattCorrelationError("private response frame")

    coordinator.async_read_object = fail_read
    with pytest.raises(ThinGattCorrelationError):
        await coordinator._async_update_data()
    assert coordinator._diagnostic_poll_count == 1
    assert coordinator._diagnostic_error_counts["correlation"] == 1
