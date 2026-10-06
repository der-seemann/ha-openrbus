# HA OpenRBus 0.4.5 release preflight

Status: **candidate prepared; Core publication is first in release order.**
The manifest pins the exact OpenRBus Core 0.4.5 dependency.

## Scope

- Use source-aligned English names for supported catalog entities while
  preserving their stable unique IDs.
- Support the additive Thin-RPC batch-poll service with an eight-frame bound,
  ordered decoding, and fallback to older one-frame firmware.
- Keep reconnect, recovery-fence, cancellation, and transport deadline
  behavior covered by the candidate regression suite.

## Validation and limits

- Full HA tests, lint, formatting, compilation, manifest/JSON checks, and
  `git diff --check` must pass against the exact release source and published
  Core 0.4.5 wheel.
- Isolated Test-HA has 2,877 stable unique IDs and the 21m42 post-batch
  observation met the qualified read-only release-preparation gate. This is
  not evidence of perfect transport continuity or physical write safety.
- New entity-name hints do not automatically rename IDs on existing HA
  installations. Manually customized IDs may require an explicit migration
  when an entry is recreated.
- Release validation did not authorize or exercise heating-system writes.

## Publication order

Complete exact-commit HA Tests, HACS validation, and Hassfest after Core 0.4.5
is published. Create the non-draft, non-prerelease `v0.4.5` GitHub Release
only after all exact-commit workflows pass.
