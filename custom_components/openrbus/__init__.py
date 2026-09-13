"""OpenRBus integration for Home Assistant."""

from __future__ import annotations

import asyncio

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from openrbus.protocol.canip import ObjectAddress

from .const import DOMAIN
from .coordinator import OpenRBusCoordinator

PLATFORMS = ["sensor"]


async def _cancel_task(task: asyncio.Task[object]) -> None:
    """Cancel and await a task from a config-entry unload callback."""
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OpenRBus from a config entry."""
    coordinator = OpenRBusCoordinator(hass, entry)
    entry.runtime_data = coordinator
    if not hass.services.has_service(DOMAIN, "read_object"):
        async def read_object(call: ServiceCall) -> dict[str, object]:
            selected = hass.config_entries.async_get_entry(call.data["entry_id"])
            if selected is None or selected.domain != DOMAIN:
                raise HomeAssistantError("Unknown OpenRBus config entry")
            target: OpenRBusCoordinator = selected.runtime_data
            try:
                address = ObjectAddress.parse(call.data["object"])
            except ValueError as error:
                raise HomeAssistantError("object must use hhhh:ss notation") from error
            result = await target.async_read_object(address, node=call.data["node"])
            return {
                "node": result.node,
                "object": str(result.address),
                "status": result.status,
                "raw_response": result.raw_value.hex(),
                "value": result.value,
            }

        hass.services.async_register(
            DOMAIN,
            "read_object",
            read_object,
            schema=vol.Schema(
                {
                    vol.Required("entry_id"): str,
                    vol.Required("object"): str,
                    vol.Optional("node", default=0xFF): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    first_refresh = hass.async_create_task(
        coordinator.async_config_entry_first_refresh()
    )

    entry.async_on_unload(lambda: _cancel_task(first_refresh))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an OpenRBus config entry."""
    coordinator: OpenRBusCoordinator = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await coordinator.async_shutdown()
    if not hass.config_entries.async_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, "read_object")
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload OpenRBus after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
