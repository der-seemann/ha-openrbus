# HA OpenRBus 0.4.1 release review

Status: release candidate prepared; no commit, tag, GitHub release, HACS
submission, or PyPI publication was performed by this review.

## Verdict

The HA repository is a `0.4.1` release candidate. Its current dirty tree passes
the isolated HA suite against a locally built Core `0.4.1` wheel. The package
has not been published; HACS/Hassfest, the exact ESPHome source compile, live
transport acceptance, and a clean-candidate rerun remain open.

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
| Diagnostics | Aggregate-only output; public Thin-RPC diagnostics enforce an allowlist even if an internal caller stores raw proxy fields. No addresses, names, object identities, serials, service names, credentials, or exception messages are exposed. |
| Documentation | README, changelog, Core provenance, installation, troubleshooting, privacy, and history guidance updated. |

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
| Valid manifest, documentation, issue tracker, code owner | Ready | Version and Core requirement are `0.4.1`; Hassfest still runs in CI. |
| Brand asset | Ready | `brand/icon.png` and SVG source are present. |
| GitHub description, topics, issues enabled | External | Verify repository settings before submission. |
| Full GitHub Release, not tag only | External | Create `v0.4.1` after CI passes. |
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

- HA pytest suite: 215 passed, 6 dependency/runtime warnings, 0 failures in an
  isolated Python 3.14.4 environment with Home Assistant 2026.9.1, pytest
  9.1.1, pytest-asyncio 1.4.0, Ruff 0.16.6, and matching Core 0.4.1 source
  supplied through `PYTHONPATH`. This includes bounded stale-pairing reset/rearm
  and Thin-RPC recovery-fence regressions. This environment is separate from the
  running Test-HA instance.
- Python bytecode compilation: passed for `custom_components/openrbus` and
  `tests`.
- JSON parsing: passed for the manifest, HACS metadata, strings, and all
  translations.
- `git diff --check`: passed.
- Ruff check, pytest, and bytecode compilation: passed locally; CI installs the
  published Core wheel after Core 0.4.1 is available.
- HACS Action and Hassfest: configured in GitHub Actions; run on the release
  commit before tagging.
- Live transport and write acceptance: not included in this check. Do not infer
  release readiness from the software suite.

## External release sequence

1. Review and release `openrbus` Core `0.4.1` to PyPI.
2. Review and release the ESPHome proxy firmware/source at its coordinated
   version, with no installation-specific captures or configuration.
3. Run HA tests against the published Core wheel, then run HACS Action and
   Hassfest on the clean HA release commit.
4. Commit the reviewed HA tree, tag `v0.4.1`, and create a full GitHub Release.
5. Verify HACS can install the GitHub release as a custom repository.
6. Submit the owner-authored PR to `hacs/default/integration`.
