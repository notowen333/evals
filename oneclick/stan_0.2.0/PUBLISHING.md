# Publishing `strands-agent`

## Agent identity

The OneClick agent ID must be exactly:

```text
strands-agent
```

Do not register or invoke it as `strands-stan` or `strand-agent`. The directory
name (`stan_0.2.0`) and Python package name (`strands_stan`) are
implementation names; they do not determine the OneClick agent ID.

The task executor looks up `harbor_command` in Agents AppConfig using the agent
ID. A mismatched ID causes an error such as:

```text
Missing 'harbor_command' in agent config
```

The ID must match in all three places:

1. The evaluation request: `agentId=strands-agent`
2. The Agents table record written by `build-and-push.sh`
3. The Agents AppConfig key containing `harbor_command`

Publishing registers the Agents table record, but it does not modify AppConfig.
Confirm the `strands-agent` AppConfig entry exists before starting an evaluation.

## Private Stan source

Never commit or Git-push the Stan source code.

- The private checkout at `/stan/` and every synchronized `strands_stan/`
  directory are ignored by the repository's root `.gitignore`.
- `sync-source.sh` copies the required package from
  `stan/stan-py/src/strands_stan` into this directory before each build.
- Do not use `git add -f` on either the checkout or synchronized source.
- Do not remove or narrow the `strands_stan/` ignore rule.

Verify the synchronized source is ignored before committing:

```bash
git check-ignore -v \
  oneclick/stan_0.2.0/strands_stan/agent.py
```

The source must be included in the private runtime image so the agent can run.
"Never push the source" means never commit or push it to Git; publishing the
runtime image to the approved private `agents-{stage}` ECR repository is
expected.

## Build and publish

Use a new `vX.Y.Z` version for each release. From the repository root:

```bash
# Build locally without publishing
./oneclick/stan_0.2.0/build-and-push.sh v0.2.0

# Build, push to gamma, and register the agent version
./oneclick/stan_0.2.0/build-and-push.sh publish gamma v0.2.0
```

For `v0.2.0`, the expected identifiers are:

```text
agentId:        strands-agent
agentVersionId: v0.2.0
ECR tag:        strands-agent-v0.2.0
```

The publish script synchronizes the private source, builds the `linux/amd64`
image, pushes it to the selected stage's private ECR repository, writes the
Agents table record, and reads that record back for verification. Treat the
publish as successful only when the final record reports the exact agent ID,
version, image URI, and `active` status.
