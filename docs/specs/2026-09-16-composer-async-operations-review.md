# Adversarial review: composer async operations design

- **Target:** `docs/specs/2026-09-16-composer-async-operations-design.md`
- **Reviewed at:** `release/0.8.1` @ `8f4384936`, clean tree, 2026-09-16
- **Ticket:** elspeth-7a663a062c
- **Posture:** read-only. Nothing in the tree was modified. This is an attack
  report, not a balanced assessment: it records what broke under attack.
- **Historical scope:** This reviews the original 2026-09-16 draft. The target
  file now contains a [revised design](2026-09-16-composer-async-operations-design.md)
  and an [implementation plan](../plans/2026-09-20-composer-async-operations.md).
  Line anchors and quoted claims below refer to the original draft and the
  source SHA above, not to the revised document.

**Correction to B4 and S5 (2026-09-20):** Returning an `operation_id` in a
202 response does not imply that the server minted it; it could echo a
client-minted ID. The original draft omitted the minting decision. B4's
server-minting consequences were conditional and should not have been called
confirmed blockers. S5's independent point remains: the draft promised
same-`request_hash` replay without specifying a database key or an equivalent
atomic binding for that promise. The revised design explicitly uses a
client-minted ID and a `(session_id, operation_id)` primary key for retries
of that exact action.

**Counts: 4 BLOCKER, 11 SERIOUS, 7 MINOR.**

Instrument control: every negative grep in this report was paired with a
known-positive control on the same pattern and the same tool invocation.
Where a grep returns nothing, the control is stated inline. The specific
false-negative hazard here is the module/table name collision
(`src/elspeth/web/sessions/routes/guided_operations.py` the module vs
`guided_operations` the table), which is itself finding S3.

---

## BLOCKER 1 — The server-owned task orphans the lease heartbeat, and §3's task-identity claim is false

§3 asserts:

> `current_task().cancelling()` reads at `guided_plan.py:238` and `:730` stay
> coherent, because the cancel is delivered to the task actually doing the
> work. `_track_compose_inflight` moves from "owner of the request task" to
> "owner of the operation task" — the identity it always wanted.

`_track_compose_inflight` cannot move. It is a FastAPI **yield dependency**
mounted in the route signature:

```
src/elspeth/web/sessions/routes/_helpers.py:2432
async def _track_compose_inflight(
    session_id: UUID, request: Request, user: ...
) -> AsyncIterator[None]:
```

mounted at `messages.py:121`, `compose.py:94`, `guided_plan.py:327`,
`guided.py:2902`, `guided.py:5745` as `Depends(_track_compose_inflight)`.
It therefore runs in the **request** task, before the handler body, and its
teardown closes with the request's exit stack. Its own docstring states the
scope it was built for:

> The count spans the ENTIRE request — including the wait on the per-session
> compose lock, before any progress snapshot is published — and is
> decremented only when the request's exit stack closes.

Two consequences, both of which the spec asserts away:

**(a) The lease is actively released at 202, and the renewal loop is
destroyed with it.** The dependency's teardown (`_helpers.py:2617-2629`) is
unconditional:

```
finally:
    primary_error = sys.exception()
    try:
        try:
            if not heartbeat.done():
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
        finally:
            await registry.finish_request(lease)
    finally:
        finish_composer_request_metrics(...)
```

So when the request ends at 202 the exit stack cancels the renewal task
(created at `:2577`) and calls `registry.finish_request(lease)`. The operation
task then runs with **no lease at all** — not an unrenewed one. Every
enforcement path in the renewal loop goes with it: all five
`owner_task.cancel(...)` sites (`_helpers.py:2544, 2550, 2554, 2566, 2571`,
carrying `_COMPOSER_HEARTBEAT_TRANSIENT_EXHAUSTED`,
`_COMPOSER_HEARTBEAT_RENEWAL_DEFECT` and `_COMPOSER_HEARTBEAT_LEASE_LOST`)
are unreachable, because the loop that contains them no longer exists and the
`owner_task` they target has completed. The composer turn is exactly the work
whose unbounded runtime the lease exists to bound.

A third-order consequence: `terminal_status` is initialised to `"completed"`
at `:2484` and is only changed by the `except` arms at `:2580-2616`, which can
no longer fire for a turn-time outcome. Every composer request metric would
record `completed` at 202-time regardless of how the turn actually ended.

**(b) The server-fault / user-Stop discrimination is silently lost.**
`guided_plan.py:739-748` reads:

```
heartbeat_cancelled = _composer_heartbeat_cancel_of(exc) is not None
cancel_failure_code: GuidedOperationFailureCode = (
    _guided_full_failure_code(settlement_failure) if settlement_failure is not None
    else "operation_failed" if heartbeat_cancelled
    else "request_cancelled" if disconnected or caller_cancelled
    else "operation_failed"
)
```

with the comment at `:733-738` citing finding #28 by name: "A cancel delivered
by the compose heartbeat after it lost the request's lease also leaves
`cancelling() > 0`, but it is a server fault, not a user Stop". With the
heartbeat cancelling a dead request task, `heartbeat_cancelled` is never true
inside the operation task, and `disconnected` (`_is_client_disconnect_cancel`)
is never true either because there is no request to disconnect from. The
three-way discrimination collapses to whatever the explicit cancel endpoint
sets. This is the same class of defect as the abandoned decorator — a cancel
reclassified with no exception and no type error — which the spec's own
"Constraint for any design in this area" box says must not be reintroduced.

**(c) §4's settlement-signal claim is false by construction.** §4 says
"`inflight_requests` remains the settlement signal it already is
(elspeth-06a23adfcc)." The signal's lifetime is bound to the HTTP request the
design deletes. The SPA's contract is explicit
(`src/elspeth/web/frontend/src/stores/sessionStore.ts:704`):

> Settlement is QUIESCENCE, not phase: `inflight_requests === 0` on the
> progress snapshot.

and `:746` `if (inflightRequests === 0) { return; }` in
`waitForCancelledComposeToSettle`. Under 202 that count drops to zero while
the turn is mid-flight. Whatever §8 does with it, the sentence in §4 is not
true and cannot be carried into implementation as written.

**(d) The spec's own falsification criterion fires on the spec's own design.**
§3 and Testing both say: "The five tests that killed the decorator become the
regression guard for this section. They must pass unmodified in intent; if
any needs rewriting, that is a signal the design has reintroduced the split."
`tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py:11` pins
exactly the identity this design changes:

> ``asyncio.current_task()`` as the request owner: driving ``anext()`` from the
> [driving task]

and asserts `outcome.cancelling_after_teardown == 0` at `:253`, `:283` and
`:300` against a task it cancels at `:160`. Those three assertions at minimum
cannot hold unmodified once the heartbeat must cancel a task other than its
own. (Of the file's 13 tests, only those three pin owner identity; `:317`
`test_plain_cancelled_error_is_not_a_heartbeat_cancel` is a pure marker test
and is unaffected.)

**Repro:** read `src/elspeth/web/sessions/routes/_helpers.py:2432-2600` and
`tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py:1-30,150-305`;
`grep -n 'Depends(_track_compose_inflight)' -r src/elspeth/web/`.

---

## BLOCKER 2 — The §2 partition is not realizable: "admission-time" raises fire inside an unbounded lock wait

§2 divides failures into "before the turn starts, keep their real HTTP status,
returned synchronously" and "turn-time, becomes a `failure_code`". §7 calls the
enumeration "the main measurement task of implementation". The partition is not
merely unenumerated — for a large class of sites it is **not well-defined**,
because those raises are downstream of a lock whose wait is unbounded.

`POST /{session_id}/recompose` (`compose.py`):

```
109:     compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
110:     async with (
111:         compose_lock,
112:         await SessionOperationLease.acquire(... SessionOperationKind.COMPOSE ...),
119:     ):
...
134:         records = await service.get_messages(session.id, limit=None)
136:         if not conversation_records:
137:             raise HTTPException(status_code=400, detail="No messages to recompose from")
138:         if conversation_records[-1].role != "user":
139:             raise HTTPException(status_code=409, detail="Cannot recompose: ...")
```

The 400 and the 409 are textbook admission refusals — request-shape
preconditions on the transcript. Both fire **inside** the compose lock, after a
full unbounded history load. `POST /{session_id}/messages` has the same shape:
lock + COMPOSE lease at `messages.py:140-150`, then the state-ownership 404s at
`:182` and `:187`.

(Precision: the *session* 404 comes from `_verify_session_ownership` at
`messages.py:137`, which is genuinely pre-lock, so the spec's bullet "unknown
session (404)" is fine. The problem is the other admission-shaped raises.)

So the design must choose one of:

1. Return the 400/409/404 synchronously — which requires holding the request
   open across a compose-lock wait that can be the full duration of another
   turn. That is precisely the minutes-long idle request the design exists to
   eliminate, and the dependency's own docstring already names the lock wait as
   part of the request.
2. Move them to `failure_code` — contradicting §2's explicit list ("admission
   refusal and quota checked **at admission**", "request-shape validation
   (422)") and giving the user a 202 for a request that was structurally
   invalid before any provider was contacted.
3. Restructure the routes so every admission check precedes lock acquisition —
   real work the spec does not mention, and not always possible: `:136`
   depends on `service.get_messages`, which today is deliberately read under
   the lock so the precondition is not evaluated against a racing transcript.

This is not "the enumeration is deferred". Deferral would be acceptable. The
gap is that §2 presents the partition as a line that already exists in the
code and merely needs tracing; it does not.

**Repro:** `awk 'NR>=105 && NR<=145' src/elspeth/web/sessions/routes/composer/compose.py`;
`awk 'NR>=130 && NR<=200' src/elspeth/web/sessions/routes/messages.py`.

---

## BLOCKER 3 — The schema section names the wrong constraint on the wrong table, and §4's completion contract is illegal under the constraint it does not name

§1's "Trap" box:

> `ck_guided_operation_admission_blocks_kind` is
> `CheckConstraint("kind = 'guided_start'")` — a single-value CHECK. Widening it
> hits the known PostgreSQL reflection defect where a one-element `IN` is
> reflected as `=` (elspeth-d0e62aea41).

That constraint is at `src/elspeth/web/sessions/models.py:975` and belongs to
**`guided_operation_admission_blocks`** (table defined at `:955`), a different
table — the durable negative-admission authority for a guided start whose
client lost its request body. Widening `guided_operations.kind` does not touch
it.

The constraint actually governing `kind` on the operation record is
`ck_guided_operations_kind` at `models.py:1067-1069`:

```
"kind IN ('guided_start', 'guided_respond', 'guided_chat', 'guided_convert',
          'guided_reenter', 'guided_plan', 'state_revert', 'session_fork')"
```

An **eight**-value `IN`, which does not trigger the one-element reflection
defect at all — so the Trap's warning does not apply to the change it is
attached to. It also **already contains `guided_plan`**; `guided_plan.py`
already reserves with `kind="guided_plan"` at `:411, :628, :690, :775, :930`.
The §1 kind table lists `guided_plan` alongside two genuinely new kinds as
though all three need adding.

The constraints the spec needed to name and does not:

**`ck_guided_operations_result_locator` (`models.py:1124-1143`)** — a per-kind
result-shape CHECK. New kinds `compose_message` / `compose_recompose` fall into
its catch-all arm:

```
"(kind NOT IN ('session_fork', 'guided_plan') AND result_kind = 'composition_state' "
"AND result_state_id IS NOT NULL AND result_message_id IS NULL AND result_session_id IS NULL "
"AND (proposal_id IS NULL OR kind IN ('guided_respond', 'guided_chat')))"
```

A completed operation of a new compose kind is therefore **required to have
`result_message_id IS NULL` and `proposal_id IS NULL`**. §4's wire contract
says the opposite:

> On `completed`, the client follows `result_message_id` / `result_state_id`.

For `compose_message` the natural result *is* a chat message. The completion
contract in §4 cannot be satisfied without editing a constraint the spec never
mentions.

**`ck_guided_operations_status_bundle` (`models.py:1096-1122`)** — pins the
NULL-ness of every result column against `status`, including
`status='failed' → ... result_message_id IS NULL AND proposal_id IS NULL AND
response_hash IS NULL`. Every new terminal shape §7 introduces must be checked
against it. Not mentioned.

**`uq_guided_operations_active_proposal_admission` (`models.py:1155-1162`)** —
a unique partial index on `(session_id, proposal_id)` where
`status='in_progress' AND proposal_id IS NOT NULL`, on both dialects. Not
mentioned.

Finally the Evidence row cites `models.py:996-1111` for "Operation record shape
and enums". The table begins at `:990`; the range cuts off mid-way through
`ck_guided_operations_status_bundle` (ends `:1122`) and excludes
`ck_guided_operations_result_locator` entirely — i.e. the cited window is
exactly the window that hides the two constraints that break the design.

**Repro:** `awk 'NR>=950 && NR<=1165' src/elspeth/web/sessions/models.py`;
`grep -n 'ck_guided_operations_kind\|ck_guided_operation_admission_blocks_kind' src/elspeth/web/sessions/models.py`.

---

## BLOCKER 4 — `operation_id` is client-minted, and "the HTTP surface never exposes an intermediate 202" is a written contract of the function the design builds on

§2's wire contract has the server return the handle:

```
{ "operation_id": "...", "kind": "compose_message", "poll_after_ms": 500 }
```

The existing operation primitive requires the **client** to mint it
(`src/elspeth/web/sessions/routes/guided_operations.py:506-511`):

```
dumped_request = request.model_dump(mode="python")
if "operation_id" not in dumped_request:
    raise AuditIntegrityError("Strict guided operation request has no operation_id")
operation_id = dumped_request["operation_id"]
```

`reserve_or_replay_guided_operation` takes no `operation_id` argument at all —
it reads it out of the request body. Three existing mechanisms depend on that
direction:

- `guided_operation_admission_blocks` (`models.py:955-983`): the docstring says
  it exists for "a guided start whose client lost its request body before the
  server ever reserved an operation row … an exact operation id is permanently
  closed as `request_cancelled`". A client cannot pre-close an id the server
  has not yet minted.
- The 409 idempotency guard at `:526-529` and `:538-542` ("Operation id is
  already bound to a different request") binds `(operation_id, request_hash)`.
  With a server-minted id there is nothing to collide.
- `reconcile_guided_start_operation` (`guided.py:1322`) is the client-driven
  reconciliation of an id the client already holds.

And the same function's docstring, at `guided_operations.py:495`, states:

> The HTTP surface never exposes an intermediate 202 response.

That is a written invariant of the module the design proposes to generalise,
directly negated by §3's Decision 3 ("Cutover: always 202, hard"). The spec
does not acknowledge it exists, so the reader cannot tell whether the
invariant was considered and overruled or simply not read. Note also
`:492-494`: "Active requests are polled to a terminal result" — the existing
contract resolves a concurrent duplicate by **holding the second request open
until the first settles**, which is the very shape the design is removing.

**Repro:** `awk 'NR>=486 && NR<=545' src/elspeth/web/sessions/routes/guided_operations.py`.

---

## SERIOUS

### S1 — Five routes share the compose shape, not three

Evidence row: "Three routes share the compose shape | `grep -n '_track_compose_inflight'` → `messages.py:121`, `compose.py:94`, `guided_plan.py:327`".

The grep as specified returns five mount sites:

```
messages.py:121       POST /{session_id}/messages          (decorator :109)
compose.py:94         POST /{session_id}/recompose         (decorator :83)
guided_plan.py:327    POST /{session_id}/guided/plan       (decorator :322)
guided.py:2902        POST /{session_id}/guided/respond    (decorator :2896)
guided.py:5745        POST /{session_id}/guided/chat       (decorator :5739)
```

Both missed routes are provider-touching, not bookkeeping endpoints:
`POST /guided/chat` runs `_run_guided_chat_provider_attempt` (`guided.py:5731`),
and `post_guided_respond`'s body (`guided.py:2897-3640`) carries 17 matches for
`composer_service|provider|llm_calls|planner`. Both have the same buffered
shape and the same lease. Decision 1
("Scope: all three compose routes, not `/messages` first … splitting means
designing the operation contract once and re-arguing it twice") is made on an
undercount: the design splits anyway, leaving two provider-driven routes on the
cut-prone synchronous shape, and forcing the compose lock to serialise a
request-task holder against an operation-task holder for the same session.

### S2 — §6's premise is wrong: durable cross-instance COMPOSE exclusion already exists

§6:

> The per-session compose lock is currently an in-process `asyncio.Lock` in a
> `WeakValueDictionary` (`_helpers.py:323`). It is correct today only because a
> request pins to one instance for the whole turn — an accident this design
> removes.

The `asyncio.Lock` is not the exclusion primitive. Every compose route takes it
**together with** a durable fenced lease in the same `async with`
(`messages.py:141-150`, `compose.py:110-119`):

```
async with (
    compose_lock,
    await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_operation_lease,
):
```

`_SessionOperationAuthorityRepository.acquire`
(`src/elspeth/web/coordination/repository.py:4633`) is an exclusive,
DB-backed, cross-instance lease:

> ``BLOB_READ`` is the one shareable kind … **Every other kind is exclusive and
> advances the row by epoch.**

with `raise SessionOperationConflictError` when `released_at is None and
lease_expires_at > database_now`, under `with_for_update()` on the session row.
The SQLite subclass says the same
(`coordination/sqlite_authority.py:18-25`): "A live lease always conflicts".
It is installed on both dialects at `app.py:1578-1584`, with a
`NotImplementedError` for any third dialect.

So the `asyncio.Lock` supplies **queueing**, not correctness: it makes a
same-instance second compose *wait* instead of taking an immediate
`SessionOperationConflictError`. §6's stated motivation — that the lock is only
correct by instance affinity — does not hold, which makes the proposed "fifth
authority" a duplicate of an existing sixth. The real design question (what a
cross-instance waiter should do when the fence is held) is never asked.

Three further problems with §6 as written:

- **"keyed by session id" is false.** The registry is keyed by arbitrary
  strings, and two call sites deliberately use composite keys:
  `guided_chat_atomic.py:1412` `get_lock(f"{session_id}:guided-chat-admission")`
  and `guided.py:3634` `get_lock(f"{session_id}:guided-respond-admission")`.
  An advisory lock keyed by session id collapses three distinct locks into one.
- **Blast radius.** `get_lock` is called at ~25 sites across 9 files
  (`messages.py`, `compose.py`, `guided_plan.py`, `guided.py`,
  `guided_chat_atomic.py`, `proposals.py`, `state.py`, `interpretation.py`,
  `sessions.py`) **plus `execution/routes.py:1445`** — the execution layer the
  Non-goals section declares out of scope. Replacing the primitive is not
  scoped to the three compose routes.
- **Semantics change unstated.** A PostgreSQL session-level advisory lock is
  held by a *connection* and is re-entrant (a counter); `asyncio.Lock` is
  neither. A transaction-level advisory lock releases at commit, which is
  unusable for a compose turn that commits per-operation transactions
  mid-flight (see `sessionStore.ts:760-767`). Holding a pooled connection for
  the full turn duration is a pool-exhaustion design the spec does not mention.

### S3 — §1's "already read and written by" list is wrong for at least five of its seven files, and the rename estimate is wrong

§1:

> `guided_operations` is already … already read and written by `messages.py`,
> `guided_plan.py`, `composer/state.py`, `composer/guided_chat_atomic.py`,
> `sessions.py`, `service.py` and `blobs/service.py`.

Only **four** source files touch the table object
(`grep -rln 'guided_operations_table' src/`):

```
src/elspeth/web/sessions/models.py
src/elspeth/web/sessions/service.py
src/elspeth/web/coordination/repository.py
src/elspeth/web/blobs/service.py
```

The rest match on the *module* `src/elspeth/web/sessions/routes/guided_operations.py`:
`messages.py:94` imports `_join_shielded_task_after_cancellation` from it — a
task-join helper, not a read or a write of the record; `state.py:43`,
`sessions.py:43` and `guided_chat_atomic.py:141` are likewise module imports.
`coordination/repository.py`, which *is* a heavy table user (lines 704-752,
1923-1925), is not in the list.

Decisively, **`compose.py` — `POST /recompose`, one of the three routes being
converted — has zero references to anything `guided_operation`**:

```
$ grep -n 'guided_operation' src/elspeth/web/sessions/routes/composer/compose.py; echo "exit=$?"
exit=1
$ grep -c 'guided_operation' src/elspeth/web/sessions/routes/composer/guided_plan.py   # positive control
19
```

(The positive control is required here precisely because the module/table name
collision makes a bare grep unreliable in the other direction.)

So for the two headline routes (`/messages`, `/recompose`) the operation record
is **net-new integration**, not an existing record whose `kind` needs widening.
§1's framing — "already a durable, leased, pollable async-operation record …
Widen `kind`" — understates the work by the entire compose-side integration.

Risk 3 ("The rename touches nine files") is likewise wrong: 15 source files
contain the string `guided_operations`, 41 contain `guided_operation`, 78 files
repo-wide (excluding `.venv`) contain `guided_operations`.

Risk 3's mitigation is also wrong — "a missed site fails loudly at import or
query time rather than silently". Two counter-examples:

- `src/elspeth/web/_azure_container_apps_acceptance/controller.py:57` holds the
  table name in a raw SQL string constant:
  `GUIDED_OPERATIONS_SINCE_SQL: Final = "SELECT count(*) FROM guided_operations WHERE ..."`.
  That fails during a deployment acceptance run, not at import.
- `src/elspeth/web/sessions/schema.py:102,108,109,403-406` pins three trigger
  names (`trg_guided_operations_terminal_immutable`,
  `trg_guided_operations_reject_admission_block_insert`,
  `trg_guided_operations_reject_admission_block_update`) inside the fail-closed
  schema-shape comparator, including a literal `relation.relname =
  'guided_operations'` in reflection SQL at `:403`. A missed rename there makes
  a freshly created database classify as non-current — a startup refusal whose
  cause is a string mismatch three layers from the rename. The spec does not
  mention the triggers at all.

### S4 — Lease-expiry semantics conflict: the row models takeover, §5 proposes reaping

§5 introduces "a lease-expiry reaper [that] settles operations whose
`lease_expires_at` has passed without renewal".

The existing record models expiry as **takeover**, not settlement:

- `guided_operations.attempt` (`models.py:1000`), `CheckConstraint("attempt >= 1")` (`:1078`).
- `guided_operation_events.event_kind IN ('claimed','renewed','taken_over','completed','failed')`
  with `ck_guided_operation_events_bundle` requiring
  `event_kind = 'taken_over' AND prior_attempt IS NOT NULL AND prior_attempt = attempt - 1`
  (`models.py:1200-1204`, the `taken_over` arm at `:1202`).
- `reserve_or_replay_guided_operation(..., takeover_expired: bool = True)`
  (`guided_operations.py:484`), docstring `:492-494`: "Once a lease expires the
  caller returns to the atomic reserve primitive, which either performs the
  sole takeover or observes the competing taker's active/terminal outcome."

These are different delivery guarantees on the same row. Takeover is
at-least-once: a second attempt re-runs the work. Reaping is at-most-once: the
row settles `failed` and nothing re-runs. For a compose turn that has already
persisted the canonical user row and made provider calls, takeover means a
second provider spend and a duplicate assistant row; reaping means the row says
`failed` while the original attempt may still be mid-flight (see S6). §5
chooses reaping without saying why the existing takeover semantic does not
apply to compose kinds, or what `attempt` means for a compose operation.

### S5 — `request_hash` idempotency has no database-level guard

Testing: "a replayed POST with the same `request_hash` must return the existing
operation, not start a second turn."

There is no unique index on `(session_id, request_hash)`. The only unique
constraints on the table are:

- `pk_guided_operations(session_id, operation_id)` (`models.py:1013`)
- `uq_guided_operations_request_binding(session_id, operation_id, request_hash)`
  (`models.py:1014-1019`) — implied by the PK, and present as the FK target for
  `guided_operation_events` (`models.py:1184-1189`), not as an idempotency key.
- `uq_guided_operations_active_proposal_admission(session_id, proposal_id)`
  partial (`models.py:1155-1162`).

Today idempotency is carried by the **client-minted `operation_id`** and the
409 on `(operation_id, request_hash)` mismatch (B4). Invert the minting
direction as §2 does and `request_hash` becomes the only replay key, enforced
by a read-then-write (`get_guided_operation` at `guided_operations.py:532`,
then `reserve` at `:516`) with nothing in the schema to catch the interleaving.
Two concurrent identical POSTs both read "absent" and both insert with
different server-minted ids. Both succeed. Two turns run, two provider spends.

### S6 — §5's cancel is the lease heartbeat's cancel, at the heartbeat's latency — and BLOCKER 1 is what removes it

§5: "Sets `failure_code='request_cancelled'` … and cancels the owning task.
**Immediate and precise**; replaces today's close-the-socket abort."

The correction first, because it is what makes the finding precise: a
cross-instance cancel **does** exist today, and it is DB-mediated rather than
absent. Revoking or settling the row on instance A causes instance B's next
`registry.renew_request(lease)` to raise `ComposerRequestLeaseLost`, which the
renewal loop converts to `owner_task.cancel(_COMPOSER_HEARTBEAT_LEASE_LOST)`
(`_helpers.py:2566`); and B's next fenced write raises
`GuidedOperationFenceLostError`, which `guided_plan.py:768` handles by joining
the durable winner (also at `:883` and `:924`). That is designed fail-closed fencing, not an absence of a
terminal state. The spec's Non-goals rejection of `LISTEN/NOTIFY` is therefore
not the gap it first appears to be.

What is actually wrong with §5:

1. **"Immediate and precise" is false.** The only cross-instance delivery path
   is the renewal poll, and `_COMPOSER_HEARTBEAT_SECONDS = 15.0`
   (`_helpers.py:2328`) against `_COMPOSER_REQUEST_LEASE_SECONDS = 60`
   (`:2330`). Worst-case delivery is one full heartbeat interval after the
   cancel row lands, not immediate, and the imprecision grows with the fenced
   write's own spacing. §5 sells the endpoint on a latency property the
   mechanism underneath it does not have.
2. **The spend leak is bounded but real, and §5 mis-sizes it.** Because the
   cancel lands at the next renewal or the next fenced write, one in-flight
   provider call always completes and is paid for. Risk 2's mitigation
   ("explicit cancel is the primary path, so the TTL can be generous") reads
   as though explicit cancel eliminates the leak; it moves it from a TTL-sized
   window to a heartbeat-sized one.
3. **BLOCKER 1 deletes the mechanism §5 depends on.** The renewal loop and the
   lease itself are torn down at 202 (`_helpers.py:2617-2629`). So the one path
   that carries a cancel across instances today is the path the design's own
   execution model destroys, and §5 does not propose a replacement — it
   proposes `asyncio.Task.cancel()`, which reaches only the calling process.

The same three points apply to the reaper backstop: it settles a row whose task,
on another replica, learns of the settlement only at its next renewal or fenced
write — and under the new model, at neither.

Filed SERIOUS rather than BLOCKER: a cross-instance cancel channel is addable
without disturbing the rest of the design, and the fencing needed to make it
fail closed already exists.

### S7 — The `current_task()` inventory is short by nine sites, and the most important miss is the one that delivers the cancel

Evidence row: "Task identity is read inside handler bodies | `grep -n 'current_task()'` → `_helpers.py:2486`, `guided_plan.py:238`, `guided_plan.py:730`".

The grep over `src/elspeth/web/` returns twelve:

```
sessions.py:703                 caller_task = asyncio.current_task()
sessions/service.py:7099        current_task = asyncio.current_task()
routes/guided_operations.py:402 caller_task = asyncio.current_task()
routes/_helpers.py:2236         task = asyncio.current_task()          <-- the disconnect watcher
routes/_helpers.py:2486         owner_task = asyncio.current_task()
composer/guided_chat_atomic.py:2391  cancel_caller_task = ...
composer/proposals.py:67        caller_task = ...
composer/pipeline_settlement.py:71   caller_task = ...
composer/guided_plan.py:238     enclosing_task = ...
composer/guided_plan.py:730     caller_task = ...
composer/guided.py:1805         caller_task = ...
composer/guided.py:5557         caller_task = ...
```

Even the most charitable scoping (the three named route files plus `_helpers.py`)
gives four, not three. The miss is `_helpers.py:2236` —
`_cancel_on_client_disconnect`, the context manager that **delivers** every
client-disconnect cancel, mounted at `messages.py:347`, `compose.py:191`,
`guided_plan.py:491`, `guided_chat_atomic.py:1498`. The first bullet of its
docstring (`_helpers.py:2217-2220`) states a constraint the design must reckon
with:

> The guarded block MUST be awaited inline in the route task — running it in a
> child task would launder the CancelledError instance at the task boundary and
> drop the attached `llm_calls` audit records.

`attach_llm_calls` (`composer/llm_response_parsing.py:917`, called at
`composer/service.py:7150,7994,8674-8713`, `pipeline_planner.py:3249`,
`tool_batch.py:323`) rides on the `CancelledError` **instance**. The watcher
also manipulates the very counter §3 relies on: `task.uncancel()` at
`_helpers.py:2288` and `:2312`. §5's "the origin already cancels correctly when
a client goes away … Do not rebuild that; only its trigger changes" is an
understatement: the mechanism is welded to `request.receive()` and to the route
task, and re-homing it onto a request-less operation task is a rewrite of the
marker protocol, the `uncancel()` bookkeeping, and the audit-record attachment
path. Whether audit records survive that move is unaddressed, and this is audit
evidence, not telemetry.

Also unaddressed: `guided.py:1805` and `:5557` are `cancelling()` reads in the
two routes S1 shows the design forgot.

### S8 — §3's "Nothing wraps a handler body" implies a verbatim move the body cannot survive

The handler bodies are saturated with the live `Request`:

```
messages.py   26 textual `request.` uses
compose.py    16
guided_plan.py 8
```

including `request.app.state.*` (survives), `request.state.composer_request_lease`
(survives as an object), `_failure_log_request_id(request)` reading
`request.scope["state"]` (`_helpers.py:2188-2196`), `request.url.path`
(`_helpers.py:2475`), and — fatally — `await request.receive()` inside
`_cancel_on_client_disconnect` (`_helpers.py:2246`), which after response
completion no longer describes a live client. §3's framing ("The request
handler's job ends at 'record the operation, start the task, return 202'" and
"Nothing wraps a handler body") reads as though the body relocates unchanged.
It cannot: every `request`-derived read has to be resolved into an owned
context object before the 202 is sent, which is the bulk of the implementation
work and is not costed anywhere in the spec.

### S9 — §8 addresses only the error path; the success path consumes the POST body and the operation record cannot express it

`sessionStore.ts:2131-2132`:

```ts
const { message, state } = result;
const proposals = result.proposals ?? [];
```

driving, in the `set()` block at `:2133-2185`:

- `lastComposeChangedPipeline: versionChanged` — computed from
  `state?.version !== s.compositionState?.version` (elspeth-bf9c296ee5, the
  "Pipeline updated" vs "Response ready" badge);
- `getExecutionStore().clearValidation()` on a version change (R4-H3);
- `mergeCompositionProposals(s.compositionProposals, proposals)`;
- `selectedNodeId: null` when the selected node no longer exists in the new state;
- the defensive backfill of `message` by id when `loadInflightMessages` failed.

The retry/recompose path does the same thing at `sessionStore.ts:2687-2688`:

```ts
const { message: assistantMessage, state } = result;
const proposals = result.proposals ?? [];
```

so both of the two sites §8 says "converge on one path" consume the POST body
on success, not only on error.

§8 discusses discriminators, `isComposeAbort`, and the Retry affordance. It says
nothing about any of the above. Worse, the operation record **cannot carry
`proposals`**: it has a single nullable `proposal_id` column
(`models.py:1002`), and `ck_guided_operations_result_locator` forbids even that
one on a completed non-guided kind (BLOCKER 3). §7's closing claim — "The
operation record is the authority; it must not be lossier than the advisory
channel beside it" — is asserted about the failure enum while the success side
is measurably lossier than the response body it replaces.

### S10 — The client-side compose deadline and the Stop button both become inert, unmentioned

`src/elspeth/web/frontend/src/config/composer.ts:29`:
`export const DEFAULT_COMPOSE_TIMEOUT_MS = 270_000 + COMPOSE_CLIENT_GRACE_MS;`
fired by `controller.abort(COMPOSE_TIMEOUT_ABORT_REASON)` at `:109`.

`src/elspeth/web/frontend/src/hooks/useComposer.ts:66`:
`activeControllerRef.current?.abort(COMPOSE_USER_CANCEL_ABORT_REASON);`
and `ChatPanel.tsx:930` for the guided controller.

Both abort a `fetch` that, under a hard 202, has already resolved in
milliseconds. The 270-second client-side deadline silently ceases to bound
anything — there is then **no** client-side deadline on the operation at all —
and the Stop button aborts nothing unless rewired to `POST …/cancel`. §8 says
`isComposeAbort` "stops meaning 'our AbortSignal fired'", which is true but
addresses only the classification, not the two mechanisms that produce the
signal. `isComposeAbort` also has eleven call sites
(`sessionStore.ts:421, 2209, 2239, 2246, 2260, 2286, 2746, 2764, 2779, 2798,
4124, 4357`), one of which (`:421`) is the base of
`isAmbiguousComposeNetworkFailure` — a function whose whole premise ("A fetch
transport failure does not tell the browser whether the POST reached the
server") changes meaning when the POST is a 202 handle fetch.

### S11 — The crash and race windows in §3 are unenumerated, and the ordering is unspecified

§3 gives the handler three jobs — "record the operation, start the task, return
202" — in that textual order, but the spec never states it as a required
ordering or analyses the windows. They are:

| Window | Failure | Result |
|---|---|---|
| row written, process dies before task starts | no runner | row sits `in_progress` until the lease lapses; recovery depends on S4's unresolved takeover-vs-reap choice |
| row written, task started, process dies before 202 is flushed | client has no `operation_id` | orphan turn runs to completion (or is reaped) with no client able to poll or cancel it; with a server-minted id (B4) the client cannot even use `guided_operation_admission_blocks` to close it |
| task started before row written, process dies between | turn runs with no row | no lease, no poll target, no reaper visibility; the turn's writes land with no operation to settle |
| 202 returned before row committed | client polls a 404 | client must distinguish "not yet visible" from "never existed"; §4 defines no non-terminal error for the poll |
| client POSTs twice concurrently | S5 | two rows, two turns, two provider spends |

The spec's only stated idempotency answer is `request_hash`, which S5 shows has
no database-level guard. None of these windows is named in §Risks.

---

## MINOR (anchor and measurement defects)

| # | Claim | Actual |
|---|---|---|
| M1 | Evidence + §4: "Durable progress is PostgreSQL-only \| `app.py:1722`, `if session_engine.dialect.name == "postgresql"`" | `app.py:1722` is a comment line; the `if` is at **1723**. |
| M2 | §6: "the seam that already exists at `app.py:1720-1725`, where four durable cross-instance authorities are installed" | The block is **1723-1739** (`:1720` is `composer_availability`, unrelated). It is also one of **three** dialect splits: `:1579-1584` (session operation authority), `:1691`, `:1723`. |
| M3 | Evidence: "route decorators at `:110`, `:84`, `:322`" | `messages.py` `@router.post(` is at **109**; `compose.py` at **83**; `guided_plan.py:322` correct. |
| M4 | §2/§7: "the ~18 `HTTPException` sites" | `messages.py` **16** `raise HTTPException` (one, `:1185`, belongs to the GET route at `:1146`, so 15 in `send_message`); `compose.py` **14**; `guided_plan.py` **1**. Three-file total **31**. "~18" is ~20% high for `messages.py` alone and 42% low for the enumeration §7 actually requires. |
| M5 | Evidence: "Operation record shape and enums \| `models.py:996-1111`" | Table starts at **990**; the range truncates `ck_guided_operations_status_bundle` (1096-1122) and excludes `ck_guided_operations_result_locator` (1124-1143) — see BLOCKER 3. |
| M6 | §"Why not a heartbeat": "`_track_compose_inflight` (`_helpers.py:2486`)" | The function is at **2432**; `:2486` is the `owner_task` capture. Defensible as a pointer to the cited behaviour, but the function anchor is wrong. |
| M7 | Testing: "the five tests that killed the decorator (`test_compose_heartbeat_renewal.py`, `test_routes.py`)" | Nine files in `tests/` are named `test_routes.py`; the intended one is presumably `tests/unit/web/sessions/test_routes.py`. `test_compose_heartbeat_renewal.py` resolves to `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py` and contains **13** tests, not five — the five are unnamed, so the §3 regression guard is not identifiable from the spec. |

### Evidence-table claims that survived attack

Recorded so the corrected rows are not re-measured:

- `tests/unit/web/conftest.py:65,109` — both are `"sqlite:///:memory:"`. Correct.
- "1,911 test functions" under `tests/unit/web/sessions` — `grep -rc 'def test_' … | sum` = **1911**. Correct.
- "113 files" — 113 files under that tree contain `def test_` (117 `.py` total). Correct.
- "68 testcontainer files" — `find tests/testcontainer -name '*.py' | wc -l` = **68**. Correct.
- "Only the execution layer streams" — `grep -rn 'StreamingResponse' src/elspeth/web/` returns only `execution/routes.py:30,399,1951`. Correct.
- "Compose lock is in-process \| `_helpers.py:323`, `WeakValueDictionary[str, asyncio.Lock]`" — line **323** exactly. Correct as a line anchor; the conclusion drawn from it is S2.
- "Progress failure taxonomy \| `progress.py:341-390` (three convergence reasons), `:407-411` (`client_cancelled`)" — `convergence_progress_event` at `:339` with reasons at `:375,:383,:390`; `client_cancelled_progress_event` at `:394` with `reason="client_cancelled"` at `:411`. Correct.
- "SPA already polls twice per turn \| `sessionStore.ts:2108-2110`; `loadInflightMessages` at `:2595-2612`" — `startComposerProgressPolling` at `:2108-2109`, `startInflightMessagesPolling` at `:2110-2111`; the merge block is `:2594-2605`. Correct.
- §8's two fallback anchors `sessionStore.ts:2230` and `:2757` — both are exactly `"Failed to send message. Please try again."`. Correct.
- §7's convergence-loss argument — `progress.py` publishes three discriminated
  sub-causes and the `failure_code` enum (`models.py:1087-1093`) has twelve
  values with no convergence discrimination. The gap is real and correctly
  diagnosed.

---

## What was attacked and found nothing

So the all-clear on these is a result and not a gap in the search:

- **The 12-value `failure_code` enum listing in §7** matches
  `ck_guided_operations_failure_code` (`models.py:1087-1093`) exactly, in order.
- **The `llm_auth_error` gap** is real: `sessionStore.ts:2222-2224` and
  `:2753-2754` both dispatch on `502 + llm_auth_error`, and no enum value maps.
- **The reaper's index exists.** §5's `SELECT … WHERE lease_expires_at < now`
  is backed by `Index("ix_guided_operations_status_lease", status, lease_expires_at)`
  at `models.py:1154`. The spec does not claim this; it is simply not a defect.
- **The SQLite non-goal measurement** is sound on every number checked (M5 list).
- **`guided_operations` is genuinely present on both dialects** with
  `ddl_if`-split CHECKs for `request_hash`, `response_hash` and
  `unproducible_output_fields` (`models.py:1074-1081, 1145-1152, 1099-1115`),
  including a deliberate `'array'::text` spelling for PostgreSQL deparse
  parity. §1's "present on both dialects" is correct.
- **Moving the turn off the request task does not break the compose lock's
  release path.** This was attacked directly (brief angle 5). `asyncio.Lock`
  has no task affinity — unlike `threading.Lock` it may be released by a task
  other than the acquirer, and in any case acquire and release travel together
  inside the `async with` in the handler body (`messages.py:141`,
  `compose.py:110`, `guided_plan.py:451-452`), so both move into the operation
  task as a unit. No deadlock and no leak from relocation alone. The lock
  findings in S2 are about the wrong primitive being replaced for the wrong
  stated reason, not about release.
- **`_track_compose_inflight` teardown ordering under FastAPI 0.136.1 /
  Starlette 1.3.1** was investigated as a possible escape hatch for BLOCKER 1
  and is not one: whether the yield-dependency's exit stack closes before or
  after the response bytes, the dependency's scope is the request, and the
  request is what the design shortens to milliseconds.

---

## Summary of what the design would need before it can be implemented

Stated as consequences, not as a redesign:

1. A cancellation and lease-enforcement owner that is not a request-scoped
   FastAPI dependency (B1, S7).
2. A settlement signal for the SPA that is not `inflight_requests` (B1c).
3. An admission phase that completes before the compose lock, or an explicit
   decision that lock-blocked preconditions return 202 (B2).
4. `ck_guided_operations_result_locator`, `ck_guided_operations_status_bundle`
   and `uq_guided_operations_active_proposal_admission` named, and a completion
   contract for compose kinds that satisfies them (B3, S9).
5. A ruling on who mints `operation_id`, and what replaces
   `guided_operation_admission_blocks` and the 409 binding if the server does
   (B4, S5).
6. A cross-instance cancel channel, or an explicit single-instance scope
   contradicting the K7 motivation (S6).
7. Takeover-vs-reap chosen and justified for compose kinds (S4).
8. `/guided/respond` and `/guided/chat` either in scope or explicitly excluded
   with a reason (S1).
9. §6 rewritten against the existing `SessionOperationLease`, or dropped (S2).

```json
{"findings": [
  {"title": "Server-owned task orphans the lease heartbeat: _track_compose_inflight is a request-scoped FastAPI yield dependency and cannot become the operation task's owner",
   "severity": "critical",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/_helpers.py", "src/elspeth/web/sessions/routes/composer/guided_plan.py", "src/elspeth/web/frontend/src/stores/sessionStore.ts", "tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py"],
   "repro": "grep -rn 'Depends(_track_compose_inflight)' src/elspeth/web/; awk 'NR>=2432 && NR<=2635' src/elspeth/web/sessions/routes/_helpers.py; awk 'NR>=725 && NR<=750' src/elspeth/web/sessions/routes/composer/guided_plan.py; sed -n '700,760p' src/elspeth/web/frontend/src/stores/sessionStore.ts; sed -n '1,30p;250,305p' tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py",
   "detail": "_track_compose_inflight (_helpers.py:2432) is a FastAPI Depends yield dependency; its docstring states the count 'spans the ENTIRE request'. owner_task at :2486 is the request task. The teardown at :2617-2629 is unconditional: it cancels the renewal task created at :2577 and calls registry.finish_request(lease). So at 202 the lease is ACTIVELY RELEASED and the renewal loop destroyed -- the operation task then runs with no lease at all, and all five owner_task.cancel() sites (:2544/:2550/:2554/:2566/:2571) are unreachable. heartbeat_cancelled at guided_plan.py:739 is then never true inside the operation task -- silently collapsing the finding-#28 server-fault vs user-Stop discrimination the code comment at :733-738 names explicitly (the classification block is :739-748). terminal_status, initialised 'completed' at :2484 and changed only by the except arms at :2580-2616, would record 'completed' for every composer request at 202-time. Section 4's claim that inflight_requests 'remains the settlement signal it already is' is false by construction: sessionStore.ts:704 and :746 define settlement as inflight_requests === 0, and that count now drops to zero at 202. The spec's own falsification criterion fires: test_compose_heartbeat_renewal.py:11 pins current_task() as the request owner and :253/:283/:300 assert cancelling_after_teardown == 0 on a task it cancels at :160; those three assertions at minimum cannot hold unmodified (of the file's 13 tests only those three pin owner identity)."},

  {"title": "The admission/turn-time partition is not well-defined: admission-shaped raises fire inside an unbounded compose-lock wait",
   "severity": "critical",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/composer/compose.py", "src/elspeth/web/sessions/routes/messages.py"],
   "repro": "awk 'NR>=105 && NR<=145 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/sessions/routes/composer/compose.py; awk 'NR>=130 && NR<=200 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/sessions/routes/messages.py",
   "detail": "compose.py acquires the compose lock plus the COMPOSE SessionOperationLease at :109-119, then raises 400 'No messages to recompose from' at :137 and 409 'Cannot recompose' at :139 -- both request-shape preconditions that section 2 assigns to the synchronous side, both downstream of an unbounded lock wait and a full get_messages() load. messages.py has the same shape: lock+lease at :140-150, state-ownership 404s at :182 and :187. (The session 404 at _verify_session_ownership, messages.py:137, IS pre-lock, so that bullet is fine.) Answering these synchronously requires holding the request open across another turn's duration -- the exact failure the design exists to remove -- and _track_compose_inflight's docstring already names the lock wait as part of the request. This is not the deferred enumeration section 7 acknowledges; it is a line the spec presents as already existing in the code, which does not."},

  {"title": "Schema section names the wrong CHECK on the wrong table; the real per-kind result constraint makes section 4's completion contract illegal",
   "severity": "critical",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/models.py"],
   "repro": "grep -n 'ck_guided_operations_kind\\|ck_guided_operation_admission_blocks_kind\\|ck_guided_operations_result_locator\\|ck_guided_operations_status_bundle' src/elspeth/web/sessions/models.py; awk 'NR>=1060 && NR<=1165 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/sessions/models.py",
   "detail": "The 'Trap' box cites ck_guided_operation_admission_blocks_kind (models.py:975), which belongs to the guided_operation_admission_blocks table (:955), not guided_operations. The constraint actually governing kind is ck_guided_operations_kind (:1067-1069), an EIGHT-value IN that already contains 'guided_plan' -- so the one-element-IN PostgreSQL reflection defect the box warns about does not apply to the described change, and guided_plan is not a new kind (guided_plan.py already reserves it at :411,:628,:690,:775,:930). The constraints the spec never names: ck_guided_operations_result_locator (:1124-1143) puts new compose kinds in the arm requiring result_kind='composition_state' AND result_message_id IS NULL AND proposal_id IS NULL, directly contradicting section 4's 'On completed, the client follows result_message_id / result_state_id'; ck_guided_operations_status_bundle (:1096-1122); and the partial unique index uq_guided_operations_active_proposal_admission (:1155-1162). The Evidence row cites models.py:996-1111, a window that truncates the status bundle and excludes the result locator entirely."},

  {"title": "operation_id is client-minted, and 'the HTTP surface never exposes an intermediate 202' is a written contract of the function the design builds on",
   "severity": "critical",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/guided_operations.py", "src/elspeth/web/sessions/models.py", "src/elspeth/web/sessions/routes/composer/guided.py"],
   "repro": "awk 'NR>=486 && NR<=545 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/sessions/routes/guided_operations.py; awk 'NR>=953 && NR<=983' src/elspeth/web/sessions/models.py",
   "detail": "reserve_or_replay_guided_operation takes no operation_id parameter: it reads it from the request body at guided_operations.py:506-511 and raises AuditIntegrityError if absent. Section 2's wire contract returns operation_id FROM the server, inverting the direction three existing mechanisms depend on -- guided_operation_admission_blocks (models.py:955-983, letting a client pre-close an exact id whose request body it lost), the 409 '(operation_id, request_hash) already bound' guard at guided_operations.py:526-529 and :538-542, and reconcile_guided_start_operation (guided.py:1322). The same docstring states at :495 'The HTTP surface never exposes an intermediate 202 response' and at :492-494 that a concurrent duplicate is resolved by holding the second request open until the first settles. The spec negates both without acknowledging either exists, so a reader cannot tell whether the invariant was overruled or unread."},

  {"title": "Five routes share the compose shape, not three: /guided/respond and /guided/chat are missed by the Evidence grep and by Decision 1",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/composer/guided.py"],
   "repro": "grep -rn 'Depends(_track_compose_inflight)' src/elspeth/web/",
   "detail": "The Evidence row claims the grep yields messages.py:121, compose.py:94, guided_plan.py:327. It yields five: those three plus guided.py:2902 (POST /{session_id}/guided/respond, decorator :2896) and guided.py:5745 (POST /{session_id}/guided/chat, decorator :5739). Both are provider-touching: /guided/chat runs _run_guided_chat_provider_attempt (guided.py:5731) and post_guided_respond's body (guided.py:2897-3640) carries 17 matches for composer_service|provider|llm_calls|planner. Both have the same buffered shape and the same lease, so it keeps the middlebox-cut defect the design exists to fix. Decision 1's rationale ('splitting means designing the operation contract once and re-arguing it twice') is made on the undercount -- the design splits anyway, and the compose lock must then serialise a request-task holder against an operation-task holder for the same session. guided.py:1805 and :5557 are also cancelling() reads in these forgotten routes."},

  {"title": "Section 6's premise is wrong: durable cross-instance COMPOSE exclusion already exists beside the asyncio.Lock",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/coordination/repository.py", "src/elspeth/web/coordination/sqlite_authority.py", "src/elspeth/web/sessions/routes/messages.py", "src/elspeth/web/app.py"],
   "repro": "awk 'NR>=4633 && NR<=4690 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/coordination/repository.py; cat src/elspeth/web/coordination/sqlite_authority.py; awk 'NR>=140 && NR<=151' src/elspeth/web/sessions/routes/messages.py; awk 'NR>=1578 && NR<=1584' src/elspeth/web/app.py; grep -rn 'get_lock(' src/elspeth/web/",
   "detail": "Every compose route takes the asyncio.Lock TOGETHER WITH SessionOperationLease.acquire(..., SessionOperationKind.COMPOSE, owner_instance_id=...) in one async with (messages.py:141-150, compose.py:110-119). repository.py:4633's docstring says 'Every other kind is exclusive and advances the row by epoch', and it raises SessionOperationConflictError when released_at IS NULL and lease_expires_at > database_now, under with_for_update(). sqlite_authority.py:18-25: 'A live lease always conflicts.' Installed on both dialects at app.py:1578-1584. So the asyncio.Lock supplies queueing, not correctness, and section 6's 'correct today only because a request pins to one instance' is false; the proposed fifth authority duplicates an existing one. Three further defects in section 6: the registry is keyed by arbitrary strings, not session ids (guided_chat_atomic.py:1412 and guided.py:3634 use f'{session_id}:guided-chat-admission' / ':guided-respond-admission'); get_lock is called at ~25 sites across 9 files INCLUDING execution/routes.py:1445, which Non-goals declares out of scope; and a PostgreSQL advisory lock is connection-held and re-entrant while asyncio.Lock is neither, with the transaction-scoped variant unusable for a turn that commits per-operation mid-flight."},

  {"title": "Section 1's 'already read and written by' list is wrong for five of seven files; /recompose has zero operation-record integration and the nine-file rename estimate is low",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/composer/compose.py", "src/elspeth/web/sessions/routes/messages.py", "src/elspeth/web/sessions/schema.py", "src/elspeth/web/_azure_container_apps_acceptance/controller.py"],
   "repro": "grep -rln 'guided_operations_table' src/  # -> 4 files; grep -n 'guided_operation' src/elspeth/web/sessions/routes/composer/compose.py; echo exit=$?  # -> exit=1; grep -c 'guided_operation' src/elspeth/web/sessions/routes/composer/guided_plan.py  # positive control -> 19; grep -rl 'guided_operations' src/ | wc -l  # -> 15",
   "detail": "Only four src files touch guided_operations_table: models.py, service.py, coordination/repository.py, blobs/service.py. The others named in section 1 match the MODULE routes/guided_operations.py -- messages.py:94 imports _join_shielded_task_after_cancellation (a task-join helper, not a read or write of the record); state.py:43, sessions.py:43, guided_chat_atomic.py:141 are likewise module imports. coordination/repository.py, a heavy table user (:704-752, :1923-1925), is absent from the list. Decisively, compose.py (POST /recompose, one of the three converted routes) has ZERO guided_operation references -- grep exit 1, with a positive control of 19 hits on guided_plan.py proving the pattern works, a control required here because of the module/table name collision. So for /messages and /recompose the operation record is net-new integration, not a kind widening. Risk 3's 'nine files' is also low: 15 src files contain guided_operations, 78 repo-wide. Its mitigation ('fails loudly at import or query time') is contradicted by controller.py:57, a raw SQL string constant, and schema.py:102,108,109,403-406, which pins three trg_guided_operations_* trigger names and a literal relname='guided_operations' inside the fail-closed schema-shape comparator."},

  {"title": "Lease-expiry semantics conflict: the record models takeover (attempt / taken_over / takeover_expired), section 5 proposes reap-as-failed",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/models.py", "src/elspeth/web/sessions/routes/guided_operations.py"],
   "repro": "grep -n 'attempt\\|taken_over' src/elspeth/web/sessions/models.py | sed -n '1,40p'; awk 'NR>=484 && NR<=500' src/elspeth/web/sessions/routes/guided_operations.py",
   "detail": "The existing row models expiry as takeover: guided_operations.attempt (models.py:1000, CHECK attempt >= 1), guided_operation_events event_kind 'taken_over' with ck_guided_operation_events_bundle requiring prior_attempt = attempt - 1 (models.py:1202), and reserve_or_replay_guided_operation(takeover_expired=True) whose docstring at :492-494 says an expired lease returns to 'the atomic reserve primitive, which either performs the sole takeover or observes the competing taker's active/terminal outcome'. Section 5 instead settles lapsed operations as failed. These are different delivery guarantees on the same row: takeover is at-least-once (a compose turn that already persisted the canonical user row and called the provider re-runs = double spend + duplicate assistant row), reaping is at-most-once. The spec neither chooses nor justifies, and never says what attempt means for a compose operation."},

  {"title": "request_hash idempotency has no database-level guard; the only 'request binding' unique constraint is PK-implied and exists as an FK target",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/models.py", "src/elspeth/web/sessions/routes/guided_operations.py"],
   "repro": "grep -n 'UniqueConstraint\\|PrimaryKeyConstraint\\|^Index(' src/elspeth/web/sessions/models.py | sed -n '/guided_operations/p'; awk 'NR>=1013 && NR<=1020;NR>=1186 && NR<=1196' src/elspeth/web/sessions/models.py",
   "detail": "Testing requires 'a replayed POST with the same request_hash must return the existing operation, not start a second turn', but there is no unique index on (session_id, request_hash). The table's unique constraints are pk_guided_operations(session_id, operation_id) at models.py:1013, uq_guided_operations_request_binding(session_id, operation_id, request_hash) at :1014-1019 -- implied by the PK and present only as the FK target for guided_operation_events at :1184-1189 -- and the partial uq_guided_operations_active_proposal_admission at :1155-1162. Today idempotency rides on the client-minted operation_id and the 409 binding check. Invert minting per section 2 and request_hash becomes the sole replay key, enforced by a read-then-write (get_guided_operation at guided_operations.py:532, then reserve at :516) with nothing in the schema to catch the interleaving: two concurrent identical POSTs both read absent, both insert under different server-minted ids, two turns run, two provider spends."},

  {"title": "Section 5's cancel is the lease heartbeat's cancel at the heartbeat's latency, not 'immediate and precise' -- and BLOCKER 1 destroys that path",
   "severity": "high",
   "confidence": "probable",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/models.py"],
   "repro": "grep -n '_COMPOSER_HEARTBEAT_SECONDS\\|_COMPOSER_REQUEST_LEASE_SECONDS\\|_COMPOSER_HEARTBEAT_LEASE_LOST' src/elspeth/web/sessions/routes/_helpers.py; awk 'NR>=2617 && NR<=2630' src/elspeth/web/sessions/routes/_helpers.py",
   "detail": "Correction first, because it makes the finding precise: a cross-instance cancel DOES exist today, DB-mediated. Revoking or settling the row on instance A makes instance B's next registry.renew_request raise ComposerRequestLeaseLost, which the renewal loop converts to owner_task.cancel(_COMPOSER_HEARTBEAT_LEASE_LOST) at _helpers.py:2566; B's next fenced write raises GuidedOperationFenceLostError, handled at guided_plan.py:768 (also :883, :924) by joining the durable winner. That is designed fail-closed fencing, so the Non-goals rejection of LISTEN/NOTIFY is not the gap it appears to be. What is wrong: (1) section 5 calls the cancel 'Immediate and precise', but the only delivery path is the renewal poll at _COMPOSER_HEARTBEAT_SECONDS = 15.0 (:2328) against a 60 s lease (:2330) -- worst case a full heartbeat interval; (2) one in-flight provider call therefore always completes and is paid for, so Risk 2's 'explicit cancel is the primary path, so the TTL can be generous' moves the spend leak from a TTL-sized window to a heartbeat-sized one rather than eliminating it; (3) BLOCKER 1 destroys the mechanism -- the renewal loop and the lease are torn down at 202 (:2617-2629) -- and section 5's replacement is asyncio.Task.cancel(), which reaches only the calling process. The reaper backstop inherits all three."},

  {"title": "The current_task() inventory is short by nine sites; the critical miss is _cancel_on_client_disconnect, which delivers the cancel and forbids running the guarded block in a child task",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/_helpers.py", "src/elspeth/web/composer/llm_response_parsing.py"],
   "repro": "grep -rn 'current_task()' src/elspeth/web/  # -> 12 sites, not 3; awk 'NR>=2199 && NR<=2326 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/sessions/routes/_helpers.py",
   "detail": "The Evidence row claims the grep yields _helpers.py:2486, guided_plan.py:238 and :730. Over src/elspeth/web/ it yields twelve; even scoping to the three named route files plus _helpers.py gives four. The miss is _helpers.py:2236 -- _cancel_on_client_disconnect, mounted at messages.py:347, compose.py:191, guided_plan.py:491, guided_chat_atomic.py:1498, the context manager that DELIVERS every client-disconnect cancel. The first bullet of its docstring, at :2217-2220, states 'The guarded block MUST be awaited inline in the route task -- running it in a child task would launder the CancelledError instance at the task boundary and drop the attached llm_calls audit records.' attach_llm_calls (llm_response_parsing.py:917, called from composer/service.py:7150,7994,8674-8713, pipeline_planner.py:3249, tool_batch.py:323) rides on the CancelledError instance, so this is audit evidence, not telemetry. The watcher also manipulates the very counter section 3 relies on, calling task.uncancel() at :2288 and :2312. Section 5's 'the origin already cancels correctly ... only its trigger changes' understates a rewrite of the marker protocol, the uncancel bookkeeping and the audit-record attachment path onto a request-less task."},

  {"title": "'Nothing wraps a handler body' implies a verbatim relocation the body cannot survive: 50 request.* reads including request.receive()",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/sessions/routes/messages.py", "src/elspeth/web/sessions/routes/composer/compose.py", "src/elspeth/web/sessions/routes/composer/guided_plan.py", "src/elspeth/web/sessions/routes/_helpers.py"],
   "repro": "for f in src/elspeth/web/sessions/routes/messages.py src/elspeth/web/sessions/routes/composer/compose.py src/elspeth/web/sessions/routes/composer/guided_plan.py; do echo \"$f: $(grep -c 'request\\.' $f)\"; done  # -> 26 / 16 / 8",
   "detail": "The three handler bodies carry 26, 16 and 8 textual request.* reads: request.app.state.* (survives), request.state.composer_request_lease (survives as an object), _failure_log_request_id(request) reading request.scope['state'] (_helpers.py:2188-2196), request.url.path (_helpers.py:2475), and -- fatally -- await request.receive() inside _cancel_on_client_disconnect (_helpers.py:2246), which after response completion no longer describes a live client. Section 3 reads as though the body relocates unchanged ('The request handler's job ends at record the operation, start the task, return 202'; 'Nothing wraps a handler body'). Every request-derived read must instead be resolved into an owned context before the 202 is sent -- the bulk of the implementation, uncosted anywhere in the spec."},

  {"title": "Section 8 addresses only the error path; the success path consumes the POST body and the operation record cannot express its proposals list",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/frontend/src/stores/sessionStore.ts", "src/elspeth/web/sessions/models.py"],
   "repro": "awk 'NR>=2125 && NR<=2190 {printf \"%d: %s\\n\", NR, $0}' src/elspeth/web/frontend/src/stores/sessionStore.ts; awk 'NR>=1124 && NR<=1143' src/elspeth/web/sessions/models.py",
   "detail": "sessionStore.ts:2131-2132 destructures the POST body -- const { message, state } = result; const proposals = result.proposals ?? [] -- driving lastComposeChangedPipeline (the version comparison behind the 'Pipeline updated' vs 'Response ready' badge, elspeth-bf9c296ee5), getExecutionStore().clearValidation() on a version change (R4-H3), mergeCompositionProposals, selectedNodeId clearing, and the defensive backfill of message by id. The retry/recompose path does the same at :2687-2688, so BOTH sites section 8 says 'converge on one path' consume the POST body on success, not only on error. Section 8 discusses only discriminators, isComposeAbort and the Retry affordance. The operation record cannot carry proposals: it has a single nullable proposal_id (models.py:1002) and ck_guided_operations_result_locator (:1124-1143) forbids even that on a completed non-guided kind. Section 7's claim that 'the operation record is the authority; it must not be lossier than the advisory channel beside it' is asserted about the failure enum while the success side is measurably lossier than the response body it replaces."},

  {"title": "The client-side 270-second compose deadline and the Stop button both become inert under a hard 202, unmentioned in section 8",
   "severity": "high",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/frontend/src/config/composer.ts", "src/elspeth/web/frontend/src/hooks/useComposer.ts", "src/elspeth/web/frontend/src/components/chat/ChatPanel.tsx", "src/elspeth/web/frontend/src/stores/sessionStore.ts"],
   "repro": "grep -rn 'COMPOSE_TIMEOUT_MS\\|COMPOSE_USER_CANCEL_ABORT_REASON\\|COMPOSE_TIMEOUT_ABORT_REASON' src/elspeth/web/frontend/src/ --include='*.ts' --include='*.tsx' | grep -v test; grep -n 'isComposeAbort' src/elspeth/web/frontend/src/stores/sessionStore.ts",
   "detail": "config/composer.ts:29 sets DEFAULT_COMPOSE_TIMEOUT_MS = 270_000 + COMPOSE_CLIENT_GRACE_MS, fired by controller.abort(COMPOSE_TIMEOUT_ABORT_REASON) at :109; useComposer.ts:66 and ChatPanel.tsx:930 abort the same controllers for the Stop button. Both abort a fetch that under a hard 202 resolved in milliseconds, so the 270-second client-side deadline stops bounding anything (leaving NO client-side deadline on the operation) and Stop aborts nothing unless rewired to POST .../cancel. Section 8 says isComposeAbort 'stops meaning our AbortSignal fired', which addresses classification but not the two mechanisms producing the signal. isComposeAbort also has eleven call sites (sessionStore.ts:421, 2209, 2239, 2246, 2260, 2286, 2746, 2764, 2779, 2798, 4124, 4357), one of which (:421) underpins isAmbiguousComposeNetworkFailure, whose premise ('A fetch transport failure does not tell the browser whether the POST reached the server') changes meaning when the POST is a handle fetch."},

  {"title": "The crash and race windows between 'write the row', 'return 202' and 'start the task' are unenumerated and the ordering is unspecified",
   "severity": "high",
   "confidence": "probable",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md"],
   "repro": "Design reading: section 3 names three handler jobs without stating an ordering constraint; section Risks names none of the windows; the only stated idempotency answer is request_hash, which has no unique index (see the request_hash finding).",
   "detail": "Five windows go unnamed: (1) row written, process dies before the task starts -> no runner, row sits in_progress until the lease lapses, recovery depends on the unresolved takeover-vs-reap choice; (2) row written and task started, process dies before the 202 flushes -> the client has no operation_id, so an orphan turn runs or is reaped with nothing able to poll or cancel it, and with a server-minted id the client cannot even use guided_operation_admission_blocks to close it; (3) task started before the row commits -> a turn runs with no lease, no poll target and no reaper visibility, and its writes have no operation to settle; (4) 202 returned before the row commits -> the client polls a 404 and section 4 defines no non-terminal error to distinguish 'not yet visible' from 'never existed'; (5) concurrent duplicate POSTs -> two rows, two turns, two provider spends."},

  {"title": "Anchor and measurement defects: app.py:1722 is a comment, two of three route decorators are off by one, the ~18 HTTPException figure is wrong in both readings, and the models.py Evidence range hides the two constraints that break the design",
   "severity": "low",
   "confidence": "confirmed",
   "files": ["docs/specs/2026-09-16-composer-async-operations-design.md", "src/elspeth/web/app.py", "src/elspeth/web/sessions/routes/messages.py", "src/elspeth/web/sessions/routes/composer/compose.py", "src/elspeth/web/sessions/models.py", "tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py"],
   "repro": "grep -n 'dialect.name == \"postgresql\"' src/elspeth/web/app.py  # -> 1581, 1691, 1723 (not 1722); grep -n '@router.post(' src/elspeth/web/sessions/routes/messages.py src/elspeth/web/sessions/routes/composer/compose.py  # -> 109, 83 (spec says 110, 84); for f in messages.py compose.py guided_plan.py; do grep -c 'raise HTTPException' ...; done  # -> 16 / 14 / 1",
   "detail": "M1 app.py:1722 is a comment line; the dialect if is at 1723 (cited twice: Evidence and section 4). M2 the section 6 seam is app.py:1723-1739, not 1720-1725, and is one of three dialect splits (1579-1584, 1691, 1723). M3 route decorators are at messages.py:109 and compose.py:83, not :110 and :84 (guided_plan.py:322 correct). M4 HTTPException raise sites are 16 / 14 / 1 = 31 across the three files -- '~18' is ~20% high for messages.py alone (15 of its 16 are in send_message; :1185 belongs to the GET at :1146) and 42% low for the three-file enumeration section 7 requires. M5 the Evidence range models.py:996-1111 starts after the table (:990) and truncates ck_guided_operations_status_bundle (ends :1122) while excluding ck_guided_operations_result_locator (:1124-1143) entirely. M6 _track_compose_inflight is at _helpers.py:2432; :2486 is the owner_task capture. M7 'the five tests' are unnamed and unidentifiable: test_compose_heartbeat_renewal.py holds 13 tests, and nine files in tests/ are named test_routes.py. Verified-correct rows (do not re-measure): conftest.py:65,109; 1,911 test functions; 113 files; 68 testcontainer files; StreamingResponse only in execution/routes.py; _helpers.py:323; progress.py convergence/client_cancelled anchors; sessionStore.ts:2108-2110, :2595-2612, :2230, :2757; the 12-value failure_code enum listing; the llm_auth_error and convergence gaps."}
]}
```
