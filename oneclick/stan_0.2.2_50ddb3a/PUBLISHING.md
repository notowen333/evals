# Publishing `strands-agent` 0.2.2_50ddb3a

Use the [OneClick release instructions](../README.md) to create later
versions. This file records the identity and checks for this immutable release.

## Release identity

The identifiers are:

```text
directory:      stan_0.2.2_50ddb3a
agentId:        strands-agent
agentVersionId: v0.2.2_50ddb3a
display name:   Strands Stan 0.2.2_50ddb3a
ECR tag:        strands-agent-v0.2.2-50ddb3a
Stan commit:    50ddb3a2a22349f38b11f80cc25af3314c723261
```

The task executor looks up `harbor_command` in Agents AppConfig by
`agentId`. Keep that stable as `strands-agent`; do not use the directory name,
`strands-stan`, or `strand-agent` as the ID. Publishing writes the Agents table
record but does not modify AppConfig.

## Source check

`RELEASE_ID` records the runtime and OneClick label. `sync-source.sh`
reconstructs `strands_stan/` from the exact commit in `STAN_COMMIT`,
independently of the Stan checkout's current branch. The source directory is
ignored by Git but included in the private runtime image.

```bash
test "$(cat oneclick/stan_0.2.2_50ddb3a/RELEASE_ID)" = \
  "v0.2.2_50ddb3a"
./oneclick/stan_0.2.2_50ddb3a/sync-source.sh
test "$(cat oneclick/stan_0.2.2_50ddb3a/strands_stan/.stan-commit)" = \
  "50ddb3a2a22349f38b11f80cc25af3314c723261"
git check-ignore -v \
  oneclick/stan_0.2.2_50ddb3a/strands_stan/agent.py
```

Never use `git add -f` on `/stan/` or `strands_stan/`.

## Build and publish

```bash
# Build locally without publishing
./oneclick/stan_0.2.2_50ddb3a/build-and-push.sh

# Build, push to gamma, and register the agent version
./oneclick/stan_0.2.2_50ddb3a/build-and-push.sh publish gamma
```

The script derives the release labels from the directory and verifies its
`_50ddb3a` suffix against `STAN_COMMIT`. Treat publishing as successful only
when the final Agents table read-back reports `strands-agent`,
`v0.2.2_50ddb3a`, the expected image URI, and `active` status.
