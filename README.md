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

The `openrbus.write_object` service accepts `allow_unsafe: true` only as an explicit confirmation for catalog rows classified as experimental. Regular source-backed rows do not require that confirmation merely because their physical write behavior is unverified. The service still verifies the effective device access level, type/range/enumeration constraints, and read-back. A failed verification is reported as an error; the integration never treats a requested value as proof that a write worked. The service field keeps its existing `allow_unsafe` API key for compatibility.

Use the catalog-projected Number, Select, and Switch entities for normal automation. Readable controls remain pollable even when their write path is blocked. Do not enable expert entities unless their register semantics are understood for the connected device.

## Entities and polling

The integration creates one Home Assistant device per discovered OpenRBus node and projects the protocol catalog into node-scoped entities:

- Sensors expose identity, diagnostics, read-only values, and expert rows.
- Numbers expose validated numeric registers that are safe to represent as a Home Assistant number.
- Selects expose complete enumerations with translated option labels.
- Switches are created only for explicitly boolean enumerations.

The basic set is enabled by default. Less common or noisy catalog rows are present in the entity registry but disabled by default. Writable catalog rows whose exact address is absent from the connected device's discovered capabilities also start disabled, even when comparable rows exist on other models. To activate one that applies to your device, open **Configure entity selection** in the Options flow and enable it for that device. This changes entity selection; the existing write enable and access checks still govern whether a control can write. Regular controls require positive IAE writable evidence for the matching family and object, or a bounded matching family-array inference, plus complete access-level evidence. OBD-only `IsReadOnly=False` declarations are experimental and require both write access and the separate, default-off experimental-write option. Explicit read-only, conflicting, unknown, or incomplete evidence is not enabled by that option; absence of a read-only flag alone does not establish write permission. These source classifications are separate from physical write validation, so a regular control may still have unverified physical safety. Unique IDs include the config entry, node, and protocol object address, so multiple nodes do not collide. Polling is grouped into fast, standard, and slow intervals; change those intervals in the Options flow rather than editing YAML.

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

The current release candidate is version `0.4.6`. The HA integration and the `openrbus` protocol core are versioned independently, and the HA manifest pins the compatible Core release exactly. Run the test suite in a Home Assistant development environment with the pinned dependencies in `requirements-test.txt`:

```console
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m compileall -q custom_components
```

The repository also runs HACS validation and Home Assistant Hassfest in GitHub Actions. See [CHANGELOG.md](CHANGELOG.md) and the version-specific release notes for the release scope, quality-scale matrix, privacy review, and remaining external release actions.

## License

OpenRBus for Home Assistant is released under the MIT License. See [LICENSE](LICENSE).
