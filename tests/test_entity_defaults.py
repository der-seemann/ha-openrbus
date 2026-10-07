from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.discovery import DeviceIdentity
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
from custom_components.openrbus.identity import stable_object_id
from custom_components.openrbus.number import OpenRBusNumber
from custom_components.openrbus.register_entities import OpenRBusRegisterEntity
from custom_components.openrbus.select import OpenRBusSelect
from custom_components.openrbus.sensor import _entity_enabled_by_default
from custom_components.openrbus.switch import OpenRBusSwitch
from custom_components.openrbus.zones import ZoneProfile, ZoneReadState


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


@pytest.mark.parametrize("override_scope", ("entity", "group"))
def test_manual_override_adds_nonrecommended_sensor_to_poll_set(
    monkeypatch, override_scope: str
) -> None:
    """An active picker choice must drive both entity creation and polling."""
    row = _register("5501:01", levels=("Installer",))
    identity = SimpleNamespace(node=3)
    config_entry = SimpleNamespace(
        entry_id="entry",
        data={},
        options=(
            {CONF_GROUP_OVERRIDES: {"device:3:category:unclassified": True}}
            if override_scope == "group"
            else {}
        ),
        async_on_unload=lambda _callback: None,
    )
    parent = SimpleNamespace(
        config_entry=config_entry,
        inventories=(SimpleNamespace(identity=identity, capabilities={}),),
        devices=(),
        language="en",
        effective_access_levels={3: 3},
        configured_access_level=3,
        entity_overrides={"uid": True} if override_scope == "entity" else {},
        zone_profiles={},
        zone_overrides={},
        poll_intervals={"fast": 1, "standard": 10, "slow": 60},
    )

    class FakePollingCoordinator:
        def __init__(self, _hass, _parent, _group, addresses, _interval, **_kwargs):
            self.addresses = addresses

        async def async_shutdown(self):
            pass

    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")
    monkeypatch.setattr(
        register_entities, "recommended_addresses", lambda _identity: frozenset()
    )
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_args, **_kwargs: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(
        register_entities, "_poll_selection_filter_counts", lambda *_args: {}
    )
    monkeypatch.setattr(register_entities, "_poll_activation_counts", lambda *_args: {})
    monkeypatch.setattr(register_entities, "bitfield_structure", lambda _row: None)
    monkeypatch.setattr(register_entities, "control_kind", lambda *_args: None)
    monkeypatch.setattr(register_entities, "catalog_visible", lambda *_args: True)
    monkeypatch.setattr(
        register_entities, "OpenRBusPollingCoordinator", FakePollingCoordinator
    )
    monkeypatch.setattr(
        register_entities,
        "schedule_first_refresh_in_background",
        lambda *_args, **_kwargs: None,
    )
    hass = SimpleNamespace(data={})

    polling = register_entities.ensure_polling_coordinators(hass, parent)

    assert polling["standard"].addresses == ((3, row.address),)


def test_inferred_source_rw_rows_default_off_until_capability_is_discovered(
    monkeypatch,
) -> None:
    address = ObjectAddress.parse("3700:04")
    row = _register("3700:04", levels=("User",))
    row.writable = True
    row.write_declared = True
    row.safety = "source_supported"
    identity = SimpleNamespace(node=3)
    runtime_node = SimpleNamespace(identity=identity, capabilities={})
    parent = SimpleNamespace(
        inventories=(runtime_node,),
        entity_overrides={},
        zone_profiles={},
        zone_overrides={},
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")

    assert not register_entities.entity_enabled_by_default(parent, identity, row)
    runtime_node.capabilities[address] = object()
    assert register_entities.entity_enabled_by_default(parent, identity, row)


@pytest.mark.parametrize(
    ("read_level", "write_enabled"),
    ((1, False), (1, True), (3, False), (3, True)),
)
def test_unobserved_source_write_declaration_is_independent_of_control_projection(
    monkeypatch, read_level: int, write_enabled: bool
) -> None:
    """Poll defaults use source facts through all HA access projections."""
    row = _register("3700:04", levels=("User",))
    # The HA write option determines sensor vs control projection; it does not
    # change the source declaration that controls absent-slot poll defaults.
    row.writable = write_enabled
    row.write_declared = True  # Core source evidence retains the base fact.
    row.safety = "source_supported"
    identity = SimpleNamespace(node=3)
    runtime_node = SimpleNamespace(
        identity=identity,
        capabilities={ObjectAddress.parse("3700:00"): object()},
    )
    parent = SimpleNamespace(
        inventories=(runtime_node,),
        entity_overrides={},
        zone_profiles={},
        zone_overrides={},
        configured_access_level=read_level,
        write_enabled=write_enabled,
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")

    # The canonical array head does not prove that concrete subindex 04 exists.
    assert not register_entities.entity_enabled_by_default(parent, identity, row)
    # A stale enabled registry projection must not bypass the poll-selection
    # guard for this absent inferred write.
    assert not register_entities._poll_row_selected(parent, identity, row)

    runtime_node.capabilities[ObjectAddress.parse("3700:04")] = object()
    assert register_entities.entity_enabled_by_default(parent, identity, row)
    assert register_entities._poll_row_selected(parent, identity, row)


def test_readonly_array_rows_keep_normal_basic_read_default(monkeypatch) -> None:
    row = _register("3700:04", levels=("User",))
    row.writable = False
    row.write_declared = False
    row.safety = "read_only"
    identity = SimpleNamespace(node=3)
    parent = SimpleNamespace(
        inventories=(SimpleNamespace(identity=identity, capabilities={}),),
        entity_overrides={},
        zone_profiles={},
        zone_overrides={},
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")

    assert register_entities.entity_enabled_by_default(parent, identity, row)
    # An explicit saved picker enable is the only absent-slot bypass.
    assert register_entities._poll_row_selected(parent, identity, row)


def test_manual_override_can_select_an_inferred_source_rw_row(monkeypatch) -> None:
    row = _register("3700:04", levels=("User",))
    row.writable = True
    row.write_declared = True
    row.safety = "source_supported"
    identity = SimpleNamespace(node=3)
    parent = SimpleNamespace(
        inventories=(SimpleNamespace(identity=identity, capabilities={}),),
        entity_overrides={"uid": True},
        zone_profiles={},
        zone_overrides={},
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_args: "uid")

    assert register_entities.entity_enabled_by_default(parent, identity, row)


def test_existing_inferred_rw_rows_reconcile_defaults_and_preserve_user_choices(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=3)
    runtime_node = SimpleNamespace(identity=identity, capabilities={})
    row = _register("3700:04", levels=("User",))
    row.writable = True
    row.write_declared = True
    row.safety = "source_supported"
    auto_enabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="catalog-row",
        entity_id="number.inferred",
        disabled_by=None,
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="catalog-row",
        entity_id="number.user_disabled",
        disabled_by="user",
    )

    class Registry:
        def __init__(self):
            self.entities = {
                auto_enabled.entity_id: auto_enabled,
                user_disabled.entity_id: user_disabled,
            }

        def async_update_entity(self, entity_id, **changes):
            self.entities[entity_id].disabled_by = changes["disabled_by"]

    registry = Registry()
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_args, **_kwargs: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(
        register_entities, "entity_unique_id", lambda *_args: "catalog-row"
    )
    monkeypatch.setattr(register_entities, "entity_group_key", lambda *_args: "group")
    monkeypatch.setattr(
        register_entities, "entity_category_key", lambda *_args: "category"
    )
    monkeypatch.setattr(register_entities, "control_kind", lambda *_args: None)
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry", data={}, options={}),
        inventories=(runtime_node,),
        devices=(),
        entity_overrides={},
        effective_access_levels={},
        language="en",
        configured_access_level=1,
        zone_profiles={},
        zone_overrides={},
        write_enabled=True,
    )

    register_entities.async_apply_entity_overrides(object(), parent)
    # A manually enabled registry row is user intent, even when the runtime
    # capability snapshot temporarily omits the inferred write address.
    assert auto_enabled.disabled_by is None
    assert user_disabled.disabled_by == "user"

    parent.entity_overrides["catalog-row"] = True
    auto_enabled.disabled_by = None
    register_entities.async_apply_entity_overrides(object(), parent)
    assert auto_enabled.disabled_by is None
    assert user_disabled.disabled_by == "user"

    # An explicit picker disable and reset-to-default may still apply the
    # catalog's integration-disabled fallback.
    parent.entity_overrides["catalog-row"] = False
    register_entities.async_apply_entity_overrides(object(), parent)
    assert auto_enabled.disabled_by == "integration"
    parent.entity_overrides["catalog-row"] = None
    auto_enabled.disabled_by = None
    register_entities.async_apply_entity_overrides(object(), parent)
    assert auto_enabled.disabled_by == "integration"


@pytest.mark.asyncio
async def test_user_enabled_zone_row_survives_retained_profile_read_failure(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=5, family="SCB-10")
    runtime_node = SimpleNamespace(identity=identity, capabilities={})
    row = _register("3410:01", levels=("User",))
    enabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="zone-row",
        entity_id="sensor.zone_row",
        disabled_by=None,
    )

    class Registry:
        def __init__(self):
            self.entities = {enabled.entity_id: enabled}

        def async_update_entity(self, entity_id, **changes):
            self.entities[entity_id].disabled_by = changes["disabled_by"]

    registry = Registry()
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_args, **_kwargs: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(
        register_entities, "entity_unique_id", lambda *_args: "zone-row"
    )
    monkeypatch.setattr(register_entities, "entity_group_key", lambda *_args: "group")
    monkeypatch.setattr(
        register_entities, "entity_category_key", lambda *_args: "category"
    )
    monkeypatch.setattr(register_entities, "control_kind", lambda *_args: None)
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry", data={}, options={}),
        inventories=(runtime_node,),
        devices=(),
        entity_overrides={},
        effective_access_levels={},
        language="en",
        configured_access_level=1,
        zone_profiles={(5, 1): ZoneProfile(5, 1, 250)},
        zone_profile_states={(5, 1): ZoneReadState.UNKNOWN},
        _zone_confirmed_active_slots={(5, 1)},
        zone_overrides={},
        write_enabled=True,
    )

    register_entities.async_apply_entity_overrides(object(), parent)

    assert enabled.disabled_by is None
    assert not register_entities.zone_row_enabled(parent, identity, row)
    entity = OpenRBusRegisterEntity.__new__(OpenRBusRegisterEntity)
    entity._register = row
    entity._identity = identity
    entity._parent = parent
    entity._effective_access_level = 1
    entity.coordinator = SimpleNamespace(is_value_available=lambda *_args: True)
    assert not entity.available
    with pytest.raises(HomeAssistantError, match="not confirmed active"):
        await entity._async_write(21)


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
        address=ObjectAddress(0x3404, 1),
        internal_code="CP020",
        name_en="Zone function",
        name_de="Zonenfunktion",
    )
    parent = SimpleNamespace(
        zone_profiles={(3, 1): ZoneProfile(3, 1, 2)},
        zone_overrides={},
    )
    assert register_entities.entity_category(parent, identity, row) == "zone"
    parent.zone_profiles[(3, 1)] = ZoneProfile(3, 1, 6)
    assert register_entities.entity_category(parent, identity, row) == "dhw"
    parent.zone_profiles[(3, 1)] = ZoneProfile(3, 1, 0)
    assert register_entities.zone_row_enabled(parent, identity, row)
    parent.zone_profiles[(3, 1)] = ZoneProfile(3, 1, None)
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
            "address": ObjectAddress.parse("3500:02"),
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
    normal = _register("3500:02", levels=("User",))
    identity = SimpleNamespace(node=5)
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            data={"ble_device": "00:11:22:33:44:55"}, options={}
        ),
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


def test_shared_row_projection_gates_zone_activity_before_platforms_and_picker(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=5)
    rows = tuple(
        _register(address, levels=("User",))
        for address in (
            "340f:01",  # confirmed slot label, active below
            "3410:02",  # confirmed slot label, explicitly inactive below
            "3410:03",  # historical profile, but CP020 read is unknown
            "3404:02",  # CP021 function selector always remains on parent
            "340c:01",  # unresolved flattened activity dimension
            "3500:00",  # ordinary non-zone parent entity
            "3057:00",  # source-confirmed scalar parent control
        )
    )
    parent = SimpleNamespace(
        inventories=(SimpleNamespace(identity=identity),),
        devices=(),
        effective_access_levels={5: 1},
        configured_access_level=1,
        configured_read_access_level=1,
        config_entry=SimpleNamespace(
            data={"ble_device": "00:11:22:33:44:55"}, options={}
        ),
        zone_profiles={
            (5, 1): ZoneProfile(5, 1, 2),
            (5, 2): ZoneProfile(5, 2, 0),
            (5, 3): ZoneProfile(5, 3, 2),
        },
        zone_profile_states={
            (5, 1): ZoneReadState.CONFIRMED_ACTIVE,
            (5, 2): ZoneReadState.CONFIRMED_DISABLED,
            (5, 3): ZoneReadState.UNKNOWN,
        },
    )
    monkeypatch.setattr(
        register_entities, "catalog_for_node", lambda *_args, **_kwargs: rows
    )

    projected = [row[1].address for row in register_entities.rows_for_parent(parent)]
    assert projected == [
        rows[0].address,
        rows[3].address,
        rows[5].address,
        rows[6].address,
    ]

    disabled_uid = stable_object_id(parent, 5, 0x3410, 3)
    parent.zone_projection_history = {
        "5:scb-10": {
            3: {"uids": [disabled_uid], "family": "scb-10", "node": 5, "slot": 3}
        }
    }
    parent.zone_profile_states[(5, 3)] = ZoneReadState.CONFIRMED_DISABLED
    parent._zone_projection_store_blocked = True
    projected_after_fresh_zero = [
        row[1].address for row in register_entities.rows_for_parent(parent)
    ]
    assert rows[2].address not in projected_after_fresh_zero


def test_inactive_zone_registry_cleanup_is_exact_and_entry_scoped(monkeypatch) -> None:
    identity = DeviceIdentity(
        node=5,
        device_code=0,
        parameter_number=0,
        name="Test node",
        family="SCB-10",
    )
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id="entry-one",
            data={
                "ble_device": "00:11:22:33:44:55",
                "zone_overrides": {"5:4": False, "5:5": "false"},
            },
            options={},
        ),
        inventories=(
            SimpleNamespace(identity=identity),
            SimpleNamespace(
                identity=DeviceIdentity(
                    node=6,
                    device_code=0,
                    parameter_number=0,
                    name="Unsupported EHC slot node",
                    family="EHC-16",
                )
            ),
        ),
        zone_profiles={
            (5, 1): ZoneProfile(5, 1, 2),
            (5, 2): ZoneProfile(5, 2, 0),
            (5, 3): ZoneProfile(5, 3, 2),
            (5, 5): ZoneProfile(5, 5, 250),
            (6, 2): ZoneProfile(6, 2, 250),
        },
        zone_profile_states={
            (5, 1): ZoneReadState.CONFIRMED_ACTIVE,
            (5, 2): ZoneReadState.CONFIRMED_DISABLED,
            (5, 3): ZoneReadState.UNKNOWN,
            (5, 5): ZoneReadState.UNKNOWN,
            (6, 2): ZoneReadState.UNKNOWN,
        },
        _zone_confirmed_active_slots={(5, 3), (5, 5)},
        zone_overrides={"5:4": False, "5:5": "false"},
    )
    inactive_uid = stable_object_id(parent, 5, 0x3410, 2)
    active_uid = stable_object_id(parent, 5, 0x340F, 1)
    unresolved_uid = stable_object_id(parent, 5, 0x340D, 2)
    unresolved_active_slot_uid = stable_object_id(parent, 5, 0x340C, 1)
    unresolved_inactive_slot_uid = stable_object_id(parent, 5, 0x340C, 2)
    unresolved_cooling_uid = stable_object_id(parent, 5, 0x3412, 2)
    unresolved_cp080_hk30_uid = stable_object_id(parent, 5, 0x340C, 0x1E)
    unresolved_catalog_slot_uid = stable_object_id(parent, 5, 0x3412, 0x1E)
    unresolved_unproven_slot_uid = stable_object_id(parent, 5, 0x3412, 0x1F)
    global_uid = stable_object_id(parent, 5, 0x540E, 2)
    unknown_uid = stable_object_id(parent, 5, 0x3410, 3)
    override_disabled_uid = stable_object_id(parent, 5, 0x3410, 4)
    malformed_override_uid = stable_object_id(parent, 5, 0x3410, 5)
    initial_unknown_uid = stable_object_id(parent, 5, 0x3410, 6)
    selector_uid = stable_object_id(parent, 5, 0x3404, 2)
    non_zone_uid = stable_object_id(parent, 5, 0x3810, 2)
    unresolved_header_uid = stable_object_id(parent, 5, 0x541A, 0)
    parent_global_uid = stable_object_id(parent, 5, 0x5422, 2)
    outside_legacy_range_uid = stable_object_id(parent, 5, 0x5733, 2)
    unsupported_ehc_uid = stable_object_id(parent, 6, 0x3408, 2)
    entries = {
        "sensor.inactive": SimpleNamespace(
            entity_id="sensor.inactive",
            unique_id=inactive_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
            name="My custom entity name",
            area_id="zone-area",
            disabled_by="user",
        ),
        "sensor.active": SimpleNamespace(
            entity_id="sensor.active",
            unique_id=active_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.unresolved": SimpleNamespace(
            entity_id="sensor.unresolved",
            unique_id=unresolved_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "number.unresolved_active_slot": SimpleNamespace(
            entity_id="number.unresolved_active_slot",
            unique_id=unresolved_active_slot_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "number.unresolved_inactive_slot": SimpleNamespace(
            entity_id="number.unresolved_inactive_slot",
            unique_id=unresolved_inactive_slot_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "number.unresolved_cooling": SimpleNamespace(
            entity_id="number.unresolved_cooling",
            unique_id=unresolved_cooling_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "number.unresolved_catalog_slot": SimpleNamespace(
            entity_id="number.unresolved_catalog_slot",
            unique_id=unresolved_catalog_slot_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "number.unresolved_cp080_hk30": SimpleNamespace(
            entity_id="number.unresolved_cp080_hk30",
            unique_id=unresolved_cp080_hk30_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "number.unresolved_unproven_slot": SimpleNamespace(
            entity_id="number.unresolved_unproven_slot",
            unique_id=unresolved_unproven_slot_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "sensor.global": SimpleNamespace(
            entity_id="sensor.global",
            unique_id=global_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.unknown": SimpleNamespace(
            entity_id="sensor.unknown",
            unique_id=unknown_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
            name="Unknown remains recoverable",
            area_id="unknown-area",
            disabled_by="user",
        ),
        "sensor.override_disabled": SimpleNamespace(
            entity_id="sensor.override_disabled",
            unique_id=override_disabled_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.malformed_override": SimpleNamespace(
            entity_id="sensor.malformed_override",
            unique_id=malformed_override_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.initial_unknown": SimpleNamespace(
            entity_id="sensor.initial_unknown",
            unique_id=initial_unknown_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "number.unsupported_ehc_slot": SimpleNamespace(
            entity_id="number.unsupported_ehc_slot",
            unique_id=unsupported_ehc_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="number",
        ),
        "select.selector": SimpleNamespace(
            entity_id="select.selector",
            unique_id=selector_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="select",
        ),
        "sensor.non_zone": SimpleNamespace(
            entity_id="sensor.non_zone",
            unique_id=non_zone_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.unresolved_header": SimpleNamespace(
            entity_id="sensor.unresolved_header",
            unique_id=unresolved_header_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.noncanonical_uid": SimpleNamespace(
            entity_id="sensor.noncanonical_uid",
            unique_id=f"{inactive_uid}:suffix",
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.parent_global": SimpleNamespace(
            entity_id="sensor.parent_global",
            unique_id=parent_global_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.outside_legacy_range": SimpleNamespace(
            entity_id="sensor.outside_legacy_range",
            unique_id=outside_legacy_range_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "sensor.foreign_platform": SimpleNamespace(
            entity_id="sensor.foreign_platform",
            unique_id=inactive_uid,
            platform="other",
            config_entry_id="entry-one",
            domain="sensor",
        ),
        "button.foreign_domain": SimpleNamespace(
            entity_id="button.foreign_domain",
            unique_id=inactive_uid,
            platform=DOMAIN,
            config_entry_id="entry-one",
            domain="button",
        ),
        "sensor.other_entry": SimpleNamespace(
            entity_id="sensor.other_entry",
            unique_id=inactive_uid,
            platform=DOMAIN,
            config_entry_id="entry-two",
            domain="sensor",
        ),
    }

    class Registry:
        def __init__(self):
            self.entities = entries
            self.removed = []

        def async_remove(self, entity_id):
            self.removed.append(entity_id)

    registry = Registry()
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)

    assert register_entities.cleanup_inactive_zone_entities(object(), parent) == 10
    assert sorted(registry.removed) == sorted(
        [
            "sensor.inactive",
            "sensor.unresolved",
            "sensor.override_disabled",
            "sensor.initial_unknown",
            "number.unresolved_catalog_slot",
            "number.unresolved_cp080_hk30",
            "number.unresolved_active_slot",
            "number.unresolved_inactive_slot",
            "number.unresolved_cooling",
            "number.unsupported_ehc_slot",
        ]
    )
    # HA's async_remove persists a deleted-entity tombstone; this helper does
    # not mutate user metadata or remove device-registry rows.
    assert entries["sensor.inactive"].name == "My custom entity name"
    assert entries["sensor.inactive"].area_id == "zone-area"
    assert entries["sensor.inactive"].disabled_by == "user"
    assert entries["sensor.unknown"].entity_id == "sensor.unknown"
    assert entries["sensor.unknown"].name == "Unknown remains recoverable"
    assert entries["sensor.unknown"].area_id == "unknown-area"
    assert entries["sensor.unknown"].disabled_by == "user"
    assert entries["select.selector"].entity_id == "select.selector"
    assert entries["sensor.global"].entity_id == "sensor.global"
    assert entries["sensor.non_zone"].entity_id == "sensor.non_zone"
    assert entries["sensor.unresolved_header"].entity_id == "sensor.unresolved_header"
    assert entries["sensor.noncanonical_uid"].entity_id == "sensor.noncanonical_uid"
    assert (
        entries["number.unresolved_unproven_slot"].entity_id
        == "number.unresolved_unproven_slot"
    )
    assert entries["sensor.parent_global"].entity_id == "sensor.parent_global"
    assert (
        entries["sensor.outside_legacy_range"].entity_id
        == "sensor.outside_legacy_range"
    )
    assert entries["sensor.foreign_platform"].entity_id == "sensor.foreign_platform"
    assert entries["button.foreign_domain"].entity_id == "button.foreign_domain"


def test_zone_entity_is_unavailable_during_unknown_selector_state() -> None:
    entity = OpenRBusRegisterEntity.__new__(OpenRBusRegisterEntity)
    entity._register = _register("346a:01", levels=("User",))
    entity._identity = SimpleNamespace(node=4, family="Ehc-16")
    entity._parent = SimpleNamespace(
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2)},
        zone_profile_states={(4, 1): ZoneReadState.UNKNOWN},
    )
    entity._effective_access_level = 1
    entity.coordinator = SimpleNamespace(is_value_available=lambda *_args: True)

    assert not entity.available


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
    zone_cooling = _register("341a:01", levels=("User",))
    buffer_cooling = _register("3504:00", levels=("User",))
    explicit_screed = _register("3483:01", levels=("User",))
    screed_config = _register("344d:01", levels=("User",))
    unrelated_heating = _register("3500:02", levels=("User",))
    runtime_node = SimpleNamespace(identity=identity, capabilities={})
    parent = SimpleNamespace(
        inventories=(runtime_node,),
        devices=(),
        effective_access_levels={5: 1},
        configured_access_level=1,
        configured_read_access_level=1,
        config_entry=SimpleNamespace(data={}, options={}),
        zone_profiles={(5, 1): ZoneProfile(5, 1, 2)},
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
    # Active zone-scoped rows are projected independently of the optional
    # cooling/screed visibility flags.
    assert [row[1].address for row in filtered] == [
        zone_cooling.address,
        explicit_screed.address,
        screed_config.address,
        unrelated_heating.address,
    ]

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

    # Unresolved exact zone objects are excluded before platform construction;
    # they are not reprojected as parent entities by this visibility helper.
    assert register_entities.rows_for_parent(parent) == ()
    register_entities.async_apply_diagnostic_visibility(object(), parent)
    assert screed.disabled_by is None
    assert user_disabled.disabled_by == "user"

    parent.config_entry.options[CONF_SCREED_DRYING_ENABLED] = True
    assert register_entities.rows_for_parent(parent) == ()
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


def test_unknown_zone_profile_preserves_registry_and_user_choices(monkeypatch) -> None:
    enabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="zone-row",
        entity_id="sensor.zone",
        disabled_by=None,
    )
    integration_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="zone-row",
        entity_id="sensor.zone_disabled",
        disabled_by="integration",
    )
    user_disabled = SimpleNamespace(
        platform=DOMAIN,
        config_entry_id="entry",
        unique_id="zone-row",
        entity_id="sensor.zone_user_disabled",
        disabled_by="user",
    )

    class Registry:
        def __init__(self) -> None:
            self.entities = {
                item.entity_id: item
                for item in (enabled, integration_disabled, user_disabled)
            }

        def async_update_entity(self, entity_id, **changes):
            self.entities[entity_id].disabled_by = changes["disabled_by"]

    registry = Registry()
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id="entry",
            data={},
            options={
                "group_overrides": {
                    "device:4:category:unclassified": True,
                    "device:4:category:zone": True,
                }
            },
        ),
        entity_overrides={},
        effective_access_levels={},
        language="en",
        configured_access_level=1,
        write_enabled=False,
    )
    row = SimpleNamespace(
        readable=True,
        writable=False,
        write_declared=False,
        internal_code="zone row",
        address=ObjectAddress.parse("2001:02"),
    )
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_a, **_k: ((SimpleNamespace(node=4), row, "standard", True),),
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_a: "zone-row")
    monkeypatch.setattr(
        register_entities, "zone_subindex", lambda _register, _identity=None: 1
    )
    monkeypatch.setattr(register_entities, "zone_row_enabled", lambda *_a: False)
    monkeypatch.setattr(register_entities, "profile_for", lambda *_a: None)

    register_entities.async_apply_entity_overrides(object(), parent)

    assert enabled.disabled_by is None
    assert integration_disabled.disabled_by == "integration"
    assert user_disabled.disabled_by == "user"
