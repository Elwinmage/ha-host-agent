"""GPU collector: NVIDIA through nvidia-smi, AMD through the amdgpu sysfs interface."""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import GpuConfig, slugify
from host_agent.i18n import Translator
from host_agent.model import WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading

NVIDIA_FIELDS = (
    "index",
    "pci.bus_id",
    "name",
    "temperature.gpu",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "power.draw",
    "power.limit",
    "fan.speed",
    "{reasons}.active",
    "pstate",
    "driver_version",
)
# Renamed in recent drivers; the old name is tried when the new one is refused
REASON_FIELDS = ("clocks_event_reasons", "clocks_throttle_reasons")

# nvmlClocksEventReasons bits
NVIDIA_REASONS = {
    0x1: "idle",
    0x2: "applications_clocks",
    0x4: "sw_power_cap",
    0x8: "hw_slowdown",
    0x10: "sync_boost",
    0x20: "sw_thermal_slowdown",
    0x40: "hw_thermal_slowdown",
    0x80: "hw_power_brake",
    0x100: "display_clock",
}
# Reasons reported as a problem (power capping under load is normal)
NVIDIA_BAD_REASONS = 0x8 | 0x20 | 0x40 | 0x80


@dataclass
class GpuReading:
    key: str
    name: str
    vendor: str
    temperature: float | None = None
    utilization: float | None = None
    memory_used: int | None = None  # bytes
    memory_total: int | None = None
    power: float | None = None  # W
    power_limit: float | None = None
    fan: float | None = None  # % (NVIDIA) or rpm (AMD)
    reasons: list[str] = field(default_factory=list)
    bad_reasons: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def memory_pct(self) -> float | None:
        if self.memory_used is None or not self.memory_total:
            return None
        return round(self.memory_used * 100 / self.memory_total, 1)


def _number(value: str) -> float | None:
    value = value.strip()
    if not value or value.startswith("[") or value.upper() in ("N/A", "NOT SUPPORTED"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_nvidia_csv(text: str) -> list[GpuReading]:
    """Output of `nvidia-smi --query-gpu=<NVIDIA_FIELDS> --format=csv,noheader,nounits`."""
    gpus = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < len(NVIDIA_FIELDS):
            continue
        (index, bus_id, name, temp, util, mem_used, mem_total, power, limit, fan, reasons, pstate, driver) = (
            cols[: len(NVIDIA_FIELDS)]
        )
        mask = 0
        with contextlib.suppress(ValueError):
            mask = int(reasons, 16)
        mem_used_mib, mem_total_mib = _number(mem_used), _number(mem_total)
        gpus.append(
            GpuReading(
                key=f"gpu_{slugify(bus_id)}",
                name=name,
                vendor="nvidia",
                temperature=_number(temp),
                utilization=_number(util),
                memory_used=int(mem_used_mib * 2**20) if mem_used_mib is not None else None,
                memory_total=int(mem_total_mib * 2**20) if mem_total_mib is not None else None,
                power=_number(power),
                power_limit=_number(limit),
                fan=_number(fan),
                reasons=[label for bit, label in NVIDIA_REASONS.items() if mask & bit],
                bad_reasons=[
                    label for bit, label in NVIDIA_REASONS.items() if mask & bit & NVIDIA_BAD_REASONS
                ],
                extra={"index": index, "pci_bus": bus_id, "pstate": pstate, "driver": driver},
            )
        )
    return gpus


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    value = _read(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def read_amd(root: str = "/") -> list[GpuReading]:
    """amdgpu cards from /sys/class/drm/cardN/device."""
    gpus = []
    drm = Path(root, "sys/class/drm")
    for card in sorted(drm.glob("card[0-9]*")):
        if "-" in card.name:  # connectors (card0-HDMI-A-1)
            continue
        device = card / "device"
        driver = device / "driver"
        try:
            if os.path.basename(os.readlink(driver)) != "amdgpu":
                continue
        except OSError:
            continue
        try:
            bus = os.path.basename(os.path.realpath(device))
        except OSError:
            bus = card.name
        temps: dict[str, float] = {}
        power = power_cap = fan = None
        for hwmon in sorted(device.glob("hwmon/hwmon*")):
            for item in sorted(hwmon.glob("temp*_input")):
                value = _read_int(item)
                if value is None:
                    continue
                label = _read(item.with_name(item.name.replace("_input", "_label"))) or item.name
                temps[label] = value / 1000
            for name in ("power1_average", "power1_input"):
                value = _read_int(hwmon / name)
                if value is not None:
                    power = round(value / 1e6, 1)
                    break
            cap = _read_int(hwmon / "power1_cap")
            power_cap = round(cap / 1e6, 1) if cap else power_cap
            fan = _read_int(hwmon / "fan1_input") or fan
        name = _read(device / "product_name") or f"AMD GPU {bus}"
        gpus.append(
            GpuReading(
                key=f"gpu_{slugify(bus)}",
                name=name,
                vendor="amd",
                # Edge temperature, comparable to NVIDIA's GPU temperature: the junction
                # (hotspot, up to 110 °C by design) stays available in the attributes
                temperature=temps.get("edge") or (max(temps.values()) if temps else None),
                utilization=_read_int(device / "gpu_busy_percent"),
                memory_used=_read_int(device / "mem_info_vram_used"),
                memory_total=_read_int(device / "mem_info_vram_total"),
                power=power,
                power_limit=power_cap,
                fan=fan,
                extra={"pci_bus": bus, "temperatures": temps, "fan_unit": "rpm"},
            )
        )
    return gpus


class GpuCollector(Collector):
    name = "gpu"

    def __init__(
        self,
        cfg: GpuConfig,
        translate: Translator,
        runner: CommandRunner = run_command,
        root: str = "/",
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg
        self.root = root
        self._reason_field: str | None = None

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def nvidia(self) -> list[GpuReading]:
        if not (shutil.which(self.cfg.nvidia_smi) or os.path.isfile(self.cfg.nvidia_smi)):
            return []
        for reason_field in [self._reason_field] if self._reason_field else REASON_FIELDS:
            query = ",".join(f.format(reasons=reason_field) for f in NVIDIA_FIELDS)
            res = self.run([self.cfg.nvidia_smi, f"--query-gpu={query}", "--format=csv,noheader,nounits"], 30)
            if res.returncode == 0:
                self._reason_field = reason_field
                return parse_nvidia_csv(res.stdout)
        raise RuntimeError(f"nvidia-smi failed: {(res.stderr or res.stdout).strip()[:200]}")

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        for gpu in self.nvidia() + read_amd(self.root):
            result.extend(self._gpu(gpu))
        return result

    def _gpu(self, gpu: GpuReading) -> CollectorResult:
        key = gpu.key
        common: dict[str, Any] = {"subject": key, "subject_label": gpu.name, "persistent": True}
        entities = [
            EntitySpec(
                key,
                "binary_sensor",
                self.t("gpu_problem", name=gpu.name),
                device_class="problem",
                icon="mdi:expansion-card",
                role="problem",
                **common,
            ),
            EntitySpec(
                f"{key}_temperature",
                "sensor",
                self.t("gpu_temperature", name=gpu.name),
                device_class="temperature",
                unit="°C",
                state_class="measurement",
                **common,
            ),
            EntitySpec(
                f"{key}_utilization",
                "sensor",
                self.t("gpu_utilization", name=gpu.name),
                unit="%",
                state_class="measurement",
                icon="mdi:gauge",
                **common,
            ),
            EntitySpec(
                f"{key}_memory",
                "sensor",
                self.t("gpu_memory", name=gpu.name),
                unit="%",
                state_class="measurement",
                icon="mdi:memory",
                precision=1,
                **common,
            ),
            EntitySpec(
                f"{key}_power",
                "sensor",
                self.t("gpu_power", name=gpu.name),
                device_class="power",
                unit="W",
                state_class="measurement",
                precision=1,
                **common,
            ),
        ]
        issues: list[Issue] = []
        severity = self.cfg.temperature.check(gpu.temperature)
        if severity:
            issues.append(
                Issue(
                    key,
                    "gpu_temperature_high",
                    severity,
                    self.t(
                        "gpu_temperature_high",
                        value=gpu.temperature,
                        limit=int(self.cfg.temperature.limit_for(severity)),
                    ),
                )
            )
        severity = self.cfg.memory.check(gpu.memory_pct)
        if severity:
            issues.append(
                Issue(
                    key,
                    "gpu_memory_high",
                    severity,
                    self.t(
                        "gpu_memory_high",
                        value=gpu.memory_pct,
                        limit=int(self.cfg.memory.limit_for(severity)),
                    ),
                )
            )
        if gpu.bad_reasons and self.cfg.throttle_is_warning:
            issues.append(
                Issue(
                    key, "gpu_throttled", WARNING, self.t("gpu_throttled", reasons=", ".join(gpu.bad_reasons))
                )
            )

        attrs = {
            "kind": "gpu",
            "name": gpu.name,
            "vendor": gpu.vendor,
            "memory_used_bytes": gpu.memory_used,
            "memory_total_bytes": gpu.memory_total,
            "power_limit_w": gpu.power_limit,
            "fan": gpu.fan,
            "throttle_reasons": gpu.reasons,
            **gpu.extra,
        }
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(key, issues, attrs),
                Reading(f"{key}_temperature", gpu.temperature),
                Reading(f"{key}_utilization", gpu.utilization),
                Reading(f"{key}_memory", gpu.memory_pct),
                Reading(f"{key}_power", gpu.power),
            ],
            issues=issues,
        )
