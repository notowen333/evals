# StrandsBenchmark Orchestrator

The EC2 orchestrator (`i-0cfa2a926fa20f5f0`) runs benchmark jobs via the
`run-benchmark.sh` script. It launches ephemeral EC2 fleet instances, manages
the run, and uploads results to S3 when complete.

## Usage

Via SSM from any machine with AWS credentials:

```bash
aws ssm send-command \
  --instance-ids "i-0cfa2a926fa20f5f0" \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["#!/bin/bash","cd /home/ubuntu/evals","setsid bash strands-infra-runner/run-benchmark.sh <agent> <model> <dataset> [concurrency] </dev/null >/home/ubuntu/<name>.log 2>&1 &","sleep 2","ps aux | grep run-benchmark | grep -v grep && echo LAUNCHED"]}' \
  --timeout-seconds 120
```

**Important:** SSM `AWS-RunShellScript` kills background processes when the command
exits. Plain `nohup &` / `disown` does NOT work. You must use `setsid` to fully
detach the process into its own session. The `sleep 2` gives it time to start so
you can confirm it's running.

## Arguments

```
run-benchmark.sh <agent> <model> <dataset> [concurrency]
```

| Arg | Options | Default |
|-----|---------|---------|
| `agent` | `stan_0.2.0`, `benchmark_agent`, or any dir under `examples/` | required |
| `model` | `sonnet-4.6`, `opus-4.6`, `opus-4.8`, `sonnet-5`, or a raw model ID (e.g. `openai.gpt-5.6-sol` for GPT via Bedrock Mantle) | required |
| `dataset` | Any Harbor dataset (e.g. `swe-bench/swe-bench-verified`, `terminal-bench/terminal-bench-2-1`, `gaia`) | required |
| `concurrency` | Number of parallel EC2 instances | 500 |

## Examples

```bash
# SWE-bench Verified with Stan on Sonnet 4.6
run-benchmark.sh stan_0.2.0 sonnet-4.6 swe-bench/swe-bench-verified

# Terminal-Bench 2.1 with the benchmark agent on Opus
run-benchmark.sh benchmark_agent opus-4.8 terminal-bench/terminal-bench-2-1 89

# GAIA with Stan on Sonnet 4.6
run-benchmark.sh stan_0.2.0 sonnet-4.6 gaia
```

## Job naming and results

Job names are auto-generated as `<agent>--<model>--<dataset-slug>`. This produces
a flat directory under `jobs/` that the Harbor viewer can scan directly:

```
jobs/
├── stan_0.2.0--sonnet-4.6--terminal-bench-terminal-bench-2-1/
│   ├── config.json       ← Harbor job config (agent, env, dataset)
│   ├── result.json       ← Aggregate metrics (started_at, stats, evals)
│   ├── lock.json
│   ├── job.log
│   └── <trial-id>/      ← One per task (contains agent logs, trajectory, etc.)
├── stan_0.2.0--opus-4.8--medagentbench/
│   └── ...
```

**Important:** The job directory must be flat (`jobs/<name>/<trials>`), NOT
double-nested (`jobs/<name>/<name>/<trials>`). The `run-benchmark.sh` script
sets `-o jobs` and `--job-name <name>` to produce the correct structure. If you
see `started_at: null` or `n_total_trials: 1` in the viewer, the directory is
double-nested and needs to be flattened.

The `result.json` contains `started_at`/`finished_at` timestamps — these are
populated by Harbor automatically when the run begins and ends. The viewer uses
these to show dates.

Results upload to S3 at:
```
s3://strands-benchmark-results/<agent>/<model>/<dataset-slug>/
```

Local results on the orchestrator at:
```
/home/ubuntu/evals/jobs/<agent>--<model>--<dataset-slug>/
```

### Viewing results

The Harbor viewer runs on port 7842 (production mode with pre-built static assets).
Access via SSM port forwarding:

```bash
# From your machine:
aws ssm start-session --target i-0cfa2a926fa20f5f0 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["7842"],"localPortNumber":["7842"]}' \
  --region us-east-1

# Then open http://localhost:7842
```

If the viewer needs restarting on the orchestrator:
```bash
harbor view jobs/ --port 7842 --host 0.0.0.0 --jobs
```

**Note:** The viewer requires pre-built frontend static assets at
`<site-packages>/harbor/viewer/static/`. If missing (e.g. after pip reinstall),
rebuild from the source tree:
```bash
cd /home/ubuntu/harbor-src/apps/viewer
npm install @rollup/rollup-linux-x64-gnu @tailwindcss/oxide-linux-x64-gnu lightningcss-linux-x64-gnu
npm run build
cp -r build/client /home/ubuntu/evals/.venv/lib/python3.13/site-packages/harbor/viewer/static
```

## Monitoring

Check if a run is alive:
```bash
aws ssm send-command ... --parameters '{"commands":["#!/bin/bash","ps aux | grep run-benchmark | grep -v grep && echo RUNNING || echo DONE"]}'
```

Tail the log:
```bash
aws ssm send-command ... --parameters '{"commands":["#!/bin/bash","tail -20 /home/ubuntu/benchmark-<job-name>.log"]}'
```

Check fleet instance count:
```bash
aws ssm send-command ... --parameters '{"commands":["aws ec2 describe-instances --filters Name=instance-state-name,Values=running,pending Name=key-name,Values=harbor-benchmark --query \"length(Reservations[].Instances[])\" --output text"]}'
```

## Infrastructure

- **Orchestrator:** `c7i.8xlarge` (32 vCPU) — handles 500 concurrent SSH sessions
- **Fleet instances:** `m7i.xlarge` (4 vCPU, 16GB, 64GB disk) — ephemeral, one per task
- **AMI:** `ami-02657217947a5c8ca` (Ubuntu + Docker CE/Compose pre-baked, no bootstrap needed)
- **SSH key:** `harbor-benchmark` (private key at `/root/.ssh/harbor-benchmark.pem` on orchestrator)
- **IAM:** Fleet nodes use `StrandsBenchmarkHarborNodeRole` instance profile (Bedrock access)
- **Region:** us-east-1
- **Subnet:** None pinned — EC2 auto-spreads across AZs with capacity

## Stan agent setup

Stan agents (`stan_*`) are installed directly from the private repo via a GitHub
PAT stored in Secrets Manager (`stan_pat-lUflBx`). No manual sync or scp needed.

- `STAN_BRANCH` env var controls which branch to install (default: `main`).
- The PAT is fetched at run time by `run-benchmark.sh`.
- `strands-agents>=1.45.0` is passed as `AGENT_DEPS` so the fleet containers
  install the correct SDK version.

To cut a new Stan version for benchmarking, just tag/branch in the Stan repo and
pass `STAN_BRANCH=<ref>` when launching the run.

## Directory layout on the orchestrator

```
/home/ubuntu/evals/
├── jobs/                ← active benchmark results (viewer points here)
│   ├── stan_0.2.0--opus-4.8--medagentbench/
│   └── ...
├── jobs/legacy/         ← old/pre-refactor results (not shown in viewer)
└── examples/stan_0.2.0/ ← agent wrapper (agent.py only, no Stan source)
```

Old results were moved to `jobs/legacy/` so the viewer only shows current runs.
If you need to re-flatten a double-nested job: `mv jobs/X/X/* jobs/X/ && rmdir jobs/X/X`.

## Prerequisites

Before first use, ensure the orchestrator has:
- [x] Latest code: `git pull origin harbor-adapter`
- [x] venv installed: `source .venv/bin/activate && pip install -e .[harbor]`
- [x] SSH key at `/root/.ssh/harbor-benchmark.pem`
- [x] Secrets Manager access for `stan_pat-lUflBx` (via instance role)
- [x] bun installed (`/root/.bun/bin/bun`) for the Harbor viewer dev mode
- [x] Harbor viewer running: `harbor view jobs/ --port 7842 --host 0.0.0.0 --jobs`
- [x] Docker network pool expanded (`/etc/docker/daemon.json` — only for local Docker runs)

## Known Limitations

- **`allow_internet=false` benchmarks** (e.g. DeepSWE) do not work on the EC2 fleet.
  The EC2 environment applies `network_mode: none` statically — no dynamic switching.
  These require Docker local or Modal environments.
- **Orchestrator must stay alive** for the full run. Use `nohup` + SSM. If it dies,
  the cleanup handler terminates orphan instances but S3 upload won't fire.
- **EC2 RunInstances rate limit** (bucket 5, refill 2/sec) causes some launch failures
  at 500 concurrency. `--max-retries 2 --retry-include RuntimeError` handles this.
