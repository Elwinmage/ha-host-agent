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
