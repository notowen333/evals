# Creating a OneClick Stan version

Each OneClick Stan release is an immutable wrapper around one exact Stan
commit. Create a new directory for every release; never update an existing
release to point at different Stan source.

## Naming

Use an underscore `_` to separate the version from the seven-character Stan
commit SHA. The `@` character is **not supported** by the OneClick platform and
must not be used in directory names, version IDs, or display names.

| Surface | Format | Example |
| --- | --- | --- |
| Directory | `stan_<version>_<sha>` | `stan_0.2.2_50ddb3a` |
| OneClick version ID | `v<version>_<sha>` | `v0.2.2_50ddb3a` |
| ATIF agent version | `v<version>_<sha>` | `v0.2.2_50ddb3a` |
| Display name | `Strands Stan <version>_<sha>` | `Strands Stan 0.2.2_50ddb3a` |

Keep the OneClick agent ID as `strands-agent`.

Docker image tags use a hyphen `-` at the version/SHA boundary (e.g.
`strands-agent-v0.2.2-50ddb3a`) because Docker restricts certain characters in
tags.

## Create the release

Run these steps from the repository root.

1. Fetch Stan and select the fetched `origin/main` commit. Do not use a
   possibly stale local `HEAD`.

   ```bash
   git -C stan fetch origin main --tags --prune
   STAN_COMMIT="$(git -C stan rev-parse 'origin/main^{commit}')"
   STAN_SHORT="$(git -C stan rev-parse --short=7 "$STAN_COMMIT")"
   git -C stan show --no-patch --format='%H %s' "$STAN_COMMIT"
   ```

2. Choose the next wrapper version, then copy the previous release before
   changing it.

   ```bash
   PREVIOUS="oneclick/stan_0.2.2_50ddb3a"
   NEXT_VERSION="0.2.3"
   NEXT="oneclick/stan_${NEXT_VERSION}_${STAN_SHORT}"
   cp -R "$PREVIOUS" "$NEXT"
   ```

3. Pin the release ID and full Stan commit.

   ```bash
   printf 'v%s_%s\n' "$NEXT_VERSION" "$STAN_SHORT" > "$NEXT/RELEASE_ID"
   printf '%s\n' "$STAN_COMMIT" > "$NEXT/STAN_COMMIT"
   ```

   The runtime reads `RELEASE_ID`, while the sync script reads `STAN_COMMIT`.
   The build validates both files against the directory name. Update
   `README.md` and `PUBLISHING.md` with the new version, short SHA, full SHA,
   and relevant Stan changes. Do not edit `AGENT_ID`; it remains
   `strands-agent`.

4. Synchronize the pinned source. The script archives `STAN_COMMIT` directly
   from the local Stan Git object database, so the Stan checkout does not need
   to be switched or reset.

   ```bash
   "$NEXT/sync-source.sh"
   ```

5. Check the release before building.

   ```bash
   test "$(cat "$NEXT/RELEASE_ID")" = "v${NEXT_VERSION}_${STAN_SHORT}"
   test "$(cat "$NEXT/strands_stan/.stan-commit")" = "$STAN_COMMIT"
   git check-ignore -v "$NEXT/strands_stan/agent.py"
   bash -n "$NEXT/build-and-push.sh" "$NEXT/sync-source.sh"
   python3 -m compileall -q "$NEXT"
   hatch run hatch-static-analysis:ruff check "$NEXT"/*.py
   hatch run hatch-static-analysis:ruff format --check "$NEXT"/*.py
   rg -n '0\.2\.2|50ddb3a' "$NEXT" --glob '!strands_stan/**'
   ```

   The final `rg` command uses the previous release values from this example.
   It must return no stale references except intentional changelog text.

6. Build locally. The script derives the version from the directory name and
   rejects a directory suffix that does not match `STAN_COMMIT`.

   ```bash
   "$NEXT/build-and-push.sh"
   ```

7. Publish only after the local build succeeds.

   ```bash
   "$NEXT/build-and-push.sh" publish gamma
   ```

   Publishing pushes the private runtime image and writes the Agents table
   record. Treat it as successful only when the final read-back shows the
   expected agent ID, SHA-suffixed version, image URI, and `active` status.

## Source handling

The root `.gitignore` excludes both `/stan/` and every `strands_stan/`
directory. The synchronized source must be present in the private runtime
image, but it must not be committed:

```bash
git status --short
git check-ignore -v oneclick/stan_0.2.2_50ddb3a/strands_stan/agent.py
```

Never use `git add -f` for the Stan checkout or a synchronized
`strands_stan/` directory. Commit the release wrapper, `RELEASE_ID`, and
`STAN_COMMIT` pins, not the synchronized source.
