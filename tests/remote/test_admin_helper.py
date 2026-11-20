"""Tests of the privileged remote helper, run against a fake root and a fake systemctl.

Requires root (the helper sets file ownership); CI runs them with sudo.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "custom_components" / "host_agent" / "remote" / "host-agent-admin"
AGENT_SRC = REPO / "agent" / "src" / "host_agent"

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="helper tests need root")

FAKE_SYSTEMCTL = """#!/bin/bash
# Minimal systemctl: the service "fails" when the active version contains a BROKEN marker
STATE="${HOST_AGENT_ROOT}/fake-systemctl"
mkdir -p "$STATE"
case "$1" in
  restart) echo restart >> "$STATE/log";;
  is-active)
    [[ "$2" == "--quiet" ]] && shift
    if unzip -l "${HOST_AGENT_ROOT}/opt/host-agent/current/host-agent.pyz" 2>/dev/null | grep -q BROKEN; then
      echo failed; exit 3
    fi
    echo active;;
  show) echo 0;;
  is-enabled) echo enabled;;
  *) echo "$*" >> "$STATE/log";;
esac
"""


def build_pyz(dest: Path, version: str, broken: bool = False) -> Path:
    """Build a minimal agent archive: real CLI code, patched version, no dependencies needed for
    `version` and `validate` (validate needs PyYAML, available in the test environment)."""
    work = dest.parent / f"build-{version}{'-broken' if broken else ''}"
    shutil.copytree(AGENT_SRC, work / "host_agent")
    init = work / "host_agent" / "__init__.py"
    init.write_text(init.read_text().replace('__version__ = "', f'__version__ = "{version}"  # "'))
    if broken:
        (work / "BROKEN").write_text("x")
    # PyYAML and paho from the test interpreter
    import paho
    import yaml

    shutil.copytree(Path(yaml.__file__).parent, work / "yaml")
    shutil.copytree(Path(paho.__file__).parent, work / "paho")
    with zipfile.ZipFile(dest, "w") as zf:
        for path in work.rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".so":
                continue
            zf.write(path, path.relative_to(work))
        zf.writestr("__main__.py", "from host_agent.cli import main\nraise SystemExit(main())\n")
    return dest


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "root"
    (root / "var/lib/host-agent-admin/incoming").mkdir(parents=True)
    (root / "opt/host-agent/versions").mkdir(parents=True)
    fake = tmp_path / "systemctl"
    fake.write_text(FAKE_SYSTEMCTL)
    fake.chmod(0o755)
    return {
        "root": root,
        "incoming": root / "var/lib/host-agent-admin/incoming",
        "env": {
            **os.environ,
            "HOST_AGENT_ROOT": str(root),
            "HOST_AGENT_SYSTEMCTL": str(fake),
            "HOST_AGENT_PYTHON": sys.executable,
            "HOST_AGENT_STABLE_WAIT": "0",
        },
        "tmp": tmp_path,
    }


def helper(env, *args):
    return subprocess.run([str(HELPER), *args], env=env["env"], capture_output=True, text=True, check=False)


def stage(env, pyz: Path, config: str | None = None, secret: str | None = None) -> str:
    shutil.copy(pyz, env["incoming"] / "host-agent.pyz")
    if config is not None:
        (env["incoming"] / "config.yaml").write_text(config)
    if secret is not None:
        (env["incoming"] / "mqtt.secret").write_text(secret)
    return hashlib.sha256(pyz.read_bytes()).hexdigest()


CONFIG = """node_id: bkup
mqtt:
  host: 192.0.2.1
  password_file: /etc/host-agent/mqtt.secret
"""


def current(env) -> str:
    return os.path.basename(os.readlink(env["root"] / "opt/host-agent/current"))


def test_install_upgrade_and_status(env):
    v1 = build_pyz(env["tmp"] / "v1.pyz", "0.2.0")
    res = helper(env, "install", "0.2.0", stage(env, v1, CONFIG, "s3cret"))
    assert res.returncode == 0, res.stderr
    assert "INSTALLED 0.2.0" in res.stdout
    assert current(env) == "0.2.0"
    conf = env["root"] / "etc/host-agent/config.yaml"
    assert conf.read_text() == CONFIG
    assert oct(conf.stat().st_mode)[-3:] == "640"
    assert not (env["incoming"] / "mqtt.secret").exists()
    assert (env["root"] / "usr/local/bin/host-agent").exists()

    v2 = build_pyz(env["tmp"] / "v2.pyz", "0.2.1")
    res = helper(env, "install", "0.2.1", stage(env, v2))
    assert res.returncode == 0, res.stderr
    assert current(env) == "0.2.1"

    status = dict(line.split("=", 1) for line in helper(env, "status").stdout.splitlines())
    assert status["version"] == "0.2.1"
    assert status["previous"] == "0.2.0"
    assert status["active"] == "active"
    assert status["config"] == "yes"


def test_failed_upgrade_rolls_back(env):
    v1 = build_pyz(env["tmp"] / "v1.pyz", "0.2.0")
    assert helper(env, "install", "0.2.0", stage(env, v1, CONFIG, "s3cret")).returncode == 0
    bad = build_pyz(env["tmp"] / "bad.pyz", "0.3.0", broken=True)
    new_config = CONFIG.replace("bkup", "other")
    res = helper(env, "install", "0.3.0", stage(env, bad, new_config))
    assert res.returncode == 3
    assert "ROLLED_BACK 0.2.0" in res.stdout
    assert current(env) == "0.2.0"
    # configuration restored too
    assert (env["root"] / "etc/host-agent/config.yaml").read_text() == CONFIG


def test_rejects_bad_input(env):
    v1 = build_pyz(env["tmp"] / "v1.pyz", "0.2.0")
    sha = stage(env, v1, CONFIG, "s3cret")
    assert helper(env, "install", "0.2.0", "0" * 64).returncode == 65  # checksum
    assert helper(env, "install", "0.9.9", sha).returncode == 65  # version mismatch
    assert helper(env, "install", "../../etc", sha).returncode == 64
    assert helper(env, "bogus").returncode == 64

    (env["incoming"] / "config.yaml").write_text("mqtt: {hots: x}\n")
    res = helper(env, "install", "0.2.0", sha)
    assert res.returncode == 65 and "invalid agent configuration" in res.stderr
    assert not (env["root"] / "opt/host-agent/current").exists()


def test_refuses_symlinked_incoming(env):
    v1 = build_pyz(env["tmp"] / "v1.pyz", "0.2.0")
    sha = stage(env, v1)
    secret = env["tmp"] / "shadow"
    secret.write_text("root:secret")
    (env["incoming"] / "config.yaml").symlink_to(secret)
    res = helper(env, "install", "0.2.0", sha)
    assert res.returncode == 65 and "refusing" in res.stderr


def test_configure_and_rollback(env):
    v1 = build_pyz(env["tmp"] / "v1.pyz", "0.2.0")
    assert helper(env, "install", "0.2.0", stage(env, v1, CONFIG, "s3cret")).returncode == 0
    v2 = build_pyz(env["tmp"] / "v2.pyz", "0.2.1")
    assert helper(env, "install", "0.2.1", stage(env, v2)).returncode == 0

    (env["incoming"] / "config.yaml").write_text(CONFIG.replace("192.0.2.1", "192.0.2.2"))
    res = helper(env, "configure")
    assert res.returncode == 0, res.stderr
    assert "192.0.2.2" in helper(env, "read-config").stdout

    res = helper(env, "rollback")
    assert res.returncode == 0 and "ROLLED_BACK 0.2.0" in res.stdout
    assert current(env) == "0.2.0"


def test_uninstall_purge(env):
    v1 = build_pyz(env["tmp"] / "v1.pyz", "0.2.0")
    assert helper(env, "install", "0.2.0", stage(env, v1, CONFIG, "s3cret")).returncode == 0
    res = helper(env, "uninstall", "--purge")
    assert res.returncode == 0
    assert not (env["root"] / "opt/host-agent").exists()
    assert not (env["root"] / "etc/host-agent").exists()


def test_reboot_returns_before_the_host_goes_down(env):
    import time

    res = helper(env, "reboot")
    assert res.returncode == 0, res.stderr
    assert "REBOOTING" in res.stdout
    log = env["root"] / "fake-systemctl" / "log"
    # delayed: nothing yet when the command returns, the reboot follows
    assert not log.exists() or "reboot" not in log.read_text()
    for _ in range(80):
        if log.exists() and "reboot" in log.read_text():
            break
        time.sleep(0.1)
    assert "reboot" in log.read_text()


FAKE_APT = """#!/bin/bash
# Fake apt-get: records its arguments and environment; "upgrade" fails when FAKE_APT_FAIL is set
echo "$*" >> "${HOST_AGENT_ROOT}/apt.log"
echo "DEBIAN_FRONTEND=${DEBIAN_FRONTEND}" >> "${HOST_AGENT_ROOT}/apt.log"
[[ "$*" == *upgrade* ]] && sleep "${FAKE_APT_SLEEP:-0}"
[[ "$*" == *upgrade* && -n "${FAKE_APT_FAIL:-}" ]] && { echo "E: broken"; exit 100; }
[[ "$*" == *update* && -n "${FAKE_APT_UPDATE_FAIL:-}" ]] && { echo "E: no network" >&2; exit 100; }
echo "done"
"""


@pytest.fixture
def apt(env):
    fake = env["tmp"] / "apt-get"
    fake.write_text(FAKE_APT)
    fake.chmod(0o755)
    env["env"]["HOST_AGENT_APT_GET"] = str(fake)
    # No dirvish backup by default (pgrep exits 1 when nothing matches)
    pgrep = env["tmp"] / "pgrep"
    pgrep.write_text("#!/bin/bash\nexit 1\n")
    pgrep.chmod(0o755)
    env["env"]["HOST_AGENT_PGREP"] = str(pgrep)
    return env


def wait_upgrade(env, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = helper(env, "apt-upgrade-status").stdout
        if "state=running" not in out:
            return out
        time.sleep(0.1)
    raise AssertionError("upgrade did not finish")


def test_apt_update(apt):
    res = helper(apt, "apt-update")
    assert res.returncode == 0, res.stderr
    assert "APT_UPDATED" in res.stdout
    assert "update" in (apt["root"] / "apt.log").read_text()


def test_apt_update_failure(apt):
    apt["env"]["FAKE_APT_UPDATE_FAIL"] = "1"
    res = helper(apt, "apt-update")
    assert res.returncode == 3
    assert "apt-get update failed" in res.stderr


def test_apt_not_available(env):
    env["env"]["HOST_AGENT_APT_GET"] = "definitely-not-installed"
    for command in ("apt-update", "apt-upgrade"):
        res = helper(env, command)
        assert res.returncode == 69
        assert "apt is not available" in res.stderr


def test_upgrade_keeps_configuration_and_is_unattended(apt):
    res = helper(apt, "apt-upgrade")
    assert res.returncode == 0, res.stderr
    assert "UPGRADE_STARTED" in res.stdout
    status = wait_upgrade(apt)
    assert "state=ok" in status and "exit=0" in status
    log = (apt["root"] / "apt.log").read_text()
    assert "--force-confold" in log and "--force-confdef" in log
    assert "DEBIAN_FRONTEND=noninteractive" in log
    # `upgrade`, never `dist-upgrade` (no package is ever removed)
    assert "dist-upgrade" not in log and "-y" in log
    assert "== apt-get upgrade" in helper(apt, "apt-upgrade-log").stdout


def test_upgrade_failure_is_reported(apt):
    apt["env"]["FAKE_APT_FAIL"] = "1"
    assert helper(apt, "apt-upgrade").returncode == 0
    status = wait_upgrade(apt)
    assert "state=failed" in status and "exit=100" in status
    assert "E: broken" in helper(apt, "apt-upgrade-log").stdout


def test_upgrade_refused_during_a_backup(apt):
    pgrep = apt["tmp"] / "pgrep"
    pgrep.write_text("#!/bin/bash\necho 4242\n")
    res = helper(apt, "apt-upgrade")
    assert res.returncode == 75
    assert "backup is running" in res.stderr
    assert "state=idle" in helper(apt, "apt-upgrade-status").stdout
    assert not (apt["root"] / "apt.log").exists()


def test_upgrade_not_started_twice_and_status_does_not_wait(apt):
    import time

    apt["env"]["FAKE_APT_SLEEP"] = "2"
    assert helper(apt, "apt-upgrade").returncode == 0
    time.sleep(0.5)
    begin = time.monotonic()
    assert "state=running" in helper(apt, "apt-upgrade-status").stdout
    # the lifecycle commands are not blocked by the upgrade either
    assert helper(apt, "status").returncode == 0
    assert time.monotonic() - begin < 1.5
    again = helper(apt, "apt-upgrade")
    assert again.returncode == 75 and "already running" in again.stderr
    assert helper(apt, "apt-update").returncode == 75
    assert "state=ok" in wait_upgrade(apt)


def test_dead_worker_is_reported_as_interrupted(apt):
    state = apt["root"] / "var/lib/host-agent-upgrade"
    state.mkdir(parents=True)
    (state / "state").write_text("state=running\nstarted=1\nfinished=\nexit=\n")
    status = helper(apt, "apt-upgrade-status").stdout
    assert "state=failed" in status and "exit=interrupted" in status
