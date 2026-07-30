# Docker Deployment Guide

This guide covers running ELSPETH in Docker containers for development and production deployments.

## Table of Contents

- [Quick Start](#quick-start)
- [Standalone Web Server](#standalone-web-server)
- [Volume Mounts](#volume-mounts)
- [Environment Variables](#environment-variables)
- [Common Commands](#common-commands)
- [Using docker-compose](#using-docker-compose)
- [Health Checks](#health-checks)
- [Image Tags](#image-tags)
- [Container Registries](#container-registries)
- [Pipeline Configuration](#pipeline-configuration)
- [Building Locally](#building-locally)
- [Runtime Image Contract](#runtime-image-contract)
- [Troubleshooting](#troubleshooting)

---

## Quick Start

ELSPETH containers follow a **CLI-first design** - arguments are passed directly to the `elspeth` CLI:

```bash
: "${IMAGE_TAG:?export an exact published sha-* or v* image tag}"

# Show help
docker run ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} --help

# Check version
docker run ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} --version

# List available plugins
docker run ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} plugins list
```

Confirm the tag exists before use:

```bash
docker buildx imagetools inspect \
  "ghcr.io/johnm-dta/elspeth:${IMAGE_TAG}" >/dev/null
```

Do not infer an image tag from the Python package version. A source release and
a registry publication are separate events.

---

## Standalone Web Server

The published image runs as UID/GID 1654. A bind mount hides the directories
created in the image, so prepare the web data roots on the host and make them
writable by that identity:

```bash
mkdir -p ./data/blobs ./data/outputs
sudo chown -R 1654:1654 ./data
sudo chmod 0700 ./data ./data/blobs ./data/outputs
```

Generate fresh signing keys in the shell, then pass all required web settings
into the container. The values below for the four Composer limits match the
project's browser-test configuration:

```bash
export ELSPETH_WEB_SECRET_KEY="$(openssl rand -hex 32)"
export ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY="$(openssl rand -base64 32)"

docker run --rm --name elspeth-web \
  -p 8451:8451 \
  -e ELSPETH_WEB__DATA_DIR=/app/data \
  -e ELSPETH_WEB__SECRET_KEY="${ELSPETH_WEB_SECRET_KEY}" \
  -e ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY="${ELSPETH_WEB_SHAREABLE_LINK_SIGNING_KEY}" \
  -e ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=15 \
  -e ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10 \
  -e ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=180.0 \
  -e ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=60 \
  -v "$(pwd)/data:/app/data" \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  web --host 0.0.0.0 --port 8451
```

The explicit `0.0.0.0` bind is required for Docker's published port to reach
the process; the native CLI keeps its safer `127.0.0.1` default. From another
terminal, check readiness with:

```bash
curl -fsS http://127.0.0.1:8451/api/ready
```

For persistent deployments, store both generated keys in a secret manager and
reuse them across restarts. Rotating `ELSPETH_WEB__SECRET_KEY` invalidates
sessions; rotating `ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY` invalidates
existing shareable links. Never bake either key into an image or commit it.

---

## Volume Mounts

Mount your configuration and data directories to standard container paths:

| Host Path | Container Path | Mode | Purpose |
|-----------|----------------|------|---------|
| `./config` | `/app/config` | `ro` | Pipeline YAML, settings |
| `./input` | `/app/input` | `ro` | Source data files (CSV, JSON, etc.) |
| `./output` | `/app/output` | `rw` | Sink output files |
| `./data` | `/app/data` | `rw` | SQLite audit DB, checkpoints, payloads |
| `./secrets` | `/app/secrets` | `ro` | Sensitive config files (optional) |

**Example:**

```bash
docker run --rm \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/data:/app/data \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  run --settings /app/config/pipeline.yaml --execute
```

---

## Environment Variables

Pass secrets and configuration via environment variables. See the [Environment Variables Reference](../reference/environment-variables.md) for the complete list.

The image contains PostgreSQL clients, not a PostgreSQL server or the `psql`
command. These clients are the psycopg v3 and psycopg2 Python drivers. The
image supports
`postgresql+psycopg://` with psycopg v3 and
`postgresql+psycopg2://` with psycopg2. Compose provisions a PostgreSQL
container. The tracked AWS ECS Terraform package provisions Aurora PostgreSQL
outside the application task. Azure production and BYO Kubernetes deployments
require operator-provided external PostgreSQL. Native Linux may use SQLite on
one persistent host. Azure production requires external Azure Database for
PostgreSQL. Azure VM SQLite is supported only for explicitly non-production use
on one persistent host.

```bash
docker run --rm \
  -e DATABASE_URL="sqlite:////app/data/audit.db" \
  -e OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" \
  -e ELSPETH_FINGERPRINT_KEY="${ELSPETH_FINGERPRINT_KEY}" \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/data:/app/data \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  run --settings /app/config/pipeline.yaml --execute
```

**Key variables for Docker:**

| Variable | Purpose |
|----------|---------|
| `ELSPETH_FINGERPRINT_KEY` | Secret fingerprinting (required if config contains API keys) |
| `OPENROUTER_API_KEY` | LLM provider API key |
| `DATABASE_URL` | Audit database (default: SQLite) |

For PostgreSQL, ELSPETH also accepts bare `postgresql://...`, which uses the
bundled psycopg2 driver. Images built with the `postgres` extra contain both
PostgreSQL clients. The official generic image is built
with `INSTALL_EXTRAS=all`; verify an artifact's selected profile before
promotion with:

```bash
docker image inspect --format '{{ index .Config.Labels "io.elspeth.install-extras" }}' "$IMAGE"
```

---

## Common Commands

For complete CLI reference including all options and flags, see [User Manual - CLI Commands](user-manual.md#cli-commands).

### Run a Pipeline

```bash
docker run --rm \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/data:/app/data \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  run --settings /app/config/pipeline.yaml --execute
```

### Validate Configuration

```bash
docker run --rm \
  -v $(pwd)/config:/app/config:ro \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  validate --settings /app/config/pipeline.yaml
```

### Explain a Row

For interactive exploration, mount the state and use the TUI (requires `-it`):

```bash
docker run -it --rm \
  -v $(pwd)/data:/app/data:ro \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  explain --run latest --row 42 --database /app/data/audit.db
```

For non-interactive environments (CI/CD), use text or JSON explain output:

```bash
docker run --rm \
  -v $(pwd)/data:/app/data:ro \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  explain --run latest --row 42 --no-tui --database /app/data/audit.db

docker run --rm \
  -v $(pwd)/data:/app/data:ro \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  explain --run latest --row 42 --json --database /app/data/audit.db
```

### Resume an Interrupted Run

```bash
docker run --rm \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/input:/app/input:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/data:/app/data \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  resume abc123 --execute
```

---

## Using docker-compose

For easier management, use docker-compose:

```yaml
# docker-compose.yaml
services:
  elspeth:
    image: ghcr.io/johnm-dta/elspeth:${IMAGE_TAG:?set IMAGE_TAG to sha-<commit> or v*}
    environment:
      - DATABASE_URL=${DATABASE_URL:-sqlite:////app/data/audit.db}
      - OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
      - ELSPETH_FINGERPRINT_KEY=${ELSPETH_FINGERPRINT_KEY:-}
    volumes:
      - ./config:/app/config:ro
      - ./input:/app/input:ro
      - ./output:/app/output
      - ./data:/app/data
    command: ["--help"]
```

### docker-compose Commands

```bash
# Run a pipeline
docker compose run --rm elspeth run --settings /app/config/pipeline.yaml --execute

# Validate config
docker compose run --rm elspeth validate --settings /app/config/pipeline.yaml

# Check health
docker compose run --rm elspeth health --verbose

# Explain a decision (interactive TUI)
docker compose run -it --rm elspeth explain --run latest --row 42 --database /app/data/audit.db
```

### Production Docker Compose

Run the shipped bundle from the repository root. It starts one web process,
PostgreSQL 16, distinct session and Landscape databases, schema initialization,
and separate PostgreSQL and ELSPETH state volumes.
The `web-init` service runs `doctor deployment --init-schema` before the web
service starts.

1. Create the repository-root `.env`:

   ```bash
   cp deploy/compose/.env.example .env
   chmod 600 .env
   ```

2. Generate the database password:

   ```bash
   openssl rand -hex 24
   ```

   Put the result in `.env` as `POSTGRES_PASSWORD`. It must remain a
   48-character lowercase hexadecimal value. The bundle uses this same
   URL-unreserved value for the PostgreSQL role and connection URLs; do not
   paste an arbitrary unencoded password into a database URL. Complete the
   other required secrets and set `IMAGE_TAG` to an immutable `sha-*` or `v*`
   release tag.

3. Start the exact three-file bundle:

   ```bash
   docker compose --env-file .env \
     -f docker-compose.yaml \
     -f deploy/compose/postgres.yaml \
     -f deploy/compose/web-postgres.yaml up -d
   ```

4. Verify the one-shot schema gate and readiness:

   ```bash
   docker compose --env-file .env \
     -f docker-compose.yaml \
     -f deploy/compose/postgres.yaml \
     -f deploy/compose/web-postgres.yaml run --rm web-init \
     doctor deployment
   curl -fsS http://127.0.0.1:8451/api/ready
   ```

The `postgres_data` volume is database persistence. The `elspeth_state`
volume holds payload persistence and local data. Back up and restore them as
separate stores. Do not scale `web` beyond one replica or run more than one
web process.

---

## Health Checks

The `health` command verifies system readiness:

```bash
# Basic health check
docker run --rm ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} health

# Verbose output
docker run --rm ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} health --verbose

# JSON output (for automation)
docker run --rm ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} health --json
```

### Example JSON Output

Exact plugin counts and the Python patch version vary by release. A generic
image without `DATABASE_URL` reports the database check as skipped:

```json
{
  "status": "healthy",
  "version": "0.7.2",
  "commit": "unavailable",
  "checks": {
    "version": {"status": "ok", "value": "0.7.2"},
    "commit": {"status": "warn", "value": "unavailable"},
    "python": {"status": "ok", "value": "3.13.5"},
    "database": {"status": "skip", "value": "DATABASE_URL not set"},
    "config_dir": {"status": "ok", "value": "/app/config"},
    "output_dir": {"status": "ok", "value": "/app/output"},
    "plugins": {"status": "ok", "value": "7 sources, 31 transforms, 8 sinks"},
    "web": {"status": "skip", "value": "skipped via --skip-web"}
  }
}
```

### Kubernetes

This release does not ship Kubernetes manifests. BYO manifests must use one
web process in one replica, `strategy: Recreate`, external PostgreSQL, and
persistent payload storage. See the
[deployment platform contract](../reference/deployment-platforms.md#kubernetes).

---

## Image Tags

| Tag Pattern | Example | Use Case |
|-------------|---------|----------|
| `sha-<commit>` | `sha-<full-commit>` | CI/CD deployments (immutable, recommended) |
| `v<version>` | `v<released-version>` | Published release versions |

Use `sha-<commit>` tags for immutable deployments. The build workflow does not
publish `latest`. Always inspect the selected registry tag before using it.

---

## Container Registries

Images are published to:

- **GitHub Container Registry**: `ghcr.io/johnm-dta/elspeth`
- **Azure Container Registry**: `<your-acr>.azurecr.io/elspeth` (if configured)

### Pulling from Private Registry

```bash
# GitHub Container Registry
printf '%s' "$GITHUB_TOKEN" \
  | docker login ghcr.io -u "$GITHUB_USERNAME" --password-stdin
docker pull ghcr.io/johnm-dta/elspeth:${IMAGE_TAG}

# Azure Container Registry
az acr login --name your-acr
docker pull your-acr.azurecr.io/elspeth:${IMAGE_TAG}
```

---

## Pipeline Configuration

Pipeline configurations in containers should use **absolute container paths**:

```yaml
# config/pipeline.yaml
source:
  plugin: csv
  on_success: output              # Route rows directly to sink
  options:
    path: /app/input/data.csv     # Container path, not host path
    schema:
      mode: observed

sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: /app/output/results.csv  # Container path

landscape:
  url: ${DATABASE_URL:-sqlite:////app/data/audit.db}

payload_store:
  base_path: /app/data/payloads
```

**Common mistake:** Using host paths like `./input/data.csv` instead of container paths `/app/input/data.csv`.

---

## Building Locally

```bash
# Build the generic release profile
docker build \
  --build-arg INSTALL_EXTRAS=all \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  -t elspeth:local .

# Run locally built image
docker run --rm elspeth:local --version

# Build the lean AWS/PostgreSQL profile
docker build \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  -t elspeth:aws-postgres .
```

The Python and Node versions are pinned in `Dockerfile` and `.node-version`;
they are not Docker build arguments. Update those reviewed version surfaces
and their contract tests together when intentionally changing a toolchain.

## Runtime Image Contract

The release Dockerfile uses three stages:

1. a pinned Node.js 24 builder runs `npm ci` and creates the React bundle;
2. a pinned Python 3.13 builder installs locked Python extras, installs ELSPETH
   non-editably, normalizes generated frontend directories to `0755` and files
   to `0644`, and prepares the runtime filesystem; and
3. a pinned `gcr.io/distroless/python3-debian13:debug-nonroot` image receives
   only that prepared runtime.

The final image runs as UID/GID 1654, contains no package manager or OS build
toolchain, and does not contain `psql`. The debug-nonroot variant deliberately
retains BusyBox `/bin/sh` compatibility for the shipped Compose initialization
step and the existing ECS entrypoint wrapper. Treat that shell as a narrow
launch/diagnostic dependency, not as an invitation to mutate a running
container.

Verify the built artifact rather than inferring its contents from the
Dockerfile:

```bash
IMAGE=elspeth:local

test "$(docker image inspect "$IMAGE" --format '{{.Config.User}}')" = "elspeth"
test "$(docker run --rm --entrypoint id "$IMAGE" -u)" = "1654"
test "$(docker run --rm --entrypoint id "$IMAGE" -g)" = "1654"
docker run --rm --entrypoint /bin/sh "$IMAGE" -c \
  'test -d /app/data/blobs &&
   test -d /app/data/outputs &&
   test -r /opt/venv/lib/python3.13/site-packages/elspeth/web/frontend/dist/index.html &&
   test ! -e /usr/bin/apt-get &&
   test ! -e /usr/bin/psql'
```

---

## Troubleshooting

For general ELSPETH troubleshooting (API errors, configuration issues, etc.), see the [Troubleshooting Guide](troubleshooting.md). Below are Docker-specific issues.

### Common Docker Errors

- **"File not found"** - See [File Not Found Errors](troubleshooting.md#file-not-found-errors) (verify volume mounts and container paths)
- **"Permission denied"** - The published image runs as UID/GID 1654. Create
  the bind-mounted directory and assign it to that identity, for example
  `mkdir -p ./output && sudo chown 1654:1654 ./output && chmod 0700 ./output`.
  Do not make deployment data world-writable.

### Database connection refused

**Symptom:** `OperationalError: could not connect to server`

**Cause:** PostgreSQL not accessible from container.

**Fix:**
- In the shipped three-file Compose bundle, use the service name `postgres`,
  not `localhost`
- Standalone: Use `--network host` or ensure container can reach database

### Secrets not fingerprinted

**Symptom:** `SecretFingerprintError: ELSPETH_FINGERPRINT_KEY not set`

**Cause:** Missing required environment variable.

**Fix:**
```bash
export ELSPETH_FINGERPRINT_KEY="$(openssl rand -hex 32)"
: "${IMAGE_TAG:?set the confirmed published image tag}"
docker run --rm \
  -e ELSPETH_FINGERPRINT_KEY="${ELSPETH_FINGERPRINT_KEY}" \
  ghcr.io/johnm-dta/elspeth:${IMAGE_TAG} \
  health --json
```

## See Also

- [Deployment Platforms](../reference/deployment-platforms.md) - Maintained and BYO support boundaries
- [AWS ECS Cold Install](../runbooks/aws-ecs-cold-install.md) - Complete disposable stack with Aurora, monitoring, and Bedrock
- [AWS ECS Existing-Service Redeploy](../runbooks/aws-ecs-existing-service-redeploy.md) - Everyday immutable image redeploy
- [AWS ECS Full Acceptance Runbook](../runbooks/aws-ecs-deployment.md) - Disposable two-scenario provisioning and acceptance
- [Your First Pipeline](your-first-pipeline.md) - Getting started guide
- [Configuration Reference](../reference/configuration.md) - Complete config options
- [Runbooks](../runbooks/) - Operational procedures
