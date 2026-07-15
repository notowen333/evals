#!/bin/bash
# run-benchmark.sh — Single entrypoint for the EC2 orchestrator.
#
# Usage:
#   run-benchmark <agent> <model> <dataset> [concurrency]
#
# Examples:
#   run-benchmark stan_0.1.1 sonnet-4.6 terminal-bench/terminal-bench-2-1
#   run-benchmark stan_0.1.1 sonnet-4.6 swe-bench/swe-bench-verified 500
#   run-benchmark stan_0.1.1 opus-4.8 gaia
#   run-benchmark trivial sonnet-4.6 terminal-bench/terminal-bench-2-1 89
#
# Results upload to: s3://strands-benchmark-results/<agent>/<model>/<dataset-slug>/
# Local results at:  jobs/<agent>--<model>--<dataset-slug>/

set -euo pipefail

AGENT="${1:?Usage: run-benchmark <agent> <model> <dataset> [concurrency]}"
MODEL="${2:?Usage: run-benchmark <agent> <model> <dataset> [concurrency]}"
DATASET="${3:?Usage: run-benchmark <agent> <model> <dataset> [concurrency]}"
CONCURRENCY="${4:-500}"

# --- Resolve agent path ---
# Convention: agents live at /home/ubuntu/agents/<name>/ with agent.py exporting MyAgent.
AGENTS_DIR="/home/ubuntu/agents"
AGENT_PATH="${AGENTS_DIR}/${AGENT}"
AGENT_MODULE="agent:MyAgent"

if [ ! -d "$AGENT_PATH" ]; then
  echo "Agent not found: $AGENT_PATH" >&2
  echo "Available agents: $(ls "$AGENTS_DIR" 2>/dev/null | tr '\n' ' ')" >&2
  exit 1
fi

# --- Resolve model ID ---
case "$MODEL" in
  sonnet-4.6|sonnet)
    MODEL_ID="us.anthropic.claude-sonnet-4-6"
    ;;
  opus-4.8|opus)
    MODEL_ID="us.anthropic.claude-opus-4-8"
    ;;
  sonnet-5|sonnet5)
    MODEL_ID="us.anthropic.claude-sonnet-5-v1"
    ;;
  *)
    # Allow passing raw model IDs
    MODEL_ID="$MODEL"
    ;;
esac

# --- Instance type (always xlarge to handle any task's resource requirements) ---
INSTANCE_TYPE="m7i.xlarge"

# --- Build job name and paths ---
DATASET_SLUG="${DATASET//\//-}"
JOB_NAME="${AGENT}--${MODEL}--${DATASET_SLUG}"
OUTPUT_DIR="jobs/${JOB_NAME}"
S3_PREFIX="${AGENT}/${MODEL}/${DATASET_SLUG}"
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

rm -rf "$OUTPUT_DIR"

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
export STRANDS_MODEL="$MODEL_ID"

python examples/ec2-fleet/run.py 2>&1 | tee "$LOG_FILE"
RUN_EXIT=$?

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
