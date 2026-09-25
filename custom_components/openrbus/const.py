"""Constants for the OpenRBus integration."""

from datetime import timedelta

DOMAIN = "openrbus"

BACKEND_NATIVE = "native_bluetooth"
BACKEND_THIN_RPC = "esphome_thin_rpc"
BACKEND_OPTIONS = (BACKEND_NATIVE, BACKEND_THIN_RPC)

CONF_BACKEND = "backend"
CONF_THIN_REQUEST_SERVICE = "thin_rpc_request_service"
CONF_THIN_POLL_SERVICE = "thin_rpc_poll_service"
CONF_THIN_DIAGNOSTICS_SERVICE = "thin_rpc_diagnostics_service"
CONF_THIN_REQUEST_HANDLE = "thin_rpc_request_handle"
CONF_THIN_RESPONSE_HANDLE = "thin_rpc_response_handle"
CONF_THIN_CONTROLLER = "thin_rpc_controller"
CONF_THIN_PROFILE = "thin_rpc_profile"
CONF_THIN_KEY_SECRET = "thin_rpc_key_secret"

CONF_PAIR_ACTION = "pair_action"
CONF_BLE_DEVICE = "ble_device"
CONF_BLE_SOURCE = "ble_source"
CONF_THIN_TARGET_ADDRESS_TYPE = "thin_rpc_target_address_type"
CONF_PASSKEY = "passkey"
CONF_AUTH_KEY = "auth_encryption_key"
CONF_ACCESS_ACK = "access_level_acknowledged"
CONF_WRITE_ENABLED = "write_enabled"
CONF_LANGUAGE = "language"
CONF_POLL_FAST = "poll_interval_fast"
CONF_POLL_STANDARD = "poll_interval_standard"
CONF_POLL_SLOW = "poll_interval_slow"

# Navigation is an action of the flow, not an integration setting.  It is
# intentionally kept separate from the persisted configuration keys and is
# removed as soon as a form is handled.
CONF_FLOW_ACTION = "flow_action"
FLOW_ACTION_NEXT = "next"
FLOW_ACTION_BACK = "back"
FLOW_ACTION_OPTIONS = (FLOW_ACTION_NEXT, FLOW_ACTION_BACK)

# Maximum catalog access level selected by the user.  This is only an entity
# visibility filter; authorization remains owned by openrbus/Thin-RPC.
CONF_ACCESS_LEVEL = "access_level"
ACCESS_LEVEL_OPTIONS = (1, 2, 3)
ACCESS_LEVEL_LABELS = {
    "Benutzer (1)": 1,
    "Installateur (2)": 2,
    "Fachhandwerker (3)": 3,
}
# ``vol.In`` treats mapping keys as submitted values and mapping values as
# display labels.  Keep the numeric values in the flow payload so the REST
# and frontend paths agree with the Core/coordinator contract.
ACCESS_LEVEL_CHOICES = {
    1: "Benutzer (1)",
    2: "Installateur (2)",
    3: "Fachhandwerker (3)",
}
LANGUAGE_OPTIONS = ("de", "en")
DEFAULT_LANGUAGE = "de"
DEFAULT_POLL_INTERVALS = {
    CONF_POLL_FAST: 30,
    CONF_POLL_STANDARD: 120,
    CONF_POLL_SLOW: 600,
}
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)
