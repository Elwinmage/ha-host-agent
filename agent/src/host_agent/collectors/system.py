"""System collector: CPU temperature, load, memory, boot time, pending reboot."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector
from host_agent.config import SystemConfig
from host_agent.i18n import Translator
from host_agent.model import CollectorResult, EntitySpec, Issue, Reading, problem_reading

KEY = "system"
# CPU sensors preferred, in order (hwmon chip names)
CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "cpu-thermal")


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def cpu_temperature(root: str = "/") -> float | None:
    """Highest CPU temperature (°C) from hwmon, falling back to thermal zones."""
    base = Path(root)
    best: float | None = None
    for hwmon in sorted((base / "sys/class/hwmon").glob("hwmon*")):
        if _read(hwmon / "name") not in CPU_HWMON:
            continue
        for item in hwmon.glob("temp*_input"):
            raw = _read(item)
            if raw and raw.lstrip("-").isdigit():
                value = int(raw) / 1000
                best = value if best is None else max(best, value)
    if best is not None:
        return round(best, 1)
    for zone in sorted((base / "sys/class/thermal").glob("thermal_zone*")):
        kind = (_read(zone / "type") or "").lower()
        if "cpu" in kind or "soc" in kind or "x86_pkg" in kind:
            raw = _read(zone / "temp")
            if raw and raw.lstrip("-").isdigit():
                return round(int(raw) / 1000, 1)
    return None


def memory_used_pct(root: str = "/") -> float | None:
    info: dict[str, int] = {}
    for line in (_read(Path(root) / "proc/meminfo") or "").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            info[key] = int(parts[0])
    total, available = info.get("MemTotal"), info.get("MemAvailable")
    if not total or available is None:
        return None
    return round((total - available) * 100 / total, 1)


def boot_time(now: datetime, root: str = "/") -> datetime | None:
    raw = (_read(Path(root) / "proc/uptime") or "").split()
    try:
        return (now - timedelta(seconds=float(raw[0]))).replace(microsecond=0)
    except (IndexError, ValueError):
        return None


def load_1(root: str = "/") -> float | None:
    raw = (_read(Path(root) / "proc/loadavg") or "").split()
    try:
        return float(raw[0])
    except (IndexError, ValueError):
        return None


class SystemCollector(Collector):
    name = "system"

    def __init__(
        self,
        cfg: SystemConfig,
        translate: Translator,
        root: str = "/",
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        super().__init__(translate)
        self.cfg = cfg
        self.root = root
        self.now = now

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def collect(self) -> CollectorResult:
        common: dict[str, Any] = {"subject": KEY, "subject_label": "system"}
        entities = [
            EntitySpec(
                KEY,
                "binary_sensor",
                self.t("system_problem"),
                device_class="problem",
                icon="mdi:chip",
                role="problem",
                **common,
            ),
            EntitySpec(
                f"{KEY}_cpu_temperature",
                "sensor",
                self.t("system_cpu_temperature"),
                device_class="temperature",
                unit="°C",
                state_class="measurement",
                precision=1,
                **common,
            ),
            EntitySpec(
                f"{KEY}_load",
                "sensor",
                self.t("system_load"),
                state_class="measurement",
                icon="mdi:gauge",
                precision=2,
                **common,
            ),
            EntitySpec(
                f"{KEY}_memory",
                "sensor",
                self.t("system_memory"),
                unit="%",
                state_class="measurement",
                icon="mdi:memory",
                precision=1,
                **common,
            ),
            EntitySpec(
                f"{KEY}_boot",
                "sensor",
                self.t("system_boot"),
                device_class="timestamp",
                entity_category="diagnostic",
                **common,
            ),
        ]
        now = self.now()
        temperature = cpu_temperature(self.root)
        memory = memory_used_pct(self.root)
        boot = boot_time(now, self.root)
        issues: list[Issue] = []
        severity = self.cfg.cpu_temperature.check(temperature)
        if severity:
            issues.append(
                Issue(
                    KEY,
                    "cpu_temperature_high",
                    severity,
                    self.t(
                        "cpu_temperature_high",
                        value=temperature,
                        limit=int(self.cfg.cpu_temperature.limit_for(severity)),
                    ),
                )
            )
        severity = self.cfg.memory.check(memory)
        if severity:
            issues.append(
                Issue(
                    KEY,
                    "memory_high",
                    severity,
                    self.t("memory_high", value=memory, limit=int(self.cfg.memory.limit_for(severity))),
                )
            )

        attrs = {
            "kind": "system",
            "cpu_count": os.cpu_count(),
            "reboot_required": Path(self.root, "run/reboot-required").exists()
            or Path(self.root, "var/run/reboot-required").exists(),
            "os": next(
                (
                    line.split("=", 1)[1].strip('"')
                    for line in (_read(Path(self.root) / "etc/os-release") or "").splitlines()
                    if line.startswith("PRETTY_NAME=")
                ),
                None,
            ),
            "kernel": os.uname().release,
            "model": _read(Path(self.root) / "proc/device-tree/model")
            or _read(Path(self.root) / "sys/class/dmi/id/product_name"),
        }
        if isinstance(attrs["model"], str):
            attrs["model"] = attrs["model"].strip("\x00")
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(KEY, issues, attrs),
                Reading(f"{KEY}_cpu_temperature", temperature),
                Reading(f"{KEY}_load", load_1(self.root)),
                Reading(f"{KEY}_memory", memory),
                Reading(f"{KEY}_boot", boot.isoformat() if boot else None),
            ],
            issues=issues,
        )
