#!/bin/bash
# run-matrix.sh — Fire-and-forget pass@k matrix launcher.
#
# Launches the model matrix against a dataset and walks away. Two concurrency
# limits are respected:
#
#   EC2:   models run SEQUENTIALLY, so peak fleet size is one cell's worth of
#          instances no matter how many models or how large k is.
#   Tokens: per-cell concurrency is capped per model (see MODEL_CONCURRENCY),
#          because Bedrock throttling — not EC2 — was the largest error class in
#          the k=1 baselines (sonnet-5: 53/206 ApiRateLimitError).
#
# Usage:
#   run-matrix.sh [-d dataset] [-k attempts] [-m models] [-n concurrency] [-a agent]
#
# Examples:
#   run-matrix.sh                                  # 5-model matrix at pass@2
#   run-matrix.sh -k 4                             # same matrix at pass@4
#   run-matrix.sh -m opus-4.8,sonnet-4.6 -k 2      # just two models
#
# State/logs (tail these):
#   <state>/matrix.log    driver log — one line per cell start/finish
#   <state>/status.tsv    machine-readable cell status
#   <state>/<model>.log   full run log for that cell
#   <state>/COMPLETE      written when the whole matrix finishes
#   where <state> = /home/ubuntu/matrix-runs/<dataset-slug>--k<N>/
#
# Resumable: re-run with the same -d/-k and cells already marked OK are skipped.

set -uo pipefail

EVALS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_ROOT="${MATRIX_STATE_ROOT:-/home/ubuntu/matrix-runs}"
HARBOR_STRANDS_CHECKOUT="${HARBOR_STRANDS_CHECKOUT:-/home/ubuntu/harbor-strands-working}"

DATASET="strands-harness-benchmark-index"
N_ATTEMPTS=2
AGENT="stan"
CONCURRENCY=""

# The five models with full k=1 baselines on the index.
DEFAULT_MODELS="opus-4.8,openai.gpt-5.6-sol,sonnet-5,openai.zai.glm-5,kimi-k2.5"
MODELS=""

# --- Token-concurrency caps (nodes) --------------------------------------
# Max concurrent trials per model, i.e. max in-flight requests to that endpoint.
# Anything not listed uses DEFAULT_MODEL_CONCURRENCY. Lower = fewer
# ApiRateLimitErrors, longer wall clock. Tuned from the k=1 baseline error rates:
#   sonnet-5     53/206 throttled at ~206 concurrent → cap hard
#   glm-5/kimi   mantle + moonshot, moderate error rates → cap moderately
#   opus/gpt-sol 8 and 28 errors, mostly not throttling → run wide
DEFAULT_MODEL_CONCURRENCY=120

model_token_cap() {
  case "$1" in
    opus-4.8|opus)          echo 206 ;;  # 8/206 errors at full width — run wide
    openai.gpt-5.6-sol)     echo 206 ;;  # 28/206, mostly stream drops not 429s
    sonnet-5)               echo 80  ;;  # 53/206 throttled — cap hard
    sonnet-4.6|sonnet)      echo 120 ;;
    openai.zai.glm-5)       echo 100 ;;  # mantle proxy, moderate error rate
    kimi-k2.5|kimi)         echo 100 ;;
    *)                      echo "$DEFAULT_MODEL_CONCURRENCY" ;;
  esac
}

while getopts "d:k:m:n:a:" opt; do
  case "$opt" in
    d) DATASET="$OPTARG" ;;
    k) N_ATTEMPTS="$OPTARG" ;;
    m) MODELS="$OPTARG" ;;
    n) CONCURRENCY="$OPTARG" ;;
    a) AGENT="$OPTARG" ;;
    *) echo "Usage: run-matrix.sh [-d dataset] [-k attempts] [-m models] [-n concurrency] [-a agent]" >&2; exit 2 ;;
  esac
done
MODELS="${MODELS:-$DEFAULT_MODELS}"

# --- EC2 ceiling ---------------------------------------------------------
# Static on purpose: a full wave of the 206-task index is 824 vCPU, well under
# the account's 9216 vCPU On-Demand Standard quota, so EC2 headroom is not the
# binding constraint and a quota API lookup would only add a silent failure mode.
VCPU_PER_NODE=4
MAX_FLEET_VCPU="${MAX_FLEET_VCPU:-2048}"
MAX_FLEET_NODES=$((MAX_FLEET_VCPU / VCPU_PER_NODE))

# Task count for this dataset. For the locally-materialized index, count dirs on
# disk — datasets.json carries a hardcoded number that goes stale as the index
# grows (206 today, ~250 expected).
dataset_task_count() {
  local local_ds="${HARBOR_STRANDS_CHECKOUT}/datasets/${DATASET}" n=""
  if [ -d "$local_ds" ]; then
    n=$(find "$local_ds" -mindepth 2 -maxdepth 2 -name task.toml | wc -l | tr -d ' ')
  fi
  if [ -z "$n" ] || [ "$n" -eq 0 ]; then
    n=$(python3 -c "
import json
try:
    with open('${EVALS_DIR}/strands-infra-runner/datasets.json') as fh:
        print(int(json.load(fh).get('${DATASET}', 500)))
except Exception:
    print(500)
")
  fi
  echo "$n"
}

TASK_COUNT="$(dataset_task_count)"

# --- Pin the agent version for the WHOLE matrix ---------------------------
# Each cell would otherwise resolve Stan's branch HEAD independently. Over a
# 10h+ matrix, a push to Stan mid-run would silently give later models a
# different agent version than earlier ones, making the comparison invalid.
# Resolve the SHA once here and hand every cell that exact commit.
pin_stan_commit() {
  local pat ref="${STAN_BRANCH:-main}"
  pat=$(python3 -c "
import json, boto3
c = boto3.client('secretsmanager', region_name='us-east-1')
s = c.get_secret_value(SecretId='arn:aws:secretsmanager:us-east-1:879381280403:secret:stan_pat-lUflBx')
print(json.loads(s['SecretString'])['stan_pat'])
" 2>/dev/null) || return 1
  git ls-remote "https://x-access-token:${pat}@github.com/awsarron/stan.git" "$ref" \
    | awk '{print $1}' | head -1
}

PINNED_STAN_SHA=""
if [[ "$AGENT" == stan* ]]; then
  PINNED_STAN_SHA="$(pin_stan_commit)"
  if [ -z "$PINNED_STAN_SHA" ]; then
    echo "ERROR: Could not resolve Stan commit for ref '${STAN_BRANCH:-main}'." >&2
    echo "  Refusing to start: cells would each resolve their own SHA and the" >&2
    echo "  matrix would not be internally comparable." >&2
    exit 1
  fi
fi

# Concurrency for one cell: never more than there are tasks, never above this
# model's token cap, never above the EC2 ceiling or Harbor's SSH/FD limit.
cell_concurrency() {
  local model="$1"
  if [ -n "$CONCURRENCY" ]; then
    echo "$CONCURRENCY"
    return
  fi
  local cap
  cap="$(model_token_cap "$model")"
  python3 -c "print(max(1, min(${TASK_COUNT}, ${cap}, ${MAX_FLEET_NODES}, 500)))"
}

# vCPU currently consumed by running/pending instances in the region.
fleet_vcpu_in_use() {
  python3 <<'PY'
import sys

import boto3

try:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    total = 0
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                opts = inst.get("CpuOptions", {})
                total += opts.get("CoreCount", 1) * opts.get("ThreadsPerCore", 1)
    print(total)
except Exception as exc:
    # Unknown usage: report the ceiling so callers wait rather than pile on.
    print(f"WARNING: describe-instances failed: {exc}", file=sys.stderr)
    print(10**9)
PY
}

DATASET_SLUG="${DATASET//\//-}"
MATRIX_ID="${DATASET_SLUG}--k${N_ATTEMPTS}"
STATE_DIR="${STATE_ROOT}/${MATRIX_ID}"
MATRIX_LOG="${STATE_DIR}/matrix.log"
STATUS_FILE="${STATE_DIR}/status.tsv"

mkdir -p "$STATE_DIR"
touch "$STATUS_FILE"

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$MATRIX_LOG"
}

cell_status() {
  awk -F'\t' -v c="$1" '$2 == c {s = $3} END {print s}' "$STATUS_FILE"
}

record() {
  printf '%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "${3:-}" >>"$STATUS_FILE"
}

IFS=',' read -r -a MODEL_LIST <<<"$MODELS"

log "=== Matrix start: ${MATRIX_ID} ==="
log "  Dataset:  ${DATASET} (${TASK_COUNT} tasks)"
log "  Attempts: k=${N_ATTEMPTS}  →  $((TASK_COUNT * N_ATTEMPTS)) trials per model"
log "  Agent:    ${AGENT}${PINNED_STAN_SHA:+ @ ${PINNED_STAN_SHA:0:7} (pinned for all cells)}"
log "  Models:   ${MODELS}"
log "  EC2 ceiling: ${MAX_FLEET_VCPU} vCPU (${MAX_FLEET_NODES} nodes), one model at a time"
for m in "${MODEL_LIST[@]}"; do
  m="$(echo "$m" | tr -d '[:space:]')"
  [ -z "$m" ] && continue
  log "    ${m}: $(cell_concurrency "$m") concurrent trials"
done
log "  State:    ${STATE_DIR}"

rm -f "${STATE_DIR}/COMPLETE"

# --- Preflight -------------------------------------------------------------
# Fail in the first minute rather than at 3am after burning a cell. Every check
# here corresponds to a failure we have actually hit on this rig.
preflight() {
  local fail=0

  # Orchestrator creds. If these are dead, every cell dies identically.
  if ! aws sts get-caller-identity --region us-east-1 >/dev/null 2>&1; then
    echo "PREFLIGHT FAIL: AWS credentials are not usable on the orchestrator" >&2
    fail=1
  fi

  # Disk. A 206-task job dir is ~166MB; k=2 doubles it, times N models.
  local avail_gb needed_gb
  avail_gb=$(df -BG --output=avail /home/ubuntu | tail -1 | tr -dc '0-9')
  needed_gb=$(python3 -c "print(max(5, int(0.18 * ${TASK_COUNT} * ${N_ATTEMPTS} * ${#MODEL_LIST[@]} / 1024) + 5))")
  if [ "${avail_gb:-0}" -lt "$needed_gb" ]; then
    echo "PREFLIGHT FAIL: only ${avail_gb}GB free on /home/ubuntu, need ~${needed_gb}GB" >&2
    fail=1
  else
    log "  preflight: disk ${avail_gb}GB free (need ~${needed_gb}GB) OK"
  fi

  # Dataset is materialized and matches the adapter's declared size.
  local ds="${HARBOR_STRANDS_CHECKOUT}/datasets/${DATASET}"
  local adapter="${HARBOR_STRANDS_CHECKOUT}/adapters/${DATASET}"
  if [ -d "$ds" ]; then
    local expected
    expected=$(python3 -c "
import json
try:
    with open('${adapter}/adapter_metadata.json') as fh:
        print(json.load(fh)[0]['harbor_adapter'][0]['adapted_benchmark_size'])
except Exception:
    print('')
" 2>/dev/null)
    if [ -n "$expected" ] && [ "$TASK_COUNT" -ne "$expected" ]; then
      echo "PREFLIGHT FAIL: dataset has ${TASK_COUNT} tasks, adapter declares ${expected}" >&2
      echo "  Re-run: bash strands-infra-runner/setup-strands-harness-benchmark.sh" >&2
      fail=1
    else
      log "  preflight: dataset ${TASK_COUNT} tasks OK"
    fi
    if [ ! -f "${adapter}/metric_plugin.py" ]; then
      echo "PREFLIGHT FAIL: metric plugin missing at ${adapter}/metric_plugin.py" >&2
      fail=1
    fi
  fi

  # Mantle key: needed by the tau3 slice of the index AND by any openai.* model.
  if ! python3 -c "
import boto3
boto3.client('secretsmanager', region_name='us-east-1').get_secret_value(
    SecretId='${BEDROCK_API_KEY_SECRET_ID:-bedrock_api_key}')
" >/dev/null 2>&1; then
    echo "PREFLIGHT FAIL: cannot read Secrets Manager '${BEDROCK_API_KEY_SECRET_ID:-bedrock_api_key}'" >&2
    echo "  The tau3 slice of the index and all openai.* models need this." >&2
    fail=1
  else
    log "  preflight: mantle API key readable OK"
  fi

  # Every model resolves to a real endpoint. A bad alias silently falls through
  # to a raw model ID and fails all 206 tasks (this is how the kimi run died:
  # 724/734 ValidationException on 'kimi-k2.5').
  local m
  for m in "${MODEL_LIST[@]}"; do
    m="$(echo "$m" | tr -d '[:space:]')"
    [ -z "$m" ] && continue
    if ! MODEL_CHECK="$m" python3 - <<'PY'
import os
import sys

import boto3

alias = os.environ["MODEL_CHECK"]
aliases = {
    "sonnet-4.6": "us.anthropic.claude-sonnet-4-6",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "opus-4.6": "global.anthropic.claude-opus-4-6-v1",
    "opus-4.8": "global.anthropic.claude-opus-4-8",
    "opus": "global.anthropic.claude-opus-4-8",
    "sonnet-5": "global.anthropic.claude-sonnet-5",
    "sonnet5": "global.anthropic.claude-sonnet-5",
    "kimi-k2.5": "moonshotai.kimi-k2.5",
    "kimi-2.5": "moonshotai.kimi-k2.5",
    "kimi": "moonshotai.kimi-k2.5",
}
model_id = aliases.get(alias, alias)

# openai.* models go through the Mantle proxy, not Bedrock's model registry.
if model_id.startswith("openai."):
    sys.exit(0)

base = model_id.split(".", 1)[1] if model_id.split(".", 1)[0] in {
    "us", "global", "eu", "apac"
} else model_id
known = {
    m["modelId"]
    for m in boto3.client("bedrock", region_name="us-east-1").list_foundation_models()[
        "modelSummaries"
    ]
}
if not any(k == base or k.startswith(base) for k in known):
    print(f"  '{alias}' -> '{model_id}' not found in Bedrock us-east-1", file=sys.stderr)
    sys.exit(1)
PY
    then
      echo "PREFLIGHT FAIL: model '${m}' does not resolve to a known endpoint" >&2
      fail=1
    fi
  done
  [ "$fail" -eq 0 ] && log "  preflight: all ${#MODEL_LIST[@]} models resolve OK"

  return "$fail"
}

if [ "${SKIP_PREFLIGHT:-0}" != "1" ]; then
  log "Running preflight checks..."
  if ! preflight; then
    log "ABORTING: preflight failed. Fix the above, or set SKIP_PREFLIGHT=1 to override."
    exit 2
  fi
  log "Preflight passed."
fi

FAILED=()
SKIPPED=()
SUCCEEDED=()

for MODEL in "${MODEL_LIST[@]}"; do
  MODEL="$(echo "$MODEL" | tr -d '[:space:]')"
  [ -z "$MODEL" ] && continue

  if [ "$(cell_status "$MODEL")" = "OK" ]; then
    log "SKIP ${MODEL} — already completed in this matrix"
    SKIPPED+=("$MODEL")
    continue
  fi

  CELL_CONCURRENCY="$(cell_concurrency "$MODEL")"
  CELL_LOG="${STATE_DIR}/${MODEL}.log"
  log "RUN  ${MODEL} at -n ${CELL_CONCURRENCY} → ${CELL_LOG}"
  record "$MODEL" "RUNNING" "n=${CELL_CONCURRENCY}"

  # Wait for EC2 room under the ceiling. Measuring real usage (not just our own
  # nodes) means an unrelated run delays us instead of being ignored or killed.
  # Bounded so a permanently-busy account can't hang the matrix overnight.
  NEEDED_VCPU=$((CELL_CONCURRENCY * VCPU_PER_NODE))
  for i in $(seq 1 240); do
    IN_USE=$(fleet_vcpu_in_use)
    [ $((IN_USE + NEEDED_VCPU)) -le "$MAX_FLEET_VCPU" ] && break
    if [ "$i" -eq 240 ]; then
      log "     WARNING: ${IN_USE} vCPU still in use after 4h; proceeding anyway"
      break
    fi
    log "     waiting for EC2 room: ${IN_USE} in use, need ${NEEDED_VCPU}, ceiling ${MAX_FLEET_VCPU}"
    sleep 60
  done

  START_EPOCH=$(date -u +%s)

  # JOB_NAME_SUFFIX gives the pass@k run its own job dir and S3 prefix so it
  # never archives or overwrites the k=1 baselines.
  # STAN_BRANCH is the pinned SHA (not a branch name) so every cell installs the
  # identical agent build; VERSION_TAG keeps job dirs tagged with that SHA.
  JOB_NAME_SUFFIX="--k${N_ATTEMPTS}" \
  N_ATTEMPTS="$N_ATTEMPTS" \
  ${PINNED_STAN_SHA:+STAN_BRANCH="$PINNED_STAN_SHA"} \
  ${PINNED_STAN_SHA:+VERSION_TAG="${PINNED_STAN_SHA:0:7}"} \
    bash "${EVALS_DIR}/strands-infra-runner/run-benchmark.sh" \
      "$AGENT" "$MODEL" "$DATASET" "$CELL_CONCURRENCY" \
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

  # Terminate anything this cell's own cleanup missed, so a crashed cell can't
  # leak instances into the next one. Scoped to this cell's harbor:job tag —
  # never a blanket sweep, which would kill instances from another run.
  ORPHANS=$(aws ec2 describe-instances --region us-east-1 \
    --filters Name=instance-state-name,Values=running,pending \
              Name=key-name,Values=harbor-benchmark \
              "Name=tag:harbor:job,Values=${AGENT}@*--${MODEL}--${DATASET_SLUG}--k${N_ATTEMPTS}" \
    --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null || true)
  if [ -n "${ORPHANS// /}" ]; then
    log "     terminating orphans for this cell: ${ORPHANS}"
    aws ec2 terminate-instances --region us-east-1 --instance-ids $ORPHANS >/dev/null 2>&1 || true
  fi
done

log "=== Matrix complete: ${MATRIX_ID} ==="
log "  OK:      ${#SUCCEEDED[@]} (${SUCCEEDED[*]:-none})"
log "  FAILED:  ${#FAILED[@]} (${FAILED[*]:-none})"
log "  SKIPPED: ${#SKIPPED[@]} (${SKIPPED[*]:-none})"

# Final sync so results are durable even if the 5-minute mirror cron is behind.
aws s3 sync /home/ubuntu/evals/jobs/ s3://strands-benchmark-results-mirror/jobs/ \
  --region us-east-1 --only-show-errors 2>&1 | tee -a "$MATRIX_LOG"

# One line per cell so you can see at a glance that results landed. Per-source
# and equal-weighted metrics are computed by the job's metric plugin and live in
# each job's result.json / the viewer.
for MODEL in "${MODEL_LIST[@]}"; do
  MODEL="$(echo "$MODEL" | tr -d '[:space:]')"
  [ -z "$MODEL" ] && continue
  python3 - "$MODEL" "$DATASET_SLUG" "$N_ATTEMPTS" <<'PY' 2>&1 | tee -a "$MATRIX_LOG"
import glob
import json
import sys

model, dataset_slug, k = sys.argv[1:]
paths = glob.glob(f"/home/ubuntu/evals/jobs/*--{model}--{dataset_slug}--k{k}/result.json")
if not paths:
    print(f"  {model}: no result.json")
    sys.exit()
with open(paths[0]) as fh:
    stats = (json.load(fh).get("stats") or {})
for key, ev in (stats.get("evals") or {}).items():
    merged = {}
    for m in ev.get("metrics") or []:
        merged.update(m)
    pak = {kk: round(vv, 4) for kk, vv in sorted((ev.get("pass_at_k") or {}).items())}
    print(
        f"  {model}: n={ev.get('n_trials')} errors={ev.get('n_errors')} "
        f"mean={merged.get('mean')} equal_weighted={merged.get('equal_weighted_mean')} "
        f"pass@k={pak or '(none)'}"
    )
PY
done

date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/COMPLETE"
log "Wrote ${STATE_DIR}/COMPLETE"

[ "${#FAILED[@]}" -eq 0 ] || exit 1
