# Dockerfile for ELSPETH - Auditable Sense/Decide/Act Pipelines
#
# Multi-stage build for minimal runtime image.
# Default builds bundle all plugins; INSTALL_EXTRAS selects a lean plugin set.
#
# No default command - explicit command required (web, run, etc.).
# Container orchestrators should configure appropriate health checks per deployment.
#
# Usage:
#   docker build -t elspeth .
#   docker run elspeth --help                                                # Show available commands
#   docker run elspeth --version                                             # Show version
#   docker run elspeth run --settings /app/config/pipeline.yaml              # Run batch pipeline
#   docker run -p 8451:8451 <required-web-env> elspeth web --host 0.0.0.0   # Start web server

# One canonical build selection is threaded through every stage. The runtime
# label makes the selected extras inspectable on the final artifact; official
# generic GHCR/ACR builds set this explicitly to "all".
ARG INSTALL_EXTRAS="all"

# =============================================================================
# Stage 1: Frontend Builder
# =============================================================================
FROM node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d AS frontend-builder

WORKDIR /frontend

# Keep the package manager as reproducible as the Node base on every target
# architecture, and fail the build immediately if either runtime drifts.
RUN npm install --global npm@11.6.2 && \
    test "$(node --version)" = "v24.18.0" && \
    test "$(npm --version)" = "11.6.2"

# Install frontend dependencies from the lockfile first (layer caching)
COPY src/elspeth/web/frontend/package.json src/elspeth/web/frontend/package-lock.json ./
RUN npm ci

# Build the React SPA. The resulting dist/ is ignored by git and .dockerignore,
# so the release image must build it inside Docker.
COPY src/elspeth/web/frontend/ ./
RUN npm run build

# =============================================================================
# Stage 2: Python Builder
# =============================================================================
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

# Install uv for fast, deterministic dependency resolution
# Using official installer (https://docs.astral.sh/uv/getting-started/installation/)
COPY --from=ghcr.io/astral-sh/uv@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /usr/local/bin/uv

# Set up working directory
WORKDIR /build

# Copy only dependency specification first (layer caching)
COPY pyproject.toml uv.lock ./
COPY elspeth-lints/ ./elspeth-lints/

# Create virtual environment and sync the selected locked dependencies.
# The default "all" preserves the shared GHCR/ACR image behavior.
ARG INSTALL_EXTRAS
RUN uv venv /opt/venv && \
    . /opt/venv/bin/activate && \
    test -n "$INSTALL_EXTRAS" && \
    set -f && \
    set -- && \
    for e in $INSTALL_EXTRAS; do \
        case "$e" in [a-z0-9]*) ;; *) exit 2 ;; esac; \
        case "$e" in *[!a-z0-9-]*) exit 2 ;; esac; \
        set -- "$@" --extra "$e"; \
    done && \
    test "$#" -gt 0 && \
    uv sync --frozen "$@" --no-install-project --active

# Copy source code
COPY src/ ./src/

# Hatch requires the project readme while building metadata. Use fixed content
# and an epoch timestamp so public README edits cannot alter release images.
RUN printf '%s\n' '# ELSPETH package metadata' > README.md && \
    touch --date=@0 README.md

# Install the project from the lockfile (non-editable) with the same selected extras.
RUN . /opt/venv/bin/activate && \
    test -n "$INSTALL_EXTRAS" && \
    set -f && \
    set -- && \
    for e in $INSTALL_EXTRAS; do \
        case "$e" in [a-z0-9]*) ;; *) exit 2 ;; esac; \
        case "$e" in *[!a-z0-9-]*) exit 2 ;; esac; \
        set -- "$@" --extra "$e"; \
    done && \
    test "$#" -gt 0 && \
    uv sync --frozen "$@" --no-editable --active

# Copy built SPA assets into the installed package, where app.py looks for
# elspeth/web/frontend/dist at runtime.
COPY --from=frontend-builder /frontend/dist /tmp/frontend-dist/
RUN find /tmp/frontend-dist -type d -exec chmod 0755 {} + && \
    find /tmp/frontend-dist -type f -exec chmod 0644 {} + && \
    . /opt/venv/bin/activate && \
    python -c 'from pathlib import Path; import shutil; import elspeth.web; target = Path(elspeth.web.__file__).parent / "frontend" / "dist"; shutil.rmtree(target, ignore_errors=True); target.parent.mkdir(parents=True, exist_ok=True); shutil.copytree("/tmp/frontend-dist", target)' && \
    rm -rf /tmp/frontend-dist

# Prepare everything the final stage would otherwise need to manufacture.
# The debug distroless variant retains BusyBox utilities for the documented
# Docker smoke and the AWS ECS launch wrapper, while the application identity
# and writable roots stay unchanged.
RUN groupadd --gid 1654 elspeth && \
    useradd --uid 1654 --gid elspeth --shell /bin/sh --home-dir /home/elspeth elspeth && \
    mkdir -p \
        /runtime-root/app/config \
        /runtime-root/app/data/blobs \
        /runtime-root/app/data/outputs \
        /runtime-root/app/input \
        /runtime-root/app/ops \
        /runtime-root/app/output \
        /runtime-root/app/secrets \
        /runtime-root/app/state \
        /runtime-root/etc \
        /runtime-root/home/elspeth \
        /runtime-root/usr/bin && \
    ln -s /busybox/sh /runtime-root/usr/bin/sh && \
    cp /etc/passwd /runtime-root/etc/passwd && \
    cp /etc/group /runtime-root/etc/group && \
    chown -R 1654:1654 /runtime-root/app /runtime-root/home/elspeth && \
    sed -i \
        -e 's#^home = .*#home = /usr/bin#' \
        -e 's#^executable = .*#executable = /usr/bin/python3.13#' \
        /opt/venv/pyvenv.cfg && \
    ln -sfn /usr/bin/python3.13 /opt/venv/bin/python && \
    ln -sfn python /opt/venv/bin/python3 && \
    ln -sfn python /opt/venv/bin/python3.13

# =============================================================================
# Stage 3: Runtime
# =============================================================================
FROM gcr.io/distroless/python3-debian13:debug-nonroot@sha256:6418f576f2011f5d265d03f53aee812b4efcba5c6646a3f4d855b9fb51cd2d72 AS runtime

ARG INSTALL_EXTRAS

# Labels for container registry
LABEL org.opencontainers.image.title="ELSPETH"
LABEL org.opencontainers.image.description="Auditable Sense/Decide/Act Pipelines"
LABEL org.opencontainers.image.source="https://github.com/johnm-dta/elspeth"
LABEL org.opencontainers.image.licenses="MIT"
LABEL io.elspeth.install-extras="$INSTALL_EXTRAS"
LABEL io.elspeth.runtime-uid="1654"
LABEL io.elspeth.runtime-gid="1654"

# Copy the prepared identity, home, application roots, and virtual environment.
COPY --from=builder /runtime-root/ /
COPY --from=builder /opt/venv /opt/venv

# Set up PATH to use venv
ENV PATH="/opt/venv/bin:$PATH"
ENV HOME="/home/elspeth"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set working directory
WORKDIR /app

# Switch to non-root user
USER elspeth

# Expose web interface port (used when running `elspeth web`)
EXPOSE 8451

# No image-level HEALTHCHECK - container orchestrators should configure
# appropriate health checks per deployment type:
#
#   Web task definitions: loopback GET /api/health
#   ALB target groups:     GET /api/ready
#   Batch tasks:           process exit code (no persistent health endpoint)
#
# An image-level probe would mark batch containers unhealthy even when their
# process-exit contract is working correctly.

# Entry point is the elspeth CLI
# Arguments after image name are passed directly to elspeth
ENTRYPOINT ["/opt/venv/bin/elspeth"]

# Default command shows help - explicit command required for all operations.
# The web server requires ELSPETH_WEB__SECRET_KEY for non-loopback hosts,
# so we don't default to `web` which would fail without configuration.
CMD ["--help"]
