"""Source-grounded register names and Home Assistant entity ID suggestions."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from openrbus.discovery import DeviceIdentity
from openrbus.registry import Registry

from .zones import (
    ZoneKind,
    profile_for,
    zone_device_name,
    zone_enabled,
    zone_subindex,
)

# Normalized, concise names derived from the German medium descriptions in
# ProfiTool OBD 1.47. Each code was joined to the source metadata's exact
# FriendlyName and object address; see docs/entity-names.md. Keep these keyed
# by exact codes because abbreviations are context-dependent.
_REGISTER_NAMES_DE = {
    # BM001 is the measured buffer-tank temperature. Its manufacturer short
    # form "Gem.PuSpTemp" is expanded from the exact 5501 OBD translation.
    "BM001": "Gemessene Pufferspeichertemperatur",
    "CM030": "Heizkreis-Raumtemperatur",
    "CM210": "Außentemperatur des Heizkreises",
    "CM220": "Kurzzeitmittel der Außentemperatur des Heizkreises",
    "CM230": "Langzeitmittel der Außentemperatur des Heizkreises",
    "CM380": "Status des Heizkreis-Sicherheitstemperaturbegrenzers",
    "AP004": "Verzögerungszeit bis zur Öffnung des Hydraulikventils",
    "CM320": "Geschätzte Zeit bis zum Start der elektrischen Zusatzheizung für den Trinkwarmwasserspeicher",
    "CP360": "Trinkwarmwasser-Sollwert des Kreises im Eco-Betrieb",
    "CP430": "Optimierung der Trinkwarmwasser-Speicherladung",
    "HP002": "Maximale Vorlauftemperatur der Wärmepumpe ohne Zusatzerzeuger",
    "HP071": "Minimale Rücklauftemperatur der Wärmepumpe, unterhalb der das Abtauen abgebrochen wird",
    "HP072": "Minimale Vorlauftemperatur der Wärmepumpe, unterhalb der der Zusatzerzeuger beim Abtauen startet",
    "HP094": "Startzeit des leisen Betriebs der Wärmepumpe",
    "HP095": "Endzeit des leisen Betriebs der Wärmepumpe",
    "HP155": "Mindeststillstandszeit des Wärmepumpenverdichters",
    "HP010": "Mindestdurchflussmenge",
    "HP045": "Maximale Schaltdifferenz zum Ausschalten der Wärmepumpe",
    "HP046": "Minimale Schaltdifferenz zum Ausschalten der Wärmepumpe",
    "HP070": "Minimale Rücklauftemperatur der Wärmepumpe, unterhalb der der Zusatzerzeuger beim Abtauen startet",
    "HP073": "Minimale Vorlauftemperatur der Wärmepumpe, unterhalb der das Abtauen gestoppt wird",
    "HP074": "Minimale Schaltdifferenz der Wärmepumpe",
    "HP090": "Maximale Fehlerverstärkung für Meldungen an den hydraulischen Zusatzerzeuger",
    "HP143": "Maximale Trinkwarmwassertemperatur für den Wärmepumpenbetrieb",
    "HP161": "Mindestbetriebszeit des Verdichters der Wärmepumpe",
    "HP164": "Minimale Vorlauftemperatur, bei der der Verdichter arbeiten darf",
    "HP165": "Minimale Vorlauftemperatur vor dem Neustart des Verdichters",
    "HP206": "Maximale Außentemperatur, die die Außeneinheit während des intelligenten Zuordnungsalgorithmus absenkt",
    "HP208": "Maximale Vorlauftemperatur der Außeneinheit während des intelligenten Zuordnungsalgorithmus",
    "AP006": "Mindestwasserdruck für die Meldung eines niedrigen Wasserdrucks",
    "AP007": "Vorlauftemperaturgrenze, deren Überschreitung das Gerät für zehn Minuten sperrt",
    "AP077": "Höchste darstellbare Ebene für Parameter und Signale",
    "AP080": "Außentemperaturgrenze zur Aktivierung des Frostschutzes",
    "AP092": "Sollwert für die maximale Heizleistung",
    "PP016": "Maximale Pumpendrehzahl im Heizbetrieb",
    "DP011": "Maximale Temperatur zum Abschalten des Wärmeerzeugers für die Trinkwarmwasserbereitung",
    "DP037": "Minimale Drehzahl der Trinkwarmwasser-Ladepumpe",
    "DP038": "Maximale Drehzahl der Trinkwarmwasser-Ladepumpe",
    "DP476": "Mindestlaufzeit der Trinkwarmwasser-Pumpe",
    "HM129": "Maximale Frequenzbegrenzung im leisen Betrieb",
    "HP047": "Minimale Startverzögerung des Zusatzerzeugers bei Erreichen seiner minimalen Außentemperatur",
    "HP048": "Maximale Startverzögerung des Zusatzerzeugers bei Erreichen seiner maximalen Außentemperatur",
    "HP049": "Minimale Außentemperatur für die Startverzögerung des Zusatzerzeugers",
    "HP050": "Maximale Außentemperatur für die Startverzögerung des Zusatzerzeugers",
    "HP051": "Minimale Außentemperatur für den Betrieb der Wärmepumpe",
    "HP178": "Maximale Strombegrenzung der Außeneinheit",
    "AP063": "Maximaler Vorlauftemperatur-Sollwert für Heizung",
    "AP084": "Maximal verfügbare Leistung für die Trinkwarmwasserbereitung",
    "AP061": "Maximale Korrektur der Systemtemperatur bei vorhandener Anlagentemperatur",
    "PP018": "Minimale Pumpendrehzahl im Heizbetrieb",
    "DP003": "Maximale Gebläsedrehzahl bei Trinkwarmwasserbereitung",
    "DP048": "Minimale Heizdauer zwischen Trinkwarmwasserbereitungsperioden",
    "BP015": "Minimale Nachlaufzeit der Pufferladepumpe",
    "EP030": "Minimaler Temperatursollwert für die 0–10-Volt-Anforderung",
    "EP031": "Maximaler Temperatursollwert für die 0–10-Volt-Anforderung",
    "EP032": "Minimaler Leistungssollwert für die 0–10-Volt-Anforderung",
    "EP033": "Maximaler Leistungssollwert für die 0–10-Volt-Anforderung",
    "EP034": "Minimaler Spannungssollwert für die 0–10-Volt-Anforderung",
    "EP035": "Maximaler Spannungssollwert für die 0–10-Volt-Anforderung",
    "CP000": "Maximaler Vorlauftemperatur-Sollwertbereich",
    "CP010": "Vorlauftemperatur-Sollwert ohne Außensensor",
    "CP020": "Funktion des Heizkreises",
    "CP030": "Regelbereich des Heizkreis-Mischventils",
    "CP040": "Pumpennachlaufzeit des Heizkreises",
    "CP050": "Mischerüberhöhung des Heizkreises",
    "CP060": "Raumsollwert des Heizkreises im Ferienbetrieb",
    "CP070": "Raumsollwert des Heizkreises im Nachtbetrieb",
    "CP080": "Raumsollwert der Heizkreisaktivität",
    "CP130": "Außentemperaturfühler für den Heizkreis",
    "CP140": "Raumsollwert der Heizkreisaktivität im Kühlbetrieb",
    "CP190": "Heizkreis-Sicherheitstemperaturbegrenzer aktiviert",
    "CP200": "Raumtemperatur-Sollwert im Heizkreis-Kühlbetrieb",
    "CP210": "Komfort-Startwert des Heizkreises",
    "CP220": "Nacht-Startwert des Heizkreises",
    "CP230": "Steigung der Heizkurve",
    "CP240": "Einfluss des Raumgeräts auf den Heizkreis",
    "CP250": "Kalibrierung des Raumtemperaturfühlers",
    "CP260": "Mindestvorlauftemperatur des Heizkreises",
    "CP270": "Sollwert für Fußbodenkühlung",
    "CP280": "Kühlsollwert des Gebläsekonvektors",
    "CP290": "Pumpenausgangskonfiguration des Heizkreises",
    "CP310": "Automatische Anpassung der Heizkurve",
    "CP320": "Betriebsart des Heizkreises",
    "CP340": "Reduzierter Nachtbetrieb des Heizkreises",
    "CP400": "Dauer der Anti-Legionellenfunktion",
    "CP460": "Trinkwarmwasser-Vorrang des Heizkreises",
    "CP500": "Vorlauftemperatursensor des Heizkreises aktiviert",
    "CP530": "PWM-Pumpendrehzahl im Heizkreis",
    "CP560": "Anti-Legionellenhäufigkeit des Heizkreises",
    "CP570": "Zeitprogramm des Heizkreises",
    "CP630": "Starttag der Anti-Legionellenfunktion",
    "CP660": "Heizkreis-Symbol für Anzeige und Raumgerät",
    "CP680": "Raumgeräte-Buskanal des Heizkreises",
    "CP700": "Offset des Trinkwarmwasserfühlers",
    "CP730": "Heizkreis-Aufheizgeschwindigkeit",
    "CP740": "Heizkreis-Abkühlgeschwindigkeit",
    "CP750": "Maximale Vorheizzeit des Heizkreises",
    "CP780": "Regelungsstrategie des Heizkreises",
    "CP800": "Heizmodus des gewerblichen Trinkwarmwasserspeichers",
    "CP850": "Hydraulischer Abgleich im Heizkreis möglich",
    "CP900": "Trinkwarmwasser-Zirkulation",
    "DP004": "Häufigkeit der Anti-Legionellenfunktion",
    "DP047": "Maximale Dauer der Trinkwarmwasserbereitung",
    "HP003": "Minimale Vorlauftemperatur der Wärmepumpe im Kühlbetrieb",
    "HP180": "Externer Drucksensor",
}

# Concise English slugs for the same reviewed code/address set. These expand
# awkward source labels for new HA entity IDs; all other IDs use Core's
# localized English short label.
_REGISTER_NAMES_EN = {
    "CP000": "maximum flow temperature setpoint range",
    "CP010": "flow temperature setpoint without outdoor sensor",
    "CP020": "heating circuit function",
    "CP030": "heating circuit mixing valve control range",
    "CP040": "heating circuit pump overrun time",
    "CP050": "heating circuit mixing valve boost",
    "CP060": "heating circuit holiday room temperature setpoint",
    "CP070": "heating circuit night room temperature setpoint",
    "CP080": "heating circuit activity room temperature setpoint",
    "CP130": "heating circuit outdoor sensor",
    "CP200": "heating circuit cooling room temperature setpoint",
    "CP210": "heating circuit comfort start value",
    "CP220": "heating circuit night start value",
    "CP230": "heating curve slope",
    "CP240": "heating circuit room unit influence",
    "CP250": "room temperature sensor calibration",
    "CP260": "heating circuit minimum flow temperature",
    "CP270": "underfloor cooling setpoint",
    "CP280": "fan coil cooling setpoint",
    "CP290": "heating circuit pump output configuration",
    "CP310": "heating curve auto adaptation",
    "CP320": "heating circuit operating mode",
    "CP340": "reduced night mode",
    "CP400": "anti legionella program duration",
    "CP460": "domestic hot water priority",
    "CP500": "heating circuit flow temperature sensor",
    "CP530": "PWM pump speed",
    "CP560": "anti legionella frequency",
    "CP570": "heating circuit time program",
    "CP630": "anti legionella start day",
    "CP660": "heating circuit display icon",
    "CP680": "room unit bus channel",
    "CP700": "domestic hot water sensor offset",
    "CP730": "heat up speed",
    "CP740": "cool down speed",
    "CP780": "heating control strategy",
    "CP800": "commercial domestic hot water heating mode",
    "CP850": "hydraulic balancing capability",
    "CP900": "domestic hot water circulation",
    "DP004": "anti legionella frequency",
    "DP047": "maximum domestic hot water production time",
    "HP003": "minimum heat pump flow temperature in cooling mode",
    "HP180": "external pressure sensor",
}

# These two exact, per-zone objects share the same manufacturer short label.
# Their normalized meanings come from their distinct OBD medium descriptions:
# the CP190 setting enables the safety thermostat check; CM380 reports status.
_REGISTER_NAMES_EN_DISPLAY = {
    "BM001": "Measured buffer tank temperature",
    "AP004": "Time until the hydraulic valve opens",
    "CM320": "Estimated time until the electric auxiliary heater for the hot water tank starts",
    "CP360": "Domestic hot water circuit setpoint in eco mode",
    "CP430": "Domestic hot water tank charging optimization",
    "HP002": "Maximum heat pump flow temperature without auxiliary heat source",
    "HP072": "Minimum flow temperature for auxiliary heat source start during defrost",
    "HP071": "Minimum heat-pump return temperature below which defrost is aborted",
    "HP094": "Heat pump quiet operation start time",
    "HP095": "Heat pump quiet operation end time",
    "HP155": "Heat pump compressor minimum off time",
    "HP010": "Minimum flow rate",
    "HP045": "Maximum heat pump switch-off differential",
    "HP046": "Minimum heat pump switch-off differential",
    "HP070": "Minimum heat-pump return temperature for auxiliary heat source start during defrost",
    "HP073": "Minimum heat-pump flow temperature at which defrost stops",
    "HP074": "Minimum heat pump switching differential",
    "HP090": "Maximum error gain for hydraulic auxiliary heat source reporting",
    "HP143": "Maximum domestic hot water temperature for heat pump operation",
    "HP161": "Minimum heat pump compressor run time",
    "HP164": "Minimum flow temperature at which the compressor may run",
    "HP165": "Minimum flow temperature before compressor restart",
    "HP206": "Maximum outdoor unit temperature during its intelligent assignment algorithm",
    "HP208": "Maximum outdoor unit flow temperature during its intelligent assignment algorithm",
    "AP006": "Minimum water pressure for low-pressure notification",
    "AP007": "Flow temperature limit that locks the unit for ten minutes when exceeded",
    "AP077": "Highest displayable parameter and signal level",
    "AP080": "Outdoor temperature limit for frost protection activation",
    "AP092": "Setpoint for maximum heating output",
    "PP016": "Maximum pump speed in heating mode",
    "DP011": "Maximum temperature for stopping the heat generator during domestic hot water production",
    "DP037": "Minimum domestic hot water charging pump speed",
    "DP038": "Maximum domestic hot water charging pump speed",
    "DP476": "Minimum domestic hot water pump run time",
    "HM129": "Maximum frequency limit in quiet operation",
    "HP047": "Minimum auxiliary heat source start delay at its minimum outdoor temperature",
    "HP048": "Maximum auxiliary heat source start delay at its maximum outdoor temperature",
    "HP049": "Minimum outdoor temperature for the auxiliary heat source start delay",
    "HP050": "Maximum outdoor temperature for the auxiliary heat source start delay",
    "HP051": "Minimum outdoor temperature for heat pump operation",
    "HP178": "Maximum outdoor unit current limit",
    "AP063": "Maximum heating flow temperature setpoint",
    "AP084": "Maximum available output for domestic hot water production",
    "AP061": "Maximum system temperature correction when a system temperature is available",
    "PP018": "Minimum pump speed in heating mode",
    "DP003": "Maximum fan speed during domestic hot water operation",
    "DP048": "Minimum heating duration between domestic hot water periods",
    "BP015": "Minimum buffer pump overrun time",
    "EP030": "Minimum temperature setpoint for the 0 to 10 volt request",
    "EP031": "Maximum temperature setpoint for the 0 to 10 volt request",
    "EP032": "Minimum output setpoint for the 0 to 10 volt request",
    "EP033": "Maximum output setpoint for the 0 to 10 volt request",
    "EP034": "Minimum voltage setpoint for the 0 to 10 volt request",
    "EP035": "Maximum voltage setpoint for the 0 to 10 volt request",
    "CP190": "Heating circuit safety thermostat enabled",
    "CM380": "Heating circuit safety thermostat status",
}

# These static code whitelists were generated from exact Core address/code ↔
# OBD 1.47 `FriendlyName` joins and manually checked against each matching
# German `MediumDescription` or `LongDescription`. Only whole-token
# short-label replacements are allowed. Unconfirmed abbreviations stay as
# published by Core.
_HK_CODES = frozenset(
    [
        "BP018",
        "BP028",
        "BP054",
        "CM040",
        "CM060",
        "CM070",
        "CM080",
        "CM090",
        "CM110",
        "CM120",
        "CM130",
        "CM190",
        "CM200",
        "CM210",
        "CM220",
        "CM230",
        "CM240",
        "CM260",
        "CM270",
        "CM290",
        "CM300",
        "CP010",
        "CP020",
        "CP030",
        "CP040",
        "CP050",
        "CP060",
        "CP070",
        "CP080",
        "CP130",
        "CP210",
        "CP220",
        "CP230",
        "CP240",
        "CP260",
        "CP290",
        "CP320",
        "CP340",
        "CP530",
        "CP550",
        "CP570",
        "CP610",
        "CP620",
        "CP660",
        "CP670",
        "CP680",
        "CP730",
        "CP740",
        "CP750",
        "CP780",
        "FM020",
        "NM003",
    ]
)
_TWW_CODES = frozenset(
    [
        "AC006",
        "AC025",
        "AC033",
        "AM001",
        "AP084",
        "BM000",
        "CM250",
        "CP430",
        "CP700",
        "CP790",
        "CP900",
        "AC009",
        "AC056",
        "AC057",
        "CM340",
        "CP360",
        "CP710",
        "CP810",
        "CP910",
        "CP920",
        "DC004",
        "DC005",
        "DM007",
        "DM008",
        "DM019",
        "DM029",
        "DP003",
        "DP008",
        "DP010",
        "DP030",
        "DP033",
        "DP047",
        "DP048",
        "DP060",
        "DP080",
        "DP120",
        "DP130",
        "DP334",
        "DP337",
        "DP467",
        "DP468",
        "DP469",
        "EC001",
        "EP148",
        "EP149",
        "GP068",
        "HP143",
        "HP144",
        "HP154",
        "SM069",
        "DP070",
        "DP140",
        "DP200",
    ]
)
_WP_CODES = frozenset(
    [
        "DP467",
        "DP468",
        "DP469",
        "EC000",
        "EC001",
        "EM263",
        "EM264",
        "EM265",
        "EM266",
        "EM267",
        "EM268",
        "EM269",
        "EM270",
        "EM271",
        "EM324",
        "EM411",
        "EP023",
        "EP024",
        "EP025",
        "EP026",
        "EP027",
        "EP096",
        "EP146",
        "EP147",
        "EP148",
        "EP149",
        "EP150",
        "EP151",
        "EP152",
        "HC006",
        "HC007",
        "HC009",
        "HM001",
        "HM002",
        "HM003",
        "HM008",
        "HM009",
        "HM010",
        "HM015",
        "HM017",
        "HM020",
        "HM021",
        "HM022",
        "HM023",
        "HM025",
        "HM026",
        "HM027",
        "HM028",
        "HM032",
        "HM033",
        "HM034",
        "HM035",
        "HM036",
        "HM037",
        "HM038",
        "HM039",
        "HM040",
        "HM041",
        "HM042",
        "HM043",
        "HM044",
        "HM045",
        "HM046",
        "HM049",
        "HM050",
        "HM051",
        "HM054",
        "HM089",
        "HM090",
        "HM095",
        "HM103",
        "HM115",
        "HM116",
        "HM134",
        "HM154",
        "HM157",
        "HM158",
        "HM160",
        "HM161",
        "HM172",
        "HM173",
        "HM183",
        "HM184",
        "HP001",
        "HP002",
        "HP003",
        "HP004",
        "HP012",
        "HP019",
        "HP020",
        "HP023",
        "HP024",
        "HP025",
        "HP026",
        "HP028",
        "HP045",
        "HP046",
        "HP051",
        "HP052",
        "HP053",
        "HP056",
        "HP057",
        "HP058",
        "HP071",
        "HP072",
        "HP074",
        "HP075",
        "HP083",
        "HP109",
        "HP110",
        "HP136",
        "HP137",
        "HP138",
        "HP139",
        "HP140",
        "HP143",
        "HP146",
        "HP155",
        "HP156",
        "HP157",
        "HP158",
        "HP159",
        "HP160",
        "HP161",
        "HP199",
        "HP200",
        "HP201",
        "HP202",
        "HP203",
        "HP211",
        "NP327",
        "PP011",
        "PP021",
        "PP022",
    ]
)
# Preserve the pre-existing English slug behavior while German display-name
# coverage grows. Suggested IDs are a separate, explicitly frozen concern.
_HK_ID_CODES = _HK_CODES - {"CM110", "CP080", "CP340"}
_TWW_ID_CODES = _TWW_CODES - {
    "AC009",
    "AC056",
    "AC057",
    "CM340",
    "CP360",
    "CP710",
    "CP810",
    "CP910",
    "CP920",
    "DC004",
    "DC005",
    "DM007",
    "DM008",
    "DM019",
    "DM029",
    "DP003",
    "DP008",
    "DP010",
    "DP030",
    "DP033",
    "DP047",
    "DP048",
    "DP060",
    "DP080",
    "DP120",
    "DP130",
    "DP334",
    "DP337",
    "DP467",
    "DP468",
    "DP469",
    "EC001",
    "EP148",
    "EP149",
    "GP068",
    "HP143",
    "HP144",
    "HP154",
    "SM069",
}
_WP_ID_CODES = _WP_CODES - {"HP155"}
_SOURCE_TERMS_DE = {
    "HK": (_HK_CODES, "Heizkreis"),
    "TWW": (_TWW_CODES, "Trinkwarmwasser"),
    "WP": (_WP_CODES, "Wärmepumpe"),
    "Hzg": (
        frozenset(
            {
                "AC008",
                "AC024",
                "AC039",
                "AC055",
                "AC098",
                "AP063",
                "CP810",
                "EC000",
                "EP146",
                "EP147",
                "GP088",
                "NC000",
                "NP227",
                "NP228",
                "PM002",
                "PP002",
                "PP004",
                "PP006",
                "PP009",
                "PP010",
                "PP016",
                "PP018",
                "PP023",
                "SC068",
                "SM022",
                "SM034",
                "SM070",
            }
        ),
        "Heizung",
    ),
    "Temp.": (
        frozenset(
            {
                "AP184",
                "AP185",
                "CP260",
                "DP001",
                "DP011",
                "DP044",
                "DP512",
                "EM098",
                "FM037",
                "FM038",
                "FM039",
                "FM040",
                "FM041",
                "FM042",
                "FM043",
                "FM044",
                "FM046",
                "FM047",
                "FM048",
                "FM049",
                "HM073",
                "HM094",
                "HP039",
                "HP040",
                "HP160",
                "HP207",
                "IM022",
                "NM001",
                "NP289",
                "NP327",
                "SM023",
                "SM034",
                "SM070",
                "SM131",
                "SM133",
                "SP055",
                "SP056",
                "SP182",
                "SP285",
                "SP286",
                "SP290",
            }
        ),
        "Temperatur",
    ),
    "Vorlauftemp.": (
        frozenset({"AP154", "EM012", "HM185", "HP002", "NM164"}),
        "Vorlauftemperatur",
    ),
    "Rücklauftemp.": (
        frozenset({"EM013", "EM148", "GM014", "HM186", "NM165"}),
        "Rücklauftemperatur",
    ),
    "Außentemp.": (
        frozenset({"AM046", "HP013", "HP051", "HP052", "NM027"}),
        "Außentemperatur",
    ),
    "Speichertemp.": (frozenset({"DM062"}), "Speichertemperatur"),
    "Sollw.": (
        frozenset(
            {
                "AP063",
                "CM070",
                "CM080",
                "CM110",
                "CP350",
                "CP370",
                "CP600",
                "DP008",
                "DP377",
            }
        ),
        "Sollwert",
    ),
    "Abt.": (frozenset({"HP070", "HP071", "HP072", "HP073"}), "Abtauen"),
    "Konf.": (frozenset({"AP055", "HP059", "HP076"}), "Konfiguration"),
    "Betr.": (frozenset({"DP050", "HP094"}), "Betrieb"),
    "Zusatzhzg": (frozenset({"CM320"}), "Zusatzheizung"),
    "TWWSp": (frozenset({"DM001", "DM006"}), "Trinkwarmwasserspeicher"),
    "TVorlauf": (
        frozenset({"AM016", "CM040", "EM269", "HP012", "HP060", "HP072", "HP073"}),
        "Vorlauftemperatur",
    ),
    "Trücklauf": (
        frozenset({"AM018", "EM270", "HP070", "HP071"}),
        "Rücklauftemperatur",
    ),
    "Drehz": (
        frozenset(
            {
                "CP530",
                "DP453",
                "DP454",
                "GP007",
                "GP042",
                "HM023",
                "HM069",
                "NM169",
                "NP295",
                "NP296",
                "PP016",
                "PP018",
                "SM036",
                "SM047",
                "SP162",
                "SP172",
            }
        ),
        "Drehzahl",
    ),
    "Stab.grad": (frozenset({"DP030"}), "Stabilisierungsgradient"),
    "Ges.": (frozenset({"AC103", "EM178", "FC004", "FC005", "PC002"}), "Gesamt"),
    "Energieverbr.": (frozenset({"AC093", "DC001"}), "Energieverbrauch"),
    "Gel.": (frozenset({"AC009", "AC010", "AC070", "AC075"}), "Gelieferte"),
}
_SOURCE_TERMS_EN = {
    "HK": (_HK_ID_CODES, "heating circuit"),
    "TWW": (_TWW_ID_CODES, "domestic hot water"),
    "WP": (_WP_ID_CODES, "heat pump"),
}
_SOURCE_TERM_PATTERNS = {
    token: re.compile(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", re.IGNORECASE)
    for token in _SOURCE_TERMS_DE
}

# The public Core catalog stores array parameter codes at the canonical :00
# head. The IWR manual explicitly enumerates these five-zone code groups.
# Larger arrays (for example CP080/CP140) and unlisted groups are excluded.
_FIVE_ZONE_PARAMETER_ARRAYS = frozenset(
    {
        "CP000",
        "CP010",
        "CP020",
        "CP030",
        "CP040",
        "CP050",
        "CP060",
        "CP070",
        "CP200",
        "CP210",
        "CP220",
        "CP230",
        "CP240",
        "CP250",
        "CP260",
        "CP270",
        "CP280",
        "CP290",
        "CP320",
        "CP330",
        "CP340",
        "CP350",
        "CP360",
        "CP370",
        "CP380",
        "CP390",
        "CP400",
        "CP420",
        "CP430",
        "CP440",
        "CP460",
        "CP470",
        "CP480",
        "CP490",
        "CP500",
        "CP510",
        "CP520",
        "CP530",
        "CP540",
        "CP550",
        "CP560",
        "CP570",
        "CP600",
        "CP610",
        "CP620",
        "CP630",
        "CP640",
        "CP650",
        "CP660",
        "CP670",
        "CP680",
        "CP690",
        "CP700",
        "CP710",
        "CP720",
        "CP730",
        "CP740",
        "CP750",
        "CP760",
        "CP770",
        "CP780",
    }
)

_REGISTRY = Registry.load_default()


def register_display_name(register: Any, language: str) -> str | None:
    """Return a reviewed localized override, or ``None`` to use Core's label."""

    code = getattr(register, "internal_code", None)
    address = getattr(register, "address", None)
    if not isinstance(code, str) or address is None:
        return None
    definition = _REGISTRY.find(address)
    if definition is None or definition.code != code:
        return None

    if language == "de":
        reviewed = _REGISTER_NAMES_DE.get(code)
        if reviewed is not None:
            return reviewed
        return _expand_source_terms(
            getattr(register, "name_de", None), code, _SOURCE_TERMS_DE
        )
    if language == "en":
        return _REGISTER_NAMES_EN_DISPLAY.get(code)
    return None


def parameter_code_for(
    register: Any, identity: DeviceIdentity | None = None
) -> str | None:
    """Return a manufacturer parameter code proven for this exact address."""

    code = getattr(register, "internal_code", None)
    address = getattr(register, "address", None)
    if not isinstance(code, str) or address is None:
        return None

    definition = _REGISTRY.find(address)
    # A catalog entry is the address-to-code join. Do not trust a detached or
    # inconsistent display object to supply a parameter code on its own.
    if definition is None or definition.code != code:
        return None
    if definition.wire.is_array:
        # This manual documents IWR five-zone registers. The Core family must
        # also be resolved to one of the families represented by that model;
        # unknown families and larger/unlisted arrays get no guessed code.
        family = (
            getattr(identity, "family", None) or getattr(identity, "model", None)
            if identity is not None
            else None
        )
        exact_family_address = isinstance(family, str) and any(
            row.address == address and row.family.casefold() == family.casefold()
            for row in definition.evidence.devices
        )
        if (
            code in _FIVE_ZONE_PARAMETER_ARRAYS
            and isinstance(family, str)
            and family.casefold() in {"ehc-16", "scb-10"}
            and exact_family_address
            and 1 <= getattr(address, "subindex", 0) <= 5
        ):
            return f"CP{int(code[2:]) + address.subindex - 1:03d}"
        # :00 is an array count. Never repeat the canonical head code as if
        # it identified every subindex when source coverage is absent.
        return None
    return code


def suggested_object_id(
    parent: Any,
    identity: DeviceIdentity,
    register: Any,
    *,
    english_name: str | None = None,
) -> str:
    """Build a readable, deterministic Home Assistant object ID suggestion."""

    slot = zone_subindex(register, identity)
    profile = profile_for(parent, identity.node, slot) if slot is not None else None
    language = getattr(parent, "language", "de")
    if profile is not None and zone_enabled(parent, identity.node, slot):
        # Home Assistant prefixes suggested IDs with the registered device
        # name when has_entity_name=True. Match that exact name so the ID
        # contains each device/node/zone component once.
        device_name = zone_device_name(profile, language)
    else:
        device_name = (
            getattr(identity, "display_name", None)
            or getattr(identity, "name", None)
            or f"OpenRBus node {identity.node}"
        )
    device_tokens = _slug(device_name).split("_")

    def has_sequence(*tokens: str) -> bool:
        width = len(tokens)
        return any(
            device_tokens[index : index + width] == list(tokens)
            for index in range(len(device_tokens) - width + 1)
        )

    # HA supplies the device name; this suffix supplies only identity that is
    # not already present there. Exact token sequences avoid false matches
    # such as node_1 being found inside model EHC-16.
    parts: list[str] = []
    if not has_sequence("node", str(identity.node)):
        parts.append(f"node_{identity.node}")

    code = parameter_code_for(register, identity)
    if code:
        parts.append(_slug(code))

    if isinstance(slot, int) and slot > 0:
        marker = (
            f"hk_{slot}"
            if profile is not None
            and profile.kind is ZoneKind.HEATING
            and zone_enabled(parent, identity.node, slot)
            else f"zone_{slot}"
        )
        if not (
            has_sequence("zone", str(slot))
            or has_sequence("hk", str(slot))
            or has_sequence(f"hk{slot}")
            or has_sequence("heizkreis", str(slot))
            or has_sequence("heating", "circuit", str(slot))
        ):
            parts.append(marker)

    label = (
        english_name
        or (_REGISTER_NAMES_EN.get(code) if code is not None else None)
        or _expand_source_terms(
            getattr(register, "name_en", None), code, _SOURCE_TERMS_EN
        )
        or getattr(register, "name_en", None)
        or code
        or "register"
    )
    label_slug = _slug(label)
    if slot and slot > 0 and label_slug.startswith("zone_"):
        label_slug = label_slug.removeprefix("zone_")
    parts.append(label_slug or "register")
    return "_".join(parts)


def _expand_source_terms(value: object, code: str | None, terms: dict) -> str | None:
    """Expand only source-audited whole tokens for this exact code."""

    if not isinstance(value, str) or not code:
        return None
    result = value
    changed = False
    for token, (codes, replacement) in terms.items():
        if code in codes:
            result, count = _SOURCE_TERM_PATTERNS[token].subn(replacement, result)
            changed = changed or count > 0
    return result if changed else None


def _slug(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    normalized = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")


__all__ = [
    "parameter_code_for",
    "register_display_name",
    "suggested_object_id",
]
