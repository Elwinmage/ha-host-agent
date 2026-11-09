from __future__ import annotations

from host_agent.collectors.filesystem import FilesystemCollector, Usage, parse_mounts
from host_agent.config import FilesystemConfig, Thresholds

GIB = 1024**3

MOUNTS = """\
/dev/mmcblk0p2 / ext4 rw,noatime 0 0
devtmpfs /dev devtmpfs rw 0 0
proc /proc proc rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/mmcblk0p1 /boot/firmware vfat rw 0 0
/dev/sda1 /bkup ext4 rw,relatime 0 0
/dev/sda1 /srv/bind ext4 rw,relatime 0 0
/dev/loop0 /snap/core/1 squashfs ro 0 0
nas:/export /mnt/nas\\040share nfs4 rw 0 0
"""

USAGE = {
    "/": Usage(total=30 * GIB, used=10 * GIB, free=19 * GIB),
    "/boot/firmware": Usage(total=GIB // 2, used=GIB // 10, free=GIB // 2 - GIB // 10),
    "/bkup": Usage(total=440 * GIB, used=400 * GIB, free=18 * GIB),
    "/mnt/nas share": Usage(total=100 * GIB, used=50 * GIB, free=50 * GIB),
}


def make(cfg, t, mounts=MOUNTS):
    return FilesystemCollector(cfg, t, mounts_reader=lambda: mounts, usage_reader=USAGE.__getitem__)


def test_parse_mounts_unescapes():
    assert parse_mounts(MOUNTS)[-1].mountpoint == "/mnt/nas share"


def test_selection_and_thresholds(t_en):
    cfg = FilesystemConfig(overrides={"/bkup": Thresholds(90, 97)})
    result = make(cfg, t_en).collect()
    problems = {e.key: e for e in result.entities if e.role == "problem"}
    assert set(problems) == {"fs_root", "fs_boot_firmware", "fs_bkup", "fs_mnt_nas_share"}
    readings = {r.key: r for r in result.readings}
    # 400 / (400 + 18) = 95.7 % -> warning with the /bkup override (90/97)
    assert readings["fs_bkup_used_pct"].state == 95.7
    assert readings["fs_bkup"].state == "ON"
    assert readings["fs_bkup"].attrs["severity"] == "warning"
    assert readings["fs_bkup_free"].state == 18.0
    assert readings["fs_root"].state == "OFF"
    assert not problems["fs_bkup"].persistent


def test_required_mount_missing(t_fr):
    cfg = FilesystemConfig(required=["/bkup"])
    mounts = "\n".join(line for line in MOUNTS.splitlines() if "/dev/sda1" not in line)
    result = make(cfg, t_fr, mounts).collect()
    readings = {r.key: r for r in result.readings}
    assert readings["fs_bkup"].state == "ON"
    assert readings["fs_bkup"].attrs["issues"][0]["message"] == "Non monté"
    assert readings["fs_bkup"].attrs["severity"] == "critical"
    assert readings["fs_bkup_used_pct"].state is None


def test_include_mounts(t_en):
    cfg = FilesystemConfig(include_mounts=["/bkup"])
    result = make(cfg, t_en).collect()
    assert {e.subject for e in result.entities} == {"fs_bkup"}


def test_small_filesystem_in_mib(t_en):
    result = make(FilesystemConfig(), t_en).collect()
    free = next(e for e in result.entities if e.key == "fs_boot_firmware_free")
    assert free.unit == "MiB"
    reading = next(r for r in result.readings if r.key == "fs_boot_firmware_free")
    assert reading.state == round((GIB // 2 - GIB // 10) / 1024**2, 2)
    assert next(e for e in result.entities if e.key == "fs_bkup_free").unit == "GiB"


def test_keeps_only_required(t_en):
    collector = make(FilesystemConfig(required=["/bkup/dirvish"]), t_en)
    from host_agent.model import EntitySpec

    assert collector.keeps(EntitySpec("fs_bkup_dirvish", "binary_sensor", "x", subject="fs_bkup_dirvish"))
    assert not collector.keeps(EntitySpec("fs_bkup", "binary_sensor", "x", subject="fs_bkup"))
