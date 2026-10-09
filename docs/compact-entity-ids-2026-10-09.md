# Compact entity IDs and heating circuit device labels — 2026-10-09

This source candidate gives newly registered OpenRBus entities a stable ID
that identifies the bus node, model, and source-backed register meaning. A
mapped zone row also includes its zone slot and known CP020 designation. For
example, SCB-10 node 4, zone 1, `SCB HK1`, and CP020 becomes
`n4_scb_10_z1_scb_hk1_cp020_heating_circuit_function`.

Unzoned rows use `n<node>_<model>_<code-or-exact-address>_<english-label>`.
Rows without a proven parameter code use the exact object index/subindex. A
CP020 selector receives its known slot in its ID, while its existing parent
device association remains unchanged. Source-mapped zone rows use the reviewed
zone projection map; labels and array position alone do not assign a slot.
Bitfield entities append their source field name, and the two node identity
diagnostics use `device_type` and `parameter_number` suffixes. Clock companion
entity unique IDs are unchanged.

Heating child devices use `<register designation> (<model> (N<node>) - Zone
<slot>)`, such as `SCB HK1 (SCB-10 (N4) - Zone 1)`. Other zone device labels
continue to use the selected language. The device and entity unique IDs are
unchanged. Existing entity-registry IDs, custom names, areas, and disable flags
remain associated through Home Assistant's existing stable unique-ID lookup;
this source change contains no bulk registry rename or upgrade migration.

## Bounded collision audit

The audit used the canonical Core catalog and `catalog_for_node` for five
evidence-backed model families. It covered 2,937 family/address rows, including
320 array heads and 1,977 array elements. The conservative possible platform
sets contained 2,947 sensor rows, 957 number rows, 404 select rows, 179 switch
rows, and 15 time companions. The canonical catalog contains 18 one-bit
structures without direct family association; conservatively applying them
across those five model prefixes contributed 940 field/domain candidates. Two
node identity diagnostics per family were also included. No duplicate full
IDs appeared in any domain, including checks between bitfield names and
whole-register labels. An unknown wider array dimension uses its exact address
rather than Home Assistant's automatic `_2` suffix.

These counts describe source-catalog possibilities, including known rows that
can be disabled by the integration. They do not describe the current private
Test-HA registry or prove which rows a specific device exposes. No runtime
installation, registry mutation, physical read, release, or push was part of
this candidate.

## Validation

The focused `tests/test_entity_names.py` module passed (158 tests). Ruff check,
Ruff format check, and `git diff --check` passed. The Home Assistant 2026.9
object-ID derivation helper confirms that a full preassigned ID avoids a
second device-name prefix, and registry coverage confirms an existing custom
ID remains tied to its unchanged unique ID.
