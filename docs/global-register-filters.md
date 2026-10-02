# Global register filters: reviewed mapping

The two optional filters are installation-wide and default off. The mapping in
`custom_components/openrbus/optional_register_filters.py` is an explicit set
of exact CANopen index/subindex identities. It does not inspect label
fragments, infer membership from neighboring objects, or use manufacturer
navigation categories. The catalog remains responsible for deciding which
registers are projected for each discovered device. Core's family evidence
therefore scopes family-known rows (EHC-16, SCB-10, and MK-3); entries without
family evidence are filtered only if the current node's runtime capability
actually exposed that exact object.

## Cooling

The positive set contains the heat-pump cooling limits and setpoints
(`2303:00`, `234f:00`, `2350:00`, `2354:00`, `2355:00`, `23a7:00`,
`23b3:00`, `23b4:00`, `23cb:00`, `23cc:00`, `23d8:00`, `23d9:00`), explicit
cooling configuration and capacity values (`3011:00`, `301e:00`, `301f:00`,
`303c:00`, `30f3:00`–`30f5:00`, `30f9:00`, `30fa:00`, `30ff:00`, `3103:00`),
cooling-specific control terms (`3218:00`–`321a:00`, `321d:00`), zone cooling
activities and setpoints (`3411:00`, `3412:00`, `341a:00`, `341b:00`,
`3460:00`, `3466:00`, `346b:00`), and buffer/cascade cooling configuration
(`3504:00`, `370a:00`, `370e:00`, `384b:00`–`384e:00`, `5724:00`).

Cooling hardware and outcome values are included where the OBD function
identifies the heat-pump free-cooling valve (`430f:00`), heat-pump flow
setpoint (`4321:00`, HM033), cooling energy consumption/production
(`5046:00`, `5087:00`, `512e:00`, `5132:00`, `513c:00`, `5140:00`, `5149:00`),
or hours in cooling mode (`530f:00`). This covers the user examples: EHC-16
HM033, MK-3 cooling energy consumption and production, SCB-10 BP004 buffer
cooling setpoint, and zone floor-cooling setpoint CP270. Family-specific
availability comes from the Core catalog; for example `4321:00` is EHC-16,
`3504:00` and `370a:00` are SCB-10, and `513c:00`/`5140:00` are MK-3.

The map also contains a small number of cooling-only Core entries for which
the normalized family-evidence list is empty. These remain gated by actual
runtime capability on a node; an address does not make a new device or catalog
category appear.

## Screed drying

The positive set is `344d:00`–`344f:00` (CP470–CP490 screed duration and
temperature configuration), `3483:00`–`348c:00` (ZP000–ZP090, three program
steps, their start/end temperatures, and enable), and `5447:00`–`544a:00`
(ZM000/ZM010/ZM020/ZC000 current setpoint, start/end time, and remaining
duration). The OBD function text for ZP000 explicitly identifies the number
of days in the first screed-drying step. Core device evidence associates
ZP000–ZP090 and ZM000–ZC000 with EHC-16 and SCB-10; CP470–CP490 are filtered
only when exposed by a node catalog/runtime capability.

## Reviewed exclusions and ambiguous cases

The full semantic candidate scan was checked against the Core localized
catalog and OBD 1.47 datapoint name/description/function. The following
cooling-word matches are deliberately classified as neither filter:

| Addresses / register | Decision | Reason |
| --- | --- | --- |
| `2275:00` | Neither | Burner fan pre-purge cools the heat exchanger during forced DHW calibration. |
| `201c:00`, `2304:00`, `30d6:00`, `430e:00`, `434a:00`, `434b:00`, `540c:00` | Neither | Shared heating/cooling mapping, hysteresis, state, valve, mode, modulation, or bitfield; not a cooling-only entity. |
| `23a5:00`, `2a0f:00`–`2a15:00`, `2a43:00`, `2a49:00`–`2a4a:00`, `2a58:00`–`2a59:00`, `4a01:00` | Neither | Solar/DHW/CH tank or DHW heat-pump restart recooling, distinct from system cooling mode. |
| `342e:00`, `3613:00` | Neither | DHW zone release or pump delay prevents unintended tank/plate cooling; both belong to DHW operation. |
| `461a:00`, `461e:00`, `4639:00`, `463a:00`, `490e:03`–`490e:04` | Neither | Fuel-cell, water-treatment, or CHP engine coolant circuits, not building cooling mode. |
| `4858:06` | Neither (uncertain) | Inverter cooling-water temperature; current OBD description does not establish that this is the system cooling-mode function, so it is retained. |
| `AP059` | Neither | Alternate third-party mapping contains both heating and cooling encodings. |

Where evidence is mixed or describes a distinct cooling process, the
conservative result is “neither” and the register remains visible. The exact
positive set and this exclusion list are the review record; no complete
FunctionGroup-to-device assignment is required or implied.
