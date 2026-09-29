"""Evidence-backed OpenRBus zone classification and presentation.

Zone objects in the BDR catalogue are CANopen arrays.  Their subindex is a
stable bus identity, while the function configured in CP020 is the only
reliable indicator of whether that slot is in use and what it controls.  Do
not guess a zone from a node number or from a static device family.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ZONE_FUNCTION_INDEX = 0x3404  # CP020, Zone Function
ZONE_FRIENDLY_NAME_INDEX = 0x340F


class ZoneKind(StrEnum):
    """User-facing grouping inferred from the manufacturer ZoneFunction enum."""

    GENERAL = "general"
    HEATING = "heating"
    DHW = "dhw"
    SOLAR = "solar"
    INACTIVE = "inactive"
    OTHER = "other"


_INACTIVE_FUNCTIONS = frozenset({0})
_DHW_FUNCTIONS = frozenset({6, 7, 10, 11, 12, 13, 31})
# Direct/mixing/high-temperature/fan-convector/BSB are heating circuits.  The
# remaining enabled ZoneFunction values are intentionally "other" rather
# than guessed as heating or DHW.
_HEATING_FUNCTIONS = frozenset({1, 2, 4, 5, 200})


@dataclass(frozen=True, slots=True)
class ZoneProfile:
    """One discovered zone slot.  ``function`` is None when it was not read."""

    node: int
    subindex: int
    function: int | None
    friendly_name: str | None = None

    @property
    def kind(self) -> ZoneKind:
        if self.function in _INACTIVE_FUNCTIONS:
            return ZoneKind.INACTIVE
        if self.function in _DHW_FUNCTIONS:
            return ZoneKind.DHW
        if self.function in _HEATING_FUNCTIONS:
            return ZoneKind.HEATING
        return ZoneKind.OTHER if self.function is not None else ZoneKind.INACTIVE

    @property
    def active(self) -> bool:
        return self.function is not None and self.kind is not ZoneKind.INACTIVE

    @property
    def label(self) -> str:
        """Return a display label; friendly names never affect stable IDs."""

        if self.friendly_name and self.friendly_name.strip():
            return self.friendly_name.strip()
        # CANopen array indexes are zero-based; installers call the first
        # slot "Zone 1".  Keep that convention only in display text.
        return f"Zone {self.subindex + 1}"


def zone_subindex(register: Any) -> int | None:
    """Return the zone slot represented by a known zone-array row.

    The catalogue carries unrelated arrays too, therefore names alone are not
    enough.  The known ZoneFunction array plus the manufacturer Zone/CP/CM
    naming is the conservative evidence boundary until Core exports the
    ZoneDiscovered event as a public runtime model.
    """

    index = getattr(getattr(register, "address", None), "index", None)
    subindex = getattr(getattr(register, "address", None), "subindex", None)
    if not isinstance(index, int) or not isinstance(subindex, int):
        return None
    # SCB/EHC zone configuration and monitoring objects use this zone-array
    # range.  Keep the range deliberately narrow; general objects must never
    # be relabelled as a guessed zone.
    if 0x3400 <= index <= 0x3477 or 0x5402 <= index <= 0x5444:
        semantic = " ".join(
            str(getattr(register, field, "") or "")
            for field in ("internal_code", "name_en", "name_de")
        ).casefold()
        if "zone" in semantic or str(getattr(register, "internal_code", "")).upper().startswith(("CP", "CM", "CC")):
            return subindex
    return None


def profile_for(parent: Any, node: int, subindex: int) -> ZoneProfile | None:
    """Read a coordinator's discovered profile without coupling to its type."""

    profiles = getattr(parent, "zone_profiles", {}) or {}
    return profiles.get((node, subindex))


def normalized_overrides(value: object) -> dict[str, bool]:
    """Parse persisted per-zone selections, ignoring malformed legacy data."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): bool(enabled) for key, enabled in value.items()}


def override_key(node: int, subindex: int) -> str:
    return f"{node}:{subindex}"


def zone_enabled(parent: Any, node: int, subindex: int) -> bool:
    """Apply explicit user choice before the discovered active/inactive default."""

    overrides = normalized_overrides(getattr(parent, "zone_overrides", {}))
    explicit = overrides.get(override_key(node, subindex))
    if explicit is not None:
        return explicit
    profile = profile_for(parent, node, subindex)
    return bool(profile and profile.active)


def entity_zone_label(parent: Any, identity: Any, register: Any) -> str | None:
    """Return an active zone's label for entity display, never for its ID."""

    subindex = zone_subindex(register)
    if subindex is None:
        return None
    profile = profile_for(parent, getattr(identity, "node", -1), subindex)
    return profile.label if profile and zone_enabled(parent, profile.node, subindex) else None


def zone_device_name(profile: ZoneProfile) -> str:
    """Describe a non-empty logical zone device in Home Assistant."""

    kind = {
        ZoneKind.HEATING: "Heating",
        ZoneKind.DHW: "DHW",
        ZoneKind.SOLAR: "Solar",
        ZoneKind.OTHER: "Other",
    }.get(profile.kind, "Zone")
    return f"{profile.label} ({kind})"


__all__ = [
    "ZONE_FRIENDLY_NAME_INDEX",
    "ZONE_FUNCTION_INDEX",
    "ZoneKind",
    "ZoneProfile",
    "entity_zone_label",
    "normalized_overrides",
    "override_key",
    "profile_for",
    "zone_device_name",
    "zone_enabled",
    "zone_subindex",
]
