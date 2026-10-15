# Optional feature opt-ins — 2026-10-09

This source-only candidate adds a default-off cascade-functions preference to
the HA config and options flows. Existing exact Cooling and screed filters
remain default-off. `docs/global-register-filters.md` records the reviewed
address maps, Core 0.4.7 meanings, OBD 1.47 evidence boundary, array rules,
overlaps, and exclusions.

Cascade has 202 exact mapped catalog heads. A row with more than one optional
membership is visible only when every corresponding preference is enabled;
Cooling/Cascade overlap is `370a:00`, `370e:00`, `384b:00`–`384e:00`, and
`5724:00`. Existing Core-projected array members inherit a map only after
exact-head, array, bound, code, datatype, storage, and type-conflict checks.
The map never creates rows. Turning a filter off removes rows from picker,
entity projection, and polling while integration-owned registry disables can
be reversed on enable; user disables remain intact. Stable entity unique IDs
are retained.

The focused verification passed: `tests/test_optional_register_filters.py`
and `tests/test_config_flow.py` (69 passed), plus the cascade lifecycle/picker
selection from `tests/test_entity_defaults.py` (2 passed). Ruff lint and format
checks, translation JSON parsing, and `git diff --check` passed. No full suite
or live HA gate was run; Test-HA, production, physical registers, OTA, release,
and push were untouched.

Candidate base: `a8800a30fd5750c1940160fbdb7dcf9f7f0441cb`. Supervisor review
approved the source and documentation before this local commit.
