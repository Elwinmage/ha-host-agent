"""Collector registry."""

from __future__ import annotations

from host_agent.collectors.base import Collector
from host_agent.collectors.filesystem import FilesystemCollector
from host_agent.collectors.smart import SmartCollector
from host_agent.config import Config
from host_agent.i18n import Translator


def build_collectors(cfg: Config, translate: Translator) -> list[Collector]:
    collectors: list[Collector] = []
    if cfg.smart.enabled:
        collectors.append(SmartCollector(cfg.smart, translate))
    if cfg.filesystem.enabled:
        collectors.append(FilesystemCollector(cfg.filesystem, translate))
    return collectors


__all__ = ["Collector", "FilesystemCollector", "SmartCollector", "build_collectors"]
