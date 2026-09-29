from __future__ import annotations

from custom_components.openrbus.proxy_provisioning import (
    check_proxy_compatibility,
    read_proxy_yaml,
)


def test_proxy_compatibility_accepts_current_schema_and_contract() -> None:
    result = check_proxy_compatibility(
        {"rpc_schema_version": 3, "pair_contract": "pair_terminal_v3"}
    )
    assert result.compatible
    assert result.reason == "compatible"


def test_proxy_compatibility_rejects_old_unknown_and_mismatched_firmware() -> None:
    assert check_proxy_compatibility({"rpc_schema_version": 2}).reason == (
        "proxy_update_required"
    )
    assert check_proxy_compatibility({}).reason == "missing_rpc_schema_version"
    assert (
        check_proxy_compatibility(
            {"rpc_schema_version": 4, "pair_contract": "pair_terminal_v3"}
        ).reason
        == "unsupported_rpc_schema_version"
    )
    assert (
        check_proxy_compatibility(
            {"rpc_schema_version": 3, "pair_contract": "pair_terminal_v2"}
        ).reason
        == "unsupported_pair_contract"
    )


def test_proxy_yaml_is_generic_and_secret_placeholder_only() -> None:
    yaml = read_proxy_yaml()
    assert "!secret api_encryption_key" in yaml
    assert "!secret ota_password" in yaml
    assert "!secret wifi_ssid" in yaml
    assert "!secret wifi_password" in yaml
    assert "!secret ble_target_mac" in yaml
    assert "REPLACE_WITH_" not in yaml


def test_proxy_pair_action_clears_only_disarmed_stale_terminal_state() -> None:
    yaml = read_proxy_yaml()
    assert "const bool stale_terminal" in yaml
    assert "id(openrbus_pair_state) == 4" in yaml
    assert "id(openrbus_pair_state) == 5" in yaml
    assert "!id(openrbus_pairing_armed) && stale_terminal" in yaml
    assert "!id(ehc16_ble_client).connected()" in yaml
    assert "id(openrbus_pair_state) = 0;" in yaml
