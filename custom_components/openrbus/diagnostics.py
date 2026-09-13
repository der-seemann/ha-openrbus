"""Secret-free diagnostics for the OpenRBus config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_GENERATION_ENTITY,
    CONF_RAW_READ_ACTION,
    CONF_RESPONSE_ENTITY,
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return transport and inventory metadata without credentials."""

    coordinator = entry.runtime_data
    return {
        "gateway": {
            "response_entity": entry.data.get(CONF_RESPONSE_ENTITY),
            "generation_entity": entry.data.get(CONF_GENERATION_ENTITY),
            "raw_read_available": bool(entry.data.get(CONF_RAW_READ_ACTION)),
        },
        "coordinator": {
            "cycle_id": getattr(coordinator, "_cycle_id", 0),
            "poll_lock_locked": coordinator._poll_lock.locked(),
            "last_update_success": coordinator.last_update_success,
        },
    }
