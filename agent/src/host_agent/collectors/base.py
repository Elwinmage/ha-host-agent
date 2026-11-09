"""Collector base class and process helpers."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from host_agent.i18n import Translator
from host_agent.model import CollectorResult


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str], int], CommandResult]


def run_command(args: Sequence[str], timeout: int) -> CommandResult:
    """Run a command without a shell; never raises on non-zero exit codes."""
    try:
        proc = subprocess.run(  # noqa: S603 - args are built internally, no shell
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as err:
        return CommandResult(127, "", str(err))
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timeout after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


class Collector(ABC):
    """A collector produces entities, readings and issues for one domain."""

    name: str = "collector"

    def __init__(self, translate: Translator, runner: CommandRunner = run_command) -> None:
        self.t = translate
        self.run = runner

    @property
    @abstractmethod
    def interval(self) -> int:
        """Polling interval in seconds."""

    @abstractmethod
    def collect(self) -> CollectorResult:
        """Collect current data. May raise; the runner handles failures."""
