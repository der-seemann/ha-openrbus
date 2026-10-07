"""Shared registry-to-HA projection for OpenRBus register entities.

The OpenRBus catalogue is deliberately richer than Home Assistant's entity
model.  This module is the single place where a catalogue row is classified
as a read-only sensor or as a safe, typed control.  It also owns the shared
polling coordinators so forwarding ``sensor``, ``number``, ``select`` and
``switch`` concurrently cannot create duplicate transport reads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from openrbus.catalog import RegisterCatalogEntry, catalog_for_node
from openrbus.discovery import DeviceIdentity
from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import Registry

from .bridge import GenericRead
from .const import (
    CONF_COOLING_ENABLED,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_GROUP_OVERRIDES,
    CONF_NODE_OVERRIDES,
    CONF_SCREED_DRYING_ENABLED,
    CONF_ZONE_OVERRIDES,
    DEFAULT_COOLING_ENABLED,
    DOMAIN,
)
from .coordinator import (
    OpenRBusCoordinator,
    OpenRBusPollingCoordinator,
    schedule_first_refresh_in_background,
)
from .entity_names import register_display_name, suggested_object_id
from .identity import stable_gateway_id, stable_node_id, stable_object_id
from .optional_register_filters import OPTIONAL_REGISTER_FILTERS
from .zones import (
    ZONE_FAMILY_SLOT_OBJECTS,
    ZONE_PARENT_OBJECT_SOURCES,
    ZONE_SLOT_OBJECT_SOURCES,
    ZONE_UNRESOLVED_OBJECT_SOURCES,
    ZoneAssociation,
    ZoneKind,
    ZoneReadState,
    entity_zone_label,
    override_key,
    profile_for,
    zone_association,
    zone_device_name,
    zone_enabled,
    zone_is_active,
    zone_is_confirmed_disabled,
    zone_projection_exists,
    zone_projection_record,
    zone_read_state,
    zone_subindex,
)

CATALOG_REGISTRY = Registry.load_default()


def register_name(register: RegisterCatalogEntry, language: str) -> str:
    """Return a Core locale label while supporting the previous catalog API."""

    reviewed = register_display_name(register, language)
    if reviewed:
        return reviewed

    localized = getattr(register, "name", None)
    if callable(localized):
        return localized(language)
    return (
        (register.name_de if language == "de" else register.name_en)
        or register.name_en
        or register.name_de
    )


def identity_for_runtime(runtime_node: Any) -> DeviceIdentity:
    """Return the evidence-backed identity from an inventory or identity."""

    return getattr(runtime_node, "identity", runtime_node)


def runtime_nodes(parent: OpenRBusCoordinator) -> tuple[Any, ...]:
    """Return the current node projection without assuming a node number."""

    return tuple(parent.inventories or parent.devices)


def _legacy_zone_bound_subindex(register: Any) -> int | None:
    """Identify rows that the pre-map zone classifier could have tagged.

    This migration guard mirrors the old source classifier only for exact
    catalog records. It is intentionally not used for current projection.
    """

    address = getattr(register, "address", None)
    index = getattr(address, "index", None)
    subindex = getattr(address, "subindex", None)
    if not isinstance(index, int) or not isinstance(subindex, int):
        return None
    if not (0x3400 <= index <= 0x3477 or 0x5402 <= index <= 0x5444):
        return None
    semantic = " ".join(
        str(getattr(register, field, "") or "")
        for field in ("internal_code", "name_en", "name_de")
    ).casefold()
    internal_code = str(getattr(register, "internal_code", "")).upper()
    if (
        "zone" in semantic
        or internal_code.startswith(("CP", "CM", "CC"))
        or any(
            marker in semantic
            for marker in (
                "heizkreis",
                "heating circuit",
                "hk,",
                "hk ",
                " hk",
                "hk/",
                "hk-",
            )
        )
    ):
        return subindex
    return None


def poll_group(register: RegisterCatalogEntry, recommended: frozenset) -> str:
    """Assign a stable polling group from registry semantics."""

    if register.address in recommended or register.unit in {"°C", "bar", "%"}:
        return "fast"
    semantic = " ".join(
        str(value or "")
        for value in (register.internal_code, register.name_en, register.name_de)
    ).casefold()
    if any(
        marker in semantic
        for marker in (
            "ident",
            "version",
            "diagnostic",
            "configuration",
            "config",
            "parameter number",
            "device type",
        )
    ):
        return "slow"
    return "standard"


def recommended_addresses(identity: DeviceIdentity) -> frozenset[ObjectAddress]:
    """Resolve conservative defaults from Core's registry."""

    try:
        resolution = getattr(identity, "registry_resolution", None)
        family = getattr(resolution, "family", None) or getattr(
            identity, "family", None
        )
        return frozenset(
            item.address
            for item in CATALOG_REGISTRY.recommended_registers(device_family=family)
        )
    except (AttributeError, TypeError, ValueError):
        return frozenset()


def access_level(value: object) -> int | None:
    """Map Core's public access labels, failing closed for unknown levels."""

    labels = {
        "level 0": 0,
        "user": 1,
        "installer": 2,
        "professional": 3,
    }
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return labels.get(str(value).casefold())
    return numeric if 0 <= numeric <= 3 else None


def catalog_visible(register: RegisterCatalogEntry, max_access_level: int) -> bool:
    """Return whether a row has explicit read evidence at this level."""

    if not register.readable:
        return False
    evidence = register.access_level_evidence.get("read", {})
    levels = evidence.get("levels", ()) if isinstance(evidence, dict) else ()
    numeric = [access_level(level) for level in levels]
    return bool(numeric) and any(
        level is not None and level <= max_access_level for level in numeric
    )


def diagnostics_enabled(parent: OpenRBusCoordinator) -> bool:
    """Return the persisted expert-diagnostics preference for an entry."""

    configured = dict(getattr(parent.config_entry, "data", {}) or {})
    configured.update(getattr(parent.config_entry, "options", {}) or {})
    return bool(configured.get(CONF_DIAGNOSTICS_ENABLED, False))


def is_diagnostic_register(register: RegisterCatalogEntry) -> bool:
    """Classify explicit diagnostic/debug catalogue rows conservatively.

    Core currently exposes no dedicated diagnostic flag on every catalogue
    row.  Prefer one when it arrives, and otherwise only match the explicit
    German/English diagnostic terms.  Do not infer that faults, status values
    or configuration are diagnostics: those are normal operational entities.
    """

    category = str(getattr(register, "category", "") or "").casefold()
    if category in {"diagnostic", "diagnostics", "debug"}:
        return True
    semantic = " ".join(
        str(getattr(register, field, "") or "")
        for field in ("internal_code", "name_de", "name_en")
    ).casefold()
    return any(marker in semantic for marker in ("diagnostic", "diagnose", "debug"))


def screed_drying_enabled(parent: OpenRBusCoordinator) -> bool:
    """Return the persisted commissioning-program preference for an entry."""

    configured = dict(getattr(parent.config_entry, "data", {}) or {})
    configured.update(getattr(parent.config_entry, "options", {}) or {})
    return bool(configured.get(CONF_SCREED_DRYING_ENABLED, False))


def is_screed_drying_register(register: RegisterCatalogEntry) -> bool:
    """Return whether the reviewed exact register map classifies this row."""

    return "screed" in OPTIONAL_REGISTER_FILTERS.get(
        getattr(register, "address", None), ()
    )


def cooling_enabled(parent: OpenRBusCoordinator) -> bool:
    """Return the explicit parent-level cooling projection preference."""

    configured = dict(getattr(parent.config_entry, "data", {}) or {})
    configured.update(getattr(parent.config_entry, "options", {}) or {})
    return bool(configured.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED))


def is_cooling_register(register: RegisterCatalogEntry) -> bool:
    """Return whether the reviewed exact register map classifies this row."""

    return "cooling" in OPTIONAL_REGISTER_FILTERS.get(
        getattr(register, "address", None), ()
    )


def is_optional_filter_register(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
    filter_name: str,
) -> bool:
    """Apply the reviewed exact-address classification."""

    semantic_match = (
        is_screed_drying_register(register)
        if filter_name == "screed"
        else is_cooling_register(register)
    )
    return semantic_match


def rows_for_parent(
    parent: OpenRBusCoordinator,
    *,
    include_diagnostics: bool | None = None,
    include_screed_drying: bool | None = None,
    include_cooling: bool | None = None,
) -> tuple[tuple[DeviceIdentity, RegisterCatalogEntry, str, bool], ...]:
    """Project all known rows and their poll eligibility for this entry."""

    configured = max(
        1,
        min(
            3,
            int(
                getattr(
                    parent,
                    "configured_read_access_level",
                    getattr(parent, "configured_access_level", 1),
                )
            ),
        ),
    )
    if include_diagnostics is None:
        include_diagnostics = diagnostics_enabled(parent)
    if include_screed_drying is None:
        include_screed_drying = screed_drying_enabled(parent)
    if include_cooling is None:
        include_cooling = cooling_enabled(parent)
    rows: list[tuple[DeviceIdentity, RegisterCatalogEntry, str, bool]] = []
    for runtime_node in runtime_nodes(parent):
        identity = identity_for_runtime(runtime_node)
        effective = parent.effective_access_levels.get(identity.node)
        max_level = (
            min(configured, effective)
            if effective is not None
            else (1 if configured == 1 else None)
        )
        recommended = recommended_addresses(identity)
        experimental = bool(
            getattr(parent, "write_enabled", False)
            and getattr(parent, "experimental_writes", False)
        )
        if experimental:
            registers = catalog_for_node(
                runtime_node,
                CATALOG_REGISTRY,
                experimental_writes=True,
            )
        else:
            registers = catalog_for_node(runtime_node, CATALOG_REGISTRY)
        for register in registers:
            association = zone_association(register, identity)
            if association is ZoneAssociation.UNRESOLVED:
                # Exact source-known zone data without a proven slot dimension
                # must not become a duplicate parent entity.
                continue
            zone_active = (
                zone_is_active(parent, identity.node, register.address.subindex)
                if association is ZoneAssociation.ZONE_SLOT
                else True
            )
            if association is ZoneAssociation.ZONE_SLOT and not zone_active:
                if (
                    zone_read_state(parent, identity.node, register.address.subindex)
                    is not ZoneReadState.UNKNOWN
                ):
                    continue
                projected_uid = entity_unique_id(parent, identity, register)
                if not zone_projection_exists(
                    parent,
                    identity.node,
                    register.address.subindex,
                    identity,
                    projected_uid,
                ):
                    continue
                # Activity must be known and positive before any platform sees
                # a new zone row. Historical manifest rows are retained only
                # to preserve identity while current state is unknown.
            if not include_diagnostics and is_diagnostic_register(register):
                continue
            if not include_screed_drying and is_optional_filter_register(
                parent, identity, register, "screed"
            ):
                continue
            if not include_cooling and is_optional_filter_register(
                parent, identity, register, "cooling"
            ):
                continue
            rows.append(
                (
                    identity,
                    register,
                    poll_group(register, recommended),
                    max_level is not None and catalog_visible(register, max_level),
                )
            )
    return tuple(rows)


def zone_row_enabled(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> bool:
    """Return whether an exact zone row has positive activity and selection."""

    association = zone_association(register, identity)
    if association is ZoneAssociation.UNRESOLVED:
        return False
    if association is not ZoneAssociation.ZONE_SLOT:
        return True
    slot = register.address.subindex
    return zone_is_active(parent, identity.node, slot) and zone_enabled(
        parent, identity.node, slot
    )


def _unobserved_source_rw_row(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> bool:
    """Identify newly writable catalog rows absent from runtime discovery.

    Family and bounded-array metadata makes comparable registers selectable,
    but does not establish that an object exists on this installation. Keep
    those newly writable rows out of the initial poll set until the user
    selects them or discovery confirms them.
    """

    # ``writable`` is the active write projection.  It is false when HA's
    # write option is off and for experimental rows while their opt-in is off;
    # neither state erases the underlying source declaration.  Use Core's
    # declaration fact so read-only catalog rows keep their normal defaults.
    if not getattr(register, "write_declared", False):
        return False
    for runtime_node in runtime_nodes(parent):
        if identity_for_runtime(runtime_node).node != identity.node:
            continue
        capabilities = getattr(runtime_node, "capabilities", None)
        if capabilities is None:
            return False
        addresses = (
            capabilities.keys()
            if isinstance(capabilities, Mapping)
            else (getattr(item, "address", item) for item in capabilities)
        )
        return register.address not in addresses
    return False


def _poll_selection_filter_counts(
    parent: OpenRBusCoordinator,
    complete_rows: tuple[tuple[DeviceIdentity, RegisterCatalogEntry, str, bool], ...],
    rows: tuple[tuple[DeviceIdentity, RegisterCatalogEntry, str, bool], ...],
) -> dict[str, int]:
    """Summarize poll-row filtering without retaining object identities."""

    counts = {
        "runtime_nodes": len(runtime_nodes(parent)),
        "catalog_rows": len(complete_rows),
        "diagnostics_filtered": 0,
        "screed_filtered": 0,
        "cooling_filtered": 0,
        "rows_after_optional_filters": len(rows),
    }
    for _identity, register, _group, _allowed in complete_rows:
        if not diagnostics_enabled(parent) and is_diagnostic_register(register):
            counts["diagnostics_filtered"] += 1
        if not screed_drying_enabled(parent) and is_screed_drying_register(register):
            counts["screed_filtered"] += 1
        if not cooling_enabled(parent) and is_cooling_register(register):
            counts["cooling_filtered"] += 1
    return counts


def _poll_activation_counts(
    hass: HomeAssistant,
    parent: OpenRBusCoordinator,
    rows: tuple[tuple[DeviceIdentity, RegisterCatalogEntry, str, bool], ...],
) -> dict[str, int]:
    """Return payload-free attribution for default and registry poll activation."""
    counts = {
        "runtime_capabilities_supported": 0,
        "runtime_capabilities_not_supported": 0,
        "runtime_capabilities_unknown": 0,
        "runtime_capabilities_temporary_failed": 0,
        "runtime_capability_exact_matches": 0,
        "runtime_capability_absent_rows": 0,
        "write_declared_rows": 0,
        "unobserved_declared_write_rows": 0,
        "unobserved_guard_rows": 0,
        "default_enabled_rows": 0,
        "default_disabled_rows": 0,
        "registry_uid_matches": 0,
        "registry_uid_missing": 0,
        "registry_enabled_matches": 0,
        "registry_integration_disabled_matches": 0,
        "registry_user_disabled_matches": 0,
    }
    for group in ("fast", "standard", "slow"):
        for suffix in (
            "default_enabled",
            "default_disabled",
            "declared_write_absent",
            "registry_enabled",
            "registry_missing",
        ):
            counts[f"{group}_{suffix}"] = 0
    for runtime_node in runtime_nodes(parent):
        capabilities = getattr(runtime_node, "capabilities", None) or ()
        items = (
            capabilities.values() if isinstance(capabilities, Mapping) else capabilities
        )
        for item in items:
            status = getattr(item, "status", None)
            status_name = str(getattr(status, "value", status) or "unknown").casefold()
            key = {
                "supported": "runtime_capabilities_supported",
                "not_supported": "runtime_capabilities_not_supported",
                "unknown": "runtime_capabilities_unknown",
                "temporarily_failed": "runtime_capabilities_temporary_failed",
            }.get(status_name, "runtime_capabilities_unknown")
            counts[key] += 1

    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError):
        registry = None
    platforms = ("sensor", "number", "select", "switch", "binary_sensor")
    for identity, register, _group, _allowed in rows:
        declared = bool(getattr(register, "write_declared", False))
        absent = _unobserved_source_rw_row(parent, identity, register)
        enabled_default = entity_enabled_by_default(parent, identity, register)
        counts["write_declared_rows"] += int(declared)
        counts["unobserved_declared_write_rows"] += int(declared and absent)
        counts["unobserved_guard_rows"] += int(absent)
        counts["default_enabled_rows"] += int(enabled_default)
        counts["default_disabled_rows"] += int(not enabled_default)
        counts[
            f"{_group}_{'default_enabled' if enabled_default else 'default_disabled'}"
        ] += 1
        counts[f"{_group}_declared_write_absent"] += int(declared and absent)
        matching_entries = []
        if registry is not None:
            uid = entity_unique_id(parent, identity, register)
            for platform in platforms:
                entity_id = registry.async_get_entity_id(platform, DOMAIN, uid)
                if entity_id:
                    entry = registry.entities.get(entity_id)
                    if entry is not None:
                        matching_entries.append(entry)
        if not matching_entries:
            counts["registry_uid_missing"] += 1
            counts[f"{_group}_registry_missing"] += 1
            continue
        counts["registry_uid_matches"] += 1
        if any(
            getattr(entry, "disabled_by", None) is None for entry in matching_entries
        ):
            counts["registry_enabled_matches"] += 1
            counts[f"{_group}_registry_enabled"] += 1
        if any(
            str(getattr(entry, "disabled_by", "")).casefold() == "integration"
            for entry in matching_entries
        ):
            counts["registry_integration_disabled_matches"] += 1
        if any(
            str(getattr(entry, "disabled_by", "")).casefold() == "user"
            for entry in matching_entries
        ):
            counts["registry_user_disabled_matches"] += 1
    exact_by_node: dict[int, set[ObjectAddress]] = {}
    for runtime_node in runtime_nodes(parent):
        capabilities = getattr(runtime_node, "capabilities", None) or ()
        addresses = (
            capabilities.keys()
            if isinstance(capabilities, Mapping)
            else (getattr(item, "address", item) for item in capabilities)
        )
        exact_by_node[identity_for_runtime(runtime_node).node] = set(addresses)
    counts["runtime_capability_exact_matches"] = sum(
        register.address in exact_by_node.get(identity.node, set())
        for identity, register, _group, _allowed in rows
    )
    counts["runtime_capability_absent_rows"] = sum(
        identity.node in exact_by_node
        and register.address not in exact_by_node[identity.node]
        for identity, register, _group, _allowed in rows
    )
    return counts


def entity_group_key(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> str:
    """Return the stable selector group key shared by UI and projection."""

    node = identity.node
    if is_diagnostic_register(register):
        base = f"node:{node}:optional:diagnostics"
    elif is_optional_filter_register(parent, identity, register, "screed"):
        base = f"node:{node}:optional:screed"
    elif is_optional_filter_register(parent, identity, register, "cooling"):
        base = f"node:{node}:optional:cooling"
    else:
        slot = zone_subindex(register, identity)
        if slot is not None:
            base = f"node:{node}:zone:{slot}"
        else:
            base = f"node:{node}:object:{int(register.address.index):04x}"
    # Unverified write declarations remain read-only projections. Keep these
    # groups separate so a picker never implies the writes were validated.
    if getattr(register, "safety", None) == "unverified":
        return f"{base}:write-unverified"
    return base


def entity_category_key(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> str:
    """Stable Device → Category scope, separate from legacy group identities."""

    return (
        f"device:{identity.node}:category:{entity_category(parent, identity, register)}"
    )


def entity_category_override_keys(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> tuple[str, ...]:
    """Return the current category key followed by pre-migration General."""

    key = entity_category_key(parent, identity, register)
    legacy_general = f"device:{identity.node}:category:general"
    return (key,) if key == legacy_general else (key, legacy_general)


def entity_category(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> str:
    """Classify from explicit catalogue category or an active CP02x function.

    Register names and product aliases are deliberately not classifiers: a
    label can mention a subsystem without proving category membership. Rows
    lacking structured category or active function evidence stay explicitly
    unclassified.
    """

    association = zone_association(register, identity)
    slot = (
        register.address.subindex
        if association is ZoneAssociation.FUNCTION_SELECTOR
        else zone_subindex(register, identity)
    )
    if slot is not None:
        from .zones import ZoneKind, profile_for

        profile = profile_for(parent, identity.node, slot, identity)
        if profile is not None and profile.kind is ZoneKind.HEATING:
            return "zone"
        if profile is not None and profile.kind is ZoneKind.DHW:
            return "dhw"
    # Core may publish a typed category on catalogue entries. Only accept an
    # exact normalized value; do not infer membership from register wording.
    raw_category = str(getattr(register, "category", "") or "").strip().casefold()
    category = " ".join(raw_category.replace("_", " ").replace("-", " ").split())
    explicit_categories = {
        "general": "general",
        "heating system": "heating_system",
        "zone": "zone",
        "domestic hot water": "dhw",
        "dhw": "dhw",
        "heat pump": "heat_pump",
    }
    return explicit_categories.get(category, "unclassified")


def _configured_selection_overrides(
    parent: OpenRBusCoordinator,
) -> tuple[dict[str, bool], dict[str, bool]]:
    config_entry = getattr(parent, "config_entry", None)
    configured = dict(getattr(config_entry, "data", {}) or {})
    configured.update(getattr(config_entry, "options", {}) or {})
    return (
        normalized_entity_overrides(configured.get(CONF_NODE_OVERRIDES)),
        normalized_entity_overrides(configured.get(CONF_GROUP_OVERRIDES)),
    )


def _selection_override(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
    unique_id: str,
) -> bool | None:
    entity_overrides = normalized_entity_overrides(
        getattr(parent, "entity_overrides", {})
    )
    explicit_entity = entity_overrides.get(unique_id)
    if explicit_entity is not None:
        return explicit_entity
    node_overrides, group_overrides = _configured_selection_overrides(parent)
    group_key = entity_group_key(parent, identity, register)
    explicit_group = next(
        (
            group_overrides[key]
            for key in entity_category_override_keys(parent, identity, register)
            if group_overrides.get(key) is not None
        ),
        None,
    )
    if explicit_group is None:
        explicit_group = group_overrides.get(group_key)
    if explicit_group is not None:
        return explicit_group
    return node_overrides.get(str(identity.node))


def entity_enabled_by_default(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
    *,
    unique_id: str | None = None,
) -> bool:
    """Combine conservative register defaults with discovered zone policy."""

    default = (
        zone_row_enabled(parent, identity, register)
        and not _unobserved_source_rw_row(parent, identity, register)
        and (
            register.address in recommended_addresses(identity)
            or (
                register.readable
                and getattr(register, "datatype", None) not in {"STRUCT", "OCTETSTRING"}
                and any(
                    str(level).casefold() in {"level 0", "user"}
                    for level in (
                        (getattr(register, "access_level_evidence", {}) or {}).get(
                            "read", {}
                        )
                        or {}
                    ).get("levels", ())
                )
            )
        )
    )
    # Manual choices may expose a non-recommended row, but cannot bypass
    # discovery/access, hidden-category, or zone safety gates.
    unique_id = unique_id or entity_unique_id(parent, identity, register)
    explicit = _selection_override(parent, identity, register, unique_id)
    if explicit is None:
        return default
    return (
        bool(explicit)
        and register.readable
        and zone_row_enabled(parent, identity, register)
    )


def _poll_row_selected(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> bool:
    """Keep absent inferred writes out of polling unless the picker opts in.

    An older sensor or typed projection can remain enabled in HA's registry
    after the catalog's default changes.  Since the poller intentionally
    accepts any enabled projection sharing a stable ID, enforce this safety
    default before rows enter the polling coordinator.  Explicit persisted
    picker choices still work through ``entity_enabled_by_default``.
    """

    return not _unobserved_source_rw_row(
        parent, identity, register
    ) or entity_enabled_by_default(parent, identity, register)


def normalized_entity_overrides(value: object) -> dict[str, bool | None]:
    """Parse persistent entity and scope choices, including inherited reset markers."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): None if enabled is None or enabled == "default" else bool(enabled)
        for key, enabled in value.items()
    }


def async_apply_entity_overrides(
    hass: HomeAssistant, parent: OpenRBusCoordinator
) -> None:
    """Apply generic choices to existing rows, preserving HA user decisions."""
    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError):
        return
    overrides = normalized_entity_overrides(getattr(parent, "entity_overrides", {}))
    node_overrides, group_overrides = _configured_selection_overrides(parent)
    entry_id = parent.config_entry.entry_id
    safe: dict[str, bool] = {}
    rows_by_uid: dict[str, tuple[DeviceIdentity, RegisterCatalogEntry]] = {}
    explicitly_scoped: set[str] = set()
    reconcile_defaults: set[str] = set()
    node_overrides, group_overrides = _configured_selection_overrides(parent)
    for identity, register, _group, allowed in rows_for_parent(
        parent,
        include_diagnostics=True,
        include_screed_drying=True,
        include_cooling=True,
    ):
        uid = entity_unique_id(parent, identity, register)
        rows_by_uid[uid] = (identity, register)
        if _unobserved_source_rw_row(parent, identity, register):
            reconcile_defaults.add(uid)
        group_key = entity_group_key(parent, identity, register)
        if (
            uid in overrides
            or entity_category_key(parent, identity, register) in group_overrides
            or group_key in group_overrides
            or str(identity.node) in node_overrides
        ):
            explicitly_scoped.add(uid)
        # A manual enable is constrained by current access evidence and all
        # explicit opt-in categories. Zone selection also remains sovereign.
        category_visible = (
            (not is_diagnostic_register(register) or diagnostics_enabled(parent))
            and (
                not is_optional_filter_register(parent, identity, register, "screed")
                or screed_drying_enabled(parent)
            )
            and (
                not is_optional_filter_register(parent, identity, register, "cooling")
                or cooling_enabled(parent)
            )
        )
        effective = parent.effective_access_levels.get(identity.node)
        safe[uid] = bool(
            allowed
            and category_visible
            and zone_row_enabled(parent, identity, register)
            and (
                control_kind(register, parent.language) is None
                or write_access_allowed(parent, register, effective)
            )
        )
        structure = bitfield_structure(register)
        if structure is not None:
            for field in structure.fields:
                if field.bit_length == 1:
                    bit_uid = f"{uid}:bit:{field.name}"
                    safe[bit_uid] = safe[uid]
                    rows_by_uid[bit_uid] = (identity, register)
                    if (
                        bit_uid in overrides
                        or entity_category_key(parent, identity, register)
                        in group_overrides
                        or group_key in group_overrides
                        or str(identity.node) in node_overrides
                    ):
                        explicitly_scoped.add(bit_uid)
    for entity in tuple(registry.entities.values()):
        uid = getattr(entity, "unique_id", None)
        if (
            getattr(entity, "platform", None) != DOMAIN
            or getattr(entity, "config_entry_id", None) != entry_id
            or (uid not in safe and uid not in overrides)
            or (
                uid not in overrides
                and uid not in explicitly_scoped
                and uid not in reconcile_defaults
            )
        ):
            continue
        row = rows_by_uid.get(uid)
        if row is not None:
            identity, register = row
            slot = zone_subindex(register, identity)
            # A missing CP020 read is unknown, not proof that a previously
            # enabled zone is inactive. Keep its registry choice until a
            # positive active/inactive profile arrives.
            if (
                slot is not None
                and profile_for(parent, identity.node, slot, identity) is None
            ):
                continue
            profile = (
                profile_for(parent, identity.node, slot, identity)
                if slot is not None
                else None
            )
            previously_active = (profile is not None and profile.active) or (
                slot is not None
                and (identity.node, slot)
                in getattr(parent, "_zone_confirmed_active_slots", set())
            )
            retained_active_unknown = (
                slot is not None
                and previously_active
                and zone_read_state(parent, identity.node, slot)
                is ZoneReadState.UNKNOWN
            )
        else:
            retained_active_unknown = False
        disabled_by = str(getattr(entity, "disabled_by", "") or "").casefold()
        if uid not in safe:
            should_enable = False
        else:
            identity, register = rows_by_uid[uid]
            should_enable = (
                entity_enabled_by_default(parent, identity, register, unique_id=uid)
                and safe[uid]
            )
        if not should_enable and not disabled_by:
            # ``disabled_by=None`` is a persisted user enable in HA.  The
            # inferred source-RW fallback below only changes the default; it
            # must not revoke that choice when capabilities are temporarily
            # absent.  Explicit picker/scope choices and all safety/category
            # gates still take precedence.
            if (
                retained_active_unknown
                or (uid in reconcile_defaults and safe.get(uid, False))
            ) and (uid not in overrides and uid not in explicitly_scoped):
                continue
            registry.async_update_entity(
                entity.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
        elif should_enable and disabled_by == "integration":
            registry.async_update_entity(entity.entity_id, disabled_by=None)


def async_apply_diagnostic_visibility(
    hass: HomeAssistant, parent: OpenRBusCoordinator
) -> None:
    """Apply opt-in diagnostics/screed visibility without deleting registry rows.

    A user disable remains authoritative.  Enabling diagnostics only revives
    projections whose normal policy permits them; a diagnostics toggle must
    never bypass the independent write-safety policy for a typed control.
    Screed drying uses the same registry-preserving lifecycle, but remains a
    distinct option and classification.  This is intentionally called on
    platform setup, after the options update listener has reloaded the entry.
    """

    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError):
        return
    diagnostics_visible = diagnostics_enabled(parent)
    screed_drying_visible = screed_drying_enabled(parent)
    cooling_visible = cooling_enabled(parent)
    permitted: dict[str, bool] = {}
    visibility: dict[str, bool] = {}
    entry_id = parent.config_entry.entry_id
    # These two identity projections have Diagnostic category in HA and are
    # part of the same expert surface even though they are not catalog rows.
    permitted[stable_object_id(parent, 0xFF, 0x2001, 0x02)] = True
    for runtime_node in runtime_nodes(parent):
        identity = identity_for_runtime(runtime_node)
        if getattr(identity, "device_code", None) is not None:
            permitted[
                f"{stable_node_id(parent, identity.node)}:identity:device_code"
            ] = True
        if getattr(identity, "parameter_number", None) is not None:
            permitted[
                f"{stable_node_id(parent, identity.node)}:identity:parameter_number"
            ] = True
    for identity, register, _group, _allowed in rows_for_parent(
        parent,
        include_diagnostics=True,
        include_screed_drying=True,
        include_cooling=True,
    ):
        diagnostic = is_diagnostic_register(register)
        screed_drying = is_optional_filter_register(
            parent, identity, register, "screed"
        )
        cooling = is_optional_filter_register(parent, identity, register, "cooling")
        if not diagnostic and not screed_drying and not cooling:
            continue
        unique_id = entity_unique_id(parent, identity, register)
        permitted[unique_id] = control_kind(
            register, parent.language
        ) is None or write_access_allowed(
            parent,
            register,
            parent.effective_access_levels.get(identity.node),
        )
        # A row can theoretically carry both classifications.  Both opt-ins
        # must then be enabled; a generic heating row never reaches this map.
        visibility[unique_id] = (
            (not diagnostic or diagnostics_visible)
            and (not screed_drying or screed_drying_visible)
            and (not cooling or cooling_visible)
        )
    for entity in tuple(registry.entities.values()):
        if (
            getattr(entity, "platform", None) != DOMAIN
            or getattr(entity, "config_entry_id", None) != entry_id
            or getattr(entity, "unique_id", None) not in permitted
        ):
            continue
        disabled_by = str(getattr(entity, "disabled_by", "") or "").casefold()
        if (
            not visibility.get(entity.unique_id, diagnostics_visible)
            and not disabled_by
        ):
            registry.async_update_entity(
                entity.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
        elif (
            visibility.get(entity.unique_id, diagnostics_visible)
            and disabled_by == "integration"
            and permitted[entity.unique_id]
        ):
            registry.async_update_entity(entity.entity_id, disabled_by=None)


def ensure_polling_coordinators(
    hass: HomeAssistant,
    parent: OpenRBusCoordinator,
) -> dict[str, OpenRBusPollingCoordinator]:
    """Create/reuse one polling coordinator per group for all HA platforms."""

    complete_rows = rows_for_parent(
        parent,
        include_diagnostics=True,
        include_screed_drying=True,
        include_cooling=True,
    )
    rows = rows_for_parent(parent)
    selection_counts = {
        **_poll_selection_filter_counts(parent, complete_rows, rows),
        **_poll_activation_counts(hass, parent, rows),
        "read_access_excluded": 0,
        "zone_excluded": 0,
        "unobserved_declared_write_not_selected": 0,
        "not_recommended": 0,
        "pollable_fast": 0,
        "pollable_standard": 0,
        "pollable_slow": 0,
    }
    addresses: dict[str, set[tuple[int, ObjectAddress]]] = {
        "fast": set(),
        "standard": set(),
        "slow": set(),
    }
    metadata: dict[str, dict[tuple[int, ObjectAddress], RegisterCatalogEntry]] = {
        "fast": {},
        "standard": {},
        "slow": {},
    }
    for identity, register, group, allowed in rows:
        if not allowed:
            selection_counts["read_access_excluded"] += 1
            continue
        if not zone_row_enabled(parent, identity, register):
            selection_counts["zone_excluded"] += 1
            continue
        if not _poll_row_selected(parent, identity, register):
            selection_counts["unobserved_declared_write_not_selected"] += 1
            continue
        effective = parent.effective_access_levels.get(identity.node)
        structure = bitfield_structure(register)
        if structure is not None and register.readable:
            if effective is None or not catalog_visible(register, effective):
                continue
            addresses[group].add((identity.node, register.address))
            metadata[group][(identity.node, register.address)] = register
            continue
        kind = control_kind(register, parent.language)
        if kind is not None:
            # A typed control is a read projection first.  Do not couple its
            # polling to write policy: a readable L3 row must still publish
            # its current value when the write evidence is unverified or the
            # explicit unsafe-write option is off.  ``_async_update_data``
            # applies the entity-registry enabled/disabled gate per row.
            if effective is None or not catalog_visible(register, effective):
                continue
        else:
            recommended = recommended_addresses(identity)
            evidence = register.access_level_evidence.get("read", {})
            levels = evidence.get("levels", ()) if isinstance(evidence, dict) else ()
            safe_default = register.datatype not in {"STRUCT", "OCTETSTRING"} and any(
                str(level).casefold() in {"level 0", "user"} for level in levels
            )
            if (
                register.address not in recommended
                and not safe_default
                and not entity_enabled_by_default(parent, identity, register)
            ):
                selection_counts["not_recommended"] += 1
                continue
        addresses[group].add((identity.node, register.address))
        metadata[group][(identity.node, register.address)] = register
        selection_counts[f"pollable_{group}"] += 1

    parent._openrbus_poll_selection_diagnostics = selection_counts

    cache = getattr(parent, "_openrbus_polling_coordinators", None)
    if cache is None:
        cache = {}
        parent._openrbus_polling_coordinators = cache
    # Diagnostics may receive a config-entry runtime object that is not the
    # same platform setup object on every supported Home Assistant version.
    # Keep the live pollers in the integration's HA-owned registry as the
    # authoritative fallback, keyed by entry ID.
    hass.data.setdefault(f"{DOMAIN}_polling_coordinators", {})[
        parent.config_entry.entry_id
    ] = cache
    for group, group_addresses in addresses.items():
        existing = cache.get(group)
        if existing is None:
            existing = OpenRBusPollingCoordinator(
                hass,
                parent,
                group,
                tuple(
                    sorted(
                        group_addresses,
                        key=lambda item: (item[0], item[1].index, item[1].subindex),
                    )
                ),
                parent.poll_intervals[group],
                register_metadata=metadata[group],
            )
            cache[group] = existing
            parent.config_entry.async_on_unload(existing.async_shutdown)
            schedule_first_refresh_in_background(
                hass,
                parent.config_entry,
                existing,
                name=f"OpenRBus {group} initial refresh",
            )
        else:
            existing.add_registers(group_addresses, metadata[group])
    return cache


def cleanup_legacy_sensor_entities(
    hass: HomeAssistant,
    parent: OpenRBusCoordinator,
) -> int:
    """Migrate registry rows whose current projection changed to a sensor.

    This function keeps its historical name because older platform modules
    call it during setup.  It now also handles the reverse migration required
    by the array-count guard: an older release could have registered a
    ``number``, ``select`` or ``switch`` for a row which is a sensor in the
    current catalogue.  The migration is deliberately derived from the
    current catalogue and exact OpenRBus unique ID; it never scans or removes
    arbitrary HA entities.

    Registry cleanup is naturally idempotent.  Once the stale typed row is
    removed, subsequent reloads find no matching entry.  Metadata is retained
    on the runtime coordinator until the sensor platform has created the new
    row, where :func:`restore_migrated_sensor_entities` applies the safe HA
    registry customizations that survived the migration.
    """

    registry = er.async_get(hass)
    entry_id = parent.config_entry.entry_id
    pending = getattr(parent, "_openrbus_sensor_migrations", None)
    if pending is None:
        pending = {}
        parent._openrbus_sensor_migrations = pending

    # Iterate the registry rather than relying solely on async_get_entity_id:
    # HA normally enforces unique IDs per platform, but iterating lets us
    # remain safe if a legacy registry contains duplicate/stale rows.  Every
    # predicate below is intentional: integration, platform, entry and the
    # complete stable object identity must all match.
    registry_entries = tuple(registry.entities.values())
    downgrade_rows: set[str] = set()
    typed_rows: set[str] = set()
    for identity, register, _group, _poll_allowed in rows_for_parent(parent):
        if not register.readable:
            continue
        unique_id = entity_unique_id(parent, identity, register)
        effective = parent.effective_access_levels.get(identity.node)
        if effective is None and parent.configured_access_level == 1:
            effective = 1
        if control_kind(register, parent.language) is None or not write_access_allowed(
            parent, register, effective
        ):
            downgrade_rows.add(unique_id)
        else:
            typed_rows.add(unique_id)
    removed = 0
    for entity in registry_entries:
        if (
            getattr(entity, "platform", None) != DOMAIN
            or getattr(entity, "config_entry_id", None) != entry_id
            or getattr(entity, "unique_id", None) is None
        ):
            continue
        unique_id = entity.unique_id
        domain = getattr(entity, "domain", None)
        # Preserve the original sensor->typed cleanup as well as the new
        # typed->sensor downgrade.  Both directions use the same strict
        # integration/entry/identity guards above.
        if domain == "sensor" and unique_id in typed_rows:
            registry.async_remove(entity.entity_id)
            removed += 1
            continue
        if (
            domain not in {"number", "select", "switch"}
            or unique_id not in downgrade_rows
        ):
            continue
        metadata = pending.setdefault(unique_id, {})
        for field in _MIGRATED_REGISTRY_FIELDS:
            value = getattr(entity, field, None)
            if field in {"disabled_by", "hidden_by"} and not _is_user_registry_value(
                value
            ):
                # Integration-disabled defaults belong to the old platform,
                # not to the replacement sensor.  Explicit user choices are
                # the only disable/hide state safe to carry over.
                continue
            # Empty values are defaults, not user customizations.  Keeping
            # only meaningful values also avoids passing HA sentinels through
            # a later update call.
            if value is not None and value != "":
                metadata.setdefault(field, value)
        registry.async_remove(entity.entity_id)
        removed += 1
    return removed


def cleanup_inactive_zone_entities(
    hass: HomeAssistant,
    parent: OpenRBusCoordinator,
) -> int:
    """Remove stale mapped child rows, retaining HA's restorable tombstones.

    Home Assistant's EntityRegistry.async_remove persists a DeletedRegistryEntry
    keyed by domain/platform/unique_id. Its next async_get_or_create restores
    the old entity_id, name, area, disabled/hidden choices, icon, labels,
    aliases, options, and registry UUID. Device registry rows are deliberately
    untouched; stable child identifiers reconnect to the same user-named
    device when its CP020 function becomes active again.
    """

    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError):
        return 0
    entry_id = parent.config_entry.entry_id
    inactive_unique_ids: set[str] = set()
    config_entry = parent.config_entry
    override_source_present = False
    validated_overrides: dict[str, object] = {}
    for source in (
        getattr(config_entry, "data", {}) or {},
        getattr(config_entry, "options", {}) or {},
    ):
        if CONF_ZONE_OVERRIDES in source:
            override_source_present = True
            values = source.get(CONF_ZONE_OVERRIDES)
            if isinstance(values, Mapping):
                validated_overrides.update(values)
    if not override_source_present:
        configured_overrides = getattr(parent, "zone_overrides", {})
        if isinstance(configured_overrides, Mapping):
            validated_overrides.update(configured_overrides)
    for runtime_node in runtime_nodes(parent):
        identity = identity_for_runtime(runtime_node)
        node = identity.node
        # Retire legacy rows for exact catalog objects whose dimension is
        # unresolved.  They are intentionally absent from every platform
        # projection; do not infer ownership from an address range or label.
        for register in catalog_for_node(identity, CATALOG_REGISTRY):
            index = getattr(getattr(register, "address", None), "index", None)
            slot = _legacy_zone_bound_subindex(register)
            if (
                index not in ZONE_UNRESOLVED_OBJECT_SOURCES
                or index in ZONE_PARENT_OBJECT_SOURCES
                or slot is None
                or slot <= 0
            ):
                continue
            inactive_unique_ids.add(stable_object_id(parent, node, index, slot))
        for index in ZONE_SLOT_OBJECT_SOURCES:
            allowed_families = ZONE_FAMILY_SLOT_OBJECTS.get(index)
            if allowed_families is not None:
                resolution = getattr(identity, "registry_resolution", None)
                family = getattr(identity, "family", None) or getattr(
                    resolution, "family", None
                )
                if str(family or "").strip().casefold() not in allowed_families:
                    continue
            for slot in range(1, 11):
                override = validated_overrides.get(override_key(identity.node, slot))
                explicitly_disabled = type(override) is bool and override is False
                confirmed_disabled = zone_is_confirmed_disabled(parent, node, slot)
                # Only a persisted exact projection manifest can retain rows
                # through UNKNOWN. A registry ghost or a cached activity label
                # is not enough to keep a never-projected slot alive.
                record = zone_projection_record(parent, node, slot, identity)
                unique_id = stable_object_id(parent, node, index, slot)
                manifested = bool(record and unique_id in record.get("uids", ()))
                session_confirmed = (node, slot) in getattr(
                    parent, "_zone_confirmed_active_slots", set()
                )
                unknown_without_history = (
                    zone_read_state(parent, node, slot) is ZoneReadState.UNKNOWN
                    and not record
                    and not session_confirmed
                )
                if (
                    confirmed_disabled
                    or explicitly_disabled
                    or unknown_without_history
                    or (
                        not zone_is_active(parent, node, slot)
                        and record is not None
                        and not manifested
                    )
                ):
                    inactive_unique_ids.add(unique_id)
                    continue
    removed = 0
    allowed_domains = {"sensor", "binary_sensor", "number", "select", "switch"}
    for entity in tuple(registry.entities.values()):
        unique_id = getattr(entity, "unique_id", "")
        base_unique_id, bit_separator, bit_name = unique_id.partition(":bit:")
        exact_mapped_row = base_unique_id in inactive_unique_ids and (
            not bit_separator or (bool(bit_name) and ":" not in bit_name)
        )
        if (
            getattr(entity, "platform", None) != DOMAIN
            or getattr(entity, "config_entry_id", None) != entry_id
            or getattr(entity, "domain", None) not in allowed_domains
            or not exact_mapped_row
        ):
            continue
        registry.async_remove(entity.entity_id)
        removed += 1
    return removed


def _is_user_registry_value(value: object) -> bool:
    """Return whether a registry enum/value represents an explicit user choice."""

    return str(getattr(value, "value", value)).casefold() == "user"


_MIGRATED_REGISTRY_FIELDS = (
    "device_id",
    "area_id",
    "disabled_by",
    "hidden_by",
    "icon",
    "name",
    "entity_category",
    "labels",
)


def restore_migrated_sensor_entities(
    hass: HomeAssistant,
    parent: OpenRBusCoordinator,
) -> int:
    """Restore safe registry metadata after a typed-to-sensor migration.

    HA creates the replacement sensor only after ``async_add_entities``.  A
    separate post-add step therefore preserves device/area associations and
    explicit user customizations without trying to manufacture entity IDs or
    touching any other integration's registry rows.  If setup is interrupted
    before the replacement exists, the pending metadata remains for the next
    idempotent setup attempt.
    """

    pending = getattr(parent, "_openrbus_sensor_migrations", None)
    if not pending:
        return 0
    registry = er.async_get(hass)
    entry_id = parent.config_entry.entry_id
    restored = 0
    remaining: dict[str, dict[str, object]] = {}
    for unique_id, metadata in pending.items():
        sensor_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        sensor = registry.entities.get(sensor_id) if sensor_id else None
        if (
            sensor is None
            or getattr(sensor, "platform", None) != DOMAIN
            or getattr(sensor, "config_entry_id", None) != entry_id
            or getattr(sensor, "domain", None) != "sensor"
        ):
            remaining[unique_id] = metadata
            continue
        changes = {
            field: value
            for field, value in metadata.items()
            if field in _MIGRATED_REGISTRY_FIELDS
            and getattr(sensor, field, None) != value
        }
        if not changes:
            restored += 1
            continue
        try:
            registry.async_update_entity(sensor.entity_id, **changes)
        except (TypeError, ValueError):
            # Registry API fields vary slightly across supported HA versions.
            # Apply each field independently so one optional field cannot
            # prevent device/area association from being preserved.
            for field, value in changes.items():
                try:
                    registry.async_update_entity(sensor.entity_id, **{field: value})
                except (TypeError, ValueError):
                    continue
        restored += 1
    parent._openrbus_sensor_migrations = remaining
    return restored


def entity_unique_id(
    parent: OpenRBusCoordinator,
    identity: DeviceIdentity,
    register: RegisterCatalogEntry,
) -> str:
    """Stable object identity shared by sensor and control projections."""

    return stable_object_id(
        parent,
        identity.node,
        register.address.index,
        register.address.subindex,
    )


def migrate_stable_registry_ids(
    hass: HomeAssistant, parent: OpenRBusCoordinator
) -> None:
    """Move legacy entry-scoped IDs to the physical gateway/register rule.

    Home Assistant keeps entity IDs and registry metadata when ``unique_id``
    changes, so history and user customizations remain attached to the same
    entity row. Conflicting destination IDs are left untouched for review.
    """
    try:
        registry = er.async_get(hass)
    except (AttributeError, TypeError):
        return
    entry_id = parent.config_entry.entry_id
    gateway = stable_gateway_id(parent)
    for entity in tuple(registry.entities.values()):
        if (
            getattr(entity, "platform", None) != DOMAIN
            or getattr(entity, "config_entry_id", None) != entry_id
        ):
            continue
        old = str(getattr(entity, "unique_id", ""))
        new: str | None = None
        prefix = f"{entry_id}:node:"
        if old.startswith(prefix) and ":object:" in old:
            tail = old[len(prefix) :]
            node_text, object_text = tail.split(":object:", 1)
            parts = object_text.split(":")
            try:
                node = int(node_text)
                index = int(parts[0], 16)
                subindex = int(parts[1], 16)
            except (ValueError, IndexError):
                continue
            new = stable_object_id(parent, node, index, subindex)
            if len(parts) > 2:
                new += ":" + ":".join(parts[2:])
        elif old == f"{entry_id}_2001_02":
            new = stable_object_id(parent, 0xFF, 0x2001, 0x02)
        elif old.startswith(f"{entry_id}_node_"):
            tail = old[len(f"{entry_id}_node_") :]
            node_text, separator, field = tail.partition("_")
            if separator and field in {"device_code", "parameter_number"}:
                try:
                    new = f"{gateway}:node:{int(node_text)}:identity:{field}"
                except ValueError:
                    continue
        if not new or new == old:
            continue
        collision = registry.async_get_entity_id(entity.domain, entity.platform, new)
        if collision and collision != entity.entity_id:
            continue
        registry.async_update_entity(entity.entity_id, new_unique_id=new)
    try:
        devices = dr.async_get(hass)
    except (AttributeError, TypeError):
        return
    device_prefix = f"{entry_id}:node:"
    for device in tuple(devices.devices.values()):
        if entry_id not in getattr(device, "config_entries", ()):
            continue
        replacements = set(device.identifiers)
        changed = False
        for domain, identifier in tuple(replacements):
            if domain != DOMAIN or not identifier.startswith(device_prefix):
                continue
            tail = identifier[len(device_prefix) :]
            node_text, separator, suffix = tail.partition(":zone:")
            try:
                node_identifier = stable_node_id(parent, int(node_text))
            except ValueError:
                continue
            replacements.remove((domain, identifier))
            replacements.add(
                (
                    domain,
                    f"{node_identifier}:zone:{suffix}"
                    if separator
                    else node_identifier,
                )
            )
            changed = True
        if changed:
            existing = devices.async_get_device(identifiers=replacements)
            if existing is None or existing.id == device.id:
                devices.async_update_device(device.id, new_identifiers=replacements)


def definition_for(register: RegisterCatalogEntry):
    """Return the immutable registry definition for a catalogue row."""

    return CATALOG_REGISTRY.find(register.address)


def bitfield_structure(register: RegisterCatalogEntry):
    """Return a structure only when its canonical definition has 1-bit fields."""

    definition = definition_for(register)
    wire = getattr(definition, "wire", None)
    if wire is None or getattr(wire, "is_array", False):
        return None
    name = getattr(wire, "struct_name", None)
    structure = CATALOG_REGISTRY.structure(name) if name else None
    if structure is None or not any(
        field.bit_length == 1 for field in structure.fields
    ):
        return None
    return structure


def enum_for(register: RegisterCatalogEntry):
    """Return a complete enumeration definition, if one is declared."""

    definition = definition_for(register)
    enum_name = getattr(getattr(definition, "wire", None), "enum_name", None)
    if not enum_name:
        return None
    return next(
        (item for item in CATALOG_REGISTRY.enums if item.name == enum_name), None
    )


def enum_options(
    register: RegisterCatalogEntry, language: str
) -> tuple[tuple[int, str], ...]:
    """Return only enumerations with evidence-backed labels for every value."""

    enumeration = enum_for(register)
    if enumeration is None or not enumeration.values:
        return ()
    result: list[tuple[int, str]] = []
    for value in enumeration.values:
        label = enumeration.label(value, language)
        if not label:
            return ()
        result.append((int(value), str(label)))
    return tuple(result)


def control_kind(register: RegisterCatalogEntry, language: str = "de") -> str | None:
    """Classify a writable row without inventing semantics."""

    if not register.writable or not register.readable:
        return None
    definition = definition_for(register)
    wire = getattr(definition, "wire", None)
    # In the CANopen-style array representation, :00 is the subindex-count
    # object.  It is decoded as an integer and Core deliberately rejects
    # writes to it; the writable typed control is the evidence-backed
    # concrete element (:01..:N), not the canonical array definition.
    if register.subindex == 0 and getattr(wire, "is_array", False):
        return None
    wire_type = getattr(wire, "type", None)
    if getattr(wire_type, "value", wire_type) == "ENUMERATION":
        options = enum_options(register, language)
        if not options:
            return None
        values = {value for value, _label in options}
        enum_name = str(getattr(wire, "enum_name", "")).casefold()
        labels = {label.casefold() for _value, label in options}
        bool_names = {"offon", "onoff", "yesno", "noyes", "boolean", "bool"}
        bool_labels = {
            "on",
            "off",
            "yes",
            "no",
            "ein",
            "aus",
            "ja",
            "nein",
            "enabled",
            "disabled",
        }
        if values == {0, 1} and (enum_name in bool_names or labels <= bool_labels):
            return "switch"
        return "select"
    type_name = str(getattr(wire_type, "value", wire_type) or register.datatype)
    if type_name.startswith(("UINT", "INT")):
        return "number"
    return None


def write_access_allowed(
    parent: OpenRBusCoordinator,
    register: RegisterCatalogEntry,
    effective_access_level: int | None,
) -> bool:
    """Return true only for explicitly enabled and proven write access."""

    if (
        not parent.write_enabled
        or parent.configured_write_access_level not in (1, 2, 3)
        or not register.writable
        or effective_access_level is None
        or effective_access_level < parent.configured_write_access_level
    ):
        return False
    classification = getattr(register, "write_classification", None)
    if classification is None:
        # Compatibility for older Core catalog objects and synthetic fixtures.
        safety = getattr(register, "safety", "unverified")
        classification = (
            "regular" if safety in {"validated", "source_supported"} else "experimental"
        )
    if classification == "experimental" and not getattr(
        parent, "experimental_writes", False
    ):
        return False
    if classification != "regular" and classification != "experimental":
        return False
    evidence = register.access_level_evidence.get("write", {})
    if (
        not isinstance(evidence, dict)
        or not evidence.get("known")
        or not evidence.get("complete")
    ):
        return False
    levels = [access_level(level) for level in evidence.get("levels", ())]
    # Multiple static levels mean that the catalogue cannot identify the
    # concrete device family.  Do not turn an ambiguous row into a write
    # control; Core would reject that write for the same reason.
    return (
        len(levels) == 1
        and levels[0] is not None
        and levels[0] <= effective_access_level
    )


def should_project_as_control(
    parent: OpenRBusCoordinator,
    register: RegisterCatalogEntry,
    effective_access_level: int | None,
) -> bool:
    """Use a typed HA control only when both policy and Core evidence allow writes."""

    return control_kind(register, parent.language) is not None and write_access_allowed(
        parent, register, effective_access_level
    )


def storage_bounds(storage: str) -> tuple[Decimal, Decimal] | None:
    """Return conservative raw bounds for integer storage."""

    try:
        bits = int(storage.removeprefix("UINT").removeprefix("INT"))
    except (AttributeError, ValueError):
        return None
    if bits <= 0 or bits > 32:
        return None
    if storage.startswith("INT"):
        return Decimal(-(1 << (bits - 1))), Decimal((1 << (bits - 1)) - 1)
    return Decimal(0), Decimal((1 << bits) - 1)


def number_bounds(register: RegisterCatalogEntry) -> tuple[float, float, float] | None:
    """Derive HA number bounds/step from registry constraints and wire scale."""

    definition = definition_for(register)
    constraint = getattr(definition, "constraint", None)
    declared = (
        (getattr(constraint, "minimum", None), getattr(constraint, "maximum", None))
        if constraint is not None
        else (None, None)
    )
    bounds = (
        declared
        if declared[0] is not None and declared[1] is not None
        else storage_bounds(register.storage)
    )
    if bounds is None or bounds[0] is None or bounds[1] is None:
        return None
    scale = register.scale if register.scale is not None else Decimal(1)
    if scale <= 0:
        return None
    # Registry constraints are engineering-unit limits (the same values that
    # Core's encoder validates).  Only raw storage bounds need conversion by
    # the wire gain.
    minimum, maximum = (
        (bounds[0], bounds[1])
        if declared[0] is not None and declared[1] is not None
        else (bounds[0] * scale, bounds[1] * scale)
    )
    precision = (
        getattr(constraint, "precision", None) if constraint is not None else None
    )
    step = scale if scale < 1 else Decimal(1)
    if precision is not None:
        step = max(step, Decimal(1).scaleb(-int(precision)))
    if minimum >= maximum or step <= 0:
        return None
    return float(minimum), float(maximum), float(step)


class OpenRBusRegisterEntity(CoordinatorEntity[OpenRBusPollingCoordinator]):
    """Common identity, state and safety behavior for all projections."""

    _attr_has_entity_name = True

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest a source-grounded entity ID without changing registry IDs."""

        return suggested_object_id(
            self._parent,
            self._identity,
            self._register,
            english_name=getattr(self, "_openrbus_english_name", None),
        )

    def __init__(
        self,
        parent: OpenRBusCoordinator,
        coordinator: OpenRBusPollingCoordinator,
        identity: DeviceIdentity,
        register: RegisterCatalogEntry,
        *,
        language: str = "de",
        effective_access_level: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._parent = parent
        self._identity = identity
        self._register = register
        self._language = language
        self._effective_access_level = effective_access_level
        self._attr_name = self.name_with_zone(
            register_name(register, language), parent, identity, register
        )
        self._attr_unique_id = entity_unique_id(parent, identity, register)
        self._attr_native_unit_of_measurement = register.unit

    async def async_added_to_hass(self) -> None:
        """Track entities whose zone label may be learned after startup."""

        await super().async_added_to_hass()
        self._parent._zone_entities.add(self)

    async def async_will_remove_from_hass(self) -> None:
        """Release the coordinator's reference during platform unload."""

        self._parent._zone_entities.discard(self)
        await super().async_will_remove_from_hass()

    def async_refresh_zone_name(self) -> None:
        """Apply zone labels discovered by the bounded deferred read pass."""

        self._attr_name = self.name_with_zone(
            register_name(self._register, self._language),
            self._parent,
            self._identity,
            self._register,
        )
        self.async_write_ha_state()

    @staticmethod
    def name_with_zone(name: str, parent: Any, identity: Any, register: Any) -> str:
        """Make zone-scoped register names distinguishable in HA's entity list."""

        zone_label = entity_zone_label(parent, identity, register)
        slot = zone_subindex(register, identity)
        profile = (
            profile_for(parent, identity.node, slot, identity)
            if slot is not None
            else None
        )
        if profile is not None and zone_enabled(parent, identity.node, slot):
            # The active zone is the entity's HA device, so HA already adds
            # its localized circuit label to the visible full entity name.
            # Remove only matching leading labels; never rewrite interior
            # manufacturer words such as "Heizkreisfunktion".
            prefixes = [
                f"Heizkreis {slot}",
                f"Heating circuit {slot}",
                (profile.friendly_name or "").strip(),
            ]
            for prefix in prefixes:
                if prefix and name.startswith(f"{prefix} — "):
                    name = name[len(prefix) + 3 :]
            if profile.kind is ZoneKind.HEATING:
                if parent.language == "de":
                    name = re.sub(r"^Heizkreis(?:[-\s,:]+)", "", name, count=1)
                else:
                    name = re.sub(r"^Heating circuit(?:[-\s,:]+)", "", name, count=1)
            return name
        if zone_label and zone_label.startswith("Heizkreis "):
            name = re.sub(r"^Heizkreis(?:[-\s,:]+)", "", name, count=1)
        elif zone_label and zone_label.startswith("Heating circuit "):
            name = re.sub(r"^Heating circuit(?:[-\s,:]+)", "", name, count=1)
        return f"{zone_label} — {name}" if zone_label else name

    @property
    def _result(self) -> GenericRead | Exception | None:
        return (self.coordinator.data or {}).get(
            (self._identity.node, self._register.address)
        )

    @property
    def available(self) -> bool:
        slot = zone_subindex(self._register, self._identity)
        if slot is not None and not zone_is_active(
            self._parent, self._identity.node, slot
        ):
            return False
        return (
            self._effective_access_level is not None
            and catalog_visible(self._register, self._effective_access_level)
            and self.coordinator.is_value_available(
                self._identity.node, self._register.address
            )
        )

    @property
    def device_info(self) -> DeviceInfo:
        slot = zone_subindex(self._register, self._identity)
        profile = (
            profile_for(self._parent, self._identity.node, slot, self._identity)
            if slot is not None
            else None
        )
        projected_unknown = (
            profile is not None
            and zone_projection_exists(
                self._parent, profile.node, profile.subindex, self._identity
            )
            and zone_read_state(self._parent, profile.node, profile.subindex)
            is ZoneReadState.UNKNOWN
        )
        if profile is not None and (
            zone_enabled(self._parent, profile.node, profile.subindex)
            or projected_unknown
        ):
            node_identifier = stable_node_id(self._parent, self._identity.node)
            return DeviceInfo(
                identifiers={
                    ("openrbus", f"{node_identifier}:zone:{profile.subindex}")
                },
                name=zone_device_name(profile, self._parent.language),
                manufacturer=getattr(self._identity, "manufacturer", None)
                or "OpenRBus",
                model=getattr(self._identity, "model", None)
                or getattr(self._identity, "family", None),
                via_device=("openrbus", node_identifier),
            )
        return DeviceInfo(
            identifiers={
                (
                    "openrbus",
                    stable_node_id(self._parent, self._identity.node),
                )
            },
            name=(
                getattr(self._identity, "display_name", None)
                or self._identity.name
                or f"OpenRBus node {self._identity.node}"
            ),
            manufacturer=getattr(self._identity, "manufacturer", None) or "OpenRBus",
            model=getattr(self._identity, "model", None)
            or getattr(self._identity, "family", None),
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        register = self._register
        definition = definition_for(register)
        constraint = getattr(definition, "constraint", None)
        enumeration = enum_for(register)
        return {
            "node": register.node,
            "index": register.index,
            "subindex": register.subindex,
            "object_address": str(register.address),
            "internal_code": register.internal_code,
            "datatype": register.datatype,
            "storage": register.storage,
            "enum": getattr(getattr(definition, "wire", None), "enum_name", None),
            "enum_values": list(getattr(enumeration, "values", ()) or ()),
            "enum_options": [
                {"value": value, "label": label}
                for value, label in enum_options(register, self._language)
            ],
            "min": (
                str(constraint.minimum)
                if constraint is not None
                and getattr(constraint, "minimum", None) is not None
                else None
            ),
            "max": (
                str(constraint.maximum)
                if constraint is not None
                and getattr(constraint, "maximum", None) is not None
                else None
            ),
            "precision": getattr(constraint, "precision", None),
            "scale": str(register.scale) if register.scale is not None else None,
            "unit": register.unit,
            "readable": register.readable,
            "writable": register.writable,
            "write_classification": getattr(
                register, "write_classification", "unknown"
            ),
            # Experimental opt-in follows the source authorization class;
            # ``unsafe`` below is retained for compatibility and describes
            # physical validation only.
            "experimental_write": getattr(register, "write_classification", "unknown")
            == "experimental",
            "access_level_evidence": register.access_level_evidence,
            "safety": register.safety,
            "unsafe": register.safety != "validated",
            "provenance": list(register.provenance),
            "raw_value": self._result.raw_value.hex()
            if isinstance(self._result, GenericRead)
            else "",
            "poll_group": self.coordinator.group,
            "write_enabled": self._parent.write_enabled,
            "effective_access_level": self._effective_access_level,
            "access_blocked": not write_access_allowed(
                self._parent, register, self._effective_access_level
            ),
        }

    async def _async_write(self, value: Any) -> None:
        """Write through Core and publish only the confirmed read-back value."""

        slot = zone_subindex(self._register, self._identity)
        if slot is not None and not zone_is_active(
            self._parent, self._identity.node, slot
        ):
            raise HomeAssistantError(
                "OpenRBus zone function state is not confirmed active"
            )
        if not write_access_allowed(
            self._parent, self._register, self._effective_access_level
        ):
            raise HomeAssistantError(
                "OpenRBus write access is unavailable for this register"
            )
        plan = await self._parent.async_write_object(
            self._register.address,
            value,
            node=self._identity.node,
            # The experimental acknowledgement follows source classification;
            # physical validation status is reported separately.
            allow_unsafe=getattr(
                self._register,
                "write_classification",
                "experimental" if self._register.safety == "unverified" else "regular",
            )
            == "experimental",
            verify=True,
        )
        if not plan.verified:
            raise HomeAssistantError("OpenRBus write was not read-back verified")
        readback = await self._parent.async_read_object(
            self._register.address, node=self._identity.node
        )
        self.coordinator.async_set_updated_data(
            {
                **(self.coordinator.data or {}),
                (self._identity.node, self._register.address): readback,
            }
        )


__all__ = [
    "CATALOG_REGISTRY",
    "OpenRBusRegisterEntity",
    "access_level",
    "catalog_visible",
    "cleanup_inactive_zone_entities",
    "cleanup_legacy_sensor_entities",
    "control_kind",
    "definition_for",
    "ensure_polling_coordinators",
    "entity_enabled_by_default",
    "entity_unique_id",
    "enum_for",
    "enum_options",
    "number_bounds",
    "poll_group",
    "recommended_addresses",
    "restore_migrated_sensor_entities",
    "rows_for_parent",
    "runtime_nodes",
    "write_access_allowed",
    "zone_row_enabled",
]
