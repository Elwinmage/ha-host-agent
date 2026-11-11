"""Collector registry."""

from __future__ import annotations

from host_agent.collectors.base import Collector
from host_agent.collectors.dirvish import DirvishCollector
from host_agent.collectors.filesystem import FilesystemCollector
from host_agent.collectors.jobs import JobsCollector
from host_agent.collectors.smart import SmartCollector
from host_agent.collectors.system import SystemCollector
from host_agent.config import Config
from host_agent.i18n import Translator


def build_collectors(cfg: Config, translate: Translator) -> list[Collector]:
    collectors: list[Collector] = []
    if cfg.system.enabled:
        collectors.append(SystemCollector(cfg.system, translate))
    if cfg.smart.enabled:
        collectors.append(SmartCollector(cfg.smart, translate))
    if cfg.filesystem.enabled:
        collectors.append(FilesystemCollector(cfg.filesystem, translate))
    if cfg.dirvish.enabled:
        collectors.append(DirvishCollector(cfg.dirvish, translate))
    if cfg.jobs.items:
        collectors.append(JobsCollector(cfg.jobs, cfg.reports_dir, translate))
    return collectors


__all__ = [
    "Collector",
    "DirvishCollector",
    "FilesystemCollector",
    "JobsCollector",
    "SmartCollector",
    "SystemCollector",
    "build_collectors",
]
