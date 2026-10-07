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

from .access_storage import (
    async_load_access_profile,
    async_save_access_profile,
    normalize_mac,
)
from .const import (
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
    CONF_BLE_NAME,
    CONF_BLE_SOURCE,
    CONF_COOLING_ENABLED,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_ENTITY_OVERRIDES,
    CONF_EXPERIMENTAL_WRITES,
    CONF_FLOW_ACTION,
    CONF_GROUP_OVERRIDES,
    CONF_INVALID_VALUE_DISABLE_AFTER,
    CONF_LANGUAGE,
    CONF_NODE_OVERRIDES,
    CONF_PAIR_ACTION,
    CONF_PASSKEY,
    CONF_POLL_FAST,
    CONF_POLL_SLOW,
    CONF_POLL_STANDARD,
    CONF_READ_ACCESS_LEVEL,
    CONF_SCREED_DRYING_ENABLED,
    CONF_THIN_CONTROLLER,
    CONF_THIN_DIAGNOSTICS_SERVICE,
    CONF_THIN_KEY_SECRET,
    CONF_THIN_POLL_SERVICE,
    CONF_THIN_PROFILE,
    CONF_THIN_REQUEST_HANDLE,
    CONF_THIN_REQUEST_SERVICE,
    CONF_THIN_RESPONSE_HANDLE,
    CONF_THIN_TARGET_ADDRESS_TYPE,
    CONF_TRANSPORT_MIGRATION,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
    CONF_ZONE_OVERRIDES,
    DEFAULT_COOLING_ENABLED,
    DEFAULT_DIAGNOSTICS_ENABLED,
    DEFAULT_EXPERIMENTAL_WRITES,
    DEFAULT_INVALID_VALUE_DISABLE_AFTER,
    DEFAULT_LANGUAGE,
    DEFAULT_POLL_INTERVALS,
    DEFAULT_SCREED_DRYING_ENABLED,
    DOMAIN,
    FLOW_ACTION_BACK,
    FLOW_ACTION_NEXT,
    FLOW_ACTION_OPTIONS,
    LANGUAGE_OPTIONS,
    WRITE_ACCESS_LEVEL_OPTIONS,
)
from .register_entities import (
    bitfield_structure,
    control_kind,
    entity_category,
    entity_category_key,
    entity_category_override_keys,
    entity_enabled_by_default,
    entity_group_key,
    entity_unique_id,
    register_name,
    rows_for_parent,
    write_access_allowed,
    zone_row_enabled,
)
from .transport import (
    async_scan_thin_rpc_devices,
    controller_prefix,
    resolve_thin_rpc_capability,
    thin_rpc_controller_choices,
)
from .zones import override_key, zone_device_name

ESPHOME_PROXY_SETUP_URL = (
    "https://github.com/der-seemann/openrbus/blob/v0.4.4/"
    "tools/phase1a/esphome/README.md"
)

_CONF_ENTITY_PICKER = "_openrbus_entity_picker"
_CONF_ENTITY_NODES = "_openrbus_entity_nodes"
_CONF_ENTITY_NODE_PRESET = "_openrbus_entity_node_preset"
_CONF_ENTITY_GROUPS = "_openrbus_entity_groups"
_CONF_ENTITY_GROUP_PRESET = "_openrbus_entity_group_preset"
_CONF_ENTITY_GROUP_EDIT = "_openrbus_entity_group_edit"
_CONF_ENTITY_GROUP_ACTION = "_openrbus_entity_group_action"
_CONF_ENTITY_ITEMS = "_openrbus_entity_items"
_CONF_ENTITY_SEARCH = "_openrbus_entity_search"
_CONF_ENTITY_PRESET = "_openrbus_entity_preset"
_CONF_ENTITY_ITEM_NAV = "_openrbus_entity_item_nav"
_ENTITY_PICKER_ACTIONS = ("edit", "done")
_ENTITY_PRESETS = ("keep", "default", "all", "none")

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


def _access_level_slider(minimum: int, maximum: int) -> selector.NumberSelector:
    """Return an integer slider; translations explain each numeric level."""

    return selector.NumberSelector(
        {
            "min": minimum,
            "max": maximum,
            "step": 1,
            "mode": selector.NumberSelectorMode.SLIDER,
        }
    )


def _flow_action(user_input: object) -> str:
    """Read a navigation action while treating missing values as Continue."""

    if not isinstance(user_input, Mapping):
        return FLOW_ACTION_NEXT
    return str(user_input.get(CONF_FLOW_ACTION, FLOW_ACTION_NEXT))


def _without_flow_action(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Copy submitted values without persisting the transient action."""

    return {key: value for key, value in user_input.items() if key != CONF_FLOW_ACTION}


def _transport_route(values: Mapping[str, Any]) -> tuple[object, ...]:
    """Return only the settings which identify a physical transport route."""

    backend = values.get(CONF_BACKEND, BACKEND_NATIVE)
    if backend == BACKEND_NATIVE:
        return (backend, values.get(CONF_BLE_SOURCE))
    return (
        backend,
        values.get(CONF_THIN_CONTROLLER),
        values.get(CONF_THIN_REQUEST_SERVICE),
        values.get(CONF_THIN_POLL_SERVICE),
        values.get(CONF_THIN_DIAGNOSTICS_SERVICE),
    )


def _transport_switch_is_safe(
    current: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """Allow a retained-identity switch only for the same canonical BLE MAC.

    Entity IDs and HA history are intentionally retained by keeping the same
    config entry.  That is correct only when both routes terminate at the
    same heating gateway.  Unknown/non-MAC targets fail closed instead of
    accidentally rebinding an existing entry to another installation.
    """

    old_mac = normalize_mac(current.get(CONF_BLE_DEVICE))
    new_mac = normalize_mac(candidate.get(CONF_BLE_DEVICE))
    return old_mac is not None and old_mac == new_mac


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


def _read_access_level(values: Mapping[str, Any]) -> int:
    """Read the new independent policy with the legacy read-level fallback."""

    return _safe_access_level(
        values.get(CONF_READ_ACCESS_LEVEL, values.get(CONF_ACCESS_LEVEL, 1))
    )


def _write_access_level(values: Mapping[str, Any]) -> int:
    """Read the explicit write policy; old entries retain their old behavior."""

    value = values.get(CONF_WRITE_ACCESS_LEVEL, values.get(CONF_ACCESS_LEVEL, 1))
    if value == "Kein Schreibzugriff (0)":
        return 0
    value = ACCESS_LEVEL_LABELS.get(value, value)
    try:
        level = int(value)
    except (TypeError, ValueError):
        return 1
    return level if level in WRITE_ACCESS_LEVEL_OPTIONS else 1


def _normalize_access_policies(values: dict[str, Any]) -> None:
    """Canonicalize independent policies while retaining the legacy read alias."""

    read_level = _read_access_level(values)
    values[CONF_READ_ACCESS_LEVEL] = read_level
    values[CONF_WRITE_ACCESS_LEVEL] = _write_access_level(values)
    values[CONF_ACCESS_LEVEL] = read_level


async def _async_apply_mac_profile(hass, values: dict[str, Any]) -> None:
    """Prefill only missing setup values from the selected gateway's profile."""

    profile = await async_load_access_profile(hass, values.get(CONF_BLE_DEVICE))
    for key, value in profile.items():
        if values.get(key) in (None, ""):
            values[key] = value
    _normalize_access_policies(values)


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
    """Load warning fragments in the HA locale, with an English fallback.

    Home Assistant's integration translations are allowed to be incomplete
    while a locale is being introduced.  Permission warnings must never turn
    into an empty acknowledgement page in that case, so fill only missing
    fragments from the canonical English strings.
    """

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
    required = {_ACCESS_WARNING_KEY} if _safe_access_level(access_level) >= 2 else set()
    if bool(write_enabled):
        required.update({_WRITE_WARNING_KEY, _OPTIMIZATION_NOTE_KEY})
    missing = required.difference(localized)
    if missing and language != "en":
        try:
            english = await translation.async_get_translations(
                hass, "en", "common", integrations=(DOMAIN,)
            )
        except Exception:  # noqa: BLE001 - warnings must not break setup
            english = {}
        localized = {**english, **localized}
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
    return (
        f"{display_name} — MAC {display_address}" if display_name else display_address
    )


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


def _remember_ble_name(
    hass,
    values: dict[str, Any],
    *,
    scanned: Mapping[str, Mapping[str, Any]] | None = None,
    previous: Mapping[str, Any] | None = None,
) -> None:
    """Persist only the advertised name matching the currently selected MAC."""

    address = values.get(CONF_BLE_DEVICE)
    if not isinstance(address, str) or not address.strip():
        values.pop(CONF_BLE_NAME, None)
        return
    name = None
    if scanned:
        name = _stable_label_name(
            (
                record.get("name")
                for key, record in scanned.items()
                if key.casefold() == address.casefold()
                or str(record.get("address", "")).casefold() == address.casefold()
            ),
            address,
        )
    if name is None and values.get(CONF_BACKEND, BACKEND_NATIVE) == BACKEND_NATIVE:
        discovered_names = []
        for info in bluetooth.async_discovered_service_info(hass, connectable=True):
            candidate = getattr(info, "address", None) or getattr(
                getattr(info, "device", None), "address", None
            )
            if (
                isinstance(candidate, str)
                and candidate.casefold() == address.casefold()
            ):
                discovered_names.extend(
                    (
                        getattr(info, "name", None),
                        getattr(getattr(info, "device", None), "name", None),
                    )
                )
        name = _stable_label_name(discovered_names, address)
    if name is None and previous:
        old_address = previous.get(CONF_BLE_DEVICE)
        if (
            isinstance(old_address, str)
            and old_address.casefold() == address.casefold()
        ):
            name = _usable_ble_name(previous.get(CONF_BLE_NAME), address)
    if name:
        values[CONF_BLE_NAME] = name
    else:
        values.pop(CONF_BLE_NAME, None)


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
                description_placeholders={
                    "esphome_proxy_setup_url": ESPHOME_PROXY_SETUP_URL
                },
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
            scan_devices = getattr(self, "_thin_scan_devices", {})
            selected = pending.get(CONF_BLE_DEVICE)
            _remember_ble_name(self.hass, pending, scanned=scan_devices)
            self._remember_native_source(pending)
            if selected in scan_devices:
                pending[CONF_THIN_TARGET_ADDRESS_TYPE] = scan_devices[selected].get(
                    "address_type", 0
                )
            await _async_apply_mac_profile(self.hass, pending)
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
        selected = pending.get(CONF_BLE_DEVICE)
        if isinstance(selected, str) and selected.strip():
            choices.setdefault(
                selected,
                _format_ble_target_label(selected, pending.get(CONF_BLE_NAME)),
            )
        schema = (
            {
                vol.Required(
                    CONF_BLE_DEVICE,
                    default=pending.get(CONF_BLE_DEVICE, next(iter(choices))),
                ): vol.In(choices)
            }
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
            _remember_ble_name(self.hass, pending, scanned=scan_devices)
            if selected in scan_devices:
                pending[CONF_THIN_TARGET_ADDRESS_TYPE] = scan_devices[selected].get(
                    "address_type", 0
                )
            await _async_apply_mac_profile(self.hass, pending)
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
            data_schema=_flow_schema(
                {
                    vol.Required(
                        CONF_BLE_DEVICE,
                        default=pending.get(CONF_BLE_DEVICE, next(iter(choices))),
                    ): vol.In(choices)
                }
            ),
        )

    async def async_step_access_level(self, user_input=None) -> ConfigFlowResult:
        pending = dict(getattr(self, "_pending_user_input", {}))
        if user_input is None:
            return self.async_show_form(
                step_id="access_level",
                data_schema=_flow_schema(
                    {
                        vol.Required(
                            CONF_READ_ACCESS_LEVEL,
                            default=_read_access_level(pending),
                        ): _access_level_slider(1, 3),
                        vol.Required(
                            CONF_WRITE_ACCESS_LEVEL,
                            default=_write_access_level(pending),
                        ): _access_level_slider(0, 3),
                        vol.Required(
                            CONF_WRITE_ENABLED,
                            default=pending.get(CONF_WRITE_ENABLED, False),
                        ): cv.boolean,
                        vol.Required(
                            CONF_DIAGNOSTICS_ENABLED,
                            default=pending.get(
                                CONF_DIAGNOSTICS_ENABLED,
                                DEFAULT_DIAGNOSTICS_ENABLED,
                            ),
                        ): cv.boolean,
                        vol.Required(
                            CONF_SCREED_DRYING_ENABLED,
                            default=pending.get(
                                CONF_SCREED_DRYING_ENABLED,
                                DEFAULT_SCREED_DRYING_ENABLED,
                            ),
                        ): cv.boolean,
                        vol.Required(
                            CONF_COOLING_ENABLED,
                            default=pending.get(
                                CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED
                            ),
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
        _normalize_access_policies(pending)
        self._pending_user_input = pending
        if not _warning_required(
            max(_read_access_level(pending), _write_access_level(pending)),
            pending.get(CONF_WRITE_ENABLED, False),
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
            level = max(_read_access_level(pending), _write_access_level(pending))
            # Transport/profile/service metadata is deliberately not part of
            # the public flow.  It is selected from the discovered proxy
            # capability below and persisted internally.
            schema = {
                # Keep the schema JSON-serializable for HA's REST flow API.
                # Numeric/PIN validation is performed by the transport/core
                # boundary after the form is submitted.
                vol.Required(
                    CONF_PASSKEY, default=pending.get(CONF_PASSKEY, "")
                ): cv.string,
                # The role was selected and acknowledged in the preceding
                # steps.  Level 1 intentionally has no auth-key field.
            }
            if level >= 2:
                schema[
                    vol.Required(CONF_AUTH_KEY, default=pending.get(CONF_AUTH_KEY, ""))
                ] = cv.string
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
        level = max(_read_access_level(pending), _write_access_level(pending))
        if level >= 2 and not _valid_auth_key(pending.get(CONF_AUTH_KEY)):
            return self.async_show_form(
                step_id="credentials",
                data_schema=_flow_schema(
                    {
                        vol.Required(
                            CONF_PASSKEY, default=pending.get(CONF_PASSKEY, "")
                        ): cv.string,
                        vol.Required(
                            CONF_AUTH_KEY, default=pending.get(CONF_AUTH_KEY, "")
                        ): cv.string,
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
            _normalize_access_policies(user_input)
            await async_save_access_profile(self.hass, user_input)
            return self.async_create_entry(
                title=(
                    "OpenRBus Local Bluetooth"
                    if backend == BACKEND_NATIVE
                    else "OpenRBus ESPHome Gateway"
                ),
                data={
                    CONF_PAIR_ACTION: pair_actions[0] if pair_actions else None,
                    CONF_BLE_DEVICE: user_input.get(CONF_BLE_DEVICE),
                    CONF_BLE_NAME: user_input.get(CONF_BLE_NAME),
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
                    CONF_ACCESS_LEVEL: _read_access_level(user_input),
                    CONF_READ_ACCESS_LEVEL: _read_access_level(user_input),
                    CONF_WRITE_ACCESS_LEVEL: _write_access_level(user_input),
                    CONF_LANGUAGE: user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                    CONF_WRITE_ENABLED: bool(user_input.get(CONF_WRITE_ENABLED, False)),
                    CONF_EXPERIMENTAL_WRITES: bool(
                        user_input.get(CONF_EXPERIMENTAL_WRITES, False)
                    ),
                    CONF_DIAGNOSTICS_ENABLED: bool(
                        user_input.get(
                            CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                        )
                    ),
                    CONF_SCREED_DRYING_ENABLED: bool(
                        user_input.get(
                            CONF_SCREED_DRYING_ENABLED,
                            DEFAULT_SCREED_DRYING_ENABLED,
                        )
                    ),
                    CONF_COOLING_ENABLED: bool(
                        user_input.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED)
                    ),
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
                    CONF_INVALID_VALUE_DISABLE_AFTER: int(
                        user_input.get(
                            CONF_INVALID_VALUE_DISABLE_AFTER,
                            DEFAULT_INVALID_VALUE_DISABLE_AFTER,
                        )
                    ),
                },
            )

        schema = {
            vol.Optional(CONF_BACKEND, default=BACKEND_NATIVE): vol.In(BACKEND_OPTIONS),
            vol.Optional(CONF_ACCESS_LEVEL, default=1): vol.In(ACCESS_LEVEL_OPTIONS),
            vol.Optional(CONF_WRITE_ENABLED, default=False): cv.boolean,
            vol.Optional(
                CONF_EXPERIMENTAL_WRITES, default=DEFAULT_EXPERIMENTAL_WRITES
            ): cv.boolean,
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
            vol.Optional(
                CONF_INVALID_VALUE_DISABLE_AFTER,
                default=DEFAULT_INVALID_VALUE_DISABLE_AFTER,
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=31536000)),
            vol.Optional(CONF_BLE_DEVICE, default=""): str,
            vol.Optional(CONF_PASSKEY, default=""): cv.string,
        }
        return self.async_show_form(
            step_id="user",
            description_placeholders={
                "esphome_proxy_setup_url": ESPHOME_PROXY_SETUP_URL
            },
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
        selected = current.get(CONF_BLE_DEVICE)
        if isinstance(selected, str) and selected.strip():
            choices.setdefault(
                selected,
                _format_ble_target_label(selected, current.get(CONF_BLE_NAME)),
            )
        self._native_ble_sources = _native_ble_source_map(self.hass)
        if choices:
            return vol.In(choices)
        return str

    def _zone_choices(self) -> dict[str, str]:
        """Build an explicit, persisted selection only from discovered slots."""

        runtime = getattr(self._config_entry, "runtime_data", None)
        profiles = getattr(runtime, "zone_profiles", {}) or {}
        language = getattr(runtime, "language", "de")
        return {
            override_key(profile.node, profile.subindex): zone_device_name(
                profile, language
            )
            for _key, profile in sorted(profiles.items())
        }

    def _entity_choices(
        self, configured: Mapping[str, Any] | None = None
    ) -> dict[str, str]:
        """Return only currently discovered, readable, safe scalar projections."""
        runtime = getattr(self._config_entry, "runtime_data", None)
        if runtime is None:
            return {}
        preferences = {**self._config_entry.data, **self._config_entry.options}
        if configured:
            preferences.update(configured)
        choices: dict[str, str] = {}
        for identity, register, _group, allowed in rows_for_parent(
            runtime,
            include_diagnostics=bool(
                preferences.get(CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED)
            ),
            include_screed_drying=bool(
                preferences.get(
                    CONF_SCREED_DRYING_ENABLED, DEFAULT_SCREED_DRYING_ENABLED
                )
            ),
            include_cooling=bool(
                preferences.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED)
            ),
        ):
            if (
                not allowed
                or not register.readable
                or not zone_row_enabled(runtime, identity, register)
            ):
                continue
            effective = runtime.effective_access_levels.get(identity.node)
            if control_kind(
                register, runtime.language
            ) is not None and not write_access_allowed(runtime, register, effective):
                continue
            uid = entity_unique_id(runtime, identity, register)
            base_label = f"{identity.node}: {register_name(register, runtime.language)}"
            structure = bitfield_structure(register)
            bit_fields = (
                tuple(field for field in structure.fields if field.bit_length == 1)
                if structure is not None
                else ()
            )
            if bit_fields:
                for field in bit_fields:
                    choices[f"{uid}:bit:{field.name}"] = (
                        f"{base_label} — {field.label(runtime.language)}"
                    )
            else:
                choices[uid] = base_label
        return choices

    def _entity_default_selection(self) -> list[str]:
        """Return enabled-by-default/currently-selected entity identities."""
        runtime = getattr(self._config_entry, "runtime_data", None)
        if runtime is None:
            return []
        stored = dict(getattr(runtime, "entity_overrides", {}) or {})
        current = {**self._config_entry.data, **self._config_entry.options}
        overrides = current.get(CONF_ENTITY_OVERRIDES, {})
        if isinstance(overrides, Mapping):
            stored.update({str(key): bool(value) for key, value in overrides.items()})
        selected: list[str] = []
        for identity, register, _group, allowed in rows_for_parent(runtime):
            if (
                not allowed
                or not register.readable
                or not zone_row_enabled(runtime, identity, register)
            ):
                continue
            effective = runtime.effective_access_levels.get(identity.node)
            if control_kind(
                register, runtime.language
            ) is not None and not write_access_allowed(runtime, register, effective):
                continue
            base_uid = entity_unique_id(runtime, identity, register)
            structure = bitfield_structure(register)
            bit_fields = (
                tuple(field for field in structure.fields if field.bit_length == 1)
                if structure is not None
                else ()
            )
            uids = (
                tuple(f"{base_uid}:bit:{field.name}" for field in bit_fields)
                if bit_fields
                else (base_uid,)
            )
            default = entity_enabled_by_default(runtime, identity, register)
            if control_kind(register, runtime.language) is not None:
                default = default and write_access_allowed(runtime, register, effective)
            selected.extend(uid for uid in uids if stored.get(uid, default))
        return selected

    def _entity_selection_catalog(self) -> dict[str, dict[str, Any]]:
        """Build a stable Device -> category -> entity projection.

        Core currently exposes zone slot evidence but not manufacturer
        FunctionGroup records. Non-zone rows are therefore grouped by their
        CANopen object index, which is stable and keeps each leaf selector
        bounded. The UI does not claim those object groups are manufacturer
        FunctionGroups.
        """
        runtime = getattr(self._config_entry, "runtime_data", None)
        if runtime is None:
            return {}
        configured = {**self._config_entry.data, **self._config_entry.options}
        configured.update(getattr(self, "_entity_picker_pending", {}) or {})
        cooling = bool(configured.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED))
        rows = rows_for_parent(
            runtime,
            include_diagnostics=bool(
                configured.get(CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED)
            ),
            include_screed_drying=bool(
                configured.get(
                    CONF_SCREED_DRYING_ENABLED, DEFAULT_SCREED_DRYING_ENABLED
                )
            ),
            include_cooling=cooling,
        )
        groups: dict[str, dict[str, Any]] = {}
        for identity, register, _poll_group, allowed in rows:
            if (
                not allowed
                or not register.readable
                or not zone_row_enabled(runtime, identity, register)
            ):
                continue
            effective = runtime.effective_access_levels.get(identity.node)
            if control_kind(
                register, runtime.language
            ) is not None and not write_access_allowed(runtime, register, effective):
                continue
            base_uid = entity_unique_id(runtime, identity, register)
            structure = bitfield_structure(register)
            bit_fields = (
                tuple(field for field in structure.fields if field.bit_length == 1)
                if structure is not None
                else ()
            )
            items = (
                tuple(
                    (
                        f"{base_uid}:bit:{field.name}",
                        f"{register_name(register, runtime.language)} — {field.label(runtime.language)}",
                    )
                    for field in bit_fields
                )
                if bit_fields
                else ((base_uid, register_name(register, runtime.language)),)
            )
            category = entity_category(runtime, identity, register)
            group_key = entity_category_key(runtime, identity, register)
            category_labels = {
                "general": ("Allgemein", "General"),
                "heating_system": ("Heizungsanlage", "Heating system"),
                "zone": ("Zone", "Zone"),
                "dhw": ("Trinkwarmwasser", "Domestic hot water"),
                "heat_pump": ("Wärmepumpe", "Heat pump"),
                "unclassified": ("Nicht klassifiziert", "Unclassified"),
            }
            group_label = category_labels[category][
                0 if runtime.language == "de" else 1
            ]
            if getattr(register, "safety", None) == "unverified":
                write_note = (
                    "Physische Schreibvalidierung fehlt"
                    if runtime.language == "de"
                    else "Physical write validation unavailable"
                )
                group_label = f"{group_label} — {write_note}"
            node = groups.setdefault(
                group_key,
                {
                    "node": identity.node,
                    "node_label": identity.display_name,
                    "device_label": self._device_label(identity),
                    "label": group_label,
                    "items": {},
                    "search": {},
                },
            )
            register_search = " ".join(
                (
                    register_name(register, runtime.language),
                    str(register.address),
                    str(register.internal_code or ""),
                    str(identity.node),
                    identity.display_name,
                    group_label,
                )
            ).casefold()
            for uid, label in items:
                node["items"][uid] = f"Node {identity.node} — {label}"
                node["search"][uid] = f"{register_search} {label}".casefold()
        return dict(
            sorted(
                groups.items(),
                key=lambda item: (
                    item[1]["node"],
                    item[1]["label"].casefold(),
                    item[0],
                ),
            )
        )

    @staticmethod
    def _device_label(identity: Any) -> str:
        """Show the discovered identity without a product-name allowlist."""
        resolution = getattr(identity, "registry_resolution", None)
        model = getattr(resolution, "model", None) or getattr(identity, "model", None)
        family = getattr(resolution, "family", None) or getattr(
            identity, "family", None
        )
        return str(model or family or identity.display_name or "OpenRBus")

    def _picker_overrides(self) -> dict[str, bool | None]:
        overrides = getattr(self, "_entity_picker_overrides", None)
        if isinstance(overrides, dict):
            return overrides
        current = {**self._config_entry.data, **self._config_entry.options}
        stored = current.get(CONF_ENTITY_OVERRIDES, {})
        return dict(stored) if isinstance(stored, Mapping) else {}

    def _picker_node_overrides(self) -> dict[str, bool | None]:
        overrides = getattr(self, "_entity_picker_node_overrides", None)
        if isinstance(overrides, dict):
            return overrides
        current = {**self._config_entry.data, **self._config_entry.options}
        stored = current.get(CONF_NODE_OVERRIDES, {})
        return dict(stored) if isinstance(stored, Mapping) else {}

    def _picker_group_overrides(self) -> dict[str, bool | None]:
        overrides = getattr(self, "_entity_picker_group_overrides", None)
        if isinstance(overrides, dict):
            return overrides
        current = {**self._config_entry.data, **self._config_entry.options}
        stored = current.get(CONF_GROUP_OVERRIDES, {})
        return dict(stored) if isinstance(stored, Mapping) else {}

    def _picker_default_selected(
        self, groups: Mapping[str, Mapping[str, Any]]
    ) -> set[str]:
        runtime = getattr(self._config_entry, "runtime_data", None)
        if runtime is None:
            return set()
        stored = self._picker_overrides()
        node_overrides = self._picker_node_overrides()
        group_overrides = self._picker_group_overrides()
        defaults: set[str] = set()
        rows = {
            entity_unique_id(runtime, identity, register): (identity, register)
            for identity, register, _group, allowed in rows_for_parent(
                runtime,
                include_diagnostics=True,
                include_screed_drying=True,
                include_cooling=True,
            )
            if allowed
        }
        for group_key, group in groups.items():
            for uid in group["items"]:
                base_uid = uid.split(":bit:", 1)[0]
                row = rows.get(base_uid)
                default = False
                if row:
                    identity, register = row
                    default = entity_enabled_by_default(
                        runtime, identity, register, unique_id=base_uid
                    )
                selected = stored.get(uid)
                if selected is None:
                    selected = group_overrides.get(group_key)
                if selected is None and row:
                    selected = next(
                        (
                            group_overrides[key]
                            for key in entity_category_override_keys(
                                runtime, identity, register
                            )
                            if group_overrides.get(key) is not None
                        ),
                        None,
                    )
                if selected is None and row:
                    selected = group_overrides.get(
                        entity_group_key(runtime, identity, register)
                    )
                if selected is None:
                    selected = node_overrides.get(str(group["node"]))
                if selected is None:
                    selected = default
                if selected:
                    defaults.add(uid)
        return defaults

    @staticmethod
    def _apply_picker_preset(
        overrides: dict[str, bool | None], uids: Iterable[str], preset: str
    ) -> None:
        if preset == "default":
            # Keep a reset marker so existing registry entries are recalculated
            # when a prior scope-level disable is removed. ``None`` inherits.
            overrides.update({uid: None for uid in uids})
        elif preset in {"all", "none"}:
            enabled = preset == "all"
            overrides.update({uid: enabled for uid in uids})

    def _entity_picker_form(self, step: str, fields: dict[vol.Marker, object]):
        return self.async_show_form(step_id=step, data_schema=vol.Schema(fields))

    async def async_step_entity_nodes(self, user_input=None):
        groups = self._entity_selection_catalog()
        nodes = sorted({group["node"] for group in groups.values()})
        node_labels = {
            str(node): next(
                group.get("device_label", group["node_label"])
                for group in groups.values()
                if group["node"] == node
            )
            + f" (Node {node})"
            for node in nodes
        }
        if user_input is not None:
            self._entity_picker_pending = dict(
                getattr(self, "_entity_picker_pending", {}) or {}
            )
            if _flow_action(user_input) == FLOW_ACTION_BACK:
                pending = dict(self._entity_picker_pending)
                pending[CONF_ENTITY_OVERRIDES] = self._picker_overrides()
                pending[CONF_NODE_OVERRIDES] = self._picker_node_overrides()
                pending[CONF_GROUP_OVERRIDES] = self._picker_group_overrides()
                self._pending_options_input = pending
                self._entity_picker_pending = None
                self._entity_picker_overrides = None
                self._entity_picker_node_overrides = None
                self._entity_picker_group_overrides = None
                return await self.async_step_init()
            selected_nodes = {
                int(value) for value in user_input.get(_CONF_ENTITY_NODES, ())
            }
            self._entity_picker_nodes = selected_nodes
            overrides = self._picker_overrides()
            self._apply_picker_preset(
                self._picker_node_overrides(),
                (str(node) for node in selected_nodes),
                str(user_input.get(_CONF_ENTITY_NODE_PRESET, "keep")),
            )
            self._entity_picker_overrides = overrides
            self._entity_picker_node_overrides = self._picker_node_overrides()
            return await self.async_step_entity_groups()
        defaults = self._picker_default_selected(groups)
        selected_nodes = getattr(self, "_entity_picker_nodes", None)
        default_nodes = (
            sorted(str(node) for node in selected_nodes)
            if selected_nodes is not None
            else sorted(
                {
                    str(group["node"])
                    for group in groups.values()
                    if any(uid in defaults for uid in group["items"])
                }
            )
        )
        return self._entity_picker_form(
            "entity_nodes",
            {
                vol.Optional(
                    _CONF_ENTITY_NODES, default=default_nodes
                ): cv.multi_select(node_labels),
                vol.Required(
                    _CONF_ENTITY_NODE_PRESET, default="keep"
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(_ENTITY_PRESETS),
                        translation_key="entity_selection_action",
                    )
                ),
                **dict([_navigation_field()]),
            },
        )

    async def async_step_entity_groups(self, user_input=None):
        groups = self._entity_selection_catalog()
        selected_nodes = set(getattr(self, "_entity_picker_nodes", set()))
        eligible = {
            key: group
            for key, group in groups.items()
            if group["node"] in selected_nodes
        }
        if user_input is not None:
            if _flow_action(user_input) == FLOW_ACTION_BACK:
                return await self.async_step_entity_nodes()
            overrides = self._picker_group_overrides()
            selected_groups = set(user_input.get(_CONF_ENTITY_GROUPS, ()))
            self._apply_picker_preset(
                overrides,
                selected_groups.intersection(eligible),
                str(user_input.get(_CONF_ENTITY_GROUP_PRESET, "keep")),
            )
            self._entity_picker_group_overrides = overrides
            action = str(user_input.get(_CONF_ENTITY_GROUP_ACTION, "done"))
            if action == "done":
                return await self.async_step_entity_selection_done()
            group_key = str(user_input.get(_CONF_ENTITY_GROUP_EDIT, ""))
            if group_key not in eligible:
                return self._entity_picker_form(
                    "entity_groups",
                    self._entity_group_schema(eligible, selected_groups, error=True),
                )
            self._entity_picker_group = group_key
            self._entity_picker_search = ""
            return await self.async_step_entity_items()
        defaults = self._picker_default_selected(eligible)
        selected_groups = [
            key
            for key, group in eligible.items()
            if any(uid in defaults for uid in group["items"])
        ]
        return self._entity_picker_form(
            "entity_groups", self._entity_group_schema(eligible, set(selected_groups))
        )

    def _entity_group_schema(
        self,
        groups: Mapping[str, Mapping[str, Any]],
        selected: set[str],
        *,
        error: bool = False,
    ) -> dict[vol.Marker, object]:
        choices = {
            key: f"{group['label']} ({len(group['items'])})"
            for key, group in groups.items()
        }
        fields: dict[vol.Marker, object] = {
            vol.Optional(
                _CONF_ENTITY_GROUPS, default=sorted(selected)
            ): cv.multi_select(choices),
            vol.Required(
                _CONF_ENTITY_GROUP_PRESET, default="keep"
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(_ENTITY_PRESETS),
                    translation_key="entity_selection_action",
                )
            ),
            vol.Required(
                _CONF_ENTITY_GROUP_ACTION, default="edit"
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(_ENTITY_PICKER_ACTIONS),
                    translation_key="entity_group_action",
                )
            ),
        }
        if groups:
            fields[
                vol.Required(_CONF_ENTITY_GROUP_EDIT, default=next(iter(groups)))
            ] = vol.In(choices)
        fields.update(dict([_navigation_field()]))
        return fields

    async def async_step_entity_items(self, user_input=None):
        groups = self._entity_selection_catalog()
        key = getattr(self, "_entity_picker_group", None)
        if key not in groups:
            return await self.async_step_entity_groups()
        group = groups[key]
        search = (
            str(getattr(self, "_entity_picker_search", "") or "").casefold().strip()
        )
        items = {
            uid: label
            for uid, label in group["items"].items()
            if not search or search in group["search"].get(uid, "")
        }
        if user_input is not None:
            action = str(user_input.get(_CONF_ENTITY_ITEM_NAV, "next"))
            if action == "back":
                return await self.async_step_entity_groups()
            overrides = self._picker_overrides()
            preset = str(user_input.get(_CONF_ENTITY_PRESET, "default"))
            self._apply_picker_preset(overrides, items, preset)
            if preset == "custom":
                chosen = set(user_input.get(_CONF_ENTITY_ITEMS, ()))
                overrides.update({uid: uid in chosen for uid in items})
            self._entity_picker_overrides = overrides
            self._entity_picker_search = str(user_input.get(_CONF_ENTITY_SEARCH, ""))
            if action == "done":
                return await self.async_step_entity_selection_done()
            if action == "search":
                return await self.async_step_entity_items()
            return await self.async_step_entity_groups()
        selected = self._picker_default_selected({key: group})
        fields: dict[vol.Marker, object] = {
            vol.Required(_CONF_ENTITY_SEARCH, default=search): cv.string,
            vol.Required(
                _CONF_ENTITY_PRESET, default="custom"
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["custom", *_ENTITY_PRESETS],
                    translation_key="entity_item_action",
                )
            ),
            vol.Optional(
                _CONF_ENTITY_ITEMS, default=sorted(selected.intersection(items))
            ): cv.multi_select(items),
            vol.Required(
                _CONF_ENTITY_ITEM_NAV, default="search"
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["search", "next", "done", "back"],
                    translation_key="entity_item_navigation",
                )
            ),
        }
        return self._entity_picker_form("entity_items", fields)

    async def async_step_entity_selection_done(self, user_input=None):
        pending = dict(getattr(self, "_entity_picker_pending", {}) or {})
        pending[CONF_ENTITY_OVERRIDES] = self._picker_overrides()
        pending[CONF_NODE_OVERRIDES] = self._picker_node_overrides()
        pending[CONF_GROUP_OVERRIDES] = self._picker_group_overrides()
        self._entity_picker_pending = None
        self._entity_picker_overrides = None
        self._entity_picker_node_overrides = None
        self._entity_picker_group_overrides = None
        return await self.async_step_init(pending)

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            # If the ordinary full form exactly matches its initial values
            # except for diagnostics, update only that option and skip all
            # rediscovery. This is useful for local diagnosis without making
            # unrelated settings or transport state part of the operation.
            baseline = getattr(self, "_diagnostic_toggle_baseline", None)
            only_diagnostics_changed = (
                CONF_DIAGNOSTICS_ENABLED in user_input
                and isinstance(baseline, dict)
                and set(user_input) == set(baseline) | {CONF_DIAGNOSTICS_ENABLED}
                and all(user_input.get(key) == value for key, value in baseline.items())
            )
            if only_diagnostics_changed:
                options = dict(self._config_entry.options)
                options[CONF_DIAGNOSTICS_ENABLED] = bool(
                    user_input[CONF_DIAGNOSTICS_ENABLED]
                )
                return self.async_create_entry(title="", data=options)
            user_input = _without_flow_action(user_input)
            previous = {**self._config_entry.data, **self._config_entry.options}
            zone_choices = self._zone_choices()
            selected_zones = user_input.get(CONF_ZONE_OVERRIDES)
            if isinstance(selected_zones, (list, tuple, set)):
                # Persist choices for the visible profiles, but never allow a
                # stale/manual choice to enable a CP020-disabled or unreadable
                # slot. The runtime gate also enforces this during projection.
                profiles = (
                    getattr(
                        getattr(self._config_entry, "runtime_data", None),
                        "zone_profiles",
                        {},
                    )
                    or {}
                )
                profiles_by_key = {
                    override_key(profile.node, profile.subindex): profile
                    for profile in profiles.values()
                }
                user_input[CONF_ZONE_OVERRIDES] = {
                    key: key in selected_zones and profiles_by_key[key].active
                    for key in zone_choices
                }
            entity_choices = self._entity_choices({**previous, **user_input})
            selected_entities = user_input.get(CONF_ENTITY_OVERRIDES)
            pending_options = getattr(self, "_pending_options_input", {}) or {}
            stored_entities = pending_options.get(
                CONF_ENTITY_OVERRIDES, previous.get(CONF_ENTITY_OVERRIDES, {})
            )
            merged_entities = (
                dict(stored_entities) if isinstance(stored_entities, Mapping) else {}
            )
            if isinstance(selected_entities, (list, tuple, set)):
                # Retain overrides for temporarily undiscovered identities;
                # they have no effect until a safe matching row returns.
                merged_entities.update(
                    {key: key in selected_entities for key in entity_choices}
                )
            user_input[CONF_ENTITY_OVERRIDES] = merged_entities
            for key in (CONF_NODE_OVERRIDES, CONF_GROUP_OVERRIDES):
                scoped = user_input.get(
                    key,
                    pending_options.get(key, previous.get(key, {})),
                )
                user_input[key] = dict(scoped) if isinstance(scoped, Mapping) else {}
            self._pending_options_input = None
            if bool(user_input.pop(_CONF_ENTITY_PICKER, False)):
                self._entity_picker_pending = dict(user_input)
                self._entity_picker_overrides = merged_entities
                node_overrides = user_input.get(CONF_NODE_OVERRIDES, {})
                group_overrides = user_input.get(CONF_GROUP_OVERRIDES, {})
                self._entity_picker_node_overrides = (
                    dict(node_overrides) if isinstance(node_overrides, Mapping) else {}
                )
                self._entity_picker_group_overrides = (
                    dict(group_overrides)
                    if isinstance(group_overrides, Mapping)
                    else {}
                )
                return await self.async_step_entity_nodes()
            await _async_apply_mac_profile(self.hass, user_input)
            # Read and write policy are independently persisted.  A higher
            # read policy must not silently become write permission.
            _normalize_access_policies(user_input)
            options_scan_complete = bool(
                user_input.pop("_options_scan_complete", False)
            )
            level = max(_read_access_level(user_input), _write_access_level(user_input))
            if user_input.get(CONF_BACKEND, BACKEND_NATIVE) == BACKEND_NATIVE:
                # The source is intentionally hidden from the public options
                # form.  Preserve an already persisted source if the current
                # scan cannot refresh it during this edit.
                existing = getattr(self._config_entry, "options", {}).get(
                    CONF_BLE_SOURCE
                ) or getattr(self._config_entry, "data", {}).get(CONF_BLE_SOURCE)
                if existing and not user_input.get(CONF_BLE_SOURCE):
                    user_input[CONF_BLE_SOURCE] = existing
            _remember_ble_name(
                self.hass,
                user_input,
                scanned=getattr(self, "_options_scan_devices", None),
                previous=previous,
            )
            self._remember_native_source(user_input)
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
                    if not options_scan_complete:
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
            # An options entry reloads only this existing entry.  Preserve
            # its old options for setup-time, read-only validation and
            # rollback; a second config entry would change device/entity IDs
            # and break history, dashboards and automations.
            if _transport_route(previous) != _transport_route(user_input):
                if not _transport_switch_is_safe(previous, user_input):
                    return self.async_show_form(
                        step_id="init", errors={"base": "transport_mac_mismatch"}
                    )
                user_input[CONF_TRANSPORT_MIGRATION] = {
                    "previous_options": dict(self._config_entry.options),
                    "previous_route": _transport_route(previous),
                }
            await async_save_access_profile(self.hass, user_input)
            return self.async_create_entry(title="", data=user_input)
        # Options are the current source of truth; entry data is the legacy
        # fallback for entries created before this setting was moved to
        # options.  Normalize only this field so unrelated option defaults are
        # preserved exactly as stored.
        current = {**self._config_entry.data, **self._config_entry.options}
        current.update(getattr(self, "_pending_options_input", {}) or {})
        current.pop(CONF_FLOW_ACTION, None)
        stored_profile = await async_load_access_profile(
            self.hass, current.get(CONF_BLE_DEVICE)
        )
        for key, value in stored_profile.items():
            current.setdefault(key, value)
        _normalize_access_policies(current)
        # The effective transport level may be higher than the saved read
        # policy because it satisfies write authorization. Keep the persisted
        # read selection as the form default so open/save without edits is
        # stable and does not widen entity visibility.
        self._native_ble_sources = _native_ble_source_map(self.hass)
        fields = {
            vol.Required(
                CONF_BACKEND,
                default=current.get(CONF_BACKEND, BACKEND_NATIVE),
            ): vol.In(BACKEND_OPTIONS),
            vol.Required(
                CONF_READ_ACCESS_LEVEL,
                default=_read_access_level(current),
            ): _access_level_slider(1, 3),
            vol.Required(
                CONF_WRITE_ACCESS_LEVEL,
                default=_write_access_level(current),
            ): _access_level_slider(0, 3),
            vol.Required(
                CONF_LANGUAGE,
                default=current.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
            ): vol.In(LANGUAGE_OPTIONS),
            vol.Required(
                CONF_WRITE_ENABLED,
                default=current.get(CONF_WRITE_ENABLED, False),
            ): cv.boolean,
            vol.Required(
                CONF_EXPERIMENTAL_WRITES,
                default=current.get(
                    CONF_EXPERIMENTAL_WRITES, DEFAULT_EXPERIMENTAL_WRITES
                ),
            ): cv.boolean,
            vol.Required(
                CONF_DIAGNOSTICS_ENABLED,
                default=current.get(
                    CONF_DIAGNOSTICS_ENABLED, DEFAULT_DIAGNOSTICS_ENABLED
                ),
            ): cv.boolean,
            vol.Required(
                CONF_SCREED_DRYING_ENABLED,
                default=current.get(
                    CONF_SCREED_DRYING_ENABLED, DEFAULT_SCREED_DRYING_ENABLED
                ),
            ): cv.boolean,
            vol.Required(
                CONF_COOLING_ENABLED,
                default=current.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED),
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
                    CONF_POLL_STANDARD, DEFAULT_POLL_INTERVALS[CONF_POLL_STANDARD]
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
            vol.Required(
                CONF_POLL_SLOW,
                default=current.get(
                    CONF_POLL_SLOW, DEFAULT_POLL_INTERVALS[CONF_POLL_SLOW]
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=86400)),
            vol.Required(
                CONF_INVALID_VALUE_DISABLE_AFTER,
                default=current.get(
                    CONF_INVALID_VALUE_DISABLE_AFTER,
                    DEFAULT_INVALID_VALUE_DISABLE_AFTER,
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=31536000)),
        }
        zone_choices = self._zone_choices()
        if zone_choices:
            stored = current.get(CONF_ZONE_OVERRIDES, {})
            profiles = (
                getattr(self._config_entry.runtime_data, "zone_profiles", {}) or {}
            )
            profiles_by_key = {
                override_key(profile.node, profile.subindex): profile
                for profile in profiles.values()
            }
            # Device-disabled and unreadable profiles are shown with their
            # state in the label, but cannot be selected as active zones.
            selected = [
                key
                for key in zone_choices
                if profiles_by_key[key].active
                and (
                    not isinstance(stored, Mapping)
                    or stored.get(key, profiles_by_key[key].active)
                )
            ]
            fields[vol.Optional(CONF_ZONE_OVERRIDES, default=selected)] = (
                cv.multi_select(zone_choices)
            )
        entity_choices = self._entity_choices(current)
        if entity_choices:
            fields[vol.Optional(_CONF_ENTITY_PICKER, default=False)] = cv.boolean
        self._diagnostic_toggle_baseline = {
            marker.schema: marker.default()
            for marker in fields
            if getattr(marker, "schema", None) != CONF_DIAGNOSTICS_ENABLED
        }
        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "esphome_proxy_setup_url": ESPHOME_PROXY_SETUP_URL
            },
            data_schema=vol.Schema(fields),
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
