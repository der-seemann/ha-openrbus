# OpenRBus for Home Assistant

Early HACS custom-integration scaffold for communicating with compatible
BDR-Thermea heating systems through [OpenRBus](https://github.com/der-seemann/openrbus).

## Current status

This repository is pre-release scaffolding, not a production-ready integration.
It currently provides:

- discovery through Home Assistant's central Bluetooth integration;
- selection of connectable gateways advertised by local adapters or ESPHome
  Bluetooth proxies;
- pairing-PIN collection for the future pairing/authentication step;
- a read-only-by-default access-policy option with an explicit write warning;
- a `DataUpdateCoordinator` skeleton that connects through Home Assistant's
  selected BLE route and calls `OpenRBusClient.read_many()` for a small set of
  identification objects.

No entities are exposed yet. Pairing-PIN handling is not wired into the
OpenRBus transport yet: OpenRBus 0.3.0 expects the gateway to be paired already.
The stored PIN is never logged, but this scaffold does not currently consume it.

Enabling writes only opens the integration-level client gate. OpenRBus still
applies its independent access-level, device-evidence, unsafe-write, value,
rate-limit, and read-back checks. No write service or entity exists in this
scaffold.

## Architecture

The Python protocol core runs on the Home Assistant host. ESPHome devices are
passive `bluetooth_proxy` range extenders; they contain no OpenRBus framing,
codec, discovery, authorization, or write logic.

The integration uses the BLE device selected by Home Assistant and connects
through `bleak-retry-connector`, allowing Home Assistant to choose a local
adapter or an ESPHome proxy.

## Installation

The manifest is intentionally pinned to the final dependency declaration:

```json
"requirements": ["openrbus==0.3.0"]
```

OpenRBus 0.3.0 must be available on PyPI before ordinary HACS installation can
complete. TestPyPI URLs and local paths are not committed to the manifest.

For local development before the PyPI release, install the sibling checkout in
the same Home Assistant development environment:

```console
python -m pip install -e ../openrbus
```

Then copy or link `custom_components/openrbus` into the Home Assistant config
directory and restart Home Assistant.

## Planned next steps

1. Complete and test pairing/authentication using the entered PIN.
2. Discover bus nodes and their device families.
3. Turn coordinator data into diagnostic and register entities.
4. Add explicit write controls only after end-to-end safety tests.
5. Add config-flow, coordinator, and unload/reload tests against Home Assistant.
