# Harbor benchmark worker for ECS (EC2 launch type, privileged mode).
#
# Runs the Docker daemon + strands-evals benchmark inside a single container.
# The agent's task containers are built/run via Docker-in-Docker.
#
# Build (from repo root):
#   docker build -t harbor-worker -f examples/ecs-worker/Dockerfile .
#
# ECS task definition must set:
#   - privileged: true (required for DinD)
#   - memory/cpu sized for concurrent trials (4GB+ / 2+ vCPU recommended)
#   - IAM task role with Bedrock invoke permissions

FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY . /app
WORKDIR /app
RUN pip install --no-cache-dir -e ".[harbor]"

# ---

FROM ubuntu:24.04

# Install Docker CLI + daemon, Python, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git python3 python3-pip iptables \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Copy Python environment from builder
COPY --from=builder /usr/local/lib/python3.12 /usr/lib/python3/dist-packages/
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

WORKDIR /app

# Copy agent examples
COPY examples/benchmark_agent/ /app/agents/python/
COPY examples/benchmark_agent_ts/ /app/agents/typescript/

# Entrypoint: start dockerd in the background, then run the benchmark
COPY examples/ecs-worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
