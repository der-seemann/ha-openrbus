# Entity names and suggested IDs

Home Assistant entity names use Core's localized register label unless the
German label is an abbreviated manufacturer short name. A code-keyed glossary
then uses a concise, normalized German expansion grounded in the original
ProfiTool OBD 1.47 German medium descriptions. It does not expand abbreviations
with global text replacement.

## Source-audited German labels

Every row below was joined by exact object address and `FriendlyName` in OBD
1.47. The HA label is a concise normalized rendering of the German source
meaning, not a copied translation paragraph. The glossary is a manually
reviewed subset of 43 codes selected for common short forms resolved by the
per-register German medium description. It does not replace every vendor
short label.

For remaining labels, HA expands only three exact-token abbreviations through
reviewed code whitelists: `HK` becomes *Heizkreis* for 49 codes, `TWW` becomes
*Trinkwarmwasser* for 14 codes, and `WP` becomes *Wärmepumpe* for 133 codes.
Each whitelist code has a matching OBD register code/address and a German
medium description spelling out the corresponding term. The same code sets
support concise English ID labels (`heating_circuit`, `domestic_hot_water`,
`heat_pump`). Substrings such as `PWM` do not match `WP`. If a medium
description only repeats an abbreviation, the automatic expansion is not
allowed; the 43 direct glossary entries above still take precedence.

| Code | Address | HA German label |
| --- | --- | --- |
| CP000 | `3401:00` | Maximaler Vorlauftemperatur-Sollwertbereich |
| CP010 | `3402:00` | Vorlauftemperatur-Sollwert ohne Außensensor |
| CP020 | `3404:00` | Funktion des Heizkreises |
| CP030 | `3405:00` | Regelbereich des Heizkreis-Mischventils |
| CP040 | `3408:00` | Pumpennachlaufzeit des Heizkreises |
| CP050 | `3409:00` | Mischerüberhöhung des Heizkreises |
| CP060 | `340a:00` | Raumsollwert des Heizkreises im Ferienbetrieb |
| CP070 | `340b:00` | Raumsollwert des Heizkreises im Nachtbetrieb |
| CP080 | `340c:00` | Raumsollwert der Heizkreisaktivität |
| CP130 | `340e:00` | Außentemperaturfühler für den Heizkreis |
| CP200 | `3413:00` | Raumtemperatur-Sollwert im Heizkreis-Kühlbetrieb |
| CP210 | `3414:00` | Komfort-Startwert des Heizkreises |
| CP220 | `3415:00` | Nacht-Startwert des Heizkreises |
| CP230 | `3416:00` | Steigung der Heizkurve |
| CP240 | `3417:00` | Einfluss des Raumgeräts auf den Heizkreis |
| CP250 | `3418:00` | Kalibrierung des Raumtemperaturfühlers |
| CP260 | `3419:00` | Mindestvorlauftemperatur des Heizkreises |
| CP270 | `341a:00` | Sollwert für Fußbodenkühlung |
| CP280 | `341b:00` | Kühlsollwert des Gebläsekonvektors |
| CP290 | `341c:00` | Pumpenausgangskonfiguration des Heizkreises |
| CP310 | `341e:00` | Automatische Anpassung der Heizkurve |
| CP320 | `341f:00` | Betriebsart des Heizkreises |
| CP340 | `3424:00` | Reduzierter Nachtbetrieb des Heizkreises |
| CP400 | `342a:00` | Dauer der Anti-Legionellenfunktion |
| CP460 | `3430:00` | Trinkwarmwasser-Vorrang des Heizkreises |
| CP500 | `3450:00` | Vorlauftemperatursensor des Heizkreises aktiviert |
| CP530 | `3453:00` | PWM-Pumpendrehzahl im Heizkreis |
| CP560 | `3456:00` | Anti-Legionellenhäufigkeit des Heizkreises |
| CP570 | `3458:00` | Zeitprogramm des Heizkreises |
| CP630 | `345e:00` | Starttag der Anti-Legionellenfunktion |
| CP660 | `3463:00` | Heizkreis-Symbol für Anzeige und Raumgerät |
| CP680 | `3465:00` | Raumgeräte-Buskanal des Heizkreises |
| CP700 | `3467:00` | Offset des Trinkwarmwasserfühlers |
| CP730 | `346a:00` | Heizkreis-Aufheizgeschwindigkeit |
| CP740 | `346b:00` | Heizkreis-Abkühlgeschwindigkeit |
| CP780 | `3471:00` | Regelungsstrategie des Heizkreises |
| CP800 | `3473:00` | Heizmodus des gewerblichen Trinkwarmwasserspeichers |
| CP850 | `347d:00` | Hydraulischer Abgleich im Heizkreis möglich |
| CP900 | `3478:00` | Trinkwarmwasser-Zirkulation |
| DP004 | `3604:00` | Häufigkeit der Anti-Legionellenfunktion |
| DP047 | `3630:00` | Maximale Dauer der Trinkwarmwasserbereitung |
| HP003 | `2303:00` | Minimale Vorlauftemperatur der Wärmepumpe im Kühlbetrieb |
| HP180 | `23ab:00` | Externer Drucksensor |

## Suggested entity IDs

New register entity IDs include the normalized device family and node, a
parameter code when the exact register mapping is supported, a known
heating-circuit (`hk_N`) or zone (`zone_N`) slot, and a concise English name.
For example:

`ehc_16_node_1_cp730_hk_1_heat_up_speed`

The original OBD metadata assigns CP730 to array head `346a:00`. The Brötje
IWR training manual explicitly lists CP730–CP734 for heating-circuit slots
1–5; Core must also show the exact family/address evidence before HA uses one
of those per-slot codes. The same training manual lists explicit five-slot
code groups for CP000–CP070, CP200–CP290, CP320–CP400, CP420–CP440,
CP460–CP570, and CP600–CP780. HA limits derivation to those listed groups,
the EHC-16/SCB-10 families, and slots 1–5. It excludes the larger CP080/CP140
arrays and does not calculate codes from `MaxArraySize` alone. The manual's
per-slot code listing is supplementary evidence; Core catalog address/code
and family/address provenance remains required.

Supplemental code-list source: [Brötje IWR training manual 7830322 (2022),
section 4.1.3](https://polo.broetje.de/pdf/7830322-schulung_wgb%3D1%3Dpdf_%28bdr_a4_manual%29%3Dde-de_.pdf).

When the zone label already says `Heizkreis 1` or `Heating circuit 1`, a
matching leading circuit phrase is removed from the entity name to avoid a
duplicate heading. Other wording stays intact.

The suggestion does not change an existing entity ID. The registry `unique_id`
generation, config-entry options, and user overrides remain unchanged.
