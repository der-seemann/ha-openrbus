# Register projection semantics

This note describes Home Assistant presentation policy in the integration. It
never changes OpenRBus Core authorization, catalog write declarations, or
physical write behavior.

## Cumulative energy and temperature

Energy rows become read-only HA sensors with device class `energy` and state
class `total_increasing` only when the address is in the reviewed cumulative
counter set in `register_entities.py` and Core still agrees on family, code,
wire/storage type, unit, scale, array layout, and bounds. This includes the
scalar EHC-16 counters `5044:00`, `5045:00`, `5085:00`, `5086:00`, `5088:00`,
`5089:00`, `50da:00`, `50f7:00`, `50f8:00`, and `5144:00`, plus concrete
bounded elements of the reviewed energy arrays `512c`, `512d`, `512f`, `5130`,
`5131`, `5133`, `5139`, `513b`–`5141`. Array count headers, writable settings,
signed/net values, and rows without matching Core evidence are excluded.
Negative, non-finite, boolean, malformed, or out-of-wire counter readings are
not published as sensor states. In particular, choosing an HA sensor for a
source-writable counter suppresses the HA control projection only; Core's
source declaration and write authorization remain unchanged.

Temperature metadata is limited to the source-reviewed temperature measurement
codes listed by `is_temperature_measurement()`. Such numeric sensor values use
device class `temperature` and state class `measurement`. Setpoints and other
controls are not reclassified. Non-numeric, non-finite, or out-of-wire
measurements are unavailable.

## Status labels and unknown values

The 14 unresolved status/error objects are classified only when their exact
address, EHC-16 provenance, Core definition/code, numeric wire and storage
types, scale, no-unit declaration, and scalar/array bounds match the layout
allowlist in `register_entities.py`. They are sensors and carry no unit or
statistics class. An undecoded value maps to a fixed HA state key
(`unknown_status_code` or `unknown_error_code`); the numeric value and raw wire
bytes remain in state attributes. This keeps the translation key stable and
avoids claiming that an unexplained integer has a physical meaning. Unknown
values of fully catalogued enumerations use `unknown_status_code` for the two
status enums, or `unknown_code` for other enumerations; invalid wire values are
unavailable. Existing enum option values and control write encodings are not
changed.

The exact 14 source rows reviewed are `4326:00` (HM038), `4356:00`, `437e:00`
(HM092), `4395:01` (HM144), `4397:00` (HM119), `4399:00` (HM122), `439e:00`
(HM126), `439f:00` (HM127), `43a2:00` (HM130), `43b6:01` (HM162), `43c6:00`
(HM189), `434c:00`, `50b3:01`, and `517c:00` (AM191). The indexed Android OBD
1.47 `BDR_OBD.xml` gives names and short descriptions for these signals but no
enum/bitfield mapping for their values; it does not prove what any individual
code means. A bounded Core and source search found no usable decoder for these
14 layouts. They remain unresolved beyond their source descriptions.

For the catalogued `DeviceSubStatus` enum at `5011:00`, Core's English value
255 is `SuOutOfResetsWait1Hr`; the integration uses the neutral user-facing
translation “Reset limit reached; wait one hour (substatus 255)” without
expanding the unexplained `Su` prefix. Other enum machine values are retained
as returned by Core. Translation keys are assigned from the static source
classification during entity construction, including before the first read;
the API state itself is a stable machine key, while Home Assistant's translated
state is the user-facing phrase. Raw code, source enum label, and wire bytes are
available as attributes. Friendly names and stable unique IDs are unaffected.

## Evidence limits

Source metadata and labels are descriptive evidence, not device write
authorization. SCB optional rows are projected only when the current catalog is supplied with
validated runtime capability evidence. Capability evidence does not grant write
authorization, and proving a capability does not trigger physical reads. The
source-only compact-ID audit maps rows already present in the entity registry;
it does not synthesize absent optional rows. No Test-HA registry migration,
service restart, physical write, or production change is part of this source
change.
