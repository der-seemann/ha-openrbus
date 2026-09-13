"""Constants for the OpenRBus integration."""

from datetime import timedelta

DOMAIN = "openrbus"

CONF_RESPONSE_ENTITY = "response_entity"
CONF_GENERATION_ENTITY = "generation_entity"
CONF_REFRESH_ACTION = "refresh_action"
CONF_RAW_READ_ACTION = "raw_read_action"
CONF_DYNAMIC_ENABLE_ACTION = "dynamic_enable_action"
CONF_DYNAMIC_DISABLE_ACTION = "dynamic_disable_action"
CONF_PAIR_ACTION = "pair_action"
CONF_BLE_DEVICE = "ble_device"
CONF_PASSKEY = "passkey"

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)
