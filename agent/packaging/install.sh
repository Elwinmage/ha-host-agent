#!/usr/bin/env bash
# Manual installation of ha-host-agent (the HA integration will automate these steps).
# Usage: sudo ./install.sh host-agent.pyz [config.yaml]
set -euo pipefail

PYZ="${1:?usage: install.sh host-agent.pyz [config.yaml]}"
CONFIG_SRC="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/host-agent
CONF_DIR=/etc/host-agent
SERVICE=host-agent.service

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

python3 - <<'EOF' || { echo "Python >= 3.11 is required" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF
command -v smartctl >/dev/null || echo "WARNING: smartctl not found (apt install smartmontools)"

# Dedicated system user without shell nor home
if ! id host-agent >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin host-agent
fi

# Read access for the GPU, Raspberry Pi firmware and kernel log collectors
for group in video render systemd-journal adm; do
    getent group "${group}" >/dev/null 2>&1 && usermod -aG "${group}" host-agent || true
done

VERSION="$(python3 "${PYZ}" version | awk '{print $2}')"
install -d -m 0755 "${PREFIX}/versions/${VERSION}"
install -m 0644 "${PYZ}" "${PREFIX}/versions/${VERSION}/host-agent.pyz"
PREVIOUS="$(readlink -f "${PREFIX}/current" 2>/dev/null || true)"
ln -sfn "${PREFIX}/versions/${VERSION}" "${PREFIX}/current"

install -d -m 0750 -o root -g host-agent "${CONF_DIR}"
if [[ ! -f "${CONF_DIR}/config.yaml" ]]; then
    install -m 0640 -o root -g host-agent "${CONFIG_SRC:-${HERE}/config.example.yaml}" "${CONF_DIR}/config.yaml"
    echo "Created ${CONF_DIR}/config.yaml: edit it (mqtt section) before starting"
fi
if [[ ! -f "${CONF_DIR}/mqtt.secret" ]]; then
    install -m 0640 -o root -g host-agent /dev/null "${CONF_DIR}/mqtt.secret"
    echo "Put the MQTT password in ${CONF_DIR}/mqtt.secret"
fi

# CLI wrapper: always runs the active version
cat > /usr/local/bin/host-agent <<'WRAPPER'
#!/bin/sh
exec /usr/bin/python3 /opt/host-agent/current/host-agent.pyz --config "${HOST_AGENT_CONFIG:-/etc/host-agent/config.yaml}" "$@"
WRAPPER
chmod 0755 /usr/local/bin/host-agent

install -m 0644 "${HERE}/${SERVICE}" "/etc/systemd/system/${SERVICE}"
systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null

if [[ -s "${CONF_DIR}/mqtt.secret" ]] || grep -qE '^\s*password:' "${CONF_DIR}/config.yaml"; then
    systemctl restart "${SERVICE}"
    sleep 5
    if ! systemctl is-active --quiet "${SERVICE}"; then
        echo "Service failed to start" >&2
        if [[ -n "${PREVIOUS}" && "${PREVIOUS}" != "$(readlink -f "${PREFIX}/current")" ]]; then
            echo "Rolling back to ${PREVIOUS}" >&2
            ln -sfn "${PREVIOUS}" "${PREFIX}/current"
            systemctl restart "${SERVICE}"
        fi
        journalctl -u "${SERVICE}" -n 30 --no-pager >&2
        exit 1
    fi
    echo "ha-host-agent ${VERSION} running"
else
    echo "ha-host-agent ${VERSION} installed; start it with: systemctl start ${SERVICE}"
fi
