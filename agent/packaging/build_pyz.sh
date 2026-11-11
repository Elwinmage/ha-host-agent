#!/usr/bin/env bash
# Build a self-contained, architecture independent host-agent.pyz.
# Usage: agent/packaging/build_pyz.sh [output_dir]
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-${AGENT_DIR}/dist}"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT

PYTHON="${PYTHON:-python3}"

# Runtime dependencies only, then drop compiled extensions: PyYAML falls back
# to its pure Python implementation, which keeps the archive portable (armv7/arm64/x86_64).
"${PYTHON}" -m pip install --quiet --no-compile --target "${BUILD_DIR}" "paho-mqtt>=2.0,<3" "PyYAML>=6.0"
find "${BUILD_DIR}" \( -name '*.so' -o -name '*.pyd' -o -name '__pycache__' \) -prune -exec rm -rf {} +
rm -rf "${BUILD_DIR:?}/bin" "${BUILD_DIR:?}/_yaml"

cp -r "${AGENT_DIR}/src/host_agent" "${BUILD_DIR}/"
find "${BUILD_DIR}/host_agent" -name '__pycache__' -prune -exec rm -rf {} +

VERSION="$("${PYTHON}" -c "import sys; sys.path.insert(0, '${AGENT_DIR}/src'); import host_agent; print(host_agent.__version__)")"
mkdir -p "${OUT_DIR}"
# Own entry point: zipapp's --main would discard the exit status of main()
printf 'from host_agent.cli import main\n\nraise SystemExit(main())\n' > "${BUILD_DIR}/__main__.py"
"${PYTHON}" -m zipapp "${BUILD_DIR}" \
    --python "/usr/bin/env python3" \
    --compress \
    --output "${OUT_DIR}/host-agent.pyz"
(cd "${OUT_DIR}" && sha256sum host-agent.pyz > host-agent.pyz.sha256)
echo "${OUT_DIR}/host-agent.pyz (version ${VERSION})"
