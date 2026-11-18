"""Host actions (reboot, Wake-on-LAN) and kernel log rules set from the card."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from conftest import entry_data
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
)

try:
    import probatio as vol  # Home Assistant 2026.10+
except ImportError:
    import voluptuous as vol  # type: ignore[no-redef,unused-ignore]

from custom_components.host_agent import remote
from custom_components.host_agent.const import DOMAIN
from custom_components.host_agent.events import EVENT_PROBLEM
from custom_components.host_agent.wol import magic_packet

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])

REBOOT = "button.serveur_de_sauvegarde_reboot_host"
WAKE = "button.serveur_de_sauvegarde_wake_host"
OPTIONS = {"auto_update": False, "language": "fr", "collectors": {}}


async def setup(hass: HomeAssistant, options: dict | None = None, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="0123456789abcdef",
        title="Serveur de sauvegarde",
        data=entry_data(**data),
        options={**OPTIONS, **(options or {})},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def online(hass: HomeAssistant, up: bool = True) -> None:
    async_fire_mqtt_message(hass, "host_agent/bkup/availability", "online" if up else "offline")


# --------------------------------------------------------------------------- #
# Wake-on-LAN packet
# --------------------------------------------------------------------------- #


def test_magic_packet() -> None:
    packet = magic_packet("DC:a6:32:00-00-01")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("dca632000001") * 16
    with pytest.raises(ValueError):
        magic_packet("dc:a6:32")


# --------------------------------------------------------------------------- #
# Reboot
# --------------------------------------------------------------------------- #


async def test_reboot_button_is_opt_in(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass)
    assert hass.states.get(REBOOT) is None
    assert hass.states.get(WAKE) is None


async def test_reboot_asks_the_helper_and_plans_the_outage(
    hass: HomeAssistant, mqtt_mock, fake_remote
) -> None:
    entry = await setup(hass, {"allow_reboot": True})
    online(hass)
    await hass.async_block_till_done()
    state = hass.states.get(REBOOT)
    assert state.state != "unavailable"
    # found by the card through its attributes
    assert state.attributes["node_id"] == "bkup" and state.attributes["action"] == "reboot"
    runtime = entry.runtime_data
    assert not runtime.reboot_planned

    await hass.services.async_call("button", "press", {"entity_id": REBOOT}, blocking=True)
    fake_remote.reboot.assert_awaited_once()
    assert runtime.reboot_planned

    # the offline and back online transitions of the host are flagged as planned
    online(hass, False)
    await hass.async_block_till_done()
    assert runtime.reboot_planned
    assert hass.states.get(REBOOT).state == "unavailable"  # nothing to reboot while it is down
    online(hass)
    await hass.async_block_till_done()
    assert runtime.reboot_planned  # still planned for the "back online" event
    # then the flag lapses shortly after the host is back
    assert runtime._reboot_deadline is not None
    with patch("custom_components.host_agent.runtime.time.monotonic", return_value=1e9):
        assert not runtime.reboot_planned


async def test_event_carries_the_planned_flag(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass, {"allow_reboot": True})
    discovery = {
        "dev": {"ids": ["host_agent_bkup"], "name": "bkup", "cns": [["mac", "dc:a6:32:00:00:01"]]},
        "o": {"name": "ha-host-agent"},
        "avty_t": "host_agent/bkup/availability",
        "cmps": {
            "problem": {
                "p": "binary_sensor",
                "unique_id": "host_agent_bkup_problem",
                "default_entity_id": "binary_sensor.bkup_problem",
                "name": "Problem",
                "device_class": "problem",
                "state_topic": "host_agent/bkup/state/problem",
                "value_template": "{{ value_json.state }}",
                "payload_on": "ON",
                "payload_off": "OFF",
            }
        },
    }
    async_fire_mqtt_message(hass, "homeassistant/device/bkup/config", json.dumps(discovery))
    await hass.async_block_till_done()
    online(hass)
    async_fire_mqtt_message(hass, "host_agent/bkup/state/problem", json.dumps({"state": "OFF", "attrs": {}}))
    await hass.async_block_till_done()
    events = async_capture_events(hass, EVENT_PROBLEM)

    online(hass, False)  # a real outage
    await hass.async_block_till_done()
    online(hass)
    await hass.async_block_till_done()
    assert [(e.data["transition"], e.data["planned"]) for e in events] == [
        ("offline", False),
        ("online", False),
    ]

    events.clear()
    await hass.services.async_call("button", "press", {"entity_id": REBOOT}, blocking=True)
    online(hass, False)
    await hass.async_block_till_done()
    online(hass)
    await hass.async_block_till_done()
    assert [(e.data["transition"], e.data["planned"]) for e in events] == [
        ("offline", True),
        ("online", True),
    ]
    assert entry.runtime_data.reboot_planned


async def test_outdated_helper_is_explained(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass, {"allow_reboot": True})
    online(hass)
    await hass.async_block_till_done()
    fake_remote.reboot.side_effect = remote.RemoteError("usage: host-agent-admin status|install ...")
    with pytest.raises(HomeAssistantError, match="outdated"):
        await hass.services.async_call("button", "press", {"entity_id": REBOOT}, blocking=True)


# --------------------------------------------------------------------------- #
# Wake-on-LAN button
# --------------------------------------------------------------------------- #


async def test_wake_button(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass, {"wake_on_lan": True, "wol_broadcast": "192.168.0.255"})
    online(hass)
    await hass.async_block_till_done()
    assert hass.states.get(WAKE).state == "unavailable"  # the host is up: nothing to wake
    online(hass, False)
    await hass.async_block_till_done()
    assert hass.states.get(WAKE).state != "unavailable"
    assert hass.states.get(WAKE).attributes["action"] == "wake"
    with patch("custom_components.host_agent.runtime.send_magic_packet") as send:
        await hass.services.async_call("button", "press", {"entity_id": WAKE}, blocking=True)
    send.assert_called_once_with("dc:a6:32:00:00:01", "192.168.0.255")


async def test_no_wake_on_raspberry_pi_or_without_mac(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass, {"wake_on_lan": True}, model="Raspberry Pi 5 Model B Rev 1.0")
    assert hass.states.get(WAKE) is None


async def test_no_wake_without_mac(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass, {"wake_on_lan": True}, mac=None)
    assert hass.states.get(WAKE) is None


# --------------------------------------------------------------------------- #
# Kernel log rules
# --------------------------------------------------------------------------- #


async def test_set_kernel_rule(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)

    async def call(**data):
        await hass.services.async_call(DOMAIN, "set_kernel_rule", {"node_id": "bkup", **data}, blocking=True)

    await call(category="usb", mode="threshold", threshold=4)
    # validated and applied by the agent on the host first
    config = fake_remote.configure.await_args.args[1]
    assert "threshold: 4" in config and "usb:" in config and "kernel:" in config
    assert entry.options["collectors"]["kernel"]["rules"] == {"usb": {"threshold": 4}}

    await call(category="ata", mode="ignore")
    assert entry.options["collectors"]["kernel"]["rules"] == {
        "usb": {"threshold": 4},
        "ata": {"ignore": True},
    }
    await call(category="usb", mode="remove")
    assert entry.options["collectors"]["kernel"]["rules"] == {"ata": {"ignore": True}}
    await call(category="ata", mode="remove")
    # the last rule removed: no empty section left behind
    assert "rules" not in entry.options["collectors"]["kernel"]


async def test_set_kernel_rule_errors(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    with pytest.raises(ServiceValidationError, match="No loaded"):
        await hass.services.async_call(
            DOMAIN, "set_kernel_rule", {"node_id": "nope", "category": "usb", "mode": "ignore"}, blocking=True
        )
    with pytest.raises(ServiceValidationError, match="threshold"):
        await hass.services.async_call(
            DOMAIN,
            "set_kernel_rule",
            {"node_id": "bkup", "category": "usb", "mode": "threshold"},
            blocking=True,
        )
    # critical categories cannot be hidden
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "set_kernel_rule", {"node_id": "bkup", "category": "io", "mode": "ignore"}, blocking=True
        )
    # the agent refuses the configuration: nothing is saved
    fake_remote.configure.side_effect = remote.RemoteError("invalid agent configuration")
    with pytest.raises(HomeAssistantError, match="refused"):
        await hass.services.async_call(
            DOMAIN, "set_kernel_rule", {"node_id": "bkup", "category": "usb", "mode": "ignore"}, blocking=True
        )
    assert entry.options["collectors"] == {}


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


async def test_options_general_and_kernel_rules(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(
        hass, {"collectors": {"kernel": {"rules": {"usb": {"threshold": 4}, "ata": {"ignore": True}}}}}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # the rules created from the card are listed in the menu
    assert "kernel_rules" in result["menu_options"]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "general"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "auto_update": False,
            "language": "fr",
            "allow_reboot": True,
            "wake_on_lan": True,
            "wol_broadcast": "not-an-ip",
        },
    )
    assert result["errors"] == {"wol_broadcast": "invalid_broadcast"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "auto_update": False,
            "language": "fr",
            "allow_reboot": True,
            "wake_on_lan": True,
            "wol_broadcast": "192.168.0.255",
        },
    )
    assert result["type"] == "create_entry"
    assert entry.options["allow_reboot"] is True and entry.options["wol_broadcast"] == "192.168.0.255"
    await hass.async_block_till_done()
    assert hass.states.get(REBOOT) is not None and hass.states.get(WAKE) is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "kernel_rules"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"remove": ["usb"]})
    assert result["type"] == "create_entry"
    assert entry.options["collectors"]["kernel"]["rules"] == {"ata": {"ignore": True}}
