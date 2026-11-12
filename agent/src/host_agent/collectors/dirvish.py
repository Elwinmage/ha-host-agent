"""dirvish collector: status and age of the last image of each vault."""

from __future__ import annotations

import gzip
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
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


@dataclass
class RsyncStats:
    files: int | None
    size: int | None  # bytes
    transferred: int | None  # regular files transferred (changed or new)

    def as_dict(self) -> dict[str, int | None]:
        return {"files": self.files, "size_bytes": self.size, "transferred": self.transferred}


_STATS = {
    "files": re.compile(r"^Number of files:\s*([\d,.]+)", re.M),
    "transferred": re.compile(r"^Number of (?:regular )?files transferred:\s*([\d,.]+)", re.M),
    "size": re.compile(r"^Total file size:\s*([\d,.]+)", re.M),
}
TAIL_BYTES = 64 * 1024


def parse_rsync_stats(text: str) -> RsyncStats | None:
    """`rsync --stats` summary (3.0 and 3.1+ formats, with or without thousands separators)."""
    values: dict[str, int | None] = {}
    for name, pattern in _STATS.items():
        matches = pattern.findall(text)
        values[name] = int(re.sub(r"[,.]", "", matches[-1])) if matches else None
    if all(v is None for v in values.values()):
        return None
    return RsyncStats(**values)


def read_image_stats(image: Path, max_bytes: int) -> RsyncStats | None:
    """Statistics at the end of the image log (`log`, or `log.gz` once compressed)."""
    plain, compressed = image / "log", image / "log.gz"
    try:
        if plain.is_file():
            with open(plain, "rb") as fh:
                fh.seek(max(0, plain.stat().st_size - TAIL_BYTES))
                return parse_rsync_stats(fh.read().decode("utf-8", errors="replace"))
        if compressed.is_file() and compressed.stat().st_size <= max_bytes:
            tail = b""
            # verbose logs list every file: stream and keep only the end
            with gzip.open(compressed, "rb") as fh:
                while chunk := fh.read(1024 * 1024):
                    tail = (tail + chunk)[-TAIL_BYTES:]
            return parse_rsync_stats(tail.decode("utf-8", errors="replace"))
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return None


def baseline_of(previous: list[RsyncStats]) -> RsyncStats | None:
    """Median of the previous images (at least 3 to be meaningful)."""
    if len(previous) < 3:
        return None

    def med(values: list[int | None]) -> int | None:
        known = [v for v in values if v is not None]
        return int(median(known)) if known else None

    return RsyncStats(
        files=med([s.files for s in previous]),
        size=med([s.size for s in previous]),
        transferred=med([s.transferred for s in previous]),
    )


def detect_anomalies(
    last: RsyncStats, base: RsyncStats, cfg: DirvishConfig
) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if last.size is not None and base.size:
        drop = round((base.size - last.size) * 100 / base.size)
        if drop >= cfg.size_drop_pct:
            found.append(("vault_size_drop", {"pct": drop}))
    if last.files is not None and base.files:
        drop = round((base.files - last.files) * 100 / base.files)
        if drop >= cfg.files_drop_pct:
            found.append(("vault_files_drop", {"pct": drop, "count": base.files - last.files}))
    if last.transferred is not None and base.transferred is not None:
        limit = max(cfg.changes_min, cfg.changes_factor * base.transferred)
        if last.transferred > limit:
            found.append(("vault_changes_spike", {"count": last.transferred, "usual": base.transferred}))
    return found


class DirvishCollector(Collector):
    name = "dirvish"

    def __init__(self, cfg: DirvishConfig, translate: Translator, now: Callable[[], datetime] = _now) -> None:
        super().__init__(translate)
        self.cfg = cfg
        self.now = now
        # rsync statistics per finished image (images never change once complete)
        self._stats: dict[str, RsyncStats | None] = {}
        self._seen: set[str] = set()

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
        self._seen = set()
        for name, path in find_vaults(self.banks()):
            if name in self.cfg.exclude:
                continue
            result.extend(self._vault(name, path, now))
        # forget expired images
        self._stats = {k: v for k, v in self._stats.items() if k in self._seen}
        return result

    def _image_stats(self, image: Image) -> RsyncStats | None:
        cache_key = str(image.path)
        self._seen.add(cache_key)
        if cache_key not in self._stats:
            self._stats[cache_key] = read_image_stats(image.path, self.cfg.max_log_mb * 2**20)
        return self._stats[cache_key]

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
        if self.cfg.anomaly:
            successes = [img for img in finished if img.status == SUCCESS][: self.cfg.anomaly_history + 1]
            stats = [self._image_stats(img) for img in successes]
            if stats and stats[0]:
                attrs["last_stats"] = stats[0].as_dict()
                baseline = baseline_of([s for s in stats[1:] if s])
                if baseline:
                    attrs["baseline_stats"] = baseline.as_dict()
                    for code, kwargs in detect_anomalies(stats[0], baseline, self.cfg):
                        add(code, WARNING, **kwargs)
        return CollectorResult(
            entities=entities,
            readings=[
                problem_reading(key, issues, attrs),
                Reading(f"{key}_last_success", attrs["last_success"]),
            ],
            issues=issues,
        )
