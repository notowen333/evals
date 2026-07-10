#!/bin/bash
set -e

# Start Docker daemon in background
dockerd --host=unix:///var/run/docker.sock &
DOCKERD_PID=$!

# Wait for Docker to be ready
echo "Waiting for Docker daemon..."
for i in $(seq 1 30); do
    if docker info >/dev/null 2>&1; then
        echo "Docker ready."
        break
    fi
    sleep 1
done

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon failed to start" >&2
    exit 1
fi

# Run the benchmark command passed as arguments
# Example: docker run harbor-worker strands-evals benchmark --name my-run ...
exec "$@"
