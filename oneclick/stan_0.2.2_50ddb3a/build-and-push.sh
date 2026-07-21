#!/usr/bin/env bash
# Build, publish, and register the Stan factory agent for OneClick.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="$(basename "$SCRIPT_DIR")"
AGENT_ID="strands-agent"

usage() {
  echo "Usage: $0 [publish [beta|gamma|prod]]"
  echo "  $0"
  echo "  $0 publish gamma"
}

if [[ "$RELEASE_DIR" =~ ^stan_([0-9]+\.[0-9]+\.[0-9]+)_([0-9a-f]{7})$ ]]; then
  RELEASE_VERSION="${BASH_REMATCH[1]}"
  RELEASE_SHA="${BASH_REMATCH[2]}"
else
  echo "Release directory must match stan_<version>_<7-character-sha>: ${RELEASE_DIR}" >&2
  exit 1
fi

PINNED_STAN_COMMIT="$(tr -d '[:space:]' < "${SCRIPT_DIR}/STAN_COMMIT")"
if [[ ! "$PINNED_STAN_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "STAN_COMMIT must contain one full lowercase 40-character Git SHA" >&2
  exit 1
fi
if [[ "${PINNED_STAN_COMMIT:0:7}" != "$RELEASE_SHA" ]]; then
  echo "Directory SHA ${RELEASE_SHA} does not match STAN_COMMIT ${PINNED_STAN_COMMIT}" >&2
  exit 1
fi

EXPECTED_VERSION="v${RELEASE_VERSION}_${RELEASE_SHA}"
VERSION="$(tr -d '[:space:]' < "${SCRIPT_DIR}/RELEASE_ID")"
if [[ "$VERSION" != "$EXPECTED_VERSION" ]]; then
  echo "RELEASE_ID ${VERSION} does not match directory version ${EXPECTED_VERSION}" >&2
  exit 1
fi

PUBLISH=false
STAGE="prod"
case "$#" in
  0) ;;
  1)
    if [[ "$1" != "publish" ]]; then
      usage >&2
      exit 1
    fi
    PUBLISH=true
    ;;
  2)
    if [[ "$1" != "publish" ]]; then
      usage >&2
      exit 1
    fi
    PUBLISH=true
    STAGE="$2"
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac

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
# Docker reserves @ for digests, so image tags use a hyphen at this boundary.
IMAGE_VERSION="v${RELEASE_VERSION}-${RELEASE_SHA}"
ECR_TAG="${AGENT_ID}-${IMAGE_VERSION}"
LOCAL_IMAGE="${AGENT_ID}:${IMAGE_VERSION}"
REMOTE_IMAGE="${ECR_REPO_URI}:${ECR_TAG}"
DISPLAY_NAME="Strands Stan ${RELEASE_VERSION}_${RELEASE_SHA}"

"${SCRIPT_DIR}/sync-source.sh"

echo "Building ${LOCAL_IMAGE} for OneClick version ${VERSION}..."
docker build --platform linux/amd64 -t "$LOCAL_IMAGE" "$SCRIPT_DIR"

if [[ "$PUBLISH" != true ]]; then
  echo "Image built locally: ${LOCAL_IMAGE}"
  echo "OneClick version: ${VERSION}"
  echo "To publish: $0 publish ${STAGE}"
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
  \"name\": {\"S\": \"${DISPLAY_NAME}\"},
  \"description\": {\"S\": \"Stan ${RELEASE_SHA} factory agent using task-container-bound bash and file editor tools\"},
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
