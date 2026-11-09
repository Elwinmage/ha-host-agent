"""Persistent agent state (known entities, subjects last seen)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from host_agent.model import EntitySpec

_LOGGER = logging.getLogger(__name__)
STATE_VERSION = 1


@dataclass
class AgentState:
    known: dict[str, EntitySpec] = field(default_factory=dict)
    last_seen: dict[str, str] = field(default_factory=dict)  # subject -> ISO timestamp

    def to_json(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "known": {k: s.as_dict() for k, s in self.known.items()},
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AgentState:
        known = {k: EntitySpec.from_dict(v) for k, v in (data.get("known") or {}).items()}
        return cls(known=known, last_seen=dict(data.get("last_seen") or {}))


class StateStore:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None

    def load(self) -> AgentState:
        if not self.path or not self.path.exists():
            return AgentState()
        try:
            return AgentState.from_json(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as err:
            _LOGGER.warning("Ignoring unreadable state file %s: %s", self.path, err)
            return AgentState()

    def save(self, state: AgentState) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state.to_json(), fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as err:
            _LOGGER.warning("Cannot write state file %s: %s", self.path, err)
