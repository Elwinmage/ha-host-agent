from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from custom_components.host_agent.const import DOMAIN
from custom_components.host_agent.ssh import InvalidAuth

# Recent mqtt_mock fixtures leave the MQTT "misc" periodic timer running after the test
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


USER_INPUT = {"host": "192.168.0.20", "port": 22, "username": "pi", "password": "raspberry"}
MQTT_INPUT = {
    "device_name": "Serveur de sauvegarde",
    "mqtt_host": "192.168.0.109",
    "mqtt_port": 1883,
    "mqtt_username": "host-agent-bkup",
    "mqtt_password": "secret",
    "language": "fr",
    "auto_update": True,
}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


def _agent_reports(hass: HomeAssistant, version: str = "0.2.0") -> None:
    async_fire_mqtt_message(hass, "host_agent/bkup/availability", "online")
    async_fire_mqtt_message(
        hass,
        "host_agent/bkup/state/agent_version",
        json.dumps({"state": version, "attrs": {"protocol_version": 1}}),
    )


async def test_full_flow(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "host_key"
    assert result["description_placeholders"]["fingerprint"] == "SHA256:abc"
    # trust on first use: the probe connection is not pinned
    assert fake_remote.ssh_instances[0].pinned is None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "agent"
    schema_defaults = {
        str(k): k.default()
        for k in result["data_schema"].schema
        if hasattr(k, "default") and callable(k.default)
    }
    assert schema_defaults["mqtt_host"] == "mock-broker"  # broker of the HA MQTT entry

    with patch("custom_components.host_agent.config_flow.HEALTH_TIMEOUT", 5):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], MQTT_INPUT)
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        # the agent publishes retained messages; emulate them until the flow subscribed
        for _ in range(20):
            await asyncio.sleep(0.05)
            _agent_reports(hass)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    data = result["data"]
    assert data["node_id"] == "bkup"
    assert data["private_key"] == "PRIVATE"
    assert data["host_key"] == "ssh-ed25519 AAAAhostkey"
    assert data["deployed_version"] == "0.2.0"
    assert "password" not in data  # admin credentials are never stored
    # settings of the former manual installation are kept
    assert result["options"]["collectors"] == {"filesystem": {"required": ["/bkup/dirvish"]}}
    assert result["options"]["auto_update"] is True

    fake_remote.bootstrap.assert_awaited_once()
    install_args = fake_remote.install.await_args.args
    assert "host: 192.168.0.109" in install_args[2]
    assert install_args[3] == "secret"
    # every connection after the probe pins the confirmed key; admin account uses the new key
    admin = [s for s in fake_remote.ssh_instances if s.username == "host-agent-admin"]
    assert admin and all(s.pinned == "ssh-ed25519 AAAAhostkey" and s.private_key == "PRIVATE" for s in admin)


async def test_agent_not_reporting(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.host_agent.config_flow.HEALTH_TIMEOUT", 0.1):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], MQTT_INPUT)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "agent_not_reporting"


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [(InvalidAuth("no"), "invalid_auth")],
)
async def test_user_errors(hass: HomeAssistant, mqtt_mock, fake_remote, side_effect, error) -> None:
    fake_remote.preflight.side_effect = side_effect
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": error}


async def test_auth_required_and_sudo(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, "password": ""})
    assert result["errors"] == {"base": "auth_required"}
    fake_remote.check_sudo.return_value = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "sudo_failed"}


async def test_old_python_aborts(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    from dataclasses import replace

    from conftest import SYSTEM

    fake_remote.preflight.return_value = replace(SYSTEM, python="3.9.2")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "python_too_old"


async def test_already_configured(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="0123456789abcdef").add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth_with_new_host_key(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    from conftest import entry_data

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="0123456789abcdef", data=entry_data(host_key="ssh-ed25519 OLD")
    )
    entry.add_to_hass(hass)
    with patch("custom_components.host_agent.async_setup_entry", AsyncMock(return_value=True)):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "pi", "password": "raspberry"}
        )
        assert result["step_id"] == "reauth_host_key"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["host_key"] == "ssh-ed25519 AAAAhostkey"
    assert entry.data["private_key"] == "PRIVATE"
