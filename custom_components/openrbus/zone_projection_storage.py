"""Durable identity history for positively confirmed zone projections.

This store preserves only the identity of rows that were already planned from
fresh CP020 evidence.  It is never activity, read, poll, or write authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from homeassistant.helpers.storage import Store

from .zones import (
    ZONE_FAMILY_SLOT_OBJECTS,
    ZONE_SLOT_OBJECT_SOURCES,
    ZoneProfile,
)

_STORAGE_VERSION = 1
_STORAGE_PREFIX = "openrbus.zone_projection"
_UID_RE = re.compile(
    r"^gateway:[0-9a-f]{32}:node:(?P<node>[1-9][0-9]{0,2}):object:"
    r"(?P<index>[0-9a-f]{4}):(?P<slot>[0-9a-f]{2})(?::bit:[A-Za-z0-9_]+)?$"
)


def _store_key(entry_id: str, target: object) -> str:
    normalized_target = str(target or "").strip().casefold()
    payload = json.dumps(
        [str(entry_id), normalized_target], ensure_ascii=True, separators=(",", ":")
    ).encode()
    return f"{_STORAGE_PREFIX}.{hashlib.sha256(payload).hexdigest()}"


def _node_family_key(node: int, family: str) -> str:
    return f"{node}:{family.strip().casefold()}"


def _valid_slot(value: object, *, node: int, family: str) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    slot = value.get("slot")
    function = value.get("function")
    uids = value.get("uids")
    if type(slot) is not int or not 1 <= slot <= 10:
        return None
    if type(function) is not int or function <= 0:
        return None
    if not ZoneProfile(node, slot, function).active or not isinstance(uids, list):
        return None
    family_key = family.strip().casefold()
    valid_uids: set[str] = set()
    for uid in uids:
        if not isinstance(uid, str):
            continue
        match = _UID_RE.fullmatch(uid)
        if (
            match is None
            or int(match.group("node")) != node
            or int(match.group("slot"), 16) != slot
        ):
            continue
        index = int(match.group("index"), 16)
        allowed_families = ZONE_FAMILY_SLOT_OBJECTS.get(index)
        if index not in ZONE_SLOT_OBJECT_SOURCES or (
            allowed_families is not None and family_key not in allowed_families
        ):
            continue
        valid_uids.add(uid)
    if not valid_uids:
        return None
    return {
        "slot": slot,
        "function": function,
        "friendly_name": (
            value.get("friendly_name")
            if isinstance(value.get("friendly_name"), str)
            else None
        ),
        "node_name": value.get("node_name")
        if isinstance(value.get("node_name"), str)
        else None,
        "short_name": (
            value.get("short_name")
            if isinstance(value.get("short_name"), str)
            else None
        ),
        "uids": sorted(valid_uids),
        "node": node,
        "family": family.strip().casefold(),
    }


def _validated_payload(value: object) -> dict[str, dict[int, dict[str, Any]]]:
    """Parse a storage payload, dropping malformed or unsupported records."""
    if not isinstance(value, Mapping) or value.get("schema") != _STORAGE_VERSION:
        return {}
    nodes = value.get("nodes")
    if not isinstance(nodes, Mapping):
        return {}
    result: dict[str, dict[int, dict[str, Any]]] = {}
    for key, raw_slots in nodes.items():
        if not isinstance(key, str) or not isinstance(raw_slots, list):
            continue
        try:
            node_text, family = key.split(":", 1)
            node = int(node_text)
        except (ValueError, TypeError):
            continue
        if not 1 <= node <= 255 or not family:
            continue
        slots: dict[int, dict[str, Any]] = {}
        for raw in raw_slots:
            record = _valid_slot(raw, node=node, family=family)
            if record is not None:
                slots[record["slot"]] = record
        if slots:
            result[key] = slots
    return result


async def async_load_zone_projection(hass, entry_id: str, target: object):
    """Load exact node/family slot identities for this entry and target."""
    store = Store[dict[str, Any]](hass, _STORAGE_VERSION, _store_key(entry_id, target))
    return _validated_payload(await store.async_load())


async def async_save_zone_projection(
    hass, entry_id: str, target: object, value
) -> None:
    """Persist the complete validated projection manifest atomically."""
    nodes: dict[str, list[dict[str, Any]]] = {}
    if isinstance(value, Mapping):
        for key, slots in value.items():
            if not isinstance(key, str) or not isinstance(slots, Mapping):
                continue
            try:
                node_text, family = key.split(":", 1)
                node = int(node_text)
            except (ValueError, TypeError):
                continue
            records = [
                record
                for raw in slots.values()
                if (record := _valid_slot(raw, node=node, family=family)) is not None
            ]
            if records:
                nodes[_node_family_key(node, family)] = sorted(
                    records, key=lambda record: record["slot"]
                )
    store = Store[dict[str, Any]](hass, _STORAGE_VERSION, _store_key(entry_id, target))
    await store.async_save({"schema": _STORAGE_VERSION, "nodes": nodes})


def zone_profile_from_projection(record: object) -> ZoneProfile | None:
    """Rebuild display identity only; callers must still check fresh state."""
    if not isinstance(record, Mapping):
        return None
    try:
        return ZoneProfile(
            int(record["node"]),
            int(record["slot"]),
            int(record["function"]),
            record.get("friendly_name"),
            record.get("node_name"),
            record.get("short_name"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def node_family_key(node: int, family: str) -> str:
    """Return the normalized key used by coordinator projection records."""
    return _node_family_key(node, family)
