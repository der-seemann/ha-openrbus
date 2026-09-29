# Bitfield entities

OpenRBus Core's packaged registry is the source for packed structure layouts,
bit offsets, and localized field labels. Home Assistant exposes each canonical
one-bit field as a read-only binary sensor with a stable per-node, per-register,
per-field unique ID. Non-boolean fields remain available through their existing
register projection.

The registry's current bitfield write evidence applies only to whole parent
objects and is classified as unverified; it does not establish independent
field write semantics. The integration therefore creates no bitfield switches.
Decoding is read-only, requires the complete registered structure length, and
does not rewrite or normalize unknown bits.

## Primary access audit — 2026-09-28

The canonical Android OBD 1.47 XML/XSD gives each `MetaDataPointStruct` one
object-level `IsReadOnly` attribute. Its structure fields provide names,
descriptions, bit offsets, bit lengths and field types, but no field-level
read/write flag or access level. Among the 43 Core parent addresses below,
OBD declares 41 whole objects `IsReadOnly=False` and two read-only. The 41
declarations are global dictionary metadata; they do not say that a field can
be written independently or identify an applicable device family.

The authorized offline ProfiTool IAE audit covered 20 distinct local captures,
256 definition files and 19,413 definition rows. An IAE definition identifies
an object by `id.name` and optional `id.subIndex`, with `type`, `writable`,
`loaded`, `readLevel` and `writeLevel` on that object row. The definition
shape has no field name, bit offset, bit length or mask. No row matched any of
the 43 structure parents by canonical OBD object name or Core short code, so
the captures provide no family-specific access level, UI type or write flag
for these parents or their fields. No snapshot values were retained.

Core's loaded registry contains 20 structures with 132 one-bit fields across
43 parent addresses. The registry summary counters currently say 13
structures, 117 fields and 33 registers; those counters do not match the
loaded definitions, so this audit uses the actual register and structure
records. Exact parent addresses, grouped by structure:

| Structure | Parent addresses | ProfiTool family evidence |
| --- | --- | --- |
| BatteryStatus | `582a:00` | none in audited IAE definitions |
| BufferConfiguration | `5510:00`, `5511:00` | none |
| ConfigBitfield | `541c:00`, `5422:00`, `550c:00` | none |
| ConsumerManagerStatus | `540c:00`, `5427:00`, `550b:00`, `5513:00` | none |
| DhwStatusBitField | `5619:00` | none |
| DiscoveredDevice | `516e:00` | none |
| DiscoveredProducer | `5171:00` | none |
| DiscoveredZone | `5172:00` | none |
| HeatDrawCommandBitfield | `540b:00`, `5423:00`, `550a:00`, `5512:00` | none |
| InputRegister | `581c:00`, `5828:00`, `5833:00`, `5834:00` | none |
| ObcSpmInputs | `4813:00` | none |
| ObcSpmOutputs | `4814:00` | none |
| OutputRegister | `5829:00`, `5830:00`, `5831:00`, `5832:00` | none |
| PowerSupplySources | `582c:00` | none |
| SafetyUnitStatusBitfield | `4916:03` | none |
| bufferValveCommandBitfield | `5503:00` | none |
| bufferValveStatusBitfield | `5502:00` | none |
| producerManagerStatusBitfield | `5425:00`, `5707:00`, `570d:00`, `570f:00`, `5712:00` | none |
| producerSpecialRequest | `5708:00`, `570e:00`, `5710:00`, `5713:00` | none |
| timeUpdateStruct | `504a:00`, `581a:00` | none |

This evidence does not justify changing the existing per-field binary sensors
into switches. A future switch needs primary, family-specific evidence for an
independent field write and a verified read-modify-write path that preserves
all unrelated and unknown bits.

### Safest next evidence collection

Do not select a field for a live write from the current global OBD metadata:
its writable bit applies to the parent object and does not identify a field
mask or independent field operation. First obtain a manufacturer source for a
specific family's field-level command semantics and access level, plus a
known-good bench/capture fixture that can verify the complete parent value
before and after the operation. Add non-device-writing tests for mask
construction and preservation of every other bit, including unknown bits.
Any live validation would require separate explicit authorization for that
specific object; it is outside the current CP730--CP734-only live-write scope.
