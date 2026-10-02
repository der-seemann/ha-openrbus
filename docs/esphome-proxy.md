# ESPHome proxy preparation and compatibility

The HA integration can return its bundled generic proxy reference from the
`openrbus.prepare_proxy_yaml` service. The response contains the YAML only; HA
does not write ESPHome files, collect ESPHome credentials, or retain them.
Review the response and save it in the ESPHome configuration directory. The
template's Wi-Fi, Native API encryption, OTA, fallback access point, and BLE
target values use `!secret` references. Obtain `secrets.yaml.example` and the
required C++ headers from the same tagged Core source release; the YAML alone
is not a complete firmware package.

Pin all ESPHome source files to the immutable OpenRBus `v0.4.3` tag and use the
ESPHome version specified by that release's source README. Never use a moving
branch for a deployed proxy. The integration requires RPC schema 3 and pairing
contract `pair_terminal_v3`; missing, older, newer, or mismatched diagnostics
fail setup with an update-required compatibility error. The protocol capability
marker is `openrbus_thin_gatt.v1`.

When the configured ESPHome pairing action is used, setup waits for both the
RPC link and ESPHome BLE client to report disconnected before opening the Thin
session. A successful service response only confirms that ESPHome accepted the
action. `gateway_authenticated` describes gateway authorization on a link, and
the RPC `already_secure_success` terminal describes a later Thin `PAIR_ENCRYPT`
request; neither proves that this pairing action reached its disconnect
boundary. If the boundary does not occur before the bounded timeout, setup
fails and the proxy pairing state must be diagnosed. A terminal diagnostic
left over from an earlier session is not reused as evidence for a new arm.
In the reference Thin-RPC YAML, the legacy pairing-state interval is disabled
when Thin-RPC owns the BLE lifecycle, while `openrbus_pair` rejects a request
unless its legacy state is idle. A stale non-idle state can therefore produce
`failed_connect` and return before requesting disconnect. An accepted ESPHome
API call alone does not establish that the action reached its disconnect
instruction; inspect the current pairing state and build provenance.

For firmware reconciliation, retain the exact ESPHome build inputs, compile
timestamp, and image digest for the deployed image. A source-level
`ble_client.disconnect()` call proves only that a disconnect was requested;
post-action snapshots taken after reconnection cannot prove that the transient
boundary occurred. Do not OTA a candidate unless a known-good rollback image
with verified provenance and a usable recovery path are available. The
2026-09-28 test-proxy investigation found ESPHome 2026.8.2 at compile time
`2026-09-21 13:41:01 +0200`, while retained candidate images were built no
later than 2026-09-20. The live `failed_connect` state is consistent with the
stale-state rejection path above, but exact firmware provenance and rollback
compatibility remain unresolved; no update was performed.

Updates are operator initiated through ESPHome. OpenRBus does not flash, start
an OTA transfer, or change the device configuration. Before installing an
update, keep the current YAML, private secrets, and a known-good image with a
local recovery path. Check the proxy's board, BLE target, ESPHome version, and
release compatibility; compile and review locally, then use ESPHome's normal
OTA path only when the device is reachable and the operator has a recovery
option. Confirm authenticated read-only operation afterward. If it fails,
restore the previous source and image through the local recovery path. Never
put secrets, generated firmware, or installation identifiers in HA service
output, logs, or source control.
