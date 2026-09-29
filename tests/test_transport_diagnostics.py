"""Privacy and stability coverage for aggregate transport diagnostics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.errors import (
    CanOpenAbortError,
    RequestTimeoutError,
    TransportError,
    ValidationError,
)
from openrbus.protocol.canip import ObjectAddress
from openrbus.transport.thin_gatt import (
    ThinGattCorrelationError,
    ThinGattSessionStateError,
)

from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.coordinator import (
    OpenRBusCoordinator,
    OpenRBusPollingCoordinator,
)
from custom_components.openrbus.diagnostics import (
    _safe_poll_snapshot,
    _safe_recovery_fence,
    _safe_setup_response,
    async_get_config_entry_diagnostics,
)
from custom_components.openrbus.setup_observability import (
    RecoveryFenceMetrics,
    SetupResponseMetrics,
)
from custom_components.openrbus.transport import (
    _read_error_class,
    _read_error_subtype,
    _tag_read_error,
)
from custom_components.openrbus.validity import RegisterValidityTracker


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
    ],
)
def test_session_error_subtype_is_fixed_and_redacted(error, expected) -> None:
    assert _read_error_subtype(error, "session") == expected


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
            "item_failures": [
                {
                    "node": 4,
                    "index": 0x1234,
                    "subindex": 2,
                    "error_class": "item",
                    "error": "private abort text",
                }
            ]
        },
        include_item_failures=True,
    )
    assert opted_in == {
        "item_failures": [
            {"node": 4, "index": 0x1234, "subindex": 2, "error_class": "item"}
        ]
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
    }
    assert _safe_recovery_fence({**snapshot, "identity": "private"}) == snapshot

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
            entry_id="entry", options={"diagnostics_enabled": True}
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
