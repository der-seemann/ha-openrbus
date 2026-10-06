# HA OpenRBus 0.4.4 release preflight

Status: **candidate validated; Core publication is first in release order.**

## Scope

- Give Heizkreis entities a circuit number and discovered zone name.
- Show BLE names and MAC addresses together in initial and later device
  selections, without carrying a name across a MAC change.
- Add an ESPHome proxy setup link and configuration guide at transport choice.
- Restore saved read/write access levels in setup and options and show them as
  labeled selectors.
- Bound family-array catalogs to type-consistent evidence-backed rows.
- Keep explicitly enabled entities in polling, interpret entity retirement
  thresholds in minutes, and continue zone discovery when an earlier node is
  unavailable.

## Validation

- HA test suite: **315 passed**, 6 dependency/runtime warnings, Python 3.14.
- Ruff check and format check, Python compile, JSON checks, and `git diff
  --check` passed.
- Core 0.4.4 candidate: **171 passed**; Ruff, formatting, strict mypy,
  publication checks, wheel/sdist build, strict Twine checks, and artifact
  privacy audit passed locally. Exact-commit CI must pass before release.
- Isolated Test-HA uses the exact candidate source and Core `0.4.4`. The
  config entry reached `loaded` after one expected deferred-profile reload.
  Node 4 slot 1 was enabled and presented as `Heizkreis 1 — SCB HK1`; its
  temperature, heating, and name entities received live values. Poller
  diagnostics showed 175 fast, 418 standard, and 33 slow selected rows, with
  411 standard and 33 slow reads successful in the sampled cycle.
- Six Node-1 register reads return `unsupported_access`; one MK3 supplier-code
  value is not decoded. They remain unavailable and are not represented as
  successful readings. No register-write API was called during validation.

## Publication order

Publish Core commit `1afb1d553d2b0522c64ffe5cde881beae40d5ed5` as `v0.4.4`,
run the trusted PyPI publisher, and verify a fresh public `openrbus==0.4.4`
installation. Then publish the HA candidate, whose CI installs that exact
Core commit. Both repositories' exact-commit workflows must pass before their
GitHub releases are created.
