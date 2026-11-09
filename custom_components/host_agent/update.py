"""Update entity: installs the agent version bundled with the integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HostAgentConfigEntry
from .const import CONF_AUTO_UPDATE, PROJECT_URL
from .entity import HostAgentEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: HostAgentConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([AgentUpdate(entry.runtime_data)])


class AgentUpdate(HostAgentEntity, UpdateEntity):
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS | UpdateEntityFeature.RELEASE_NOTES
    )
    _attr_title = "ha-host-agent"
    _attr_release_url = f"{PROJECT_URL}/releases"

    def __init__(self, runtime: Any) -> None:
        super().__init__(runtime, "agent")

    @property
    def installed_version(self) -> str | None:
        return self.runtime.installed_version

    @property
    def latest_version(self) -> str:
        return self.runtime.bundle.version

    @property
    def in_progress(self) -> bool:
        return self.runtime.busy

    @property
    def update_percentage(self) -> int | None:
        return self.runtime.progress if self.runtime.busy else None

    @property
    def auto_update(self) -> bool:
        return bool(self.runtime.entry.options.get(CONF_AUTO_UPDATE))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        agent = self.runtime.agent
        return {"agent_online": agent.online, "protocol_version": agent.protocol}

    async def async_release_notes(self) -> str | None:
        return (
            f"Version **{self.latest_version}** of the agent is bundled with this integration.\n\n"
            "The update uploads it over SSH, restarts the service, checks that the agent reports "
            "back over MQTT and restores the previous version otherwise.\n\n"
            f"[Release notes]({PROJECT_URL}/releases)"
        )

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        await self.runtime.async_install()
