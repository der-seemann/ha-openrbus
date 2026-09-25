"""Writable enumerated OpenRBus registers exposed as Home Assistant selects."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .register_entities import (
    OpenRBusRegisterEntity,
    cleanup_legacy_sensor_entities,
    control_kind,
    ensure_polling_coordinators,
    enum_options,
    rows_for_parent,
    write_access_allowed,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create only enumerations with complete registry labels."""

    parent = entry.runtime_data
    polling = ensure_polling_coordinators(hass, parent)
    entities: list[OpenRBusSelect] = []
    for identity, register, group, _poll_allowed in rows_for_parent(parent):
        if control_kind(register, parent.language) != "select":
            continue
        options = enum_options(register, parent.language)
        if not options:
            continue
        effective = parent.effective_access_levels.get(identity.node)
        if effective is None and parent.configured_access_level == 1:
            effective = 1
        entities.append(
            OpenRBusSelect(
                parent,
                polling[group],
                identity,
                register,
                options,
                language=parent.language,
                effective_access_level=effective,
            )
        )
    async_add_entities(entities)
    cleanup_legacy_sensor_entities(hass, parent)


class OpenRBusSelect(OpenRBusRegisterEntity, SelectEntity):
    """One registry-backed enumeration."""

    def __init__(self, parent, coordinator, identity, register, options, **kwargs):
        super().__init__(parent, coordinator, identity, register, **kwargs)
        self._value_to_option = dict(options)
        self._option_to_value = {label: value for value, label in options}
        self._attr_options = list(self._option_to_value)
        self._attr_entity_registry_enabled_default = write_access_allowed(
            parent, register, self._effective_access_level
        )

    @property
    def available(self) -> bool:
        # Read availability is independent of write safety.  The write gate
        # remains in ``_async_write``/``async_select_option`` so an unsafe or
        # otherwise unproven control can still show its polled value.
        return super().available

    @property
    def current_option(self) -> str | None:
        result = self._result
        value = getattr(result, "value", None)
        return self._value_to_option.get(value)

    async def async_select_option(self, option: str) -> None:
        if option not in self._option_to_value:
            raise ValueError("unknown OpenRBus enumeration option")
        await self._async_write(self._option_to_value[option])
