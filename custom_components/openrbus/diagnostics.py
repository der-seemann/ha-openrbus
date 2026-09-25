"""Secret-free diagnostics for the OpenRBus config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BACKEND


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return aggregate metadata without credentials or installation data.

    Diagnostics are commonly attached to issue reports.  Do not export node
    names, object identities, addresses, serials, service names, or exception
    messages: those values can identify a private heating installation or
    reveal local infrastructure.  Users can still report useful lifecycle
    and availability information from the aggregate fields below.
    """

    coordinator = entry.runtime_data
    devices = tuple(
        getattr(coordinator, "devices", ())
        or getattr(coordinator, "discovered_devices", ())
        or ()
    )
    discovery_error = getattr(coordinator, "discovery_error", None)
    return {
        "gateway": {
            "backend": entry.data.get(CONF_BACKEND),
        },
        "coordinator": {
            "cycle_id": getattr(coordinator, "_cycle_id", 0),
            "poll_lock_locked": coordinator._poll_lock.locked(),
            "last_update_success": coordinator.last_update_success,
            "discovery_attempted": getattr(coordinator, "_discovery_attempted", False),
            "discovery_error_type": (
                type(discovery_error).__name__ if discovery_error is not None else None
            ),
            "device_count": len(devices),
            "inventory_count": len(getattr(coordinator, "inventories", ()) or ()),
        },
    }
