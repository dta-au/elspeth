# Panel position — Operations / SRE lens

Tree: `release/0.8.1` @ `6506f6a7f` (measured 2026-09-28). Read-only except this file.
Position: **B — separate `composer_async_operations` table, share the receipt request-hash codec only.**
Confidence: moderate-high on "separate table" (A is the option I would argue hardest against);
low-stakes on B vs C (the codec choice is almost invisible to operations).

## 1. What I measured

| Fact | Evidence |
|---|---|
| Receipt reservation requires an already-held, live SOL context; the row is born under the fence | `service.py:1045-1076` (`_require_session_operation_context_on_connection` at :1063-1065 before `reserve_operation_receipt`); route helper acquires `SessionOperationLease` then reserves, `routes/operation_receipts.py:239-250` |
| Receipts recover by **lazy takeover at the next reserve**, not by a reaper: an expired `in_progress` row gets a new token/attempt and a `taken_over` event | `operation_receipts.py:318-353` |
| `ix_session_operation_receipts_status_lease (status, lease_expires_at)` exists but no periodic scan or reaper consumes it; every select on the table is PK-, session- or `result_session_id`-scoped | `models.py:819-823`; the 12 `select(session_operation_receipts_table…)` sites (grep inventory, not a registry count): `blobs/service.py:487,525,2286`, `service.py:2188`, `operation_receipts.py:206`, `repository.py:724,737,756,765,3019,4062,5186` |
| The archive decision reads receipts by `status == 'in_progress'` **without a kind filter**, and raises `AuditIntegrityError` for any kind outside fork/revert | `coordination/repository.py:722-734` |
| Non-fork receipts map to the COMPOSE SOL kind by an `else` branch | `service.py:1062` (`SESSION_FORK if kind == "session_fork" else COMPOSE`), `routes/operation_receipts.py:242` |
| Every renewal of a receipt appends an event row; every default read re-validates the whole event log | `operation_receipts.py:369-399` (`renewed` event), `:203-218` (`verify_events=True` default → `_validate_events` loads all events, :170-200) |
| Terminal immutability is `BEFORE UPDATE` only | `models.py:1653-1654`, `:1861-1862` |
| Shared thread pool for every sync DB call is 16 workers + 16 queued | `web/async_workers.py:14,18` |
| `MessageWithStateResponse` = `message`, `state` (full `CompositionStateResponse` when changed), `proposals` | `schemas.py:234-243` |
| Session epoch is 71 | `models.py:59`, `schema.py:45` |

## 2. The three strongest reasons (SRE lens)

### R1 — Two different recovery models must not share one row type

Receipts and compose jobs recover from a dead owner in **opposite** ways:

- a receipt is request-scoped, born under a held SOL, and its expired `in_progress` row is *taken over and re-executed*
  by whichever retry arrives next (`operation_receipts.py:318-353`); nothing scans for it;
- a compose job is admitted **before** any lease (spec §2: "The POST does not wait for a session compose lock"),
  may be reclaimed only while `queued`, and a `running` row is **never** taken over; a periodic, cross-session reaper
  settles it `worker_lost` once the bound SOL lapses (spec §1, §3; contract § Composite start).

At 03:00 the operator question is "this row looks expired — will something pick it up, and what will it do?". In one
table the answer depends on `kind`, and the columns that answer it differ per kind (`lease_expires_at` means
"retry may re-run" for a fork but would be NULL/unused on a running compose row, whose liveness is the SOL row in
another table). Option A also forces a second, *unfenced* insert path into a table whose only writer today proves a
live SOL first (`service.py:1063-1065`) — the receipts' strongest operational invariant ("a receipt row exists ⇒
someone held the session fence when it was born") stops being true for the table. Keeping the tables apart keeps
each table's "stuck row" diagnosis a single, kind-free rule.

### R2 — Failure isolation: A couples compose defects into fork/revert/archive hot paths; B does not

Under A, compose rows become visible to eleven existing receipt read sites written for fork/revert only. The
concrete break I can point at: `decide_and_soft_archive` selects any `in_progress` receipt without a kind filter
and raises `AuditIntegrityError` for an unknown kind (`repository.py:722-734`). If A's running/claimed state reuses
`in_progress`, archiving a session with a live compose turn becomes a 500 — and D7 in the contract says compose
jobs must **not** refuse archive at all. If A uses new statuses instead, every receipt query, CHECK bundle,
`_validate_row` arm (`operation_receipts.py:93-148`), `_terminal_hash` and the terminal trigger become a
kind × status matrix, so a compose-side codec or CHECK regression rides the same module and the same
AST-fingerprinted writer manifest (`test_session_db_mutation_authority.py:189-191`, `:2589-2709`) as fork/revert.
The blast radius of a compose change is then "fork, revert, archive, blob fork-copy" — exactly the surfaces that
must keep working while a compose incident is being diagnosed (revert is the user's escape hatch).

Physical contention also moves into the fork/revert table under A: compose rows are updated several times
(queued → claimed → renewed → running → terminal) and carry a large `result_json` (a full `MessageWithStateResponse`,
including the whole composition state when it changed, `schemas.py:234-243`), so dead tuples and TOAST churn land
in the table the archive and fork paths read. On SQLite the single writer is shared either way, so this is a
PostgreSQL-only effect, but it is the deployment where multi-instance load exists.

What the table choice does **not** isolate (applies to every option, and deserves its own plan line): the SOL is
per-session by design (R3 — a running compose turn will 409 a fork/revert on that session until its deadline), the
session advisory lock, the DB connection pool, and the 16+16 `run_sync_in_worker` pool (`async_workers.py:14,18`).
At the default cap (`composer_async_max_queued_operations=64`), 1 s polls from every active tab plus each
instance's 1 s scan and the worker's own DB calls all queue on that pool beside fork/revert. A compose load spike
therefore degrades fork/revert through `AsyncWorkerAdmissionTimeoutError`, not through the table.

### R3 — Working set, retention and scans stay simple and bounded in a dedicated table

- The nonterminal population is bounded by the admission cap (≤ 64 default, ≤ 10 000 max, contract § Settings), so
  every worker/reaper query on a dedicated table (`claim_next`, `list_expired_running`, `list_expired_queued`,
  `count_nonterminal`) is O(cap) behind a status-leading index, independent of history. Under A every such query
  also needs `kind IN (...)` and a kind-leading or kind-partial index, and the capacity count must not accidentally
  count fork/revert rows.
- Settled compose rows grow without bound and are the heaviest rows in the session DB after composition states.
  Whether an old `result_json` may ever be pruned is an owner/ADR-046 question I do **not** answer here (the stored
  response is the only record of the exact projection the user was shown). A dedicated table keeps that decision
  open with its own authority and without touching fork receipts, which *are* archive-deciding evidence
  (`repository.py:752-770`: completed fork receipts make an archive soft). Under A, any future retention rule needs a
  kind-scoped DELETE exception on a table that also holds records the archive logic depends on.
- The receipts' append-an-event-per-renewal + validate-all-events-per-read design (`operation_receipts.py:170-218`,
  `:369-399`) is right for short fork/revert requests; for a job polled every second for minutes it is write
  amplification plus O(events) validation on every poll. B avoids adopting it by accident.

## 3. B vs C (codec)

Operationally the codec is invisible except when diagnosing a 409 "operation id already bound to a different
request". One hash scheme (session + kind + normalized strict DTO minus `operation_id`,
`operation_receipts.py:39-53`) is easier to reason about than two byte-for-byte siblings that can drift. Prefer B,
but share it as a kind-agnostic function (parameter typed `str` / a union Literal defined beside it), not by widening
`OperationReceiptKind` — widening that Literal would make compose kinds pass `_validate_identity`
(`operation_receipts.py:65-71`) and the kind→SOL `else` branch (`service.py:1062`) silently. D's larger shared core
(identity + terminal replay + event log) buys an event log I argue compose jobs should not have; I would not pay
for it now.

## 4. Strongest argument against my position

A (or D) gives one place for "every client-minted session operation": one operator query for "what is in flight on
this session", one durable claim/attempt history (the receipt events table) instead of structlog-only history for
compose claims, one terminal-immutability trigger and one mutation authority for a team that is about to grow.
B leaves compose jobs with **no durable claim history** — `attempt` and `claim_owner_instance_id` survive, but "claimed
by X, released on SOL conflict, reclaimed by Y" lives only in `composer_operation.*` slog events (T11 lines 67-78).
My answer: the per-session union is a documented diagnostic query (below), not a shared table, and claim churn is
transport telemetry, not audit evidence — the turn's audit lives in `chat_messages` and the LLM-call cohort. If the
owner rules claim history is evidence, add a compose-owned events table under B rather than merging.

## 5. Consequences for T02–T06 (and the ops tasks that follow)

- **T02 (types/codec):** import the receipt request-hash (made kind-agnostic in the same commit, with its schema
  string unchanged) instead of `composer_operation_request_hash`; do not widen `OperationReceiptKind`. Decide the
  identity question in §6 first — the plan's `SendMessageRequest(_GuidedOperationRequest)` no longer compiles against
  the tree.
- **T03 (schema):** re-anchor every insertion point — the task cites `guided_operation_events_table` (:1279) and guided
  triggers that `7001600fe` removed; insert after `session_operation_receipt_events_table` (`models.py:825-870`). Add a
  `(status, deadline_at)` index (or extend the claimable index) so `list_expired_queued` does not rely on the small
  working set alone; keep `(session_id, status)` for the D8 active check and the operator query. Table comment should
  state the retention class ("transport job; retention is an owner decision; not archive-deciding").
- **T04 (authority):** unchanged in shape. Make `count_nonterminal`, `claim_next` discovery and both listers
  status-index-only queries with explicit `LIMIT`; state the soft-cap bound (F-C7) in the docstring as planned.
  No change to `decide_and_soft_archive` is needed under B (it never sees compose rows) — record that as the
  deliberate D7 consequence. Under A this task would additionally own a kind filter at `repository.py:724-734`.
- **T05/T06 (composites):** unaffected by the table choice; they bind to the SOL triple either way. Under A they
  would have to bypass `settle_operation_receipt`, whose CAS requires a live receipt `lease_expires_at`
  (`operation_receipts.py:499-510`) that a SOL-bound running row does not have — i.e. A would reuse the table but
  not the machinery.
- **T11/T12 (worker, poll):** add a pool-pressure line: poll GETs and the scan share the 16+16 pool with fork/revert;
  measure p99 of fork/revert under 64 active polls in T16's PG suite, and keep the poll read a single PK select with
  no event-log validation on non-terminal rows.
- **T15/T17 (deploy, epoch):** the next epoch is **72**, not 68; the bump is a stop-the-world recreate per
  `docs/runbooks/staging-session-db-recreation.md` identical for every option (an in-flight fork receipt or compose
  job is lost at the reset either way). Add the epoch-72 paragraph and an operator "stuck turn" section:

  ```sql
  SELECT j.status, j.kind, j.attempt, j.claim_owner_instance_id, j.created_at, j.started_at,
         j.deadline_at, j.cancel_requested_at, j.failure_code,
         f.operation_kind, f.owner_instance_id, f.operation_epoch, f.lease_expires_at, f.released_at
  FROM composer_async_operations j
  LEFT JOIN session_operation_fences f ON f.session_id = j.session_id
  WHERE j.session_id = :sid AND j.status IN ('queued','running');
  -- plus: SELECT kind, status, attempt, lease_expires_at FROM session_operation_receipts
  --       WHERE session_id = :sid AND status = 'in_progress';
  ```

  Rules the runbook states: `queued` + expired claim → any instance reclaims; `running` + live fence → turn in
  progress on `owner_instance_id`; `running` + lapsed/released fence → the reaper settles `worker_lost` on its next
  pass; a `running` row whose session is archived → `settle_lost_inactive_session`.

## 6. Facts in the brief (or plan) that are wrong or missing

1. **Missing:** `POST /messages` already has a client-minted idempotency key and table. `SendMessageRequest` carries a
   required `client_request_id: UUID` (`schemas.py:143-153`), bound by `message_ingress_receipts`
   (`models.py:464-487`, epoch 69) to exactly one user message (`service.py:4508-4538`). A compose job with a separate
   `operation_id` would give one send two idempotency keys and two 409 vocabularies — a diagnosis hazard. The panel
   should decide whether `compose_message.operation_id` *is* the `client_request_id` (my recommendation) or is bound
   to it on the row.
2. **Stale in the contract:** `_GuidedOperationRequest` no longer exists; the strict base is `_SessionOperationRequest`
   (`schemas.py:72-77`). `SendMessageRequest` derives from the non-strict `_RequestModel` (`schemas.py:66-69,143`), so
   it does not satisfy the receipt codec's strict + `operation_id` preconditions (`operation_receipts.py:41-45`) today.
3. **Stale in the spec:** §2 names `guided_operation_request_hash` as the model; it is gone from `src/` (0 grep hits);
   the live equivalent is `operation_receipt_request_hash`.
4. **Stale in the contract:** "Epoch 68 bump" — the tree is at 71 (`models.py:59`), so this change is epoch 72.
5. **Missing from the brief (bears on A):** receipt reserve requires a live SOL context (`service.py:1063-1065`), and
   `state_revert` rides the COMPOSE SOL kind via an `else` branch (`service.py:1062`). A queued, pre-lease compose row
   cannot use `reserve_operation_receipt` at all.
6. The brief's receipt facts (PK, kind/status sets, single lease, locators, `response_hash`, no request/result JSON,
   events table columns, `OperationReceiptTakenOver`) all verified at `models.py:727-870` and
   `operation_receipts.py:1-527`.
