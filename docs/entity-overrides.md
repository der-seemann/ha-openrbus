# Device, category, and entity selection

The Home Assistant Options Flow presents three steps: **Device → Category → Entities**. The device step includes every discovered node and displays its runtime model, family, or discovered name; no fixed product-name allowlist controls the picker. A category appears only when catalog or runtime configuration evidence assigns at least one entity to it. Rows without category evidence use the neutral **Nicht klassifiziert / Unclassified** bucket. The final step selects individual entities and bitfields.

Membership is conservative. Active CP02x configuration evidence can assign configured heating functions to Zone and configured domestic-hot-water functions to Trinkwarmwasser. DHW remains its own category; DHW values do not create heating circuits. Disabled, unread, or unknown configuration slots remain inactive by default. Other categories are accepted only from structured category metadata supplied by Core. Register names, model names, neighboring object rows, and static Original-App category labels do not establish runtime category membership. Cooling, screed drying, diagnostics, access, readability, and write safety gates remain independent of picker category.

An explicitly enabled Home Assistant registry row for a non-recommended,
readable sensor or binary sensor opts that exact read-only projection into
polling. It does not bypass current read authorization, optional-category or
zone filters, explicit user disables, or the exclusion for unobserved declared
writes. Typed controls do not use this manual-registry opt-in path.

Choices persist at three scopes: entity unique IDs, `device:<node>:category:<category>` keys stored in `group_overrides`, and node keys. Entity choice takes precedence over current category, legacy General category, legacy `node:<node>:zone/object:<...>` group choice, node choice, then the automatic default. Legacy node and group keys remain stored and effective for compatibility.

Entity IDs use one deterministic rule across platforms: a SHA-256 prefix of the configured BLE target identifier, CANopen node, and object index/subindex. Conventional MAC addresses retain their existing canonical byte hash; opaque adapter identifiers such as UUIDs are hashed as normalized UTF-8 text. The raw identifier is not exposed in entity IDs. Different gateways therefore receive distinct IDs, while reinstalling the integration against the same target and bus identities recreates the same IDs regardless of Home Assistant's random config-entry ID. Entries without a configured BLE target fail setup rather than silently use an installation-specific service name.

At setup, legacy entry-scoped entity unique IDs and device identifiers are migrated in place. Home Assistant retains each entity's `entity_id`, so recorder history and entity customizations remain attached; registry `disabled_by: user` is preserved. Legacy per-entity picker overrides are translated in the coordinator's runtime view. The regression suite covers identity repeatability across recreated entries and in-place registry migration with a custom entity ID and user disable.

Read access remains independently configured as level 1 Benutzer, level 2 Installateur, or level 3 Fachhandwerker. Write access remains subject to the existing independent access-level, explicit-enable, and Core evidence gates; the picker cannot enable a write.

Source-supported and experimental writable catalog rows that are absent from
the node's exact runtime capability list remain available in this picker, but
default to disabled so a comparable parameter is not polled as though it were
installed. A concrete discovered address follows the normal default policy.
The guard uses Core's base `write_declared` metadata, which is independent of
whether HA currently projects the row as a sensor or an enabled write control.
The entity registry applies the same default during setup while preserving
`disabled_by: user` and explicit saved picker selections. Poll selection also
enforces the absent-row default before consulting legacy registry projections,
so an old enabled sensor/control cannot activate an absent inferred write. Use
the Options Flow entity picker to opt into such a row. A catalog array head
does not prove that a concrete subindex exists.

## Global optional filters

Estrichtrocknung and Kühlbetrieb are default-off, entry-wide filters. A
manually reviewed exact-address map classifies each register from its OBD
function and Core's localized catalogue label. The existing device catalog
still decides whether a register is available on a specific node; the filter
does not assign manufacturer navigation categories or invent device names.
Filtering runs before picker choices, entity construction, and poll-group
assembly. Turning a filter back on restores its normal projection. Existing
entity registry rows and user disable choices are retained.

The implementation is in `optional_register_filters.py`; the evidence,
positive register lists, functional boundaries, and exclusions are recorded in
`global-register-filters.md`. Runtime FunctionGroup discovery is not used for
these filters because the tested controller does not expose the candidate
arrays and these are not manufacturer FunctionGroup categories.
