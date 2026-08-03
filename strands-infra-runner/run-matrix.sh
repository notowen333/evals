#!/bin/bash
# run-matrix.sh — Fire-and-forget pass@k matrix launcher.
#
# Launches the model matrix against a dataset and walks away.
#
# Each cell runs at full width — one EC2 node per trial (tasks x k) — bounded only
# by the fleet vCPU ceiling and Harbor's SSH/FD limit. Models still run
# SEQUENTIALLY, so peak fleet size is one cell's worth of instances no matter how
# many models or how large k is.
#
# Per-model token caps are available but empty by default: the k=2 matrix showed
# 1 ApiRateLimitError in 2060 trials, so endpoint throttling is not the binding
# constraint. See model_token_cap() before adding one back.
#
# Usage:
#   run-matrix.sh [-d dataset] [-k attempts] [-m models] [-n concurrency] [-a agents]
#                 [-s stan-ref]
#
# Examples:
#   run-matrix.sh                                  # 5-model matrix at pass@2
#   run-matrix.sh -k 4                             # same matrix at pass@4
#   run-matrix.sh -m opus-4.8,sonnet-4.6 -k 2      # just two models
#   run-matrix.sh -a claude-code,opencode           # full supported native matrix
#   run-matrix.sh -s 45fed43                       # pin an exact Stan commit
#   run-matrix.sh -s my-feature-branch             # or a branch/tag
#
# State/logs (tail these):
#   <state>/matrix.log    driver log — one line per cell start/finish
#   <state>/status.tsv    machine-readable cell status
#   <state>/<model>.log   full run log for that cell
#   <state>/COMPLETE      written when the whole matrix finishes
#   where <state> includes the dataset, agent versions, and k.
#
# Resumable: re-run with the same -d/-k and cells already marked OK are skipped.

set -uo pipefail

EVALS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORCHESTRATOR_EVALS_DIR="${ORCHESTRATOR_EVALS_DIR:-$EVALS_DIR}"
export ORCHESTRATOR_EVALS_DIR
STATE_ROOT="${MATRIX_STATE_ROOT:-/home/ubuntu/matrix-runs}"
HARBOR_STRANDS_CHECKOUT="${HARBOR_STRANDS_CHECKOUT:-/home/ubuntu/harbor-strands-working}"
HARBOR_REPO_URL="${HARBOR_REPO_URL:-https://github.com/notowen333/harbor.git}"
HARBOR_REF="${HARBOR_REF:-strands-working-fork}"

DATASET="strands-harness-benchmark-index"
N_ATTEMPTS=2
AGENTS="stan"
CONCURRENCY=""
INTER_CELL_COOLDOWN_SECONDS="${INTER_CELL_COOLDOWN_SECONDS:-60}"
FLEET_DRAIN_TIMEOUT_SECONDS="${FLEET_DRAIN_TIMEOUT_SECONDS:-1200}"
# Stan ref to pin: a branch, tag, or full/short commit SHA. Defaults to
# STAN_BRANCH, else main's current HEAD. Resolved once for the whole matrix.
STAN_REF_OVERRIDE="${STAN_REF_OVERRIDE:-}"

# Stan's five-model baseline and the full model set used for native products.
STAN_DEFAULT_MODELS="opus-4.8,openai.gpt-5.6-sol,sonnet-5,openai.zai.glm-5,kimi-k2.5"
NATIVE_DEFAULT_MODELS="sonnet-4.6,${STAN_DEFAULT_MODELS}"
MODELS=""

# --- Token-concurrency caps (nodes) --------------------------------------
# Max concurrent trials per model, i.e. max in-flight requests to that endpoint.
#
# These were originally cut to 80-100 for sonnet-5/glm-5/kimi on the theory that
# Bedrock throttling was the dominant error class (53/206 ApiRateLimitError in the
# k=1 baselines). The k=2 matrix (2026-07-27, 2060 trials) disproved that:
#
#   ApiRateLimitError:        1 trial in 2060
#   retries:                  0-3 per model
#   error classes observed:   NonZeroAgentExitCode (262), AgentTimeout (115)
#
# Both remaining classes are agent-side and do NOT scale with concurrency, so the
# caps bought nothing and cost ~2.1h of a 6.4h matrix. The 429s that do appear in
# the logs are the agent's own web tools hitting external sites (Brave, Semantic
# Scholar), not Bedrock — throttling those by shrinking the fleet is pointless.
#
# Default is now "no per-model cap": the real ceilings are MAX_FLEET_NODES and
# Harbor's SSH/FD limit. Add an entry here only with evidence of endpoint-side
# throttling for that model, and note the run it came from.
DEFAULT_MODEL_CONCURRENCY=100000

model_token_cap() {
  case "$1" in
    # No models currently need a token cap. Example of an evidence-backed entry:
    #   some-model)  echo 80 ;;  # <N>/<total> ApiRateLimitError in <run>
    *) echo "$DEFAULT_MODEL_CONCURRENCY" ;;
  esac
}

while getopts "d:k:m:n:a:s:" opt; do
  case "$opt" in
    d) DATASET="$OPTARG" ;;
    k) N_ATTEMPTS="$OPTARG" ;;
    m) MODELS="$OPTARG" ;;
    n) CONCURRENCY="$OPTARG" ;;
    a) AGENTS="$OPTARG" ;;
    s) STAN_REF_OVERRIDE="$OPTARG" ;;
    *) echo "Usage: run-matrix.sh [-d dataset] [-k attempts] [-m models] [-n concurrency] [-a agents] [-s stan-ref]" >&2; exit 2 ;;
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
if ! [[ "$INTER_CELL_COOLDOWN_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: INTER_CELL_COOLDOWN_SECONDS must be a non-negative integer" >&2
  exit 2
fi
if ! [[ "$FLEET_DRAIN_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: FLEET_DRAIN_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi

IFS=',' read -r -a RAW_AGENT_LIST <<<"$AGENTS"
AGENT_LIST=()
HAS_STAN=0
HAS_NATIVE=0
for agent in "${RAW_AGENT_LIST[@]}"; do
  agent="$(echo "$agent" | tr -d '[:space:]')"
  case "$agent" in
    claude)
      agent="claude-code"
      ;;
    claude-code|opencode)
      HAS_NATIVE=1
      ;;
    stan*)
      HAS_STAN=1
      ;;
    "")
      continue
      ;;
  esac
  AGENT_LIST+=("$agent")
done
if [ "${#AGENT_LIST[@]}" -eq 0 ]; then
  echo "ERROR: at least one agent is required" >&2
  exit 2
fi

# Native product runs cover the full benchmark model set by default. Unsupported
# pairs are reported and omitted (Claude Code only speaks the Claude protocol).
if [ -z "$MODELS" ]; then
  if [ "$HAS_NATIVE" -eq 0 ]; then
    MODELS="$STAN_DEFAULT_MODELS"
  else
    MODELS="$NATIVE_DEFAULT_MODELS"
  fi
fi
IFS=',' read -r -a RAW_MODEL_LIST <<<"$MODELS"
MODEL_LIST=()
for model in "${RAW_MODEL_LIST[@]}"; do
  model="$(echo "$model" | tr -d '[:space:]')"
  [ -n "$model" ] && MODEL_LIST+=("$model")
done
if [ "${#MODEL_LIST[@]}" -eq 0 ]; then
  echo "ERROR: at least one model is required" >&2
  exit 2
fi

resolve_model_id() {
  case "$1" in
    sonnet-4.6|sonnet) echo "us.anthropic.claude-sonnet-4-6" ;;
    opus-4.6) echo "global.anthropic.claude-opus-4-6-v1" ;;
    opus-4.8|opus) echo "global.anthropic.claude-opus-4-8" ;;
    sonnet-5|sonnet5) echo "global.anthropic.claude-sonnet-5" ;;
    kimi-k2.5|kimi-2.5|kimi) echo "moonshotai.kimi-k2.5" ;;
    *) echo "$1" ;;
  esac
}

agent_model_incompatibility() {
  local agent="$1" model="$2" model_id
  model_id="$(resolve_model_id "$model")"
  case "$agent" in
    claude-code)
      if [[ "$model_id" != *anthropic.claude* ]]; then
        echo "Claude Code requires the Anthropic Claude protocol"
      fi
      ;;
  esac
}

CELL_AGENTS=()
CELL_MODELS=()
INCOMPATIBLE_CELLS=()
for model in "${MODEL_LIST[@]}"; do
  for agent in "${AGENT_LIST[@]}"; do
    reason="$(agent_model_incompatibility "$agent" "$model")"
    if [ -n "$reason" ]; then
      INCOMPATIBLE_CELLS+=("${agent}/${model}: ${reason}")
    else
      CELL_AGENTS+=("$agent")
      CELL_MODELS+=("$model")
    fi
  done
done
if [ "${#CELL_AGENTS[@]}" -eq 0 ]; then
  echo "ERROR: no compatible agent/model cells were selected" >&2
  printf '  %s\n' "${INCOMPATIBLE_CELLS[@]}" >&2
  exit 2
fi

# --- EC2 ceiling ---------------------------------------------------------
# Static on purpose: the account's On-Demand Standard quota is 9216 vCPU in
# us-east-1, so this 2048 is a self-imposed budget, not a hard limit — a quota API
# lookup would only add a silent failure mode. Raise MAX_FLEET_VCPU to go wider:
# the 206-task index at k=2 is 412 trials = 1648 vCPU (fits); at k=4 it is 824
# trials = 3296 vCPU, which this ceiling clamps to 500 nodes.
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
  local ref="${STAN_REF_OVERRIDE:-${STAN_BRANCH:-main}}"

  # Already a SHA: use it as-is. `git ls-remote` matches refs only and returns
  # nothing for a raw commit, so resolving it would spuriously "fail".
  if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "$ref"
    return 0
  fi

  local pat
  pat=$(python3 -c "
import json, boto3
c = boto3.client('secretsmanager', region_name='us-east-1')
s = c.get_secret_value(SecretId='arn:aws:secretsmanager:us-east-1:879381280403:secret:stan_pat-lUflBx')
print(json.loads(s['SecretString'])['stan_pat'])
" 2>/dev/null) || return 1
  git ls-remote "https://x-access-token:${pat}@github.com/awsarron/stan.git" "$ref" \
    | awk '{print $1}' | head -1
}

pin_harbor_commit() {
  local ref="$HARBOR_REF"
  if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]]; then
    echo "$ref"
    return 0
  fi
  git ls-remote "$HARBOR_REPO_URL" "$ref" | awk '{print $1}' | head -1
}

STAN_REF="${STAN_REF_OVERRIDE:-${STAN_BRANCH:-main}}"
PINNED_STAN_SHA=""
if [ "$HAS_STAN" -eq 1 ]; then
  PINNED_STAN_SHA="$(pin_stan_commit)"
  if [ -z "$PINNED_STAN_SHA" ]; then
    echo "ERROR: Could not resolve Stan commit for ref '${STAN_REF}'." >&2
    echo "  Refusing to start: cells would each resolve their own SHA and the" >&2
    echo "  matrix would not be internally comparable." >&2
    exit 1
  fi
fi

PINNED_HARBOR_SHA=""
if [ "${MATRIX_DRY_RUN:-0}" != "1" ]; then
  PINNED_HARBOR_SHA="$(pin_harbor_commit)"
  if [ -z "$PINNED_HARBOR_SHA" ]; then
    echo "ERROR: Could not resolve Harbor commit for ref '${HARBOR_REF}'." >&2
    exit 1
  fi
fi
HARBOR_STATE_TAG="${PINNED_HARBOR_SHA:-$HARBOR_REF}"
HARBOR_STATE_TAG="${HARBOR_STATE_TAG//\//-}"
HARBOR_VERSION_TAG="${HARBOR_STATE_TAG:0:7}"
JOB_SUFFIX="--harbor${HARBOR_VERSION_TAG}--k${N_ATTEMPTS}"

# Concurrency for one cell. The unit is TRIALS, not tasks: at k=2 Harbor schedules
# 412 independent trials for 206 tasks (verified — 412 trial dirs on disk), so
# capping at TASK_COUNT would leave half the work queued behind the first wave.
#
# Bounded by, in order: total trials in the cell, this model's token cap (none by
# default, see above), the EC2 vCPU ceiling, and 500 for Harbor's SSH/FD limit.
cell_concurrency() {
  local model="$1"
  if [ -n "$CONCURRENCY" ]; then
    echo "$CONCURRENCY"
    return
  fi
  local cap
  cap="$(model_token_cap "$model")"
  python3 -c "print(max(1, min(${TASK_COUNT} * ${N_ATTEMPTS}, ${cap}, ${MAX_FLEET_NODES}, 500)))"
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
AGENT_STATE_PARTS=()
for agent in "${AGENT_LIST[@]}"; do
  case "$agent" in
    stan*)
      AGENT_STATE_PARTS+=("${agent}@${PINNED_STAN_SHA:0:7}")
      ;;
    claude-code)
      AGENT_STATE_PARTS+=("${agent}@${CLAUDE_CODE_VERSION:-2.1.220}")
      ;;
    opencode)
      AGENT_STATE_PARTS+=("${agent}@${OPENCODE_VERSION:-1.18.9}")
      ;;
    *)
      AGENT_STATE_PARTS+=("$agent")
      ;;
  esac
done
AGENT_STATE_ID="$(IFS=+; echo "${AGENT_STATE_PARTS[*]}")"
MODEL_STATE_ID="$(IFS=+; echo "${MODEL_LIST[*]}")"
MODEL_STATE_ID="${MODEL_STATE_ID//\//-}"
MATRIX_ID="${DATASET_SLUG}--agents-${AGENT_STATE_ID}--models-${MODEL_STATE_ID}--harbor-${HARBOR_VERSION_TAG}--k${N_ATTEMPTS}"
STATE_DIR="${STATE_ROOT}/${MATRIX_ID}"
MATRIX_LOG="${STATE_DIR}/matrix.log"
STATUS_FILE="${STATE_DIR}/status.tsv"

if [ "${MATRIX_DRY_RUN:-0}" = "1" ]; then
  echo "matrix_id=${MATRIX_ID}"
  echo "dataset=${DATASET}"
  echo "attempts=${N_ATTEMPTS}"
  echo "harbor_ref=${PINNED_HARBOR_SHA:-$HARBOR_REF}"
  for cell_index in "${!CELL_AGENTS[@]}"; do
    printf 'cell=%s\t%s\t%s\n' \
      "${CELL_AGENTS[$cell_index]}" "${CELL_MODELS[$cell_index]}" \
      "$(cell_concurrency "${CELL_MODELS[$cell_index]}")"
  done
  if [ "${#INCOMPATIBLE_CELLS[@]}" -gt 0 ]; then
    for skipped in "${INCOMPATIBLE_CELLS[@]}"; do
      printf 'unsupported=%s\n' "$skipped"
    done
  fi
  exit 0
fi

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

log "=== Matrix start: ${MATRIX_ID} ==="
log "  Dataset:  ${DATASET} (${TASK_COUNT} tasks)"
log "  Attempts: k=${N_ATTEMPTS}  →  $((TASK_COUNT * N_ATTEMPTS)) trials per cell"
log "  Agents:   ${AGENT_STATE_ID}"
log "  Harbor:   ${PINNED_HARBOR_SHA} (from '${HARBOR_REF}', pinned for all cells)"
log "  Models:   ${MODELS}"
log "  EC2 ceiling: ${MAX_FLEET_VCPU} vCPU (${MAX_FLEET_NODES} nodes), one cell at a time"
log "  Launch pacing: ${EC2_LAUNCH_RATE_PER_SEC:-1.5}/s, burst ${EC2_LAUNCH_BURST:-3}; ${INTER_CELL_COOLDOWN_SECONDS}s cooldown"
for cell_index in "${!CELL_AGENTS[@]}"; do
  log "    ${CELL_AGENTS[$cell_index]}/${CELL_MODELS[$cell_index]}: $(cell_concurrency "${CELL_MODELS[$cell_index]}") concurrent trials"
done
if [ "${#INCOMPATIBLE_CELLS[@]}" -gt 0 ]; then
  for skipped in "${INCOMPATIBLE_CELLS[@]}"; do
    log "    unsupported ${skipped}"
  done
fi
log "  State:    ${STATE_DIR}"

rm -f "${STATE_DIR}/COMPLETE" "${STATE_DIR}/INCOMPLETE"

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
  needed_gb=$(python3 -c "print(max(5, int(0.18 * ${TASK_COUNT} * ${N_ATTEMPTS} * ${#CELL_AGENTS[@]} / 1024) + 5))")
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

  # A pinned SHA that doesn't exist in the Stan remote fails at `pip install` in
  # every cell. `git ls-remote` can't validate a raw SHA, so ask the GitHub API.
  if [ -n "$PINNED_STAN_SHA" ]; then
    if ! STAN_SHA="$PINNED_STAN_SHA" python3 - <<'PY'
import json
import os
import urllib.error
import urllib.request

import boto3

sha = os.environ["STAN_SHA"]
secret = boto3.client("secretsmanager", region_name="us-east-1").get_secret_value(
    SecretId="arn:aws:secretsmanager:us-east-1:879381280403:secret:stan_pat-lUflBx"
)
pat = json.loads(secret["SecretString"])["stan_pat"]
req = urllib.request.Request(
    f"https://api.github.com/repos/awsarron/stan/commits/{sha}",
    headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
)
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        json.load(resp)
except urllib.error.HTTPError as exc:
    raise SystemExit(f"  github says {exc.code} for commit {sha}")
PY
    then
      echo "PREFLIGHT FAIL: Stan commit '${PINNED_STAN_SHA}' not found in awsarron/stan" >&2
      fail=1
    else
      log "  preflight: Stan commit ${PINNED_STAN_SHA:0:7} exists in remote OK"
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

agent_version_tag() {
  case "$1" in
    stan*) echo "${PINNED_STAN_SHA:0:7}" ;;
    claude-code) echo "${CLAUDE_CODE_VERSION:-2.1.220}" ;;
    opencode) echo "${OPENCODE_VERSION:-1.18.9}" ;;
    *) echo "" ;;
  esac
}

cell_job_name() {
  local agent="$1" model="$2" version
  version="$(agent_version_tag "$agent")"
  echo "${agent}${version:+@${version}}--${model}--${DATASET_SLUG}${JOB_SUFFIX}"
}

cell_fleet_instance_ids() {
  local job_name="$1"
  aws ec2 describe-instances --region us-east-1 \
    --filters Name=instance-state-name,Values=pending,running,stopping,shutting-down \
              "Name=tag:harbor:job,Values=${job_name}" \
    --query "Reservations[].Instances[].InstanceId" --output text 2>/dev/null || true
}

terminate_and_drain_cell_fleet() {
  local job_name="$1" ids waited=0
  ids="$(cell_fleet_instance_ids "$job_name")"
  if [ -n "${ids// /}" ]; then
    log "     terminating remaining instances for ${job_name}: ${ids}"
    # shellcheck disable=SC2086 # AWS CLI expects one argument per instance ID.
    aws ec2 terminate-instances --region us-east-1 --instance-ids $ids >/dev/null 2>&1 || true
  fi

  while true; do
    ids="$(cell_fleet_instance_ids "$job_name")"
    if [ -z "${ids// /}" ]; then
      log "     fleet drained for ${job_name}"
      return 0
    fi
    if [ "$waited" -ge "$FLEET_DRAIN_TIMEOUT_SECONDS" ]; then
      log "     WARNING: fleet did not drain within ${FLEET_DRAIN_TIMEOUT_SECONDS}s: ${ids}"
      return 1
    fi
    sleep 15
    waited=$((waited + 15))
  done
}

completed_job_path() {
  local agent="$1" model="$2" version pattern
  version="$(agent_version_tag "$agent")"
  pattern="${ORCHESTRATOR_EVALS_DIR}/jobs/${agent}${version:+@${version}}--${model}--${DATASET_SLUG}${JOB_SUFFIX}/result.json"
  python3 - "$pattern" "$DATASET" "$TASK_COUNT" "$N_ATTEMPTS" <<'PY'
import glob
import json
import sys
from collections import Counter
from pathlib import Path

pattern, dataset, expected_tasks, k = sys.argv[1:]
expected_tasks, k = int(expected_tasks), int(k)
for path in sorted(glob.glob(pattern), reverse=True):
    try:
        with open(path) as fh:
            trials = json.load(fh).get("trial_results") or []
    except (OSError, json.JSONDecodeError):
        continue
    counts = Counter(
        trial.get("task_name")
        for trial in trials
        if trial.get("source") == dataset and trial.get("task_name")
    )
    has_clean_trial = any(trial.get("exception_info") is None for trial in trials)
    if (
        len(counts) == expected_tasks
        and set(counts.values()) == {k}
        and has_clean_trial
    ):
        print(Path(path).parent)
        break
PY
}

for cell_index in "${!CELL_AGENTS[@]}"; do
    AGENT="${CELL_AGENTS[$cell_index]}"
    MODEL="${CELL_MODELS[$cell_index]}"
    CELL_KEY="${AGENT}/${MODEL}"

    EXISTING_JOB="$(completed_job_path "$AGENT" "$MODEL")"
    if [ -n "$EXISTING_JOB" ]; then
      log "SKIP ${CELL_KEY} — complete result already exists at ${EXISTING_JOB}"
      if [ "$(cell_status "$CELL_KEY")" != "OK" ]; then
        record "$CELL_KEY" "OK" "reused=${EXISTING_JOB}"
      fi
      SKIPPED+=("$CELL_KEY")
      continue
    fi

    CELL_CONCURRENCY="$(cell_concurrency "$MODEL")"
    CELL_LOG="${STATE_DIR}/${AGENT}--${MODEL}.log"
    log "RUN  ${CELL_KEY} at -n ${CELL_CONCURRENCY} → ${CELL_LOG}"
    record "$CELL_KEY" "RUNNING" "n=${CELL_CONCURRENCY}"

  # Wait for EC2 room under the ceiling. Measuring real usage (not just our own
  # nodes) means an unrelated run delays us instead of being ignored or killed.
  # Bounded so a permanently-busy account can't hang the matrix overnight.
    NEEDED_VCPU=$((CELL_CONCURRENCY * VCPU_PER_NODE))
    for wait_index in $(seq 1 240); do
      IN_USE=$(fleet_vcpu_in_use)
      [ $((IN_USE + NEEDED_VCPU)) -le "$MAX_FLEET_VCPU" ] && break
      if [ "$wait_index" -eq 240 ]; then
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
  #
  # Built as an array and passed to `env` rather than as `${VAR:+NAME=val}` command
  # prefixes: bash decides which words are assignments BEFORE expanding them, so an
  # expansion that yields "NAME=val" is run as a command (exit 127), not assigned.
    CELL_ENV=(
      "JOB_NAME_SUFFIX=${JOB_SUFFIX}"
      "N_ATTEMPTS=${N_ATTEMPTS}"
      "HARBOR_REF=${PINNED_HARBOR_SHA}"
    )
    if [[ "$AGENT" == stan* ]]; then
      CELL_ENV+=(
        "STAN_BRANCH=${PINNED_STAN_SHA}"
        "VERSION_TAG=${PINNED_STAN_SHA:0:7}"
      )
    fi

    env "${CELL_ENV[@]}" \
      bash "${EVALS_DIR}/strands-infra-runner/run-benchmark.sh" \
        "$AGENT" "$MODEL" "$DATASET" "$CELL_CONCURRENCY" \
        </dev/null >"$CELL_LOG" 2>&1
    CELL_EXIT=$?
    ELAPSED=$(( $(date -u +%s) - START_EPOCH ))

    if [ "$CELL_EXIT" -eq 0 ]; then
      COMPLETED_JOB="$(completed_job_path "$AGENT" "$MODEL")"
      if [ -n "$COMPLETED_JOB" ]; then
        log "OK   ${CELL_KEY} in ${ELAPSED}s"
        record "$CELL_KEY" "OK" "${ELAPSED}s"
        SUCCEEDED+=("$CELL_KEY")
      else
        CELL_EXIT=6
        log "FAIL ${CELL_KEY} produced no complete result with a clean trial after ${ELAPSED}s"
      fi
    fi
    if [ "$CELL_EXIT" -ne 0 ]; then
      log "FAIL ${CELL_KEY} exit=${CELL_EXIT} after ${ELAPSED}s — see ${CELL_LOG}"
      record "$CELL_KEY" "FAIL" "exit=${CELL_EXIT} ${ELAPSED}s"
      FAILED+=("$CELL_KEY")
    fi

  # Drain this exact cell before the next one can provision. The cooldown starts
  # only after the fleet is gone, keeping RunInstances bursts separated.
    terminate_and_drain_cell_fleet "$(cell_job_name "$AGENT" "$MODEL")" || true
    if [ "$INTER_CELL_COOLDOWN_SECONDS" -gt 0 ]; then
      log "     cooling down ${INTER_CELL_COOLDOWN_SECONDS}s before the next cell"
      sleep "$INTER_CELL_COOLDOWN_SECONDS"
    fi
done

log "=== Matrix complete: ${MATRIX_ID} ==="
log "  OK:      ${#SUCCEEDED[@]} (${SUCCEEDED[*]:-none})"
log "  FAILED:  ${#FAILED[@]} (${FAILED[*]:-none})"
log "  SKIPPED: ${#SKIPPED[@]} (${SKIPPED[*]:-none})"

# Final sync so results are durable even if the 5-minute mirror cron is behind.
aws s3 sync "${ORCHESTRATOR_EVALS_DIR}/jobs/" s3://strands-benchmark-results-mirror/jobs/ \
  --region us-east-1 --only-show-errors 2>&1 | tee -a "$MATRIX_LOG"

# One line per cell so you can see at a glance that results landed. Per-source
# and equal-weighted metrics are computed by the job's metric plugin and live in
# each job's result.json / the viewer.
for cell_index in "${!CELL_AGENTS[@]}"; do
    AGENT="${CELL_AGENTS[$cell_index]}"
    MODEL="${CELL_MODELS[$cell_index]}"
    python3 - "$AGENT" "$MODEL" "$DATASET_SLUG" "$N_ATTEMPTS" "$HARBOR_VERSION_TAG" "$ORCHESTRATOR_EVALS_DIR" <<'PY' 2>&1 | tee -a "$MATRIX_LOG"
import glob
import json
import sys

agent, model, dataset_slug, k, harbor_version, evals_dir = sys.argv[1:]
paths = glob.glob(
    f"{evals_dir}/jobs/{agent}@*--{model}--{dataset_slug}"
    f"--harbor{harbor_version}--k{k}/result.json"
)
if not paths:
    print(f"  {agent}/{model}: no result.json")
    sys.exit()
path = max(paths, key=lambda item: __import__("os").path.getmtime(item))
with open(path) as fh:
    stats = (json.load(fh).get("stats") or {})
for key, ev in (stats.get("evals") or {}).items():
    merged = {}
    for m in ev.get("metrics") or []:
        merged.update(m)
    pak = {kk: round(vv, 4) for kk, vv in sorted((ev.get("pass_at_k") or {}).items())}
    print(
        f"  {agent}/{model}: n={ev.get('n_trials')} errors={ev.get('n_errors')} "
        f"mean={merged.get('mean')} equal_weighted={merged.get('equal_weighted_mean')} "
        f"pass@k={pak or '(none)'}"
    )
PY
done

if [ "${#FAILED[@]}" -eq 0 ]; then
  date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/COMPLETE"
  log "Wrote ${STATE_DIR}/COMPLETE"
else
  date -u +%Y-%m-%dT%H:%M:%SZ >"${STATE_DIR}/INCOMPLETE"
  log "Wrote ${STATE_DIR}/INCOMPLETE"
  exit 1
fi
