"""Freshness rules for the ESPHome read-generation marker."""

from __future__ import annotations


def is_new_generation(previous: int | None, candidate: int | None) -> bool:
    """Accept only a strictly newer marker; raw value equality is irrelevant."""
    return candidate is not None and (previous is None or candidate > previous)
