"""systemd services: failed units and services that must be running."""

from __future__ import annotations

import fnmatch
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import ServicesConfig
from host_agent.i18n import Translator
from host_agent.model import CRITICAL, WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading

KEY = "services"


def parse_failed(text: str) -> list[str]:
    """Unit names from `systemctl list-units --state=failed --plain --no-legend`."""
    units = []
    for line in text.splitlines():
        parts = line.split()
        if parts:
            # Some versions prefix the line with a status marker
            unit = parts[1] if parts[0] in ("●", "*") and len(parts) > 1 else parts[0]
            units.append(unit)
    return units


def _unit(name: str) -> str:
    return name if "." in name else f"{name}.service"


class ServicesCollector(Collector):
    name = "services"

    def __init__(
        self, cfg: ServicesConfig, translate: Translator, runner: CommandRunner = run_command
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def _systemctl(self, *args: str) -> str:
        res = self.run(["systemctl", "--no-pager", *args], 30)
        if res.returncode not in (0, 3) and not res.stdout:  # is-active returns 3 for inactive
            raise RuntimeError(f"systemctl {' '.join(args)}: {res.stderr.strip()[:200]}")
        return res.stdout

    def collect(self) -> CollectorResult:
        failed = [
            unit
            for unit in parse_failed(
                self._systemctl("list-units", "--state=failed", "--plain", "--no-legend")
            )
            if not any(fnmatch.fnmatch(unit, pattern) for pattern in self.cfg.ignore)
        ]
        watched: dict[str, str] = {}
        if self.cfg.watch:
            units = [_unit(name) for name in self.cfg.watch]
            states = self._systemctl("is-active", *units).splitlines()
            watched = {
                unit: (states[i].strip() if i < len(states) else "unknown") for i, unit in enumerate(units)
            }

        issues: list[Issue] = []
        for unit, state in watched.items():
            if state != "active":
                issues.append(
                    Issue(
                        KEY, f"service_down_{unit}", CRITICAL, self.t("service_down", unit=unit, state=state)
                    )
                )
        for unit in failed:
            if unit not in watched:
                issues.append(Issue(KEY, f"unit_failed_{unit}", WARNING, self.t("unit_failed", unit=unit)))

        common: dict[str, Any] = {"subject": KEY, "subject_label": "services"}
        attrs = {"kind": "services", "failed": failed, "watched": watched}
        return CollectorResult(
            entities=[
                EntitySpec(
                    KEY,
                    "binary_sensor",
                    self.t("services_problem"),
                    device_class="problem",
                    icon="mdi:cogs",
                    role="problem",
                    **common,
                ),
                EntitySpec(
                    f"{KEY}_failed",
                    "sensor",
                    self.t("services_failed"),
                    state_class="measurement",
                    icon="mdi:alert-circle-outline",
                    **common,
                ),
            ],
            readings=[problem_reading(KEY, issues, attrs), Reading(f"{KEY}_failed", len(failed))],
            issues=issues,
        )
