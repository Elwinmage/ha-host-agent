"""host_agent_problem events and the alert blueprint driven by them."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import entry_data
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.host_agent.const import DOMAIN
from custom_components.host_agent.events import EVENT_PROBLEM, owner_node

pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])

BLUEPRINT = Path(__file__).parents[2] / "blueprints/automation/host_agent/host_agent_alert.yaml"
SENSOR = "binary_sensor.bkup_problem"  # host-level: every issue of the host
DISK = "binary_sensor.bkup_disk_sda"

WARN = {"id": "system:memory_high", "code": "memory_high", "severity": "warning", "message": "Memory 95 %"}
CRIT = {"id": "disk_sda:smart", "code": "smart_failed", "severity": "critical", "message": "SMART failed"}
# The host sensor lists the issues of every subject, with the subject's name
HWARN = {**WARN, "subject_name": "System"}
HCRIT = {**CRIT, "subject_name": "Disk sda"}


def problem(key: str, name: str) -> dict:
    return {
        "p": "binary_sensor",
        "unique_id": f"host_agent_bkup_{key}",
        "default_entity_id": f"binary_sensor.bkup_{key}",
        "name": name,
        "device_class": "problem",
        "state_topic": f"host_agent/bkup/state/{key}",
        "value_template": "{{ value_json.state }}",
        "json_attributes_topic": f"host_agent/bkup/state/{key}",
        "json_attributes_template": "{{ value_json.attrs | tojson }}",
        "payload_on": "ON",
        "payload_off": "OFF",
    }


def discover(hass: HomeAssistant, keys: dict[str, str]) -> None:
    components = {key: problem(key, name) for key, name in keys.items()}
    # A plain sensor of the same host must be ignored
    components["system_load"] = {
        "p": "sensor",
        "unique_id": "host_agent_bkup_system_load",
        "default_entity_id": "sensor.bkup_system_load",
        "name": "Load",
        "state_topic": "host_agent/bkup/state/system_load",
        "value_template": "{{ value_json.state }}",
    }
    payload = {
        "dev": {"ids": ["host_agent_bkup"], "name": "bkup", "cns": [["mac", "dc:a6:32:00:00:01"]]},
        "o": {"name": "ha-host-agent"},
        "avty_t": "host_agent/bkup/availability",
        "cmps": components,
    }
    async_fire_mqtt_message(hass, "homeassistant/device/bkup/config", json.dumps(payload))


def publish(hass: HomeAssistant, key: str, state: str, issues: list[dict] | None = None) -> None:
    attrs = {"kind": "host" if key == "problem" else "disk", "issues": issues or []}
    async_fire_mqtt_message(
        hass, f"host_agent/bkup/state/{key}", json.dumps({"state": state, "attrs": attrs})
    )


def online(hass: HomeAssistant, up: bool = True) -> None:
    async_fire_mqtt_message(hass, "host_agent/bkup/availability", "online" if up else "offline")


async def setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="0123456789abcdef",
        title="Serveur de sauvegarde",
        data=entry_data(),
        options={"auto_update": False, "language": "fr", "collectors": {}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def ready(hass: HomeAssistant) -> None:
    """Host discovered after the integration (registry update path), healthy."""
    await setup(hass)
    discover(hass, {"problem": "Problem", "disk_sda": "Disk sda"})
    await hass.async_block_till_done()
    online(hass)
    publish(hass, "problem", "OFF")
    publish(hass, "disk_sda", "OFF")
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "off"


def test_owner_node() -> None:
    nodes = ["bkup", "bkup_2"]
    assert owner_node("host_agent_bkup_system", nodes) == "bkup"
    assert owner_node("host_agent_bkup_2_system", nodes) == "bkup_2"
    assert owner_node("host_agent_tux_system", nodes) is None


async def test_events(hass: HomeAssistant, mqtt_mock, fake_remote) -> None:
    await ready(hass)
    events = async_capture_events(hass, EVENT_PROBLEM)

    publish(hass, "problem", "ON", [WARN])
    await hass.async_block_till_done()
    publish(hass, "problem", "ON", [WARN, CRIT])
    await hass.async_block_till_done()
    publish(hass, "problem", "ON", [WARN, CRIT])  # nothing changed
    await hass.async_block_till_done()
    publish(hass, "problem", "OFF")
    await hass.async_block_till_done()
    online(hass, False)
    await hass.async_block_till_done()
    online(hass)
    await hass.async_block_till_done()

    seen = [(e.data["entity_id"], e.data["transition"]) for e in events]
    assert seen[:3] == [(SENSOR, "problem"), (SENSOR, "new_issue"), (SENSOR, "resolved")]
    # availability changes reach the sensors of a host in no particular order
    assert sorted(seen[3:5]) == [(DISK, "offline"), (SENSOR, "offline")]
    assert sorted(seen[5:]) == [(DISK, "online"), (SENSOR, "online")]
    first = events[0].data
    assert first["host"] == "Serveur de sauvegarde"
    assert first["issues"] == [WARN] and first["previous_issues"] == []
    assert first["state"] == "on" and first["previous_state"] == "off"
    assert first["host_sensor"] is True and first["kind"] == "host"
    assert [e.data["host_sensor"] for e in events if e.data["entity_id"] == DISK] == [False, False]
    assert events[1].data["previous_issues"] == [WARN]


def install_blueprint(config_dir: str) -> None:
    folder = Path(config_dir, "blueprints/automation/host_agent")
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(BLUEPRINT, folder / BLUEPRINT.name)


async def automation(hass: HomeAssistant, **inputs) -> list:
    await hass.async_add_executor_job(install_blueprint, hass.config.config_dir)
    calls = async_mock_service(hass, "test", "alert")
    config = {
        "automation": {
            "alias": "alerts",
            "use_blueprint": {
                "path": "host_agent/host_agent_alert.yaml",
                "input": {
                    "persistent_notification": False,
                    "alert_actions": [
                        {
                            "action": "test.alert",
                            "data": {
                                "title": "{{ title }}",
                                "message": "{{ message }}",
                                "event": "{{ event }}",
                                "severity": "{{ severity }}",
                            },
                        }
                    ],
                    **inputs,
                },
            },
        }
    }
    assert await async_setup_component(hass, "automation", config)
    await hass.async_block_till_done()
    assert hass.states.get("automation.alerts").state == "on"
    return calls


def advance(hass: HomeAssistant, minutes: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))


async def settle(hass: HomeAssistant) -> None:
    """Process pending messages while an automation run waits for its delay.

    async_block_till_done would wait for that run, and time is frozen.
    """
    for _ in range(50):
        await asyncio.sleep(0)


async def done(hass: HomeAssistant) -> None:
    """Wait for runs that do not wait for a delay.

    Older Home Assistant versions run parallel automations as background tasks,
    which async_block_till_done does not wait for.
    """
    await hass.async_block_till_done()
    await settle(hass)


async def test_blueprint_watches_every_host(hass: HomeAssistant, mqtt_mock, fake_remote, freezer) -> None:
    await ready(hass)
    calls = await automation(hass)

    # Confirmed problem: alert after the 5 min delay, with the issues of that moment
    publish(hass, "problem", "ON", [HWARN])
    publish(hass, "disk_sda", "ON", [CRIT])  # already in the host alert: not watched
    await settle(hass)
    freezer.tick(timedelta(minutes=2))
    publish(hass, "problem", "ON", [HWARN, HCRIT])
    await settle(hass)
    assert calls == []  # new issue before confirmation: included in the problem alert
    freezer.tick(timedelta(minutes=4))
    advance(hass, 0)
    await done(hass)
    assert len(calls) == 1
    alert = calls[0].data
    assert alert["event"] == "problem" and alert["severity"] == "critical"
    assert alert["title"] == "🔴 Serveur de sauvegarde : Anomalie"
    assert alert["message"] == "• System : Memory 95 %\n• Disk sda : SMART failed"

    # Back to normal
    publish(hass, "problem", "OFF")
    publish(hass, "disk_sda", "OFF")
    await done(hass)
    assert [c.data["event"] for c in calls] == ["problem", "resolved"]
    assert calls[-1].data["title"] == "🟢 Serveur de sauvegarde : Retour à la normale"

    # A short problem is neither alerted nor reported resolved
    publish(hass, "problem", "ON", [HCRIT])
    await settle(hass)
    freezer.tick(timedelta(minutes=1))
    publish(hass, "problem", "OFF")
    await settle(hass)
    freezer.tick(timedelta(minutes=5))
    advance(hass, 0)
    await done(hass)
    assert len(calls) == 2

    # Agent offline for 30 min, then back: one alert per host, not per sensor
    online(hass, False)
    await settle(hass)
    freezer.tick(timedelta(minutes=31))
    advance(hass, 0)
    await done(hass)
    assert [c.data["event"] for c in calls[2:]] == ["offline"]
    online(hass)
    await done(hass)
    assert [c.data["event"] for c in calls[3:]] == ["online"]


async def test_blueprint_selection_and_severity(hass: HomeAssistant, mqtt_mock, fake_remote, freezer) -> None:
    await ready(hass)
    calls = await automation(hass, problem_entities=[DISK], min_severity="critical")

    publish(hass, "problem", "ON", [HCRIT])  # not selected
    publish(hass, "disk_sda", "ON", [WARN])  # below the minimum severity
    await settle(hass)
    freezer.tick(timedelta(minutes=6))
    advance(hass, 0)
    await done(hass)
    assert calls == []

    publish(hass, "disk_sda", "ON", [WARN, CRIT])
    await done(hass)
    assert [c.data["event"] for c in calls] == ["new_issue"]
    assert calls[0].data["message"] == "• bkup Disk sda : SMART failed"

    # Brief outage: no offline alert
    online(hass, False)
    await settle(hass)
    freezer.tick(timedelta(minutes=10))
    online(hass)
    await settle(hass)
    freezer.tick(timedelta(minutes=30))
    advance(hass, 0)
    await done(hass)
    assert [c.data["event"] for c in calls] == ["new_issue"]
