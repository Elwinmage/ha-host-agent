"""Data model shared by collectors, runner and publisher."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

WARNING = "warning"
CRITICAL = "critical"
OK = "ok"

_SEVERITY_RANK = {OK: 0, WARNING: 1, CRITICAL: 2}


def severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 0)


def max_severity(severities: list[str]) -> str:
    result = OK
    for sev in severities:
        if severity_rank(sev) > severity_rank(result):
            result = sev
    return result


@dataclass(frozen=True)
class Issue:
    """A problem detected on a subject (disk, filesystem, host...)."""

    subject: str  # key of the problem entity the issue belongs to
    code: str
    severity: str
    message: str

    @property
    def id(self) -> str:
        return f"{self.subject}:{self.code}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class EntitySpec:
    """Static description of an entity, turned into an MQTT discovery component."""

    key: str
    platform: str  # sensor, binary_sensor, button
    name: str
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    entity_category: str | None = None
    icon: str | None = None
    precision: int | None = None
    # Grouping: entities of the same subject (one disk, one filesystem) share it.
    subject: str | None = None
    subject_label: str | None = None
    # "problem" entities carry issues; persistent subjects raise "missing" when gone.
    role: str = "metric"
    persistent: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntitySpec:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Reading:
    """Current value of an entity."""

    key: str
    state: Any
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectorResult:
    entities: list[EntitySpec] = field(default_factory=list)
    readings: list[Reading] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def extend(self, other: CollectorResult) -> None:
        self.entities.extend(other.entities)
        self.readings.extend(other.readings)
        self.issues.extend(other.issues)


def problem_reading(key: str, issues: list[Issue], attrs: dict[str, Any] | None = None) -> Reading:
    """Build the reading of a problem binary_sensor from its issues."""
    own = [i for i in issues if i.subject == key]
    severity = max_severity([i.severity for i in own])
    data = dict(attrs or {})
    data["severity"] = severity
    data["issues"] = [i.as_dict() for i in own]
    return Reading(key=key, state="ON" if own else "OFF", attrs=data)
