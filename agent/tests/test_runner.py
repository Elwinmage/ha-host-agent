from __future__ import annotations

import json
from datetime import UTC, datetime

from host_agent.collectors.base import Collector
from host_agent.i18n import Translator
from host_agent.model import CollectorResult, EntitySpec, Issue, Reading, problem_reading
from host_agent.mqtt.discovery import DeviceInfo, Topics
from host_agent.runner import Runner
from host_agent.state import StateStore


class FakeCollector(Collector):
    name = "fake"

    def __init__(self):
        super().__init__(Translator("en"))
        self.disks = {"disk_a": []}
        self.fail = False

    @property
    def interval(self):
        return 60

    def collect(self):
        if self.fail:
            raise RuntimeError("boom")
        result = CollectorResult()
        for key, issues in self.disks.items():
            common = {"subject": key, "subject_label": key, "persistent": True}
            result.entities += [
                EntitySpec(
                    key, "binary_sensor", f"Disk {key}", device_class="problem", role="problem", **common
                ),
                EntitySpec(f"{key}_temperature", "sensor", f"Temp {key}", unit="°C", **common),
            ]
            result.readings += [problem_reading(key, issues), Reading(f"{key}_temperature", 40)]
            result.issues += issues
        result.entities.append(EntitySpec("fs_tmp", "sensor", "Tmp"))  # non persistent
        result.readings.append(Reading("fs_tmp", 1))
        return result


class FakeTransport:
    connected = True

    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, retain=True, qos=1):
        self.messages.append((topic, payload, retain))

    def last(self, topic):
        for t, p, _ in reversed(self.messages):
            if t == topic:
                return json.loads(p) if p else p
        return None


def make_runner(tmp_path, collector):
    clock = [0.0]
    runner = Runner(
        collectors=[collector],
        topics=Topics("homeassistant", "host_agent", "bkup"),
        device=DeviceInfo("bkup", "Backup", model="Raspberry Pi", mac="aa:bb:cc:dd:ee:ff"),
        translate=Translator("en"),
        store=StateStore(str(tmp_path / "state.json")),
        clock=lambda: clock[0],
        now=lambda: datetime(2026, 11, 9, 7, 0, tzinfo=UTC),
    )
    return runner, clock


DISCOVERY = "homeassistant/device/bkup/config"


def test_discovery_and_states(tmp_path):
    collector = FakeCollector()
    collector.disks["disk_a"] = [Issue("disk_a", "smart_failed", "critical", "SMART failed")]
    runner, _ = make_runner(tmp_path, collector)
    transport = FakeTransport()
    runner.transport = transport
    runner.step()

    disc = transport.last(DISCOVERY)
    assert disc["dev"]["ids"] == ["host_agent_bkup"]
    assert disc["dev"]["cns"] == [["mac", "aa:bb:cc:dd:ee:ff"]]
    assert disc["avty_t"] == "host_agent/bkup/availability"
    comp = disc["cmps"]["disk_a"]
    assert comp["p"] == "binary_sensor"
    assert comp["default_entity_id"] == "binary_sensor.bkup_disk_a"
    assert comp["state_topic"] == "host_agent/bkup/state/disk_a"
    assert disc["cmps"]["refresh"]["command_topic"] == "host_agent/bkup/cmd/refresh"

    host = transport.last("host_agent/bkup/state/problem")
    assert host["state"] == "ON"
    assert host["attrs"]["severity"] == "critical"
    assert host["attrs"]["issues"][0]["subject_name"] == "Disk disk_a"
    assert transport.last("host_agent/bkup/state/problems")["state"] == 1
    assert all(retain for _, _, retain in transport.messages)


def test_missing_disk_then_forget(tmp_path):
    collector = FakeCollector()
    runner, clock = make_runner(tmp_path, collector)
    transport = FakeTransport()
    runner.transport = transport
    runner.step()

    # Disk disappears: still published, flagged as missing (critical)
    collector.disks = {}
    clock[0] += 61
    runner.step()
    disk = transport.last("host_agent/bkup/state/disk_a")
    assert disk["state"] == "ON"
    assert disk["attrs"]["issues"][0]["code"] == "missing"
    assert transport.last("host_agent/bkup/state/disk_a_temperature")["state"] is None
    assert "disk_a" in transport.last(DISCOVERY)["cmps"]

    # State survives a restart
    runner2, _ = make_runner(tmp_path, collector)
    runner2.transport = transport
    runner2.step()
    assert transport.last("host_agent/bkup/state/disk_a")["attrs"]["issues"][0]["code"] == "missing"

    # Forget removes the components (platform-only entry) and clears states
    runner2.handle_command("forget", "disk_a")
    runner2.step()
    discoveries = [json.loads(p) for t, p, _ in transport.messages if t == DISCOVERY]
    assert discoveries[-2]["cmps"]["disk_a"] == {"p": "binary_sensor"}
    assert "disk_a" not in discoveries[-1]["cmps"]  # clean document retained afterwards
    assert transport.last("host_agent/bkup/state/disk_a") == b""


def test_non_persistent_entity_removed(tmp_path):
    collector = FakeCollector()
    runner, clock = make_runner(tmp_path, collector)
    transport = FakeTransport()
    runner.transport = transport
    runner.step()
    original = collector.collect

    def without_tmp():
        result = original()
        result.entities = [e for e in result.entities if e.key != "fs_tmp"]
        result.readings = [r for r in result.readings if r.key != "fs_tmp"]
        return result

    collector.collect = without_tmp
    clock[0] += 61
    runner.step()
    discoveries = [json.loads(p) for t, p, _ in transport.messages if t == DISCOVERY]
    assert discoveries[-2]["cmps"]["fs_tmp"] == {"p": "sensor"}
    assert "fs_tmp" not in discoveries[-1]["cmps"]


def test_collector_failure_keeps_entities(tmp_path):
    collector = FakeCollector()
    runner, clock = make_runner(tmp_path, collector)
    transport = FakeTransport()
    runner.transport = transport
    runner.step()
    collector.fail = True
    clock[0] += 61
    runner.step()
    host = transport.last("host_agent/bkup/state/problem")
    assert host["attrs"]["issues"][0]["code"] == "collector_failed_fake"
    assert "disk_a" in transport.last(DISCOVERY)["cmps"]
    assert transport.last("host_agent/bkup/state/disk_a_temperature")["state"] is None


def test_refresh_and_republish(tmp_path):
    collector = FakeCollector()
    runner, clock = make_runner(tmp_path, collector)
    transport = FakeTransport()
    runner.transport = transport
    runner.step()
    count = len(transport.messages)
    runner.step()  # nothing due, nothing published
    assert len(transport.messages) == count

    clock[0] += 20
    runner.handle_command("refresh", "PRESS")
    runner.step()
    assert len(transport.messages) > count

    count = len(transport.messages)
    runner.request_republish()  # HA restarted
    runner.step()
    assert any(t == DISCOVERY for t, _, _ in transport.messages[count:])


def test_released_subject_is_removed(tmp_path):
    """A persistent subject the collector no longer wants (config change) is removed, not missing."""
    collector = FakeCollector()
    runner, clock = make_runner(tmp_path, collector)
    transport = FakeTransport()
    runner.transport = transport
    runner.step()
    collector.disks = {}
    collector.keeps = lambda spec: False
    clock[0] += 61
    runner.step()
    discoveries = [json.loads(p) for t, p, _ in transport.messages if t == DISCOVERY]
    assert discoveries[-2]["cmps"]["disk_a"] == {"p": "binary_sensor"}
    assert "disk_a" not in discoveries[-1]["cmps"]
    assert transport.last("host_agent/bkup/state/problem")["state"] == "OFF"


def test_button_dispatched_to_owner(tmp_path):
    collector = FakeCollector()
    pressed = []
    original = collector.collect

    def with_button():
        result = original()
        result.entities.append(EntitySpec("disk_a_selftest_short", "button", "Start", subject="disk_a"))
        return result

    collector.collect = with_button
    collector.on_command = lambda key: pressed.append(key) or True
    runner, _ = make_runner(tmp_path, collector)
    runner.transport = FakeTransport()
    runner.step()
    runner.handle_command("disk_a_selftest_short", "PRESS")
    runner.handle_command("unknown_button", "PRESS")
    runner.step()
    assert pressed == ["disk_a_selftest_short"]
