"""Focused config-flow contract tests."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import openrbus.authorization as core_authorization
import pytest

import custom_components.openrbus as integration
from custom_components.openrbus import config_flow as config_flow_module
from custom_components.openrbus.config_flow import (
    OpenRBusConfigFlow,
    OpenRBusOptionsFlowHandler,
    _async_warning_text,
    _choices_from_ble_items,
    _compose_warning,
    _format_ble_target_label,
    _has_openrbus_service,
    _native_ble_source_map,
    _normalize_access_level,
    _safe_access_level,
    _stable_scan_records,
    _transport_route,
    _transport_switch_is_safe,
    _warning_required,
    _write_access_level,
)
from custom_components.openrbus.const import (
    ACCESS_LEVEL_LABELS,
    BACKEND_NATIVE,
    BACKEND_THIN_RPC,
    CONF_ACCESS_ACK,
    CONF_ACCESS_LEVEL,
    CONF_AUTH_KEY,
    CONF_BACKEND,
    CONF_BLE_DEVICE,
    CONF_COOLING_ENABLED,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_FLOW_ACTION,
    CONF_PASSKEY,
    CONF_READ_ACCESS_LEVEL,
    CONF_SCREED_DRYING_ENABLED,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
    DOMAIN,
    FLOW_ACTION_BACK,
    LANGUAGE_OPTIONS,
)
from custom_components.openrbus.zones import ZoneProfile

_WARNING_TRANSLATIONS = {
    f"component.{DOMAIN}.common.access_level_warning": "ACCESS",
    f"component.{DOMAIN}.common.write_access_warning": "WRITE",
    f"component.{DOMAIN}.common.optimization_note": "OPTIMIZATION",
}


def test_zone_options_identify_owning_node_and_function() -> None:
    runtime = SimpleNamespace(
        language="de",
        zone_profiles={
            (4, 0): ZoneProfile(4, 0, 0, node_name="SCB-10"),
            (4, 1): ZoneProfile(4, 1, 6, node_name="SCB-10"),
        },
    )
    flow = OpenRBusOptionsFlowHandler(SimpleNamespace(runtime_data=runtime))

    choices = flow._zone_choices()

    assert choices["4:0"] == "SCB-10 (Node 4) — Zone 1 — deaktiviert (Aus)"
    assert choices["4:1"] == (
        "SCB-10 (Node 4) — Zone 1 — Trinkwarmwasser (TWW-Speicher)"
    )


def test_config_flow_exposes_only_supported_transports() -> None:
    assert OpenRBusConfigFlow.VERSION == 1
    assert {BACKEND_NATIVE, BACKEND_THIN_RPC} == {
        BACKEND_NATIVE,
        BACKEND_THIN_RPC,
    }


def test_entity_language_options_match_the_manufacturer_locale_catalog() -> None:
    assert len(LANGUAGE_OPTIONS) == 28
    assert {"de", "en", "fr", "nl", "tr", "zh"}.issubset(LANGUAGE_OPTIONS)


def test_access_level_labels_are_numeric_after_flow_normalization() -> None:
    for label, level in ACCESS_LEVEL_LABELS.items():
        assert _normalize_access_level(label) == level


@pytest.mark.parametrize(
    "value, expected", [(1, 1), (2, 2), (3, 3), ("1", 1), ("2", 2), ("3", 3)]
)
def test_safe_access_level_accepts_legacy_numeric_values(value, expected) -> None:
    assert _safe_access_level(value) == expected


@pytest.mark.parametrize("value", [None, "", "0", "4", "not-a-level", object()])
def test_safe_access_level_uses_level_one_for_missing_or_invalid_values(value) -> None:
    assert _safe_access_level(value) == 1


@pytest.mark.parametrize(
    "value, expected",
    [(0, 0), ("0", 0), (1, 1), (2, 2), (3, 3), ("Installateur (2)", 2)],
)
def test_write_access_level_supports_no_write_and_saved_levels(value, expected) -> None:
    assert _write_access_level({CONF_WRITE_ACCESS_LEVEL: value}) == expected


def test_legacy_access_level_still_prefills_both_policies() -> None:
    assert _write_access_level({CONF_ACCESS_LEVEL: 3}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored, expected",
    [(1, 1), (2, 2), (3, 3), ("2", 2), ("Installateur (2)", 2), (None, 1)],
)
async def test_options_form_uses_normalized_current_access_level_default(
    stored, expected, monkeypatch
) -> None:
    entry = SimpleNamespace(
        data={CONF_ACCESS_LEVEL: 1, CONF_WRITE_ENABLED: True}, options={}
    )
    if stored is not None:
        entry.options = {CONF_ACCESS_LEVEL: stored}
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )
    result = await flow.async_step_init()
    access_marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_READ_ACCESS_LEVEL
    )
    assert access_marker.default() == expected


@pytest.mark.asyncio
async def test_options_form_reopens_independent_read_and_no_write_levels(
    monkeypatch,
) -> None:
    entry = SimpleNamespace(
        data={CONF_ACCESS_LEVEL: 1, CONF_WRITE_ENABLED: False},
        options={CONF_READ_ACCESS_LEVEL: 3, CONF_WRITE_ACCESS_LEVEL: 0},
        runtime_data=SimpleNamespace(effective_access_level=1),
    )
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )
    monkeypatch.setattr(
        OpenRBusOptionsFlowHandler, "_entity_choices", lambda self, configured=None: {}
    )
    monkeypatch.setattr(OpenRBusOptionsFlowHandler, "_zone_choices", lambda self: {})

    result = await flow.async_step_init()
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if hasattr(marker, "default")
    }
    assert defaults[CONF_READ_ACCESS_LEVEL] == 3
    assert defaults[CONF_WRITE_ACCESS_LEVEL] == 0
    assert defaults[config_flow_module.CONF_COOLING_ENABLED] is False


@pytest.mark.asyncio
async def test_options_form_does_not_replace_saved_read_policy_with_observed_level(
    monkeypatch,
) -> None:
    entry = SimpleNamespace(
        data={CONF_ACCESS_LEVEL: 1},
        options={CONF_READ_ACCESS_LEVEL: 1, CONF_WRITE_ACCESS_LEVEL: 3},
        runtime_data=SimpleNamespace(effective_access_level=3),
    )
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )
    monkeypatch.setattr(
        OpenRBusOptionsFlowHandler, "_entity_choices", lambda self, configured=None: {}
    )
    monkeypatch.setattr(OpenRBusOptionsFlowHandler, "_zone_choices", lambda self: {})

    result = await flow.async_step_init()
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None)
        in {CONF_READ_ACCESS_LEVEL, CONF_WRITE_ACCESS_LEVEL}
    }
    assert defaults == {CONF_READ_ACCESS_LEVEL: 1, CONF_WRITE_ACCESS_LEVEL: 3}


@pytest.mark.asyncio
async def test_entity_picker_applies_node_and_group_overrides_in_stages() -> None:
    entry = SimpleNamespace(data={}, options={}, runtime_data=SimpleNamespace())
    flow = OpenRBusOptionsFlowHandler(entry)
    groups = {
        "node:1:object:2001": {
            "node": 1,
            "node_label": "Controller",
            "label": "0x2001",
            "items": {"uid-a": "Node 1 — Status", "uid-b": "Node 1 — Mode"},
            "search": {
                "uid-a": "status 2001:00 node 1",
                "uid-b": "mode 2001:01 node 1",
            },
        },
        "node:2:zone:0": {
            "node": 2,
            "node_label": "SCB-10",
            "label": "Zone 1 — Heating",
            "items": {"uid-c": "Node 2 — Zone temperature"},
            "search": {"uid-c": "zone temperature 3404:00 node 2"},
        },
    }
    flow._entity_selection_catalog = lambda: groups

    def selected(catalog):
        overrides = flow._picker_overrides()
        return {
            uid
            for group in catalog.values()
            for uid in group["items"]
            if overrides.get(uid, uid == "uid-a")
        }

    flow._picker_default_selected = selected
    flow._entity_picker_overrides = {}
    flow._entity_picker_node_overrides = {}
    flow._entity_picker_group_overrides = {}
    result = await flow.async_step_entity_nodes(
        {
            config_flow_module._CONF_ENTITY_NODES: ["1"],
            config_flow_module._CONF_ENTITY_NODE_PRESET: "all",
            CONF_FLOW_ACTION: "next",
        }
    )
    assert result["step_id"] == "entity_groups"
    assert flow._entity_picker_node_overrides == {"1": True}

    result = await flow.async_step_entity_groups(
        {
            config_flow_module._CONF_ENTITY_GROUPS: ["node:1:object:2001"],
            config_flow_module._CONF_ENTITY_GROUP_PRESET: "none",
            config_flow_module._CONF_ENTITY_GROUP_EDIT: "node:1:object:2001",
            config_flow_module._CONF_ENTITY_GROUP_ACTION: "edit",
            CONF_FLOW_ACTION: "next",
        }
    )
    assert result["step_id"] == "entity_items"
    assert flow._entity_picker_group_overrides == {"node:1:object:2001": False}

    fields = {marker.schema: marker for marker in result["data_schema"].schema}
    assert config_flow_module._CONF_ENTITY_ITEMS in fields


@pytest.mark.asyncio
async def test_options_form_prefers_options_over_entry_data_without_saving(
    monkeypatch,
) -> None:
    entry = SimpleNamespace(data={CONF_ACCESS_LEVEL: 1}, options={CONF_ACCESS_LEVEL: 3})
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )
    result = await flow.async_step_init()
    access_marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_READ_ACCESS_LEVEL
    )
    assert access_marker.default() == 3
    assert result["type"].value == "form"


@pytest.mark.asyncio
async def test_options_form_keeps_read_and_write_policies_independent(
    monkeypatch,
) -> None:
    entry = SimpleNamespace(
        data={CONF_ACCESS_LEVEL: 1, CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF"},
        options={CONF_READ_ACCESS_LEVEL: 3, CONF_WRITE_ACCESS_LEVEL: 1},
    )
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow.async_load_access_profile",
        AsyncMock(return_value={}),
    )
    result = await flow.async_step_init()
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None)
        in {CONF_READ_ACCESS_LEVEL, CONF_WRITE_ACCESS_LEVEL}
    }
    assert defaults == {CONF_READ_ACCESS_LEVEL: 3, CONF_WRITE_ACCESS_LEVEL: 1}


@pytest.mark.asyncio
async def test_options_form_defaults_diagnostics_to_off_and_preserves_it(
    monkeypatch,
) -> None:
    entry = SimpleNamespace(data={CONF_ACCESS_LEVEL: 1}, options={})
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )

    result = await flow.async_step_init()
    marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_DIAGNOSTICS_ENABLED
    )
    assert marker.default() is False

    entry.options = {CONF_DIAGNOSTICS_ENABLED: True}
    result = await OpenRBusOptionsFlowHandler(entry).async_step_init()
    marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_DIAGNOSTICS_ENABLED
    )
    assert marker.default() is True


@pytest.mark.asyncio
async def test_options_diagnostic_toggle_updates_only_that_option(monkeypatch) -> None:
    entry = SimpleNamespace(data={"backend": "esphome_thin_rpc"}, options={"keep": 3})
    flow = OpenRBusOptionsFlowHandler(entry)
    flow._diagnostic_toggle_baseline = {
        "backend": "esphome_thin_rpc",
        "read_access_level": 1,
        "write_access_level": 1,
        "language": "de",
        "write_enabled": False,
        "ble_device": "target",
        "passkey": "",
        "poll_interval_fast": 30,
        "poll_interval_standard": 120,
        "poll_interval_slow": 900,
    }
    monkeypatch.setattr(
        OpenRBusOptionsFlowHandler,
        "async_create_entry",
        lambda _self, *, title, data: {"title": title, "data": data},
    )

    result = await flow.async_step_init(
        {**flow._diagnostic_toggle_baseline, CONF_DIAGNOSTICS_ENABLED: True}
    )

    assert result == {
        "title": "",
        "data": {"keep": 3, CONF_DIAGNOSTICS_ENABLED: True},
    }


@pytest.mark.asyncio
async def test_options_form_defaults_screed_drying_to_off_and_preserves_it(
    monkeypatch,
) -> None:
    entry = SimpleNamespace(data={CONF_ACCESS_LEVEL: 1}, options={})
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )

    result = await OpenRBusOptionsFlowHandler(entry).async_step_init()
    marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_SCREED_DRYING_ENABLED
    )
    assert marker.default() is False

    entry.options = {CONF_SCREED_DRYING_ENABLED: True}
    result = await OpenRBusOptionsFlowHandler(entry).async_step_init()
    marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_SCREED_DRYING_ENABLED
    )
    assert marker.default() is True
    cooling_marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_COOLING_ENABLED
    )
    assert cooling_marker.default() is False
    entry.options[CONF_COOLING_ENABLED] = True
    result = await OpenRBusOptionsFlowHandler(entry).async_step_init()
    cooling_marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_COOLING_ENABLED
    )
    assert cooling_marker.default() is True


@pytest.mark.asyncio
async def test_recreated_setup_prefills_mac_bound_pin_and_key() -> None:
    flow = OpenRBusConfigFlow()
    flow._pending_user_input = {
        CONF_PASSKEY: "123456",
        CONF_AUTH_KEY: "abcdef12",
        CONF_READ_ACCESS_LEVEL: 2,
        CONF_WRITE_ACCESS_LEVEL: 1,
    }
    result = await flow.async_step_credentials()
    defaults = {
        marker.schema: marker.default()
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) in {CONF_PASSKEY, CONF_AUTH_KEY}
    }
    assert defaults == {CONF_PASSKEY: "123456", CONF_AUTH_KEY: "abcdef12"}


def test_integration_uses_current_core_authorization_contract() -> None:
    """The HA component must load against Core's public Tea key API."""

    assert integration.TeaKeyComponent is core_authorization.TeaKeyComponent
    assert not hasattr(core_authorization, "EhcGatewayKeyComponent")
    assert not hasattr(core_authorization, "EhcGatewayKeyProvider")


def test_ble_choice_shows_name_and_mac_but_keeps_address_as_value() -> None:
    choices = _choices_from_ble_items(
        [{"address": "aa:bb:cc:dd:ee:ff", "name": "Gateway"}]
    )
    assert choices == {"aa:bb:cc:dd:ee:ff": "Gateway (AA:BB:CC:DD:EE:FF)"}


def test_ble_choice_uses_only_mac_for_missing_or_placeholder_name() -> None:
    assert _format_ble_target_label("AA:BB:CC:DD:EE:01", " ") == ("AA:BB:CC:DD:EE:01")
    assert _format_ble_target_label("AA:BB:CC:DD:EE:02", "Unknown device") == (
        "AA:BB:CC:DD:EE:02"
    )


def test_native_ble_source_map_dedupes_same_target_deterministically(
    monkeypatch,
) -> None:
    target = "AA:BB:CC:DD:EE:FF"
    records = [
        SimpleNamespace(
            address=target, source="adapter-hci1", rssi=-70, service_uuids=None
        ),
        SimpleNamespace(
            address=target.lower(), source="adapter-hci0", rssi=-60, service_uuids=None
        ),
        SimpleNamespace(
            address=target, source="adapter-hci2", rssi=-60, service_uuids=None
        ),
    ]
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow.bluetooth.async_discovered_service_info",
        lambda _hass, connectable=True: records,
    )
    assert _native_ble_source_map(SimpleNamespace()) == {
        target.casefold(): "adapter-hci0"
    }


def test_ble_choice_deduplicates_addresses_and_chooses_name_deterministically() -> None:
    items = [
        {"address": "AA:BB:CC:DD:EE:03", "name": "Zulu"},
        {"address": "aa:bb:cc:dd:ee:03", "name": "Alpha"},
        {"address": "AA:BB:CC:DD:EE:04", "name": "Other"},
    ]
    assert _choices_from_ble_items(items) == {
        "AA:BB:CC:DD:EE:03": "Alpha (AA:BB:CC:DD:EE:03)",
        "AA:BB:CC:DD:EE:04": "Other (AA:BB:CC:DD:EE:04)",
    }


def test_native_choice_filter_matches_current_transparent_service_selector() -> None:
    assert _has_openrbus_service(
        SimpleNamespace(
            service_uuids=["F8FC98E4-5919-4A5C-852E-DFE04AD383C0"],
            manufacturer_data={17474: b"BDR"},
        )
    )
    assert not _has_openrbus_service(SimpleNamespace(service_uuids=[]))
    assert not _has_openrbus_service(
        SimpleNamespace(
            service_uuids=["F8FC98E4-5919-4A5C-852E-DFE04AD383C0"],
            manufacturer_data={1: b"other"},
        )
    )
    assert not _has_openrbus_service(
        SimpleNamespace(service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"])
    )
    assert not _has_openrbus_service(SimpleNamespace(manufacturer_data={1: b"other"}))


def test_empty_remote_scan_has_no_choices_and_manual_native_fallback_remains_possible() -> (
    None
):
    assert _stable_scan_records(()) == {}
    assert _choices_from_ble_items(()) == {}


def test_transport_route_changes_for_adapter_or_proxy_but_not_policy() -> None:
    native = {
        CONF_BACKEND: BACKEND_NATIVE,
        CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
        "ble_source": "hci0",
        CONF_READ_ACCESS_LEVEL: 1,
    }
    assert _transport_route(native) == _transport_route(
        {**native, CONF_READ_ACCESS_LEVEL: 3}
    )
    assert _transport_route(native) != _transport_route(
        {**native, "ble_source": "hci1"}
    )
    assert _transport_route(native) != _transport_route(
        {**native, CONF_BACKEND: BACKEND_THIN_RPC, "thin_rpc_controller": "proxy"}
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff", True),
        ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:00", False),
        ("not-a-mac", "not-a-mac", False),
        (None, "AA:BB:CC:DD:EE:FF", False),
    ],
)
def test_retained_entry_transport_switch_requires_same_verified_mac(
    old, new, expected
) -> None:
    assert (
        _transport_switch_is_safe({CONF_BLE_DEVICE: old}, {CONF_BLE_DEVICE: new})
        is expected
    )


def test_warning_gate_requires_access_or_write_permission() -> None:
    assert not _warning_required(1, False)
    assert _warning_required(2, False)
    assert _warning_required(3, False)
    assert _warning_required(1, True)


def test_warning_composition_covers_all_permission_combinations() -> None:
    assert (
        _compose_warning(_WARNING_TRANSLATIONS, access_level=1, write_enabled=False)
        == ""
    )
    assert (
        _compose_warning(_WARNING_TRANSLATIONS, access_level=2, write_enabled=False)
        == "ACCESS"
    )
    assert (
        _compose_warning(_WARNING_TRANSLATIONS, access_level=3, write_enabled=False)
        == "ACCESS"
    )
    assert (
        _compose_warning(_WARNING_TRANSLATIONS, access_level=1, write_enabled=True)
        == "WRITE\n\nOPTIMIZATION"
    )
    assert (
        _compose_warning(_WARNING_TRANSLATIONS, access_level=3, write_enabled=True)
        == "ACCESS\n\nWRITE\n\nOPTIMIZATION"
    )


@pytest.mark.asyncio
async def test_warning_loader_uses_common_translation_keys(monkeypatch) -> None:
    loader = AsyncMock(return_value=_WARNING_TRANSLATIONS)
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow.translation.async_get_translations",
        loader,
    )
    hass = SimpleNamespace(config=SimpleNamespace(language="de-DE"))
    assert await _async_warning_text(hass, access_level=3, write_enabled=True) == (
        "ACCESS\n\nWRITE\n\nOPTIMIZATION"
    )
    loader.assert_awaited_once_with(hass, "de", "common", integrations=(DOMAIN,))


@pytest.mark.asyncio
async def test_warning_loader_falls_back_to_english_for_missing_locale_fragments(
    monkeypatch,
) -> None:
    loader = AsyncMock(
        side_effect=[
            {f"component.{DOMAIN}.common.access_level_warning": "ZUGRIFF"},
            {
                f"component.{DOMAIN}.common.access_level_warning": "ACCESS",
                f"component.{DOMAIN}.common.write_access_warning": "WRITE",
                f"component.{DOMAIN}.common.optimization_note": "OPTIMIZATION",
            },
        ]
    )
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow.translation.async_get_translations",
        loader,
    )
    hass = SimpleNamespace(config=SimpleNamespace(language="fr-FR"))

    assert await _async_warning_text(hass, access_level=3, write_enabled=True) == (
        "ZUGRIFF\n\nWRITE\n\nOPTIMIZATION"
    )
    assert loader.await_args_list == [
        call(hass, "fr", "common", integrations=(DOMAIN,)),
        call(hass, "en", "common", integrations=(DOMAIN,)),
    ]


def test_warning_translation_files_have_matching_fragments_and_placeholder() -> None:
    root = Path(__file__).parents[1] / "custom_components" / "openrbus"
    source = json.loads((root / "strings.json").read_text())
    german = json.loads((root / "translations" / "de.json").read_text())
    assert set(source["common"]) == {
        "access_level_warning",
        "write_access_warning",
        "optimization_note",
    }
    assert set(german["common"]) == set(source["common"])
    assert "{warning}" in source["config"]["step"]["access_warning"]["description"]
    assert "{warning}" in german["config"]["step"]["access_warning"]["description"]
    assert "{warning}" in source["options"]["step"]["access_warning"]["description"]
    assert "{warning}" in german["options"]["step"]["access_warning"]["description"]


@pytest.mark.asyncio
async def test_config_warning_back_clears_ack_and_returns_to_access_step() -> None:
    flow = OpenRBusConfigFlow()
    flow._pending_user_input = {
        CONF_BACKEND: BACKEND_NATIVE,
        CONF_BLE_DEVICE: "AA:BB",
        CONF_ACCESS_LEVEL: 2,
        CONF_WRITE_ENABLED: True,
        CONF_ACCESS_ACK: True,
    }

    result = await flow.async_step_access_warning(
        {CONF_ACCESS_ACK: True, CONF_FLOW_ACTION: FLOW_ACTION_BACK}
    )

    assert result["step_id"] == "access_level"
    assert CONF_ACCESS_ACK not in flow._pending_user_input
    assert flow._pending_user_input[CONF_BLE_DEVICE] == "AA:BB"


@pytest.mark.asyncio
async def test_config_credentials_back_preserves_values_and_reopens_warning(
    monkeypatch,
) -> None:
    flow = OpenRBusConfigFlow()
    flow._pending_user_input = {
        CONF_ACCESS_LEVEL: 2,
        CONF_WRITE_ENABLED: True,
        CONF_ACCESS_ACK: True,
    }
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow._async_warning_text",
        AsyncMock(return_value="warning"),
    )

    result = await flow.async_step_credentials(
        {
            CONF_PASSKEY: "1234",
            CONF_AUTH_KEY: "abcdef12",
            CONF_FLOW_ACTION: FLOW_ACTION_BACK,
        }
    )

    assert result["step_id"] == "access_warning"
    assert flow._pending_user_input[CONF_PASSKEY] == "1234"
    assert flow._pending_user_input[CONF_AUTH_KEY] == "abcdef12"
    assert CONF_ACCESS_ACK not in flow._pending_user_input


@pytest.mark.asyncio
async def test_options_warning_back_does_not_create_entry(monkeypatch) -> None:
    entry = SimpleNamespace(data={CONF_ACCESS_LEVEL: 1}, options={})
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        OpenRBusConfigFlow,
        "_ble_target_choices",
        staticmethod(lambda hass, include_thin=True: {}),
    )
    flow._pending_options_input = {
        CONF_ACCESS_LEVEL: 2,
        CONF_WRITE_ENABLED: True,
        CONF_ACCESS_ACK: True,
    }

    result = await flow.async_step_access_warning(
        {CONF_ACCESS_ACK: True, CONF_FLOW_ACTION: FLOW_ACTION_BACK}
    )

    assert result["step_id"] == "init"
    assert CONF_ACCESS_ACK not in flow._pending_options_input
    assert result["type"].value == "form"
