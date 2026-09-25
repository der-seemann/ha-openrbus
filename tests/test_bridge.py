"""Tests for the narrow read-only ESPHome bridge."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from openrbus.errors import ChecksumError, ProtocolError
from openrbus.protocol.ble_segments import BleSegmentCodec

_SPEC = importlib.util.spec_from_file_location(
    "openrbus_ha_bridge",
    Path(__file__).parents[1] / "custom_components/openrbus/bridge.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_BRIDGE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BRIDGE
_SPEC.loader.exec_module(_BRIDGE)
decode_device_type = _BRIDGE.decode_device_type

KNOWN_GOOD = "ff01020000000100ff200102161efd82"


def test_decodes_hardware_verified_device_type() -> None:
    result = decode_device_type(KNOWN_GOOD)
    assert str(result.address) == "2001:02"
    assert result.raw_value == bytes.fromhex("161e")
    assert result.value == 7702


def test_rejects_wrong_crc() -> None:
    with pytest.raises(ChecksumError):
        decode_device_type(KNOWN_GOOD[:-2] + "00")


def test_rejects_wrong_object_correlation() -> None:
    message = bytes.fromhex("01020000000100ff200103161e")
    segment = BleSegmentCodec().encode(message)[0].hex()
    with pytest.raises(ProtocolError, match="correlation"):
        decode_device_type(segment)
