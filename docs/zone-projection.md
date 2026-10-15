# Zone projection

OpenRBus reads the manufacturer Zone Function (CP020, object `3404:x`) after
normal node discovery.  This read-only evidence decides whether a zone-array
slot is inactive, a heating circuit, DHW, or another configured function.
The optional manufacturer Zone Friendly Name (`340f:x`) becomes the display
name when available.

Inactive or unreadable slots are disabled by default and are not included in
the polling set.  The Options flow exposes discovered slots as persistent
manual selections; an explicit selection can retain a normally inactive slot.
This is separate from HA's regular per-entity registry enable/disable state.

Active zone-array entities belong to a logical child device under their bus
node.  The child identifier is `entry:node:<node>:zone:<subindex>`.  Entity
unique IDs remain `entry:node:<node>:object:<index>:<subindex>`, so changing a
friendly zone name or a function never changes history, dashboards, or
automations.

Solar is not a value represented by the current manufacturer `ZoneFunction`
enum.  Global solar objects stay in the parent device's general projection;
adding a solar child device requires Core to expose a public `ZoneDiscovered`
function-group record or equivalent runtime evidence.

For a mixer heating circuit (CP020 value 2), the exact source-backed
anti-Legionella register set is excluded from the entity picker, projection,
and poll selection. It includes program setpoints and timing (`3428:00`,
`3429:00`, `342a:00`, `3456:00`, `345e:00`, `348f:00`–`3491:00`, `365d:00`,
`3680:00`, `3681:00`, `3686:00`, `3687:00`, `36aa:00`, `36ab:00`) and
per-zone status/timestamps (`5450:00`–`5452:00`, `561e:00`, `561f:00`). These
objects are valid only for documented DHW functions. Shared system-level DHW
objects are not included in this per-zone map. The address-to-source-name
evidence is in [`zone-object-map-audit.md`](zone-object-map-audit.md); the
runtime gate is an exact address map, not a label-fragment rule.
