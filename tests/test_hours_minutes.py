"""Source-backed HoursMinutes display and time-control behavior."""

from datetime import time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from openrbus.catalog import catalog_for_node
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.protocol.canip import ObjectAddress

import custom_components.openrbus.time as time_module
from custom_components.openrbus import PLATFORMS, _async_forward_platforms
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.hours_minutes import (
    hours_minutes_to_text,
    hours_minutes_to_time,
    is_clock_hours_minutes,
    time_to_hours_minutes,
)
from custom_components.openrbus.register_entities import (
    control_kind,
    entity_unique_id,
    number_bounds,
)
from custom_components.openrbus.sensor import OpenRBusRegisterSensor
from custom_components.openrbus.time import OpenRBusTime, _enabled_number_row


def _row(family: str, address: str):
    device_family = {"EHC-16": "Ehc-16", "SCB-10": "Scb-10"}[family]
    identity = resolve_device_identity(
        DeviceIdentity(1, None, None, family, family=device_family)
    )
    register = next(
        item
        for item in catalog_for_node(identity)
        if item.address == ObjectAddress.parse(address)
    )
    return identity, register


def _parent():
    return SimpleNamespace(
        language="de",
        config_entry=SimpleNamespace(
            data={"ble_device": "00:11:22:33:44:55"}, entry_id="entry"
        ),
        write_enabled=True,
        configured_write_access_level=2,
        experimental_writes=True,
    )


def test_source_clock_rows_match_numeric_examples_and_leave_duration_numeric() -> None:
    _, start = _row("EHC-16", "235e:00")
    _, end = _row("EHC-16", "235f:00")
    _, scb_start = _row("SCB-10", "3429:01")
    _, duration = _row("SCB-10", "3475:01")

    assert [start.internal_code, end.internal_code, scb_start.internal_code] == [
        "HP094",
        "HP095",
        "CP390",
    ]
    assert all(is_clock_hours_minutes(row) for row in (start, end, scb_start))
    assert control_kind(start) == "number"
    assert duration.internal_code == "CP820"
    assert not is_clock_hours_minutes(duration)
    assert not is_clock_hours_minutes(
        SimpleNamespace(unit="HoursMinutes", datatype="ARRAY", internal_code="HP094")
    )


@pytest.mark.asyncio
async def test_fresh_entry_forwards_number_platform_before_time_companions() -> None:
    calls = []

    async def record_forward(_entry, platforms):
        calls.append(platforms)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=record_forward)
    )

    await _async_forward_platforms(hass, SimpleNamespace())

    assert calls == [
        [platform for platform in PLATFORMS if platform != "time"],
        ["time"],
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, "00:00"),
        (18, "03:00"),
        (72, "12:00"),
        (108, "18:00"),
        (143, "23:50"),
    ],
)
def test_ten_minute_values_format_as_clock_time(raw: int, expected: str) -> None:
    assert hours_minutes_to_text(raw) == expected
    assert hours_minutes_to_time(raw).isoformat() == f"{expected}:00"


@pytest.mark.parametrize("raw", [-1, 144, 255, True, 108.0, "108"])
def test_invalid_wire_values_do_not_become_clock_times(raw: object) -> None:
    assert hours_minutes_to_text(raw) is None
    assert hours_minutes_to_time(raw) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (time(0, 0), 0),
        (time(3, 0), 18),
        (time(12, 0), 72),
        (time(18, 0), 108),
        (time(23, 50), 143),
    ],
)
def test_time_values_roundtrip_only_on_ten_minute_boundaries(
    value: time, expected: int
) -> None:
    assert time_to_hours_minutes(value) == expected
    assert hours_minutes_to_time(expected) == value


@pytest.mark.parametrize(
    "value",
    [
        time(18, 5),
        time(18, 10, 1),
        time(18, 10, 0, 1),
        time(18, 10, tzinfo=timezone.utc),
    ],
)
def test_unrepresentable_time_values_are_rejected(value: time) -> None:
    with pytest.raises(ValueError):
        time_to_hours_minutes(value)


class _FakeEntityRegistry:
    def __init__(self, *entities):
        self.entities = {entity.entity_id: entity for entity in entities}

    def async_get_entity_id(self, domain, platform, unique_id):
        del platform
        entity = next(
            (
                item
                for item in self.entities.values()
                if item.domain == domain and item.unique_id == unique_id
            ),
            None,
        )
        return entity.entity_id if entity else None


@pytest.mark.parametrize("disabled_by", [None, "user", "integration"])
def test_time_companion_follows_the_existing_number_enabled_state(
    disabled_by: str | None,
) -> None:
    number = SimpleNamespace(
        entity_id="number.hp094",
        domain="number",
        unique_id="object:235e:00",
        disabled_by=disabled_by,
    )
    registry = _FakeEntityRegistry(number)

    enabled = _enabled_number_row(registry, number.unique_id)

    assert (enabled is not None) is (disabled_by is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_by", [None, "user", "integration"])
async def test_time_setup_adds_companion_only_for_enabled_number(
    monkeypatch, disabled_by: str | None
) -> None:
    identity, register = _row("EHC-16", "235e:00")
    parent = _parent()
    parent.effective_access_levels = {identity.node: 2}
    parent.configured_access_level = 2
    entry = SimpleNamespace(runtime_data=parent)
    number = SimpleNamespace(
        entity_id="number.hp094",
        domain="number",
        unique_id=entity_unique_id(parent, identity, register),
        disabled_by=disabled_by,
    )
    registry = _FakeEntityRegistry(number)
    coordinator = SimpleNamespace()
    added = []
    monkeypatch.setattr(time_module.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        time_module, "async_apply_diagnostic_visibility", lambda *_args: None
    )
    monkeypatch.setattr(
        time_module,
        "ensure_polling_coordinators",
        lambda *_args: {"standard": coordinator},
    )
    monkeypatch.setattr(
        time_module,
        "rows_for_parent",
        lambda *_args: ((identity, register, "standard", True),),
    )
    monkeypatch.setattr(time_module, "catalog_visible", lambda *_args: True)
    monkeypatch.setattr(time_module, "should_project_as_control", lambda *_args: True)
    monkeypatch.setattr(time_module, "number_bounds", lambda _register: (0, 143, 1))

    await time_module.async_setup_entry(
        SimpleNamespace(), entry, lambda entities: added.extend(entities)
    )

    if disabled_by is None:
        assert len(added) == 1
        assert added[0].unique_id == f"{number.unique_id}:time"
        assert added[0]._attr_entity_registry_enabled_default
    else:
        assert added == []


@pytest.mark.asyncio
async def test_enabled_time_companion_survives_disabled_legacy_number(monkeypatch):
    identity, register = _row("EHC-16", "235e:00")
    parent = _parent()
    parent.effective_access_levels = {identity.node: 2}
    parent.configured_access_level = 2
    entry = SimpleNamespace(runtime_data=parent)
    number = SimpleNamespace(
        entity_id="number.hp094",
        domain="number",
        unique_id=entity_unique_id(parent, identity, register),
        disabled_by="user",
    )
    existing_time = SimpleNamespace(
        entity_id="time.hp094_time",
        domain="time",
        unique_id=f"{number.unique_id}:time",
        disabled_by=None,
    )
    registry = _FakeEntityRegistry(number, existing_time)
    coordinator = SimpleNamespace()
    added = []
    monkeypatch.setattr(time_module.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        time_module, "async_apply_diagnostic_visibility", lambda *_args: None
    )
    monkeypatch.setattr(
        time_module,
        "ensure_polling_coordinators",
        lambda *_args: {"standard": coordinator},
    )
    monkeypatch.setattr(
        time_module,
        "rows_for_parent",
        lambda *_args: ((identity, register, "standard", True),),
    )
    monkeypatch.setattr(time_module, "catalog_visible", lambda *_args: True)
    monkeypatch.setattr(time_module, "should_project_as_control", lambda *_args: True)
    monkeypatch.setattr(time_module, "number_bounds", lambda _register: (0, 143, 1))

    await time_module.async_setup_entry(
        SimpleNamespace(), entry, lambda entities: added.extend(entities)
    )

    assert len(added) == 1
    assert added[0].unique_id == existing_time.unique_id


def test_time_entity_reads_native_time_and_keeps_raw_values() -> None:
    identity, register = _row("EHC-16", "235e:00")
    parent = _parent()
    result = GenericRead(1, register.address, b"\x6c", 108)
    coordinator = SimpleNamespace(
        data={(1, register.address): result},
        group="standard",
        last_update_success=True,
        is_value_available=lambda _node, _address: True,
    )
    entity = OpenRBusTime(
        parent,
        coordinator,
        identity,
        register,
        number_bounds(register),
        language="de",
        effective_access_level=2,
        enabled_by_default=True,
    )

    assert entity.native_value == time(18, 0)
    assert entity.unique_id.endswith(":time")
    assert entity.extra_state_attributes["raw_value"] == "6c"
    assert entity.extra_state_attributes["raw_numeric_value"] == 108
    assert entity.extra_state_attributes["formatted_time"] == "18:00"


def test_readonly_sensor_displays_clock_text_and_preserves_raw_value() -> None:
    identity, register = _row("EHC-16", "235e:00")
    entity = OpenRBusRegisterSensor.__new__(OpenRBusRegisterSensor)
    entity._parent = SimpleNamespace()
    entity._identity = identity
    entity._register = register
    entity._effective_access_level = 2
    entity.coordinator = SimpleNamespace(
        data={(1, register.address): GenericRead(1, register.address, b"\x6c", 108)},
        is_value_available=lambda _node, _address: True,
    )

    assert entity.native_value == "18:00"
    assert entity.available


def test_readonly_sensor_marks_invalid_clock_ticks_unavailable() -> None:
    identity, register = _row("EHC-16", "235e:00")
    entity = OpenRBusRegisterSensor.__new__(OpenRBusRegisterSensor)
    entity._parent = SimpleNamespace()
    entity._identity = identity
    entity._register = register
    entity._effective_access_level = 2
    entity.coordinator = SimpleNamespace(
        data={(1, register.address): GenericRead(1, register.address, b"\x90", 144)},
        is_value_available=lambda _node, _address: True,
    )

    assert entity.native_value is None
    assert not entity.available


@pytest.mark.asyncio
async def test_time_picker_converts_to_raw_ticks_without_bypassing_write_path() -> None:
    identity, register = _row("EHC-16", "235e:00")
    parent = _parent()
    coordinator = SimpleNamespace()
    entity = OpenRBusTime(
        parent,
        coordinator,
        identity,
        register,
        (0, 143, 1),
        language="de",
        effective_access_level=2,
        enabled_by_default=True,
    )
    entity._async_write = AsyncMock()

    await entity.async_set_value(time(18, 0))

    entity._async_write.assert_awaited_once_with(108)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [time(18, 5), time(18, 10, 1)])
async def test_time_picker_rejects_values_that_do_not_fit_the_wire_unit(
    value: time,
) -> None:
    identity, register = _row("EHC-16", "235e:00")
    entity = OpenRBusTime(
        _parent(),
        SimpleNamespace(),
        identity,
        register,
        (0, 143, 1),
        language="de",
        effective_access_level=2,
        enabled_by_default=True,
    )
    entity._async_write = AsyncMock()

    with pytest.raises(HomeAssistantError, match="ten-minute boundary"):
        await entity.async_set_value(value)

    entity._async_write.assert_not_awaited()


def test_time_entity_rejects_invalid_read_ticks_and_preserves_them_in_attributes() -> (
    None
):
    identity, register = _row("EHC-16", "235e:00")
    parent = _parent()
    coordinator = SimpleNamespace(
        data={(1, register.address): GenericRead(1, register.address, b"\x90", 144)},
        group="standard",
        last_update_success=True,
        is_value_available=lambda _node, _address: True,
    )
    entity = OpenRBusTime(
        parent,
        coordinator,
        identity,
        register,
        number_bounds(register),
        language="de",
        effective_access_level=2,
        enabled_by_default=True,
    )

    assert entity.native_value is None
    assert not entity.available
    assert entity.extra_state_attributes["raw_numeric_value"] == 144
    assert entity.extra_state_attributes["raw_value"] == "90"


@pytest.mark.asyncio
async def test_time_picker_rejects_register_constraint_overflow() -> None:
    identity, register = _row("EHC-16", "235e:00")
    entity = OpenRBusTime(
        _parent(),
        SimpleNamespace(),
        identity,
        register,
        (0, 142, 1),
        language="de",
        effective_access_level=2,
        enabled_by_default=True,
    )
    entity._async_write = AsyncMock()

    with pytest.raises(HomeAssistantError, match="source-defined range"):
        await entity.async_set_value(time(23, 50))

    entity._async_write.assert_not_awaited()
