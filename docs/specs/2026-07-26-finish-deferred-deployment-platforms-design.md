# Finish Deferred Deployment Platforms Design, v3

**Date:** 2026-07-26
**Status:** Proposed for implementation-plan review
**Supersedes:** v2 of this design and the deferred Azure
Container Apps, Kubernetes, and machine-readable profile portions of
`2026-07-24-cross-platform-deployment-contract-design.md`

Implement this design in the existing worktree
`/home/john/elspeth/.worktrees/deferred-platform-completion` on branch
`codex/deferred-platform-completion`. The worktree starts at the plan-bearing
release commit `696b3d141`. Do not recreate the retired
`.worktrees/cross-platform-deployment-contract` worktree or cherry-pick its
reverted Azure bundle as a finished change.

## Decision Summary

Finish the deferred Kubernetes, Azure Container Apps (ACA), and platform-profile
work without claiming general multi-replica support. The implementation keeps
one web process per container and one steady-state replica. It supports only:

- stop-before-start deployment for Docker Compose, Linux systemd, AWS ECS, and
  the Kubernetes base; and
- ACA multiple-revision mode with exactly one active revision receiving 100%
  of traffic at steady state, plus brief overlap when both revisions share one
  storage generation and have exactly the same runtime compatibility key.

The compatibility key is the ordered tuple:

```text
(
  SESSION_SCHEMA_EPOCH,
  SQLITE_SCHEMA_EPOCH,
  WEB_COORDINATION_PROTOCOL_VERSION,
)
```

For this release, `WEB_COORDINATION_PROTOCOL_VERSION == 1`. It increments for
any incompatible change to membership, operation/run fence authority, run-start
reservation or saga transitions, atomic-baseline semantics, cancellation
ordering, recovery admission, cleanup claims, or execution-authority checks.
Telemetry-only additions, closed-label additions that do not change decisions,
and provider/documentation changes do not increment it. A bump is a hard-cut
event: code, tests, execution envelopes, profiles, runbooks, and receipts must
all move together.

Changing any member requires a maintenance cutover. ELSPETH remains pre-release:
the operator revokes the old generation, deletes and recreates its databases,
and starts a fresh storage generation instead of migrating state. There is no
old/new process overlap across different compatibility keys and no rollback
after the destructive boundary.

Within one compatible generation, PostgreSQL-backed coordination provides:

- renewable instance and run ownership leases;
- a database-backed `SessionOperationFence` for every session mutation;
- a durable cross-database run-start saga and secret-reference-only execution
  envelope;
- fail-closed recovery admission; and
- database-clock expiry, bounded cleanup, and leak-safe telemetry.

Kubernetes retains `strategy: Recreate`. SQLite retains its existing
single-process path and implements the same session-mutation authority
signatures through a local table-backed adapter. These paths do not depend on
distributed takeover, but neither bypasses mutation fencing.

## Context and Existing Baseline

The merged baseline already provides the runtime target/state contract,
PostgreSQL drivers, external-PostgreSQL startup and doctor checks, a Compose
PostgreSQL web stack, AWS ECS support, a portable Linux systemd bundle, and
real-PostgreSQL deployment tests. This design extends those surfaces.

Merge `ee5fb6cc9` also made `src/elspeth/web/aws_ecs_acceptance.py` the permanent
AWS acceptance facade. Domain code lives in the side-effect-free private
package `src/elspeth/web/_aws_ecs_acceptance/`. New deployment work preserves
that layer direction and reruns its facade, dependency, receipt, capture,
Bedrock, cleanup, and PostgreSQL owner tests whenever shared web coordination
changes.

The previously reverted ACA bundle assumed that single-revision mode and
`maxReplicas: 1` eliminated process overlap. ACA can overlap old and new
revisions during rollout or maintenance prewarming, so that assumption is not
a correctness boundary. This revision defines the overlap that ELSPETH can
actually tolerate and makes incompatible cutovers explicit.

## Requirements

The implementation must deliver all of the following as one release body:

1. a maintained Kubernetes/Kustomize base with static render and real kind
   smoke coverage;
2. safe transient ACA overlap for equal compatibility keys;
3. a workload-only ACA Bicep module and real provider acceptance;
4. exactly five machine-readable deployment profiles;
5. public support claims that follow, rather than precede, executable evidence;
6. release-gated CI jobs that contribute to the existing `CI Success` result;
7. preservation of pre-release database recreation and the SQLite
   single-process path; and
8. an immutable, machine-validated provider receipt produced only after final
   code review and a clean final build.

## Chosen Architecture

Four workstreams converge on the profiles and documentation:

```text
Kubernetes base + kind smoke ---------------------------+
                                                          +--> profiles + docs
compatible-generation web fencing --> ACA + live smoke --+
maintenance cutover + schema recreation ----------------+
CI aggregation + evidence validation -------------------+
```

This arrangement lets Kubernetes proceed independently while preventing ACA or
the profiles from claiming maintained support before the coordination and
provider gates exist.

## Alternatives Considered

### 1. General multi-replica scale with rolling schema migration

This would make several steady-state replicas and mixed schema epochs a product
feature. It would require expand/contract migrations, backward-compatible
protocols, multi-writer plugin semantics, and a much broader throughput and
failure corpus.

**Rejected:** It replaces the pre-release recreation doctrine and exceeds this
milestone. The selected design solves transient overlap only and fails closed
at compatibility-key boundaries.

### 2. Treat ACA `maxReplicas: 1` as stop-before-start

This preserves the simplest runtime but depends on ACA never prewarming or
overlapping revisions.

**Rejected:** The platform can violate the assumption. An operator note cannot
turn an unenforced rollout property into a safety invariant.

### 3. Hold one global PostgreSQL advisory lock

Only the lock holder would become ready and process work.

**Rejected:** A readiness-gated replacement can deadlock behind the outgoing
revision, and a non-ready standby cannot exercise safe maintenance prewarming.
A global lock also does not fence session writes, cross-database run creation,
or a stale worker that resumes after lease expiry.

### Rationale for the chosen approach

The selected architecture reuses ELSPETH's existing monotonic-token and
compare-and-swap patterns, retains the current release posture, and narrows ACA
overlap to an exact, testable compatibility contract. It adds durable state
only where process-local authority is unsafe and keeps PostgreSQL as the sole
distributed coordination dependency.

## Invariants

The implementation and its tests enforce these invariants:

1. **Compatibility:** two live revisions may overlap only within the same
   storage generation and when all three compatibility-key values are equal.
2. **Single process:** every supported profile sets `WEB_CONCURRENCY=1` and
   `steady_state_replicas: 1`; none claims horizontal scale.
3. **Session mutation:** every compose, proposal, execute, archive, or progress
   write carries the current `SessionOperationFence`. A zero-row
   compare-and-swap is terminal fence loss, never a fallback write. Creation
   atomically inserts a server-generated, non-caller-selectable session ID and
   an epoch-1 `create` fence, completes initialization under that fence, and
   releases it before commit/return. The persisted epoch-1 row is inactive; the
   first later operation advances to epoch 2. Physical deletion under the
   current fence may cascade both rows. No deleted-ID registry exists or is
   required: stale update-only CAS cannot recreate a deleted session or fence.
4. **Run mutation:** every sessions-database run write carries the current
   `RunOwnershipFence`; every Landscape write carries its existing coordination
   token. Every web-run Landscape mutation belongs to an exhaustive,
   architecture-tested fenced inventory. The sole first-statement token-
   creation exception is `begin_run_with_baseline`, which accepts a durable
   typed `WebRunStartPermit` or explicit `LocalRunStartPermit`; Landscape never
   reads or mutates Sessions. It atomically validates/records the supplied
   permit subject and creates the run, epoch-1 token, and baseline.
5. **Execution authority and audit admission:** immediately before every
   source, transform, sink, batch, aggregation, lifecycle, plugin, or effect
   call, the engine passes `ExecutionAuthorityCheck` and synchronously commits
   an invocation-admission audit fact. Audit failure prevents invocation. After
   return it rechecks authority and commits the post-call/result-admission audit
   fact before any result/disposition commit; telemetry follows authoritative
   audit and state, never leads them.
6. **Plugin admission:** a fresh run may persist incomplete sources and execute
   normally, but it is explicitly recovery-ineligible. Recovery code makes no
   plugin call unless the durable envelope, sequence-0 baseline, complete
   sources, exact execution fingerprint and generation, resolver-version
   bindings, and effect-safety checks all pass.
7. **Cancellation priority:** a durable cancellation request prevents resume.
   Reconciliation cancels first and never resumes merely to cancel.
8. **Time authority:** the sessions database decides sessions leases and the
   Landscape database decides Landscape leases. Code never compares a
   timestamp from one database with a timestamp from the other.
9. **Secrets:** durable execution state contains immutable resolver-version and
   target bindings, never resolved credentials, database URLs, tokens, or key
   material.
10. **Audit primacy:** probative saga, takeover, recovery, cancellation,
    fence-loss, and plugin-admission facts commit synchronously before derived
    telemetry. Exporter failure cannot erase or replace audit evidence.
11. **Bounded state:** every transient coordination table has indexed expiry,
   bounded batch cleanup, and retention that does not depend on knowing an HMAC
   preimage or an old key.
12. **Evidence:** a maintained provider claim names one reviewed source commit,
    one all-extras multiarch index plus amd64/arm64 children in exact GHCR/ACR,
    artifact hashes, compatibility key, and successful acceptance receipt. A
    subsequent relevant change invalidates that receipt.

## Deployment Generations and Compatibility

### Generation identity

`deployment_generation` identifies one shared storage generation: an exact
sessions database, Landscape database, versioned database-role/secret set, and
normalized NFS run root. It remains stable throughout every equal-key revision
rollout. It changes only during the hard maintenance cut that creates fresh
databases, roles, secrets, and NFS root.

`revision_label` identifies application code, not storage. It binds the
immutable image digest and required OCI
`org.opencontainers.image.revision` source commit. Instance registration
persists generation, revision label, and compatibility key. Readiness fails if
an active or draining peer has another generation or compatibility key.

Equal compatibility keys are necessary but not sufficient for automatic run
takeover. A different revision may prewarm, receive new traffic, and drain the
old revision, but it may automatically recover an existing run only when its
complete execution fingerprint and deployment generation exactly match the
run's durable envelope.

`WEB_COORDINATION_PROTOCOL_VERSION` is an explicit integer, not an inferred
hash or deployment label. Version 1 names the authority, saga, reservation,
baseline, cancellation, cleanup, and execution-check contract in this design.
Any incompatible semantic or wire/state-machine change increments it and
therefore requires the maintenance cutover. Compatible metrics/exporter or
provider-only work leaves it at 1.

### Equal-key transient overlap

ACA maintained mode sets `activeRevisionsMode: Multiple`. Its steady-state
postcondition is exactly one active revision, exactly 100% application traffic
to that revision, and zero active replicas for every older revision.

For an unchanged generation and compatibility key, the operator state machine
is:

```text
old_active_100
  -> candidate_active_0
  -> candidate_ready_0
  -> candidate_active_100__old_active_0
  -> old_draining_0
  -> old_inactive_0_replicas
  -> candidate_steady_100
```

Each step records intent in the external control ledger, performs one bounded
provider action, observes the live postcondition, and atomically records the
observation before advancing:

1. activate the candidate at 0% traffic with the same databases, NFS run root,
   generation, and compatibility key;
2. target the candidate revision directly and verify its revision label,
   readiness, generation, key, and acceptance-probe binding;
3. atomically set candidate traffic to 100% and old traffic to 0%, then read
   the live traffic weights back;
4. mark the old revision draining, reject new claims, and finish or durably
   cancel its work; and
5. deactivate the old revision and verify both traffic weight 0 and replica
   count 0 before declaring the candidate steady.

Every step is idempotent by observed postcondition. `--resume` re-reads the
ledger and live ACA state and continues the first incomplete intended step; it
never infers success from a timed-out command. Before the traffic shift,
`--abort` deactivates the candidate and restores the old 100% steady state.
After the shift, it may restore the old revision only while that equal-key
revision is ready and no irreversible action occurred; otherwise it converges
forward to the candidate's steady state.

An unequal key makes the candidate unready and unable to acquire any session or
run fence. The deployment must switch to maintenance cutover instead.

### Maintenance cutover for an epoch or protocol change

The runbook performs this ordered state transition:

```text
serving
  -> maintenance_ingress
  -> old_generation_draining
  -> old_revision_traffic_zero
  -> old_revision_deactivated
  -> old_revision_zero_replicas
  -> old_roles_revoked
  -> old_connections_terminated
  -> irreversible_boundary
  -> databases_deleted_and_recreated
  -> new_roles_and_secrets_active
  -> new_nfs_generation_ready
  -> new_generation_ready
  -> serving
```

The operator must:

1. route public traffic to a maintenance response, set every application
   revision to 0% traffic, and stop new sessions;
2. drain or explicitly cancel active work;
3. explicitly deactivate every old revision and verify from ACA that each has
   traffic weight 0 and replica count 0;
4. revoke the old generation's sessions and Landscape database roles and
   terminate every remaining connection for those exact roles;
5. cross the irreversible boundary only after verifying that no old role can
   reconnect;
6. delete and recreate the pre-release sessions and Landscape databases for
   the new epochs;
7. create fresh, versioned database roles and secrets that the old revision
   never possessed;
8. provision a generation-specific NFS subpath and make it the only mounted
   ELSPETH storage path for the new revision; and
9. restore ingress only after startup, readiness, schema, role, and storage
   checks pass.

The hard-cut driver gives every step the same intent/action/observed-
postcondition discipline as an equal-key rollout. `--resume` reconciles
ambiguous provider results against the deterministic intended resource
identities. Before role revocation, `--abort` may reactivate the old revision,
verify one active replica at 100% traffic, and restore ingress. After the
irreversible boundary it can only resume forward or run scoped cleanup; it
never restarts the old image.

Old database credentials and the old NFS subpath are never reused by the new
generation. After `old_roles_revoked`, rollback is forward-only: repair or
replace the new generation. Do not restart the old image against recreated
state. This preserves ELSPETH's delete-and-recreate policy without pretending
that incompatible revisions can overlap.

## PostgreSQL Runtime Coordination

### Instance membership

Each FastAPI lifespan mints an opaque `instance_id` and registers:

- deployment target, storage generation, compatibility key, immutable image
  digest, and OCI revision label;
- `started_at`, `last_heartbeat_at`, and `lease_expires_at`; and
- state `active`, `draining`, or `stopped`.

The heartbeat transaction obtains the sessions database's current time and
renews membership plus sessions leases that the instance still owns. Shutdown
changes `active -> draining`, fails readiness, rejects new claims, and keeps
renewing while owned work drains. It then changes `draining -> stopped`.
Process death causes no state write; sessions-database lease expiry makes
sessions ownership takeover possible.

SQLite uses the current in-process membership-free path. Selecting distributed
coordination without external PostgreSQL fails startup.

### `SessionOperationFence`

The sessions database stores one persistent operation-fence row per session:

```text
SessionOperationFence(
  session_id,
  operation_id,
  lease_token,
  operation_kind,
  owner_instance_id,
  operation_epoch,
  lease_expires_at,
  released_at,
)
```

`operation_kind` is a closed vocabulary covering `create`, `compose`,
`proposal`, `execute`, `archive`, and `progress`. `operation_id` is the durable logical
request identity. `lease_token` is a separate random, unforgeable token minted
for each acquisition or takeover. `owner_instance_id` is diagnostic metadata;
it is not fencing authority.

Session creation and first authority acquisition are one lifecycle operation.
`create_session_with_initial_fence` mints a server-generated,
non-caller-selectable UUID plus random create `operation_id` and `lease_token`,
sets `operation_kind=create`, records the non-null diagnostic
`owner_instance_id`, epoch 1, and a non-null database-clock lease expiry, then
performs every session initialization write under that exact fence. In the same
transaction it releases the create fence before commit: `released_at` becomes
the database release time and `lease_expires_at` is set to that same time, while
the create operation ID/token/kind/owner remain non-null forensic fields. The
method returns only after commit, so creation can never expose or leak an active
lease. A row is active only when `released_at IS NULL` and its lease is live.

Callers cannot supply, restore, or select a session ID. A primary-key collision
causes the whole transaction to retry with a newly generated ID; no existing
row is adopted. The first later operation locks the inactive epoch-1 row,
advances to epoch 2, mints a new operation ID/token, writes the new closed kind,
owner and lease expiry, and clears `released_at`. Every later acquisition does
the same monotonic advance, including after a release or expiry. Release keeps
all authority columns non-null and makes the row inactive. A non-expired active
holder causes a conflict. An expired active holder may be replaced only after
the owning instance lease has also expired.

The current archive operation may physically delete an eligible session. It
must hold the current `archive` fence and atomically delete the session plus its
operation-fence row through the schema-owned cascade. There is no permanent
deleted-ID table, denylist, or promise that a random UUID can never recur.
Safety instead follows from non-caller-selectable creation and strict write
shapes: acquire, renew, mutate, release, and stale retry paths are update-only.
A stale CAS against a missing parent therefore changes zero rows and cannot
recreate either row. A soft-retained session keeps its fence for the same
retention period as the parent.

Every durable mutation in those flows compare-and-swaps exactly
`session_id + operation_id + lease_token + operation_epoch`. The owner renews
the fence while work continues and writes its final state before release. A
stale or suspended worker receives `SessionOperationFenceLost` and performs no
progress, proposal, archive, execution, or fallback write. Changing only
`owner_instance_id` can never authorize a mutation.

Composer progress keeps one bounded latest-snapshot row per session. Individual
in-flight request rows expire, but their creation, update, and completion still
require the current operation fence. Reads and WebSocket replay use durable
state and do not depend on a process-local lock or broadcaster.

### SQLite local authority

The web/session service depends on one `SessionOperationAuthority` interface in
both database modes. PostgreSQL supplies the renewable distributed adapter.
SQLite supplies a `SQLiteLocalSessionOperationAuthority` with the same acquire,
renew, compare-and-swap, release, archive-delete, and fence-lost signatures.

The SQLite adapter combines the existing process/file session lock with a
table-backed operation epoch and random lease token. It performs exact CAS
inside the same SQLite transaction as each mutation. Because supported SQLite
profiles run one web process, the adapter has no instance-membership heartbeat,
lease takeover, or peer recovery. The absence of distributed membership never
makes session mutation authority optional, and shared service code contains no
`if sqlite: skip fence` arm.

### Sessions-database mutation inventory

An architecture test classifies every sessions-database mutating call site as
either session-scoped or global. Session-scoped writes must receive and verify
the current `SessionOperationFence` in the same transaction. Global writers
must be explicitly named with their separate authority and may not accept a
session ID as an implicit bypass. The initial inventory includes session
service/routes and guided operations; composer service, progress,
`tutorial_service.py`, `sessions/routes/composer/state.py`,
`reviewed_source_authority.py`, and `composer/tools/sources.py`; blob writers;
audit-story/proposal reference writers; interpretation-event writers; and
execution/run projection writers. The gate walks the repository/AST, fails on
an unclassified mutator, and makes additions choose session-scoped fencing or
a reviewed global authority deliberately.

### Run ownership

The existing sessions `runs` row gains the durable values needed for:

```text
RunOwnershipFence(run_id, owner_instance_id, owner_epoch)
```

It also records the sessions-database-clock lease expiry, cancellation request
time, and a bounded non-secret cancellation source enum. Creation assigns epoch
1 in the same sessions-database transaction as the run-start intent. Takeover
uses the sessions database's current time, requires both the owning instance
and run lease to be expired, locks candidates with `FOR UPDATE SKIP LOCKED`,
and increments `owner_epoch`.

Every run projection, output link, blob finalization, and terminal update
compare-and-swaps the full fence. Landscape writes continue to use the engine's
existing `(run_id, worker_id, leader_epoch)` coordination token. Code acquires
authorities in this order to avoid deadlock:

```text
SessionOperationFence -> RunOwnershipFence -> Landscape coordination token
```

Cancellation-request insertion is the one deliberate exception: the endpoint
may atomically set the durable request without owning the run so cancellation
cannot be lost behind a dead owner. Reconciliation then follows the normal
authority order.

### Landscape clock and fenced mutation inventory

Every Landscape leadership acquisition, renewal, expiry, takeover, scheduler
lease, effect lease, fence verification, and stale-owner decision obtains time
from the Landscape database inside the transaction that decides it. Callers no
longer pass `datetime.now()` into those decisions. Sessions and Landscape
timestamps are forensic values only across the cross-database saga: code orders
cross-database work by monotonic saga states, tokens, and fingerprints, never by
comparing their timestamps.

An architecture test inventories every Landscape mutation reachable from a web
run: run lifecycle and status, graph registration, run coordination and worker
membership, rows/tokens/outcomes, node and routing state, scheduler queues,
leases, barriers and dispositions, batches/aggregations/coalesces, calls and
operations, sink effects/reservations/finalization, checkpoints, artifacts,
source completion, export, and terminal finalization. Every listed mutation
requires the current Landscape coordination token and opens a fenced
transaction whose first statement verifies that token. Adding an unclassified
mutating method or a web execution path to an unfenced repository call fails
the architecture gate.

The only exception to first-statement token verification is the named
`begin_run_with_baseline` creation transaction described below. It may create
an epoch-1 token only from a supplied durable typed `RunStartPermit` subject.
It validates and records that subject entirely inside Landscape; it never
queries, locks, updates, or otherwise participates in a Sessions transaction.
Exact retry of the same permit subject is idempotent; another permit, run, or
fingerprint is refused. No other Landscape method may mint a coordination
token.

### `ExecutionAuthorityCheck`

The engine injects one exact `ExecutionAuthorityCheck` into source iteration,
run-context creation, retry, transform, batch/aggregation, sink-effect,
audit-export effect, trigger, export, lifecycle, and cleanup executors. The AST
inventory includes `run_context_factory.py`, `executors/sink_effects.py`,
`orchestrator/audit_export_effects.py`, and `triggers.py` in addition to the
processor, retry, batch adapter, source iteration, transform/aggregation/sink
executors, and orchestration owners. For a web run it verifies the sessions
`RunOwnershipFence` using sessions-database time and the Landscape coordination
token using Landscape-database time. It requires a configured minimum lease
margin in each database but never compares the two absolute timestamps.

Immediately before every source `on_start`/`load`/iterator advance, transform
or aggregation lifecycle/process call, batch-adapter submission, sink/effect
publication, retry attempt, plugin preflight, `on_complete`, `close`, and other
plugin or effect callback, the engine (1) runs the authority check and then (2)
synchronously commits a closed `plugin_invocation_admitted` audit fact under
that same authority. If either step fails, the call is not invoked.

After a call returns or raises, and before any returned result, failure state,
or effect disposition can commit, the engine rechecks authority and
synchronously commits the corresponding closed post-call/result-admission
audit fact. Audit failure prevents result/disposition commit just as authority
loss does. Only after authoritative audit and the associated durable state
commit may derived success/failure telemetry emit. A stale process may release
framework-owned local resources, but it invokes no plugin lifecycle hook after
authority loss. A structural test enumerates these call/audit/commit sequences
and rejects a direct plugin call, result commit, or telemetry signal that
bypasses the order.

## Durable Run-Start Saga

Starting execution spans the sessions and Landscape databases, so it is an
explicit, restartable saga rather than two optimistic writes.

### Execution envelope

The first sessions-database transaction creates a stable run ID and immutable
execution envelope containing:

- serialized graph and non-secret frozen settings;
- application, plugin-registry, configuration, and graph fingerprints;
- source identities, immutable content digests, and completeness state;
- plugin capability/effect-safety classifications;
- deployment generation and compatibility key; and
- secret references with immutable `resolver_kind`, target identity, and
  resolver-version binding.

The sessions start intent, immutable envelope, run fence, and permit state
commit in the Sessions database. They are saga-joined to the Landscape
baseline, not cross-database atomic with it. Source completeness is recorded
honestly: incomplete sources are valid for a fresh run but set
`automatic_recovery_eligible: false`; reconciliation must never rewrite
historical completeness to manufacture recovery admission.

The persistence boundary rejects resolved values and known credential-bearing
fields. A versioned Key Vault reference binds vault, secret name, and immutable
secret version; a user-secret reference binds its durable row identity and
version; another resolver must provide an equivalent immutable version claim.
Fresh Key Vault binding captures the provider-returned version and later
recovery calls `get_secret(name, version)`; fresh-run caching is keyed by the
complete vault/name/version identity, never name alone. Automatic-recovery
resolution bypasses that value cache and revalidates the bound version against
the provider immediately before use. The recreated Sessions schema
stores a non-null positive monotonic version on each user-secret row. Upsert
advances it atomically; fresh binding returns row ID/version, while recovery
uses one atomic row-ID/version predicate and cannot read a replacement value
after rotation. Resolved values remain ephemeral and are never part of an
envelope or cache key.
The public secrets contract carries a persistence-safe `BoundSecretRef` through
`ResolvedSecret`, `WebSecretResolver.bind/resolve_exact`, `WebSecretService`,
the core secret-ref tree walk, and `ExecutionService`. It retains resolver kind,
redacted target identity, and immutable version without exposing the value.
No layer may reconstruct name/fingerprint-only metadata, call a private store
to bypass this carrier, or fall back from `resolve_exact` to name-only
resolution. Repr, logs, diagnostics, and APIs redact target/version fields that
can reveal tenancy.
An environment-variable name alone is unversioned and makes automatic recovery
ineligible unless its resolver can prove the same deployment-secret version as
the envelope. A value fingerprint observed later does not retroactively make an
unversioned reference safe.

Recovery validates the immutable resolver identity/version during metadata-only
admission, then resolves that exact reference immediately before the owning
plugin call. Immutable-version continuity is explicit: creating a newer Key
Vault version does not invalidate an enabled/readable bound older version, and
recovery never falls forward to latest. Disabling, deleting, or making the bound
version unavailable between admission and invocation fails closed because the
recovery path bypasses/revalidates the cache. Updating a user secret advances
its row version and fails the exact row/version predicate; the user store never
falls back to name-only.
The authority check runs after resolution and immediately before invocation.
Diagnostic rendering redacts the reference target, resolver metadata that can
reveal tenancy, and resolved value.

### Typed run-start permits

`RunStartPermit` is a closed tagged union with exactly two variants:

- `WebRunStartPermit` is issued and durably persisted by Sessions. A Sessions
  CAS on the start-intent row is the sole start-versus-cancel linearization:
  `pending -> start_permitted` creates the permit; `pending ->
  cancelled_before_permit` creates no permit. Issuance verifies the current
  `SessionOperationFence` and `RunOwnershipFence` in that Sessions transaction.
  The permit stores a stable permit ID, run ID, monotonic permit epoch, fence
  epochs/identities (not raw lease tokens), envelope/topology/source/checkpoint
  subject hashes, generation, compatibility key, and canonical permit-subject
  hash. Its immutable subject and `start_permitted` state persist and can be
  rehydrated exactly after process death.
- `LocalRunStartPermit` is explicit for CLI/direct single-owner starts. It
  stores a stable local permit ID/run ID, local-owner identity, immutable
  envelope/topology/source/checkpoint subject hashes, and canonical subject
  hash. It asserts no Sessions authority and cannot be used for a web run.

After a `WebRunStartPermit` is issued, a later cancellation request does not
revoke or mutate its immutable subject. It records a separate durable cancel
request. Start winning therefore means **eventual baseline materialization**,
not that a Landscape baseline already exists. A crash after permit issuance and
before Landscape creation is the explicit `start_permit_issued` saga state.
Reconciliation rehydrates that exact permit and completes the Landscape step.

### Atomic Landscape baseline

Landscape replaces `begin_run` with one idempotent
`begin_run_with_baseline` transaction. It is the sole exception to the rule
that a Landscape mutation's first statement verifies an existing coordination
token. It accepts a serialized `WebRunStartPermit` or `LocalRunStartPermit`,
validates the closed variant and canonical immutable subject without reading or
mutating Sessions, and atomically:

1. records the exact permit variant, ID, canonical subject and hash;
2. creates the Landscape run;
3. creates and returns its epoch-1 coordination token; and
4. writes the sequence-0 audit baseline.

The transaction commits all three rows or none. A Landscape run can never exist
without sequence 0. This applies to every fresh CLI, web, and direct repository
run, including checkpoint-disabled runs. For a disabled run, sequence 0 records
topology, envelope/fingerprint, source manifest posture, and
`automatic_recovery_eligible: false`; disabling checkpoints suppresses only
later periodic checkpoints and automatic recovery, never the audit baseline.

Exact retry with the same permit kind, permit ID, run ID, canonical subject
hash, envelope fingerprint, and baseline inputs returns the existing atomic
bundle. Reusing a permit for another run or changing any subject/input fails
closed. A Local permit retry follows the same rule. No other method can create
an epoch-1 token. There is no durable or observable Landscape run-created state
without a baseline. The later
`CheckpointCoordinator.checkpoint_run_start` seam is removed, and fresh-run
execution cannot write another sequence-0 row. `RunLifecycleCoordinator`, CLI,
web, tests/fixtures, and every direct `RecorderFactory.run_lifecycle` caller use
`begin_run_with_baseline`; an architecture test rejects the old `begin_run` or
late run-start checkpoint call.

### Saga states and transitions

```text
draft
  -> start_intent
  -> start_permit_issued
  -> baseline_checkpointed
  -> running
  -> terminal

start_permit_issued | baseline_checkpointed | running
  -> recovery_required

start_intent -> terminal_cancelled

start_permit_issued | baseline_checkpointed | running
  -> cancel_pending -> terminal_cancelled
```

Transitions are idempotent and monotonic:

1. `start_intent` durably records the envelope and run ownership. The Sessions
   CAS either cancels before permit issuance or issues/persists one exact
   `WebRunStartPermit` under current operation/run fences. This is not atomic
   with Landscape; the saga joins the databases.
2. `start_permit_issued` is durable even when no Landscape row exists. The
   permit can be rehydrated exactly by any authorized reconciler after a crash.
3. `begin_run_with_baseline` validates/records the supplied permit subject and
   creates or finds the Landscape run, epoch-1
   coordination row, and sequence-0 audit baseline in one Landscape
   transaction, regardless of later-checkpoint posture. It performs no Sessions
   operation.
4. `baseline_checkpointed` records in the sessions database that the atomic
   Landscape bundle exists. It preserves the envelope's actual source-
   completeness posture; incomplete sources remain a valid fresh-run state but
   are recovery-ineligible. A crash after the Landscape commit but before this
   transition leaves `start_permit_issued`; reconciliation verifies the matching
   atomic bundle and advances the same saga.
5. `running` is reachable only after both databases durably reflect the same
   run identity and the baseline exists.
6. `terminal` reconciles Landscape outcome into the sessions projection under
   both fences.

Crash injection tests stop the process before and after
`begin_run_with_baseline` and every sessions transition. They prove that a
rollback leaves no Landscape run, a committed Landscape run always has its
epoch-1 coordination row and sequence-0 checkpoint, and reconciliation either
advances the same saga once or records `recovery_required`. It never creates a
duplicate Landscape run or invokes a plugin from an incomplete envelope.

Cancellation and start race by compare-and-swap on the same Sessions start-
intent state. If cancellation wins `pending -> cancelled_before_permit`, no
permit exists and no Landscape run is created. If start wins `pending ->
start_permitted`, the durable permit exists but the baseline may not yet. A
later cancellation records `cancel_pending`; the reconciler must rehydrate the
permit, call `begin_run_with_baseline` if necessary, then use the epoch-1 token
to terminal-cancel with zero plugin calls. Exact retries observe the same CAS
winner and Landscape bundle. Thus start-won always converges to one baseline,
including a crash before Landscape, but never implies an immediate baseline.

## Recovery and Cancellation

### Automatic recovery admission

After lease expiry, a claimant may take ownership but must evaluate recovery
from durable metadata without instantiating or calling a plugin. Automatic
recovery is allowed only when all conditions are true:

1. the current application, plugin registry, graph, non-secret configuration,
   immutable resolver versions, deployment generation, and compatibility key
   produce the envelope's exact execution fingerprint;
2. the sequence-0 baseline exists, passes its integrity check, and marks later
   checkpoint/recovery support enabled;
3. every source is complete, immutable for this run, and matches its digest;
4. every remaining operation is classified effect-safe by a closed,
   fail-closed policy; and
5. the claimant has both the new `RunOwnershipFence` and Landscape leadership
   token.

Equal compatibility alone never admits recovery across revisions. Unknown
capability, missing metadata, execution-fingerprint or generation drift,
unversioned/unmatched secret resolution, incomplete source, disabled later
checkpoints, missing baseline, or an external effect without enforced
idempotency makes the run ineligible. The claimant synchronously persists
`recovery_required` with a bounded reason enum before emitting telemetry and
makes zero plugin calls. An operator may then cancel or use the existing
explicit recovery workflow after resolving the condition; automatic recovery
never guesses.

Before each plugin boundary, the worker rechecks its run lease and coordination
token. It starts no new plugin call when renewal is uncertain. A plugin call
already in progress may return after lease loss, but the stale worker cannot
commit its result. Because a new worker does not automatically replay an
effect-unsafe operation, takeover does not duplicate that external effect.

### Cancel-only reconciliation

The cancel endpoint atomically records `cancel_requested_at` regardless of the
receiving replica and CASes the Sessions start-intent state. Reconcilers always
inspect cancellation before recovery:

1. if cancellation wins before permit issuance, mark the saga
   `terminal_cancelled`; no permit or Landscape run exists;
2. if a web permit exists but Landscape does not, rehydrate the exact permit and
   materialize its atomic baseline with zero plugin calls;
3. once the matching Landscape epoch-1 bundle exists, acquire the run and
   Landscape fences and issue only the fenced post-baseline cancel operation;
4. keep `cancel_pending` until Landscape confirms a terminal state; and
5. project the terminal result into sessions state under the current fences.

The reconciler never calls resume in order to cancel and never executes a
plugin while cancellation is pending. A peer owner observes the durable request
on heartbeat; process-local events are latency optimizations only.

## Database Time, Retention, and Cleanup

All distributed time-to-live (TTL) creation, renewal, comparison, and expiry
uses the deciding database's time in the deciding transaction. The sessions
adapter obtains sessions PostgreSQL time for instance/run leases, operation
fences, WebSocket tickets, composer in-flight rows, rate-limit windows, and the
new cleanup claim. Landscape obtains Landscape database time for leadership,
workers, scheduler/effect leases, fencing, checkpoint/recovery decisions, and
its existing retention behavior, which is outside the new cleaner. Application
wall time is presentation-only. A timestamp
read from one database is never passed to the other database as an authority or
used in a cross-database expiry comparison.

WebSocket tickets store only SHA-256 digests and consume once atomically across
replicas. The PostgreSQL rate limiter stores HMAC-SHA256 subject digests, locks
one bucket, prunes its expired events, and inserts the next event in one
transaction. SQLite keeps the current in-memory ticket and limiter paths.

A periodic **Sessions-database-only** cleaner must hold both a bounded global
Sessions cleanup claim and row-level `FOR UPDATE SKIP LOCKED` claims in that
same database. It introduces no Landscape table or epoch change and does not
own Landscape retention. The global row has a
random token, monotonic epoch, database-clock expiry, maximum renewal count,
and maximum wall-independent batch count; acquiring it cannot create an
unbounded leader. Every deletion transaction verifies and renews that exact
claim as its first statement, then selects a configured row limit with
`SKIP LOCKED` under a short statement timeout.

Claim loss, expiry, epoch change, or a zero-row renewal raises
`CleanupClaimLost` before any delete. A suspended stale cleaner therefore
cannot resume deleting after a successor takes the claim. The cleaner prunes
only rows whose indexed timestamp is expired according to that same database:

- consumed/expired tickets;
- completed/expired composer in-flight rows;
- old stopped/expired instance rows;
- expired rate-limit events and empty inactive buckets; and
- terminal transient saga-control rows after the run's evidence retention
  window.

Cleanup never deletes execution envelopes, cancellation requests, active
leases, non-terminal saga/run state, any sequence-0 or later checkpoint, or
audit evidence. It never independently deletes a soft-retained
`SessionOperationFence`; only physical parent-session deletion under the
current archive fence may cascade it. There is no deleted-identity table.
Global expiry does not
recompute subject digests, so unique subjects and HMAC key rotation cannot
create permanently unreachable rows. Every claim, batch, and transaction has a
hard bound; later valid claimants continue remaining work.

## Audit Primacy and Leak-Safe Observability

The sessions audit is explicitly owned by the saga, reconciler,
`recovery_admission.py`, web execution service, sessions service/routes, and
coordination cleanup/lifecycle paths. It records run-start permit issuance and
saga, session/run fence acquire/renew/release/loss, cancellation request and
reconciliation, cleanup claim, and `recovery_required` transitions. Landscape
audit owners include the run-context factory, source/processor/retry/batch and
transform/aggregation/sink executors, sink effects, audit-export effects,
triggers, orchestration lifecycle/export/cleanup, and existing coordination
repositories. They record leadership acquire/renew/takeover/refusal, worker
eviction, execution-authority admission/loss, checkpoint posture, recovery
admission, plugin/effect boundary, and terminal reconciliation. Architecture
tests reject an authoritative transition without its synchronous audit owner.
These facts commit in their authoritative database before corresponding
metrics or logs.

Plugin/effect audit admission is part of execution authority, not eventual
observability. The mandatory order at every invocation is:

```text
authority check
  -> synchronous invocation-admitted audit commit
  -> plugin/effect invocation
  -> authority check after return/raise
  -> synchronous post-call/result-admission audit commit
  -> returned-result/failure/effect-disposition commit
  -> derived telemetry
```

Failure of the pre-invocation audit commit means the plugin/effect is never
called. Failure of the post-call audit commit means its result or disposition
is never committed. A raised call is also synchronously audited before retry,
terminal handling, or derived telemetry. The audit transaction is fence-bound
in its authoritative database; no asynchronous exporter or log can satisfy the
admission requirement.

Telemetry is derived evidence. An exporter failure cannot roll back, replace,
or suppress an already committed audit fact, and recovery/cancellation does not
proceed merely because a metric emitted. If the audit write itself fails, the
operation fails closed; only then may code emit one bounded, low-cardinality
emergency log without subject identifiers. If audit succeeds but telemetry
infrastructure fails, code keeps the audit result and emits at most one bounded
exporter-health log. Ordinary successful and refused paths do not use logs as a
substitute for audit.

After the mandatory audit invariant lands, a separate compatible runtime delta
adds the closed telemetry/exporter surface. The existing authenticated
Prometheus surface exposes low-cardinality metrics
for:

- membership heartbeat success/failure and incompatible-key readiness;
- operation/run fence acquisition, renewal, takeover, and fence loss;
- run-start saga state and reconciliation outcomes;
- automatic-recovery admission and `recovery_required` reason;
- cancellation request and cancel-only reconciliation;
- cleanup rows and duration by a closed table-family label; and
- active, draining, stopped, and expired instance counts.

Structured logs use closed event/reason enums and deployment target only. They
must not include usernames, user IDs, client addresses, session/run IDs,
ticket or subject digests, database URLs, secret references, resolved secrets,
payload content, or exception text that can contain those values. Tests capture
logs and metric labels for failure paths and reject high-cardinality or secret
material.

## Reproducible Multi-Architecture Image Contract

The Docker frontend builder uses the multi-architecture index
`node:24.18.0-bookworm-slim@sha256:6f7b03f7c2c8e2e784dcf9295400527b9b1270fd37b7e9a7285cf83b6951452d`
and installs/verifies npm `11.6.2`. The Dockerfile never copies the repository
`README.md`. It creates a deterministic build-only README stub before Python
package installation, sufficient for packaging metadata but independent of
public claim text. Node/npm and workflow-contract tests pin this behavior so a
post-acceptance README promotion cannot change image bytes.

The acceptance candidate is one all-extras OCI index built once for
`linux/amd64,linux/arm64` and pushed by that build to the exact preflighted GHCR
and ACR repositories. Both repositories must resolve the same index digest and
the receipt binds that digest, each amd64/arm64 child digest, OCI source
revision, `io.elspeth.install-extras=all`, and representative plugin import
smoke results. QEMU executes the arm64 smoke; live ACA executes the amd64
child. A lean amd64-only candidate is not eligible.

The build-input manifest covers Dockerfile, `.dockerignore`, admitted source,
frontend lockfiles, `pyproject.toml`, `uv.lock`, `elspeth-lints`, pinned base/uv
images, target platforms, BuildKit/buildx, and build arguments. Host
`README.md` is deliberately excluded and contract tests prove every Task 24
post-acceptance path is disjoint from image inputs.

## Kubernetes Bundle

`deploy/kubernetes/base/` contains a provider-neutral Kustomize base with:

- one `Deployment`, `strategy: Recreate`, and `replicas: 1`;
- one ClusterIP `Service` on port 8451;
- non-secret runtime/composer configuration;
- a persistent-volume claim for data, blob, and payload paths;
- a documentation-only Secret example excluded from `kustomization.yaml`;
- an immutable GHCR image example;
- `WEB_CONCURRENCY=1`, target `kubernetes`, and external PostgreSQL state;
- secret-key references for both database URLs and application keys;
- liveness `/api/health` and readiness `/api/ready`; and
- non-root UID/GID 1654 with a restrictive PVC initialization step.

The base does not install PostgreSQL, ingress, TLS, a storage class, a database
operator, or cloud identity controllers. Operators supply those dependencies.
The shipped profile has no rollout overlap because `Recreate` is the supported
strategy.

Static CI renders the base with a version- and checksum-pinned `kubectl`. A
Docker-backed kind lane builds and loads the final ELSPETH image, provisions
PostgreSQL only as harness infrastructure, initializes separate sessions and
Landscape databases, applies a test overlay, and proves readiness plus a
provider-free run. Harness PostgreSQL never appears in the shipped base.

## Azure Container Apps Bundle

`deploy/azure-container-apps/main.bicep` is workload-only. It assumes the
operator has already created:

- a custom-VNet ACA environment;
- a user-assigned managed identity;
- Key Vault secrets;
- Azure Database for PostgreSQL with separate logical databases;
- NFS Azure Files environment storage; and
- a generation-specific `elspeth` subtree with UID/GID 1654 and mode `0700`.

The module uses an immutable ACR image, Key Vault references, explicit
target/state values, ACA multiple-revision mode, one active/100%-traffic
revision at steady state, one replica per active revision, one web process,
NFS-mounted data/payload paths, and standard health/readiness probes. It does
not create a database, place credentials in parameter files, repair permissions
from a privileged application container, or treat `maxReplicas: 1` as a
zero-overlap guarantee.

The module accepts the deployment generation and all compatibility-key members
explicitly. Equal-key revisions may use transient overlap. Any epoch or protocol
change uses the maintenance cutover and fresh database roles, secrets, and NFS
subpath described above.

CI compiles the Bicep module and inert example parameters with a version- and
checksum-pinned Bicep CLI. Source-contract tests reject mutable images,
single-revision mode, a steady state other than one active revision at 100%
traffic, credential parameters, excess replicas/processes, missing NFS
generation, and an overlap claim not tied to exact generation and
compatibility-key equality.

## ACA Provider Acceptance Artifacts

ACA acceptance has one named implementation surface and one evidence contract:

- packaged validator:
  `src/elspeth/web/azure_container_apps_acceptance.py`;
- private scenario engine: `src/elspeth/web/_azure_container_apps_acceptance/`;
- operator driver: `scripts/acceptance/azure-container-apps.sh`;
- receipt schema:
  `deploy/azure-container-apps/acceptance-evidence.schema.json`;
- tracked sanitized receipt:
  `docs/operator/evidence/azure-container-apps/0.7.2.json`; and
- exact receipt reference plus SHA-256 in
  `deploy/platforms/azure-container-apps.yaml`.

The ACA receipt uses schema identifier
`elspeth.aca-provider-acceptance.v1`. Provider-neutral canonical hashing,
closed-field projection, immutable-subject, and redaction primitives live in
`src/elspeth/web/acceptance_common.py`. The implementation extracts only those
generic helpers from the AWS private contracts with unchanged AWS regression
behavior; ACA never imports AWS-private modules, duplicates the AWS receipt
schema, or claims that it describes ACA resources or Azure Files behavior.

`azure_container_apps_acceptance.py` is a stable, side-effect-free import and
validation facade. Provider clients, scenario transitions, control-ledger
mutation, sanitization, cleanup, and receipt assembly live in the private
`_azure_container_apps_acceptance/` package. A permanent architecture test
enforces complete private-module inventory, one-way private layering,
acyclicity, no private-to-facade imports, and no provider calls or domain logic
in the facade. The shell script is a thin invocation boundary, not a second
scenario implementation.

### Durable operator control ledger

The driver requires an external control-ledger path in addition to the
authority file. The ledger is a regular, non-symlink file owned by the invoking
user with exact mode `0600`, located outside the worktree. Each update writes a
same-directory mode-`0600` temporary file, fsyncs it, atomically renames it over
the ledger, and fsyncs the directory. A shell `trap` requests best-effort
cleanup, but only this ledger proves work remains; trap success is never the
cleanup authority.

Before the first provider mutation, the ledger records deterministic intended
identities and immutable subjects for the complete run:

- separate exact ARM container IDs for the resource group, managed environment,
  container app, identity, Key Vault, storage account/share, and every
  disposable provider resource;
- a deterministic revision suffix and index/amd64-child/OCI revision binding;
- separate run-scoped logical database-name and database-role prefixes;
- for each disposable credential, one unique run-scoped Key Vault secret name,
  scenario nonce, and owner tag (no version exists or is preclaimed yet); for
  prerequisite credentials, the exact existing read-only version or exact
  version whose enabled state may be disabled/restored;
- the live vault's soft-delete retention and purge-protection posture, whether
  purge of run-owned disposable names is expressly authorized, and the exact
  terminal cleanup state allowed by that posture;
- the normalized NFS run root beneath the authorized share; and
- generation, compatibility key, scenario nonce, receipt subject hashes, and
  cleanup deadline.

The validator rejects a prefix where an exact ARM ID is required, overlapping
database and role prefixes, a Key Vault reference outside the declared secret
scope, an absolute/parent-traversing NFS child, a symlink component, or a
resolved NFS path outside the normalized run root. Creation and cleanup use the
pre-recorded name/scope; they never invent a replacement name. Those contained-
object limits come from the operator authority file, not the driver ledger:
the ledger may only derive a database name, role, disposable secret name, and
NFS child within the separately authorized prefixes/root and cannot broaden or
replace those limits.

Disposable secret mutation uses an exact dedicated acceptance-only Key Vault,
distinct from any read-only prerequisite vault. Before the absence check the
operator authority grants and attests an exclusive-writer window through the
cleanup deadline; live preflight proves that the acceptance identity is the
only data-plane principal with set/delete/recover/purge authority and that no
access policy, group, or other assignment grants those verbs. The operator
also freezes control-plane role/access-policy changes for that window. If
exclusive custody cannot be enumerated, proved, or retained, the scenario
stops and performs no whole-name mutation. A name prefix alone is explicitly
not concurrency authority.

While that exclusive-writer custody is live, preflight proves the intended
disposable name is absent from both active and soft-deleted inventories. The
provider's check and `set` are not claimed to be atomic; exclusive custody is
the reservation that excludes a competing writer in the gap. Any pre-existing
collision blocks without recovery, purge, or mutation. `set` then chooses the disposable secret version. After the set succeeds,
the ledger adopts the single provider-returned version and records it with the
observed nonce/owner tags. On timeout or disconnect, reconciliation lists
the entire active version inventory for the pre-recorded name, requires it to
be a singleton equal to the adopted/matching nonce+owner version, and requires
no soft-deleted collision. Zero, multiple, or any unmatched version is
ambiguous and blocks; the driver never guesses or retries another set.
Immediately before whole-name delete, cleanup revalidates exclusive custody and
repeats that singleton all-versions ownership proof. Custody drift or an
unmatched version blocks deletion; an external concurrent writer is a breached
operator authority condition, never a race the delete API can fence.
Azure deletion, recovery, and
purge operate on the whole secret name/all versions, so cleanup authority for a
disposable credential is the unique name scope, not an individual version.
Existing prerequisite secret versions are read-only or may undergo only an
explicit exact-version disable/restore; they are never deleted, recovered, or
purged.

Preflight reads and records `softDeleteRetentionInDays`, purge protection, and
the caller's purge permission. Deleting a disposable unique name first moves
the whole name/all versions to Key Vault's soft-deleted namespace. If purge is
expressly authorized, the caller has the permission, and purge protection does
not prohibit it, cleanup purges the exact owned name and proves it absent from
both active and deleted inventories. Otherwise the exact owned soft-deleted
tombstone is the accepted terminal cleanup state until provider retention
expires; the ledger and receipt record that state, retention posture, adopted
version, nonce, and owner evidence. Unique future runs never reuse that name.
Cleanup never recovers or purges a name unless the ledger proves it is the
run-owned disposable subject, and it never treats a same-named non-owned
tombstone as success.

Every scenario step records `intended`, then `action_started`, then a live
`postcondition_observed` result. On timeout, disconnect, or another ambiguous
provider failure, the driver reads the exact intended ARM/database/role/secret
or NFS identity and reconciles its postcondition before retrying. `--resume`
continues the first incomplete intended step. `--cleanup-only` performs no
acceptance scenario and repeatedly reconciles only ledger-owned disposable
subjects to their absent/restored postcondition or the exact authorized owned
soft-deleted Key Vault tombstone terminal. Either mode refuses a ledger,
authority, image, generation, or subject-hash mismatch.

ARM authority and contained-object authority remain distinct. An exact ARM
container ID does not authorize every object beneath it: database and role
creation/deletion are limited to disjoint run prefixes, disposable Key Vault
cleanup to pre-recorded unique name scope/all versions with either verified
purge or an exact owned soft-deleted terminal tombstone, prerequisite Key Vault
changes to exact-version disable/restore only, and NFS operations to the
normalized run subtree. Preflight records
outside-scope baselines for each container; cleanup proves those baselines are
unchanged as well as proving run-owned subjects absent, restored, or—only for
the authorized disposable Key Vault scope—the exact owned soft-deleted
tombstone terminal.

### Non-production authority

The operator invokes the driver with an authority file outside Git:

```bash
scripts/acceptance/azure-container-apps.sh \
  --authority-file /secure/aca-authority.json \
  --control-ledger /secure/aca-control-ledger.json
```

The driver and validator refuse to start unless the authority file:

- is a regular, non-symlink file owned by the invoking user with exact mode
  `0600`;
- declares `environment_class: non-production` and
  `destructive_cleanup_authorized: true`;
- allowlists the Azure tenant, subscription, exact resource-group and managed-
  environment resource IDs, a run-scoped container-app name prefix, and the
  operator-supplied PostgreSQL, storage, identity, and Key Vault prerequisites;
- separately grants disjoint run-scoped database-name and database-role
  prefixes, the exact dedicated acceptance-only mutation-vault ARM ID, its
  disposable secret-name prefix plus purge-or-tombstone policy and sole writer
  principal, and an exact normalized NFS run root beneath the authorized share;
- attests a control-plane change freeze and exclusive data-plane writer window
  for that mutation vault through the cleanup deadline; preflight must enumerate
  and reject any competing set/delete/recover/purge principal or uninspectable
  access policy/role assignment;
- requires the resource group and managed environment to carry live tags
  `elspeth.environment=acceptance` and `elspeth.production=false`;
- distinguishes immutable operator prerequisites from disposable resources
  that this acceptance run may create and delete; and
- gives the external mode-`0600` control-ledger path, cleanup deadline, and
  raw-evidence directory outside the repository.

The dedicated mutation vault is not the resolver for operator prerequisites;
any database-admin Key Vault reference is an exact version in a separate read-
only prerequisite scope. The authority file contains identifiers and limits,
not credentials. Database-
admin authority is a versioned non-secret resolver descriptor: either Azure AD
token acquisition metadata or an exact Key Vault secret name and immutable
version. No token, password, connection value, or resolved secret appears in
authority, ledger, evidence, logs, or receipt. Database work uses
`postgres:17.6-bookworm@sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3`
(amd64 child
`sha256:45cd22f8d32e189d245403954882f88e7a8714301fda80dab6da90f1265b25a3`)
as the only `psql` client. A read-only preflight resolves credentials only into
the process boundary, proves the exact database privileges, and discards them.

Azure commands use an ephemeral mode-`0700` copy of the operator's Azure CLI
configuration mounted into the pinned Azure CLI container. The copy may refresh
tokens during bounded execution, is never the source config, and is destroyed
after the run. Before each provider mutation, the driver compares the live
Azure subject to the allowlist and fails closed on a
mismatch. Every created resource receives the acceptance-run ID and owner tags.
Cleanup uses only the exact resource IDs returned by creation; it never deletes,
retags, or replaces the operator-supplied databases, storage account/share,
managed environment, VNet, identity, or Key Vault. The receipt records the
authority-file SHA-256 but never its path or contents.

### Closed scenario set

The schema accepts exactly these scenario IDs, each once and with result
`passed`:

1. `authority-preflight`;
2. `immutable-subject-binding`;
3. `startup-readiness-provider-free-run`;
4. `equal-key-overlap-handoff`;
5. `peer-cancellation`;
6. `websocket-ticket-handoff`;
7. `composer-progress-handoff`;
8. `maintenance-prewarm`;
9. `sigstop-stale-owner-refusal`;
10. `sigkill-owner-takeover`;
11. `azure-files-cross-revision-visibility`;
12. `azure-files-atomic-replace-contention`;
13. `azure-files-crash-before-replace`;
14. `azure-files-crash-after-replace`;
15. `azure-files-tombstone-delete-recovery`;
16. `global-coordination-retention`;
17. `runtime-telemetry-and-redaction`;
18. `incompatible-key-readiness-refusal`;
19. `maintenance-cutover-forward-only`; and
20. `authority-scoped-cleanup-and-baseline-restore`.

Together they prove startup, durable replay, equal-key overlapping revisions,
peer cancellation, cross-replica ticket consumption and composer progress,
and stale-owner write refusal. `sigstop-stale-owner-refusal` performs
`SIGSTOP`, lease expiry, takeover, and `SIGCONT`; the resumed worker is denied
before any durable write. `sigkill-owner-takeover` kills the owner permanently
and expects only its replacement to recover.

The SIGSTOP lane uses an acceptance-only probe and lease profile bound to the
authority nonce and exact candidate revision. Its liveness failure threshold is
longer than the sessions-database lease expiry plus the configured takeover and
observation budgets; readiness fails promptly so the stopped revision receives
no traffic, but ACA cannot restart or terminate it before takeover and the
subsequent `SIGCONT`. The driver reads back the live probe settings, records
the database-derived lease/takeover observations and configured timing values,
and rejects a run that does not satisfy that inequality. The profile is
unavailable without non-production acceptance authority, cannot be selected by
the public deployment profile, and is removed before the candidate returns to
steady state. Tests prove both its scenario-bound availability and its
fail-closed production absence.

The five `azure-files-*` scenarios use the real allowlisted Azure Files NFS
mount. They prove cross-revision identity and content visibility, exactly one
winner under atomic-replace contention, classification and safe retry before
replace, no duplicate publication after replace, and idempotent tombstone
deletion recovery. Restart reconciliation must present no partial blob as
complete, duplicate no finalization, and bound every orphan. Local filesystems,
emulators, and mocked storage cannot satisfy them.

`global-coordination-retention` seeds fresh and expired Sessions rows, runs the
new bounded Sessions cleaner concurrently from both revisions, proves that only
expired transient Sessions rows disappear while run/audit facts remain, and
observes backlog convergence. It also proves Landscape's existing retention is
unchanged and no Landscape cleanup table or epoch change was introduced.
`runtime-telemetry-and-redaction` checks authenticated metrics and sanitized
logs for scenario-specific deltas while high-entropy content, ticket, and
operator-input canaries occur zero times in every committed or displayed
artifact.

`maintenance-cutover-forward-only` uses disposable databases and a fresh NFS
generation and proves that revoked old roles cannot reconnect.
`authority-scoped-cleanup-and-baseline-restore` invokes ledger reconciliation
on the normal path and proves that `--cleanup-only` can finish an interrupted
run. A shell trap merely requests the same best-effort cleanup. The scenario
passes only after live reads show every exact disposable resource ID absent,
every temporarily changed revision-mode or traffic postcondition restored, and
no prerequisite or out-of-authority resource changed.

### Raw and sanitized evidence

The driver writes raw Azure responses, process logs, and crash artifacts only
to the mode-`0700` directory named by the authority file. That directory must
resolve outside the Git worktree. Raw evidence is never staged, embedded, or
hashed by path into a tracked file. After successful validation, the operator
moves any retained raw bundle to an access-controlled evidence store and the
driver deletes its local temporary copy.

The packaged validator creates the tracked candidate from a strict allowlist of
non-secret fields and closed reason enums. It rejects database URLs, secret
references or values, tickets, subject digests, user/session/run identifiers,
client addresses, raw exception text, and unrecognized fields. A repository
test scans the sanitized receipt and verifies that cleanup passed before the
receipt can be accepted.

## Immutable ACA Acceptance Receipt

Provider acceptance occurs in this order:

```text
implementation
  -> independent code review
  -> repairs
  -> clean complete agent-merge gate
  -> clean complete operator-release gate with current signed judgments
  -> final immutable candidate image build
  -> ACA deployment and acceptance
  -> receipt generation and validation
  -> evidence/documentation-only maintained claim
  -> promote the accepted index without rebuilding
```

The ACA-specific receipt binds these immutable subjects:

- reviewed source commit and a canonical implementation-subject tree hash. Its
  manifest includes all image inputs and all receipt-bound deployment,
  workflow, validator, scenario, schema, test, and runbook-contract files, and
  excludes only the receipt plus its receipt-derived profile/support/changelog
  projections;
- the all-extras multi-architecture candidate index digest as resolved in both
  preflighted GHCR and ACR repositories, its exact amd64 and arm64 child
  digests, required `org.opencontainers.image.revision` label equal to the
  reviewed source commit, and `io.elspeth.install-extras=all` label;
- a canonical build-input manifest covering `Dockerfile`, `.dockerignore`,
  `src/**` as admitted by that ignore file, `pyproject.toml`, `uv.lock`,
  `elspeth-lints/**`, frontend lockfiles, the deterministic build-only README
  stub, `INSTALL_EXTRAS=all`, pinned base/uv images, both target platforms,
  BuildKit/buildx version, and build arguments. Host `README.md` is excluded;
- Bicep module and parameter hashes;
- validator, driver, evidence-schema, runbook-contract, and acceptance-test
  hashes;
- a canonical ACA profile-contract hash computed with only the receipt
  `reference` and `sha256` fields omitted, avoiding a self-reference cycle;
- deployment generation and full compatibility key;
- non-secret Azure resource identities and revision names;
- pinned tool versions, representative plugin import results, and QEMU arm64
  plus native amd64 smoke results;
- the exact closed scenario outcomes and redacted evidence digests;
- authority-file SHA-256 and successful cleanup outcome; and
- schema identifier `elspeth.aca-provider-acceptance.v1` plus database-clock
  acceptance time.

After generating and validating
`docs/operator/evidence/azure-container-apps/0.7.2.json`, the ACA profile stores
that exact repository path and the SHA-256 of its exact bytes. Cross-profile
tests recompute the receipt SHA, recompute the canonical profile-contract hash,
and require both directions to match. The validator rejects missing or extra
scenarios, mutable images, hash drift, key mismatch, failed cleanup,
pre-review timestamps, and receipts from another source, artifact, authority,
or Azure subject set.

To avoid a receipt/profile cycle, receipt generation first renders the intended
final maintained profile in memory, hashes its canonical contract with only the
receipt `reference` and `sha256` values omitted, embeds that hash in the
receipt, and then writes the receipt plus the exact rendered profile. The
support-state field remains part of the hash; it is not an omitted or mutable
claim.

The receipt is generated from the exact index whose amd64 child is exercised by
every live scenario. The cached cosign `v3.0.6` binary signs each repository's
index and verifies it with the authority-bound certificate identity and OIDC
issuer. BuildKit attaches provenance and SBOM attestations to each platform
manifest, not to the top-level index. The packaged registry-artifact validator
therefore resolves both registries, rejects null or missing evidence, and
requires exactly one provenance statement and one SPDX SBOM statement whose
subject is the recorded amd64 child, plus the same pair for the recorded arm64
child. Provenance predicate type is a closed supported SLSA value
(`https://slsa.dev/provenance/v0.2` or
`https://slsa.dev/provenance/v1`); SBOM predicate type is exactly
`https://spdx.dev/Document`. It records the accepted SLSA version. Cosign
separately binds the index. Merely printing `imagetools inspect` output,
getting exit zero with `null`, or checking that some attestation exists is
insufficient. The release job
later promotes the already-present candidate digest **within each registry**
with a manifest-copy/tag operation. It performs no cross-registry copy and no
build, verifies both final tags resolve to the same accepted index digest, and
re-verifies signer identity, OCI labels, child digests, provenance, SBOM, and
build-input manifest. A main-branch image produced incidentally for the
evidence/documentation commit is ignored and is ineligible for the release tag.

After acceptance, the only permitted source-tree changes are the tracked
sanitized receipt, the ACA profile's receipt reference/SHA and support-state
claim, and receipt-derived public support/changelog text explicitly excluded
from the image and immutable receipt subject. `README.md` is legal only because
the Dockerfile's deterministic stub removed it from image inputs. A permanent
contract proves the complete closed allowlist is disjoint from the canonical
image-input manifest. The profile schema and all other
profile contract fields, release workflow, Bicep, validator, driver, private
scenario engine, authority/control-ledger rules, evidence schema, acceptance
tests, runbook contract, Docker inputs, and application code must already be
final before the candidate build. A change to any of those subjects, or to the
accepted image or its metadata, invalidates the receipt and restarts review,
candidate build, live acceptance, sanitization, cleanup, receipt generation,
and validation. The final evidence/documentation commit is therefore not an
excuse to rebuild or change the accepted runtime.

## Machine-Readable Profiles

`deploy/platforms/` contains JSON Schema plus exactly:

```text
docker-compose.yaml
linux-systemd.yaml
aws-ecs.yaml
azure-container-apps.yaml
kubernetes.yaml
```

Each profile records target, state modes, database ownership, required extras,
image delivery, payload storage, process/replica posture, compatibility key,
rollout posture, tracked artifacts, automated acceptance, provider receipt when
required, and authoritative runbook.

The ACA profile has exactly two legal support states. A release-candidate tree
has no receipt reference/SHA and says `release-candidate` everywhere in the
profile and public support documentation. A maintained tree says `maintained`
and has a provider-receipt object with the exact reference
`docs/operator/evidence/azure-container-apps/0.7.2.json` and the SHA-256 of that
file's exact bytes. The receipt independently binds the canonical ACA profile
contract, so the relationship is bidirectional. Absence, a malformed or stale
receipt, a path/SHA mismatch, or any other invalid binding can never support a
maintained claim. Once a maintained receipt is checked in, drift fails the
repository and release gates closed; it does not silently downgrade public
claims.

Cross-profile tests enforce:

- production recommendations never use compatibility mode `auto`;
- AWS, ACA, and Kubernetes require external PostgreSQL and the `postgres`
  extra;
- Compose alone ships a PostgreSQL sidecar;
- every tracked path exists and is known to Git;
- image examples are immutable or release-specific;
- every profile uses one web process and one steady-state replica;
- no profile claims horizontal scale;
- ACA alone claims `fenced-transient`, qualified as equal-key only and backed by
  a current validated receipt; and
- stop-before-start/Recreate profiles claim `none` for overlap.

Two complete contract fixtures exercise the legal state machine: receipt
absent/profile-and-docs release-candidate, and current bidirectionally bound
receipt/profile-and-docs maintained. Mutating any receipt subject, profile
contract field, reference, SHA, support state, or public support claim makes the
fixture fail. A malformed checked-in receipt is also a repository-integrity
failure even though it supplies no basis for anything beyond release-candidate
support.

## CI and Release Gates

Kubernetes render, kind smoke, PostgreSQL overlap, Bicep compile, profile
validation, and receipt-contract tests live in the unified
`.github/workflows/ci.yaml`. That workflow defines required jobs with IDs
`web-multi-instance`, `kubernetes-kind`, and
`azure-container-apps-bicep`; `ci-success.needs` names all three explicitly.
Do not implement them as standalone workflows whose result can bypass the
aggregator. Image publication continues to depend on successful `CI Success`.

The release process separates operator-owned candidate construction from
workflow-owned release publication. Before live ACA acceptance the Task 22
freeze step performs one all-extras
`linux/amd64,linux/arm64` build, records the complete Docker-input manifest,
applies the OCI revision/extras labels, and pushes the same candidate index to
the exact GHCR and ACR repositories. It signs and identity-verifies each
repository index, records both child digests, executes amd64 and QEMU arm64
smokes, and invokes the packaged fail-closed validator over the amd64/arm64
BuildKit provenance and SBOM subjects and predicates in both registries. After
the receipt and maintained docs land, the release workflow promotes each
registry's already-present candidate digest within that registry. The job
contains no build or cross-registry copy, rejects source/index/child/label or
signer mismatch, and proves both final tags resolve to the same accepted index
digest. Workflow and source-contract tests inspect this separation so a final
evidence/docs commit cannot rebuild or substitute a different image.

The repository-owned local gate has two explicit modes. Its default
**agent-merge** mode is a preparatory diagnostic gate, not authoritative merge
or source-freeze approval. It runs
Python 3.12 compatibility, the current pip-audit and license checks, and every
local gate, including trust-tier and trust-boundary scanners plus non-keyed
allowlist coverage/edit checks, a Wardline trust-surface assurance whose
boundary count and numeric coverage must be nonzero, a fail-on-error local-only
Wardline scan, and the Legis policy-boundary evidence check. A zero/null
Wardline assurance is an inert gate failure. It sets the repository's
`shape-only-when-key-missing` signature mode: findings, metadata shape, scope,
coverage, and forbidden edits still fail, while cryptographic signature
authenticity is the sole unverified property. `--operator-release` requires
`ELSPETH_JUDGE_METADATA_HMAC_KEY` and reruns the same scanner/coverage inventory
with signature verification `required`. The required-mode result is mandatory
before source freeze, again after receipt binding, and on the merged tree.
Shape-only results can never authorize merge. Neither mode claims that GitHub-
hosted contexts ran. The open P0 is therefore an operator-authenticity blocker
for freeze and merge, not a reason to skip trust scanners; it must be resolved
inside this plan before Task 22.

The generic unit invocation may continue excluding the `testcontainer` marker.
Every PostgreSQL, kind, or container-backed lane invokes its tests separately:

```bash
env -u VIRTUAL_ENV CI=1 uv run --frozen pytest -q -n 0 -m testcontainer <explicit paths>
```

The kind job installs and verifies its checksum-pinned `kind` and `kubectl`
tools before collection. Existing broad `tests/testcontainer/` collection must
not sweep a kind test into a runner without those prerequisites. Static
checksum-contract tests verify every published tool pin against the version
used by its workflow.

The full release gate includes:

- schema, state-transition, fence, saga, cancellation, cleanup, telemetry,
  rate-limit, ticket, and profile unit tests;
- SQLite single-process regression tests;
- real PostgreSQL and two-process tests;
- Kubernetes render and kind smoke;
- Bicep compile and source contracts;
- current Compose, Linux, AWS, image, lint, type, architecture, documentation,
  version-surface, non-inert Wardline, and Legis policy-boundary gates; and
- post-review live ACA/Azure Files acceptance plus receipt validation.

All schema-epoch surfaces, including integration expectations and every
release/runbook statement, move to the new epoch in the same release body.
A repository-wide contract test rejects stale prior-epoch operational guidance.

## Failure-Acceptance Matrix

| Scenario | Required result |
|---|---|
| Equal-key ACA overlap | Both instances become ready; fences serialize writes; old revision drains |
| Equal-key ACA rollout completion | Candidate is the sole active revision at 100% traffic; old revision is inactive at 0 traffic and 0 replicas; every rerun resumes from observed postconditions |
| Unequal compatibility key | Candidate is unready and obtains no fence |
| Session creation | `create` epoch-1 fence has non-null ID/token/owner/lease, guards all initialization, and is atomically released/inactive before return; first later operation advances to epoch 2 |
| Session operation release, idle period, and reacquisition | While the session exists, its fence remains; new random lease token and strictly greater epoch; stale CAS is refused |
| Session physical archive and stale retry | Current archive fence atomically deletes the session and cascades its fence; IDs are server-generated/non-caller-selectable and stale update-only CAS cannot recreate either row; no deleted-ID table exists |
| SQLite session operation | Same service signatures and local authority-table CAS; no membership/takeover, but no unfenced mutation path |
| `SIGKILL` owner | Replacement uses the same saga/run identity; killed process is never expected to resume |
| `SIGSTOP`, lease expiry, takeover, `SIGCONT` | Acceptance-only liveness budget exceeds lease plus takeover/observation budget; new owner advances epoch; resumed old worker receives fence loss before a durable write |
| Crash after web permit issuance before Landscape | Durable `start_permit_issued` is rehydrated; Landscape validates/records the permit without Sessions access and creates exactly one run/token/sequence-0 bundle |
| Crash before/after `begin_run_with_baseline` | Local or web typed permit exact retry is idempotent; rollback leaves no Landscape run and commit atomically records permit subject plus one run/token/sequence-0 baseline |
| Cancel races start permit | Cancel-before-permit wins: no permit and no Landscape run. Start-permit wins: later cancel materializes the eventual baseline if absent, terminal-cancels it, and makes zero plugin calls |
| Fresh run with checkpoints disabled | Same atomic sequence-0 baseline records disabled recovery posture; no later checkpoint and no automatic recovery; plugin execution remains valid |
| Missing checkpoint/source or fingerprint drift | Durable `recovery_required`; zero plugin calls |
| Equal compatibility key but image/generation/resolver drift | Durable `recovery_required`; exact execution fingerprint is required; zero plugin calls |
| Sessions clock and Landscape clock disagree | Each lease/fence decision uses only its own database clock; no cross-database timestamp comparison |
| Authority/audit around plugin/effect call | Pre-call authority plus synchronous admission audit precede invocation; post-call authority plus audit precede result/disposition; either failure refuses the next boundary and telemetry follows durable state |
| Effect-unsafe remaining work | Durable `recovery_required`; no automatic replay |
| Cancel before permit issuance | Terminal cancelled; no permit or Landscape run created |
| Cancel after permit but before Landscape | Reconciler rehydrates permit, materializes baseline solely to terminal-cancel, and invokes zero plugins |
| Cancel after atomic Landscape baseline | Cancel-only reconciliation; sequence 0 exists; no resume or plugin call |
| Azure Files crash at blob boundaries | No partial completed blob, duplicate finalization, or unbounded orphan |
| HMAC rotation and unique rate subjects | Expired rows remain globally cleanable in bounded batches |
| Sessions cleanup claimant is suspended and superseded | Lost Sessions claim is detected before delete; the stale cleaner refuses while the successor uses Sessions row claims plus `SKIP LOCKED`; Landscape schema/retention is unchanged and durable run/saga/checkpoint/cancel/audit facts remain |
| Epoch/protocol cutover | Maintenance ingress; old revision first reaches inactive, 0 traffic, and 0 replicas; then old roles disconnect and fresh databases, roles, versioned secrets, and NFS generation cross the forward-only boundary |
| Ambiguous ACA provider response | External mode-0600 ledger reconciles the exact intended ARM/database/role/Key-Vault/NFS identity; `--resume` or `--cleanup-only` never invents or broadens scope |
| Audit exporter failure | Authoritative audit fact remains committed; telemetry failure cannot erase it; only bounded infrastructure-health logging occurs |
| Audit admission write failure | Pre-call failure prevents invocation; post-call failure prevents result/disposition commit; no success telemetry emits |
| ACA release-candidate documentation state | No receipt binding; profile and every public claim say release-candidate; maintained release is refused |
| ACA maintained documentation state | Exact schema/scenario/subject hashes, accepted dual-registry index/children/OCI revision, bidirectional profile reference/SHA, redaction, and authority-scoped cleanup all pass |
| Candidate image construction | One all-extras amd64/arm64 index reaches exact GHCR+ACR with identical index/child bindings, QEMU arm64 and ACA amd64 smokes, identity-bound index signatures, and fail-closed provenance/SPDX subject validation for both children in both registries |

## Documentation Transition

Current documentation correctly says that Kubernetes is bring-your-own and ACA
is deferred/release-candidate. Change those claims only with the evidence they
describe:

- call Kubernetes maintained after the release-gated kind lane passes;
- call ACA maintained only after coordination, Bicep, real ACA/Azure Files,
  maintenance rehearsal, exact accepted-image promotion contract, and receipt
  gates pass; and
- make `deploy/platforms/` authoritative only after all referenced artifacts,
  tests, receipts, and runbooks exist.

The release-candidate and maintained states are whole-tree states, not prose
conventions. The profile, receipt binding, support matrix, runbook banner, and
release notes must agree. No/missing/invalid current receipt means there is no
basis for a maintained claim. A current receipt bound in both directions is
required for maintained; any later drift is a hard gate failure until new live
acceptance evidence is produced.

The docs continue to say that the application image contains PostgreSQL clients,
not a PostgreSQL server. Cloud and Kubernetes operators supply external
PostgreSQL and persistent payload storage.

## Non-Goals

- Running PostgreSQL in the ELSPETH application container.
- Shipping PostgreSQL in the Kubernetes base or ACA module.
- Supporting multiple steady-state replicas, multiple web processes per
  replica, or horizontal throughput scaling.
- Supporting mixed compatibility keys, rolling schema migrations,
  expand/contract epochs, or rollback after the maintenance cutover's
  irreversible boundary.
- Migrating pre-release sessions or Landscape databases; delete-and-recreate
  remains the policy.
- Automatically recovering a run with fingerprint drift, an incomplete source,
  no baseline checkpoint, unknown effect safety, or non-idempotent external
  effects.
- Persisting resolved secrets in the execution envelope, receipt, logs, or
  metrics.
- Adding Redis, a database operator, ingress, DNS, TLS, cloud networking,
  identity provisioning, or backup policy.
- Replacing or flattening the refactored AWS acceptance facade/package.
- Treating local filesystem tests as Azure Files NFS acceptance.
- Automatically deploying live AWS or Azure resources from ordinary CI.
- Creating document-signing ceremony; the immutable receipt protects an actual
  reviewed image, deployment, and provider acceptance result.
