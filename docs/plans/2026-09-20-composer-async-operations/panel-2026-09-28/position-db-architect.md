# Panel position: database architecture (SQLite + PostgreSQL)

Tree: `release/0.8.1` @ `6506f6a7f`, read-only. Every line reference below was read on that tree.
Scratch probes ran against a throwaway SQLite file in the session scratchpad. No repo file other
than this one was written.

## Position

**B: a separate `composer_async_operations` table.** I have **high** confidence that the job must
not share a table with fork/revert receipts (Option A). **B versus C is outside this lens.** The
codec is a Python function. The database stores only its 64-hex output, and that output is
domain-separated by `kind` in the preimage whichever function computes it. From the physical
design, B and C produce the same schema.

**D** takes two forms:

- As a shared **supertype table** (`session_operations` plus subtype detail tables, or one event
  log shared by both tables), I reject it:
  - Consistency between the parent row's status and the subtype row's status crosses tables, so
    no CHECK can enforce it. That leaves triggers or writer discipline.
  - Every terminal write in the R2 composite would touch two rows.
  - An event log shared by two parent tables cannot have a real FK to either parent.
- As **shared code** (the canonical hashing primitive and the Tier-1 UUID and timestamp
  validators), D is B under another name.

## Why not A: the premise fails when measured

The brief describes A as "reusing its reserve/renew/settle/event machinery". That machinery cannot
host the compose lifecycle unless it is rewritten with kind branches.

1. **There is no queued state to reuse.** `reserve_operation_receipt` inserts `status='in_progress'`
   with a live `lease_token` and `lease_expires_at` in the same statement (operation_receipts.py:294-307).
   The status bundle requires `in_progress ⇒ lease_token IS NOT NULL AND lease_expires_at IS NOT NULL`
   (models.py:794-801). A compose job is admitted `queued` with no claim at all (T03:67-71).
2. **Takeover is exactly what the compose job forbids.** An expired `in_progress` receipt is taken over
   with a new token and `attempt + 1` (operation_receipts.py:318-353, `OperationReceiptTakenOver`). The spec
   says an expired `running` compose row "is never taken over for another provider attempt" (spec §1).
   Reusing `reserve` for compose would put a provider replay path one branch away from the compose row.
3. **The event log cannot represent admission.** The events bundle CHECK (models.py:862-871) allows
   `claimed`/`renewed` only with `lease_expires_at IS NOT NULL`, and `_validate_events` rejects any
   receipt whose first event is not `claimed` (operation_receipts.py:183-184). An unleased queued
   admission, a claim release back to unclaimed, a cancel marker and a start-under-SOL have no event
   kind. Every one would widen `event_kind` and the events bundle.
4. **R3 conflicts with the receipts' liveness clock.** Every receipt writer compare-and-swaps on
   the row's own lease: `renew` (operation_receipts.py:383), `bind` (:433) and `settle` (:507) all
   require `lease_expires_at > now`. A lapsed row lease means the fence is lost. The compose
   `running` row uses a different clock: its liveness is the COMPOSE `SessionOperationLease`
   (contract §Composite start: "Job-lease liveness == SOL liveness"). T03:79 keeps the claim triple
   on `running`, but the claim expiry is not renewed once the SOL has been adopted. A running
   compose row whose claim expiry has lapsed while its SOL is live is legitimate under R3. The
   receipt machinery would fence that row out as lost. Reusing `renew`/`settle` would therefore
   need a kind branch that swaps the clock.
5. **The mutation-authority gate has no `kind` dimension.** `TablePolicy.permits` matches on
   `(authority, operation)` for the whole table (test_session_db_mutation_authority.py:36-46).
   `test_operation_receipt_writers_are_exact_and_fenced` pins receipts' extra authorities to
   exactly `(("SessionForkParentReceiptMutations", {"update"}),)` (:12212). Under A,
   `ComposerAsyncOperationAuthority`, `SessionOperationAuthority` (the start composite) and
   `SessionComposerOperationTerminalAuthority` would all need update, and in one case insert,
   rights on `session_operation_receipts`. The gate would then legally let each of them rewrite
   fork and revert rows. That is a real weakening of an existing integrity pin, not a cosmetic
   re-pin.
6. **Readers already assume that every receipt is fork or revert.** `_validate_identity` raises on any
   other kind (operation_receipts.py:68-69). `_validate_row` raises on any status outside the three
   (:143-144). `decide_and_soft_archive` selects any `in_progress` receipt for the session and raises
   `AuditIntegrityError` if its kind is not fork/revert (coordination/repository.py:722-733). A works
   only if compose uses a disjoint status vocabulary (`queued`/`running`, never `in_progress`), so these
   readers never see compose rows. Two disjoint state machines distinguished by `kind` are two tables
   stored in one. Readers that touch the table directly and would all need auditing: repository.py:722-760,
   service.py:2185-2195, blobs/service.py:487-495, :525-530 and :2285-2293.

### The size of A's CHECK surface (measured)

Live metadata, measured by instrumenting `models.metadata`:

- `session_operation_receipts`: 17 columns, 13 distinct CHECK names (16 objects, because dialect
  pairs use `ddl_if`), and 1 index.
- Events table: 11 columns and 7 distinct CHECK names.
- The plan's separate table has 23 named CHECKs (T03:58). None of them depends on `kind`.

Merged under A:

- **Kind and status cells.** 4 kinds × 5 statuses give 20 (kind, status) cells, of which 14 are
  legal. Fork and revert take `in_progress`/`completed`/`failed`; compose takes
  `queued`/`running`/`completed`/`failed`.
- **The status bundle.** It grows from 3 arms (models.py:794-801) to 7:
  - `in_progress` for fork/revert;
  - `queued` and `running` for compose;
  - `completed` and `failed`, once for each family.
  Each arm must also null out the other family's columns: about 14 compose columns in the fork and
  revert arms, and 4 or 5 fork/revert columns in the compose arms.
- **`attempt`.** Receipts use `attempt >= 1` (models.py:766). The plan uses `attempt >= 0` with
  `claim_token IS NOT NULL ⇒ attempt >= 1`, because `attempt` counts claims taken (T03, the
  `ck_..._attempt` CHECK). The same column would need a kind-conditional CHECK.
- **`failure_code`.** The two closed vocabularies share only `operation_failed` and `request_cancelled`
  out of 9 distinct codes (models.py:780-782; contract D13). The CHECK becomes kind-conditional.
- **Punned columns.** `response_hash` is the hash of a strict DTO that is rebuilt from locators
  (operation_receipts.py:56-62). `result_sha256` is the hash of stored canonical JSON. Putting both
  in one column gives it two meanings. The plan's `user_message_id` (the row the worker inserted)
  and the fork's `originating_message_id` (the fork source) are different facts, and would also be
  tempting to pun.
- **PostgreSQL reflection.** Longer AND/OR arms are fine: the collector compares expression ASTs
  (`core/schema_shape.py:1143-1159`). Kind-gated arms, however, invite one-element
  `kind IN ('compose_message')` fragments, which PostgreSQL reflects as `=`. That is the
  elspeth-d0e62aea41 defect class (test_schema_probe_postgres.py:312-340). A adds more places for it
  to recur.

### What A really saves (the strongest counterargument, answered)

A adds no trigger. The existing `trg_session_operation_receipts_terminal_immutable` predicate
`WHEN OLD.status IN ('completed','failed')` (models.py:1636-1656, :1857-1865) covers compose terminals
word for word. A also adds no `TablePolicy` row, although it widens the existing one. Without punning, it
edits one digest-inventory row (test_digest_column_shape_checks.py:114) instead of adding a new
one. B costs:

- one trigger, in 5 places: the PostgreSQL cohort entry, the SQLite listener,
  `_REQUIRED_AUDIT_TRIGGERS` (schema.py:96-108), the PostgreSQL `validate_required_triggers` arm
  (:380-403) and the doc comment (:60-94);
- the exact-set test pins;
- one TablePolicy row and one digest row.

That is roughly 30-60 lines of declarative, pattern-matched schema. A avoids it by making the
receipts table carry a kind-gated state machine that the whole-tree gates cannot scope. Those gates
work per table, and a separate table is the unit they can prove things about.

## Physical-design findings that apply under B (recommendations for T03, T04 and T06)

1. **Keep forensic identity on terminal rows.** T03's completed and failed arms null the whole claim
   triple (T03:82, :87). After a `worker_lost` settlement, the row then no longer records which
   instance held the job. The SOL quad is kept, but `session_operation_fences` has one row per session
   and is overwritten on the next acquire. Recommendation: on settlement, null only `claim_token`
   and `claim_expires_at`, and keep `claim_owner_instance_id` and `attempt`. Precedent: the fences
   comment "Release never nulls forensic authority" (models.py:321-324). This changes the T03
   bundle's terminal arms and the T04/T06 settle writers.
2. **Make D8 a schema invariant.** D8 already refuses a second nonterminal operation id per session
   under the session lock. T03's comment makes queued siblings legal at the schema layer. Replace
   `uq_..._one_running_per_session (session_id) WHERE status = 'running'` with
   `(session_id) WHERE status IN ('queued','running')`. Precedent: `uq_runs_one_active_per_session`
   uses a two-value IN on both dialects (models.py:2005-2011). The index also answers the F-B2 cancel
   predicate read and the D8 active check with a single-row probe. The partial-index symmetry
   validator requires identical `sqlite_where` and `postgresql_where` (schema.py:583-620).
3. **Make the claimable index partial** on `status IN ('queued','running')`. Terminal rows are kept
   until the session is deleted and become the long-run majority. They never need to be in the claim
   or reaper index.
4. **Drop `ix_composer_async_operations_session_status`** unless T04 names a query that needs it. The
   PK `(session_id, operation_id)` prefix serves session-scoped scans, and (2) serves the nonterminal
   ones. Every status transition otherwise maintains one more index.
5. **SQLite write amplification (measured; applies equally to every option).** With a
   393,334-character `request_json`:
   - 10 `UPDATE`s that change a column's size wrote 4,078,832 B of WAL, about 408 KB each. That is
     the whole record, overflow pages included.
   - Same-size updates wrote about 3.7 KB each.
   - Column order made no difference to either writes or reads: 2000 point reads took 9.0 ms for
     the middle-column order and 8.9 ms for the last-column order.
   - Positive control: rewriting `request_json` itself cost 3,996,432 B.

   A job has about 5-6 size-changing transitions: claim, start, user row, cancel marker, terminal. The
   worst case is therefore about 2.4 MB of WAL per maximal request. Typical requests are a few KB.
   On PostgreSQL the TOAST pointer is carried over unchanged when `request_json` is not updated. **No
   payload side table is warranted.** The bound is finite and the table choice does not change it.
6. **Claim scan and locking.** Spec §3 asks for `FOR UPDATE SKIP LOCKED`. That is superseded by the
   contract deviation (contract.md:22): a discovery read, then one `locked_session_transaction`
   (advisory lock first, then row) per candidate (sessions/locking.py:280-284). That order matches
   every other session writer. A batch `SKIP LOCKED` would take row locks before the advisory lock
   and invert it. There is no cross-kind lock interaction under any option. On a shared table, a
   status-prefixed index scan would never visit fork or revert rows anyway. The D3 cluster count
   takes no global hot row. F-C7's soft cap is the honest price of that.
7. **Cascades and FKs.**
   - The actor FK is RESTRICT to `identities.identity_id`, which is consistent with
     `sessions.user_id` (models.py:187).
   - I measured the composite RESTRICT FK to `chat_messages(id, session_id)` on SQLite only. Deleting
     a session whose receipt row has `originating_message_id` set succeeds with
     `PRAGMA foreign_keys=1` (SQLite 3.47.1). The job's `user_message_id` FK has the same shape.
   - **PostgreSQL is unverified here.** T16 should add: archive-delete (D7) a session holding a
     `running` job with `user_message_id` set, on PostgreSQL.
8. **`result_json` growth.** `MessageWithStateResponse` carries `state` only when the composition
   changed (schemas.py:234-243). Each state-changing turn therefore stores a second copy of the state
   inside an immutable row that is kept until the session is deleted. I have not measured the size.
   T00 should record the p50/p99 `result_json` length from the eval battery before T03 fixes "no
   length bound".
9. **Ingress-receipt overlap** (another panellist owns this). When `client_request_id == operation_id`,
   `job.user_message_id` duplicates `message_ingress_receipts.user_message_id` (models.py:464-488).
   T03 should pin one relationship rather than store both independently.

## Consequences for T02-T06

- **T02:** B keeps one canonical hashing primitive. The compose request schema string and the
  closed compose kind Literal stay separate from `OperationReceiptKind` (protocol.py:264). The
  receipt codec's `kind: OperationReceiptKind` parameter must not be widened to carry compose kinds.
  Parameterise instead: `schema`, plus `kind` from each caller's own closed union.
- **T03:** keep the separate table. Rebase every anchor onto the post-`7001600fe` tree. Apply
  recommendations 1-4, and name the tightened unique index honestly
  (`uq_..._one_active_per_session`). The epoch bump is 72 (T17).
- **T04:** settle writers keep `claim_owner_instance_id` and `attempt`. `claim_next` and
  `list_expired_*` read only through partial indexes. The poll `get` should project without
  `request_json` if T04 keeps a separate poll read.
- **T05:** no change from this lens. The composite start writes the SOL quad onto the job row inside
  the same `_locked_transaction` as the fence advance.
- **T06:** the terminal CAS stays keyed as contracted. The terminal arm keeps the owner id.
- **T16:** add the PostgreSQL archive-delete-with-running-job cascade test (7).

## Facts in the brief or plan that are wrong or stale

- `schema.py:45` is `_COORDINATION_HARD_CUT_EPOCH = 71`, not `SESSION_SCHEMA_EPOCH`. The session epoch is
  `models.py:59`. Both constants equal 71 today, but they are different constants.
- T03 is stale against `7001600fe`:
  - It anchors the new table after `guided_operation_events_table` (T03:22, :689) and edits
    `trg_guided_operation_events_no_delete` (T03:880).
  - `guided_operation` now has 0 occurrences in models.py.
  - `POSTGRESQL_AUDIT_DDL_COHORT` is at models.py:1453, not :1861.
- Contract "Adopted deviations" row "epoch bump … 68 at plan time" is stale: the current epoch is 71,
  so the next free epoch is 72.
- The brief correctly says the receipt row has no actor column. The events table's `actor` column
  records a code-role label (`"composer_route"`, `"composer_route_guard"`,
  routes/operation_receipts.py:36-37), not an identity. It is not a substitute for `actor_user_id`.
- This is not an error in the brief. The lens asks about `FOR UPDATE SKIP LOCKED` behaviour, but
  the contract deviation (contract.md:22) has already replaced spec §3's batch `SKIP LOCKED` claim.
- The brief's receipt caller list mixes two kinds of dependency:
  - `blobs/service.py` and `coordination/repository.py` query the **table** directly and do not use
    the module's helpers.
  - `service.py:169` and `:1083` import the helpers, including the private `_outcome` and
    `_validate_identity`.
