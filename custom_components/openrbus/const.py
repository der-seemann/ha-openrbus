"""Constants for the OpenRBus integration."""

from datetime import timedelta

DOMAIN = "openrbus"

CONF_ENABLE_WRITES = "enable_writes"
CONF_PAIRING_PIN = "pairing_pin"
CONF_RESPONSE_ENTITY = "response_entity"
CONF_REFRESH_ACTION = "refresh_action"

DEFAULT_ENABLE_WRITES = False
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)

TRANSPARENT_SERVICE_UUID = "f8fc98e4-5919-4a5c-852e-dfe04ad383c0"

# Safe identification reads used only to prove the coordinator path. Entity
# selection and device-family discovery will replace this fixed set.
INITIAL_NODE = 0xFF
INITIAL_REGISTERS = ("2001:02",)
