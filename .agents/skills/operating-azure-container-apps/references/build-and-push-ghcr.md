# Build and push the ACA source image to GHCR

Use this when the selected ELSPETH commit lacks a GHCR image. This publishes
the shared ELSPETH image used by Azure Container Apps; ACA normally consumes
a digest-preserving copy in ACR. This procedure stops at GHCR. For the ACR
copy, doctor Job and revision rollout, continue with
[the ACA command sheet](command-cheatsheet.md).

## Pin the source

Run from the selected checkout. An uncommitted file must not enter a release
image. A branch name is mutable, so record the full commit before building.

```bash
cd "$(git rev-parse --show-toplevel)"
set -euo pipefail
SOURCE_SHA=$(git rev-parse HEAD)
git status --short --branch
git rev-list --left-right --count "origin/release/0.8.1...$SOURCE_SHA"
IMAGE="ghcr.io/dta-au/elspeth:sha-${SOURCE_SHA}"
BUILD_DIR=$(mktemp -d /tmp/elspeth-ghcr-source-XXXXXX)
git archive --format=tar "$SOURCE_SHA" | tar -xf - -C "$BUILD_DIR"
test "$(git show "$SOURCE_SHA:Dockerfile" | sha256sum | cut -d' ' -f1)" = \
  "$(sha256sum "$BUILD_DIR/Dockerfile" | cut -d' ' -f1)"
```

`git archive` includes the tracked contents of that commit and excludes dirty
or untracked work. Do not build from the live checkout when its status is
dirty. If the commit exists only locally, state that plainly with the image
handoff: the OCI revision label identifies the source, but a remote reader
cannot fetch that commit until the branch is published.

## Check Docker and authenticate

Docker daemon access may need the agent's approved elevated tool execution.
Check which builder platforms are actually available. Azure Container Apps
uses the `linux/amd64` image here; the repository CI builds both amd64 and
arm64. Do not call a single-platform image multi-architecture. If both are
required, use a builder with working arm64 emulation or the CI workflow.

```bash
umask 077
DOCKER_CONFIG=$(mktemp -d /tmp/elspeth-ghcr-auth-XXXXXX)
export DOCKER_CONFIG
docker version
BUILDER="elspeth-ghcr-${SOURCE_SHA:0:12}-$$"
docker buildx create --name "$BUILDER" --driver docker-container
docker buildx inspect "$BUILDER" --bootstrap
```

Set `DOCKER_CONFIG` before creating the builder and keep it unchanged through
cleanup: Buildx stores named builder definitions under that directory.

Check `gh auth status` in a context with network access. The active GitHub
account can be different from the account with `write:packages`; select the
intended account explicitly. Keep registry credentials in a private temporary
Docker config rather than the default `~/.docker/config.json`. Never print a
token or put it in a command argument.

```bash
gh auth status
gh auth token --hostname github.com --user johnm-dta |
  docker login ghcr.io --username johnm-dta --password-stdin
```

If `gh auth status` cannot validate a package-write account, stop before
building for publication. Do not assume that `repo` scope grants package
write access. A sandboxed authentication check can fail when its network is
restricted; repeat it through the approved network path before diagnosing
the credential itself.

## Build and push

Use the repo's official generic image setting, `INSTALL_EXTRAS=all`, and the
full commit SHA tag. The Dockerfile builds the SPA inside the image and pins
its base images. The source archive makes the context independent of ignored
worktrees and local generated files.

```bash
BUILD_LOG=$(mktemp /tmp/elspeth-ghcr-build-XXXXXX.log)
BUILD_META=$(mktemp /tmp/elspeth-ghcr-meta-XXXXXX.json)
if docker buildx build \
    --builder "$BUILDER" \
    --platform linux/amd64 \
    --build-arg INSTALL_EXTRAS=all \
    --label "org.opencontainers.image.revision=$SOURCE_SHA" \
    --provenance=true --sbom=true \
    --metadata-file "$BUILD_META" \
    --tag "$IMAGE" --push "$BUILD_DIR" >"$BUILD_LOG" 2>&1; then
  echo 'build_exit=0'
else
  result=$?
  echo "build_exit=$result log=$BUILD_LOG"
  tail -n 50 "$BUILD_LOG"
  exit "$result"
fi
BUILT_DIGEST=$(jq -r '."containerimage.digest"' "$BUILD_META")
test "$BUILT_DIGEST" != null && test -n "$BUILT_DIGEST"
printf 'source=%s\nimage=%s\nbuilt_digest=%s\n' \
  "$SOURCE_SHA" "$IMAGE" "$BUILT_DIGEST"
```

The CLI exiting zero is the build and push result. Read the saved log if it
fails; a `tail` of an in-progress log is not a success signal. Provenance and
SBOM are build attestations. A local push does not receive the CI workflow's
GitHub OIDC signature, and it is not a signed release promotion.

## Verify the remote artifact

```bash
REMOTE_DIGEST=$(docker buildx imagetools inspect "$IMAGE" \
  --format '{{.Manifest.Digest}}')
test "$REMOTE_DIGEST" = "$BUILT_DIGEST"
docker buildx imagetools inspect "$IMAGE" --raw |
  jq -r '.manifests[] | [.platform.os, .platform.architecture,
    (.annotations["vnd.docker.reference.type"] // "image")] | @tsv'
docker buildx imagetools inspect "$IMAGE" --format '{{json .Image}}' |
  jq -e --arg sha "$SOURCE_SHA" \
    '.architecture == "amd64" and .os == "linux" and
     .config.Labels["io.elspeth.install-extras"] == "all" and
     .config.Labels["org.opencontainers.image.revision"] == $sha'
docker run --rm --pull=always "ghcr.io/dta-au/elspeth@$REMOTE_DIGEST" --version
docker run --rm "ghcr.io/dta-au/elspeth@$REMOTE_DIGEST" health --json
```

Read the health JSON, including its skipped dependency checks. With a clean
archive build, `health --json` can report `commit: unavailable` because the
container has no `.git`; use the verified OCI revision label and pinned
source archive for commit identity. CLI health does not prove ACA readiness,
database compatibility or live deployment. Hand off the `@sha256:` reference
for the digest-preserving ACR copy; do not deploy from the mutable tag.

After verification, remove the temporary credential and builder if they are
no longer needed:

```bash
docker logout ghcr.io
docker buildx rm "$BUILDER"
rm -rf -- "$DOCKER_CONFIG" "$BUILD_DIR"
```
