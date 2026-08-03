#!/bin/bash
# Run the full TB21, GAIA, TAU3, and SWE-bench Pro native-agent matrix.
#
# The default queue is 4 sources x 3 models x 2 agents at pass@2:
# 24 sequential cells and 16,320 total trials. Agent order alternates globally
# because run-matrix.sh orders each source model-first, then agent.

set -uo pipefail

EVALS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCHESTRATOR_EVALS_DIR="${ORCHESTRATOR_EVALS_DIR:-/home/ubuntu/evals}"
export ORCHESTRATOR_EVALS_DIR
STATE_ROOT="${FULL_SUITE_STATE_ROOT:-/home/ubuntu/full-native-suite-runs}"
HARBOR_REPO_URL="${HARBOR_REPO_URL:-https://github.com/notowen333/harbor.git}"
REQUESTED_HARBOR_REF="${HARBOR_REF:-strands-working-fork}"
AGENTS="claude-code,opencode"
MODELS="opus-4.8,sonnet-5,sonnet-4.6"
N_ATTEMPTS=2
CONCURRENCY=""

SOURCE_NAMES=("tb21" "gaia" "tau3" "swe-bench-pro")
SOURCE_DATASETS=(
  "terminal-bench/terminal-bench-2-1"
  "gaia/gaia"
  "sierra-research/tau3-bench"
  "scale-ai/swe-bench-pro"
)
SOURCE_TASK_COUNTS=(89 165 375 731)

usage() {
  echo "Usage: run-full-native-suite.sh [-k attempts] [-m models] [-n concurrency] [-a agents]" >&2
}

while getopts "k:m:n:a:" opt; do
  case "$opt" in
    k) N_ATTEMPTS="$OPTARG" ;;
    m) MODELS="$OPTARG" ;;
    n) CONCURRENCY="$OPTARG" ;;
    a) AGENTS="$OPTARG" ;;
    *) usage; exit 2 ;;
  esac
done

if ! [[ "$N_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: attempts must be a positive integer, got: ${N_ATTEMPTS}" >&2
  exit 2
fi
if [ -n "$CONCURRENCY" ] && ! [[ "$CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: concurrency must be a positive integer, got: ${CONCURRENCY}" >&2
  exit 2
fi

pin_harbor_commit() {
  local ref="$REQUESTED_HARBOR_REF"
  if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "$ref"
    return 0
  fi
  git ls-remote "$HARBOR_REPO_URL" "$ref" | awk '{print $1}' | head -1
}

PINNED_HARBOR_SHA=""
if [ "${FULL_SUITE_DRY_RUN:-0}" != "1" ]; then
  PINNED_HARBOR_SHA="$(pin_harbor_commit)"
  if [ -z "$PINNED_HARBOR_SHA" ]; then
    echo "ERROR: Could not resolve Harbor commit for ref '${REQUESTED_HARBOR_REF}'." >&2
    exit 1
  fi
fi
HARBOR_VERSION_TAG="${PINNED_HARBOR_SHA:-$REQUESTED_HARBOR_REF}"
HARBOR_VERSION_TAG="${HARBOR_VERSION_TAG//\//-}"
HARBOR_VERSION_TAG="${HARBOR_VERSION_TAG:0:7}"
export HARBOR_REF="${PINNED_HARBOR_SHA:-$REQUESTED_HARBOR_REF}"

AGENT_STATE_ID="${AGENTS//,/+}"
AGENT_STATE_ID="${AGENT_STATE_ID//claude-code/claude-code@${CLAUDE_CODE_VERSION:-2.1.220}}"
AGENT_STATE_ID="${AGENT_STATE_ID//opencode/opencode@${OPENCODE_VERSION:-1.18.9}}"
MODEL_STATE_ID="${MODELS//,/+}"
SUITE_ID="full-native-suite--agents-${AGENT_STATE_ID}--models-${MODEL_STATE_ID}--harbor-${HARBOR_VERSION_TAG}--k${N_ATTEMPTS}"
STATE_DIR="${STATE_ROOT}/${SUITE_ID}"
SUITE_LOG="${STATE_DIR}/suite.log"
STATUS_FILE="${STATE_DIR}/status.tsv"

matrix_args() {
  local dataset="$1"
  MATRIX_ARGS=(
    -d "$dataset"
    -k "$N_ATTEMPTS"
    -m "$MODELS"
    -a "$AGENTS"
  )
  if [ -n "$CONCURRENCY" ]; then
    MATRIX_ARGS+=(-n "$CONCURRENCY")
  fi
}

if [ "${FULL_SUITE_DRY_RUN:-0}" = "1" ]; then
  echo "suite_id=${SUITE_ID}"
  echo "attempts=${N_ATTEMPTS}"
  echo "harbor_ref=${HARBOR_REF}"
  total_tasks=0
  total_cells=0
  total_trials=0
  for source_index in "${!SOURCE_NAMES[@]}"; do
    source="${SOURCE_NAMES[$source_index]}"
    dataset="${SOURCE_DATASETS[$source_index]}"
    tasks="${SOURCE_TASK_COUNTS[$source_index]}"
    total_tasks=$((total_tasks + tasks))
    printf 'source=%s\tdataset=%s\ttasks=%s\ttrials_per_cell=%s\n' \
      "$source" "$dataset" "$tasks" "$((tasks * N_ATTEMPTS))"
    matrix_args "$dataset"
    while IFS= read -r line; do
      if [[ "$line" == cell=* ]]; then
        printf 'cell=%s\t%s\n' "$source" "${line#cell=}"
        total_cells=$((total_cells + 1))
        total_trials=$((total_trials + tasks * N_ATTEMPTS))
      elif [[ "$line" == unsupported=* ]]; then
        printf 'unsupported=%s\t%s\n' "$source" "${line#unsupported=}"
      fi
    done < <(
      MATRIX_DRY_RUN=1 HARBOR_STRANDS_CHECKOUT=/nonexistent \
        bash "${EVALS_DIR}/strands-infra-runner/run-matrix.sh" "${MATRIX_ARGS[@]}"
    )
  done
  echo "total_tasks=${total_tasks}"
  echo "total_cells=${total_cells}"
  echo "total_trials=${total_trials}"
  exit 0
fi

mkdir -p "${STATE_DIR}/sources"
touch "$STATUS_FILE"
rm -f "${STATE_DIR}/COMPLETE" "${STATE_DIR}/INCOMPLETE"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$SUITE_LOG"
}

record() {
  printf '%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-}" >>"$STATUS_FILE"
}

log "=== Full native suite start: ${SUITE_ID} ==="
log "  Sources:  TB21 (89), GAIA (165), TAU3 (375), SWE-bench Pro (731)"
log "  Agents:   ${AGENT_STATE_ID}"
log "  Models:   ${MODELS}"
log "  Harbor:   ${PINNED_HARBOR_SHA} (from '${REQUESTED_HARBOR_REF}', pinned for all sources)"
log "  Attempts: k=${N_ATTEMPTS}"
log "  Queue:    24 sequential cells, 16,320 trials with defaults"
log "  Runner checkout: ${EVALS_DIR} (immutable code executed by this suite)"
log "  Runtime checkout: ${ORCHESTRATOR_EVALS_DIR} (.venv, jobs, reports, and viewer data; runner code is not executed here)"
log "  State:    ${STATE_DIR}"

FAILED_SOURCES=()
COMPLETED_SOURCES=()

for source_index in "${!SOURCE_NAMES[@]}"; do
  source="${SOURCE_NAMES[$source_index]}"
  dataset="${SOURCE_DATASETS[$source_index]}"
  tasks="${SOURCE_TASK_COUNTS[$source_index]}"
  checkpoint="${STATE_DIR}/sources/$((source_index + 1))-${source}.COMPLETE"
  matrix_args "$dataset"

  log "SOURCE ${source}: ${dataset}, ${tasks} tasks, $((tasks * N_ATTEMPTS)) trials per cell"
  record "$source" "RUNNING" "dataset=${dataset}"
  start_epoch="$(date -u +%s)"

  bash "${EVALS_DIR}/strands-infra-runner/run-matrix.sh" "${MATRIX_ARGS[@]}" \
    2>&1 | tee -a "$SUITE_LOG"
  source_exit="${PIPESTATUS[0]}"
  elapsed=$(( $(date -u +%s) - start_epoch ))

  if [ "$source_exit" -eq 0 ]; then
    date -u +%Y-%m-%dT%H:%M:%SZ >"$checkpoint"
    record "$source" "OK" "${elapsed}s checkpoint=${checkpoint}"
    COMPLETED_SOURCES+=("$source")
    log "SOURCE OK ${source} in ${elapsed}s"
  else
    rm -f "$checkpoint"
    record "$source" "FAIL" "exit=${source_exit} ${elapsed}s"
    FAILED_SOURCES+=("$source")
    log "SOURCE FAIL ${source} exit=${source_exit} after ${elapsed}s"
  fi
done

log "=== Full native suite complete: ${SUITE_ID} ==="
log "  OK:     ${#COMPLETED_SOURCES[@]} (${COMPLETED_SOURCES[*]:-none})"
log "  FAILED: ${#FAILED_SOURCES[@]} (${FAILED_SOURCES[*]:-none})"

if [ "${#FAILED_SOURCES[@]}" -eq 0 ]; then
  date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/COMPLETE"
  log "Wrote ${STATE_DIR}/COMPLETE"
else
  date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/INCOMPLETE"
  log "Wrote ${STATE_DIR}/INCOMPLETE"
  exit 1
fi
