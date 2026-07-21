# Running Benchmarks

## Quick start

From any machine with AWS credentials:

```bash
aws ssm send-command \
  --instance-ids "i-0cfa2a926fa20f5f0" \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["#!/bin/bash","export HOME=/root","cd /home/ubuntu/evals","git pull origin harbor-adapter","nohup env VERSION_TAG=<sha> bash examples/ec2-fleet/run-benchmark.sh <agent> <model> <dataset> > /home/ubuntu/<name>.log 2>&1 < /dev/null &","disown $!","echo PID: $!"],"executionTimeout":["120"]}' \
  --timeout-seconds 120 \
  --region us-east-1
```

Replace `<sha>`, `<agent>`, `<model>`, `<dataset>`, and `<name>` with your values.

## Job naming

Jobs are named automatically:

```
<agent>@<commit-sha>--<model>--<dataset-slug>
```

The `@<commit-sha>` comes from the `VERSION_TAG` env var. **Always set it** so
you can trace exactly which Stan code produced a result.

| Component | Source | Example |
|-----------|--------|---------|
| agent | First arg (directory name) | `stan_0.2.0` |
| commit-sha | `VERSION_TAG` env var | `2c58790` |
| model | Second arg (alias) | `opus-4.6` |
| dataset-slug | Third arg with `/` → `-` | `terminal-bench-terminal-bench-2-1` |

Full example: `stan_0.2.0@2c58790--opus-4.6--terminal-bench-terminal-bench-2-1`

### How to find the commit SHA

The Stan version on the orchestrator is always printed during install:
```
pip show strands-agents-stan | grep Version
# 0.0.1.dev4+g2c58790c1 → sha is 2c58790
```

Or check the Stan repo directly:
```bash
cd stan && git log --oneline -1
```

## Arguments

```
run-benchmark.sh <agent> <model> <dataset> [concurrency]
```

| Arg | What to pass | Default |
|-----|-------------|---------|
| `agent` | Directory under `examples/` | required |
| `model` | Alias or raw Bedrock model ID | required |
| `dataset` | Harbor dataset path | required |
| `concurrency` | Parallel EC2 instances | 500 |

### Model aliases

| Alias | Model ID |
|-------|----------|
| `sonnet-4.6` / `sonnet` | `us.anthropic.claude-sonnet-4-6` |
| `opus-4.6` | `global.anthropic.claude-opus-4-6-v1` |
| `opus-4.8` / `opus` | `us.anthropic.claude-opus-4-8` |
| `sonnet-5` / `sonnet5` | `global.anthropic.claude-sonnet-5` |

### Datasets

| Benchmark | Dataset path | Tasks |
|-----------|-------------|-------|
| SWE-bench Verified | `swe-bench/swe-bench-verified` | 500 |
| Terminal-Bench 2.1 | `terminal-bench/terminal-bench-2-1` | 89 |
| GAIA | `gaia` | 165 |
| MedAgentBench | `medagentbench` | 300 |

## Environment variables

| Var | Purpose | Default |
|-----|---------|---------|
| `VERSION_TAG` | Commit SHA in job name | (none — omits `@sha`) |
| `STAN_BRANCH` | Git ref to install Stan from | `main` |
| `INSTANCE_TYPE` | Fleet node instance type | `m7i.xlarge` |

## Full examples

```bash
# SWE-bench Verified, Sonnet 4.6
env VERSION_TAG=2c58790 bash examples/ec2-fleet/run-benchmark.sh stan_0.2.0 sonnet-4.6 swe-bench/swe-bench-verified

# Terminal-Bench 2.1, Opus 4.6
env VERSION_TAG=2c58790 bash examples/ec2-fleet/run-benchmark.sh stan_0.2.0 opus-4.6 terminal-bench/terminal-bench-2-1

# Terminal-Bench 2.1, Sonnet 5
env VERSION_TAG=2c58790 bash examples/ec2-fleet/run-benchmark.sh stan_0.2.0 sonnet-5 terminal-bench/terminal-bench-2-1

# GAIA, Opus 4.6, 300 concurrency
env VERSION_TAG=2c58790 bash examples/ec2-fleet/run-benchmark.sh stan_0.2.0 opus-4.6 gaia 300

# Specific Stan branch/commit
env VERSION_TAG=abc1234 STAN_BRANCH=abc1234 bash examples/ec2-fleet/run-benchmark.sh stan_0.2.0 sonnet-4.6 swe-bench/swe-bench-verified
```

## Viewing results

The Harbor viewer is a web app running on the orchestrator.

```bash
# Kill stale tunnels
lsof -ti:5173 | xargs kill -9 2>/dev/null; lsof -ti:8081 | xargs kill -9 2>/dev/null

# Connect (two ports: React frontend + Python API)
aws ssm start-session --target i-0cfa2a926fa20f5f0 --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["5173"],"localPortNumber":["5173"]}' --region us-east-1 &
aws ssm start-session --target i-0cfa2a926fa20f5f0 --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8081"],"localPortNumber":["8081"]}' --region us-east-1

# Open http://localhost:5173
```

## Monitoring

```bash
# Is a run still going?
pgrep -f run-benchmark && echo RUNNING || echo DONE

# Tail the log
tail -f /home/ubuntu/<name>.log

# Count remaining fleet instances
aws ec2 describe-instances --filters \
  Name=instance-state-name,Values=running,pending \
  Name=key-name,Values=harbor-benchmark \
  --query "length(Reservations[].Instances[])" --output text
```

## Results

| Where | Path |
|-------|------|
| Orchestrator | `jobs/<job-name>/` |
| S3 | `s3://strands-benchmark-results/<agent>/<model>/<dataset-slug>/` |
| Archived (previous runs) | `jobs/archived/<job-name>--<timestamp>/` |

Previous runs with the same job name are **archived, never deleted**.
S3 versioning is enabled on the results bucket.

---

## Under the hood

### What `run-benchmark.sh` does

1. Resolves agent path, model ID, job name
2. For `stan_*` agents: fetches PAT from Secrets Manager, pip installs Stan,
   copies `strands_stan/` into agent dir so containers can import it
3. Archives any previous run with same name
4. Calls `run.py` → `harbor run` with EC2 fleet environment (500 parallel nodes)
5. Each node: boot → Docker → install strands → upload agent → run → verify
6. Post-run: patches agent display name, uploads all results to S3

### Infrastructure

- **Orchestrator:** `i-0cfa2a926fa20f5f0` (c7i.8xlarge, 32 vCPU, us-east-1)
- **Fleet nodes:** m7i.xlarge (4 vCPU, 16GB, 64GB disk) — ephemeral, one per task
- **IAM:** Fleet nodes use `StrandsBenchmarkHarborNodeRole` (Bedrock access)
- **Viewer:** Runs in `.viewer-venv` (isolated from benchmark installs)
- **Stan PAT:** Secrets Manager `stan_pat-lUflBx`

### Hard-won learnings

1. **Run orchestrator on big box** — 500 SSH sessions need ≥16 cores. Laptop stalls.
2. **Non-burstable instances** — t3 exhausts credits during setup. m7i.xlarge works.
3. **64GB root volume** — Docker layers + uv cache overflow 8GB default.
4. **Python 3.12 forced** — SWE-bench ships py3.9, strands needs ≥3.10.
5. **Instance profile > forwarded creds** — no token expiry, no secrets in argv.
6. **RunInstances rate limit** — bucket of 5, refill 2/sec. `--max-retries 2` handles it.
7. **Never delete results** — archive only. S3 versioning as backup.
