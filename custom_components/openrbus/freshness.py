"""Freshness rules for the ESPHome read-generation marker."""

from __future__ import annotations

import math


def parse_generation(value: object) -> int | None:
    """Parse a finite ESPHome generation value; NaN is not a marker."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number)


def is_new_generation(
    previous: int | None,
    candidate: int | None,
    *,
    reset_allowed: bool = False,
) -> bool:
    """Return whether *candidate* is a fresh response marker.

    The ESP marker is monotonic during one runtime.  A lower marker is only
    valid after an independently observed reconnect/reboot; this prevents a
    delayed stale event from completing a poll.
    """
    if candidate is None or previous is None:
        return candidate is not None
    if candidate > previous:
        return True
    return reset_allowed and candidate != previous
