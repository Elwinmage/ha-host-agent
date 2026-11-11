"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from host_agent.i18n import SUPPORTED_LANGUAGES

DEFAULT_CONFIG_PATH = "/etc/host-agent/config.yaml"
DEFAULT_STATE_FILE = "/var/lib/host-agent/state.json"
PASSWORD_ENV = "HOST_AGENT_MQTT_PASSWORD"  # noqa: S105 - env var name, not a secret

DEFAULT_EXCLUDE_FSTYPES = [
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "fuse.gvfsd-fuse",
    "fuse.portal",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "proc",
    "pstore",
    "ramfs",
    "rpc_pipefs",
    "securityfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
]
DEFAULT_EXCLUDE_MOUNTS = ["/proc/*", "/sys/*", "/dev/*", "/run/*", "/snap/*", "/var/lib/docker/*"]


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "root"


@dataclass
class Thresholds:
    warning: float
    critical: float

    def check(self, value: float | None) -> str | None:
        if value is None:
            return None
        if value >= self.critical:
            return "critical"
        if value >= self.warning:
            return "warning"
        return None

    def limit_for(self, severity: str) -> float:
        return self.critical if severity == "critical" else self.warning


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    tls: bool = False
    ca_certs: str | None = None
    tls_insecure: bool = False
    client_id: str | None = None
    keepalive: int = 60
    discovery_prefix: str = "homeassistant"
    base_topic: str = "host_agent"


@dataclass
class SmartDevice:
    name: str
    type: str | None = None


@dataclass
class SelfTestSchedule:
    """Self-tests started by the agent itself (no smartd needed)."""

    enabled: bool = True
    short_interval_hours: int = 24  # 0 = never
    long_interval_days: int = 7  # 0 = never
    # Local time window "HH:MM-HH:MM" (may cross midnight); empty = any time
    window: str = "02:00-06:00"

    def in_window(self, now: datetime) -> bool:
        if not self.window:
            return True
        start, end = _parse_window(self.window)
        minutes = now.hour * 60 + now.minute
        if start <= end:
            return start <= minutes < end
        return minutes >= start or minutes < end


def _parse_window(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", value.strip())
    if not match:
        raise ConfigError(f"Invalid time window '{value}' (expected HH:MM-HH:MM)")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ConfigError(f"Invalid time window '{value}'")
    return h1 * 60 + m1, h2 * 60 + m2


@dataclass
class SmartConfig:
    enabled: bool = True
    interval: int = 3600
    smartctl: str = "/usr/sbin/smartctl"
    sudo: bool = False
    timeout: int = 60
    devices: list[SmartDevice] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    temperature: Thresholds = field(default_factory=lambda: Thresholds(55, 65))
    selftest_max_age_hours: int = 168
    ata_attribute_thresholds: dict[int, int] = field(default_factory=lambda: {5: 0, 187: 0, 197: 0, 198: 0})
    nvme_wear: Thresholds = field(default_factory=lambda: Thresholds(90, 100))
    ignore_error_log: bool = False
    selftest: SelfTestSchedule = field(default_factory=SelfTestSchedule)


@dataclass
class FilesystemConfig:
    enabled: bool = True
    interval: int = 300
    include_mounts: list[str] = field(default_factory=list)
    exclude_mounts: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_MOUNTS))
    exclude_fstypes: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_FSTYPES))
    required: list[str] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=lambda: Thresholds(85, 95))
    overrides: dict[str, Thresholds] = field(default_factory=dict)

    def thresholds_for(self, mount: str) -> Thresholds:
        return self.overrides.get(mount, self.thresholds)


@dataclass
class DirvishConfig:
    """dirvish vaults: last image status and age (enabled when banks are found)."""

    enabled: bool = True
    interval: int = 900
    master_conf: str = "/etc/dirvish/master.conf"
    # Empty: banks listed in master.conf
    banks: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_age_hours: int = 26
    history_days: int = 14
    # rsync exit 24 ("some files vanished before they could be transferred") is normal on live systems
    vanished_is_ok: bool = True


@dataclass
class JobSpec:
    name: str
    # Newest file matching this glob = last run (e.g. a log written by the job)
    path: str | None = None
    # Regex that must appear in the file for the run to count as successful
    success_pattern: str | None = None
    # Status pushed with `host-agent report <name>` instead of a file
    report: bool = False
    max_age_hours: int = 26

    @property
    def key(self) -> str:
        return f"job_{slugify(self.name)}"


@dataclass
class JobsConfig:
    """Freshness of arbitrary jobs (rsync pushes, cron scripts, off-site copies...)."""

    interval: int = 300
    items: list[JobSpec] = field(default_factory=list)


@dataclass
class SystemConfig:
    enabled: bool = True
    interval: int = 60
    cpu_temperature: Thresholds = field(default_factory=lambda: Thresholds(75, 85))
    memory: Thresholds = field(default_factory=lambda: Thresholds(90, 97))


@dataclass
class Config:
    node_id: str
    name: str
    language: str = "en"
    log_level: str = "INFO"
    state_file: str = DEFAULT_STATE_FILE
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    smart: SmartConfig = field(default_factory=SmartConfig)
    filesystem: FilesystemConfig = field(default_factory=FilesystemConfig)
    dirvish: DirvishConfig = field(default_factory=DirvishConfig)
    jobs: JobsConfig = field(default_factory=JobsConfig)
    system: SystemConfig = field(default_factory=SystemConfig)

    @property
    def reports_dir(self) -> Path:
        return Path(self.state_file).parent / "reports"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _section(data: Any, path: str) -> dict[str, Any]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"'{path}' must be a mapping")
    return data


def _check_keys(data: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Unknown option(s) in '{path}': {', '.join(sorted(unknown))}")


def _typed(data: dict[str, Any], key: str, kind: type | tuple[type, ...], default: Any, path: str) -> Any:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    # bool is a subclass of int: refuse it where a number is expected
    if isinstance(value, bool) and kind in (int, float, (int, float)):
        raise ConfigError(f"'{path}.{key}' must be a number")
    if not isinstance(value, kind):
        raise ConfigError(f"'{path}.{key}' has an invalid type")
    return value


def _str_list(data: dict[str, Any], key: str, default: list[str], path: str) -> list[str]:
    value = _typed(data, key, list, default, path)
    if not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{path}.{key}' must be a list of strings")
    return list(value)


def _thresholds(data: Any, default: Thresholds, path: str) -> Thresholds:
    if data is None:
        return default
    section = _section(data, path)
    _check_keys(section, {"warning", "critical"}, path)
    warning = _typed(section, "warning", (int, float), default.warning, path)
    critical = _typed(section, "critical", (int, float), default.critical, path)
    if warning > critical:
        raise ConfigError(f"'{path}': warning must be lower than or equal to critical")
    return Thresholds(warning, critical)


def _parse_mqtt(data: Any) -> MqttConfig:
    section = _section(data, "mqtt")
    allowed = set(MqttConfig.__dataclass_fields__) | {"password_file"}
    _check_keys(section, allowed, "mqtt")
    d = MqttConfig()
    cfg = MqttConfig(
        host=_typed(section, "host", str, d.host, "mqtt"),
        port=_typed(section, "port", int, d.port, "mqtt"),
        username=_typed(section, "username", str, None, "mqtt"),
        password=_typed(section, "password", str, None, "mqtt"),
        tls=_typed(section, "tls", bool, d.tls, "mqtt"),
        ca_certs=_typed(section, "ca_certs", str, None, "mqtt"),
        tls_insecure=_typed(section, "tls_insecure", bool, d.tls_insecure, "mqtt"),
        client_id=_typed(section, "client_id", str, None, "mqtt"),
        keepalive=_typed(section, "keepalive", int, d.keepalive, "mqtt"),
        discovery_prefix=_typed(section, "discovery_prefix", str, d.discovery_prefix, "mqtt"),
        base_topic=_typed(section, "base_topic", str, d.base_topic, "mqtt"),
    )
    # Secret resolution order: environment, password_file, inline password.
    env_password = os.environ.get(PASSWORD_ENV)
    password_file = _typed(section, "password_file", str, None, "mqtt")
    if env_password:
        cfg.password = env_password
    elif password_file:
        try:
            cfg.password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as err:
            raise ConfigError(f"Cannot read mqtt.password_file: {err}") from err
    return cfg


def _parse_smart(data: Any) -> SmartConfig:
    path = "collectors.smart"
    section = _section(data, path)
    _check_keys(section, set(SmartConfig.__dataclass_fields__), path)
    d = SmartConfig()

    devices: list[SmartDevice] = []
    for idx, item in enumerate(_typed(section, "devices", list, [], path)):
        if isinstance(item, str):
            devices.append(SmartDevice(name=item))
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            _check_keys(item, {"name", "type"}, f"{path}.devices[{idx}]")
            devices.append(SmartDevice(name=item["name"], type=item.get("type")))
        else:
            raise ConfigError(f"'{path}.devices[{idx}]' must be a path or a {{name, type}} mapping")

    raw_thr = _typed(section, "ata_attribute_thresholds", dict, None, path)
    if raw_thr is None:
        ata_thr = d.ata_attribute_thresholds
    else:
        try:
            ata_thr = {int(k): int(v) for k, v in raw_thr.items()}
        except (TypeError, ValueError) as err:
            raise ConfigError(
                f"'{path}.ata_attribute_thresholds' must map attribute ids to integers"
            ) from err

    interval = _typed(section, "interval", int, d.interval, path)
    if interval < 60:
        raise ConfigError(f"'{path}.interval' must be at least 60 seconds")

    return SmartConfig(
        enabled=_typed(section, "enabled", bool, d.enabled, path),
        interval=interval,
        smartctl=_typed(section, "smartctl", str, d.smartctl, path),
        sudo=_typed(section, "sudo", bool, d.sudo, path),
        timeout=_typed(section, "timeout", int, d.timeout, path),
        devices=devices,
        exclude=_str_list(section, "exclude", [], path),
        temperature=_thresholds(section.get("temperature"), d.temperature, f"{path}.temperature"),
        selftest_max_age_hours=_typed(section, "selftest_max_age_hours", int, d.selftest_max_age_hours, path),
        ata_attribute_thresholds=ata_thr,
        nvme_wear=_thresholds(section.get("nvme_wear"), d.nvme_wear, f"{path}.nvme_wear"),
        ignore_error_log=_typed(section, "ignore_error_log", bool, d.ignore_error_log, path),
        selftest=_parse_selftest(section.get("selftest"), f"{path}.selftest"),
    )


def _parse_selftest(data: Any, path: str) -> SelfTestSchedule:
    section = _section(data, path)
    _check_keys(section, set(SelfTestSchedule.__dataclass_fields__), path)
    d = SelfTestSchedule()
    schedule = SelfTestSchedule(
        enabled=_typed(section, "enabled", bool, d.enabled, path),
        short_interval_hours=_typed(section, "short_interval_hours", int, d.short_interval_hours, path),
        long_interval_days=_typed(section, "long_interval_days", int, d.long_interval_days, path),
        window=_typed(section, "window", str, d.window, path),
    )
    if schedule.short_interval_hours < 0 or schedule.long_interval_days < 0:
        raise ConfigError(f"'{path}': intervals must be positive (0 disables)")
    if schedule.window:
        _parse_window(schedule.window)
    return schedule


def _parse_filesystem(data: Any) -> FilesystemConfig:
    path = "collectors.filesystem"
    section = _section(data, path)
    _check_keys(section, set(FilesystemConfig.__dataclass_fields__), path)
    d = FilesystemConfig()
    thresholds = _thresholds(section.get("thresholds"), d.thresholds, f"{path}.thresholds")
    overrides = {
        str(mount): _thresholds(value, thresholds, f"{path}.overrides.{mount}")
        for mount, value in _section(section.get("overrides"), f"{path}.overrides").items()
    }
    interval = _typed(section, "interval", int, d.interval, path)
    if interval < 10:
        raise ConfigError(f"'{path}.interval' must be at least 10 seconds")
    return FilesystemConfig(
        enabled=_typed(section, "enabled", bool, d.enabled, path),
        interval=interval,
        include_mounts=_str_list(section, "include_mounts", [], path),
        exclude_mounts=_str_list(section, "exclude_mounts", d.exclude_mounts, path),
        exclude_fstypes=_str_list(section, "exclude_fstypes", d.exclude_fstypes, path),
        required=_str_list(section, "required", [], path),
        thresholds=thresholds,
        overrides=overrides,
    )


def _interval(section: dict[str, Any], default: int, minimum: int, path: str) -> int:
    value = _typed(section, "interval", int, default, path)
    if value < minimum:
        raise ConfigError(f"'{path}.interval' must be at least {minimum} seconds")
    return int(value)


def _positive(section: dict[str, Any], key: str, default: int, path: str) -> int:
    value = _typed(section, key, int, default, path)
    if value < 1:
        raise ConfigError(f"'{path}.{key}' must be at least 1")
    return int(value)


def _parse_dirvish(data: Any) -> DirvishConfig:
    path = "collectors.dirvish"
    section = _section(data, path)
    _check_keys(section, set(DirvishConfig.__dataclass_fields__), path)
    d = DirvishConfig()
    return DirvishConfig(
        enabled=_typed(section, "enabled", bool, d.enabled, path),
        interval=_interval(section, d.interval, 60, path),
        master_conf=_typed(section, "master_conf", str, d.master_conf, path),
        banks=_str_list(section, "banks", [], path),
        exclude=_str_list(section, "exclude", [], path),
        max_age_hours=_positive(section, "max_age_hours", d.max_age_hours, path),
        history_days=_positive(section, "history_days", d.history_days, path),
        vanished_is_ok=_typed(section, "vanished_is_ok", bool, d.vanished_is_ok, path),
    )


def _parse_jobs(data: Any) -> JobsConfig:
    path = "collectors.jobs"
    section = _section(data, path)
    _check_keys(section, {"interval", "items"}, path)
    items: list[JobSpec] = []
    seen: set[str] = set()
    for idx, raw in enumerate(_typed(section, "items", list, [], path)):
        item_path = f"{path}.items[{idx}]"
        item = _section(raw, item_path)
        _check_keys(item, {"name", "path", "success_pattern", "report", "max_age_hours"}, item_path)
        name = _typed(item, "name", str, None, item_path)
        if not name:
            raise ConfigError(f"'{item_path}.name' is required")
        spec = JobSpec(
            name=name,
            path=_typed(item, "path", str, None, item_path),
            success_pattern=_typed(item, "success_pattern", str, None, item_path),
            report=_typed(item, "report", bool, False, item_path),
            max_age_hours=_positive(item, "max_age_hours", 26, item_path),
        )
        if bool(spec.path) == spec.report:
            raise ConfigError(f"'{item_path}': set either 'path' or 'report: true'")
        if spec.success_pattern:
            try:
                re.compile(spec.success_pattern)
            except re.error as err:
                raise ConfigError(f"'{item_path}.success_pattern': {err}") from err
        if spec.key in seen:
            raise ConfigError(f"'{item_path}': duplicate job name '{name}'")
        seen.add(spec.key)
        items.append(spec)
    return JobsConfig(interval=_interval(section, JobsConfig.interval, 30, path), items=items)


def _parse_system(data: Any) -> SystemConfig:
    path = "collectors.system"
    section = _section(data, path)
    _check_keys(section, set(SystemConfig.__dataclass_fields__), path)
    d = SystemConfig()
    return SystemConfig(
        enabled=_typed(section, "enabled", bool, d.enabled, path),
        interval=_interval(section, d.interval, 10, path),
        cpu_temperature=_thresholds(
            section.get("cpu_temperature"), d.cpu_temperature, f"{path}.cpu_temperature"
        ),
        memory=_thresholds(section.get("memory"), d.memory, f"{path}.memory"),
    )


def parse_config(data: Any) -> Config:
    root = _section(data, "<root>")
    _check_keys(
        root, {"node_id", "name", "language", "log_level", "state_file", "mqtt", "collectors"}, "<root>"
    )

    hostname = socket.gethostname().split(".")[0]
    node_id = slugify(_typed(root, "node_id", str, hostname, "<root>"))
    language = _typed(root, "language", str, "en", "<root>")
    if language not in SUPPORTED_LANGUAGES:
        raise ConfigError(f"'language' must be one of: {', '.join(SUPPORTED_LANGUAGES)}")

    collectors = _section(root.get("collectors"), "collectors")
    _check_keys(collectors, {"smart", "filesystem", "dirvish", "jobs", "system"}, "collectors")

    return Config(
        node_id=node_id,
        name=_typed(root, "name", str, hostname, "<root>"),
        language=language,
        log_level=str(_typed(root, "log_level", str, "INFO", "<root>")).upper(),
        state_file=_typed(root, "state_file", str, DEFAULT_STATE_FILE, "<root>"),
        mqtt=_parse_mqtt(root.get("mqtt")),
        smart=_parse_smart(collectors.get("smart")),
        filesystem=_parse_filesystem(collectors.get("filesystem")),
        dirvish=_parse_dirvish(collectors.get("dirvish")),
        jobs=_parse_jobs(collectors.get("jobs")),
        system=_parse_system(collectors.get("system")),
    )


def load_config(path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigError(f"Cannot read configuration file {path}: {err}") from err
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Invalid YAML in {path}: {err}") from err
    return parse_config(data)
