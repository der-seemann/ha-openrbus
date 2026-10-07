"""Read-only OpenRBus sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from openrbus.catalog import RegisterCatalogEntry
from openrbus.discovery import DeviceIdentity
from openrbus.registry import Registry
from openrbus.value_codec import CanOpenTimeOfDay

from .bridge import GenericRead
from .const import CONF_READ_ACCESS_LEVEL, DOMAIN
from .coordinator import OpenRBusCoordinator, OpenRBusPollingCoordinator
from .entity_names import suggested_object_id
from .identity import stable_node_id, stable_object_id
from .register_entities import (
    async_apply_diagnostic_visibility,
    async_apply_entity_overrides,
    cleanup_legacy_sensor_entities,
    diagnostics_enabled,
    ensure_polling_coordinators,
    entity_enabled_by_default,
    entity_unique_id,
    entity_zone_label,
    register_name,
    restore_migrated_sensor_entities,
    rows_for_parent,
    should_project_as_control,
)
from .zones import (
    profile_for,
    zone_device_name,
    zone_enabled,
    zone_is_active,
    zone_projection_exists,
    zone_read_state,
    zone_subindex,
)

_CATALOG_REGISTRY = Registry.load_default()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up verified gateway and discovered-node entities."""

    coordinator = entry.runtime_data
    async_apply_diagnostic_visibility(hass, coordinator)
    async_apply_entity_overrides(hass, coordinator)
    show_diagnostics = diagnostics_enabled(coordinator)
    entities: list[SensorEntity] = (
        [OpenRBusDeviceTypeSensor(coordinator)] if show_diagnostics else []
    )
    configured = dict(entry.data)
    configured.update(getattr(entry, "options", {}))
    try:
        configured_level = max(
            1,
            min(
                3,
                int(
                    configured.get(
                        CONF_READ_ACCESS_LEVEL, configured.get("access_level", 1)
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        configured_level = 1
    runtime_nodes = coordinator.inventories or tuple(coordinator.devices)
    cleanup_legacy_sensor_entities(hass, coordinator)
    rows = rows_for_parent(coordinator)
    polling = ensure_polling_coordinators(hass, coordinator)
    entity_registry = er.async_get(hass)
    for runtime_node in runtime_nodes:
        identity = getattr(runtime_node, "identity", runtime_node)
        if show_diagnostics and identity.device_code is not None:
            entities.append(
                OpenRBusIdentitySensor(
                    coordinator, identity, "device_code", language=coordinator.language
                )
            )
        if show_diagnostics and identity.parameter_number is not None:
            entities.append(
                OpenRBusIdentitySensor(
                    coordinator,
                    identity,
                    "parameter_number",
                    language=coordinator.language,
                )
            )
    # Register every readable row, but let typed control platforms own rows
    # whose registry metadata proves a Number/Select/Switch projection.
    for identity, register, group, _poll_allowed in rows:
        effective_level = coordinator.effective_access_levels.get(identity.node)
        if effective_level is None and coordinator.configured_access_level == 1:
            effective_level = 1
        if not register.readable or should_project_as_control(
            coordinator, register, effective_level
        ):
            continue
        # A legacy typed projection may already own this stable object ID even
        # when the expanded Core catalogue no longer has enough wire evidence
        # to classify the row again.  Do not add a second sensor with the same
        # unique ID; retain the existing typed registry row instead.
        unique_id = entity_unique_id(coordinator, identity, register)
        if any(
            entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
            for platform in ("number", "select", "switch")
        ):
            continue
        entities.append(
            OpenRBusRegisterSensor(
                coordinator,
                polling[group],
                identity,
                register,
                language=coordinator.language,
                enabled_by_default=entity_enabled_by_default(
                    coordinator, identity, register
                ),
                effective_access_level=(
                    coordinator.effective_access_levels.get(identity.node)
                    if coordinator.effective_access_levels.get(identity.node)
                    is not None
                    else (1 if configured_level == 1 else None)
                ),
            )
        )
    async_add_entities(entities)
    restore_migrated_sensor_entities(hass, coordinator)


def _poll_group(register: RegisterCatalogEntry, recommended: frozenset) -> str:
    """Assign a stable group using catalog semantics, never address lists.

    Recommendations are Core registry metadata.  The remaining fallback uses
    the catalog's names/unit/code fields so new object addresses automatically
    receive a group without an HA-maintained register table.
    """

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


def _recommended_addresses(identity: DeviceIdentity | Any) -> frozenset:
    """Resolve conservative defaults from Core's registry, never HA addresses."""

    try:
        resolution = getattr(identity, "registry_resolution", None)
        family = getattr(resolution, "family", None) or getattr(
            identity, "family", None
        )
        return frozenset(
            item.address
            for item in _CATALOG_REGISTRY.recommended_registers(device_family=family)
        )
    except (AttributeError, TypeError, ValueError):
        # Older Core wheels remain usable; no recommendation is safer than a
        # guessed enablement when the registry API is unavailable.
        return frozenset()


def _catalog_visible(register: RegisterCatalogEntry, max_access_level: int) -> bool:
    """Return whether a readable row may be polled at the effective level.

    Missing access/channel evidence is deliberately fail-closed.  The row is
    still registered from the Core catalogue, but it is not polled or exposed
    as available until Core reports an effective level and matching read
    evidence.
    """

    if not register.readable:
        return False
    evidence = register.access_level_evidence.get("read", {})
    levels = evidence.get("levels", ()) if isinstance(evidence, dict) else ()
    if not levels:
        return False
    numeric = {
        "level 0": 0,
        "user": 1,
        "installer": 2,
        "professional": 3,
    }
    return any(
        numeric.get(str(level).lower(), 99) <= max_access_level for level in levels
    )


def _entity_enabled_by_default(
    register: RegisterCatalogEntry, recommended: frozenset
) -> bool:
    """Enable only conservative, useful defaults from a complete catalog.

    Discovery keeps every readable capability in the entity registry so users
    can opt into the complete device-specific catalog.  New catalog rows are
    otherwise disabled to avoid a large, noisy entity set and accidental
    polling.  Core recommendations remain authoritative; as a fallback,
    scalar values with explicit level-0/user read evidence are the small
    basic slice users can safely see immediately.
    """

    if not register.readable:
        return False
    if register.address in recommended:
        return True
    if register.datatype in {"STRUCT", "OCTETSTRING"}:
        return False
    evidence = register.access_level_evidence.get("read", {})
    levels = evidence.get("levels", ()) if isinstance(evidence, dict) else ()
    return any(str(level).casefold() in {"level 0", "user"} for level in levels)


class OpenRBusDeviceTypeSensor(CoordinatorEntity[OpenRBusCoordinator], SensorEntity):
    """Expose the live EHC device-type code from object 2001:02."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def suggested_object_id(self) -> str:
        """Keep the gateway diagnostic ID language-neutral."""

        return "openrbus_device_type"

    def __init__(self, coordinator: OpenRBusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Device type" if coordinator.language == "en" else "Gerätetyp"
        self._attr_unique_id = stable_object_id(coordinator, 0xFF, 0x2001, 0x02)

    @property
    def native_value(self) -> int | str | None:
        return self.coordinator.data.value if self.coordinator.data else None

    @property
    def available(self) -> bool:
        """Keep the last valid value visible across one transient poll error."""
        return self.coordinator.data is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "object_address": "2001:02",
            "bus_target": "FF",
            "raw_value": data.raw_value.hex() if data else "",
            "poll_success": self.coordinator.last_update_success,
        }


class OpenRBusIdentitySensor(CoordinatorEntity[OpenRBusCoordinator], SensorEntity):
    """Expose one stable, read-only identity value for a discovered bus node."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def suggested_object_id(self) -> str:
        """Return a node-qualified English suffix for Home Assistant."""

        field_name = {
            "device_code": "device_identity",
            "parameter_number": "parameter_number",
        }[self._field]
        return f"node_{self._identity.node}_{field_name}"

    def __init__(
        self,
        coordinator: OpenRBusCoordinator,
        identity,
        field: str,
        *,
        language: str = "de",
    ) -> None:
        super().__init__(coordinator)
        self._identity = identity
        self._field = field
        labels = {
            "device_code": ("Device type", "Gerätetyp"),
            "parameter_number": ("Parameter number", "Parameternummer"),
        }
        english, german = labels[field]
        self._attr_name = english if language == "en" else german
        self._attr_unique_id = (
            f"{stable_node_id(coordinator, identity.node)}:identity:{field}"
        )

    @property
    def native_value(self) -> int | None:
        return getattr(self._identity, self._field)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={
                (
                    "openrbus",
                    stable_node_id(self.coordinator, self._identity.node),
                )
            },
            name=_identity_display_name(self._identity),
            manufacturer=getattr(self._identity, "manufacturer", None) or "OpenRBus",
            model=getattr(self._identity, "model", None)
            or getattr(self._identity, "family", None)
            or (
                f"Device {self._identity.device_code}"
                if self._identity.device_code is not None
                else None
            ),
        )


class OpenRBusRegisterSensor(
    CoordinatorEntity[OpenRBusPollingCoordinator], SensorEntity
):
    """One read-only register projected from the node's public catalog.

    Register entities subscribe to one configurable group coordinator.  A
    group performs one serialized batch per node and all entities in that
    group share its polling interval.
    """

    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False

    @property
    def suggested_object_id(self) -> str | None:
        """Suggest the same readable identity format as register controls."""

        return suggested_object_id(self._parent, self._identity, self._register)

    def __init__(
        self,
        parent: OpenRBusCoordinator,
        coordinator: OpenRBusPollingCoordinator,
        identity: DeviceIdentity,
        register: RegisterCatalogEntry,
        *,
        language: str = "de",
        enabled_by_default: bool = False,
        effective_access_level: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._parent = parent
        self._identity = identity
        self._register = register
        self._attr_entity_registry_enabled_default = enabled_by_default
        self._language = language
        self._effective_access_level = effective_access_level
        self._attr_name = register_name(register, language)
        if zone_label := entity_zone_label(parent, identity, register):
            self._attr_name = f"{zone_label} — {self._attr_name}"
        if register.datatype == "TIME_OF_DAY":
            # HA has no time-only sensor device class/native value, so publish
            # a stable clock string. Keep the standardized protocol date
            # separately; it has no timezone semantics and is not a timestamp.
            self._attr_native_unit_of_measurement = None
        else:
            self._attr_native_unit_of_measurement = register.unit
        address = register.address
        # Node number is a stable protocol identity.  Include the concrete
        # object address, never a localized/display name, in the unique ID.
        self._attr_unique_id = stable_object_id(
            parent, identity.node, address.index, address.subindex
        )

    @property
    def native_value(self) -> Any:
        result = self._result
        if not isinstance(result, GenericRead):
            return None
        if isinstance(result.value, CanOpenTimeOfDay):
            return _time_of_day_native_value(result.value)
        return result.value

    @property
    def available(self) -> bool:
        slot = zone_subindex(self._register, self._identity)
        if slot is not None and not zone_is_active(
            self._parent, self._identity.node, slot
        ):
            return False
        return (
            self._effective_access_level is not None
            and _catalog_visible(self._register, self._effective_access_level)
            and self.coordinator.is_value_available(
                self._identity.node, self._register.address
            )
        )

    @property
    def _result(self) -> GenericRead | Exception | None:
        return (self.coordinator.data or {}).get(
            (self._identity.node, self._register.address)
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
            and zone_read_state(self._parent, profile.node, profile.subindex).value
            == "unknown"
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
            name=_identity_display_name(self._identity),
            manufacturer=getattr(self._identity, "manufacturer", None) or "OpenRBus",
            model=getattr(self._identity, "model", None)
            or getattr(self._identity, "family", None)
            or (
                f"Device {self._identity.device_code}"
                if self._identity.device_code is not None
                else None
            ),
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        register = self._register
        return {
            "node": register.node,
            "index": register.index,
            "subindex": register.subindex,
            "object_address": str(register.address),
            "internal_code": register.internal_code,
            "name_de": register.name_de,
            "name_en": register.name_en,
            "datatype": register.datatype,
            "storage": register.storage,
            "scale": str(register.scale) if register.scale is not None else None,
            "unit": register.unit,
            "readable": register.readable,
            "writable": register.writable,
            "access_level_evidence": register.access_level_evidence,
            "safety": register.safety,
            "provenance": list(register.provenance),
            "raw_value": self._result.raw_value.hex()
            if isinstance(self._result, GenericRead)
            else "",
            "protocol_day_counter": self._result.value.days
            if isinstance(self._result, GenericRead)
            and isinstance(self._result.value, CanOpenTimeOfDay)
            else None,
            "protocol_date": self._result.value.protocol_date.isoformat()
            if isinstance(self._result, GenericRead)
            and isinstance(self._result.value, CanOpenTimeOfDay)
            else None,
            "poll_group": self.coordinator.group,
            "write_enabled": self._parent.write_enabled,
            "effective_access_level": self._effective_access_level,
            "access_blocked": not (
                self._effective_access_level is not None
                and _catalog_visible(self._register, self._effective_access_level)
            ),
        }


def _time_of_day_native_value(value: CanOpenTimeOfDay) -> str:
    """Format a time-only value without inventing date or timezone meaning."""

    seconds, milliseconds = divmod(value.milliseconds, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _identity_display_name(identity: DeviceIdentity) -> str:
    """Use Core's evidence-backed label with a stable node fallback."""

    display_name = getattr(identity, "display_name", None)
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return identity.name or f"OpenRBus node {identity.node}"
