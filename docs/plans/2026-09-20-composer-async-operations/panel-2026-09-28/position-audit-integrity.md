# Panel position: audit integrity and trust tiers

Lens: ADR-046 audit primacy, ADR-032 validate-by-trust-domain, Tier-1 read
guards, the session-DB mutation-authority gate. Tree `release/0.8.1` @
`6506f6a7f`, read-only; every citation below was read on that tree.

## Position

**B, sharpened: a separate `composer_async_operations` table with its own
append-only event table, and one canonicalisation core shared with receipts
but parameterised by schema tag.** Confidence: high on "not A"; moderate-high
on B over C; D rejected on this lens.

"Shared codec" should not mean calling `operation_receipt_request_hash`
as it stands. It hardcodes `_REQUEST_SCHEMA = "session-operation-receipt-request.v1"`
(`src/elspeth/web/sessions/operation_receipts.py:36`, used at `:53`). Calling it
for compose would stamp compose hashes with the receipt schema and tie the two
families' versioning together. The audit-grade version is one function that
performs the strict/extra-forbid check, the `operation_id` exclusion, the
default/None materialisation and the `stable_hash`. It takes the schema tag as
an argument. Receipts pass `session-operation-receipt-request.v1` (byte-identical
hashes, no churn) and compose passes `composer-operation-request.v1`. The response
hash works the same way: `operation_receipt_response_hash` (`:56-62`) and the
planned `composer_operation_result_hash` canonicalise the same kind of object
(a strict public DTO), so they should share one implementation.

## Three strongest reasons

### 1. Under A, authority blur is structural, not a matter of style

`TablePolicy.permits` (`tests/unit/architecture/test_session_db_mutation_authority.py:36-44`)
matches on table, authority and operation only. It knows nothing about `kind`.
The receipts table currently has one owner and exactly one narrow grant
(`:190-195`: `SessionOperationReceiptAuthority` plus
`SessionForkParentReceiptMutations` for `update`), and a dedicated test pins
that exact tuple (`:12212`, and `:12213-12215` checks that the grant does not
extend to `delete`).

The compose job already needs three writers by the contract's own count
(`contract.md:202-203`: `ComposerAsyncOperationAuthority` owner,
`SessionOperationAuthority` update for the R3 start composite,
`SessionComposerOperationTerminalAuthority` update for the R2 terminal). Under A,
every one of those grants applies to fork and revert rows too, and the gate
cannot tell the difference. In the other direction,
`_ForkParentReceiptMutations.bind_fork_receipt`
(`src/elspeth/web/coordination/repository.py:4105-4150`, manifest `:2586-2595`)
gains update reach over compose rows. The only thing stopping that is the
exact-field predicate the author happened to write. The gate is supposed to
prove "one named authority per writer, least privilege". Under A it can only
prove "some authority from this union touched this table". Fork/revert writers
and the compose worker would share one blast radius. Under B each table keeps a
policy the gate can actually verify.

### 2. Under A, takeover machinery is a provider-replay hazard

`reserve_operation_receipt` takes over any expired `in_progress` row with a new
attempt and token (`operation_receipts.py:316-353`, and `OperationReceiptTakenOver`
at `:349`). That is correct for fork and revert, where the protected work can
simply be redone. A compose `running` row must never be taken over (spec §1,
§4 "No provider replay"). A replay would duplicate the `llm_calls` audit cohort
and the provider spend for a single user action. Under A, the only thing
preventing a replay would be a kind branch inside shared reserve/renew code that
every future editor must remember. Under B the compose table has no takeover
code path to misuse. The safety comes from the code's structure, not from
someone remembering a rule.

### 3. Under A, one row type would carry two incompatible models of terminal evidence

Receipts rebuild the response from locators and then verify it:
`_replay_verified` (`src/elspeth/web/sessions/routes/operation_receipts.py:95-104`)
rebuilds the response from `result_session_id`/`result_state_id` and compares
`operation_receipt_response_hash` with the stored `response_hash`. Compose has
to store the response and then verify it, because the response cannot be
rebuilt. `_state_response(..., live_validation=...)`
(`src/elspeth/web/sessions/routes/_helpers.py:880-920`) fills in
`validation_warnings`/`validation_suggestions` only from a just-computed,
never-persisted `ValidationSummary`. `MessageWithStateResponse.proposals`
(`schemas.py:234-243`) is read at the final commit. A locator therefore cannot
reproduce the payload, and rebuilding it would be exactly the "rebuilt audit
evidence" that ADR-046 forbids.

Under A, the terminal-evidence parts of the receipt module would each need a
per-kind dispatch:

- `_validate_row` (`:93-148`)
- `_terminal_hash` (`:151-167`, which covers `response_hash` and locators but
  not a result body)
- `_validate_events` (`:170-200`)
- `_outcome` (`:221-234`)

`response_hash` would mean "hash of the rebuilt response" on some rows, and
`result_sha256` would mean "hash of the stored body" on others. Two traps are
already latent in that code:

- `_validate_identity` rejects every kind except fork/revert (`:68`). Fail-closed
  is good, but it means every reader must be widened at the same moment.
- `_outcome` has a bare `else` that treats any non-fork row as a state revert
  (`:232-233`).

Tier-1 read guards stay easiest to review when each row shape has one
validator, and B keeps it that way.

## Finding on the plan, independent of A/B/C: compose has no append-only history

Receipts ship an append-only event log: `session_operation_receipt_events`
(`models.py:825-873`) has `claimed/renewed/taken_over/completed/failed` events,
an `actor`, and a `terminal_hash` that is checked against the row on every read
(`_validate_events`, `operation_receipts.py:170-200`). It is protected by
`trg_..._no_update` and `trg_..._no_delete` (`models.py:1658-1698`, SQLite
`:1868-1885`, and the required inventory at `schema.py:106-108`).

The planned `composer_async_operations` table has no event table at all: there
are zero hits for `composer_async_operation_events` in the plan folder or the
spec. Its row is changed in place (claim, release, reclaim, start, cancel,
settle), and on settlement it nulls the whole claim triple, including
`claim_owner_instance_id` (`T03.md:716-717` and the status bundle at `T03.md:834-849`;
`T06.md:319`). The SOL triple kept on a terminal row does not recover who ran
the job. `session_operation_fences` has **`session_id` as its only primary key**
(`models.py:325-351`), so the next operation on the session overwrites its
`owner_instance_id`. After that, the compose row's
`(session_operation_id, lease_token, epoch)` points at nothing that is kept.

Consequences for a settled compose row:

- **Which instance ran the turn:** lost at settlement.
- **Which settlement path decided the terminal:** unrecorded. The owner CAS,
  `settle_lost` (reaper), `settle_own_lapsed` (F-M3),
  `settle_lost_inactive_session` and `settle_unstarted` (queued deadline or
  cancel) all leave the same row shape. A `worker_lost` or `request_cancelled`
  terminal is exactly where an auditor asks "who decided this, and on what
  evidence".
- **Claim/release/reclaim history:** reduced to an `attempt` counter. This is
  low value, because queued claims have no side effects.

This is a gap in audit primacy, and it is not solved by choosing A. Under A the
compose writers would append to the receipt events table, which widens that
table's policy, and that policy is currently pinned to exactly one authority
with `operation_authorities == ()` (`test_session_db_mutation_authority.py:12210-12211`).
Under D a shared events table has the same cost. It also has no composite FK
target: the receipt events FK is
`(session_id, operation_id, request_hash) → session_operation_receipts`
(`models.py:840-845`), and it cannot point at two parent tables without a
supertype table. **B with its own `composer_async_operation_events` table is the
best fit on this lens.**

Recommended vocabulary for that table:

- `admitted`
- `claimed`, `claim_released`, `reclaimed`
- `started`, carrying `owner_instance_id` and the SOL triple
- `cancel_requested`, carrying the actor
- `completed` and `failed`, carrying a `settled_by` path discriminator and a
  `terminal_hash` over `(status, failure_code, result_schema, result_sha256, attempt, sol triple)`

It should have the same no-update and no-delete-while-session-exists triggers,
and every read should verify it exactly as `_validate_events` does. Queued-claim
renewals should not be evented, because they are heartbeat traffic with no
evidential value (receipts event their renewals only because a receipt lease
guards side effects).

A deletion gap goes with it, read from the DDL alone. The receipt events FK
cascades from the receipt row (`models.py:840-845`), and the events `no_delete`
trigger aborts while the session still exists (`models.py:1682-1690`). A direct
`DELETE` of a receipt row while its session lives therefore aborts when the
cascade reaches its events. The planned compose table has only the UPDATE-time
terminal-immutable trigger (`T03.md:63`), no DELETE guard and no child table, so
a terminal compose row can be deleted while its session exists. Under B with an
event table, compose gets the same protection.

Keep this claim measured. The startup trigger inventory already catches a
silently dropped immutability trigger, so the event log is not needed as a
second immutability witness. Its value is settlement provenance and history.
If the owner rules an event table out of scope, the minimum that avoids losing
evidence is two changes on the row: keep a `started_by_instance_id` through
settlement, and add a `settled_by` discriminator with a closed CHECK.

## What I checked and did not treat as a problem

`request_json` is cleared at settlement in every option (spec §1 calls it
session data that is removed atomically). `request_hash`, `user_message_id` and
the persisted user row keep the binding, so clearing it does not lose audit
evidence.

## Strongest argument against my position

A reuses a facility that already exists, is gated, and has an event log:
reserve, renew, settle, append-only events, `terminal_hash` checked on read, the
terminal-immutable trigger and the required-trigger inventory. B means building
and gating a second copy of all of that for a second table:

- a second trigger family in five places
- a second `_validate_row`
- a second event verifier
- a second digest-shape entry
- a second PG-reflection bundle

Two copies can drift, and a gap in the copy nobody is watching is a real
integrity risk. Having to recommend a compose event table is itself evidence
that B, as the plan wrote it, had already drifted below the receipts standard.
Under A that drift would have been impossible, because the compose row would
have inherited the event log.

My answer: the shared canonicalisation core, together with an explicit
conformance test that both families ship the same invariant set, captures most
of the anti-drift benefit. It does so without a kind-blind authority union and
without a takeover path next to a no-replay row. Duplicated lines are cheaper
than a gate that cannot tell writers apart.

## Consequences for T02–T06

- **T02 (types and codec).** Extract one private core from
  `operation_receipt_request_hash` and `operation_receipt_response_hash`, for
  example in a mode-neutral module, with an explicit `schema: str` argument.
  Keep the receipt wrappers byte-identical (prove it with a pinned-hash test
  over a fixture DTO). `composer_operation_request_hash` becomes a thin wrapper
  that passes `composer-operation-request.v1`, and
  `composer_operation_result_hash` reuses the response core. The core hashes a
strict DTO, not text. Today the contract's `composer_operation_result_hash(result_json: str)`
takes text, while `operation_receipt_response_hash` takes a `BaseModel`. The
compose write and read paths should therefore run text →
`MessageWithStateResponse`/`ComposerOperationError.model_validate(..., strict=True)`
→ the shared hash, so both families hash the same kind of object. The spec §2
  sentence "the model to follow, not a function to share" predates the
  mode-neutral receipts, and T17 should amend it. Add
  `ComposerOperationEventKind` and the terminal-hash schema string.
  `ComposerOperationRecord.__post_init__` remains the only Tier-1 row validator
  for this table, and a sibling validator is needed for event rows.
- **T03 (schema).** The anchors are stale. `T03.md:22`, `:689` and `:880-926`
  insert relative to `guided_operation_events_table` at `models.py:1279`, which
  has 0 hits after `7001600fe`. Re-anchor after
  `session_operation_receipt_events_table` (`models.py:825-873`). Add
  `composer_async_operation_events` with PK
  `(session_id, operation_id, sequence)` and a composite FK
  `(session_id, operation_id, request_hash)` to the job table. That requires a
  `UniqueConstraint(session_id, operation_id, request_hash)` on the job, as
  receipts do at `models.py:748`. Also add a per-event-kind bundle CHECK written
  as AND/OR arms, and no-update/no-delete triggers in all five trigger places
  plus `_REQUIRED_AUDIT_TRIGGERS` and the PG catalogue query (`schema.py:397-402`
  pattern). Add a digest-shape entry for `terminal_hash`. If the event table is
  refused, keep `claim_owner_instance_id` (or a new `started_by_instance_id`)
  on terminal rows and add `settled_by`. The epoch bump is 72, not the 68 the
  contract names (`models.py:59` = 71).
- **T04 (authority).** `ComposerAsyncOperationAuthority` is the only writer of
  both the job table and the events table for admit, claim, release, cancel and
  every `settle_*` path. Each mutation appends its event in the same locked
  transaction. The events `TablePolicy` should have
  `operation_authorities == ()`, pinned by a test mirroring `:12205-12215`.
- **T05/T06 (composites, R2/R3 unchanged).** Follow the receipts precedent:
  `SessionServiceImpl.settle_fork_operation_receipt` runs inside its own locked
  transaction but writes through `settle_operation_receipt(conn, ...)`
  (`service.py:1169-1175`), and the AST gate attributes that write to the
  helper's symbol, `SessionOperationReceiptAuthority`
  (`test_session_db_mutation_authority.py:458-467`). Put the start-to-running
  CAS and the terminal CAS in connection-taking helpers in the compose authority
  module, and call them from `start_composer_async_operation` and
  `complete_/fail_composer_async_operation` inside their existing transactions.
  The transaction boundaries (R2 composite terminal, R3 bound start) stay exactly
  the same, and only the location of the `UPDATE` statement moves. The compose
  table policy then drops from three authorities to one, with no operation
  grants, and the `started` and `completed`/`failed` events are appended by the
  same named authority in the same transaction.
- **Read side (T12).** The poll's re-validation (contract §HTTP: `result_sha256`
  plus the strict DTO) should also verify the terminal event's `terminal_hash`
  against the row. A mismatch raises `AuditIntegrityError`, as
  `_validate_events` does.

## Facts in the brief

Every fact the brief states was verified at `6506f6a7f`:

- `models.py:727` table shape
- the receipt event columns (the brief omits `occurred_at`)
- the helper set and `OperationReceiptTakenOver`
- the caller files, including direct raw reads in `blobs/service.py:487-530,2286-2290`
  that filter on `kind='session_fork'`
- epoch 71 at `models.py:59`

Two corrections concern the plan rather than the brief:

- The T03 insertion anchors are stale.
- The terminal compose row loses `claim_owner_instance_id`, and
  `session_operation_fences` is keyed by `session_id` alone, so the SOL triple
  cannot recover it.
