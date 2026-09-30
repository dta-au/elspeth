# Panel position: solution architecture

- **Panellist:** solution architect (forward-design reviewer lens)
- **Tree:** `release/0.8.1` @ `6506f6a7f`, main checkout, read-only
- **Position:** **B**, amended (call it **B′**). Use a separate `composer_async_operations` table and one shared
  request codec. Before T02 starts, also reconcile the send route's existing `client_request_id`, which the brief
  does not mention.
- **Confidence:** high on "not A" and "not D". Moderate-high on B over C. Moderate on the exact
  `client_request_id` reconciliation, which needs an owner ruling.

## 1. What I checked

I read each item myself and did not take the brief's word for it:

- the brief;
- the spec, all of it;
- `contract.md`, all of it;
- `src/elspeth/web/sessions/operation_receipts.py` (527 lines);
- the receipts and receipt-events tables (`models.py:724-878`);
- the `session_operation_fences` table (`models.py:325-352`);
- the receipt callers: `service.py:170-175,1045-1230`, `coordination/repository.py:714-770,3015-3030`,
  `blobs/service.py:487-495`, `routes/operation_receipts.py`, `routes/sessions.py:900-1210` and
  `routes/composer/state.py:683-790`;
- the table policy (`tests/unit/architecture/test_session_db_mutation_authority.py:188-194`);
- the trigger inventory (`schema.py:80-112`);
- the request DTOs (`schemas.py:72-165`);
- the live send route (`routes/messages.py:111-260`);
- the 09-27 boundary-repair plan (`docs/plans/2026-09-27-composer-system-boundary-repair.md:30,65-71`);
- the provenance of commits `7001600fe` and `8630db9b8`.

## 2. Findings the brief and plan miss

These change the plan whichever option is chosen.

### F1. The send route already has a client-minted idempotency identity

The brief's facts section does not list this, and no file under the plan folder mentions it.

- **Evidence:**
  - `SendMessageRequest.client_request_id: UUID` (`schemas.py:154`).
  - The immutable `message_ingress_receipts` table, keyed `(session_id, client_request_id)` and bound to the user row
    (`models.py:464-487`). It has `no_update`/`no_delete` triggers in the required set (`schema.py:84-86,103-104`).
  - The route looks up the receipt before the insert and returns `409 message_already_accepted` or
    `message_idempotency_conflict` (`routes/messages.py:111-125,174-181,229-246`).
  - It was added in epoch 69 by `8630db9b8` on 2026-09-27 (`schema.py:40-41`), two days after the async plan was
    frozen.
  - Recompose also changed. `RecomposeRequest` now carries `expected_user_message_id` (`schemas.py:162-165`). The
    contract says `RecomposeRequest` has "only `operation_id`" (`contract.md:161`).
- **Measured:** `grep -rln "client_request_id\|message_ingress" docs/plans/2026-09-20-composer-async-operations/`
  returns nothing.
- **Consequence:** as written, T02/T10 would give `POST /messages` two client-minted identities, `client_request_id` and
  `operation_id`. Each would have its own table and its own replay semantics: the ingress receipt answers 409 to an
  exact duplicate, and the job answers 202 plus replay. John's doctrine is "no dual acceptance, no old pathways", and
  two idempotency keys for one user action is exactly that. **This is a bigger integration gap than the choice
  between A, B, C and D.**

### F2. The plan's other anchors have also gone stale

- **Removed from the tree but still cited:**
  - `_GuidedOperationRequest` and `guided_operation_request_hash` are gone. The live base class is
    `_SessionOperationRequest` (`schemas.py:72-88`). T02 still imports the guided codec (`T02.md:225,546`) and still
    runs `tests/unit/web/sessions/test_guided_operation_requests.py`, which no longer exists.
  - T03's insertion anchor is "after `guided_operation_events_table` :1279" (`T03.md:22,689`). That table is gone.
  - T06 cites `require_guided_operation_authority_on_connection` and the `guided_operations` blob write fence
    (`T06.md:64,124`). Neither exists.
- **Wrong values:**
  - The epoch bump says 68 (`contract.md:39`). The tree is at 71, so the next value is 72.
  - The spec §1 still says "`guided_operations` is untouched" and §3 keeps three synchronous guided routes. Only two
    `_track_compose_inflight` mounts remain (`routes/messages.py:142`, `routes/composer/compose.py:102`).

### F3. The receipts table is not what excludes concurrent operations

The COMPOSE vs fork/revert exclusion comes from the one-row-per-session `session_operation_fences` table
(`models.py:320-352`), acquired through `SessionOperationLease`. Fork and revert take their own receipt lease *and*
a `SessionOperationLease` (`routes/operation_receipts.py`: `OperationReceiptLease` carries both). Moving compose into
receipts therefore gains no mutual exclusion that the fence does not already provide.

## 3. The options against the canonical failure modes

### A. Extend receipts: rejected

This option runs straight into **integration reality gap** and **coupling**. The receipts module does not store
rows under a neutral schema; it encodes the fork/revert semantics directly into its core. Each point below would
have to become a kind-conditional branch:

1. **Takeover is the core CAS.** `reserve_operation_receipt` takes over an expired `in_progress` row and bumps
   `attempt` (`operation_receipts.py:318-353`). Compose's safety property is the opposite: a `running` row is
   **never** taken over and there is no provider replay (spec §1, §4). Under A, "no provider replay" becomes an
   `if kind in COMPOSE_KINDS` inside the one function that decides whether a second turn can start. A safety invariant
   held by a kind switch in shared code is the weakest form it can take.
2. **Replay is rebuild-and-compare, not store-and-return.** A receipt stores locators and `response_hash`. Replay
   rebuilds the DTO from live state and compares hashes (`routes/operation_receipts.py` `_replay_verified`;
   `operation_receipts.py:56-62,221-234`). Compose must store the full response, because `proposals` is read after
   commit and `live_validation` is never persisted (spec §1). The two terminal models have different evidence
   semantics.
3. **Hard-coded invariants would all have to widen.** These include:
   - `_validate_identity` rejects any kind other than fork or revert (`:68`);
   - `_validate_row`'s status/locator arms (`:111-148`);
   - `_outcome`'s `else` branch treats any non-fork kind as revert (`:228-233`);
   - `_terminal_hash`'s field set (`:151-167`);
   - the status-bundle and result-locator CHECKs (`models.py:797-808`);
   - the failure-code CHECK. The two code sets are almost disjoint: receipts use `stale_conflict`, `integrity_error`,
     `custody_error` and `quota_exceeded`; compose uses `http_error`, `worker_lost` and `deadline_expired` (D13). Both
     share only `operation_failed` and `request_cancelled`.
   - The event-kind CHECK (`models.py:847-849`) has no queued, claim-released, cancel-requested or started events.

   The result is a per-kind CHECK arm matrix. Every arm would have to pass the PostgreSQL reflection lesson (AND/OR
   arms, no one-element IN).
4. **Blast radius on the live archive decision.** `decide_and_soft_archive` refuses to archive while *any* receipt is
   `in_progress`, and raises `AuditIntegrityError` for an unknown kind (`coordination/repository.py:724-735`). Under
   A, an in-flight compose job either blocks archive, which contradicts plan D7 "No archive refusal", or trips a Tier-1
   integrity error. The `durable_history_exists` probe (`:747-770`) would also need a compose-specific decision. That
   is a behaviour change to fork/archive, which shipped today (`7001600fe`, 2026-09-28 09:24, already on
   `origin/release/0.8.1`).
5. **Ownership.** One `TablePolicy` owner, `SessionOperationReceiptAuthority`, would gain worker-claim,
   start-composite and terminal-composite co-writers. With several developers, the fork/revert maintainer and the
   composer-async maintainer would both hold the same CHECK, trigger and validator. Every change to either feature
   would re-run the other's PostgreSQL race suite.

A **does not reduce the whole-tree gate cost** either. Widening a table still costs a digest inventory update, CHECK
reflection, trigger inventory and an epoch cut, the same as adding one. It also puts a live facility's tests in the
blast radius.

### D. Generalise into a shared core: rejected

This option is **gold-plating and premature generalisation**. With F1 there are now *three* client-minted session
identities: receipts, ingress receipts and the compose job. Each has a different retry rule:

| Facility | Retry rule |
|---|---|
| Receipts | take over and resume |
| Ingress receipts | reject an exact duplicate with 409 |
| Compose job | replay the stored terminal; never resume `running` |

Only two things are genuinely common: the identity validation and the request-hash binding, and both are pure
functions. The parts D names beyond those do not generalise:

- **Terminal replay** means rebuild for receipts and store for compose.
- **Event log:** the compose table has none in the plan, which is acceptable because the audit truth for a turn is
  the chat-row and LLM-call cohort, not the transport row.

A shared "core" would be an abstract base whose subclasses override every method that matters. As an ADR, D would
have to name a requirement it serves, and the only candidate is "less code". That does not survive ADR-046, where an
audit-grade invariant expressed through a polymorphic hook is harder to review than one expressed directly. D also
maximises blast radius: it refactors a facility that shipped today, in the same change that introduces the new one.

### C. Separate table with its own codec: rejected, narrowly

C's recorded rationale was "the existing `guided_operation_request_hash` is the model to follow, not a function to
share" (spec §2; `contract.md:117-118`, "Never imports the guided codec"). That was correct on 09-25, when the only
sibling was a codec on a mode being deleted. The rationale has since **expired**. The live sibling,
`operation_receipt_request_hash` (`operation_receipts.py:39-53`), is mode-neutral by its own docstring. It already
implements the exact contract C would clone:

- a strict and extra-forbid check;
- `operation_id` must be present and is excluded from the hash;
- defaults and `None` are materialised;
- `{schema, session_id, kind, request}` goes through `stable_hash`.

The DTO base it assumes, `_SessionOperationRequest`, is the class compose's DTOs should now derive from. Keeping two
byte-for-byte sibling codecs on an audit-grade binding creates drift risk: a hardening fix lands in one and not the
other. That is the "weak decision record" failure mode, where a decision outlives its reason.

### B′. Separate table with a shared request codec: recommended

- **Cohesion.** `composer_async_operations` is a durable async queue plus a result store with a two-phase lease. Its
  queued claim is reclaimable, and `running` is bound to the COMPOSE SOL (R3) and never taken over. The receipts
  table is a synchronous idempotency ledger that replays by locator. Separate tables give each responsibility one
  owner, one `TablePolicy`, one terminal-immutable trigger and one set of CHECK bundles that read cleanly per status
  with no per-kind arms.
- **Coupling.** Share only what is identical by construction: the request-binding codec. Keep each facility's schema
  string, so each versions independently. Do not share the response hash. Receipts hash a *rebuilt* DTO; compose
  hashes stored canonical JSON (`composer_operation_result_hash`) and re-verifies it on read. The two verification
  points differ.
- **Blast radius.** Receipts behaviour stays byte-identical. The only receipts-side change is to move the codec body,
  and a golden vector pins it.
- **Reads cleanly as an ADR.** Context: sync replay-by-locator vs async store-and-return. Decision: separate
  table and shared codec. Rejected: A (safety invariant would become a kind switch, and the archive would regress), C
  (its rationale expired), D (no second consumer for anything beyond the codec). Reversibility: moderate, because
  merging tables later is an epoch cut; that is acceptable pre-release.

## 4. The amendment: one identity per user action

The owner must decide this before T02. My recommendation: **the compose job's `operation_id` *is* the send's
client-minted key.**

- **Send.** Drop the separate `client_request_id` field. The async `SendMessageRequest` derives from
  `_SessionOperationRequest` with `operation_id`, `content` and `state_id`. Alternatively, keep the wire name
  `client_request_id` and have the job key on it; either way there must be exactly one field.
- **The worker's user-row insert** writes the `message_ingress_receipts` row keyed by that same id, in the same
  transaction as the user row. The ingress receipt stays as the immutable user-row binding: the acceptance key
  exposed on message projections (`schemas.py:216`) that the frontend reconciles on (`sessionStore.ts:662-680`).
  Choose **one** authority for "operation → user row". Either the job's `user_message_id` column goes, or the
  ingress receipt stops being written for async sends. Do not keep both.
- **The ingress receipt's 409 lookup** (`routes/messages.py:174-181`) becomes unreachable for `/messages` once the job
  row decides replay. Delete it with the cutover; do not keep it as a dual acceptance path.
- **Recompose.** `RecomposeRequest` carries `operation_id` plus `expected_user_message_id`, and the request hash binds
  both. The `expected_user_message_id` mismatch (`routes/composer/compose.py:158`) is a worker check that yields a
  terminal 409 envelope, consistent with spec §3's "checks that can change while waiting".

If John prefers to keep the ingress receipt's separate key (for example, because it predates async and he wants
the two phases distinct), the ADR must say why two client identities per send are not dual acceptance. I do not
think that case can be made.

## 5. Consequences for T02-T06

- **T02 (types and codec):**
  - Extract the body of `operation_receipt_request_hash` into a kind-agnostic function in a neutral module, for
    example `sessions/operation_request_codec.py`. Its signature would be
    `session_operation_request_hash(*, schema: str, session_id, kind: str, request)`.
  - The receipts wrapper keeps `session-operation-receipt-request.v1`. Compose passes `composer-operation-request.v1`.
  - Add a golden-vector test proving fork/revert hashes are byte-identical before and after.
  - Remove every guided-codec import and test reference (`T02.md:58,225,471,546`).
  - Rebase the DTOs on `_SessionOperationRequest`.
  - Apply the §4 identity decision.
  - Re-measure `COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH` (F-m1) for the new DTO shapes, because recompose gains a
    field.
- **T03 (schema):**
  - Keep the dedicated table.
  - Move the anchor next to `session_operation_receipt_events_table` (`models.py:825-878`).
  - Delete the "guided_operations is untouched" comment.
  - The trigger inventory now has 11 required triggers including the receipts triggers (`schema.py:96-112`). Add
    `trg_composer_async_operations_terminal_immutable` in all five places.
  - The epoch becomes 72, not 68.
  - Decide explicitly, and state in the table comment, that a compose job:
    - never blocks archive (D7);
    - is not "durable history" in `decide_and_soft_archive` (`repository.py:747-770`), because its chat rows already
      are.
  - Resolve the duplication between `user_message_id` and the ingress receipt (§4).
- **T04 (authority):**
  - Keep a separate `ComposerAsyncOperationAuthority` and a separate `TablePolicy`.
  - Its D8 active-operation check reads only its own table. Exclusion against fork/revert stays with the SOL (F3).
  - Add a test showing that a queued compose job does not block a fork, and that fork/revert does not see compose
    rows.
- **T05 (composite start):**
  - The refactor of `acquire` into `_advance_exclusive_fence_on_connection` sits under the receipts' own SOL
    acquisition (`OperationReceiptLease` holds a `SessionOperationLease`). Add the fork/revert receipt route tests
    (`test_operation_receipts.py`, `test_operation_receipts_postgres.py`) to T05's regression set.
- **T06 (composite terminal):**
  - The F-B2/C2 cancel predicate in `_require_session_operation_context_on_connection` fires only when a composer
    `running` row is bound to the exact fence triple. Add a negative control showing that fork/revert writes under
    their own SOL are unaffected.
  - Re-measure the call sites. The guided callers T06 lists (`T06.md:64,124`) are gone.
- **Adjacent tasks:**
  - **T07:** the guided mounts are gone. After the T13 cutover, `_track_compose_inflight` has zero mounts, so
    *delete* it rather than extract-and-keep.
  - **D11/T01:** re-derive the list of synchronous consumers. It is now the tutorial run wait
    (`tutorial_service.py:444`), `explain_run_diagnostics` and proposal settlement. There are no guided routes.

## 6. The strongest argument against my position

The receipts lease *is* half of compose's state machine. A claimed `queued` row is reclaimable after expiry, and
receipts implement exactly that as `taken_over` plus an attempt bump plus an append-only event. Under B′ the tree
then holds two lease/attempt/CAS implementations over session-scoped client ids. They differ only in what happens
after `running`. A future developer fixing a lease-renewal race in one will not know to fix the other.

That is a real duplication cost, and it is the best case for A or D. I still reject both for three reasons:

1. The shared half is the *unsafe-to-share* half. Takeover is correct for receipts and forbidden for a running
   compose job, and a shared implementation would hold that difference as a flag.
2. The compose claim is deliberately thin: the running fence is the SOL (R3), so the job has no running lease of its
   own to duplicate.
3. The duplication can be mitigated with a cross-reference note in both modules and a shared test vocabulary, which
   costs far less than a merged state machine.

## 7. Facts in the brief I found wrong or incomplete

- **Incomplete (the material one):** the brief does not mention `message_ingress_receipts` or
  `SendMessageRequest.client_request_id` (`models.py:464-487`; `schemas.py:154`; epoch 69, `8630db9b8`), nor
  `RecomposeRequest.expected_user_message_id` (`schemas.py:162-165`). See F1.
- **Minor:**
  - `schema.py:45` is `_COORDINATION_HARD_CUT_EPOCH = 71`. `SESSION_SCHEMA_EPOCH` is defined at `models.py:59` and
    imported at `schema.py:27`. Both values are 71, so the claim holds.
  - `TutorialFreeformShell.tsx:112` calls `composer.sendMessage`, the `useComposer` hook, which wraps
    `sessionStore.sendMessage` (`hooks/useComposer.ts:10,20`). The path is as described.
  - The "terminal-immutability trigger inventory (5 places)" is right in shape, but the required set is now 11
    triggers, including three receipts triggers (`schema.py:96-112`).
- **Confirmed:**
  - The receipts table shape, PK, kind, status and absent columns (`models.py:727-823`).
  - The events table (`:825-878`).
  - Introduction by `7001600fe` (`git log --diff-filter=A`), which is on `origin/release/0.8.1`.
  - The callers, including `blobs/service.py:487-495`.
  - `OperationReceiptTakenOver` in the outcome union (`protocol.py:315,344`).

## Confidence, risk, gaps and caveats

- **Confidence.**
  - High that A is wrong. The evidence is direct: `operation_receipts.py:68,111-148,228-233,318-353` and
    `repository.py:724-735`.
  - High that D is gold-plating, reasoning from the three divergent retry rules.
  - Moderate-high that B beats C. This is a judgement about drift risk against coupling a composer module to a
    sessions codec module; the neutral-module extraction removes most of that coupling.
  - Moderate on the §4 shape. The direction ("one identity") is high confidence; which table owns the user-row
    binding is the owner's call.
- **Risk.**
  - Implementation risk for B′: medium. It is the planned work plus a small codec extraction plus the F1
    reconciliation.
  - Reversibility: moderate, since any table merge or split later is one pre-release epoch cut.
  - The main risk is proceeding on the stale plan. T02/T03/T06 would fail on their first command because the imports
    and anchors are gone.
- **Information gaps.**
  - I did not read the other panel positions, so this position stayed independent.
  - I did not run tests.
  - I did not measure whether the frontend's post-09-27 ingress reconciliation (`sessionStore.ts` +287 lines in
    `8630db9b8`) overlaps the planned T14 custody store. It probably does, and the overlap should be measured before
    T14.
- **Caveats and follow-ups, in order:**
  1. John rules on §4, one identity per send.
  2. Re-baseline the plan against `6506f6a7f`: T00 re-measure plus a stale-anchor sweep of T02-T07.
  3. Write the ADR for B′ with the rejected alternatives above.
  4. Add a golden-vector test before the codec move.
