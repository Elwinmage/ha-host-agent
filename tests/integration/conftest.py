from __future__ import annotations

from collections.abc import Generator
from contextlib import asynccontextmanager
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.host_agent import remote
from custom_components.host_agent.bundle import AgentBundle
from custom_components.host_agent.ssh import GeneratedKey, HostKey

BUNDLE = AgentBundle(version="0.2.0", protocol=1, data=b"pyz", sha256="0" * 64, unit=b"[Unit]")

SYSTEM = remote.SystemInfo(
    hostname="bkup",
    machine_id="0123456789abcdef",
    mac="dc:a6:32:00:00:01",
    python="3.13.5",
    systemd=True,
    smartctl="7.4",
    arch="armv7l",
    uid=1000,
    sudo=True,
    installed="0.1.2",
    managed=False,
    os="Debian GNU/Linux 13 (trixie)",
)

HOST_KEY = HostKey(public_key="ssh-ed25519 AAAAhostkey", fingerprint="SHA256:abc")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeSsh:
    """Stands for SshClient; records the instances created."""

    instances: ClassVar[list[FakeSsh]] = []

    def __init__(self, host, port, username, *, password=None, private_key=None, host_key=None, **_kw):
        self.host, self.port, self.username = host, port, username
        self.password, self.private_key, self.pinned = password, private_key, host_key
        self.server_host_key = HOST_KEY
        FakeSsh.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.fixture
def fake_remote() -> Generator[MagicMock]:
    FakeSsh.instances = []
    mocks = MagicMock()
    mocks.preflight = AsyncMock(return_value=SYSTEM)
    mocks.check_sudo = AsyncMock(return_value=True)
    mocks.read_existing_config = AsyncMock(
        return_value="language: fr\ncollectors:\n  filesystem:\n    required: [/bkup/dirvish]\n"
    )
    mocks.bootstrap = AsyncMock()
    mocks.status = AsyncMock(return_value={"version": "0.1.2"})
    mocks.install = AsyncMock()
    mocks.configure = AsyncMock()
    mocks.rollback = AsyncMock()
    mocks.restart = AsyncMock()
    mocks.uninstall = AsyncMock()
    targets = [
        "preflight",
        "check_sudo",
        "read_existing_config",
        "bootstrap",
        "status",
        "install",
        "configure",
        "rollback",
        "restart",
        "uninstall",
    ]
    patches = [patch(f"custom_components.host_agent.remote.{name}", getattr(mocks, name)) for name in targets]
    patches += [
        patch("custom_components.host_agent.config_flow.SshClient", FakeSsh),
        patch("custom_components.host_agent.runtime.SshClient", FakeSsh),
        patch("custom_components.host_agent.config_flow.load_bundle", return_value=BUNDLE),
        patch("custom_components.host_agent.load_bundle", return_value=BUNDLE),
        patch("custom_components.host_agent.config_flow.load_remote_files", return_value={}),
        patch(
            "custom_components.host_agent.config_flow.generate_client_key",
            return_value=GeneratedKey("PRIVATE", "ssh-ed25519 AAAAclient"),
        ),
        patch(
            "custom_components.host_agent.config_flow.async_get_source_ip",
            AsyncMock(return_value="192.168.0.109"),
        ),
    ]
    for p in patches:
        p.start()
    mocks.ssh_instances = FakeSsh.instances
    yield mocks
    for p in patches:
        p.stop()


@asynccontextmanager
async def noop():
    yield


def entry_data(**overrides):
    data = {
        "host": "192.168.0.20",
        "port": 22,
        "private_key": "PRIVATE-OLD",
        "host_key": "ssh-ed25519 AAAAhostkey",
        "node_id": "bkup",
        "machine_id": "0123456789abcdef",
        "mac": "dc:a6:32:00:00:01",
        "device_name": "Serveur de sauvegarde",
        "mqtt_host": "192.168.0.109",
        "mqtt_port": 1883,
        "mqtt_username": "host-agent-bkup",
        "mqtt_password": "secret",
        "deployed_version": "0.1.2",
    }
    data.update(overrides)
    return data
