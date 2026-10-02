# OpenRBus for Home Assistant

OpenRBus is a local Home Assistant integration for compatible BDR-Thermea heating systems. It uses the vendor-independent [`openrbus` protocol core](https://github.com/der-seemann/openrbus) and supports native Bluetooth through Home Assistant and an ESPHome Thin-RPC proxy.

No cloud account is required. The integration is read-only by default. Writing is a deliberate, two-part opt-in and every write is validated by the protocol core and read back before it is reported as successful.

## Installation

### HACS

1. Install [HACS](https://www.hacs.xyz/docs/use/).
2. Open HACS, search for **OpenRBus**, and install the integration.
3. Restart Home Assistant.
4. Add **OpenRBus** from **Settings → Devices & services → Add integration**.

Until the repository is listed in the HACS default catalogue, add `der-seemann/ha-openrbus` as a custom **Integration** repository. A release must be installed rather than a development branch when validating a production setup.

### Manual installation

Download the `custom_components/openrbus` directory from a release and copy it to `<config>/custom_components/openrbus`. Restart Home Assistant and add the integration through the UI. Do not install an editable checkout of the protocol core into a running Home Assistant environment; the integration pins the published `openrbus` wheel in `manifest.json`.

## First setup

The config flow collects only the transport and access settings needed for the selected path. It never contains a device address or credential in this README; use values discovered by your own Home Assistant instance.

### Native Bluetooth

1. Ensure the Home Assistant Bluetooth integration is running and the gateway is paired/trusted in the host Bluetooth manager when the platform requires it.
2. Select **Local Bluetooth**.
3. Select the discovered gateway. If several adapters report the same gateway, the selected Home Assistant scanner source is persisted so reconnects do not silently switch adapters.
4. Choose the access level and complete the credentials step.

### ESPHome Thin-RPC

1. Flash and configure the supported ESPHome proxy firmware from the [`openrbus` repository](https://github.com/der-seemann/openrbus).
2. Confirm that the proxy exposes the complete request, poll, and diagnostics service trio.
3. Select **ESPHome Thin-RPC**, choose the proxy, and select the gateway from the proxy's bounded BLE scan.
4. Complete the access and credential steps. The proxy carries BLE frames; it does not implement OpenRBus policy or write authorization.

If the proxy is unavailable, the flow aborts with a localized error instead of inventing a target or silently using a stale scan result.

For local setup, the `openrbus.prepare_proxy_yaml` service returns the generic
reference YAML without collecting or storing credentials. See
[`docs/esphome-proxy.md`](docs/esphome-proxy.md) for version compatibility and
operator-managed OTA and rollback guidance.

## Access levels and write safety

Access level 1 is the safe default and is intended for read-only operation. Higher levels require the configured gateway credentials and a confirmation warning. Enabling **write access** is separate from selecting a level.

The `openrbus.write_object` service also requires `allow_unsafe: true` for catalog rows whose write safety has not been independently validated. The service verifies the effective device access level, type/range/enumeration constraints, and read-back. A failed verification is reported as an error; the integration never treats a requested value as proof that a write worked.

Use the catalog-projected Number, Select, and Switch entities for normal automation. Readable controls remain pollable even when their write path is blocked. Do not enable expert entities unless their register semantics are understood for the connected device.

## Entities and polling

The integration creates one Home Assistant device per discovered OpenRBus node and projects the protocol catalog into node-scoped entities:

- Sensors expose identity, diagnostics, read-only values, and expert rows.
- Numbers expose validated numeric registers that are safe to represent as a Home Assistant number.
- Selects expose complete enumerations with translated option labels.
- Switches are created only for explicitly boolean enumerations.

The basic set is enabled by default. Less common or noisy catalog rows are present in the entity registry but disabled by default. Unique IDs include the config entry, node, and protocol object address, so multiple nodes do not collide. Polling is grouped into fast, standard, and slow intervals; change those intervals in the Options flow rather than editing YAML.

The integration polls readable rows independently of write permission. Batch responses are correlated by node and object address. Unsupported or partial items use a bounded single-object fallback, so one malformed response does not hide otherwise healthy entities. Reconnects and reloads close the old coordinator before new platform listeners are created.

## Services

The integration registers these response-capable services:

- `openrbus.read_object` — read one object from a node;
- `openrbus.read_group` — read a bounded group of objects;
- `openrbus.catalog` — inspect the projected catalog;
- `openrbus.write_object` — perform an explicitly authorized, verified write.

Service schemas and complete parameter documentation are available in `custom_components/openrbus/services.yaml` and in the Home Assistant service UI. Service calls must select the intended config entry and node; no global or implicit device is chosen.

## History, recorder, and diagnostics

OpenRBus reports state through normal Home Assistant entities. Enable the Home Assistant `recorder` integration to retain history and the `history` integration/dashboard to display it. The integration does not maintain a second database and does not make history visible by itself.

Config-entry diagnostics are intentionally aggregate and secret-free. They omit addresses, service names, object identities, serial numbers, names, credentials, and exception messages. Review a downloaded diagnostics file before sharing it if other custom integrations are included in the same report.

## Privacy and security

All communication is local. Configuration data such as a Bluetooth address, PIN, or protocol key belongs in Home Assistant's protected config storage and must not be copied into issues, screenshots, README files, or test fixtures. Use synthetic values such as `02:00:00:00:00:01` in examples. Debug logs can contain transport metadata; enable debug logging only while reproducing a problem and redact the resulting log before sharing it.

The integration does not upload telemetry. The ESPHome proxy is optional and does not receive OpenRBus register policy. For a security report, use the repository's private GitHub security/contact channel rather than posting credentials or captures in a public issue.

## Troubleshooting

### Native gateway is not listed

Confirm that Bluetooth is enabled, the gateway advertises, and the host has a working HA Bluetooth adapter. If the device is paired to another application, release that connection before retrying. When multiple adapters are present, remove stale Bluetooth entries only through the host Bluetooth manager and repeat discovery.

### Thin-RPC controller is not listed

Check that the ESPHome proxy is online and that all three OpenRBus actions are available. A partial service set is rejected intentionally. Reload the ESPHome integration after changing the proxy configuration.

### Authentication or access-level failure

Verify the credential in Home Assistant's flow and the gateway's configured role. Re-run the Options flow after changing credentials. A configured level is a maximum/request, not proof of the effective level; writes remain blocked until the live gateway reports sufficient access.

### Entities are unavailable

Check the coordinator's last-update status and transport logs. A temporary link loss should recover on the next bounded reconnect. Unsupported object responses are kept unavailable rather than being assigned a guessed value. If an upgrade leaves an old entity projection in the registry, reload the entry; the idempotent registry migration converts obsolete typed array-count rows back to sensors while preserving supported user registry settings.

### Removing the integration

Remove the OpenRBus config entry from **Settings → Devices & services**, then remove the integration from HACS (or delete `custom_components/openrbus` for a manual installation). Home Assistant's config-entry and entity-registry cleanup remains under the user's control; export any diagnostics or recorder data before deleting it if it is needed for troubleshooting.

## Development and release validation

The release candidate is version `0.4.2`. The HA integration and the `openrbus` protocol core are versioned independently but the HA manifest pins the compatible Core release exactly. Run the test suite in a Home Assistant development environment with the pinned dependencies in `requirements-test.txt`:

```console
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m compileall -q custom_components
```

The repository also runs HACS validation and Home Assistant Hassfest in GitHub Actions. See [CHANGELOG.md](CHANGELOG.md) and [RELEASE_HA_OPENRBUS_0.4.2.md](RELEASE_HA_OPENRBUS_0.4.2.md) for the release scope, quality-scale matrix, privacy review, and remaining external release actions.

## License

OpenRBus for Home Assistant is released under the MIT License. See [LICENSE](LICENSE).
