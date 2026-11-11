"""Host Agent: install, configure and update ha-host-agent on Linux hosts."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from . import remote
from .bundle import BundleError, load_bundle
from .const import CONF_NODE_ID, DEFAULT_DISCOVERY_PREFIX, DOMAIN
from .runtime import HostAgentRuntime
from .ssh import SshError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.UPDATE, Platform.BUTTON]

type HostAgentConfigEntry = ConfigEntry[HostAgentRuntime]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_FILE = Path(__file__).parent / "frontend" / "host-agent-card.js"
CARD_URL = f"/{DOMAIN}/frontend/host-agent-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the Lovelace card and load it in every frontend session (no resource to declare)."""
    if hass.http is None or "frontend" not in hass.config.components:
        return True
    if not await hass.async_add_executor_job(CARD_FILE.is_file):
        _LOGGER.warning(
            "Card not found in %s (run 'npm run build' in card/ for a development checkout)", CARD_FILE
        )
        return True
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(CARD_FILE), True)])
    # The version busts the browser cache after each update of the integration
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HostAgentConfigEntry) -> bool:
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT is not available")
    try:
        bundle = await hass.async_add_executor_job(load_bundle)
    except BundleError as err:
        raise ConfigEntryNotReady(str(err)) from err

    runtime = HostAgentRuntime(hass, entry, bundle)
    await runtime.async_start()
    entry.runtime_data = runtime
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HostAgentConfigEntry) -> None:
    # The options flow already pushed the agent configuration; only refresh entities
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HostAgentConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: HostAgentConfigEntry) -> None:
    """Uninstall the agent and remove its MQTT device (best effort)."""
    try:
        bundle = await hass.async_add_executor_job(load_bundle)
        runtime = HostAgentRuntime(hass, entry, bundle)
        async with asyncio.timeout(60):
            await runtime.async_uninstall(purge=True)
    except (BundleError, SshError, HomeAssistantError, OSError, TimeoutError, remote.RemoteError) as err:
        _LOGGER.warning("%s: could not uninstall the agent: %s", entry.title, err)
    try:
        # Empty retained discovery document: HA deletes the MQTT device and its entities
        await mqtt.async_publish(
            hass,
            f"{DEFAULT_DISCOVERY_PREFIX}/device/{entry.data[CONF_NODE_ID]}/config",
            "",
            qos=1,
            retain=True,
        )
    except HomeAssistantError as err:
        _LOGGER.warning("%s: could not remove the MQTT device: %s", entry.title, err)
