#!/usr/bin/env bash
# Build the agent and embed it in the integration (done by CI at release time).
# Usage: scripts/bundle_agent.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${REPO}/custom_components/host_agent/agent"

agent_version="$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' "${REPO}/agent/src/host_agent/__init__.py")"
manifest_version="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" \
    "${REPO}/custom_components/host_agent/manifest.json")"
if [[ "${agent_version}" != "${manifest_version}" ]]; then
    echo "Version mismatch: agent ${agent_version}, manifest ${manifest_version}" >&2
    exit 1
fi

"${REPO}/agent/packaging/build_pyz.sh" "${DEST}"
cp "${REPO}/agent/packaging/host-agent.service" "${DEST}/host-agent.service"
echo "Bundled agent ${agent_version} in ${DEST}"
