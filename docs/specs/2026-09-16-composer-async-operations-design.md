# Composer async operations

- **Status:** Revised design for implementation planning. This replaces the
  rejected 2026-09-16 draft; the [review](2026-09-16-composer-async-operations-review.md)
  remains a historical review of that draft.
- **Scope (2026-09-28):** freeform is the only composer. Guided was removed
  from the product in `7001600fe` (2026-09-28). The cutover covers
  `/messages` and `/recompose`. The first-run tutorial's Build sends through
  `/messages`, so the cutover covers it with no tutorial branch.
- **Second amendment (2026-09-28):** folds in the storage panel's
  recommendation (B′) and owner rulings 1–7
  ([`RULINGS.md`](../plans/2026-09-20-composer-async-operations/panel-2026-09-28/RULINGS.md)).
  The binding names, signatures, columns and wire shapes are in the
  [interface contract](../plans/2026-09-20-composer-async-operations/contract.md);
  where this document and the contract differ, the contract wins.
- **Ticket:** legacy elspeth-7a663a062c (blocks elspeth-ad5628ecda). These are
  historical tracker identifiers; neither has a GitHub issue as of 2026-09-25.
- **Target release:** 0.8.1 (`release/0.8.1`). Recheck the branch tip before
  implementation or release-note edits.
- **Plan:** [2026-09-20-composer-async-operations.md](../plans/2026-09-20-composer-async-operations.md)
  (the plan index).

## Problem and acceptance

A Composer turn can take minutes while its HTTP response sends no bytes. A
middlebox can end that request before the origin's compose budget, destroying a
healthy turn. Cloudflare cut measured requests at about 125 seconds and a
client-side Zscaler hop cut a DTA-Dev request at about 60 seconds. Changing a
server or ALB timeout cannot establish a bound for every user's network.

The fix is complete when every long Composer authoring route acknowledges an
accepted request promptly, continues the turn without the POST socket, and
exposes its durable terminal outcome through short, authenticated polls. A
lost acknowledgement, page reload, Stop, worker death, a deploy, a base that
moved while the request waited, and a request routed to a different instance
must each have a defined outcome. The LLM remains the author of pipeline
structure; no provider bypass is introduced.

The two freeform routes using `_track_compose_inflight` are the cutover set:

| Operation kind | Existing route | Existing success shape |
|---|---|---|
| `compose_message` | `POST /{session_id}/messages` | `MessageWithStateResponse` |
| `compose_recompose` | `POST /{session_id}/recompose` | `MessageWithStateResponse` |

These are the only production mounts of `_track_compose_inflight`; the
dependency is deleted with the cutover. State edits, proposal decisions,
execution, and run routes are outside this cutover. The client and tests must
show that neither route in the table retains a long, buffered POST. No
tutorial-only dispatch or provider bypass is permitted.

## Decisions

### 1. A new transport job table; `session_operation_receipts` is untouched

Add a `composer_async_operations` table. It is the durable admission, worker,
poll, and cancellation authority for the two freeform routes, with its own
single authority and table policy. The `session_operation_receipts` table,
its events, its authority, and its writer pin do not change; only its request
hash function delegates to the shared normaliser described in §2. Receipts
are the wrong home for a compose job: a receipt takes over an expired
in-progress row and rebuilds its response from locators, while a compose job
must never take over a running row and must store the full response DTO,
because parts of that response (live validation) are never persisted. The
worker must prepare the validated public response before its final commit. A
worker must never run a second provider turn merely to repair a missing
transport result.

The table has a composite primary key `(session_id, operation_id)`, a closed
two-value `kind` (`compose_message`, `compose_recompose`), a CHECK for each
status shape, and closed CHECKs on its failure code and settlement path. A
later kind is a schema change with its own epoch bump, not a dual-acceptance
widening. The relevant fields are:

| Field group | Contract |
|---|---|
| Identity | `session_id`, client-minted canonical UUID `operation_id` (also the send's `message_ingress_receipts` key, §2), `kind`, canonical `request_hash`, authenticated `actor_user_id`, the POST's `request_id`, and the bound base `base_state_id` (`NULL` = "no state") |
| Queue | `status` = `queued`, `running`, `completed`, or `failed`; `created_at`, `updated_at`, `started_at`, absolute operation deadline |
| Custody | claim token and owner instance; claim expiry only while `queued`; `attempt`; `cancel_requested_at`; the bound session operation fence (`session_operation_id`, lease token, epoch) set at `running`. Owner, attempt, and the fence are kept at settlement |
| Input | bounded, strict DTO JSON while queued or running; cleared on terminal settlement |
| Output | exactly one validated success response JSON or safe failure envelope on terminal settlement, a schema discriminator, SHA-256 of canonical JSON, a closed `failure_code`, a closed `settled_by` naming the path that settled the row, the originating `user_message_id`, and `settled_at` |

The request JSON is session data, not a log or metric. It contains no bearer
token, cookie, or request object. It uses the same database access boundary as
other session data and is removed atomically at settlement. Result JSON is the
*actual* validated public response DTO, the full `MessageWithStateResponse`
including its `proposals` list. A single proposal locator cannot represent
that response. Store only the existing public projection; never store an
internal provider object or raw exception. Canonical hashing and
response-schema validation run before publication and again on read. The
completed payload and hash are immutable.

Settled rows are retained with their session and are not a separate
retention class (ruling 4). A database delete guard refuses to delete a job
row while its session row exists; session deletion removes the job by
cascade, and the job never blocks archive. Clearing the request JSON at
settlement does not lose the forensic record: `base_state_id` stays on every
row, a send's user row is bound through `user_message_id` and its ingress
receipt, and a recompose binds `user_message_id` to the user row it retried
when it starts. There is no compose events table (ruling 3); the forensic
columns on the terminal row answer which instance ran or reaped a turn, on
which attempt, and by which path.

The foreign-key actions are chosen so that a session delete cascades on both
SQLite and PostgreSQL: job→session `CASCADE`; job→actor identity `RESTRICT`;
job→user message and job→base state `NO ACTION`, which is checked at
statement end so cascade order cannot fail the delete; and
ingress→job on `(session_id, operation_id, user_message_id)` `CASCADE`.

The new table has two distinct lease states, and the database enforces the
difference rather than leaving it to design intent. A claimed `queued` row can
be reclaimed after claim expiry because no turn side effect is allowed before
`running`; every reclaim predicate includes `status = 'queued'`. A `running`
row carries no claim expiry at all: its liveness *is* the liveness of the
COMPOSE `SessionOperationLease` (SOL) that the same start transaction minted
(R3). A transition-guard trigger forbids a running row returning to queued,
any change to its identity, base, owner, or bound fence, clearing
`cancel_requested_at`, changing `user_message_id` once set, and any update of a
terminal row. An expired `running` row is therefore never taken over for
another provider attempt. It settles `worker_lost` unless a committed
cancellation determines the outcome. The worker's claim and transition to
`running` are compare-and-swap writes fenced by its token. At most one
nonterminal job exists per session; a partial unique index on `session_id`
over `queued` and `running` rows enforces it (D8).

A write under a fence bound to a job requires that job to be `running` with
`cancel_requested_at IS NULL`, unless the write is `audit_only` (ruling 2).
Loss of the fence, a cancel marker, or a terminal job therefore cancels work
and forbids a late write, including a write that would follow the terminal
under a lingering SOL. §4 names the sites and the audit-only writers. The
bound fence stays on terminal rows because this predicate finds the job
through it; nulling it would make the predicate fail open.

### 2. Client-minted identity and HTTP contract

The SPA mints one canonical UUID *before* each user action and persists that
ID and the strict request body locally until it observes a terminal result.
There is one ID per send, and its wire name is `operation_id` everywhere: the
request DTOs, the 202 acknowledgement, the poll URL, the transcript field
(`ChatMessageResponse.client_request_id` is renamed to `operation_id`), and
the ingress column (renamed from `client_request_id` in the epoch cut). There
is no alias, no second client key, and no dual acceptance (rulings 1 and 6).
The freeform send and recompose DTOs are strict and reject unknown fields.
Both carry `state_id`, the head the operator saw when authoring the request;
recompose also carries `expected_user_message_id`. An absent `state_id` means
"no state", read literally, and hashes identically to an explicit `null`
(ruling 7).

The `message_ingress_receipts` row stays the immutable acceptance record of a
send, but its key becomes the job's `operation_id` and the worker writes it,
not the route. In the send's user-row transaction, through the existing single
ingress writer path, the worker inserts the user row, binds that row to the
job's `user_message_id`, then inserts the ingress receipt keyed by the
operation ID with its requested state equal to the job's `base_state_id`. The
composite foreign key from ingress to the job makes the two agree by schema.
An ingress row that already exists for the operation at that point is a
Tier-1 integrity failure, never a 409. The route's ingress lookup, its
`message_already_accepted` and `message_idempotency_conflict` 409 arms, and
the SPA's transcript-matching recovery are deleted at cutover. Recompose
inserts no user row and has no ingress row.

A retry uses the exact same ID and body. The server hashes the normalized
body excluding `operation_id` (including `state_id` and, for recompose,
`expected_user_message_id`), with session and kind in the binding. One
neutral request normaliser serves both this facility and session operation
receipts, each with its own closed schema tag, and one strict response hash
is shared the same way. A golden-vector test, computed from the unmodified
receipts codec before the extraction, pins receipt hashes byte for byte; if
that vector moves, a separate compose codec with the same body ships instead
and receipts are left alone. The primary key atomically prevents two
reservations of the same ID; the existing row is returned only when kind,
actor, and hash all agree. A mismatch is 409 `composer_operation_conflict`. A
new ID is a new user action, not a network retry.

Admission runs in this order: authentication, body decode, session ownership,
strict body validation, primary-key lookup (the same ID and binding replays
the existing row with no charge; a mismatch is 409), the per-user rate limit
for new IDs only, a soft capacity check, and an atomic queue insert. There is
no ingress lookup before 202. Capacity is a soft cluster cap: concurrent
admissions for different sessions can overshoot it by at most the number of
concurrent admitters. The one-nonterminal-job index turns a second new ID on a
busy session into 409 `composer_operation_active`, whose body carries the
existing job's `operation_id` and `kind` so the client can attach to it.
Every accepted POST returns `202 Accepted` with:

```json
{"operation_id":"<client UUID>","kind":"compose_message","status":"queued","poll_after_ms":1000}
```

The same valid ID/body returns 202 with the same ID and its current status,
whether queued, running, or terminal. The POST does not wait for a session
compose lock, a provider, or a worker claim, and it checks nothing about the
bound base beyond body validity; the base is checked when the job starts (§3).
Authentication and ownership failures retain 401/403/404; malformed bodies
retain 422; a bound-ID mismatch or an active job is 409; a rate-limited new ID
keeps today's synchronous 429, and a full queue is a synchronous 429
`composer_queue_full` with a `Retry-After` hint. A transport failure before
the client receives 202 is ambiguous: the client first polls by its known ID,
then resubmits the *same* ID/body only if no row exists. An initial 404 from
that poll is not evidence that an in-flight POST cannot still commit; the
retry's unique-key conflict/replay is the final arbiter.

`GET /api/sessions/{session_id}/operations/{operation_id}` returns 200 with
`operation_id`, `kind`, `status`, `cancel_requested`, `poll_after_ms`,
`deadline_at`, `deadline_remaining_ms`, and one of `result` or `error` on a
terminal row. `deadline_remaining_ms` is computed on the database clock and is
0 on a terminal row. The poll checks current session ownership on every call.
A foreign or missing session is 404 `Session not found`; a missing job is 404
`Operation not found`. The poll never reads or returns the request JSON.
`result` is the existing `MessageWithStateResponse`, discriminated by the
row's `kind`; the client applies it through the matching route's current
success reducer. The read re-validates the stored hash and the strict DTO; a
mismatch is an audit integrity failure, not a degraded answer. `error`
contains the safe public `http_status`, the closed `failure_code`, the
existing `error_type` where present, and the exact public response body. A
terminal internal defect has a generic `operation_failed` envelope and a
server-side diagnostic ID. The poll never returns an arbitrary exception
string or secret-bearing detail. The poll uses `Cache-Control: no-store`; an
optional `Retry-After` is advisory.

The operation row, not Composer progress or `inflight_requests`, is the
authority for settlement. The worker keeps a composer request lease per
running job, so progress publishing, `inflight_requests`, and PostgreSQL
identity revocation keep working. Progress snapshots remain advisory phase UX,
can be process-local on SQLite, and are keyed by the persisted user-message ID
(the row the send inserts, or recompose's expected row), never by the
operation ID. A progress failure is logged and never decides the terminal.
Both frontend pollers can continue while a job is active, but they cannot
declare success, failure, or quiescence.

### 3. Worker and admission ordering

An app-lifespan worker on every instance scans committed `queued` jobs. On
PostgreSQL, it claims with `FOR UPDATE SKIP LOCKED` and a token-checked update;
on SQLite, a single-instance transactional compare-and-swap supplies the same
claim contract. Claims are bounded by configured worker capacity. At most one
nonterminal job exists per session (D8), so a session has at most one
claimable job. Claim discovery skips a session whose operation fence is live,
and the worker respects the existing durable `SessionOperationLease` COMPOSE
exclusion. The local `asyncio.Lock` can still serialize work within one
process, but it is a queueing aid, not the cross-instance authority. A claim
waiting on that lock is renewed, and the wait observes a local cancel, the
cancel marker, and the deadline. No PostgreSQL advisory lock is added.

The worker claims a queued row, then runs one locked start transaction that
advances the session's COMPOSE fence, checks the job's preconditions, and
marks the job `running` before any user-message insert, provider call, or
state write. The worker then adopts the SOL context that transaction minted.
The precondition gate runs, in order:

1. ownership: the session still belongs to the job's actor under the
   instance's auth provider;
2. a foreign or unknown base: a `base_state_id` that is not a state of this
   session is the byte-identical 404 `State not found` it is today;
3. a moved base: the current head must equal `base_state_id` by ID, and an
   absent base requires that the session has no head (rulings 5 and 7);
4. for recompose, the transcript: the three existing refusals (no messages,
   last row not a user row, `recompose_user_message_mismatch`) with their
   exact bodies.

A refusal rolls the whole start transaction back, including the fence
advance, and the worker settles the job as never started. There is no user
row, no provider call, and no started provider attempt. A moved or unstated
base is a terminal 409 `stale_compose_state` (§5). Because the head equals the
bound base when the turn starts, the turn still seeds from the head as it
does today, and a client whose view lagged is refused at start rather than
composed against a state it did not see.

If another session operation owns the lease, the worker releases its claim
and leaves the job queued for a later scan. A queued job holds no SOL, so
Run and diagnostics proceed while it waits; a running job makes them answer
today's 409 for an active session operation. A queued claim lost to process
death expires and is safely reclaimable. One absolute operation deadline is
set at admission from the existing `composer_timeout_seconds` budget. Queue
time consumes that budget; each later stage (the compose call, the planner,
auto-commit settlement) receives only the time remaining. Expiry while queued
settles `failed` with the public deadline envelope without starting a turn.

Checks whose answer can change while the job waits are start or turn checks,
not admission checks. A moved base, a changed transcript, a lost ownership, a
chargeable-admission refusal, or a stale conflict inside the turn may
therefore yield 202 followed by a terminal 400/403/404/409/503 envelope. The
SPA must render that envelope with the same meaning as today's synchronous
error. The design does not promise a synchronous error from a check performed
after a wait. Only the short, stable admission checks listed in §2 run before
202.

The worker owns its task, lease renewal, cancellation marker handling, and
metrics. `_track_compose_inflight` is a FastAPI request-scoped yield dependency
and must not be reused as the task owner. Extract its renewal/failure policy,
and the durable-completion signal it currently keeps on request state, into
an operation-owned lifecycle handle that both the worker and the still
synchronous routes use. Until cutover the two freeform routes keep identical
renewal, failure, 503, and metrics behaviour, proved by their existing
heartbeat and telemetry tests passing unmodified. Remove the dependency from
the two freeform routes only when their frontend settlement path uses the
operation row, and delete it then.
Build a typed worker context from the validated DTO, session identity,
authenticated actor ID, and safe request metadata before returning 202.
The worker receives no `Request` and never calls `request.receive()`.
`_cancel_on_client_disconnect` is not mounted around a detached turn. Its
`CancelledError` audit evidence and `uncancel()` bookkeeping must be preserved
in an operation-owned cancellation path, with the LLM-call audit cohort
persisted before terminal publication. The turn keeps the routes' owned
child-task settlement custody and typed error ladder; it rebuilds the route
bodies rather than moving them. Auto-title is joined before the terminal
transaction with today's two-second bound; on timeout it is cancelled and its
cancellation joined first, so no non-audit write follows the terminal and a
completed response is never replaced.

The worker renews both its job claim and session operation lease from the
server, independently of browser polls. An absent browser or a broken poll
does not end the turn. The existing compose wall-clock budget still bounds
queueing and provider work together; the hard 202 cutover does not extend it.
Worker shutdown stops claiming, releases any claim that returns after the
stop, cancels owned jobs with a shutdown marker, and joins each for at most
`composer_async_drain_seconds`. A job still unsettled after that bound is
left to a peer's reaper. Every deploy or drain therefore settles in-flight
turns as 503 `composer_operation_worker_lost`; this is documented behaviour,
not a defect. An unclean crash is resolved by lease expiry and a
startup/periodic reaper. No reaper resumes a `running` turn.

The current `WebSettings` boot validator and ECS Terraform validation require
`composer_timeout_seconds` to fit below the declared HTTP transport idle
ceiling. That coupling must be removed for the two asynchronous turns or
the application still cannot fund a turn longer than a user's shortest
unknown proxy timeout. Keep the positive compose budget and its planning
warning. Keep the transport ceiling and headroom for the synchronous
consumers that still use them, and validate that those two settings remain
internally consistent. Those consumers are run diagnostics, proposal Accept
settlement, and the tutorial run wait in `tutorial_service.py`. Once the
compose budget may exceed the transport ceiling they must not inherit it: a
synchronous consumer is bounded by the smaller of `composer_timeout_seconds`
and the transport-safe budget (ceiling minus headroom). Every configuration
valid before this change keeps the same synchronous bound. No new status key
is published for this cap; `/api/system/status` keeps `composer_timeout_seconds`
as an informational field. Update the ECS plan-time cap, config tests,
deployment documentation, and any mirrored comments together. This is a
change to the *relationship* between budgets, not a request to increase the
compose timeout or ALB idle timeout.

### 4. Cancellation and races

`POST /api/sessions/{session_id}/operations/{operation_id}/cancel` checks
current ownership and atomically sets `cancel_requested_at` on a queued or
running job. For a queued, unclaimed job it settles `request_cancelled` in
that same transaction. For an owned job it returns `202` with
`cancel_requested: true`; the job remains nonterminal until the owner has
stopped and persisted required audit evidence. A terminal job returns 200 with
its terminal row; a missing job is 404. The local owner is signalled directly;
a remote owner observes the marker on its next bounded renewal. A lost lease
also cancels its local task. Cancellation latency is bounded by the renewal
interval plus a database call, rather than claimed to be immediate. Only the
cancel endpoint's marker is a user Stop: an unmarked `CancelledError` out of
a detached turn settles `worker_lost`.

Every terminal write is a single compare-and-swap against the live claim
token, the bound fence, and `cancel_requested_at`. Every settlement records
its `settled_by` path and keeps the claim owner, attempt, and bound fence. If
cancel commits first, completion is rejected, late session writes are fenced,
and the worker settles `request_cancelled` after persisting the LLM-call
audit cohort `audit_only`. If completion commits first, cancel returns the
already completed result and does not relabel it. A reaper uses the same
comparison: a lapsed running lease becomes `worker_lost`, or
`request_cancelled` if a cancel marker was already committed, with explicit
audit-incomplete diagnostics when process death made the final audit
unproducible. It cannot silently claim the task never ran. A running job
whose session was archived after its owner died is settled by a dedicated
inactive-session path and never leaks capacity. A failure to close or renew
the SOL after the terminal commits, including an exception group from the
lease's exit, is logged and never relabels the committed terminal.

The positive predicate of §1 is enforced at the two session write families:
the session-service write-authority chokepoint (chat-message inserts and
composer mutation transactions), and the session operation authority's
`mutate` for COMPOSE contexts (the blob tools). The shared exact-fence
predicate is not changed, because renewal, release, adoption, and archive
deletion use it and must keep working after the terminal. The audit-only
writers are the LLM-call and turn-audit cohort persistence, the planner's
audit write, provider-attempt finish, settle, and cancel-before-dispatch, and
the cohort's own token-usage and session-updated writes. Provider-attempt
admission and session title updates are fenced: a cancel-marked turn must not
dispatch or retitle. The implementation measures every legitimate write under
the same SOL after the terminal before landing the predicate, and ships a
negative control that a non-audit write after the terminal fails and a
control that fork and revert writes under their own SOL are unaffected.

The essential crash windows have these outcomes:

| Window | Required outcome |
|---|---|
| Queue commit before 202; response lost | Client polls/retries its ID; no second row or turn. |
| 202 before worker claim | Any live instance can claim the durable queued row. |
| Head moved between admission and start | Terminal 409 `stale_compose_state`; no user row; no provider call. |
| Worker dies before `running` | Queued claim expires; another worker may claim. No side effect has started. |
| Worker dies after `running` | Reaper publishes `worker_lost` or the committed cancel. No provider replay. Existing fenced writes and audit remain visible on reload. |
| Deploy or drain during a running job | The owner cancels and joins within the drain bound, or a peer's reaper settles it; 503 `composer_operation_worker_lost`; no replay. |
| Cancel, completion, and lease expiry race | One token-checked terminal transition wins; the others read that terminal row. |

Persist the public final response and transport terminal in the same session
transaction as the final assistant/result publication (R2). Earlier audit or
user rows may remain after failure, as they can today; the SPA reloads
authoritative state before offering a new action. A retry of the same ID
never creates another originating user row: the worker writes a send's user
row only once per job, and the ingress receipt keyed by the operation ID
makes a second one a schema violation.

### 5. Frontend and failure semantics

Both freeform route callers use one operation transport: submit, recover
ambiguous acknowledgement, poll to terminal, then dispatch the existing
route-specific success or error reducer. The success reducer must still apply
messages, composition state, **all proposals**, validation clearing,
selection changes, and interpretation refreshes.
The current `MessageWithStateResponse` body cannot be replaced by a state ID
alone. The client keeps the operation ID and body in session storage, which
deliberately stores the body for this kind only, unlike the fingerprint-only
session operation retry store. A page reload resumes polling the persisted
operation ID. Session switches stop local pollers but do not cancel server
work; returning to the session reattaches to its active operation. If storage
refuses the descriptor, the turn runs from memory, and after a reload the
server's 409 `composer_operation_active` is the only reattach path. A poll
answering 404 `Session not found` takes today's session-not-found path, clears
the descriptor, and never resubmits. A second action for the same session
remains disabled while a job is nonterminal. After every non-success terminal
the client reloads authoritative state.

Because the request binds the base the operator saw, the client holds Send
until the session's composition state has loaded, and recompose sends the head
at the time of the retry click. While a same-tab send is nonterminal, the
controls that move the head (proposal Accept, revert, interpretation resolve,
and YAML import) are disabled; Reject stays enabled. A terminal
`stale_compose_state` shows today's stale copy and keeps the content, so the
user can resend it. Resending after a refusal is a new user action: it uses a
new operation ID and the reloaded head. Only an ambiguous acknowledgement
reuses the ID and body. The tutorial's send guard and Continue gate read the
same client operation state; the server has no tutorial branch.

The Stop button calls the cancel endpoint for that exact operation ID and
waits for the operation's terminal state. `AbortController` is used only to
bound individual short HTTP calls; aborting a completed 202 fetch does not
cancel a turn. The client deadline is a monotonic browser-clock deadline
derived from the latest `deadline_remaining_ms` plus the existing client
grace, which only tightens across poll bodies, so a skewed wall clock can
neither cancel early nor wait forever. If the server has not settled by then,
the client issues an explicit cancel and reconciles the terminal row. The
synchronous compose timer and its server-timeout readiness plumbing lose
their last caller and are deleted. A transient poll error retries with
backoff and does not declare the turn failed. The existing progress and
in-flight message pollers remain for UX but stop when the operation is
terminal.

Post-202 errors preserve the current public HTTP error semantics inside the
terminal envelope, including provider authentication versus availability,
the three convergence reasons, policy and integrity refusals, chargeable
admission, and stale conflicts. The adapter uses the existing safe error
projection, built from the application's real exception handlers; it does not
collapse these to one `operation_failed` code. The POST's `request_id` is
persisted on the job and placed where today's handler would place it: inside
a dict `detail`, top-level for a flat handler body, and absent for a string
`detail`, whose stored body stays byte-identical to today's. Each raise path
in the two routes must be inventoried in the implementation plan and proven
to land either before 202 or in the terminal envelope. Unknown exceptions use
the generic envelope only after audit and server diagnostics are retained.

The ingress 409 bodies (`message_already_accepted`,
`message_idempotency_conflict`) are removed, not preserved: a same-ID retry is
a replay, and a same-ID retry with a different body is
`composer_operation_conflict`. This design fixes four terminal bodies, each
carrying the request ID as above:

| Outcome | Status | Body |
|---|---|---|
| Base moved or unstated | 409, flat | `{"error_type":"stale_compose_state","detail":"The session changed before this request started. Review the current pipeline and send again."}` |
| Deadline passed | 504 | `{"error_type":"composer_operation_deadline_expired","detail":"The composer request waited too long to start. Please resubmit.","timeout_seconds":<configured>}` |
| Worker lost | 503 | `{"error_type":"composer_operation_worker_lost","detail":"The server stopped while composing this request. Reload to see what was saved, then resubmit."}` |
| User Stop | 499 | `{"error_type":"request_cancelled","detail":"The composer request was stopped."}` |

The stale body keeps today's `error_type` but not today's detail, which says
the session changed "while the compose turn was running" and is false for a
refusal before the turn.

## Verification gates

1. Schema and transaction tests on SQLite and serial PostgreSQL prove the
   status CHECKs, one-ID reservation, the one-nonterminal-job index,
   claim/reclaim with a queued-only negative control, the transition guard
   and delete guard (each with a control that goes red when the guard is
   removed), cross-instance COMPOSE exclusion, the positive fence predicate
   with its post-terminal negative control, cancel/completion/expiry races,
   and the atomic terminal result. A golden vector proves receipt request
   hashes are byte-identical after the normaliser extraction.
2. A provider held beyond 125 seconds returns a short 202, survives the
   client POST disconnect, and yields the exact former success DTO by poll.
   The same test family covers both routes and proves one provider turn.
   The tutorial's Build reaches the same route with no server branch.
3. Kill a worker in each crash window above. A queued job resumes safely;
   a running job terminates without replay; stale owners cannot write. A
   base moved while queued is refused with no user row and no provider call.
   On PostgreSQL, a session delete cascades through a running send job with
   its user row and ingress receipt, and a queued job is not starved by
   steady fork/revert traffic.
4. Verify cancellation audit evidence, server-fault classification, and
   `current_task().cancelling()` in the *actual* worker task. Adapt the old
   heartbeat tests to the new owner without weakening their failure intent.
5. Frontend tests cover lost 202, reload, cross-instance poll, Stop, deadline,
   stale transcript, stale-base refusal and resend with a new ID, the
   head-moving control guard, exact response application, a poll outage, and
   the absence of the transcript-matching ingress recovery. The Playwright transition-ledger
   recorder moves its turn boundary to the terminal poll, so the
   per-transition provider-call gate still counts calls.
6. Run the canonical full-suite gate and the serial testcontainer selection
   before integration. Capture terminal exits and frozen-tree evidence; a
   scoped green run alone does not certify this cross-cutting change.

The [implementation plan](../plans/2026-09-20-composer-async-operations.md)
names the files, order, and acceptance tests for these gates.
