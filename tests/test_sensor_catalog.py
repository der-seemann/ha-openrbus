"""Focused HA catalog-to-entity projection tests."""

from collections import Counter

from openrbus.catalog import catalog_for_node
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.protocol.canip import ObjectAddress
from openrbus.value_codec import CanOpenTimeOfDay

from custom_components.openrbus.register_entities import (
    control_kind,
    entity_unique_id,
)
from custom_components.openrbus.sensor import (
    _catalog_visible,
    _time_of_day_native_value,
)


def test_canopen_time_projects_clock_text_and_standardized_date() -> None:
    milliseconds = (23 * 3600 + 59 * 60 + 58) * 1000 + 123
    assert _time_of_day_native_value(
        CanOpenTimeOfDay(milliseconds=milliseconds, days=42)
    ) == "23:59:58.123"
    protocol_date = CanOpenTimeOfDay(
        milliseconds=milliseconds, days=42
    ).protocol_date.isoformat()
    assert protocol_date == "1984-02-12"


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


def test_cp733_without_public_write_evidence_projects_as_read_only_sensor() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))
    catalog = catalog_for_node(identity, max_access_level=3)
    cp733 = [row for row in catalog if row.address == ObjectAddress(0x346A, 4)]
    cp730_count = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress(0x346A, 0)
    )

    assert len(cp733) == 1
    # Public catalog metadata has not validated this write or supplied
    # installation-specific evidence, so the integration keeps it read-only.
    assert control_kind(cp733[0]) is None
    assert cp733[0].writable is False
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
            (855, 617, (855, 0, 0, 0), (617, 0, 0, 0)),
        ),
        (
            DeviceIdentity(88, 528, None, None),
            (784, 388, (784, 0, 0, 0), (388, 0, 0, 0)),
        ),
        (
            DeviceIdentity(3, 7702, 3, "GTW-Bluetooth"),
            (26, 22, (26, 0, 0, 0), (22, 0, 0, 0)),
        ),
        (
            DeviceIdentity(55, 7688, 258, "GTW-08"),
            (26, 22, (26, 0, 0, 0), (22, 0, 0, 0)),
        ),
        (DeviceIdentity(99, 5123, 9, "MK3"), (68, 50, (68, 0, 0, 0), (50, 0, 0, 0))),
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


def test_mk3_identity_energy_and_can_rows_never_project_as_controls() -> None:
    """Recovered declarations cannot make sensitive MK3 state mutable."""

    identity = resolve_device_identity(DeviceIdentity(99, 5123, 9, "MK3"))
    catalog = catalog_for_node(identity)
    sensitive_names = {"Device type", "Article number", "Conn Matrix CAN"}
    rows = [row for row in catalog if row.name_en in sensitive_names]

    assert rows
    assert all(not row.writable for row in rows)
    assert all(control_kind(row) is None for row in rows)
