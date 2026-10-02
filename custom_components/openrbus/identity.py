"""Stable, privacy-safe identities for Home Assistant projections."""

from __future__ import annotations

import hashlib
from typing import Any

from .const import CONF_BLE_DEVICE


def stable_gateway_id(parent: Any) -> str:
    """Hash the configured BLE target identifier for stable HA IDs."""
    configured = dict(parent.config_entry.data)
    configured.update(getattr(parent.config_entry, "options", {}))
    target = configured.get(CONF_BLE_DEVICE)
    if not isinstance(target, str) or not target.strip():
        raise ValueError("OpenRBus stable entity IDs require a configured BLE target")
    canonical = target.strip().casefold()
    compact = canonical.replace(":", "").replace("-", "")
    if len(compact) == 12 and all(char in "0123456789abcdef" for char in compact):
        # Preserve IDs already assigned to conventional Bluetooth MACs.
        identity = bytes.fromhex(compact)
    else:
        # HA adapters may expose stable platform identifiers such as UUIDs.
        # These are valid BLE targets in the config flow and must produce the
        # same deterministic IDs without exposing the identifier itself.
        identity = canonical.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:32]
    return f"gateway:{digest}"


def stable_node_id(parent: Any, node: int) -> str:
    """Return the canonical HA device identifier for one CANopen node."""
    return f"{stable_gateway_id(parent)}:node:{node}"


def stable_object_id(parent: Any, node: int, index: int, subindex: int) -> str:
    """Return the canonical HA entity ID for one node/register pair."""
    return f"{stable_node_id(parent, node)}:object:{index:04x}:{subindex:02x}"


def migrate_legacy_unique_id(parent: Any, value: str) -> str:
    """Translate a previous entry-scoped ID without needing catalogue data."""
    entry_id = parent.config_entry.entry_id
    prefix = f"{entry_id}:node:"
    if value.startswith(prefix) and ":object:" in value:
        tail = value[len(prefix) :]
        node_text, object_text = tail.split(":object:", 1)
        parts = object_text.split(":")
        try:
            result = stable_object_id(
                parent, int(node_text), int(parts[0], 16), int(parts[1], 16)
            )
        except (ValueError, IndexError):
            return value
        return result + (":" + ":".join(parts[2:]) if len(parts) > 2 else "")
    if value == f"{entry_id}_2001_02":
        return stable_object_id(parent, 0xFF, 0x2001, 0x02)
    prefix = f"{entry_id}_node_"
    if value.startswith(prefix):
        node_text, separator, field = value[len(prefix) :].partition("_")
        if separator and field in {"device_code", "parameter_number"}:
            try:
                return f"{stable_node_id(parent, int(node_text))}:identity:{field}"
            except ValueError:
                return value
    return value
