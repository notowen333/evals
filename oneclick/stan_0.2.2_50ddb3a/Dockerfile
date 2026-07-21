# strands-agent: Stan's default factory packaged as a sibling-container image
# for AWSCEATAgentTaskExecutor's DockerAgentBridge.
#
# The agent runs in this image and operates on the separate Task Container: its
# bash / file-editor tools (strands DockerSandbox) run `docker exec` against the
# task container over the Docker socket the bridge mounts, so the agent's edits
# land in the task repo that the bridge's git-diff patch extraction reads.
#
# Build context is this directory. sync-source.sh copies the ignored Stan Python
# package here before the image is built:
#   docker build --platform linux/amd64 -t strands-agent:dev .
#   ./build-and-push.sh publish <stage>

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Docker CLI client, required by DockerSandbox to `docker exec` into the task
# container. Only the client is needed (no daemon) — it talks to the host
# daemon via the socket the bridge mounts. Installed from Docker's apt repo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

# Strands runtime. strands-agents-tools provides the vended bash and
# file_editor tools that DockerSandbox binds to the task container.
# harbor provides the ATIF trajectory models (harbor.models.trajectories,
# harbor.utils.trajectory_utils) that trajectory.py uses to emit trajectory.json;
# it is imported for those data models only, not run.
RUN python3 -m pip install --upgrade pip \
    && python3 -m pip install \
        "strands-agents>=1.47.0,<2.0.0" \
        "strands-agents-tools>=0.1.0,<1.0.0" \
        "boto3>=1.28.0" \
        "harbor>=0.17.1"

# Install the OneClick bridge and synchronized Stan source.
WORKDIR /app
COPY . /app/strand_agent
COPY strands_stan /app/strands_stan
ENV PYTHONPATH=/app

# No ENTRYPOINT: DockerAgentBridge supplies the full command at runtime.
