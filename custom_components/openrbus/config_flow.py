"""Config flow for the ESPHome OpenRBus proxy."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector, translation

from openrbus.transport import discover_ble_devices
from openrbus.transport.ble import TRANSPARENT_SERVICE

from .const import (
    ACCESS_LEVEL_CHOICES,
    ACCESS_LEVEL_LABELS,
    ACCESS_LEVEL_OPTIONS,
    BACKEND_NATIVE,
    BACKEND_OPTIONS,
    BACKEND_THIN_RPC,
    CONF_ACCESS_ACK,
    CONF_ACCESS_LEVEL,
    CONF_AUTH_KEY,
    CONF_BACKEND,
    CONF_BLE_DEVICE,
    CONF_BLE_SOURCE,
    CONF_FLOW_ACTION,
    CONF_LANGUAGE,
    CONF_PAIR_ACTION,
    CONF_PASSKEY,
    CONF_POLL_FAST,
    CONF_POLL_SLOW,
    CONF_POLL_STANDARD,
    CONF_THIN_CONTROLLER,
    CONF_THIN_DIAGNOSTICS_SERVICE,
    CONF_THIN_KEY_SECRET,
    CONF_THIN_POLL_SERVICE,
    CONF_THIN_PROFILE,
    CONF_THIN_REQUEST_HANDLE,
    CONF_THIN_REQUEST_SERVICE,
    CONF_THIN_RESPONSE_HANDLE,
    CONF_THIN_TARGET_ADDRESS_TYPE,
    CONF_WRITE_ENABLED,
    DEFAULT_LANGUAGE,
    DEFAULT_POLL_INTERVALS,
    DOMAIN,
    FLOW_ACTION_BACK,
    FLOW_ACTION_NEXT,
    FLOW_ACTION_OPTIONS,
    LANGUAGE_OPTIONS,
)
from .transport import (
    async_scan_thin_rpc_devices,
    controller_prefix,
    resolve_thin_rpc_capability,
    thin_rpc_controller_choices,
)

_WARNING_TRANSLATION_PREFIX = f"component.{DOMAIN}.common."
_ACCESS_WARNING_KEY = f"{_WARNING_TRANSLATION_PREFIX}access_level_warning"
_WRITE_WARNING_KEY = f"{_WARNING_TRANSLATION_PREFIX}write_access_warning"
_OPTIMIZATION_NOTE_KEY = f"{_WARNING_TRANSLATION_PREFIX}optimization_note"


def _navigation_field() -> tuple[vol.Marker, selector.SelectSelector]:
    """Return the transient, localized navigation action field.

    Home Assistant currently has no backend ``back`` result for data-entry
    forms.  A select action is the supported fallback: it is visible as
    navigation in the UI, and callers remove it before updating pending data
    or creating an entry.
    """

    return (
        vol.Required(CONF_FLOW_ACTION, default=FLOW_ACTION_NEXT),
        selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(FLOW_ACTION_OPTIONS), translation_key="flow_action"
            )
        ),
    )


def _flow_schema(fields: dict[vol.Marker, object]) -> vol.Schema:
    """Build a form schema with the transient localized navigation action."""

    action, action_selector = _navigation_field()
    return vol.Schema({**fields, action: action_selector})


def _flow_action(user_input: object) -> str:
    """Read a navigation action while treating missing values as Continue."""

    if not isinstance(user_input, Mapping):
        return FLOW_ACTION_NEXT
    return str(user_input.get(CONF_FLOW_ACTION, FLOW_ACTION_NEXT))


def _without_flow_action(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Copy submitted values without persisting the transient action."""

    return {key: value for key, value in user_input.items() if key != CONF_FLOW_ACTION}


def _normalize_access_level(value: object) -> int:
    """Convert the translated flow value to Core's numeric access level."""
    selected = ACCESS_LEVEL_LABELS.get(value, value)
    return int(selected)


def _safe_access_level(value: object, *, default: int = 1) -> int:
    """Return a supported numeric access level for form defaults.

    Options written by older versions (and some HA/frontend paths) can contain
    either a string or a translated label.  Form defaults must be one of the
    values accepted by ``vol.In``; never let an invalid persisted value break
    opening the options flow.
    """

    try:
        level = _normalize_access_level(value)
    except (TypeError, ValueError):
        return default
    return level if level in ACCESS_LEVEL_OPTIONS else default


def _warning_required(access_level: object, write_enabled: object) -> bool:
    """Return whether changing these settings requires acknowledgement."""

    try:
        level = int(access_level)
    except (TypeError, ValueError):
        level = 1
    return level >= 2 or bool(write_enabled)


def _compose_warning(
    translations: Mapping[str, str], *, access_level: object, write_enabled: object
) -> str:
    """Compose the warning from localized integration translation fragments."""

    fragments: list[str] = []
    try:
        level = int(access_level)
    except (TypeError, ValueError):
        level = 1
    if level >= 2 and (access_warning := translations.get(_ACCESS_WARNING_KEY)):
        fragments.append(access_warning)
    if bool(write_enabled):
        if write_warning := translations.get(_WRITE_WARNING_KEY):
            fragments.append(write_warning)
        if optimization_note := translations.get(_OPTIMIZATION_NOTE_KEY):
            fragments.append(optimization_note)
    return "\n\n".join(fragments)


async def _async_warning_text(
    hass, *, access_level: object, write_enabled: object
) -> str:
    """Load and compose warning fragments in the Home Assistant UI locale."""

    if not _warning_required(access_level, write_enabled):
        return ""
    language = getattr(getattr(hass, "config", None), "language", "en") or "en"
    # Core uses short locale names for backend translation resources.  Keep the
    # fallback to English inside HA's translation loader, not in Python text.
    language = str(language).split("-", 1)[0].split("_", 1)[0]
    try:
        localized = await translation.async_get_translations(
            hass, language, "common", integrations=(DOMAIN,)
        )
    except Exception:  # noqa: BLE001 - a warning must not break setup
        localized = {}
    return _compose_warning(
        localized, access_level=access_level, write_enabled=write_enabled
    )


def _valid_auth_key(value: object) -> bool:
    """Validate the eight-hex-digit Core key without a REST-incompatible schema."""

    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}", value) is not None


_UNUSABLE_BLE_NAMES = frozenset(
    {"", "unknown", "unknown device", "unnamed", "n/a", "none", "null", "ble device"}
)
_BDR_COMPANY_ID = 17474


def _usable_ble_name(value: object, address: str) -> str | None:
    """Return a meaningful advertised name, or ``None`` for a placeholder."""

    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or name.casefold() in _UNUSABLE_BLE_NAMES:
        return None
    if name.casefold() == address.strip().casefold():
        return None
    return name


def _format_ble_target_label(address: str, name: object = None) -> str:
    """Format the UI label while keeping the address as the option value."""

    # A MAC is conventionally displayed in upper case even when BlueZ or a
    # remote proxy reports lower case.  Do not alter the value used by the
    # flow: platform identifiers and UUIDs are allowed there as well.
    display_address = address.upper()
    display_name = _usable_ble_name(name, address)
    return f"{display_name} ({display_address})" if display_name else display_address


def _has_openrbus_service(info: object) -> bool:
    """Match the native transport selector and explicit original BDR guard.

    Older HA/Core test doubles may not expose ``service_uuids``.  In that
    case the metadata is unavailable and the candidate is retained; an
    explicitly empty/non-matching list is filtered.
    """

    service_uuids = getattr(info, "service_uuids", None)
    target = TRANSPARENT_SERVICE.casefold()
    if service_uuids is not None and not any(
        str(uuid).strip("{}").casefold() == target for uuid in service_uuids
    ):
        return False

    # BLEBridge's primary advertisement callback used Company ID 17474.  HA
    # exposes manufacturer data on native discovery records; apply that guard
    # only when the adapter actually supplies manufacturer metadata.  A
    # missing field is retained for compatibility with sparse HA records.
    manufacturer_data = getattr(info, "manufacturer_data", None)
    return not manufacturer_data or _BDR_COMPANY_ID in manufacturer_data


def _stable_label_name(names: Iterable[object], address: str) -> str | None:
    """Choose one advertised name independently of scan/event ordering."""

    usable = [
        name
        for value in names
        if (name := _usable_ble_name(value, address)) is not None
    ]
    return min(usable, key=lambda name: (name.casefold(), name)) if usable else None


def _choices_from_ble_items(items: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Build deterministic address-valued choices from scan records."""

    names_by_address: dict[str, tuple[str, list[object]]] = {}
    for item in items:
        address = item.get("address")
        if not isinstance(address, str) or not address.strip():
            continue
        address = str(address).strip()
        key = address.casefold()
        if key not in names_by_address:
            names_by_address[key] = (address, [])
        canonical, names = names_by_address[key]
        # The address is an opaque transport identifier, but BLE MAC strings
        # are case-insensitive.  Pick a stable spelling if adapters disagree.
        if (address.casefold(), address) < (canonical.casefold(), canonical):
            canonical = address
            names_by_address[key] = (canonical, names)
        names.append(item.get("name", ""))
    choices = {
        address: _format_ble_target_label(address, _stable_label_name(names, address))
        for address, names in names_by_address.values()
    }
    return dict(sorted(choices.items(), key=lambda pair: pair[0].casefold()))


def _native_ble_source_map(hass) -> dict[str, str]:
    """Choose the best currently advertised local adapter per target.

    A target can be visible through multiple local adapters.  Persist the
    source that supplied the strongest current advertisement so the native
    transport can resolve the matching scanner/adapter instead of silently
    falling back to Bleak's process-global default.
    """

    selected: dict[str, tuple[int, str]] = {}
    try:
        records = bluetooth.async_discovered_service_info(hass, connectable=True)
    except (AttributeError, RuntimeError, TypeError):
        # The Bluetooth integration may not be initialized while an options
        # form is rendered (and lightweight flow tests use a hass-less flow).
        # Leave the map empty; the runtime will require a later scan-resolved
        # source instead of guessing an adapter.
        return {}
    for info in records:
        if not _has_openrbus_service(info):
            continue
        address = getattr(info, "address", None)
        source = getattr(info, "source", None)
        if not isinstance(address, str) or not address.strip():
            continue
        if not isinstance(source, str) or not source.strip():
            continue
        try:
            rssi = int(getattr(info, "rssi", -127))
        except (TypeError, ValueError):
            rssi = -127
        key = address.strip().casefold()
        candidate = (rssi, source.strip())
        previous = selected.get(key)
        if (
            previous is None
            or candidate[0] > previous[0]
            or (
                candidate[0] == previous[0]
                and candidate[1].casefold() < previous[1].casefold()
            )
        ):
            selected[key] = candidate
    return {key: source for key, (_rssi, source) in selected.items()}


def _stable_scan_records(
    items: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Dedupe remote scan records by address without depending on event order."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        address = item.get("address")
        if isinstance(address, str) and address.strip():
            record = dict(item)
            record["address"] = address.strip()
            grouped.setdefault(record["address"].casefold(), []).append(record)
    selected: dict[str, dict[str, Any]] = {}
    for records in grouped.values():
        address = min(
            (str(item["address"]) for item in records),
            key=lambda value: (value.casefold(), value),
        )
        chosen = min(
            records,
            key=lambda item: (
                _usable_ble_name(item.get("name"), str(item["address"])) is None,
                (
                    _usable_ble_name(item.get("name"), str(item["address"])) or ""
                ).casefold(),
                str(item.get("address_type", "")),
                str(item.get("rssi", "")),
            ),
        )
        chosen["address"] = address
        selected[address] = chosen
    return dict(sorted(selected.items(), key=lambda pair: pair[0].casefold()))


class OpenRBusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure the verified HA -> ESPHome -> OpenRBus path."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return OpenRBusOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Start the guided setup without embedding an installation-specific target."""
        if user_input is None:
            pending = getattr(self, "_pending_user_input", None) or {}
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_BACKEND,
                            default=pending.get(CONF_BACKEND, BACKEND_NATIVE),
                        ): vol.In(BACKEND_OPTIONS),
                        vol.Required(
                            CONF_LANGUAGE,
                            default=pending.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                        ): vol.In(LANGUAGE_OPTIONS),
                    }
                ),
            )
        self._pending_user_input = dict(user_input)
        return await self.async_step_ble_target()

    @staticmethod
    def _ble_target_choices(hass, *, include_thin: bool = True):
        """Return discovered, user-visible BLE targets without hardcoded devices."""
        native_items: list[dict[str, Any]] = []
        for info in bluetooth.async_discovered_service_info(hass, connectable=True):
            if not _has_openrbus_service(info):
                continue
            address = getattr(info, "address", None) or getattr(
                getattr(info, "device", None), "address", None
            )
            name = getattr(info, "name", None) or getattr(
                getattr(info, "device", None), "name", None
            )
            if address:
                native_items.append({"address": str(address), "name": name})
        choices = _choices_from_ble_items(native_items)
        # A proxy controller is itself a valid selectable BLE route.  Its
        # controller discovery is already scoped to one complete service trio.
        if include_thin:
            for key, capability in thin_rpc_controller_choices(hass).items():
                choices.setdefault(key, key or capability.request_service)
        return dict(sorted(choices.items()))

    def _remember_native_source(self, pending: dict[str, Any]) -> None:
        """Carry the selected HA scanner source through the flow privately."""

        if pending.get(CONF_BACKEND, BACKEND_NATIVE) != BACKEND_NATIVE:
            return
        address = pending.get(CONF_BLE_DEVICE)
        if not isinstance(address, str) or not address.strip():
            return
        sources = getattr(self, "_native_ble_sources", None)
        if sources is None:
            sources = _native_ble_source_map(self.hass)
        source = sources.get(address.casefold())
        if source:
            pending[CONF_BLE_SOURCE] = source

    async def async_step_ble_target(self, user_input=None) -> ConfigFlowResult:
        pending = dict(getattr(self, "_pending_user_input", {}))
        if pending.get(CONF_BACKEND) == BACKEND_THIN_RPC:
            choices = thin_rpc_controller_choices(self.hass)
            if user_input is not None:
                if _flow_action(user_input) == FLOW_ACTION_BACK:
                    pending.update(_without_flow_action(user_input))
                    self._pending_user_input = pending
                    return await self.async_step_user()
                pending.update(_without_flow_action(user_input))
            selected = pending.get(CONF_THIN_CONTROLLER)
            if selected is None and len(choices) == 1:
                selected = next(iter(choices))
                pending[CONF_THIN_CONTROLLER] = selected
            if selected is None:
                if not choices:
                    return self.async_abort(reason="no_thin_rpc_services")
                self._pending_user_input = pending
                return self.async_show_form(
                    step_id="thin_controller",
                    data_schema=_flow_schema(
                        {vol.Required(CONF_THIN_CONTROLLER): vol.In(choices)}
                    ),
                )
            if selected not in choices:
                return self.async_abort(reason="invalid_thin_rpc_controller")
            capability = choices[selected]
            pending.update(
                {
                    CONF_THIN_REQUEST_SERVICE: capability.request_service,
                    CONF_THIN_POLL_SERVICE: capability.poll_service,
                    CONF_THIN_DIAGNOSTICS_SERVICE: capability.diagnostics_service,
                }
            )
            self._pending_user_input = pending
            return await self.async_step_thin_scan()
        if user_input is not None:
            if _flow_action(user_input) == FLOW_ACTION_BACK:
                pending.update(_without_flow_action(user_input))
                self._pending_user_input = pending
                return await self.async_step_user()
            pending.update(_without_flow_action(user_input))
            self._remember_native_source(pending)
            scan_devices = getattr(self, "_thin_scan_devices", {})
            selected = pending.get(CONF_BLE_DEVICE)
            if selected in scan_devices:
                pending[CONF_THIN_TARGET_ADDRESS_TYPE] = scan_devices[selected].get(
                    "address_type", 0
                )
            self._pending_user_input = pending
            return await self.async_step_access_level()
        backend = pending.get(CONF_BACKEND, BACKEND_NATIVE)
        choices = self._ble_target_choices(
            self.hass, include_thin=backend == BACKEND_THIN_RPC
        )
        self._native_ble_sources = _native_ble_source_map(self.hass)
        if backend == BACKEND_NATIVE and not choices:
            try:
                discovered = await discover_ble_devices(timeout=5.0)
            except (RuntimeError, OSError):
                discovered = ()
            fallback_items = []
            for device in discovered:
                address = getattr(device, "address", None)
                if address:
                    fallback_items.append(
                        {"address": str(address), "name": getattr(device, "name", None)}
                    )
            choices.update(_choices_from_ble_items(fallback_items))
        schema = (
            {vol.Required(CONF_BLE_DEVICE): vol.In(choices)}
            if choices
            else {vol.Required(CONF_BLE_DEVICE): str}
        )
        return self.async_show_form(
            step_id="ble_target", data_schema=_flow_schema(schema)
        )

    async def async_step_thin_scan(self, user_input=None) -> ConfigFlowResult:
        """Show all BLE advertisements visible behind the selected ESP proxy."""

        pending = dict(getattr(self, "_pending_user_input", {}))
        if user_input is not None:
            if _flow_action(user_input) == FLOW_ACTION_BACK:
                pending.update(_without_flow_action(user_input))
                self._pending_user_input = pending
                return await self.async_step_thin_controller()
            pending.update(_without_flow_action(user_input))
            selected = pending.get(CONF_BLE_DEVICE)
            scan_devices = getattr(self, "_thin_scan_devices", {})
            if selected in scan_devices:
                pending[CONF_THIN_TARGET_ADDRESS_TYPE] = scan_devices[selected].get(
                    "address_type", 0
                )
            self._pending_user_input = pending
            return await self.async_step_access_level()
        capability = resolve_thin_rpc_capability(
            self.hass,
            request_service=pending[CONF_THIN_REQUEST_SERVICE],
            poll_service=pending[CONF_THIN_POLL_SERVICE],
            diagnostics_service=pending[CONF_THIN_DIAGNOSTICS_SERVICE],
        )
        if not capability.available:
            return self.async_abort(reason="no_thin_rpc_services")
        try:
            devices = await async_scan_thin_rpc_devices(self.hass, capability)
        except Exception as error:  # noqa: BLE001 - config flow must show a stable abort
            _ = error
            return self.async_abort(reason="thin_rpc_scan_failed")
        scan_records = _stable_scan_records(devices)
        choices = _choices_from_ble_items(scan_records.values())
        if not choices:
            return self.async_abort(reason="thin_rpc_no_ble_devices")
        self._thin_scan_devices = scan_records
        self._pending_user_input = pending
        return self.async_show_form(
            step_id="thin_scan",
            data_schema=_flow_schema({vol.Required(CONF_BLE_DEVICE): vol.In(choices)}),
        )

    async def async_step_access_level(self, user_input=None) -> ConfigFlowResult:
        pending = dict(getattr(self, "_pending_user_input", {}))
        if user_input is None:
            return self.async_show_form(
                step_id="access_level",
                data_schema=_flow_schema(
                    {
                        vol.Required(
                            CONF_ACCESS_LEVEL,
                            default=pending.get(CONF_ACCESS_LEVEL, 1),
                        ): vol.In(ACCESS_LEVEL_CHOICES),
                        vol.Required(
                            CONF_WRITE_ENABLED,
                            default=pending.get(CONF_WRITE_ENABLED, False),
                        ): cv.boolean,
                    }
                ),
            )
        if _flow_action(user_input) == FLOW_ACTION_BACK:
            pending.update(_without_flow_action(user_input))
            self._pending_user_input = pending
            if pending.get(CONF_BACKEND) == BACKEND_THIN_RPC:
                return await self.async_step_thin_scan()
            return await self.async_step_ble_target()
        pending.update(_without_flow_action(user_input))
        # ``vol.In`` accepts the translated labels above.  Normalize the
        # label before persisting the numeric level used by Core/coordinator.
        pending[CONF_ACCESS_LEVEL] = _normalize_access_level(
            pending.get(CONF_ACCESS_LEVEL, 1)
        )
        self._pending_user_input = pending
        if not _warning_required(
            pending[CONF_ACCESS_LEVEL], pending.get(CONF_WRITE_ENABLED, False)
        ):
            return await self.async_step_credentials()
        return await self.async_step_access_warning()

    async def async_step_access_warning(self, user_input=None) -> ConfigFlowResult:
        pending = dict(getattr(self, "_pending_user_input", {}))
        if user_input is not None and _flow_action(user_input) == FLOW_ACTION_BACK:
            # Acknowledgement is deliberately invalidated when navigating
            # away.  Forward navigation must show this warning again.
            pending.pop(CONF_ACCESS_ACK, None)
            self._pending_user_input = pending
            return await self.async_step_access_level()
        if not user_input or not user_input.get(CONF_ACCESS_ACK):
            return self.async_show_form(
                step_id="access_warning",
                description_placeholders={
                    "warning": await _async_warning_text(
                        self.hass,
                        access_level=pending.get(CONF_ACCESS_LEVEL, 1),
                        write_enabled=pending.get(CONF_WRITE_ENABLED, False),
                    )
                },
                data_schema=_flow_schema(
                    {vol.Required(CONF_ACCESS_ACK, default=False): cv.boolean}
                ),
                errors={"base": "access_confirmation_required"}
                if user_input is not None
                else None,
            )
        pending[CONF_ACCESS_ACK] = True
        self._pending_user_input = pending
        return await self.async_step_credentials()

    async def async_step_credentials(self, user_input=None) -> ConfigFlowResult:
        pending = dict(getattr(self, "_pending_user_input", {}))
        if user_input is None:
            level = int(pending.get(CONF_ACCESS_LEVEL, 1))
            # Transport/profile/service metadata is deliberately not part of
            # the public flow.  It is selected from the discovered proxy
            # capability below and persisted internally.
            schema = {
                # Keep the schema JSON-serializable for HA's REST flow API.
                # Numeric/PIN validation is performed by the transport/core
                # boundary after the form is submitted.
                vol.Required(CONF_PASSKEY, default=""): cv.string,
                # The role was selected and acknowledged in the preceding
                # steps.  Level 1 intentionally has no auth-key field.
            }
            if level >= 2:
                schema[vol.Required(CONF_AUTH_KEY, default="")] = cv.string
            return self.async_show_form(
                step_id="credentials", data_schema=_flow_schema(schema)
            )
        pending.update(_without_flow_action(user_input))
        if _flow_action(user_input) == FLOW_ACTION_BACK:
            pending.pop(CONF_ACCESS_ACK, None)
            self._pending_user_input = pending
            if _warning_required(
                pending.get(CONF_ACCESS_LEVEL, 1),
                pending.get(CONF_WRITE_ENABLED, False),
            ):
                return await self.async_step_access_warning()
            return await self.async_step_access_level()
        level = int(pending.get(CONF_ACCESS_LEVEL, 1))
        if level >= 2 and not _valid_auth_key(pending.get(CONF_AUTH_KEY)):
            return self.async_show_form(
                step_id="credentials",
                data_schema=_flow_schema(
                    {
                        vol.Required(
                            CONF_PASSKEY, default=pending.get(CONF_PASSKEY, "")
                        ): cv.string,
                        vol.Required(CONF_AUTH_KEY): cv.string,
                    }
                ),
                errors={"base": "auth_key_required"},
            )
        self._pending_user_input = None
        return await self._async_create_from_input(pending)

    async def _async_create_from_input(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select the two ESPHome entities used by the read-only coordinator."""
        actions = self.hass.services.async_services().get("esphome", {})
        pair_actions = sorted(
            name for name in actions if name.endswith("openrbus_pair")
        )
        if user_input is not None:
            user_input = _without_flow_action(user_input)
            backend = user_input.get(CONF_BACKEND, BACKEND_NATIVE)
            if backend == BACKEND_THIN_RPC:
                choices = thin_rpc_controller_choices(
                    self.hass,
                    request_service=user_input.get(
                        CONF_THIN_REQUEST_SERVICE, "openrbus_gatt_rpc_request"
                    ),
                    poll_service=user_input.get(
                        CONF_THIN_POLL_SERVICE, "openrbus_gatt_rpc_poll"
                    ),
                    diagnostics_service=user_input.get(
                        CONF_THIN_DIAGNOSTICS_SERVICE,
                        "openrbus_gatt_rpc_diagnostics",
                    ),
                )
                selected = user_input.get(CONF_THIN_CONTROLLER)
                entity_prefix = controller_prefix(user_input.get(CONF_BLE_DEVICE))
                if (
                    selected is None
                    and len(choices) == 1
                    and (entity_prefix is None or entity_prefix in choices)
                ):
                    selected = next(iter(choices))
                    user_input[CONF_THIN_CONTROLLER] = selected
                if selected is None:
                    if not choices:
                        if backend == BACKEND_THIN_RPC:
                            return self.async_abort(reason="no_thin_rpc_services")
                    else:
                        self._pending_user_input = dict(user_input)
                        return self.async_show_form(
                            step_id="thin_controller",
                            data_schema=_flow_schema(
                                {vol.Required(CONF_THIN_CONTROLLER): vol.In(choices)}
                            ),
                        )
                elif selected not in choices:
                    return self.async_abort(reason="invalid_thin_rpc_controller")
                else:
                    selected_capability = choices[selected]
                    user_input[CONF_THIN_REQUEST_SERVICE] = (
                        selected_capability.request_service
                    )
                    user_input[CONF_THIN_POLL_SERVICE] = (
                        selected_capability.poll_service
                    )
                    user_input[CONF_THIN_DIAGNOSTICS_SERVICE] = (
                        selected_capability.diagnostics_service
                    )
            # Thin-GATT profile and proxy authentication provenance are
            # internal implementation details.  The selected compatible
            # service trio is the only route metadata exposed to the core.
            if backend not in BACKEND_OPTIONS:
                return self.async_abort(reason="invalid_transport")
            identity = (
                user_input.get(CONF_BLE_DEVICE)
                or user_input.get(CONF_THIN_REQUEST_SERVICE)
                or "thin_rpc"
            )
            await self.async_set_unique_id(f"esphome_bridge:{identity}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=(
                    "OpenRBus Local Bluetooth"
                    if backend == BACKEND_NATIVE
                    else "OpenRBus ESPHome Gateway"
                ),
                data={
                    CONF_PAIR_ACTION: pair_actions[0] if pair_actions else None,
                    CONF_BLE_DEVICE: user_input.get(CONF_BLE_DEVICE),
                    CONF_BLE_SOURCE: user_input.get(CONF_BLE_SOURCE),
                    CONF_THIN_TARGET_ADDRESS_TYPE: user_input.get(
                        CONF_THIN_TARGET_ADDRESS_TYPE, 0
                    ),
                    CONF_PASSKEY: user_input.get(CONF_PASSKEY) or None,
                    CONF_BACKEND: backend,
                    CONF_THIN_REQUEST_SERVICE: user_input.get(CONF_THIN_REQUEST_SERVICE)
                    or "openrbus_gatt_rpc_request",
                    CONF_THIN_POLL_SERVICE: user_input.get(CONF_THIN_POLL_SERVICE)
                    or "openrbus_gatt_rpc_poll",
                    CONF_THIN_DIAGNOSTICS_SERVICE: user_input.get(
                        CONF_THIN_DIAGNOSTICS_SERVICE
                    )
                    or "openrbus_gatt_rpc_diagnostics",
                    CONF_THIN_REQUEST_HANDLE: user_input.get(CONF_THIN_REQUEST_HANDLE)
                    or None,
                    CONF_THIN_RESPONSE_HANDLE: user_input.get(CONF_THIN_RESPONSE_HANDLE)
                    or None,
                    CONF_THIN_CONTROLLER: user_input.get(CONF_THIN_CONTROLLER),
                    # Kept for existing runtime compatibility; never
                    # collected from a normal user-facing form.
                    CONF_THIN_PROFILE: user_input.get(CONF_THIN_PROFILE) or {},
                    CONF_THIN_KEY_SECRET: user_input.get(CONF_THIN_KEY_SECRET) or None,
                    CONF_AUTH_KEY: user_input.get(CONF_AUTH_KEY) or None,
                    CONF_ACCESS_ACK: bool(user_input.get(CONF_ACCESS_ACK, False)),
                    CONF_ACCESS_LEVEL: int(user_input.get(CONF_ACCESS_LEVEL, 1)),
                    CONF_LANGUAGE: user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                    CONF_WRITE_ENABLED: bool(user_input.get(CONF_WRITE_ENABLED, False)),
                    CONF_POLL_FAST: int(
                        user_input.get(
                            CONF_POLL_FAST, DEFAULT_POLL_INTERVALS[CONF_POLL_FAST]
                        )
                    ),
                    CONF_POLL_STANDARD: int(
                        user_input.get(
                            CONF_POLL_STANDARD,
                            DEFAULT_POLL_INTERVALS[CONF_POLL_STANDARD],
                        )
                    ),
                    CONF_POLL_SLOW: int(
                        user_input.get(
                            CONF_POLL_SLOW, DEFAULT_POLL_INTERVALS[CONF_POLL_SLOW]
                        )
                    ),
                },
            )

        schema = {
            vol.Optional(CONF_BACKEND, default=BACKEND_NATIVE): vol.In(BACKEND_OPTIONS),
            vol.Optional(CONF_ACCESS_LEVEL, default=1): vol.In(ACCESS_LEVEL_OPTIONS),
            vol.Optional(CONF_WRITE_ENABLED, default=False): cv.boolean,
            vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(
                LANGUAGE_OPTIONS
            ),
            vol.Optional(
                CONF_POLL_FAST, default=DEFAULT_POLL_INTERVALS[CONF_POLL_FAST]
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
            vol.Optional(
                CONF_POLL_STANDARD, default=DEFAULT_POLL_INTERVALS[CONF_POLL_STANDARD]
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
            vol.Optional(
                CONF_POLL_SLOW, default=DEFAULT_POLL_INTERVALS[CONF_POLL_SLOW]
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
            vol.Optional(CONF_BLE_DEVICE, default=""): str,
            vol.Optional(CONF_PASSKEY, default=""): cv.string,
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(schema),
        )

    async def async_step_thin_controller(self, user_input=None):
        """Choose one complete controller trio before creating the entry."""
        if user_input is None:
            pending = dict(getattr(self, "_pending_user_input", {}))
            choices = thin_rpc_controller_choices(self.hass)
            if not choices:
                return self.async_abort(reason="thin_rpc_controller_required")
            controller_key = vol.Required(CONF_THIN_CONTROLLER)
            if pending.get(CONF_THIN_CONTROLLER):
                controller_key = vol.Required(
                    CONF_THIN_CONTROLLER,
                    default=pending[CONF_THIN_CONTROLLER],
                )
            return self.async_show_form(
                step_id="thin_controller",
                data_schema=_flow_schema({controller_key: vol.In(choices)}),
            )
        pending = dict(getattr(self, "_pending_user_input", {}))
        pending.update(_without_flow_action(user_input))
        if _flow_action(user_input) == FLOW_ACTION_BACK:
            self._pending_user_input = pending
            return await self.async_step_user()
        self._pending_user_input = pending
        return await self.async_step_ble_target()


class OpenRBusOptionsFlowHandler(OptionsFlow):
    """Explicitly opt existing entries into a different backend."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    def _remember_native_source(self, values: dict[str, Any]) -> None:
        """Persist the source matching an options-flow target selection."""

        if values.get(CONF_BACKEND, BACKEND_NATIVE) != BACKEND_NATIVE:
            return
        address = values.get(CONF_BLE_DEVICE)
        if not isinstance(address, str) or not address.strip():
            return
        sources = getattr(self, "_native_ble_sources", None)
        if sources is None:
            sources = _native_ble_source_map(self.hass)
        source = sources.get(address.casefold())
        if source:
            values[CONF_BLE_SOURCE] = source

    def _native_ble_schema(self, current: dict[str, Any]):
        """Use HA's live Bluetooth discovery for the native target field.

        The string fallback is intentional for an adapter that is temporarily
        offline; it preserves an existing target without inventing a device.
        """
        choices = OpenRBusConfigFlow._ble_target_choices(self.hass, include_thin=False)
        self._native_ble_sources = _native_ble_source_map(self.hass)
        if choices:
            return vol.In(choices)
        return str

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            user_input = _without_flow_action(user_input)
            level = _safe_access_level(user_input.get(CONF_ACCESS_LEVEL, 1))
            # Keep the persisted payload canonical even when an older
            # frontend submits a string/translated access-level value.
            user_input[CONF_ACCESS_LEVEL] = level
            if user_input.get(CONF_BACKEND, BACKEND_NATIVE) == BACKEND_NATIVE:
                # The source is intentionally hidden from the public options
                # form.  Preserve an already persisted source if the current
                # scan cannot refresh it during this edit.
                existing = getattr(self._config_entry, "options", {}).get(
                    CONF_BLE_SOURCE
                ) or getattr(self._config_entry, "data", {}).get(CONF_BLE_SOURCE)
                if existing and not user_input.get(CONF_BLE_SOURCE):
                    user_input[CONF_BLE_SOURCE] = existing
            self._remember_native_source(user_input)
            user_input.pop("_options_scan_complete", None)
            # Level 1 deliberately has no authorization key.  Remove a stale
            # value from an older entry when downgrading in Options.
            if level == 1:
                user_input.pop(CONF_AUTH_KEY, None)
            if _warning_required(
                level, user_input.get(CONF_WRITE_ENABLED, False)
            ) and not user_input.get(CONF_ACCESS_ACK):
                self._pending_options_input = dict(user_input)
                return self.async_show_form(
                    step_id="access_warning",
                    description_placeholders={
                        "warning": await _async_warning_text(
                            self.hass,
                            access_level=level,
                            write_enabled=user_input.get(CONF_WRITE_ENABLED, False),
                        )
                    },
                    data_schema=_flow_schema(
                        {vol.Required(CONF_ACCESS_ACK, default=False): cv.boolean}
                    ),
                    errors={"base": "access_confirmation_required"},
                )
            if not _warning_required(level, user_input.get(CONF_WRITE_ENABLED, False)):
                user_input[CONF_ACCESS_ACK] = False
            backend = user_input.get(CONF_BACKEND, BACKEND_NATIVE)
            if backend == BACKEND_THIN_RPC:
                choices = thin_rpc_controller_choices(
                    self.hass,
                    request_service=user_input.get(
                        CONF_THIN_REQUEST_SERVICE, "openrbus_gatt_rpc_request"
                    ),
                    poll_service=user_input.get(
                        CONF_THIN_POLL_SERVICE, "openrbus_gatt_rpc_poll"
                    ),
                    diagnostics_service=user_input.get(
                        CONF_THIN_DIAGNOSTICS_SERVICE,
                        "openrbus_gatt_rpc_diagnostics",
                    ),
                )
                selected = user_input.get(CONF_THIN_CONTROLLER) or None
                if selected is None and len(choices) == 1:
                    selected = next(iter(choices))
                if selected is None and len(choices) > 1:
                    self._pending_options_input = dict(user_input)
                    return self.async_show_form(
                        step_id="thin_controller",
                        data_schema=_flow_schema(
                            {vol.Required(CONF_THIN_CONTROLLER): vol.In(choices)}
                        ),
                    )
                if selected is not None and selected not in choices:
                    return self.async_show_form(
                        step_id="init", errors={"base": "invalid_thin_rpc_controller"}
                    )
                if selected in choices:
                    capability = choices[selected]
                    user_input[CONF_THIN_CONTROLLER] = selected
                    user_input[CONF_THIN_REQUEST_SERVICE] = capability.request_service
                    user_input[CONF_THIN_POLL_SERVICE] = capability.poll_service
                    user_input[CONF_THIN_DIAGNOSTICS_SERVICE] = (
                        capability.diagnostics_service
                    )
                    # Thin-RPC options use the same live remote BLE scan as
                    # initial setup.  If the proxy is offline, retain the
                    # existing/manual target and let the normal validation
                    # path continue.
                    if not user_input.get("_options_scan_complete"):
                        try:
                            devices = await async_scan_thin_rpc_devices(
                                self.hass, capability
                            )
                        except Exception:  # noqa: BLE001 - options must remain editable
                            devices = ()
                        scan_records = _stable_scan_records(devices)
                        scan_choices = _choices_from_ble_items(scan_records.values())
                        if scan_choices:
                            self._options_scan_devices = scan_records
                            pending = dict(user_input)
                            pending.pop("_options_scan_complete", None)
                            self._pending_options_input = pending
                            return self.async_show_form(
                                step_id="thin_scan",
                                data_schema=_flow_schema(
                                    {
                                        vol.Required(CONF_BLE_DEVICE): vol.In(
                                            scan_choices
                                        )
                                    }
                                ),
                            )
                elif backend == BACKEND_THIN_RPC:
                    return self.async_show_form(
                        step_id="init", errors={"base": "no_thin_rpc_services"}
                    )
            if level >= 2 and not _valid_auth_key(user_input.get(CONF_AUTH_KEY)):
                self._pending_options_input = dict(user_input)
                return self.async_show_form(
                    step_id="credentials",
                    data_schema=_flow_schema({vol.Required(CONF_AUTH_KEY): cv.string}),
                    errors={"base": "auth_key_required"},
                )
            return self.async_create_entry(title="", data=user_input)
        # Options are the current source of truth; entry data is the legacy
        # fallback for entries created before this setting was moved to
        # options.  Normalize only this field so unrelated option defaults are
        # preserved exactly as stored.
        current = {**self._config_entry.data, **self._config_entry.options}
        current.update(getattr(self, "_pending_options_input", {}) or {})
        current.pop(CONF_FLOW_ACTION, None)
        access_level = _safe_access_level(current.get(CONF_ACCESS_LEVEL, 1))
        self._native_ble_sources = _native_ble_source_map(self.hass)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BACKEND,
                        default=current.get(CONF_BACKEND, BACKEND_NATIVE),
                    ): vol.In(BACKEND_OPTIONS),
                    vol.Required(
                        CONF_ACCESS_LEVEL,
                        default=access_level,
                    ): vol.In(ACCESS_LEVEL_OPTIONS),
                    vol.Required(
                        CONF_LANGUAGE,
                        default=current.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                    ): vol.In(LANGUAGE_OPTIONS),
                    vol.Required(
                        CONF_WRITE_ENABLED,
                        default=current.get(CONF_WRITE_ENABLED, False),
                    ): cv.boolean,
                    vol.Required(
                        CONF_BLE_DEVICE,
                        default=current.get(CONF_BLE_DEVICE, ""),
                    ): self._native_ble_schema(current),
                    vol.Required(
                        CONF_PASSKEY,
                        default=current.get(CONF_PASSKEY, ""),
                    ): cv.string,
                    vol.Required(
                        CONF_POLL_FAST,
                        default=current.get(
                            CONF_POLL_FAST, DEFAULT_POLL_INTERVALS[CONF_POLL_FAST]
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
                    vol.Required(
                        CONF_POLL_STANDARD,
                        default=current.get(
                            CONF_POLL_STANDARD,
                            DEFAULT_POLL_INTERVALS[CONF_POLL_STANDARD],
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
                    vol.Required(
                        CONF_POLL_SLOW,
                        default=current.get(
                            CONF_POLL_SLOW, DEFAULT_POLL_INTERVALS[CONF_POLL_SLOW]
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
                }
            ),
        )

    async def async_step_access_warning(self, user_input=None):
        pending = dict(getattr(self, "_pending_options_input", {}))
        if user_input is not None and _flow_action(user_input) == FLOW_ACTION_BACK:
            pending.pop(CONF_ACCESS_ACK, None)
            self._pending_options_input = pending
            return await self.async_step_init()
        if not user_input or not user_input.get(CONF_ACCESS_ACK):
            return self.async_show_form(
                step_id="access_warning",
                description_placeholders={
                    "warning": await _async_warning_text(
                        self.hass,
                        access_level=pending.get(CONF_ACCESS_LEVEL, 1),
                        write_enabled=pending.get(CONF_WRITE_ENABLED, False),
                    )
                },
                data_schema=_flow_schema(
                    {vol.Required(CONF_ACCESS_ACK, default=False): cv.boolean}
                ),
                errors={"base": "access_confirmation_required"},
            )
        pending[CONF_ACCESS_ACK] = True
        self._pending_options_input = None
        return await self.async_step_init(pending)

    async def async_step_credentials(self, user_input=None):
        """Collect only the plaintext Core auth key for L2/L3."""
        pending = dict(getattr(self, "_pending_options_input", {}))
        if user_input is not None:
            pending.update(_without_flow_action(user_input))
            if _flow_action(user_input) == FLOW_ACTION_BACK:
                self._pending_options_input = pending
                return await self.async_step_init()
        if not user_input or not _valid_auth_key(user_input.get(CONF_AUTH_KEY)):
            return self.async_show_form(
                step_id="credentials",
                data_schema=_flow_schema({vol.Required(CONF_AUTH_KEY): cv.string}),
                errors={"base": "auth_key_required"}
                if user_input is not None
                else None,
            )
        self._pending_options_input = None
        pending.pop(CONF_FLOW_ACTION, None)
        return self.async_create_entry(title="", data=pending)

    async def async_step_thin_controller(self, user_input=None):
        if user_input is None:
            pending = dict(getattr(self, "_pending_options_input", {}))
            choices = thin_rpc_controller_choices(
                self.hass,
                request_service=pending.get(
                    CONF_THIN_REQUEST_SERVICE, "openrbus_gatt_rpc_request"
                ),
                poll_service=pending.get(
                    CONF_THIN_POLL_SERVICE, "openrbus_gatt_rpc_poll"
                ),
                diagnostics_service=pending.get(
                    CONF_THIN_DIAGNOSTICS_SERVICE,
                    "openrbus_gatt_rpc_diagnostics",
                ),
            )
            if not choices:
                return self.async_abort(reason="thin_rpc_controller_required")
            controller_key = vol.Required(CONF_THIN_CONTROLLER)
            if pending.get(CONF_THIN_CONTROLLER):
                controller_key = vol.Required(
                    CONF_THIN_CONTROLLER,
                    default=pending[CONF_THIN_CONTROLLER],
                )
            return self.async_show_form(
                step_id="thin_controller",
                data_schema=_flow_schema({controller_key: vol.In(choices)}),
            )
        pending = dict(getattr(self, "_pending_options_input", {}))
        pending.update(_without_flow_action(user_input))
        if _flow_action(user_input) == FLOW_ACTION_BACK:
            self._pending_options_input = pending
            return await self.async_step_init()
        self._pending_options_input = None
        return await self.async_step_init(pending)

    async def async_step_thin_scan(self, user_input=None):
        """Persist a target selected from the proxy's current BLE scan."""
        pending = dict(getattr(self, "_pending_options_input", {}))
        if user_input is None:
            return self.async_abort(reason="thin_rpc_scan_failed")
        pending.update(_without_flow_action(user_input))
        if _flow_action(user_input) == FLOW_ACTION_BACK:
            self._pending_options_input = pending
            return await self.async_step_thin_controller()
        item = getattr(self, "_options_scan_devices", {}).get(
            pending.get(CONF_BLE_DEVICE)
        )
        if item:
            pending[CONF_THIN_TARGET_ADDRESS_TYPE] = item.get("address_type", 0)
        pending["_options_scan_complete"] = True
        self._pending_options_input = None
        return await self.async_step_init(pending)
