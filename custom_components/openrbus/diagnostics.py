"""Secret-free diagnostics for the OpenRBus config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BACKEND, CONF_DIAGNOSTICS_ENABLED, DOMAIN
from .setup_observability import OUTCOMES as _SETUP_OUTCOMES
from .setup_observability import PHASES as _SETUP_PHASES

_POLL_GROUPS = ("fast", "standard", "slow")
_ERROR_CLASSES = ("abort", "item", "batch", "decode", "correlation", "session")
_ERROR_SUBTYPES = (
    "session.not_ready",
    "session.link_lost",
    "session.timeout",
    "session.not_secure",
    "session.transport",
    "batch.malformed",
    "batch.abort",
    "batch.fallback",
)
_SUBTYPES_BY_CLASS = {
    "session": frozenset(
        name.split(".", 1)[1] for name in _ERROR_SUBTYPES if name.startswith("session.")
    ),
    "batch": frozenset(
        name.split(".", 1)[1] for name in _ERROR_SUBTYPES if name.startswith("batch.")
    ),
}
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
_BATCH_EXCEPTION_TYPES = frozenset(
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
_BATCH_EVENTS = ("malformed", "abort", "fallback")
_FENCE_TIMEOUT_CLASSES = (
    "both_connected",
    "link_active",
    "parent_connected",
    "state_unavailable",
)
_COUNTER_MAX = 2_147_483_647


def _safe_poll_snapshot(
    snapshot: object, *, include_item_failures: bool = False
) -> dict[str, Any]:
    """Project only bounded counters from a poller diagnostic snapshot."""
    if not isinstance(snapshot, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in (
        "poll_count",
        "success_items",
        "failed_items",
        "available_items",
        "unavailable_items",
    ):
        value = snapshot.get(key)
        if type(value) is int and 0 <= value <= _COUNTER_MAX:
            safe[key] = value
    quarantined_count = snapshot.get("quarantined_count")
    if type(quarantined_count) is int and 0 <= quarantined_count <= _COUNTER_MAX:
        safe["quarantined_count"] = quarantined_count
    delta = snapshot.get("availability_delta")
    if type(delta) is int and -_COUNTER_MAX <= delta <= _COUNTER_MAX:
        safe["availability_delta"] = delta
    in_progress = snapshot.get("poll_in_progress")
    if type(in_progress) is bool:
        safe["poll_in_progress"] = in_progress
    errors = snapshot.get("error_counts")
    if isinstance(errors, dict):
        safe["error_counts"] = {
            name: errors.get(name, 0)
            for name in _ERROR_CLASSES
            if type(errors.get(name, 0)) is int
            and 0 <= errors.get(name, 0) <= _COUNTER_MAX
        }
    subtype_counts = snapshot.get("error_subtype_counts")
    if isinstance(subtype_counts, dict):
        safe["error_subtype_counts"] = {
            name: subtype_counts.get(name, 0)
            for name in _ERROR_SUBTYPES
            if type(subtype_counts.get(name, 0)) is int
            and 0 <= subtype_counts.get(name, 0) <= _COUNTER_MAX
        }
    # Address-level attribution is available only when the entry's existing
    # expert diagnostics option is enabled. Project a strict protocol-address
    # schema; never copy arbitrary adapter fields or exception text.
    failures = snapshot.get("item_failures")
    if include_item_failures and isinstance(failures, (tuple, list)):
        safe["item_failures"] = [
            {
                "node": item["node"],
                "index": item["index"],
                "subindex": item["subindex"],
                "error_class": item["error_class"],
                **(
                    {"error_subtype": item["error_subtype"]}
                    if item.get("error_subtype")
                    in _SUBTYPES_BY_CLASS.get(item["error_class"], ())
                    else {}
                ),
                **(
                    {"abort_category": item["abort_category"]}
                    if item.get("abort_category") in _ABORT_CATEGORIES
                    else {}
                ),
                **(
                    {"decode_subtype": item["decode_subtype"]}
                    if item.get("decode_subtype") in _DECODE_DETAILS
                    else {}
                ),
                **(
                    {"batch_exception_type": item["batch_exception_type"]}
                    if item.get("batch_exception_type") in _BATCH_EXCEPTION_TYPES
                    else {}
                ),
            }
            for item in failures[-16:]
            if isinstance(item, dict)
            and type(item.get("node")) is int
            and 0 <= item["node"] <= 255
            and type(item.get("index")) is int
            and 0 <= item["index"] <= 65535
            and type(item.get("subindex")) is int
            and 0 <= item["subindex"] <= 255
            and item.get("error_class") in _ERROR_CLASSES
        ]
        quarantined = snapshot.get("quarantined_items")
        if isinstance(quarantined, (tuple, list)):
            safe["quarantined_items"] = [
                {
                    "node": item["node"],
                    "index": item["index"],
                    "subindex": item["subindex"],
                    "error_class": item["error_class"],
                    "subtype": item["subtype"],
                }
                for item in quarantined[-16:]
                if isinstance(item, dict)
                and type(item.get("node")) is int
                and 0 <= item["node"] <= 255
                and type(item.get("index")) is int
                and 0 <= item["index"] <= 65535
                and type(item.get("subindex")) is int
                and 0 <= item["subindex"] <= 255
                and (
                    (item.get("error_class") == "abort"
                     and item.get("subtype") in {"unsupported_access", "read_not_supported"})
                    or (item.get("error_class") == "decode"
                        and item.get("subtype") == "visible_string_non_ascii")
                )
            ]
    return safe


def _safe_setup_response(snapshot: object) -> dict[str, Any]:
    """Project setup response telemetry through its fixed aggregate schema."""
    if not isinstance(snapshot, dict):
        return {}
    safe: dict[str, Any] = {}
    if snapshot.get("scope") == "backend_lifetime":
        safe["scope"] = "backend_lifetime"
    attempts = snapshot.get("setup_attempts")
    if type(attempts) is int and 0 <= attempts <= _COUNTER_MAX:
        safe["setup_attempts"] = attempts
    for phase in _SETUP_PHASES:
        item = snapshot.get(phase)
        if not isinstance(item, dict):
            continue
        outcomes = item.get("outcomes")
        buckets = item.get("elapsed_ms_buckets")
        if not isinstance(outcomes, dict) or not isinstance(buckets, dict):
            continue
        safe[phase] = {
            "outcomes": {
                outcome: min(_COUNTER_MAX, max(0, outcomes.get(outcome, 0)))
                for outcome in _SETUP_OUTCOMES
                if type(outcomes.get(outcome)) is int
            },
            "elapsed_ms_buckets": {
                bucket: min(_COUNTER_MAX, max(0, buckets.get(bucket, 0)))
                for bucket in (
                    "lt_100ms",
                    "100_499ms",
                    "500_1999ms",
                    "2_9999ms",
                    "gte_10s",
                )
                if type(buckets.get(bucket)) is int
            },
        }
    return safe


def _safe_recovery_fence(snapshot: object) -> dict[str, Any]:
    """Project physical-disconnect recovery through its fixed schema."""
    if not isinstance(snapshot, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in ("attempts", "timeout_count"):
        value = snapshot.get(key)
        if type(value) is int and 0 <= value <= _COUNTER_MAX:
            safe[key] = value
    for key in ("dispatch_acknowledged", "link_active", "parent_connected"):
        value = snapshot.get(key)
        if type(value) is bool or value is None:
            safe[key] = value
    classes = snapshot.get("timeout_classes")
    if isinstance(classes, dict):
        safe["timeout_classes"] = {
            name: min(_COUNTER_MAX, max(0, classes[name]))
            for name in _FENCE_TIMEOUT_CLASSES
            if type(classes.get(name)) is int
        }
    last_class = snapshot.get("last_timeout_class")
    if last_class is None or (
        isinstance(last_class, str) and last_class in _FENCE_TIMEOUT_CLASSES
    ):
        safe["last_timeout_class"] = last_class
    attempt = snapshot.get("disconnect_attempt")
    if isinstance(attempt, dict):
        safe_attempt: dict[str, Any] = {}
        for key in ("attempt_id", "session_epoch"):
            value = attempt.get(key)
            if value is None or (type(value) is int and 0 <= value <= _COUNTER_MAX):
                safe_attempt[key] = value
        outcome = attempt.get("outcome")
        if outcome is None or (
            isinstance(outcome, str)
            and outcome in {
            "missing_channel",
            "missing_session",
            "missing_identity",
            "invalid_epoch",
            "service_completed",
            "service_not_acknowledged",
            "service_exception",
            }
        ):
            safe_attempt["outcome"] = outcome
        exception_class = attempt.get("exception_class")
        if exception_class is None or (
            isinstance(exception_class, str)
            and exception_class
            in {
                "HomeAssistantError",
                "TimeoutError",
                "TransportError",
                "RuntimeError",
                "other",
            }
        ):
            safe_attempt["exception_class"] = exception_class
        if safe_attempt:
            safe["disconnect_attempt"] = safe_attempt
    return safe


def _safe_batch_events(snapshot: object) -> dict[str, int]:
    """Project only bounded batch-recovery event counters."""
    if not isinstance(snapshot, dict):
        return {}
    return {
        name: value
        for name in _BATCH_EVENTS
        if type(value := snapshot.get(name)) is int and 0 <= value <= _COUNTER_MAX
    }


def _safe_thin_rpc_frame_trace(snapshot: object) -> list[dict[str, Any]]:
    """Project a small request/response trace without frame payloads."""
    if not isinstance(snapshot, (tuple, list)):
        return []
    operations = {
        "CAPABILITY", "CANCEL", "CONNECT", "CONNECTION_STATE", "DISCONNECT",
        "DISCONNECTED", "DISCOVER", "ENCRYPTION_STATE", "FLOW_CONTROL",
        "HANDLE_LOOKUP", "NOTIFICATION", "PAIR_ENCRYPT", "SUBSCRIBE", "WRITE",
        "WRITE_CHAR", "WRITE_DESCRIPTOR", "READ_CHAR", "READ_DESCRIPTOR",
        "SCAN", "SCAN_RESULT", "SCAN_DONE",
    }
    kinds = {"request", "response", "event"}
    statuses = {
        "OK", "ACCEPTED", "ERROR", "WRITE_FAILED", "CANCELLED", "success",
        "failed", "timeout", "terminal_success", "terminal_error", "late_callback",
    }
    states = {"connected", "disconnected", "encrypted", "failed"}
    safe: list[dict[str, Any]] = []
    for item in snapshot[-64:]:
        if not isinstance(item, dict):
            continue
        record: dict[str, Any] = {
            "direction": item["direction"]
            if item.get("direction") in {"request", "response"}
            else "other",
            "kind": item["kind"] if item.get("kind") in kinds else "other",
            "op": item["op"] if item.get("op") in operations else "other",
            "request_id_present": item.get("request_id_present") is True,
        }
        for key in ("seq", "epoch", "request_id"):
            value = item.get(key)
            if type(value) is int and 0 <= value <= 4_294_967_295:
                record[key] = value
        value = item.get("read_operation_id")
        if type(value) is int and 1 <= value <= 4_294_967_295:
            record["read_operation_id"] = value
        if item.get("status") in statuses:
            record["status"] = item["status"]
        if item.get("state_category") in states:
            record["state_category"] = item["state_category"]
        if item.get("context") == "discovery":
            record["context"] = "discovery"
        safe.append(record)
    return safe


def _safe_read_operation_trace(snapshot: object) -> list[dict[str, Any]]:
    """Project address-free bus operation IDs and terminal outcomes."""
    if not isinstance(snapshot, (tuple, list)):
        return []
    safe = []
    for item in snapshot[-32:]:
        if not isinstance(item, dict):
            continue
        operation_id = item.get("operation_id")
        if type(operation_id) is not int or not 1 <= operation_id <= 4_294_967_295:
            continue
        record: dict[str, Any] = {"operation_id": operation_id}
        if item.get("kind") in {"single", "batch"}:
            record["kind"] = item["kind"]
        if item.get("outcome") in {"success", "partial_error", "error"}:
            record["outcome"] = item["outcome"]
        epoch = item.get("epoch")
        if type(epoch) is int and 0 <= epoch <= 4_294_967_295:
            record["epoch"] = epoch
        safe.append(record)
    return safe


def _safe_read_transport_capture(snapshot: object) -> dict[str, Any]:
    """Project pre/post proxy counters for the single captured read."""
    if not isinstance(snapshot, dict):
        return {}
    operation_id = snapshot.get("operation_id")
    if type(operation_id) is not int or not 1 <= operation_id <= 4_294_967_295:
        return {}
    safe: dict[str, Any] = {"operation_id": operation_id}
    if snapshot.get("outcome") in {"success", "error"}:
        safe["outcome"] = snapshot["outcome"]
    counters = (
        "epoch", "epoch_count", "rpc_requests", "last_rpc_request_id",
        "att_write_char_calls", "att_write_char_callbacks",
        "last_att_write_request_id", "notification_callbacks",
        "last_notification_epoch", "last_notification_seq",
        "last_notification_request_id",
        "event_queue_depth", "poll_frame_calls", "poll_nonempty_returns",
        "poll_empty_returns", "poll_queue_depth_before",
        "poll_queue_depth_after", "total_frames_enqueued",
    )
    statuses = {"none", "WRITE_CHAR", "success", "failed", "waiting_notification"}
    safe_enums = {
        "last_enqueued_kind": {"none", "event", "response"},
        "last_enqueued_op": {
            "none",
            "NOTIFICATION",
            "CONNECTED",
            "DISCONNECTED",
            "CONNECT",
            "DISCONNECT",
            "WRITE_CHAR",
            "READ_CHAR",
            "PAIR_ENCRYPT",
            "CAPABILITY",
            "ERROR",
            "INVALID_REQUEST",
        },
    }
    booleans = {
        "host_ready", "parent_connected", "link_active",
        "last_att_write_waiting_notification", "last_notification_nonempty",
        "last_notification_matched_request",
    }
    for phase in ("before", "after"):
        values = snapshot.get(phase)
        if not isinstance(values, dict):
            continue
        projected: dict[str, Any] = {}
        for key in counters:
            value = values.get(key)
            if type(value) is int and 0 <= value <= 4_294_967_295:
                projected[key] = value
        for key in (
            "last_att_write_status", "last_rpc_request_op",
            "last_enqueued_kind", "last_enqueued_op",
        ):
            value = values.get(key)
            allowed = (
                statuses
                if key in {"last_att_write_status", "last_rpc_request_op"}
                else safe_enums.get(key, set())
            )
            if isinstance(value, str) and value in allowed:
                projected[key] = value
        for key in booleans:
            value = values.get(key)
            if type(value) is bool:
                projected[key] = value
        safe[phase] = projected
    return safe


def _safe_proxy_read_counters(snapshot: object) -> dict[str, Any]:
    """Project only bounded request, callback, and liveness counters."""
    if not isinstance(snapshot, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in (
        "epoch", "epoch_count", "rpc_requests", "last_rpc_request_id",
        "att_write_char_calls", "att_write_char_callbacks",
        "last_att_write_request_id", "notification_callbacks",
        "last_notification_epoch", "last_notification_seq",
        "last_notification_request_id",
        "event_queue_depth", "poll_frame_calls", "poll_nonempty_returns",
        "poll_empty_returns", "poll_queue_depth_before",
        "poll_queue_depth_after", "total_frames_enqueued",
    ):
        value = snapshot.get(key)
        if type(value) is int and 0 <= value <= 4_294_967_295:
            safe[key] = value
    for key in ("last_att_write_status", "last_rpc_request_op"):
        value = snapshot.get(key)
        if isinstance(value, str) and value in {
            "none", "WRITE_CHAR", "success", "failed", "waiting_notification"
        }:
            safe[key] = value
    last_enqueued_kind = snapshot.get("last_enqueued_kind")
    if last_enqueued_kind in {"none", "event", "response"}:
        safe["last_enqueued_kind"] = last_enqueued_kind
    last_enqueued_op = snapshot.get("last_enqueued_op")
    if last_enqueued_op in {
        "none", "NOTIFICATION", "CONNECTED", "DISCONNECTED", "CONNECT",
        "DISCONNECT", "WRITE_CHAR", "READ_CHAR", "PAIR_ENCRYPT",
        "CAPABILITY", "ERROR", "INVALID_REQUEST",
    }:
        safe["last_enqueued_op"] = last_enqueued_op
    for key in (
        "host_ready", "parent_connected", "link_active",
        "last_att_write_waiting_notification", "last_notification_nonempty",
        "last_notification_matched_request",
    ):
        value = snapshot.get(key)
        if type(value) is bool:
            safe[key] = value
    return safe


def _safe_batch_failure_trace(snapshot: object) -> dict[str, Any]:
    """Project the one-shot batch failure trace through a fixed schema."""
    if not isinstance(snapshot, dict):
        return {}
    operation_id = snapshot.get("operation_id")
    if type(operation_id) is not int or not 1 <= operation_id <= 4_294_967_295:
        return {}
    stages = {
        "get_list_call", "response_parse", "single_fallback", "recovery_dispatch"
    }
    exception_types = _BATCH_EXCEPTION_TYPES
    stage = snapshot.get("stage")
    exception_class = snapshot.get("exception_class")
    if stage not in stages or exception_class not in exception_types:
        return {}
    safe: dict[str, Any] = {
        "operation_id": operation_id,
        "stage": stage,
        "exception_class": exception_class,
    }
    for key in ("request_id", "epoch"):
        value = snapshot.get(key)
        if type(value) is int and 0 <= value <= 4_294_967_295:
            safe[key] = value
    for phase in ("before", "after"):
        safe[phase] = _safe_proxy_read_counters(snapshot.get(phase))
    return safe


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return aggregate metadata without credentials or installation data.

    Diagnostics are commonly attached to issue reports.  Do not export node
    names, object identities, addresses, serials, service names, or exception
    messages: those values can identify a private heating installation or
    reveal local infrastructure.  Users can still report useful lifecycle
    and availability information from the aggregate fields below.
    """

    coordinator = entry.runtime_data
    devices = tuple(
        getattr(coordinator, "devices", ())
        or getattr(coordinator, "discovered_devices", ())
        or ()
    )
    discovery_error = getattr(coordinator, "discovery_error", None)
    backend = getattr(coordinator, "_backend", None)
    backend_diagnostics = getattr(backend, "diagnostics", None)
    try:
        backend_snapshot = (
            backend_diagnostics() if callable(backend_diagnostics) else {}
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break issue export
        backend_snapshot = {}
    # Project only explicit numeric lifecycle facts. Never serialize an
    # adapter-provided mapping wholesale: it may grow private fields later.
    transport_session = (
        {
            key: value
            for key in ("session_generation", "session_epoch")
            if type(value := backend_snapshot.get(key)) is int
            and 0 <= value <= 2_147_483_647
        }
        if isinstance(backend_snapshot, dict)
        else {}
    )
    if isinstance(backend_snapshot, dict):
        recovery_fence = _safe_recovery_fence(backend_snapshot.get("recovery_fence"))
        if recovery_fence:
            transport_session["recovery_fence"] = recovery_fence
    setup_response = _safe_setup_response(
        backend_snapshot.get("setup_response")
        if isinstance(backend_snapshot, dict)
        else None
    )
    batch_events = _safe_batch_events(
        backend_snapshot.get("batch_events")
        if isinstance(backend_snapshot, dict)
        else None
    )
    thin_rpc_frame_trace = _safe_thin_rpc_frame_trace(
        backend_snapshot.get("thin_rpc_frame_trace")
        if isinstance(backend_snapshot, dict)
        else None
    )
    read_operation_trace = _safe_read_operation_trace(
        backend_snapshot.get("read_operation_trace")
        if isinstance(backend_snapshot, dict)
        else None
    )
    read_transport_capture = _safe_read_transport_capture(
        backend_snapshot.get("last_read_transport_capture")
        if isinstance(backend_snapshot, dict)
        else None
    )
    batch_failure_trace = _safe_batch_failure_trace(
        backend_snapshot.get("last_batch_failure_trace")
        if isinstance(backend_snapshot, dict)
        else None
    )
    polling = getattr(coordinator, "_openrbus_polling_coordinators", {})
    hass_data = getattr(hass, "data", {})
    polling_store = (
        hass_data.get(f"{DOMAIN}_polling_coordinators", {})
        if isinstance(hass_data, dict)
        else {}
    )
    entry_pollers = (
        polling_store.get(getattr(entry, "entry_id", None), {})
        if isinstance(polling_store, dict)
        else {}
    )
    if not isinstance(polling, dict) or not polling:
        polling = entry_pollers
    # Some HA platform setup paths hand diagnostics a reconstituted entry
    # object with a runtime_data instance that differs from the one owning the
    # pollers. Resolve the sole active entry only when both the integration
    # and our registry unambiguously contain one entry.
    if not polling and isinstance(polling_store, dict) and len(polling_store) == 1:
        try:
            active_entries = hass.config_entries.async_entries(DOMAIN)
        except (AttributeError, TypeError):
            active_entries = ()
        if len(active_entries) == 1:
            polling = next(iter(polling_store.values()))
    poller_registry = {
        "stored_entry_count": min(_COUNTER_MAX, len(polling_store))
        if isinstance(polling_store, dict)
        else 0,
        "entry_match": bool(entry_pollers),
        "selected_groups": [group for group in _POLL_GROUPS if group in polling]
        if isinstance(polling, dict)
        else [],
    }
    poll_diagnostics = {}
    if isinstance(polling, dict):
        for group in _POLL_GROUPS:
            poller = polling.get(group)
            snapshot = getattr(poller, "diagnostics", None)
            if callable(snapshot):
                try:
                    poll_diagnostics[group] = _safe_poll_snapshot(
                        snapshot(),
                        include_item_failures=bool(
                            getattr(entry, "options", {}).get(
                                CONF_DIAGNOSTICS_ENABLED, False
                            )
                        ),
                    )
                except Exception:  # noqa: BLE001, S112 - best-effort, log-free export
                    continue
    poll_in_progress = any(
        snapshot.get("poll_in_progress") is True
        for snapshot in poll_diagnostics.values()
    )
    return {
        "gateway": {
            "backend": entry.data.get(CONF_BACKEND),
        },
        "coordinator": {
            "cycle_id": getattr(coordinator, "_cycle_id", 0),
            "poll_in_progress": poll_in_progress,
            "last_update_success": coordinator.last_update_success,
            "discovery_attempted": getattr(coordinator, "_discovery_attempted", False),
            "discovery_error_type": (
                type(discovery_error).__name__ if discovery_error is not None else None
            ),
            "device_count": len(devices),
            "inventory_count": len(getattr(coordinator, "inventories", ()) or ()),
            "transport_session": transport_session,
            "setup_response": setup_response,
            "batch_events": batch_events,
            "thin_rpc_frame_trace": thin_rpc_frame_trace,
            "read_operation_trace": read_operation_trace,
            "read_transport_capture": read_transport_capture,
            "batch_failure_trace": batch_failure_trace,
            "poll_groups": poll_diagnostics,
            "poller_registry": poller_registry,
            "coordinator_poll_count": min(
                _COUNTER_MAX,
                max(0, getattr(coordinator, "_diagnostic_poll_count", 0))
                if type(getattr(coordinator, "_diagnostic_poll_count", 0)) is int
                else 0,
            ),
            "coordinator_error_counts": _safe_poll_snapshot(
                {"error_counts": getattr(coordinator, "_diagnostic_error_counts", {})}
            ).get("error_counts", dict.fromkeys(_ERROR_CLASSES, 0)),
        },
    }
