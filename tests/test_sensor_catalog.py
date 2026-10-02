"""Focused HA catalog-to-entity projection tests."""

from dataclasses import replace

from openrbus.catalog import catalog_for_node
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.protocol.canip import ObjectAddress
from openrbus.value_codec import CanOpenTimeOfDay

from custom_components.openrbus.register_entities import (
    control_kind,
    entity_unique_id,
    should_project_as_control,
)
from custom_components.openrbus.sensor import (
    _catalog_visible,
    _time_of_day_native_value,
)


def test_synthetic_confirmed_register_projects_as_control_only_after_both_gates() -> (
    None
):
    identity = resolve_device_identity(DeviceIdentity(3, 7702, 3, "GTW-Bluetooth"))
    row = next(
        candidate
        for source in catalog_for_node(identity, max_access_level=3)
        if (candidate := replace(source, writable=True))
        and control_kind(candidate) == "number"
    )
    confirmed = replace(
        row,
        safety="validated",
        access_level_evidence={
            "write": {"known": True, "complete": True, "levels": ["User"]}
        },
    )
    parent = type(
        "Parent",
        (),
        {
            "language": "en",
            "write_enabled": False,
            "configured_write_access_level": 1,
        },
    )()
    assert not should_project_as_control(parent, confirmed, 1)

    parent.write_enabled = True
    assert should_project_as_control(parent, confirmed, 1)
    parent.configured_write_access_level = 0
    assert not should_project_as_control(parent, confirmed, 1)


def test_canopen_time_projects_clock_text_and_standardized_date() -> None:
    milliseconds = (23 * 3600 + 59 * 60 + 58) * 1000 + 123
    assert (
        _time_of_day_native_value(CanOpenTimeOfDay(milliseconds=milliseconds, days=42))
        == "23:59:58.123"
    )
    protocol_date = CanOpenTimeOfDay(
        milliseconds=milliseconds, days=42
    ).protocol_date.isoformat()
    assert protocol_date == "1984-02-12"


def test_scb_catalog_is_projected_and_pollable_at_each_configured_level() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))

    catalogs = {
        level: catalog_for_node(identity, max_access_level=level) for level in (1, 2, 3)
    }
    for level, catalog in catalogs.items():
        assert all(item.readable for item in catalog)
        assert all(_catalog_visible(item, level) for item in catalog)
    assert len(catalogs[1]) < len(catalogs[2]) < len(catalogs[3])
    assert ObjectAddress(0x346A, 10) in {row.address for row in catalogs[3]}


def test_register_entity_catalog_is_not_capped_by_configured_access_level() -> None:
    for identity in (
        DeviceIdentity(3, 7702, 3, "GTW-Bluetooth"),
        DeviceIdentity(55, 7688, 258, "GTW-08"),
        DeviceIdentity(99, 5123, 9, "MK3"),
    ):
        resolved = resolve_device_identity(identity)
        complete = catalog_for_node(resolved)
        low = catalog_for_node(resolved, max_access_level=1)
        assert complete
        assert len(complete) >= len(low)
        assert {row.address for row in low} <= {row.address for row in complete}


def test_cp733_validated_scoped_write_projects_as_select() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))
    catalog = catalog_for_node(identity, max_access_level=3)
    cp733 = [row for row in catalog if row.address == ObjectAddress(0x346A, 4)]
    cp730_count = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress(0x346A, 0)
    )

    assert len(cp733) == 1
    assert control_kind(cp733[0]) == "select"
    assert cp733[0].writable is True
    assert cp733[0].safety == "validated"
    assert cp733[0].address != cp730_count.address
    assert control_kind(cp730_count) is None

    class _Entry:
        config_entry = type(
            "ConfigEntry",
            (),
            {"entry_id": "entry", "data": {"ble_device": "00:11:22:33:44:55"}},
        )()

    assert entity_unique_id(_Entry(), identity, cp733[0]) == (
        "gateway:48f4634d1002f9f3c7570cb43e00dd86:node:4:object:346a:04"
    )
    other = resolve_device_identity(DeviceIdentity(77, None, None, "SCB-10"))
    other_cp733 = next(
        row
        for row in catalog_for_node(other, max_access_level=3)
        if row.address == ObjectAddress(0x346A, 4)
    )
    assert entity_unique_id(_Entry(), other, other_cp733) != entity_unique_id(
        _Entry(), identity, cp733[0]
    )


def test_unverified_writable_declaration_needs_experimental_opt_in() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))
    address = ObjectAddress(0x500F, 0)
    ordinary = next(row for row in catalog_for_node(identity) if row.address == address)
    experimental = next(
        row
        for row in catalog_for_node(identity, experimental_writes=True)
        if row.address == address
    )
    parent = type(
        "Parent",
        (),
        {
            "language": "en",
            "write_enabled": True,
            "experimental_writes": False,
            "configured_write_access_level": 1,
        },
    )()

    assert ordinary.safety == "unverified"
    assert ordinary.writable is False
    assert not should_project_as_control(parent, ordinary, 1)
    parent.experimental_writes = True
    assert experimental.writable is True
    assert should_project_as_control(parent, experimental, 1)


def test_entity_uid_is_repeatable_across_entry_recreation() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))
    row = next(
        item
        for item in catalog_for_node(identity)
        if item.address == ObjectAddress(0x346A, 4)
    )

    def parent(entry_id: str, target: str):
        return type(
            "Parent",
            (),
            {
                "config_entry": type(
                    "Entry",
                    (),
                    {"entry_id": entry_id, "data": {"ble_device": target}},
                )()
            },
        )()

    original = entity_unique_id(parent("random-a", "00:11:22:33:44:55"), identity, row)
    recreated = entity_unique_id(parent("random-b", "001122334455"), identity, row)
    other_target = entity_unique_id(
        parent("random-c", "00:11:22:33:44:56"), identity, row
    )
    assert original == recreated
    assert original != other_target


def test_family_projection_counts_and_polling_are_bounded() -> None:
    cases = (
        DeviceIdentity(4, None, None, "SCB-10"),
        DeviceIdentity(88, 528, None, None),
        DeviceIdentity(3, 7702, 3, "GTW-Bluetooth"),
        DeviceIdentity(55, 7688, 258, "GTW-08"),
        DeviceIdentity(99, 5123, 9, "MK3"),
    )
    for raw in cases:
        identity = resolve_device_identity(raw)
        complete = catalog_for_node(identity)
        pollable = [row for row in complete if _catalog_visible(row, 3)]
        assert complete
        assert len(pollable) <= len(complete)
        assert all(row.readable for row in complete)
        assert all(_catalog_visible(row, 3) for row in pollable)


def test_mk3_identity_energy_and_can_rows_never_project_as_controls() -> None:
    """Recovered declarations cannot make sensitive MK3 state mutable."""

    identity = resolve_device_identity(DeviceIdentity(99, 5123, 9, "MK3"))
    catalog = catalog_for_node(identity)
    sensitive_names = {"Device type", "Article number", "Conn Matrix CAN"}
    rows = [row for row in catalog if row.name_en in sensitive_names]

    assert rows
    assert all(not row.writable for row in rows)
    assert all(control_kind(row) is None for row in rows)
