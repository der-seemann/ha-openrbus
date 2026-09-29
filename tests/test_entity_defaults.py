from types import SimpleNamespace

import pytest
from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus import register_entities
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.const import (
    CONF_DIAGNOSTICS_ENABLED,
    CONF_SCREED_DRYING_ENABLED,
    DOMAIN,
)
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


def test_manual_entity_override_changes_default_but_keeps_zone_safety(
    monkeypatch,
) -> None:
    row = _register("5501:01", levels=("Installer",))
    identity = SimpleNamespace(node=3)
    parent = SimpleNamespace(
        entity_overrides={"uid": True},
        zone_profiles={},
        zone_overrides={},
    )
    # The ID function is patched in the test module's implementation namespace
    # to keep the test independent of HA's entity registry.
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")
    assert register_entities.entity_enabled_by_default(parent, identity, row)
    parent.entity_overrides["uid"] = False
    assert not register_entities.entity_enabled_by_default(parent, identity, row)
    row.readable = False
    parent.entity_overrides["uid"] = True
    assert not register_entities.entity_enabled_by_default(parent, identity, row)


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


def test_diagnostic_register_classification_is_explicit_only() -> None:
    diagnostic = SimpleNamespace(
        internal_code="CAN diagnostic counter", name_de="Diagnose", name_en="Debug"
    )
    operational = SimpleNamespace(
        internal_code="", name_de="Brennerstatus", name_en="Burner status"
    )
    assert register_entities.is_diagnostic_register(diagnostic)
    assert not register_entities.is_diagnostic_register(operational)


def test_diagnostic_visibility_toggles_only_integration_owned_registry_rows(
    monkeypatch,
) -> None:
    """Options reloads retain IDs and never override a user disable."""

    normal = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="entry_2001_02",
        entity_id="sensor.openrbus_device_type",
        disabled_by=None,
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="entry_2001_02",
        entity_id="sensor.openrbus_device_type_user",
        disabled_by="user",
    )

    class Registry:
        def __init__(self):
            self.entities = {
                normal.entity_id: normal,
                user_disabled.entity_id: user_disabled,
            }

        def async_update_entity(self, entity_id, **changes):
            if changes.get("disabled_by") is not None:
                assert (
                    changes["disabled_by"]
                    is register_entities.er.RegistryEntryDisabler.INTEGRATION
                )
            for entity in self.entities.values():
                if entity.entity_id == entity_id:
                    entity.disabled_by = changes["disabled_by"]

    registry = Registry()
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id="entry", data={}, options={CONF_DIAGNOSTICS_ENABLED: False}
        ),
        inventories=(),
        devices=(),
        language="de",
        effective_access_levels={},
        configured_access_level=1,
    )

    register_entities.async_apply_diagnostic_visibility(object(), parent)
    assert normal.disabled_by == "integration"
    assert user_disabled.disabled_by == "user"

    parent.config_entry.options[CONF_DIAGNOSTICS_ENABLED] = True
    register_entities.async_apply_diagnostic_visibility(object(), parent)
    assert normal.disabled_by is None
    assert user_disabled.disabled_by == "user"


def test_screed_classifier_is_narrow_and_uses_canonical_catalog_labels() -> None:
    assert register_entities.is_screed_drying_register(
        SimpleNamespace(
            internal_code="ScreedStartTemp",
            name_en="Screed start temp 1",
            name_de="Estrich Starttemperatur 1",
        )
    )
    assert not register_entities.is_screed_drying_register(
        SimpleNamespace(
            internal_code="HeatingProgram1",
            name_en="Heating program 1",
            name_de="Heizprogramm 1",
        )
    )


def test_screed_visibility_preserves_user_disable(monkeypatch) -> None:
    screed = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="entry:node:1:object:348c:00",
        entity_id="switch.openrbus_screed_drying",
        disabled_by=None,
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="entry:node:1:object:348c:00",
        entity_id="switch.openrbus_screed_drying_user",
        disabled_by="user",
    )

    class Registry:
        def __init__(self):
            self.entities = {
                screed.entity_id: screed,
                user_disabled.entity_id: user_disabled,
            }

        def async_update_entity(self, entity_id, **changes):
            if changes.get("disabled_by") is not None:
                assert (
                    changes["disabled_by"]
                    is register_entities.er.RegistryEntryDisabler.INTEGRATION
                )
            for entity in self.entities.values():
                if entity.entity_id == entity_id:
                    entity.disabled_by = changes["disabled_by"]

    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id="entry", data={}, options={CONF_SCREED_DRYING_ENABLED: False}
        ),
        inventories=(SimpleNamespace(identity=SimpleNamespace(node=1)),),
        devices=(),
        language="de",
        effective_access_levels={1: 1},
        configured_access_level=1,
        write_enabled=False,
    )
    row = SimpleNamespace(
        internal_code="ScreedDryingEnable",
        name_en="Screed drying enable",
        name_de="Estrichtrocknung aktivieren",
        readable=True,
        writable=False,
        address=ObjectAddress.parse("348c:00"),
        node=1,
        datatype="UINT16",
        unit=None,
        access_level_evidence={"read": {"levels": ["User"]}},
    )
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: Registry())
    monkeypatch.setattr(register_entities, "catalog_for_node", lambda *_args: (row,))
    monkeypatch.setattr(
        register_entities, "entity_unique_id", lambda *_args: screed.unique_id
    )

    # Disabled means no platform projection and no polling address. The
    # entity registry row from an earlier opt-in is handled separately below.
    assert register_entities.rows_for_parent(parent) == ()
    register_entities.async_apply_diagnostic_visibility(object(), parent)
    assert screed.disabled_by == "integration"
    assert user_disabled.disabled_by == "user"

    parent.config_entry.options[CONF_SCREED_DRYING_ENABLED] = True
    assert len(register_entities.rows_for_parent(parent)) == 1
    register_entities.async_apply_diagnostic_visibility(object(), parent)
    assert screed.disabled_by is None
    assert user_disabled.disabled_by == "user"


def test_generic_override_only_changes_integration_owned_safe_rows(monkeypatch) -> None:
    enabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="safe",
        entity_id="sensor.safe",
        disabled_by="integration",
    )
    hidden = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="hidden",
        entity_id="sensor.hidden",
        disabled_by=None,
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="safe",
        entity_id="sensor.user_disabled",
        disabled_by="user",
    )

    class Registry:
        def __init__(self):
            self.entities = {
                item.entity_id: item for item in (enabled, hidden, user_disabled)
            }

        def async_update_entity(self, entity_id, **changes):
            if changes.get("disabled_by") is not None:
                assert (
                    changes["disabled_by"]
                    is register_entities.er.RegistryEntryDisabler.INTEGRATION
                )
            self.entities[entity_id].disabled_by = changes["disabled_by"]

    registry = Registry()
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry", data={}, options={}),
        entity_overrides={"safe": True, "hidden": True},
        effective_access_levels={},
        language="en",
        configured_access_level=1,
        write_enabled=False,
    )
    row = SimpleNamespace(
        readable=True,
        writable=False,
        internal_code="safe",
        address=ObjectAddress.parse("2001:02"),
    )
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_a, **_k: ((SimpleNamespace(node=1), row, "standard", True),),
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_a: "safe")

    register_entities.async_apply_entity_overrides(object(), parent)
    assert enabled.disabled_by is None
    assert (
        hidden.disabled_by == "integration"
    )  # stale enable cannot expose an unsafe row
    assert user_disabled.disabled_by == "user"

    parent.entity_overrides["safe"] = False
    register_entities.async_apply_entity_overrides(object(), parent)
    assert enabled.disabled_by == "integration"
