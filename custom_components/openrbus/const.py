"""Constants for the OpenRBus integration."""

from datetime import timedelta

DOMAIN = "openrbus"

CONF_RESPONSE_ENTITY = "response_entity"
CONF_GENERATION_ENTITY = "generation_entity"
CONF_REFRESH_ACTION = "refresh_action"

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)
