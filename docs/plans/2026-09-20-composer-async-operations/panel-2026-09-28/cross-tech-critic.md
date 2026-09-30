# Cross-examination: tech-selection critic

Critic: tech-selection-critic. Tree: `release/0.8.1` @ `6506f6a7f`, main checkout, read-only.
Opening position: `position-tech-critic.md` (B′). Every citation below was re-read in this pass.

## Summary (machine-readable)

- verdict: REVISIT (B as the brief words it). KEEP a separate table. Recommend B′ with two new schema amendments.
- confidence: HIGH that the table should be separate. MEDIUM on the ingress resolution, which needs an owner ruling.
- primary_weakness: B's case for "structural" safety is overstated. As contracted, a `running` compose row still carries a `claim_token` and a stale `claim_expires_at`, and a status predicate is the only thing that stops a reclaim.
- alternative_recommended: B′ (separate table, a lifted neutral codec, one client-minted id per send) plus the running-row claim-expiry fix below

## 1. New red-team finding against B: the "structural" invariant isn't structural yet

The systems-thinker says that under B "the compose authority has no takeover-capable method at all" (Level 10, structure). Leverage says that under B′ "the running row has no lease column, so a takeover compare-and-swap cannot be written without a schema change". **The contract says otherwise, and so does the plan's own code.**

- The contract's `running` bundle requires the claim triple NOT NULL (`contract.md:191`, "running: request_json, claim triple, SOL triple, started_at NOT NULL").
- `T03.md:708-710`: "A running row keeps its claim and additionally binds the session-operation (COMPOSE) fence".
- `T05.md:496` asserts that `job["claim_expires_at"] is not None` after the start composite runs.
- `renew_claim` covers the queued claim only (`contract.md:228-230`). Every running row's `claim_expires_at` is therefore expired within `composer_async_claim_lease_seconds` (30 s by default), and it stays expired for the whole turn.
- Nothing reads that expiry once the row is running. `list_expired_running` keys on the SOL fence (`T04.md:2592-2620`), which follows R3's "the running row has no separate lease expiry" (`contract.md:268`).
- The reclaim predicate is `claim_token IS NULL OR claim_expires_at <= now` (`contract.md:227`, `T04.md:2282`). Only the sibling `status == "queued"` term (`T04.md:2281`) keeps it off running rows.

So B, as specified, has the same kind of guard it criticises in A. One predicate, which a future editor must keep, separates a running row from a second provider turn. That predicate lives in a table with only one lifecycle, which is better than A's kind branch, but it is still Level 5, not Level 10. The row also carries a dead timestamp that looks like an expired lease to any future "reap expired claims" query.

**Fix (cheap, and it makes the claim true).** At the start composite, set `claim_expires_at = NULL` and keep `claim_token` and `claim_owner_instance_id`. Change the running bundle to "`claim_token` and owner NOT NULL, `claim_expires_at` NULL". The reclaim disjunction then evaluates to NULL on every running row, so the database cannot select a running row even when the status filter is missing. Add leverage's transition-guard trigger to forbid running→queued. With both in place the invariant is structural. Without them, B's headline advantage over A is a narrower version of the same risk.

## 2. Rebuttals, position by position

### systems-thinker (B)
- **Rebut:** "no takeover-capable method at all" is false as specified (§1). `claim_next` is exactly that method, and the status term is its only guard.
- **Rebut:** the "Success-to-the-Successful" loop between compose traffic and fork/revert review attention is speculative. No measurement supports it, and the reasons for separation that hold up are the takeover arm, the fence model and the per-table mutation-authority gate.
- **Agree:** as written, T02 must parameterise the codec (`operation_receipts.py:36-53`; `protocol.py:264`). This matches my B1.
- **Agree:** add a negative test that no path moves a running row back to a state that can be adopted. §1 shows why this test is needed rather than decorative.
- **Gap:** the systems-thinker's position does not mention `message_ingress_receipts`.

### leverage (B′)
- **Rebut (factual):** "the running row has no lease column" is wrong against `contract.md:191` and `T03.md:708-710` (§1). The transition-guard trigger leverage proposes is the right remedy, but it is necessary, not supplementary.
- **Rebut:** ingress option (1), in which the job row takes over dedup and the ingress table is removed, is weaker than keeping ingress:
  - The ingress row is immutable from insert, with no-update and no-delete triggers (`schema.py:84-86,104-105`). The job row is mutable until terminal, and `user_message_id` is NULL while queued (`contract.md:25`).
  - D7 cascades the job away with the session, and the job is "not durable history" (repository.py:747-770).
  - Folding the binding into the job row weakens the immutability of "this client request produced this user row" under ADR-046.
- **Accept (plausible, orthogonal to A/B):** the positive predicate at `_require_session_operation_context_on_connection` (`service.py:970-985`), because D12 lets the SOL outlive a terminal job (`contract.md:82`). **Caveat:** T06 must first measure whether any legitimate write runs under the same SOL after the terminal CAS, such as proposal settlement or lease close. If one does, the positive form refuses it.
- **Accept:** a writer-manifest pin that only the start composite sets `status='running'`.

### solution-architect (B′)
- **Rebut:** "exactly one of `job.user_message_id` or the ingress receipt is kept as the authority" can't hold for both kinds.
  - Recompose writes no ingress row. `RecomposeRequest` has only `expected_user_message_id` (`schemas.py:162-165`), and `compose.py` has no ingress call; a grep for `message_ingress` in `routes/composer/` returns nothing.
  - The job column is therefore the only binding for recompose, and for send the ingress row is the immutable one. Keep both.
  - Make them agree by construction: write the user row, the ingress row and `job.user_message_id` in one transaction in T06.
  - Add a Tier-1 re-check on the poll's terminal re-validation for `compose_message`: the ingress row for `(session_id, operation_id)` must name the same `user_message_id`.
  - A schema-level composite FK is possible but needs an extra nullable column for each kind. It is not worth that cost.
- **Accept:** add `test_operation_receipts.py` and `test_operation_receipts_postgres.py` to T05's regression set. The `_advance_exclusive_fence_on_connection` refactor sits under the receipts' own SOL acquire (`routes/operation_receipts.py:239-248`).
- **Accept:** delete the `message_already_accepted` 409 lookup (`routes/messages.py:111-125,174-181`) at cutover.

### db-architect (B)
- **Mostly agree.** Three of its points change my plan consequences (§3).
- **Friendly amendment:** its forensic-retention fix (null only `claim_token` and `claim_expires_at` at terminal) should go further. Null `claim_expires_at` at *running*, not only at terminal (§1).
- **Note:** its partial unique index `(session_id) WHERE status IN ('queued','running')` has a direct two-element precedent that the PG schema comparator already accepts: `uq_runs_one_active_per_session`, `models.py:2005-2011`. It enforces D8 inside the table. It does not close my B4, the cross-table case of a revert landing between admission and start, and nothing short of A could.

### python-architect (C)
- **Rebut:** its C "with an optional shared helper" is B′ with the obligation removed. An optional extraction is how two definitions of "same request" drift, and nothing enforces byte-lockstep between siblings.
- **Rebut:** "No brief fact was wrong" misses `message_ingress_receipts` (`models.py:464-488`) and `SendMessageRequest.client_request_id` (`schemas.py:154`). Six other panellists found these. They change T02, T03 and T10 whichever of A–D is chosen.
- **Rebut:** "`claim_next` with `FOR UPDATE SKIP LOCKED`" is stale. `contract.md:22` replaced it with a discovery read plus one `locked_session_transaction` per candidate.
- **Rebut:** the claim that "existing persisted `request_hash` values were computed against [the receipt tag]" carries no compatibility weight. Epoch 72 resets the session store. The real argument for a byte-identity test is behavioural equivalence within the release, not stored rows.
- **Agree:** its main structural point, that `reserve_operation_receipt` would have to invert takeover by kind (`operation_receipts.py:316-353`), is the strongest single reason against A.

### audit-integrity (B)
- **Accept:** a terminal compose row loses which instance ran it. `session_operation_fences` is keyed by `session_id` alone (`models.py:325-351`), so the SOL triple on the row can't recover it later. This moves my B3 from "an unpriced decision" to "must fix". The minimum is to keep `claim_owner_instance_id` and `attempt` at terminal and add a closed `settled_by` column (owner, reaper_lost, own_lapsed, inactive_session, unstarted, cancel).
- **Accept (recommended, not blocking):** a no-delete-while-session-exists guard mirroring `trg_session_operation_receipt_events_no_delete` (`models.py:1676-1690`). The job row holds the only stored copy of the public terminal response.
- **Rebut:** a full `composer_async_operation_events` table under every option. Queued-claim churn is transport telemetry. The turn's audit lives in `chat_messages` and the `llm_calls` cohort. Once owner, attempt and `settled_by` survive terminal, an events log adds renewal-and-reclaim history that no identified reader needs. It should be the owner's call, not a panel default.

### sre (B)
- **Accept, and it strengthens the case against A:** A needs a second, unfenced insert path, because receipt reservation today requires a live SOL context (`service.py:1063-1065`). That breaks the receipts' creation invariant.
- **Accept:** the shared `run_sync_in_worker` pool, not the table, is the real isolation lever.
- **Agree with its own rebuttal:** claim churn is telemetry.

### codex (B, high)
- **Agree** on the core: a domain-separated shared primitive, two wrappers, and golden vectors.
- **Rebut:** it keeps "T02 … in their planned homes" and "T05 exactly as planned". The planned base `_GuidedOperationRequest` no longer exists (`schemas.py:66-77`), and T05 as planned leaves the stale running-claim expiry (§1).
- **Gap:** it also misses `message_ingress_receipts`.

## 3. What changed my mind

1. **Forensic retention is mandatory, not an owner choice** (db-architect, audit-integrity). The fence table's single-key PK means nothing else records which instance ran the turn.
2. **Adopt the one-nonterminal-per-session partial unique index** (db-architect). It turns D8 from an authority read into a schema invariant, and the precedent already passes PG reflection.
3. **The transition-guard trigger is required** (leverage). My own §1 finding shows that B without it only moves the guard; it does not remove it.
4. **Confidence in keeping the table separate rises from MEDIUM to HIGH.** Eight of nine positions agree. The two strongest independent reasons are sre's unfenced-insert point and audit-integrity's point about kind-blind TablePolicy grants (`test_session_db_mutation_authority.py:36-46,12205-12215`).

My option does not change. Nothing presented makes A cheaper or safer, or makes D earn its abstraction. C is B′ without its drift guard.

## 4. Final position

**B′.**
- A dedicated `composer_async_operations` table with its own `TablePolicy`.
- A request-hash normaliser lifted into a neutral module. It takes the schema tag as a parameter and a `str` kind validated by each caller, and each facility keeps its own tag and closed kind union. Receipts re-point at it and a golden vector pins that receipt hashes are unchanged. `OperationReceiptKind` is not widened.
- One client-minted id per send: the wire field is `operation_id` on `_SessionOperationRequest`, and it is the ingress key. Ingress stays as the immutable record the worker writes. Its 409 arms retire, and a conflict at the worker becomes Tier-1.
- Two schema amendments that make B's safety claim true:
  - null `claim_expires_at` at running;
  - a transition-guard trigger, plus a negative control.

## 5. Plan consequences (delta over my opening)

- **Before T02:** John rules on one id per send. Recommended: `operation_id` is the ingress key, with no alias. Recompose gains `operation_id` and has no ingress row.
- **T02:**
  - Lift the codec as above.
  - Rebase `SendMessageRequest` and `RecomposeRequest` onto `_SessionOperationRequest` (`schemas.py:72`).
  - Amend the codec sentence in spec §2.
- **T03:**
  - Running bundle: `claim_token` and owner NOT NULL, `claim_expires_at` NULL.
  - Terminal bundles keep `claim_owner_instance_id` and `attempt`, and add a closed `settled_by`.
  - Add the partial unique index `(session_id) WHERE status IN ('queued','running')`, which replaces one-running-per-session.
  - Add a transition-guard trigger on both dialects (no running→queued; running identity columns frozen). It must be added in the same 5 places as the terminal trigger.
  - Optionally add a no-delete-while-session-exists guard.
  - The epoch is 72.
- **T04:** keep the `status == 'queued'` predicate in the reclaim path; the running bundle makes it defence in depth. Pin in the writer manifest that only the start composite writes `status='running'`.
- **T05:**
  - The start composite nulls `claim_expires_at`, and `T05.md:496` inverts.
  - Add both receipts test files to the regression set.
  - Decide whether a head change since admission is refused or recorded at start (my B4).
- **T06:**
  - Write the user row, the ingress row and `job.user_message_id` in one transaction.
  - Add the Tier-1 agreement re-check on poll re-validation for `compose_message`.
  - Measure post-terminal writes under the bound SOL before adopting leverage's positive predicate.

## Confidence Assessment

HIGH on the separate table: the independent reasons converge and each was re-verified. HIGH on §1 as a fact, from the contract, T03, T04 and T05 text. MEDIUM on whether nulling `claim_expires_at` at running breaks any T05, T06 or T11 read I haven't traced. I grepped T05, T06 and T11 for `claim_expires_at` and found only the start-composite verification (`T05.md:965-981`), the test assert (`T05.md:496`), test aging helpers, and the terminal nulling (`T06.md:319`). MEDIUM on the ingress resolution, which is an owner ruling.

## Risk Assessment

- **If §1 is ignored:** B ships with a running row that a status-blind reclaim query would adopt. The failure is a second provider turn: a duplicated assistant row, duplicated `llm_calls` and double spend.
- **If both ids are kept:** a lost-202 retry produces a 202 replay at one layer and a 409 at the other.
- **If full events tables become a panel default:** each future kind carries a trigger pair and a TablePolicy with no reader.
- **Re-test trigger for D:** a third client-minted-id kind with queued/async semantics.

## Information Gaps

- Partly closed. The worker's claim-renewal task is created before the lock wait and handed to `_start` (`T11.md:2175-2190`). `renew_claim` is queued-only (`contract.md:228`), so renewal cannot legitimately continue past running. I did not read `_start`'s body to confirm that it stops the task.
- I haven't measured post-terminal writes under the bound SOL (needed for leverage's predicate).
- I haven't checked whether the SPA's `client_request_id` recovery (`sessionStore.ts` ~L676-855) is the tutorial Build path's only retry mechanism.

## Caveats

- Scope is the table/codec choice and its seams. R2, R3 and the freeform-only scope are not reopened.
- No cost modelling.
- I did not re-verify db-architect's SQLite WAL measurements; they are neutral between the options.
