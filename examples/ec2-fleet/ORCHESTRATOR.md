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
  --parameters '{"commands":["#!/bin/bash","cd /home/ubuntu/evals","nohup bash examples/ec2-fleet/run-benchmark.sh <agent> <model> <dataset> [concurrency] > /home/ubuntu/<name>.log 2>&1 < /dev/null &","disown $!","echo PID: $!"],"executionTimeout":["120"]}' \
  --timeout-seconds 120
```

## Arguments

```
run-benchmark.sh <agent> <model> <dataset> [concurrency]
```

| Arg | Options | Default |
|-----|---------|---------|
| `agent` | `stan_0.2.0`, `benchmark_agent`, or any dir under `examples/` | required |
| `model` | `sonnet-4.6`, `opus-4.8`, `sonnet-5`, or a raw Bedrock model ID | required |
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

## Results

Results upload to S3 at:
```
s3://strands-benchmark-results/<agent>/<model>/<dataset-slug>/
```

Local results on the orchestrator at:
```
/home/ubuntu/evals/jobs/<agent>--<model>--<dataset-slug>/
```

## Monitoring

Check if a run is alive:
```bash
aws ssm send-command ... --parameters '{"commands":["pgrep -f run-benchmark && echo RUNNING || echo DONE"]}'
```

Tail the log:
```bash
aws ssm send-command ... --parameters '{"commands":["tail -20 /home/ubuntu/<name>.log"]}'
```

Check fleet instance count:
```bash
aws ssm send-command ... --parameters '{"commands":["aws ec2 describe-instances --filters Name=instance-state-name,Values=running,pending Name=key-name,Values=harbor-benchmark --query \"length(Reservations[].Instances[])\" --output text"]}'
```

## Infrastructure

- **Orchestrator:** `c7i.8xlarge` (32 vCPU) — handles 500 concurrent SSH sessions
- **Fleet instances:** `m7i.xlarge` (4 vCPU, 16GB, 64GB disk) — ephemeral, one per task
- **SSH key:** `harbor-benchmark` (private key at `/root/.ssh/harbor-benchmark.pem` on orchestrator)
- **IAM:** Fleet nodes use `StrandsBenchmarkHarborNodeRole` instance profile (Bedrock access)
- **Region:** us-east-1
- **Subnet:** `subnet-0ca6e05ed9d6e1871` (us-east-1d, public)

## Prerequisites

Before first use, ensure the orchestrator has:
- [x] Latest code: `git pull origin harbor-adapter`
- [x] venv installed: `source .venv/bin/activate && pip install -e .[harbor]`
- [x] SSH key at `/root/.ssh/harbor-benchmark.pem`
- [x] Stan agents at `examples/stan_<version>/` (in-repo, synced from `stan/` via `sync-source.sh`)
- [x] Docker network pool expanded (`/etc/docker/daemon.json` — only for local Docker runs)

## Known Limitations

- **`allow_internet=false` benchmarks** (e.g. DeepSWE) do not work on the EC2 fleet.
  The EC2 environment applies `network_mode: none` statically — no dynamic switching.
  These require Docker local or Modal environments.
- **Orchestrator must stay alive** for the full run. Use `nohup` + SSM. If it dies,
  the cleanup handler terminates orphan instances but S3 upload won't fire.
- **EC2 RunInstances rate limit** (bucket 5, refill 2/sec) causes some launch failures
  at 500 concurrency. `--max-retries 2 --retry-include RuntimeError` handles this.
