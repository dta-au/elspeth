# Cross-examination: solution architect

- **Panellist:** solution architect (forward-design reviewer lens)
- **Tree:** `release/0.8.1` @ `a375d7f13` (HEAD at cross-examination; the brief pinned `6506f6a7f`, and
  every file cited below is unchanged between them for this question). Main checkout, read-only.
- **Opening position:** B′. That is a separate `composer_async_operations` table, a shared request codec in a
  neutral module, and one client-minted identity per send, reconciled with `client_request_id` /
  `message_ingress_receipts` before T02.
- **Final position:** B′, **amended**. The table decision is unchanged. The identity rule is sharper now: one
  structural authority for "operation → user row", not an either/or left to the implementer. I also take five
  refinements from the db-architect, audit-integrity, leverage and codex panellists; they are listed in §3.

## 1. Where the panel stands

| Panellist | Table | Codec | Found F1 (ingress identity) | Net distance from B′ |
|---|---|---|---|---|
| systems-thinker | separate | lift required (says so under plan consequences) | no | B′ minus identity |
| leverage | separate | neutral lift | yes | B′ plus structural hardening |
| tech-critic | separate | neutral lift | yes | B′ |
| db-architect | separate | "one primitive, not a DB question" | yes (T03 note) | B′ plus index and forensic fixes |
| python-architect | separate | own codec, with the lift as an "optional addendum" | no | B′ with the lift optional |
| audit-integrity | separate | shared core, request and response | no | B′ plus an event log |
| sre | separate | kind-agnostic reuse | yes | B′ |
| codex | separate | domain-separated shared primitive | no | B′ |

Nobody argues for A or D. Eight of eight want a separate table. Seven of eight want the request codec shared in some
form; python-architect's C carries the same lift as an addendum. Five panellists independently found the
`message_ingress_receipts` / `client_request_id` gap that the brief and plan omit. The real disagreements are about
what sits *around* the table: identity ownership, forensic retention, an event log, and DB-level guards. That makes the
panel's main output a set of plan amendments, not an A–D verdict.

## 2. Rebuttals, one per differing position

### python-architect (C)

- **Their claim:** C, with "optional low-risk addendum: factor the shared codec shape … into one generic helper both
  codecs call."
- **Rebuttal:** that addendum *is* B′'s codec decision. So the gap between C and B′ is only whether the lift is
  optional. It should not be optional:
  - C's recorded rationale ("the model to follow, not a function to share", spec §2; `contract.md:117-118`) named a
    sibling on a mode being deleted. That sibling no longer exists: `guided_operation_request_hash` has 0 hits in
    `src/`.
  - The live sibling, `operation_receipt_request_hash` (`operation_receipts.py:39-53`), is mode-neutral. It enforces
    exactly the rules C would clone: strict and forbid (`:42`), `operation_id` present and excluded (`:44-48`), and
    defaults and None materialised (`:49-51`).
  - An optional lift on an audit-grade binding means two definitions of "same request" that can drift. That is the
    weak-decision-record failure mode, where a decision outlives its reason.
- **What I accept from them:** the takeover contradiction (`operation_receipts.py:314-353`) is the single strongest
  argument against A and should lead the ADR. I also accept (from codex and tech-critic) that C is the right
  *fallback* if the golden-vector byte-identity test for receipt hashes cannot be made to pass. Receipt identity must
  not move.

### systems-thinker, sre, db-architect, audit-integrity (plain "B")

- **Their claim:** "B" as the brief phrases it: reuse `operation_receipt_request_hash`.
- **Rebuttal:** B as worded cannot be built, and several of them say so under "plan consequences". The function
  takes `kind: OperationReceiptKind`, a two-value Literal (`protocol.py:264`). It hard-codes
  `_REQUEST_SCHEMA = "session-operation-receipt-request.v1"` (`operation_receipts.py:36,53`). Widening
  `OperationReceiptKind` would let compose kinds through `_validate_identity` (`:68`) and into `_outcome`'s revert
  `else` branch. So "B" and "B′" are the same design, and the ADR should name it B′ so nobody implements the literal
  wording.
- **The substantive difference is F1.** B without the identity rule still ships two client-minted keys on one
  `POST /messages` body:
  - `client_request_id` is required on `SendMessageRequest` (`schemas.py:154`). It is bound by immutable
    `message_ingress_receipts` (`models.py:464-487`), and the route rejects duplicates with a 409
    (`routes/messages.py:175,242`).
  - `operation_id` would be the job key, with replay-to-202 semantics.

  That is the dual acceptance the owner forbids, however good the table decision is. Of these four, db-architect and
  sre did flag the ingress relationship. systems-thinker and audit-integrity did not, and their T02 consequences need
  it added.

### audit-integrity (B plus a mandatory `composer_async_operation_events` table)

- **Their claim:** the job needs an append-only event log. Otherwise settlement nulls `claim_owner_instance_id`, and
  the SOL triple cannot recover which instance ran, because `session_operation_fences` is keyed by `session_id`
  alone (`models.py:325-352`).
- **Verified.** Settlement nulls the claim triple at terminal (T03 row contract, `T03.md:80-87`: "claim triple NULL"
  on complete and fail; `contract.md:193-196`). The fence row is overwritten by the next operation. The start quad
  kept on the row (`session_operation_id`, token, epoch, `started_at`) carries no owner instance.
- **Rebuttal on the remedy, not the finding.** A full event table is a second schema object, two more triggers in
  five places, a terminal-hash digest entry and a Tier-1 validator. No named requirement in the spec or contract
  asks for claim history. The turn's audit truth is the `chat_messages` and `llm_calls` cohort; the job row is
  custody transport. Under the gold-plating check, the minimal fix that closes the *evidence loss* wins:
  - keep `claim_owner_instance_id` and `attempt` on terminal rows, and null only `claim_token` and
    `claim_expires_at`. This follows the precedent that "release never nulls forensic authority"
    (`models.py:321-324`), and db-architect proposed the same fix independently;
  - add a terminal-row DELETE guard (see §3).

  If John rules that claim, release and reclaim history is product audit evidence under ADR-046, then add the events
  table *under B*, using the receipts pattern, as audit-integrity and sre both say. That is an additive decision
  either way, not an argument for A.
- **Conceded:** the forensic-loss finding and the delete gap. See §3.

### leverage (B′, plus identity option (1): the job row replaces `message_ingress_receipts`)

- **Their claim:** the owner picks between (1) the job row takes over ingress dedup and the ingress table is removed,
  and (2) `operation_id` IS the ingress key.
- **Rebuttal to (1):**
  - The ingress row is immutable, with `no_update`/`no_delete` triggers in `_REQUIRED_AUDIT_TRIGGERS`
    (`schema.py:103-104`). The job row is mutable by design: status moves, `request_json` is cleared, and cancel
    races.
  - Moving the only operation→user-row binding onto a mutable row weakens an ADR-046 guarantee that already
    shipped. It also widens blast radius into readers outside the composer: the ACA acceptance controller
    (`_azure_container_apps_acceptance/controller.py`, measured by grep), the message projection's
    `client_request_id` (`schemas.py:216`), and the SPA reconcile paths (`sessionStore.ts:676,703,790,803,855`).
  - Option (2) gets one identity without that cost. Recommend (2) and drop (1) from the ruling request.
- **Accepted from leverage:**
  - the transition-guard trigger;
  - the positive fence predicate. I checked `service.py:970-1000`: the method validates the fence triple only. D12
    (`contract.md:82`) does let the SOL outlive a terminal job after a close failure, so the negative predicate in
    F-B2/C2 (`contract.md:46`), which fires only on `status='running' AND cancel_requested_at IS NOT NULL`, would
    admit a late non-audit write under a lingering lease.
  - the writer-manifest pin that only T05's start sets `status='running'`.

### tech-critic (B′, keep both `job.user_message_id` and ingress, "require an invariant that they agree")

- **Their claim:** the ingress row stays as the immutable acceptance record, and the job keeps its own
  `user_message_id` FK, with an agreement invariant between them.
- **Rebuttal:** an agreement invariant between two independently written columns is a documented invariant, not a
  structural one. A CHECK cannot span tables, and review is the only thing enforcing it. My opening position said
  "keep exactly one, not both", but left the choice to the implementer. The better answer makes agreement
  structural:
  - **Preferred:** drop `composer_async_operations.user_message_id`. The operation→user-row binding is
    `message_ingress_receipts(session_id, client_request_id = operation_id)`, written by the worker in the user-row
    transaction. I verified that the job column is send-only: recompose leaves it NULL (`T10.md` recompose test,
    `record.user_message_id is None`). So nothing is lost for recompose.
  - **Acceptable:** keep the column, but replace its FK to `chat_messages` (`contract.md:187`) with a composite FK
    `(session_id, operation_id, user_message_id)` → `message_ingress_receipts(session_id, client_request_id, user_message_id)`.
    This needs a unique constraint over those three ingress columns; it is trivially unique because the PK is a
    subset. With a NULL `user_message_id` (queued, recompose), MATCH SIMPLE skips the check on both dialects. The
    two cannot disagree.
- **Accepted from tech-critic:**
  - A worker-side ingress conflict becomes a Tier-1 `AuditIntegrityError`, not a 409. After cutover it can only mean
    an ingress row with no job.
  - T13/T14 delete the route's `message_already_accepted` / `message_idempotency_conflict` arms and the SPA's
    transcript-matching recovery, so no dual recovery path remains.
  - The T05 head-change question is real: a revert can run while a send is queued, because a queued job holds no
    SOL. But it is not an A–D discriminator. The receipts table provides no exclusion here either (my F3: exclusion
    lives in `session_operation_fences`). It goes to John as a separate D9-adjacent ruling.

### db-architect (B, partial unique index on nonterminal rows)

- **Their claim:** replace `uq_…_one_running_per_session (session_id) WHERE status='running'` with a unique index
  on `(session_id) WHERE status IN ('queued','running')`.
- **Accepted, and it changes my T03.** D8 (`contract.md:78`) is exactly "at most one nonterminal job per session".
  Today the plan enforces it as a read-then-insert in the authority, which is racy between two admissions. The index
  makes it a schema invariant: the losing concurrent insert gets an `IntegrityError`, which maps to the D8 409. The
  precedent is live and uses a two-element IN, not the one-element form that PostgreSQL reflects as `=`
  (`uq_runs_one_active_per_session`, `models.py:2005-2011`). It subsumes the running-only index, and it serves sre's
  D8 lookup, so `ix_…_session_status` can go unless T04 names another query.
- **Also accepted:** the PostgreSQL archive-delete test for a running job with `user_message_id` set (T16). Under my
  preferred identity shape the RESTRICT FK to `chat_messages` disappears, but the test should still prove D7 on
  PostgreSQL.

### sre (B)

- No disagreement on the table. Their `(session_id, status)` index for D8 is superseded by db-architect's partial
  unique index. The shared `run_sync_in_worker` pool point (`async_workers.py`) is correct and orthogonal to A–D.
  I accept it as a T16 measurement.

### codex (B, domain-separated shared primitive)

- Agreement. **Accepted refinement:** the neutral codec's schema argument should be a closed Literal of the two
  tags, not a free `str` as my opening signature had it. A typo'd tag would silently open a third hash domain.

## 3. What changed my mind

1. **The D8 index.** From db-architect: one nonterminal job per session becomes a partial unique index, replacing
   the running-only index. D8 becomes structural.
2. **Forensic retention.** From audit-integrity and db-architect: terminal rows keep `claim_owner_instance_id` and
   `attempt`, and null only `claim_token` and `claim_expires_at`. The completed and failed CHECK bundle arms change
   to match.
3. **The terminal DELETE guard.** From audit-integrity, verified: receipt rows are protected indirectly, because
   deleting one cascades into `session_operation_receipt_events`, whose `no_delete` trigger refuses while the
   session exists (`models.py:1680-1696`). The planned compose table has only an UPDATE trigger (`T03.md:63`). Add a
   BEFORE DELETE guard on terminal rows with the same `EXISTS (SELECT 1 FROM sessions …)` predicate, so D7 session
   cascades still work.
4. **The transition guard.** From leverage: widen the planned terminal-immutable UPDATE trigger into a
   transition-guard trigger. It refuses any update to a terminal row, `running` → `queued`, and changes to kind,
   request hash, actor or the SOL triple on a running row. It stays one UPDATE trigger function rather than a second
   one, which keeps the inventory cost to two triggers (UPDATE and DELETE). Add a negative control that tries to
   reclaim a running row and must fail. The reason: "a running row is never taken over" is the invariant whose
   failure means a second provider turn, with duplicate spend and a duplicate audit cohort. It earns a DB rule, not
   only a code convention.
5. **The positive fence predicate.** From leverage (moderate confidence): at
   `_require_session_operation_context_on_connection` (`service.py:970`), refuse unless `audit_only` if any job row
   bound to the exact fence triple is not `running`, or has `cancel_requested_at IS NOT NULL`. This closes the D12
   lingering-lease window that the negative form leaves open.
6. **The response hash.** From audit-integrity, leverage and tech-critic: share the strict-DTO response hash
   primitive as well. My opening position said not to share it. I was wrong about the coupling:
   `operation_receipt_response_hash` (`operation_receipts.py:56-62`) carries no domain tag and no kind; it is a
   generic content hash. Pin `stable_hash(json.loads(result_json)) == response_hash(dto)` so there is one definition.
7. **The identity rule.** Refined from "one of two, implementer's choice" to a structural authority (§2,
   tech-critic). The ingress receipt owns operation → user row. The wire field is `operation_id`, because the strict
   base (`schemas.py:72-88`) and the codec (`operation_receipts.py:44`) both pin that name. The ingress column and
   the projection field are renamed in the same epoch cut, with no alias.

Unchanged: A is rejected, because the safety invariant would become a kind switch and archive/D7 would regress
(`repository.py:722-735`). D is rejected as gold-plating: three client-minted facilities, three retry rules. C is
the fallback only if receipt byte-identity cannot be proven.

## 4. Final position, and the plan amendments it implies

**B′-final:** a separate `composer_async_operations` table, owned by `ComposerAsyncOperationAuthority` with its own
TablePolicy. The receipts table, its authority pin and its behaviour are untouched.

- **Before T02 (owner ruling):** one identity per send. `operation_id` is the ingress key. The ingress receipt stays
  immutable and is the only operation → user-row authority. Leverage's option (1) is withdrawn from the ask.
- **T02:**
  - Add a neutral codec module: `session_operation_request_hash(*, schema: Literal[<receipt tag>, <compose tag>], session_id, kind: str, request)`,
    plus the shared strict-DTO response hash.
  - Golden vectors pin receipt hashes byte-identical.
  - `OperationReceiptKind` is not widened.
  - The DTOs are rebased on `_SessionOperationRequest`. `RecomposeRequest` carries `expected_user_message_id` in the
    hash.
  - `client_request_id` is renamed to `operation_id` on the wire, with no alias.
- **T03:**
  - Epoch 72, not 68.
  - Re-anchor after `session_operation_receipt_events_table` (`models.py:825`).
  - Nonterminal partial unique index.
  - Terminal rows keep owner and attempt.
  - The transition-guard UPDATE trigger plus a terminal DELETE guard, added to all five inventory places.
    `_REQUIRED_AUDIT_TRIGGERS` goes from 11 to 13.
  - Drop `user_message_id`, or FK it to ingress.
  - A table comment stating D7: the job never blocks archive and is not durable history in
    `decide_and_soft_archive`.
- **T04:**
  - Admission dedupes by job PK. An ingress conflict is Tier-1.
  - Writer-manifest pin: only T05 sets `running`.
  - Tests: a queued compose job does not block fork/revert, and receipts never see compose rows.
- **T05:**
  - Add the receipts route suites (`test_operation_receipts.py`, `_postgres.py`) to the regression set.
  - Route the head-change-while-queued question to John.
- **T06:** the positive fence predicate, with negative controls for a lingering lease after the terminal CAS and for
  fork/revert under their own SOL.
- **T13/T14:** delete the ingress 409 arms and the SPA transcript-match recovery at cutover.
- **Plan hygiene (everyone found this):** re-baseline T02-T07 against the current tip. The epoch, the guided
  anchors, `_GuidedOperationRequest` and the deleted test file are all stale.

**ADR shape:**

- **Context:** sync replay-by-locator vs async store-and-return, and three client-minted facilities with three
  retry rules.
- **Decision:** B′-final.
- **Rejected:** A (a kind switch on the takeover path, plus a D7/archive regression), C (its rationale has expired,
  and drift), D (no second consumer beyond identity and codec).
- **Re-test trigger for D:** a third kind with queued/claim/running semantics.
- **Reversibility:** moderate, since any later merge or split is one pre-release epoch cut.

## Confidence Assessment

**Overall confidence: high on the table decision, moderate-high on the amendments.**

| Claim | Confidence | Basis |
|---|---|---|
| Not A | High | `operation_receipts.py:68,228-233,314-353`; `repository.py:722-735` (verified by me and by 6 others) |
| Not D | High | Three divergent retry rules (receipts takeover, ingress 409, compose replay/no-resume) |
| Codec lift over C | Moderate-high | Expired rationale; drift on an audit binding; fallback to C if golden vector fails |
| One identity, ingress as authority | Moderate-high | `models.py:464-487`, `schema.py:103-104`, `schemas.py:154,216`; `T10.md` shows `user_message_id` is send-only |
| Nonterminal partial unique index | High | D8 text `contract.md:78`; precedent `models.py:2005-2011` |
| Forensic retention and delete guard | High | `T03.md:80-87`, `models.py:321-352`, `models.py:1680-1696` |
| Transition guard | Moderate | Judgement: DB rule on the replay-safety invariant, against trigger-inventory cost |
| Positive fence predicate | Moderate | `service.py:970-1000`, `contract.md:46,82`; not exercised against T06 ordering |
| Event log not required | Moderate | No named requirement; hinges on John's ADR-046 reading |

## Risk Assessment

- **Implementation risk:** Medium. **Reversibility:** Moderate (epoch cut).
- **Risks:**
  - **Proceeding on the stale plan.** High likelihood, high severity. T02/T03/T06 cite removed symbols. Mitigation:
    re-baseline before T02.
  - **Codec lift changes receipt hashes.** Low likelihood, high severity. Mitigation: golden vector first, and C as
    fallback.
  - **Identity rename breaks the SPA or ACA acceptance.** Medium likelihood, medium severity. Mitigation: rename in
    one epoch cut, sweep `sessionStore.ts`, the ACA controller, and `replica_probes.py` (per leverage).
  - **Positive predicate over-fences a legitimate write in T06's composite.** Low-medium likelihood, medium severity.
    Mitigation: order the terminal CAS after the assistant rows, and add negative controls.
  - **Transition trigger or index trips PostgreSQL CHECK/index reflection.** Low likelihood, medium severity.
    Mitigation: use the two-element IN form and the testcontainer suite.

## Information Gaps

- I did not re-derive every call site of `_require_session_operation_context_on_connection`. The positive predicate's
  reach is unmeasured.
- I did not inspect `replica_probes.py` or the ACA controller's ingress usage beyond grep presence. The rename blast
  radius is estimated, not measured.
- I have not seen John's reading of whether claim history is ADR-046 product evidence. That decides the event-log
  question.
- I did not run any tests. The PostgreSQL behaviour of MATCH SIMPLE on the optional composite FK to ingress is standard
  and matches SQLite, but it has not been measured here.

## Caveats & Required Follow-ups

1. John rules on one identity per send: `operation_id` is the ingress key, and the ingress receipt is the authority.
2. John rules on the event log against minimal forensic retention (ADR-046).
3. John rules on a head change while a job is queued (T05, D9-adjacent).
4. Re-baseline the plan against the current tip before T02 starts.
5. Write the B′-final ADR with the rejected alternatives and the D re-test trigger.
6. Land the golden-vector test before the codec move.
