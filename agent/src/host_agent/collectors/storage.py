"""Storage pools: Linux software RAID (/proc/mdstat) and ZFS (zpool)."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import StorageConfig, slugify
from host_agent.i18n import Translator
from host_agent.model import CRITICAL, WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading


@dataclass
class MdArray:
    name: str
    level: str
    state: str  # active / inactive
    devices: list[str]
    failed: list[str]
    status: str | None  # e.g. "UU_"
    action: str | None = None  # recovery / resync / check / reshape
    progress: float | None = None

    @property
    def degraded(self) -> bool:
        return bool(self.failed) or (self.status is not None and "_" in self.status)


def parse_mdstat(text: str) -> list[MdArray]:
    arrays: list[MdArray] = []
    current: MdArray | None = None
    for line in text.splitlines():
        match = re.match(
            r"^(md\w+)\s*:\s*(\w+)\s+(?:\([\w-]+\)\s+)?(raid\d+|linear|multipath)?\s*(.*)$", line
        )
        if match:
            name, state, level, rest = match.groups()
            members = re.findall(r"(\w+)\[\d+\](\([A-Z]\))?", rest)
            current = MdArray(
                name=name,
                level=level or "?",
                state=state,
                devices=[dev for dev, _ in members],
                failed=[dev for dev, flag in members if flag == "(F)"],
                status=None,
            )
            arrays.append(current)
            continue
        if current is None:
            continue
        status = re.search(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]", line)
        if status:
            current.status = status.group(3)
        action = re.search(r"(recovery|resync|check|reshape|repair)\s*=\s*([\d.]+)%", line)
        if action:
            current.action = action.group(1)
            current.progress = float(action.group(2))
    return arrays


@dataclass
class ZPool:
    name: str
    health: str
    capacity: float | None
    size: int | None
    allocated: int | None
    errors: str | None = None
    scrub_errors: int | None = None
    scrub_end: datetime | None = None
    scrub_running: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def parse_zpool_list(text: str) -> list[ZPool]:
    """`zpool list -Hp -o name,health,size,allocated,capacity`."""
    pools = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 5:
            continue

        def num(value: str) -> int | None:
            return int(value) if value.isdigit() else None

        capacity = num(cols[4].rstrip("%"))
        pools.append(
            ZPool(
                cols[0],
                cols[1],
                float(capacity) if capacity is not None else None,
                num(cols[2]),
                num(cols[3]),
            )
        )
    return pools


_SCRUB_DONE = re.compile(r"scrub repaired \S+ in \S+ with (\d+) errors on (.+)$")


def parse_zpool_status(text: str, pools: dict[str, ZPool]) -> None:
    """Enrich pools with scrub results and the errors line of `zpool status`."""
    current: ZPool | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("pool:"):
            current = pools.get(line.split(":", 1)[1].strip())
            continue
        if current is None:
            continue
        if line.startswith("scan:"):
            done = _SCRUB_DONE.search(line)
            if done:
                current.scrub_errors = int(done.group(1))
                try:
                    current.scrub_end = datetime.strptime(
                        done.group(2).strip(), "%a %b %d %H:%M:%S %Y"
                    ).astimezone()
                except ValueError:
                    current.scrub_end = None
            current.scrub_running = "in progress" in line
        elif line.startswith("errors:"):
            current.errors = line.split(":", 1)[1].strip()


class StorageCollector(Collector):
    name = "storage"

    def __init__(
        self,
        cfg: StorageConfig,
        translate: Translator,
        runner: CommandRunner = run_command,
        mdstat: str = "/proc/mdstat",
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg
        self.mdstat = mdstat
        self.now = now

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        try:
            text = Path(self.mdstat).read_text(encoding="utf-8")
        except OSError:
            text = ""
        for array in parse_mdstat(text):
            result.extend(self._md(array))
        if shutil.which(self.cfg.zpool) or Path(self.cfg.zpool).is_file():
            res = self.run([self.cfg.zpool, "list", "-Hp", "-o", "name,health,size,allocated,capacity"], 30)
            pools = {pool.name: pool for pool in parse_zpool_list(res.stdout)} if res.returncode == 0 else {}
            if pools:
                status = self.run([self.cfg.zpool, "status"], 30)
                parse_zpool_status(status.stdout, pools)
            for pool in pools.values():
                result.extend(self._zpool(pool))
        return result

    def _entities(self, key: str, label: str, icon: str, capacity: bool) -> list[EntitySpec]:
        common: dict[str, Any] = {"subject": key, "subject_label": label, "persistent": True}
        entities = [
            EntitySpec(
                key,
                "binary_sensor",
                self.t("pool_problem", name=label),
                device_class="problem",
                icon=icon,
                role="problem",
                **common,
            )
        ]
        if capacity:
            entities.append(
                EntitySpec(
                    f"{key}_capacity",
                    "sensor",
                    self.t("pool_capacity", name=label),
                    unit="%",
                    state_class="measurement",
                    icon="mdi:database",
                    **common,
                )
            )
        return entities

    def _md(self, array: MdArray) -> CollectorResult:
        key = f"raid_{slugify(array.name)}"
        issues: list[Issue] = []
        if array.degraded:
            issues.append(
                Issue(
                    key,
                    "raid_degraded",
                    CRITICAL,
                    self.t(
                        "raid_degraded", status=array.status or "?", failed=", ".join(array.failed) or "-"
                    ),
                )
            )
        elif array.state != "active":
            issues.append(Issue(key, "raid_inactive", CRITICAL, self.t("raid_inactive", state=array.state)))
        if array.action in ("recovery", "reshape"):
            issues.append(
                Issue(
                    key,
                    "raid_rebuilding",
                    WARNING,
                    self.t("raid_rebuilding", action=array.action, progress=array.progress),
                )
            )
        attrs = {
            "kind": "pool",
            "type": "mdadm",
            "name": array.name,
            "level": array.level,
            "state": array.state,
            "devices": array.devices,
            "failed_devices": array.failed,
            "status": array.status,
            "action": array.action,
            "progress": array.progress,
        }
        return CollectorResult(
            entities=self._entities(key, array.name, "mdi:harddisk-plus", False),
            readings=[problem_reading(key, issues, attrs)],
            issues=issues,
        )

    def _zpool(self, pool: ZPool) -> CollectorResult:
        key = f"zpool_{slugify(pool.name)}"
        issues: list[Issue] = []
        if pool.health != "ONLINE":
            issues.append(
                Issue(key, "zpool_unhealthy", CRITICAL, self.t("zpool_unhealthy", health=pool.health))
            )
        if pool.errors and not pool.errors.lower().startswith("no known data errors"):
            issues.append(Issue(key, "zpool_errors", CRITICAL, self.t("zpool_errors", errors=pool.errors)))
        if pool.scrub_errors:
            issues.append(
                Issue(
                    key, "zpool_scrub_errors", WARNING, self.t("zpool_scrub_errors", count=pool.scrub_errors)
                )
            )
        age_days = (self.now() - pool.scrub_end).days if pool.scrub_end else None
        if not pool.scrub_running and (age_days is None or age_days > self.cfg.scrub_max_age_days):
            issues.append(
                Issue(
                    key,
                    "zpool_scrub_outdated",
                    WARNING,
                    self.t("zpool_scrub_outdated", days=age_days if age_days is not None else "∞"),
                )
            )
        severity = self.cfg.capacity.check(pool.capacity)
        if severity:
            issues.append(
                Issue(
                    key,
                    "zpool_capacity",
                    severity,
                    self.t(
                        "zpool_capacity",
                        value=pool.capacity,
                        limit=int(self.cfg.capacity.limit_for(severity)),
                    ),
                )
            )
        attrs = {
            "kind": "pool",
            "type": "zfs",
            "name": pool.name,
            "health": pool.health,
            "size_bytes": pool.size,
            "allocated_bytes": pool.allocated,
            "errors": pool.errors,
            "last_scrub": pool.scrub_end.isoformat() if pool.scrub_end else None,
            "scrub_errors": pool.scrub_errors,
            "scrub_running": pool.scrub_running,
        }
        return CollectorResult(
            entities=self._entities(key, pool.name, "mdi:database", True),
            readings=[problem_reading(key, issues, attrs), Reading(f"{key}_capacity", pool.capacity)],
            issues=issues,
        )
