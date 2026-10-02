# HA OpenRBus 0.4.3 release preflight

Status: **PUBLISHED AND VERIFIED.** The
0.4.2 release history remains in `RELEASE_HA_OPENRBUS_0.4.2.md`.

## Scope

- Expose regular writable controls for editable IAE/RXDX parameters and
  compatible bounded zone slots when write access is enabled.
- Add the default-off experimental write option for eligible registers without
  an explicit `IsReadOnly` declaration; the regular write option and access
  authorization remain required.
- Preserve stable identity and registry metadata behavior across sensor/control
  projection. Explicit read-only entries and unresolved wire-type conflicts
  stay blocked.
- Integration version and exact Core dependency are both 0.4.3. ESP proxy
  source and firmware are not changed.

## Validation record

The final HA suite passed 287 tests against the exact freshly built Core 0.4.3
wheel. HA Ruff, compile, JSON and diff checks passed. The isolated Test-HA
candidate passed its 1,814.7-second read-only gate at R3/R3/W3, with 29 stable
active IDs, 26 available states, no new unavailable state, advancing poll
groups, and a healthy proxy. Three states were unavailable at baseline. The
experimental option was restored off, and no register-write API was called.
The exact HA workflow now installs Core from public source commit
`d4bfcc02fe04bd410e669165822d882e562e249f`; Core will be published before
the HA release.

## Publication verification — 2026-10-02

The exact HA tag `v0.4.3` resolves to
`ee8a35b4263e34f0a152149f7780aea226394f47`. Tests run
[37036075913](https://github.com/der-seemann/ha-openrbus/actions/runs/37036075913)
and Validate run
[37036076299](https://github.com/der-seemann/ha-openrbus/actions/runs/37036076299)
passed on that SHA; Validate includes HACS and Hassfest. Core's exact tested
source commit `d4bfcc02fe04bd410e669165822d882e562e249f` was published as
`openrbus==0.4.3` on PyPI before this release. The manifest and HACS metadata
were validated and identify integration version 0.4.3 and minimum Home
Assistant 2026.8.0.

Public HA GitHub Release:
https://github.com/der-seemann/ha-openrbus/releases/tag/v0.4.3. It uses
GitHub's source archive; no binary or firmware asset applies. The release
tree contains no private evidence, local paths, credentials, or raw runtime
exports.
## Final exact-candidate gate update (2026-10-02 16:15 CEST)

The Core `write_declared` fix was built into a fresh 0.4.3 wheel (SHA-256 `d88e6697141abfc1662445de68b4ee516068b37719b41e77c7fccd96595da714`) and HA's full suite passed **286 tests** against it. HA source/style/compile gates previously passed; the candidate component matched 27/27 deployed files during the live check.

In the isolated Test-HA, the exact candidate loaded at R1/W0 with writes off and one entry/process. After setup completed in about 138 seconds, live preflight found only **12/3,702** active entities available (0.32%); fast polling had 63 successes and 18 aborts, standard had 63 aborts and was still running, and slow polling was still running. Proxy readiness and queues were healthy, and no session/correlation errors occurred. This fails the availability gate, so the 30-minute gate was not run and no register write was issued.

Original component/Core bytes, config entry, and registries were restored from a private backup outside `custom_components`; original R3/W3/write-enabled data and empty options are restored and loaded. **Publication remains NO-GO.**

## Final Test-HA validation (2026-10-02)

The corrected candidate was installed into the isolated Test-HA service.
The active PID and command/config path were verified; the loaded HA component
matched source for the tested files, and live diagnostics exposed the new
absent-write projection counter. Core's installed runtime contains the
`write_declared` catalog field.

At the restored R3/R3/W3 configuration (master write enabled, experimental
writes off), the live registry had 7,214 matching rows; 7,196 were
integration-disabled and 18 enabled. The active HA state set contained 29
entities, 26 available. The normal Options Flow added the explicit default
options without changing effective levels, write settings, or override maps.
A minimal Options Flow scan-completion fix prevents repeat Thin-RPC scans; its
regression test is in `tests/test_config_flow.py`.

With experimental writes enabled temporarily, projection added 40 numbers,
13 selects, and 14 switches (67 total); all remained integration-disabled
because those rows were absent from exact runtime capabilities. Experimental
writes were then returned to off. No register-write API was invoked.

A fresh uninterrupted read-only gate passed in 1,814.7 seconds over 120
samples. The 29 enabled state IDs remained unchanged and 26/29 stayed
available; no new entity became unavailable. Fast/standard/slow poll counts
advanced 9→69, 2→17, and 1→4 with zero failed items and zero abort, batch,
correlation, decode, item, or session errors. Transport stayed at epoch 11,
generation 1; recovery-fence counters remained zero. The proxy stayed ready
and connected with empty queues. The three unavailable states were present at
baseline and did not change. Full HA suite: 287 passed (six dependency/runtime
warnings).

Current Test-HA settings are restored to R3/R3/W3, write enabled,
experimental writes off. Payload-free gate output remains in the private
test environment and is not included in this release tree. No register-write
API was called during this release validation, so it does not physically
validate writes. The candidate is ready for final package/publication checks.
