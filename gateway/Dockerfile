# syntax=docker/dockerfile:1
#
# Multi-stage build: the builder stage installs the gateway wheel (and its
# dependencies) into an isolated venv at /venv; the final stage copies only
# that venv onto a fresh, pinned base and runs as a non-root UID with no
# further installation. There is no shell entrypoint and no ``pip install``
# in the final stage -- what ships is exactly what the builder produced.
#
# Base image is pinned by digest (not just the ``3.12-slim`` tag) so a
# repository push behind that tag can never silently change what this
# image is built FROM.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS builder

ARG GATEWAY_REVISION
RUN test -n "$GATEWAY_REVISION"

RUN python -m venv /venv

COPY pyproject.toml /build/pyproject.toml
COPY src /build/src

RUN /venv/bin/pip install --no-cache-dir /build

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ARG GATEWAY_REVISION
LABEL org.opencontainers.image.revision="${GATEWAY_REVISION}" \
    io.elspeth.llm-gateway.runtime-identity="elspeth-llm-gateway" \
    io.elspeth.llm-gateway.runtime-version="0.1.0" \
    io.elspeth.llm-gateway.contract-major="1" \
    io.elspeth.llm-gateway.adapter-name="reference_v1_invoke" \
    io.elspeth.llm-gateway.adapter-version="0.1.0" \
    io.elspeth.llm-gateway.adapter-api-major="1" \
    io.elspeth.llm-gateway.adapter-fingerprint="d51d14e3696236892fecd0436681ab105872558a9ab43a0d545cc725b3d8ab6b"

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
