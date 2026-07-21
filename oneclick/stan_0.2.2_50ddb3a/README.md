# strands-agent 0.2.2_50ddb3a

Packages `strands_stan.harness_agent()` as a OneClick sibling-container image.
Stan keeps its default prompt, model behavior, thinking, and context management.
Its built-in tools are bound to the task container through `DockerSandbox`.

This release pins Stan commit
`50ddb3a2a22349f38b11f80cc25af3314c723261` (`50ddb3a`), the fetched
`origin/main` head when the release was created. Compared with the prior
wrapper, this rewritten Stan head uses full Bedrock model IDs directly and
falls back to the main model when it cannot identify a Bedrock family for
`web_fetch`. `RELEASE_ID` and `STAN_COMMIT` are the tracked release metadata.

See [the OneClick release instructions](../README.md) to create another
version.

## Build and publish

From the repository root:

```bash
./oneclick/stan_0.2.2_50ddb3a/sync-source.sh
./oneclick/stan_0.2.2_50ddb3a/build-and-push.sh
./oneclick/stan_0.2.2_50ddb3a/build-and-push.sh publish gamma
```

Publishing registers:

```text
agentId:        strands-agent
agentVersionId: v0.2.2_50ddb3a
display name:   Strands Stan 0.2.2_50ddb3a
ECR tag:        strands-agent-v0.2.2-50ddb3a
Stan commit:    50ddb3a2a22349f38b11f80cc25af3314c723261
```

The ECR tag uses `-50ddb3a` because Docker does not permit `@` or `_` in
certain tag positions; using a hyphen keeps it universally safe.

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
