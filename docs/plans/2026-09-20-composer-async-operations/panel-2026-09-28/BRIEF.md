# Design panel brief — where does the composer async operation live?

Tree: `release/0.8.1` @ `6506f6a7f` (2026-09-28), main checkout, read-only for panellists.
Question owner: John. The panel advises; it does not decide.

## The question

The composer async-operations change (spec `docs/specs/2026-09-16-composer-async-operations-design.md`,
plan `docs/plans/2026-09-20-composer-async-operations.md` + folder) needs a durable, per-session,
client-minted-id job row for `POST /messages` and `POST /recompose`. Guided mode has now been removed
(`7001600fe`), and a mode-neutral facility `session_operation_receipts` has replaced `guided_operations`.
**Should the async job be stored in `session_operation_receipts`, or in its own table?**

## Options (the panel may add or merge options)

- **A. Extend receipts.** Add `compose_message` / `compose_recompose` kinds to `session_operation_receipts`
  and grow it (queued status, claim vs running, request JSON, full result JSON, cancel marker, actor,
  deadline, SOL-fence binding columns), reusing its reserve/renew/settle/event machinery.
- **B. Separate table, shared codec.** Keep a dedicated `composer_async_operations` table (shape in the plan
  contract, `contract.md` § Table and the Review-pass decisions) but reuse `operation_receipt_request_hash`
  (and possibly other receipt helpers) instead of a duplicate codec.
- **C. Separate table, own codec.** As the plan was written on 09-25 (`composer_operation_request_hash`,
  a byte-for-byte sibling of the receipt codec).
- **D. Generalise.** Extract a shared "client-minted session operation" core (identity, request hash,
  terminal replay, event log) used by both receipts and a compose-job extension/table.

## Measured facts (verify before relying on them)

- `session_operation_receipts` (`src/elspeth/web/sessions/models.py` ~:727): PK `(session_id, operation_id)`;
  `kind IN ('session_fork','state_revert')`; `status IN ('in_progress','completed','failed')`; one `lease_token`
  / `lease_expires_at` / `attempt`; result **locators** (`originating_message_id`, `result_state_id`,
  `result_session_id`) + `response_hash`; `failure_code`, `failure_diagnostics` JSON; no request JSON, no full
  result JSON, no cancel marker, no actor column on the row, no queued state.
- Companion append-only `session_operation_receipt_events_table` (sequence, event_kind, actor, attempt,
  prior_attempt, lease_expires_at, request_hash, terminal_hash).
- `src/elspeth/web/sessions/operation_receipts.py` (module docstring: "Mode-neutral immutable request and
  response bindings for fork/revert"): `operation_receipt_request_hash`, `operation_receipt_response_hash`,
  `reserve_operation_receipt`, `renew_operation_receipt`, `bind_operation_receipt`, `settle_operation_receipt`,
  `require_live_operation_receipt`, `read_operation_receipt`; the outcome union includes `OperationReceiptTakenOver`
  (an expired in-progress receipt is taken over with a new attempt). Callers in `service.py`,
  `coordination/repository.py`, `blobs/service.py`, routes `sessions.py`, `composer/state.py`,
  `routes/operation_receipts.py`. Introduced by `7001600fe`.
- The spec's own reasons the compose job is different (spec §1, §3, §4): the job must store the **full**
  public `MessageWithStateResponse` (its `proposals` are read after the final commit and `live_validation` is
  never persisted, so a locator cannot rebuild it); it has a **queued** phase admitted before any lease, a
  claim that may be reclaimed while queued but a `running` row that is **never taken over** (no provider
  replay); a cancel marker that races completion; an absolute deadline; bounded request JSON cleared on
  settlement; admission capacity counting; and a `running` fence bound to the session's COMPOSE
  `SessionOperationLease` (ruling R3) with the terminal committed in the same transaction as the final
  assistant publication (ruling R2).
- Session schema epoch is 71 (`models.py:59`, `schema.py:45`); any table change is a hard epoch cut with a
  session-store reset (pre-release; the operator has approved such resets before).
- The first-run tutorial's Build now goes through `sessionStore.sendMessage` → `POST /messages`
  (`TutorialFreeformShell.tsx:112`), so this job table is also the tutorial path.
- Whole-tree gates that care about a new or widened table: `tests/unit/architecture/test_session_db_mutation_authority.py`
  (exact TablePolicy set, named authorities, AST-fingerprinted writer manifest),
  `tests/unit/architecture/test_digest_column_shape_checks.py`, PostgreSQL CHECK reflection
  (`tests/testcontainer/web/test_schema_probe_postgres.py`; per-status bundles must be AND/OR arms, no
  one-element IN), the terminal-immutability trigger inventory (5 places).

## Owner doctrine the answer must respect (from John)

- No tech debt, no old pathways, no dual acceptance: "make upgrades without thinking about old pathways".
- Removing debt is not the same as deleting unfinished intent.
- Audit grade is a product characteristic (ADR-046); audit evidence must never be lost or rebuilt.
- Pre-release, one developer becoming several: supportability and clear ownership matter.
- Composer invariants: the LLM authors structure; no tutorial-special path.

## What each panellist returns

A position (A/B/C/D or a new option) with confidence, the three strongest reasons, the strongest argument
against your own position, concrete consequences for the plan's tasks T02–T06 (schema, codec, authority,
composite start/terminal), and any fact above you found to be wrong.
