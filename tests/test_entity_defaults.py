from types import SimpleNamespace

import pytest
from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus import register_entities
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.const import (
    CONF_COOLING_ENABLED,
    CONF_DIAGNOSTICS_ENABLED,
    CONF_GROUP_OVERRIDES,
    CONF_NODE_OVERRIDES,
    CONF_SCREED_DRYING_ENABLED,
    DOMAIN,
)
from custom_components.openrbus.number import OpenRBusNumber
from custom_components.openrbus.register_entities import OpenRBusRegisterEntity
from custom_components.openrbus.select import OpenRBusSelect
from custom_components.openrbus.sensor import _entity_enabled_by_default
from custom_components.openrbus.switch import OpenRBusSwitch
from custom_components.openrbus.zones import ZoneProfile


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
        unit=None,
        internal_code=None,
        name_en=None,
        name_de=None,
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


def test_entity_selection_precedence_is_entity_then_group_then_node(
    monkeypatch,
) -> None:
    row = _register("5501:01", levels=("Installer",))
    identity = SimpleNamespace(node=3)
    parent = SimpleNamespace(
        entity_overrides={},
        zone_profiles={},
        zone_overrides={},
        config_entry=SimpleNamespace(
            data={},
            options={
                CONF_NODE_OVERRIDES: {"3": False},
                CONF_GROUP_OVERRIDES: {"node:3:object:5501": True},
            },
        ),
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")

    assert register_entities.entity_enabled_by_default(parent, identity, row)
    parent.config_entry.options[CONF_GROUP_OVERRIDES]["device:3:category:general"] = (
        False
    )
    assert not register_entities.entity_enabled_by_default(parent, identity, row)
    parent.config_entry.options[CONF_GROUP_OVERRIDES].pop("device:3:category:general")
    parent.entity_overrides["uid"] = False
    assert not register_entities.entity_enabled_by_default(parent, identity, row)
    parent.entity_overrides.clear()
    parent.config_entry.options[CONF_GROUP_OVERRIDES]["node:3:object:5501"] = None
    parent.config_entry.options[CONF_NODE_OVERRIDES]["3"] = True
    assert register_entities.entity_enabled_by_default(parent, identity, row)
    parent.config_entry.options[CONF_NODE_OVERRIDES]["3"] = None
    assert not register_entities.entity_enabled_by_default(parent, identity, row)
    row.readable = False
    parent.entity_overrides["uid"] = True
    assert not register_entities.entity_enabled_by_default(parent, identity, row)


def test_categories_follow_cp020_heating_dhw_and_disabled_profiles() -> None:
    identity = SimpleNamespace(node=3)
    row = SimpleNamespace(
        address=ObjectAddress(0x3404, 0),
        internal_code="CP020",
        name_en="Zone function",
        name_de="Zonenfunktion",
    )
    parent = SimpleNamespace(
        zone_profiles={(3, 0): ZoneProfile(3, 0, 2)},
        zone_overrides={},
    )
    assert register_entities.entity_category(parent, identity, row) == "zone"
    parent.zone_profiles[(3, 0)] = ZoneProfile(3, 0, 6)
    assert register_entities.entity_category(parent, identity, row) == "dhw"
    parent.zone_profiles[(3, 0)] = ZoneProfile(3, 0, 0)
    assert not register_entities.zone_row_enabled(parent, identity, row)
    parent.zone_profiles[(3, 0)] = ZoneProfile(3, 0, None)
    assert register_entities.entity_category(parent, identity, row) == "unclassified"
    row.category = "Heat pump"
    assert register_entities.entity_category(parent, identity, row) == "heat_pump"
    row.category = None
    row.name_en = "Heat pump status"
    assert register_entities.entity_category(parent, identity, row) == "unclassified"


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


def test_cooling_rows_are_hidden_until_parent_option_is_enabled(monkeypatch) -> None:
    entry = SimpleNamespace(data={}, options={})
    parent = SimpleNamespace(
        config_entry=entry,
        inventories=(SimpleNamespace(identity=SimpleNamespace(node=1)),),
        devices=(),
        language="en",
        effective_access_levels={1: 1},
        configured_read_access_level=1,
        configured_access_level=1,
    )
    cooling = _register("4321:00", levels=("User",))
    cooling.node = 1
    cooling.writable = False
    cooling.safety = "read_only"
    normal = SimpleNamespace(
        **{
            **cooling.__dict__,
            "internal_code": "HeatingMode",
            "name_en": "Heating mode",
            "name_de": "Heizbetrieb",
            # Same CANopen object index: filtering one cooling datapoint must
            # not remove unrelated datapoints from a mixed object.
            "address": ObjectAddress.parse("3482:02"),
        }
    )
    unnamed = SimpleNamespace(
        **{
            **normal.__dict__,
            "internal_code": "Mode2",
            "name_en": "Mode 2",
            "name_de": "Modus 2",
            "address": ObjectAddress.parse("3500:02"),
        }
    )
    monkeypatch.setattr(
        register_entities,
        "catalog_for_node",
        lambda *_args: (cooling, normal, unnamed),
    )

    # Exact approved object identities determine the optional filter; labels
    # alone never hide a row.
    assert [row[1] for row in register_entities.rows_for_parent(parent)] == [
        normal,
        unnamed,
    ]
    entry.options[CONF_COOLING_ENABLED] = True
    assert [row[1] for row in register_entities.rows_for_parent(parent)] == [
        cooling,
        normal,
        unnamed,
    ]


def test_poll_selection_counts_report_aggregate_filter_reason(monkeypatch) -> None:
    cooling = _register("513c:00", levels=("User",))
    normal = _register("3482:02", levels=("User",))
    identity = SimpleNamespace(node=5)
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(data={}, options={}),
        inventories=(SimpleNamespace(identity=identity),),
        devices=(),
        language="en",
        effective_access_levels={5: 1},
        configured_read_access_level=1,
        configured_access_level=1,
    )
    monkeypatch.setattr(
        register_entities,
        "catalog_for_node",
        lambda *_args: (cooling, normal),
    )

    complete = register_entities.rows_for_parent(
        parent,
        include_diagnostics=True,
        include_screed_drying=True,
        include_cooling=True,
    )
    selected = register_entities.rows_for_parent(parent)
    counts = register_entities._poll_selection_filter_counts(parent, complete, selected)

    assert counts == {
        "runtime_nodes": 1,
        "catalog_rows": 2,
        "diagnostics_filtered": 0,
        "screed_filtered": 0,
        "cooling_filtered": 1,
        "rows_after_optional_filters": 1,
    }


def test_diagnostic_visibility_toggles_only_integration_owned_registry_rows(
    monkeypatch,
) -> None:
    """Options reloads retain IDs and never override a user disable."""

    normal = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="gateway:48f4634d1002f9f3c7570cb43e00dd86:node:255:object:2001:02",
        entity_id="sensor.openrbus_device_type",
        disabled_by=None,
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="gateway:48f4634d1002f9f3c7570cb43e00dd86:node:255:object:2001:02",
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
            entry_id="entry",
            data={"ble_device": "00:11:22:33:44:55"},
            options={CONF_DIAGNOSTICS_ENABLED: False},
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
        _register("3483:00", levels=("User",))
    )
    assert not register_entities.is_screed_drying_register(
        SimpleNamespace(
            internal_code="ScreedStartTemp",
            name_en="Screed start temp 1",
            name_de="Estrich Starttemperatur 1",
        )
    )


def test_reviewed_filter_map_covers_examples_across_device_families(
    monkeypatch,
) -> None:
    assert register_entities.CATALOG_REGISTRY.find(
        "4321:00"
    ).evidence.device_families == ("Ehc-16",)
    assert register_entities.CATALOG_REGISTRY.find(
        "5140:00"
    ).evidence.device_families == ("Mk-3",)
    assert register_entities.CATALOG_REGISTRY.find(
        "3504:00"
    ).evidence.device_families == ("Scb-10",)
    assert register_entities.CATALOG_REGISTRY.find(
        "3483:00"
    ).evidence.device_families == (
        "Ehc-16",
        "Scb-10",
    )
    identity = SimpleNamespace(node=5, family=None)
    mk3_consumption = _register("513c:00", levels=("User",))
    mk3_production = _register("5140:00", levels=("User",))
    ehc_setpoint = _register("4321:00", levels=("User",))
    zone_cooling = _register("341a:00", levels=("User",))
    buffer_cooling = _register("3504:00", levels=("User",))
    explicit_screed = _register("3483:00", levels=("User",))
    screed_config = _register("344d:00", levels=("User",))
    unrelated_heating = _register("3482:02", levels=("User",))
    runtime_node = SimpleNamespace(identity=identity, capabilities={})
    parent = SimpleNamespace(
        inventories=(runtime_node,),
        devices=(),
        effective_access_levels={5: 1},
        configured_access_level=1,
        configured_read_access_level=1,
        config_entry=SimpleNamespace(data={}, options={}),
    )
    monkeypatch.setattr(
        register_entities,
        "catalog_for_node",
        lambda *_args: (
            mk3_consumption,
            mk3_production,
            ehc_setpoint,
            zone_cooling,
            buffer_cooling,
            explicit_screed,
            screed_config,
            unrelated_heating,
        ),
    )

    filtered = register_entities.rows_for_parent(parent)
    assert [row[1].address for row in filtered] == [unrelated_heating.address]

    picker_rows = register_entities.rows_for_parent(
        parent, include_cooling=True, include_screed_drying=True
    )
    cooling_group = register_entities.entity_group_key(parent, identity, mk3_production)
    assert [row[1].address for row in picker_rows] == [
        mk3_consumption.address,
        mk3_production.address,
        ehc_setpoint.address,
        zone_cooling.address,
        buffer_cooling.address,
        explicit_screed.address,
        screed_config.address,
        unrelated_heating.address,
    ]
    assert cooling_group == "node:5:optional:cooling"
    assert not register_entities.is_screed_drying_register(
        SimpleNamespace(
            internal_code="HeatingProgram1",
            name_en="Heating program 1",
            name_de="Heizprogramm 1",
        )
    )


def test_filter_classification_uses_object_and_not_matching_name_fragments() -> None:
    assert register_entities.is_cooling_register(_register("4321:00", levels=("User",)))
    assert register_entities.is_screed_drying_register(
        _register("344d:00", levels=("User",))
    )
    # Heat-exchanger purge and solar tank recooling use cooling language but
    # are separate functions from building cooling mode.
    for address in ("2275:00", "2a0f:00", "2a10:00", "2a13:00"):
        row = _register(address, levels=("User",))
        assert not register_entities.is_cooling_register(row)
        assert not register_entities.is_screed_drying_register(row)
    for address in ("2304:00", "430e:00", "540c:00"):
        row = _register(address, levels=("User",))
        assert not register_entities.is_cooling_register(row)


def test_screed_visibility_preserves_user_disable(monkeypatch) -> None:
    screed = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="gateway:48f4634d1002f9f3c7570cb43e00dd86:node:1:object:348c:00",
        entity_id="switch.openrbus_screed_drying",
        disabled_by=None,
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="gateway:48f4634d1002f9f3c7570cb43e00dd86:node:1:object:348c:00",
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
            entry_id="entry",
            data={"ble_device": "00:11:22:33:44:55"},
            options={CONF_SCREED_DRYING_ENABLED: False},
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
