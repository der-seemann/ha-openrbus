"""Regression tests for evidence-backed zone projection."""

from types import SimpleNamespace

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.zones import (
    ZoneKind,
    ZoneProfile,
    entity_zone_label,
    normalized_overrides,
    override_key,
    zone_device_name,
    zone_enabled,
    zone_function_label,
    zone_function_slots,
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
    # Known but unclassified values and unknown values are never guessed CH.
    assert ZoneProfile(4, 0, 9).kind is ZoneKind.OTHER
    assert ZoneProfile(4, 0, 250).kind is ZoneKind.UNKNOWN
    assert not ZoneProfile(4, 0, 250).active


def test_zone_function_slot_range_comes_from_catalog_array_bound() -> None:
    # The manufacturer definition allows ten CP020 items even though the
    # currently ingested family profiles include only the first five rows.
    assert zone_function_slots([_row(0x3404, 0)]) == tuple(range(1, 11))
    assert zone_function_slots([_row(0x3404, 5)]) == tuple(range(1, 11))
    assert zone_function_slots([_row(0x3405, 0)]) == ()
    assert ZoneProfile(4, 1, 2).label == "Zone 1"


def test_zone_array_subindex_and_display_name_are_stable_separate_concerns() -> None:
    assert zone_subindex(_row(0x5405, 0)) == 0
    assert zone_subindex(_row(0x2001, 2, name="Device type")) is None
    profile = ZoneProfile(4, 0, 2, "Wohnbereich")
    assert profile.label == "Wohnbereich"
    assert override_key(4, 0) == "4:0"


def test_device_disabled_slots_cannot_be_reenabled_by_stale_override() -> None:
    parent = SimpleNamespace(
        zone_profiles={(4, 0): ZoneProfile(4, 0, 0)}, zone_overrides={}
    )
    assert not zone_enabled(parent, 4, 0)
    parent.zone_overrides = {"4:0": True}
    assert not zone_enabled(parent, 4, 0)


def test_zone_label_includes_node_slot_and_manufacturer_function_in_selected_language() -> (
    None
):
    de = ZoneProfile(4, 0, 6, node_name="SCB-10")
    en = ZoneProfile(4, 0, 6, node_name="SCB-10")
    assert zone_function_label(6, "de") == "TWW-Speicher"
    assert zone_function_label(6, "en") == "DHW tank"
    assert zone_device_name(de, "de") == (
        "SCB-10 (Node 4) — Zone 1 — Trinkwarmwasser (TWW-Speicher)"
    )
    assert zone_device_name(en, "en") == (
        "SCB-10 (Node 4) — Zone 1 — Domestic hot water (DHW tank)"
    )


def test_disabled_and_unknown_function_names_are_explicit() -> None:
    assert "deaktiviert" in zone_device_name(ZoneProfile(4, 2, 0), "de")
    assert "Unbekannte Funktion (250)" in zone_device_name(ZoneProfile(4, 2, 250), "de")


def test_custom_zone_label_is_applied_without_affecting_identity() -> None:
    parent = SimpleNamespace(
        zone_profiles={(4, 1): ZoneProfile(4, 1, 2, "Obergeschoss")},
        zone_overrides={},
        language="de",
    )
    assert entity_zone_label(parent, SimpleNamespace(node=4), _row(0x5405, 1)) == (
        "OpenRBus-Knoten 4 — Zone 1 — Heizkreis (Mischerheizkreis) — Obergeschoss"
    )


def test_malformed_persisted_override_is_ignored() -> None:
    assert normalized_overrides(None) == {}
    assert normalized_overrides({"4:0": 1, 5: False}) == {"4:0": True, "5": False}
