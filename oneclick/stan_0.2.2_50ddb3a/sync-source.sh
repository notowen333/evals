#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STAN_REPO="${STAN_REPO:-${REPO_ROOT}/stan}"
PIN_FILE="${SCRIPT_DIR}/STAN_COMMIT"
DEST_DIR="${SCRIPT_DIR}/strands_stan"
RELEASE_DIR="$(basename "$SCRIPT_DIR")"

if [[ ! -f "$PIN_FILE" ]]; then
  echo "Missing Stan commit pin: ${PIN_FILE}" >&2
  exit 1
fi

STAN_COMMIT="$(tr -d '[:space:]' < "$PIN_FILE")"
if [[ ! "$STAN_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STAN_COMMIT must contain one full lowercase 40-character Git SHA" >&2
  exit 1
fi

STAN_SHORT="${STAN_COMMIT:0:7}"
if [[ "$RELEASE_DIR" != *"_${STAN_SHORT}" ]]; then
  echo "Release directory ${RELEASE_DIR} must end with _${STAN_SHORT}" >&2
  exit 1
fi

if [[ ! -d "${STAN_REPO}/.git" ]]; then
  echo "Stan Git checkout not found at ${STAN_REPO}" >&2
  exit 1
fi

if ! git -C "$STAN_REPO" cat-file -e "${STAN_COMMIT}^{commit}" 2>/dev/null; then
  echo "Stan commit ${STAN_COMMIT} is not available in ${STAN_REPO}" >&2
  echo "Fetch the Stan remote, then retry" >&2
  exit 1
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/oneclick-stan.XXXXXX")"
trap 'rm -rf "$TEMP_DIR"' EXIT

git -C "$STAN_REPO" archive "$STAN_COMMIT" stan-py/src/strands_stan |
  tar -x -C "$TEMP_DIR"

rm -rf "$DEST_DIR"
cp -R "${TEMP_DIR}/stan-py/src/strands_stan" "$DEST_DIR"
printf '%s\n' "$STAN_COMMIT" > "${DEST_DIR}/.stan-commit"

echo "Synchronized Stan ${STAN_SHORT} into ${DEST_DIR}"
