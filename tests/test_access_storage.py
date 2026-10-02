"""MAC-bound credential/access-profile contract tests."""

from __future__ import annotations

import pytest

from custom_components.openrbus.access_storage import (
    async_load_access_profile,
    async_save_access_profile,
    normalize_mac,
)
from custom_components.openrbus.const import (
    CONF_AUTH_KEY,
    CONF_BLE_DEVICE,
    CONF_PASSKEY,
    CONF_READ_ACCESS_LEVEL,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
)


def test_mac_normalization_rejects_non_gateway_identifiers() -> None:
    assert normalize_mac("AA:BB:cc:DD:ee:fF") == "aa:bb:cc:dd:ee:ff"
    assert normalize_mac("controller.openrbus") is None


@pytest.mark.asyncio
async def test_profile_survives_entry_lifecycle_and_merges_policies(
    monkeypatch,
) -> None:
    payload: dict[str, object] | None = None

    class MemoryStore:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, *_args):
            pass

        async def async_load(self):
            return payload

        async def async_save(self, value):
            nonlocal payload
            payload = value

    monkeypatch.setattr("custom_components.openrbus.access_storage.Store", MemoryStore)
    first_entry = {
        CONF_BLE_DEVICE: "AA:BB:CC:DD:EE:FF",
        CONF_PASSKEY: "123456",
        CONF_AUTH_KEY: "abcdef12",
        CONF_READ_ACCESS_LEVEL: 3,
        CONF_WRITE_ACCESS_LEVEL: 0,
        CONF_WRITE_ENABLED: True,
    }
    await async_save_access_profile(object(), first_entry)

    # The old config entry is intentionally absent: a fresh entry locates
    # the same profile by MAC alone, including the distinct no-write value.
    recreated = await async_load_access_profile(object(), "aa:bb:cc:dd:ee:ff")
    assert recreated == {
        CONF_PASSKEY: "123456",
        CONF_AUTH_KEY: "abcdef12",
        CONF_READ_ACCESS_LEVEL: 3,
        CONF_WRITE_ACCESS_LEVEL: 0,
        CONF_WRITE_ENABLED: True,
    }
