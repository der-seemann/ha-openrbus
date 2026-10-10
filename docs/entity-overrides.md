# Device, category, and entity selection

The Home Assistant Options Flow overview separates **Connection**, **Access**,
**Features**, and **Polling and display**. Submissions stay in a flow-local
draft until **Save changes** is selected; closing the flow discards the draft.
Saving retains the existing config-entry identity. Home Assistant documents
[`OptionsFlowWithReload`](https://developers.home-assistant.io/docs/core/integration/options_flow/)
to schedule one reload after changed options are stored;
unchanged options do not schedule another reload. The integration does not
register an update listener, so it cannot trigger a second blocking reload.

Backend controller and gateway selections, when needed, also use native menu
actions around their forms. The entity picker uses native Home Assistant menu
actions around its forms:
device selection, category bulk selection, then entity selection. The category
menu has direct actions to edit a category, continue searching the current
category, return to devices, or finish back to the Options overview. Search
state and selections remain in the flow draft until the options are saved.
Home Assistant's [data-entry flow API](https://developers.home-assistant.io/docs/data_entry_flow_index/)
documents menus and forms but does not define form-level
Back/Next button fields; the flow does not add a direction selector to the
picker forms.

The device step includes every discovered node and displays its runtime model,
family, or discovered name; no fixed product-name allowlist controls the
picker. A category appears only when catalog or runtime configuration
evidence assigns at least one entity to it. Rows without category evidence use
the neutral **Nicht klassifiziert / Unclassified** bucket. The final step
selects individual entities and bitfields.

Membership is conservative. Active CP02x configuration evidence can assign configured heating functions to Zone and configured domestic-hot-water functions to Trinkwarmwasser. DHW remains its own category; DHW values do not create heating circuits. Disabled, unread, or unknown configuration slots remain inactive by default. Other categories are accepted only from structured category metadata supplied by Core. Register names, model names, neighboring object rows, and static Original-App category labels do not establish runtime category membership. Cooling, screed drying, diagnostics, access, readability, and write safety gates remain independent of picker category.

An explicitly enabled Home Assistant registry row for a non-recommended,
readable sensor or binary sensor opts that exact read-only projection into
polling. It does not bypass current read authorization, optional-category or
zone filters, or explicit user disables. This read-only opt-in does not enable
the write API; typed controls do not use this manual-registry opt-in path.

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

Estrichtrocknung, Kühlbetrieb, and Kaskadenfunktionen are default-off,
entry-wide filters. Manually reviewed exact-address maps classify rows using
their OBD operational meaning and Core catalog definition. The existing
device catalog still decides whether a register is available on a specific
node; the filters do not assign manufacturer navigation categories or invent
device names. Filtering runs before picker choices, entity construction, and
poll-group assembly. Turning a filter back on restores the normal projection
with the same unique IDs. Existing entity registry rows are retained;
integration-disabled rows return when all applicable filters are enabled, and
`disabled_by: user` remains authoritative. A row with overlapping Cooling and
Cascade membership requires both preferences.

The implementation is in `optional_register_filters.py`; the evidence,
positive register lists, functional boundaries, and exclusions are recorded in
`global-register-filters.md`. Runtime FunctionGroup discovery is not used for
these filters because the tested controller does not expose the candidate
arrays and these are not manufacturer FunctionGroup categories.

## CP020 function-specific zone objects and child names

CP020 specialization is a separate exact-address gate in `zones.py`. The
current Core `ZoneFunction` enum identifies mixing circuits as `2`, swimming
pools as `3`, process heat as `9` or `14`, and DHW functions as `6`, `7`, `10`,
`11`, `12`, `13`, or `31`. Exact zone-slot objects with DHW, swimming-pool, or
process-heat semantics are gated by those memberships; shared heating values
remain available. Unknown CP020 reads do not expose specialized objects. This
gate is shared by picker eligibility, entity defaults, and poll selection.
The reviewed DHW-primary block has source-backed objects at `363d`–`3661` and
`3665`–`3670`; `3662`–`3664` are not in the source map and are deliberately
not inferred from the surrounding addresses. Existing filtered registry rows
are retained and integration-disabled, preserving their IDs and any user
disable decision.

Active zone child devices use only their configured register designation as
the HA device name, falling back to `Zone N`. Node model, node number, and
one-based zone number remain in the supported `DeviceInfo.model` field;
existing zone identifiers and `via_device` ownership are unchanged.
