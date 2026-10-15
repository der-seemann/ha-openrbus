# Register display decoration handoff — 2026-10-09

The HA read-only sensor path now renders a Core-labelled enumeration value as
its localized label. It only does so when the exact Core definition agrees
with the row's enum type, storage, code, and unit; the definition has no type
conflict; and an array element is within `1..max_items`. Array count objects,
out-of-range values, unknown enum values, booleans, and existing text remain
unchanged. The underlying integer remains available as `raw_numeric_value`.
Text enum states are unitless and have no measurement device/state class.

The reported appliance status and substatus are `500f:00` (`AM012`,
`DeviceState`) and `5011:00` (`AM014`, `DeviceSubStatus`). The Core German enum
tables provide labels for all 27 and 90 declared values. HA previously exposed
the decoded integer unchanged in `OpenRBusRegisterSensor.native_value`, even
though the control path already had localized enum labels. No Core change is
needed.

`3811:00` (`EP046`) is a CANopen array-count object. Its source definition is
an `InputDigitalConfiguration` enum array with ten items. The readable label
“Auswahl der Funktion des Digitaleingangs” applies only to supported array
elements `3811:01` through `3811:0a`; it does not rename the count object or
invent individual input names. The precise code, current Core definition,
source label, data type, storage, enum, and maximum are checked before the
override applies.

`2001:0d` is the `UINT16` device configuration table version; its readable
German name is “Version der Gerätekonfigurationstabelle.” `4356:00` is the
`UINT8` outdoor-unit protection-functions value; its readable name is
“Schutzfunktionen der Außeneinheit der Wärmepumpe.” The manufacturer describes
active protections and their effect on the outdoor unit, but does not define
an enum, structure, or bit positions for this byte. It remains a numeric raw
value; no bitmask interpretation is inferred. Decoding this AE state remains
an evidence backlog until an exact value/bit decoder is sourced. Likewise,
`439e:00` (outdoor
unit substatus) and `4327:00` (heat-pump PCU substatus) are raw U8 definitions,
distinct from appliance substatus `5011:00`.

The bounded census found 629 manufacturer enum metadata points, each joined
to a Core enum definition. Of 204 Core enum definitions, 194 have every label
in German alone; with Core's English fallback, all referenced enums are
complete. The only definition still incomplete across both locales is the
unused `Unit` enum. A separate name census found 53 status-, substatus-, or
protection-named raw numeric catalog entries without enum/structure wire
decoration. These remain candidates, not proven decoders; no bulk conversion
was applied.

Evidence: manufacturer OBD 1.47 register and German/English translation
metadata; normalized Core registry and locale catalog. Source payloads are
not included. Scoped HA tests cover known enum labels, unknown numeric values,
existing text, booleans, array count and bounds, type/storage/unit mismatch,
and catalog type conflicts. No runtime install, physical write, or release was
performed.
