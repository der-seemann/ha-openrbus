"""Registry-backed flag entities stay read-only and use localized field names."""

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.binary_sensor import OpenRBusBitfieldSensor
from custom_components.openrbus.register_entities import bitfield_structure


def test_only_registered_one_bit_fields_are_projected() -> None:
    # A canonical, non-array bitfield; array-backed structures are excluded.
    register = type("Register", (), {"address": ObjectAddress.parse("5503:00")})()
    structure = bitfield_structure(register)
    assert structure is not None
    assert any(field.bit_length == 1 for field in structure.fields)
    assert not hasattr(OpenRBusBitfieldSensor, "async_turn_on")
    assert not hasattr(OpenRBusBitfieldSensor, "async_turn_off")


def test_structures_without_flags_are_not_exposed() -> None:
    register = type("Register", (), {"address": ObjectAddress.parse("200d:01")})()
    assert bitfield_structure(register) is None
