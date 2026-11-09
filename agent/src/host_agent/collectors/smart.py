"""SMART collector based on `smartctl --json` (ATA/SAT, SCSI and NVMe)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import SmartConfig, SmartDevice, slugify
from host_agent.i18n import Translator
from host_agent.model import (
    CRITICAL,
    WARNING,
    CollectorResult,
    EntitySpec,
    Issue,
    Reading,
    problem_reading,
)

_LOGGER = logging.getLogger(__name__)

# smartctl exit status bits (see smartctl(8) "RETURN VALUES")
EXIT_CMDLINE = 1 << 0
EXIT_OPEN_FAILED = 1 << 1
EXIT_COMMAND_FAILED = 1 << 2
EXIT_DISK_FAILING = 1 << 3
EXIT_PREFAIL_BELOW = 1 << 4
EXIT_USAGE_PAST = 1 << 5
EXIT_ERROR_LOG = 1 << 6
EXIT_SELFTEST_LOG = 1 << 7

# ATA self-test status (upper nibble of the status byte)
ATA_ST_OK = 0
ATA_ST_ABORTED = 1
ATA_ST_INTERRUPTED = 2
ATA_ST_FAILURES = {3, 4, 5, 6, 7, 8}
ATA_ST_IN_PROGRESS = 15

# NVMe self-test results
NVME_ST_OK = 0
NVME_ST_FAILURES = {5, 6, 7}
NVME_ST_UNUSED = 15

NVME_CRITICAL_FLAGS = {
    0: "available spare below threshold",
    1: "temperature threshold exceeded",
    2: "reliability degraded",
    3: "media read-only",
    4: "volatile memory backup failed",
    5: "persistent memory region read-only",
}

# Minimum delay between two scheduled start attempts on the same disk (seconds)
AUTO_RETRY_DELAY = 6 * 3600

# Standard ATA self-test log stores a 16-bit power-on-hours timestamp
ATA_LIFETIME_WRAP = 65536


@dataclass
class SelfTestInfo:
    supported: bool = False
    in_progress: bool = False
    poll_minutes: int = 0
    short_minutes: int = 2
    long_minutes: int = 60
    # power-on hours since the last attempt (any outcome) of each kind
    last_attempt_hours: dict[str, int] = field(default_factory=dict)
    last_ok_hours: int | None = None  # power-on hours elapsed since last successful test
    aborted_since_ok: int = 0
    last_result: str | None = None
    failed: dict[str, Any] | None = None


@dataclass
class DiskReport:
    key: str
    label: str
    device: str
    dev_type: str | None
    protocol: str | None
    model: str | None
    serial: str | None
    firmware: str | None
    capacity_bytes: int | None
    temperature: int | None
    power_on_hours: int | None
    smart_passed: bool | None
    exit_status: int
    selftest: SelfTestInfo = field(default_factory=SelfTestInfo)
    issues: list[Issue] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "device": self.device,
            "device_type": self.dev_type,
            "protocol": self.protocol,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.firmware,
            "capacity_bytes": self.capacity_bytes,
            "power_on_hours": self.power_on_hours,
            "smart_passed": self.smart_passed,
            "smartctl_exit_status": self.exit_status,
            "selftest_supported": self.selftest.supported,
            "selftest_in_progress": self.selftest.in_progress,
            "selftest_last_result": self.selftest.last_result,
            "selftests_aborted_since_last_ok": self.selftest.aborted_since_ok,
            "selftest_last_attempt_hours": self.selftest.last_attempt_hours,
        }
        attrs.update(self.extra)
        return attrs


# --------------------------------------------------------------------------- #
# Parsing (pure functions, unit tested against real smartctl outputs)
# --------------------------------------------------------------------------- #


def disk_key(data: dict[str, Any], device: str) -> str:
    serial = data.get("serial_number")
    return f"disk_{slugify(serial) if serial else slugify(os.path.basename(device))}"


def _model(data: dict[str, Any]) -> str | None:
    return data.get("model_name") or data.get("scsi_model_name") or data.get("scsi_product")


def _temperature(data: dict[str, Any]) -> int | None:
    value = (data.get("temperature") or {}).get("current")
    # Many USB bridges report 0 when they do not pass the real value through
    if not isinstance(value, int) or value <= 0:
        return None
    return value


def _parse_ata_selftest(data: dict[str, Any], poh: int | None) -> SelfTestInfo:
    info = SelfTestInfo()
    smart_data = data.get("ata_smart_data") or {}
    info.supported = bool((smart_data.get("capabilities") or {}).get("self_tests_supported"))
    status = (smart_data.get("self_test") or {}).get("status") or {}
    if isinstance(status.get("value"), int) and status["value"] >> 4 == ATA_ST_IN_PROGRESS:
        info.in_progress = True
    polling = (smart_data.get("self_test") or {}).get("polling_minutes") or {}
    info.poll_minutes = int(polling.get("extended") or polling.get("short") or 0)
    info.short_minutes = int(polling.get("short") or info.short_minutes)
    info.long_minutes = int(polling.get("extended") or info.long_minutes)

    log = data.get("ata_smart_self_test_log") or {}
    table = ((log.get("extended") or {}).get("table")) or ((log.get("standard") or {}).get("table")) or []

    for entry in table:
        # 1/2 = offline short/extended, 129/130 = same in captive mode
        kind = {1: "short", 2: "long"}.get(int((entry.get("type") or {}).get("value") or 0) & 0x7F)
        lifetime = entry.get("lifetime_hours")
        if kind and kind not in info.last_attempt_hours and isinstance(lifetime, int) and poh is not None:
            info.last_attempt_hours[kind] = (poh - lifetime) % ATA_LIFETIME_WRAP

    latest_meaningful: dict[str, Any] | None = None
    for entry in table:
        code = int((entry.get("status") or {}).get("value") or 0) >> 4
        if code in (ATA_ST_ABORTED, ATA_ST_INTERRUPTED, ATA_ST_IN_PROGRESS):
            if info.last_ok_hours is None:
                info.aborted_since_ok += 1
            continue
        if latest_meaningful is None:
            latest_meaningful = entry
        if code == ATA_ST_OK:
            lifetime = entry.get("lifetime_hours")
            if isinstance(lifetime, int) and poh is not None:
                info.last_ok_hours = (poh - lifetime) % ATA_LIFETIME_WRAP
            elif info.last_ok_hours is None:
                info.last_ok_hours = 0
            break

    if table:
        info.last_result = (table[0].get("status") or {}).get("string")
    if latest_meaningful is not None:
        code = int((latest_meaningful.get("status") or {}).get("value") or 0) >> 4
        if code in ATA_ST_FAILURES:
            info.failed = {
                "type": (latest_meaningful.get("type") or {}).get("string", "?"),
                "status": (latest_meaningful.get("status") or {}).get("string", "?"),
            }
    return info


def _parse_nvme_selftest(data: dict[str, Any], poh: int | None) -> SelfTestInfo:
    info = SelfTestInfo()
    admin = data.get("nvme_optional_admin_commands") or {}
    log = data.get("nvme_self_test_log") or {}
    info.supported = bool(admin.get("self_test")) or bool(log)
    current = (log.get("current_self_test_operation") or {}).get("value")
    info.in_progress = isinstance(current, int) and current != 0
    info.poll_minutes = 10

    table = log.get("table") or []
    for entry in table:
        kind = {1: "short", 2: "long"}.get(int((entry.get("self_test_code") or {}).get("value") or 0))
        hours = entry.get("power_on_hours")
        if kind and kind not in info.last_attempt_hours and isinstance(hours, int) and poh is not None:
            info.last_attempt_hours[kind] = max(poh - hours, 0)

    latest_meaningful: dict[str, Any] | None = None
    for entry in table:
        result = (entry.get("self_test_result") or {}).get("value")
        if not isinstance(result, int) or result == NVME_ST_UNUSED:
            continue
        if result != NVME_ST_OK and result not in NVME_ST_FAILURES:
            # aborted variants
            if info.last_ok_hours is None:
                info.aborted_since_ok += 1
            continue
        if latest_meaningful is None:
            latest_meaningful = entry
        if result == NVME_ST_OK:
            hours = entry.get("power_on_hours")
            info.last_ok_hours = poh - hours if isinstance(hours, int) and poh is not None else 0
            break
    if table:
        info.last_result = (table[0].get("self_test_result") or {}).get("string")
    if latest_meaningful is not None:
        result = (latest_meaningful.get("self_test_result") or {}).get("value")
        if result in NVME_ST_FAILURES:
            info.failed = {
                "type": (latest_meaningful.get("self_test_code") or {}).get("string", "?"),
                "status": (latest_meaningful.get("self_test_result") or {}).get("string", "?"),
            }
    return info


def parse_smart(
    data: dict[str, Any],
    device: str,
    cfg: SmartConfig,
    t: Translator,
    returncode: int | None = None,
) -> DiskReport:
    """Turn a `smartctl --json -a` document into a DiskReport with issues."""
    dev_info = data.get("device") or {}
    smartctl = data.get("smartctl") or {}
    exit_status = int(smartctl.get("exit_status", returncode if returncode is not None else 0))
    protocol = dev_info.get("protocol")
    poh = (data.get("power_on_time") or {}).get("hours")
    model = _model(data)
    key = disk_key(data, device)
    short = os.path.basename(device)

    report = DiskReport(
        key=key,
        label=f"{model} ({short})" if model else short,
        device=device,
        dev_type=dev_info.get("type"),
        protocol=protocol,
        model=model,
        serial=data.get("serial_number"),
        firmware=data.get("firmware_version") or data.get("scsi_revision"),
        capacity_bytes=(data.get("user_capacity") or {}).get("bytes"),
        temperature=_temperature(data),
        power_on_hours=poh if isinstance(poh, int) else None,
        smart_passed=(data.get("smart_status") or {}).get("passed"),
        exit_status=exit_status,
    )
    issues: list[Issue] = []

    def add(code: str, severity: str, **kwargs: Any) -> None:
        issues.append(Issue(subject=key, code=code, severity=severity, message=t(code, **kwargs)))

    # Device could not be read at all
    if exit_status & (EXIT_CMDLINE | EXIT_OPEN_FAILED):
        messages = [m.get("string", "") for m in smartctl.get("messages") or []]
        add("smart_unavailable", WARNING, detail="; ".join(messages) or f"exit {exit_status}")
        report.issues = issues
        return report

    if report.smart_passed is False or exit_status & EXIT_DISK_FAILING:
        add("smart_failed", CRITICAL)

    if exit_status & EXIT_COMMAND_FAILED:
        add("smart_command_failed", WARNING)

    if protocol == "ATA":
        _ata_checks(data, cfg, report, add)
        report.selftest = _parse_ata_selftest(data, report.power_on_hours)
    elif protocol == "NVMe":
        _nvme_checks(data, cfg, report, add)
        report.selftest = _parse_nvme_selftest(data, report.power_on_hours)
    else:
        _scsi_checks(data, report, add)

    # Self-test evaluation (common to ATA and NVMe)
    st = report.selftest
    if st.failed:
        add("selftest_failed", CRITICAL, type=st.failed["type"], status=st.failed["status"])
    elif st.supported and cfg.selftest_max_age_hours > 0:
        if st.last_ok_hours is None:
            add("selftest_never", WARNING)
        elif st.last_ok_hours > cfg.selftest_max_age_hours:
            add("selftest_outdated", WARNING, hours=st.last_ok_hours, aborted=st.aborted_since_ok)

    # Temperature: NVMe has device-provided limits, others use configuration
    if report.temperature is not None and protocol != "NVMe":
        severity = cfg.temperature.check(report.temperature)
        if severity:
            add(
                "temperature_high",
                severity,
                value=report.temperature,
                limit=int(cfg.temperature.limit_for(severity)),
            )

    report.issues = issues
    return report


def _ata_checks(data: dict[str, Any], cfg: SmartConfig, report: DiskReport, add: Any) -> None:
    flagged_attr = False
    watched: dict[str, Any] = {}
    for attr in (data.get("ata_smart_attributes") or {}).get("table") or []:
        name = attr.get("name", f"attr_{attr.get('id')}")
        when_failed = attr.get("when_failed") or ""
        if when_failed == "now":
            add("attr_failing", CRITICAL, name=name)
            flagged_attr = True
        elif when_failed == "past":
            add("attr_failed_past", WARNING, name=name)
            flagged_attr = True
        attr_id = attr.get("id")
        if attr_id in cfg.ata_attribute_thresholds:
            raw = (attr.get("raw") or {}).get("value")
            watched[name] = raw
            limit = cfg.ata_attribute_thresholds[attr_id]
            if isinstance(raw, int) and raw > limit:
                add("attr_nonzero", WARNING, name=name, value=raw, max=limit)
    report.extra["watched_attributes"] = watched

    exit_status = report.exit_status
    if not flagged_attr and exit_status & EXIT_PREFAIL_BELOW:
        add("smart_exit_flag", CRITICAL, detail="prefail attribute below threshold")
    if not flagged_attr and exit_status & EXIT_USAGE_PAST:
        add("smart_exit_flag", WARNING, detail="attribute below threshold in the past")

    error_count = (((data.get("ata_smart_error_log") or {}).get("summary") or {}).get("count")) or 0
    report.extra["error_log_count"] = error_count
    if not cfg.ignore_error_log and (error_count or exit_status & EXIT_ERROR_LOG):
        add("error_log", WARNING, count=error_count or "?")


def _nvme_checks(data: dict[str, Any], cfg: SmartConfig, report: DiskReport, add: Any) -> None:
    log = data.get("nvme_smart_health_information_log") or {}
    critical_warning = int(log.get("critical_warning") or 0)
    if critical_warning:
        flags = [label for bit, label in NVME_CRITICAL_FLAGS.items() if critical_warning & (1 << bit)]
        add("nvme_critical_warning", CRITICAL, flags=", ".join(flags) or hex(critical_warning))

    media_errors = int(log.get("media_errors") or 0)
    if media_errors:
        add("nvme_media_errors", CRITICAL, value=media_errors)

    spare = log.get("available_spare")
    spare_thr = log.get("available_spare_threshold")
    if isinstance(spare, int) and isinstance(spare_thr, int) and spare < spare_thr:
        add("nvme_spare_low", CRITICAL, value=spare, limit=spare_thr)

    used = log.get("percentage_used")
    severity = cfg.nvme_wear.check(used) if isinstance(used, int) else None
    if severity:
        add("nvme_wear", severity, value=used, limit=int(cfg.nvme_wear.limit_for(severity)))

    temp_limits = data.get("temperature") or {}
    if report.temperature is not None:
        crit = temp_limits.get("critical_limit_max")
        warn = temp_limits.get("op_limit_max")
        if isinstance(crit, int) and crit > 0 and report.temperature >= crit:
            add("temperature_high", CRITICAL, value=report.temperature, limit=crit)
        elif isinstance(warn, int) and warn > 0 and report.temperature >= warn:
            add("temperature_high", WARNING, value=report.temperature, limit=warn)

    report.extra.update(
        {
            "percentage_used": used,
            "available_spare": spare,
            "media_errors": media_errors,
            "error_log_entries": log.get("num_err_log_entries"),
            "unsafe_shutdowns": log.get("unsafe_shutdowns"),
            "data_units_written": log.get("data_units_written"),
        }
    )


def _scsi_checks(data: dict[str, Any], report: DiskReport, add: Any) -> None:
    defects = data.get("scsi_grown_defect_list")
    if isinstance(defects, int) and defects > 0:
        add("scsi_grown_defects", WARNING, value=defects)
    report.extra["grown_defects"] = defects


# --------------------------------------------------------------------------- #
# Collector
# --------------------------------------------------------------------------- #


class SmartCollector(Collector):
    name = "smart"

    def __init__(
        self,
        cfg: SmartConfig,
        translate: Translator,
        runner: CommandRunner = run_command,
        clock: Any = time.monotonic,
        local_now: Callable[[], datetime] = datetime.now,
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg
        self.clock = clock
        self.local_now = local_now
        # Reports kept while a self-test runs, so we do not poll the drive meanwhile
        self._held: dict[str, tuple[float, DiskReport]] = {}
        # Manual requests from the HA buttons: disk key -> "short" | "long"
        self._manual: dict[str, str] = {}
        # Last scheduled start attempt per disk (monotonic), to avoid retry loops
        self._last_auto_start: dict[str, float] = {}

    def on_command(self, key: str) -> bool:
        for kind in ("short", "long"):
            suffix = f"_selftest_{kind}"
            if key.startswith("disk_") and key.endswith(suffix):
                self._manual[key[: -len(suffix)]] = kind
                return True
        return False

    def _due_kind(self, st: SelfTestInfo) -> str | None:
        sched = self.cfg.selftest
        attempts = st.last_attempt_hours
        if sched.long_interval_days > 0:
            last = attempts.get("long")
            if last is None or last >= sched.long_interval_days * 24:
                return "long"
        if sched.short_interval_hours > 0:
            # a long test covers the short one
            last_any = min((h for h in attempts.values()), default=None)
            if last_any is None or last_any >= sched.short_interval_hours:
                return "short"
        return None

    def _maybe_start_selftest(self, device: SmartDevice, report: DiskReport, now: float) -> None:
        st = report.selftest
        if not st.supported or st.in_progress:
            return
        kind = self._manual.pop(report.key, None)
        manual = kind is not None
        if kind is None:
            sched = self.cfg.selftest
            if not sched.enabled or not sched.in_window(self.local_now()):
                return
            if now - self._last_auto_start.get(report.key, -AUTO_RETRY_DELAY) < AUTO_RETRY_DELAY:
                return
            kind = self._due_kind(st)
            if kind is None:
                return
            self._last_auto_start[report.key] = now

        args = ["-t", kind]
        if device.type:
            args += ["-d", device.type]
        res = self.run(self._cmd(*args, device.name), self.cfg.timeout)
        if res.returncode & (EXIT_CMDLINE | EXIT_OPEN_FAILED | EXIT_COMMAND_FAILED):
            detail = (res.stderr or res.stdout).strip().splitlines()[-1:] or [f"exit {res.returncode}"]
            _LOGGER.warning("Cannot start %s self-test on %s: %s", kind, device.name, detail[0])
            report.extra["selftest_start_error"] = detail[0]
            return
        minutes = st.long_minutes if kind == "long" else st.short_minutes
        _LOGGER.info(
            "Started %s self-test on %s (%s, ~%d min)",
            kind,
            device.name,
            "manual" if manual else "scheduled",
            minutes,
        )
        st.in_progress = True
        report.extra["selftest_started"] = {
            "kind": kind,
            "manual": manual,
            "at": self.local_now().isoformat(timespec="seconds"),
        }
        # Leave the drive alone until the test should be over
        self._held[device.name] = (now + minutes * 60 + 120, report)

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def _cmd(self, *args: str) -> list[str]:
        base = [self.cfg.smartctl]
        if self.cfg.sudo and os.geteuid() != 0:
            base = ["sudo", "-n", self.cfg.smartctl]
        return [*base, *args]

    def discover(self) -> list[SmartDevice]:
        if self.cfg.devices:
            devices = list(self.cfg.devices)
        else:
            res = self.run(self._cmd("--scan-open", "--json=c"), self.cfg.timeout)
            try:
                payload = json.loads(res.stdout or "{}")
            except json.JSONDecodeError as err:
                raise RuntimeError(f"smartctl --scan-open: invalid JSON ({res.stderr.strip()})") from err
            devices = [
                SmartDevice(name=d["name"], type=d.get("type"))
                for d in payload.get("devices") or []
                if d.get("name")
            ]
        return [d for d in devices if d.name not in self.cfg.exclude]

    def query(self, device: SmartDevice) -> tuple[dict[str, Any], int]:
        args = ["--json=c", "-a"]
        if device.type:
            args += ["-d", device.type]
        res = self.run(self._cmd(*args, device.name), self.cfg.timeout)
        try:
            return json.loads(res.stdout), res.returncode
        except json.JSONDecodeError:
            detail = (res.stderr or res.stdout or "").strip()[:200] or f"exit {res.returncode}"
            return {
                "smartctl": {
                    "exit_status": res.returncode | EXIT_OPEN_FAILED,
                    "messages": [{"string": detail}],
                }
            }, res.returncode

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        seen: set[str] = set()
        now = self.clock()
        for device in self.discover():
            held = self._held.get(device.name)
            if held and now < held[0]:
                report = held[1]
                _LOGGER.debug("Self-test running on %s, reusing previous data", device.name)
            else:
                data, rc = self.query(device)
                report = parse_smart(data, device.name, self.cfg, self.t, rc)
                if report.serial and report.serial in self.cfg.exclude:
                    continue
                if report.selftest.in_progress:
                    wait = max(report.selftest.poll_minutes, 2) * 60 + 60
                    self._held[device.name] = (now + wait, report)
                else:
                    self._held.pop(device.name, None)
                    self._maybe_start_selftest(device, report, now)
            if report.key in seen:  # same disk reachable through two paths
                continue
            seen.add(report.key)
            result.extend(self._to_result(report))
        return result

    def _to_result(self, report: DiskReport) -> CollectorResult:
        key, label = report.key, report.label
        common: dict[str, Any] = {"subject": key, "subject_label": label, "persistent": True}
        entities = [
            EntitySpec(
                key=key,
                platform="binary_sensor",
                name=self.t("disk_health", label=label),
                device_class="problem",
                icon="mdi:harddisk",
                role="problem",
                **common,
            ),
            EntitySpec(
                key=f"{key}_temperature",
                platform="sensor",
                name=self.t("disk_temperature", label=label),
                device_class="temperature",
                unit="°C",
                state_class="measurement",
                **common,
            ),
        ]
        readings = [
            problem_reading(key, report.issues, report.attributes()),
            Reading(key=f"{key}_temperature", state=report.temperature),
        ]
        if report.selftest.supported:
            entities.append(
                EntitySpec(
                    key=f"{key}_selftest_age",
                    platform="sensor",
                    name=self.t("disk_selftest_age", label=label),
                    device_class="duration",
                    unit="h",
                    state_class="measurement",
                    entity_category="diagnostic",
                    icon="mdi:clipboard-check-outline",
                    **common,
                )
            )
            readings.append(Reading(key=f"{key}_selftest_age", state=report.selftest.last_ok_hours))
            for kind, icon in (("short", "mdi:play-circle-outline"), ("long", "mdi:play-speed")):
                entities.append(
                    EntitySpec(
                        key=f"{key}_selftest_{kind}",
                        platform="button",
                        name=self.t(f"disk_selftest_{kind}", label=label),
                        entity_category="config",
                        icon=icon,
                        **common,
                    )
                )
        return CollectorResult(entities=entities, readings=readings, issues=list(report.issues))
