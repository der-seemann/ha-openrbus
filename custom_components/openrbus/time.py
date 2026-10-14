"""Writable, source-backed HoursMinutes clock entities."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DOMAIN
from .bridge import GenericRead
from .entity_names import assign_compact_entity_id
from .hours_minutes import (
    hours_minutes_to_text,
    hours_minutes_to_time,
    is_clock_hours_minutes,
    time_to_hours_minutes,
)
from .register_entities import (
    OpenRBusRegisterEntity,
    async_apply_diagnostic_visibility,
    catalog_visible,
    ensure_polling_coordinators,
    entity_unique_id,
    number_bounds,
    rows_for_parent,
    should_project_as_control,
    zone_row_enabled,
)


def _enabled_number_row(registry, unique_id: str):
    """Find an enabled existing number row without changing its registry state."""

    entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
    if entity_id is None:
        return None
    entity = registry.entities.get(entity_id)
    if entity is None or getattr(entity, "disabled_by", None) is not None:
        return None
    return entity


def _enabled_existing_time_row(registry, unique_id: str):
    """Find an already-enabled companion even if its number was disabled."""

    entity_id = registry.async_get_entity_id("time", DOMAIN, f"{unique_id}:time")
    if entity_id is None:
        return None
    entity = registry.entities.get(entity_id)
    if entity is None or getattr(entity, "disabled_by", None) is not None:
        return None
    return entity


def _existing_time_row(registry, unique_id: str):
    """Find the persisted time companion regardless of its enabled state."""

    entity_id = registry.async_get_entity_id("time", DOMAIN, f"{unique_id}:time")
    if entity_id is None:
        return None
    return registry.entities.get(entity_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a time-picker companion only for an enabled numeric control."""

    parent = entry.runtime_data
    async_apply_diagnostic_visibility(hass, parent)
    polling = ensure_polling_coordinators(hass, parent)
    registry = er.async_get(hass)
    entities: list[OpenRBusTime] = []
    for identity, register, group, _poll_allowed in rows_for_parent(parent):
        if not register.readable or not is_clock_hours_minutes(register):
            continue
        base_unique_id = entity_unique_id(parent, identity, register)
        time_entry = _existing_time_row(registry, base_unique_id)
        if not zone_row_enabled(parent, identity, register):
            if (
                time_entry is not None
                and getattr(time_entry, "disabled_by", None) is None
            ):
                registry.async_update_entity(
                    time_entry.entity_id,
                    disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                )
            continue
        effective = parent.effective_access_levels.get(identity.node)
        if effective is None and parent.configured_access_level == 1:
            effective = 1
        if (
            effective is None
            or not catalog_visible(register, effective)
            or not should_project_as_control(parent, register, effective)
        ):
            continue

        number_entry = _enabled_number_row(registry, base_unique_id)
        existing_time_entry = _enabled_existing_time_row(registry, base_unique_id)
        # Keep the time picker inside the existing user's selected read/write
        # scope. It is a companion to the existing number entity, never a new
        # default selection or a replacement for its stable entity ID.
        if number_entry is None and existing_time_entry is None:
            continue
        bounds = number_bounds(register)
        if bounds is None or bounds != (0, 143, 1):
            continue
        if (
            number_entry is not None
            and time_entry is not None
            and getattr(time_entry, "disabled_by", None) == "integration"
        ):
            registry.async_update_entity(time_entry.entity_id, disabled_by=None)
        entities.append(
            OpenRBusTime(
                parent,
                polling[group],
                identity,
                register,
                bounds,
                language=parent.language,
                effective_access_level=effective,
                enabled_by_default=True,
            )
        )
    for entity in entities:
        assign_compact_entity_id(entity, "time")
    async_add_entities(entities)


class OpenRBusTime(OpenRBusRegisterEntity, TimeEntity):
    """A 10-minute time-picker companion retaining the raw numeric control."""

    def __init__(
        self,
        parent,
        coordinator,
        identity,
        register,
        bounds: tuple[float, float, float],
        *,
        language: str,
        effective_access_level: int,
        enabled_by_default: bool,
    ) -> None:
        super().__init__(
            parent,
            coordinator,
            identity,
            register,
            language=language,
            effective_access_level=effective_access_level,
        )
        self._hours_minutes_bounds = bounds[:2]
        suffix = "Uhrzeit" if self._language == "de" else "Time"
        self._attr_name = f"{self._attr_name} ({suffix})"
        # The original Number entity keeps its exact unique ID/entity_id.
        self._attr_unique_id = f"{self._attr_unique_id}:time"
        self._attr_entity_registry_enabled_default = enabled_by_default

    def _decode(self, value: object) -> time | None:
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        minimum, maximum = self._hours_minutes_bounds
        if not minimum <= value <= maximum:
            return None
        return hours_minutes_to_time(value)

    @property
    def native_value(self) -> time | None:
        """Return the confirmed register value in HA's native time shape."""

        result = self._result
        if not isinstance(result, GenericRead):
            return None
        return self._decode(result.value)

    @property
    def available(self) -> bool:
        """Fail closed when the wire value cannot represent a valid clock time."""

        if not super().available:
            return False
        result = self._result
        return (
            isinstance(result, GenericRead) and self._decode(result.value) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Keep both the original bytes and the raw numeric tick value visible."""

        attributes = super().extra_state_attributes
        result = self._result
        raw = result.value if isinstance(result, GenericRead) else None
        attributes["raw_numeric_value"] = raw if isinstance(raw, int) else None
        attributes["formatted_time"] = hours_minutes_to_text(raw)
        return attributes

    async def async_set_value(self, value: time) -> None:
        """Convert only exact 10-minute values and use the shared write fence."""

        try:
            raw = time_to_hours_minutes(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        minimum, maximum = self._hours_minutes_bounds
        if not minimum <= raw <= maximum:
            raise HomeAssistantError(
                "Time is outside the register's source-defined range"
            )
        await self._async_write(raw)
