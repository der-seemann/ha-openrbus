"""Writable, semantically boolean OpenRBus registers as switches."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
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
    enum_options,
    rows_for_parent,
    should_project_as_control,
    write_access_allowed,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create switches only for registry enums with proven boolean semantics."""

    parent = entry.runtime_data
    async_apply_diagnostic_visibility(hass, parent)
    polling = ensure_polling_coordinators(hass, parent)
    entities: list[OpenRBusSwitch] = []
    for identity, register, group, _poll_allowed in rows_for_parent(parent):
        effective = parent.effective_access_levels.get(identity.node)
        if effective is None and parent.configured_access_level == 1:
            effective = 1
        if control_kind(register, parent.language) != "switch" or not (
            should_project_as_control(parent, register, effective)
        ):
            continue
        options = dict(enum_options(register, parent.language))
        if set(options) != {0, 1}:
            continue
        entities.append(
            OpenRBusSwitch(
                parent,
                polling[group],
                identity,
                register,
                language=parent.language,
                effective_access_level=effective,
            )
        )
    async_add_entities(entities)
    cleanup_legacy_sensor_entities(hass, parent)


class OpenRBusSwitch(OpenRBusRegisterEntity, SwitchEntity):
    """One registry-backed 0/1 enumeration with explicit boolean semantics."""

    def __init__(self, parent, coordinator, identity, register, **kwargs):
        super().__init__(parent, coordinator, identity, register, **kwargs)
        self._attr_entity_registry_enabled_default = entity_enabled_by_default(
            parent, identity, register
        ) and write_access_allowed(parent, register, self._effective_access_level)

    @property
    def available(self) -> bool:
        # Read availability is independent of write safety.  SET remains
        # guarded by ``_async_write``.
        return super().available

    @property
    def is_on(self) -> bool | None:
        result = self._result
        value = getattr(result, "value", None)
        return bool(value) if value in (0, 1) else None

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_write(1)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_write(0)
