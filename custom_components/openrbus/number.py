"""Writable numeric OpenRBus registers exposed as Home Assistant numbers."""

from __future__ import annotations

from decimal import Decimal

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .register_entities import (
    OpenRBusRegisterEntity,
    async_apply_diagnostic_visibility,
    cleanup_legacy_sensor_entities,
    control_kind,
    ensure_polling_coordinators,
    entity_enabled_by_default,
    number_bounds,
    rows_for_parent,
    write_access_allowed,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create registry-backed numeric controls without probing or writing."""

    parent = entry.runtime_data
    async_apply_diagnostic_visibility(hass, parent)
    polling = ensure_polling_coordinators(hass, parent)
    entities: list[OpenRBusNumber] = []
    for identity, register, group, _poll_allowed in rows_for_parent(parent):
        if control_kind(register, parent.language) != "number":
            continue
        bounds = number_bounds(register)
        if bounds is None:
            continue
        effective = parent.effective_access_levels.get(identity.node)
        if effective is None and parent.configured_access_level == 1:
            effective = 1
        entities.append(
            OpenRBusNumber(
                parent,
                polling[group],
                identity,
                register,
                bounds,
                language=parent.language,
                effective_access_level=effective,
            )
        )
    async_add_entities(entities)
    cleanup_legacy_sensor_entities(hass, parent)


class OpenRBusNumber(OpenRBusRegisterEntity, NumberEntity):
    """One constrained numeric registry row."""

    _attr_mode = NumberMode.BOX

    def __init__(self, parent, coordinator, identity, register, bounds, **kwargs):
        super().__init__(parent, coordinator, identity, register, **kwargs)
        minimum, maximum, step = bounds
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_entity_registry_enabled_default = (
            entity_enabled_by_default(parent, identity, register)
            and write_access_allowed(parent, register, self._effective_access_level)
        )

    @property
    def available(self) -> bool:
        # Read availability is independent of write safety.  SET remains
        # guarded by ``_async_write``.
        return super().available

    @property
    def native_value(self) -> float | int | None:
        result = self._result
        if result is None or not hasattr(result, "value"):
            return None
        value = result.value
        if isinstance(value, Decimal):
            return float(value)
        return (
            value
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(value)
