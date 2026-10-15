# Holiday register opt-in — 2026-10-09

This source-only candidate adds a default-off `holiday_enabled` option to the
config and options flows. In German Home Assistant presents it as
„Ferienfunktionen anzeigen“ and describes that it exposes holiday schedules
and temperatures when the controller provides them. The option controls HA
selection, entity visibility and polling only; it does not issue device writes.

## Exact register scope

The opt-in uses an explicit CANopen address map with seven entries:

| Address | OBD object | Function |
|---|---|---|
| `340a:00` | `parZoneAmbiantHolidaySetpoint` (CP060) | Zone holiday room setpoint |
| `3421:00` | `parZoneStartTimeHoliday` | Zone holiday start |
| `3422:00` | `parZoneEndTimeHoliday` | Zone holiday end |
| `3427:00` | `parZoneDhwHolidaySetpoint` (CP370) | Zone holiday DHW setpoint |
| `365e:00` | `parZoneDHWPrimaryStartTimeHoliday` (DP170) | Primary DHW holiday start |
| `365f:00` | `parZoneDHWPrimaryEndTimeHoliday` (DP180) | Primary DHW holiday end |
| `3675:00` | `parZoneDhwPrimaryHolidaySetpoint` (DP337) | Primary DHW holiday setpoint |

Evidence: manufacturer OBD 1.47 `BDR_OBD.xml` metadata at the exact addresses
identifies these as holiday parameters; each is an array with a declared
maximum size of ten. Core v0.4.7 resolves each address and wire definition.
The existing optional-array classifier accepts nonzero subindices only when
the Core catalog confirms the exact array head, bound, code, wire type and
storage with no type conflict. The source-side zone association and register
join for the zone entries are recorded in `zone-object-map-audit.md`.

The exact map excludes `5413:00` (CM130 current zone activity), `560e:00`
(DM009 DHW mode), `560f:00` (DM019 current DHW activity), and `5628:00`
(DM084 current DHW mode). Their descriptions mention Holiday or Antifrost as
one of several current states; they are mixed operational signals, not
holiday schedule or setpoint controls. Generic reduced, comfort and
antifrost registers are also outside this option.

## Lifecycle

With the option off (including for older entries without the key), the seven
registers are excluded before entity selection and polling. Existing entity
registry rows retain their stable unique IDs. Turning the option off marks
integration-owned rows disabled; a user-disabled row remains user-disabled.
If another optional category also classifies a row, all corresponding opt-ins
must be on before it is projected.

## Validation

Focused tests cover the exact seven-address map and mixed-status exclusions,
Core array bounds and wire consistency, off/on selection, independent
cross-membership gates, config/options defaults and persisted preference, and
registry cleanup with user-disable preservation. No Test-HA runtime, production
system, physical write, OTA or release was used for this candidate.
