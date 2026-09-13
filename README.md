# OpenRBus for Home Assistant

Early HACS custom-integration scaffold for communicating with compatible
BDR-Thermea heating systems through [OpenRBus](https://github.com/der-seemann/openrbus).

## Current status

This repository is pre-release scaffolding, not a production-ready integration.
The verified Phase-2 transport path currently provides:

- discovery through Home Assistant's central Bluetooth integration;
- selection of connectable gateways advertised by local adapters or ESPHome
  Bluetooth proxies;
- pairing-PIN collection for the future pairing/authentication step;
- a read-only-by-default access-policy option with an explicit write warning;
- a `DataUpdateCoordinator` for the ESPHome OpenRBus proxy;
- a read-only `openrbus.read_object` expert service for arbitrary registry
  addresses, serialized through the same coordinator lock;
- secret-free diagnostics and explicit unsupported-object handling.

No entities are exposed yet. Pairing-PIN handling is not wired into the
OpenRBus transport yet: OpenRBus 0.3.0 expects the gateway to be paired already.
The stored PIN is never logged, but this scaffold does not currently consume it.

Enabling writes only opens the integration-level client gate. OpenRBus still
applies its independent access-level, device-evidence, unsafe-write, value,
rate-limit, and read-back checks. No write service or entity exists in this
scaffold.

## Architecture

The Python protocol core runs on the Home Assistant host. The verified runtime
architecture is `HA -> ESPHome Native API -> OpenRBusTransport -> EHC`.
ESPHome transports raw frames only; registry lookup, discovery, decoding,
scaling and entity semantics remain in Python. The standalone ESP-IDF PoC is
kept separate and is not required for this integration.

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

1. Run bounded bus-directory discovery against a real non-gateway node.
2. Confirm registry family matches and add only positively verified entities.
3. Live-validate native CAN-IP `GET_LIST`; otherwise use sequential batches.
4. Add profile-controlled read-only entities and rediscovery options.
5. Add explicit write controls only after a separate safety phase.
