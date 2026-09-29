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
