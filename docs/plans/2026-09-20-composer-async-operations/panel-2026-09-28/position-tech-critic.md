# Position: tech-selection critic (red-team the likely winner)

Critic: tech-selection-critic. Tree: `release/0.8.1` @ `6506f6a7f`, main checkout, read-only.
Every citation below comes from a read of that tree. Nothing was executed.

## Summary (machine-readable)

- verdict: REVISIT (B as the brief states it); KEEP the separate table
- confidence: MEDIUM
- primary_weakness: B's "reuse `operation_receipt_request_hash`" can't be built without changing the receipts module's public contract. All four options also miss a third client-minted-id facility on the same route (`message_ingress_receipts`).
- alternative_recommended: **B′**, a separate `composer_async_operations` table plus a request-binding codec lifted to a neutral home. The send path has exactly ONE client-minted id, shared with `message_ingress_receipts`.

## The claim under attack

The brief expects B: a dedicated `composer_async_operations` table (contract.md § Table, lines 174-204) that reuses `operation_receipt_request_hash` instead of a duplicate codec. The case for B rests on four points:
- receipts store a locator, but compose must store the full `MessageWithStateResponse`;
- compose has a queued phase, a claim and a running fence that is never taken over;
- sharing the codec avoids tech debt;
- a new table stays out of the way of fork/revert, which just landed.

## Finding 0: every option misses a facility on the same route

`message_ingress_receipts` (`src/elspeth/web/sessions/models.py:464-488`) landed in `8630db9b8` on 2026-09-27 at epoch 69 (`schema.py:40-41`), two days after the plan's 09-25 contract. It holds immutable acceptance records with no-update and no-delete triggers (`schema.py:84-86,104-105`), keyed `(session_id, client_request_id)`. It binds the client key to one route-owned user row and to the original `requested_state_id`.

`SendMessageRequest` already carries a required `client_request_id: UUID` (`schemas.py:154`). The route looks up ingress under the COMPOSE SOL before it does any work (`routes/messages.py:167-175`). The user-row insert re-checks it atomically (`service.py:4539-4718`). A duplicate returns 409 `message_already_accepted` or `message_idempotency_conflict` (`routes/messages.py:111-125`).

I ran `grep -rn "client_request_id\|ingress_receipt\|message_ingress"` over the spec and the whole plan folder. It returns nothing.

Consequences for every option, A to D:
- **Two client-minted ids on one body.** The contract adds a required `operation_id` to `SendMessageRequest` (contract.md:158). With `client_request_id` still there, the send body carries two client-minted ids that both claim to identify "this user action". That is the dual acceptance John forbids. The two ids can also disagree: same `operation_id` with a new `client_request_id` is a 202 replay at admission and then a fresh ingress at the worker. The reverse case is a 409 conflict at admission, or at the worker when the ingress key is reused.
- **Duplicated binding.** The job's `user_message_id` column (contract.md:182) records the same fact as `message_ingress_receipts.user_message_id`.
- **Two dedup points for one send.** Admission is pre-202 and holds no SOL. It can't call `lookup_message_ingress`, which requires a COMPOSE `session_operation_context` (`service.py:4541-4570`). So under the plan as written, one send is deduplicated at admission (job PK) and again at the worker's user-row insert (ingress PK). The two carry different semantics: 202 replay versus 409.

**Resolution (my recommendation):** use one id.
- The job's `operation_id` is the ingress `client_request_id`. Pick one wire name and rename it everywhere. The project is pre-release, so there is no alias and no transitional field.
- Keep `message_ingress_receipts` as the immutable acceptance record. The worker writes it in the same transaction as the user row, keyed by the job's `operation_id`.
- Its conflict arm stops being a client-facing 409. The job's `request_hash` already bound content and `state_id` at admission, so a mismatch at the worker is a Tier-1 `AuditIntegrityError`. The `message_already_accepted` 409 retires, because a replay is now a 202 with status.

The alternative is to fold ingress into the job row and delete the table. I reject it under ADR-046. The ingress row is immutable from insert. The job row is mutable until terminal: `user_message_id` is NULL while queued (contract.md:25) and written under the running fence. Folding would weaken the immutability of the "this client request produced this user row" binding. The only fix would be a second no-overwrite trigger on one column, which re-creates the dedicated table's guarantee with more machinery.

Whichever way John rules, T02, T03 and T10 cannot be executed as written until this is decided.

## Attack on B as stated

### B1. "Reuse `operation_receipt_request_hash`" is not a no-touch reuse

`operation_receipts.py:39-53`:
- **Kind type.** `kind: OperationReceiptKind` is `Literal["session_fork", "state_revert"]` (`protocol.py:264`). Passing `compose_message` means widening that Literal, which also widens every receipt function's accepted kind in the type checker. The alternatives are `str` or a union, and either one changes the receipts contract.
- **Field name.** It hard-requires a field literally named `operation_id` (L44) and excludes that name (L48). Today's send id is named `client_request_id` (Finding 0).
- **Strictness.** It requires `strict=True, extra="forbid"` (L42). Today's `SendMessageRequest` and `RecomposeRequest` derive from `_RequestModel`, which is non-strict (`schemas.py:66-69,143,162`). T10 makes send strict, but recompose isn't strict today.
- **Domain tag.** It bakes in `"session-operation-receipt-request.v1"` (L36). A compose request hash carrying the receipts facility's tag is a naming lie. Any change to the receipt tag's version would also move compose hashes.

So B, done honestly, is one of two things. The first is C, an own codec. The second is a lift: move the normalisation policy (strict/forbid assertion, exclude the id field, materialise defaults and None, bind schema + session + kind) into a mode-neutral function and re-point receipts at it. That means `schema` is a parameter, `id_field` is a parameter and `kind` is a `str` validated by the caller. The brief's "reuse the helper and change nothing else" doesn't exist.

### B2. The saving is smaller than it looks

The real primitives, `stable_hash` and `is_lower_sha256_hex`, are already shared from `elspeth.contracts.hashing` (`operation_receipts.py:15`). The receipt wrapper is about 12 lines of policy. The case for sharing it isn't line count. It is that there should be one definition of "same request" across the two client-minted-id facilities in the session store. If the two definitions drift, "exclude_defaults" can differ between them, and so can what a retry is. That is a real but modest benefit. It argues for the lift, not for importing a function whose module docstring says "for fork/revert" (`operation_receipts.py:1`).

The spec in the working tree still says, at spec §2: "the existing `guided_operation_request_hash` is the model to follow, not a function to share". That function is deleted. The sentence's intent is C. Choosing B′ means amending spec §2 in the same task (T02), not only the plan.

### B3. B has no event log, and at terminal it no longer records which instance ran the turn

Receipts have an append-only companion with a `terminal_hash` (`models.py:825-877`), which `_validate_events` checks (`operation_receipts.py:170-200`). The contract's job table has no events table. Its status bundles null the claim triple on `completed` and `failed` (contract.md:195-196). After terminal, therefore, the row no longer records:
- which instance claimed or ran the turn;
- how many queued reclaims happened, other than the `attempt` count;
- who requested cancel, and whether the terminal was reached by the owner, the reaper (`settle_lost`), `settle_own_lapsed` or `settle_lost_inactive_session`.

The actual audit record of the turn (chat rows, `llm_calls`, the audit cohort) lives elsewhere and is not lost. So this is not automatically an ADR-046 violation. It is an unpriced decision, though. Compose is the more expensive and more failure-prone operation, yet it gets less custody history than fork/revert. **B must answer it explicitly.** The answer is either an "operational state only" ruling written into the table comment, or a `composer_async_operation_events` sibling in T03. That answer is a T03 consequence. It is not a reason to pick D.

### B4. No cross-facility "one nonterminal operation per session" invariant

With two tables, no single partial unique index can say "a session has at most one nonterminal client operation". Sequence:
1. A send is admitted and queued. It holds no SOL, and `user_message_id` is NULL.
2. A `state_revert` acquires COMPOSE and commits a new head. Revert uses `SessionOperationKind.COMPOSE` (`routes/operation_receipts.py:241`).
3. The worker starts the send.

The send then composes against a head the user did not see when they pressed Send. The `state_id` it carries is provenance only (`routes/messages.py:191-218` seeds the loop from the actual head). The live synchronous route behaves the same way today, so this is not a regression and not decisive for A over B. The plan should still state it, and T04/T05 should decide whether the start composite should refuse or record a head change since admission. A would not fix this for free anyway, because receipts are synchronous and hold SOL, not a queued slot.

### B5. B's gate cost is real, but it is paid once

A new table means a `TablePolicy` (`test_session_db_mutation_authority.py`), the digest inventory, PG CHECK reflection, the trigger in 5 places and an epoch cut. A pays nearly the same cost. It still bumps the epoch, and it rewrites every CHECK on an existing table into kind×status disjunctions, which carries more PG reflection risk. So the gate cost does not favour A.

## Attack on A

**The locator argument is true but not decisive.** `MessageWithStateResponse` carries `proposals` (`schemas.py:243`). `_state_response(..., live_validation=)` builds `validation_warnings` and `validation_suggestions` only from the live summary (`routes/_helpers.py:878-920`), and nothing persists them. So a locator replay can't rebuild the response. That kills reuse of receipts' **replay model**: `_outcome` rebuilds from a locator (`operation_receipts.py:221-234`) and routes re-hash a rebuilt DTO (`routes/operation_receipts.py:95-105`). It does not kill the **table**, because A could add a nullable `result_json`.

**What does kill A:**
- **(a) Fail-open takeover coupling.** `reserve_operation_receipt` takes over any expired `in_progress` row unconditionally: new token, `attempt+1`, a `taken_over` event (`operation_receipts.py:316-353`). A compose row in that table needs a kind guard on exactly that arm. The spec says an expired `running` row is never taken over (spec §1, the "two distinct lease states" paragraph). A single missed guard means a second provider turn: a double assistant row and double LLM spend. That is the wrong failure direction for a shared facility.
- **(b) Two fence models in one row shape.** Receipts carry their own 300 s lease (`routes/operation_receipts.py:39`) under an SOL they already hold (`:242-248`). R3 binds `running` to the SOL with no separate expiry and a separate queued-claim token. One `lease_token` column can't mean both, so A needs a second token column and kind-conditional semantics for the first.
- **(c) Different status vocabularies.** `in_progress` versus `queued`/`running`. `ck_session_operation_receipts_status_bundle` (`models.py:796-801`), `ck_..._result_locator` (`:803-808`), `_validate_row` (`operation_receipts.py:93-148`), `settle_operation_receipt` and the events bundle CHECK (`models.py:870-876`) would all become kind×status disjunctions.
- **(d) Hard-coded kinds.** `_validate_identity` hard-codes the two kinds (`operation_receipts.py:68`), so A rewrites the identity core of a facility that is a day old.

A's only real win is the event log (B3), and B can buy that by adding a sibling table.

## Attack on D

D extracts a generic "client-minted session operation" core now. There are two consumers, and their fence models differ (own lease with takeover, versus queued claim followed by SOL-bound running that is never taken over). The genuinely shared part is identity, the request-binding codec and terminal immutability. B′ already shares the codec, and immutability is a trigger pattern, not code. D would reopen fork/revert, which landed at epoch 71 (`schema.py:43-44`), for no behaviour gain. "Removing debt" is not a mandate to build abstractions ahead of a third consumer. Reject D for now. The re-test trigger is a third client-minted-id operation kind that needs queued or async semantics.

## Attack on C

C isn't wrong, and it is what the spec text says. It leaves two definitions of "same request" that byte-for-byte copies are meant to keep in lockstep, and nothing enforces that. Under "no tech debt" that is a standing drift hazard. B′ removes it at the cost of about 20 lines of churn in a module that isn't a writer, so the AST writer manifest is untouched. If John weighs "don't touch day-old receipts code" above the drift hazard, C is the acceptable fallback. It must not be relabelled as B.

## Which option survives

**B′: separate `composer_async_operations` table, with a request-binding codec lifted to a neutral module, and one client-minted id per send.**

1. Separate table. This is kept for the takeover and fence reasons (a)-(d), not mainly for the locator reason.
2. Lift the hash policy into a mode-neutral function that takes `schema`, `session_id`, `kind: str` and `request`, with the id field name as a parameter or fixed as `operation_id`. Receipts and compose each keep **their own schema tag and kind vocabulary**. Only the normalisation policy is shared. The home is a new small module, not `operation_receipts.py`, so neither facility imports the other.
3. Use one client-minted id on the send body, shared with `message_ingress_receipts` (Finding 0). Ingress stays as the immutable acceptance record the worker writes, and its 409s retire into the job's 202 replay.
4. B3 needs an explicit answer: an events sibling, or a written "operational-only" ruling.

## Plan consequences (T02-T06)

- **T02 (types and codec).**
  - Replace `composer_operation_request_hash` with a call to the lifted neutral codec (tag `composer-operation-request.v1`). Re-point `operation_receipt_request_hash` at it in the same commit, with byte-identity tests on existing receipt hashes.
  - Settle the id: rename `client_request_id` or `operation_id` so the send body has one. Rebase the DTOs onto `_SessionOperationRequest` (`schemas.py:72`); `_GuidedOperationRequest` no longer exists.
  - `RecomposeRequest` already exists with `expected_user_message_id` (`schemas.py:162-165`), so it is a change to an existing class, not a new one.
  - Amend spec §2's codec sentence.
- **T03 (table).**
  - Decide `user_message_id` against ingress. The recommendation is to keep the column as an FK and keep ingress as the immutable record. Add a CHECK or an authority invariant that, once running, `user_message_id` equals the ingress row for `(session_id, operation_id)`.
  - Add a `composer_async_operation_events` sibling with the same trigger pair as receipt events, or record the "operational-only" ruling in the table comment.
  - The epoch is the next free one after 71, not 68.
- **T04 (authority).**
  - `admit` dedupes by job PK only. The worker's ingress insert conflict becomes Tier-1, not a 409.
  - The mutation-authority `TablePolicy` for any events sibling needs a named authority.
  - Document the cross-facility gap (B4) in the authority docstring.
- **T05 (composite start).** Decide whether a head change since admission (revert or fork) is refused or recorded at start. B4 applies.
- **T06 (composite terminal).**
  - The user row and the ingress receipt are written in the worker's own transaction. That must be the same transaction that sets the job's `user_message_id`, or T06's composite has to re-verify the binding.
  - The `message_already_accepted` and `message_idempotency_conflict` error types and their SPA recovery (`sessionStore.ts` client_request_id matching, ~L676-855) are deleted in T13/T14. No dual recovery path.

## Where the claim holds up

- A separate table is right. The takeover arm (`operation_receipts.py:316-353`) and the SOL-bound, never-taken-over running fence are incompatible in one row shape.
- The full-DTO requirement is real (`schemas.py:243`, `_helpers.py:878-920`).
- The gate cost is roughly equal for A and B, so it doesn't penalise B.

## Confidence Assessment

MEDIUM.
- The receipts code, schema, DTO bases and ingress-receipt path were read directly and are cited above.
- I did not read the uncommitted 72+/54− diff on the spec, the T02-T06 task files line by line, or the other panel positions.
- No test or instrument was run, so claims such as "the lift leaves the AST writer manifest untouched" are inferences from the module being hash-only, not measured.
- The ingress finding is high confidence as a fact. It is medium confidence as to its best resolution, which is an owner ruling.

## Risk Assessment

- **If B′ is accepted:**
  - The lift touches day-old receipts code. A byte-identity test on existing receipt request hashes is the guard, and an epoch cut makes stored-hash drift harmless anyway.
  - Keeping ingress plus the job means two tables record the user-row binding. If T06 doesn't write them in one transaction, they can disagree after a crash.
- **If B is accepted as stated:** someone widens `OperationReceiptKind` to make it compile. After that, `reserve_operation_receipt` type-checks with compose kinds and only the runtime `_validate_identity` stands between a compose row and the takeover arm.
- **If Finding 0 is ignored:** the shipped send body carries two idempotency keys, with a 202 replay at one layer and a 409 at the other. A client retry after a lost 202 reproduces that disagreement.
- **Re-test trigger for D:** a third client-minted-id operation that needs queued or async semantics.

## Information Gaps

- I haven't read the uncommitted spec diff (`git diff --stat` shows 72+/54−), so it may already address ingress or the codec.
- I haven't checked whether the SPA's `client_request_id` recovery in `sessionStore.ts` (~L676-855) is load-bearing for the tutorial Build path (`TutorialFreeformShell.tsx:112`).
- I haven't verified whether `trg_message_ingress_receipts_no_delete` interacts with the job table's cascade on session delete.
- I didn't measure how T13's 131+ test sites construct `SendMessageRequest`, which sets the churn of the id rename.

## Caveats

- This critique covers only the table and codec choice and its interaction with existing id facilities.
- It does not re-open R0, R2, R3 or the freeform-only scope.
- No cost modelling.
- It assumes the spec's lease semantics (queued reclaim, running never taken over) are themselves correct.

## Fact corrections to the brief and plan

1. The brief omits `message_ingress_receipts` (`models.py:464`, epoch 69, `8630db9b8`). `SendMessageRequest` already has `client_request_id: UUID` (`schemas.py:154`). The spec and plan don't mention either.
2. `contract.md:158,161` use `_GuidedOperationRequest` as the DTO base. That class is gone; the live strict base is `_SessionOperationRequest` (`schemas.py:72`).
3. `contract.md:161` calls `RecomposeRequest` "NEW, only operation_id". It exists, is non-strict, and carries `expected_user_message_id` (`schemas.py:162-165`).
4. `contract.md:39,471` say "epoch 68". The tree is at 71 (`models.py:59`, `schema.py:45`). The brief is right.
5. Receipts `operation_id` is bounded to 1..128 characters (`models.py:763`, `operation_receipts.py:66`), not a canonical 36-character UUID. The DTO layer enforces 36 (`schemas.py:77-88`), but the table does not. That differs from the compose contract's length-36 CHECK.
6. Spec §2 cites `guided_operation_request_hash` as the model. It was deleted in `7001600fe`.
7. The brief's B wording ("reuse `operation_receipt_request_hash`") is not implementable without changing that function's signature (`kind: OperationReceiptKind`, the fixed `operation_id` field, the strict requirement, the fixed tag).
