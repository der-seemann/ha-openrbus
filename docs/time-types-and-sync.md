# Time type projection and synchronization boundary

Core's registry currently exposes `TIME_OF_DAY` using the six-byte CANopen
representation: milliseconds since midnight plus a 16-bit protocol day counter.
Core returns this as `CanOpenTimeOfDay`. The CiA TIME protocol defines the day
counter as days since 1984-01-01, now exposed by Core as `protocol_date` and by
HA as a `protocol_date` attribute. This standardized calendar date has no
timezone interpretation. HA continues to publish the clock component as a
stable `HH:MM:SS.mmm` text sensor state; neither component is a timestamp.
Primary reference: [CiA TIME protocol](https://www.can-cia.org/can-knowledge/special-function-protocols).

No separate registry `DATE`, `TIME`, or `DATE_TIME` wire type is present.
`581a:00` (`Time update stat RU`) and `504a:00` (`Struct time updates`) are
registry `STRUCT` objects containing a `TIME_OF_DAY` field, not canonical time
set commands. The read-only `3014:00` entry is named `UTC time`, but its type is
still `TIME_OF_DAY`; its standard day epoch is defined, but no object-specific
timezone semantics are provided. A field's label or write
declaration is insufficient evidence for calendar conversion or a write.

The time-related registry rows examined have no `validated` write safety. In
particular, the write declarations for `581a:00`, `504a:00`, and writable-
declared `TIME_OF_DAY` objects remain unverified. There is no automatic or
manual synchronization action. Such a feature requires primary evidence for
the exact set object, complete encoding and meaning (including timezone/DST
behavior), access level, and reversible write validation. Any later sync must
compare against an identified UTC source, skip writes below a configured drift
threshold, and pass through the existing independent read/write and safety
gates.

Before exposing a native timestamp, collect a passive capture aligned with
known device display date/time and timezone/DST state for each candidate
object. Before clock synchronization, obtain the vendor definition of the
exact set command, its family access levels, and the `504a:00` update-type/DLS
semantics. Do not use writes as discovery probes; separately authorize any
reversible validation under the project live-write policy.
