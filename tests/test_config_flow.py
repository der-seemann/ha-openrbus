"""Focused config-flow contract tests."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import openrbus.authorization as core_authorization
import pytest

import custom_components.openrbus as integration
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
    _warning_required,
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
    CONF_FLOW_ACTION,
    CONF_PASSKEY,
    CONF_WRITE_ENABLED,
    DOMAIN,
    FLOW_ACTION_BACK,
)

_WARNING_TRANSLATIONS = {
    f"component.{DOMAIN}.common.access_level_warning": "ACCESS",
    f"component.{DOMAIN}.common.write_access_warning": "WRITE",
    f"component.{DOMAIN}.common.optimization_note": "OPTIMIZATION",
}


def test_config_flow_exposes_only_supported_transports() -> None:
    assert OpenRBusConfigFlow.VERSION == 1
    assert {BACKEND_NATIVE, BACKEND_THIN_RPC} == {
        BACKEND_NATIVE,
        BACKEND_THIN_RPC,
    }


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
        if getattr(marker, "schema", None) == CONF_ACCESS_LEVEL
    )
    assert access_marker.default() == expected


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
        if getattr(marker, "schema", None) == CONF_ACCESS_LEVEL
    )
    assert access_marker.default() == 3
    assert result["type"].value == "form"


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
