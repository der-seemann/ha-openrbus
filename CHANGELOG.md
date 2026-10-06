# Changelog

## 0.4.4 — release candidate

### Added

- Include the discovered heating-circuit number in visible entity names and
  use available zone labels to distinguish circuits.
- Show the BLE advertisement name and MAC address together throughout setup
  and options, and retain a device name only for its matching MAC.
- Add an ESPHome Bluetooth-proxy setup link and instructions at transport
  selection, including a reusable OpenRBus proxy YAML service.
- Display saved read and write access levels as labeled numeric sliders in the
  setup and options flows.

### Fixed

- Keep initial polling and deferred zone discovery in config-entry-owned
  background tasks so they do not hold up integration startup.
- Make reload wait for in-flight poll/session cleanup and recover from a
  transient bounded Thin-RPC read failure without reusing an old session.
- Include persisted entity/group enables in poll selection, so explicitly
  enabled non-recommended sensors receive state updates.
- Interpret the invalid-value retirement option in its documented minutes,
  preventing entities from being disabled after only that many seconds.
- Recover heating-circuit discovery that completes after startup so eligible
  zone entities re-enter polling, later devices are reached when one node is
  unavailable, and unknown profiles do not disable entities.

### Compatibility and limits

- The HA manifest pins the exact OpenRBus Core 0.4.4 dependency.
- Unsupported per-device objects remain unavailable. On the isolated Test-HA,
  six Node-1 reads returned `unsupported_access`; the MK3 `2001:01` supplier
  code remains unavailable because the Core catalog has a visible-string vs.
  octet-string type conflict and the runtime bytes are not valid ASCII.

## 0.4.3 — release

### Added

- Project IAE/RXDX writable declarations and compatible bounded zone slots as regular controls when write access is enabled. A separate default-off experimental option exposes otherwise eligible registers without an explicit read-only declaration.
- Persist the experimental option across setup and options flows; writes still require configured and effective access authorization.
- Default catalog writable rows that are absent from the device's discovered capabilities to disabled. Use **Configure entity selection** in the Options flow to enable a comparable row only when it applies to the connected device.

### Safety

- Explicit read-only declarations, unresolved device wire-type conflicts, ambiguous access levels, and invalid values remain blocked.
- HA and its exact Core dependency are versioned together at 0.4.3.

## 0.4.2 — release

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
  and complete Core write evidence. The published 0.4.2 catalog enabled no
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
