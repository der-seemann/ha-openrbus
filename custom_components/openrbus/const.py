"""Constants for the OpenRBus integration."""

from datetime import timedelta

DOMAIN = "openrbus"

CONF_ENABLE_WRITES = "enable_writes"
CONF_PAIRING_PIN = "pairing_pin"

DEFAULT_ENABLE_WRITES = False
DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)

TRANSPARENT_SERVICE_UUID = "f8fc98e4-5919-4a5c-852e-dfe04ad383c0"

# BDR Thermea manufacturer identifier observed on supported gateways. Some
# gateways do not advertise the transparent service UUID, so discovery must
# not rely on the UUID alone.
BDR_THERMEA_MANUFACTURER_ID = 0x4442

# Safe identification reads used only to prove the coordinator path. Entity
# selection and device-family discovery will replace this fixed set.
INITIAL_NODE = 0x01
INITIAL_REGISTERS = ("2001:02", "2001:05", "3042:00")
