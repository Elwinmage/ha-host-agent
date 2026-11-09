"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
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
class Config:
    node_id: str
    name: str
    language: str = "en"
    log_level: str = "INFO"
    state_file: str = DEFAULT_STATE_FILE
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    smart: SmartConfig = field(default_factory=SmartConfig)
    filesystem: FilesystemConfig = field(default_factory=FilesystemConfig)


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
    )


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
    _check_keys(collectors, {"smart", "filesystem"}, "collectors")

    return Config(
        node_id=node_id,
        name=_typed(root, "name", str, hostname, "<root>"),
        language=language,
        log_level=str(_typed(root, "log_level", str, "INFO", "<root>")).upper(),
        state_file=_typed(root, "state_file", str, DEFAULT_STATE_FILE, "<root>"),
        mqtt=_parse_mqtt(root.get("mqtt")),
        smart=_parse_smart(collectors.get("smart")),
        filesystem=_parse_filesystem(collectors.get("filesystem")),
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
