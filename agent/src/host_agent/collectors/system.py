"""System collector: CPU temperature, load, memory, boot time, pending reboot."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import SystemConfig
from host_agent.i18n import Translator
from host_agent.model import CRITICAL, WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading

KEY = "system"
# CPU sensors preferred, in order (hwmon chip names)
CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "cpu-thermal")


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


# Preferred sensor label per chip: the one the CPU itself regulates on.
# AMD: Tdie is the real die temperature (Tctl carries a +10/+20 °C offset on some
# Ryzen models); per-CCD sensors are left aside. Intel: the package sensor.
PREFERRED_LABELS = {
    "k10temp": ("Tdie", "Tctl"),
    "zenpower": ("Tdie", "Tctl"),
    "coretemp": ("Package id",),
}


def _chip_temperature(hwmon: Path, chip: str) -> float | None:
    readings: dict[str, float] = {}
    for item in sorted(hwmon.glob("temp*_input")):
        raw = _read(item)
        if raw and raw.lstrip("-").isdigit():
            label = _read(item.with_name(item.name.replace("_input", "_label"))) or item.name
            readings[label] = int(raw) / 1000
    if not readings:
        return None
    for wanted in PREFERRED_LABELS.get(chip, ()):
        matches = [value for label, value in readings.items() if label.startswith(wanted)]
        if matches:
            return max(matches)
    return max(readings.values())


def cpu_temperature(root: str = "/") -> float | None:
    """CPU temperature (°C) from hwmon (highest package), falling back to thermal zones."""
    base = Path(root)
    best: float | None = None
    for hwmon in sorted((base / "sys/class/hwmon").glob("hwmon*")):
        chip = _read(hwmon / "name") or ""
        if chip not in CPU_HWMON:
            continue
        value = _chip_temperature(hwmon, chip)
        if value is not None:
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


# Raspberry Pi firmware "get_throttled" bits
PI_FLAGS = {
    0: "under_voltage",
    1: "freq_capped",
    2: "throttled",
    3: "soft_temp_limit",
    16: "under_voltage_occurred",
    17: "freq_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temp_limit_occurred",
}


def pi_throttled(root: str, run: CommandRunner) -> int | None:
    """Raspberry Pi throttling flags (sysfs on recent kernels, vcgencmd otherwise); None elsewhere."""
    model = _read(Path(root) / "proc/device-tree/model") or ""
    if "raspberry" not in model.lower():
        return None
    raw = _read(Path(root) / "sys/devices/platform/soc/soc:firmware/get_throttled")
    if raw is None and shutil.which("vcgencmd"):
        res = run(["vcgencmd", "get_throttled"], 10)
        raw = res.stdout.strip().split("=", 1)[-1] if res.returncode == 0 else None
    try:
        return int(raw, 16) if raw else None
    except ValueError:
        return None


def ntp_synchronized(run: CommandRunner) -> bool | None:
    if not shutil.which("timedatectl"):
        return None
    res = run(["timedatectl", "show", "--property=NTPSynchronized", "--value"], 10)
    value = res.stdout.strip().lower()
    return {"yes": True, "no": False}.get(value) if res.returncode == 0 else None


class SystemCollector(Collector):
    name = "system"

    def __init__(
        self,
        cfg: SystemConfig,
        translate: Translator,
        root: str = "/",
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        runner: CommandRunner = run_command,
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg
        self.root = root
        self.now = now

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def _pi_issues(self, flags: int) -> list[Issue]:
        issues = []
        if flags & 0x1:
            issues.append(Issue(KEY, "pi_undervoltage", CRITICAL, self.t("pi_undervoltage")))
        elif flags & 0x10000:
            issues.append(Issue(KEY, "pi_undervoltage_past", WARNING, self.t("pi_undervoltage_past")))
        if flags & (0x4 | 0x8):
            issues.append(Issue(KEY, "pi_throttled", WARNING, self.t("pi_throttled")))
        return issues

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

        attrs: dict[str, Any] = {
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
        if self.cfg.power:
            throttled = pi_throttled(self.root, self.run)
            if throttled is not None:
                attrs["pi_throttled"] = hex(throttled)
                attrs["pi_flags"] = [label for bit, label in PI_FLAGS.items() if throttled & (1 << bit)]
                issues.extend(self._pi_issues(throttled))
        if self.cfg.ntp:
            synced = ntp_synchronized(self.run)
            attrs["ntp_synchronized"] = synced
            if synced is False:
                issues.append(Issue(KEY, "ntp_unsynchronized", WARNING, self.t("ntp_unsynchronized")))
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
