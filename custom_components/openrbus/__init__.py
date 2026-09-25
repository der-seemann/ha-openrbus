"""OpenRBus integration for Home Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path

import voluptuous as vol
from annotatedyaml import YAMLException
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.util.yaml import Secrets

from openrbus.authorization import (
    TeaKeyComponent,
)
from openrbus.catalog import catalog_for_node
from openrbus.protocol.canip import ObjectAddress
from openrbus.transport.ble import (
    GATEWAY_AUTH_REQUEST,
    GATEWAY_AUTH_RESPONSE,
    GATEWAY_IDENT_REQUEST,
    GATEWAY_IDENT_RESPONSE,
    REQUEST_EXTENDED,
    RESPONSE_EXTENDED,
    TRANSPARENT_SERVICE,
)
from openrbus.transport.thin_gatt import ThinGattProfile

from .const import (
    BACKEND_NATIVE,
    BACKEND_THIN_RPC,
    CONF_AUTH_KEY,
    CONF_BACKEND,
    CONF_THIN_KEY_SECRET,
    CONF_THIN_PROFILE,
    DOMAIN,
)
from .coordinator import OpenRBusCoordinator

PLATFORMS = ["sensor", "number", "select", "switch"]

# The opt-in ESPHome Thin-RPC firmware exposes the same canonical EHC
# characteristics as the native Core transport.  Keep these implementation
# details out of the user-facing flow: the proxy capability selects the
# transport, while this integration supplies the known profile internally.
_DEFAULT_THIN_KEY_SECRET = "openrbus_ehc_key"
_THIN_CCCD = "00002902-0000-1000-8000-00805f9b34fb"


def _default_thin_profile() -> ThinGattProfile:
    """Return the canonical EHC profile used by the bundled Thin-RPC proxy."""

    return ThinGattProfile(
        service="6a37b97e-779d-457f-8182-edf334edd01f",
        identity=GATEWAY_IDENT_REQUEST,
        auth=GATEWAY_AUTH_REQUEST,
        notify=GATEWAY_IDENT_RESPONSE,
        roles={
            "identity": (
                "6a37b97e-779d-457f-8182-edf334edd01f",
                GATEWAY_IDENT_REQUEST,
            ),
            "identity_notify": (
                "6a37b97e-779d-457f-8182-edf334edd01f",
                GATEWAY_IDENT_RESPONSE,
            ),
            "identity_cccd": (
                "6a37b97e-779d-457f-8182-edf334edd01f",
                GATEWAY_IDENT_RESPONSE,
                _THIN_CCCD,
            ),
            "auth": (
                "6a37b97e-779d-457f-8182-edf334edd01f",
                GATEWAY_AUTH_REQUEST,
            ),
            "auth_notify": (
                "6a37b97e-779d-457f-8182-edf334edd01f",
                GATEWAY_AUTH_RESPONSE,
            ),
            "auth_cccd": (
                "6a37b97e-779d-457f-8182-edf334edd01f",
                GATEWAY_AUTH_RESPONSE,
                _THIN_CCCD,
            ),
            "request": (TRANSPARENT_SERVICE, REQUEST_EXTENDED),
            "response": (TRANSPARENT_SERVICE, RESPONSE_EXTENDED),
            "response_cccd": (TRANSPARENT_SERVICE, RESPONSE_EXTENDED, _THIN_CCCD),
        },
    )


def _thin_profile(value: object) -> ThinGattProfile | None:
    """Build Core's caller-supplied profile from validated entry data."""
    if not isinstance(value, Mapping):
        return None
    service = value.get("service")
    identity = value.get("identity")
    auth = value.get("auth")
    notify = value.get("notify")
    roles_value = value.get("roles", {})
    if not all(isinstance(item, str) and item for item in (service, identity, auth)):
        return None
    if notify is not None and (not isinstance(notify, str) or not notify):
        return None
    if not isinstance(roles_value, Mapping):
        return None
    roles: dict[str, tuple[str, ...]] = {}
    for role, uuids in roles_value.items():
        if not isinstance(role, str) or not isinstance(uuids, (list, tuple)):
            return None
        if len(uuids) not in {2, 3} or not all(
            isinstance(item, str) and item for item in uuids
        ):
            return None
        roles[role] = tuple(uuids)
    required_roles = {
        "identity",
        "identity_notify",
        "identity_cccd",
        "auth",
        "auth_notify",
        "auth_cccd",
        "request",
        "response",
        "response_cccd",
    }
    if not required_roles.issubset(roles):
        return None
    try:
        return ThinGattProfile(
            service=service,
            identity=identity,
            auth=auth,
            notify=notify,
            roles=roles,
        )
    except ValueError:
        return None


def _thin_key_provider(
    hass: HomeAssistant, secret_name: object
) -> Callable[[bytes], TeaKeyComponent] | None:
    """Resolve a four-byte EHC key from HA's existing secrets.yaml mechanism."""
    if not isinstance(secret_name, str) or not secret_name.strip():
        return None
    reference = secret_name.strip()
    try:
        Secrets(Path(hass.config.config_dir)).get(__file__, reference)
    except (OSError, ValueError, KeyError, YAMLException):
        return None

    def provider(_challenge: bytes) -> TeaKeyComponent:
        try:
            secret = Secrets(Path(hass.config.config_dir)).get(__file__, reference)
            raw = bytes.fromhex(secret.strip())
            return TeaKeyComponent.from_bytes(raw)
        except (OSError, ValueError, KeyError, YAMLException) as error:
            raise HomeAssistantError(
                "Thin-RPC EHC key secret must reference 8 hexadecimal digits in secrets.yaml"
            ) from error

    return provider


def _auth_key_provider(value: object) -> Callable[[bytes], TeaKeyComponent] | None:
    """Return a Core key provider for the explicit HA auth-key field.

    The key is deliberately passed to Core as runtime material; HA does not
    duplicate challenge/response or TEA.  The legacy secrets.yaml reference
    remains supported for existing entries.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = bytes.fromhex(value.strip())
        component = TeaKeyComponent.from_bytes(raw)
    except (ValueError, TypeError):
        return None

    def provider(_challenge: bytes) -> TeaKeyComponent:
        return component

    return provider


def _thin_runtime(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[ThinGattProfile | None, Callable[[bytes], TeaKeyComponent] | None]:
    configured = dict(entry.data)
    configured.update(getattr(entry, "options", {}))
    configured_profile = _thin_profile(configured.get(CONF_THIN_PROFILE))
    if configured.get(CONF_BACKEND) == BACKEND_THIN_RPC:
        profile = configured_profile or _default_thin_profile()
        provider = _auth_key_provider(
            configured.get(CONF_AUTH_KEY)
        ) or _thin_key_provider(
            hass, configured.get(CONF_THIN_KEY_SECRET) or _DEFAULT_THIN_KEY_SECRET
        )
    else:
        profile = configured_profile
        provider = _auth_key_provider(
            configured.get(CONF_AUTH_KEY)
        ) or _thin_key_provider(hass, configured.get(CONF_THIN_KEY_SECRET))
    return profile, provider


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
    configured = dict(entry.data)
    configured.update(getattr(entry, "options", {}))
    backend = configured.get(CONF_BACKEND)
    profile, key_provider = _thin_runtime(hass, entry)
    if backend not in {BACKEND_NATIVE, BACKEND_THIN_RPC}:
        raise HomeAssistantError(
            "Unsupported OpenRBus transport; choose Local Bluetooth or ESPHome Thin-RPC"
        )
    if backend == BACKEND_THIN_RPC and (profile is None or key_provider is None):
        raise HomeAssistantError(
            "Thin-RPC requires a valid profile and an EHC key secret reference"
        )
    coordinator = OpenRBusCoordinator(
        hass,
        entry,
        thin_key_provider=key_provider,
        thin_profile=profile,
    )
    entry.runtime_data = coordinator
    await coordinator.async_start()
    # Migrate registry rows before any platform is forwarded.  This is a
    # registry-only projection migration; it performs no transport reads or
    # writes and is safe to repeat on every reload.
    from .register_entities import cleanup_legacy_sensor_entities

    cleanup_legacy_sensor_entities(hass, coordinator)
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
                    vol.Optional("node", default=0xFF): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=255)
                    ),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def read_group(call: ServiceCall) -> dict[str, object]:
            selected = hass.config_entries.async_get_entry(call.data["entry_id"])
            if selected is None or selected.domain != DOMAIN:
                raise HomeAssistantError("Unknown OpenRBus config entry")
            target: OpenRBusCoordinator = selected.runtime_data
            try:
                addresses = tuple(
                    ObjectAddress.parse(item) for item in call.data["objects"]
                )
            except ValueError as error:
                raise HomeAssistantError("objects must use hhhh:ss notation") from error
            if not 1 <= len(addresses) <= 16:
                raise HomeAssistantError("read_group accepts 1..16 objects")
            results = await target.async_read_objects(addresses, node=call.data["node"])
            results = [
                (
                    {
                        "object": str(result.address),
                        "status": result.status,
                        "raw_response": result.raw_value.hex(),
                        "value": result.value,
                    }
                    if not isinstance(result, HomeAssistantError)
                    else {"status": "error", "error": str(result)}
                )
                for result in results
            ]
            return {"results": results}

        hass.services.async_register(
            DOMAIN,
            "read_group",
            read_group,
            schema=vol.Schema(
                {
                    vol.Required("entry_id"): str,
                    vol.Required("objects"): [str],
                    vol.Optional("node", default=0xFF): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=255)
                    ),
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def write_object(call: ServiceCall) -> dict[str, object]:
            selected = hass.config_entries.async_get_entry(call.data["entry_id"])
            if selected is None or selected.domain != DOMAIN:
                raise HomeAssistantError("Unknown OpenRBus config entry")
            target: OpenRBusCoordinator = selected.runtime_data
            try:
                address = ObjectAddress.parse(call.data["object"])
            except ValueError as error:
                raise HomeAssistantError("object must use hhhh:ss notation") from error
            plan = await target.async_write_object(
                address,
                call.data["value"],
                node=call.data["node"],
                allow_unsafe=call.data["allow_unsafe"],
                verify=call.data["verify"],
            )
            return {
                "node": plan.node,
                "object": str(plan.address),
                "verified": plan.verified,
                "required_access_level": (
                    int(plan.required_access_level)
                    if plan.required_access_level is not None
                    else None
                ),
            }

        hass.services.async_register(
            DOMAIN,
            "write_object",
            write_object,
            schema=vol.Schema(
                {
                    vol.Required("entry_id"): str,
                    vol.Required("object"): str,
                    vol.Required("value"): vol.Any(bool, int, float, str),
                    vol.Optional("node", default=0xFF): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=255)
                    ),
                    vol.Optional("allow_unsafe", default=False): cv.boolean,
                    vol.Optional("verify", default=True): cv.boolean,
                }
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )

        async def catalog(call: ServiceCall) -> dict[str, object]:
            selected = hass.config_entries.async_get_entry(call.data["entry_id"])
            if selected is None or selected.domain != DOMAIN:
                raise HomeAssistantError("Unknown OpenRBus config entry")
            target: OpenRBusCoordinator = selected.runtime_data
            node = call.data["node"]
            runtime_node = next(
                (
                    item
                    for item in (target.inventories or target.devices)
                    if getattr(
                        item,
                        "node",
                        getattr(getattr(item, "identity", None), "node", None),
                    )
                    == node
                ),
                None,
            )
            if runtime_node is None:
                raise HomeAssistantError("Node is not currently discovered")
            return {
                "node": node,
                "registers": [
                    item.as_dict() for item in catalog_for_node(runtime_node)
                ],
            }

        hass.services.async_register(
            DOMAIN,
            "catalog",
            catalog,
            schema=vol.Schema(
                {
                    vol.Required("entry_id"): str,
                    vol.Required("node"): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=255)
                    ),
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
        hass.services.async_remove(DOMAIN, "read_group")
        hass.services.async_remove(DOMAIN, "catalog")
        hass.services.async_remove(DOMAIN, "write_object")
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload OpenRBus after options change."""
    await hass.config_entries.async_reload(entry.entry_id)
