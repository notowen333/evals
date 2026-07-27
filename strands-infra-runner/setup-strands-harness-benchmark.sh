#!/bin/bash
# Materialize the custom 206-task Strands harness benchmark on the orchestrator.

set -euo pipefail

EVALS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HARBOR_REPO_URL="${HARBOR_REPO_URL:-https://github.com/notowen333/harbor.git}"
HARBOR_BRANCH="${HARBOR_BRANCH:-strands-working-fork}"
HARBOR_STRANDS_CHECKOUT="${HARBOR_STRANDS_CHECKOUT:-/home/ubuntu/harbor-strands-working}"
DATASET_NAME="strands-harness-benchmark-index"
ADAPTER_DIR="${HARBOR_STRANDS_CHECKOUT}/adapters/${DATASET_NAME}"
SOURCE_DIR="${HARBOR_STRANDS_CHECKOUT}/datasets/${DATASET_NAME}-sources"
DATASET_DIR="${HARBOR_STRANDS_CHECKOUT}/datasets/${DATASET_NAME}"
STAGING_DIR="${DATASET_DIR}.staging"

export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin

if [ ! -d "${HARBOR_STRANDS_CHECKOUT}/.git" ]; then
  git clone --branch "$HARBOR_BRANCH" --single-branch \
    "$HARBOR_REPO_URL" "$HARBOR_STRANDS_CHECKOUT"
else
  if [ -n "$(git -C "$HARBOR_STRANDS_CHECKOUT" status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: Tracked changes exist in ${HARBOR_STRANDS_CHECKOUT}" >&2
    echo "  Resolve them before updating the managed benchmark checkout" >&2
    exit 1
  fi
  git -C "$HARBOR_STRANDS_CHECKOUT" fetch origin "$HARBOR_BRANCH"
  git -C "$HARBOR_STRANDS_CHECKOUT" checkout "$HARBOR_BRANCH"
  git -C "$HARBOR_STRANDS_CHECKOUT" merge --ff-only FETCH_HEAD
fi

source "${EVALS_DIR}/.venv/bin/activate"

rm -rf "$STAGING_DIR"
python "${ADAPTER_DIR}/run_adapter.py" \
  --output-dir "$STAGING_DIR" \
  --download-sources-to "$SOURCE_DIR"

TASK_COUNT=$(find "$STAGING_DIR" -mindepth 2 -maxdepth 2 -name task.toml | wc -l)
if [ "$TASK_COUNT" -ne 206 ]; then
  echo "ERROR: Expected 206 generated tasks, found ${TASK_COUNT}" >&2
  exit 1
fi

for prefix_and_count in "tb-:28" "gaia-:47" "tau3-:48" "swebenchpro-:83"; do
  prefix="${prefix_and_count%%:*}"
  expected="${prefix_and_count##*:}"
  actual=$(find "$STAGING_DIR" -mindepth 1 -maxdepth 1 -type d -name "${prefix}*" | wc -l)
  if [ "$actual" -ne "$expected" ]; then
    echo "ERROR: Expected ${expected} ${prefix} tasks, found ${actual}" >&2
    exit 1
  fi
done

git -C "$HARBOR_STRANDS_CHECKOUT" rev-parse HEAD >"${STAGING_DIR}/.harbor-source-commit"
rm -rf "$DATASET_DIR"
mv "$STAGING_DIR" "$DATASET_DIR"

echo "Materialized ${TASK_COUNT} tasks at ${DATASET_DIR}"
echo "Harbor source: $(cat "${DATASET_DIR}/.harbor-source-commit")"
