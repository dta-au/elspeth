# Cross-examination: database architecture (SQLite + PostgreSQL)

Tree: `release/0.8.1`, read-only. HEAD has moved from `6506f6a7f` to `a375d7f13` since the brief was
written. `git diff --stat 6506f6a7f a375d7f13` touches only two gateway files
(`gateway/src/elspeth_llm_gateway/core/config.py` and `gateway/tests/test_config.py`), so every
sessions/receipts line cited below is the same on both commits.

## Final position

**B′ (high confidence against A; moderate on the details).** Keep a separate
`composer_async_operations` table. Lift the request normaliser into a neutral function where each
facility passes its own schema tag and its own closed kind union. Use one client-minted identity per
send, and make its link to `message_ingress_receipts` **structural** (see §3). My opening position
said B vs C was "outside the database lens". I now take a side because of two points the panel made:

1. **The epoch cut makes the lift free.** Tech-critic's point is that no stored hash survives a
   hard cut, so there are no stored bytes to preserve.
2. **Two normalisers are a drift risk to the stored audit binding.** Each one decides what counts
   as "the same request", and that decision is what gets stored.

The table, CHECK and gate arguments against A are unchanged. Nothing any panellist put forward
revives A.

## 1. Rebuttals, by position

### python-architect — C

- **Where we agree.** The table analysis is the same as mine, and the physical schema under B and C
  is identical.
- **Where I disagree: the codec.** C leaves two functions, each defining the normalisation stored
  as `request_hash`: strict/forbid, `exclude operation_id`, and `None`/default materialisation
  (operation_receipts.py:39-53).
  - Nothing ties the two definitions together. A later edit to one silently changes what "the same
    request" means for one table only.
  - The objection to lifting is "don't touch day-old receipts". That weakens once you note that
    epoch 72 wipes every stored receipt hash anyway (models.py:59 is 71; any table change is a hard
    cut).
  - So the lift costs one golden-vector test, and it removes a class of drift.
- **Factual correction.** The claim that receipts "only ever does exact-PK lookups" is wrong. Two
  counter-examples:
  - `decide_and_soft_archive` runs a session-prefix status scan (coordination/repository.py:723-731).
  - The same function runs a **cross-session** scan on `kind='session_fork' AND status='in_progress'
    AND result_session_id=?` (repository.py:736-744).

  These two scans are also where A would first cause trouble: they would need kind filters or
  disjoint statuses.
- **Also wrong:** "T03 unaffected under C". T03 anchors on `guided_operation_events_table` and
  `trg_guided_operation_events_no_delete`, which have 0 occurrences in models.py after `7001600fe`.
  T03 needs re-anchoring under every option.

### leverage — B′

- **Accepted: the ingress precondition.** It is verified. `SendMessageRequest.client_request_id: UUID`
  is at schemas.py:154. `message_ingress_receipts` is at models.py:464-488, with PK
  `(session_id, client_request_id)` and unique `user_message_id`. Its immutability triggers are
  `trg_message_ingress_receipts_no_update` and `no_delete` (schema.py:104-105; models.py:1616-1634,
  1847-1853).
- **Rejected: leverage's option (1), which folds ingress into the job row and drops the table.**
  - The job row is mutable by design: claim, release, start, cancel marker and settle are all
    UPDATEs.
  - Ingress is the immutable acceptance record, with no_update and no_delete while its session
    lives.
  - Folding would mean either losing that immutability, or rebuilding it as column-level guards on
    a row that is otherwise mutable.
  - Tech-critic's option (keep ingress, with `operation_id == client_request_id`, and the worker
    writing ingress in the user-row transaction) is the sound shape. §3 gives the FK that makes it
    one fact instead of two.
- **Accepted, merged into one trigger: the transition guard.** There is a precedent for
  column-scoped immutability in this schema: `trg_chat_messages_immutable_content ... BEFORE UPDATE
  OF content` (models.py:1566-1568). Do not add a second trigger to the five-place inventory
  (schema.py:96-108 required set, :380-403 PG arm). Instead, widen the planned
  `trg_composer_async_operations_terminal_immutable` into one transition-guard function that refuses:
  - any UPDATE when `OLD.status IN ('completed','failed')`, which is the existing predicate;
  - `OLD.status='running'` going to anything other than `running`, `completed` or `failed`;
  - any change to `session_id`, `operation_id`, `kind`, `request_hash`, `actor_user_id` or
    `deadline_at`;
  - once `OLD.status='running'`, any change to the SOL triple or to `started_at`;
  - `cancel_requested_at` going from non-NULL to NULL.

  The inventory cost is the same, one name in five places, and it gives more structure. SQLite
  expresses this as a `WHEN` predicate, and PG as one plpgsql function. PG CHECK reflection does
  not apply to triggers, whose inventory is validated by name (schema.py:380-403).
- **Rejected: the writer-manifest pin "only T05 sets status='running'".** `TablePolicy.permits`
  matches on `(authority, operation)` only (test_session_db_mutation_authority.py:36-46). It has no
  value dimension, so this pin needs new gate machinery. The trigger above puts the same invariant
  into the database, where it applies to every writer, including a future one.
- **Accepted, with an index: the positive predicate at service.py:970.** "Any job bound to this
  fence must be `running` with no cancel marker" needs a lookup by fence. Terminal rows must be
  visible to it, so my nonterminal partial index does not serve it. Every compose write would then
  scan the session's whole PK prefix, which grows with turn count. Recommendation: a partial unique
  index `uq_composer_async_operations_fence (session_id, session_operation_epoch) WHERE
  session_operation_epoch IS NOT NULL`.
  - Epochs are strictly monotonic per session: `operation_epoch = row.operation_epoch + 1`
    (coordination/repository.py:4703, :5380). The fence row is one per session and survives release
    (models.py:321-324).
  - So `(session_id, epoch)` is unique per acquire. The index also makes "at most one job per fence"
    a schema invariant.
  - Use identical `sqlite_where` and `postgresql_where` (schema.py:583-620 symmetry check). This is
    an `IS NOT NULL` predicate, not IN, so there is no reflection risk.

### solution-architect — B′

- **Accepted in substance.** Separate table, neutral codec with per-facility tags and a golden
  vector, identity unified before T02, epoch 72, and the archive staying receipts-only (D7).
- **One refinement.** SA says "exactly one of `job.user_message_id` or the ingress receipt is kept
  as the authority".
  - Dropping the job column is not clean. A recompose job inserts no user row. Its target, the
    `expected_user_message_id` (schemas.py:162-165), lives only in `request_json`, which is cleared
    at settlement (contract.md:190-195, completed/failed arms).
  - Keep the column. Make ingress the authority for sends through the FK in §3, so the two can never
    disagree. That is one fact enforced by the database, not two stored independently.
- **Also accepted: SA's test "a queued compose job does not block fork/revert".** It is the reason
  I reject tech-critic's implied single-index exclusion below.

### tech-critic — B′

- **Accepted:** the codec cannot be built as the option was worded (verified at
  operation_receipts.py:36-46: fixed tag, `kind: OperationReceiptKind`, required `operation_id`,
  strict/forbid). Also accepted: ingress stays the immutable record, and the worker-side ingress
  conflict becomes Tier-1.
- **Rebutted: T05 head-change.** Tech-critic says "with two tables no single index can prevent it"
  (revert/fork between admission and start). The premise is true, but the implied advantage of A is
  not one anyone wants.
  - A cross-kind `one active per session` partial index in a shared table would make a queued
    compose job reject every fork/revert admission until the job starts or reaches its deadline.
  - That contradicts the no-blocking intent (SA's T04 test), and it moves run-time mutual exclusion
    from the SOL (models.py:320-352) into admission.
  - The right answer is what tech-critic offers as its alternative: the start composite re-verifies
    the head, or records the change, under the COMPOSE SOL.
- **Rebutted: "A could add a nullable `result_json`".** True, but it adds a fifth kind-conditional
  arm to the status bundle. It does not rescue A.

### audit-integrity — B

- **Accepted, and it changes my recommendations: the delete gap.**
  - Receipt rows cannot be deleted while their session lives. The protection is indirect: the
    events FK cascades (models.py:838-843), and the events `no_delete` trigger raises while
    `EXISTS (SELECT 1 FROM sessions WHERE id = OLD.session_id)` (models.py:1675-1690).
  - The planned job table has only an UPDATE trigger (contract.md:199-200).
  - Fix: add a `no_delete`-while-session-lives trigger, the exact ingress/chat pattern
    (models.py:1581-1586, 1847-1853). It could not be folded into the UPDATE guard: SQLite triggers
    are per event and a DELETE trigger has no `NEW`. This is the only new trigger name I recommend.
  - Under §3, send jobs also get the receipts-style cascade protection through the ingress FK.
- **Accepted: the forensic retention.** Keep `claim_owner_instance_id` and `attempt` on terminal
  rows. This was my point 1 as well. Add a closed `settled_by` column, which is the minimal fallback
  audit-integrity names.
- **Accepted: collapsing writers to one authority with connection-taking helpers.** The precedent
  is verified: `settle_operation_receipt(conn, …)` is called inside the service's own locked
  transaction (service.py:1168-1172). This tightens the TablePolicy from three authorities to one,
  the same shape as the receipts events pin (test_session_db_mutation_authority.py:12211). From a
  gate standpoint it is strictly better.
- **Not adopted as a requirement: a `composer_async_operation_events` table.**
  - The retained owner id and attempt, the SOL quad, `started_at`, `settled_by`, `failure_code` and
    `settled_at` record the terminal custody on an immutable row.
  - What an events table adds is release and reclaim history, which SRE and tech-critic both class
    as transport telemetry. The turn's audit lives in `chat_messages` and the LLM-call cohort.
  - If John rules it is evidence, the physical design is cheap. Use a composite FK on
    `(session_id, operation_id, request_hash)`, which needs a unique key as at models.py:748. Add the
    trigger pair, and do not event renewals: per-renewal events are the write amplification to
    avoid.

### sre — B

- **Partly rebutted: indexes.**
  - SRE wants to keep `(session_id, status)`. My partial unique
    `(session_id) WHERE status IN ('queued','running')` answers both D8 (contract.md:78: "refuses a
    NEW operation id while the session has a nonterminal job") and the operator's "what is live"
    question with a single-row probe.
  - Per-session history reads go through the PK prefix `(session_id, operation_id)`.
  - A full `(session_id, status)` index is kept in step on every status transition and serves
    nothing extra.
- **Accepted: `(status, deadline_at)`.** `list_expired_queued` filters on `deadline_at`, which
  `ix_..._claimable (status, claim_expires_at, created_at)` (contract.md:197) cannot seek on. Make it
  partial on nonterminal statuses, so terminal rows (the long-run majority) stay out of it.
- **Corrected fact.** SRE says `ix_session_operation_receipts_status_lease` (models.py:819-823) has
  "no reaper or scan that uses it". No reaper uses it. But the cross-session archive query at
  repository.py:736-744 filters on `status='in_progress'` without `session_id`, and that is exactly
  the query the status prefix serves. The index is not dead.
- **Accepted:** explicit LIMITs on scans, an operator stuck-turn query, and the same epoch-reset
  procedure under every option.

### systems-thinker — B

This agrees with me. One point needs adjusting. The "negative test asserting no path moves running
back" should be a **database** guard, the trigger above, with a negative-control test. A Python-only
test can prove only the paths it knows about.

### codex — B (shared domain-separated codec)

This agrees with the refined position. Codex's fallback is sound: if the golden vector cannot prove
receipt hashes are unchanged, use C rather than change receipt identity. It is moot after the epoch
cut, but it is the right discipline.

## 2. Points that genuinely changed my mind

1. **The ingress identity (leverage, SA, tech-critic, SRE).** My opening mentioned it only as
   "another panellist owns this". It is a schema question in its own right: two client-minted keys
   on one body is dual acceptance, and there is a structural fix (§3).
2. **The delete gap (audit-integrity).** It is verified, and a precedent trigger closes it cheaply.
3. **B vs C.** I moved from "out of lens" to preferring the lift, once the epoch cut is taken into
   account.
4. **Missing indexes.** `(status, deadline_at)` (SRE) and the fence-lookup index (a consequence of
   leverage's positive predicate) were both missing from my index plan.

## 3. The ingress relationship: the concrete shape (if John rules `operation_id == client_request_id`)

- **Keep `message_ingress_receipts` immutable and worker-written** in the user-row transaction.
- **Add a composite FK from ingress to the job:**
  `(session_id, client_request_id, user_message_id) → composer_async_operations(session_id,
  operation_id, user_message_id)`, `ON DELETE CASCADE`.
  - This needs a unique key `uq_composer_async_operations_user_message (session_id, operation_id,
    user_message_id)`. The precedent is the receipts' `(session_id, operation_id, request_hash)`
    unique key, which is the target of the events FK (models.py:748, :838-843).
  - The job's `user_message_id` and the ingress row are written in the same transaction, so the FK
    is satisfied when the ingress row is inserted.
- **What the FK buys:**
  - Every send's ingress row must have a job with the same id and the same user message.
  - Once ingress exists, `job.user_message_id` cannot change. An UPDATE would violate the FK:
    PostgreSQL's default is NO ACTION, and SQLite enforces it with `foreign_keys=1`.
  - Deleting the job cascades into ingress, whose `no_delete` trigger then refuses while the
    session lives. Send jobs therefore inherit the receipts-style delete protection.
- **Recompose jobs** have no ingress row and are unconstrained by this FK. The job-table `no_delete`
  trigger covers them.
- **Type width.** `client_request_id` is an unbounded `String`, and the plan's `operation_id` is
  `String(36)`. Align them in the same epoch: bound ingress to 36, the canonical UUID length the
  DTO already enforces.
- **What to delete at cutover.** The 409 `message_already_accepted` path and its lookup
  (service.py:4517-4519) go, so there is no dual acceptance. The transcript join at
  service.py:4759-4764 stays valid unchanged.
- **Test to add.** A PG testcontainer test (T16) should archive-delete (D7) a session that has a
  running job, `user_message_id` set, and an ingress row. It must succeed on both dialects. I
  measured the RESTRICT composite FK only on SQLite.

## 4. Consolidated T02–T06 consequences (database lens)

- **T02:** add a neutral `session_operation_request_hash(*, schema, session_id, kind: str, request)`.
  - The receipt wrapper keeps its tag and a golden vector. The compose wrapper uses
    `composer-operation-request.v1`.
  - Do not widen `OperationReceiptKind` (protocol.py:264).
  - Rebase the DTOs onto `_SessionOperationRequest` (schemas.py:72).
- **T03:**
  - Separate table, re-anchored after `session_operation_receipt_events_table` (models.py:825-873).
  - Terminal arms keep `claim_owner_instance_id` and `attempt`, and add a closed `settled_by`.
  - Indexes:
    - `uq_..._one_active_per_session (session_id) WHERE status IN ('queued','running')`;
    - `ix_..._claimable`, made partial on nonterminal statuses;
    - `ix_..._expiry (status, deadline_at)`, partial on nonterminal statuses;
    - `uq_..._fence (session_id, session_operation_epoch) WHERE session_operation_epoch IS NOT
      NULL`;
    - the `(session_id, operation_id, user_message_id)` unique key for the ingress FK;
    - drop `ix_..._session_status`.
  - Triggers: the terminal-immutable trigger widened into one transition guard, plus a
    `no_delete`-while-session-lives trigger. That is 2 names, each in 5 places, and the required set
    goes from 11 to 13.
  - Ingress FK and width change as in §3. Epoch 72.
- **T04:**
  - One authority, `ComposerAsyncOperationAuthority`, owns every write, including connection-taking
    start and terminal CAS helpers. `TablePolicy` has `operation_authorities == ()`.
  - The worker-side ingress conflict is Tier-1.
  - Scans carry a LIMIT and read through the partial indexes.
  - A poll read projects without `request_json`.
- **T05:** the start composite writes the SOL quad under the COMPOSE SOL. The fence index makes a
  second binding to the same fence a constraint violation. Re-verify or record the head at start.
  There is no cross-kind admission exclusion.
- **T06:** the terminal CAS goes through the authority's connection-taking helper, in the R2
  transaction. The positive fence predicate reads through `uq_..._fence`, with a negative control
  that writes under a lingering SOL after the terminal CAS.
- **T16:** add the PG archive-delete-with-running-job-and-ingress test, plus trigger negative
  controls: running→queued, a change to the identity columns, and deleting a job while its session
  lives.
