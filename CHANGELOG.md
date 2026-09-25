# Changelog

## 0.4.0 — release candidate

### Added

- Native Bluetooth and ESPHome Thin-RPC setup paths with persisted transport
  source selection.
- Catalog-backed Number, Select, Switch, and Sensor projections with stable
  node/object unique IDs.
- Read polling independent of write permission, bounded batch fallback, and
  reload-safe entity lifecycle handling.
- Secret-free diagnostics, localized setup/options warnings, history/recorder
  documentation, and idempotent typed-entity registry migration.

### Security and safety

- Access level 1 and read-only operation remain the defaults.
- Elevated access and writes require explicit user confirmation and effective
  live access-level evidence.
- Writes use Core validation and verified read-back; an unverified result is
  never reported as successful.
- Release documentation and diagnostics contain no installation-specific
  addresses, credentials, captures, or database files.

### Compatibility

- Pins `openrbus==0.4.0` in the integration manifest.
- Minimum Home Assistant version for HACS is `2026.8.0`.
