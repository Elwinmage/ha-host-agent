from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from conftest import entry_data
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.host_agent.const import DOMAIN

UPDATE = "update.serveur_de_sauvegarde_agent"


def report(hass: HomeAssistant, version: str, protocol: int = 1, online: bool = True) -> None:
    async_fire_mqtt_message(hass, "host_agent/bkup/availability", "online" if online else "offline")
    async_fire_mqtt_message(
        hass,
        "host_agent/bkup/state/agent_version",
        json.dumps({"state": version, "attrs": {"protocol_version": protocol}}),
    )


async def setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="0123456789abcdef",
        title="Serveur de sauvegarde",
        data=entry_data(),
        options=options or {"auto_update": False, "language": "fr", "collectors": {}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_update_entity_and_install(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    report(hass, "0.1.2")
    await hass.async_block_till_done()
    state = hass.states.get(UPDATE)
    assert state.state == "on"
    assert state.attributes["installed_version"] == "0.1.2"
    assert state.attributes["latest_version"] == "0.2.0"

    async def agent_restarts(*_args, **_kw):
        # the new agent reports a moment after the helper returned
        hass.loop.call_later(0.05, report, hass, "0.2.0")

    fake_remote.install.side_effect = agent_restarts
    await hass.services.async_call("update", "install", {"entity_id": UPDATE}, blocking=True)
    await hass.async_block_till_done()

    state = hass.states.get(UPDATE)
    assert state.state == "off"
    assert state.attributes["installed_version"] == "0.2.0"
    assert entry.data["deployed_version"] == "0.2.0"
    fake_remote.rollback.assert_not_awaited()
    config = fake_remote.install.await_args.args[2]
    assert "language: fr" in config and "node_id: bkup" in config


async def test_install_rolls_back_when_agent_silent(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    report(hass, "0.1.2")
    await hass.async_block_till_done()
    with (
        patch("custom_components.host_agent.runtime.HEALTH_TIMEOUT", 0.1),
        pytest.raises(HomeAssistantError, match="previous version restored"),
    ):
        await hass.services.async_call("update", "install", {"entity_id": UPDATE}, blocking=True)
    fake_remote.rollback.assert_awaited_once()
    assert entry.data["deployed_version"] == "0.1.2"


async def test_single_device_shared_with_mqtt(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass)
    registry = dr.async_get(hass)
    device = registry.async_get_device(connections={(dr.CONNECTION_NETWORK_MAC, "dc:a6:32:00:00:01")})
    assert device is not None
    assert (DOMAIN, "0123456789abcdef") in device.identifiers


async def test_protocol_mismatch_issue(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    report(hass, "0.1.2", protocol=2)
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"protocol_mismatch_{entry.entry_id}")
    report(hass, "0.2.0", protocol=1)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, f"protocol_mismatch_{entry.entry_id}") is None


async def test_host_key_mismatch_starts_reauth(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    from custom_components.host_agent.ssh import HostKeyMismatch

    entry = await setup(hass)
    fake_remote.restart.side_effect = HostKeyMismatch()
    with pytest.raises(HomeAssistantError, match="host key changed"):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.serveur_de_sauvegarde_restart_agent"}, blocking=True
        )
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"host_key_changed_{entry.entry_id}")
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_auto_update(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass, {"auto_update": True, "language": "fr", "collectors": {}})
    report(hass, "0.1.2")
    await hass.async_block_till_done()

    async def agent_restarts(*_args, **_kw):
        hass.loop.call_later(0.01, report, hass, "0.2.0")

    fake_remote.install.side_effect = agent_restarts
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=130))
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    fake_remote.install.assert_awaited_once()
    assert hass.states.get(UPDATE).attributes["installed_version"] == "0.2.0"


async def test_options_flow_pushes_configuration(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"auto_update": True, "language": "fr", "collectors": {"bogus": {}}},
    )
    assert result["errors"] == {"collectors": "invalid_collectors"}

    from custom_components.host_agent.remote import RemoteError

    fake_remote.configure.side_effect = RemoteError("invalid agent configuration")
    collectors = {"filesystem": {"required": ["/bkup/dirvish"]}}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"auto_update": True, "language": "fr", "collectors": collectors}
    )
    assert result["errors"] == {"base": "configure_failed"}

    fake_remote.configure.side_effect = None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"auto_update": True, "language": "fr", "collectors": collectors}
    )
    assert result["type"] == "create_entry"
    assert "/bkup/dirvish" in fake_remote.configure.await_args.args[1]
    await hass.async_block_till_done()
    assert entry.options["collectors"] == collectors


async def test_remove_entry_uninstalls(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    fake_remote.uninstall.assert_awaited_once()
    mqtt_mock.async_publish.assert_any_call("homeassistant/device/bkup/config", "", 1, True)
