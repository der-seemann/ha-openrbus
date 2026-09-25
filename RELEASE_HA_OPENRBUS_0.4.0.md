# HA OpenRBus 0.4.0 release review

Status: release candidate prepared; no commit, tag, GitHub release, HACS
submission, or PyPI publication was performed by this review.

## Verdict

The HA repository is structurally ready for a `0.4.0` release after the
companion `openrbus` Core `0.4.0` package and ESPHome proxy release have passed
their independent reviews. The local HA suite is green. Publication remains a
coordinated external action because the manifest pins the Core package exactly.

## Review scope

The review covered config flow and Options defaults, native Bluetooth adapter
source persistence, ESPHome Thin-RPC service discovery and lifecycle, Core
access-level/write gates, coordinator batching and polling, registry-backed
Number/Select/Switch/Sensor projection, reload/unload behavior, diagnostics,
translations, history/recorder assumptions, packaging, privacy, and HACS
metadata.

### Findings and fixes

| Area | Result |
| --- | --- |
| Options access-level default | Current persisted level is used and normalized before rendering. |
| Native multi-adapter selection | The HA scanner source is persisted and passed explicitly; no silent default-adapter fallback. |
| Thin-RPC setup | Complete request/poll/diagnostics capability is required; target selection comes from a bounded scan. |
| Writes | Disabled by default; effective access, catalog constraints, Core safety, explicit confirmation, and read-back are required. |
| Polling | Readability is independent of write permission; malformed/partial batches use bounded single-object fallback. |
| Entity lifecycle | Platforms unload before coordinator shutdown; registry projection migration is idempotent and reload-safe. |
| Diagnostics | Aggregate-only output; no addresses, names, object identities, serials, service names, credentials, or exception messages. |
| Documentation | README, changelog, Core provenance, installation, troubleshooting, privacy, and history guidance updated. |

The observed Home Assistant test topology contained 1,799 OpenRBus entities.
That is a node- and catalog-dependent validation result, not a universal
entity-count promise for other installations.

## Quality-scale matrix

The integration targets Home Assistant Bronze and implements the applicable
Bronze fundamentals: UI config flow, unique config entry, connection checks
before setup, config-flow test coverage, runtime data, entity unique IDs,
appropriate local polling, common modules, action setup, dependency
transparency, and installation/action documentation. It additionally provides
Silver/Gold-oriented features including unloading, credential validation,
devices, diagnostics, unavailable entities, disabled expert entities,
discovery, and translated entity names where supported by the catalog.

The declared manifest value is conservatively `quality_scale: bronze`; this
does not claim Home Assistant Core inclusion or certification. Final quality
scale assessment is an external maintainer decision.

## HACS Default matrix

Current official guidance: [HACS default repositories](https://www.hacs.xyz/docs/publish/include/),
[HACS GitHub Action](https://www.hacs.xyz/docs/publish/action/), and
[HACS general requirements](https://www.hacs.xyz/docs/publish/start/).

| Requirement | Status | Evidence / remaining action |
| --- | --- | --- |
| Public GitHub repository | External | Repository must remain public and active. |
| Root README | Ready | Installation, configuration, safety, troubleshooting, removal, and privacy are documented. |
| Root `hacs.json` with `name` | Ready | Includes a supported minimum Home Assistant version. |
| Integration under `custom_components/<domain>` | Ready | `custom_components/openrbus` is the release payload. |
| Valid manifest, documentation, issue tracker, code owner | Ready | Version and Core requirement are `0.4.0`; Hassfest still runs in CI. |
| Brand asset | Ready | `brand/icon.png` and SVG source are present. |
| GitHub description, topics, issues enabled | External | Verify repository settings before submission. |
| Full GitHub Release, not tag only | External | Create `v0.4.0` after CI passes. |
| HACS Action | Ready in CI | `.github/workflows/validate.yml` runs the pinned HACS action. |
| Hassfest | Ready in CI | The same workflow runs official Hassfest validation. |
| HACS default PR | External | Owner/major contributor submits an alphabetical PR to `hacs/default/integration` after release. |
| Stars or popularity threshold | Not required by current docs | Do not claim a star requirement; current HACS inclusion guidance lists owner/contributor and validation checks instead. |

HACS default inclusion is not automatic after publishing. The owner must open
the default-repository PR only after the release and both actions pass.

## Privacy and release-tree audit

The release tree was checked for private MAC/Bluetooth addresses, IP addresses,
hostnames, entry IDs, serials, PINs, keys, tokens, recorder databases, live
captures, absolute user paths, and plant-specific names. Internal live-evidence
documents were removed or replaced by generic build/provenance guidance.
Fixtures use synthetic documentation values only. Git history may still contain
older development artifacts; rewriting public history is intentionally outside
this preparation and must not be done without an explicit repository-owner
decision.

## Tests and checks

- HA pytest suite: 146 passed, 6 dependency deprecation warnings, 0 failures.
- Python bytecode compilation: passed.
- JSON parsing of manifest, HACS metadata, strings, and translations: required
  as a release gate.
- Pytest, Ruff, and bytecode compilation: configured in GitHub Actions against
  the published Core wheel.
- HACS Action and Hassfest: configured in GitHub Actions; run on the release
  commit before tagging.
- Live hardware: only a read-only status check is allowed for final release
  acceptance. No write, backend switch, pairing reset, or credential change is
  part of this release preparation.

## External release sequence

1. Review and release `openrbus` Core `0.4.0` to PyPI.
2. Review and release the ESPHome proxy firmware/source at its coordinated
   version, with no installation-specific captures or configuration.
3. Run HA tests against the published Core wheel, then run HACS Action and
   Hassfest on the clean HA release commit.
4. Commit the reviewed HA tree, tag `v0.4.0`, and create a full GitHub Release.
5. Verify HACS can install the GitHub release as a custom repository.
6. Submit the owner-authored PR to `hacs/default/integration`.
