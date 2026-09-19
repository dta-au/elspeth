# Composer async operations

- **Status:** Revised design for implementation planning. This replaces the
  rejected 2026-09-16 draft; the [review](2026-09-16-composer-async-operations-review.md)
  remains a historical review of that draft.
- **Ticket:** elspeth-7a663a062c (blocks elspeth-ad5628ecda)
- **Target release:** 0.8.1 (`release/0.8.1`). Recheck the branch tip before
  implementation or release-note edits.
- **Plan:** [2026-09-20-composer-async-operations.md](../plans/2026-09-20-composer-async-operations.md)

## Problem and acceptance

A Composer turn can take minutes while its HTTP response sends no bytes. A
middlebox can end that request before the origin's compose budget, destroying a
healthy turn. Cloudflare cut measured requests at about 125 seconds and a
client-side Zscaler hop cut a DTA-Dev request at about 60 seconds. Changing a
server or ALB timeout cannot establish a bound for every user's network.

The fix is complete when every long Composer authoring route acknowledges an
accepted request promptly, continues the turn without the POST socket, and
exposes its durable terminal outcome through short, authenticated polls. A
lost acknowledgement, page reload, Stop, worker death, and a request routed to
a different instance must each have a defined outcome. The LLM remains the
author of pipeline structure, and guided and tutorial paths use the same
backend rules.

The five routes using `_track_compose_inflight` are the cutover set:

| Operation kind | Existing route | Existing success shape |
|---|---|---|
| `compose_message` | `POST /{session_id}/messages` | `MessageWithStateResponse` |
| `compose_recompose` | `POST /{session_id}/recompose` | `MessageWithStateResponse` |
| `guided_plan` | `POST /{session_id}/guided/plan` | `CompositionProposalResponse` or `GuidedPlanDeclinedResponse` |
| `guided_respond` | `POST /{session_id}/guided/respond` | `GuidedRespondResponse` |
| `guided_chat` | `POST /{session_id}/guided/chat` | `GuidedChatResponse` |

`/guided/start`, state edits, proposal decisions, execution, and run routes
are outside this cutover. They do not use the five-route request lifecycle.
The client and tests must show that no route in the table retains a long,
buffered POST. No tutorial-only dispatch or provider bypass is permitted.

## Decisions

### 1. A transport job is separate from the existing guided operation

Add a `composer_async_operations` table. It is the durable admission, worker,
poll, and cancellation authority for the five routes. Keep `guided_operations`
and its existing name, checks, result locators, and synchronous callers. The
guided routes still use their guided operation internally, bound to the same
client operation ID and request hash. The transport job carries the new
asynchronous lifecycle; the guided record retains its audit and mutation
fence. A terminal guided result and its validated public transport result
must commit in the same session transaction. The worker must prepare that
public projection before committing the internal result. A worker must never
run a second provider turn merely to repair a missing transport result.

This avoids the rejected draft's false `kind` constraint, illegal
`result_message_id` shape, table rename, and partial account of rename sites.
The new table has a composite primary key `(session_id, operation_id)`, a
foreign key to the session, a closed five-value `kind`, and a CHECK for each
status shape. The relevant fields are:

| Field group | Contract |
|---|---|
| Identity | `session_id`, client-minted canonical UUID `operation_id`, `kind`, canonical `request_hash`, authenticated `actor_user_id` |
| Queue | `status` = `queued`, `running`, `completed`, or `failed`; `created_at`, `updated_at`, absolute operation deadline |
| Custody | claim token, owner instance, lease expiry, attempt, and `cancel_requested_at` |
| Input | bounded, strict DTO JSON while queued or running; cleared on terminal settlement |
| Output | exactly one validated success response JSON or safe failure envelope on terminal settlement, a schema discriminator, SHA-256 of canonical JSON, and `settled_at` |

The request JSON is session data, not a log or metric. It contains no bearer
token, cookie, or request object. It uses the same database access boundary as
other session data and is removed atomically at settlement. Session deletion
removes the job. Result JSON is the *actual* validated public response DTO,
including `MessageWithStateResponse.proposals` and guided union outcomes. A
single proposal locator cannot represent that response. Store only the
existing public projection; never store an internal provider object or raw
exception. Canonical hashing and response-schema validation run before
publication and again on read. The completed payload and hash are immutable.

The new table has two distinct lease states. A claimed `queued` row can be
reclaimed after claim expiry because no turn side effect is allowed before
`running`. An expired `running` row is never taken over for another provider
attempt. It settles `worker_lost` unless a committed cancellation determines
the outcome. The worker's claim and transition to `running` are
compare-and-swap writes fenced by its token.
All worker-authored session writes, including the final result, require both
the existing `SessionOperationLease` context and the transport job's live
running fence. Loss of either fence cancels work and forbids a late write.

### 2. Client-minted identity and HTTP contract

The SPA mints one canonical UUID *before* each user action and persists that
ID and the strict request body locally until it observes a terminal result.
The freeform send and recompose DTOs gain `operation_id`; the three guided
DTOs already require one. A retry uses the exact same ID and body. The server
hashes normalized body content excluding `operation_id`, with session and kind
in the binding. The three guided routes use their existing
`guided_operation_request_hash` byte for byte; the freeform routes use an
equivalent versioned codec. The primary key atomically
prevents two reservations of the same ID; the existing row is returned only
when kind, actor, and hash all agree. A mismatch is 409. A new ID is a new
user action, not a network retry.

After authentication, session ownership, strict body validation, durable
capacity admission, and an atomic queue insert commit, every accepted POST
returns `202 Accepted` with:

```json
{"operation_id":"<client UUID>","kind":"compose_message","status":"queued","poll_after_ms":1000}
```

The same valid ID/body returns 202 with the same ID and its current status,
whether queued, running, or terminal. The POST does not wait for a session
compose lock, a provider, or a worker claim. Authentication and ownership
failures retain 401/403/404; malformed bodies retain 422; a bound-ID mismatch
is 409; a full worker queue is a synchronous 429 with a retry hint. A transport failure
before the client receives 202 is ambiguous: the client first polls by its
known ID, then resubmits the *same* ID/body only if no row exists. An initial
404 from that poll is not evidence that an in-flight POST cannot still commit;
the retry's unique-key conflict/replay is the final arbiter.

`GET /api/sessions/{session_id}/operations/{operation_id}` returns 200 with
`kind`, `status`, `cancel_requested`, and one of `result` or `error` on a
terminal row. It checks current session ownership on every call. A missing
job is 404. `result` is a discriminated union of the five existing success
DTOs; the client applies it through each route's current success reducer.
`error` contains the safe public `http_status`, existing `error_type` where
present, and the public response body. A terminal internal defect has a
generic `operation_failed` envelope and a server-side diagnostic ID. The
poll never returns an arbitrary exception string or secret-bearing detail.
The poll uses `Cache-Control: no-store`; an optional `Retry-After` is advisory.

The operation row, not Composer progress or `inflight_requests`, is the
authority for settlement. Progress snapshots remain advisory phase UX and
can be process-local on SQLite. Both frontend pollers can continue while a
job is active, but they cannot declare success, failure, or quiescence.

### 3. Worker and admission ordering

An app-lifespan worker on every instance scans committed `queued` jobs. On
PostgreSQL, it claims with `FOR UPDATE SKIP LOCKED` and a token-checked update;
on SQLite, a single-instance transactional compare-and-swap supplies the same
claim contract. Claims are bounded by configured worker capacity. The worker
chooses the oldest queued job per session and respects the existing durable
`SessionOperationLease` COMPOSE exclusion. The local `asyncio.Lock` can still
serialize work within one process, but it is a queueing aid, not the
cross-instance authority. No PostgreSQL advisory lock is added.

The worker claims a queued row, obtains `SessionOperationLease`, rechecks
session ownership and transcript/state preconditions under that authority,
then atomically marks the job `running` before any user-message insert,
provider call, or guided transition. If another session operation owns the
lease, the worker releases its claim and leaves the job queued for a later
scan. A queued claim lost to process death expires and is safely reclaimable.
One absolute operation deadline is set at admission from the existing
`composer_timeout_seconds` budget. Queue time consumes that budget; the worker
uses only the remaining time. Expiry while queued settles `failed` with a
public timeout envelope without starting a turn.

Checks whose answer can change while waiting for the session lease are worker
checks. A preexisting user message, stale state/turn token, changed quota, or
competing guided transition therefore may yield 202 followed by a terminal
400/404/409/429 envelope. The SPA must render that envelope with the same
meaning as today's synchronous error. The design does not promise a
synchronous error from a check performed after a lock wait. Only the short,
stable admission checks listed in §2 run before 202.

The worker owns its task, lease renewal, cancellation marker handling, and
metrics. `_track_compose_inflight` is a FastAPI request-scoped yield dependency
and must not be reused as the task owner. Extract its renewal/failure policy
into an operation-owned lifecycle. Remove that dependency from the five
routes only when their frontend settlement path uses the operation row.
Build a typed worker context from the validated DTO, session identity,
authenticated actor ID, and safe request metadata before returning 202.
The worker receives no `Request` and never calls `request.receive()`.
`_cancel_on_client_disconnect` is not mounted around a detached turn. Its
`CancelledError` audit evidence and `uncancel()` bookkeeping must be preserved
in an operation-owned cancellation path, with the LLM-call audit cohort
persisted before terminal publication.

The worker renews both its job claim and session operation lease from the
server, independently of browser polls. An absent browser or a broken poll
does not end the turn. The existing compose wall-clock budget still bounds
queueing and provider work together; the hard 202 cutover does not extend it.
Worker shutdown
requests cancellation and drains owned tasks; an unclean crash is resolved by
lease expiry and a startup/periodic reaper. No reaper resumes a `running` turn.

The current `WebSettings` boot validator and ECS Terraform validation require
`composer_timeout_seconds` to fit below the declared HTTP transport idle
ceiling. That coupling must be removed for these five asynchronous turns or
the application still cannot fund a turn longer than a user's shortest
unknown proxy timeout. Keep the positive compose budget and its planning
warning. Keep the transport ceiling and headroom for synchronous routes that
still use them, including the tutorial run wait in `tutorial_service.py`, and
validate that those two settings remain internally consistent. Update the
ECS plan-time cap, config tests, deployment documentation, and any mirrored
comments together. This is a change to the *relationship* between budgets,
not a request to increase the compose timeout or ALB idle timeout.

### 4. Cancellation and races

`POST /api/sessions/{session_id}/operations/{operation_id}/cancel` checks
current ownership and atomically sets `cancel_requested_at` on a queued or
running job. For a queued, unclaimed job it may settle `request_cancelled`
in that same transaction. For an owned job it returns `202` with status
`cancel_requested`; the job remains nonterminal until the owner has stopped
and persisted required audit evidence. The local owner is signalled directly;
a remote owner observes the marker on its next bounded renewal. A lost lease
also cancels its local task. Cancellation latency is bounded by the renewal
interval plus a database call, rather than claimed to be immediate.

Every terminal write is a single compare-and-swap against the live claim
token and `cancel_requested_at`. If cancel commits first, completion is
rejected, late session writes are fenced, and the worker settles
`request_cancelled` after its audit join. If completion commits first, cancel
returns the already completed result and does not relabel it. A reaper uses
the same comparison: a lapsed running lease becomes `worker_lost`, or
`request_cancelled` if a cancel marker was already committed, with explicit
audit-incomplete diagnostics when process death made
the final audit unproducible. It cannot silently claim the task never ran.

The essential crash windows have these outcomes:

| Window | Required outcome |
|---|---|
| Queue commit before 202; response lost | Client polls/retries its ID; no second row or turn. |
| 202 before worker claim | Any live instance can claim the durable queued row. |
| Worker dies before `running` | Queued claim expires; another worker may claim. No side effect has started. |
| Worker dies after `running` | Reaper publishes `worker_lost` or the committed cancel. No provider replay. Existing fenced writes and audit remain visible on reload. |
| Cancel, completion, and lease expiry race | One token-checked terminal transition wins; the others read that terminal row. |

For the three guided routes, existing guided-operation takeover may be used
only to **terminalize** a stranded internal row after worker loss; it must not
repeat the planner/provider turn. Internal guided settlement and transport
publication are one transaction, so no committed internal terminal can exist
without its transport terminal. For freeform routes, persist the public final
response and transport terminal in the same session transaction as the final
assistant/result publication. Earlier audit or user rows may remain after
failure, as they can today; the SPA reloads
authoritative state before offering a new action. A retry of the same ID never
creates another originating user row.

### 5. Frontend and failure semantics

All five route callers use one operation transport: submit, recover ambiguous
acknowledgement, poll to terminal, then dispatch the existing route-specific
success or error reducer. The success reducer must still apply messages,
composition state, **all proposals**, guided session, next turn, terminal
state, validation clearing, selection changes, and interpretation refreshes.
The current `MessageWithStateResponse` body cannot be replaced by a state ID
alone. A page reload resumes polling the persisted operation ID. Session
switches stop local pollers but do not cancel server work; returning to the
session reattaches to its active operation. A second action for the same
session remains disabled while a job is nonterminal.

The Stop button calls the cancel endpoint for that exact operation ID and
waits for the operation's terminal state. `AbortController` is used only to
bound individual short HTTP calls; aborting a completed 202 fetch does not
cancel a turn. The existing client compose deadline tracks the server
operation deadline plus its existing client grace. If the server has not
settled by then, the client issues an explicit cancel and reconciles the
terminal row. A transient poll error retries with backoff
and does not declare the turn failed. The existing progress and in-flight
message pollers remain for UX but stop when the operation is terminal.

Post-202 errors preserve the current public HTTP error semantics inside the
terminal envelope, including provider authentication versus availability,
the three convergence reasons, policy and integrity refusals, quota, and
stale conflicts. The adapter uses the existing safe error projection; it
does not collapse these to one `operation_failed` code. Each raise path in
the five routes must be inventoried in the implementation plan and proven
to land either before 202 or in the terminal envelope. Unknown exceptions
use the generic envelope only after audit and server diagnostics are retained.

## Verification gates

1. Schema and transaction tests on SQLite and serial PostgreSQL prove the
   status CHECKs, one-ID reservation, claim/reclaim, cross-instance COMPOSE
   exclusion, cancel/completion/expiry races, and atomic terminal result.
2. A provider held beyond 125 seconds returns a short 202, survives the
   client POST disconnect, and yields the exact former success DTO by poll.
   The same test family covers all five routes and proves one provider turn.
3. Kill a worker in each crash window above. A queued job resumes safely;
   a running job terminates without replay; stale owners cannot write.
4. Verify cancellation audit evidence, server-fault classification, and
   `current_task().cancelling()` in the *actual* worker task. Adapt the old
   heartbeat tests to the new owner without weakening their failure intent.
5. Frontend tests cover lost 202, reload, cross-instance poll, Stop, deadline,
   stale transcript, exact response application, and a poll outage.
6. Run the canonical full-suite gate and the serial testcontainer selection
   before integration. Capture terminal exits and frozen-tree evidence; a
   scoped green run alone does not certify this cross-cutting change.

The [implementation plan](../plans/2026-09-20-composer-async-operations.md)
names the files, order, and acceptance tests for these gates.
