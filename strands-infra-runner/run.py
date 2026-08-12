"""Run a Harbor benchmark across a fleet of ephemeral EC2 instances.

Calls harbor directly in-process with a patched asyncio thread pool (512
workers instead of the default 14) so hundreds of EC2 instances can boot
and progress simultaneously.

Prerequisites:
    - AWS credentials in environment
    - SSH key pair created: aws ec2 create-key-pair --key-name harbor-benchmark
    - Security group allowing SSH inbound

Usage:
    .venv/bin/python strands-infra-runner/run.py

Customize by editing the sys.argv list below, or override via env vars.
"""

import atexit
import json
import os
import signal
import sys

# --- File descriptor limit ---
# Each concurrent SSH session to a fleet node uses several FDs. At 500 nodes the
# default soft limit (1024 on Ubuntu/macOS) is exhausted, stalling SSH setup.
# Raise the soft limit toward the hard limit before anything opens sockets.
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    _target = min(100_000, _hard) if _hard != resource.RLIM_INFINITY else 100_000
    if _soft < _target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_target, _hard))
except Exception as _e:
    print(f"Could not raise FD limit: {_e}", file=sys.stderr)

# --- Orphan cleanup ---
# Scoped to THIS job's instances only (by tag), so concurrent runs don't kill each other.
KEY_NAME = os.environ.get("KEY_NAME", "harbor-benchmark")
JOB_TAG = os.environ.get("JOB_NAME", "ec2-fleet")
DRY_RUN = os.environ.get("HARBOR_DRY_RUN") == "1"


def _terminate_fleet():
    """Terminate running/pending instances tagged with this job's name."""
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "instance-state-name", "Values": ["running", "pending"]},
                {"Name": "key-name", "Values": [KEY_NAME]},
                {"Name": "tag:harbor:job", "Values": [JOB_TAG]},
            ]
        )
        ids = [i["InstanceId"] for r in resp["Reservations"] for i in r["Instances"]]
        if ids:
            ec2.terminate_instances(InstanceIds=ids)
            print(f"\nCleanup: terminated {len(ids)} instances for job '{JOB_TAG}'.", file=sys.stderr)
    except Exception as e:
        print(f"\nCleanup failed: {e}", file=sys.stderr)


def _signal_handler(signum, frame):
    print(f"\nCaught signal {signum}, cleaning up fleet...", file=sys.stderr)
    _terminate_fleet()
    sys.exit(1)


if not DRY_RUN:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    atexit.register(_terminate_fleet)

# Thread pool scaling is handled natively by the Harbor fork (strands-working-fork)
# in cli/utils.py — it sets max_workers based on n_concurrent_trials.
# No monkeypatch needed.

# Config — edit these or set via environment
AGENT_PATH = os.environ.get("AGENT_PATH", "./examples/benchmark_agent")
AGENT_MODULE = os.environ.get("AGENT_MODULE", "agent:MyAgent")
AGENT_DEPS = os.environ.get("AGENT_DEPS")  # optional: override installed deps (default: strands-agents-tools)
AGENT_NAME = os.environ.get("AGENT_NAME")  # display name for the viewer (e.g. stan)
HARBOR_AGENT = os.environ.get(
    "HARBOR_AGENT",
    "strands_evals.benchmarks.harbor.installed.py:StrandsInstalledPyAgent",
)
HARBOR_MODEL_NAME = os.environ.get("HARBOR_MODEL_NAME")
AGENT_VERSION = os.environ.get("AGENT_VERSION")
DATASET = os.environ.get("DATASET", "swe-bench/swe-bench-verified")
DATASET_PATH = os.environ.get("DATASET_PATH")
JOB_PLUGIN = os.environ.get("JOB_PLUGIN")
CONCURRENCY = os.environ.get("CONCURRENCY", "500")
INSTANCE_TYPE = os.environ.get("INSTANCE_TYPE", "m7i.xlarge")
ROOT_VOLUME_GB = os.environ.get("ROOT_VOLUME_GB", "64")
KEY_NAME = os.environ.get("KEY_NAME", "harbor-benchmark")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", os.path.expanduser("~/.ssh/harbor-benchmark.pem"))
SECURITY_GROUP = os.environ.get("SECURITY_GROUP", "sg-0657fe39a6bbbc8f6")
SUBNET = os.environ.get("SUBNET")  # None = EC2 picks AZ with capacity (all subnets have public IP)
REGION = os.environ.get("AWS_REGION", "us-east-1")
JOB_NAME = os.environ.get("JOB_NAME", "ec2-fleet")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "jobs/ec2-fleet")
N_TASKS = os.environ.get("N_TASKS")  # optional: cap number of tasks (for testing)
N_ATTEMPTS = os.environ.get("N_ATTEMPTS")  # optional: attempts per task (pass@k)
MAX_RETRIES = os.environ.get("MAX_RETRIES", "2")
EC2_LAUNCH_RATE_PER_SEC = os.environ.get("EC2_LAUNCH_RATE_PER_SEC", "1.5")
EC2_LAUNCH_BURST = os.environ.get("EC2_LAUNCH_BURST", "3")
TASK_NAMES = [
    name.strip()
    for name in os.environ.get("TASK_NAMES", "").split(",")
    if name.strip()
]
# Optional: attach an IAM instance profile (name or ARN) to each fleet instance.
# When set, the instance's role provides AWS access (auto-refreshing), so we skip
# forwarding the orchestrator's credentials as env vars. The role needs only
# bedrock:InvokeModel + bedrock:InvokeModelWithResponseStream on the model.
IAM_INSTANCE_PROFILE = os.environ.get("IAM_INSTANCE_PROFILE")

os.environ.setdefault("BENCHMARK_S3_BUCKET", "strands-benchmark-results")


def _resolve_aws_creds() -> dict[str, str]:
    """Resolve AWS creds from the boto3 session (SSO, profiles, env, instance role).

    These are injected into every fleet instance via --ae so the agent can reach
    Bedrock. Env vars alone are not enough when using SSO / assumed roles.
    """
    import boto3
    session = boto3.Session(region_name=REGION)
    creds = session.get_credentials()
    if creds is None:
        raise SystemExit("No AWS credentials found — run `aws sso login` or set creds.")
    frozen = creds.get_frozen_credentials()
    env = {"AWS_ACCESS_KEY_ID": frozen.access_key, "AWS_SECRET_ACCESS_KEY": frozen.secret_key}
    if frozen.token:
        env["AWS_SESSION_TOKEN"] = frozen.token
    env["AWS_REGION"] = REGION
    return env


# If an instance profile is attached, the node authenticates via its role — no
# need to forward the orchestrator's creds (avoids token expiry + creds in argv).
_aws_creds = {} if IAM_INSTANCE_PROFILE else _resolve_aws_creds()

_native_agents = {"claude-code", "opencode", "codex"}
_is_native_agent = HARBOR_AGENT in _native_agents
if _is_native_agent and not HARBOR_MODEL_NAME:
    raise SystemExit(
        f"HARBOR_MODEL_NAME is required for native agent {HARBOR_AGENT!r}."
    )

_agent_args = ["-a", HARBOR_AGENT]
if _is_native_agent:
    _agent_args.extend(["-m", HARBOR_MODEL_NAME])
    if AGENT_VERSION:
        _agent_args.extend(["--ak", f"version={AGENT_VERSION}"])
    if HARBOR_AGENT == "opencode":
        # Keep title generation and other small-model work on the benchmark model.
        opencode_config: dict[str, object] = {"small_model": HARBOR_MODEL_NAME}
        if opencode_base_url := os.environ.get("OPENCODE_OPENAI_BASE_URL"):
            provider = HARBOR_MODEL_NAME.split("/", 1)[0]
            opencode_config["provider"] = {
                provider: {"options": {"baseURL": opencode_base_url}}
            }
        opencode_config_json = json.dumps(opencode_config, separators=(",", ":"))
        _agent_args.extend(["--ak", f"opencode_config={opencode_config_json}"])
else:
    if AGENT_NAME:
        _agent_args.extend(["--ak", f"display_name={AGENT_NAME}"])
    _agent_args.extend(
        [
            "--ak",
            f"agent_path={AGENT_PATH}",
            "--ak",
            f"agent_module={AGENT_MODULE}",
        ]
    )
    if AGENT_DEPS:
        _agent_args.extend(["--ak", f"agent_deps={AGENT_DEPS}"])

_agent_env = {"AWS_REGION": REGION, **_aws_creds}
if _is_native_agent and HARBOR_AGENT == "claude-code":
    # Keep Claude Code's main, fast, and subagent calls on the benchmark model.
    _agent_env.update(
        {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_MODEL": HARBOR_MODEL_NAME,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": HARBOR_MODEL_NAME,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": HARBOR_MODEL_NAME,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": HARBOR_MODEL_NAME,
            "CLAUDE_CODE_SUBAGENT_MODEL": HARBOR_MODEL_NAME,
        }
    )
elif _is_native_agent and HARBOR_AGENT == "opencode":
    if HARBOR_MODEL_NAME.startswith("openai/"):
        missing = [
            key
            for key in ("OPENAI_API_KEY", "OPENCODE_OPENAI_BASE_URL")
            if not os.environ.get(key)
        ]
        if not DRY_RUN and missing:
            raise SystemExit(
                f"{', '.join(missing)} required for OpenCode Mantle runs."
            )
        _agent_env.update(
            {
                "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                "OPENAI_BASE_URL": "${OPENCODE_OPENAI_BASE_URL}",
            }
        )
    else:
        if not DRY_RUN and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            raise SystemExit(
                "AWS_BEARER_TOKEN_BEDROCK is required for OpenCode Bedrock runs."
            )
        # Harbor resolves host-env templates at construction and persists the
        # templates, not the secret values, in the job configuration.
        _agent_env["AWS_BEARER_TOKEN_BEDROCK"] = "${AWS_BEARER_TOKEN_BEDROCK}"
elif _is_native_agent and HARBOR_AGENT == "codex":
    # Codex reads OPENAI_API_KEY and OPENAI_BASE_URL through its
    # ModelConnectionSpec(default_provider="openai"). run-benchmark.sh sets
    # both via configure_codex_mantle before invoking this script.
    missing = [
        key
        for key in ("OPENAI_API_KEY", "CODEX_OPENAI_BASE_URL")
        if not os.environ.get(key)
    ]
    if not DRY_RUN and missing:
        raise SystemExit(
            f"{', '.join(missing)} required for Codex Mantle runs."
        )
    _agent_env.update(
        {
            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
            "OPENAI_BASE_URL": "${CODEX_OPENAI_BASE_URL}",
        }
    )
elif not _is_native_agent and os.environ.get("STRANDS_MODEL"):
    _agent_env["STRANDS_MODEL"] = os.environ["STRANDS_MODEL"]

sys.argv = [
    "harbor", "run",
    *_agent_args,
    *(("-p", DATASET_PATH) if DATASET_PATH else ("-d", DATASET)),
    *(("--plugin", JOB_PLUGIN) if JOB_PLUGIN else ()),
    "-n", CONCURRENCY,
    *(["-k", N_ATTEMPTS] if N_ATTEMPTS else []),
    *[arg for name in TASK_NAMES for arg in ("-i", name)],
    *(["-l", N_TASKS] if N_TASKS else []),
    "-e", "ec2",
    "--ek", f"region={REGION}",
    "--ek", "ami_id=ami-02657217947a5c8ca",
    "--ek", f"instance_type={INSTANCE_TYPE}",
    "--ek", f"root_volume_size_gb={ROOT_VOLUME_GB}",
    "--ek", f"key_name={KEY_NAME}",
    "--ek", f"ssh_key_path={SSH_KEY_PATH}",
    "--ek", f'security_group_ids=["{SECURITY_GROUP}"]',
    *(["--ek", f"subnet_id={SUBNET}"] if SUBNET else []),
    "--ek", "ssh_user=ubuntu",
    "--ek", "compose_up_timeout_sec=600",
    "--ek", f"launch_rate_per_sec={EC2_LAUNCH_RATE_PER_SEC}",
    "--ek", f"launch_burst={EC2_LAUNCH_BURST}",
    "--ek", f'tags={{"harbor:job":"{JOB_TAG}"}}',
    *(["--ek", f"iam_instance_profile={IAM_INSTANCE_PROFILE}"] if IAM_INSTANCE_PROFILE else []),
    *[arg for k, v in _agent_env.items() for arg in ("--ae", f"{k}={v}")],
    "-o", OUTPUT_DIR,
    "--job-name", JOB_NAME,
    "--max-retries", MAX_RETRIES,
    "--retry-include", "EnvironmentStartTimeoutError",
    "--retry-include", "RuntimeError",
    # Bedrock throttling was the single largest error class in the k=1 baselines
    # (53/206 on sonnet-5). Retrying it keeps a throttled trial from scoring 0.
    "--retry-include", "ApiRateLimitError",
    "--yes",
    "--debug",
]

if DRY_RUN:
    print(json.dumps(sys.argv, indent=2))
    raise SystemExit(0)

from harbor.cli.main import app
app()
