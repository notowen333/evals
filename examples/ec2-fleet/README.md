# EC2 Fleet Runner

Run a Harbor benchmark across a fleet of **ephemeral EC2 instances** — one
instance per task, all running in parallel. No standing infrastructure: the
only thing you own ahead of time is an SSH key pair. Everything else (instances,
Docker, networks) is created on the fly and torn down when each task finishes.

This is the path for **scaled runs** (e.g. all 500 SWE-bench Verified tasks at
once). For single-instance / local Docker runs, use `strands-evals benchmark`
instead.

## Why this exists (and why it bypasses the CLI wrapper)

`strands-evals benchmark` shells out to `harbor run` as a **subprocess**. That
subprocess boundary was fatal for fleet scale — see "Thread pool" below. This
script instead calls Harbor's job runner **in-process** (`from harbor.cli.main
import app; app()`) so we control the event loop and thread pool.

Consequence: Harbor knows nothing about the `on_benchmark_complete` hook (that
only fires in the `strands-evals` wrapper). So result upload to S3 is done here,
on the orchestrator, after the run finishes.

## Quick start

```bash
# One-time: create the SSH key pair the orchestrator uses to reach fleet nodes
aws ec2 create-key-pair --key-name harbor-benchmark \
  --query KeyMaterial --output text > ~/.ssh/harbor-benchmark.pem
chmod 600 ~/.ssh/harbor-benchmark.pem

# One-time: a security group allowing SSH inbound (orchestrator -> nodes)
# (reuse an existing one, or create + authorize port 22)

# Run
.harbor-venv/bin/python examples/ec2-fleet/run.py
```

Configure via env vars (see the `Config` block in `run.py`):

```bash
DATASET=swe-bench/swe-bench-verified \
CONCURRENCY=500 \
INSTANCE_TYPE=m7i.large \
ROOT_VOLUME_GB=64 \
IAM_INSTANCE_PROFILE=StrandsBenchmarkHarborNodeRole \
JOB_NAME=swe-verified-fleet \
OUTPUT_DIR=jobs/swe-verified-fleet \
.harbor-venv/bin/python examples/ec2-fleet/run.py
```

Use `N_TASKS=8` to cap the task count for a smoke test.

## How it works

```
Orchestrator (this script, runs on your Mac / a bastion)
  ├── launches N ephemeral EC2 nodes (one per task)
  ├── each node: boot → bootstrap Docker → build task image → run agent → verify
  ├── Harbor pulls each node's results back to OUTPUT_DIR (local) over SSH
  ├── each node is terminated when its task finishes
  └── after ALL tasks done → _upload_results_to_s3() bulk-uploads OUTPUT_DIR
```

**Nodes are pure compute.** They never touch S3 and (with an instance profile)
never receive forwarded credentials. Results aggregate locally on the
orchestrator first, then upload in one pass.

## Hard-won learnings

These are the things that broke, and the fixes baked into `run.py`:

### 1. Thread pool — the reason for the in-process design
Harbor's EC2 environment uses `asyncio.to_thread()` for blocking boto3 waiter
calls (`instance_running`, `instance_status_ok`). Python's default thread pool
is `min(32, cpu_count + 4)` — **14 on a 10-core Mac**. With 500 trials each
needing waiter threads, only ~30 instances could progress at once.

Fix: monkey-patch `asyncio.run` to install a `ThreadPoolExecutor(max_workers=512)`
on the loop. This **only works in-process** — a subprocess (the CLI wrapper) gets
its own default 14-worker pool, which is why we bypass it. With the patch we hit
491+ concurrent instances in ~4 minutes.

### 2. Instance type — `m7i.large`, not `t3.medium`
`t3.medium` is burstable. The agent-setup phase (install uv + download Python
3.12 + strands + botocore + deps) is CPU/IO heavy and exhausts burst credits,
throttling to baseline. Result: **every** trial hit `AgentSetupTimeoutError` at
360s (0/500 agents even started).

`m7i.large` (non-burstable) installs in ~40s and completes end-to-end reliably.
The agent itself is I/O-bound (Bedrock calls) so it doesn't need much CPU — the
setup phase is what demands it. Cost for a 20-min × 500 run: ~$17 on m7i.large
vs ~$7 on t3.medium. Negligible; pay for reliability.

### 3. Root volume — 64GB, not the 8GB default
The Ubuntu AMI defaults to an 8GB root. SWE-bench task images + Docker layers +
uv's cache (Python download + wheels) overflow it → `No space left on device`
during dependency extraction. 64GB `gp3` clears it. Extra cost is ~$0.20 total
for a 500-instance run (volumes are ephemeral, alive ~10 min each).

### 4. Python version — `uv venv --python 3.12`
Many SWE-bench tasks ship a Python 3.9 conda `testbed` environment. Without
pinning, `uv` builds the agent venv against 3.9, and `strands-agents>=1.45.0`
requires `>=3.10` → install fails. The adapter now forces `--python 3.12` (uv
downloads its own interpreter, independent of the container's Python). *(Fix
lives in the adapter: `src/strands_evals/benchmarks/harbor/installed/py/agent.py`.)*

### 5. Credentials — IAM instance profile beats forwarding
Two ways for the agent to reach Bedrock from a node:
- **Forward creds** (`_resolve_aws_creds()` → `--ae AWS_ACCESS_KEY_ID=...`):
  works, but reading env vars alone fails under SSO/assumed-role, and the tokens
  can expire mid-run on long jobs.
- **IAM instance profile** (`IAM_INSTANCE_PROFILE=...`): attaches a role to each
  node; boto3 on the node auto-refreshes creds from the instance metadata. No
  secrets in argv, no expiry. The role needs `bedrock:InvokeModel` +
  `bedrock:InvokeModelWithResponseStream`.

Prefer the instance profile. When it's set, the script skips cred forwarding.

### 6. EC2 `RunInstances` API rate limit (not fully solved)
The `RunInstances` API has a hard, **non-adjustable** token bucket (capacity 5,
refill 2/sec). Firing 500 launches at once overwhelms it; boto3's 10 built-in
retries absorb most, but some trials still fail with `RequestLimitExceeded`.
`--max-retries` + `--retry-include EnvironmentStartTimeoutError` catches the
launch-timeout variants. For a true fix, Harbor would need to batch launches
with `RunInstances MaxCount` (one API call for many instances) — an upstream
change. Or open an AWS Support case (the quota is raise-able via support even
though self-service says "not adjustable").

### 7. Orphan cleanup
If the orchestrator dies, in-flight nodes leak. `run.py` registers a SIGINT/
SIGTERM handler + `atexit` that terminates every instance with the fleet key
pair. Note: the S3 upload only runs on a *clean* finish — a hard crash cleans up
instances but won't upload the partial `OUTPUT_DIR`.

## Cost reference (us-east-1 on-demand, 20 min × 500 instances)

| Instance    | $/hr    | per 20-min task | × 500  |
|-------------|---------|-----------------|--------|
| t3.medium   | $0.0416 | $0.0139         | ~$7    |
| m7i.large   | $0.1008 | $0.0336         | ~$17   |

Plus ~$0.73 EBS (64GB gp3, prorated). Instance type dominates; storage is noise.

## Prerequisites checklist

- [ ] AWS credentials (SSO login or env) on the orchestrator
- [ ] SSH key pair created; private key at `SSH_KEY_PATH`
- [ ] Security group allowing SSH (port 22) inbound, in the target VPC
- [ ] Subnet in that VPC with a route to the internet (public IP assigned)
- [ ] (Recommended) IAM instance profile with Bedrock invoke permissions
- [ ] Sufficient on-demand vCPU quota (500 × m7i.large = 1000 vCPUs)
