import pytest
from openrbus.protocol.ble_segments import BleSegmentCodec
from openrbus.protocol.canip import CanIpMessage, GenericFunction, ObjectAddress
from openrbus.protocol.selector import wrap_canip

from custom_components.openrbus.bridge import decode_read_response


def test_decode_generic_device_type_response() -> None:
    result = decode_read_response(
        "ff01020000000100ff200102161efd82",
        address=ObjectAddress(0x2001, 2),
    )
    assert result.value == 0x1E16
    assert result.status == "supported"


def test_decode_generic_negative_is_explicitly_unsupported() -> None:
    address = ObjectAddress(0x5013, 0)
    response = CanIpMessage(
        GenericFunction.READ_NEGATIVE,
        b"\xff" + address.wire + bytes.fromhex("00000206"),
    )
    wire = BleSegmentCodec().encode(wrap_canip(response.encode()))[0].hex()
    with pytest.raises(ValueError, match="OBJECT_NOT_SUPPORTED"):
        decode_read_response(wire, address=address)
