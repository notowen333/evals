# Stan 0.2.0

Stan `harness_agent()` with all built-in tools (bash, read, write, edit,
web_fetch, web_search) and plugins (todos). Uses `context_manager="auto"`.

Source synced from `stan/stan-py/src/strands_stan/` at commit `3f5a429`
(feat: add web_search built-in).

## Usage

```bash
# On the EC2 orchestrator:
run-benchmark.sh stan_0.2.0 sonnet-4.6 swe-bench/swe-bench-verified

# Locally:
./examples/stan_0.2.0/sync-source.sh   # refresh from stan/ checkout
```

## Creating a new version

```bash
cp -r examples/stan_0.2.0 examples/stan_0.3.0
cd examples/stan_0.3.0
# Update stan/ checkout to desired commit, then:
./sync-source.sh
# Edit agent.py if factory options change
```
