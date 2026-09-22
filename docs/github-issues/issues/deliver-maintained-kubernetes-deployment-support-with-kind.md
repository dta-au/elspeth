---
title: Deliver maintained Kubernetes deployment support with kind acceptance
labels: [area/deployment, type/task]
---

Deliver maintained Kubernetes deployment support: canonical base manifests, deterministic render validation, an immutable accepted-digest policy, and a checksum-pinned kind acceptance run. `deploy/` currently ships Compose, systemd, AWS ECS and Azure Container Apps targets and no Kubernetes manifests.

## Scope

- **Canonical base manifests**, plus a managed-Kubernetes overlay (AKS is in scope).
- **Deterministic render validation** — rendering the manifests twice from the same inputs must produce identical output, so a render diff means an input changed.
- **Immutable accepted-digest policy** for the deployed image, so an accepted deployment names a digest rather than a moving tag.
- **Checksum-pinned kind acceptance** covering identity, health and readiness, persistence, configuration, and teardown. Pinning the tooling by checksum is part of the deliverable, not an optimisation.
- Shared CI workflow edits coordinated with the final deployment-gate task rather than landed piecemeal, since both touch the same workflow files.

## Bar

An earlier plan for this work was written to a single-replica deployment. That is not the bar: the manifests, storage choices and acceptance checks are to be written to a multi-replica bar, and the sizing behind that needs restating before anyone writes manifests.

## Note

The source for this task is a scope statement rather than a design. The descriptions of "deterministic render validation" and "immutable accepted-digest policy" above are the extent of what is recorded, so whoever picks this up should expect to settle the details. Planning material that appears to cover the same ground — a kustomize base, a render gate, a kind harness, acceptance probes and an AKS overlay — sits under `docs/plans/2026-09-13-kubernetes-and-identity/`; check it against the multi-replica bar before treating it as current.
