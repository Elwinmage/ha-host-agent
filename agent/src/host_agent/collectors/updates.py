"""Pending package updates (apt, dnf), without root and without refreshing the indexes."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import UpdatesConfig
from host_agent.i18n import Translator
from host_agent.model import WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading

KEY = "updates"
# Package names listed in the attributes (the counts are always exact)
MAX_LISTED = 50


@dataclass
class Pending:
    manager: str
    packages: list[str] = field(default_factory=list)
    security: list[str] = field(default_factory=list)


def parse_apt_simulation(text: str) -> Pending:
    """`apt-get -s dist-upgrade`: 'Inst pkg [old] (new Debian:13.1/stable-security [amd64])'."""
    pending = Pending("apt")
    for line in text.splitlines():
        if not line.startswith("Inst "):
            continue
        name = line.split()[1]
        pending.packages.append(name)
        origin = line[line.find("(") :] if "(" in line else ""
        if re.search(r"[-/]security", origin, re.I):
            pending.security.append(name)
    return pending


def parse_dnf(check_update: str, security: str) -> Pending:
    """`dnf -q check-update` (exit 100 when updates) and `dnf -q updateinfo list --security`."""
    pending = Pending("dnf")
    for line in check_update.splitlines():
        parts = line.split()
        if len(parts) == 3 and "." in parts[0] and not line.startswith(" "):
            pending.packages.append(parts[0].rsplit(".", 1)[0])
        elif line.startswith("Obsoleting"):
            break
    for line in security.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            pending.security.append(parts[-1])
    return pending


class UpdatesCollector(Collector):
    name = "updates"

    def __init__(
        self, cfg: UpdatesConfig, translate: Translator, runner: CommandRunner = run_command
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def pending(self) -> Pending | None:
        if shutil.which("apt-get"):
            res = self.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "dist-upgrade"], 300)
            if res.returncode != 0:
                raise RuntimeError(f"apt-get -s: {res.stderr.strip()[:200]}")
            return parse_apt_simulation(res.stdout)
        if shutil.which("dnf"):
            res = self.run(["dnf", "-q", "-C", "check-update"], 300)
            if res.returncode not in (0, 100):
                raise RuntimeError(f"dnf check-update: {res.stderr.strip()[:200]}")
            sec = self.run(["dnf", "-q", "-C", "updateinfo", "list", "--security"], 300)
            return parse_dnf(res.stdout, sec.stdout if sec.returncode == 0 else "")
        return None

    def collect(self) -> CollectorResult:
        pending = self.pending()
        if pending is None:
            return CollectorResult()
        issues: list[Issue] = []
        if pending.security and self.cfg.security_is_warning:
            issues.append(
                Issue(
                    KEY, "security_updates", WARNING, self.t("security_updates", count=len(pending.security))
                )
            )
        common: dict[str, Any] = {"subject": KEY, "subject_label": "updates"}
        attrs = {
            "kind": "updates",
            "manager": pending.manager,
            "security_count": len(pending.security),
            "packages": pending.packages[:MAX_LISTED],
            "security_packages": pending.security[:MAX_LISTED],
        }
        return CollectorResult(
            entities=[
                EntitySpec(
                    KEY,
                    "binary_sensor",
                    self.t("updates_problem"),
                    device_class="problem",
                    icon="mdi:package-up",
                    role="problem",
                    **common,
                ),
                EntitySpec(
                    f"{KEY}_pending",
                    "sensor",
                    self.t("updates_pending"),
                    state_class="measurement",
                    icon="mdi:package-up",
                    **common,
                ),
                EntitySpec(
                    f"{KEY}_security",
                    "sensor",
                    self.t("updates_security"),
                    state_class="measurement",
                    icon="mdi:shield-alert",
                    **common,
                ),
            ],
            readings=[
                problem_reading(KEY, issues, attrs),
                Reading(f"{KEY}_pending", len(pending.packages)),
                Reading(f"{KEY}_security", len(pending.security)),
            ],
            issues=issues,
        )
