"""Read-only binary sensors for canonical registry-defined flags."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from openrbus.bitfields import decode_bitfields

from .register_entities import (
    OpenRBusRegisterEntity,
    async_apply_diagnostic_visibility,
    bitfield_structure,
    cleanup_legacy_sensor_entities,
    ensure_polling_coordinators,
    entity_enabled_by_default,
    rows_for_parent,
    zone_row_enabled,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one stable read-only entity for each registered one-bit field."""

    parent = entry.runtime_data
    async_apply_diagnostic_visibility(hass, parent)
    polling = ensure_polling_coordinators(hass, parent)
    entities: list[OpenRBusBitfieldSensor] = []
    for identity, register, group, allowed in rows_for_parent(parent):
        if not allowed or not zone_row_enabled(parent, identity, register):
            continue
        effective = parent.effective_access_levels.get(identity.node)
        structure = bitfield_structure(register)
        if structure is None or effective is None:
            continue
        for field in structure.fields:
            if field.bit_length != 1:
                continue
            entities.append(
                OpenRBusBitfieldSensor(
                    parent,
                    polling[group],
                    identity,
                    register,
                    structure,
                    field,
                    language=parent.language,
                    effective_access_level=effective,
                )
            )
    async_add_entities(entities)
    cleanup_legacy_sensor_entities(hass, parent)


class OpenRBusBitfieldSensor(OpenRBusRegisterEntity, BinarySensorEntity):
    """One registry-labelled flag decoded from its parent's packed value."""

    def __init__(self, parent, coordinator, identity, register, structure, field, **kwargs):
        super().__init__(parent, coordinator, identity, register, **kwargs)
        self._structure = structure
        self._field = field
        self._attr_name = field.label(self._language)
        self._attr_unique_id = f"{self._attr_unique_id}:bit:{field.name}"
        self._attr_entity_registry_enabled_default = entity_enabled_by_default(
            parent, identity, register, unique_id=self._attr_unique_id
        )

    @property
    def is_on(self) -> bool | None:
        result = self._result
        if result is None or isinstance(result, Exception):
            return None
        try:
            return decode_bitfields(self._structure, result.raw_value).get(self._field.name)
        except (TypeError, ValueError):
            return None
