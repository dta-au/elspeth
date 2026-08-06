# The agent binaries are static Go (see `file`: "statically linked"), so the
# runtime base contributes no capability to the agent itself. It exists only
# for the task definition, which overrides this ENTRYPOINT with
# `/bin/sh -ceu <cw_agent_command>` and health-probes the listener with
# `python -c`. The base must therefore supply a POSIX shell, coreutils
# (base64, sha256sum, mkdir, printf) and python -- and nothing else.
#
# This is deliberately the same digest the application image pins, which is
# the only base with a zero-finding ECR Basic scan on record for this
# registry. A Debian-derived base (python:3.13-slim) made the runbook's
# require_clean_scan gate unsatisfiable: 33 findings, all OS packages with no
# upstream fix, none of them reachable by the agent (elspeth-878dedd7f5).
FROM amazon/cloudwatch-agent@sha256:4722a6621bc05c94a852513a8c463dc7a69e3f15b99702aa347e5f8a0d994e7f AS agent
FROM gcr.io/distroless/python3-debian13:debug-nonroot@sha256:6418f576f2011f5d265d03f53aee812b4efcba5c6646a3f4d855b9fb51cd2d72

ARG ELSPETH_RELEASE_SHA
LABEL org.opencontainers.image.revision=$ELSPETH_RELEASE_SHA

COPY --from=agent /opt/aws /opt/aws

# Distroless ships BusyBox at /busybox (already on PATH, so base64, sha256sum,
# mkdir and printf resolve) but no /bin/sh. The task definition's entryPoint is
# `/bin/sh`, and /bin is a symlink to usr/bin on Debian 13, so one link in
# /usr/bin restores it without touching the Terraform contract.
SHELL ["/busybox/sh", "-c"]
USER root
RUN ln -s /busybox/sh /usr/bin/sh
USER nonroot

ENV RUN_IN_CONTAINER=True

ENTRYPOINT ["/opt/aws/amazon-cloudwatch-agent/bin/start-amazon-cloudwatch-agent"]
