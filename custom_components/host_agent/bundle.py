"""Agent archive bundled with the integration (injected by CI at release time)."""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

BUNDLE_DIR = Path(__file__).parent / "agent"
REMOTE_DIR = Path(__file__).parent / "remote"
PYZ_NAME = "host-agent.pyz"
UNIT_NAME = "host-agent.service"


class BundleError(Exception):
    """The agent archive is missing or invalid."""


@dataclass(frozen=True)
class AgentBundle:
    version: str
    protocol: int
    data: bytes
    sha256: str
    unit: bytes


def _read_meta(data: bytes) -> tuple[str, int]:
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            source = zf.read("host_agent/__init__.py").decode()
    except (zipfile.BadZipFile, KeyError) as err:
        raise BundleError(f"invalid agent archive: {err}") from err
    version = re.search(r'^__version__\s*=\s*"([^"]+)"', source, re.M)
    protocol = re.search(r"^PROTOCOL_VERSION\s*=\s*(\d+)", source, re.M)
    if not version or not protocol:
        raise BundleError("cannot read the agent version")
    return version.group(1), int(protocol.group(1))


@lru_cache(maxsize=1)
def load_bundle(directory: Path = BUNDLE_DIR) -> AgentBundle:
    """Load the bundled agent. Blocking: call from an executor."""
    pyz = directory / PYZ_NAME
    unit = directory / UNIT_NAME
    if not pyz.is_file() or not unit.is_file():
        raise BundleError(
            f"agent archive not found in {directory} (run scripts/bundle_agent.sh in a development checkout)"
        )
    data = pyz.read_bytes()
    version, protocol = _read_meta(data)
    return AgentBundle(
        version=version,
        protocol=protocol,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        unit=unit.read_bytes(),
    )


@lru_cache(maxsize=1)
def load_remote_files() -> dict[str, bytes]:
    """Scripts uploaded during the bootstrap. Blocking: call from an executor."""
    return {
        name: (REMOTE_DIR / name).read_bytes() for name in ("bootstrap.sh", "host-agent-admin", "sudoers")
    }
