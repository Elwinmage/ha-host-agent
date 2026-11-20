"""Host actions (reboot, Wake-on-LAN, package updates) and kernel log rules set from the card."""

from __future__ import annotations

import asyncio
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
UPDATE_LISTS = "button.serveur_de_sauvegarde_update_package_lists"
UPGRADE = "button.serveur_de_sauvegarde_upgrade_packages"
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


# --------------------------------------------------------------------------- #
# Package updates
# --------------------------------------------------------------------------- #


def assert_refresh_asked(mqtt_mock) -> None:
    # Newer Home Assistant versions add keyword arguments (message_expiry_interval): compare the positional ones
    assert any(
        call.args[:4] == ("host_agent/bkup/cmd/refresh", "PRESS", 1, False)
        for call in mqtt_mock.async_publish.call_args_list
    )


async def wait_for(hass: HomeAssistant, predicate, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
    raise AssertionError("condition not reached")


async def test_update_lists_is_always_there_and_asks_for_a_refresh(
    hass: HomeAssistant, mqtt_mock, fake_remote
) -> None:
    await setup(hass)
    online(hass)
    await hass.async_block_till_done()
    state = hass.states.get(UPDATE_LISTS)
    assert state.attributes["node_id"] == "bkup" and state.attributes["action"] == "apt_update"
    # the upgrade itself is opt-in
    assert hass.states.get(UPGRADE) is None

    await hass.services.async_call("button", "press", {"entity_id": UPDATE_LISTS}, blocking=True)
    fake_remote.apt_update.assert_awaited_once()
    # the agent is asked to recount the pending packages
    assert_refresh_asked(mqtt_mock)


async def test_update_lists_error_is_reported(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await setup(hass)
    online(hass)
    await hass.async_block_till_done()
    fake_remote.apt_update.side_effect = remote.RemoteError("apt is not available on this host")
    with pytest.raises(HomeAssistantError, match="apt is not available"):
        await hass.services.async_call("button", "press", {"entity_id": UPDATE_LISTS}, blocking=True)
    fake_remote.apt_update.side_effect = remote.RemoteError("usage: host-agent-admin status|install")
    with pytest.raises(HomeAssistantError, match="outdated"):
        await hass.services.async_call("button", "press", {"entity_id": UPDATE_LISTS}, blocking=True)
    # the user is offered the re-authentication that reinstalls the helper
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_upgrade_runs_in_the_background_and_reports(
    hass: HomeAssistant, mqtt_mock, fake_remote, monkeypatch
) -> None:
    monkeypatch.setattr("custom_components.host_agent.runtime.UPGRADE_POLL", 0.01)
    entry = await setup(hass, {"allow_upgrade": True})
    online(hass)
    await hass.async_block_till_done()
    events = async_capture_events(hass, "host_agent_upgrade")
    fake_remote.upgrade_status.side_effect = [
        {"state": "running", "started": "100"},
        {"state": "ok", "started": "100", "finished": "160", "exit": "0"},
    ]
    fake_remote.upgrade_log.return_value = "== apt-get upgrade\n0 upgraded\n"

    assert hass.states.get(UPGRADE).attributes["upgrade_state"] == "idle"
    await hass.services.async_call("button", "press", {"entity_id": UPGRADE}, blocking=True)
    fake_remote.apt_upgrade.assert_awaited_once()
    state = hass.states.get(UPGRADE)
    assert state.attributes["upgrade_state"] == "running"
    # nothing else can be started meanwhile
    with pytest.raises(HomeAssistantError, match="already running"):
        await hass.services.async_call("button", "press", {"entity_id": UPGRADE}, blocking=True)
    with pytest.raises(HomeAssistantError, match="upgrade is running"):
        await hass.services.async_call("button", "press", {"entity_id": UPDATE_LISTS}, blocking=True)

    await wait_for(hass, lambda: entry.runtime_data.upgrade["state"] == "ok")
    state = hass.states.get(UPGRADE)
    assert state.attributes["upgrade_state"] == "ok"
    assert state.attributes["upgrade_exit"] == "0"
    assert state.attributes["upgrade_log"] == ["== apt-get upgrade", "0 upgraded"]
    await wait_for(hass, lambda: bool(events))
    assert events[0].data["result"] == "ok" and events[0].data["node_id"] == "bkup"
    assert_refresh_asked(mqtt_mock)


async def test_failed_upgrade_fires_the_event(
    hass: HomeAssistant, mqtt_mock, fake_remote, monkeypatch
) -> None:
    monkeypatch.setattr("custom_components.host_agent.runtime.UPGRADE_POLL", 0.01)
    entry = await setup(hass, {"allow_upgrade": True})
    online(hass)
    await hass.async_block_till_done()
    events = async_capture_events(hass, "host_agent_upgrade")
    # an unreachable host (reboot, sshd restart) does not end the follow-up
    fake_remote.upgrade_status.side_effect = [
        None,
        {"state": "failed", "exit": "100"},
    ]
    from custom_components.host_agent.ssh import SshError

    fake_remote.upgrade_status.side_effect = [
        SshError("down"),
        {"state": "failed", "exit": "100"},
    ]
    fake_remote.upgrade_log.return_value = "E: broken\n"
    await hass.services.async_call("button", "press", {"entity_id": UPGRADE}, blocking=True)
    await wait_for(hass, lambda: entry.runtime_data.upgrade["state"] == "failed")
    await wait_for(hass, lambda: bool(events))
    assert events[0].data["result"] == "failed" and events[0].data["exit_code"] == "100"


async def test_upgrade_refused_while_a_backup_runs(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass, {"allow_upgrade": True})
    online(hass)
    await hass.async_block_till_done()
    fake_remote.apt_upgrade.side_effect = remote.RemoteError("a dirvish backup is running, upgrade refused")
    with pytest.raises(HomeAssistantError, match="backup is running"):
        await hass.services.async_call("button", "press", {"entity_id": UPGRADE}, blocking=True)
    assert entry.runtime_data.upgrade["state"] == "idle"


async def test_upgrade_needs_the_option(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    with pytest.raises(HomeAssistantError, match="not enabled"):
        await entry.runtime_data.async_apt_upgrade()
    fake_remote.apt_upgrade.assert_not_awaited()


async def test_upgrade_in_progress_is_recovered_after_a_restart(
    hass: HomeAssistant, mqtt_mock, fake_remote, monkeypatch
) -> None:
    monkeypatch.setattr("custom_components.host_agent.runtime.UPGRADE_POLL", 0.01)
    monkeypatch.setattr("custom_components.host_agent.runtime.UPGRADE_RECOVER_DELAY", 0.01)
    fake_remote.upgrade_status.side_effect = [
        {"state": "running", "started": "100"},
        {"state": "ok", "started": "100", "finished": "160", "exit": "0"},
    ]
    entry = await setup(hass, {"allow_upgrade": True})
    await wait_for(hass, lambda: entry.runtime_data.upgrade["state"] == "ok")


async def test_upgrade_option_in_the_options_flow(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    entry = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "general"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"auto_update": False, "language": "fr", "allow_reboot": False, "allow_upgrade": True},
    )
    assert result["type"] == "create_entry"
    assert entry.options["allow_upgrade"] is True
    await hass.async_block_till_done()
    assert hass.states.get(UPGRADE) is not None
