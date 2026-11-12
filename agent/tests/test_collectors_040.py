"""Collectors added in 0.4.0: GPU, services, storage, updates, kernel log, and the new
checks of the system, filesystem and dirvish collectors."""

from __future__ import annotations

import gzip
import io
import os
from datetime import datetime, timedelta

import pytest

from host_agent.collectors.base import CommandResult
from host_agent.collectors.dirvish import (
    DirvishCollector,
    RsyncStats,
    baseline_of,
    detect_anomalies,
    parse_rsync_stats,
)
from host_agent.collectors.filesystem import FilesystemCollector, Usage, parse_mounts
from host_agent.collectors.gpu import GpuCollector, parse_nvidia_csv, read_amd
from host_agent.collectors.kernel import KernelCollector, classify
from host_agent.collectors.services import ServicesCollector, parse_failed
from host_agent.collectors.storage import StorageCollector, parse_mdstat
from host_agent.collectors.system import SystemCollector
from host_agent.collectors.updates import UpdatesCollector, parse_apt_simulation, parse_dnf
from host_agent.config import (
    DirvishConfig,
    FilesystemConfig,
    GpuConfig,
    KernelConfig,
    ServicesConfig,
    StorageConfig,
    SystemConfig,
    UpdatesConfig,
    parse_config,
)

NOW = datetime(2026, 11, 12, 12, 0).astimezone()


def readings(result):
    return {r.key: r for r in result.readings}


def codes(reading):
    return {(i["code"], i["severity"]) for i in reading.attrs["issues"]}


class FakeRun:
    """Command runner answering by the first matching argument."""

    def __init__(self, answers: dict[str, CommandResult]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        joined = " ".join(args)
        for needle, result in self.answers.items():
            if needle in joined:
                return result
        return CommandResult(127, "", "not found")


# --------------------------------------------------------------------------- #
# GPU
# --------------------------------------------------------------------------- #

NVIDIA_CSV = (
    "0, 00000000:01:00.0, NVIDIA GeForce RTX 3060, 88, 97, 11800, 12288, 168.50, 170.00, 85, "
    "0x0000000000000044, P2, 550.54.14\n"
    "1, 00000000:02:00.0, NVIDIA T400, [N/A], 0, 5, 2048, [N/A], [N/A], [N/A], 0x0000000000000001, P8, 550.54.14\n"
)


def test_parse_nvidia():
    gpus = parse_nvidia_csv(NVIDIA_CSV)
    first, second = gpus
    assert first.key == "gpu_00000000_01_00_0"
    assert first.temperature == 88 and first.utilization == 97
    assert first.memory_pct == 96.0
    assert first.reasons == ["sw_power_cap", "hw_thermal_slowdown"]
    assert first.bad_reasons == ["hw_thermal_slowdown"]
    assert second.temperature is None and second.power is None
    assert second.bad_reasons == []


def test_nvidia_falls_back_to_old_reason_field(t_en, monkeypatch, tmp_path):
    smi = tmp_path / "nvidia-smi"
    smi.write_text("#!/bin/sh\n")
    smi.chmod(0o755)
    run = FakeRun(
        {
            "clocks_event_reasons": CommandResult(
                2, "", 'Field "clocks_event_reasons.active" is not a valid field'
            ),
            "clocks_throttle_reasons": CommandResult(0, NVIDIA_CSV, ""),
        }
    )
    collector = GpuCollector(GpuConfig(nvidia_smi=str(smi)), t_en, runner=run, root=str(tmp_path))
    result = readings(collector.collect())
    gpu = result["gpu_00000000_01_00_0"]
    assert codes(gpu) == {("gpu_temperature_high", "warning"), ("gpu_throttled", "warning")}
    assert gpu.attrs["kind"] == "gpu" and gpu.attrs["vendor"] == "nvidia"
    assert result["gpu_00000000_01_00_0_power"].state == 168.5
    # the working field name is remembered
    collector.collect()
    assert "clocks_event_reasons" not in run.calls[-1][1]


@pytest.fixture
def amd_root(tmp_path):
    pci = tmp_path / "sys/devices/pci0000:00/0000:03:00.0"
    hwmon = pci / "hwmon/hwmon3"
    hwmon.mkdir(parents=True)
    (tmp_path / "sys/bus/pci/drivers/amdgpu").mkdir(parents=True)
    os.symlink(tmp_path / "sys/bus/pci/drivers/amdgpu", pci / "driver")
    (pci / "gpu_busy_percent").write_text("37\n")
    (pci / "mem_info_vram_used").write_text(str(4 * 2**30))
    (pci / "mem_info_vram_total").write_text(str(16 * 2**30))
    (pci / "product_name").write_text("Radeon RX 6800\n")
    for index, (label, value) in enumerate((("edge", 61000), ("junction", 98000), ("mem", 70000)), start=1):
        (hwmon / f"temp{index}_input").write_text(str(value))
        (hwmon / f"temp{index}_label").write_text(label)
    (hwmon / "power1_average").write_text("187000000")
    (hwmon / "power1_cap").write_text("203000000")
    (hwmon / "fan1_input").write_text("1450")
    drm = tmp_path / "sys/class/drm"
    (drm / "card1").mkdir(parents=True)
    os.symlink(pci, drm / "card1/device")
    (drm / "card1-DP-1").mkdir()
    return tmp_path


def test_amd(amd_root, t_fr):
    (gpu,) = read_amd(str(amd_root))
    assert gpu.name == "Radeon RX 6800"
    assert gpu.temperature == 61.0  # edge; junction kept in the attributes
    assert gpu.extra["temperatures"]["junction"] == 98.0
    assert gpu.memory_pct == 25.0
    assert gpu.power == 187.0 and gpu.power_limit == 203.0
    hwmon = next(amd_root.glob("sys/devices/*/*/hwmon/hwmon3"))
    (hwmon / "temp1_input").write_text("96000")
    collector = GpuCollector(GpuConfig(nvidia_smi="/nonexistent"), t_fr, root=str(amd_root))
    reading = readings(collector.collect())["gpu_0000_03_00_0"]
    assert reading.attrs["issues"][0]["message"] == "Température 96.0 °C (seuil 95 °C)"


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #


def test_services(t_en):
    assert parse_failed(
        "● smartd.service loaded failed failed SMART\nfoo.timer loaded failed failed x\n"
    ) == ["smartd.service", "foo.timer"]
    run = FakeRun(
        {
            "list-units": CommandResult(
                0, "smartd.service loaded failed failed SMART\nfoo.timer loaded failed failed X\n", ""
            ),
            "is-active": CommandResult(3, "active\ninactive\n", ""),
        }
    )
    cfg = ServicesConfig(watch=["sshd", "mosquitto.service"], ignore=["foo.*"])
    r = readings(ServicesCollector(cfg, t_en, runner=run).collect())
    assert r["services_failed"].state == 1
    assert codes(r["services"]) == {
        ("service_down_mosquitto.service", "critical"),
        ("unit_failed_smartd.service", "warning"),
    }
    assert run.calls[-1][-2:] == ["sshd.service", "mosquitto.service"]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

MDSTAT = """Personalities : [raid1] [raid6] [raid5]
md1 : active raid1 sdc1[1](F) sdb1[0]
      976630464 blocks super 1.2 [2/1] [U_]
      bitmap: 2/8 pages [8KB], 65536KB chunk

md0 : active (auto-read-only) raid5 sdd[0] sde[1] sdf[3]
      1953260544 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/3] [UUU]
      [=>...................]  check =  8.5% (83046400/976630272) finish=74.2min speed=200693K/sec

unused devices: <none>
"""

ZPOOL_LIST = "tank\tONLINE\t3985729650688\t3500000000000\t88\nbackup\tDEGRADED\t1000\t100\t10\n"
ZPOOL_STATUS = """  pool: tank
 state: ONLINE
  scan: scrub repaired 0B in 05:12:44 with 2 errors on Sun Sep  6 05:36:45 2026
config:
errors: No known data errors

  pool: backup
 state: DEGRADED
  scan: scrub in progress since Thu Nov 12 10:00:00 2026
errors: 3 data errors, use '-v' for a list
"""


def test_mdstat():
    md1, md0 = parse_mdstat(MDSTAT)
    assert md1.degraded and md1.failed == ["sdc1"] and md1.status == "U_"
    assert md0.level == "raid5" and not md0.degraded and md0.action == "check" and md0.progress == 8.5


def test_storage(tmp_path, t_en):
    mdstat = tmp_path / "mdstat"
    mdstat.write_text(MDSTAT)
    zpool = tmp_path / "zpool"
    zpool.write_text("#!/bin/sh\n")
    run = FakeRun({"list": CommandResult(0, ZPOOL_LIST, ""), "status": CommandResult(0, ZPOOL_STATUS, "")})
    collector = StorageCollector(
        StorageConfig(zpool=str(zpool)), t_en, runner=run, mdstat=str(mdstat), now=lambda: NOW
    )
    r = readings(collector.collect())
    assert codes(r["raid_md1"]) == {("raid_degraded", "critical")}
    assert r["raid_md0"].state == "OFF"
    assert codes(r["zpool_tank"]) == {
        ("zpool_scrub_errors", "warning"),
        ("zpool_scrub_outdated", "warning"),
        ("zpool_capacity", "warning"),
    }
    assert r["zpool_tank_capacity"].state == 88.0
    assert codes(r["zpool_backup"]) == {("zpool_unhealthy", "critical"), ("zpool_errors", "critical")}
    assert r["zpool_backup"].attrs["scrub_running"] is True


# --------------------------------------------------------------------------- #
# Updates
# --------------------------------------------------------------------------- #

APT = """Reading package lists...
Inst libssl3t64 [3.5.1-1] (3.5.1-1+deb13u1 Debian-Security:13/stable-security [arm64])
Inst openssh-client [1:10.0p1-7] (1:10.0p1-7+deb13u1 Debian:13.2/stable [arm64])
Inst tzdata [2025b-4] (2025b-4+deb13u1 Debian:13.2/stable-updates [all])
Conf libssl3t64 (3.5.1-1+deb13u1 Debian-Security:13/stable-security [arm64])
"""


def test_apt(t_en, monkeypatch):
    pending = parse_apt_simulation(APT)
    assert pending.packages == ["libssl3t64", "openssh-client", "tzdata"]
    assert pending.security == ["libssl3t64"]
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    run = FakeRun({"apt-get": CommandResult(0, APT, "")})
    r = readings(UpdatesCollector(UpdatesConfig(), t_en, runner=run).collect())
    assert r["updates_pending"].state == 3 and r["updates_security"].state == 1
    assert codes(r["updates"]) == {("security_updates", "warning")}


def test_dnf():
    check = "\nkernel.x86_64    6.11.4-301.fc41    updates\nopenssl-libs.x86_64  1:3.2.2-9.fc41  updates\n"
    sec = "FEDORA-2026-1 Important/Sec. openssl-libs-1:3.2.2-9.fc41.x86_64\n"
    pending = parse_dnf(check, sec)
    assert pending.packages == ["kernel", "openssl-libs"]
    assert pending.security == ["openssl-libs-1:3.2.2-9.fc41.x86_64"]


# --------------------------------------------------------------------------- #
# Kernel log
# --------------------------------------------------------------------------- #

JOURNAL = """2026-11-12T03:10:01+0100 bkup kernel: usb 2-1: reset SuperSpeed USB device number 2 using xhci_hcd
2026-11-12T03:10:02+0100 bkup kernel: sd 0:0:0:0: [sda] tag#3 uas_eh_abort_handler 0 uas-tag 4 inflight: IN
2026-11-12T03:10:03+0100 bkup kernel: blk_update_request: I/O error, dev sda, sector 123456 op 0x0:(READ)
2026-11-12T03:10:04+0100 bkup kernel: EXT4-fs error (device sda1): ext4_find_entry:1455: inode #2: comm ls
2026-11-12T03:11:00+0100 bkup kernel: hwmon hwmon1: Undervoltage detected!
2026-11-12T03:12:00+0100 bkup kernel: eth0: Link is Up - 1Gbps/Full
"""


def test_kernel(t_en):
    import re

    counts, recent = classify(JOURNAL.splitlines(), [re.compile("Link is Up")], [re.compile("uas_eh")])
    assert counts == {"usb": 1, "io": 1, "filesystem": 1, "power": 1, "custom": 1}
    assert recent[0]["category"] == "usb"
    run = FakeRun({"journalctl": CommandResult(0, JOURNAL, "")})
    r = readings(KernelCollector(KernelConfig(window_hours=6), t_en, runner=run).collect())
    assert r["kernel_errors"].state == 5
    assert ("kernel_filesystem", "critical") in codes(r["kernel"])
    assert ("kernel_usb", "warning") in codes(r["kernel"])
    assert "-6h" in run.calls[0]


# --------------------------------------------------------------------------- #
# System: Raspberry Pi power, NTP
# --------------------------------------------------------------------------- #


@pytest.fixture
def pi_root(tmp_path):
    (tmp_path / "proc/device-tree").mkdir(parents=True)
    (tmp_path / "proc/device-tree/model").write_text("Raspberry Pi 4 Model B Rev 1.4\x00")
    firmware = tmp_path / "sys/devices/platform/soc/soc:firmware"
    firmware.mkdir(parents=True)
    (firmware / "get_throttled").write_text("50000\n")
    return tmp_path


def test_pi_power_and_ntp(pi_root, t_fr, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    run = FakeRun({"timedatectl": CommandResult(0, "no\n", "")})
    r = readings(
        SystemCollector(SystemConfig(), t_fr, root=str(pi_root), now=lambda: NOW, runner=run).collect()
    )
    system = r["system"]
    assert system.attrs["pi_flags"] == ["under_voltage_occurred", "throttled_occurred"]
    assert system.attrs["ntp_synchronized"] is False
    assert codes(system) == {("pi_undervoltage_past", "warning"), ("ntp_unsynchronized", "warning")}

    (pi_root / "sys/devices/platform/soc/soc:firmware/get_throttled").write_text("50005\n")
    run = FakeRun({"timedatectl": CommandResult(0, "yes\n", "")})
    r = readings(
        SystemCollector(SystemConfig(), t_fr, root=str(pi_root), now=lambda: NOW, runner=run).collect()
    )
    assert codes(r["system"]) == {("pi_undervoltage", "critical"), ("pi_throttled", "warning")}


# --------------------------------------------------------------------------- #
# Filesystem: read-only, inodes
# --------------------------------------------------------------------------- #


def test_filesystem_readonly_and_inodes(t_en):
    mounts = "/dev/sda1 /bkup/dirvish ext4 ro,relatime 0 0\n/dev/mmcblk0p1 /boot/firmware vfat ro 0 0\n"
    assert parse_mounts(mounts)[0].read_only
    usage = {
        "/bkup/dirvish": Usage(total=100, used=10, free=90, inodes_total=1000, inodes_free=20),
        "/boot/firmware": Usage(total=100, used=10, free=90),
    }
    cfg = FilesystemConfig(readonly_ok=["/boot/firmware"])
    collector = FilesystemCollector(cfg, t_en, mounts_reader=lambda: mounts, usage_reader=usage.__getitem__)
    r = readings(collector.collect())
    assert codes(r["fs_bkup_dirvish"]) == {("fs_readonly", "critical"), ("fs_inodes", "critical")}
    assert r["fs_bkup_dirvish_inodes"].state == 98.0
    assert r["fs_boot_firmware"].state == "OFF"
    assert r["fs_boot_firmware_inodes"].state is None


def test_filesystem_reads_host_mount_table(t_en, monkeypatch):
    # The agent's namespace (ProtectSystem=strict) shows everything read-only:
    # options must come from PID 1's table
    host = "/dev/sda1 /data ext4 rw,relatime 0 0\n"
    agent_ns = "/dev/sda1 /data ext4 ro,relatime 0 0\n"
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/1/mounts":
            return io.StringIO(host)
        if path == "/proc/self/mounts":
            return io.StringIO(agent_ns)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    usage = {"/data": Usage(total=100, used=10, free=90)}
    collector = FilesystemCollector(FilesystemConfig(), t_en, usage_reader=usage.__getitem__)
    r = readings(collector.collect())
    assert r["fs_data"].state == "OFF"
    assert r["fs_data"].attrs["read_only"] is False

    # hidepid: only the agent's namespace is readable, read-only state is unknown
    def no_pid1(path, *args, **kwargs):
        if path == "/proc/1/mounts":
            raise PermissionError(path)
        return fake_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", no_pid1)
    r = readings(collector.collect())
    assert r["fs_data"].state == "OFF"
    assert r["fs_data"].attrs["read_only"] is None


# --------------------------------------------------------------------------- #
# dirvish: consistency of the last image
# --------------------------------------------------------------------------- #

STATS_31 = """sending incremental file list
home/eric/file
Number of files: 1,234,567 (reg: 1,100,000, dir: 134,567)
Number of created files: 1,234,567 (reg: 1,100,000, dir: 134,567)
Number of deleted files: 0
Number of regular files transferred: 2,345
Total file size: 345,678,901,234 bytes
Total transferred file size: 1,234,567,890 bytes

sent 1,345,678 bytes  received 9,876,543 bytes  12,345.67 bytes/sec
total size is 345,678,901,234  speedup is 30,794.12
"""
STATS_30 = "Number of files: 1500\nNumber of files transferred: 12\nTotal file size: 99999 bytes\n"


def test_parse_rsync_stats():
    assert parse_rsync_stats(STATS_31) == RsyncStats(files=1234567, size=345678901234, transferred=2345)
    assert parse_rsync_stats(STATS_30) == RsyncStats(files=1500, size=99999, transferred=12)
    assert parse_rsync_stats("nothing") is None


def test_anomalies():
    cfg = DirvishConfig()
    base = baseline_of(
        [RsyncStats(1000, 10_000, 100), RsyncStats(1010, 10_100, 120), RsyncStats(990, 9_900, 90)]
    )
    assert base == RsyncStats(1000, 10_000, 100)
    assert baseline_of([RsyncStats(1, 1, 1)] * 2) is None
    assert detect_anomalies(RsyncStats(1005, 10_050, 150), base, cfg) == []
    found = dict(detect_anomalies(RsyncStats(700, 7_000, 5000), base, cfg))
    assert found["vault_size_drop"] == {"pct": 30}
    assert found["vault_files_drop"] == {"pct": 30, "count": 300}
    assert found["vault_changes_spike"] == {"count": 5000, "usual": 100}


def _image(bank, day: datetime, stats: str, compress: bool):
    image = bank / "home" / day.strftime("%Y%m%d")
    (image / "tree").mkdir(parents=True)
    begin = day.strftime("%Y-%m-%d %H:%M:%S")
    (image / "summary").write_text(f"Backup-begin: {begin}\nBackup-complete: {begin}\nStatus: success\n")
    log = "file\n" * 2000 + stats
    if compress:
        with gzip.open(image / "log.gz", "wt") as fh:
            fh.write(log)
    else:
        (image / "log").write_text(log)


def test_dirvish_anomaly_detection(tmp_path, t_fr):
    bank = tmp_path / "vaults"
    (bank / "home" / "dirvish").mkdir(parents=True)
    for days in range(1, 6):
        _image(
            bank,
            NOW - timedelta(days=days),
            f"Number of files: {100_000 + days}\nNumber of files transferred: 150\nTotal file size: 5000000 bytes\n",
            compress=True,
        )
    _image(
        bank,
        NOW - timedelta(hours=2),
        "Number of files: 60000\nNumber of files transferred: 59000\nTotal file size: 5000000 bytes\n",
        compress=False,
    )
    collector = DirvishCollector(DirvishConfig(banks=[str(bank)]), t_fr, now=lambda: NOW)
    home = readings(collector.collect())["vault_home"]
    assert codes(home) == {("vault_files_drop", "warning"), ("vault_changes_spike", "warning")}
    assert home.attrs["last_stats"] == {"files": 60000, "size_bytes": 5000000, "transferred": 59000}
    assert home.attrs["baseline_stats"]["transferred"] == 150
    # cached: the second collection does not re-read the logs
    for log in bank.rglob("log*"):
        log.unlink()
    assert readings(collector.collect())["vault_home"].attrs["last_stats"]["files"] == 60000


def test_new_sections_config():
    cfg = parse_config(
        {
            "collectors": {
                "gpu": {"temperature": {"warning": 80, "critical": 90}},
                "services": {"watch": ["sshd"], "interval": 60},
                "storage": {"scrub_max_age_days": 40},
                "updates": {"security_is_warning": False},
                "kernel": {"window_hours": 12, "patterns": ["segfault"]},
                "filesystem": {"readonly_ok": ["/boot/firmware"], "inodes": {"warning": 80, "critical": 90}},
                "system": {"power": False},
                "dirvish": {"anomaly_history": 10, "changes_factor": 3},
            }
        }
    )
    assert cfg.gpu.temperature.critical == 90
    assert cfg.services.watch == ["sshd"]
    assert cfg.kernel.patterns == ["segfault"]
    assert cfg.dirvish.changes_factor == 3.0
    assert cfg.filesystem.inodes.warning == 80
    for bad in ({"updates": {"interval": 10}}, {"kernel": {"patterns": ["("]}}, {"gpu": {"bogus": 1}}):
        with pytest.raises(ValueError):
            parse_config({"collectors": bad})
