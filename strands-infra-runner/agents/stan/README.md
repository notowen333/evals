# Stan

Stan `harness_agent()` with all built-in tools (bash, read, write, edit,
web_fetch, web_search) and plugins (todos). Uses `context_manager="auto"`.
The wrapper also tells the agent the Harbor task root so file operations stay
inside the task workspace. Harbor-provided MCP servers are registered as
callable Strands tools, including TAU3's direct tool server.

`run-benchmark.sh` installs Stan from the ref in `STAN_BRANCH` (default:
`main`) and bundles that source into this directory for Harbor to upload to
fleet nodes.

## Usage

```bash
# On the EC2 orchestrator:
run-benchmark.sh stan kimi-k2.5 swe-bench/swe-bench-verified

# Pin a Stan branch, tag, or commit:
STAN_BRANCH=<ref> run-benchmark.sh stan kimi-k2.5 swe-bench/swe-bench-verified
```
