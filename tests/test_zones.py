"""Regression tests for evidence-backed zone projection."""

from types import SimpleNamespace

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus.zones import (
    ZONE_OBJECT_CANDIDATE_SOURCES,
    ZONE_SLOT_OBJECT_SOURCES,
    ZONE_UNRESOLVED_OBJECT_SOURCES,
    ZoneAssociation,
    ZoneKind,
    ZoneProfile,
    entity_zone_label,
    normalized_overrides,
    override_key,
    zone_association,
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
    assert zone_subindex(_row(0x5405, 0)) is None
    assert zone_subindex(_row(0x340F, 5)) == 5
    assert zone_subindex(_row(0x2001, 2, name="Device type")) is None
    profile = ZoneProfile(4, 0, 2, "Wohnbereich")
    assert profile.label == "Wohnbereich"
    assert override_key(4, 0) == "4:0"


def test_heating_circuit_registers_receive_their_visible_circuit_number() -> None:
    register = _row(0x340F, 3, name="Name Aktivität HK")
    parent = SimpleNamespace(zone_profiles={}, zone_overrides={}, language="de")
    identity = SimpleNamespace(node=7)

    assert zone_subindex(register) == 3
    assert entity_zone_label(parent, identity, register) == "Heizkreis 3"


def test_exact_catalog_map_has_parent_selectors_and_unresolved_exclusions() -> None:
    assert len(ZONE_SLOT_OBJECT_SOURCES) == 262
    assert len(ZONE_OBJECT_CANDIDATE_SOURCES) == 272
    # Exact address membership wins; row labels never broaden association.
    assert zone_subindex(_row(0x3410, 2, name="not a zone")) == 2
    # Membership is exact by object address; a catalog-backed slot stays
    # zone-scoped even when the row's display label is unrelated.
    assert zone_subindex(_row(0x3654, 2, name="not a zone")) == 2
    assert zone_subindex(_row(0x36FF, 2, name="Zone-like but unmapped")) is None
    assert zone_subindex(_row(0x5443, 2, name="Zone current status")) is None
    assert zone_association(_row(0x3404, 2)) is ZoneAssociation.FUNCTION_SELECTOR
    assert zone_association(_row(0x3404, 0)) is ZoneAssociation.UNRESOLVED
    assert zone_association(_row(0x340C, 2)) is ZoneAssociation.UNRESOLVED
    assert zone_association(_row(0x346A, 2)) is ZoneAssociation.ZONE_SLOT
    assert zone_association(_row(0x340D, 2)) is ZoneAssociation.UNRESOLVED
    assert (
        zone_association(_row(0x3406, 2), SimpleNamespace(family="Ehc-16"))
        is ZoneAssociation.ZONE_SLOT
    )
    assert zone_association(_row(0x3406, 2)) is ZoneAssociation.UNRESOLVED
    assert (
        zone_association(_row(0x3406, 2), SimpleNamespace(family="unknown"))
        is ZoneAssociation.UNRESOLVED
    )
    assert zone_association(_row(0x540E, 2)) is ZoneAssociation.PARENT
    assert zone_association(_row(0x5422, 2)) is ZoneAssociation.PARENT
    assert zone_association(_row(0x5423, 2)) is ZoneAssociation.PARENT
    assert zone_association(_row(0x5432, 2)) is ZoneAssociation.PARENT
    assert 0x340C in ZONE_UNRESOLVED_OBJECT_SOURCES
    assert 0x340D in ZONE_UNRESOLVED_OBJECT_SOURCES


def test_custom_heating_circuit_name_is_shown_with_its_number() -> None:
    register = _row(0x340F, 3, name="Name Aktivität HK")
    profile = ZoneProfile(7, 3, 2, friendly_name="Wohnzimmer")
    parent = SimpleNamespace(
        zone_profiles={(7, 3): profile}, zone_overrides={}, language="de"
    )

    assert entity_zone_label(parent, SimpleNamespace(node=7), register) == (
        "Heizkreis 3 — Wohnzimmer"
    )


def test_long_zone_name_precedes_short_hardware_name_and_short_is_fallback() -> None:
    assert ZoneProfile(7, 3, 2, "Wohnbereich", short_name="HK3").label == (
        "Wohnbereich"
    )
    profile = ZoneProfile(7, 3, 2, short_name="HK3")
    assert profile.label == "HK3"
    assert zone_device_name(profile, "de").endswith("— HK3")


def test_device_disabled_slots_cannot_be_reenabled_by_stale_override() -> None:
    parent = SimpleNamespace(
        zone_profiles={(4, 1): ZoneProfile(4, 1, 0)}, zone_overrides={}
    )
    assert not zone_enabled(parent, 4, 1)
    parent.zone_overrides = {"4:1": True}
    assert not zone_enabled(parent, 4, 1)


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
    assert entity_zone_label(parent, SimpleNamespace(node=4), _row(0x340F, 1)) == (
        "Heizkreis 1 — Obergeschoss"
    )


def test_malformed_persisted_override_is_ignored() -> None:
    assert normalized_overrides(None) == {}
    assert normalized_overrides({"4:0": 1, 5: False}) == {"4:0": True, "5": False}
