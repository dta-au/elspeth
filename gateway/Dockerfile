# syntax=docker/dockerfile:1
#
# Multi-stage build: the builder takes uv from an immutable image digest, uses
# the lock for both the build tools and runtime graph, then installs the exact
# local wheel without dependency resolution. The final stage copies only the
# runtime venv onto a fresh, pinned base and runs as a non-root UID with no
# further installation. There is no shell entrypoint or build tool at runtime.
#
# Base image is pinned by digest (not just the ``3.12-alpine`` tag) so a
# repository push behind that tag can never silently change what this
# image is built FROM.
FROM python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111 AS builder

ARG GATEWAY_REVISION
RUN test -n "$GATEWAY_REVISION"

# The binary comes from the same digest-pinned source as the root release
# image. A mutable uv tag must never participate in this build.
COPY --from=ghcr.io/astral-sh/uv@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /build
COPY pyproject.toml uv.lock ./

# Install the locked runtime and build graphs before copying source so
# source-only changes can reuse this layer. The separate build venv keeps the
# pinned backend out of the shipped runtime environment.
RUN uv venv /build-venv && \
    VIRTUAL_ENV=/build-venv UV_PROJECT_ENVIRONMENT=/build-venv PATH="/build-venv/bin:$PATH" uv sync \
      --directory /build \
      --frozen \
      --no-default-groups \
      --group container-build \
      --no-install-project \
      --active

COPY src /build/src

# Build with the backend already installed from the lock. Then create a clean
# runtime venv, sync only its locked third-party graph, and add the exact local
# wheel without dependency resolution.
RUN VIRTUAL_ENV=/build-venv PATH="/build-venv/bin:$PATH" uv build \
      --directory /build \
      --no-build-isolation \
      --wheel \
      --out-dir /dist && \
    uv venv /venv && \
    VIRTUAL_ENV=/venv UV_PROJECT_ENVIRONMENT=/venv PATH="/venv/bin:$PATH" uv sync \
      --directory /build \
      --frozen \
      --no-default-groups \
      --no-install-project \
      --active && \
    uv pip install --python /venv/bin/python --no-deps /dist/elspeth_llm_gateway-*.whl

FROM python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111

ARG GATEWAY_REVISION
LABEL org.opencontainers.image.revision="${GATEWAY_REVISION}" \
    io.elspeth.llm-gateway.runtime-identity="elspeth-llm-gateway" \
    io.elspeth.llm-gateway.runtime-version="0.1.0" \
    io.elspeth.llm-gateway.contract-major="1" \
    io.elspeth.llm-gateway.adapter-name="reference_v1_invoke" \
    io.elspeth.llm-gateway.adapter-version="0.1.0" \
    io.elspeth.llm-gateway.adapter-api-major="1" \
    io.elspeth.llm-gateway.adapter-fingerprint="2b195149cd2056ef51bab6cbd19f3da7c629a95eda614614f385e04933846c83"

# PYTHONDONTWRITEBYTECODE: this image runs read-only (no writes outside
# /tmp); the interpreter must never attempt to write .pyc files next to the
# venv's site-packages. PATH puts the venv's absolute python/uvicorn ahead
# of the base image's system interpreter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH"

COPY --from=builder /venv /venv

# Fixed, non-root, non-allocatable UID/GID (distroless convention) -- never
# root, and never a UID that could collide with a container-host account.
USER 65532:65532

EXPOSE 8787

# Absolute /venv/bin/python is mandatory: a bare "python" on PATH would
# still resolve to the venv first here, but an absolute path removes any
# doubt and survives a PATH override from `docker run --env PATH=...`.
# No shell entrypoint (exec form only) -- PID 1 is uvicorn itself, so
# SIGTERM reaches it directly for --timeout-graceful-shutdown to matter.
ENTRYPOINT ["/venv/bin/python", "-m", "uvicorn", "elspeth_llm_gateway.core.app:build", "--host", "0.0.0.0", "--port", "8787", "--factory", "--timeout-graceful-shutdown", "30"]
