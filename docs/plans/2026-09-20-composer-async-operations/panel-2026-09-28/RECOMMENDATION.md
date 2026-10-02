# Panel recommendation — where the composer async operation lives

Chair's synthesis for John. The panel advises; John decides.
Tree verified at `a375d7f13` (`release/0.8.1`, 2026-09-28). The brief pinned `6506f6a7f`; the only
difference is a gateway timeout change unrelated to this question.
Inputs: `BRIEF.md`, nine `position-*.md` and nine `cross-*.md` in this folder.

## Recommendation: B′ — a separate table, a shared request normaliser, one client id per send

1. **A separate `composer_async_operations` table** with its own authority,
   `ComposerAsyncOperationAuthority`, and its own `TablePolicy`. `session_operation_receipts`, its
   events, its authority and its writer pin do not change.
2. **One shared request normaliser in a neutral module.** It keeps the strict+forbid check, the
   `operation_id` exclusion, the default and `None` materialisation, and the schema-tagged `stable_hash`.
   It takes a closed `Literal` of two schema tags: `session-operation-receipt-request.v1` and
   `composer-operation-request.v1`. Each facility keeps a typed wrapper over its own closed kind union.
   Do **not** widen `OperationReceiptKind`. The strict-DTO response hash is shared the same way.
   A golden-vector test must prove that receipt hashes stay byte-identical. **If that cannot be proved,
   ship C** (a separate composer codec) rather than change the identity of durable receipts.
3. **One client-minted id per send.** This is an **owner ruling needed before T02**. The panel
   recommends that the job's `operation_id` *is* the `message_ingress_receipts` key:
   - Ingress stays the immutable acceptance record. The worker writes it in the same transaction as the
     user row.
   - A composite FK **from ingress to the job** makes the two agree by schema, not by review.
   - The route's 409 ingress arms and the SPA's transcript-matching recovery are deleted at cutover.
     There is no alias and no dual acceptance.
4. **Schema amendments that make B safe in practice, not only by design.** These are listed per task
   below:
   - a transition-guard trigger;
   - no live claim expiry on `running` rows;
   - D8 enforced as a partial unique index;
   - terminal rows keep their forensic columns (`claim_owner_instance_id`, `attempt`, and a new
     closed `settled_by`);
   - a delete guard while the session exists.

**Confidence:**

- **High** that the job belongs in its own table.
- **Moderate** on B′ over C. This depends on the golden vector.
- **Owner-dependent** on the identity shape.

## Vote tally

There are nine seats. Codex counts: the relayed "codex: Forwarded" was the courier subagent's status,
not a position. `position-codex.md` is a full B argument (high confidence), and `cross-codex.md` is B′
(high confidence). Leverage and SRE both flagged this.

| Seat | Opening | Final |
|---|---|---|
| systems-thinker | B (moderate) | B′ |
| leverage | B′ (moderate) | B′ |
| solution-architect | B′ (high) | B′ amended |
| tech-critic | B′ (moderate) | B′ (high) |
| db-architect | B (high) | B′ |
| python-architect | **C** (high) | B′ |
| audit-integrity | B (high) | B′ |
| sre | B (moderate) | B′ |
| codex | B (high) | B′ (high) |
| **Total** | **A 0 · B 5 · B′ 3 · C 1 · D 0** | **B′ 9 · others 0** |

The five B openers were B in spirit only. Every panellist who checked found that B as the brief words
it ("reuse `operation_receipt_request_hash`") cannot be built as written. The function fixes four
things:

- the schema tag (`operation_receipts.py:36,53`);
- the kind type, a two-value `Literal` (`protocol.py:264`);
- the presence of a field literally named `operation_id` (`:44`);
- a strict/forbid DTO (`:42`).

Today's `SendMessageRequest` has none of these: it is the coercing `_RequestModel` carrying
`client_request_id` (`schemas.py:143-155`). So the final agreement is on substance, not just on a
label.

## Decisive reasons

1. **Option A would put the running-row safety invariant behind a kind branch that works against the
   facility's default.** `reserve_operation_receipt` unconditionally takes over any expired
   `in_progress` row (new token, `attempt + 1`, a `taken_over` event, `OperationReceiptTakenOver`;
   `operation_receipts.py:316-355`). A compose `running` row must never be taken over, because a
   replay would mean a second provider turn and a duplicated `llm_calls` cohort. Under A, the only
   guard would be a kind check someone has to remember. Receipts also insert rows `in_progress` with a
   live lease on first write (`:294-307`), and their CHECK requires a lease on every in-progress row
   (`models.py:794-801`). The compose job's unleased `queued` phase is therefore illegal in that table.
   Nobody argued for A in either round.
2. **Option A breaks existing readers and the authority gate on the first day.**
   - `decide_and_soft_archive` selects any `in_progress` receipt without a kind filter. It raises a
     Tier-1 `AuditIntegrityError` for an unknown kind, or refuses the archive
     (`coordination/repository.py:723-735`). Either outcome contradicts D7, "no archive refusal"
     (`contract.md:77`).
   - `_validate_identity` rejects any kind other than fork or revert (`operation_receipts.py:68`), and
     `_outcome` decodes every completed non-fork row as a revert (`:228-233`).
   - `TablePolicy.permits` matches only authority and operation. It has no concept of kind
     (`test_session_db_mutation_authority.py:36-44`). Every compose writer would gain write access to
     fork/revert rows, and the exact receipts pin (`:12200-12216`) would have to widen.
3. **Receipt reservation needs a lease that a queued compose job does not have yet.** Receipts are
   reserved under a live session operation lease (SOL) (`routes/operation_receipts.py:239-248`;
   `service.py:1063-1065`). A compose job is admitted **before** any fence exists (spec §3). Option A
   therefore also needs a second, unfenced insert path into the receipts table.
4. **Option D is premature.** Three client-minted session identities now exist:
   - receipts take the operation over and resume it;
   - message ingress rejects a duplicate with 409;
   - the compose job replays its stored terminal result and never resumes a running turn.

   These differ in their core branches. Receipts rebuild the response from locators, then check its
   hash (`routes/operation_receipts.py:95-104`). Compose must store the full DTO, because
   `live_validation` is never persisted (`routes/_helpers.py:878-920`). A shared core would be a base
   class whose subclasses override everything that matters. What genuinely is shared, the request
   normalisation, is exactly what B′ shares.
5. **B′ is better than C because "same request" is a Tier-1 binding, and one definition of it should
   not drift.** C's recorded rationale cites `guided_operation_request_hash`, which now has 0 hits in
   `src/`. The live, mode-neutral `operation_receipt_request_hash` (`operation_receipts.py:39-53`) is
   what C would copy. The epoch-72 reset wipes every stored receipt hash, so the extraction costs one
   golden-vector test.
6. **Identity is the highest-leverage point, and it lies outside options A to D.**
   `SendMessageRequest` already requires `client_request_id: UUID` (`schemas.py:154`). It is bound
   immutably through `message_ingress_receipts` (`models.py:464-488`; epoch 69, `8630db9b8` of
   2026-09-27). The route checks it and answers 409 (`routes/messages.py:116,175,242`). The SPA
   recovers by matching it against the transcript (`sessionStore.ts:676-803`). The plan as written adds
   `operation_id` as a second client key on the same body, which is the dual acceptance the owner
   forbids. Neither the plan folder nor the spec mentions either identifier (grep: 0 hits).

## Conflicts resolved by the chair

These were checked against the tree, not taken from the panellists' reports.

| Conflict | Positions | Resolution and evidence |
|---|---|---|
| How ingress and the job agree | SA: drop `job.user_message_id`. Audit-integrity: FK from the job to ingress. Tech-critic: keep both and re-check agreement on poll. DB-architect: FK from ingress to the job. | **DB-architect's direction.** The only ingress writer is `service.py:1396` (`_insert_message_ingress_receipt`, on the send path). `routes/composer/compose.py` (recompose) has 0 hits for `client_request_id` or `message_ingress`; the positive control is `routes/messages.py:117,175,242`. So recompose jobs have `user_message_id` set and no ingress row. Dropping the column loses recompose's target. A job→ingress FK would fail on every recompose row. An ingress→job FK on `(session_id, client_request_id, user_message_id) → (session_id, operation_id, user_message_id)` works for both kinds: a send's ingress must match its job, and `job.user_message_id` is frozen once ingress references it. |
| Does a running row have "no lease column"? | Systems-thinker and leverage (opening): yes, so the invariant is structural. Tech-critic, SRE, codex: no. | **Tech-critic, SRE and codex are right.** The running bundle requires the claim triple NOT NULL (`T03.md:838-840`), and start keeps it ("The claim triple is KEPT", `T03.md:79`). `renew_claim` covers queued rows only (`contract.md:228-230`), so a running row carries a stale `claim_expires_at`. The reclaim predicate `claim_token IS NULL OR claim_expires_at <= now` stays off running rows only because of its `status == 'queued'` term (`T04.md:2281-2282`). That makes the invariant a rule, not a structure, until the T03/T04 amendments below land. |
| Can the compose TablePolicy narrow to one authority? | Audit-integrity and leverage: yes, through connection-taking helpers. Python-architect: attribution follows the calling symbol, so it doesn't collapse automatically. | **Yes, if the SQL lives in the helpers.** The gate attributes a write to the symbol that contains the SQL. `settle_operation_receipt` in `operation_receipts.py` is pinned to `SessionOperationReceiptAuthority` (`test_session_db_mutation_authority.py:458-467`), and exactly 6 writer sites exist there (`:12200-12205`). The wrapper `settle_fork_operation_receipt` writes nothing itself (`service.py:1160-1175`). Python-architect's caution holds only as "this is a T05/T06 placement requirement", so the chair states it that way. |
| Events table vs forensic columns on the row | Audit-integrity (opening): a mandatory events table. SRE, tech-critic, db-architect: telemetry or ceremony. | **Forensic columns on the row; an events table only if John makes a ruling under ADR-046.** Audit-integrity withdrew the mandatory events table. The underlying loss is real: T03 nulls the claim triple at terminal (`T03.md:82,87`; CHECK arms `:841-847`), and `session_operation_fences` is keyed by `session_id` alone, with the comment "Release never nulls forensic authority" (`models.py:320-332`). Once the next operation overwrites the fence, nothing records which instance ran or reaped a `worker_lost` turn. |
| D8 index | Plan: unique on `running`. SRE: `(session_id, status)`. DB-architect: partial unique on queued+running. | **DB-architect's index, adopted by all.** It enforces D8/R10 exactly as the owner wrote it ("a nonterminal job", `contract.md:78`). It is not a new rule. The predicate is a two-element `IN`, which is safe under PG reflection; the precedent is `uq_runs_one_active_per_session` (`models.py:2005-2011`). |
| Leverage's identity option (1): fold ingress into the job row | Leverage (opening) offered options (1) and (2) neutrally, then chose (2) in cross-examination. Five others rejected (1). | **Rejected.** Ingress is immutable, with `no_update`/`no_delete` in `_REQUIRED_AUDIT_TRIGGERS` (`schema.py:104-105`). The job row is mutable by design. The transcript read path joins `client_request_id` onto every chat row (`service.py:4759-4764`). |

## Dissent, stated fairly

- **Python-architect (opening C), and tech-critic's own counter-case.** Extracting a shared normaliser
  churns receipts code that landed the same day (`7001600fe`, 2026-09-28). Spec §2 literally says the
  codec is "the model to follow, not a function to share" (`spec:109-110`). Two 12-line codecs, each
  pinned by a golden-vector test, keep their drift bounded and couple nothing across facilities. If
  John values "don't touch day-old receipts" above the drift hazard, C is acceptable, provided it is
  called C and not B. **Panel answer:** the golden-vector test is the gate for both options. Under B′
  it also proves the receipt bytes did not change. If the proof fails, fall back to C (codex's rule,
  adopted by SRE, db-architect and audit-integrity). Audit-integrity withdrew its argument that codec
  drift is an *integrity* risk: the distinct tags prevent any collision between families. The case for
  sharing is maintainability.
- **Audit-integrity (opening): a compose events table is required under ADR-046.** Claim, release and
  reclaim history is not recorded anywhere, and receipts have an append-only log that compose lacks.
  **Panel answer:** queued claims have no side effects, so their churn is transport telemetry. The
  turn's audit record lives in `chat_messages` and `llm_calls` (F-B2/C2 persists these even on cancel).
  Forensic columns on the terminal row answer every forensic question anyone raised. If John rules that
  claim history is product evidence, add `composer_async_operation_events` under B. That is not an
  argument for A.
- **Steelman for A/D (SRE, audit-integrity, db-architect "against itself").** One facility means one
  operator query, one trigger, one reviewed Tier-1 validator, and about 30-60 fewer lines of schema
  DDL. A second facility can drift, and the plan has already drifted: no delete guard, no forensic
  retention. **Panel answer:** B′ closes those drifts directly. The price of A is a kind-blind authority
  union plus a takeover path sitting beside a row that must never be replayed.
- **Python-architect, procedural:** the positive fence predicate amends F-B2/C2, which is a review-pass
  decision marked "override everything". The panel agrees, so it is listed below as an owner sign-off
  item and not as an adopted consequence.

## Owner decisions this recommendation needs

1. **Identity (precondition to T02).** All 9 seats agree it is a precondition to T02. 7 of 9 explicitly recommend
   option (2) (leverage, solution-architect, tech-critic, db-architect, audit-integrity, sre, codex);
   systems-thinker and python-architect confirm the fact but leave the shape to John:
   - `operation_id` equals the ingress key;
   - ingress is kept immutable and worker-written;
   - an ingress conflict found by the worker is Tier-1;
   - the `message_already_accepted` / `message_idempotency_conflict` 409s (`routes/messages.py:116`) and
     the SPA transcript-matching recovery (`sessionStore.ts:676-803`) are deleted at cutover.

   The owner must also choose one wire name: codex, tech-critic and SA recommend `operation_id`, with
   ingress's physical key renamed in the epoch cut. This ruling forces two alignments. The DTO moves
   from coercing `_RequestModel` with `client_request_id: UUID` to `_SessionOperationRequest` (strict,
   `operation_id: str`, length 36; `schemas.py:72-88`). Ingress `client_request_id` goes from an
   unbounded `String` to 36 characters. The ruling also forces the ACA P1 probe re-scope (see T13/T15).
2. **Positive fence predicate at `service.py:970`** (leverage; endorsed by audit-integrity, SA,
   tech-critic and db-architect). It amends F-B2/C2. Today
   `_require_session_operation_context_on_connection` checks only `released_at IS NULL AND
   lease_expires_at > now` (`service.py:970-996`). D12 allows the SOL close to fail *after* the terminal
   CAS (`contract.md:82`), so the negative F-B2/C2 predicate (`contract.md:46`) still admits non-audit
   writes after settlement. The proposal: if any job row is bound to this fence triple, it must be
   `running` with `cancel_requested_at IS NULL`, unless the write is `audit_only`.
   Precondition (tech-critic): T06 first measures every legitimate write under the same SOL after the
   terminal CAS.
3. **Compose events table** (ADR-046 custody ruling). The default is no; forensic columns on the row
   instead.
4. **Retention of settled rows** (SRE). The delete guard makes settled rows undeletable while their
   session lives, and they carry the largest `result_json` in the session DB. John should rule on
   retention rather than let a trigger decide it by default.
5. **A head change between admission and start** (tech-critic; adjacent to D9). A fork or revert
   through COMPOSE SOL can move the head while a send is queued. Does the start composite refuse, or
   record the change? No table choice prevents this, because exclusion lives in the SOL.

## Consequences for plan tasks

**Before T02: re-baseline the plan.**

- The epoch cut is **72**, not 68 (`contract.md:39`).
- The guided anchors and symbols are gone; see Fact corrections.
- The identity ruling (owner decision 1) must be made first.

**T02: owned types and codecs.**

- Extract a neutral `session_operation_request_hash(*, schema: Literal[<two tags>], session_id, kind:
  str, request)` from `operation_receipts.py:39-53`, plus the strict-DTO response hash (`:56-62`).
- `operation_receipt_request_hash` delegates to it, keeping its tag and its `OperationReceiptKind` type.
  `composer_operation_request_hash` passes `composer-operation-request.v1` and `ComposerOperationKind`.
- Add golden-vector tests for both families. Receipt bytes must be unchanged; if not, fall back to C.
- Rebase `SendMessageRequest` and `RecomposeRequest` on `_SessionOperationRequest` (`schemas.py:72`),
  not `_GuidedOperationRequest` (`contract.md:158,161`).
- `RecomposeRequest` already exists with `expected_user_message_id` (`schemas.py:162-165`). Change it;
  do not create it. Put `expected_user_message_id` and `state_id` in the hash.
- Pin one result-hash definition: re-validate the text with the strict model, then apply the shared DTO
  hash.
- Delete the imports of the deleted guided codec and its test (`T02.md:58,225,471,546`).
- Re-measure `COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH`.

**T03: table, CHECKs, indexes, triggers, TablePolicy.**

- Keep the dedicated table. Re-anchor it after `session_operation_receipt_events_table`
  (`models.py:825-873`); the `guided_operation_events` anchors (`T03.md:22,689,880`) no longer exist.
- **Running arm:** keep `claim_token` and `claim_owner_instance_id`, but make `claim_expires_at` NULL.
  The start composite nulls it. Amend the "claim triple set or cleared as a unit" rule.
- **Terminal arms:** null only `claim_token` and `claim_expires_at`. Keep `claim_owner_instance_id` and
  `attempt`, and add a closed `settled_by` column. The values name the settle paths: owner terminal,
  `settle_unstarted`, `settle_lost`, `settle_own_lapsed`, `settle_lost_inactive_session`,
  `request_cancel`.
- **Transition-guard trigger.** Widen `trg_composer_async_operations_terminal_immutable` into one UPDATE
  guard on both dialects. It forbids:
  - `running → queued`;
  - any change to `kind`, `request_hash`, `actor_user_id`, the claim owner or the SOL triple on a
    running row;
  - clearing `cancel_requested_at`;
  - changing `user_message_id` once it is NOT NULL (NULL→value is allowed while running). For sends the
    ingress FK also pins it; recompose rows have no ingress row, so only this trigger arm pins theirs;
  - any update to a terminal row.
- **Delete guard.** Add a `BEFORE DELETE` trigger that fires while the session exists (pattern
  `models.py:1676-1692`). The D7 cascade still passes.
- Both new triggers go in all 5 trigger places and in `_REQUIRED_AUDIT_TRIGGERS` (which grows from 11 to
  13: the widened guard keeps the planned name `trg_composer_async_operations_terminal_immutable`, which
  is not among today's 11, and the delete guard adds a second name; `schema.py:96-112`).
- **Indexes:**
  - replace `uq_..._one_running_per_session` (`contract.md:199`) with a partial unique `(session_id)
    WHERE status IN ('queued','running')`;
  - make the claimable index partial on nonterminal rows;
  - add a partial unique `(session_id, session_operation_epoch)` to serve the fence-predicate lookup;
  - drop `ix_..._session_status` unless T04 names a query that needs it;
  - add `(status, deadline_at)` only if `list_expired_queued` cannot use the others. SRE withdrew this
    request; db-architect accepted it; T04 should measure.
- **Ingress (subject to owner decision 1):**
  - add `UNIQUE(session_id, operation_id, user_message_id)` on the job;
  - add a composite FK from `message_ingress_receipts(session_id, client_request_id, user_message_id)`
    to the job;
  - bound ingress `client_request_id` to 36 characters.
- **Table comment** must state:
  - a compose job never blocks archive (D7);
  - it is not durable history in `decide_and_soft_archive` (`repository.py:747-770`);
  - its retention class;
  - "Job-lease liveness == SOL liveness".
- **TablePolicy:** `TablePolicy("composer_async_operations", "session",
  "ComposerAsyncOperationAuthority")` with `operation_authorities == ()`. It narrows from three
  authorities (`contract.md:202-203`) because of the T05/T06 helper placement. The receipts pin
  (`test_session_db_mutation_authority.py:12200-12216`) stays untouched.

**T04: authority.**

- `ComposerAsyncOperationAuthority` is the only writer of the job table and never calls
  `reserve_operation_receipt`.
- `claim_next` and `release_claim` keep `status='queued'` in every CAS `WHERE`.
- `admit` maps the IntegrityError from the D8 index to `ComposerOperationActiveError` (the D8 409).
  A unique-index violation carries no row data, so on IntegrityError `admit` re-reads the session's
  nonterminal row to fill the 409 body's `operation_id` and `kind` (`contract.md:78`). The pre-insert
  PK read stays, because it tells a same-id replay from a new-id conflict; only the racy D8
  read-then-insert check is replaced by the index.
- Every settle writes `settled_by` and keeps the owner and attempt.
- **Negative controls:** a lapsed running claim can never be reclaimed or returned to queued, at both
  the Python and the trigger layer, and the trigger negative control must go red when the guard is
  removed. A queued compose job does not block fork/revert, and receipts never see compose rows.
- Record in the docstring that no cross-table "one nonterminal op per session" invariant exists;
  exclusion lives in `session_operation_fences`.
- Every scan and count gets an explicit `LIMIT`. Poll reads leave out `request_json`.

**T05: composite start, under R3 unchanged.**

- Put the start-to-running CAS SQL in a connection-taking helper in the compose authority module, called
  inside `start_composer_async_operation`'s existing transaction. The precedent is
  `settle_fork_operation_receipt` calling `settle_operation_receipt(conn, …)` (`service.py:1160-1175`).
- The CAS sets `claim_expires_at = NULL`.
- Add `test_operation_receipts.py` and `test_operation_receipts_postgres.py` to the regression set,
  because `_advance_exclusive_fence_on_connection` sits under the receipts' SOL acquire
  (`routes/operation_receipts.py:239-248`).
- Add a writer pin so that only this helper sets `status='running'`. This is enforced by the trigger
  and by the gate attributing that symbol.
- Owner decision 5 decides whether this composite also re-verifies the head.

**T06: composite terminal, under R2 unchanged.**

- Put the terminal CAS SQL in a connection-taking helper in the compose authority module, as in T05.
- For sends, write the user row, the ingress row and `job.user_message_id` in **one** transaction; the
  FK enforces that they agree.
- If owner decision 2 is approved: add the positive predicate at `service.py:970` with the post-terminal
  negative control (write under a lingering SOL after the terminal CAS must fail), plus a control that
  fork/revert writes under their own SOL are unaffected.
- Re-measure call sites; the guided callers cited at `T06.md:64,124` are gone.

**Beyond T02-T06.**

- **T13/T14:** single-key cutover, deleting the 409 arms and the SPA recovery.
- **T13/T15:** re-scope the ACA replica probe P1 (SRE). Its body and observer read `client_request_id`
  and assume a synchronous, fence-taking POST (`replica_probes.py:525,630-647`).
- **T07:** only two `_track_compose_inflight` mounts remain (`routes/messages.py:142`,
  `routes/composer/compose.py:102`); delete it after cutover.
- **T16:** a PostgreSQL archive-cascade test with a running job, `user_message_id` set and an ingress
  row (the composite FK was measured only on SQLite); a starvation test for a queued compose job under
  steady fork/revert traffic (SRE).
- **T17:** epoch 72.

## Spec text consequences (`docs/specs/2026-09-16-composer-async-operations-design.md`)

**§1**

- The heading "A new transport job table; `guided_operations` is untouched" (`:56`) and its body
  (`:59-61`) become "`session_operation_receipts` is untouched". State the reason: receipts take over
  expired rows and reconstruct from locators; compose must never take over a running row and must store
  the full DTO.
- The Custody row of the field table: the claim owner and attempt are kept at settlement, `settled_by`
  is recorded, and running rows carry no claim expiry.
- `:92-98` "never taken over": say that a DB transition-guard trigger and a queued-only reclaim
  predicate enforce this, with liveness equal to SOL liveness (R3). Do not leave it as design intent.
- Add: one nonterminal job per session is a partial unique index (D8), and terminal rows cannot be
  deleted while their session lives.
- The guided references at `:11` and `:41-52` (three guided routes stay synchronous) are stale (also
  contract D11).

**§2**

- Replace `:108-110` ("the existing `guided_operation_request_hash` is the model to follow, not a
  function to share") with: one neutral request normaliser, a per-facility schema tag, and a
  golden-vector byte-identity pin for receipts.
- Replace "gain a required `operation_id`" (`:105-106`) with the one-id rule from owner decision 1:
  the operation id **is** the ingress key, the ingress receipt is the immutable acceptance record, and
  there is no second client key.
- The D8 admission check is already owed to §2 through T17; add that a schema index enforces it.

## Fact corrections confirmed by the chair

| # | Claim | Correct fact |
|---|---|---|
| 1 | Brief and plan: silent on the existing send identity | `SendMessageRequest.client_request_id: UUID` (`schemas.py:154`) and `message_ingress_receipts` (`models.py:464-488`; epoch 69, `8630db9b8` 2026-09-27) exist. 0 mentions in the plan folder or the spec. |
| 2 | Contract: epoch 68 (`contract.md:39`) | The tree is at 71 (`models.py:59`). The next cut is 72. |
| 3 | Brief: "`schema.py:45`" is the session epoch | `schema.py:45` is `_COORDINATION_HARD_CUT_EPOCH = 71`. `SESSION_SCHEMA_EPOCH = 71` is at `models.py:59`. Both are 71; they are different constants. |
| 4 | Contract: `_GuidedOperationRequest` and `guided_operation_request_hash` (`contract.md:117,158,161`); spec `:109` | 0 hits in `src/`. The strict base is `_SessionOperationRequest` (`schemas.py:72`). |
| 5 | Contract: `RecomposeRequest` is "NEW … only operation_id" (`contract.md:161`) | It exists, is a non-strict `_RequestModel`, and carries `expected_user_message_id` (`schemas.py:162-165`). |
| 6 | Brief: receipt "callers" | The list mixes function callers (`service.py`, `routes/operation_receipts.py`, `routes/sessions.py`, `routes/composer/state.py`) with modules that read the table directly (`coordination/repository.py:723-745`, `blobs/service.py:487-495`). It also omits `proposal_authority.py:538` (a docstring) and `schema.py` (the trigger inventory). |
| 7 | Brief: the trigger inventory "(5 places)" | There are 5 places per trigger, but the required trigger set now has 11 entries, 3 of them for receipts (`schema.py:96-112`). |
| 8 | Option B: "reuse `operation_receipt_request_hash`" | Not buildable as named. It fixes the tag, the `Literal` kind, the `operation_id` field and a strict DTO (`operation_receipts.py:36-53`; `protocol.py:264`). |
| 9 | Systems-thinker and leverage (opening): "the running row has no lease column" | The running arm requires the claim triple NOT NULL, including `claim_expires_at` (`T03.md:79,838-840`). |
| 10 | Relay: "codex: Forwarded" | That was the courier's status. The files hold a B position (opening) and a B′ position (cross). |

Panellist measurements the chair did **not** reproduce, so they carry no weight in the decision:

- db-architect's SQLite WAL figure: about 408 KB per row rewrite and about 2.4 MB per job in the worst
  case;
- SRE's claim that fork/revert traffic starves a queued job, which follows from the F-M2 discovery skip;
- SRE's shared-pool (16+16 workers) pressure argument.

## Revisit if

- The golden-vector test cannot prove receipt hashes are byte-identical after the extraction. Ship C.
- A third client-minted-id kind appears that needs queued/async semantics. Reconsider D as shared code,
  extracted from working implementations; never as a supertype table.
- John rules claim history is ADR-046 product evidence. Add a compose events table under B′.
- John rejects option (2) of the identity ruling, or chooses to fold ingress into the job. T03 and T06
  change shape.
- Receipts change to have a queued phase or no takeover. The main argument against A would weaken.
- Measured PG load shows the shared `run_sync_in_worker` pool, not the table, isolating fork/revert
  from compose polls.

## Confidence Assessment

**Overall confidence: High** on the table decision. **Moderate** on the codec detail and the schema
amendments.

| Finding | Confidence | Basis |
|---|---|---|
| Separate table (reject A) | High | 9/9 seats. The chair verified the takeover arm (`operation_receipts.py:316-355`), the archive Tier-1 (`repository.py:723-735`) and the kind-blind `TablePolicy.permits` (`test_…:36-44`). |
| Reject D | High | 9/9. Three facilities with divergent replay models were verified in the tree. |
| B′ over C (shared normaliser) | Moderate | 9/9 final, but it depends on the golden vector. Python-architect's churn cost is real. |
| One id per send (option 2) | Moderate (owner-dependent) | 9/9 say it is a precondition; 7/9 explicitly recommend option (2), and 2 leave the shape to John. The FK direction was verified against the recompose path by the chair. |
| Running-row reclaim hazard as contracted | High | Verified at `T03.md:79,838-840` and `T04.md:2281-2282`. |
| Terminal forensic loss as contracted | High | Verified at `T03.md:82,87` and `models.py:320-332`. |
| Positive fence predicate | Moderate | Reasoning from D12 is sound, but no post-terminal write inventory has been measured yet. |

## Risk Assessment

**Implementation risk:** Medium. The table choice itself is low risk. The identity cutover touches the
route, the SPA recovery and the ACA acceptance tooling.

**Reversibility:** Moderate. Every schema change is an epoch cut with a store reset, so it is cheap to
redo before release, but receipt-hash identity is durable within an epoch.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Two client ids on one send (dual acceptance) | High | Certain if the plan ships as written | Owner decision 1 before T02 |
| A running compose row is reclaimed, giving a second provider turn | Critical | Possible (one missing `status='queued'` term) | Null `claim_expires_at` on running rows, the transition-guard trigger, and a negative control that goes red |
| The extraction silently changes receipt hash bytes | High | Possible | Golden vector; C as fallback |
| Late non-audit write after the terminal CAS under a lingering SOL (D12) | Medium | Possible | Owner decision 2 plus a post-terminal negative control |
| `worker_lost` custody unrecoverable | Medium | Certain as contracted | Keep owner and attempt, add `settled_by`, add the delete guard |
| Queued compose starved by fork/revert traffic | Medium | Unmeasured | T16 test, runbook entry |
| Composite FK behaves differently on PG under archive cascade | Medium | Unmeasured on PG | T16 testcontainer test |

## Information Gaps

- [ ] **Identity ruling** (all B′ seats): the shape of T02, T03 and T06 depends on it.
- [ ] **Post-terminal write inventory under the same SOL** (tech-critic): a precondition for the
      positive predicate.
- [ ] **PG behaviour of the ingress→job composite FK under the D7 cascade** (db-architect): measured on
      SQLite only.
- [ ] **p50/p99 sizes of `request_json` and `result_json`** (db-architect, SRE): T00 should measure
      these before T03 fixes the bounds.
- [ ] **Fork/revert starvation of queued compose, and shared-pool p99 on PG under 64 polls** (SRE):
      unmeasured.
- [ ] **Retention class for settled compose rows** (SRE): an owner ruling.
- [ ] **Head change between admission and start** (tech-critic): an owner ruling adjacent to D9.
- [ ] **Chair-specific:** the chair did not re-read every T02–T06 line that the panellists cited;
      task-file line numbers other than those quoted above come from the panellists.

## Caveats

- This synthesis re-verified the disputed facts listed above. Undisputed panellist findings are
  inherited as reported.
- R2, R3, freeform-only, 202+poll, D7 and D8 are respected, not relitigated. The D8 index enforces
  D8 as written. The positive fence predicate amends F-B2/C2 and is therefore sent to John, not
  adopted.
- The panel covered storage, codec, identity and failure modes. It did not cover frontend UX, cost or
  accessibility.
- Re-run the plan review after the plan is re-baselined. This synthesis does not certify the amended
  plan.
