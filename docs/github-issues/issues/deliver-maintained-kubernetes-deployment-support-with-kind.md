---
title: Deliver maintained Kubernetes deployment support with kind acceptance
labels: [area/deployment, type/task]
---

Deliver a maintained Kubernetes deployment path for ELSPETH: canonical base manifests, deterministic render validation, an immutable accepted-digest policy, and an acceptance run on kind (Kubernetes in Docker, a local single-machine cluster used for testing).

**Where this lives.** `deploy/`, which currently ships Compose, systemd, AWS ECS and Azure Container Apps targets and no Kubernetes manifests. A Kubernetes target would be a new sibling directory there, with its acceptance run wired into `.github/workflows/`.

## Scope

- **Canonical base manifests**, plus an overlay for AKS (Azure Kubernetes Service), which is the managed target in scope.
- **Deterministic render validation** — rendering the manifests twice from the same inputs produces identical output, so any diff in a rendered manifest means an input changed rather than the tooling wobbling.
- **Immutable accepted-digest policy** — an accepted deployment names an image by content digest rather than a moving tag, so what was tested is what runs.
- **kind acceptance**, with the tooling pinned by checksum, covering identity, health and readiness, persistence, configuration, and teardown. The checksum pinning is part of the deliverable, not a refinement.
- Shared CI workflow edits coordinated with the other in-flight deployment-gate work rather than landed piecemeal, since they touch the same workflow files. Whoever picks this up should check what else is editing `.github/workflows/` before starting.

## Bar

An earlier plan for this work was written to a single-replica deployment. That is not the bar: the manifests, storage choices and acceptance checks are to be written to a multi-replica bar, and the sizing behind that needs restating before anyone writes a manifest.

## Fix

Done means: a documented Kubernetes deployment path in `deploy/` that a reader can follow end to end; a render check that fails when the same inputs produce different output; a deployment that refuses an image named by anything but a digest; and a kind-based acceptance run in CI that brings the stack up, proves identity, health and readiness, persistence and configuration, then tears it down — passing on a multi-replica deployment, not a single pod.

## Note on size and starting state

This is epic-sized: several days of work, spanning manifests, a render gate, a CI acceptance harness and documentation. It is not a good first ticket, and it should probably be split once the multi-replica sizing is settled.

The source for it is a scope statement rather than a design — the descriptions above are the full extent of what was recorded, so expect to settle details as you go. Planning material that appears to cover the same ground (a kustomize base, a render gate, a kind harness, acceptance probes and an AKS overlay) sits under `docs/plans/2026-09-13-kubernetes-and-identity/`; check it against the multi-replica bar before treating any of it as current.
