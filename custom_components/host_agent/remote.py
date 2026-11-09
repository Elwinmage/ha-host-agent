"""Remote operations: preflight, bootstrap and agent lifecycle through the helper."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .bundle import AgentBundle
from .const import HELPER, INCOMING_DIR, MIN_PYTHON
from .ssh import CommandError, SshClient, quote

_LOGGER = logging.getLogger(__name__)

# Run with the login account; everything is best effort and prints key=value lines
PREFLIGHT = r"""
echo "hostname=$(hostname -s 2>/dev/null || hostname)"
echo "machine_id=$(cat /etc/machine-id 2>/dev/null)"
iface=$(awk '$2=="00000000"{print $1; exit}' /proc/net/route 2>/dev/null)
[ -n "$iface" ] && echo "mac=$(cat /sys/class/net/$iface/address 2>/dev/null)"
echo "python=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
echo "systemd=$( [ -d /run/systemd/system ] && echo yes || echo no)"
echo "smartctl=$( (smartctl --version 2>/dev/null || /usr/sbin/smartctl --version 2>/dev/null) | awk 'NR==1{print $2}')"
echo "arch=$(uname -m)"
echo "uid=$(id -u)"
echo "sudo=$(command -v sudo >/dev/null && echo yes || echo no)"
echo "installed=$(basename "$(readlink /opt/host-agent/current 2>/dev/null)" 2>/dev/null)"
echo "managed=$( [ -x /usr/local/lib/host-agent/host-agent-admin ] && echo yes || echo no)"
echo "os=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
"""


class RemoteError(Exception):
    """A remote operation failed; the message is shown to the user."""


@dataclass
class SystemInfo:
    hostname: str
    machine_id: str
    mac: str | None
    python: str
    systemd: bool
    smartctl: str | None
    arch: str
    uid: int
    sudo: bool
    installed: str | None
    managed: bool
    os: str

    @property
    def python_ok(self) -> bool:
        try:
            parts = tuple(int(p) for p in self.python.split(".")[:2])
        except ValueError:
            return False
        return len(parts) == 2 and parts >= MIN_PYTHON


def parse_kv(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


async def preflight(ssh: SshClient) -> SystemInfo:
    kv = parse_kv(str((await ssh.run(PREFLIGHT, check=False)).stdout or ""))
    return SystemInfo(
        hostname=kv.get("hostname") or ssh.host,
        machine_id=kv.get("machine_id", ""),
        mac=(kv.get("mac") or "").lower() or None,
        python=kv.get("python", ""),
        systemd=kv.get("systemd") == "yes",
        smartctl=kv.get("smartctl") or None,
        arch=kv.get("arch", ""),
        uid=int(kv.get("uid") or -1),
        sudo=kv.get("sudo") == "yes",
        installed=kv.get("installed") or None,
        managed=kv.get("managed") == "yes",
        os=kv.get("os", ""),
    )


def _sudo(info: SystemInfo, password: str | None) -> tuple[str, str | None]:
    """Command prefix and stdin to run as root with the login account."""
    if info.uid == 0:
        return "", None
    if password:
        return "sudo -S -p '' ", password + "\n"
    return "sudo -n ", None


async def check_sudo(ssh: SshClient, info: SystemInfo, password: str | None) -> bool:
    prefix, stdin = _sudo(info, password)
    result = await ssh.run(f"{prefix}true", stdin=stdin, check=False, timeout=30)
    return result.exit_status == 0


async def read_existing_config(ssh: SshClient, info: SystemInfo, password: str | None) -> str | None:
    """Configuration of a manual installation, imported into the options."""
    prefix, stdin = _sudo(info, password)
    result = await ssh.run(f"{prefix}cat /etc/host-agent/config.yaml", stdin=stdin, check=False)
    return str(result.stdout) if result.exit_status == 0 and result.stdout else None


async def bootstrap(
    ssh: SshClient,
    info: SystemInfo,
    password: str | None,
    public_key: str,
    files: dict[str, bytes],
    unit: bytes,
) -> None:
    """Create the accounts, helper, sudoers rule and unit (needs root through sudo)."""
    tmp = str((await ssh.run("mktemp -d /tmp/host-agent-bootstrap.XXXXXX")).stdout).strip()
    try:
        uploads = {**files, "host-agent.service": unit, "authorized_key.pub": public_key.encode() + b"\n"}
        for name, data in uploads.items():
            await ssh.put(data, f"{tmp}/{name}", 0o700 if name.endswith((".sh", "-admin")) else 0o600)
        prefix, stdin = _sudo(info, password)
        try:
            result = await ssh.run(f"{prefix}sh {quote(tmp)}/bootstrap.sh {quote(tmp)}", stdin=stdin)
        except CommandError as err:
            raise RemoteError(f"bootstrap failed: {err}") from err
        if "BOOTSTRAP_OK" not in str(result.stdout):
            raise RemoteError("bootstrap did not complete")
    finally:
        await ssh.run(f"rm -rf {quote(tmp)}", check=False)


# --------------------------------------------------------------------------- #
# Operations with the restricted admin account
# --------------------------------------------------------------------------- #


async def helper(ssh: SshClient, *args: str, timeout: float = 180) -> str:
    command = " ".join(["sudo", "-n", HELPER, *(quote(a) for a in args)])
    try:
        result = await ssh.run(command, timeout=timeout)
    except CommandError as err:
        stderr = err.stderr.strip()
        lines = [line for line in stderr.splitlines() if line.startswith("ERROR")] or stderr.splitlines()
        message = lines[-1] if lines else str(err)
        if err.returncode == 3:
            rolled = next((ln for ln in err.stdout.splitlines() if ln.startswith("ROLLED_BACK")), None)
            message = f"{message} ({rolled.lower().replace('_', ' ')})" if rolled else message
        raise RemoteError(message.removeprefix("ERROR: ")) from err
    return str(result.stdout)


async def status(ssh: SshClient) -> dict[str, str]:
    return parse_kv(await helper(ssh, "status", timeout=30))


async def _stage(ssh: SshClient, config_yaml: str | None, mqtt_secret: str | None) -> None:
    if config_yaml is not None:
        await ssh.put(config_yaml.encode(), f"{INCOMING_DIR}/config.yaml", 0o600)
    if mqtt_secret is not None:
        await ssh.put(mqtt_secret.encode(), f"{INCOMING_DIR}/mqtt.secret", 0o600)


async def install(
    ssh: SshClient,
    bundle: AgentBundle,
    config_yaml: str | None = None,
    mqtt_secret: str | None = None,
) -> None:
    await ssh.put(bundle.data, f"{INCOMING_DIR}/host-agent.pyz", 0o600)
    await _stage(ssh, config_yaml, mqtt_secret)
    output = await helper(ssh, "install", bundle.version, bundle.sha256, timeout=240)
    if f"INSTALLED {bundle.version}" not in output:
        raise RemoteError("unexpected helper output")


async def configure(ssh: SshClient, config_yaml: str, mqtt_secret: str | None = None) -> None:
    await _stage(ssh, config_yaml, mqtt_secret)
    await helper(ssh, "configure", timeout=120)


async def rollback(ssh: SshClient) -> None:
    await helper(ssh, "rollback", timeout=120)


async def restart(ssh: SshClient) -> None:
    await helper(ssh, "restart", timeout=120)


async def uninstall(ssh: SshClient, purge: bool) -> None:
    await helper(ssh, "uninstall", *(["--purge"] if purge else []), timeout=120)
