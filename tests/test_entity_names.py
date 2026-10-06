from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import Registry

from custom_components.openrbus.binary_sensor import OpenRBusBitfieldSensor
from custom_components.openrbus.entity_names import (
    _HK_CODES,
    _TWW_CODES,
    _WP_CODES,
    parameter_code_for,
    register_display_name,
    suggested_object_id,
)
from custom_components.openrbus.number import OpenRBusNumber
from custom_components.openrbus.register_entities import OpenRBusRegisterEntity
from custom_components.openrbus.select import OpenRBusSelect
from custom_components.openrbus.sensor import (
    OpenRBusDeviceTypeSensor,
    OpenRBusIdentitySensor,
    OpenRBusRegisterSensor,
)
from custom_components.openrbus.switch import OpenRBusSwitch
from custom_components.openrbus.zones import ZoneProfile, zone_device_name

_CATALOG = Registry.load_default()


def test_source_term_whitelists_match_the_audited_coverage() -> None:
    assert len(_HK_CODES) == 49
    assert len(_TWW_CODES) == 14
    assert len(_WP_CODES) == 133


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
    ("code", "index", "expected"),
    [
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


def test_scalar_parameter_code_requires_exact_core_address_code_join() -> None:
    assert parameter_code_for(_register("DP004", 0x3604, 0, "Anti-leg frequency")) == (
        "DP004"
    )
    assert parameter_code_for(_register("DP004", 0x3605, 0, "Wrong address")) is None


@pytest.mark.parametrize(
    ("code", "index", "source_short", "expected"),
    [
        ("CP750", 0x346C, "Max HK-Vorheizzeit", "Max Heizkreis-Vorheizzeit"),
        ("CP430", 0x342D, "TWW Sp.lad. Opt.", "Trinkwarmwasser Sp.lad. Opt."),
        ("HP002", 0x2302, "Max. Vorlauftemp. WP", "Max. Vorlauftemp. Wärmepumpe"),
    ],
)
def test_whitelisted_source_abbreviations_expand_by_exact_code(
    code, index, source_short, expected
) -> None:
    register = _register(code, index, 0, "ignored fallback")
    register.name_de = source_short
    assert register_display_name(register, "de") == expected


@pytest.mark.parametrize(
    ("code", "index", "source_short"),
    [
        ("CP300", 0x3421, "HK, Vorlauf"),
        ("DP003", 0x3603, "Abs. max. Gebl. TWW"),
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
    register = _register("HP002", 0x2302, 0, "ignored fallback")
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
