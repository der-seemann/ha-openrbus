"""Decode the read-only Phase-1A ESPHome response bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openrbus.errors import CanOpenAbortError
from openrbus.protocol.ble_segments import BleSegmentReassembler
from openrbus.protocol.canip import CanIpMessage, ObjectAddress, parse_read_response
from openrbus.protocol.selector import unwrap_canip
from openrbus.registry import Registry
from openrbus.value_codec import decode_value

DEVICE_TYPE = ObjectAddress(0x2001, 0x02)
GATEWAY_BUS_TARGET = 0xFF
_REGISTRY = Registry.load_default()


@dataclass(frozen=True, slots=True)
class BridgeRead:
    """One correlated, CRC-checked and registry-decoded live response."""

    address: ObjectAddress
    raw_value: bytes
    value: Any


@dataclass(frozen=True, slots=True)
class GenericRead:
    """A generic read result suitable for diagnostics and expert reads."""

    node: int
    address: ObjectAddress
    raw_value: bytes
    value: Any
    status: str = "supported"


def decode_device_type(response_hex: str) -> BridgeRead:
    """Decode the exact safe Phase-1A diagnostic response."""
    segment = bytes.fromhex(response_hex)
    message = BleSegmentReassembler().feed(segment)
    if message is None:
        raise ValueError("ESPHome response is not a final BLE segment")
    canip = CanIpMessage.decode(unwrap_canip(message))
    response = parse_read_response(canip, GATEWAY_BUS_TARGET, DEVICE_TYPE)
    definition = _REGISTRY.get(DEVICE_TYPE)
    value = decode_value(
        definition, DEVICE_TYPE, response.raw_value, registry=_REGISTRY
    )
    return BridgeRead(DEVICE_TYPE, response.raw_value, value)


def decode_read_response(
    response_hex: str,
    *,
    node: int = GATEWAY_BUS_TARGET,
    address: ObjectAddress,
) -> GenericRead:
    """Decode one positive response for any registry-backed readable object.

    Negative CANopen responses are surfaced as ``OBJECT_NOT_SUPPORTED`` so a
    batch can retain successful values without conflating protocol support with
    transport failure.
    """

    segment = bytes.fromhex(response_hex)
    message = BleSegmentReassembler().feed(segment)
    if message is None:
        raise ValueError("ESPHome response is not a final BLE segment")
    canip = CanIpMessage.decode(unwrap_canip(message))
    try:
        response = parse_read_response(canip, node, address)
    except CanOpenAbortError as error:
        if error.code == 0x06020000:
            raise ValueError(f"OBJECT_NOT_SUPPORTED:{address}") from error
        raise
    definition = _REGISTRY.get(address)
    value = decode_value(definition, address, response.raw_value, registry=_REGISTRY)
    return GenericRead(node, address, response.raw_value, value)
