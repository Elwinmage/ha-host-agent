"""Collector registry."""

from __future__ import annotations

from host_agent.collectors.base import Collector
from host_agent.collectors.dirvish import DirvishCollector
from host_agent.collectors.filesystem import FilesystemCollector
from host_agent.collectors.gpu import GpuCollector
from host_agent.collectors.jobs import JobsCollector
from host_agent.collectors.kernel import KernelCollector
from host_agent.collectors.services import ServicesCollector
from host_agent.collectors.smart import SmartCollector
from host_agent.collectors.storage import StorageCollector
from host_agent.collectors.system import SystemCollector
from host_agent.collectors.updates import UpdatesCollector
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
    if cfg.gpu.enabled:
        collectors.append(GpuCollector(cfg.gpu, translate))
    if cfg.services.enabled:
        collectors.append(ServicesCollector(cfg.services, translate))
    if cfg.storage.enabled:
        collectors.append(StorageCollector(cfg.storage, translate))
    if cfg.updates.enabled:
        collectors.append(UpdatesCollector(cfg.updates, translate))
    if cfg.kernel.enabled:
        collectors.append(KernelCollector(cfg.kernel, translate))
    return collectors


__all__ = [
    "Collector",
    "DirvishCollector",
    "FilesystemCollector",
    "GpuCollector",
    "JobsCollector",
    "KernelCollector",
    "ServicesCollector",
    "SmartCollector",
    "StorageCollector",
    "SystemCollector",
    "UpdatesCollector",
    "build_collectors",
]
