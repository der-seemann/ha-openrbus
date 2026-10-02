# HA OpenRBus 0.4.2 release preflight

Status: **LOCAL RELEASE PREFLIGHT PASSED; GO for controlled commit/push and external CI validation.**
The exact-address cooling/screed candidate passed a clean Test-HA preflight
and 1,803.1 seconds of uninterrupted read-only polling on 2026-10-02. Earlier
empty-pollset attempts below are superseded. Do not tag or publish until the
pushed candidate passes GitHub CI, HACS validation, and Hassfest.

## Candidate and safeguards

The candidate provides independent read/write access levels, evidence-gated
Device → Category → Entities selection, universal CP02x–CP029 zone-function
projection, and default-off entry-wide screed-drying and cooling filters based
on exact reviewed address maps. Filter membership is independent of
manufacturer navigation categories and does not read FunctionGroup arrays.
Potential controls remain read-only unless explicit write enable, effective
access, and Core write evidence all pass. Core currently enables no writes.

Entity IDs derive from a SHA-256 prefix of the normalized physical BLE target,
CANopen node, and object index/subindex. The raw BLE target is not emitted in
the IDs or device identifiers; the stable hash remains a linkable pseudonym.
Regression tests verify that a recreated config entry for the same target
reproduces the same IDs, and that in-place registry migration keeps the
existing `entity_id` and `disabled_by: user` choice.

## Local verification

- HA suite against the rebuilt Core 0.4.2 wheel: 266 passed, with six existing
  Home Assistant/dependency warnings.
- HA Ruff check, compilation, JSON parsing, and `git diff --check` passed.
  Ruff format passed for all Paket-2 changed Python files. Eight unchanged
  baseline files still differ from the current formatter output, so a
  repository-wide format check is not claimed.
- Focused recreated-entry ID and registry-migration tests: 2 passed.
- Core and HA metadata, manifest requirement, and CI pin align at 0.4.2.
- Updated exact-address filter candidate: Core suite 166 passed; HA suite 268
  passed. Focused entity/options tests: 78 passed. Ruff, format, compilation,
  and diff whitespace checks passed for changed HA files.
- Core strict mypy, Ruff/format, full suite, build, Twine strict check, source
  publication audit, and wheel/sdist privacy scans passed.

## Isolated Test-HA result

The updated rule accepts isolated per-register aborts, decode errors, and
temporary unavailable entities when polling and transport remain healthy and
the affected items are recorded. Previous attempts stopped only because the
unavailable-ID set changed or per-entry GetList abort counts increased; those
events alone do not fail the revised gate. In the last bounded preflight, six
additional GetList aborts appeared while fast/slow errors and standard
session/decode errors stayed unchanged, all poll groups advanced, and the
proxy was ready. The adapter classifies such aborts separately and retries
the affected register through its single-read path.

For the current exact candidate, the sole Test-HA OpenRBus entry was backed up
with the HA config/device/entity registries and pre-deployment integration.
The rebuilt Core 0.4.2 wheel and candidate integration were installed only in
Test-HA and their installed files were verified byte-identical to the current
candidate (31 Core source files and 29 HA integration files). The isolated
entry was verified at read 1/write 0/writes off in both data and options.
After stabilizing the single Test-HA service and establishing a clean
505.2-second preflight, the exact candidate completed 1,809.6 uninterrupted
seconds over 115 samples. Final availability was 278/297 (93.17%); all three
poll groups advanced, no new unavailable entity appeared, and the only
pre-existing standard decode/quarantine count remained unchanged at 1.
Session/error/fence counters remained stable: HA epoch/generation 8/1 and
recovery attempts/timeouts 0/0. Proxy boot ID 2267225747, host/parent/link
readiness, callback totals and zero queue counts remained stable; no natural
BLE close occurred. The private allowlisted samples recorded ESP close
status/reason and callback counts with each HA epoch/fence observation. No
write service or Core write method was invoked. The entry was restored to
`not_loaded`, `disabled_by=user`, L1/W0/off, and the single Test-HA service
remained healthy. Detailed payload-free evidence remains in local validation
storage outside the release tree.

On 2026-10-02, the filter implementation changed to two reviewed exact-address
maps. Their coverage and excluded ambiguous functions are documented in
`docs/global-register-filters.md`; filters remain entry-wide, default-off, and
are applied before selection and entity creation. HA no longer reads runtime
FunctionGroup arrays for the filters. Current software gates pass: Core 166,
HA 268, focused HA entity/options tests 78, plus Ruff, format, compilation, and
whitespace checks for changed files. The previous live gate predates this
change; the final exact-candidate preflight and 30-minute live gate below now
pass. Do not publish until final cross-project release checks are reviewed.

## Historical supplemental validation

## Typed-array runtime validation — 2026-10-01

The rebuilt Core 0.4.2 wheel and current HA candidate were deployed to the
isolated Test-HA. With the sole entry temporarily enabled at read level 1,
write level 0, writes off, and both optional filters on, the read-only request
to `3096:00` returned CANopen abort `0x06020000` (object does not exist). A
separate bounded read-only probe of `3097:00` returned the same abort. No
array counts or rows were available; the decoder and runtime-key-to-OBD-profile
join therefore could not be verified on this controller. The integration
discarded incomplete metadata and continued using its explicit-catalog
fallback. The entry was restored to `not_loaded`, `disabled_by=user`, L1/W0/off,
and Test-HA returned to one active service process. Temporary instrumentation
and payload-free attempt summaries remain private outside the release tree.

This result concerns the retired FunctionGroup filter experiment. It does not
block the reviewed exact-address map, and it does not show whether other
controller families expose the objects. The earlier live gate is superseded
by the final exact-address gate below. No commit, push, tag, GitHub Release,
HACS submission, or other publication occurred as part of this probe.

## Earlier exact-candidate follow-up — 2026-10-02 (superseded)

The deployed HA candidate matched the worktree byte-for-byte across 27
source/config files; installed Core 0.4.2 Python files also matched all 28
worktree files. The sole entry was temporarily enabled at read level 1, write
level 0, writes off, with both filters off. Proxy host/parent/link readiness
was true and both queues were empty. The map has 69 addresses; 71 historical
registry rows matched those addresses, with zero active mapped entities.

The candidate did not reach a usable polling baseline: five runtime
inventories/devices were reported, but discovery was not recorded as
attempted; all poll groups were empty. A later enable attempt hit a
process-local controller ownership conflict. After one isolated HA service
restart, discovery and nonempty poll sets worked; the cause of the earlier
empty set remains unproven. This attempt is superseded by the final gate below.
Private aggregate evidence remains in local validation storage outside the
release tree.

## Final exact-address live gate — 2026-10-02

The exact HA candidate (27 source/config files) and installed Core 0.4.2
package (28 Python files) matched their worktrees byte-for-byte. One Test-HA
entry was enabled temporarily at read 1/write 0, write access 0, writes off,
and both filters off. Discovery completed for five inventories; poll-selection
diagnostics reported 35 fast, 192 standard, and 32 slow pollable registers.

The clean preflight passed in 190.3 seconds over 13 samples, with 421
successful item reads, all three groups advanced, and zero new error-counter
deltas. Availability was 93.17% (19 of 278 active entities already
unavailable); proxy host/parent/link readiness was true and queues drained.
The subsequent uninterrupted live gate passed at 1,803.1 seconds over 114
polling samples plus baseline/final snapshots (116 private records total).
Pollables remained 35/192/32, with read-success deltas of 1,870 fast,
2,590 standard, and 96 slow. No new unavailable entities, failed items,
error-class deltas, session/fence changes, proxy boot change, readiness loss,
or queue overflow/buildup occurred. The same 278 entities stayed active, with
the same 19 unavailable (93.17% available).

The entry was restored to `not_loaded`, `disabled_by=user`, L1/W0, write access
0, writes disabled, and filters off; both HA API state and stored policy were
verified. No write API or production system was used. Payload-free aggregate
samples and private backups remain in local validation storage outside the
release tree.

## Final package preflight — 2026-10-02

Decision: GO for the next release worker to make the authorized commits and
push the candidate. Hold both tags and all publication until remote CI,
HACS validation, and Hassfest pass on those exact commits.

- The full HA suite passed 270 tests against the freshly built Core 0.4.2
  wheel; Core's full suite passed 166 tests against the same wheel. Six focused
  tests passed for stable entity IDs, registry migration, reviewed filter
  addresses across families, disabled/enabled projection, and saved option
  values.
- Ruff passed for HA source and tests. Ruff format passed for all 20 changed
  Python files; bytecode compilation, five JSON documents, and whitespace
  checks passed. Core's Ruff, full-repository format, strict mypy 2.4.0, and
  compile checks also passed.
- HA privacy audit found only the expected brand icon and test-only address
  placeholders. No private paths, credentials, source captures, raw runtime
  exports, or original vendor files were included. The local validation paths
  were removed from this candidate release note.
- All candidate workflow YAML parsed. Local HACS/Hassfest executables and
  `actionlint` are unavailable; those GitHub Actions must pass after push and
  before tags or publication.
- The deployed live-gate bytes predate only test-fixture updates, HA diagnostic
  formatting, Core FunctionGroup formatting, and removal of a no-op typing
  cast. No transport, filter, identity, polling, or write behavior changed, so
  the 1,803.1-second live gate remains applicable to the candidate behavior.

No commit, push, tag, GitHub Release, or PyPI publication was performed in
this preflight.
