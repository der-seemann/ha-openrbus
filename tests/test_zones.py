"""Regression tests for evidence-backed zone projection."""

from types import SimpleNamespace

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.zones import (
    ZoneKind,
    ZoneProfile,
    entity_zone_label,
    normalized_overrides,
    override_key,
    zone_enabled,
    zone_subindex,
)


def _row(index: int, subindex: int, *, name: str = "Zone flow temperature"):
    return SimpleNamespace(
        address=ObjectAddress(index, subindex),
        internal_code="CM040",
        name_en=name,
        name_de=name,
    )


def test_zone_function_classification_is_from_documented_enum_values() -> None:
    assert ZoneProfile(4, 0, 0).kind is ZoneKind.INACTIVE
    assert ZoneProfile(4, 0, 2).kind is ZoneKind.HEATING
    assert ZoneProfile(4, 0, 6).kind is ZoneKind.DHW
    # Unknown enabled values must remain explicit rather than guessed as CH.
    assert ZoneProfile(4, 0, 9).kind is ZoneKind.OTHER


def test_zone_array_subindex_and_display_name_are_stable_separate_concerns() -> None:
    assert zone_subindex(_row(0x5405, 0)) == 0
    assert zone_subindex(_row(0x2001, 2, name="Device type")) is None
    profile = ZoneProfile(4, 0, 2, "Wohnbereich")
    assert profile.label == "Wohnbereich"
    assert override_key(4, 0) == "4:0"


def test_inactive_slots_are_hidden_unless_user_explicitly_enables_them() -> None:
    parent = SimpleNamespace(
        zone_profiles={(4, 0): ZoneProfile(4, 0, 0)}, zone_overrides={}
    )
    assert not zone_enabled(parent, 4, 0)
    parent.zone_overrides = {"4:0": True}
    assert zone_enabled(parent, 4, 0)


def test_custom_zone_label_is_applied_without_affecting_identity() -> None:
    parent = SimpleNamespace(
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2, "Obergeschoss")},
        zone_overrides={},
    )
    assert entity_zone_label(parent, SimpleNamespace(node=4), _row(0x5405, 1)) == "Obergeschoss"


def test_malformed_persisted_override_is_ignored() -> None:
    assert normalized_overrides(None) == {}
    assert normalized_overrides({"4:0": 1, 5: False}) == {"4:0": True, "5": False}
