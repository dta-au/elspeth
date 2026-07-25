# Finish Deferred Deployment Platforms Design

**Date:** 2026-07-26
**Status:** Approved direction for implementation planning
**Supersedes:** The deferred Azure Container Apps, Kubernetes, and
machine-readable profile portions of
`2026-07-24-cross-platform-deployment-contract-design.md`
**Implementation posture:** Start from the current release branch in a fresh
worktree. Do not revive the previously reverted Azure bundle as a finished
change.

The implementation worktree is
`.worktrees/finish-deferred-deployment-platforms` on branch
`codex/finish-deferred-deployment-platforms`. Create it from the exact
plan-bearing release commit after this spec and its implementation plan are
reviewed and committed. Do not recreate or reuse the former
`.worktrees/cross-platform-deployment-contract`: its branch is already merged,
the worktree is retired, and its commit predates the landed AWS acceptance
refactor.

## Objective

Finish the deployment work deliberately left out of the merged cross-platform
deployment contract:

1. ship and test a maintained Kubernetes/Kustomize base;
2. make the web runtime safe during the transient process overlap that Azure
   Container Apps can create even when steady-state scaling is fixed to one;
3. ship and test a workload-only Azure Container Apps Bicep module;
4. codify the five supported platforms in machine-readable profiles; and
5. update the public support matrix and runbooks only after the corresponding
   artifacts and acceptance evidence exist.

The already-merged runtime target/state contract, PostgreSQL drivers, external
PostgreSQL doctor/startup checks, Compose PostgreSQL web stack, AWS support,
portable Linux systemd bundle, and real-PostgreSQL deployment tests remain the
baseline. This work extends that baseline rather than repeating it.

The baseline now includes merge `ee5fb6cc9`, which refactored the AWS ECS
acceptance controller behind its permanent public facade. This design treats
that landed architecture as a compatibility contract, not as an invitation to
recombine or redesign the AWS controller while adding the deferred platforms.

## Deferred Work Inventory

The original plan's cancelled bodies were:

- Azure Container Apps workload Bicep and its compile gate;
- Kubernetes/Kustomize manifests and their render gate; and
- `deploy/platforms/` schema and five support profiles.

The Kubernetes and profile tasks were never committed. The Azure task existed
briefly in commit `773fbd3bf` and was reverted in `005f83f07` after architecture
review found its zero-overlap assumption unsafe. The old Azure commit is useful
as a source of parameter names and static tests, but it is not safe to
cherry-pick: Container Apps single-revision mode and `maxReplicas: 1` do not
guarantee that old and new processes never overlap during rollout or
maintenance prewarming.

## Landed AWS Acceptance Baseline

`src/elspeth/web/aws_ecs_acceptance.py` is now the stable executable and import
facade. Domain implementations live in the side-effect-free private package
`src/elspeth/web/_aws_ecs_acceptance/`; the permanent architecture guard in
`tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py` enforces its
layer direction, complete module inventory, forbidden edges, absence of
private-to-facade imports, and acyclicity. Facade compatibility is owned by
`tests/unit/web/aws_ecs_acceptance/test_facade_contract.py`, while the split
domain corpus lives under `tests/unit/web/aws_ecs_acceptance/`.

The new deployment work preserves these seams. Three landed dependencies make
the AWS corpus directly relevant even though AWS itself is not being rebuilt:

- `receipt_contracts.py` consumes `SESSION_SCHEMA_EPOCH`, so the sessions epoch
  change must keep the candidate/receipt schema facts current and rerun receipt
  plus cleanup/control tests;
- `bedrock.py` imports the web execution policy-evidence helper and composer LLM
  adapter, so execution/composer changes must run its owner tests rather than
  monkeypatching the facade; and
- `capture.py` imports composer recipes/state/YAML generation, so composer
  changes must retain the capture and facade contracts.

The AWS profile therefore points to the permanent facade, private-package
architecture guard, split owner tests, runbook contract, and real PostgreSQL
startup/readiness tests. It does not point to the superseded monolithic test
layout or describe the private package as a new public API.

## Chosen Approach

Use four dependency-aware workstreams:

```text
Kubernetes base + kind smoke ----------------------+
                                                      +--> platform profiles
web instance/run fencing --> ACA workload + smoke --+    + public support docs
```

Kubernetes is independently deliverable and may proceed while runtime fencing
is built. Azure Container Apps is not supportable until the fencing acceptance
suite passes. Profiles are deliberately last because they are the
machine-readable claim that all referenced bundles and runbooks exist.

### Alternatives rejected

1. **Restore the reverted Azure bundle and document stop-before-start.** ACA
   controls do not provide a reliable zero-process-overlap boundary, so this
   would turn an operator procedure into a correctness assumption the platform
   can violate.
2. **Use a global PostgreSQL advisory lock so only one replica becomes ready.**
   A standby that refuses readiness can deadlock a readiness-gated rollout; a
   standby that accepts traffic without shared authorities is unsafe. This also
   prevents useful maintenance prewarming.
3. **Ship static provider files and call them support.** YAML/Bicep compilation
   cannot prove database wiring, storage permissions, startup, readiness, or
   cross-process behavior. Each maintained bundle needs an executable smoke
   lane in addition to static validation.

## Support Posture

This milestone supports exactly one web process per container and one
steady-state replica per deployment. `WEB_CONCURRENCY=1` remains mandatory.
The new runtime coordination makes brief rollout overlap and failed-owner
takeover safe; it does not advertise general horizontal scale-out or increased
throughput. Profiles and docs must distinguish these concepts:

- `web_processes_per_replica: 1`;
- `steady_state_replicas: 1`;
- `horizontal_scale_supported: false`; and
- `rollout_overlap_posture`, which is `fenced-transient` only for Azure
  Container Apps and `none` for the stop-before-start/Recreate profiles.

## Web Runtime Coordination

### Authority and clocks

The shared sessions PostgreSQL database is the authority for web-instance
membership, session-run ownership, cancellation requests, short-lived tickets,
composer progress, and HTTP rate limits. Lease comparisons use PostgreSQL's
database clock inside the transaction; wall clocks from different container
hosts are never used to decide custody.

SQLite deployments retain their existing in-process implementations. They are
single-host, single-process profiles and do not need distributed membership.
Cloud and Kubernetes profiles already require external PostgreSQL, so the
distributed adapter may fail closed if selected without PostgreSQL.

### Reuse before invention

ELSPETH already has two useful coordination patterns:

- Landscape run leadership uses `CoordinationToken(run_id, worker_id,
  leader_epoch)` and CAS-fenced writes; and
- guided operations in the sessions database use an unforgeable lease token,
  monotonic attempt number, takeover, renewal, and fence-lost error.

The web layer follows those patterns. It does not introduce an unrelated
consensus service and does not weaken the existing Landscape fence. Landscape
execution and finalization continue to use the existing coordination token;
sessions-database projections use a new `RunOwnershipFence`.

### Instance membership

Each FastAPI lifespan mints one opaque `instance_id`, then registers a row with:

- deployment target and non-secret revision label;
- `started_at`;
- heartbeat expiry;
- state `active`, `draining`, or `stopped`; and
- last heartbeat time.

The heartbeat task extends membership and the leases for runs owned by that
instance. Shutdown first marks the instance draining, makes readiness fail, and
stops new run claims. It continues heartbeating while owned execution drains,
then marks the instance stopped. A dead process simply stops extending its
lease.

### Session-run ownership

The `runs` row gains:

- `owner_instance_id`;
- monotonic `owner_epoch`;
- `owner_lease_expires_at`;
- `cancel_requested_at`; and
- a bounded non-secret cancellation source.

Creating a run and assigning epoch 1 is one sessions-database transaction under
the existing session write lock. Every durable run mutation accepts
`RunOwnershipFence(run_id, owner_instance_id, owner_epoch)` and CAS-matches all
three values. A zero-row update raises a dedicated fence-lost error and performs
no fallback write.

Takeover is permitted only when both the owning instance and run lease are
expired. A claimant locks one candidate with `FOR UPDATE SKIP LOCKED`, increments
the epoch, and becomes the owner. It does not begin payload work until it has
also acquired the existing Landscape leadership fence for a run that already
has a Landscape ID. Therefore an old worker may finish an in-flight write before
the relevant resource's new epoch is issued, but it cannot write after the new
owner begins work. If the Landscape run became terminal before takeover, the
new owner reconciles the sessions projection instead of resuming it.

### Durable cancellation

The cancel endpoint records a cancellation request in the sessions database
regardless of which replica receives the HTTP request. The local owner is
signalled immediately when present; a peer owner observes the request during
its heartbeat and sets its shutdown event. `cancel_requested` in API responses
comes from durable state, not a process-local `threading.Event` lookup.

Periodic cleanup no longer interprets “not in my process-local map” as orphaned.
It only claims expired leases or terminalizes an expired pending run that never
created a Landscape run. Startup never bulk-cancels all non-terminal runs.

### Progress, tickets, and composer status

Run events are already durable and ordered by `(run_id, sequence)` in the
sessions database. External-PostgreSQL WebSocket handlers consume events after
the last seen sequence from that table and periodically re-check terminal run
status. The current in-process broadcaster may remain as the SQLite fast path,
but it is not an authority for cloud delivery. This avoids adding Redis merely
to support one steady-state replica.

WebSocket tickets are stored by SHA-256 digest, never plaintext, and consumed by
one atomic transaction. The row binds the digest to run ID, user ID, username,
expiry, and consumed state so a ticket issued through one replica works exactly
once through another.

Composer progress snapshots and in-flight request identities become durable
sessions-database rows. The latest snapshot remains bounded to one row per
session; in-flight requests are individual expiring rows so a killed replica
cannot leave a permanent positive count. The existing authenticated ownership
checks remain at the route boundary.

### Cluster-wide rate limiting

External-PostgreSQL deployments use a sessions-database sliding-window limiter.
Subjects are stored as HMAC-SHA256 digests using the existing fingerprint key;
raw user IDs and client IPs are not copied into rate-limit tables. A per-subject
bucket row is locked, expired events are pruned, and the next event is inserted
in one transaction. SQLite profiles keep the in-memory limiter.

### Blobs and Landscape finalization

The current blob service already uses durable reservation rows, PostgreSQL
session/advisory locks, atomic file replacement, and durable deletion cleanup.
The implementation must prove this with two-process PostgreSQL/shared-filesystem
tests and thread `RunOwnershipFence` through run-output link/finalize operations.
User-authored blob operations remain session-scoped and use the existing
sessions-database locks.

Landscape writes continue to use the engine's existing leader epoch. Web
takeover uses the engine resume path to acquire a new Landscape coordination
token before performing recovery, checkpoint, sink, or finalization work. No
new unfenced finalization shortcut is permitted.

## Kubernetes Bundle

`deploy/kubernetes/base/` contains a provider-neutral Kustomize base with:

- one `Deployment` using `strategy: Recreate` and `replicas: 1`;
- one ClusterIP `Service` on port 8451;
- a ConfigMap for non-secret runtime/composer settings;
- a PVC for data, blob, and payload paths;
- a documentation-only Secret example excluded from `kustomization.yaml`;
- the immutable GHCR image example;
- `WEB_CONCURRENCY=1`, target `kubernetes`, and state
  `external-postgresql`;
- secret-key references for both PostgreSQL URLs and application keys;
- liveness `/api/health` and readiness `/api/ready`; and
- non-root UID/GID 1654 with a restrictive init step for the PVC paths.

The base does not install PostgreSQL, ingress, TLS, a storage class, a database
operator, or cloud identity controllers. Operators supply external PostgreSQL,
a Secret, and a storage class that honors the pod's ownership requirements.

Static CI renders the base with a checksum-pinned `kubectl`. A Docker-backed
kind lane loads a locally built ELSPETH image, provisions PostgreSQL only as
test harness infrastructure, initializes separate sessions and Landscape
databases, applies the base through a test overlay, and proves readiness and a
provider-free run. The test PostgreSQL manifest never appears in the shipped
base.

## Azure Container Apps Bundle

`deploy/azure-container-apps/main.bicep` is workload-only. It assumes the
operator has already created:

- a custom-VNet Container Apps environment;
- a user-assigned managed identity;
- Key Vault secrets;
- Azure Database for PostgreSQL with separate logical databases;
- NFS Azure Files environment storage; and
- an `elspeth` storage subtree owned by UID/GID 1654 with mode `0700` paths.

The module uses an immutable ACR image reference, Key Vault secret references,
explicit target/state settings, one steady-state replica, one web process,
NFS-mounted data/payload paths, and the standard health/readiness probes. It
does not create a database, place credentials in parameter files, repair NFS
permissions from a privileged container, or promise that single-revision mode
eliminates transient overlap.

The previous Bicep can be consulted with `git show 773fbd3bf --
deploy/azure-container-apps`, but every parameter, resource API version, and
safety assertion is revalidated. CI compiles Bicep and its inert example
parameters with checksum-pinned Bicep CLI v0.45.15.

Provider-side acceptance remains an operator lane. It must exercise an actual
overlapping revision rollout, peer cancellation, WebSocket ticket/progress
handoff, maintenance prewarming, and old-owner refusal before documentation may
call the module maintained.

## Machine-Readable Profiles

`deploy/platforms/` contains JSON Schema plus exactly five YAML profiles:

```text
docker-compose.yaml
linux-systemd.yaml
aws-ecs.yaml
azure-container-apps.yaml
kubernetes.yaml
```

Each profile records target, state modes, database ownership, required extras,
image delivery, payload storage, process/replica posture, rollout-overlap
posture, tracked artifacts, automated acceptance paths, and authoritative
runbook. Cross-profile tests enforce:

- no production recommendation uses compatibility mode `auto`;
- AWS, ACA, and Kubernetes use external PostgreSQL and include `postgres`;
- Compose is the only shipped profile with a PostgreSQL sidecar;
- every tracked path exists and is known to Git;
- all image examples are immutable or release-specific;
- all profiles use one web process and one steady-state replica;
- no profile claims horizontal scale; and
- ACA alone claims `fenced-transient`, after its two-process and operator gates
  exist.

## Verification and Release Gates

The implementation is complete only when all of the following pass:

- unit tests for schema shape, lease transitions, fence refusal, cancellation,
  tickets, composer progress, rate limits, and profile invariants;
- integration tests that preserve the SQLite single-process path;
- real two-process PostgreSQL tests covering rollout overlap, SIGKILL takeover,
  stale-owner refusal, peer cancellation, maintenance prewarm, cross-replica
  tickets/progress, cluster-wide rate limiting, and shared blob operations;
- Kustomize render tests and a real kind readiness/run smoke;
- Bicep source contract tests and compilation of the module and example params;
- current Compose, Linux, AWS, external-PostgreSQL, image, lint, type, and full
  unit gates; and
- an operator-recorded ACA acceptance result using non-production resources.

Static CI never deploys AWS or Azure resources and never contains live
credentials. Test harness secrets are inert/local. Diagnostic assertions name
missing fields and exception classes without echoing database URLs, tickets,
keys, or subject identities.

The AWS regression gate uses the post-refactor surfaces: the facade contract,
all split owner tests, dependency-direction guard, runbook contract,
deterministic `scenario-namespace` probe, five focused PostgreSQL
startup/readiness files, and wheel/container imports of the public facade. No
new task may restore facade-owned domain logic or patch a facade alias when the
lookup owner is a private module.

## Documentation Transition

Current documentation deliberately says Kubernetes is BYO and ACA is deferred;
current tests assert that their bundle directories do not exist. Those negative
claims change only in the same commits that add verified support:

- Kubernetes becomes a maintained provider-neutral base after the kind lane
  passes;
- ACA becomes a maintained workload module after runtime fencing, Bicep
  compilation, and operator acceptance pass; and
- `deploy/platforms/` becomes the support inventory only after all referenced
  paths exist.

The docs continue to state that the application image contains PostgreSQL
clients, not a PostgreSQL server, and that cloud/Kubernetes operators supply
external PostgreSQL and persistent payload storage.

## Non-Goals

- Running PostgreSQL in the ELSPETH application container.
- Shipping PostgreSQL in the Kubernetes base or Azure module.
- Advertising multiple steady-state replicas or horizontal throughput scaling.
- Adding Redis, a database operator, ingress, DNS, TLS, cloud networking, or
  backup policy.
- Replacing, flattening, or creating a second public entry point around AWS's
  refactored task-definition and acceptance workflow.
- Migrating existing pre-release session databases; the current
  delete-and-recreate schema policy remains in force.
- Automatically deploying cloud resources from ordinary CI.
