"""Bounded, privacy-preserving Thin-RPC setup response counters."""

from __future__ import annotations

from typing import Any

_MAX = 2_147_483_647
PHASES = ("pairing_arm", "handle_lookup", "poll_request")
OUTCOMES = (
    "call_not_sent",
    "call_completed",
    "response",
    "esp_response",
    "empty_response",
    "no_esp_response",
    "timeout_no_response",
    "cancelled_before_response",
    "cancelled_after_response",
    "recovery_succeeded",
    "recovery_failed",
    "error",
)
_BUCKETS = ("lt_100ms", "100_499ms", "500_1999ms", "2_9999ms", "gte_10s")
_FENCE_TIMEOUT_CLASSES = (
    "both_connected",
    "link_active",
    "parent_connected",
    "state_unavailable",
)
_DISCONNECT_OUTCOMES = (
    "missing_channel",
    "missing_session",
    "missing_identity",
    "invalid_epoch",
    "service_completed",
    "service_not_acknowledged",
    "service_exception",
)
_DISCONNECT_EXCEPTION_CLASSES = (
    "HomeAssistantError",
    "TimeoutError",
    "TransportError",
    "RuntimeError",
    "other",
)
READ_OPERATION_ORIGINS = frozenset(
    {
        "bridge_health",
        "zone_profile",
        "manual_read_service",
        "manual_read_service_batch",
        "poll_batch",
        "coordinator_single",
        "control_readback",
        "access_discovery",
        "startup_discovery",
        "unspecified",
    }
)


class RecoveryFenceMetrics:
    """Keep the latest bounded physical-disconnect recovery evidence."""

    def __init__(self) -> None:
        self._attempts = 0
        self._dispatch_acknowledged: bool | None = None
        self._link_active: bool | None = None
        self._parent_connected: bool | None = None
        self._timeout_count = 0
        self._timeout_classes = dict.fromkeys(_FENCE_TIMEOUT_CLASSES, 0)
        self._last_timeout_class: str | None = None
        self._disconnect_attempt: dict[str, Any] = {}

    def begin_attempt(
        self, session_epoch: object = None, origin: object = "unspecified"
    ) -> int:
        self._attempts = min(_MAX, self._attempts + 1)
        self._dispatch_acknowledged = None
        self._link_active = None
        self._parent_connected = None
        self._last_timeout_class = None
        self._disconnect_attempt = {
            "attempt_id": self._attempts,
            "session_epoch": (
                session_epoch
                if type(session_epoch) is int and 0 <= session_epoch <= _MAX
                else None
            ),
            "outcome": None,
            "exception_class": None,
            "origin": (
                origin
                if isinstance(origin, str) and origin in READ_OPERATION_ORIGINS
                else "unspecified"
            ),
        }
        return self._attempts

    def record_disconnect_outcome(
        self, outcome: str, exception: BaseException | None = None
    ) -> None:
        if outcome not in _DISCONNECT_OUTCOMES:
            return
        self._disconnect_attempt["outcome"] = outcome
        if outcome == "service_exception" and exception is not None:
            name = type(exception).__name__
            self._disconnect_attempt["exception_class"] = (
                name if name in _DISCONNECT_EXCEPTION_CLASSES[:-1] else "other"
            )

    def record_dispatch(self, acknowledged: bool) -> None:
        self._dispatch_acknowledged = acknowledged is True

    def record_state(self, link_active: object, parent_connected: object) -> None:
        self._link_active = link_active if type(link_active) is bool else None
        self._parent_connected = (
            parent_connected if type(parent_connected) is bool else None
        )

    def record_timeout(self) -> None:
        if (
            type(self._link_active) is not bool
            or type(self._parent_connected) is not bool
        ):
            outcome = "state_unavailable"
        elif self._link_active and self._parent_connected:
            outcome = "both_connected"
        elif self._link_active:
            outcome = "link_active"
        else:
            outcome = "parent_connected"
        self._timeout_count = min(_MAX, self._timeout_count + 1)
        self._timeout_classes[outcome] = min(_MAX, self._timeout_classes[outcome] + 1)
        self._last_timeout_class = outcome

    def diagnostics(self) -> dict[str, Any]:
        """Return only fixed counters, booleans, and fixed timeout classes."""
        result = {
            "attempts": self._attempts,
            "dispatch_acknowledged": self._dispatch_acknowledged,
            "link_active": self._link_active,
            "parent_connected": self._parent_connected,
            "timeout_count": self._timeout_count,
            "timeout_classes": dict(self._timeout_classes),
            "last_timeout_class": self._last_timeout_class,
        }
        if self._disconnect_attempt:
            result["disconnect_attempt"] = dict(self._disconnect_attempt)
        return result


class SetupResponseMetrics:
    """Keep only fixed phase/outcome counters and coarse elapsed buckets."""

    def __init__(self) -> None:
        self._counts: dict[str, dict[str, int]] = {
            phase: dict.fromkeys(OUTCOMES, 0) for phase in PHASES
        }
        self._elapsed: dict[str, dict[str, int]] = {
            phase: dict.fromkeys(_BUCKETS, 0) for phase in PHASES
        }
        self._handle_lookup_response_seen = False
        self._setup_cancellation_recorded = False
        self._setup_attempts = 0

    def begin_attempt(self) -> None:
        """Count one real backend startup attempt in this object's lifetime."""
        self._setup_attempts = min(_MAX, self._setup_attempts + 1)
        self._handle_lookup_response_seen = False
        self._setup_cancellation_recorded = False

    def mark_handle_lookup_response(self) -> None:
        self._handle_lookup_response_seen = True

    def record_setup_cancellation(self, elapsed_ms: float) -> None:
        """Classify a setup-task cancellation against observed RPC evidence."""
        if self._handle_lookup_response_seen and not self._setup_cancellation_recorded:
            self.record("handle_lookup", "cancelled_after_response", elapsed_ms)
            self._setup_cancellation_recorded = True

    @staticmethod
    def _bucket(elapsed_ms: float) -> str:
        if elapsed_ms < 100:
            return _BUCKETS[0]
        if elapsed_ms < 500:
            return _BUCKETS[1]
        if elapsed_ms < 2000:
            return _BUCKETS[2]
        if elapsed_ms < 10000:
            return _BUCKETS[3]
        return _BUCKETS[4]

    def record(self, phase: str, outcome: str, elapsed_ms: float) -> None:
        """Saturating record; ignore anything outside the fixed schema."""
        if phase not in self._counts or outcome not in OUTCOMES:
            return
        counts = self._counts[phase]
        counts[outcome] = min(_MAX, counts[outcome] + 1)
        bucket = self._bucket(max(0.0, elapsed_ms))
        bucket_counts = self._elapsed[phase]
        bucket_counts[bucket] = min(_MAX, bucket_counts[bucket] + 1)

    def diagnostics(self) -> dict[str, Any]:
        """Return a fresh JSON-safe projection containing no caller data."""
        snapshot: dict[str, Any] = {
            "scope": "backend_lifetime",
            "setup_attempts": self._setup_attempts,
        }
        snapshot.update(
            {
                phase: {
                    "outcomes": dict(self._counts[phase]),
                    "elapsed_ms_buckets": dict(self._elapsed[phase]),
                }
                for phase in PHASES
            }
        )
        return snapshot
