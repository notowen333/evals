# Running Benchmarks

## Quick start

From any machine with AWS credentials:

```bash
aws ssm send-command \
  --instance-ids "i-0cfa2a926fa20f5f0" \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["#!/bin/bash","export HOME=/root","git config --global --add safe.directory /home/ubuntu/evals","cd /home/ubuntu/evals","git pull origin harbor-adapter","setsid env STAN_BRANCH=<ref> bash strands-infra-runner/run-benchmark.sh stan <model> <dataset> <concurrency> </dev/null >/home/ubuntu/<name>.log 2>&1 &","sleep 2","pgrep -af run-benchmark && echo LAUNCHED"],"executionTimeout":["120"]}' \
  --timeout-seconds 120 \
  --region us-east-1
```

Replace `<ref>`, `<model>`, `<dataset>`, `<concurrency>`, and `<name>` with your values. Use
`setsid`; SSM terminates processes left in its command session after the command
exits, even when they were started with `nohup` or `disown`.

## Job naming

Jobs are named automatically:

```
<agent>@<commit-sha>--<model>--<dataset-slug>
```

The `@<commit-sha>` comes from the `VERSION_TAG` env var. For Stan, the runner
derives it from `STAN_BRANCH` when `VERSION_TAG` is unset.

| Component | Source | Example |
|-----------|--------|---------|
| agent | First arg | `stan`, `claude-code`, `opencode`, or `omp` |
| version | Stan commit or native product version | `2c58790` or `2.1.220` |
| model | Second arg (alias) | `opus-4.6` |
| dataset-slug | Third arg with `/` → `-` | `terminal-bench-terminal-bench-2-1` |

Full example: `stan@2c58790--opus-4.6--terminal-bench-terminal-bench-2-1`

### How to find the commit SHA

The runner prints `Stan commit: <sha>` after resolving `STAN_BRANCH`, and the
same SHA appears in the job directory name.

## Arguments

```
run-benchmark.sh <agent> <model> <dataset> [concurrency]
```

| Arg | What to pass | Default |
|-----|-------------|---------|
| `agent` | `claude-code`, `opencode`, `omp`, or a directory under `strands-infra-runner/agents/` | required |
| `model` | Alias or raw Bedrock model ID | required |
| `dataset` | Harbor dataset path | required |
| `concurrency` | Parallel EC2 instances | Dataset task count, capped at 2,000; 500 for unknown datasets |

### Model aliases

| Alias | Model ID |
|-------|----------|
| `sonnet-4.6` / `sonnet` | `us.anthropic.claude-sonnet-4-6` |
| `opus-4.6` | `global.anthropic.claude-opus-4-6-v1` |
| `opus-4.8` / `opus` | `global.anthropic.claude-opus-4-8` |
| `sonnet-5` / `sonnet5` | `global.anthropic.claude-sonnet-5` |
| `kimi-k2.5` / `kimi-2.5` / `kimi` | `moonshotai.kimi-k2.5` |

Kimi runs through Bedrock Runtime in `us-east-1`. See the
[Kimi K2.5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k2-5.html).

### Datasets

| Benchmark | Dataset path | Tasks |
|-----------|-------------|-------|
| SWE-bench Verified | `swe-bench/swe-bench-verified` | 500 |
| Terminal-Bench 2.1 | `terminal-bench/terminal-bench-2-1` | 89 |
| GAIA | `gaia/gaia` | 165 |
| MedAgentBench | `stanford/medagentbench` | 300 |
| TAU3 | `sierra-research/tau3-bench` | 375 |

## Environment variables

| Var | Purpose | Default |
|-----|---------|---------|
| `VERSION_TAG` | Commit SHA in job name | Stan commit resolved from `STAN_BRANCH` |
| `STAN_BRANCH` | Git ref to install Stan from | `main` |
| `CLAUDE_CODE_VERSION` | Claude Code CLI version installed by Harbor | `2.1.220` |
| `OPENCODE_VERSION` | OpenCode CLI version installed by Harbor | `1.18.9` |
| `OMP_VERSION` | Omp (Oh-My-Pi) CLI version installed by Harbor | `17.2.15` |
| `HARBOR_STRANDS_CHECKOUT` | Separate Harbor fork checkout used by the custom benchmark | `/home/ubuntu/harbor-strands-working` |
| `INSTANCE_TYPE` | Fleet node instance type | `m7i.xlarge` |
| `BEDROCK_API_KEY_SECRET_ID` | Bedrock bearer-token secret used by OpenCode and TAU3 | `bedrock_api_key` |
| `TAU3_MANTLE_REGION` | Bedrock Mantle region used by TAU3 | `us-east-1` |
| `TAU3_USER_MODEL` | TAU3 simulated-user model, including LiteLLM provider prefix | `openai/openai.gpt-oss-120b` |
| `TAU3_NL_ASSERTIONS_MODEL` | TAU3 assertion-grader model | Same as `TAU3_USER_MODEL` |

## Full examples

```bash
# SWE-bench Verified, Sonnet 4.6
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan sonnet-4.6 swe-bench/swe-bench-verified

# Terminal-Bench 2.1, Opus 4.6
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan opus-4.6 terminal-bench/terminal-bench-2-1

# Terminal-Bench 2.1, Sonnet 5
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan sonnet-5 terminal-bench/terminal-bench-2-1

# Claude Code through Bedrock and Harbor's native adapter
bash strands-infra-runner/run-benchmark.sh claude-code sonnet-4.6 swe-bench/swe-bench-verified

# OpenCode through Bedrock and Harbor's native adapter
bash strands-infra-runner/run-benchmark.sh opencode sonnet-4.6 swe-bench/swe-bench-verified

# Omp (Oh-My-Pi) through Bedrock — the fleet instance profile handles auth
bash strands-infra-runner/run-benchmark.sh omp sonnet-4.6 swe-bench/swe-bench-verified

# Full supported pass@2 index matrix for both native products
setsid bash strands-infra-runner/run-matrix.sh \
  -a claude-code,opencode -k 2 \
  </dev/null >/home/ubuntu/native-agents-k2.log 2>&1 &

# GAIA, Opus 4.6
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan opus-4.6 gaia/gaia

# Terminal-Bench 2.1, Kimi K2.5
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan kimi-k2.5 terminal-bench/terminal-bench-2-1

# One-task TAU3 smoke test
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan sonnet-4.6 sierra-research/tau3-bench 1

# Materialize the 206-task custom benchmark, then run it
bash strands-infra-runner/setup-strands-harness-benchmark.sh
env VERSION_TAG=2c58790 bash strands-infra-runner/run-benchmark.sh stan sonnet-4.6 strands-harness-benchmark-index

# Specific Stan branch/commit
env VERSION_TAG=abc1234 STAN_BRANCH=abc1234 bash strands-infra-runner/run-benchmark.sh stan sonnet-4.6 swe-bench/swe-bench-verified
```

## Viewing results

The Harbor viewer runs on port 7842 on the orchestrator.

```bash
# Connect to the production viewer
aws ssm start-session --target i-0cfa2a926fa20f5f0 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["7842"],"localPortNumber":["7842"]}' \
  --region us-east-1

# Open http://localhost:7842
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
2. Selects Harbor's native adapter for `claude-code`, `opencode`, and `omp`;
   other agents continue through the custom Strands adapter
3. For `stan` agents: fetches PAT from Secrets Manager, pip installs Stan,
   copies `strands_stan/` into agent dir so containers can import it
4. For TAU3: fetches `bedrock_api_key` and configures the simulated user and
   assertion grader to use Bedrock Mantle
5. Archives any previous run with same name
6. Calls `run.py` → `harbor run` with an EC2 fleet sized to the dataset
7. Each node: boot → Docker → install the selected agent → run → verify
8. Uploads all results to S3

### Infrastructure

- **Orchestrator:** `i-0cfa2a926fa20f5f0` (c7i.8xlarge, 32 vCPU, us-east-1)
- **Fleet nodes:** m7i.xlarge (4 vCPU, 16GB, 64GB disk) — ephemeral, one per task
- **IAM:** Fleet nodes use `StrandsBenchmarkHarborNodeRole` (Bedrock access)
- **Viewer:** Runs in `.viewer-venv` (isolated from benchmark installs)
- **Stan PAT:** Secrets Manager `stan_pat-lUflBx`
- **TAU3 Bedrock API key:** Secrets Manager `bedrock_api_key`

### Native competitor agents

`claude-code` and `opencode` run the actual product CLIs through Harbor's
built-in adapters. Claude Code uses the fleet node's
`StrandsBenchmarkHarborNodeRole`. OpenCode's current Bedrock provider does not
consume EC2 instance metadata, so the launcher loads `bedrock_api_key` and
supplies it as `AWS_BEARER_TOKEN_BEDROCK`.

Claude Code receives raw Claude Bedrock model IDs. Its Sonnet, Opus, Haiku, and
subagent aliases are all pinned to that same ID so auxiliary calls cannot use a
different model. Claude Code cannot run non-Claude models. OpenCode receives
`amazon-bedrock/<model-id>` for native Bedrock models and `openai/<model-id>`
for GPT/GLM models served by Bedrock Mantle. GPT uses Mantle's `/openai/v1`
Responses route; GLM uses `/v1` with its transport-only `openai.` prefix
removed. OpenCode's small-model work is pinned to the benchmark model. Neither
run enables custom skills, MCP servers, persistent memory, or web-search
credentials.

Harbor receives the OpenCode credential as the environment reference
`${AWS_BEARER_TOKEN_BEDROCK}`. The secret value is resolved at agent
construction and is not written into the launcher argv or persisted job
configuration. TAU3 also uses the same secret for its simulated user and
assertion grader through Bedrock Mantle. Claude Code does not receive it.

Omp (Oh-My-Pi) is a fork of Mario Zechner's Pi with a bundled Bedrock
provider that does its own SigV4 signing and reads AWS credentials via the
standard chain (env → shared credentials → IMDS), so on the fleet the
instance profile is picked up transparently — no bearer token forwarding
needed for `amazon-bedrock/*` model IDs. Only Mantle GPT
(`openai.gpt-5.6-sol` and friends) needs the OpenAI-compat plumbing, at
which point the launcher fetches `bedrock_api_key` and exports it as
`OPENAI_API_KEY` alongside `OMP_OPENAI_BASE_URL`. `OMP_VERSION` pins the
CLI version (default `17.2.15`) and gets baked into the job name.

### Hard-won learnings

1. **Run orchestrator on big box** — 500 SSH sessions need ≥16 cores. Laptop stalls.
2. **Non-burstable instances** — t3 exhausts credits during setup. m7i.xlarge works.
3. **64GB root volume** — Docker layers + uv cache overflow 8GB default.
4. **Python 3.12 forced** — SWE-bench ships py3.9, strands needs ≥3.10.
5. **Instance profile > forwarded creds** — no token expiry, no secrets in argv.
6. **RunInstances rate limit** — bucket of 5, refill 2/sec. Pace launches at 1.5/sec with burst 3.
7. **Never delete results** — archive only. S3 versioning as backup.

### Full native-agent source suite

Run the full directly comparable Claude matrix with:

```bash
FULL_SUITE_DRY_RUN=1 \
  RUN_GROUP=strands-full-native-20260803 \
  bash strands-infra-runner/run-full-native-suite.sh

RUN_GROUP=strands-full-native-20260803 \
  setsid bash strands-infra-runner/run-full-native-suite.sh \
  </dev/null >/home/ubuntu/full-native-suite.log 2>&1 &
```

The default is Claude Code plus OpenCode, Opus 4.8 plus Sonnet 5 plus Sonnet
4.6, and pass@2 over full TB21, GAIA, TAU3, and SWE-bench Pro. That produces
24 cells and 16,320 trials. Sources run sequentially, while the three model
lanes start seven minutes apart. Claude Code and OpenCode remain sequential
inside each model lane, avoiding concurrent pressure on the same model endpoint.
The queue validates completed results before reuse, checkpoints each source,
paces each lane's initial EC2 launches at 1.5 requests/sec with burst 3, and
drains each cell fleet. It resolves the freshest `strands-working-fork` commit
once, verifies that exact SHA after every install, and records the short SHA in
every job identity. `RUN_GROUP` defaults to `strands-full-native-20260803` for
this campaign and appears in every suite state directory, matrix state
directory, Harbor job name, and S3 prefix.
