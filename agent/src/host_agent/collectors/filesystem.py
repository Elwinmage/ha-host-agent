"""Filesystem usage collector (mounted, device-backed filesystems)."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from host_agent.collectors.base import Collector
from host_agent.config import FilesystemConfig, slugify
from host_agent.i18n import Translator
from host_agent.model import CRITICAL, CollectorResult, EntitySpec, Issue, Reading, problem_reading

GIB = 1024**3
MIB = 1024**2
# Below this size, free space is reported in MiB (e.g. /boot)
SMALL_FS_BYTES = 10 * GIB


@dataclass
class Mount:
    source: str
    mountpoint: str
    fstype: str
    options: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        return "ro" in self.options


@dataclass
class Usage:
    total: int
    used: int
    free: int  # available to unprivileged users
    inodes_total: int = 0
    inodes_free: int = 0

    @property
    def inodes_pct(self) -> float | None:
        # btrfs and some network filesystems report no inode limit
        if not self.inodes_total:
            return None
        return round((self.inodes_total - self.inodes_free) * 100 / self.inodes_total, 1)

    @property
    def used_pct(self) -> float:
        # Same formula as df: used / (used + available)
        denom = self.used + self.free
        return round(self.used * 100 / denom, 1) if denom else 0.0


def _unescape(value: str) -> str:
    # /proc/mounts escapes spaces and tabs as octal sequences
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def parse_mounts(text: str) -> list[Mount]:
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            options = tuple(parts[3].split(",")) if len(parts) > 3 else ()
            mounts.append(Mount(_unescape(parts[0]), _unescape(parts[1]), parts[2], options))
    return mounts


def statvfs_usage(path: str) -> Usage:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free_root = st.f_bfree * st.f_frsize
    return Usage(
        total=total,
        used=total - free_root,
        free=st.f_bavail * st.f_frsize,
        inodes_total=st.f_files,
        inodes_free=st.f_ffree,
    )


def mount_slug(mountpoint: str) -> str:
    return "root" if mountpoint == "/" else slugify(mountpoint)


class FilesystemCollector(Collector):
    name = "filesystem"

    def __init__(
        self,
        cfg: FilesystemConfig,
        translate: Translator,
        mounts_reader: Callable[[], str] | None = None,
        usage_reader: Callable[[str], Usage] = statvfs_usage,
    ) -> None:
        super().__init__(translate)
        self.cfg = cfg
        self._read_mounts = mounts_reader or self._proc_mounts
        self._usage = usage_reader

    @staticmethod
    def _proc_mounts() -> str:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            return fh.read()

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def select(self, mounts: list[Mount]) -> list[Mount]:
        selected: dict[str, Mount] = {}
        for mount in mounts:
            mp = mount.mountpoint
            if self.cfg.include_mounts:
                if mp not in self.cfg.include_mounts and mp not in self.cfg.required:
                    continue
            elif mp not in self.cfg.required:
                if mount.fstype in self.cfg.exclude_fstypes:
                    continue
                if any(fnmatch.fnmatch(mp, pattern) for pattern in self.cfg.exclude_mounts):
                    continue
                if not mount.source.startswith("/") and ":" not in mount.source:
                    continue  # virtual filesystem without a backing device
            # Bind mounts / btrfs subvolumes: keep the shortest mountpoint per source
            current = selected.get(mount.source)
            if current is None or len(mp) < len(current.mountpoint):
                selected[mount.source] = mount
        return sorted(selected.values(), key=lambda m: m.mountpoint)

    def keeps(self, spec: EntitySpec) -> bool:
        return spec.subject in {f"fs_{mount_slug(mp)}" for mp in self.cfg.required}

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        mounted = self.select(parse_mounts(self._read_mounts()))
        mounted_points = {m.mountpoint for m in mounted}

        for mount in mounted:
            result.extend(self._mount_result(mount, persistent=mount.mountpoint in self.cfg.required))

        # Required mountpoints that are not mounted (e.g. a backup volume)
        for mp in self.cfg.required:
            if mp not in mounted_points:
                result.extend(self._unmounted_result(mp))
        return result

    def _specs(self, mp: str, persistent: bool, small: bool = False) -> tuple[str, list[EntitySpec]]:
        key = f"fs_{mount_slug(mp)}"
        common: dict[str, Any] = {"subject": key, "subject_label": mp, "persistent": persistent}
        return key, [
            EntitySpec(
                key=key,
                platform="binary_sensor",
                name=self.t("fs_problem", mount=mp),
                device_class="problem",
                icon="mdi:folder-alert",
                role="problem",
                **common,
            ),
            EntitySpec(
                key=f"{key}_used_pct",
                platform="sensor",
                name=self.t("fs_used_pct", mount=mp),
                unit="%",
                state_class="measurement",
                icon="mdi:harddisk",
                precision=1,
                **common,
            ),
            EntitySpec(
                key=f"{key}_free",
                platform="sensor",
                name=self.t("fs_free", mount=mp),
                device_class="data_size",
                unit="MiB" if small else "GiB",
                state_class="measurement",
                precision=0 if small else 1,
                **common,
            ),
            EntitySpec(
                key=f"{key}_inodes",
                platform="sensor",
                name=self.t("fs_inodes", mount=mp),
                unit="%",
                state_class="measurement",
                entity_category="diagnostic",
                icon="mdi:file-multiple",
                precision=1,
                **common,
            ),
        ]

    def _mount_result(self, mount: Mount, persistent: bool) -> CollectorResult:
        usage = self._usage(mount.mountpoint)
        small = usage.total < SMALL_FS_BYTES
        key, entities = self._specs(mount.mountpoint, persistent, small)
        thresholds = self.cfg.thresholds_for(mount.mountpoint)
        issues = []
        severity = thresholds.check(usage.used_pct)
        if severity:
            issues.append(
                Issue(
                    key,
                    "fs_usage",
                    severity,
                    self.t("fs_usage", value=usage.used_pct, limit=thresholds.limit_for(severity)),
                )
            )
        severity = self.cfg.inodes.check(usage.inodes_pct)
        if severity:
            issues.append(
                Issue(
                    key,
                    "fs_inodes",
                    severity,
                    self.t(
                        "fs_inodes_high", value=usage.inodes_pct, limit=self.cfg.inodes.limit_for(severity)
                    ),
                )
            )
        if mount.read_only and mount.mountpoint not in self.cfg.readonly_ok:
            issues.append(Issue(key, "fs_readonly", CRITICAL, self.t("fs_readonly")))
        attrs = {
            "kind": "filesystem",
            "read_only": mount.read_only,
            "inodes_pct": usage.inodes_pct,
            "mountpoint": mount.mountpoint,
            "source": mount.source,
            "fstype": mount.fstype,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "warning_pct": thresholds.warning,
            "critical_pct": thresholds.critical,
        }
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(key, issues, attrs),
                Reading(f"{key}_used_pct", usage.used_pct),
                Reading(f"{key}_free", round(usage.free / (MIB if small else GIB), 2)),
                Reading(f"{key}_inodes", usage.inodes_pct),
            ],
            issues=issues,
        )

    def _unmounted_result(self, mp: str) -> CollectorResult:
        key, entities = self._specs(mp, persistent=True)
        issues = [Issue(key, "fs_not_mounted", CRITICAL, self.t("fs_not_mounted"))]
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(key, issues, {"kind": "filesystem", "mountpoint": mp, "mounted": False}),
                Reading(f"{key}_used_pct", None),
                Reading(f"{key}_free", None),
                Reading(f"{key}_inodes", None),
            ],
            issues=issues,
        )
