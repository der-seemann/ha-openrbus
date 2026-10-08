"""Read-only entity visibility remains independent from control authorization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.protocol.canip import ObjectAddress

import custom_components.openrbus.config_flow as config_flow_module
import custom_components.openrbus.coordinator as coordinator_module
from custom_components.openrbus import register_entities
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.config_flow import OpenRBusOptionsFlowHandler
from custom_components.openrbus.const import (
    CONF_DIAGNOSTICS_ENABLED,
    CONF_GROUP_OVERRIDES,
    CONF_NODE_OVERRIDES,
    DOMAIN,
)
from custom_components.openrbus.coordinator import OpenRBusPollingCoordinator
from custom_components.openrbus.number import OpenRBusNumber
from custom_components.openrbus.select import OpenRBusSelect
from custom_components.openrbus.switch import OpenRBusSwitch


def _catalog_row(name: str, address: str) -> SimpleNamespace:
    return SimpleNamespace(
        internal_code=name,
        address=ObjectAddress.parse(address),
        readable=True,
        writable=True,
        datatype="UINT16",
        unit=None,
        safety="unverified",
        write_classification="experimental",
        access_level_evidence={
            "read": {"levels": ["User"]},
            "write": {
                "known": True,
                "complete": True,
                "levels": ["Professional"],
            },
        },
    )


class _EntityRegistry:
    def __init__(self, *entities):
        self.entities = {entity.entity_id: entity for entity in entities}

    def async_get_entity_id(self, domain, platform, unique_id):
        del platform
        return next(
            (
                entity.entity_id
                for entity in self.entities.values()
                if entity.domain == domain and entity.unique_id == unique_id
            ),
            None,
        )

    def async_update_entity(self, entity_id, **changes):
        self.entities[entity_id].disabled_by = changes["disabled_by"]


def _registry_entity(domain, unique_id, disabled_by="integration"):
    return SimpleNamespace(
        platform=DOMAIN,
        domain=domain,
        config_entry_id="entry",
        unique_id=unique_id,
        entity_id=f"{domain}.{unique_id.replace('-', '_')}",
        disabled_by=disabled_by,
        disabled=disabled_by is not None,
    )


def test_readonly_fallback_reenables_without_authorizing_typed_control(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=3)
    sensor_row = _catalog_row("sensor-fallback", "5501:01")
    typed_row = _catalog_row("typed-control", "5501:02")
    hidden_row = _catalog_row("diagnostic-row", "5501:03")
    unreadable_row = _catalog_row("unreadable-row", "5501:04")
    inactive_row = _catalog_row("inactive-row", "5501:05")
    bitfield_row = _catalog_row("bitfield-row", "5501:06")
    scope_off_row = _catalog_row("scope-off-row", "5501:07")
    rows = (
        (identity, sensor_row, "standard", True),
        (identity, typed_row, "standard", True),
        (identity, hidden_row, "standard", True),
        (identity, unreadable_row, "standard", False),
        (identity, inactive_row, "standard", True),
        (identity, bitfield_row, "standard", True),
        (identity, scope_off_row, "standard", True),
    )
    sensor = _registry_entity("sensor", sensor_row.internal_code)
    typed = _registry_entity("number", typed_row.internal_code)
    hidden = _registry_entity("sensor", hidden_row.internal_code)
    unreadable = _registry_entity("sensor", unreadable_row.internal_code)
    inactive = _registry_entity("sensor", inactive_row.internal_code)
    bitfield = _registry_entity("binary_sensor", "bitfield-row:bit:active")
    scope_off = _registry_entity("sensor", scope_off_row.internal_code)
    explicit_off = _registry_entity("sensor", "explicit-off")
    user_disabled = _registry_entity("sensor", "user-disabled", "user")
    registry = _EntityRegistry(
        sensor,
        typed,
        hidden,
        unreadable,
        inactive,
        bitfield,
        scope_off,
        explicit_off,
        user_disabled,
    )

    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(register_entities, "rows_for_parent", lambda *_a, **_k: rows)
    monkeypatch.setattr(
        register_entities,
        "entity_unique_id",
        lambda _parent, _identity, register: register.internal_code,
    )
    monkeypatch.setattr(
        register_entities,
        "entity_group_key",
        lambda _parent, _identity, register: (
            "scope-off" if register is scope_off_row else "group"
        ),
    )
    monkeypatch.setattr(register_entities, "entity_category_key", lambda *_: "category")
    monkeypatch.setattr(
        register_entities,
        "entity_category_override_keys",
        lambda *_: ("category",),
    )
    monkeypatch.setattr(register_entities, "control_kind", lambda *_: "number")
    monkeypatch.setattr(register_entities, "write_access_allowed", lambda *_: False)
    monkeypatch.setattr(
        register_entities,
        "bitfield_structure",
        lambda register: (
            SimpleNamespace(fields=(SimpleNamespace(bit_length=1, name="active"),))
            if register is bitfield_row
            else None
        ),
    )
    monkeypatch.setattr(
        register_entities,
        "_unobserved_source_rw_row",
        lambda *_: False,
    )
    monkeypatch.setattr(
        register_entities, "recommended_addresses", lambda _identity: frozenset()
    )
    monkeypatch.setattr(
        register_entities,
        "is_diagnostic_register",
        lambda register: register is hidden_row,
    )
    monkeypatch.setattr(
        register_entities,
        "is_optional_filter_register",
        lambda *_: False,
    )
    monkeypatch.setattr(
        register_entities,
        "zone_row_enabled",
        lambda _parent, _identity, register: register is not inactive_row,
    )

    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id="entry",
            data={"ble_device": "00:11:22:33:44:55"},
            options={
                CONF_DIAGNOSTICS_ENABLED: False,
                CONF_GROUP_OVERRIDES: {"scope-off": False},
            },
        ),
        entity_overrides={
            sensor_row.internal_code: True,
            typed_row.internal_code: True,
            hidden_row.internal_code: True,
            unreadable_row.internal_code: True,
            inactive_row.internal_code: True,
            "bitfield-row:bit:active": True,
            scope_off_row.internal_code: None,
            "explicit-off": False,
            "user-disabled": True,
        },
        effective_access_levels={3: 3},
        language="en",
    )

    register_entities.async_apply_entity_overrides(object(), parent)

    assert sensor.disabled_by is None
    assert typed.disabled_by == "integration"
    assert hidden.disabled_by == "integration"
    assert unreadable.disabled_by == "integration"
    assert inactive.disabled_by == "integration"
    assert bitfield.disabled_by is None
    assert scope_off.disabled_by == "integration"
    assert explicit_off.disabled_by == "integration"
    assert user_disabled.disabled_by == "user"


def test_diagnostic_toggle_reenables_readonly_fallback_but_not_control(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=3)
    row = _catalog_row("diagnostic", "5501:01")
    sensor = _registry_entity("sensor", "diagnostic")
    typed = _registry_entity("number", "diagnostic")
    registry = _EntityRegistry(sensor, typed)
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id="entry",
            data={"ble_device": "00:11:22:33:44:55"},
            options={CONF_DIAGNOSTICS_ENABLED: True},
        ),
        inventories=(),
        devices=(),
        effective_access_levels={3: 3},
        language="en",
    )
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_a, **_k: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(register_entities, "entity_unique_id", lambda *_: "diagnostic")
    monkeypatch.setattr(register_entities, "is_diagnostic_register", lambda _row: True)
    monkeypatch.setattr(
        register_entities, "is_optional_filter_register", lambda *_: False
    )
    monkeypatch.setattr(register_entities, "control_kind", lambda *_: "number")
    monkeypatch.setattr(register_entities, "write_access_allowed", lambda *_: False)
    monkeypatch.setattr(register_entities, "bitfield_structure", lambda _row: None)
    monkeypatch.setattr(register_entities, "zone_row_enabled", lambda *_: True)

    register_entities.async_apply_diagnostic_visibility(object(), parent)

    assert sensor.disabled_by is None
    assert typed.disabled_by == "integration"


def test_options_picker_keeps_readable_control_candidate_visible_when_write_blocked(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=3, display_name="Gateway")
    row = _catalog_row("read-control", "5501:01")
    entry = SimpleNamespace(
        data={CONF_DIAGNOSTICS_ENABLED: True},
        options={},
        runtime_data=SimpleNamespace(language="en"),
    )
    flow = OpenRBusOptionsFlowHandler(entry)
    monkeypatch.setattr(
        config_flow_module,
        "rows_for_parent",
        lambda *_a, **_k: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(config_flow_module, "zone_row_enabled", lambda *_: True)
    monkeypatch.setattr(
        config_flow_module, "entity_unique_id", lambda *_: "read-control"
    )
    monkeypatch.setattr(
        config_flow_module, "register_name", lambda *_: "Operating mode"
    )
    monkeypatch.setattr(config_flow_module, "bitfield_structure", lambda _row: None)
    monkeypatch.setattr(
        config_flow_module, "entity_enabled_by_default", lambda *_a, **_k: True
    )

    assert flow._entity_choices() == {"read-control": "3: Operating mode"}
    assert flow._entity_default_selection() == ["read-control"]


@pytest.mark.parametrize(
    ("entity_overrides", "options"),
    (
        ({"read-control": False}, {}),
        ({}, {CONF_GROUP_OVERRIDES: {"group": False}}),
        ({}, {CONF_NODE_OVERRIDES: {"3": False}}),
    ),
)
def test_explicit_picker_or_scope_disable_overrides_manual_registry_enable(
    monkeypatch, entity_overrides, options
) -> None:
    identity = SimpleNamespace(node=3)
    row = _catalog_row("read-control", "5501:01")
    row.write_declared = True
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry", data={}, options=options),
        inventories=(SimpleNamespace(identity=identity, capabilities={}),),
        entity_overrides=entity_overrides,
        zone_profiles={},
        zone_overrides={},
        language="en",
    )
    enabled_sensor = _registry_entity("sensor", "read-control", disabled_by=None)
    monkeypatch.setattr(
        register_entities, "entity_unique_id", lambda *_: "read-control"
    )
    monkeypatch.setattr(
        register_entities, "recommended_addresses", lambda *_: frozenset()
    )
    monkeypatch.setattr(register_entities, "entity_group_key", lambda *_: "group")
    monkeypatch.setattr(register_entities, "entity_category_key", lambda *_: "category")
    monkeypatch.setattr(
        register_entities, "entity_category_override_keys", lambda *_: ("category",)
    )
    monkeypatch.setattr(register_entities, "zone_row_enabled", lambda *_: True)

    assert not register_entities._poll_row_selected(
        parent,
        identity,
        row,
        {"read-control": (enabled_sensor,)},
    )


def test_manually_enabled_binary_bit_projection_selects_its_parent_read(
    monkeypatch,
) -> None:
    identity = SimpleNamespace(node=3)
    row = _catalog_row("bitfield-row", "5501:01")
    row.write_declared = True
    parent = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="entry", data={}, options={}),
        inventories=(SimpleNamespace(identity=identity, capabilities={}),),
        entity_overrides={},
        zone_profiles={},
        zone_overrides={},
        language="en",
    )
    enabled_bit = _registry_entity(
        "binary_sensor", "bitfield-row:bit:active", disabled_by=None
    )
    monkeypatch.setattr(
        register_entities, "entity_unique_id", lambda *_: "bitfield-row"
    )
    monkeypatch.setattr(
        register_entities, "recommended_addresses", lambda *_: frozenset()
    )
    monkeypatch.setattr(register_entities, "entity_group_key", lambda *_: "group")
    monkeypatch.setattr(register_entities, "entity_category_key", lambda *_: "category")
    monkeypatch.setattr(
        register_entities, "entity_category_override_keys", lambda *_: ("category",)
    )
    monkeypatch.setattr(register_entities, "zone_row_enabled", lambda *_: True)

    assert register_entities._poll_row_selected(
        parent,
        identity,
        row,
        {"bitfield-row:bit:active": (enabled_bit,)},
    )


@pytest.mark.asyncio
async def test_enabled_readonly_sensor_polls_while_experimental_write_stays_denied(
    monkeypatch,
) -> None:
    address = ObjectAddress.parse("5501:01")
    row = _catalog_row("read-control", "5501:01")
    row.write_declared = True
    reads = []
    writes = AsyncMock()

    async def async_read_objects(addresses, *, node, **_kwargs):
        reads.append((node, tuple(addresses)))
        return tuple(GenericRead(node, item, b"\x01", 1) for item in addresses)

    identity = SimpleNamespace(node=3)
    runtime_node = SimpleNamespace(identity=identity, capabilities={})
    enabled_sensor = _registry_entity("sensor", "read-control", disabled_by=None)
    registry = _EntityRegistry(enabled_sensor)
    config_entry = SimpleNamespace(
        entry_id="entry",
        data={},
        options={},
        async_on_unload=lambda _callback: None,
    )
    parent = SimpleNamespace(
        _shutting_down=False,
        backend_mode="native",
        write_enabled=True,
        experimental_writes=False,
        configured_write_access_level=3,
        configured_read_access_level=3,
        configured_access_level=3,
        effective_access_levels={3: 3},
        config_entry=config_entry,
        inventories=(runtime_node,),
        devices=(),
        entity_overrides={},
        language="en",
        zone_profiles={},
        zone_overrides={},
        poll_intervals={"fast": 1, "standard": 10, "slow": 60},
        async_read_objects=async_read_objects,
        async_write_object=writes,
    )
    monkeypatch.setattr(coordinator_module.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        coordinator_module,
        "stable_object_id",
        lambda *_: "read-control",
    )
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_a, **_k: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(
        register_entities, "_poll_selection_filter_counts", lambda *_: {}
    )
    monkeypatch.setattr(register_entities, "_poll_activation_counts", lambda *_: {})
    monkeypatch.setattr(
        register_entities, "entity_unique_id", lambda *_: "read-control"
    )
    monkeypatch.setattr(
        register_entities, "recommended_addresses", lambda *_: frozenset()
    )
    monkeypatch.setattr(register_entities, "entity_group_key", lambda *_: "group")
    monkeypatch.setattr(register_entities, "entity_category_key", lambda *_: "category")
    monkeypatch.setattr(
        register_entities, "entity_category_override_keys", lambda *_: ("category",)
    )
    monkeypatch.setattr(register_entities, "zone_row_enabled", lambda *_: True)
    monkeypatch.setattr(register_entities, "bitfield_structure", lambda *_: None)
    monkeypatch.setattr(register_entities, "control_kind", lambda *_: "number")
    monkeypatch.setattr(register_entities, "catalog_visible", lambda *_: True)

    class PollingHarness:
        _object_failure_scope = OpenRBusPollingCoordinator._object_failure_scope
        _bump = staticmethod(OpenRBusPollingCoordinator._bump)

        def __init__(
            self,
            hass,
            selected_parent,
            group,
            registers,
            _interval,
            *,
            register_metadata=None,
        ):
            self.hass = hass
            self.parent = selected_parent
            self.group = group
            self.registers = registers
            self.register_metadata = register_metadata or {}
            self.validity = SimpleNamespace(
                is_expired=lambda _key: False,
                is_quarantined=lambda _key, _scope: False,
                observe=lambda *_a: SimpleNamespace(expired=False),
                is_valid=lambda *_a: True,
                quarantine_deterministic_failure=lambda *_a: None,
            )
            self._diagnostic_poll_count = 0
            self._diagnostic_success_items = 0
            self._diagnostic_failed_items = 0
            self._diagnostic_error_counts = {}
            self._diagnostic_subtype_counts = {}
            self._diagnostic_item_failures = []
            self._diagnostic_available_items = 0
            self._diagnostic_total_items = 0
            self._diagnostic_availability_delta = 0
            self._diagnostic_registry_disabled_count = 0

        async def async_shutdown(self):
            return None

        async def async_poll(self):
            return await OpenRBusPollingCoordinator._async_poll_data(self)

    monkeypatch.setattr(
        register_entities,
        "OpenRBusPollingCoordinator",
        PollingHarness,
    )
    monkeypatch.setattr(
        register_entities,
        "schedule_first_refresh_in_background",
        lambda *_a, **_k: None,
    )
    hass = SimpleNamespace(data={})
    polling = register_entities.ensure_polling_coordinators(hass, parent)
    poller = polling["standard"]

    result = await poller.async_poll()

    assert result[(3, address)].value == 1
    assert reads == [(3, (address,))]
    assert not register_entities.write_access_allowed(parent, row, 3)

    parent._openrbus_polling_coordinators = {}
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda *_a, **_k: ((identity, row, "standard", False),),
    )
    read_denied_pollers = register_entities.ensure_polling_coordinators(
        SimpleNamespace(data={}), parent
    )
    assert all(not selected.registers for selected in read_denied_pollers.values())

    for entity_type, action in (
        (OpenRBusNumber, lambda entity: entity.async_set_native_value(1)),
        (
            OpenRBusSelect,
            lambda entity: entity.async_select_option("On"),
        ),
        (OpenRBusSwitch, lambda entity: entity.async_turn_on()),
    ):
        entity = entity_type.__new__(entity_type)
        entity._parent = parent
        entity._identity = identity
        entity._register = row
        entity._effective_access_level = 3
        if entity_type is OpenRBusSelect:
            entity._option_to_value = {"On": 1}
        with pytest.raises(HomeAssistantError, match="write access is unavailable"):
            await action(entity)
    writes.assert_not_awaited()
