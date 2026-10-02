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

from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import Registry

ZONE_FUNCTION_INDEX = 0x3404  # CP020, Zone Function
ZONE_FRIENDLY_NAME_INDEX = 0x340F
_REGISTRY = Registry.load_default()
_ZONE_FUNCTION_DEFINITION = _REGISTRY.find(ObjectAddress(ZONE_FUNCTION_INDEX, 0))
_ZONE_FUNCTION_ENUM = next(
    (
        item
        for item in _REGISTRY.enums
        if _ZONE_FUNCTION_DEFINITION is not None
        and item.name == _ZONE_FUNCTION_DEFINITION.wire.enum_name
    ),
    None,
)


class ZoneKind(StrEnum):
    """User-facing grouping inferred from the manufacturer ZoneFunction enum."""

    GENERAL = "general"
    HEATING = "heating"
    DHW = "dhw"
    SOLAR = "solar"
    INACTIVE = "inactive"
    OTHER = "other"
    UNKNOWN = "unknown"


_INACTIVE_FUNCTIONS = frozenset({0})
_DHW_FUNCTIONS = frozenset({6, 7, 10, 11, 12, 13, 31})
# Direct/mixing/high-temperature/fan-convector/BSB are heating circuits.  The
# Values are from Core's localized manufacturer ZoneFunction enum.  Remaining
# known enabled values are neither heating nor DHW; they stay separate.
_HEATING_FUNCTIONS = frozenset({1, 2, 4, 5, 200})


@dataclass(frozen=True, slots=True)
class ZoneProfile:
    """One discovered CP020 item; ``function`` is None when unread."""

    node: int
    subindex: int
    function: int | None
    friendly_name: str | None = None
    node_name: str | None = None

    @property
    def kind(self) -> ZoneKind:
        if self.function is None:
            return ZoneKind.UNKNOWN
        if self.function in _INACTIVE_FUNCTIONS:
            return ZoneKind.INACTIVE
        if self.function in _DHW_FUNCTIONS:
            return ZoneKind.DHW
        if self.function in _HEATING_FUNCTIONS:
            return ZoneKind.HEATING
        if zone_function_label(self.function, "en") is None:
            return ZoneKind.UNKNOWN
        return ZoneKind.OTHER

    @property
    def active(self) -> bool:
        # A numeric value absent from the manufacturer enum is not evidence
        # that this slot is a configured function. Keep it visible as unknown
        # in the profile UI, but inactive for polling/entity projection.
        return self.function is not None and self.kind not in {
            ZoneKind.INACTIVE,
            ZoneKind.UNKNOWN,
        }

    @property
    def label(self) -> str:
        """Return a display label; friendly names never affect stable IDs."""

        if self.friendly_name and self.friendly_name.strip():
            return self.friendly_name.strip()
        # CP020 item :01 is parameter CP020 / Zone 1; :02 is CP021 / Zone 2.
        return f"Zone {max(1, self.subindex)}"


def zone_function_label(function: int | None, language: str = "de") -> str | None:
    """Return the canonical Core/manufacturer label for a CP020 enum value."""

    if function is None or _ZONE_FUNCTION_ENUM is None:
        return None
    try:
        label = _ZONE_FUNCTION_ENUM.label(function, language)
    except (TypeError, ValueError):
        return None
    return str(label) if label else None


def _function_description(profile: ZoneProfile, language: str) -> str:
    if profile.kind is ZoneKind.INACTIVE:
        manufacturer_label = zone_function_label(profile.function, language)
        status = "Disabled" if language == "en" else "deaktiviert"
        return f"{status} ({manufacturer_label})" if manufacturer_label else status
    manufacturer_label = zone_function_label(profile.function, language)
    if profile.kind is ZoneKind.UNKNOWN:
        value = "not read" if profile.function is None else str(profile.function)
        return (
            f"Unknown function ({value})"
            if language == "en"
            else f"Unbekannte Funktion ({value})"
        )
    if profile.kind is ZoneKind.DHW:
        category = "Domestic hot water" if language == "en" else "Trinkwarmwasser"
        return f"{category} ({manufacturer_label})" if manufacturer_label else category
    if profile.kind is ZoneKind.HEATING:
        category = "Heating circuit" if language == "en" else "Heizkreis"
        return f"{category} ({manufacturer_label})" if manufacturer_label else category
    return manufacturer_label or (
        "Other function" if language == "en" else "Andere Funktion"
    )


def zone_display_name(profile: ZoneProfile, language: str = "de") -> str:
    """Build a language-consistent, node-scoped functional zone label."""

    node_name = (profile.node_name or "").strip()
    if node_name:
        node_label = f"{node_name} (Node {profile.node})"
    elif language == "en":
        node_label = f"OpenRBus node {profile.node}"
    else:
        node_label = f"OpenRBus-Knoten {profile.node}"
    name = (
        f"{node_label} — Zone {max(1, profile.subindex)} — "
        f"{_function_description(profile, language)}"
    )
    friendly = (profile.friendly_name or "").strip()
    if friendly:
        name = f"{name} — {friendly}"
    return name


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
        if "zone" in semantic or str(
            getattr(register, "internal_code", "")
        ).upper().startswith(("CP", "CM", "CC")):
            return subindex
    return None


def zone_function_slots(rows: Any) -> tuple[int, ...]:
    """Return CP020 slots allowed by the canonical array definition.

    Family-specific RXDX profiles may only expose the first few configured
    selector rows even though the manufacturer catalog defines a longer
    CP020 array.  Once a node catalog contains that object, use its canonical
    ``max_items`` bound and let read-only discovery establish which slots are
    implemented.  Missing slots are handled as individual read failures by
    the coordinator; no family name or installation profile is assumed here.
    """

    if _ZONE_FUNCTION_DEFINITION is None:
        return ()
    if not any(
        getattr(getattr(item, "address", None), "index", None) == ZONE_FUNCTION_INDEX
        for item in rows
    ):
        return ()
    maximum = _ZONE_FUNCTION_DEFINITION.wire.max_items
    if not isinstance(maximum, int) or maximum <= 0:
        return ()
    return tuple(range(1, maximum + 1))


def profile_for(parent: Any, node: int, subindex: int) -> ZoneProfile | None:
    """Read a coordinator's discovered profile without coupling to its type."""

    profiles = getattr(parent, "zone_profiles", {}) or {}
    return profiles.get((node, subindex))


def normalized_overrides(value: object) -> dict[str, bool]:
    """Parse persisted per-zone selections, ignoring malformed legacy data."""

    if not isinstance(value, Mapping):
        return {}
    return {str(key): bool(enabled) for key, enabled in value.items()}


def normalized_selection_overrides(value: object) -> dict[str, bool | None]:
    """Normalize hierarchical visibility choices, preserving default resets."""

    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): None if enabled is None or enabled == "default" else bool(enabled)
        for key, enabled in value.items()
    }


def override_key(node: int, subindex: int) -> str:
    return f"{node}:{subindex}"


def zone_enabled(parent: Any, node: int, subindex: int) -> bool:
    """Apply selection only to a positively read, device-active CP020 slot."""

    profile = profile_for(parent, node, subindex)
    # A stale override cannot create a zone that the device says is disabled
    # (or one whose function could not be read).
    if profile is None or not profile.active:
        return False
    overrides = normalized_overrides(getattr(parent, "zone_overrides", {}))
    explicit = overrides.get(override_key(node, subindex))
    if explicit is not None:
        return explicit
    return True


def entity_zone_label(parent: Any, identity: Any, register: Any) -> str | None:
    """Return an active zone's label for entity display, never for its ID."""

    subindex = zone_subindex(register)
    if subindex is None:
        return None
    profile = profile_for(parent, getattr(identity, "node", -1), subindex)
    language = getattr(parent, "language", "de")
    return (
        zone_display_name(profile, language)
        if profile and zone_enabled(parent, profile.node, subindex)
        else None
    )


def zone_device_name(profile: ZoneProfile, language: str = "de") -> str:
    """Describe a non-empty logical zone device in Home Assistant."""

    return zone_display_name(profile, language)


__all__ = [
    "ZONE_FRIENDLY_NAME_INDEX",
    "ZONE_FUNCTION_INDEX",
    "ZoneKind",
    "ZoneProfile",
    "entity_zone_label",
    "normalized_overrides",
    "normalized_selection_overrides",
    "override_key",
    "profile_for",
    "zone_device_name",
    "zone_display_name",
    "zone_enabled",
    "zone_function_label",
    "zone_subindex",
]
