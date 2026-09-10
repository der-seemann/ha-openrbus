"""Read-only OpenRBus sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OpenRBusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the first verified OpenRBus entity."""
    async_add_entities([OpenRBusDeviceTypeSensor(entry.runtime_data)])


class OpenRBusDeviceTypeSensor(CoordinatorEntity[OpenRBusCoordinator], SensorEntity):
    """Expose the live EHC device-type code from object 2001:02."""

    _attr_has_entity_name = True
    _attr_name = "Device type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OpenRBusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_2001_02"

    @property
    def native_value(self) -> int | str | None:
        return self.coordinator.data.value if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        data = self.coordinator.data
        return {
            "object_address": "2001:02",
            "bus_target": "FF",
            "raw_value": data.raw_value.hex() if data else "",
        }
