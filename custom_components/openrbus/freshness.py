"""Freshness rules for the ESPHome read-generation marker."""

from __future__ import annotations


def is_new_generation(previous: int | None, candidate: int | None) -> bool:
    """Accept a new marker, including a reset after an ESP reboot."""
    return candidate is not None and (previous is None or candidate != previous)
