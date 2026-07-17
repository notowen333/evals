#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOURCE_DIR="${STAN_PY_SOURCE:-${REPO_ROOT}/stan/stan-py/src/strands_stan}"
DEST_DIR="${SCRIPT_DIR}/strands_stan"

if [[ ! -f "${SOURCE_DIR}/agent.py" ]]; then
  echo "Stan Python source not found at ${SOURCE_DIR}" >&2
  exit 1
fi

mkdir -p "${DEST_DIR}"
cp -R "${SOURCE_DIR}/." "${DEST_DIR}/"
echo "Synchronized Stan Python source into ${DEST_DIR}"
