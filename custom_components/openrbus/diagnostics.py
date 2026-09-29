"""Secret-free diagnostics for the OpenRBus config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BACKEND, CONF_DIAGNOSTICS_ENABLED
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
    "session": frozenset(name.split(".", 1)[1] for name in _ERROR_SUBTYPES if name.startswith("session.")),
    "batch": frozenset(name.split(".", 1)[1] for name in _ERROR_SUBTYPES if name.startswith("batch.")),
}
_BATCH_EVENTS = ("malformed", "abort", "fallback")
_COUNTER_MAX = 2_147_483_647


def _safe_poll_snapshot(
    snapshot: object, *, include_item_failures: bool = False
) -> dict[str, Any]:
    """Project only bounded counters from a poller diagnostic snapshot."""
    if not isinstance(snapshot, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in ("poll_count", "success_items", "failed_items", "available_items", "unavailable_items"):
        value = snapshot.get(key)
        if type(value) is int and 0 <= value <= _COUNTER_MAX:
            safe[key] = value
    delta = snapshot.get("availability_delta")
    if type(delta) is int and -_COUNTER_MAX <= delta <= _COUNTER_MAX:
        safe["availability_delta"] = delta
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
                for bucket in ("lt_100ms", "100_499ms", "500_1999ms", "2_9999ms", "gte_10s")
                if type(buckets.get(bucket)) is int
            },
        }
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
        backend_snapshot = backend_diagnostics() if callable(backend_diagnostics) else {}
    except Exception:  # noqa: BLE001 - diagnostics must never break issue export
        backend_snapshot = {}
    # Project only explicit numeric lifecycle facts. Never serialize an
    # adapter-provided mapping wholesale: it may grow private fields later.
    transport_session = {
        key: value
        for key in ("session_generation", "session_epoch")
        if type(value := backend_snapshot.get(key)) is int
        and 0 <= value <= 2_147_483_647
    } if isinstance(backend_snapshot, dict) else {}
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
    polling = getattr(coordinator, "_openrbus_polling_coordinators", {})
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
    return {
        "gateway": {
            "backend": entry.data.get(CONF_BACKEND),
        },
        "coordinator": {
            "cycle_id": getattr(coordinator, "_cycle_id", 0),
            "poll_lock_locked": coordinator._poll_lock.locked(),
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
            "poll_groups": poll_diagnostics,
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
