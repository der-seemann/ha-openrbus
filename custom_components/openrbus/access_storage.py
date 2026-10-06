"""MAC-scoped OpenRBus credentials and access preferences.

This storage intentionally outlives config entries.  It lets a user remove a
transport entry and later reconfigure the *same BLE gateway* without typing
the pairing PIN/key again.  Home Assistant owns the storage directory and its
normal backup/permissions model; secrets are never logged by this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.storage import Store

from .const import (
    CONF_AUTH_KEY,
    CONF_PASSKEY,
    CONF_READ_ACCESS_LEVEL,
    CONF_WRITE_ACCESS_LEVEL,
    CONF_WRITE_ENABLED,
    DOMAIN,
)

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}.mac_access"
_MAC_RE = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$", re.IGNORECASE)
_PROFILE_FIELDS = frozenset(
    {
        CONF_PASSKEY,
        CONF_AUTH_KEY,
        CONF_READ_ACCESS_LEVEL,
        CONF_WRITE_ACCESS_LEVEL,
        CONF_WRITE_ENABLED,
    }
)


def normalize_mac(value: object) -> str | None:
    """Return a canonical MAC, rejecting controller IDs and arbitrary text."""

    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    return candidate if _MAC_RE.fullmatch(candidate) else None


def _profile_from(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy just non-empty supported fields; do not retain transport metadata."""

    return {
        key: value[key]
        for key in _PROFILE_FIELDS
        if key in value and value[key] not in (None, "")
    }


async def async_load_access_profile(hass, mac: object) -> dict[str, Any]:
    """Load one MAC-bound profile, or an empty mapping for non-MAC targets."""

    normalized = normalize_mac(mac)
    if normalized is None:
        return {}
    payload = await Store[dict[str, Any]](
        hass, _STORAGE_VERSION, _STORAGE_KEY
    ).async_load()
    if not isinstance(payload, Mapping):
        return {}
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        return {}
    stored = profiles.get(normalized)
    return _profile_from(stored) if isinstance(stored, Mapping) else {}


async def async_save_access_profile(hass, values: Mapping[str, Any]) -> None:
    """Merge access data for a real BLE MAC without exposing secret material."""

    # Import avoids coupling the storage API to the flow's generic field name.
    from .const import CONF_BLE_DEVICE

    normalized = normalize_mac(values.get(CONF_BLE_DEVICE))
    profile = _profile_from(values)
    if normalized is None or not profile:
        return
    store = Store[dict[str, Any]](hass, _STORAGE_VERSION, _STORAGE_KEY)
    payload = await store.async_load()
    profiles = dict(payload.get("profiles", {})) if isinstance(payload, Mapping) else {}
    existing = profiles.get(normalized)
    merged = _profile_from(existing) if isinstance(existing, Mapping) else {}
    merged.update(profile)
    profiles[normalized] = merged
    await store.async_save({"profiles": profiles})
