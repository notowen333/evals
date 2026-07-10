"""Run a Harbor benchmark across a fleet of ephemeral EC2 instances.

Calls harbor directly in-process with a patched asyncio thread pool (512
workers instead of the default 14) so hundreds of EC2 instances can boot
and progress simultaneously.

Prerequisites:
    - AWS credentials in environment
    - SSH key pair created: aws ec2 create-key-pair --key-name harbor-benchmark
    - Security group allowing SSH inbound

Usage:
    .harbor-venv/bin/python examples/ec2-fleet/run.py

Customize by editing the sys.argv list below, or override via env vars.
"""

import asyncio
import atexit
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

# --- Orphan cleanup ---
# If the orchestrator dies, terminate all fleet instances so we don't leak EC2s.
KEY_NAME = os.environ.get("KEY_NAME", "harbor-benchmark")


def _terminate_fleet():
    """Terminate all running/pending instances with our key pair."""
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "instance-state-name", "Values": ["running", "pending"]},
                {"Name": "key-name", "Values": [KEY_NAME]},
            ]
        )
        ids = [i["InstanceId"] for r in resp["Reservations"] for i in r["Instances"]]
        if ids:
            ec2.terminate_instances(InstanceIds=ids)
            print(f"\nCleanup: terminated {len(ids)} orphaned fleet instances.", file=sys.stderr)
    except Exception as e:
        print(f"\nCleanup failed: {e}", file=sys.stderr)


def _signal_handler(signum, frame):
    print(f"\nCaught signal {signum}, cleaning up fleet...", file=sys.stderr)
    _terminate_fleet()
    sys.exit(1)


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
atexit.register(_terminate_fleet)

# --- Thread pool patch ---
# Harbor's EC2 environment uses asyncio.to_thread() for blocking boto3 waiter
# calls. The default thread pool (14 workers on Mac, 32 max) limits how many
# instances can boot simultaneously. This patch lifts that to 512.
_original_run = asyncio.run


def _patched_run(coro, **kwargs):
    loop = asyncio.new_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=512))
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


asyncio.run = _patched_run

# Config — edit these or set via environment
AGENT_PATH = os.environ.get("AGENT_PATH", "./examples/benchmark_agent")
AGENT_MODULE = os.environ.get("AGENT_MODULE", "agent:MyAgent")
DATASET = os.environ.get("DATASET", "swe-bench/swe-bench-verified")
CONCURRENCY = os.environ.get("CONCURRENCY", "500")
INSTANCE_TYPE = os.environ.get("INSTANCE_TYPE", "t3.medium")
KEY_NAME = os.environ.get("KEY_NAME", "harbor-benchmark")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", os.path.expanduser("~/.ssh/harbor-benchmark.pem"))
SECURITY_GROUP = os.environ.get("SECURITY_GROUP", "sg-0657fe39a6bbbc8f6")
SUBNET = os.environ.get("SUBNET", "subnet-0ca6e05ed9d6e1871")
REGION = os.environ.get("AWS_REGION", "us-east-1")
JOB_NAME = os.environ.get("JOB_NAME", "ec2-fleet")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "jobs/ec2-fleet")

os.environ.setdefault("BENCHMARK_S3_BUCKET", "strands-benchmark-results")

sys.argv = [
    "harbor", "run",
    "-a", "strands_evals.benchmarks.harbor.installed.py:StrandsInstalledPyAgent",
    "--ak", f"agent_path={AGENT_PATH}",
    "--ak", f"agent_module={AGENT_MODULE}",
    "-d", DATASET,
    "-n", CONCURRENCY,
    "-e", "ec2",
    "--ek", f"region={REGION}",
    "--ek", "ami_id=ami-0a02a779008fa3b99",
    "--ek", f"instance_type={INSTANCE_TYPE}",
    "--ek", f"key_name={KEY_NAME}",
    "--ek", f"ssh_key_path={SSH_KEY_PATH}",
    "--ek", f'security_group_ids=["{SECURITY_GROUP}"]',
    "--ek", f"subnet_id={SUBNET}",
    "--ek", "ssh_user=ubuntu",
    "--ek", "bootstrap_docker=true",
    "--ae", f"AWS_ACCESS_KEY_ID={os.environ.get('AWS_ACCESS_KEY_ID', '')}",
    "--ae", f"AWS_SECRET_ACCESS_KEY={os.environ.get('AWS_SECRET_ACCESS_KEY', '')}",
    "--ae", f"AWS_SESSION_TOKEN={os.environ.get('AWS_SESSION_TOKEN', '')}",
    "--ae", f"AWS_REGION={REGION}",
    "-o", OUTPUT_DIR,
    "--job-name", JOB_NAME,
    "--max-retries", "2",
    "--retry-include", "EnvironmentStartTimeoutError",
    "--debug",
]

from harbor.cli.main import app
app()
