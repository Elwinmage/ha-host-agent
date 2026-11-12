"""Kernel log: storage and filesystem errors over a sliding window."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from host_agent.collectors.base import Collector, CommandRunner, run_command
from host_agent.config import KernelConfig
from host_agent.i18n import Translator
from host_agent.model import CRITICAL, WARNING, CollectorResult, EntitySpec, Issue, Reading, problem_reading

KEY = "kernel"
MAX_RECENT = 10

# (category, severity, regex) evaluated in order, first match wins
PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "filesystem",
        CRITICAL,
        re.compile(
            r"(EXT[234]-fs (error|warning)|Remounting filesystem read-only|BTRFS (error|critical)|"
            r"XFS \(.*\): (Corruption|metadata I/O error|Filesystem has been shut down)|F2FS-fs.*error)",
            re.I,
        ),
    ),
    (
        "io",
        CRITICAL,
        re.compile(
            r"(I/O error|blk_update_request|critical (medium|target) error|Medium Error|"
            r"Unrecovered read error|Sense Key : (Medium|Hardware) Error)",
            re.I,
        ),
    ),
    ("nvme", WARNING, re.compile(r"nvme\d+.*(timeout|reset|I/O \d+ QID|controller is down)", re.I)),
    (
        "ata",
        WARNING,
        re.compile(
            r"ata\d+(\.\d+)?: (exception Emask|hard resetting link|failed command|"
            r"SError|link is slow)",
            re.I,
        ),
    ),
    (
        "usb",
        WARNING,
        re.compile(
            r"(reset \S*?[- ]?speed( Plus)?( Gen \S+)? USB device|"
            r"uas_eh_(abort|device_reset)_handler|usb \S+: device descriptor read.*error)",
            re.I,
        ),
    ),
    ("power", WARNING, re.compile(r"(Under-?voltage detected|Voltage normalised)", re.I)),
]


def classify(
    lines: list[str],
    extra: list[re.Pattern[str]],
    ignore: list[re.Pattern[str]],
    usb_keep: Callable[[str], bool] | None = None,
) -> tuple[Counter[str], list[dict[str, str]]]:
    counts: Counter[str] = Counter()
    recent: list[dict[str, str]] = []
    for line in lines:
        if any(p.search(line) for p in ignore):
            continue
        category = None
        for name, _severity, pattern in PATTERNS:
            if pattern.search(line):
                category = name
                break
        if category is None and any(p.search(line) for p in extra):
            category = "custom"
        if category is None:
            continue
        if category == "usb" and usb_keep is not None and not usb_keep(line):
            continue
        counts[category] += 1
        recent.append({"category": category, "line": line[:300]})
    return counts, recent[-MAX_RECENT:]


SEVERITY = {name: severity for name, severity, _ in PATTERNS} | {"custom": WARNING}

# USB messages name the device by its port: "usb 2-1.4: reset SuperSpeed USB device..."
USB_PORT = re.compile(r"\busb (\d+-[\d.]+):")
# Storage drivers bound to that port, as logged when the device is detected
STORAGE_LOG = re.compile(r"\b(?:usb-storage|uas) (\d+-[\d.]+):\d")
# Every USB device is reset when the machine wakes up or boots: not a fault
WAKE = re.compile(r"(PM: (suspend|hibernation) exit|ACPI: PM: Waking up|PM: resume|Linux version \d)", re.I)
WAKE_GRACE = 120  # seconds
MASS_STORAGE_CLASS = "08"


def line_time(line: str) -> datetime | None:
    """Timestamp of a `journalctl -o short-iso` line."""
    stamp = line.split(" ", 1)[0]
    # "+0100" -> "+01:00" for Python versions older than 3.11
    stamp = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", stamp)
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


class UsbFilter:
    """Keeps the USB messages that matter: storage devices outside of wake-ups.

    Resets of keyboards, webcams or hubs, and the resets every device goes through
    when the machine resumes from sleep or boots, are not storage problems.
    """

    def __init__(self, lines: list[str], root: str = "/") -> None:
        self.root = root
        self.logged_storage = {m.group(1) for line in lines if (m := STORAGE_LOG.search(line))}
        self.wakes = [t for line in lines if WAKE.search(line) and (t := line_time(line))]

    def is_storage(self, port: str) -> bool:
        if port in self.logged_storage:
            return True
        # Device still plugged in: interface class of each of its interfaces
        for iface in Path(self.root, "sys/bus/usb/devices").glob(f"{port}:*"):
            try:
                if (iface / "bInterfaceClass").read_text().strip() == MASS_STORAGE_CLASS:
                    return True
            except OSError:
                continue
        return False

    def after_wake(self, line: str) -> bool:
        when = line_time(line)
        return when is not None and any(0 <= (when - w).total_seconds() <= WAKE_GRACE for w in self.wakes)

    def __call__(self, line: str) -> bool:
        match = USB_PORT.search(line)
        if match is None:  # uas_eh_* handlers: storage by definition
            return True
        return self.is_storage(match.group(1)) and not self.after_wake(line)


class KernelCollector(Collector):
    name = "kernel"

    def __init__(
        self,
        cfg: KernelConfig,
        translate: Translator,
        runner: CommandRunner = run_command,
        root: str = "/",
    ) -> None:
        super().__init__(translate, runner)
        self.cfg = cfg
        self.root = root
        self.extra = [re.compile(p, re.I) for p in cfg.patterns]
        self.ignore = [re.compile(p, re.I) for p in cfg.ignore]

    @property
    def interval(self) -> int:
        return self.cfg.interval

    def lines(self) -> list[str]:
        res = self.run(
            [
                self.cfg.journalctl,
                "-k",
                "-q",
                "--no-pager",
                "-o",
                "short-iso",
                "--since",
                f"-{self.cfg.window_hours}h",
            ],
            60,
        )
        if res.returncode != 0:
            raise RuntimeError(f"journalctl: {res.stderr.strip()[:200]}")
        return res.stdout.splitlines()

    def collect(self) -> CollectorResult:
        lines = self.lines()
        counts, recent = classify(lines, self.extra, self.ignore, UsbFilter(lines, self.root))
        issues = [
            Issue(
                KEY,
                f"kernel_{category}",
                SEVERITY[category],
                self.t(f"kernel_{category}", count=count, hours=self.cfg.window_hours),
            )
            for category, count in sorted(counts.items())
        ]
        common: dict[str, Any] = {"subject": KEY, "subject_label": "kernel"}
        attrs = {
            "kind": "kernel",
            "window_hours": self.cfg.window_hours,
            "counts": dict(counts),
            "recent": recent,
        }
        return CollectorResult(
            entities=[
                EntitySpec(
                    KEY,
                    "binary_sensor",
                    self.t("kernel_problem"),
                    device_class="problem",
                    icon="mdi:console-line",
                    role="problem",
                    **common,
                ),
                EntitySpec(
                    f"{KEY}_errors",
                    "sensor",
                    self.t("kernel_errors"),
                    state_class="measurement",
                    icon="mdi:alert-circle-outline",
                    **common,
                ),
            ],
            readings=[problem_reading(KEY, issues, attrs), Reading(f"{KEY}_errors", sum(counts.values()))],
            issues=issues,
        )
