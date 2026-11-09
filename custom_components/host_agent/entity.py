"""Base entity attached to the same device as the agent's MQTT entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.entity import Entity

from .const import CONF_DEVICE_NAME, CONF_MAC, DOMAIN
from .runtime import HostAgentRuntime


class HostAgentEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: HostAgentRuntime, key: str) -> None:
        self.runtime = runtime
        entry = runtime.entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_translation_key = key
        mac = entry.data.get(CONF_MAC)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            # Same MAC connection as the agent's discovery document: one single device
            connections={(CONNECTION_NETWORK_MAC, format_mac(mac))} if mac else set(),
            name=entry.data[CONF_DEVICE_NAME],
            manufacturer="ha-host-agent",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.runtime.add_listener(self.async_write_ha_state))
