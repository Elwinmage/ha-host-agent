"""Buttons: restart the agent, reboot the host, wake it up, update the host's packages."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HostAgentConfigEntry
from .const import CONF_ALLOW_REBOOT, CONF_ALLOW_UPGRADE, CONF_WAKE_ON_LAN
from .entity import HostAgentEntity
from .runtime import HostAgentRuntime, wake_supported


async def async_setup_entry(
    hass: HomeAssistant, entry: HostAgentConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    # Refreshing the package lists installs nothing: always there (the helper refuses on non-apt hosts)
    entities: list[ButtonEntity] = [RestartAgentButton(runtime), UpdateListsButton(runtime)]
    # Opt-in per host: the entity does not exist unless the option is on
    if entry.options.get(CONF_ALLOW_REBOOT):
        entities.append(RebootHostButton(runtime))
    if entry.options.get(CONF_ALLOW_UPGRADE):
        entities.append(UpgradePackagesButton(runtime))
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


class UpdateListsButton(_CardButton):
    """apt-get update: refreshes the package lists, so the pending count is accurate."""

    action = "apt_update"

    def __init__(self, runtime: HostAgentRuntime) -> None:
        super().__init__(runtime, "update_lists")

    @property
    def available(self) -> bool:
        # Stays available while an upgrade runs: the card reads its progress from the attributes
        return self.runtime.agent.online is True

    async def async_press(self) -> None:
        await self.runtime.async_apt_update()


class UpgradePackagesButton(_CardButton):
    """apt-get upgrade in the background (opt-in); the result is in the attributes and an event."""

    _attr_icon = "mdi:package-up"
    action = "apt_upgrade"

    def __init__(self, runtime: HostAgentRuntime) -> None:
        super().__init__(runtime, "upgrade_packages")

    @property
    def available(self) -> bool:
        # Stays available while an upgrade runs: the card reads its progress from the attributes
        return self.runtime.agent.online is True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        upgrade = self.runtime.upgrade
        return {
            **super().extra_state_attributes,
            "upgrade_state": upgrade.get("state", "idle"),
            "upgrade_started": upgrade.get("started"),
            "upgrade_finished": upgrade.get("finished"),
            "upgrade_exit": upgrade.get("exit"),
            "upgrade_log": upgrade.get("log", []),
        }

    async def async_press(self) -> None:
        await self.runtime.async_apt_upgrade()
