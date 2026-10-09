from dataclasses import replace
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import _async_derive_object_ids
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    EntityRegistryItems,
    EntityRegistrySettings,
)
from homeassistant.util import slugify
from openrbus.catalog import catalog_for_node
from openrbus.discovery import DeviceIdentity, resolve_device_identity
from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import Registry

from custom_components.openrbus.binary_sensor import OpenRBusBitfieldSensor
from custom_components.openrbus.entity_names import (
    _HK_CODES,
    _SOURCE_TERM_PATTERNS,
    _SOURCE_TERMS_EN,
    _TWW_CODES,
    _WP_CODES,
    _slug,
    assign_compact_entity_id,
    assign_compact_identity_entity_id,
    compact_entity_object_id,
    parameter_code_for,
    register_display_name,
    suggested_object_id,
)
from custom_components.openrbus.number import OpenRBusNumber
from custom_components.openrbus.register_entities import (
    OpenRBusRegisterEntity,
    bitfield_structure,
)
from custom_components.openrbus.select import OpenRBusSelect
from custom_components.openrbus.sensor import (
    OpenRBusDeviceTypeSensor,
    OpenRBusIdentitySensor,
    OpenRBusRegisterSensor,
)
from custom_components.openrbus.switch import OpenRBusSwitch
from custom_components.openrbus.zones import (
    ZoneAssociation,
    ZoneProfile,
    zone_association,
    zone_device_name,
    zone_subdevice_name,
    zone_subindex,
)

_CATALOG = Registry.load_default()


def test_source_term_whitelists_match_the_audited_coverage() -> None:
    assert len(_HK_CODES) == 52
    assert len(_TWW_CODES) == 53
    assert len(_WP_CODES) == 134
    assert len(_SOURCE_TERMS_EN["HK"][0]) == 49
    assert len(_SOURCE_TERMS_EN["TWW"][0]) == 14
    assert len(_SOURCE_TERMS_EN["WP"][0]) == 133


def _register(code: str | None, index: int, subindex: int, name_en: str):
    return SimpleNamespace(
        internal_code=code,
        address=ObjectAddress(index, subindex),
        name_de="HK Aufheizgrad." if code == "CP730" else name_en,
        name_en=name_en,
    )


def _parent(profile: ZoneProfile | None = None):
    return SimpleNamespace(
        language="de",
        zone_profiles=(
            {(profile.node, profile.subindex): profile} if profile is not None else {}
        ),
        zone_overrides={},
    )


def _identity(*, family="Ehc-16", node=1):
    return SimpleNamespace(node=node, family=family, model=None)


def test_reviewed_german_names_use_exact_parameter_codes() -> None:
    assert (
        register_display_name(_register("BM001", 0x5501, 1, "Gem.PuSpTemp"), "de")
        == "Gemessene Pufferspeichertemperatur"
    )
    assert (
        register_display_name(_register("BM001", 0x5501, 1, "Meas Btank temp"), "en")
        == "Measured buffer tank temperature"
    )
    assert (
        register_display_name(_register("BM001", 0x5502, 1, "Gem.PuSpTemp"), "de")
        is None
    )
    assert (
        register_display_name(_register("AP004", 0x3004, 0, "Hydr. Ventil Verz."), "de")
        == "Verzögerungszeit bis zur Öffnung des Hydraulikventils"
    )
    assert register_display_name(
        _register("CP730", 0x346A, 0, "Zone Heat up speed"), "de"
    ) == ("Heizkreis-Aufheizgeschwindigkeit")
    assert register_display_name(
        _register("CP740", 0x346B, 0, "Zone cool down speed"), "de"
    ) == ("Heizkreis-Abkühlgeschwindigkeit")
    assert (
        register_display_name(_register("CP730", 0x346A, 0, "Zone Heat up speed"), "en")
        is None
    )
    assert register_display_name(_register("CP999", 0x346A, 0, "Unknown"), "de") is None


@pytest.mark.parametrize(
    (
        "address",
        "code",
        "source_name",
        "datatype",
        "storage",
        "wrong_storage",
        "expected",
    ),
    [
        (
            "2001:0d",
            None,
            "Konfig. Tabellenver.",
            "UINT16",
            "UINT16",
            "UINT8",
            "Version der Gerätekonfigurationstabelle",
        ),
        (
            "4356:00",
            None,
            "AE-Schutzfunktionen",
            "UINT8",
            "UINT8",
            "UINT16",
            "Schutzfunktionen der Außeneinheit der Wärmepumpe",
        ),
    ],
)
def test_exact_address_names_expand_only_matching_catalog_rows(
    address, code, source_name, datatype, storage, wrong_storage, expected
) -> None:
    register = SimpleNamespace(
        internal_code=code,
        address=ObjectAddress.parse(address),
        name_de=source_name,
        datatype=datatype,
        storage=storage,
    )

    assert register_display_name(register, "de") == expected
    register.datatype = "UINT8" if datatype != "UINT8" else "ENUMERATION"
    assert register_display_name(register, "de") is None
    register.datatype = datatype
    register.storage = wrong_storage
    assert register_display_name(register, "de") is None


def test_input_function_name_only_expands_bounded_array_elements() -> None:
    identity = resolve_device_identity(
        DeviceIdentity(4, None, None, "SCB-10", family="Scb-10")
    )
    head = next(
        row
        for row in catalog_for_node(identity)
        if row.address == ObjectAddress.parse("3811:00")
    )

    assert register_display_name(head, "de") is None
    for subindex in (1, 6, 10):
        child = replace(head, index=0x3811, subindex=subindex)
        assert register_display_name(child, "de") == (
            "Auswahl der Funktion des Digitaleingangs"
        )

    overflow = replace(head, index=0x3811, subindex=11)
    assert register_display_name(overflow, "de") is None
    assert register_display_name(replace(head, datatype="UINT8"), "de") is None
    assert register_display_name(replace(head, storage="UINT16"), "de") is None
    assert register_display_name(replace(head, internal_code=None), "de") is None
    assert register_display_name(replace(head, name_de="Other"), "de") is None


@pytest.mark.parametrize(
    ("code", "index", "expected"),
    [
        ("CM030", 0x5404, "Heizkreis-Raumtemperatur"),
        ("CM210", 0x542E, "Außentemperatur des Heizkreises"),
        (
            "CM220",
            0x542F,
            "Kurzzeitmittel der Außentemperatur des Heizkreises",
        ),
        (
            "CM230",
            0x5430,
            "Langzeitmittel der Außentemperatur des Heizkreises",
        ),
        ("CP000", 0x3401, "Maximaler Vorlauftemperatur-Sollwertbereich"),
        ("CP010", 0x3402, "Vorlauftemperatur-Sollwert ohne Außensensor"),
        ("CP020", 0x3404, "Funktion des Heizkreises"),
        ("CP030", 0x3405, "Regelbereich des Heizkreis-Mischventils"),
        ("CP040", 0x3408, "Pumpennachlaufzeit des Heizkreises"),
        ("CP050", 0x3409, "Mischerüberhöhung des Heizkreises"),
        ("CP060", 0x340A, "Raumsollwert des Heizkreises im Ferienbetrieb"),
        ("CP070", 0x340B, "Raumsollwert des Heizkreises im Nachtbetrieb"),
        ("CP080", 0x340C, "Raumsollwert der Heizkreisaktivität"),
        ("CP130", 0x340E, "Außentemperaturfühler für den Heizkreis"),
        (
            "CP140",
            0x3412,
            "Raumsollwert der Heizkreisaktivität im Kühlbetrieb",
        ),
        (
            "CP190",
            0x342B,
            "Heizkreis-Sicherheitstemperaturbegrenzer aktiviert",
        ),
        (
            "CM380",
            0x5411,
            "Status des Heizkreis-Sicherheitstemperaturbegrenzers",
        ),
        ("CP200", 0x3413, "Raumtemperatur-Sollwert im Heizkreis-Kühlbetrieb"),
        ("CP210", 0x3414, "Komfort-Startwert des Heizkreises"),
        ("CP220", 0x3415, "Nacht-Startwert des Heizkreises"),
        ("CP230", 0x3416, "Steigung der Heizkurve"),
        ("CP240", 0x3417, "Einfluss des Raumgeräts auf den Heizkreis"),
        ("CP250", 0x3418, "Kalibrierung des Raumtemperaturfühlers"),
        ("CP260", 0x3419, "Mindestvorlauftemperatur des Heizkreises"),
        ("CP270", 0x341A, "Sollwert für Fußbodenkühlung"),
        ("CP280", 0x341B, "Kühlsollwert des Gebläsekonvektors"),
        ("CP290", 0x341C, "Pumpenausgangskonfiguration des Heizkreises"),
        ("CP310", 0x341E, "Automatische Anpassung der Heizkurve"),
        ("CP320", 0x341F, "Betriebsart des Heizkreises"),
        ("CP340", 0x3424, "Reduzierter Nachtbetrieb des Heizkreises"),
        ("CP400", 0x342A, "Dauer der Anti-Legionellenfunktion"),
        ("CP460", 0x3430, "Trinkwarmwasser-Vorrang des Heizkreises"),
        ("CP500", 0x3450, "Vorlauftemperatursensor des Heizkreises aktiviert"),
        ("CP530", 0x3453, "PWM-Pumpendrehzahl im Heizkreis"),
        ("CP560", 0x3456, "Anti-Legionellenhäufigkeit des Heizkreises"),
        ("CP570", 0x3458, "Zeitprogramm des Heizkreises"),
        ("CP630", 0x345E, "Starttag der Anti-Legionellenfunktion"),
        ("CP660", 0x3463, "Heizkreis-Symbol für Anzeige und Raumgerät"),
        ("CP680", 0x3465, "Raumgeräte-Buskanal des Heizkreises"),
        ("CP700", 0x3467, "Offset des Trinkwarmwasserfühlers"),
        ("CP730", 0x346A, "Heizkreis-Aufheizgeschwindigkeit"),
        ("CP740", 0x346B, "Heizkreis-Abkühlgeschwindigkeit"),
        ("CP750", 0x346C, "Maximale Vorheizzeit des Heizkreises"),
        ("CP780", 0x3471, "Regelungsstrategie des Heizkreises"),
        ("CP800", 0x3473, "Heizmodus des gewerblichen Trinkwarmwasserspeichers"),
        ("CP850", 0x347D, "Hydraulischer Abgleich im Heizkreis möglich"),
        ("CP900", 0x3478, "Trinkwarmwasser-Zirkulation"),
        ("DP004", 0x3604, "Häufigkeit der Anti-Legionellenfunktion"),
        ("DP047", 0x3630, "Maximale Dauer der Trinkwarmwasserbereitung"),
        ("HP003", 0x2303, "Minimale Vorlauftemperatur der Wärmepumpe im Kühlbetrieb"),
        ("HP180", 0x23AB, "Externer Drucksensor"),
    ],
)
def test_source_audited_code_address_name_map(code, index, expected) -> None:
    """Reviewed fixture for OBD 1.47 address/code joins and German labels."""

    register = _register(code, index, 0, "vendor source label")
    definition = _CATALOG.find(register.address)
    assert definition is not None
    assert definition.address == register.address
    assert definition.code == code
    assert register_display_name(register, "de") == expected


def test_mismatched_parameter_code_is_not_used_for_suggested_id() -> None:
    register = _register("CP730", 0x3604, 0, "Mismatched source row")
    assert parameter_code_for(register) is None


@pytest.mark.parametrize(
    ("code", "index", "expected"),
    [
        ("CP190", 0x342B, "Heating circuit safety thermostat enabled"),
        ("CM380", 0x5411, "Heating circuit safety thermostat status"),
    ],
)
def test_same_short_label_codes_have_distinct_normalized_english_names(
    code, index, expected
) -> None:
    register = _register(code, index, 1, "Safety thermostat")
    assert register_display_name(register, "en") == expected
    mismatched = _register(code, index + 1, 1, "Safety thermostat")
    assert register_display_name(mismatched, "en") is None


def test_zone_slot_safety_thermostat_registers_have_distinct_names() -> None:
    setting = _register("CP190", 0x342B, 1, "Safety thermostat")
    status = _register("CM380", 0x5411, 1, "Safety thermostat")
    identity = _identity(family="Scb-10", node=4)

    assert zone_association(setting, identity) is ZoneAssociation.ZONE_SLOT
    assert zone_association(status, identity) is ZoneAssociation.ZONE_SLOT
    assert register_display_name(setting, "de") == (
        "Heizkreis-Sicherheitstemperaturbegrenzer aktiviert"
    )
    assert register_display_name(status, "de") == (
        "Status des Heizkreis-Sicherheitstemperaturbegrenzers"
    )


def test_scalar_parameter_code_requires_exact_core_address_code_join() -> None:
    assert parameter_code_for(_register("DP004", 0x3604, 0, "Anti-leg frequency")) == (
        "DP004"
    )
    assert parameter_code_for(_register("DP004", 0x3605, 0, "Wrong address")) is None


@pytest.mark.parametrize(
    ("code", "index", "source_short", "expected"),
    [
        ("AC009", 0x5086, "Gel. Energie TWW", "Gelieferte Energie Trinkwarmwasser"),
    ],
)
def test_whitelisted_source_abbreviations_expand_by_exact_code(
    code, index, source_short, expected
) -> None:
    register = _register(code, index, 0, "ignored fallback")
    register.name_de = source_short
    assert register_display_name(register, "de") == expected


@pytest.mark.parametrize(
    ("code", "index", "source_short", "expected"),
    [
        (
            "HP002",
            0x2302,
            "Max. Vorlauftemp. WP",
            "Maximale Vorlauftemperatur der Wärmepumpe ohne Zusatzerzeuger",
        ),
        (
            "HP094",
            0x235E,
            "Beginn leiser Betr.",
            "Startzeit des leisen Betriebs der Wärmepumpe",
        ),
        ("DM001", 0x5601, "TWWSp oben", "Trinkwarmwasserspeicher oben"),
        (
            "HP072",
            0x2348,
            "Min. TVorlauf Abt.WP",
            "Minimale Vorlauftemperatur der Wärmepumpe, unterhalb der der Zusatzerzeuger beim Abtauen startet",
        ),
        (
            "HP070",
            0x2346,
            "Min. TRücklauf Abt.",
            "Minimale Rücklauftemperatur der Wärmepumpe, unterhalb der der Zusatzerzeuger beim Abtauen startet",
        ),
        (
            "HP071",
            0x2347,
            "Min.Trücklauf Abt.WP",
            "Minimale Rücklauftemperatur der Wärmepumpe, unterhalb der das Abtauen abgebrochen wird",
        ),
        (
            "HP073",
            0x2349,
            "Min. TVorlauf Abt.",
            "Minimale Vorlauftemperatur der Wärmepumpe, unterhalb der das Abtauen gestoppt wird",
        ),
        (
            "HP095",
            0x235F,
            "Ende leiser Betrieb",
            "Endzeit des leisen Betriebs der Wärmepumpe",
        ),
        (
            "CM320",
            0x543B,
            "Startzeit ZusatzHzg",
            "Geschätzte Zeit bis zum Start der elektrischen Zusatzheizung für den Trinkwarmwasserspeicher",
        ),
        (
            "HP155",
            0x23A6,
            "Min.Stillstandsz. WP",
            "Mindeststillstandszeit des Wärmepumpenverdichters",
        ),
        (
            "CP430",
            0x342D,
            "TWW Sp.lad. Opt.",
            "Optimierung der Trinkwarmwasser-Speicherladung",
        ),
        (
            "CP360",
            0x3426,
            "Eco TWW-Temp. Kreis",
            "Trinkwarmwasser-Sollwert des Kreises im Eco-Betrieb",
        ),
    ],
)
def test_verified_german_short_forms_expand_only_for_exact_codes(
    code, index, source_short, expected
) -> None:
    register = _register(code, index, 0, "unchanged English fallback")
    register.name_de = source_short
    assert register_display_name(register, "de") == expected


def test_punctuated_token_match_is_literal_and_does_not_change_english_ids() -> None:
    temp_pattern = _SOURCE_TERM_PATTERNS["Temp."]
    assert temp_pattern.search("Temp.")
    assert not temp_pattern.search("TempX")

    umlaut = _register("AM018", 0x5015, 0, "Trücklauf Pumpe")
    umlaut.name_de = "Trücklauf Pumpe"
    assert register_display_name(umlaut, "de") == "Rücklauftemperatur Pumpe"

    new_tww_code = _register("CP360", 0x3426, 1, "Eco TWW temp circuit")
    assert suggested_object_id(_parent(), _identity(), new_tww_code) == (
        "zone_1_eco_tww_temp_circuit"
    )


@pytest.mark.parametrize(
    ("code", "index", "subindex", "expected"),
    [
        ("CM030", 0x5404, 2, "Heizkreis-Raumtemperatur"),
        ("CM210", 0x542E, 2, "Außentemperatur des Heizkreises"),
        ("CM220", 0x542F, 2, "Kurzzeitmittel der Außentemperatur des Heizkreises"),
        ("CM230", 0x5430, 2, "Langzeitmittel der Außentemperatur des Heizkreises"),
        ("CP750", 0x346C, 2, "Maximale Vorheizzeit des Heizkreises"),
        (
            "CP140",
            0x3412,
            1,
            "Raumsollwert der Heizkreisaktivität im Kühlbetrieb",
        ),
    ],
)
def test_normalized_labels_cover_array_items_without_assigning_unproven_slots(
    code, index, subindex, expected
) -> None:
    register = _register(code, index, subindex, "opaque short label")
    assert register_display_name(register, "de") == expected


@pytest.mark.parametrize(
    ("code", "index", "source_short"),
    [
        ("CP300", 0x3421, "HK, Vorlauf"),
        ("HP111", 0x236F, "WP-Abschaltverz."),
    ],
)
def test_abbreviation_is_not_expanded_without_medium_description_evidence(
    code, index, source_short
) -> None:
    register = _register(code, index, 0, "unchanged fallback")
    register.name_de = source_short
    assert register_display_name(register, "de") is None


def test_noncode_and_mismatched_address_do_not_expand_abbreviations() -> None:
    no_code = _register(None, 0x3414, 0, "HK, Startp.Heizk.")
    no_code.name_de = "HK, Startp.Heizk."
    assert register_display_name(no_code, "de") is None

    wrong_address = _register("CP210", 0x3415, 0, "HK, Startp.Heizk.")
    wrong_address.name_de = "HK, Startp.Heizk."
    assert register_display_name(wrong_address, "de") is None


def test_whole_token_rule_does_not_expand_wp_inside_pwm() -> None:
    register = _register("HP111", 0x236F, 0, "ignored fallback")
    register.name_de = "PWM-Drehzahl"
    assert register_display_name(register, "de") is None


def test_direct_reviewed_glossary_takes_precedence_over_word_expansion() -> None:
    register = _register("CP210", 0x3414, 0, "Zone heating start point")
    register.name_de = "HK, Startp.Heizk."
    assert register_display_name(register, "de") == "Komfort-Startwert des Heizkreises"


@pytest.mark.parametrize(
    ("subindex", "expected"),
    [
        (0, None),
        (1, "CP730"),
        (2, "CP731"),
        (3, "CP732"),
        (4, "CP733"),
        (5, "CP734"),
        (6, None),
    ],
)
def test_cp730_codes_are_exact_and_bounded(subindex, expected) -> None:
    register = _register("CP730", 0x346A, subindex, "Zone Heat up speed")
    family = "Scb-10" if subindex in {2, 3, 4, 5} else "Ehc-16"
    assert parameter_code_for(register, _identity(family=family)) == expected


def test_manual_listed_arrays_map_only_explicit_zone_codes() -> None:
    assert (
        parameter_code_for(
            _register("CP020", 0x3404, 5, "Zone function"),
            _identity(family="Scb-10"),
        )
        == "CP024"
    )
    assert (
        parameter_code_for(
            _register("CP530", 0x3453, 1, "Zone PWM Pump speed"), _identity()
        )
        == "CP530"
    )
    assert (
        parameter_code_for(
            _register("CP530", 0x3453, 5, "Zone PWM Pump speed"),
            _identity(family="Scb-10"),
        )
        == "CP534"
    )
    assert (
        parameter_code_for(
            _register("CP530", 0x3453, 6, "Zone PWM Pump speed"), _identity()
        )
        is None
    )
    assert (
        parameter_code_for(
            _register("CP730", 0x346A, 4, "Zone Heat up speed"),
            _identity(family=None),
        )
        is None
    )
    assert (
        parameter_code_for(
            _register("CP730", 0x346A, 4, "Zone Heat up speed"),
            _identity(family="Ehc-16"),
        )
        is None
    )
    assert (
        parameter_code_for(
            _register("CP080", 0x340C, 5, "Zone activity temperature"), _identity()
        )
        is None
    )
    assert (
        parameter_code_for(_register("CP450", 0x3481, 1, "Pump type"), _identity())
        is None
    )
    assert (
        parameter_code_for(_register("HP180", 0x23AB, 0, "Ext pressure sensor"))
        == "HP180"
    )


def test_suggested_id_is_only_the_suffix_not_added_by_ha_device_prefix() -> None:
    register = _register("CP730", 0x346A, 1, "Zone Heat up speed")
    profile = ZoneProfile(1, 1, 1)

    assert suggested_object_id(_parent(profile), _identity(), register) == (
        "node_1_cp730_heat_up_speed"
    )


def test_suggested_id_requires_exact_family_address_code_evidence() -> None:
    register = _register("CP730", 0x346A, 4, "Zone Heat up speed")
    profile = ZoneProfile(4, 4, 1)

    assert (
        suggested_object_id(
            _parent(profile), _identity(family="Scb-10", node=4), register
        )
        == "node_4_cp733_heat_up_speed"
    )
    assert (
        suggested_object_id(
            _parent(profile), _identity(family="Ehc-16", node=4), register
        )
        == "node_4_heat_up_speed"
    )


def test_suggested_id_uses_zone_for_unknown_profile_and_skips_unknown_cp_code() -> None:
    register = _register("CP730", 0x346A, 6, "Zone Heat up speed")
    profile = ZoneProfile(1, 6, None)

    assert suggested_object_id(_parent(profile), _identity(), register) == (
        "zone_6_heat_up_speed"
    )


def test_suggested_id_uses_stable_node_fallback_when_family_is_unknown() -> None:
    register = _register(None, 0x2001, 2, "Device type")

    assert suggested_object_id(_parent(), _identity(family=None, node=5), register) == (
        "device_type"
    )


def test_suggested_id_uses_source_backed_english_label_for_cryptic_short_name() -> None:
    register = _register("CP010", 0x3402, 1, "Tflow setpoint zone")
    assert suggested_object_id(_parent(), _identity(), register) == (
        "cp010_zone_1_flow_temperature_setpoint_without_outdoor_sensor"
    )


@pytest.mark.parametrize(
    ("language", "name", "expected"),
    [
        (
            "de",
            "Heizkreis-Aufheizgeschwindigkeit",
            "Aufheizgeschwindigkeit",
        ),
        ("en", "Heating circuit heat-up speed", "heat-up speed"),
        ("de", "Funktion des Heizkreises", "Funktion des Heizkreises"),
    ],
)
def test_display_name_does_not_repeat_a_proven_zone_label(
    language, name, expected
) -> None:
    parent = _parent(ZoneProfile(1, 1, 1))
    parent.language = language
    register = _register("CP730", 0x346A, 1, "Zone Heat up speed")
    assert (
        OpenRBusRegisterEntity.name_with_zone(name, parent, _identity(), register)
        == expected
    )


@pytest.mark.parametrize("slot", range(1, 11))
@pytest.mark.parametrize("read_state", ["unknown", "inactive", "active"])
def test_cp020_selector_name_always_identifies_its_parent_slot(
    slot, read_state
) -> None:
    register = _register("CP020", 0x3404, slot, "Zone function")
    identity = _identity(node=1)
    profile = {
        "unknown": None,
        "inactive": ZoneProfile(1, slot, 0),
        "active": ZoneProfile(1, slot, 2),
    }[read_state]
    parent = _parent(profile)

    assert zone_association(register, identity) is ZoneAssociation.FUNCTION_SELECTOR
    assert zone_subindex(register, identity) is None
    assert (
        OpenRBusRegisterEntity.name_with_zone(
            "Funktion des Heizkreises", parent, identity, register
        )
        == f"Zone {slot} — Funktion des Heizkreises"
    )


def test_read_only_cp020_sensor_name_identifies_unknown_parent_slot() -> None:
    register = _register("CP020", 0x3404, 2, "Zone function")
    register.datatype = "ENUM"
    register.unit = None
    parent = _parent()
    parent.config_entry = SimpleNamespace(
        data={"ble_device": "00:11:22:33:44:55"}, options={}, entry_id="entry"
    )
    identity = _identity(node=1)

    entity = OpenRBusRegisterSensor(
        parent,
        SimpleNamespace(),
        identity,
        register,
        language="de",
        effective_access_level=1,
    )

    assert entity.name == "Zone 2 — Funktion des Heizkreises"
    assert entity.unique_id.endswith(":node:1:object:3404:02")
    assert zone_subindex(register, identity) is None


@pytest.mark.parametrize("subindex", [0, 11])
def test_cp020_array_header_and_out_of_bounds_item_get_no_zone_label(subindex) -> None:
    register = _register("CP020", 0x3404, subindex, "Zone function")

    assert (
        OpenRBusRegisterEntity.name_with_zone(
            "Funktion des Heizkreises", _parent(), _identity(), register
        )
        == "Funktion des Heizkreises"
    )


@pytest.mark.parametrize(
    ("code", "index", "label"),
    [
        ("CP080", 0x340C, "Raumsollwert der Heizkreisaktivität"),
        (
            "CP140",
            0x3412,
            "Raumsollwert der Heizkreisaktivität im Kühlbetrieb",
        ),
    ],
)
def test_cp080_cp140_activity_indexes_do_not_gain_zone_labels(
    code, index, label
) -> None:
    register = _register(code, index, 1, "opaque")

    assert zone_association(register, _identity()) is ZoneAssociation.UNRESOLVED
    assert (
        OpenRBusRegisterEntity.name_with_zone(
            label, _parent(ZoneProfile(1, 1, 2)), _identity(), register
        )
        == label
    )


@pytest.mark.parametrize(
    "entity_class",
    [
        OpenRBusRegisterEntity,
        OpenRBusRegisterSensor,
        OpenRBusNumber,
        OpenRBusSelect,
        OpenRBusSwitch,
        OpenRBusBitfieldSensor,
    ],
)
def test_register_platforms_expose_the_shared_suggested_id_property(
    entity_class,
) -> None:
    register = _register("CP730", 0x346A, 1, "Zone Heat up speed")
    instance = entity_class.__new__(entity_class)
    instance._parent = _parent(ZoneProfile(1, 1, 1))
    instance._identity = _identity()
    instance._register = register
    if entity_class is OpenRBusBitfieldSensor:
        instance._openrbus_english_name = "CoolingAllowed"

    expected = "node_1_cp730_"
    expected += (
        "cooling_allowed" if entity_class is OpenRBusBitfieldSensor else "heat_up_speed"
    )
    assert instance.suggested_object_id == expected


@pytest.mark.asyncio
async def test_home_assistant_composes_device_prefix_with_register_id_suffix(
    monkeypatch,
):
    """Exercise HA 2026.9's actual object_id_base composition contract."""

    register = _register("CP730", 0x346A, 1, "Zone Heat up speed")
    profile = ZoneProfile(1, 1, 1)
    parent = _parent(profile)
    identity = _identity()
    suffix = suggested_object_id(parent, identity, register)
    device_name = "EHC-16"
    assert suffix == "node_1_cp730_heat_up_speed"
    assert _ha_entity_id(monkeypatch, device_name, suffix) == (
        "select.ehc_16_node_1_cp730_heat_up_speed"
    )


def test_home_assistant_zone_device_prefix_avoids_duplicate_zone_tokens(monkeypatch):
    """Zone device labels already carry the node and slot in the HA prefix."""

    register = _register("CP730", 0x346A, 4, "Zone Heat up speed")
    profile = ZoneProfile(4, 4, 1, friendly_name="SCB HK1", node_name="SCB-10")
    parent = _parent(profile)
    identity = _identity(family="SCB-10", node=4)
    device_name = zone_device_name(profile, "de")
    suffix = suggested_object_id(parent, identity, register)
    assert suffix == "cp733_heat_up_speed"
    assert _ha_entity_id(monkeypatch, device_name, suffix) == (
        "select.scb_10_node_4_zone_4_heizkreis_direkt_scb_hk1_cp733_heat_up_speed"
    )


def test_compact_zone_id_matches_requested_node_model_zone_register_schema():
    register = _register("CP020", 0x3404, 1, "Zone function")
    profile = ZoneProfile(4, 1, 1, short_name="SCB HK1", node_name="SCB-10")
    parent = _parent(profile)
    identity = _identity(family="SCB-10", node=4)

    assert compact_entity_object_id(parent, identity, register) == (
        "n4_scb_10_z1_scb_hk1_cp020_heating_circuit_function"
    )


def test_compact_zone_entity_id_is_assigned_after_entity_construction():
    register = _register("CP020", 0x3404, 1, "Zone function")
    profile = ZoneProfile(4, 1, 1, short_name="SCB HK1", node_name="SCB-10")
    entity = SensorEntity()
    entity._parent = _parent(profile)
    entity._identity = _identity(family="SCB-10", node=4)
    entity._register = register

    assign_compact_entity_id(entity, "sensor")

    assert entity.entity_id == (
        "sensor.n4_scb_10_z1_scb_hk1_cp020_heating_circuit_function"
    )
    entity.internal_integration_suggested_object_id = entity.entity_id.removeprefix(
        "sensor."
    )
    suggested, base = _async_derive_object_ids(
        entity, SimpleNamespace(entity_namespace=None, platform_name="openrbus")
    )
    assert suggested == "n4_scb_10_z1_scb_hk1_cp020_heating_circuit_function"
    assert base is None


def test_compact_bitfield_ids_keep_the_field_name_and_stable_unique_id():
    register = _register(None, 0x5503, 0, "Configuration flags")
    structure = bitfield_structure(register)
    assert structure is not None
    parent = _parent()
    identity = _identity(family="SCB-10", node=4)
    entities = []
    for field in [item for item in structure.fields if item.bit_length == 1][:2]:
        entity = OpenRBusBitfieldSensor.__new__(OpenRBusBitfieldSensor)
        entity._parent = parent
        entity._identity = identity
        entity._register = register
        entity._openrbus_english_name = field.name
        entity._attr_unique_id = f"stable-register-id:bit:{field.name}"
        unique_id = entity.unique_id
        assign_compact_entity_id(entity, "binary_sensor")
        assert entity.unique_id == unique_id
        entities.append(entity)

    assert len(entities) == 2
    assert entities[0].entity_id != entities[1].entity_id
    assert all("_2" not in entity.entity_id for entity in entities)
    assert all(
        entity.entity_id.endswith(_slug(entity._openrbus_english_name))
        for entity in entities
    )


@pytest.mark.parametrize(
    ("field", "suffix"),
    [("device_code", "device_type"), ("parameter_number", "parameter_number")],
)
def test_node_identity_sensor_ids_use_compact_model_prefix_and_keep_unique_id(
    field, suffix
):
    entity = OpenRBusIdentitySensor.__new__(OpenRBusIdentitySensor)
    entity._identity = _identity(family="SCB-10", node=4)
    entity._field = field
    entity._attr_unique_id = f"stable:{field}"
    unique_id = entity.unique_id

    assign_compact_identity_entity_id(entity, "sensor")

    assert entity.entity_id == f"sensor.n4_scb_10_{suffix}"
    assert entity.unique_id == unique_id


@pytest.mark.asyncio
async def test_existing_custom_entity_id_is_kept_for_the_same_unique_id(
    tmp_path, monkeypatch
):
    hass = HomeAssistant(str(tmp_path))
    registry = EntityRegistry(hass)
    registry.entities = EntityRegistryItems(hass)
    registry._entities_data = registry.entities.data
    registry.deleted_entities = {}
    registry.settings = EntityRegistrySettings()
    monkeypatch.setattr(registry, "async_schedule_save", lambda: None)
    original = registry.async_get_or_create(
        "sensor",
        "openrbus",
        "stable-openrbus-unique-id",
        suggested_object_id="user_chosen_entity_name",
    )

    candidate = registry.async_get_or_create(
        "sensor",
        "openrbus",
        "stable-openrbus-unique-id",
        suggested_object_id="n4_scb_10_bm001_measured_buffer_tank_temperature",
    )

    assert original.entity_id == "sensor.user_chosen_entity_name"
    assert candidate.entity_id == original.entity_id


def test_unknown_dimensions_keep_the_exact_address_in_compact_entity_ids():
    profile = ZoneProfile(4, 1, 1, short_name="SCB HK1", node_name="SCB-10")
    parent = _parent(profile)
    identity = _identity(family="SCB-10", node=4)
    unknown = _register("CP999", 0x3404, 1, "Unknown zone row")

    assert compact_entity_object_id(parent, identity, unknown) == (
        "n4_scb_10_z1_scb_hk1_obj_3404_01_unknown_zone_row"
    )
    assert compact_entity_object_id(
        parent, identity, _register("CP020", 0x3404, 0, "Zone function")
    ) == ("n4_scb_10_obj_3404_00_zone_function")


def test_unzoned_register_ids_include_node_model_and_exact_address():
    register = _register(None, 0x2001, 2, "Device identity")
    assert compact_entity_object_id(
        _parent(), _identity(family="SCB-10", node=4), register
    ) == ("n4_scb_10_obj_2001_02_device_identity")


def test_zone_subdevice_name_uses_register_designation_model_node_and_slot():
    profile = ZoneProfile(4, 1, 1, short_name="SCB HK1", node_name="SCB-10")
    assert zone_subdevice_name(profile) == "SCB HK1 (SCB-10 (N4) - Zone 1)"


def test_non_heating_subdevice_keeps_the_selected_language():
    profile = ZoneProfile(4, 1, 6, short_name="TWW")
    assert zone_subdevice_name(profile, "en") == zone_device_name(profile, "en")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("device_code", "node_1_device_identity"),
        ("parameter_number", "node_1_parameter_number"),
    ],
)
def test_identity_diagnostic_suggestions_are_english_and_node_qualified(
    monkeypatch, field, expected
):
    instance = OpenRBusIdentitySensor.__new__(OpenRBusIdentitySensor)
    instance._field = field
    instance._identity = _identity()
    assert instance.suggested_object_id == expected
    assert _ha_entity_id(monkeypatch, "EHC-16", expected) == (
        f"select.ehc_16_{expected}"
    )


def test_gateway_device_type_suggestion_is_english():
    instance = OpenRBusDeviceTypeSensor.__new__(OpenRBusDeviceTypeSensor)
    assert instance.suggested_object_id == "openrbus_device_type"


def _ha_entity_id(monkeypatch, device_name, object_id_base):
    """Run HA Core's actual ID composer with a minimal in-memory device."""

    device = type("Device", (), {"name": device_name, "name_by_user": None})()
    monkeypatch.setattr(
        dr,
        "async_get",
        lambda hass: type(
            "DeviceRegistry", (), {"async_get": lambda self, _id: device}
        )(),
    )
    monkeypatch.setattr(dr, "async_get_effective_area_id", lambda _hass, _device: None)
    registry = type(
        "EntityRegistryHarness",
        (),
        {
            "hass": object(),
            "settings": type("Settings", (), {"entity_id_parts": None})(),
            "async_get_available_entity_id": lambda self, domain, object_id, **kwargs: (
                f"{domain}.{slugify(object_id)}"
            ),
        },
    )()
    return er.EntityRegistry._async_generate_entity_id(
        registry,
        area_id=None,
        current_entity_id=None,
        device_id="naming-test-device",
        domain="select",
        has_entity_name=True,
        name=None,
        object_id_base=object_id_base,
        platform="openrbus",
        suggested_object_id=None,
        unique_id="naming-test-uid",
    )
