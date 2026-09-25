"""Focused HA catalog-to-entity projection tests."""

from collections import Counter

from openrbus.catalog import catalog_for_node
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.register_entities import (
    control_kind,
    entity_unique_id,
    enum_options,
)
from custom_components.openrbus.sensor import _catalog_visible


def test_scb_catalog_is_projected_and_pollable_at_each_configured_level() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))

    expected = {1: 221, 2: 557, 3: 617}
    for level, count in expected.items():
        catalog = catalog_for_node(identity, max_access_level=level)
        assert len(catalog) == count
        assert all(item.readable for item in catalog)
        assert sum(_catalog_visible(item, level) for item in catalog) == count


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


def test_cp733_is_one_select_with_concrete_identity_and_array_count_is_not_writable() -> (
    None
):
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
    assert tuple(value for value, _label in enum_options(cp733[0], "de")) == tuple(
        range(6)
    )
    assert cp733[0].access_level_evidence["read"]["required_level"] == "professional"
    assert cp733[0].access_level_evidence["write"]["required_level"] == "professional"
    assert cp733[0].writable is True
    assert cp733[0].safety == "unverified"
    assert cp733[0].address != cp730_count.address
    assert control_kind(cp730_count) is None

    class _Entry:
        config_entry = type("ConfigEntry", (), {"entry_id": "entry"})()

    assert entity_unique_id(_Entry(), identity, cp733[0]) == (
        "entry:node:4:object:346a:04"
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


def test_family_projection_counts_and_polling_are_bounded() -> None:
    cases = (
        (
            DeviceIdentity(4, None, None, "SCB-10"),
            (855, 617, (186, 481, 125, 63), (55, 400, 109, 53)),
        ),
        (
            DeviceIdentity(88, 528, None, None),
            (784, 388, (220, 415, 89, 60), (58, 229, 66, 35)),
        ),
        (
            DeviceIdentity(3, 7702, 3, "GTW-Bluetooth"),
            (26, 22, (6, 15, 4, 1), (5, 13, 3, 1)),
        ),
        (
            DeviceIdentity(55, 7688, 258, "GTW-08"),
            (26, 22, (5, 19, 2, 0), (4, 16, 2, 0)),
        ),
        (DeviceIdentity(99, 5123, 9, "MK3"), (68, 50, (20, 40, 6, 2), (5, 37, 6, 2))),
    )
    for raw, (complete_count, poll_count, expected_kinds, expected_poll_kinds) in cases:
        identity = resolve_device_identity(raw)
        complete = catalog_for_node(identity)
        pollable = [row for row in complete if _catalog_visible(row, 3)]
        kind_counts = Counter(control_kind(row) or "sensor" for row in complete)
        poll_kind_counts = Counter(control_kind(row) or "sensor" for row in pollable)
        assert (len(complete), len(pollable)) == (complete_count, poll_count)
        assert (
            tuple(
                kind_counts[kind] for kind in ("sensor", "number", "select", "switch")
            )
            == expected_kinds
        )
        assert (
            tuple(
                poll_kind_counts[kind]
                for kind in ("sensor", "number", "select", "switch")
            )
            == expected_poll_kinds
        )
