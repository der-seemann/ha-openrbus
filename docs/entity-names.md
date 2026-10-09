# Entity names and suggested IDs

Home Assistant entity names use Core's localized register label unless the
German label has an exact, reviewed code-keyed override. These overrides use a
concise, normalized German expansion grounded in ProfiTool OBD 1.47. They do
not expand abbreviations with global text replacement.

The 2026-10-09 follow-up resolves the reported `Gem.PuSpTemp` label for BM001
(`5501:00`) as **Gemessene Pufferspeichertemperatur**. The exact OBD 1.47
`varBufferTankTemperature` row has German medium description
`Pufferspeichertemperatur` and long description
`Gemessene Pufferspeichertemperatur`; the longer form preserves that this is a
measured value. The English label is **Measured buffer tank temperature**.
HA validates the exact code/address pair before applying either override.

## Source-audited German labels

Every row below was joined by exact object address and `FriendlyName` in OBD
1.47. The HA label is a concise normalized rendering of the German source
meaning, not a copied translation paragraph. The glossary is a manually
reviewed subset of 53 codes selected for common short forms resolved by the
per-register German medium or long description. It does not replace every
vendor short label.

For remaining labels, HA expands only three exact-token abbreviations through
reviewed code whitelists: `HK` becomes *Heizkreis* for 52 codes, `TWW` becomes
*Trinkwarmwasser* for 53 codes, and `WP` becomes *Wärmepumpe* for 134 codes.
Each whitelist code has a matching OBD register code/address and a German
medium or long description spelling out the corresponding term. The same code sets
support concise English ID labels (`heating_circuit`, `domestic_hot_water`,
`heat_pump`). Substrings such as `PWM` do not match `WP`. If a medium
description only repeats an abbreviation, the automatic expansion is not
allowed; the direct glossary entries above still take precedence.

The display-only whole-token map also expands source-verified short forms such
as `Hzg`, `Temp.`, `Vorlauftemp.`, `Rücklauftemp.`, `Außentemp.`, `Sollw.`,
`Abt.`, `Konf.`, `Betr.`, `TWWSp`, `TVorlauf`, `Trücklauf`, `Drehz`, `Ges.`,
`Gel.`, and `Energieverbr.` for exact catalog codes. English slug expansion
continues to use the earlier frozen code lists; these display changes do not
change the existing suggested-ID helper or existing entity IDs. The separate
compact-ID candidate sets exact IDs at platform registration time for future
registry entries; its schema, collision audit, and migration boundary are
documented in [the dated candidate note](compact-entity-ids-2026-10-09.md).

| Code | Address | HA German label |
| --- | --- | --- |
| AP004 | `3004:00` | Verzögerungszeit bis zur Öffnung des Hydraulikventils |
| BM001 | `5501:00` | Gemessene Pufferspeichertemperatur |
| CM030 | `5404:00` | Heizkreis-Raumtemperatur |
| CM210 | `542e:00` | Außentemperatur des Heizkreises |
| CM220 | `542f:00` | Kurzzeitmittel der Außentemperatur des Heizkreises |
| CM230 | `5430:00` | Langzeitmittel der Außentemperatur des Heizkreises |
| CM380 | `5411:00` | Status des Heizkreis-Sicherheitstemperaturbegrenzers |
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
| CP140 | `3412:00` | Raumsollwert der Heizkreisaktivität im Kühlbetrieb |
| CP190 | `342b:00` | Heizkreis-Sicherheitstemperaturbegrenzer aktiviert |
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
| CP750 | `346c:00` | Maximale Vorheizzeit des Heizkreises |
| CP780 | `3471:00` | Regelungsstrategie des Heizkreises |
| CP800 | `3473:00` | Heizmodus des gewerblichen Trinkwarmwasserspeichers |
| CP850 | `347d:00` | Hydraulischer Abgleich im Heizkreis möglich |
| CP900 | `3478:00` | Trinkwarmwasser-Zirkulation |
| DP004 | `3604:00` | Häufigkeit der Anti-Legionellenfunktion |
| DP047 | `3630:00` | Maximale Dauer der Trinkwarmwasserbereitung |
| HP003 | `2303:00` | Minimale Vorlauftemperatur der Wärmepumpe im Kühlbetrieb |
| HP180 | `23ab:00` | Externer Drucksensor |

The CM030/CM210/CM220/CM230 and CP750 labels are direct normalized glossary
entries, joined by exact catalog address and code. CM030/CM210/CM220/CM230
items use the positive source-mapped zone slot dimension. CP140 is readable by
name but its array dimension is unresolved: neither its item ordinal nor its
`:00` head is treated as a heating-circuit slot. CP080 has the same unresolved
slot association. Array headers at `:00` are never emitted as zone slots.
CP190 and CM380 share a manufacturer short label but have distinct exact-code
meanings: the setting enables the circuit safety thermostat check; the status
register reports its current state. CP020 selectors remain parent-device
entities. Their item names include `Zone N` to identify the selector slot,
including when its state is inactive, unknown, or unavailable; the label does
not claim the zone is active. Existing user-assigned entity names and stable
unique IDs are preserved.

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

## Live abbreviated-name follow-up — 2026-10-09

A second read-only authenticated Test-HA scan found 945 OpenRBus registry rows
and 580 rows with a current HA state. A broad token scan flagged 245 distinct
labels across 277 registrations for possible abbreviations. This is a review
set, not a count of confirmed defects: it deliberately catches mixed cases
such as the valid domain terms `HK` and `TWW`, and manufacturer labels can be
abbreviated even when the token scan misses them. The previously audited 43
repeated state-name groups and 153 repeated registry-name groups are a
different, duplicate-based census.

This follow-up adds source-backed corrections including `Gem.PuSpTemp` →
`Gemessene Pufferspeichertemperatur` (`BM001`, `5501`) and `Hydr. Ventil Verz.`
→ `Verzögerungszeit bis zur Öffnung des Hydraulikventils` (`AP004`, `3004`).
The code-scoped families cover many live heating-circuit, domestic-hot-water,
temperature, setpoint, defrost, and pump labels. They use exact German medium
or long-description evidence and whole-token matching; they do not infer from
substrings such as `PWM` or apply to another register with similar wording.
The earlier English slug whitelists stay frozen at 49/14/133 codes, so the
German display expansions do not alter the existing ID suggestion behavior.

The token scan is a review aid: its 245 candidates are not 245 confirmed defects.
Source-backed direct names now cover the reviewed `Max.`/`Min.` cases where
parameter codes and German long descriptions distinguish the measure and its
condition (including the separate start/stop thresholds HP070–HP073). The
remaining candidates could not be given a safe expansion from the available
exact Core code/address join and OBD German medium/long description; those need
that source evidence before a precise label can be written. This is the explicit
coverage boundary, not a claim that every disabled or unlabelled catalog entry
has been exhaustively source-audited. The earlier 43 repeated-state / 153
repeated-registry duplicate audit is a separate census. CP020 selector-slot
behavior remains unchanged.

Provenance: authenticated config-entry, state, and entity-registry read APIs
on the isolated Test-HA; Core catalog address/code joins; and the exact German
ProfiTool OBD 1.47 BM001 translation fields. No register values, credentials,
user overrides, or entity-ID/unique-ID mappings were changed. Test-HA was not
mutated or installed from this source candidate.
