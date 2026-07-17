# strands-agent

Packages `strands_stan.harness_agent()` as a OneClick sibling-container image.
Stan keeps its default prompt, model behavior, thinking, and context management.
Its built-in tools are replaced with equivalent DockerSandbox-bound tools so
commands and edits execute in the task container.

The Stan checkout is ignored at `stan/`. Synchronize its Python source into this
package before a direct Docker build; `build-and-push.sh` does this automatically.

```bash
./oneclick/stan_0.2.0/sync-source.sh
./oneclick/stan_0.2.0/build-and-push.sh v0.2.0
./oneclick/stan_0.2.0/build-and-push.sh publish gamma v0.2.0
```

`STAN_PY_SOURCE` can override the default source location
`stan/stan-py/src/strands_stan`.

Publishing `v0.2.0` registers `agentId=strands-agent` and
`agentVersionId=v0.2.0`, backed by ECR tag `strands-agent-v0.2.0`.

## OneClick registration

```json
"strands-agent": {
  "default": {
    "harbor_command": [
      "python3", "-m", "strand_agent.run_infer",
      "--dataset-container-id", "{task_container_id}",
      "--problem-statement", "{instruction}",
      "--output-dir", "{output_dir}",
      "--container-workspace-path", "{workdir}"
    ],
    "harbor_working_dir": "/app",
    "harbor_required_params": ["model_name"],
    "artifact_paths": [],
    "max_iterations": "100"
  }
}
```

OneClick model aliases are reduced to their Bedrock model IDs before being
passed to Stan. If no model is supplied, Stan's default model is used.
