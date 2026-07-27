#!/bin/bash
# run-benchmark.sh — Single entrypoint for the EC2 orchestrator.
#
# Usage:
#   run-benchmark <agent> <model> <dataset> [concurrency]
#
# Examples:
#   run-benchmark stan sonnet-4.6 terminal-bench/terminal-bench-2-1
#   run-benchmark stan kimi-k2.5 swe-bench/swe-bench-verified 500
#   run-benchmark stan opus-4.8 gaia/gaia
#
# Results upload to: s3://strands-benchmark-results/<agent>/<model>/<dataset-slug>/
# Local results at:  jobs/<agent>@<commit-sha>--<model>--<dataset-slug>/

set -euo pipefail

AGENT="${1:?Usage: run-benchmark <agent> <model> <dataset> [concurrency]}"
MODEL="${2:?Usage: run-benchmark <agent> <model> <dataset> [concurrency]}"
DATASET="${3:?Usage: run-benchmark <agent> <model> <dataset> [concurrency]}"

EVALS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STRANDS_HARNESS_DATASET="strands-harness-benchmark-index"
HARBOR_STRANDS_CHECKOUT="${HARBOR_STRANDS_CHECKOUT:-/home/ubuntu/harbor-strands-working}"

# Auto-resolve concurrency from datasets.json (task count, capped at 2000)
if [ -n "${4:-}" ]; then
  CONCURRENCY="$4"
else
  CONCURRENCY=$(python3 -c "
import json
datasets = json.load(open('${EVALS_DIR}/strands-infra-runner/datasets.json'))
tasks = datasets.get('${DATASET}', 500)
print(min(tasks, 2000))
" 2>/dev/null || echo 500)
fi

# --- Resolve agent path ---
AGENTS_DIR="${EVALS_DIR}/strands-infra-runner/agents"
AGENT_MODULE="agent:MyAgent"

if [ -d "${AGENTS_DIR}/${AGENT}" ]; then
  AGENT_PATH="${AGENTS_DIR}/${AGENT}"
else
  echo "Agent not found: ${AGENTS_DIR}/${AGENT}" >&2
  echo "Available: $(ls "${AGENTS_DIR}" 2>/dev/null | tr '\n' ' ')" >&2
  exit 1
fi

# --- Resolve model ID ---
case "$MODEL" in
  sonnet-4.6|sonnet)
    MODEL_ID="us.anthropic.claude-sonnet-4-6"
    ;;
  opus-4.6)
    MODEL_ID="global.anthropic.claude-opus-4-6-v1"
    ;;
  opus-4.8|opus)
    MODEL_ID="global.anthropic.claude-opus-4-8"
    ;;
  sonnet-5|sonnet5)
    MODEL_ID="global.anthropic.claude-sonnet-5"
    ;;
  kimi-k2.5|kimi-2.5|kimi)
    MODEL_ID="moonshotai.kimi-k2.5"
    ;;
  *)
    # Allow passing raw model IDs
    MODEL_ID="$MODEL"
    ;;
esac

# --- Instance type (always xlarge to handle any task's resource requirements) ---
INSTANCE_TYPE="${INSTANCE_TYPE:-m7i.xlarge}"

# --- Build job name and paths ---
DATASET_SLUG="${DATASET//\//-}"
# JOB_NAME_SUFFIX distinguishes otherwise-identical runs (e.g. "--k4" for pass@k),
# so a pass@4 run never archives or overwrites the pass@1 results.
JOB_SUFFIX="${JOB_NAME_SUFFIX:-}"
JOB_NAME="${AGENT}${VERSION_TAG:+@${VERSION_TAG}}--${MODEL}--${DATASET_SLUG}${JOB_SUFFIX}"
OUTPUT_DIR="jobs"
S3_PREFIX="${AGENT}/${MODEL}/${DATASET_SLUG}${JOB_SUFFIX}"
LOG_FILE="/home/ubuntu/benchmark-${JOB_NAME}.log"

echo "=== Benchmark Run ==="
echo "  Agent:       $AGENT ($AGENT_PATH)"
echo "  Model:       $MODEL_ID"
echo "  Dataset:     $DATASET"
echo "  Concurrency: $CONCURRENCY"
echo "  Instance:    $INSTANCE_TYPE"
echo "  Output:      $OUTPUT_DIR"
echo "  S3:          s3://strands-benchmark-results/$S3_PREFIX/"
echo "  Log:         $LOG_FILE"
echo ""

# --- Setup ---
export HOME=/root
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/ubuntu/evals
source .venv/bin/activate

if [[ "$DATASET" == "$STRANDS_HARNESS_DATASET" ]]; then
  export DATASET_PATH="${HARBOR_STRANDS_CHECKOUT}/datasets/${STRANDS_HARNESS_DATASET}"
  METRIC_PLUGIN_DIR="${HARBOR_STRANDS_CHECKOUT}/adapters/${STRANDS_HARNESS_DATASET}"

  if [ ! -d "$DATASET_PATH" ]; then
    echo "ERROR: Custom dataset is not materialized at ${DATASET_PATH}" >&2
    echo "  Run: bash strands-infra-runner/setup-strands-harness-benchmark.sh" >&2
    exit 1
  fi
  if [ ! -f "${METRIC_PLUGIN_DIR}/metric_plugin.py" ]; then
    echo "ERROR: Custom metric plugin is missing from ${METRIC_PLUGIN_DIR}" >&2
    exit 1
  fi

  MATERIALIZED_TASKS=$(find "$DATASET_PATH" -mindepth 2 -maxdepth 2 -name task.toml | wc -l)
  if [ "$MATERIALIZED_TASKS" -ne 206 ]; then
    echo "ERROR: Expected 206 custom benchmark tasks, found ${MATERIALIZED_TASKS}" >&2
    echo "  Re-run: bash strands-infra-runner/setup-strands-harness-benchmark.sh" >&2
    exit 1
  fi

  export PYTHONPATH="${METRIC_PLUGIN_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
  export JOB_PLUGIN="metric_plugin:StrandsHarnessBenchmarkMetricPlugin"
  echo "  Local dataset: ${DATASET_PATH} (${MATERIALIZED_TASKS} tasks)"
  echo "  Metric plugin: ${JOB_PLUGIN}"
fi

configure_tau3_mantle() {
  local secret_id="${BEDROCK_API_KEY_SECRET_ID:-bedrock_api_key}"
  local mantle_region="${TAU3_MANTLE_REGION:-us-east-1}"
  local api_key

  echo "Configuring TAU3 simulated user through Bedrock Mantle..."
  if ! api_key=$(python3 - "$secret_id" "$mantle_region" <<'PY'
import json
import sys

import boto3

secret_id, region = sys.argv[1:]
response = boto3.client("secretsmanager", region_name=region).get_secret_value(
    SecretId=secret_id
)
secret_string = response.get("SecretString")
if not secret_string:
    raise SystemExit(f"Secret {secret_id!r} does not contain a SecretString")

try:
    decoded = json.loads(secret_string)
except json.JSONDecodeError:
    api_key = secret_string
else:
    if isinstance(decoded, str):
        api_key = decoded
    elif isinstance(decoded, dict):
        api_key = next(
            (
                decoded[key]
                for key in ("bedrock_api_key", "OPENAI_API_KEY", "api_key")
                if isinstance(decoded.get(key), str) and decoded[key]
            ),
            None,
        )
        if api_key is None:
            raise SystemExit(
                f"Secret {secret_id!r} JSON must contain bedrock_api_key, "
                "OPENAI_API_KEY, or api_key"
            )
    else:
        raise SystemExit(f"Secret {secret_id!r} must be a string or JSON object")

api_key = api_key.strip()
if not api_key:
    raise SystemExit(f"Secret {secret_id!r} contains an empty API key")
sys.stdout.write(api_key)
PY
  ); then
    echo "ERROR: Failed to load TAU3 Bedrock API key from Secrets Manager secret '${secret_id}'." >&2
    exit 1
  fi

  export OPENAI_API_KEY="$api_key"
  export OPENAI_BASE_URL="https://bedrock-mantle.${mantle_region}.api.aws/v1"
  export TAU2_USER_MODEL="${TAU3_USER_MODEL:-openai/openai.gpt-oss-120b}"
  export TAU2_NL_ASSERTIONS_MODEL="${TAU3_NL_ASSERTIONS_MODEL:-$TAU2_USER_MODEL}"

  echo "  Mantle endpoint: ${OPENAI_BASE_URL}"
  echo "  User model:      ${TAU2_USER_MODEL}"
  echo "  Assertion model: ${TAU2_NL_ASSERTIONS_MODEL}"
}

if [[ "$DATASET" == sierra-research/tau3-bench* || "$DATASET" == "$STRANDS_HARNESS_DATASET" ]]; then
  configure_tau3_mantle
fi

# Stan agents: install strands_stan from private repo and bundle it for container upload
if [[ "$AGENT" == stan* ]]; then
  STAN_PAT=$(python3 -c "
import json, boto3
client = boto3.client('secretsmanager', region_name='us-east-1')
secret = client.get_secret_value(SecretId='arn:aws:secretsmanager:us-east-1:879381280403:secret:stan_pat-lUflBx')
print(json.loads(secret['SecretString'])['stan_pat'])
")
  pip install -q --force-reinstall --no-deps "git+https://x-access-token:${STAN_PAT}@github.com/awsarron/stan.git@${STAN_BRANCH:-main}#subdirectory=stan-py"

  # Auto-derive VERSION_TAG by querying the actual remote HEAD (not pip metadata,
  # which can be stale from cached installs).
  if [ -z "${VERSION_TAG:-}" ]; then
    STAN_REF="${STAN_BRANCH:-main}"
    VERSION_TAG=$(git ls-remote "https://x-access-token:${STAN_PAT}@github.com/awsarron/stan.git" "${STAN_REF}" | cut -c1-7)
    if [ -z "$VERSION_TAG" ]; then
      echo "ERROR: Could not resolve Stan commit for ref '${STAN_REF}'" >&2
      echo "  Check network access to github.com/awsarron/stan.git" >&2
      exit 1
    fi
    echo "  Stan commit: ${VERSION_TAG} (from git ls-remote ${STAN_REF})"
    # Rebuild job name with the derived tag
    JOB_NAME="${AGENT}@${VERSION_TAG}--${MODEL}--${DATASET_SLUG}${JOB_SUFFIX}"
  fi

  # Bundle strands_stan into the agent dir so it gets uploaded to fleet containers
  STAN_SRC=$(python3 -c "import strands_stan, pathlib; print(pathlib.Path(strands_stan.__file__).parent)")
  rm -rf "${AGENT_PATH}/strands_stan"
  cp -r "${STAN_SRC}" "${AGENT_PATH}/strands_stan"

  if [[ "$MODEL_ID" == openai.* ]]; then
    export AGENT_DEPS="strands-agents[openai]>=1.45.0"
  else
    export AGENT_DEPS="strands-agents>=1.45.0"
  fi
fi

# Ensure the Harbor fork is installed (pip install -e .[harbor] can overwrite it
# with stock PyPI harbor since pyproject.toml lists harbor>=0.17.1 as a dep).
# Use a lockfile to avoid races when multiple benchmarks launch concurrently.
(
  flock -x 200
  pip install --force-reinstall --no-deps -q git+https://github.com/notowen333/harbor.git@strands-working-fork
) 200>/tmp/harbor-install.lock

# Archive previous run if it exists (NEVER delete results)
if [ -d "${OUTPUT_DIR}/${JOB_NAME}" ]; then
  mkdir -p "${OUTPUT_DIR}/archived"
  ARCHIVE_NAME="${JOB_NAME}--$(date -u +%Y%m%dT%H%M%S)"
  mv "${OUTPUT_DIR}/${JOB_NAME}" "${OUTPUT_DIR}/archived/${ARCHIVE_NAME}"
  echo "Archived previous run to ${OUTPUT_DIR}/archived/${ARCHIVE_NAME}"
fi

# --- Run ---
export AWS_REGION=us-east-1
export DATASET="$DATASET"
export JOB_NAME="$JOB_NAME"
export OUTPUT_DIR="$OUTPUT_DIR"
export CONCURRENCY="$CONCURRENCY"
export INSTANCE_TYPE="$INSTANCE_TYPE"
export ROOT_VOLUME_GB=64
export IAM_INSTANCE_PROFILE=StrandsBenchmarkHarborNodeRole
export BENCHMARK_S3_BUCKET=strands-benchmark-results
export SSH_KEY_PATH=/root/.ssh/harbor-benchmark.pem
export AGENT_PATH="$AGENT_PATH"
export AGENT_MODULE="$AGENT_MODULE"
export AGENT_NAME="$AGENT"
export STRANDS_MODEL="$MODEL_ID"

# Never let a failed run abort the script — the S3 upload below must always run so
# partial results are preserved. `set -e`/`pipefail` would otherwise kill us here,
# and `$?` after a pipe reports tee's status, not the runner's.
set +e
python strands-infra-runner/run.py 2>&1 | tee "$LOG_FILE"
RUN_EXIT=${PIPESTATUS[0]}
set -e

# Patch config.agent.name from the import path to the friendly agent name.
# Harbor writes the import path as the name; the viewer uses this field for display.
if [ -d "${OUTPUT_DIR}/${JOB_NAME}" ]; then
  find "${OUTPUT_DIR}/${JOB_NAME}" -name config.json \
    -exec sed -i "s|\"name\": \"strands_evals.benchmarks.harbor.installed.py:StrandsInstalledPyAgent\"|\"name\": \"${AGENT}\"|g" {} +
fi

# --- Upload results to S3 (always runs, even on partial failure) ---
echo ""
echo "=== Uploading results to S3 ==="
JOB_DIR="${OUTPUT_DIR}/${JOB_NAME}"
if [ -d "$JOB_DIR" ]; then
  python3 - <<PYEOF
import boto3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

s3 = boto3.client("s3", region_name="us-east-1")
base = Path("$JOB_DIR")
files = [p for p in base.rglob("*") if p.is_file()]
print(f"Uploading {len(files)} files to s3://strands-benchmark-results/$S3_PREFIX/")

def upload(p):
    s3.upload_file(str(p), "strands-benchmark-results", f"$S3_PREFIX/{p.relative_to(base)}")

with ThreadPoolExecutor(max_workers=32) as ex:
    list(ex.map(upload, files))

print("Upload complete.")
PYEOF
else
  echo "WARNING: Job directory $JOB_DIR not found, skipping S3 upload." >&2
fi

exit $RUN_EXIT
