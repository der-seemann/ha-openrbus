"""Decode the read-only Phase-1A ESPHome response bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openrbus.protocol.ble_segments import BleSegmentReassembler
from openrbus.protocol.canip import CanIpMessage, ObjectAddress, parse_read_response
from openrbus.protocol.selector import unwrap_canip
from openrbus.registry import Registry
from openrbus.value_codec import decode_value

DEVICE_TYPE = ObjectAddress(0x2001, 0x02)
GATEWAY_BUS_TARGET = 0xFF


@dataclass(frozen=True, slots=True)
class BridgeRead:
    """One correlated, CRC-checked and registry-decoded live response."""

    address: ObjectAddress
    raw_value: bytes
    value: Any


def decode_device_type(response_hex: str) -> BridgeRead:
    """Decode the exact safe Phase-1A diagnostic response."""
    segment = bytes.fromhex(response_hex)
    message = BleSegmentReassembler().feed(segment)
    if message is None:
        raise ValueError("ESPHome response is not a final BLE segment")
    canip = CanIpMessage.decode(unwrap_canip(message))
    response = parse_read_response(canip, GATEWAY_BUS_TARGET, DEVICE_TYPE)
    registry = Registry.load_default()
    definition = registry.get(DEVICE_TYPE)
    value = decode_value(definition, DEVICE_TYPE, response.raw_value, registry=registry)
    return BridgeRead(DEVICE_TYPE, response.raw_value, value)
