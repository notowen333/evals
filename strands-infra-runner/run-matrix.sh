#!/bin/bash
# run-matrix.sh — Fire-and-forget pass@k matrix runner.
#
# Runs a set of models against a dataset SEQUENTIALLY, one model at a time, so
# the fleet never exceeds one model's worth of instances. Each model's trials
# still run at full concurrency inside its own run. Designed to be launched once
# via SSM + setsid and left alone overnight.
#
# Usage:
#   run-matrix.sh [options]
#
# Options (all optional, with defaults):
#   -d <dataset>     Harbor dataset            (default: strands-harness-benchmark-index)
#   -k <n>           Attempts per task         (default: 4)
#   -m <models>      Comma-separated models    (default: see DEFAULT_MODELS)
#   -n <concurrency> Concurrent trials per run (default: auto from vCPU budget)
#   -a <agent>       Agent name                (default: stan)
#
# Examples:
#   run-matrix.sh                              # full default matrix at pass@4
#   run-matrix.sh -k 8 -m opus-4.8,sonnet-4.6  # two models at pass@8
#   run-matrix.sh -d gaia/gaia -k 3
#
# State/logs:
#   /home/ubuntu/matrix-runs/<matrix-id>/matrix.log     ← driver log (tail this)
#   /home/ubuntu/matrix-runs/<matrix-id>/status.tsv     ← one line per cell
#   /home/ubuntu/matrix-runs/<matrix-id>/<cell>.log     ← per-model run log
#   /home/ubuntu/matrix-runs/<matrix-id>/COMPLETE       ← written when all done
#
# Resumability: re-running with the same -d/-k/-m reuses the same matrix-id and
# skips cells already marked OK in status.tsv.

set -uo pipefail

EVALS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_ROOT="${MATRIX_STATE_ROOT:-/home/ubuntu/matrix-runs}"

DATASET="strands-harness-benchmark-index"
N_ATTEMPTS=4
AGENT="stan"
CONCURRENCY=""
DEFAULT_MODELS="opus-4.8,openai.gpt-5.6-sol,sonnet-4.6,sonnet-5,openai.zai.glm-5,kimi-k2.5"
MODELS=""

while getopts "d:k:m:n:a:" opt; do
  case "$opt" in
    d) DATASET="$OPTARG" ;;
    k) N_ATTEMPTS="$OPTARG" ;;
    m) MODELS="$OPTARG" ;;
    n) CONCURRENCY="$OPTARG" ;;
    a) AGENT="$OPTARG" ;;
    *) echo "Unknown option" >&2; exit 2 ;;
  esac
done
MODELS="${MODELS:-$DEFAULT_MODELS}"

# --- Fleet sizing ---------------------------------------------------------
# The binding constraint is the EC2 On-Demand Standard vCPU quota in this
# region, NOT the task count. Each fleet node is m7i.xlarge = 4 vCPU. We spend
# at most FLEET_VCPU_BUDGET vCPU on fleet nodes at any moment, leaving the rest
# of the quota as headroom for the orchestrator and any manual runs.
#
# Because models run one at a time, peak fleet size == this cap, regardless of
# how many models or how large k is.
VCPU_PER_NODE=4
QUOTA_SAFETY_FRACTION="${QUOTA_SAFETY_FRACTION:-0.55}"

# Currently-available vCPU under our safety fraction of the regional quota.
vcpu_headroom() {
  python3 - "$QUOTA_SAFETY_FRACTION" <<'PY'
import sys

import boto3

safety = float(sys.argv[1])
try:
    quota = boto3.client("service-quotas", region_name="us-east-1").get_service_quota(
        ServiceCode="ec2", QuotaCode="L-1216C47A"
    )["Quota"]["Value"]
except Exception:
    quota = 640.0

running = 0
try:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                opts = inst.get("CpuOptions", {})
                running += opts.get("CoreCount", 1) * opts.get("ThreadsPerCore", 1)
except Exception:
    # Unknown usage: assume the budget is spent rather than over-provisioning.
    running = int(quota * safety)

print(max(0, int(quota * safety) - running))
PY
}

resolve_concurrency() {
  if [ -n "$CONCURRENCY" ]; then
    echo "$CONCURRENCY"
    return
  fi
  python3 - "$DATASET" "$N_ATTEMPTS" "$VCPU_PER_NODE" "$QUOTA_SAFETY_FRACTION" <<'PY'
import json
import sys

import boto3

dataset, n_attempts, vcpu_per_node, safety = sys.argv[1:]
n_attempts = int(n_attempts)
vcpu_per_node = int(vcpu_per_node)
safety = float(safety)

try:
    with open("/home/ubuntu/evals/strands-infra-runner/datasets.json") as fh:
        tasks = int(json.load(fh).get(dataset, 500))
except Exception:
    tasks = 500

# vCPU quota headroom, minus what is already running.
try:
    quota = boto3.client("service-quotas", region_name="us-east-1").get_service_quota(
        ServiceCode="ec2", QuotaCode="L-1216C47A"
    )["Quota"]["Value"]
except Exception:
    quota = 640.0

try:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    running = 0
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                running += inst.get("CpuOptions", {}).get("CoreCount", 1) * inst.get(
                    "CpuOptions", {}
                ).get("ThreadsPerCore", 1)
except Exception:
    running = 32

budget = max(0.0, quota * safety - running)
by_quota = int(budget // vcpu_per_node)

# Cap at the TASK count, not tasks*attempts. The k=1 baselines showed the real
# ceiling is Bedrock throttling (53 ApiRateLimitError on sonnet-5), not EC2:
# holding concurrency at one-pass width keeps the model request rate identical to
# those runs and lets the k attempts spread over time instead of all at once.
# Also capped at Harbor's practical per-orchestrator ceiling (SSH sessions/FDs).
print(max(1, min(by_quota, tasks, 500)))
PY
}

RESOLVED_CONCURRENCY="$(resolve_concurrency)"

DATASET_SLUG="${DATASET//\//-}"
MATRIX_ID="${DATASET_SLUG}--k${N_ATTEMPTS}"
STATE_DIR="${STATE_ROOT}/${MATRIX_ID}"
MATRIX_LOG="${STATE_DIR}/matrix.log"
STATUS_FILE="${STATE_DIR}/status.tsv"

mkdir -p "$STATE_DIR"
touch "$STATUS_FILE"

log() {
  # Timestamped, unbuffered, and duplicated to stdout so both the driver log and
  # the SSM-captured launch output show progress.
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$MATRIX_LOG"
}

cell_status() {
  # Last recorded status for a cell, or empty if never run.
  awk -F'\t' -v c="$1" '$2 == c {s = $3} END {print s}' "$STATUS_FILE"
}

record() {
  printf '%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-}" >>"$STATUS_FILE"
}

IFS=',' read -r -a MODEL_LIST <<<"$MODELS"

log "=== Matrix start ==="
log "  Matrix ID:   ${MATRIX_ID}"
log "  Dataset:     ${DATASET}"
log "  Attempts:    k=${N_ATTEMPTS}"
log "  Agent:       ${AGENT}"
log "  Models:      ${MODELS}"
log "  Concurrency: ${RESOLVED_CONCURRENCY} nodes ($((RESOLVED_CONCURRENCY * VCPU_PER_NODE)) vCPU peak, one model at a time)"
log "  State dir:   ${STATE_DIR}"

rm -f "${STATE_DIR}/COMPLETE"

FAILED=()
SKIPPED=()
SUCCEEDED=()

for MODEL in "${MODEL_LIST[@]}"; do
  MODEL="$(echo "$MODEL" | tr -d '[:space:]')"
  [ -z "$MODEL" ] && continue

  PRIOR="$(cell_status "$MODEL")"
  if [ "$PRIOR" = "OK" ]; then
    log "SKIP ${MODEL} — already completed in this matrix (status.tsv)"
    SKIPPED+=("$MODEL")
    continue
  fi

  CELL_LOG="${STATE_DIR}/${MODEL}.log"
  log "RUN  ${MODEL} → ${CELL_LOG}"
  record "$MODEL" "RUNNING" ""

  # Block until the region has enough vCPU headroom for this cell. Checking the
  # quota (rather than just "are any fleet nodes alive") means an unrelated run
  # in this account delays us instead of being ignored or, worse, killed.
  NEEDED_VCPU=$((RESOLVED_CONCURRENCY * VCPU_PER_NODE))
  for _ in $(seq 1 240); do
    HEADROOM=$(vcpu_headroom)
    [ "${HEADROOM:-0}" -ge "$NEEDED_VCPU" ] && break
    log "     waiting for vCPU headroom: have ${HEADROOM}, need ${NEEDED_VCPU}"
    sleep 60
  done

  START_EPOCH=$(date -u +%s)

  # Each cell is a normal run-benchmark.sh invocation. JOB_NAME_SUFFIX keeps the
  # pass@k results in their own job dir and S3 prefix.
  JOB_NAME_SUFFIX="--k${N_ATTEMPTS}" \
  N_ATTEMPTS="$N_ATTEMPTS" \
    bash "${EVALS_DIR}/strands-infra-runner/run-benchmark.sh" \
      "$AGENT" "$MODEL" "$DATASET" "$RESOLVED_CONCURRENCY" \
      </dev/null >"$CELL_LOG" 2>&1
  CELL_EXIT=$?

  ELAPSED=$(( $(date -u +%s) - START_EPOCH ))
  if [ "$CELL_EXIT" -eq 0 ]; then
    log "OK   ${MODEL} in ${ELAPSED}s"
    record "$MODEL" "OK" "${ELAPSED}s"
    SUCCEEDED+=("$MODEL")
  else
    log "FAIL ${MODEL} exit=${CELL_EXIT} after ${ELAPSED}s — see ${CELL_LOG}"
    record "$MODEL" "FAIL" "exit=${CELL_EXIT} ${ELAPSED}s"
    FAILED+=("$MODEL")
  fi

  # Belt-and-braces: terminate anything this cell's own cleanup handler missed,
  # so a crashed cell cannot leak vCPU into the next one. Scoped by the
  # harbor:job tag that run.py stamps on every node it launches — never a blanket
  # sweep, which would kill instances belonging to someone else's run.
  CELL_JOB_TAG="${AGENT}@*--${MODEL}--${DATASET_SLUG}--k${N_ATTEMPTS}"
  ORPHANS=$(aws ec2 describe-instances --region us-east-1 \
    --filters Name=instance-state-name,Values=running,pending \
              Name=key-name,Values=harbor-benchmark \
              "Name=tag:harbor:job,Values=${CELL_JOB_TAG}" \
    --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null || true)
  if [ -n "${ORPHANS// /}" ]; then
    log "     terminating orphan fleet instances for this cell: ${ORPHANS}"
    aws ec2 terminate-instances --region us-east-1 --instance-ids $ORPHANS >/dev/null 2>&1 || true
  fi
done

log "=== Matrix complete ==="
log "  OK:      ${#SUCCEEDED[@]} (${SUCCEEDED[*]:-none})"
log "  FAILED:  ${#FAILED[@]} (${FAILED[*]:-none})"
log "  SKIPPED: ${#SKIPPED[@]} (${SKIPPED[*]:-none})"

# Final S3 sync so results are durable even if the 5-minute mirror cron is behind.
log "Final S3 mirror sync..."
aws s3 sync /home/ubuntu/evals/jobs/ s3://strands-benchmark-results-mirror/jobs/ \
  --region us-east-1 --only-show-errors 2>&1 | tee -a "$MATRIX_LOG"

# Scoreboard across every cell that produced a result.json.
log "=== Scoreboard ==="
python3 - "$MATRIX_ID" "$N_ATTEMPTS" <<'PY' 2>&1 | tee -a "$MATRIX_LOG"
import glob
import json
import os
import sys

matrix_id, n_attempts = sys.argv[1:]
suffix = f"--k{n_attempts}"
rows = []
for path in sorted(glob.glob(f"/home/ubuntu/evals/jobs/*{suffix}/result.json")):
    job = os.path.basename(os.path.dirname(path))
    if matrix_id.rsplit("--k", 1)[0] not in job:
        continue
    try:
        with open(path) as fh:
            result = json.load(fh)
    except Exception as exc:
        rows.append((job, f"unreadable result.json: {exc}"))
        continue
    stats = result.get("stats") or {}
    for evals_key, evals in (stats.get("evals") or {}).items():
        merged = {}
        for metric in evals.get("metrics") or []:
            merged.update(metric)
        pak = evals.get("pass_at_k") or {}
        rows.append(
            (
                job,
                f"n={evals.get('n_trials')} errors={evals.get('n_errors')} "
                f"mean={merged.get('mean')} equal_weighted={merged.get('equal_weighted_mean')} "
                f"pass@k={ {k: round(v, 4) for k, v in sorted(pak.items())} }",
            )
        )

if not rows:
    print("  (no result.json files found)")
for job, line in rows:
    print(f"  {job}\n      {line}")
PY

date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/COMPLETE"
log "Wrote ${STATE_DIR}/COMPLETE"

[ "${#FAILED[@]}" -eq 0 ] || exit 1
