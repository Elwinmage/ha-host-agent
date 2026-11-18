"""Buttons: restart the agent, reboot the host, wake it up."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HostAgentConfigEntry
from .const import CONF_ALLOW_REBOOT, CONF_WAKE_ON_LAN
from .entity import HostAgentEntity
from .runtime import HostAgentRuntime, wake_supported


async def async_setup_entry(
    hass: HomeAssistant, entry: HostAgentConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    entities: list[ButtonEntity] = [RestartAgentButton(runtime)]
    # Opt-in per host: the entity does not exist unless the option is on
    if entry.options.get(CONF_ALLOW_REBOOT):
        entities.append(RebootHostButton(runtime))
    if entry.options.get(CONF_WAKE_ON_LAN) and wake_supported(dict(entry.data)):
        entities.append(WakeHostButton(runtime))
    async_add_entities(entities)


class RestartAgentButton(HostAgentEntity, ButtonEntity):
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: HostAgentRuntime) -> None:
        super().__init__(runtime, "restart_agent")

    async def async_press(self) -> None:
        await self.runtime.async_restart()


class _CardButton(HostAgentEntity, ButtonEntity):
    """Button the host card finds through its attributes (its entity id is not derived from the node)."""

    _attr_entity_category = EntityCategory.CONFIG
    action: str

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"node_id": self.runtime.node_id, "action": self.action}


class RebootHostButton(_CardButton):
    """Reboots the whole host (not just the agent) through the restricted helper."""

    _attr_device_class = ButtonDeviceClass.RESTART
    action = "reboot"

    def __init__(self, runtime: HostAgentRuntime) -> None:
        super().__init__(runtime, "reboot_host")

    @property
    def available(self) -> bool:
        return self.runtime.agent.online is True

    async def async_press(self) -> None:
        await self.runtime.async_reboot()


class WakeHostButton(_CardButton):
    """Wake-on-LAN: only meaningful while the host is offline."""

    action = "wake"

    def __init__(self, runtime: HostAgentRuntime) -> None:
        super().__init__(runtime, "wake_host")

    @property
    def available(self) -> bool:
        return self.runtime.agent.online is not True

    async def async_press(self) -> None:
        await self.runtime.async_wake()
