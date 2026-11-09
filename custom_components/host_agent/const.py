"""Constants for the Host Agent integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "host_agent"

# MQTT contract version understood by this integration (see agent PROTOCOL_VERSION)
PROTOCOL_VERSION: Final = 1

ADMIN_USER: Final = "host-agent-admin"
HELPER: Final = "/usr/local/lib/host-agent/host-agent-admin"
INCOMING_DIR: Final = "/var/lib/host-agent-admin/incoming"
MIN_PYTHON: Final = (3, 11)

DEFAULT_SSH_PORT: Final = 22
DEFAULT_MQTT_PORT: Final = 1883
DEFAULT_BASE_TOPIC: Final = "host_agent"
DEFAULT_DISCOVERY_PREFIX: Final = "homeassistant"

# Config entry data
CONF_NODE_ID: Final = "node_id"
CONF_MACHINE_ID: Final = "machine_id"
CONF_MAC: Final = "mac"
CONF_HOST_KEY: Final = "host_key"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_SUDO_PASSWORD: Final = "sudo_password"
CONF_MQTT_HOST: Final = "mqtt_host"
CONF_MQTT_PORT: Final = "mqtt_port"
CONF_MQTT_USERNAME: Final = "mqtt_username"
CONF_MQTT_PASSWORD: Final = "mqtt_password"
CONF_DEVICE_NAME: Final = "device_name"
CONF_DEPLOYED_VERSION: Final = "deployed_version"

# Options
CONF_AUTO_UPDATE: Final = "auto_update"
CONF_LANGUAGE: Final = "language"
CONF_COLLECTORS: Final = "collectors"

LANGUAGES: Final = ["fr", "en"]

# Timings (seconds)
SSH_CONNECT_TIMEOUT: Final = 15
COMMAND_TIMEOUT: Final = 180
HEALTH_TIMEOUT: Final = 120
AUTO_UPDATE_DELAY: Final = 120
AUTO_UPDATE_RETRY: Final = 24 * 3600

ISSUE_HOST_KEY: Final = "host_key_changed"
ISSUE_PROTOCOL: Final = "protocol_mismatch"
PROJECT_URL: Final = "https://github.com/Elwinmage/ha-host-agent"
