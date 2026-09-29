"""Secret-free ESPHome proxy preparation and compatibility checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from homeassistant.exceptions import HomeAssistantError

PROXY_SOURCE_VERSION = "0.4.1"
MIN_RPC_SCHEMA_VERSION = 3
SUPPORTED_PAIR_CONTRACT = "pair_terminal_v3"
CAPABILITY_MARKER = "openrbus_thin_gatt.v1"


@dataclass(frozen=True, slots=True)
class ProxyCompatibility:
    """A sanitized compatibility result suitable for user-facing output."""

    compatible: bool
    reason: str


def check_proxy_compatibility(snapshot: Mapping[str, object]) -> ProxyCompatibility:
    """Check the minimum RPC schema and pairing contract fail-closed."""

    schema = snapshot.get("rpc_schema_version")
    contract = snapshot.get("pair_contract")
    if type(schema) is not int:
        return ProxyCompatibility(False, "missing_rpc_schema_version")
    if schema < MIN_RPC_SCHEMA_VERSION:
        return ProxyCompatibility(False, "proxy_update_required")
    if schema != MIN_RPC_SCHEMA_VERSION:
        return ProxyCompatibility(False, "unsupported_rpc_schema_version")
    if contract != SUPPORTED_PAIR_CONTRACT:
        return ProxyCompatibility(False, "unsupported_pair_contract")
    return ProxyCompatibility(True, "compatible")


def read_proxy_yaml() -> str:
    """Return the bundled reference YAML without injecting user secrets."""

    path = Path(__file__).with_name("proxy_templates") / "openrbus-ble-proxy.yaml"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as err:
        raise HomeAssistantError("OpenRBus proxy template is unavailable") from err
    if "!secret" not in content or "wifi_ssid" not in content:
        raise HomeAssistantError("OpenRBus proxy template failed its safety check")
    return content
