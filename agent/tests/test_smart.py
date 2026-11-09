from __future__ import annotations

import copy
import json
from datetime import datetime

from conftest import load_fixture

from host_agent.collectors.base import CommandResult
from host_agent.collectors.smart import SmartCollector, parse_smart
from host_agent.config import SelfTestSchedule, SmartConfig


def codes(report):
    return {(i.code, i.severity) for i in report.issues}


# --------------------------------------------------------------------------- #
# Real outputs from the bkup Raspberry Pi and an NVMe host
# --------------------------------------------------------------------------- #


def test_ata_crucial_over_usb_sat(smart_cfg, t_en):
    data = load_fixture("ata_crucial_sat.json")
    report = parse_smart(data, "/dev/sda", smart_cfg, t_en)

    assert report.key == "disk_ata0000000001"
    assert report.label == "CT480BX500SSD1 (sda)"
    assert report.protocol == "ATA"
    assert report.temperature == 43
    assert report.power_on_hours == 17487
    assert report.smart_passed is True
    assert report.selftest.supported is True
    # 16 "Aborted by host" entries precede the last successful test at 17145 h
    assert report.selftest.last_ok_hours == 17487 - 17145
    assert report.selftest.aborted_since_ok == 16
    assert report.selftest.failed is None
    assert codes(report) == {("selftest_outdated", "warning")}
    assert report.extra["watched_attributes"]["Reallocate_NAND_Blk_Cnt"] == 0


def test_ata_selftest_age_disabled(t_en):
    cfg = SmartConfig(selftest_max_age_hours=0)
    report = parse_smart(load_fixture("ata_crucial_sat.json"), "/dev/sda", cfg, t_en)
    assert report.issues == []


def test_scsi_view_of_usb_bridge(smart_cfg, t_en):
    """smartctl without -d sat through the USB bridge: exit 4, temperature 0."""
    report = parse_smart(load_fixture("scsi_usb_bridge.json"), "/dev/sda1", smart_cfg, t_en)
    assert report.protocol == "SCSI"
    assert report.temperature is None  # 0 is a bridge artefact, not a reading
    assert report.selftest.supported is False
    assert codes(report) == {("smart_command_failed", "warning")}


def test_nvme_patriot_healthy(smart_cfg, t_en):
    report = parse_smart(load_fixture("nvme_patriot.json"), "/dev/nvme1", smart_cfg, t_en)
    assert report.protocol == "NVMe"
    assert report.temperature == 45
    assert report.power_on_hours == 32629
    assert report.selftest.supported is False  # optional admin command not implemented
    assert report.extra["percentage_used"] == 3
    assert report.issues == []


# --------------------------------------------------------------------------- #
# Synthetic failures derived from the real outputs
# --------------------------------------------------------------------------- #


def _attr(data, attr_id):
    return next(a for a in data["ata_smart_attributes"]["table"] if a["id"] == attr_id)


def test_ata_failures(smart_cfg, t_en):
    data = copy.deepcopy(load_fixture("ata_crucial_sat.json"))
    data["smart_status"]["passed"] = False
    data["smartctl"]["exit_status"] = 8 | 16
    _attr(data, 5)["raw"]["value"] = 12
    _attr(data, 202)["when_failed"] = "now"
    data["ata_smart_self_test_log"]["standard"]["table"].insert(
        0,
        {
            "type": {"value": 1, "string": "Short offline"},
            "status": {"value": 0x73, "string": "Completed: read failure", "passed": False},
            "lifetime_hours": 17486,
        },
    )
    report = parse_smart(data, "/dev/sda", smart_cfg, t_en)
    assert codes(report) == {
        ("smart_failed", "critical"),
        ("attr_nonzero", "warning"),
        ("attr_failing", "critical"),
        ("selftest_failed", "critical"),
    }


def test_ata_selftest_never_ok(smart_cfg, t_en):
    data = copy.deepcopy(load_fixture("ata_crucial_sat.json"))
    table = data["ata_smart_self_test_log"]["standard"]["table"]
    data["ata_smart_self_test_log"]["standard"]["table"] = [e for e in table if e["status"]["value"] != 0]
    report = parse_smart(data, "/dev/sda", smart_cfg, t_en)
    assert ("selftest_never", "warning") in codes(report)


def test_ata_lifetime_wraparound(smart_cfg, t_en):
    data = copy.deepcopy(load_fixture("ata_crucial_sat.json"))
    data["power_on_time"]["hours"] = 65536 + 10  # log stores 16-bit hours
    data["ata_smart_self_test_log"]["standard"]["table"] = [
        {
            "type": {"value": 1, "string": "Short offline"},
            "status": {"value": 0, "string": "Completed without error", "passed": True},
            "lifetime_hours": 5,
        },
    ]
    report = parse_smart(data, "/dev/sda", smart_cfg, t_en)
    assert report.selftest.last_ok_hours == 5


def test_ata_temperature_threshold(smart_cfg, t_fr):
    data = copy.deepcopy(load_fixture("ata_crucial_sat.json"))
    data["temperature"]["current"] = 66
    report = parse_smart(data, "/dev/sda", smart_cfg, t_fr)
    issue = next(i for i in report.issues if i.code == "temperature_high")
    assert issue.severity == "critical"
    assert issue.message == "Température 66 °C (seuil 65 °C)"


def test_nvme_failures(smart_cfg, t_en):
    data = copy.deepcopy(load_fixture("nvme_patriot.json"))
    log = data["nvme_smart_health_information_log"]
    log["critical_warning"] = 0b101
    log["media_errors"] = 3
    log["available_spare"] = 5
    log["percentage_used"] = 95
    data["temperature"]["current"] = 84
    report = parse_smart(data, "/dev/nvme0", smart_cfg, t_en)
    assert codes(report) == {
        ("nvme_critical_warning", "critical"),
        ("nvme_media_errors", "critical"),
        ("nvme_spare_low", "critical"),
        ("nvme_wear", "warning"),
        ("temperature_high", "warning"),
    }
    msg = next(i.message for i in report.issues if i.code == "nvme_critical_warning")
    assert "available spare below threshold" in msg and "reliability degraded" in msg


def test_nvme_selftest_log(smart_cfg, t_en):
    data = copy.deepcopy(load_fixture("nvme_patriot.json"))
    data["nvme_optional_admin_commands"]["self_test"] = True
    data["nvme_self_test_log"] = {
        "current_self_test_operation": {"value": 0},
        "table": [
            {
                "self_test_code": {"value": 1, "string": "Short"},
                "self_test_result": {"value": 2, "string": "Aborted: Controller Reset"},
                "power_on_hours": 32628,
            },
            {
                "self_test_code": {"value": 1, "string": "Short"},
                "self_test_result": {"value": 0, "string": "Completed without error"},
                "power_on_hours": 32600,
            },
        ],
    }
    report = parse_smart(data, "/dev/nvme0", smart_cfg, t_en)
    assert report.selftest.supported
    assert report.selftest.last_ok_hours == 29
    assert report.selftest.aborted_since_ok == 1
    assert report.issues == []


def test_open_failure(smart_cfg, t_en):
    data = {"smartctl": {"exit_status": 2, "messages": [{"string": "open failed: No such device"}]}}
    report = parse_smart(data, "/dev/sdz", smart_cfg, t_en)
    assert codes(report) == {("smart_unavailable", "warning")}
    assert report.key == "disk_sdz"


# --------------------------------------------------------------------------- #
# Collector with a fake smartctl
# --------------------------------------------------------------------------- #


def NOON():  # outside the default self-test window
    return datetime(2026, 11, 9, 12, 0)


def NIGHT():
    return datetime(2026, 11, 9, 3, 0)


class FakeSmartctl:
    def __init__(self, outputs, start_rc=0):
        self.outputs = outputs
        self.start_rc = start_rc
        self.calls = []

    def starts(self):
        return [c for c in self.calls if "-t" in c]

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if "--scan-open" in args:
            return CommandResult(0, json.dumps(load_fixture("scan_open_bkup.json")), "")
        if "-t" in args:
            return CommandResult(self.start_rc, "Testing has begun.", "")
        data = self.outputs[args[-1]]
        return CommandResult(data["smartctl"]["exit_status"], json.dumps(data), "")


def test_collector_uses_scan_type(smart_cfg, t_en):
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    collector = SmartCollector(smart_cfg, t_en, runner=fake, local_now=NOON)
    result = collector.collect()
    assert fake.calls[1] == ["/usr/sbin/smartctl", "--json=c", "-a", "-d", "sat", "/dev/sda"]
    keys = {e.key for e in result.entities}
    assert keys == {
        "disk_ata0000000001",
        "disk_ata0000000001_temperature",
        "disk_ata0000000001_selftest_age",
        "disk_ata0000000001_selftest_short",
        "disk_ata0000000001_selftest_long",
    }
    problem = next(r for r in result.readings if r.key == "disk_ata0000000001")
    assert problem.state == "ON"
    assert problem.attrs["severity"] == "warning"
    assert problem.attrs["model"] == "CT480BX500SSD1"


def test_collector_holds_during_selftest(smart_cfg, t_en):
    data = copy.deepcopy(load_fixture("ata_crucial_sat.json"))
    data["ata_smart_data"]["self_test"]["status"]["value"] = 0xF9  # in progress, 90 % left
    fake = FakeSmartctl({"/dev/sda": data})
    clock = [1000.0]
    collector = SmartCollector(smart_cfg, t_en, runner=fake, clock=lambda: clock[0], local_now=NOON)
    collector.collect()
    collector.collect()  # within the hold window: no new query on the drive
    assert sum(1 for c in fake.calls if c[-1] == "/dev/sda") == 1
    clock[0] += 3600
    collector.collect()
    assert sum(1 for c in fake.calls if c[-1] == "/dev/sda") == 2


def test_collector_excludes_by_serial(t_en):
    cfg = SmartConfig(exclude=["ATA0000000001"])
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    assert SmartCollector(cfg, t_en, runner=fake, local_now=NOON).collect().entities == []


# --------------------------------------------------------------------------- #
# Self-tests started by the agent
# --------------------------------------------------------------------------- #


def test_last_attempt_per_kind(smart_cfg, t_en):
    report = parse_smart(load_fixture("ata_crucial_sat.json"), "/dev/sda", smart_cfg, t_en)
    # latest short at 17481 h, latest extended at 17456 h, power-on 17487 h
    assert report.selftest.last_attempt_hours == {"short": 6, "long": 31}
    assert report.selftest.short_minutes == 2
    assert report.selftest.long_minutes == 10


def test_no_start_outside_window(smart_cfg, t_en):
    cfg = SmartConfig(selftest=SelfTestSchedule(short_interval_hours=1))
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    SmartCollector(cfg, t_en, runner=fake, local_now=NOON).collect()
    assert fake.starts() == []


def test_scheduled_short_test(t_en):
    # short tried 6 h ago, long 31 h ago: with these intervals only the short one is due
    cfg = SmartConfig(selftest=SelfTestSchedule(short_interval_hours=4, long_interval_days=7))
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    clock = [0.0]
    collector = SmartCollector(cfg, t_en, runner=fake, clock=lambda: clock[0], local_now=NIGHT)
    result = collector.collect()
    assert fake.starts() == [["/usr/sbin/smartctl", "-t", "short", "-d", "sat", "/dev/sda"]]
    problem = next(r for r in result.readings if r.key == "disk_ata0000000001")
    assert problem.attrs["selftest_in_progress"] is True
    assert problem.attrs["selftest_started"]["kind"] == "short"
    # drive left alone while the test runs (2 min + margin)
    clock[0] += 60
    collector.collect()
    assert len([c for c in fake.calls if c[-1] == "/dev/sda"]) == 2
    # no new automatic start within the retry delay
    clock[0] += 3600
    collector.collect()
    assert len(fake.starts()) == 1


def test_scheduled_long_test_has_priority(t_en):
    cfg = SmartConfig(selftest=SelfTestSchedule(short_interval_hours=4, long_interval_days=1))
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    SmartCollector(cfg, t_en, runner=fake, local_now=NIGHT).collect()
    assert fake.starts()[0][2] == "long"


def test_nothing_due(smart_cfg, t_en):
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    SmartCollector(smart_cfg, t_en, runner=fake, local_now=NIGHT).collect()  # 24 h / 7 d defaults
    assert fake.starts() == []


def test_manual_button(smart_cfg, t_en):
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")})
    collector = SmartCollector(smart_cfg, t_en, runner=fake, local_now=NOON)
    assert collector.on_command("disk_ata0000000001_selftest_long")
    assert not collector.on_command("refresh")
    collector.collect()
    assert fake.starts() == [["/usr/sbin/smartctl", "-t", "long", "-d", "sat", "/dev/sda"]]


def test_start_failure_reported(t_en):
    cfg = SmartConfig(selftest=SelfTestSchedule(short_interval_hours=1))
    fake = FakeSmartctl({"/dev/sda": load_fixture("ata_crucial_sat.json")}, start_rc=4)
    result = SmartCollector(cfg, t_en, runner=fake, local_now=NIGHT).collect()
    problem = next(r for r in result.readings if r.key == "disk_ata0000000001")
    assert problem.attrs["selftest_in_progress"] is False
    assert "selftest_start_error" in problem.attrs


def test_window_parsing():
    sched = SelfTestSchedule(window="22:30-05:00")
    assert sched.in_window(datetime(2026, 1, 1, 23, 0))
    assert sched.in_window(datetime(2026, 1, 1, 4, 59))
    assert not sched.in_window(datetime(2026, 1, 1, 12, 0))
    assert SelfTestSchedule(window="").in_window(datetime(2026, 1, 1, 12, 0))
