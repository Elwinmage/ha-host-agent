"""End-to-end test of the SSH layer against a real host: bootstrap, install, upgrade,
failed upgrade with rollback, configuration, security checks and uninstall.

Destructive for the target host: only run it on a disposable machine/container.

    HOST_AGENT_E2E="127.0.0.1:2222:pi:raspberry" HOST_AGENT_E2E_MQTT=127.0.0.1 pytest tests/e2e
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

TARGET = os.environ.get("HOST_AGENT_E2E")
MQTT_HOST = os.environ.get("HOST_AGENT_E2E_MQTT", "127.0.0.1")
pytestmark = pytest.mark.skipif(not TARGET, reason="set HOST_AGENT_E2E=host:port:user:password")

REPO = Path(__file__).resolve().parents[2]

from custom_components.host_agent import remote
from custom_components.host_agent.agent_config import build_config
from custom_components.host_agent.bundle import AgentBundle, load_bundle, load_remote_files
from custom_components.host_agent.const import ADMIN_USER
from custom_components.host_agent.ssh import (
    CommandError,
    HostKeyMismatch,
    SshClient,
    generate_client_key,
)


def make_bundle(tmp: Path, version: str, crash: bool = False) -> AgentBundle:
    """Build an agent archive with another version (optionally crashing at start)."""
    src = tmp / f"agent-{version}"
    shutil.copytree(REPO / "agent", src, ignore=shutil.ignore_patterns("dist", "__pycache__", ".*cache"))
    init = src / "src/host_agent/__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text()))
    if crash:
        cli = src / "src/host_agent/cli.py"
        cli.write_text(
            cli.read_text()
            .replace("def cmd_run(cfg", "def cmd_run_disabled(cfg")
            .replace('"run": cmd_run,', '"run": lambda c, a: (_ for _ in ()).throw(SystemExit(1)),')
        )
    subprocess.run([str(src / "packaging/build_pyz.sh"), str(tmp / version)], check=True, capture_output=True)
    data = (tmp / version / "host-agent.pyz").read_bytes()
    base = load_bundle()
    return AgentBundle(version, base.protocol, data, hashlib.sha256(data).hexdigest(), base.unit)


def wait_mqtt(node: str, version: str, timeout: float = 30) -> bool:
    import paho.mqtt.client as mqtt

    seen = {"online": False, "version": None}
    done = threading.Event()

    def on_message(_c, _u, msg):
        if msg.topic.endswith("/availability"):
            seen["online"] = msg.payload == b"online"
        else:
            seen["version"] = json.loads(msg.payload)["state"]
        if seen["online"] and seen["version"] == version:
            done.set()

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect(MQTT_HOST)
    client.subscribe([(f"host_agent/{node}/availability", 1), (f"host_agent/{node}/state/agent_version", 1)])
    client.loop_start()
    try:
        return done.wait(timeout)
    finally:
        client.loop_stop()
        client.disconnect()


def test_full_lifecycle(tmp_path):
    host, port, user, password = TARGET.split(":", 3)
    port = int(port)
    asyncio.run(_scenario(tmp_path, host, port, user, password))


async def _scenario(tmp: Path, host: str, port: int, user: str, password: str) -> None:
    # 1. Trust on first use + preflight + sudo
    async with SshClient(host, port, user, password=password) as ssh:
        info = await remote.preflight(ssh)
        host_key = ssh.server_host_key.public_key
        assert info.python_ok and info.systemd
        assert await remote.check_sudo(ssh, info, password)
        assert not await remote.check_sudo(ssh, info, "wrong-password")

        # 2. Bootstrap
        key = generate_client_key("e2e@test")
        bundle = load_bundle()
        await remote.bootstrap(ssh, info, password, key.public_key, load_remote_files(), bundle.unit)

    node = re.sub(r"[^a-z0-9]+", "_", info.hostname.lower())
    data = {
        "node_id": node,
        "device_name": "E2E",
        "mqtt_host": MQTT_HOST,
        "mqtt_port": 1883,
        "mqtt_username": None,
    }
    options = {
        "language": "fr",
        "collectors": {"smart": {"enabled": False}, "filesystem": {"include_mounts": ["/"]}},
    }

    def admin() -> SshClient:
        return SshClient(host, port, ADMIN_USER, private_key=key.private_key, host_key=host_key)

    # 3. Install through the restricted account
    async with admin() as ssh:
        await remote.install(ssh, bundle, build_config(data, options), "")
        status = await remote.status(ssh)
        assert status["version"] == bundle.version and status["active"] == "active"

        # The restricted account cannot do anything else as root
        res = await ssh.run("sudo -n id", check=False)
        assert res.exit_status != 0
        with pytest.raises(remote.RemoteError):
            await remote.helper(ssh, "install", "1.0.0", "x")
        # ...nor read the configuration directory directly
        res = await ssh.run("cat /etc/host-agent/config.yaml", check=False)
        assert res.exit_status != 0
    assert wait_mqtt(node, bundle.version), "agent did not report over MQTT"

    # 4. Upgrade
    v2 = make_bundle(tmp, "9.0.0")
    async with admin() as ssh:
        await remote.install(ssh, v2)
    assert wait_mqtt(node, "9.0.0")

    # 5. Broken upgrade: the service does not stay up -> automatic rollback
    broken = make_bundle(tmp, "9.1.0", crash=True)
    async with admin() as ssh:
        with pytest.raises(remote.RemoteError, match=r"rolled back 9\.0\.0"):
            await remote.install(ssh, broken)
        assert (await remote.status(ssh))["version"] == "9.0.0"
    assert wait_mqtt(node, "9.0.0")

    # 6. Invalid configuration is refused, valid one applied
    async with admin() as ssh:
        bad = build_config(data, {"collectors": {"smart": {"interval": 1}}})
        with pytest.raises(remote.RemoteError, match="invalid agent configuration"):
            await remote.configure(ssh, bad)
        await remote.configure(ssh, build_config(data, {**options, "language": "en"}))
        assert "language: en" in await remote.helper(ssh, "read-config")

    # 7. Pinned host key: any other key is refused
    other = generate_client_key("other").public_key
    with pytest.raises(HostKeyMismatch):
        async with SshClient(host, port, ADMIN_USER, private_key=key.private_key, host_key=other):
            pass

    # 8. Uninstall
    async with admin() as ssh:
        await remote.uninstall(ssh, purge=True)
    async with SshClient(host, port, user, password=password, host_key=host_key) as ssh:
        info = await remote.preflight(ssh)
        assert info.installed is None
        with pytest.raises(CommandError):
            await ssh.run("test -e /etc/sudoers.d/host-agent-admin")
