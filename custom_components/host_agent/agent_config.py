"""Build the agent configuration file from the config entry."""

from __future__ import annotations

from typing import Any

import yaml

from .const import (
    CONF_COLLECTORS,
    CONF_DEVICE_NAME,
    CONF_LANGUAGE,
    CONF_MQTT_HOST,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_NODE_ID,
    DEFAULT_BASE_TOPIC,
    DEFAULT_DISCOVERY_PREFIX,
)

ALLOWED_COLLECTORS = {
    "smart",
    "filesystem",
    "dirvish",
    "jobs",
    "system",
    "gpu",
    "services",
    "storage",
    "updates",
    "kernel",
}
HEADER = (
    "# Managed by the Home Assistant host_agent integration: local changes are overwritten.\n"
    "# Edit the integration options instead.\n"
)


class CollectorsError(ValueError):
    """Invalid collectors section."""


def validate_collectors(value: Any) -> dict[str, Any]:
    """Shallow validation; the agent validates the full schema before applying it."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise CollectorsError("must be a mapping")
    unknown = set(value) - ALLOWED_COLLECTORS
    if unknown:
        raise CollectorsError(f"unknown collector(s): {', '.join(sorted(unknown))}")
    for name, section in value.items():
        if section is not None and not isinstance(section, dict):
            raise CollectorsError(f"'{name}' must be a mapping")
    return {k: v or {} for k, v in value.items()}


def import_collectors(config_text: str | None) -> dict[str, Any]:
    """Collectors section of an existing (manual) agent configuration."""
    if not config_text:
        return {}
    try:
        data = yaml.safe_load(config_text) or {}
        return validate_collectors(data.get("collectors"))
    except (yaml.YAMLError, CollectorsError, AttributeError):
        return {}


def import_language(config_text: str | None, default: str) -> str:
    try:
        data = yaml.safe_load(config_text or "") or {}
    except yaml.YAMLError:
        return default
    lang = data.get("language") if isinstance(data, dict) else None
    return lang if lang in ("fr", "en") else default


def build_config(data: dict[str, Any], options: dict[str, Any]) -> str:
    config: dict[str, Any] = {
        "node_id": data[CONF_NODE_ID],
        "name": data[CONF_DEVICE_NAME],
        "language": options.get(CONF_LANGUAGE, "en"),
        "state_file": "/var/lib/host-agent/state.json",
        "mqtt": {
            "host": data[CONF_MQTT_HOST],
            "port": data[CONF_MQTT_PORT],
            "password_file": "/etc/host-agent/mqtt.secret",
            "discovery_prefix": DEFAULT_DISCOVERY_PREFIX,
            "base_topic": DEFAULT_BASE_TOPIC,
        },
        "collectors": options.get(CONF_COLLECTORS) or {},
    }
    if data.get(CONF_MQTT_USERNAME):
        config["mqtt"]["username"] = data[CONF_MQTT_USERNAME]
    return HEADER + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
