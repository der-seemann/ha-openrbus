"""Focused HA catalog-to-entity projection tests."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from homeassistant.helpers import translation as ha_translation
from openrbus.catalog import catalog_for_node
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.protocol.canip import ObjectAddress
from openrbus.value_codec import CanOpenTimeOfDay

from custom_components.openrbus import register_entities as register_entities_module
from custom_components.openrbus import sensor as sensor_module
from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.entity_names import suggested_object_id
from custom_components.openrbus.identity import stable_object_id
from custom_components.openrbus.register_entities import (
    control_kind,
    entity_unique_id,
    is_cumulative_energy_counter,
    is_temperature_measurement,
    should_project_as_control,
)
from custom_components.openrbus.sensor import (
    OpenRBusRegisterSensor,
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
        write_classification="regular",
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


def _register_sensor(identity, register, value, language: str = "de"):
    raw_value = bytes([value]) if type(value) is int else b""
    coordinator = SimpleNamespace(
        group="standard",
        data={
            (identity.node, register.address): GenericRead(
                identity.node, register.address, raw_value, value
            )
        },
    )
    parent = SimpleNamespace(
        language=language,
        config_entry=SimpleNamespace(
            data={"ble_device": "00:11:22:33:44:55"}, options={}
        ),
        write_enabled=False,
        zone_profiles={},
        zone_overrides={},
    )
    return OpenRBusRegisterSensor(
        parent,
        coordinator,
        identity,
        register,
        language=language,
        effective_access_level=3,
    )


def _status_sensor(address: str, value, language: str = "de"):
    identity = resolve_device_identity(
        DeviceIdentity(1, None, None, "EHC-16", family="Ehc-16")
    )
    register = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse(address)
    )
    return _register_sensor(identity, register, value, language)


def test_status_and_substatus_sensors_show_catalogued_german_labels() -> None:
    status = _status_sensor("500f:00", 1)
    substatus = _status_sensor("5011:00", 1)

    assert status.native_value == "Wärmeanforderung"
    assert substatus.native_value == "Pausenzeit"
    assert status.extra_state_attributes["raw_numeric_value"] == 1
    assert substatus.extra_state_attributes["raw_numeric_value"] == 1
    assert status.native_unit_of_measurement is None
    assert substatus.native_unit_of_measurement is None
    assert status.extra_state_attributes["decoder_label_known"] is True
    assert substatus.extra_state_attributes["decoder_label_known"] is True
    reset_limit = _status_sensor("5011:00", 255)
    assert reset_limit.native_value == "reset_limit_wait_one_hour"
    assert reset_limit.translation_key == "register_status"


def test_unproven_statuses_keep_numeric_values_with_raw_attributes() -> None:
    protection = _status_sensor("4356:00", 11)
    error = _status_sensor("43b6:01", Decimal("655.35"))

    assert protection.native_value == 11
    assert protection.translation_key == "register_status"
    assert protection.extra_state_attributes["raw_numeric_value"] == 11
    assert protection.extra_state_attributes["unknown_decoder"] is True
    assert protection.extra_state_attributes["decoder_label_known"] is False
    assert error.native_value == 655.35
    assert error.extra_state_attributes["raw_numeric_value"] == 655.35
    assert error.extra_state_attributes["unknown_decoder"] is True
    encoded = json.loads(
        json.dumps({"state": error.native_value, **error.extra_state_attributes})
    )
    assert encoded["state"] == encoded["raw_numeric_value"] == 655.35


def test_unknown_enum_code_is_numeric_and_keeps_raw_value() -> None:
    unknown = _status_sensor("500f:00", 255)
    assert unknown.native_value == 255
    assert unknown.extra_state_attributes["raw_numeric_value"] == 255
    assert unknown.extra_state_attributes["unknown_decoder"] is True
    assert (
        json.loads(json.dumps(unknown.extra_state_attributes))["raw_numeric_value"]
        == 255
    )


def test_enum_sensor_keeps_unknown_values_and_existing_text_unchanged() -> None:
    unknown = _status_sensor("500f:00", 253)
    text_value = _status_sensor("5011:00", "vendor text")

    assert unknown.native_value == 253
    assert unknown.extra_state_attributes["raw_numeric_value"] == 253
    assert text_value.native_value == "vendor text"
    assert text_value.extra_state_attributes["raw_numeric_value"] is None


def test_odu_modulation_level_has_source_backed_name_and_no_percent_semantics() -> None:
    identity = resolve_device_identity(
        DeviceIdentity(1, None, None, "EHC-16", family="Ehc-16")
    )
    register = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse("434b:00")
    )
    entity = _register_sensor(identity, register, 120)
    assert entity.name == "Modulationsgrad der Wärmepumpen-Außeneinheit"
    assert entity.native_unit_of_measurement is None
    assert entity.device_class is None
    assert entity.state_class is None
    assert suggested_object_id(
        SimpleNamespace(language="de"), identity, register
    ).endswith("odu_modulation_level")
    assert entity.unique_id == stable_object_id(entity._parent, 1, 0x434B, 0)


def test_enum_sensor_does_not_treat_boolean_as_numeric_enum() -> None:
    entity = _status_sensor("500f:00", True)

    assert entity.native_value is True
    assert entity.extra_state_attributes["raw_numeric_value"] is None


def test_energy_counters_are_sensor_only_with_total_increasing_semantics() -> None:
    identity = resolve_device_identity(DeviceIdentity(1, 528, None, "EHC-16"))
    rows = catalog_for_node(identity)
    scalar = next(row for row in rows if row.address == ObjectAddress.parse("5044:00"))
    child = next(row for row in rows if row.address == ObjectAddress.parse("512c:01"))
    header = next(row for row in rows if row.address == ObjectAddress.parse("512c:00"))
    assert is_cumulative_energy_counter(scalar)
    assert is_cumulative_energy_counter(child)
    assert not is_cumulative_energy_counter(header)
    assert not is_cumulative_energy_counter(replace(child, index=0x512C, subindex=6))

    scalar_sensor = _register_sensor(identity, scalar, 0)
    child_sensor = _register_sensor(identity, child, Decimal("2413.6"))
    assert scalar_sensor.device_class.value == "energy"
    assert scalar_sensor.state_class.value == "total_increasing"
    assert child_sensor.device_class.value == "energy"
    assert child_sensor.state_class.value == "total_increasing"
    assert scalar_sensor.native_value == 0
    assert child_sensor.native_value == Decimal("2413.6")
    assert child_sensor.extra_state_attributes["raw_numeric_value"] == 2413.6
    json.dumps(child_sensor.extra_state_attributes)
    assert _register_sensor(identity, scalar, Decimal(-1)).native_value is None
    assert _register_sensor(identity, scalar, True).native_value is None
    assert _register_sensor(identity, scalar, float("inf")).native_value is None


def test_temperature_class_only_marks_source_coded_measurements() -> None:
    identity = resolve_device_identity(DeviceIdentity(1, 528, None, "EHC-16"))
    rows = catalog_for_node(identity)
    measured = next(
        row for row in rows if row.address == ObjectAddress.parse("4301:00")
    )
    setpoint = next(
        row for row in rows if row.address == ObjectAddress.parse("4303:00")
    )
    assert is_temperature_measurement(measured)
    assert not is_temperature_measurement(setpoint)
    sensor = _register_sensor(identity, measured, 36)
    assert sensor.device_class.value == "temperature"
    assert sensor.state_class.value == "measurement"
    decimal_sensor = _register_sensor(identity, measured, Decimal("36.125"))
    assert decimal_sensor.extra_state_attributes["raw_numeric_value"] == 36.125
    json.dumps(decimal_sensor.extra_state_attributes)
    overflow_scale = replace(measured, scale=Decimal("1e310"))
    overflow_sensor = _register_sensor(
        identity, overflow_scale, Decimal("1" + "0" * 309 + ".5")
    )
    assert overflow_sensor.extra_state_attributes["raw_numeric_value"] is None
    json.dumps(overflow_sensor.extra_state_attributes)
    assert _register_sensor(identity, measured, float("nan")).native_value is None
    assert _register_sensor(identity, setpoint, 40).device_class is None


def test_temperature_projection_refuses_a_core_type_conflict(monkeypatch):
    identity = resolve_device_identity(DeviceIdentity(1, 528, None, "EHC-16"))
    measured = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse("4301:00")
    )

    class ConflictedRegistry:
        def find(self, _address):
            return SimpleNamespace(
                address=measured.address,
                code=measured.internal_code,
                wire=SimpleNamespace(
                    type=SimpleNamespace(value=measured.datatype),
                    storage=SimpleNamespace(value=measured.storage),
                    unit="°C",
                    gain=1,
                    is_array=False,
                ),
                evidence=SimpleNamespace(type_conflict=True),
            )

    monkeypatch.setattr(
        register_entities_module, "CATALOG_REGISTRY", ConflictedRegistry()
    )
    assert not is_temperature_measurement(measured)


def test_status_translation_contract_is_static_and_uses_ha_state_translation(
    monkeypatch,
):
    entity = _status_sensor("5011:00", 255)
    assert entity.translation_key == "register_status"
    # Translation identity is assigned at construction, before a first read is
    # available, so HA's cached entity translation contract remains stable.
    translations = {
        "component.openrbus.entity.sensor.register_status.state.reset_limit_wait_one_hour": "Rücksetzlimit erreicht – eine Stunde warten (Substatus 255)",
        "component.openrbus.entity.sensor.register_status.state.unknown_status_code": "Unbekannter Statuscode",
        "component.openrbus.entity.sensor.register_status.state.unknown_error_code": "Unbekannter Fehlercode",
        "component.openrbus.entity.sensor.register_status.state.unknown_code": "Unbekannter Code",
    }
    monkeypatch.setattr(
        ha_translation, "async_get_cached_translations", lambda *_: translations
    )
    hass = SimpleNamespace(config=SimpleNamespace(language="de"))
    assert (
        ha_translation.async_translate_state(
            hass,
            entity.native_value,
            "sensor",
            "openrbus",
            entity.translation_key,
            None,
        )
        == translations[
            "component.openrbus.entity.sensor.register_status.state.reset_limit_wait_one_hour"
        ]
    )
    unknown = _status_sensor("500f:00", 255)
    assert unknown.native_value == 255
    assert unknown.translation_key == "register_status"
    no_read = _status_sensor("5011:00", None)
    assert no_read.native_value is None
    assert no_read.translation_key == "register_status"

    integration_path = Path(__file__).parents[1] / "custom_components" / "openrbus"
    strings = json.loads((integration_path / "strings.json").read_text())
    english = json.loads((integration_path / "translations" / "en.json").read_text())
    german = json.loads((integration_path / "translations" / "de.json").read_text())
    state_keys = set(strings["entity"]["sensor"]["register_status"]["state"])
    assert state_keys == set(english["entity"]["sensor"]["register_status"]["state"])
    assert state_keys == set(german["entity"]["sensor"]["register_status"]["state"])
    english_translations = {
        f"component.openrbus.entity.sensor.register_status.state.{key}": value
        for key, value in english["entity"]["sensor"]["register_status"][
            "state"
        ].items()
    }
    monkeypatch.setattr(
        ha_translation,
        "async_get_cached_translations",
        lambda *_: english_translations,
    )
    hass.config.language = "en"
    assert (
        ha_translation.async_translate_state(
            hass,
            "reset_limit_wait_one_hour",
            "sensor",
            "openrbus",
            "register_status",
            None,
        )
        == english_translations[
            "component.openrbus.entity.sensor.register_status.state.reset_limit_wait_one_hour"
        ]
    )


def test_model_aliases_resolve_to_canonical_core_families() -> None:
    cases = (
        ("GTW-Bluetooth", "Gtw-22"),
        ("MK3", "Mk-3"),
        ("GTW-08", "IoTGTW-1"),
    )
    for model, family in cases:
        resolved = resolve_device_identity(DeviceIdentity(3, None, None, model))
        assert resolved.family == family
        assert catalog_for_node(resolved)


def test_scb_capability_register_requires_runtime_capability_evidence() -> None:
    identity = resolve_device_identity(
        DeviceIdentity(4, None, None, "SCB-10", family="Scb-10")
    )
    address = ObjectAddress.parse("5800:01")
    assert all(row.address != address for row in catalog_for_node(identity))
    assert any(
        row.address == address
        for row in catalog_for_node(identity, capabilities=[address])
    )


def test_array_enum_sensor_formats_only_bounded_elements_not_count_header() -> None:
    identity = resolve_device_identity(
        DeviceIdentity(4, None, None, "SCB-10", family="Scb-10")
    )
    head = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse("3811:00")
    )

    count_sensor = _register_sensor(identity, head, 1)
    assert count_sensor.native_value == 1
    assert count_sensor.extra_state_attributes["raw_numeric_value"] == 1

    for subindex in (1, 6, 10):
        child = replace(head, index=0x3811, subindex=subindex)
        sensor = _register_sensor(identity, child, 1)
        assert sensor.native_value == "Erzeugersperre HK"
        assert sensor.native_unit_of_measurement is None

    overflow = replace(head, index=0x3811, subindex=11)
    assert _register_sensor(identity, overflow, 1).native_value is None


def test_enum_sensor_rejects_inconsistent_catalog_wire_metadata() -> None:
    identity = resolve_device_identity(
        DeviceIdentity(1, None, None, "EHC-16", family="Ehc-16")
    )
    status = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse("500f:00")
    )

    assert (
        _register_sensor(identity, replace(status, datatype="UINT8"), 1).native_value
        is None
    )
    assert (
        _register_sensor(identity, replace(status, storage="UINT16"), 1).native_value
        is None
    )
    with_unit = replace(status, unit="°C")
    unit_sensor = _register_sensor(identity, with_unit, 1)
    assert unit_sensor.native_value is None
    assert unit_sensor.native_unit_of_measurement == "°C"


def test_enum_sensor_rejects_catalog_type_conflict(monkeypatch) -> None:
    identity = resolve_device_identity(
        DeviceIdentity(1, None, None, "EHC-16", family="Ehc-16")
    )
    status = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse("500f:00")
    )

    class ConflictedRegistry:
        def find(self, _address):
            return SimpleNamespace(
                address=status.address,
                code=status.internal_code,
                wire=SimpleNamespace(
                    type=SimpleNamespace(value="ENUMERATION"),
                    storage=SimpleNamespace(value="UINT8"),
                    unit=None,
                    is_array=False,
                ),
                evidence=SimpleNamespace(type_conflict=True),
            )

    monkeypatch.setattr(sensor_module, "_CATALOG_REGISTRY", ConflictedRegistry())
    assert _register_sensor(identity, status, 1).native_value is None


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


def test_obd_only_writable_declaration_needs_experimental_opt_in() -> None:
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))
    address = ObjectAddress(0x1000, 0)
    ordinary = next(
        row
        for row in catalog_for_node(identity, capabilities=(address,))
        if row.address == address
    )
    catalog_experimental = next(
        row
        for row in catalog_for_node(
            identity, capabilities=(address,), experimental_writes=True
        )
        if row.address == address
    )
    experimental = replace(
        catalog_experimental,
        writable=True,
        access_level_evidence={
            "write": {"known": True, "complete": True, "levels": ["user"]}
        },
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
    assert ordinary.write_classification == "experimental"
    assert ordinary.writable is False
    assert not should_project_as_control(parent, ordinary, 1)
    parent.experimental_writes = True
    assert experimental.writable is True
    assert should_project_as_control(parent, experimental, 1)


def test_iae_source_rw_projects_as_regular_control_without_experimental_opt_in() -> (
    None
):
    identity = resolve_device_identity(DeviceIdentity(4, None, None, "SCB-10"))
    rows = {row.address: row for row in catalog_for_node(identity, max_access_level=3)}
    source_rw = rows[ObjectAddress(0x200E, 0)]
    experimental_row = next(
        row
        for row in catalog_for_node(
            identity,
            capabilities=(ObjectAddress(0x1000, 0),),
            experimental_writes=True,
        )
        if row.address == ObjectAddress(0x1000, 0)
    )
    experimental = replace(
        experimental_row,
        writable=True,
        access_level_evidence={
            "write": {"known": True, "complete": True, "levels": ["user"]}
        },
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

    assert source_rw.write_classification == "regular"
    assert source_rw.safety == "unverified"
    assert should_project_as_control(parent, source_rw, 1)
    assert experimental.write_classification == "experimental"
    assert not should_project_as_control(parent, experimental, 1)

    parent.experimental_writes = True
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
