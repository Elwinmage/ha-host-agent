"""Restart button."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HostAgentConfigEntry
from .entity import HostAgentEntity
from .runtime import HostAgentRuntime


async def async_setup_entry(
    hass: HomeAssistant, entry: HostAgentConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([RestartAgentButton(entry.runtime_data)])


class RestartAgentButton(HostAgentEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: HostAgentRuntime) -> None:
        super().__init__(runtime, "restart_agent")

    async def async_press(self) -> None:
        await self.runtime.async_restart()
