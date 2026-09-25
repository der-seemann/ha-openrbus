from types import SimpleNamespace

import pytest
from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.number import OpenRBusNumber
from custom_components.openrbus.register_entities import OpenRBusRegisterEntity
from custom_components.openrbus.select import OpenRBusSelect
from custom_components.openrbus.sensor import _entity_enabled_by_default
from custom_components.openrbus.switch import OpenRBusSwitch


def _register(
    address: str,
    *,
    levels: tuple[str, ...],
    datatype: str = "UINT16",
    readable: bool = True,
):
    return SimpleNamespace(
        address=ObjectAddress.parse(address),
        datatype=datatype,
        readable=readable,
        access_level_evidence={"read": {"levels": list(levels)}},
    )


def test_basic_level_zero_and_user_rows_are_enabled() -> None:
    assert _entity_enabled_by_default(
        _register("2001:02", levels=("Level 0",)), frozenset()
    )
    assert _entity_enabled_by_default(
        _register("5405:05", levels=("User",)), frozenset()
    )


def test_elevated_or_unknown_rows_remain_disabled_by_default() -> None:
    assert not _entity_enabled_by_default(
        _register("5501:01", levels=("Installer",)), frozenset()
    )
    assert not _entity_enabled_by_default(_register("5401:02", levels=()), frozenset())


def test_core_recommendation_can_enable_a_safe_scalar_row() -> None:
    row = _register("561b:00", levels=("Installer",))
    assert _entity_enabled_by_default(row, frozenset({row.address}))


def test_structured_rows_stay_disabled_even_with_basic_read_evidence() -> None:
    assert not _entity_enabled_by_default(
        _register("5800:01", levels=("User",), datatype="STRUCT"), frozenset()
    )


def test_unreadable_rows_stay_disabled_even_when_recommended() -> None:
    row = _register("2001:02", levels=("User",), readable=False)
    assert not _entity_enabled_by_default(row, frozenset({row.address}))


@pytest.mark.parametrize(
    "entity_type", (OpenRBusNumber, OpenRBusSelect, OpenRBusSwitch)
)
def test_typed_read_state_is_available_when_write_policy_is_blocked(
    monkeypatch, entity_type
) -> None:
    """Read projections stay live while the separate SET gate is closed."""

    monkeypatch.setattr(
        OpenRBusRegisterEntity, "available", property(lambda _self: True)
    )
    entity = entity_type.__new__(entity_type)
    entity._parent = SimpleNamespace(write_enabled=False)
    entity._register = SimpleNamespace(
        writable=True,
        access_level_evidence={"write": {"known": False, "complete": False}},
    )
    entity._effective_access_level = 3
    assert entity.available is True


def test_select_maps_polled_raw_enum_without_write_authorization() -> None:
    address = ObjectAddress(0x346A, 0x04)
    entity = OpenRBusSelect.__new__(OpenRBusSelect)
    entity._identity = SimpleNamespace(node=4)
    entity._register = SimpleNamespace(address=address)
    entity._value_to_option = {0: "Extra langsam", 1: "Langsamer", 2: "Langsam"}
    entity.coordinator = SimpleNamespace(
        data={(4, address): GenericRead(4, address, b"\x02", 2)}
    )
    assert entity.current_option == "Langsam"
