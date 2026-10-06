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
# Internal, short-lived options-flow marker for a route change.  It is never
# used by Core and deliberately contains no new credential material.
CONF_TRANSPORT_MIGRATION = "_transport_migration"

CONF_PAIR_ACTION = "pair_action"
CONF_BLE_DEVICE = "ble_device"
CONF_BLE_NAME = "ble_device_name"
CONF_BLE_SOURCE = "ble_source"
CONF_THIN_TARGET_ADDRESS_TYPE = "thin_rpc_target_address_type"
CONF_PASSKEY = "passkey"
CONF_AUTH_KEY = "auth_encryption_key"
CONF_ACCESS_ACK = "access_level_acknowledged"
CONF_WRITE_ENABLED = "write_enabled"
CONF_EXPERIMENTAL_WRITES = "experimental_writes"
DEFAULT_EXPERIMENTAL_WRITES = False
CONF_LANGUAGE = "language"
CONF_POLL_FAST = "poll_interval_fast"
CONF_POLL_STANDARD = "poll_interval_standard"
CONF_POLL_SLOW = "poll_interval_slow"
CONF_INVALID_VALUE_DISABLE_AFTER = "invalid_value_disable_after"
# Expert diagnostics are deliberately opt-in.  Keep this independent from
# transport diagnostics: the latter are needed internally for Thin-RPC.
CONF_DIAGNOSTICS_ENABLED = "diagnostics_enabled"
DEFAULT_DIAGNOSTICS_ENABLED = False
# Optional functional filters are independent of manufacturer navigation
# categories and operate from the reviewed exact register map.
CONF_SCREED_DRYING_ENABLED = "screed_drying_enabled"
DEFAULT_SCREED_DRYING_ENABLED = False
CONF_COOLING_ENABLED = "cooling_enabled"
DEFAULT_COOLING_ENABLED = False
# Explicit zone selection is intentionally separate from HA's entity-registry
# disable state: it controls whether a discovered inactive zone is projected
# and polled at all.  Keys are stable ``<node>:<zone-array-subindex>`` values.
CONF_ZONE_OVERRIDES = "zone_overrides"
# Explicit entity enable choices keyed by the stable integration unique ID.
CONF_ENTITY_OVERRIDES = "entity_overrides"
# Scope-level visibility preferences. Entity overrides take precedence over
# group overrides, which take precedence over node overrides.
CONF_NODE_OVERRIDES = "node_overrides"
CONF_GROUP_OVERRIDES = "group_overrides"

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
# ``access_level`` is retained as the read-policy alias for entries created
# before policies were split.  A read grant must never implicitly enable a
# write grant.
CONF_READ_ACCESS_LEVEL = "read_access_level"
CONF_WRITE_ACCESS_LEVEL = "write_access_level"
ACCESS_LEVEL_OPTIONS = (1, 2, 3)
WRITE_ACCESS_LEVEL_OPTIONS = (0, 1, 2, 3)
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
WRITE_ACCESS_LEVEL_CHOICES = {
    0: "Kein Schreibzugriff (0)",
    **ACCESS_LEVEL_CHOICES,
}
LANGUAGE_OPTIONS = (
    "bg",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "et",
    "fi",
    "fr",
    "hr",
    "hu",
    "it",
    "lt",
    "lv",
    "nb",
    "nl",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sr",
    "sv",
    "tr",
    "uk",
    "zh",
)
DEFAULT_LANGUAGE = "de"
DEFAULT_POLL_INTERVALS = {
    CONF_POLL_FAST: 30,
    CONF_POLL_STANDARD: 120,
    CONF_POLL_SLOW: 600,
}
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)
DEFAULT_INVALID_VALUE_DISABLE_AFTER = 48 * 60 * 60
