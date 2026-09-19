# Composer async operations implementation plan

**Goal:** Deliver the [revised design](../specs/2026-09-16-composer-async-operations-design.md)
for elspeth-7a663a062c on `release/0.8.1`: five Composer authoring routes
return a durable 202 handle without waiting for a provider, and a short poll
delivers the exact terminal result.

**Scope:** One hard cutover for `/messages`, `/recompose`, `/guided/plan`,
`/guided/respond`, and `/guided/chat`. The existing `guided_operations` table
and other guided operation routes retain their contracts. No Composer route
may author a server-derived graph or gain a tutorial-only path.

**Working setup:** Confirm the target release branch and its current SHA
before implementation. Use a dedicated worktree with both source roots on
`PYTHONPATH`; verify `elspeth.__file__` and `elspeth_lints.__file__` resolve
inside it. Read `CONTRIBUTING.md` § Whole-tree gates before code edits. Freeze
the final tip before broad validation and rerun a gate if it advances. The
schema change requires a reviewed session epoch update; coordinate the
deployed-store reset and `bootstrap-admin` with the release operator rather
than treating a local test database as deployment approval.
In commands below, `ELSPETH_WORKTREE` is the absolute path to that dedicated
worktree, exported by the implementer before running them.

## 0. Establish the failure and contract inventory

**Read:** `docs/specs/2026-09-16-composer-async-operations-review.md`,
`src/elspeth/web/sessions/routes/_helpers.py`, the five route handlers listed
below, `src/elspeth/web/sessions/routes/guided_operations.py`,
`src/elspeth/web/coordination/lifecycle.py`, and
`src/elspeth/web/frontend/src/stores/sessionStore.ts`.

1. Add one failing route integration test under
   `tests/unit/web/sessions/routes/test_composer_async_operations.py`: hold the
   provider beyond an HTTP client's short deadline and show the current POST
   remains pending. Use a controlled event, not a wall-clock sleep. Preserve
   the existing success DTO as the expected terminal value.
2. Build a reviewed table of *every* `HTTPException` and cancellation exit in
   `messages.py`, `composer/compose.py`, `composer/guided_plan.py`,
   `composer/guided.py` (`respond`, `chat`), and
   `composer/guided_chat_atomic.py`. Assign each site to stable pre-202
   admission or post-202 terminal projection. The test for each public error
   compares the old status/body with the new terminal envelope. Do not use a
   text count as proof that the inventory is complete; inspect route control
   flow and use an AST instrument with a known positive and negative control
   as a cross-check.
3. Record the current frontend success side effects for all five routes:
   message, state, proposals, validation reset, selected node, guided session,
   next turn, terminal, and interpretation refresh. The terminal result must
   exercise the same reducers.

**Gate:** The reproduction fails on the old synchronous behavior, and the
inventory has a source anchor and expected public outcome for each exit.
Do this before choosing error adapters or changing the route signatures.

## 1. Add the durable transport job schema

**Modify:** `src/elspeth/web/sessions/models.py`,
`src/elspeth/web/sessions/schema.py`,
`src/elspeth/web/sessions/protocol.py`,
`src/elspeth/web/sessions/service.py`.

**Test:** `tests/unit/web/sessions/test_schema.py`, a new
`tests/unit/web/sessions/test_composer_async_operation_service.py`,
`tests/testcontainer/web/test_schema_probe_postgres.py`.

1. Define `composer_async_operations` with a composite `(session_id,
   operation_id)` primary key, closed five-kind and four-status checks,
   bounded request JSON, actor identity, claim token/expiry, cancellation
   marker, terminal public JSON, schema discriminator, canonical hash, and
   timestamps. Check queued, running, completed, and failed NULL bundles on
   both dialects. Clear request JSON on terminal settlement. Add a scan index
   for queued jobs and expired claims.
2. Update the schema-shape identity and all epoch mirrors from the current
   release tip. Do not alter `guided_operations` constraints or rename that
   table. PostgreSQL reflection of every new CHECK and index must equal the
   declared schema, including one-value or JSON checks.
3. Add owned request, job, and result types. Parse untrusted JSON into a
   route-specific strict DTO before work. Validate and hash the public result
   DTO before write and validate it again on read. The stored response must be
   the exact body the former synchronous route returned.

**Gate:** A duplicate `(session_id, operation_id)` cannot create two jobs;
invalid status bundles and hash mismatches fail closed; SQLite and PostgreSQL
schema tests pass. Run serial PostgreSQL tests with:

```bash
cd "$ELSPETH_WORKTREE" && \
  log="$(mktemp /tmp/composer-async-schema.XXXXXX.log)" && \
  PYTHONPATH="$ELSPETH_WORKTREE/src:$ELSPETH_WORKTREE/elspeth-lints/src" \
  "$ELSPETH_WORKTREE/.venv/bin/python" -m pytest \
  tests/testcontainer/web/test_schema_probe_postgres.py -m testcontainer -n 0 \
  > "$log" 2>&1
status=$?
printf 'exit=%s log=%s\n' "$status" "$log"
tail -50 "$log"
exit "$status"
```

## 2. Implement reservation, claim, and terminal transactions

**Modify:** `src/elspeth/web/sessions/service.py` and the session-store
repository/facet that owns `guided_operations` writes. Keep the transport
writer in the same session mutation authority; do not add a second direct
engine writer from route code.

**Test:** `tests/unit/web/sessions/test_composer_async_operation_service.py`,
`tests/testcontainer/web/test_cross_process_composer_postgres.py`,
`tests/testcontainer/web/test_session_operation_fence_postgres.py`.

1. Reserve by client UUID, session, kind, actor, and canonical request hash.
   Same binding returns the existing job; any mismatch returns 409. Commit the
   queue row before a route can send 202. Add bounded queue capacity and one
   absolute deadline at admission from the configured compose budget. Queue
   time and provider time share that deadline. The three guided
   routes use `guided_operation_request_hash` exactly; the freeform routes
   use a versioned equivalent that also excludes `operation_id`.
2. Claim the oldest available queued job per session with a database token.
   PostgreSQL uses `FOR UPDATE SKIP LOCKED`; SQLite uses a transactional
   compare-and-swap. A queued claim may be reclaimed after expiry only because
   no side effect is permitted before `running`.
3. Require the existing `SessionOperationLease` COMPOSE authority before
   transitioning to `running`. On conflict, release the queue claim and retry
   later. All side-effecting service calls must verify the live transport and
   session lease fences. A running lease is renewed by the server, not by SPA
   polls. An expired running job never replays; it honors a committed cancel
   or terminalizes as `worker_lost`.
4. Commit a validated public success or safe failure envelope with its hash
   under the job fence. For freeform turns, the final result and assistant
   publication share a transaction. For guided turns, bind the internal guided
   operation to the same UUID/hash. Prepare the validated public DTO before
   the final transaction, then commit internal completion and transport
   publication atomically. A repair path never invokes the provider again.

**Gate:** Concurrent same-ID POSTs result in one row; different instances
cannot run two turns for one session; stale owners cannot commit; a crash
before `running` is reclaimable and a crash after `running` is terminal without
provider replay. Use controlled interleavings on both SQLite and PostgreSQL.

## 3. Add the app-owned worker and reaper

**Create:** `src/elspeth/web/sessions/composer_async_worker.py`.
**Modify:** `src/elspeth/web/app.py`,
`src/elspeth/web/sessions/routes/_helpers.py`, and the five route modules.
**Test:** `tests/unit/web/sessions/routes/test_composer_async_operations.py`,
`tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py`,
`tests/unit/web/sessions/routes/test_composer_request_telemetry.py`.

1. Start a bounded scanner/worker and reaper in application lifespan, after
   session-store readiness. Cancel and drain owned tasks during graceful
   shutdown; leave lease expiry as the unclean-crash backstop. Each instance
   can claim jobs; no sticky request routing is required.
2. Build an owned worker context from the strict DTO and authenticated actor.
   Extract the lease heartbeat, failure marker, metrics, and LLM-call audit
   handling from `_track_compose_inflight`. The task doing provider work is
   the task whose cancellation counter and audit-bearing `CancelledError` are
   examined. It must not retain a FastAPI `Request`, read `request.receive()`,
   or use `_cancel_on_client_disconnect`.
3. Move transcript and state checks that require a session lease into the
   worker. A later 400/404/409/429 is a terminal error envelope, even if no
   provider was called. Keep authentication, ownership, DTO validation,
   duplicate-ID binding, and queue-capacity refusal in the short POST.
4. Give the worker only the time remaining until the admission deadline.
   Poll failures or browser
   absence do not cancel the worker. Loss of either server lease cancels the
   task and fences its writes; metrics and public failure classification name
   the actual cause.
5. Decouple `composer_timeout_seconds` from the HTTP transport idle ceiling in
   `src/elspeth/web/config.py` and
   `deploy/aws-ecs/terraform/modules/scenario/variables.tf`; update
   `tests/unit/web/test_config.py` and
   `tests/unit/deployment/test_aws_ecs_terraform_package.py`. Keep the
   transport settings and their internal headroom validation because
   `src/elspeth/web/composer/tutorial_service.py` still uses them for a
   synchronous run wait. Update the Terraform README and matching deployment
   text. Prove a compose budget longer than the declared proxy ceiling boots
   with the async routes, while an invalid transport ceiling/headroom pair
   still fails.

**Gate:** The reproduction from task 0 now returns 202 while the provider is
held, and polling yields the exact former result. Heartbeat tests target the
worker's actual task and still distinguish client Stop, worker lease loss,
external shutdown, and audit attachment.

## 4. Expose polling and explicit cancellation

**Create or modify:** `src/elspeth/web/sessions/routes/composer/operations.py`,
`src/elspeth/web/sessions/schemas.py`, and route registration in
`src/elspeth/web/sessions/routes/composer/__init__.py`.

**Test:** `tests/unit/web/sessions/routes/test_composer_async_operations.py`,
`tests/testcontainer/web/test_cross_process_composer_postgres.py`.

1. Add ownership-checked GET by exact session and operation ID. Return the
   closed status union with no-store headers. Do not infer completion from a
   progress phase, local task map, or `inflight_requests`.
2. Add an idempotent cancel POST. Persist `cancel_requested_at` under the job
   row lock. Signal a local owner and let a remote owner observe it at the
   bounded renewal. Keep the job nonterminal until the task and required
   audit are settled. A completed result wins if it committed first.
3. Implement a token-checked reaper for queued-claim expiry, operation deadline,
   running lease expiry, and cancel/expiry races. Distinguish worker loss from
   client cancellation; mark audit evidence unproducible explicitly when
   process death prevents it. A worker finishing after expiry cannot write.

**Gate:** Different app instances can submit, poll, and cancel one job.
Cancel/completion/expiry races each produce exactly one terminal result and
no late session mutation.

## 5. Cut over all five HTTP routes

**Modify:** `src/elspeth/web/sessions/routes/messages.py`,
`src/elspeth/web/sessions/routes/composer/compose.py`,
`src/elspeth/web/sessions/routes/composer/guided_plan.py`,
`src/elspeth/web/sessions/routes/composer/guided.py`,
`src/elspeth/web/sessions/routes/composer/guided_chat_atomic.py`,
`src/elspeth/web/sessions/schemas.py`.

**Test:** `tests/unit/web/sessions/test_routes.py`,
`tests/unit/web/sessions/test_recompose_admission_refused.py`,
`tests/unit/web/sessions/routes/composer/test_guided_plan_terminal_publication.py`,
`tests/unit/web/sessions/test_guided_chat_integrity.py`, plus the new route
integration test.

1. Require a strict client `operation_id` on send and recompose. Preserve the
   existing guided UUID requirement. Each route performs only stable admission,
   commits/replays a transport job, then returns the same 202 handle.
2. Move each original handler's turn body into the worker entrypoint with an
   owned context. Retain the same provider calls, graph validation, result
   projection, guided transition, and audit paths. No server-authored proposal
   or tutorial branch may appear.
3. Remove `_track_compose_inflight` from these five route signatures only
   after every route uses operation settlement. Keep its other users, if any,
   measured against the current source. Update old tests for the new HTTP
   contract without weakening their underlying behavior assertions.

**Gate:** Each route returns 202 before provider release; each terminal poll
matches its former success or public error DTO; same-ID replay makes one
provider turn; missing/stale context cannot commit. The five-route inventory
from task 0 is completely reconciled.

## 6. Cut over the SPA as one operation lifecycle

**Modify:** `src/elspeth/web/frontend/src/api/client.ts`,
`src/elspeth/web/frontend/src/types/api.ts`,
`src/elspeth/web/frontend/src/types/guided.ts`,
`src/elspeth/web/frontend/src/stores/sessionStore.ts`,
`src/elspeth/web/frontend/src/stores/guidedOperationRetry.ts`,
`src/elspeth/web/frontend/src/config/composer.ts`,
`src/elspeth/web/frontend/src/hooks/useComposer.ts`, and
`src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx`.

**Test:** `src/elspeth/web/frontend/src/api/client.recovery.test.ts`,
`src/elspeth/web/frontend/src/api/client.guided.test.ts`,
`src/elspeth/web/frontend/src/stores/sessionStore.test.ts`,
`src/elspeth/web/frontend/src/stores/sessionStore.guided.test.ts`,
`src/elspeth/web/frontend/src/hooks/useComposer.test.ts`, and
`src/elspeth/web/frontend/src/components/chat/ChatPanel.test.tsx`.

1. Mint and durably remember one UUID/body before each action. After a lost
   202, poll that UUID and retry only the same binding. Reload and session
   switching reattach without starting another turn.
2. Decode the five-way success union and public terminal error envelope.
   Feed successes into existing route-specific reducers so proposals,
   version-change validation reset, guided state, and interpretation events
   still update. Progress and in-flight messages remain UX pollers only.
3. Make Stop call the cancel endpoint and wait for terminal settlement. Change
   the client deadline to the server operation deadline plus existing grace,
   followed by explicit cancel if the row remains active;
   `AbortController` only bounds individual short fetches. Retry transient
   polling errors with backoff without displaying a false failure.

**Gate:** A lost acknowledgement, page reload, session switch, Stop, deadline,
poll outage, stale transcript, and all five success DTOs behave correctly.
No optimistic item is duplicated and no pending indicator clears on 202 alone.

## 7. Integrate and accept

1. Run focused backend and frontend tests as each task lands. Before
   integration run Ruff, mypy, contracts, the key-free trust-tier comparison,
   frontend typecheck/lint/build/Vitest, and the canonical full suite on a
   frozen tip. The global judge-signature CI red is the existing operator
   signing boundary; do not re-sign or clear it during this feature.
2. Run `pytest tests/ -m testcontainer -n 0` serially for the schema,
   transaction, and cross-instance gates. Use unique log files, capture each
   terminal exit, and read `summary.txt` from
   `scripts/full-suite-gate.sh --execute --detach`; partial output is not a
   pass.
3. Exercise a real browser against an origin with a deliberately short
   front-end idle timeout: hold a provider longer than that cutoff, observe
   202 and continuing polls, then a completed result. Repeat Stop and reload.
   This is local acceptance; live-cloud acceptance remains a separate
   operator-controlled deployment step.
4. Recheck the exact release tip, schema epoch, existing users' session-store
   reset requirement, and `bootstrap-admin` procedure before deployment.
   Update user-facing API documentation and the Filigree ticket from measured
   implementation results. Do not claim the proxy problem fixed merely
   because a unit suite passed.

**Final acceptance:** One short POST plus durable poll replaces each of the
five buffered routes. The same operation ID survives lost responses and
different instances; terminal results preserve the old public success and
failure meanings; cancellation and worker loss have fenced, auditable
outcomes; the final-tip gates have recorded exits.
