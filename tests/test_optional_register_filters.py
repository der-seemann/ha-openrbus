"""Focused contracts for the opt-in register maps."""

from types import SimpleNamespace

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus import register_entities
from custom_components.openrbus.const import CONF_CASCADE_ENABLED, CONF_COOLING_ENABLED
from custom_components.openrbus.optional_register_filters import (
    OPTIONAL_REGISTER_FILTERS,
)


def _core_row(address: str, *, code: str | None = None, datatype: str | None = None):
    parsed = ObjectAddress.parse(address)
    definition = register_entities.CATALOG_REGISTRY.find(parsed)
    assert definition is not None
    return SimpleNamespace(
        address=parsed,
        internal_code=definition.code if code is None else code,
        datatype=definition.wire.type.value if datatype is None else datatype,
        storage=definition.wire.storage.value,
        unit=definition.wire.unit,
        name_en=definition.names.en,
        name_de=definition.names.de,
    )


def test_every_optional_array_member_uses_core_head_and_bounds() -> None:
    array_heads = []
    for head, memberships in OPTIONAL_REGISTER_FILTERS.items():
        if head.subindex != 0:
            continue
        definition = register_entities.CATALOG_REGISTRY.find(head)
        if definition is not None and definition.wire.is_array:
            array_heads.append((head, definition, memberships))

    assert array_heads
    for head, definition, memberships in array_heads:
        max_items = definition.wire.max_items
        assert max_items is not None
        last_item = _core_row(f"{head.index:04x}:{max_items:02x}")
        assert register_entities.optional_register_memberships(last_item) == memberships
        if max_items < 0xFF:
            overflow = SimpleNamespace(
                **{
                    **last_item.__dict__,
                    "address": ObjectAddress(head.index, max_items + 1),
                }
            )
            assert not register_entities.optional_register_memberships(overflow)

    # Runtime-present CP740 slot six is within Core's declared ten-item array,
    # even though the historical family evidence currently lists only 1..5.
    assert register_entities.is_cooling_register(_core_row("346b:06"))


def test_array_classification_rejects_scalar_unknown_and_inconsistent_rows() -> None:
    scalar_member = SimpleNamespace(
        address=ObjectAddress.parse("5724:01"),
        internal_code=None,
        datatype="UINT8",
        storage="UINT8",
    )
    assert not register_entities.optional_register_memberships(scalar_member)
    assert not register_entities.optional_register_memberships(_core_row("346a:04"))
    assert not register_entities.optional_register_memberships(
        _core_row("346b:01", code="wrong-head")
    )
    assert not register_entities.optional_register_memberships(
        _core_row("346b:01", datatype="UINT16")
    )


def test_cascade_cooling_membership_requires_both_preferences(monkeypatch) -> None:
    entry = SimpleNamespace(data={}, options={})
    identity = SimpleNamespace(node=1)
    parent = SimpleNamespace(
        config_entry=entry,
        inventories=(SimpleNamespace(identity=identity),),
        devices=(),
        language="en",
        effective_access_levels={1: 1},
        configured_read_access_level=1,
        configured_access_level=1,
    )
    shared = _core_row("5724:00")
    shared.node = 1
    shared.readable = True
    shared.writable = False
    shared.safety = "read_only"
    shared.access_level_evidence = {"read": {"levels": ["User"]}}
    normal = SimpleNamespace(
        **{
            **shared.__dict__,
            "address": ObjectAddress.parse("3500:02"),
            "internal_code": "HeatingMode",
        }
    )
    monkeypatch.setattr(
        register_entities,
        "catalog_for_node",
        lambda *_args: (shared, normal),
    )

    assert register_entities.optional_register_memberships(shared) == {
        "cooling",
        "cascade",
    }
    assert [row[1] for row in register_entities.rows_for_parent(parent)] == [normal]
    entry.options[CONF_CASCADE_ENABLED] = True
    assert [row[1] for row in register_entities.rows_for_parent(parent)] == [normal]
    entry.options[CONF_COOLING_ENABLED] = True
    selected = register_entities.rows_for_parent(parent)
    assert [row[1] for row in selected] == [shared, normal]
    assert selected[0][1].address == ObjectAddress.parse("5724:00")
