# Strands Benchmarks

Run any Strands agent against [Harbor](https://hub.harborframework.com) (Terminal-Bench) benchmarks.

## Quick Start

```bash
pip install 'strands-agents-evals[harbor]'
strands-evals benchmark-setup        # verify prerequisites (harbor, docker, auth)
strands-evals benchmark ./my_agent/ --dataset NovitaAI/tb21-file-recovery
```

## Write Your Agent

Create a directory with a Python file that defines a class with `create_agent()`:

```python
# my_agent/agent.py
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.models.model import CacheConfig
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.vended_tools.bash import make_bash
from strands.vended_tools.file_editor import make_file_editor

class MyAgent:
    def create_agent(self):
        sandbox = NotASandboxLocalEnvironment()
        return Agent(
            model=BedrockModel(model_id="us.anthropic.claude-sonnet-4-6", cache_config=CacheConfig()),
            tools=[make_bash(sandbox=sandbox), make_file_editor(sandbox=sandbox)],
            context_manager="auto",
            callback_handler=None,
        )
```

### Contract

Your class can define up to 3 methods (only `create_agent` is required):

| Method | Where it runs | Purpose |
|---|---|---|
| `create_agent(self)` | Container | Return a fresh `strands.Agent` for each trial |
| `invoke_agent(self, agent, instruction, **kwargs)` | Container | Custom invocation logic (default: `agent(instruction)`) |
| `on_benchmark_complete(self, job_dir, results)` | Host | Post-run callback (upload results, notify, etc.) |

### TypeScript

Same contract, JS exports:

```javascript
// my_agent/agent.js
export async function createAgent() { return new Agent({...}) }
export async function invokeAgent(agent, instruction, options) { ... }  // optional
```

Run with `--runtime typescript`:
```bash
strands-evals benchmark --runtime typescript ./my_ts_agent/ --dataset ...
```

## CLI Reference

```bash
strands-evals benchmark AGENT_DIR [options] [harbor flags...]
```

| Flag | Description |
|---|---|
| `--runtime python\|typescript` | Agent runtime (default: python) |
| `--name NAME` | Name for the job directory |
| `-o DIR` | Output directory (default: ./jobs) |
| `--deps DEPS` | Extra pip deps (auto-detected from requirements.txt) |
| `--unpublished-strands-ref URL` | Install strands from a git branch instead of PyPI |
| `--post-run CMD` | Shell command after run (`{job_dir}` is replaced) |

All other flags pass through to `harbor run` (e.g. `-d`, `-n`, `--n-tasks`, `--max-retries`).

## What Gets Produced

After a run, `./jobs/<name>/` contains per-trial:

```
<trial>/
  agent/
    result.json          # tokens, cycles, model_id, tool_stats, stop_reason
    conversation.json    # full message history
    conversation.jsonl   # incremental (survives timeouts)
    trajectory.json      # ATIF v1.7 (for harbor analyze)
    command-0/
      stdout.txt         # agent stdout (stock Harbor viewer layout)
    source/              # copy of your agent code
  verifier/
    reward.txt           # 0.0 or 1.0
    test-stdout.txt      # verifier output
```

### result.json

```json
{
  "input_tokens": 180220,
  "output_tokens": 8822,
  "cache_tokens": 27306,
  "stop_reason": "end_turn",
  "cycle_count": 13,
  "model_id": "us.anthropic.claude-sonnet-4-6",
  "strands_version": "1.46.0",
  "tool_stats": {
    "bash": {"call_count": 12, "success_count": 11, "error_count": 1, "total_time": 4.52},
    "file_editor": {"call_count": 3, "success_count": 3, "error_count": 0, "total_time": 0.01}
  }
}
```

## Testing Unpublished SDK Versions

```bash
strands-evals benchmark ./my_agent/ \
  --unpublished-strands-ref https://github.com/your-fork/sdk-python@your-branch \
  --dataset ...
```

The container installs from that git ref instead of PyPI.

## Examples

```
examples/
  benchmark_agent/        # Python example (ready to run)
  benchmark_agent_ts/     # TypeScript example
```

## Prerequisites

- Docker (or Docker Desktop)
- Harbor CLI (`pip install harbor`) + `harbor auth login`
- AWS credentials (for Bedrock) — resolved automatically from your session
