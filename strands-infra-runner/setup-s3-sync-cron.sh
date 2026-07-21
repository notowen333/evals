#!/usr/bin/env bash
# Install a cron job that syncs Harbor results to S3 every 5 minutes.
# Run once on the orchestrator EC2.
set -euo pipefail

BUCKET="${BENCHMARK_S3_BUCKET:-strands-benchmark-results}"
JOBS_DIR="/home/ubuntu/evals/jobs"
CRON_FILE="/etc/cron.d/harbor-s3-sync"

cat > "$CRON_FILE" <<EOF
*/5 * * * * root aws s3 sync ${JOBS_DIR}/ s3://${BUCKET}/jobs/ --region us-east-1 --quiet
EOF

chmod 644 "$CRON_FILE"
echo "Installed: ${CRON_FILE}"
echo "Syncing ${JOBS_DIR}/ → s3://${BUCKET}/jobs/ every 5 minutes"
