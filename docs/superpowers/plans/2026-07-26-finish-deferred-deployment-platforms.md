# Finish Deferred Deployment Platforms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the deferred Kubernetes, Azure Container Apps, and machine-readable platform-profile work without relying on an unsafe zero-overlap assumption.

**Architecture:** Deliver Kubernetes independently, then make external-PostgreSQL web deployments safe under transient process overlap by adding database-clocked instance membership, epoch-fenced run ownership, durable cross-replica signals, and two-process acceptance tests. Build the workload-only ACA module on that proven runtime and publish platform profiles and support claims last.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, SQLite, pytest/testcontainers, Docker, Kustomize/kubectl, kind, Bicep, YAML/JSON Schema, GitHub Actions.

**Design spec:** `docs/superpowers/specs/2026-07-26-finish-deferred-deployment-platforms-design.md`

---

## Execution Contract

- Start from the current merged release branch in a fresh worktree; do not implement in the planning worktree.
- Commit this spec and plan before creating the implementation worktree so the exact plan-bearing commit is the new branch base.
- Use `.worktrees/finish-deferred-deployment-platforms` and branch `codex/finish-deferred-deployment-platforms`; do not recreate or reuse the retired `.worktrees/cross-platform-deployment-contract` worktree.
- Use test-driven development for every runtime and deployment contract change: add one focused failing test, observe the expected failure, implement the minimum behavior, then rerun the focused test.
- Do not cherry-pick `773fbd3bf`. It is reference material only; inspect it with `git show` and revalidate every ACA assumption.
- Keep the PostgreSQL server outside the application image and outside the shipped Kubernetes and ACA bundles.
- Preserve the SQLite single-process path. Distributed coordination is selected only for external-PostgreSQL deployments.
- Keep `WEB_CONCURRENCY=1`, one process per container, one steady-state replica, and `horizontal_scale_supported: false` throughout.
- Use database time for leases and CAS-match every durable run mutation on `(run_id, owner_instance_id, owner_epoch)`.
- Never weaken the existing Landscape `CoordinationToken` fence. Web ownership authorizes sessions-database projections; Landscape leadership authorizes engine writes.
- Do not publish a maintained ACA claim until the two-process corpus, Bicep compile gate, and operator acceptance all pass.
- Attribute tracker work with the implementation agent identity, but create/claim issues only when implementation actually begins.

## Dependency Graph

```text
Tasks 1-4: coordination contracts and persistence
                  |
                  v
Tasks 5-10: lifecycle, fencing, durable authorities, two-process corpus
                  |
                  +---------------------> Tasks 13-14: ACA bundle + acceptance

Tasks 11-12: Kubernetes base + kind smoke ------------------------------+
                                                                          |
Tasks 13-14: ACA bundle + acceptance -------------------------------------+
                                                                          v
                                                        Tasks 15-17: profiles, docs, closeout
```

Tasks 11-12 may run in parallel with Tasks 2-10. Task 13 must not start before Task 10 passes. Task 15 must not claim paths that do not yet exist.

## Verified Tool Pins

Recheck these official upstream values on the implementation date and update both workflow and documentation together if they have moved:

| Tool | Pin | Linux amd64 artifact/digest |
|---|---|---|
| Bicep | `v0.45.15` | `bicep-linux-x64` SHA-256 `ff5b194b042c220df4a50d6768ed1d6c39a32894bfdc4ff83d62b115d966a7ce` |
| kubectl | `v1.36.3` | SHA-256 `629d3f410e09bf49b64ae7079f7f0bda1191efed311f7d37fdbab0ad5b0ec2b7` |
| kind | `v0.32.0` | Linux amd64 SHA-256 `50030de23cf40a18505f20426f6a8506bedf13c6e509244bd1fa9463721b0f54` |
| kind node | `v1.36.1` | `kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5` |

## Task 1: Establish the Implementation Worktree and Baseline

**Files:**

- Read: `docs/superpowers/specs/2026-07-26-finish-deferred-deployment-platforms-design.md`
- Read: `docs/superpowers/plans/2026-07-24-cross-platform-deployment-contract.md`
- Read: `docs/superpowers/specs/2026-07-24-aws-ecs-acceptance-refactor-design.md`
- Read: `docs/reference/deployment-platforms.md`
- Read: `tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py`
- Inspect only: commit `773fbd3bf`

- [ ] **Step 1: Land the planning-only commit before implementation**

In the current planning checkout, review and commit exactly these two files before creating the worktree:

```text
docs/superpowers/specs/2026-07-26-finish-deferred-deployment-platforms-design.md
docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md
```

Do not include source, tests, manifests, generated evidence, or unrelated documentation in that commit. Record the resulting commit as the implementation base:

```bash
test -z "$(git status --porcelain)"
PLAN_BASE_SHA=$(git rev-parse HEAD^{commit})
git show --stat --oneline "$PLAN_BASE_SHA"
```

Expected: the worktree is clean and the displayed commit contains only the approved spec and plan.

- [ ] **Step 2: Verify the project-local worktree directory is safe**

```bash
test -d .worktrees
git check-ignore -q .worktrees
test ! -e .worktrees/finish-deferred-deployment-platforms
test ! -e .worktrees/cross-platform-deployment-contract
test -z "$(git branch --list codex/finish-deferred-deployment-platforms)"
```

Expected: `.worktrees` exists and is ignored; the retired cross-platform worktree remains absent; and neither the new path nor branch already exists.

- [ ] **Step 3: Create an isolated implementation worktree from the plan-bearing commit**

```bash
git worktree add \
  .worktrees/finish-deferred-deployment-platforms \
  -b codex/finish-deferred-deployment-platforms \
  "$PLAN_BASE_SHA"
cd .worktrees/finish-deferred-deployment-platforms
test "$(git rev-parse HEAD^{commit})" = "$PLAN_BASE_SHA"
test -f docs/superpowers/specs/2026-07-26-finish-deferred-deployment-platforms-design.md
test -f docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md
test -z "$(git status --porcelain)"
```

Expected: the source worktree is not modified; the new worktree is clean, contains the approved documents, and is on `codex/finish-deferred-deployment-platforms` at `PLAN_BASE_SHA`.

- [ ] **Step 4: Record the live baseline**

```bash
git rev-parse HEAD
git log -1 --oneline
uv sync --all-extras
uv run pytest -q tests/unit/docs/test_deployment_platform_docs.py tests/testcontainer/web/test_external_deployment_postgres.py
uv run pytest -q \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_runbook_contract.py
probe=$(uv run python -m elspeth.web.aws_ecs_acceptance \
  scenario-namespace \
  --acceptance-run-id 00000000-0000-4000-8000-000000000001 \
  --scenario-id A)
test "$probe" = a-f8b447a7b5b51f38c800
```

Expected: dependency sync succeeds; the existing deployment documentation/external-PostgreSQL baseline passes; the post-refactor AWS facade, private owners, dependency guard, and runbook contract pass; and the deterministic probe returns the reviewed value. If a baseline test fails, stop and diagnose it before attributing the failure to this plan.

- [ ] **Step 5: Inspect, but do not restore, the reverted ACA work**

```bash
git show --stat 773fbd3bf
git show 773fbd3bf:deploy/azure-container-apps/main.bicep | sed -n '1,260p'
git show 773fbd3bf:tests/unit/deploy/test_azure_container_apps_bundle.py | sed -n '1,240p'
```

Expected: the old parameter/resource vocabulary is visible without changing the worktree.

- [ ] **Step 6: Create or claim the implementation issues**

Use Filigree's atomic `work_start`/`start-work` operation. Reuse the existing runtime blocker `elspeth-b5d7aa5655` if it remains open, and create separate Kubernetes, ACA, profiles, and closeout tasks only if equivalent live issues do not already exist. Link ACA as blocked by the runtime task; link profiles/closeout to both provider bundles.

- [ ] **Step 7: Commit only if Task 1 required a durable planning adjustment**

No commit is expected for a clean baseline. If verified pins or exact paths changed, update this plan first and commit that documentation-only correction separately.

## Task 2: Define Web Coordination Contracts

**Files:**

- Create: `src/elspeth/web/coordination/__init__.py`
- Create: `src/elspeth/web/coordination/contracts.py`
- Create: `tests/unit/web/coordination/test_contracts.py`
- Reference: `src/elspeth/contracts/coordination.py`
- Reference: `src/elspeth/web/sessions/protocol.py`

- [ ] **Step 1: Write failing value-object tests**

Cover:

- `RunOwnershipFence(run_id, owner_instance_id, owner_epoch)` rejects blank IDs and epochs below one;
- `WebInstanceLease` rejects non-positive lease intervals;
- `InstanceState` is bounded to `active`, `draining`, and `stopped`;
- `CancellationSource` is bounded and safe to persist; and
- `RunOwnershipFenceLost` carries identifiers/epochs but never database URLs or secret values.

```bash
uv run pytest -q tests/unit/web/coordination/test_contracts.py
```

Expected: collection fails because `elspeth.web.coordination` does not exist.

- [ ] **Step 2: Implement immutable contracts**

Use frozen dataclasses and string enums. The fence must have exactly these authority fields:

```python
@dataclass(frozen=True, slots=True)
class RunOwnershipFence:
    run_id: str
    owner_instance_id: str
    owner_epoch: int
```

Add typed errors for ownership loss, unavailable distributed coordination, and invalid lease configuration. Keep error formatting leak-safe.

- [ ] **Step 3: Run the focused tests**

```bash
uv run pytest -q tests/unit/web/coordination/test_contracts.py
```

Expected: all coordination contract tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/coordination tests/unit/web/coordination/test_contracts.py
git commit -m "feat(web): define cross-process coordination contracts"
```

## Task 3: Add the Sessions Schema for Distributed Web Authorities

**Files:**

- Modify: `src/elspeth/web/sessions/models.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Test: `tests/unit/web/sessions/test_schema.py`
- Create: `tests/unit/web/sessions/test_schema_postgresql.py`
- Reference: `src/elspeth/web/_aws_ecs_acceptance/receipt_contracts.py`
- Test: `tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py`
- Test: `tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py`

- [ ] **Step 1: Write failing SQLite and PostgreSQL schema tests**

Assert one schema epoch increment and the exact new shape:

```text
web_instances(
  instance_id PK, deployment_target, revision_label, state,
  started_at, heartbeat_at, heartbeat_expires_at, draining_at, stopped_at
)

runs += owner_instance_id, owner_epoch, owner_lease_expires_at,
        cancel_requested_at, cancel_source

websocket_tickets(
  ticket_digest PK, run_id, user_id, username,
  expires_at, consumed_at, created_at
)

composer_progress(
  session_id PK/FK, user_id, phase, message, progress_json,
  updated_at, owner_instance_id
)

composer_inflight_requests(
  request_id PK, session_id, user_id, instance_id, started_at, expires_at
)

web_rate_limit_buckets(scope, subject_digest, created_at, PK(scope, subject_digest))
web_rate_limit_events(id PK, scope, subject_digest, observed_at)
```

Also assert indexes for expired instance/run discovery, ticket expiry, composer inflight expiry, and `(scope, subject_digest, observed_at)` rate-window pruning. Assert deletion cascades do not remove audit/run events unexpectedly.

```bash
uv run pytest -q tests/unit/web/sessions/test_schema.py tests/unit/web/sessions/test_schema_postgresql.py
```

Expected: tests fail because the schema is still at epoch 36 and the new objects do not exist.

- [ ] **Step 2: Implement the schema as epoch 37**

Use the repository's current delete-and-recreate policy; do not add a production migration framework. Add SQLAlchemy table definitions and typed row/result models in the existing sessions model layer. Use timezone-aware timestamps and bounded text/check constraints for states and cancellation sources.

- [ ] **Step 3: Expose only typed persistence methods in the protocol**

Add signatures for instance registration/heartbeat/draining, run claim/renew/takeover, cancellation request/read, tickets, composer progress/inflight, rate-limit acquisition, and durable run-event reads. Do not expose raw SQLAlchemy sessions to routes or execution services.

- [ ] **Step 4: Run focused schema tests in both dialects**

```bash
uv run pytest -q tests/unit/web/sessions/test_schema.py tests/unit/web/sessions/test_schema_postgresql.py
```

Expected: both dialect suites pass and report schema epoch 37.

- [ ] **Step 5: Verify the AWS candidate-schema binding follows epoch 37**

```bash
uv run pytest -q \
  tests/unit/web/aws_ecs_acceptance/test_receipt_contracts.py \
  tests/unit/web/aws_ecs_acceptance/test_cleanup_control_service.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py
```

Expected: generated candidate schema facts use `SESSION_SCHEMA_EPOCH == 37`, stale-epoch validation still fails closed, and facade/private-package boundaries remain intact. Update the private lookup owner—not the facade—only if a real contract change is required.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/web/sessions tests/unit/web/sessions/test_schema.py tests/unit/web/sessions/test_schema_postgresql.py
git commit -m "feat(web): persist distributed coordination authorities"
```

## Task 4: Implement PostgreSQL Membership and Epoch-Fenced Run Ownership

**Files:**

- Create: `src/elspeth/web/coordination/repository.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/sessions/protocol.py`
- Create: `tests/unit/web/coordination/test_repository.py`
- Create: `tests/testcontainer/web/test_coordination_repository_postgres.py`

- [ ] **Step 1: Write failing repository tests**

Cover with real PostgreSQL where locking semantics matter:

- registration mints no reusable authority and rejects an existing live ID;
- heartbeat uses `CURRENT_TIMESTAMP`/database time;
- draining refuses new claims but can renew already-owned work;
- a fresh owner or fresh run lease prevents takeover;
- takeover requires both owner-instance expiry and run-lease expiry;
- two claimants using `FOR UPDATE SKIP LOCKED` produce exactly one new owner;
- takeover increments `owner_epoch` monotonically;
- every fenced run update matches run ID, owner ID, and epoch; and
- a stale fence changes zero rows and raises `RunOwnershipFenceLost` without a fallback write.

```bash
uv run pytest -q tests/unit/web/coordination/test_repository.py tests/testcontainer/web/test_coordination_repository_postgres.py
```

Expected: tests fail because `WebCoordinationRepository` is absent.

- [ ] **Step 2: Implement the repository and configuration invariants**

Add defaults through the existing settings surface:

```text
web_instance_heartbeat_seconds = 5
web_instance_lease_seconds = 30
web_run_heartbeat_seconds = 2
web_run_lease_seconds = 15
web_takeover_poll_seconds = 2
```

Validate `heartbeat < lease` and `lease >= 4 * heartbeat` for instance and run leases. Use PostgreSQL transaction-scoped locks/row locks, database time, and `FOR UPDATE SKIP LOCKED`. External-PostgreSQL selection must fail closed if the distributed repository cannot initialize.

- [ ] **Step 3: Thread fences through sessions run mutations**

Require `RunOwnershipFence` for owner-only transitions, progress/event projection, Landscape-ID binding, output linking/finalization, and terminalization. Leave authenticated user-authored session edits on their existing session ownership/write-lock path.

- [ ] **Step 4: Prove the stale writer leaves state unchanged**

In the PostgreSQL test, capture the run row and last event before a stale write, attempt the write with epoch `N`, after takeover to `N+1`, then assert both row and event sequence are byte-for-byte/field-for-field unchanged.

- [ ] **Step 5: Run focused and neighboring session tests**

```bash
uv run pytest -q \
  tests/unit/web/coordination/test_repository.py \
  tests/testcontainer/web/test_coordination_repository_postgres.py \
  tests/unit/web/sessions
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/web/coordination src/elspeth/web/sessions tests/unit/web/coordination tests/testcontainer/web/test_coordination_repository_postgres.py
git commit -m "feat(web): fence run ownership in PostgreSQL"
```

## Task 5: Integrate Instance Lifecycle, Readiness, and Safe Takeover

**Files:**

- Modify: `src/elspeth/web/app.py`
- Modify: `src/elspeth/web/config.py`
- Modify: `src/elspeth/web/deployment_contract.py`
- Modify: `src/elspeth/web/readiness.py`
- Create: `src/elspeth/web/coordination/lifecycle.py`
- Create: `tests/unit/web/coordination/test_lifecycle.py`
- Modify: `tests/unit/web/test_app.py`

- [ ] **Step 1: Write failing lifecycle tests**

Assert that:

- an external-PostgreSQL process registers before becoming ready;
- registration or heartbeat failure makes readiness fail closed;
- shutdown marks the instance `draining` before readiness fails and before new claims stop;
- heartbeats continue while owned runs drain;
- clean shutdown records `stopped`;
- abrupt death is represented only by lease expiry;
- the takeover poller claims expired work but ignores fresh work; and
- startup does not cancel or terminalize another instance's nonterminal runs.

```bash
uv run pytest -q tests/unit/web/coordination/test_lifecycle.py tests/unit/web/test_app.py
```

Expected: tests fail because lifecycle membership does not exist and startup still applies process-local orphan logic.

- [ ] **Step 2: Implement the lifecycle manager**

Mint `instance_id` with a cryptographically random opaque value at lifespan start. Persist only a bounded non-secret revision label. Run heartbeat and takeover loops as named lifespan tasks with explicit cancellation and bounded shutdown.

Order shutdown as:

1. mark instance draining;
2. fail readiness and stop accepting run claims;
3. signal local execution to drain;
4. keep heartbeating owned work until drain deadline;
5. stop heartbeat/takeover loops; and
6. record stopped when the database remains reachable.

- [ ] **Step 3: Remove unsafe startup cleanup**

Delete the bulk “cancel every nonterminal run on startup” behavior from `_service_lifespan`. Rewrite periodic cleanup to inspect durable ownership/lease state; never use `execution_service.get_live_run_ids()` as proof that a run is orphaned cluster-wide.

- [ ] **Step 4: Add readiness evidence**

`/api/ready` must return non-ready for a draining instance, expired/self-lost membership, failed external-PostgreSQL coordination initialization, or an unhealthy heartbeat loop. `/api/health` remains a liveness signal and must not require leadership.

- [ ] **Step 5: Run focused and application lifespan tests**

```bash
uv run pytest -q \
  tests/unit/web/coordination/test_lifecycle.py \
  tests/unit/web/test_app.py
```

Expected: startup preserves peer-owned runs, draining fails readiness, and all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/web/app.py src/elspeth/web/config.py src/elspeth/web/deployment_contract.py src/elspeth/web/readiness.py src/elspeth/web/coordination tests/unit/web
git commit -m "feat(web): coordinate instance lifecycle and takeover"
```

## Task 6: Fence Execution, Resume, and Durable Cancellation

**Files:**

- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/execution/routes.py`
- Modify: `src/elspeth/web/execution/protocol.py`
- Create: `src/elspeth/web/coordination/resume.py`
- Modify as needed: `src/elspeth/engine/orchestrator/core.py`
- Reference: `src/elspeth/web/_aws_ecs_acceptance/bedrock.py`
- Modify: `tests/unit/web/execution/test_service.py`
- Modify: `tests/unit/web/execution/test_routes.py`
- Create: `tests/testcontainer/web/test_cross_process_run_control_postgres.py`

- [ ] **Step 1: Write failing execution and cancellation tests**

Cover:

- run creation atomically assigns owner epoch 1;
- every background execution callback carries its immutable fence;
- losing the sessions fence stops projection/finalization without terminalizing the run;
- takeover of a run with a Landscape ID acquires a new Landscape `CoordinationToken` before resuming;
- a terminal Landscape run is reconciled rather than resumed;
- cancellation through a non-owner replica persists `cancel_requested_at` and returns accepted;
- only the current owner terminalizes a cancelled run; and
- API `cancel_requested` is read from durable state.

```bash
uv run pytest -q tests/unit/web/execution/test_service.py tests/unit/web/execution/test_routes.py tests/testcontainer/web/test_cross_process_run_control_postgres.py
```

Expected: new tests fail because shutdown events and ownership are process-local.

- [ ] **Step 2: Make execution fence-aware**

Replace ambient ownership assumptions with an explicit `RunOwnershipFence` passed into the execution worker and every owner-only sessions mutation. On `RunOwnershipFenceLost`, set the local stop signal, suppress further sessions writes, and allow the new owner to reconcile. Do not catch the error and retry with the newly observed epoch.

- [ ] **Step 3: Extract a reusable resume adapter**

Wrap the existing `Orchestrator.resume` path so web takeover can:

1. resolve the durable Landscape run;
2. acquire/take over engine leadership and receive a `CoordinationToken`;
3. re-check the sessions `RunOwnershipFence`;
4. resume with both authorities held; and
5. reconcile sessions status from a terminal engine run without reopening it.

Keep CLI resume behavior unchanged and covered by its existing tests. Do not add a web-only unfenced engine finalization path.

- [ ] **Step 4: Make cancellation durable**

The route records a request transactionally for any nonterminal run. The local-owner fast path may set its `threading.Event`, but peer owners discover cancellation during heartbeat. A non-owner route must never directly mark a peer-owned run cancelled.

- [ ] **Step 5: Run focused, engine resume, and CLI regressions**

```bash
uv run pytest -q \
  tests/unit/web/execution/test_service.py \
  tests/unit/web/execution/test_routes.py \
  tests/testcontainer/web/test_cross_process_run_control_postgres.py \
  tests/unit/web/aws_ecs_acceptance/test_bedrock_guardrails.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/unit/engine \
  tests/unit/cli
```

Expected: all selected tests pass; stale owners cannot project or finalize after takeover; the Bedrock owner still resolves `_build_web_plugin_policy_evidence` from `execution.service`; and no domain logic moves back into the AWS facade.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/web src/elspeth/engine tests/unit/web tests/testcontainer/web/test_cross_process_run_control_postgres.py
git commit -m "feat(web): fence execution resume and cancellation"
```

## Task 7: Make Tickets and Run Progress Cross-Replica

**Files:**

- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/execution/service.py`
- Modify: `src/elspeth/web/execution/routes.py`
- Modify: `src/elspeth/web/execution/progress.py`
- Modify: `src/elspeth/web/execution/websocket_ticket.py`
- Modify: `tests/unit/web/execution/test_websocket.py`
- Modify: `tests/unit/web/execution/test_progress.py`
- Create: `tests/testcontainer/web/test_cross_process_progress_postgres.py`

- [ ] **Step 1: Write failing cross-replica tests**

Assert that:

- a ticket issued through service A is consumed exactly once through service B;
- the database stores `SHA-256(ticket)`, never the plaintext ticket;
- expired, consumed, wrong-user, and wrong-run tickets fail identically without leaking which predicate failed;
- PostgreSQL WebSockets resume from `last_sequence` using durable run events;
- events arrive in strictly increasing sequence order across owner takeover; and
- terminal status closes the stream even if the final in-process broadcast occurred on another instance.

```bash
uv run pytest -q tests/unit/web/execution/test_websocket.py tests/unit/web/execution/test_progress.py tests/testcontainer/web/test_cross_process_progress_postgres.py
```

Expected: cross-process cases fail against the in-memory ticket and broadcaster stores.

- [ ] **Step 2: Implement atomic durable tickets**

Issue an opaque random bearer token, store only its SHA-256 digest with bound run/user/username/expiry fields, and consume with one conditional update returning exactly one row. Keep cleanup bounded and indexed.

- [ ] **Step 3: Implement the PostgreSQL durable progress adapter**

Add `list_run_events_after(run_id, sequence, limit)` to the sessions service. For external PostgreSQL, poll at a default 500 ms interval, emit ordered events, and re-check terminal status. Retain the local broadcaster for SQLite latency; it must not be selected as cloud authority.

- [ ] **Step 4: Run focused and WebSocket route tests**

```bash
uv run pytest -q \
  tests/unit/web/execution/test_websocket.py \
  tests/unit/web/execution/test_progress.py \
  tests/testcontainer/web/test_cross_process_progress_postgres.py
```

Expected: all selected tests pass, including exactly-once ticket consumption.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web tests/unit/web tests/testcontainer/web/test_cross_process_progress_postgres.py
git commit -m "feat(web): persist tickets and run progress"
```

## Task 8: Persist Composer Progress and Enforce Cluster-Wide Rate Limits

**Files:**

- Modify: `src/elspeth/web/composer/progress.py`
- Modify: `src/elspeth/web/composer/service.py`
- Modify: `src/elspeth/web/middleware/rate_limit.py`
- Modify: `src/elspeth/web/sessions/routes/composer/compose.py`
- Modify: `src/elspeth/web/sessions/routes/composer/guided_plan.py`
- Modify: `src/elspeth/web/auth/routes.py`
- Reference: `src/elspeth/web/_aws_ecs_acceptance/bedrock.py`
- Reference: `src/elspeth/web/_aws_ecs_acceptance/capture.py`
- Modify: `tests/unit/web/composer/test_progress.py`
- Modify: `tests/unit/web/middleware/test_rate_limit.py`
- Modify: `tests/unit/web/auth/test_routes.py`
- Create: `tests/testcontainer/web/test_cross_process_composer_postgres.py`

- [ ] **Step 1: Write failing durable composer tests**

Cover:

- progress written through A is readable through B under the same session ownership checks;
- one bounded latest-progress row replaces earlier snapshots;
- inflight requests have unique IDs and expiring leases;
- expiry of a killed instance's inflight row restores capacity;
- completion removes only its own inflight row; and
- another user's session remains undiscoverable.

- [ ] **Step 2: Write failing rate-limit tests**

Use two independent repository/service objects against the same PostgreSQL database. Assert a combined request stream cannot exceed the configured window for scopes `composer-user` and `auth-ip`; window expiry restores capacity; concurrent boundary requests admit exactly the allowed count; and the database contains only HMAC-SHA256 subject digests.

```bash
uv run pytest -q tests/unit/web/composer/test_progress.py tests/unit/web/middleware/test_rate_limit.py tests/testcontainer/web/test_cross_process_composer_postgres.py
```

Expected: cross-process cases fail because current registries and limiters are in-memory.

- [ ] **Step 3: Implement durable PostgreSQL adapters**

Use the sessions database for external-PostgreSQL deployments. Derive subject digests with HMAC-SHA256 and the existing configured fingerprint key; never persist raw IP addresses or copied user identifiers. Lock the `(scope, subject_digest)` bucket, prune expired events, count, and conditionally insert within one transaction. Keep existing in-memory adapters for SQLite.

- [ ] **Step 4: Run focused route and security tests**

```bash
uv run pytest -q \
  tests/unit/web/composer \
  tests/unit/web/middleware/test_rate_limit.py \
  tests/unit/web/auth/test_routes.py \
  tests/unit/web/aws_ecs_acceptance/test_bedrock_guardrails.py \
  tests/unit/web/aws_ecs_acceptance/test_http_capture.py \
  tests/unit/web/aws_ecs_acceptance/test_facade_contract.py \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/testcontainer/web/test_cross_process_composer_postgres.py
```

Expected: all selected tests pass, test diagnostics contain no raw subjects, and the post-refactor AWS owners retain their composer adapter/capture behavior without facade monkeypatches.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web tests/unit/web tests/testcontainer/web/test_cross_process_composer_postgres.py
git commit -m "feat(web): coordinate composer and rate limits"
```

## Task 9: Fence Run-Output Blob Operations and Prove Shared-Filesystem Safety

**Files:**

- Modify: `src/elspeth/web/blobs/service.py`
- Modify: `src/elspeth/web/sessions/service.py`
- Modify: `src/elspeth/web/execution/service.py`
- Test: `tests/unit/web/blobs/test_service.py`
- Create: `tests/testcontainer/web/test_cross_process_blobs_postgres.py`
- Reference: existing blob reservation, advisory-lock, atomic-replace, and deletion-cleanup code

- [ ] **Step 1: Write failing two-process blob tests**

Using two independent services, one PostgreSQL sessions database, and one shared temporary filesystem, prove:

- the same content converges to one terminal blob and one valid file;
- reservation takeover cannot let a stale owner overwrite final metadata;
- run-output link/finalize rejects a stale `RunOwnershipFence`;
- deletion and recovery do not remove a newly re-referenced blob; and
- a process killed between file write and metadata finalization is recoverable exactly once.

```bash
uv run pytest -q tests/unit/web/blobs/test_service.py tests/testcontainer/web/test_cross_process_blobs_postgres.py
```

Expected: at least the run-owner-fence cases fail before implementation.

- [ ] **Step 2: Reuse existing blob coordination**

Keep durable reservation rows, PostgreSQL session/advisory locks, atomic replacement, and deletion cleanup. Add `RunOwnershipFence` only to execution-owned output link/finalize mutations and CAS-check it in the same transaction. Do not require a run fence for ordinary authenticated user blob operations already protected by session ownership and session write locks.

- [ ] **Step 3: Run focused blob and execution regressions**

```bash
uv run pytest -q \
  tests/unit/web/blobs \
  tests/unit/web/execution/test_service.py \
  tests/testcontainer/web/test_cross_process_blobs_postgres.py
```

Expected: all selected tests pass; shared-file operations remain deterministic under process death.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/web/blobs src/elspeth/web/sessions/service.py src/elspeth/web/execution/service.py tests/unit/web/blobs tests/testcontainer/web/test_cross_process_blobs_postgres.py
git commit -m "feat(web): fence shared run output storage"
```

## Task 10: Build the Maintained Two-Process PostgreSQL Acceptance Corpus

**Files:**

- Create: `tests/testcontainer/web/multiprocess/__init__.py`
- Create: `tests/testcontainer/web/multiprocess/harness.py`
- Create: `tests/testcontainer/web/test_multi_instance_rollout.py`
- Create: `tests/testcontainer/web/test_multi_instance_takeover.py`
- Create: `tests/testcontainer/web/test_multi_instance_authorities.py`
- Create: `.github/workflows/web-multi-instance.yml`
- Modify: `pyproject.toml`
- Modify: `docs/guides/test-system.md`

- [ ] **Step 1: Build a black-box two-process harness**

The harness must start two real ELSPETH web processes with:

- distinct opaque instance IDs and revision labels;
- separate ports and process-local state;
- one real PostgreSQL service with separate sessions/Landscape databases;
- one shared payload/blob directory;
- isolated log capture with secret redaction; and
- deterministic process readiness, graceful terminate, and `SIGKILL` controls.

Do not satisfy these tests with two FastAPI clients sharing one Python object graph.

- [ ] **Step 2: Add the rollout-overlap scenario**

Start revision A, begin a provider-free long-enough run, start revision B before draining A, prove both liveness endpoints respond, drain A, and prove the run reaches one terminal outcome with one ordered durable event stream and no stale post-takeover writes.

- [ ] **Step 3: Add the `SIGKILL` takeover scenario**

Kill the current owner without shutdown, prove B does not take over before both leases expire, then prove epoch increments, Landscape leadership is reacquired, recovery resumes from durable state, and A cannot write if its suspended worker is allowed to continue.

- [ ] **Step 4: Add peer-control and maintenance scenarios**

Cover peer cancellation, maintenance prewarming, cross-replica ticket consumption, run progress, composer progress/inflight expiry, cluster-wide rate limiting, and shared-blob exactly-once behavior. Each scenario must assert both the successful new-owner path and the rejected stale-owner path.

- [ ] **Step 5: Add a dedicated CI hard gate**

Pin PostgreSQL/test tooling consistently with existing testcontainer workflows. Run the corpus on pull requests that touch `src/elspeth/web/**`, sessions schema, deployment contracts, or ACA files, plus the release workflow. Upload sanitized logs on failure. Do not allow an opt-out marker to turn these tests green.

- [ ] **Step 6: Run the complete corpus twice**

```bash
uv run pytest -q tests/testcontainer/web/test_multi_instance_rollout.py tests/testcontainer/web/test_multi_instance_takeover.py tests/testcontainer/web/test_multi_instance_authorities.py
uv run pytest -q tests/testcontainer/web/test_multi_instance_rollout.py tests/testcontainer/web/test_multi_instance_takeover.py tests/testcontainer/web/test_multi_instance_authorities.py
```

Expected: both consecutive runs pass, with no leaked child processes, ports, or containers.

- [ ] **Step 7: Run the existing external-deployment regression**

```bash
uv run pytest -q tests/testcontainer/web/test_external_deployment_postgres.py tests/unit/web
```

Expected: the existing external-PostgreSQL startup/doctor contract and all web unit tests pass.

- [ ] **Step 8: Commit**

```bash
git add tests/testcontainer/web .github/workflows/web-multi-instance.yml pyproject.toml docs/guides/test-system.md
git commit -m "test(web): gate transient multi-instance overlap"
```

## Task 11: Ship the Provider-Neutral Kubernetes Base

**Files:**

- Create: `deploy/kubernetes/base/kustomization.yaml`
- Create: `deploy/kubernetes/base/deployment.yaml`
- Create: `deploy/kubernetes/base/service.yaml`
- Create: `deploy/kubernetes/base/configmap.yaml`
- Create: `deploy/kubernetes/base/pvc.yaml`
- Create: `deploy/kubernetes/base/secret.example.yaml`
- Create: `tests/unit/deployment/test_kubernetes_bundle.py`
- Create: `.github/workflows/kubernetes-render.yml`

- [ ] **Step 1: Write failing source-contract tests**

Assert that the shipped base:

- renders one Deployment, Service, ConfigMap, and PVC;
- excludes `secret.example.yaml` from `kustomization.yaml`;
- has `replicas: 1`, `strategy.type: Recreate`, and `WEB_CONCURRENCY=1`;
- sets deployment target `kubernetes` and state mode `external-postgresql`;
- references both sessions and Landscape PostgreSQL URLs through Secret keys;
- uses an immutable/release-specific GHCR image example;
- runs application containers as UID/GID 1654 with `allowPrivilegeEscalation: false` and dropped capabilities;
- mounts persistent data/blob/payload paths;
- probes `/api/health` for liveness and `/api/ready` for readiness; and
- contains no PostgreSQL workload, ingress, TLS, database operator, storage class, or cloud identity resource.

```bash
uv run pytest -q tests/unit/deployment/test_kubernetes_bundle.py
```

Expected: tests fail because `deploy/kubernetes/base/` does not exist.

- [ ] **Step 2: Add the minimal Kustomize base**

Use this inventory:

```yaml
resources:
  - configmap.yaml
  - pvc.yaml
  - service.yaml
  - deployment.yaml
```

The Secret example is documentation-only and contains inert placeholders. The Deployment uses `containerPort: 8451`; the Service maps port 8451 to named target port `http`. Use an init container only to create/chmod the mounted ELSPETH subdirectories; document the storage-class requirement and avoid changing ownership outside the mounted application subtree.

- [ ] **Step 3: Add checksum-pinned render CI**

Download kubectl `v1.36.3`, verify SHA-256 before installation, run `kubectl kustomize deploy/kubernetes/base`, and validate the rendered YAML with the unit contract. Do not apply to a remote cluster in this workflow.

- [ ] **Step 4: Run source and render tests**

```bash
uv run pytest -q tests/unit/deployment/test_kubernetes_bundle.py
kubectl kustomize deploy/kubernetes/base > /tmp/elspeth-kubernetes-render.yaml
kubectl apply --dry-run=client --validate=false -f /tmp/elspeth-kubernetes-render.yaml
```

Expected: the unit test passes, Kustomize emits all four resource kinds, and client dry-run succeeds.

- [ ] **Step 5: Run deployment-policy regressions**

```bash
uv run pytest -q tests/unit/deployment tests/unit/docs/test_deployment_platform_docs.py
```

Expected: deployment tests pass after replacing only the obsolete “Kubernetes bundle absent” assertion with positive bundle assertions. Do not claim maintained Kubernetes support in docs until Task 12 passes.

- [ ] **Step 6: Commit**

```bash
git add deploy/kubernetes tests/unit/deployment/test_kubernetes_bundle.py .github/workflows/kubernetes-render.yml tests/unit/docs/test_deployment_platform_docs.py
git commit -m "feat(deploy): add provider-neutral Kubernetes base"
```

## Task 12: Add a Real kind Readiness and Provider-Free Run Smoke

**Files:**

- Create: `tests/testcontainer/deployment/kubernetes/kind-config.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/postgresql.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/test-overlay/kustomization.yaml`
- Create: `tests/testcontainer/deployment/kubernetes/test-overlay/secret.yaml`
- Create: `tests/testcontainer/deployment/test_kubernetes_kind.py`
- Create: `scripts/ci/kubernetes-kind-smoke.sh`
- Modify: `.github/workflows/kubernetes-render.yml`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write a failing test that owns the whole local-cluster lifecycle**

The test must create a uniquely named kind cluster, build the current ELSPETH Docker image, load it with `kind load docker-image`, apply a test-only PostgreSQL harness and the Kustomize test overlay, initialize separate sessions/Landscape databases using the supported doctor/schema-init commands, wait for `/api/ready`, execute a provider-free sample run, assert its terminal result, and delete the cluster in `finally`.

```bash
uv run pytest -q tests/testcontainer/deployment/test_kubernetes_kind.py
```

Expected: the test fails because the harness and smoke script do not exist.

- [ ] **Step 2: Add test-only infrastructure**

Pin kind `v0.32.0` and the node image digest from the tool-pin table. Keep PostgreSQL manifests and local credentials under `tests/testcontainer/deployment/kubernetes/`; never add them to `deploy/kubernetes/base/kustomization.yaml`. Use separate database names/URLs for sessions and Landscape.

- [ ] **Step 3: Implement the smoke script with evidence-rich failures**

Use strict shell mode, unique cluster/container/image names, bounded waits, and cleanup traps. On failure, collect sanitized `kubectl get`, pod descriptions, events, and logs. Never print Secret objects or expanded PostgreSQL URLs.

- [ ] **Step 4: Run the smoke twice**

```bash
uv run pytest -q tests/testcontainer/deployment/test_kubernetes_kind.py
uv run pytest -q tests/testcontainer/deployment/test_kubernetes_kind.py
```

Expected: both runs pass and `kind get clusters` shows no leaked test cluster afterward.

- [ ] **Step 5: Wire CI**

Add the kind smoke to `.github/workflows/kubernetes-render.yml` after static rendering. Pin downloaded binaries by checksum, build locally, and do not pull an unpublished ELSPETH image as the system under test.

- [ ] **Step 6: Commit**

```bash
git add tests/testcontainer/deployment/kubernetes tests/testcontainer/deployment/test_kubernetes_kind.py scripts/ci/kubernetes-kind-smoke.sh .github/workflows/kubernetes-render.yml pyproject.toml
git commit -m "test(deploy): prove Kubernetes startup in kind"
```

## Task 13: Build and Compile the Workload-Only ACA Module

**Depends on:** Task 10 complete and green.

**Files:**

- Create: `deploy/azure-container-apps/main.bicep`
- Create: `deploy/azure-container-apps/main.example.bicepparam`
- Create: `tests/unit/deployment/test_azure_container_apps_bundle.py`
- Create: `.github/workflows/azure-container-apps-bicep.yml`
- Reference only: commit `773fbd3bf`

- [ ] **Step 1: Reinspect current ACA APIs and the reverted module**

```bash
git show 773fbd3bf:deploy/azure-container-apps/main.bicep | sed -n '1,260p'
git show 773fbd3bf:tests/unit/deploy/test_azure_container_apps_bundle.py | sed -n '1,240p'
```

Verify current Microsoft.ContainerApp resource API versions against official Azure documentation before writing the module. Record any version change in the commit body.

- [ ] **Step 2: Write failing Bicep source-contract tests**

Assert:

- the module accepts existing custom-VNet environment, user-assigned identity, Key Vault, NFS environment-storage name, immutable ACR image, and secret names as parameters;
- it creates exactly the workload resources, not PostgreSQL, Key Vault, VNet, private DNS, storage account, or the managed environment;
- secrets are Key Vault references and never plaintext parameters/defaults;
- target/state are `azure-container-apps`/`external-postgresql`;
- `WEB_CONCURRENCY=1`, `activeRevisionsMode: 'Single'`, `minReplicas: 1`, and `maxReplicas: 1` are explicit;
- NFS mounts data/blob/payload paths without a privileged permission-repair container;
- probes use `/api/health` and `/api/ready`; and
- comments/tests never claim that the settings eliminate rollout overlap.

```bash
uv run pytest -q tests/unit/deployment/test_azure_container_apps_bundle.py
```

Expected: tests fail because the ACA directory is absent.

- [ ] **Step 3: Implement the workload-only Bicep module**

Use the operator-provided prerequisites from the design spec. Require an immutable ACR digest reference and a revision suffix/revision label that contains no secrets. Mount a prepared `elspeth` NFS subtree already owned by UID/GID 1654. Do not add database resources, inline credentials, or privileged permission repair.

- [ ] **Step 4: Compile with checksum-pinned Bicep**

```bash
bicep build deploy/azure-container-apps/main.bicep --stdout > /tmp/elspeth-aca-template.json
bicep build-params deploy/azure-container-apps/main.example.bicepparam --stdout > /tmp/elspeth-aca-parameters.json
uv run pytest -q tests/unit/deployment/test_azure_container_apps_bundle.py
```

Expected: both Bicep commands exit zero, output valid JSON, and source-contract tests pass.

- [ ] **Step 5: Add compile CI**

Download Bicep `v0.45.15`, verify the pinned SHA-256, compile module and params, run the unit contract, and upload only inert compiled artifacts. Never authenticate to Azure in ordinary CI.

- [ ] **Step 6: Commit**

```bash
git add deploy/azure-container-apps tests/unit/deployment/test_azure_container_apps_bundle.py .github/workflows/azure-container-apps-bicep.yml
git commit -m "feat(deploy): add fenced Azure Container Apps workload"
```

## Task 14: Add Provider Runbooks and Execute ACA Operator Acceptance

**Files:**

- Create: `docs/runbooks/kubernetes-deployment.md`
- Create: `docs/runbooks/azure-container-apps-deployment.md`
- Create: `docs/runbooks/azure-container-apps-acceptance.md`
- Create: `scripts/acceptance/azure-container-apps.sh`
- Create: `tests/unit/deployment/test_azure_container_apps_acceptance_contract.py`
- Modify: `tests/unit/docs/test_deployment_platform_docs.py`

- [ ] **Step 1: Write failing runbook/acceptance contract tests**

The Kubernetes runbook must name external PostgreSQL, Secret creation, storage-class/UID requirements, render/apply/rollback, and the fact that no database/ingress/TLS is shipped. The ACA runbook must name every operator prerequisite, immutable image policy, separate database URLs, prepared NFS permissions, deployment/rollback, and the transient-overlap—not horizontal-scale—support posture.

The acceptance test must verify that the script has explicit scenarios for overlapping revision rollout, peer cancellation, ticket handoff, run progress handoff, maintenance prewarm, old-owner refusal, and cleanup/evidence capture.

```bash
uv run pytest -q tests/unit/deployment/test_azure_container_apps_acceptance_contract.py tests/unit/docs/test_deployment_platform_docs.py
```

Expected: tests fail because runbooks and acceptance runner do not exist.

- [ ] **Step 2: Write the runbooks and a fail-closed operator runner**

The ACA runner accepts resource identifiers through environment variables/arguments, validates they point at non-production resources, performs read-only preflight before mutation, deploys two immutable revisions with deliberate overlap, invokes the supported API paths, captures sanitized JSON evidence, and cleans up only resources it created. It must never print secrets or make ordinary CI responsible for Azure deployment.

- [ ] **Step 3: Run the local contracts**

```bash
uv run pytest -q tests/unit/deployment/test_azure_container_apps_acceptance_contract.py tests/unit/docs/test_deployment_platform_docs.py
bash -n scripts/acceptance/azure-container-apps.sh
```

Expected: tests and shell syntax pass. The script's `--preflight-only` mode fails clearly without required operator inputs and performs no mutation.

- [ ] **Step 4: Execute provider-side acceptance with operator authorization**

```bash
scripts/acceptance/azure-container-apps.sh --preflight-only
scripts/acceptance/azure-container-apps.sh --execute --evidence-dir "$ACA_EVIDENCE_DIR"
```

Expected: every scenario passes against non-production ACA resources; evidence contains resource/revision IDs, timings, HTTP/status outcomes, owner epochs, and sanitized log references but no credentials or raw tickets.

If live Azure access is not available, merge Tasks 2-13 as an ACA release candidate only. Do not perform Task 16's maintained-ACA documentation transition and do not set the ACA profile acceptance status to maintained.

- [ ] **Step 5: Commit the runbooks/contracts, then record acceptance separately**

```bash
git add docs/runbooks/kubernetes-deployment.md docs/runbooks/azure-container-apps-deployment.md docs/runbooks/azure-container-apps-acceptance.md scripts/acceptance/azure-container-apps.sh tests/unit/deployment/test_azure_container_apps_acceptance_contract.py tests/unit/docs/test_deployment_platform_docs.py
git commit -m "docs(deploy): add Kubernetes and ACA operations guides"
```

Record the accepted environment/revision identifiers and sanitized result in the project's existing operator-evidence surface; do not commit credentials, live parameter files, or raw logs.

## Task 15: Add the Machine-Readable Platform Profiles

**Depends on:** Kubernetes and ACA tracked artifacts exist; ACA profile status must reflect whether Task 14 provider acceptance actually passed.

**Files:**

- Create: `deploy/platforms/schema.json`
- Create: `deploy/platforms/docker-compose.yaml`
- Create: `deploy/platforms/linux-systemd.yaml`
- Create: `deploy/platforms/aws-ecs.yaml`
- Create: `deploy/platforms/azure-container-apps.yaml`
- Create: `deploy/platforms/kubernetes.yaml`
- Create: `tests/unit/deployment/test_platform_profiles.py`
- Modify: `tests/unit/test_build_push_release_checks.py`

- [ ] **Step 1: Write failing schema and cross-profile tests**

The schema requires:

```text
id, display_name, deployment_target, supported_state_modes,
recommended_state_mode, database_ownership, required_extras,
image_delivery, payload_storage, web_processes_per_replica,
steady_state_replicas, horizontal_scale_supported,
rollout_overlap_posture, tracked_artifacts,
automated_acceptance, authoritative_runbook, support_status
```

Set `additionalProperties: false` recursively where practical. Enumerate deployment targets/state modes/database ownership/image delivery/payload storage/rollout posture/support status rather than accepting arbitrary strings.

Cross-profile tests require exactly five YAML files and assert:

- no production recommendation uses compatibility state `auto`;
- AWS, ACA, and Kubernetes recommend external PostgreSQL and require the `postgres` extra;
- Compose alone tracks a PostgreSQL sidecar;
- every tracked artifact, test, workflow, and runbook exists and is tracked by Git;
- all profiles set `web_processes_per_replica: 1`, `steady_state_replicas: 1`, and `horizontal_scale_supported: false`;
- ACA alone uses `rollout_overlap_posture: fenced-transient`; all others use `none`;
- image examples are immutable or release-specific; and
- ACA cannot be `maintained` unless the provider-acceptance evidence locator is populated and validated.

For the AWS profile specifically, require these post-refactor evidence paths:

```text
src/elspeth/web/aws_ecs_acceptance.py
src/elspeth/web/_aws_ecs_acceptance/__init__.py
docs/runbooks/aws-ecs-deployment.md
tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py
tests/unit/web/aws_ecs_acceptance/test_facade_contract.py
tests/unit/web/aws_ecs_acceptance/test_manifest_task_definition.py
tests/unit/web/test_aws_ecs_runbook_contract.py
tests/testcontainer/web/test_doctor_aws_ecs_postgres.py
tests/testcontainer/web/test_schema_probe_postgres.py
tests/testcontainer/web/test_aws_ecs_validate_only_startup.py
tests/testcontainer/web/test_aws_ecs_readiness_postgres.py
tests/testcontainer/web/test_landscape_write_gate_postgres.py
```

The facade is the public executable artifact; the private package remains an implementation detail protected by the architecture test.

```bash
uv run pytest -q tests/unit/deployment/test_platform_profiles.py tests/unit/test_build_push_release_checks.py
```

Expected: tests fail because `deploy/platforms/` does not exist.

- [ ] **Step 2: Implement the schema**

Use JSON Schema draft 2020-12. Model `tracked_artifacts` and `automated_acceptance` as non-empty arrays of repository-relative paths. Model acceptance evidence as an optional non-secret locator required only when `support_status` is `maintained` for a provider-side gate.

- [ ] **Step 3: Add all five profiles**

Use these core values:

| Profile | DB ownership | Image delivery | Payload storage | Required extras | Rollout overlap |
|---|---|---|---|---|---|
| Docker Compose | `compose-sidecar` | `ghcr` | `named-volume` | `webui`, `postgres` | `none` |
| Linux systemd | `host-or-managed` | `native-package` | `host-persistent` | `webui`, `postgres` | `none` |
| AWS ECS | `external-managed` | `ecr` | `external-filesystem` | `webui`, `llm`, `aws`, `postgres` | `none` |
| Azure Container Apps | `external-managed` | `acr` | `external-filesystem` | `webui`, `llm`, `azure`, `postgres` | `fenced-transient` |
| Kubernetes | `external-managed` | `ghcr` | `persistent-volume` | `webui`, `llm`, `postgres` | `none` |

For Linux, list both supported `sqlite-single` and `external-postgresql` modes but recommend based on explicit production/non-production wording. For Compose, record both application-only and PostgreSQL-sidecar artifacts without implying PostgreSQL is inside the application container.

- [ ] **Step 4: Run profile and release-contract tests**

```bash
uv run pytest -q tests/unit/deployment/test_platform_profiles.py tests/unit/test_build_push_release_checks.py tests/unit/deployment
```

Expected: exactly five profiles validate and every referenced repository path exists.

- [ ] **Step 5: Commit**

```bash
git add deploy/platforms tests/unit/deployment/test_platform_profiles.py tests/unit/test_build_push_release_checks.py
git commit -m "feat(deploy): codify supported platform profiles"
```

## Task 16: Transition Public Documentation from Deferred to Maintained

**Depends on:** Task 12 passes for Kubernetes. Task 10, Task 13, and live Task 14 acceptance pass before ACA is called maintained.

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/README.md`
- Modify: `docs/guides/docker.md`
- Modify: `docs/reference/deployment-platforms.md`
- Modify: `docs/reference/environment-variables.md`
- Modify: `docs/repository-structure.md`
- Modify: `docs/runbooks/index.md`
- Modify: `tests/unit/docs/test_deployment_platform_docs.py`
- Modify: `tests/unit/docs/test_public_release_docs.py`

- [ ] **Step 1: Replace negative documentation tests with evidence-based positive tests**

Remove assertions that `deploy/kubernetes`, `deploy/azure-container-apps`, or `deploy/platforms` must be absent. Add assertions that public support claims link to profiles, tracked bundles, runbooks, and their executable acceptance gates. Assert the docs retain all of these boundaries:

- the application image has PostgreSQL clients, not a PostgreSQL server;
- operators supply external PostgreSQL for AWS, ACA, and Kubernetes;
- the Kubernetes base does not ship PostgreSQL, ingress, TLS, or a storage class;
- the ACA module does not create its environment, network, database, Key Vault, storage account, or NFS permissions;
- one web process and one steady-state replica remain mandatory; and
- transient-overlap safety is not horizontal scale support.

```bash
uv run pytest -q tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_public_release_docs.py
```

Expected: tests fail while the current BYO/deferred prose remains.

- [ ] **Step 2: Update the authoritative support matrix and indexes**

Make `docs/reference/deployment-platforms.md` derive human-readable claims from the same values as `deploy/platforms/*.yaml`. Add links to both provider runbooks and acceptance workflows. Update the runbook/docs indexes and repository structure.

If live ACA acceptance did not pass, label ACA `release candidate`/`operator acceptance pending` consistently in the profile and docs. Do not weaken tests to permit the word `maintained` without evidence.

- [ ] **Step 3: Update installation and environment guidance**

Document the two PostgreSQL URLs, immutable image expectations, persistent storage, target/state variables, lease/heartbeat settings, and operator-owned prerequisites without repeating secrets or suggesting that PostgreSQL runs in the application image.

- [ ] **Step 4: Run public documentation and deployment tests**

```bash
uv run pytest -q tests/unit/docs tests/unit/deployment tests/unit/test_build_push_release_checks.py
```

Expected: all docs/deployment contract tests pass; no stale “Kubernetes is BYO” or “ACA is deferred” claim remains when—and only when—the corresponding gates passed.

- [ ] **Step 5: Search for contradictory claims**

```bash
rg -n "Kubernetes is BYO|Container Apps is deferred|zero overlap|PostgreSQL.*inside|multiple replicas|horizontal scale" README.md CHANGELOG.md docs deploy/platforms
```

Expected: every match is either removed or an intentional boundary/non-goal statement; there are no claims that single-revision mode guarantees zero overlap.

- [ ] **Step 6: Commit**

```bash
git add README.md CHANGELOG.md docs tests/unit/docs
git commit -m "docs(deploy): publish verified platform support"
```

## Task 17: Full Verification, Review, and Branch Closeout

**Files:**

- Verify: all changed files
- Update only if a check finds a real defect: the owning implementation/test/documentation file

- [ ] **Step 1: Run formatting and static analysis**

```bash
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy src/elspeth
```

Expected: all commands exit zero.

- [ ] **Step 2: Run the full unit suite**

```bash
uv run pytest -q tests/unit
```

Expected: all unit tests pass with no unexpected skips in deployment, coordination, profile, or docs gates.

- [ ] **Step 3: Run all deployment-specific real-environment gates**

```bash
uv run pytest -q tests/testcontainer/web/test_external_deployment_postgres.py
uv run pytest -q tests/testcontainer/web/test_multi_instance_rollout.py tests/testcontainer/web/test_multi_instance_takeover.py tests/testcontainer/web/test_multi_instance_authorities.py
uv run pytest -q tests/testcontainer/deployment/test_kubernetes_kind.py
bicep build deploy/azure-container-apps/main.bicep --stdout > /tmp/elspeth-aca-template.json
bicep build-params deploy/azure-container-apps/main.example.bicepparam --stdout > /tmp/elspeth-aca-parameters.json
```

Expected: external PostgreSQL, two-process overlap, kind, and Bicep gates all pass.

- [ ] **Step 4: Run the post-refactor AWS facade, owner, architecture, and PostgreSQL gates**

```bash
set -Eeuo pipefail
env -u VIRTUAL_ENV uv run --frozen pytest -q \
  tests/unit/architecture/test_aws_ecs_acceptance_dependencies.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/aws_ecs_acceptance \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  tests/unit/web/test_landscape_access_guard.py
probe=$(env -u VIRTUAL_ENV uv run --frozen python \
  -m elspeth.web.aws_ecs_acceptance scenario-namespace \
  --acceptance-run-id 00000000-0000-4000-8000-000000000001 \
  --scenario-id A)
test "$probe" = a-f8b447a7b5b51f38c800
env -u VIRTUAL_ENV uv run --frozen pytest -q -m testcontainer \
  tests/testcontainer/web/test_doctor_aws_ecs_postgres.py \
  tests/testcontainer/web/test_schema_probe_postgres.py \
  tests/testcontainer/web/test_aws_ecs_validate_only_startup.py \
  tests/testcontainer/web/test_aws_ecs_readiness_postgres.py \
  tests/testcontainer/web/test_landscape_write_gate_postgres.py
```

Expected: the permanent facade, all split domain owners, private dependency graph, runbook contract, deterministic probe, and five real-PostgreSQL AWS startup/readiness proofs pass.

- [ ] **Step 5: Rerun the other maintained-platform and packaging gates**

Run the current Compose image smoke, Linux systemd bundle tests, release build/push contract, and the repository's standard wheel/container build. In both wheel and container, install `webui,llm,aws,postgres`, import `elspeth.web.aws_ecs_acceptance`, run `--help`, and rerun the deterministic `scenario-namespace` probe. Use the live workflow commands if packaging mechanics changed.

Expected: this work does not regress Compose, native Linux, the installed AWS facade/private package, application-image startup, or release packaging.

- [ ] **Step 6: Audit the support claims against evidence**

```bash
git grep -n "maintained" -- README.md docs deploy/platforms
git ls-files deploy/kubernetes deploy/azure-container-apps deploy/platforms docs/runbooks scripts/acceptance .github/workflows
git diff --check
git status --short
```

Expected: every maintained claim has a tracked artifact, automated gate, and runbook; the worktree contains no secrets, live parameter files, generated evidence, or unrelated changes.

- [ ] **Step 7: Request independent reviews**

Use `superpowers:requesting-code-review` and ask separate reviewers to inspect:

1. epoch/lease correctness and stale-writer refusal;
2. process-death and two-process acceptance realism;
3. Kubernetes security/storage/startup contract;
4. ACA Bicep/workload/operator boundary; and
5. profile/documentation claim integrity.

The AWS reviewer must additionally verify that no private module imports the facade, no new private module bypasses the layer inventory, moved-symbol identity remains exact, and tests patch the private lookup owner rather than stale facade aliases.

Fix concrete findings and rerun the exact relevant gate plus the full Task 17 suite. Do not treat static render/compile review as a substitute for runtime acceptance.

- [ ] **Step 8: Close tracker work only after evidence is green**

Add concise Filigree comments containing commit IDs and exact successful commands, then close each implemented issue. Leave ACA operator acceptance open if it was not performed, even if the module compiled.

- [ ] **Step 9: Finish the branch**

Use `superpowers:finishing-a-development-branch`. Confirm the branch is clean and based on the intended release target, then present merge/rebase/PR options to the maintainer. Do not push or merge without the maintainer's requested workflow.

## Completion Criteria

This plan is complete when:

- Kubernetes has a tracked provider-neutral base, static render gate, real kind startup/readiness/run smoke, and operator runbook;
- external-PostgreSQL web deployments survive transient two-process overlap with database-clocked membership, epoch-fenced ownership, durable cancellation/tickets/progress/composer/rate-limit authorities, and stale-owner refusal;
- shared blob and Landscape finalization paths retain their resource-specific fences under takeover;
- ACA has a compiled workload-only module, operator runbook, and successful non-production overlap acceptance before it is called maintained;
- exactly five machine-readable profiles validate and point only to tracked evidence; and
- Compose, native Linux, AWS, full unit, lint, type, package, and image gates remain green.

Anything less is a partial delivery and must be represented as such in the profiles, docs, and tracker.
