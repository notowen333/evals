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
  --parameters '{"commands":["#!/bin/bash","export HOME=/root","git config --global --add safe.directory /home/ubuntu/evals","cd /home/ubuntu/evals","setsid bash strands-infra-runner/run-benchmark.sh <agent> <model> <dataset> <concurrency> </dev/null >/home/ubuntu/<name>.log 2>&1 &","sleep 2","pgrep -af run-benchmark && echo LAUNCHED"]}' \
  --timeout-seconds 120 \
  --region us-east-1
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
| `agent` | `claude-code`, `opencode`, or any dir under `strands-infra-runner/agents/` | required |
| `model` | `sonnet-4.6`, `opus-4.6`, `opus-4.8`, `sonnet-5`, `kimi-k2.5`, or a raw model ID (e.g. `openai.gpt-5.6-sol` for GPT via Bedrock Mantle) | required |
| `dataset` | Any Harbor dataset (e.g. `swe-bench/swe-bench-verified`, `terminal-bench/terminal-bench-2-1`, `gaia/gaia`) | required |
| `concurrency` | Number of parallel EC2 instances | Dataset task count, capped at 2,000; 500 for unknown datasets |

## Examples

```bash
# SWE-bench Verified with Stan on Sonnet 4.6
run-benchmark.sh stan sonnet-4.6 swe-bench/swe-bench-verified

# Terminal-Bench 2.1 with Stan on Opus
run-benchmark.sh stan opus-4.8 terminal-bench/terminal-bench-2-1 89

# GAIA with Stan on Sonnet 4.6
run-benchmark.sh stan sonnet-4.6 gaia/gaia

# Terminal-Bench 2.1 with Stan on Kimi K2.5
run-benchmark.sh stan kimi-k2.5 terminal-bench/terminal-bench-2-1 89

# One-task TAU3 smoke test; the simulated user and grader use Bedrock Mantle
run-benchmark.sh stan sonnet-4.6 sierra-research/tau3-bench 1

# Native product comparisons on the same Bedrock model
run-benchmark.sh claude-code sonnet-4.6 swe-bench/swe-bench-verified
run-benchmark.sh opencode sonnet-4.6 swe-bench/swe-bench-verified
```

## Job naming and results

Job names are auto-generated as `<agent>@<commit-sha>--<model>--<dataset-slug>`.
This produces
a flat directory under `jobs/` that the Harbor viewer can scan directly:

```
jobs/
├── stan@2c58790--sonnet-4.6--terminal-bench-terminal-bench-2-1/
│   ├── config.json       ← Harbor job config (agent, env, dataset)
│   ├── result.json       ← Aggregate metrics (started_at, stats, evals)
│   ├── lock.json
│   ├── job.log
│   └── <trial-id>/      ← One per task (contains agent logs, trajectory, etc.)
├── stan@2c58790--opus-4.8--stanford-medagentbench/
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
/home/ubuntu/evals/jobs/<agent>@<commit-sha>--<model>--<dataset-slug>/
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

The Stan agent is installed directly from the private repo via a GitHub
PAT stored in Secrets Manager (`stan_pat-lUflBx`). No manual sync or scp needed.

- `STAN_BRANCH` env var controls which ref to install (default: `main`). It
  accepts a branch, a tag, or a full/short commit SHA.
- The PAT is fetched at run time by `run-benchmark.sh`.
- `strands-agents>=1.45.0` is passed as `AGENT_DEPS` so the fleet containers
  install the correct SDK version.

To cut a new Stan version for benchmarking, just tag/branch in the Stan repo and
pass `STAN_BRANCH=<ref>` when launching the run.

`VERSION_TAG` (the `@<sha>` in the job name) is derived from `STAN_BRANCH`. When
it's a branch/tag, the SHA comes from `git ls-remote`; when it's already a SHA,
it's used directly — `ls-remote` matches refs only and returns nothing for a raw
commit, so it cannot be used to resolve or validate one.

## Native Claude Code and OpenCode setup

The `claude-code` and `opencode` names select Harbor's built-in installed-agent
adapters. No local wrapper directory is required.

| Agent | Default pinned version | Harbor model value |
|-------|------------------------|--------------------|
| `claude-code` | `2.1.220` | Raw Bedrock model ID |
| `opencode` | `1.18.9` | `amazon-bedrock/<model-id>` or `openai/<model-id>` |

Override the pins with `CLAUDE_CODE_VERSION` or `OPENCODE_VERSION`. Claude Code
uses the fleet instance profile; its main, fast, and subagent model aliases are
pinned to the selected benchmark model. Claude Code only supports Claude
models. OpenCode uses Bedrock directly for Claude/Kimi and Bedrock Mantle for
GPT/GLM. GPT uses Mantle's `/openai/v1` Responses route; GLM uses the `/v1`
OpenAI-compatible route with the transport-only `openai.` prefix removed from
its model ID. The launcher loads `bedrock_api_key` for both paths and pins
OpenCode's small-model work to the selected benchmark model. Harbor stores
environment references rather than secret values in job configuration.

No custom skills, MCP servers, memory, or web-search credentials are enabled.
For TAU3 runs, the same `bedrock_api_key` secret also configures the simulated
user and assertion grader through Bedrock Mantle. Claude Code never receives
the key.

## Matrix runs (pass@k across models)

`run-matrix.sh` launches the model matrix sequentially and unattended. Models run
one at a time, so peak fleet size is one cell regardless of `k` or model count;
per-cell concurrency is additionally capped per model to limit Bedrock throttling.

```bash
setsid bash strands-infra-runner/run-matrix.sh -k 2 \
  </dev/null >/home/ubuntu/matrix-k2.log 2>&1 &
```

| Flag | Purpose | Default |
|------|---------|---------|
| `-d` | Dataset | `strands-harness-benchmark-index` |
| `-k` | Attempts per task (pass@k) | `2` |
| `-m` | Comma-separated models | Five-model Stan baseline; full six-model native set |
| `-n` | Force per-cell concurrency | per-model cap |
| `-a` | Comma-separated agents | `stan` |
| `-s` | Stan ref to pin: branch, tag, or SHA | `STAN_BRANCH`, else `main` HEAD |

For the full supported native-product matrix:

```bash
setsid bash strands-infra-runner/run-matrix.sh \
  -a claude-code,opencode -k 2 \
  </dev/null >/home/ubuntu/native-agents-k2.log 2>&1 &
```

For the directly comparable full-source suite, use the dedicated driver:

```bash
FULL_SUITE_DRY_RUN=1 \
  bash strands-infra-runner/run-full-native-suite.sh

setsid bash strands-infra-runner/run-full-native-suite.sh \
  </dev/null >/home/ubuntu/full-native-suite.log 2>&1 &
```

The default queue runs Claude Code and OpenCode on Opus 4.8, Sonnet 5, and
Sonnet 4.6 across full TB21 (89), GAIA (165), TAU3 (375), and SWE-bench Pro
(731). At pass@2 this is 24 sequential cells and 16,320 trials. Cells alternate
Claude Code/OpenCode, each source writes a checkpoint, and exact completed
results are reused on restart. The driver resolves the latest
`strands-working-fork` commit once, installs and verifies that exact SHA for
every cell, and includes it in job identities.

Each agent/model pair has its own versioned state key and log. Re-running the
command validates completed job data and skips only that exact completed cell;
one agent cannot suppress another. Incompatible cells are reported and omitted:
Claude Code runs Sonnet/Opus, while OpenCode runs all six configured models.
A cell is successful only when it has every expected task/attempt and at least
one clean agent trial; all-error provider failures are retained as failed cells.

The Stan SHA is resolved **once** and handed to every cell, so a push to Stan
mid-matrix can't give later models a different agent build than earlier ones. A
pinned SHA is validated against the GitHub API during preflight.

State lives under `/home/ubuntu/matrix-runs/`, keyed by dataset, agent versions,
Harbor commit, and `k` (`matrix.log`, `status.tsv`, per-cell logs, `COMPLETE`).
Job dirs and S3 prefixes include `--harbor<SHA>--k<N>` so a new Harbor build or
pass@k value never reuses an older result.

## TAU3 simulated user setup

For datasets whose name starts with `sierra-research/tau3-bench`,
`run-benchmark.sh` fetches the Bedrock API key from the Secrets Manager secret
`bedrock_api_key` before launching any fleet instances. The secret can be either:

- A raw API key in `SecretString`.
- A JSON object containing `bedrock_api_key`, `OPENAI_API_KEY`, or `api_key`.

The launcher exports the values expected by the published TAU3 Harbor package:

```text
OPENAI_API_KEY=<value from Secrets Manager>
OPENAI_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/v1
TAU2_USER_MODEL=openai/openai.gpt-oss-120b
TAU2_NL_ASSERTIONS_MODEL=openai/openai.gpt-oss-120b
```

The first `openai/` in each model value selects LiteLLM's OpenAI-compatible
transport; `openai.gpt-oss-120b` is the model ID sent to Mantle. Harbor resolves
these task variables on the orchestrator and injects them into the remote TAU3
runtime and verifier containers. They must not be passed with `--ae`, which only
configures the evaluated agent.

Optional overrides:

| Variable | Purpose | Default |
|----------|---------|---------|
| `BEDROCK_API_KEY_SECRET_ID` | Secrets Manager name or ARN used by OpenCode and TAU3 | `bedrock_api_key` |
| `TAU3_MANTLE_REGION` | Mantle and secret region | `us-east-1` |
| `TAU3_USER_MODEL` | LiteLLM-prefixed simulated-user model | `openai/openai.gpt-oss-120b` |
| `TAU3_NL_ASSERTIONS_MODEL` | LiteLLM-prefixed assertion-grader model | Same as `TAU3_USER_MODEL` |

The orchestrator instance role needs `secretsmanager:GetSecretValue` for
`bedrock_api_key`. The launcher exits before provisioning fleet instances when
the secret is missing, empty, malformed, or inaccessible.

## Directory layout on the orchestrator

```
/home/ubuntu/evals/
├── jobs/                ← active benchmark results (viewer points here)
│   ├── stan@2c58790--opus-4.8--stanford-medagentbench/
│   └── ...
├── jobs/legacy/         ← old/pre-refactor results (not shown in viewer)
└── strands-infra-runner/agents/stan/ ← agent wrapper and bundled Stan source
```

Old results were moved to `jobs/legacy/` so the viewer only shows current runs.
If you need to re-flatten a double-nested job: `mv jobs/X/X/* jobs/X/ && rmdir jobs/X/X`.

## Prerequisites

Before first use, ensure the orchestrator has:
- [x] Latest code: `git pull origin harbor-adapter`
- [x] venv installed: `source .venv/bin/activate && pip install -e .[harbor]`
- [x] SSH key at `/root/.ssh/harbor-benchmark.pem`
- [x] Secrets Manager access for `stan_pat-lUflBx` (via instance role)
- [x] Secrets Manager access for `bedrock_api_key` (required for TAU3)
- [x] bun installed (`/root/.bun/bin/bun`) for the Harbor viewer dev mode
- [x] Harbor viewer running: `harbor view jobs/ --port 7842 --host 0.0.0.0 --jobs`
- [x] Docker network pool expanded (`/etc/docker/daemon.json` — only for local Docker runs)

## Known Limitations

- **`allow_internet=false` benchmarks** (e.g. DeepSWE) do not work on the EC2 fleet.
  The EC2 environment applies `network_mode: none` statically — no dynamic switching.
  These require Docker local or Modal environments.
- **Orchestrator must stay alive** for the full run. Use `setsid` + SSM. If it dies,
  the cleanup handler terminates orphan instances but S3 upload won't fire.
- **EC2 RunInstances rate limit** (bucket 5, refill 2/sec) causes some launch failures
  at 500 concurrency. EC2 launches are paced at 1.5 requests/sec with burst 3;
  cells drain fully and cool down for 60 seconds before the queue advances.
- **Kimi K2.5 has no native web-search integration** through the Bedrock provider.
  Stan continues without `web_search`; `web_fetch` uses Kimi itself for summarization.
