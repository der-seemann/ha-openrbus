"""Validity/sentinel regression coverage for register projections."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.bridge import GenericRead
from custom_components.openrbus.validity import (
    RegisterValidityTracker,
    invalid_value_reason,
)


def _register(**kwargs):
    defaults = {
        "unit": "°C",
        "storage": "INT16",
        "datatype": "INT16",
        "scale": "0.01",
        "internal_code": "",
        "name_de": "",
        "name_en": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _read(value):
    return GenericRead(4, ObjectAddress(0x1234, 1), b"", value)


def test_exact_temperature_sentinel_is_metadata_scoped() -> None:
    assert (
        invalid_value_reason(_read(-327.68), _register()) == "temperature_not_available"
    )
    # Same display value is not a rule for a different register type/gain.
    assert invalid_value_reason(_read(-327.68), _register(storage="INT32")) is None
    assert invalid_value_reason(_read(-327.68), _register(scale="0.1")) is None


def test_pump_modulation_sentinel_does_not_hide_generic_percentages() -> None:
    pump = _register(
        unit="%",
        storage="UINT16",
        datatype="UINT16",
        scale="0.1",
        name_de="Modulation Pumpe AE",
    )
    generic = _register(
        unit="%",
        storage="UINT16",
        datatype="UINT16",
        scale="0.1",
        name_de="Energiezähler Prozent",
    )
    assert invalid_value_reason(_read(6553.5), pump) == "pump_modulation_not_available"
    assert invalid_value_reason(_read(6553.5), generic) is None


def test_invalid_lifecycle_unavailable_recovery_and_expiry() -> None:
    tracker = RegisterValidityTracker(timedelta(hours=48))
    key = (4, ObjectAddress(0x1234, 1))
    register = _register()
    start = datetime(2026, 9, 27, tzinfo=timezone.utc)

    first = tracker.observe(key, _read(-327.68), register, now=start)
    assert not first.valid and not first.expired
    assert not tracker.is_valid(key, _read(-327.68))
    before_expiry = tracker.observe(
        key, _read(-327.68), register, now=start + timedelta(hours=47, minutes=59)
    )
    assert not before_expiry.expired
    expired = tracker.observe(
        key, _read(-327.68), register, now=start + timedelta(hours=48)
    )
    assert expired.expired and tracker.is_expired(key)

    recovered = tracker.observe(
        key, _read(21.5), register, now=start + timedelta(hours=49)
    )
    assert recovered.valid and recovered.recovered and not tracker.is_expired(key)
    assert tracker.is_valid(key, _read(21.5))


def test_transport_error_neither_starts_nor_clears_sentinel_interval() -> None:
    tracker = RegisterValidityTracker(timedelta(hours=1))
    key = (4, ObjectAddress(0x1234, 1))
    register = _register()
    start = datetime(2026, 9, 27, tzinfo=timezone.utc)
    tracker.observe(key, _read(-327.68), register, now=start)
    transient = tracker.observe(
        key, RuntimeError("link lost"), register, now=start + timedelta(minutes=30)
    )
    assert not transient.valid and transient.invalid_since == start
    expired = tracker.observe(
        key, _read(-327.68), register, now=start + timedelta(hours=1)
    )
    assert expired.expired


def test_abort_and_generic_item_errors_expire_only_after_continuous_interval() -> None:
    tracker = RegisterValidityTracker(timedelta(hours=1))
    key = (4, ObjectAddress(0x1234, 1))
    start = datetime(2026, 9, 27, tzinfo=timezone.utc)

    abort = RuntimeError("private abort payload")
    abort._openrbus_error_class = "abort"
    first = tracker.observe(key, abort, _register(), now=start)
    assert not first.valid and first.reason == "abort" and not first.expired
    before = tracker.observe(key, abort, _register(), now=start + timedelta(minutes=59))
    assert before.reason == "abort" and not before.expired
    expired = tracker.observe(key, abort, _register(), now=start + timedelta(hours=1))
    assert expired.expired and tracker.is_expired(key)
    recovered = tracker.observe(
        key, _read(21.5), _register(), now=start + timedelta(hours=1, minutes=1)
    )
    assert recovered.valid and not tracker.is_expired(key)

    generic = RuntimeError("private item error")
    generic._openrbus_error_class = "item"
    tracker.observe(key, generic, _register(), now=start)
    # Batch/session/decode/correlation errors never start the item lifecycle.
    transport = RuntimeError("private transport detail")
    transport._openrbus_error_class = "session"
    transient = tracker.observe(
        key, transport, _register(), now=start + timedelta(minutes=30)
    )
    assert transient.reason is None and not transient.expired
    generic_expired = tracker.observe(
        key, generic, _register(), now=start + timedelta(minutes=30)
    )
    assert not generic_expired.expired and generic_expired.reason == "item"
    generic_expired = tracker.observe(
        key, generic, _register(), now=start + timedelta(minutes=90)
    )
    assert generic_expired.expired and generic_expired.reason == "item"


def test_deterministic_failures_quarantine_only_in_exact_scope() -> None:
    tracker = RegisterValidityTracker(timedelta(hours=1))
    key = (1, ObjectAddress(0x430E, 0))
    scope = (1, ("identity", 4), 2, 7)
    abort = RuntimeError("private detail")
    abort._openrbus_error_class = "abort"
    abort._openrbus_abort_category = "unsupported_access"

    assert tracker.quarantine_deterministic_failure(key, abort, scope)
    assert tracker.is_quarantined(key, scope)
    assert tracker.quarantined_snapshot() == (
        {
            "node": 1,
            "index": 0x430E,
            "subindex": 0,
            "error_class": "abort",
            "subtype": "unsupported_access",
        },
    )
    assert not tracker.is_valid(key, _read(17))

    # A level, identity, or session change ends the quarantine and requests a
    # new read. Unknown scope also fails open rather than retaining a stale ban.
    assert not tracker.is_quarantined(key, (2, ("identity", 4), 2, 7))
    assert tracker.quarantined_count == 0
    assert tracker.quarantine_deterministic_failure(key, abort, scope)
    assert not tracker.is_quarantined(key, (1, ("identity", 5), 2, 7))
    assert tracker.quarantined_count == 0

    malformed = RuntimeError("private payload detail")
    malformed._openrbus_error_class = "decode"
    malformed._openrbus_decode_subtype = "visible_string_non_ascii"
    assert tracker.quarantine_deterministic_failure(key, malformed, scope)
    assert tracker.is_quarantined(key, scope)
    tracker.reactivate(key)
    assert not tracker.is_quarantined(key, scope)
    assert tracker.quarantined_count == 0

    transient = RuntimeError("private transport detail")
    transient._openrbus_error_class = "session"
    assert not tracker.quarantine_deterministic_failure(key, transient, scope)
    unknown_decode = RuntimeError("private decode detail")
    unknown_decode._openrbus_error_class = "decode"
    unknown_decode._openrbus_decode_subtype = "visible_string_other"
    assert not tracker.quarantine_deterministic_failure(key, unknown_decode, scope)
    assert not tracker.quarantine_deterministic_failure(key, malformed, None)
