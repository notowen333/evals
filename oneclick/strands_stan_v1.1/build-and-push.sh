#!/usr/bin/env bash
# Build, publish, and register the Stan factory agent for OneClick.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ID="strands-agent"

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 [publish] [stage] <version>"
  echo "  $0 v0.1.0"
  echo "  $0 publish gamma v0.1.0"
  exit 1
fi

if [[ "$1" == "publish" ]]; then
  PUBLISH=true
  shift
else
  PUBLISH=false
fi

if [[ "${1:-}" == "beta" || "${1:-}" == "gamma" || "${1:-}" == "prod" ]]; then
  STAGE="$1"
  VERSION="${2:-}"
else
  STAGE="prod"
  VERSION="${1:-}"
fi

if [[ -z "$VERSION" ]]; then
  echo "Version is required" >&2
  exit 1
fi

case "$STAGE" in
  beta) ACCOUNT_ID="385109576595" ;;
  gamma) ACCOUNT_ID="176646220621" ;;
  prod) ACCOUNT_ID="756811050490" ;;
  *)
    echo "Invalid stage: $STAGE" >&2
    exit 1
    ;;
esac

REGION="us-east-1"
AWS_PROFILE_NAME="ceat-${STAGE}-publisher"
ECR_REPO_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/agents-${STAGE}"
ECR_TAG="${AGENT_ID}-${VERSION}"
LOCAL_IMAGE="${AGENT_ID}:${VERSION}"
REMOTE_IMAGE="${ECR_REPO_URI}:${ECR_TAG}"

"${SCRIPT_DIR}/sync-source.sh"

echo "Building ${LOCAL_IMAGE}..."
docker build --platform linux/amd64 -t "$LOCAL_IMAGE" "$SCRIPT_DIR"

if [[ "$PUBLISH" != true ]]; then
  echo "Image built locally: ${LOCAL_IMAGE}"
  echo "To publish: $0 publish ${STAGE} ${VERSION}"
  exit 0
fi

echo "Fetching AWS credentials for ${STAGE}..."
ada credentials update \
  --provider isengard \
  --role EvaluationUser \
  --once \
  --profile "$AWS_PROFILE_NAME" \
  --account "$ACCOUNT_ID"

echo "Publishing ${REMOTE_IMAGE}..."
docker tag "$LOCAL_IMAGE" "$REMOTE_IMAGE"
aws --profile "$AWS_PROFILE_NAME" ecr get-login-password --region "$REGION" |
  docker login \
    --username AWS \
    --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
docker push "$REMOTE_IMAGE"

AGENTS_TABLE="Agents-AOS-${STAGE}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Registering ${AGENT_ID}/${VERSION} in ${AGENTS_TABLE}..."
aws --profile "$AWS_PROFILE_NAME" dynamodb put-item --region "$REGION" --table-name "$AGENTS_TABLE" --item "{
  \"agent_id\": {\"S\": \"${AGENT_ID}\"},
  \"version\": {\"S\": \"${VERSION}\"},
  \"image_uri\": {\"S\": \"${REMOTE_IMAGE}\"},
  \"interface_type\": {\"S\": \"cli\"},
  \"interface_config\": {\"M\": {\"type\": {\"S\": \"cli\"}, \"entry_point\": {\"S\": \"python3 -m strand_agent.run_infer\"}, \"parameters\": {\"M\": {}}}},
  \"name\": {\"S\": \"Strands Stan\"},
  \"description\": {\"S\": \"Stan factory agent using task-container-bound bash and file editor tools\"},
  \"resource_requirements\": {\"M\": {\"memory_gb\": {\"N\": \"16\"}, \"vcpus\": {\"N\": \"4\"}, \"timeout_seconds\": {\"N\": \"7200\"}, \"gpu\": {\"BOOL\": false}}},
  \"status\": {\"S\": \"active\"},
  \"supported_datasets\": {\"L\": []},
  \"team_name\": {\"S\": \"ceat\"},
  \"created_at\": {\"S\": \"${NOW}\"},
  \"updated_at\": {\"S\": \"${NOW}\"}
}"

aws --profile "$AWS_PROFILE_NAME" dynamodb get-item \
  --region "$REGION" \
  --table-name "$AGENTS_TABLE" \
  --key "{\"agent_id\": {\"S\": \"${AGENT_ID}\"}, \"version\": {\"S\": \"${VERSION}\"}}" \
  --query "Item.{agent_id:agent_id.S,version:version.S,image_uri:image_uri.S,status:status.S}" \
  --output json

echo "Published ${REMOTE_IMAGE}"
echo "Run with: agentId=${AGENT_ID}, agentVersionId=${VERSION}"
