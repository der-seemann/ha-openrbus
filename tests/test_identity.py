"""Stable gateway identity coverage for supported BLE address formats."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.openrbus.config_flow import (
    OpenRBusConfigFlow,
    _choices_from_ble_items,
)
from custom_components.openrbus.const import (
    BACKEND_NATIVE,
    BACKEND_THIN_RPC,
    CONF_AUTH_KEY,
    CONF_BACKEND,
    CONF_BLE_DEVICE,
    CONF_PASSKEY,
    CONF_READ_ACCESS_LEVEL,
    CONF_THIN_CONTROLLER,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
)
from custom_components.openrbus.identity import stable_gateway_id, stable_object_id


def _parent(target: str):
    return SimpleNamespace(
        config_entry=SimpleNamespace(data={CONF_BLE_DEVICE: target}, options={})
    )


def test_mac_gateway_identity_preserves_existing_hash() -> None:
    parent = _parent("AA:BB:CC:DD:EE:FF")

    assert stable_gateway_id(parent) == "gateway:17226b1f68aebacdef0746450f642874"


@pytest.mark.parametrize(
    "target",
    [
        "AA:BB:CC:DD:EE:FF",
        "aa-bb-cc-dd-ee-ff",
        "{A0B1C2D3-E4F5-6789-ABCD-0123456789EF}",
    ],
)
def test_ble_target_choices_can_produce_stable_register_ids(target: str) -> None:
    choices = _choices_from_ble_items([{"address": target, "name": "Gateway"}])
    selected = next(iter(choices))
    parent = _parent(selected)

    first = stable_object_id(parent, 4, 0x346A, 0x04)
    second = stable_object_id(_parent(selected), 4, 0x346A, 0x04)

    assert first == second
    assert first.endswith(":node:4:object:346a:04")
    assert selected not in first


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [BACKEND_NATIVE, BACKEND_THIN_RPC])
async def test_fresh_entry_with_opaque_ble_id_keeps_level_three_write_setup(
    backend: str, monkeypatch
) -> None:
    target = "{A0B1C2D3-E4F5-6789-ABCD-0123456789EF}"
    capability = SimpleNamespace(
        request_service="openrbus_gatt_rpc_request",
        poll_service="openrbus_gatt_rpc_poll",
        diagnostics_service="openrbus_gatt_rpc_diagnostics",
    )
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow.async_save_access_profile",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.openrbus.config_flow.thin_rpc_controller_choices",
        lambda *_args, **_kwargs: {"controller": capability},
    )
    flow = OpenRBusConfigFlow()
    flow.hass = SimpleNamespace(
        services=SimpleNamespace(async_services=lambda: {"esphome": {}})
    )
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = lambda: None
    flow.async_create_entry = lambda *, title, data: {"title": title, "data": data}
    payload = {
        CONF_BACKEND: backend,
        CONF_BLE_DEVICE: target,
        CONF_PASSKEY: "123456",
        CONF_AUTH_KEY: "abcdef12",
        CONF_READ_ACCESS_LEVEL: 3,
        CONF_WRITE_ACCESS_LEVEL: 3,
        CONF_WRITE_ENABLED: True,
    }
    if backend == BACKEND_THIN_RPC:
        payload[CONF_THIN_CONTROLLER] = "controller"

    result = await flow._async_create_from_input(payload)
    entry = SimpleNamespace(data=result["data"], options={})
    parent = SimpleNamespace(config_entry=entry)

    assert result["data"][CONF_BACKEND] == backend
    assert result["data"][CONF_READ_ACCESS_LEVEL] == 3
    assert result["data"][CONF_WRITE_ACCESS_LEVEL] == 3
    assert result["data"][CONF_WRITE_ENABLED] is True
    assert stable_object_id(parent, 4, 0x346A, 0x04).endswith(":node:4:object:346a:04")
