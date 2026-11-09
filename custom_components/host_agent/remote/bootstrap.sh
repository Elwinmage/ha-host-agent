#!/bin/sh
# One-time bootstrap run as root (through sudo) by the Home Assistant integration.
#
# Creates:
#   - "host-agent": system account running the agent (no shell, no home)
#   - "host-agent-admin": SSH account used by Home Assistant, key-only, allowed to
#     run nothing but /usr/local/lib/host-agent/host-agent-admin through sudo
# Installs the helper, the systemd unit and the sudoers rule. The admin password
# used to run this script is never stored by Home Assistant.
#
# Usage: bootstrap.sh <directory containing the uploaded files>
set -eu

DIR="${1:?usage: bootstrap.sh DIR}"
ADMIN=host-agent-admin
ADMIN_HOME=/var/lib/host-agent-admin

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing $1" >&2; exit 69; }; }
need useradd
need visudo
need systemctl
need flock
need sha256sum

if ! id -u host-agent >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin host-agent
fi
if ! id -u "${ADMIN}" >/dev/null 2>&1; then
    useradd --system --home-dir "${ADMIN_HOME}" --create-home --shell /bin/sh "${ADMIN}"
fi
# Key-only account: lock the password
usermod -L "${ADMIN}" >/dev/null 2>&1 || true

install -d -m 0750 -o "${ADMIN}" -g "${ADMIN}" "${ADMIN_HOME}"
install -d -m 0700 -o "${ADMIN}" -g "${ADMIN}" "${ADMIN_HOME}/.ssh" "${ADMIN_HOME}/incoming"
# "restrict": no port/agent/X11 forwarding, no pty; commands and SFTP still allowed
printf 'restrict %s\n' "$(cat "${DIR}/authorized_key.pub")" > "${ADMIN_HOME}/.ssh/authorized_keys"
chown "${ADMIN}:${ADMIN}" "${ADMIN_HOME}/.ssh/authorized_keys"
chmod 0600 "${ADMIN_HOME}/.ssh/authorized_keys"

install -d -m 0755 /usr/local/lib/host-agent
install -m 0755 -o root -g root "${DIR}/host-agent-admin" /usr/local/lib/host-agent/host-agent-admin

# Portable check (sudo and sudo-rs): show visudo's diagnostic on failure
if ! check_output="$(visudo -c -f "${DIR}/sudoers" 2>&1)"; then
    echo "ERROR: sudoers rule rejected by visudo: ${check_output}" >&2
    exit 65
fi
install -m 0440 -o root -g root "${DIR}/sudoers" /etc/sudoers.d/host-agent-admin

install -m 0644 -o root -g root "${DIR}/host-agent.service" /etc/systemd/system/host-agent.service
install -d -m 0755 /opt/host-agent/versions
install -d -m 0750 -o root -g host-agent /etc/host-agent
systemctl daemon-reload

# Former manual installation used the same layout: nothing else to migrate
echo "BOOTSTRAP_OK"
