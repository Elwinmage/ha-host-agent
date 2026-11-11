"""dirvish collector: status and age of the last image of each vault."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector
from host_agent.config import DirvishConfig, slugify
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

# Image statuses exposed to Home Assistant (and the card timeline)
SUCCESS = "success"
PARTIAL = "warning"
FAILED = "failed"
RUNNING = "running"

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# An image still without summary after this delay is considered stalled
STALLED_HOURS = 24


def _now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Image:
    name: str
    path: Path
    status: str
    status_text: str
    begin: datetime | None
    complete: datetime | None

    @property
    def when(self) -> datetime | None:
        return self.complete or self.begin

    def as_dict(self) -> dict[str, Any]:
        return {
            "image": self.name,
            "status": self.status,
            "begin": self.begin.isoformat() if self.begin else None,
            "complete": self.complete.isoformat() if self.complete else None,
        }


def parse_summary(text: str) -> dict[str, str]:
    """`key: value` lines of a dirvish summary (keys lower-cased, first occurrence wins)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0].isspace():
            continue
        key, sep, value = line.partition(":")
        if sep:
            result.setdefault(key.strip().lower(), value.strip())
    return result


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:19], _TIME_FORMAT).astimezone()
    except ValueError:
        return None


def classify_status(status: str | None, vanished_is_ok: bool) -> str:
    """Map a dirvish 'Status:' value to success / warning / failed."""
    if not status:
        return FAILED
    text = status.lower()
    if text.startswith("success"):
        return SUCCESS
    if text.startswith("warning"):
        # rsync exit 24: files vanished during the transfer, normal on a live system
        code = re.search(r"\((\d+)\)", text)
        if vanished_is_ok and code and code.group(1) == "24":
            return SUCCESS
        return PARTIAL
    return FAILED


def read_image(path: Path, vanished_is_ok: bool) -> Image:
    summary_file = path / "summary"
    try:
        summary = parse_summary(summary_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        summary = {}
    status_text = summary.get("status", "")
    begin = _parse_time(summary.get("backup-begin") or summary.get("image-now"))
    complete = _parse_time(summary.get("backup-complete"))
    if not summary_file.exists():
        status = RUNNING
        try:
            begin = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        except OSError:
            begin = None
    elif not status_text:
        # summary written at the start, status appended at the end
        status = RUNNING
    else:
        status = classify_status(status_text, vanished_is_ok)
    return Image(path.name, path, status, status_text, begin, complete)


def list_images(vault: Path, vanished_is_ok: bool) -> list[Image]:
    """Images of a vault, newest first."""
    images = []
    try:
        entries = list(os.scandir(vault))
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False) or entry.name in ("dirvish", "lost+found"):
            continue
        # An image directory always holds the transferred tree or a summary
        if not (Path(entry.path) / "summary").exists() and not (Path(entry.path) / "tree").exists():
            continue
        images.append(read_image(Path(entry.path), vanished_is_ok))

    epoch = datetime.fromtimestamp(0).astimezone()
    images.sort(key=lambda img: (img.begin or epoch, img.name), reverse=True)
    return images


def parse_banks(master_conf: str) -> list[str]:
    """`bank:` entries of dirvish master.conf (one per indented line)."""
    try:
        lines = Path(master_conf).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    banks: list[str] = []
    in_bank = False
    for line in lines:
        stripped = line.split("#", 1)[0].rstrip()
        if not stripped:
            continue
        if re.match(r"^bank\s*:", stripped):
            in_bank = True
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                banks.append(rest)
            continue
        if in_bank and line[0].isspace():
            banks.append(stripped.strip())
            continue
        in_bank = False
    return banks


def find_vaults(banks: list[str]) -> list[tuple[str, Path]]:
    """(name, path) of every vault: a bank sub-directory with a dirvish/ config directory."""
    vaults = []
    for bank in banks:
        try:
            entries = sorted(os.scandir(bank), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and (Path(entry.path) / "dirvish").is_dir():
                vaults.append((entry.name, Path(entry.path)))
    return vaults


def history(images: list[Image], days: int, now: datetime) -> list[dict[str, Any]]:
    """One entry per day (oldest first): best status of the images started that day."""
    rank = {SUCCESS: 3, RUNNING: 2, PARTIAL: 1, FAILED: 0}
    by_day: dict[str, Image] = {}
    for image in images:
        if image.begin is None:
            continue
        day = image.begin.date().isoformat()
        best = by_day.get(day)
        if best is None or rank[image.status] > rank[best.status]:
            by_day[day] = image
    result = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        found = by_day.get(day)
        result.append(
            {"date": day, "status": found.status if found else None, "image": found.name if found else None}
        )
    return result


class DirvishCollector(Collector):
    name = "dirvish"

    def __init__(self, cfg: DirvishConfig, translate: Translator, now: Callable[[], datetime] = _now) -> None:
        super().__init__(translate)
        self.cfg = cfg
        self.now = now

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def banks(self) -> list[str]:
        return self.cfg.banks or parse_banks(self.cfg.master_conf)

    def keeps(self, spec: EntitySpec) -> bool:
        return (spec.subject_label or "") not in self.cfg.exclude

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        now = self.now()
        for name, path in find_vaults(self.banks()):
            if name in self.cfg.exclude:
                continue
            result.extend(self._vault(name, path, now))
        return result

    def _vault(self, name: str, path: Path, now: datetime) -> CollectorResult:
        key = f"vault_{slugify(name)}"
        common: dict[str, Any] = {"subject": key, "subject_label": name, "persistent": True}
        entities = [
            EntitySpec(
                key,
                "binary_sensor",
                self.t("vault_problem", vault=name),
                device_class="problem",
                icon="mdi:backup-restore",
                role="problem",
                **common,
            ),
            EntitySpec(
                f"{key}_last_success",
                "sensor",
                self.t("vault_last_success", vault=name),
                device_class="timestamp",
                icon="mdi:calendar-check",
                **common,
            ),
        ]
        images = list_images(path, self.cfg.vanished_is_ok)
        latest = images[0] if images else None
        finished = [img for img in images if img.status != RUNNING]
        last_finished = finished[0] if finished else None
        last_ok = next((img for img in finished if img.status == SUCCESS), None)

        issues: list[Issue] = []

        def add(code: str, severity: str, **kwargs: Any) -> None:
            issues.append(Issue(key, code, severity, self.t(code, **kwargs)))

        if last_ok is None or last_ok.when is None:
            add("vault_never", CRITICAL)
        else:
            age_h = int((now - last_ok.when).total_seconds() // 3600)
            if age_h > self.cfg.max_age_hours:
                add("vault_outdated", CRITICAL, hours=age_h, last=last_ok.when.strftime("%Y-%m-%d %H:%M"))
        if last_finished and last_finished is not last_ok and last_finished.status in (FAILED, PARTIAL):
            add(
                "vault_failed",
                WARNING if last_finished.status == PARTIAL else CRITICAL,
                status=last_finished.status_text or last_finished.status,
            )
        if latest and latest.status == RUNNING and latest.begin:
            running_h = int((now - latest.begin).total_seconds() // 3600)
            if running_h >= STALLED_HOURS:
                add("vault_stalled", WARNING, image=latest.name, hours=running_h)

        duration = None
        if last_finished and last_finished.begin and last_finished.complete:
            duration = int((last_finished.complete - last_finished.begin).total_seconds())
        attrs = {
            "kind": "vault",
            "vault": name,
            "path": str(path),
            "running": bool(latest and latest.status == RUNNING),
            "last_image": last_finished.name if last_finished else None,
            "last_status": last_finished.status if last_finished else None,
            "last_status_text": last_finished.status_text if last_finished else None,
            "last_begin": last_finished.begin.isoformat() if last_finished and last_finished.begin else None,
            "last_duration_s": duration,
            "last_success": last_ok.when.isoformat() if last_ok and last_ok.when else None,
            "max_age_hours": self.cfg.max_age_hours,
            "images": len(images),
            "history": history(images, self.cfg.history_days, now),
        }
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(key, issues, attrs),
                Reading(f"{key}_last_success", attrs["last_success"]),
            ],
            issues=issues,
        )
