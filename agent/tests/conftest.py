from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from host_agent.config import SmartConfig
from host_agent.i18n import Translator

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def smart_cfg() -> SmartConfig:
    return SmartConfig()


@pytest.fixture
def t_en() -> Translator:
    return Translator("en")


@pytest.fixture
def t_fr() -> Translator:
    return Translator("fr")


@pytest.fixture(autouse=True)
def isolated_host(monkeypatch: pytest.MonkeyPatch):
    """Keep tests independent of the machine running them.

    Every tool is reported as installed, so a collector always takes its command
    path, and running a real command fails the test: tests inject a fake runner.
    Without this, a test could pass on a machine lacking a tool (timedatectl,
    nvidia-smi...) and fail on another one, such as the CI runner.
    """
    calls: list[list[str]] = []

    def no_real_command(args, *_args: Any, **_kwargs: Any):
        calls.append(list(args))
        raise AssertionError(f"test ran a real command: {list(args)}")

    monkeypatch.setattr("shutil.which", lambda name, *_a, **_k: f"/usr/bin/{name}")
    monkeypatch.setattr("host_agent.collectors.base.subprocess.run", no_real_command)
    yield
    # also catches commands whose failure a collector or the runner swallowed
    assert not calls, f"test ran real commands: {calls}"
