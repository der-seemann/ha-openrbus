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
    "error",
)
_BUCKETS = ("lt_100ms", "100_499ms", "500_1999ms", "2_9999ms", "gte_10s")


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
