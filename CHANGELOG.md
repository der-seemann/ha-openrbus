# Changelog

## 0.4.2 — release candidate

### Added

- Independent read and write access-level selection, including an explicit
  no-write level and safe read-only projection when writes are unavailable.
- Multi-step node, object-group, and entity selection with persistent
  precedence, plus default-off global cooling and screed-drying filters from
  reviewed exact-register maps.
- Localized diagnostic, screed-drying, group, and access-level labels and
  descriptions.

### Security and safety

- Potential controls require explicit write opt-in, positive effective access,
  and complete Core write evidence. The public catalog currently enables no
  register writes.
- The integration pins the matching `openrbus==0.4.2` Core package.

## 0.4.1 — release candidate

### Added

- Native Bluetooth and ESPHome Thin-RPC setup paths with persisted transport
  source selection.
- Catalog-backed Number, Select, Switch, and Sensor projections with stable
  node/object unique IDs.
- Read polling independent of write permission, bounded batch fallback, and
  reload-safe entity lifecycle handling.
- Secret-free diagnostics, localized setup/options warnings, history/recorder
  documentation, and idempotent typed-entity registry migration.
- Preserve the standardized CANopen `TIME_OF_DAY` date as a diagnostic
  `protocol_date` attribute without assigning a timezone.

### Security and safety

- Access level 1 and read-only operation remain the defaults.
- Elevated access and writes require explicit user confirmation and effective
  live access-level evidence.
- Writes use Core validation and verified read-back; an unverified result is
  never reported as successful.
- Release documentation and diagnostics contain no installation-specific
  addresses, credentials, captures, or database files.

### Compatibility

- Pins `openrbus==0.4.1` in the integration manifest.
- Minimum Home Assistant version for HACS is `2026.8.0`.
