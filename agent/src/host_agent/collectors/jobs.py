"""Jobs collector: freshness of arbitrary jobs, from their files or pushed reports."""

from __future__ import annotations

import glob
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector
from host_agent.config import JobsConfig, JobSpec, slugify
from host_agent.i18n import Translator
from host_agent.model import CRITICAL, WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading

# Files scanned backwards to find the last successful run
MAX_FILES = 30
# Bytes read at the end of a file to look for the success pattern
TAIL_BYTES = 256 * 1024


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Run:
    when: datetime
    ok: bool
    source: str
    message: str | None = None


def _tail(path: str) -> str:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        return fh.read().decode("utf-8", errors="replace")


def file_runs(spec: JobSpec) -> tuple[Run | None, Run | None]:
    """(last run, last successful run) from the files matching `spec.path`."""
    if not spec.path:
        return None, None
    files = []
    for path in glob.glob(spec.path):
        try:
            files.append((os.stat(path).st_mtime, path))
        except OSError:
            continue
    files.sort(reverse=True)
    pattern = re.compile(spec.success_pattern) if spec.success_pattern else None
    last: Run | None = None
    for mtime, path in files[:MAX_FILES]:
        ok = True
        if pattern:
            try:
                ok = bool(pattern.search(_tail(path)))
            except OSError:
                ok = False
        run = Run(datetime.fromtimestamp(mtime).astimezone(), ok, path)
        last = last or run
        if ok:
            return last, run
    return last, None


def report_path(reports_dir: Path, name: str) -> Path:
    return reports_dir / f"{slugify(name)}.json"


def write_report(reports_dir: Path, name: str, ok: bool, message: str | None, now: datetime) -> Path:
    """Record a run pushed by `host-agent report` (keeps the last success across failures)."""
    path = report_path(reports_dir, name)
    previous = read_report(path)
    data = {
        "name": name,
        "status": "ok" if ok else "failed",
        "time": now.isoformat(),
        "message": message,
        "last_success": now.isoformat() if ok else (previous or {}).get("last_success"),
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    # readable by the unprivileged agent when written by root (hooks)
    os.chmod(reports_dir, 0o755)  # noqa: S103
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def read_report(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def report_runs(reports_dir: Path, spec: JobSpec) -> tuple[Run | None, Run | None]:
    data = read_report(report_path(reports_dir, spec.name))
    if not data:
        return None, None

    def parse(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value)).astimezone()
        except ValueError:
            return None

    when = parse(data.get("time"))
    last = Run(when, data.get("status") == "ok", "report", data.get("message")) if when else None
    ok_when = parse(data.get("last_success")) if data.get("last_success") else None
    return last, Run(ok_when, True, "report") if ok_when else None


class JobsCollector(Collector):
    name = "jobs"

    def __init__(
        self,
        cfg: JobsConfig,
        reports_dir: Path,
        translate: Translator,
        now: Callable[[], datetime] = _now,
    ) -> None:
        super().__init__(translate)
        self.cfg = cfg
        self.reports_dir = reports_dir
        self.now = now

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def keeps(self, spec: EntitySpec) -> bool:
        return spec.subject in {job.key for job in self.cfg.items}

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        now = self.now()
        for spec in self.cfg.items:
            result.extend(self._job(spec, now))
        return result

    def _job(self, spec: JobSpec, now: datetime) -> CollectorResult:
        key = spec.key
        common: dict[str, Any] = {"subject": key, "subject_label": spec.name, "persistent": True}
        entities = [
            EntitySpec(
                key,
                "binary_sensor",
                self.t("job_problem", name=spec.name),
                device_class="problem",
                icon="mdi:calendar-sync",
                role="problem",
                **common,
            ),
            EntitySpec(
                f"{key}_last_success",
                "sensor",
                self.t("job_last_success", name=spec.name),
                device_class="timestamp",
                icon="mdi:calendar-check",
                **common,
            ),
        ]
        last, last_ok = report_runs(self.reports_dir, spec) if spec.report else file_runs(spec)

        issues: list[Issue] = []
        if last_ok is None:
            issues.append(Issue(key, "job_never", CRITICAL, self.t("job_never")))
        else:
            age_h = int((now - last_ok.when).total_seconds() // 3600)
            if age_h > spec.max_age_hours:
                issues.append(
                    Issue(
                        key,
                        "job_outdated",
                        CRITICAL,
                        self.t("job_outdated", hours=age_h, last=last_ok.when.strftime("%Y-%m-%d %H:%M")),
                    )
                )
        if last and not last.ok and last_ok is not None:
            detail = f" ({last.message})" if last.message else ""
            issues.append(Issue(key, "job_failed", WARNING, self.t("job_failed", detail=detail)))

        attrs = {
            "kind": "job",
            "job": spec.name,
            "source": "report" if spec.report else spec.path,
            "last_run": last.when.isoformat() if last else None,
            "last_status": ("ok" if last.ok else "failed") if last else None,
            "last_message": last.message if last else None,
            "last_file": last.source if last and not spec.report else None,
            "last_success": last_ok.when.isoformat() if last_ok else None,
            "max_age_hours": spec.max_age_hours,
        }
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(key, issues, attrs),
                Reading(f"{key}_last_success", attrs["last_success"]),
            ],
            issues=issues,
        )
