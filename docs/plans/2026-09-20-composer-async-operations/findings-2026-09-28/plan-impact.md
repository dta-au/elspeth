# Plan-impact map: re-basing the composer async operations plan (2026-09-28)

Seam: PLAN-IMPACT MAPPER. This is a read-only survey, and this file is its only write. It supersedes the earlier
draft at this path (same seam, 19:17 the same day). That draft was re-verified claim by claim. Its corrections and
additions are marked **[rev]**.

- **Tree.** `release/0.8.1`, pinned tip `1effedab2`. `git rev-parse HEAD` printed
  `1effedab2e0af7e09a5e8b30c66bc46ceaa130d5`. HEAD did not move, so no anchor needs a `1effedab2..HEAD` note.
- **Binding inputs.** `panel-2026-09-28/RULINGS.md` (rulings 1–5; ruling 5 endorsed) and
  `panel-2026-09-28/RECOMMENDATION.md` (B′). Guided was removed in `7001600fe`.
  `git merge-base --is-ancestor 7001600fe HEAD` exits 0.
- **Read in full.** The plan index, `contract.md`, the spec, `review/SYNTHESIS.md`, `review/codex.md`,
  `review/VERIFY.md` §§ stale/owner, `review/VERIFY-2.md` §§ verdicts/stale, `RULINGS.md` and `RECOMMENDATION.md`.
  For every task T00–T18, the `## Review-pass changes` tail was read in full.
- **Read in part.** For every task, the header, the Files/Interfaces/Gates blocks and the step list (every
  `### Task` / `- [ ] **Step` line). Targeted sections were also read: T04 `_claim_candidates` / `list_expired_queued` /
  `list_expired_running` (`T04.md:2240-2625`), T10 Step 7 and the user-row binding writer (`T10.md:578-600`, `:880-965`,
  `:1760-1775`), and T00 Appendix A headings. The remaining bodies of T02–T18 (the test code blocks) were **not**
  read line by line. Per-task verdicts rest on the interface blocks, the step lists and the review tails.
- **Measurement provenance.** Every `src/`, `tests/`, `evals/`, `deploy/` and frontend anchor below was re-measured at
  `1effedab2` in this pass unless it is marked *(inherited)*. Plan and task line numbers (`T04.md:2282`, etc.) are
  anchors into the untracked plan files as they stand now. The old findings (`findings/`, `d479eb2b4`) served only
  as a map of what to re-check.

---

## 1. CURRENT FACTS for this seam (at `1effedab2`)

### 1.1 Guided is gone from the product

- `git ls-files src/elspeth/web/composer/guided | wc -l` gives `0`.
- The following have 0 hits in `src/elspeth`: `guided_operations`, `_GuidedOperationRequest`,
  `guided_operation_request_hash`, `GuidedCustodyIntegrityError`, `composer_sync_timeout_seconds` (one `grep -rn`
  pattern, count `0`). Positive control: `grep -c guided_operations` on the spec gives `3`.
- `git ls-files | grep -c 'test_no_chain_authoring_path\|test_guided_heartbeat_cancel_classification\|test_schema9_epoch\|guidedOperationRetry'`
  gives `0`. Positive control: in the same run, `git ls-files` lists `test_compose_heartbeat_renewal.py`,
  `test_recompose_heartbeat_cancel.py`, `test_composer_request_telemetry.py`,
  `test_cross_process_composer_postgres.py` and `sessionOperationRetry.ts`.
- **`_track_compose_inflight`** (`sessions/routes/_helpers.py:2556`) has exactly 2 production mounts:
  `routes/messages.py:142` and `routes/composer/compose.py:102`. Its surface is closed to `Literal["freeform"]`
  (`_helpers.py:2599`).
- **[rev]** Test references to it: `test_composer_request_telemetry.py` 6, `test_compose_heartbeat_renewal.py` 3,
  `test_recompose_heartbeat_cancel.py` 1, and `tests/testcontainer/web/test_cross_process_composer_postgres.py` 1.
  The last is a test-app mount at `:72` (`Depends(_helpers._track_compose_inflight)`). That file's guided mount,
  which T07 cites at `:83-85`, is gone.
- **[rev]** `tests/unit/web/sessions/test_freeform_route_custody.py` is **641** lines (`wc -l`). It was 735 when
  `8630db9b8` created it; `7001600fe` later shrank it.

### 1.2 Epoch

- `SESSION_SCHEMA_EPOCH = 71` is at `sessions/models.py:59`.
- `_COORDINATION_HARD_CUT_EPOCH = 71` is at `sessions/schema.py:45`.
- The next free cut is **72**. It must be re-read immediately before the bump.
- **[rev]** Literal test pins: `git grep -n "SESSION_SCHEMA_EPOCH == "` over `tests` gives 5 files at `== 71`:
  - `test_web_blob_fencing.py:3205`
  - `test_blob_inline_resolutions_schema.py:73`
  - `test_interpretation_events_table.py:250`
  - `test_proposal_blob_effect_receipts_schema.py:27`
  - `test_schema.py:369`

  T17's "six pins" list includes the deleted `guided/test_schema9_epoch.py`. T17 must re-run its own mirror
  instrument, because this grep sees only the literal form.

### 1.3 The existing send identity (the subject of ruling 1)

**Request DTOs** (`sessions/schemas.py`):

- `_RequestModel` (`:66-69`) is `ConfigDict(extra="forbid")`, with no `strict`.
- `_SessionOperationRequest` (`:72-88`) is `ConfigDict(strict=True, extra="forbid")` with
  `operation_id: str = Field(min_length=36, max_length=36)` and a canonical-UUID validator.
- `SendMessageRequest(_RequestModel)` (`:143-159`) has `content: str = Field(min_length=1, max_length=65536)`,
  `state_id: UUID | None = None` and `client_request_id: UUID` (`:154`).
- `RecomposeRequest(_RequestModel)` (`:162-165`) has only `expected_user_message_id: UUID`, with no `state_id` and no
  client key.
- `ChatMessageResponse.client_request_id: str | None = None` (`:216`) puts the ingress key on the transcript wire.

**Ingress table** (`models.py:464-488`):

- PK `(session_id, client_request_id)`; `client_request_id` is an unbounded `String`.
- `user_message_id` is NOT NULL and UNIQUE (`uq_message_ingress_receipts_user_message`, `:484`).
- `requested_state_id` is NULL-able.
- FKs to `chat_messages(id, session_id)` (`:472-477`) and to `composition_states(id, session_id)` (`:478-483`), both
  `ON DELETE CASCADE`.
- Its triggers `trg_message_ingress_receipts_no_update` / `_no_delete` (PG DDL from `models.py:1596`) are in
  `_REQUIRED_AUDIT_TRIGGERS`. That set has exactly **11** members (`schema.py:96-112`, read in full).
- The PG delete-guard precedent: `IF EXISTS (SELECT 1 FROM sessions WHERE id = OLD.session_id) THEN RAISE` (receipt
  events, `models.py:1681-1692`).

**Writer, lookup and comparison:**

- The single writer is `SessionServiceImpl._insert_message_ingress_receipt(conn, /, *, session_id,
  client_request_id, user_message_id, requested_state_id, created_at, session_operation_context)` (`service.py:1396-1422`).
  It runs under the session write lock and requires COMPOSE.
- `lookup_message_ingress(session_id, *, client_request_id, content, requested_state_id, session_operation_context)`
  (`service.py:4539-4574`) decides "same request" as **(content, requested_state_id)**.
- `add_message_with_transcript(..., client_request_id, requested_state_id, ...)` (`service.py:4576`) returns
  `MessageIngressFresh | MessageIngressAccepted | MessageIngressConflict`.
- Its only production caller is `routes/messages.py:238` (`git grep "add_message_with_transcript("` over `src`: the
  protocol declaration, the implementation and this one call).

**Route 409 arms.** `_ingress_receipt_conflict` (`messages.py:111-124`) answers `message_already_accepted` or
`message_idempotency_conflict` with `client_request_id` and `user_message_id`. It is raised from the lookup
(`:173-181`) and from the atomic insert (`:253-254`). An unrecognised outcome raises `AuditIntegrityError`
(`:255-256`).

**SPA** (`frontend/src/stores/sessionStore.ts`, 2716 lines):

- Transcript matching on `client_request_id`: `:676`, `:703`, `:790`, `:803`, `:855`.
- The send path mints `crypto.randomUUID()` at `:1502` and calls
  `api.sendMessage(activeSessionId, content, clientRequestId, stateId, signal)` at `:1534`.
- The already-accepted arm is at `:1622-1638` and the idempotency arm at `:1684-1685`.
- Reload merge is at `:2107-2121` and reconcile at `:2183-2199`.
- **[rev] A retry reuses both keys.** `clientRequestId = retriedIntent?.client_request_id ?? crypto.randomUUID()`
  and `stateId = retriedIntent ? retriedIntent.local_requested_state_id ?? null : get().compositionState?.id ?? null`
  (`:1498-1502`).

**[rev] Blast radius of a wire rename.** `git grep -l client_request_id` over tracked non-doc, non-dist files lists
these non-test consumers:

- `sessions/schemas.py`, `models.py`, `schema.py`, `protocol.py`, `service.py`, `routes/_helpers.py`,
  `routes/messages.py`;
- `_acceptance_common/replica_probes.py` (`:525`, `:630-647`);
- `_azure_container_apps_acceptance/controller.py`, `azure_container_apps_acceptance.py` and
  `azure_container_apps_observations.py` (`:314` body);
- `deploy/azure-container-apps/scripts/acceptance.sh:484`;
- `evals/composer-battery/drive_battery.py:389`;
- `frontend/src/api/client.ts`, `types/index.ts` and `stores/sessionStore.ts`.

It also lists **21 test files**. The largest counts are `test_routes.py` 88, `test_service.py` 29,
`test_schemas.py` 19 and `test_freeform_route_custody.py` 18. The plan folder and the spec mention neither
`client_request_id` nor message ingress: every `grep -c` gives 0 (the positive control is the same grep over `src`, which hits the files listed above). The "ingress" hits in T08/T10/T15 are
**compartment** ingress (`T10.md:507`, `:2013`), a different concept.

### 1.4 Today's meaning of `state_id` **[rev]**

`messages.py:183-221`:

- An explicit `state_id` is looked up. A missing or cross-session state is a byte-identical 404 `State not found`
  (`:199`, `:201`).
- An **absent** `state_id` records `pre_send_state_id = state_record.id` (the current head) as provenance
  (`:203-204`).
- The loop base is always the head: `compose_base_state_id = state_record.id` (`:218`). The comment at `:206-217`
  cites `elspeth-e08063c3a5`: the client id legitimately lagged when a client-aborted synchronous turn kept writing.

So today an absent `state_id` means "whatever the head is". Ruling 5 says "absent means 'no state'". Today's
`StaleComposeStateError` renders a **flat** body, `409 {"error_type":"stale_compose_state","detail":"The session
changed while the compose turn was running.","request_id":_correlation_id(request)}` (`app.py:1502-1514`). It is not
the `{"detail": {...}}` shape.

### 1.5 Receipts codec (the source of the B′ extraction)

- `_REQUEST_SCHEMA = "session-operation-receipt-request.v1"` (`operation_receipts.py:36`).
- `operation_receipt_request_hash(*, session_id: UUID, kind: OperationReceiptKind, request: BaseModel) -> str`
  (`:39-53`) requires a strict/forbid config and an `operation_id` model field. It dumps with
  `exclude={"operation_id"}, exclude_unset=False, exclude_defaults=False, exclude_none=False` and returns
  `stable_hash({"schema","session_id","kind","request"})`.
- `operation_receipt_response_hash(response: BaseModel) -> str` (`:56-62`) re-validates strictly, then hashes.
- `OperationReceiptKind = Literal["session_fork", "state_revert"]` (`protocol.py:264`).
- The live plain-`RuntimeError` precedent for operation exceptions is `OperationReceipt*Error` (`protocol.py:348-360`,
  *(inherited)*).

### 1.6 Both freeform routes were restructured after the plan base

`git log --oneline d479eb2b4..HEAD -- routes/messages.py routes/composer/compose.py` lists 5 commits: `7001600fe`,
`166a83620`, `ed84adf51`, `8630db9b8` and `04c11a713`.

- **`8630db9b8`** (2026-09-27) added the owned child-task custody. `_FreeformContinuationReceipt` (`_helpers.py:1348`),
  `_capture_freeform_child` (`:1362`), `_freeform_child_result` (`:1375`) and `_join_freeform_owned_task` (`:1428`)
  first appear there, along with the ingress table and `test_freeform_route_custody.py`.
- **`04c11a713`** moved gateway failures and cancellation custody into `_helpers.py` (+148). This is where
  `_handle_composer_provider_failure` (`_helpers.py:717`) and `_handle_composer_chargeable_refusal` (`:802`) come from (`git log -S` on each symbol over `_helpers.py` names only `04c11a713`).
- **[rev] `166a83620` and `ed84adf51`** split `composer/service.py` (now **2760** lines) into
  `composer/provider_gateway.py` (625 lines), `chargeable_admission.py`, `interpretation_surfacing.py` and
  `application_policy.py`. The planner lives in `composer/planning_application.py` (837 lines). Every T08/T09/T10
  `composer/service.py` anchor from the plan base is invalid, including T10's review flag
  `composer/service.py:5003-5009`, the planner's own audit write.
- **Durable-completion flag.** It is request-scoped: `_track_compose_inflight` sets it False (`_helpers.py:2607`) and
  reads it to label the terminal (`:2708-2742`). The routes set it (`messages.py:618`, `compose.py:419`) and read it on
  the cancel path (`messages.py:945`, `compose.py:695`). The worker has no `Request`.
- **Both cancelled paths already use `_join_shielded_task_after_cancellation`** (`messages.py:965`, `:986`;
  `compose.py:707`, `:727`).
- **Recompose preamble** (`compose.py:109-166`): 400 when there are no messages; 409 when the last message is not a
  user row; 409 `recompose_user_message_mismatch` (`:155-162`). Recompose writes no ingress row (0 hits for
  `client_request_id` / `message_ingress` in `compose.py`; in the same `grep -c` run, the positive control
  `messages.py` gives 4).
- `app.py` has **13** `@app.exception_handler` decorators (`grep -c`).

### 1.7 Service, lease and repository anchors

`service.py` is **7194** lines (about 15006 at plan time). Every `service.py:NNNN` in T06/T08/T10/T11/T15/T16/T18
(`grep -c "service.py:"`: T06 14, T08 14, T10 5) must be re-derived. The live anchors are:

| Symbol | Line |
|---|---|
| `_require_session_operation_context_on_connection(self, conn, context, *, session_id, expected_kind, now) -> None`. It checks only the exact fence row, `released_at IS NULL AND lease_expires_at > now`. It has no job predicate and no `guided_fence`. | `:970-1000` |
| `_session_composer_mutation_transaction` | `:1002` |
| `_insert_chat_message` | `:1289` |
| `_insert_message_ingress_receipt` | `:1396` |
| `list_composition_proposals` | `:3294` |
| `lookup_message_ingress` | `:4539` |
| `add_message_with_transcript` | `:4576` |
| `save_composition_state` | `:4938` |
| `begin_provider_attempt` | `:5443` |
| `finish_provider_attempt` | `:5515` |
| `settle_provider_attempt` | `:5581` |
| `add_messages_atomic` | `:6893` |

The `coordination/lifecycle.py` anchors (`_raise_adopt_failure_after_release` `:208`, `SessionOperationLease` `:272`,
`acquire` `:326`, `adopt` `:426`, `wait_until_lost` `:642`, `create_task` `:650`) are *(inherited)* from the earlier
draft's `git show d479eb2b4` comparison. They were not re-measured in this pass.

### 1.8 Budget and synchronous consumers

- `ComposerServiceImpl.compose(self, message, messages, state, session_id=None, current_state_id=None,
  user_id=None, progress=None, user_message_id=None, session_operation_context=None, completion_gates=None) ->
  ComposerResult` (`composer/service.py:898-911`). It has no `guided_terminal`. `self._timeout_seconds` is bound at
  `:574` and the deadline at `:944`.
- The protocol declaration is `composer/protocol.py:1622`.
- `explain_run_diagnostics` spans `composer/service.py:851-897` and passes `timeout=self._timeout_seconds` to
  `self._provider_gateway._call_text_llm_with_audit` (`:883-887`).
- The planner keeps its own `self._timeout_seconds = settings.composer_timeout_seconds`
  (`planning_application.py:263`), used at `:737`. `_plan_and_stage_empty_pipeline` is at `:656`.
- **SPA budget consumers.** `runComposeWithTimeout` (`config/composer.ts:49`) has one production caller, freeform
  `hooks/useComposer.ts:31`. `applyServerComposerTimeout(status.composer_timeout_seconds)` is at `App.tsx:392-393`.
  The readiness latch comment is at `SideRailValidationBanner.tsx:34-37`. No guided consumer remains.
- Settlement signatures (the D6 targets), *(inherited)* from the earlier draft:
  - `settle_pipeline_proposal_under_compose_lock(*, request, user, authority, draft_hash, composer_meta=None,
    telemetry_source="compose", required_trust_mode=None, session_operation_context)` (`pipeline_settlement.py:131-141`);
  - `settle_auto_commit_intent(*, request, user, service, session_id, intent, composer_meta, telemetry_source,
    session_operation_context)` (`:405-415`).

  Neither carries `transition_assistant` or `require_transition_consumed`.

### 1.9 Other holders of the per-session compose lock **[rev]**

`git grep "_get_session_compose_lock_registry(...).get_lock"` finds these holders:

- `routes/composer/compose.py:117` and the messages route;
- `routes/composer/proposals.py:280`, `:689`;
- `routes/composer/state.py:744`, `:877`, `:1080`;
- `routes/interpretation.py:128`, `:269`;
- `execution/routes.py:1456`.

A worker job waiting on the in-process lock (F-C3) can therefore still be blocked by proposal, state, interpretation
and execution routes. The guided holder that codex C3 cited is gone, but the hazard remains.

### 1.10 Plan-side facts (untracked plan files, read at this tip)

- **T04 already bounds the claim predicates to queued rows.** `_claim_candidates` (`T04.md:2274-2300`) and
  `list_expired_queued` (`:2575-2590`) filter `status == "queued"` before
  `claim_token IS NULL OR claim_expires_at <= now` (`:2282`, `:2584`).
- **`list_expired_running` is fence-based, not expiry-based** (`T04.md:2592-2620`). It anti-joins
  `session_operation_fences` on the bound triple with `released_at IS NULL AND lease_expires_at > now`. Nulling
  `claim_expires_at` on running rows breaks nothing here.
- **`_claim_candidates` carries a correlated anti-join** that orders queued jobs per session and blocks behind a
  running job (`T04.md:2285-2300`). Its own comment says "D8 admission keeps one nonterminal job per session, so this
  holds trivially today".
- **[rev] The old plan leaves recompose's `user_message_id` NULL.**
  `test_recompose_turn_retries_the_last_user_row_without_inserting_one` asserts `record.user_message_id is None`
  (`T10.md:961`). Only send binds it, through
  `record_composer_operation_user_message_on_connection(connection, running, *, user_message_id, session_operation_context)`
  (`T10.md:72`, body `:1697-1740`) inside a sibling of `add_message_with_transcript` (`T10.md:1760-1775`).
- **`request_json` is NULL on terminal rows** (`contract.md:195-196`). Once a recompose settles, nothing on the job
  row records which user row it retried, or (under ruling 5) which base it was bound to.
- **The old F-B2/C2 predicate is named `require_no_committed_composer_cancel_on_connection`** (`repository.py`). It is
  called from the service chokepoint with `audit_only=False` and from `_SessionOperationAuthorityRepository.mutate`.
  `compare_and_swap` is exempt as a liveness proof. The stated deviations are `settle_provider_attempt` and the
  `request_cancelled` terminal in `fail_composer_async_operation` (`T06.md:3549-3583`).
- **The old start composite runs the per-kind preamble after `running`.** T00 A.3 is headed "Per-kind preamble under
  SOL (row is already `running`, R3; see Review note 3)" (`T00.md:809`). The spec, however, says the worker "rechecks
  … transcript/state preconditions under that authority, then atomically marks the job `running`" (`spec:161-164`).
- **The branch has not been started.** `feat/composer-async-ops` does not exist, and no `composer-async` worktree is
  registered *(inherited)*. `docs/plans/2026-09-23-web-review-remediation/` is untracked *(inherited)*. This plan
  folder is itself untracked (`git status --short`: `?? docs/plans/2026-09-20-composer-async-operations/`).

---

## 2. DELTA against the old findings and contract (`d479eb2b4` → `1effedab2`)

| Area | Plan / old findings say | True now |
|---|---|---|
| Guided scope | Three guided routes stay synchronous and unchanged. Scope ruling, Global Constraints, T01/T07/T11/T13/T14 prove "guided unchanged". | **Vanished** (§1.1). Every such proof has nothing left to prove. |
| Epoch | 68 (`contract.md:39`, T17: 73 hits of "68") | **72** (§1.2); 5 literal pins, not 6 |
| Send identity | Absent from the plan and the spec | `client_request_id` + `message_ingress_receipts` (epoch 69, `8630db9b8`), 409 arms, SPA recovery (§1.3). Ruling 1 folds them into one key. |
| Strict request base | `_GuidedOperationRequest` (`contract.md:117`, `:158`, `:161`) | `_SessionOperationRequest` (`schemas.py:72`) |
| `RecomposeRequest` | "NEW, only `operation_id`" | Exists, non-strict, with `expected_user_message_id` (`:162-165`). It gains the key and `state_id` (ruling 5). |
| Request codec | Copy `guided_operation_request_hash` | Extract from `operation_receipt_request_hash` (B′), with a golden vector and C as the fallback |
| Route bodies | T10 "byte-preserving move" of the pre-`8630db9b8` ladder | Different code: owned child custody, continuation receipt, `watcher_cancel`, `OpenAIError` / `ChargeableAdmissionRefused` / `InvariantError` arms, the durable flag (§1.6) |
| Composer service | Anchors in a ~5.4k-line `composer/service.py` | Split into `provider_gateway` / `chargeable_admission` / `interpretation_surfacing` / `application_policy` / `planning_application`; `service.py` is 2760 lines **[rev]** |
| `sessions/service.py` | Anchors up to `:15006` | 7194 lines; all re-derived (§1.7) |
| Planner budget | `composer/service.py:5289-5382` | `planning_application.py:263`, `:656`, `:737` |
| `compose()` signature | Has `guided_terminal` | Has none (§1.8) |
| D5 divergence | Recompose uses shield+suppress; send has a guided custody arm | **Already converged** (§1.6) |
| D6 signatures | `transition_assistant` / `require_transition_consumed` | Neither exists (§1.8) |
| D11 consumers | Guided routes + planners + diagnostics + proposals + status | Diagnostics (`composer/service.py:883-887`), proposal settlement, the status key, the tutorial wait |
| M6 | T14 feeds the sync key to the guided `runComposeWithTimeout` | Only freeform uses it (`useComposer.ts:31`); it dies at cutover |
| `_track_compose_inflight` | 5 mounts, guided kept | 2 production mounts + 1 testcontainer test-app mount (`:72`) |
| Storage placement | 3-authority policy; `claim_expires_at` on running rows; claim triple nulled at terminal; running-only unique; no delete guard | B′ amendments (rulings 1–4) |
| F-B2/C2 | Negative predicate (a cancel-marked running row) | **Positive** (ruling 2) |
| D9 | A stale `state_id` composes against the head | **Superseded** by ruling 5: refuse before side effects |
| `state_id` absent | Not discussed | Today: provenance = head (`messages.py:203-204`). Ruling 5: "no state". **[rev]** |
| ACA P1 probe | Only observation-driver edits | Counts ingress rows after a synchronous POST (`replica_probes.py:525`, `:630-647`); `acceptance.sh:484` injects the key |
| SPA custody precedent | `guidedOperationRetry` | `sessionOperationRetry.ts` (fingerprint-only, 8 KiB, 16 descriptors, 24 h; `:22-29`) |
| `sessionStore.ts` | About 4800 lines; anchors `:2174-2419`… | 2716 lines; send and recovery rewritten (§1.3) |
| F-C3 evidence | Guided turn holds the shared lock | Proposals / state / interpretation / execution hold it (§1.9); the hazard stands **[rev]** |
| Recompose job `user_message_id` | NULL (`T10.md:961`) | RECOMMENDATION assumed it is set; see the correction below **[rev]** |

### 2.1 Fact corrections to the panel and to the earlier draft **[rev]**

| # | Claim | Correct fact |
|---|---|---|
| 1 | RECOMMENDATION.md:160: "recompose jobs have `user_message_id` set … Dropping the column loses recompose's target" | In the plan, recompose leaves it NULL (`T10.md:961`). The ingress→job FK direction is still right, because recompose has no ingress row and a NULL key never triggers the FK. But the "loses recompose's target" argument holds only if the plan is **changed** to bind recompose's `user_message_id` (proposed in §3.1 T10 and §4 Q6). |
| 2 | Earlier draft: `test_freeform_route_custody.py` "735 lines" | 641 now; 735 at creation |
| 3 | Earlier draft: `_track_compose_inflight` "2 mounts" | 2 production mounts, plus a test-app mount at `test_cross_process_composer_postgres.py:72` that the cutover deletion must also retire |
| 4 | Earlier draft: T02/T17 rows name `operation_id` as the new wire name | RULINGS says "one wire name, no alias" but does not name it. Every rename below is conditional on Q1. |
| 5 | T10 review flag `composer/service.py:5003-5009` (planner audit write) | Dead anchor; the planner is in `planning_application.py` |
| 6 | Codex C3 evidence `guided.py:3032` | Gone; §1.9 holders replace it |

---

## 3. IMPLICATIONS for the plan under the rulings

### 3.1 Verdicts for tasks T00–T18

Verdict key:

- **KEEP**: the logic stands and the anchors were re-read.
- **ANCHORS-ONLY**: the logic stands; re-measure the anchors.
- **REDESIGN**: the deliverable or the interfaces change.
- **MERGE-INTO-Tn**: the task's content moves into another task.
- **DELETE**: the task goes.

"Wire name" means the Q1 name everywhere below.

| Task | Verdict | Reason | Sections that change |
|---|---|---|---|
| **T00** Baseline + Appendix A | **REDESIGN** | The worktree, P0, lints and mutation-authority baselines keep their shape. Appendix A (spec §5's raise inventory) was built from the pre-`8630db9b8` ladder; the `M:`/`C:` node ranges (`T00.md:829`, `:869`) are dead. Re-derive it from the live ladder (§1.6). Add rows for: the ingress 409 arms (deleted at cutover, ruling 1); `recompose_user_message_mismatch`; the `OpenAIError`, `ChargeableAdmissionRefused` and `InvariantError` arms; `watcher_cancel`; post-provider child failures; durable-flag labelling. New exits from the rulings: a W-row "base moved → terminal 409 `stale_compose_state` before `running`" (ruling 5); a W-row for "`state_id` absent while a head exists" (see Q5); a Tier-1 row "ingress row already exists for this operation at worker time" (ruling 1); P/X rows for positive-predicate refusals (ruling 2); `settled_by` per A.8 settlement (ruling 3). Reclassify A.3: the recommended ruling-5 placement moves the base check before `running` (spec `:161-164`). M1 → 72. M3 has no tracked lane. M7 must re-census: the guided files are gone, `test_freeform_route_custody.py` is new, and **every caller omitting `state_id` on a session with a head** changes meaning under ruling 5. M9's guided-vs-freeform framing is dead. Add M10: p50/p99 `request_json` / `result_json` sizes (panel information gap). | Steps 4 (M1), 6 (M3), 9 (M6 + M10), 10 (M7), 12 (M9); Appendix A in full (A.1–A.8); Review notes 3, 11, 12 (Review-pass tail items B1/B4/M7/m1/m6/C2 stay, re-anchored) |
| **T01** Settings + sync cap | **REDESIGN (shrink)** | Keep the five `composer_async_*` knobs. F-m5 `le=16` still matches `async_workers.py:14,18` *(inherited)*. The sync-cap consumers shrink to `explain_run_diagnostics` (`composer/service.py:883-887`), proposal-decision settlement (`pipeline_settlement.py:228` via `proposals.py:314`, *(inherited)*), the tutorial wait (`tutorial_service.py:444`, *(inherited)*) and optionally the status key (Q3). Steps 9–10 (guided-full planner, guided chat) are deleted. `commit_timeout_seconds` is still needed because the sync accept path and the worker pass different budgets. The `model_copy` precedent cited from `guided/test_step_chat.py` is gone; cite a live one. | Files (drop the guided anchors); Produces; Steps 7–14; Gates (drop the `guided.py` tier keys) |
| **T02** Owned types, codecs, DTOs | **REDESIGN, split into T02a + T02b** | **T02a (go/no-go):** extract the neutral normaliser plus the strict-DTO response hash from `operation_receipts.py:39-62`, taking a closed `Literal` of the two schema tags. `operation_receipt_request_hash` keeps its tag and its `OperationReceiptKind`. Golden vectors for both families. If receipt bytes move, stop and ship C. **T02b:** owned types; the `settled_by` Literal; `ComposerOperationRecord` gains `settled_by` and `attempt`. Record invariants: running ⇒ `claim_expires_at` NULL; terminal keeps owner and attempt and has `settled_by` set. Strict `SendMessageRequest` / `RecomposeRequest` on `_SessionOperationRequest`: the send drops `client_request_id` for the wire-name field; recompose gains the wire-name field and `state_id` next to `expected_user_message_id`. Everything except the wire key is hashed. F-m1 must re-measure: "recompose == 55" (T02 review tail) is dead, and the send width changes with the field name. Exception precedent: `OperationReceipt*Error`. Result hash: one definition (strict re-validate, then the shared hash). | CONTRACT DEVIATION blocks; Consumes (`_SessionOperationRequest`); Produces; Step 2 (golden vectors; drop `test_guided_operation_requests.py`); Steps 4, 6; F-m1 tests |
| **T03** Table and gates | **REDESIGN** | Apply every B′ schema amendment: <br>• running arm: `claim_token` + owner NOT NULL, `claim_expires_at` NULL (amend "claim triple set or cleared as a unit", `contract.md:193-196`); <br>• terminal arms null only token and expiry, keeping owner and `attempt`; <br>• closed `settled_by` + CHECK; <br>• a widened UPDATE transition guard (no running→queued; no change to kind / hash / actor / owner / SOL triple on running; no clearing `cancel_requested_at`; `user_message_id` NULL→value only; terminal immutable); <br>• a `BEFORE DELETE` guard while the session exists (the `models.py:1681-1692` pattern); <br>• a partial unique `(session_id) WHERE status IN ('queued','running')` replacing `uq_…_one_running_per_session`; <br>• a partial claimable index; <br>• a partial unique `(session_id, session_operation_epoch)`; <br>• drop `ix_…_session_status` unless T04 names a query; <br>• `UNIQUE(session_id, operation_id, user_message_id)`; <br>• single-authority `TablePolicy`; <br>• `_REQUIRED_AUDIT_TRIGGERS` 11 → 13; <br>• the table comment. <br>Re-anchor after `session_operation_receipt_events_table`. **Sequencing trap:** the ingress-side DDL (the composite FK, the 36-character bound, any rename) must not land here. The still-synchronous `/messages` inserts ingress rows with no job row (`service.py:1413-1421`), so the FK would fail every send. It moves to the cutover task. **FK actions must be chosen explicitly** (Q7): job→`chat_messages` is RESTRICT in the contract, while ingress→`chat_messages` is CASCADE (`models.py:472-477`). | Row contract; Produces; Step 4 DDL; Step 6 triggers; Step 8 TablePolicy; Step 10 PG arm (+ transition-guard and delete-guard negative controls); Gates counts; review-tail F-m1 CHECK re-measured |
| **T04** Authority | **REDESIGN (moderate)** | The method set and the review-tail arms (B1, M2, M3, F-C3, F-C7, F-m1, m4) stand. Changes: <br>• `admit` maps the D8 index `IntegrityError` to `ComposerOperationActiveError`, re-reading the nonterminal row for the body. The PK read for same-id replay stays; the racy read-then-insert active check goes. <br>• Every settle writes `settled_by` and keeps owner and attempt. <br>• The queued-only CAS terms are already there (`T04.md:2282`, `:2584`); pin them with a negative control that goes red, at both the Python layer and the trigger layer. <br>• `LIMIT` on every scan and count; the poll read omits `request_json`. <br>• `_claim_candidates`' per-session ordering anti-join (`T04.md:2285-2300`) is structurally vacuous under the D8 index. Simplify it, or keep it as the stated contract with a comment that the index enforces it. <br>• `list_expired_running` needs no change (fence-based, §1.10). <br>• The manifest counts (`_REVIEWED_WRITERS` 10 → 11, `T04.md` tail) must be recomputed, because T05's start CAS and T06's terminal CAS move into this module as connection-taking helpers. <br>• The F-M2 fence-blocked population is fork/revert/receipt/EXECUTE. | Produces; Step 2/3 tests (index race, reclaim negative control, `settled_by`, forensic retention); Step 5 bodies; Steps 7–9 manifest |
| **T05** Composite start | **REDESIGN (small–moderate)** | R3 is unchanged. The start CAS moves into a connection-taking helper in the compose authority module (precedent: `settle_fork_operation_receipt` → `settle_operation_receipt(conn, …)`, *(inherited)*). It sets `claim_expires_at = NULL`. Add a writer pin (only this helper sets `running`) and add the receipts tests to the regression set (`_advance_exclusive_fence_on_connection` sits under the receipts' SOL acquire). **Ruling 5 (recommended placement, Q4):** inside the same locked transaction, after the fence advance and before the running CAS, compare the current head with the bound base. Recompose's `expected_user_message_id` check (today's `recompose_user_message_mismatch`, `compose.py:155-162`) moves here too (Q6). On mismatch, raise a new `ComposerOperationStaleBase` or `ComposerOperationPreconditionRefused`; the transaction rolls back; the worker settles through `settle_unstarted` (`settled_by='settle_unstarted'`). This realigns the plan with spec `:161-164`. It needs a head read (`composition_states` / the session head) inside `repository.py`. | Files; Produces (raise contract, helper name); Step 1 tests (+ stale-base, + recompose-mismatch before running); Step 5; Step 9 re-pin |
| **T06** Composite terminal | **REDESIGN** | (1) **Ruling 2.** Rename and invert `require_no_committed_composer_cancel_on_connection` into a positive predicate: if any job is bound to the fence triple, require `running AND cancel_requested_at IS NULL` unless `audit_only`. **Step 1b first measures every legitimate write under the same SOL after the terminal CAS** (the ruling's precondition). It must re-decide the existing exemptions: the `compare_and_swap` liveness exemption, `settle_provider_attempt`, the `request_cancelled` terminal write, and the SOL close/release. Ship the post-terminal negative control and an unaffected-fork/revert control. The lookup uses the `(session_id, session_operation_epoch)` index. <br>(2) The terminal CAS moves to an authority-module helper, so `SessionComposerOperationTerminalAuthority` leaves the `TablePolicy`. <br>(3) Re-derive every `service.py` anchor (§1.7). The guided entries (`guided_fence`, `reserve_guided_operation`, `settle_guided_fork_operation`, the `blobs/service.py` guided fence) leave the inventory. The planner audit writer is now in `planning_application.py`. <br>(4) `settled_by`, and keep owner and attempt. The panel's "user row + ingress + `job.user_message_id` in one tx" is a turn-time write, owned by the T10 successor. | F-B2/C2 block (rewrite); measured inventory; Files; Interfaces; Steps 1b, 9b, 10b, 11, 12 |
| **T07** Request lifecycle | **REDESIGN (small)** | Purpose changes from "guided byte-unchanged" to "both freeform routes behaviour-unchanged until cutover; the worker gets the same lifecycle". The unmodified-proof set is 3 files (§1.1). **New:** the durable-completion signal (`_helpers.py:2607`, `:2708-2742`; routes `messages.py:618`, `:945`, `compose.py:419`, `:695`) moves onto the lifecycle handle, because the worker has no `Request`. `_track_compose_inflight` becomes a thin wrapper and is deleted at cutover. The testcontainer test-app mount `:72` is re-pointed at the lifecycle. | Header; Files; Produces (durable carrier); Step 7 proof list; Step 9 PG file; Gates |
| **T08** Budget parameter | **ANCHORS-ONLY (+ retarget)** | `compose(budget_seconds=)` and D10 stand. The signature is §1.8's (no `guided_terminal`). Planner threading targets `planning_application.py:263`, `:656`, `:737`. Re-run the duck-typed-fake instrument (Step 2); the composer split (§1.6) moves fakes. The structural pin in `test_no_chain_authoring_path.py` is gone; re-home it (`test_freeform_route_custody.py` or `test_operation_fence_wiring.py`). | Files; Produces; Steps 2, 7, 9; Gates |
| **T09** Error projection | **REDESIGN (small)** | Keep the MRO adapter and the `create_app` parity. Re-inventory with the partition test's own instrument (13 `app.py` decorators plus the session-operation handlers). Drop `GuidedCustodyIntegrityError`. Add parity rows: `_handle_composer_provider_failure`, `_handle_composer_chargeable_refusal`, `recompose_user_message_mismatch`, and the **`stale_compose_state` flat body with the persisted POST `request_id`** (D4 must inject top-level, matching `app.py:1502-1514`, not into `detail`). No rows for the deleted ingress 409s. Re-derive the LiteLLM rows from `provider_gateway.py` / `_helpers.py`. | Consumes; Table A; the planner code map; partition exclusions |
| **T10** App services + turn function | **REDESIGN (major)** | A "moved bodies" port is impossible (§1.6); the turn must reproduce the owned child custody, `watcher_cancel` and the durable carrier with no `Request`. **Ruling 1:** the send user-row writer becomes one transaction: chat row, then `job.user_message_id`, then the ingress row keyed by the job's id, with `requested_state_id = state_id`. The order matters because the FK needs the job column set first. An ingress row that already exists is Tier-1 (`AuditIntegrityError`), never a 409. `lookup_message_ingress` and `MessageIngressAccepted`/`Conflict` shrink to that assertion. **Proposed change** (needs sign-off, Q6): bind recompose's `user_message_id` to the verified `expected_user_message_id` while running (the trigger's NULL→value arm). This keeps forensics after `request_json` is nulled. It flips `T10.md:961`. **Ruling 5:** no preamble base check here if the T05 placement is taken. D5 becomes a parity assertion. D6 targets the live signatures. The review-tail B2/C5/m3/M7 work stands, re-anchored. The transitional legacy DTO survives for **both** routes (Q2). | Contract deviations D-T10-1..3; Files; Produces; Steps 1b, 7–12 |
| **T11** Worker, reaper, lifespan | **REDESIGN (moderate)** | The architecture and every review-tail arm (B1, B2/C2, B4, M1, M3, M5, M8+C6, C3, C5, m2, M7, B4-residual) stand. Changes: <br>• every settle passes `settled_by`; <br>• the stale-base / precondition refusal from T05 settles through `settle_unstarted` with the D14-style envelope built by T09; <br>• worker ingress Tier-1 lands in the generic arm; <br>• the C3 lock-wait family keeps its purpose (§1.9 holders); <br>• the `app.py` anchors are re-measured; <br>• the durable carrier replaces request-state reads; <br>• drop the guided gate pins; <br>• the VERIFY-2 stale items 1, 2 and 4 are retired in the rewrite. | Deviations 3/4; Consumes; Step 2 tests; Steps 5, 8; Gates |
| **T12** Poll/cancel + helpers | **ANCHORS-ONLY** | Route design, F-B3 (terminal = 0) and the helpers stand. The poll read omits `request_json`. The `test_routes.py` IDOR anchors and the inventory line "after the `guided/chat` line" are stale. | Files; Step 10 |
| **T13** Cutover + caller migration | **REDESIGN** | **Single-key cutover (ruling 1):** <br>• delete `_ingress_receipt_conflict` and its call sites (`messages.py:111-124`, `:171-181`, `:253-256`), the route's ingress lookup and the route-only `MessageIngress*` outcomes; <br>• land the **ingress DDL moved out of T03** (composite FK, 36-character bound, wire-name column); <br>• mount the strict DTOs and delete both legacy DTOs; <br>• delete `_track_compose_inflight` and its test references (§1.1). <br>**Caller migration:** re-census (T00 M7). Every body replaces `client_request_id` with the wire name. **Every caller composing on a session with a head must send the head's `state_id`** (ruling 5; Q5). <br>**Other callers:** the ACA P1 probe (`replica_probes.py:525`, `:630-647`: count the job row and the one ingress row after 202 + poll), the observation driver (`azure_container_apps_observations.py:314`), `acceptance.sh:484`, the ACA controller/facade, and the eval battery (`drive_battery.py:389`, which drives single-turn fresh sessions, so `state_id` stays absent). | Files; Produces (the 409 set loses two arms); Steps 2, 5–8b, 10, 12, 13 |
| **T14** SPA cutover | **REDESIGN (major)** | Every `sessionStore.ts` anchor is dead (§1.3). **Ruling 1:** delete the transcript-matching recovery (`:676-855` helpers; `:1622-1638`, `:1684-1685`, `:1708`, `:2107-2121`, `:2183-2199`). Custody carries the wire-name key. `ChatMessageResponse` / `types/index.ts` follow Q1. **Ruling 5:** <br>• terminal 409 `stale_compose_state` → today's stale copy, with the body kept in custody; <br>• **resend after refusal = a new operation id + the fresh head `state_id`**, while a network-ambiguous retry keeps the same id and body (today's retry reuses both, `:1498-1502`); <br>• proposal Accept/Reject is disabled while a same-tab send is queued; <br>• recompose sends `state_id`. <br>**M6 is obsolete:** `runComposeWithTimeout` and `applyServerComposerTimeout` lose their last caller. Decide the readiness latch (`App.tsx:392-393`, `SideRailValidationBanner.tsx:34-37`; Q3). The custody precedent is `sessionOperationRetry.ts` (fingerprint-only, 8 KiB). The body-storing deviation (256 KiB) is re-justified against it. Keep the review-tail work: B3, M4, RF3, RF4. | CONTRACT DEVIATION blocks; Files; Measured facts; Produces; Steps 11, 13, 14, 20 |
| **T15** Decoupling + mirrors | **REDESIGN (small)** | The mechanics stand. The canonical sentence drops "guided turns". The review-tail M6 README sentence drops the guided clause. `7001600fe` touched the runbooks *(inherited)*: re-anchor. The tutorial wait keeps the ceiling/headroom check. | Canonical sentence; Interfaces; Steps 9c, 14 |
| **T16** PG crash windows | **KEEP + extend** | The 12 functions / 14 ids and the review tail (B1, B2, M3, F-B3 `_stable`) stand. Add: <br>• PG archive cascade with a running send job, `user_message_id` set and an ingress row (the FK actions from Q7); <br>• queued-job starvation under steady fork/revert; <br>• stale base (fork in another tab while queued → 409, no user row, no provider call); <br>• a post-terminal lingering-SOL write that fails (ruling 2); <br>• `settled_by` + owner/attempt assertions on every reaped row; <br>• the delete guard refusing while the session lives. | Produces; Consumes; Step 2; Step 5b controls |
| **T17** Epoch + docs | **REDESIGN (small)** | Epoch **72**. The pin set is re-measured (5 literal pins, §1.2). The spec edits (§2 D8 line, §4/§5 cross-refs) move to the up-front amendment, so Commit A is env rows, CHANGELOG/README and the docs test. CHANGELOG lines: ruling 1 (wire change; the ingress 409s are gone), ruling 5 (a stale or absent-with-head `state_id` now refuses), and M8 (deploy drain; the review tail), re-worded for epoch 72. | Files; Steps 1, 5, 6 (removed), 9–13 |
| **T18** Gates + acceptance | **ANCHORS-ONLY** | Mechanics stand. Consumed ids change. Mutation targets become the helper-hosted CAS. Add a mutation that deletes the transition guard's running→queued arm. Acceptance scenario for ruling 5 only if cheap (otherwise T16 holds it). | Interfaces; Step 8; Steps 15–17 |

No task is DELETE. The merges are the ingress DDL (T03 → T13) and T10 Step 1b's DTO swap into T02b. There is one
split (T02 → T02a + T02b).

### 3.2 Contract items under the rulings

#### Rulings table (2026-09-25)

| Id | Verdict | How |
|---|---|---|
| Scope | **MODIFY** | Freeform is now the only composer. Delete every "guided unchanged" clause (index `:28`, `:45`, `:55`; contract `:62`). |
| R0 | KEEP | — |
| R2 | KEEP | The terminal CAS moves into the authority-module helper. |
| R3 | KEEP | Running rows carry `claim_expires_at` NULL, so the job lease is the SOL lease structurally. |

#### Defaults D1–D16

| Id | Verdict | How / why |
|---|---|---|
| D1 | **MODIFY** | The CRL stays. The durable-completion signal moves onto the lifecycle handle (§1.6). |
| D2 | **MODIFY** | The order stays. The request hash covers `state_id` (send) and `expected_user_message_id` + `state_id` (recompose) (ruling 5). There is no ingress lookup pre-202 (ruling 1). "Active-operation check" is the D8 index. |
| D3 | KEEP | Soft cap (F-C7). |
| D4 | KEEP | Add that a flat-bodied handler (`stale_compose_state`) gets the persisted POST id top-level. |
| D5 | **OBSOLETE** | Already converged (§1.6); keep only a parity test. |
| D6 | **MODIFY** | Live signatures (§1.8) plus `commit_timeout_seconds`; caller `proposals.py:314` *(inherited)*. |
| D7 | **MODIFY** | No archive refusal and `ON DELETE CASCADE` stay. Add the delete guard while the session lives and retention with the session (ruling 4). The cascade must pass the ingress→job FK on PG (Q7). |
| D8 | **MODIFY** | Enforced by the partial unique index; `IntegrityError` → 409 with a re-read body. |
| D9 | **OBSOLETE** (ruling 5) | A foreign `state_id` stays 404. A stale one is a terminal 409 `stale_compose_state` before any side effect. |
| D10 | KEEP | Re-anchor. |
| D11 | **MODIFY** | Consumers: diagnostics, proposal settlement, tutorial wait, optionally the status key (Q3). |
| D12 | KEEP | Ruling 2 makes post-settle writes fail closed. |
| D13 | KEEP | `settled_by` is a separate closed column, not a failure code. |
| D14 | KEEP | Add the ruling-5 body: today's `stale_compose_state` 409 (`app.py:1508-1514`) as `http_error`. |
| D15 | KEEP | Send uses the worker-inserted row. Recompose uses `expected_user_message_id`, now also bound on the job if Q6 is approved. |
| D16 | KEEP | — |

#### Adopted deviations (23 rows, `contract.md:18-40`)

| Row | Verdict | Note |
|---|---|---|
| `admit` / rate limit outside | KEEP | — |
| `request_cancel`, `settle_lost` `cancelled_failure` | KEEP | + `settled_by` (`request_cancel` for the unclaimed-queued settle; `settle_lost`) |
| authority read API `list_expired_queued` | KEEP | + `LIMIT`; omit `request_json` from reads that do not need it |
| archived-session `settle_lost_inactive_session` | KEEP | `settled_by='settle_lost_inactive_session'` |
| `claim_next` per-candidate tx | KEEP | The CAS keeps `status='queued'`; the anti-join simplification is optional (§3.1 T04) |
| Tier-1 decoration → `RuntimeError` | KEEP | Precedent `OperationReceipt*Error` |
| `ComposerOperationError` home | KEEP | — |
| record invariants | **MODIFY** | Running ⇒ `claim_expires_at` NULL; terminal keeps owner and `attempt` with `settled_by` NOT NULL; queued ⇒ `user_message_id` NULL. Q6 decides whether recompose binds while running. |
| `ComposerOperationFenceLost` ctor | KEEP | — |
| `ComposerOperationCancelledDuringTurn` ctor | KEEP | Also raised by the positive predicate when the bound row is cancel-marked |
| `ComposerOperationAssistantWrite` | KEEP | Re-anchor |
| `run_composer_turn` + `request_lease` | **MODIFY** | Carries the durable carrier |
| `_ComposerOperationCancel` 3 kinds | KEEP | — |
| unmarked `CancelledError` → `worker_lost` | KEEP | — |
| `instance_draining: threading.Event` | KEEP | Re-anchor |
| cancelled-turn terminal in the job frame | KEEP | — |
| lifespan placement | KEEP | Re-anchor |
| strict `SendMessageRequest` (T10 + Legacy) | **MODIFY** | Moves to T02b; base `_SessionOperationRequest`; wire-name key (Q1); a transitional legacy class for **both** routes (Q2) |
| settlement signature D6 + `commit_timeout_seconds` | **MODIFY** | As D6 |
| poll/cancel no `PermissionError` arms | KEEP | — |
| client deadline in the store poll loop | KEEP | — |
| epoch bump "68" | **MODIFY** | 72, re-read at execution |
| spec §2 D8 line owned by T17 | **OBSOLETE** | Moves to the up-front spec amendment |

#### Review-pass decisions (11 rows, `contract.md:44-56`)

| Id | Verdict | How |
|---|---|---|
| F-B2/C2 | **MODIFY (ruling 2)** | The predicate becomes positive (see T06). The `audit_only` thread and the cohort persistence stay. Re-decide the four exemptions after the measured post-terminal inventory. |
| F-B3 | KEEP | Fold the "0 on terminal rows" and tighten-only wording into the body (VERIFY-2, VERIFY). |
| F-B4 | KEEP | Fold the B4-residual qualifier into the row (VERIFY-2 stale 0). |
| F-M3 | KEEP | `settled_by='settle_own_lapsed'` |
| F-C3 | KEEP | Evidence retargets to §1.9 |
| F-C5 | KEEP | — |
| F-C7 | KEEP | — |
| F-M2 | KEEP | Population: fork/revert/receipt/EXECUTE fences; plus the T16 starvation test |
| F-m1 | **MODIFY** | Re-measure both DTOs (wire-name key; recompose + `state_id`). "393 230 vs 393 334" wording fixed (VERIFY). Add M10 p50/p99. |
| F-m5 | KEEP | — |
| B4-residual | KEEP | — |

#### Review items outside the contract table (SYNTHESIS B/M/m; codex C1–C7) **[rev]**

| Id | Verdict | Note |
|---|---|---|
| B1 / C4 | KEEP | `settle_lost_inactive_session`; `settled_by` |
| B2 / C1 | KEEP | The cohort is still persisted `audit_only` before a cancel terminal. Under ruling 2 that path must stay allowed after the cancel marker. |
| B3 | KEEP | — |
| B4 | KEEP | — |
| C2 | **MODIFY** | Superseded by ruling 2's positive form |
| C3 | KEEP | Evidence retargeted (§1.9) |
| C5 | KEEP | — |
| C6 / M8 | KEEP | CHANGELOG wording re-targeted to epoch 72 |
| C7 | KEEP | As F-C7 (soft cap) |
| M1, M2, M3, M4, M5 | KEEP | — |
| M6 | **OBSOLETE** | Guided ceiling gone; leaves only Q3 |
| M7 | **MODIFY** | Appendix A re-derived from the live ladder; P15 stays |
| m1 | **MODIFY** | As F-m1 |
| m2, m3, m4, m5 | KEEP | — |
| m6, m7, m8 | ANCHORS-ONLY | Re-measure |
| m9 | **MODIFY** | "No interim merge" now spans the DTO swap (T02b) to the cutover |
| VERIFY owner leftovers | open | T14 missing-session copy (owner); contract body drift listed in VERIFY `:140-160` to be folded in by the contract rewrite |

#### Panel amendments (RECOMMENDATION "Consequences")

| # | Amendment | Verdict | Home (§3.4) |
|---|---|---|---|
| P1 | Separate table, own authority, single-authority `TablePolicy` | ADOPT | N04, N05 |
| P2 | Shared normaliser + response hash, golden vector, fallback C | ADOPT | N02 |
| P3 | One id per send; ingress→job FK; delete 409 arms and SPA recovery | ADOPT (ruling 1) | N03, N11, N14, N15 |
| P4 | Running `claim_expires_at` NULL | ADOPT | N04, N06 |
| P5 | Terminal keeps owner and attempt; `settled_by` | ADOPT (ruling 3) | N03–N07, N12 |
| P6 | Transition guard | ADOPT | N04 |
| P7 | Delete guard | ADOPT (ruling 4) | N04 |
| P8 | D8 partial unique | ADOPT | N04, N05 |
| P9 | Claimable partial; `(session_id, session_operation_epoch)` unique; drop `session_status` unless needed; `(status, deadline_at)` only if measured | ADOPT (measured in N05) | N04, N05 |
| P10 | Triggers 11 → 13 | ADOPT (11 measured) | N04 |
| P11 | Table comment | ADOPT | N04 |
| P12 | No receipts; queued-only CAS; `IntegrityError` → 409; LIMITs; poll omits `request_json` | ADOPT | N05 |
| P13 | Start helper; receipts regression; writer pin | ADOPT | N06 |
| P14 | Terminal helper; positive predicate; user row + ingress + job column in one tx | ADOPT (the user-row half → N11) | N07, N11 |
| P15 | Single-key cutover | ADOPT | N14, N15 |
| P16 | ACA P1 re-scope | ADOPT | N14 |
| P17 | Delete `_track_compose_inflight` | ADOPT (plus the testcontainer mount) | N14 |
| P18 | PG cascade + starvation tests | ADOPT | N17 |
| P19 | Epoch 72 | ADOPT | N18 |
| P20 | Compose events table | **REJECTED** (ruling 3) | — |
| P21 | Retention | APPROVED (ruling 4) | N04 comment; spec §1 |
| P22 | Head move admission → start | **REFUSE** (ruling 5) | N03 (hash), N06 (check), N12 (settle), N15 (SPA) |

### 3.3 Second spec amendment (`docs/specs/2026-09-16-composer-async-operations-design.md`)

| Spec location | Change |
|---|---|
| Header `:6-12` | Replace the 2026-09-25 scope text: guided was removed on 2026-09-28 (`7001600fe`); freeform is the only composer; the tutorial runs freeform. Add "Second amendment 2026-09-28" citing `panel-2026-09-28/RULINGS.md`. |
| `:34-52` cutover set | Keep the table. Delete `:41-44` (guided routes keep `_track_compose_inflight`) and `:49-52` (guided/tutorial not counted). Keep "state edits, proposal decisions, execution and run routes are outside". State that `_track_compose_inflight` is deleted with the cutover. |
| §1 `:56`, `:59-61` | "`session_operation_receipts` is untouched", with the reason: receipts take over expired rows and rebuild from locators; compose never takes over a running row and stores the full DTO. |
| §1 `:74-80` field table | Custody: claim token + owner; `claim_expires_at` only while queued; `attempt`; `cancel_requested_at`; closed `settled_by`; owner + attempt kept at settlement. Output: terminal rows are retained with their session. Identity: the operation id is the send's ingress key (ruling 1). |
| §1 `:82-90` | Add: a job row cannot be deleted while its session exists (delete guard); session deletion removes it. Note the forensic loss of `request_json` at settlement, and Q6's resolution. |
| §1 `:92-100` | Replace the intent wording with the enforcement: a queued-only reclaim predicate and a DB transition guard; a running row has no claim expiry; liveness is SOL liveness (R3). Replace "require both the SOL context and the transport job's live running fence" with the positive predicate (ruling 2), including the `audit_only` exemption. Add: at most one nonterminal job per session is a partial unique index (D8). |
| §2 `:104-113` | Replace "gain a required `operation_id`" with the one-id rule (ruling 1): the client-minted id **is** the `message_ingress_receipts` key; ingress is the immutable acceptance record, worker-written with the user row; composite FK ingress→job; one wire name (Q1), no alias, no dual acceptance; the ingress 409s and the transcript-matching recovery are deleted. Replace "`guided_operation_request_hash` is the model to follow, not a function to share" with: one neutral normaliser, a per-facility tag, a golden-vector receipt pin, and a separate codec as the fallback. Ruling 5: the send's `state_id` (absent = "no state"; Q5) and the recompose `state_id` are hashed. |
| §2 `:115-131` | Add the D2 order and D8 (`composer_operation_active` 409 with `operation_id` and `kind`) as schema-enforced. No ingress lookup pre-202. |
| §2 `:133-148` | Add `deadline_remaining_ms` on the DB clock (0 on terminal rows). The poll never carries `request_json`. |
| §3 `:155-157` | "chooses the oldest queued job per session" → "at most one nonterminal job exists per session (D8), so a session has at most one claimable job". **[rev]** |
| §3 `:161-178` | Make the precondition recheck explicit and place it **before** `running` in the start transaction (ruling 5; recompose's expected-row check too). On mismatch: terminal 409 `stale_compose_state` (today's body), no user row, no provider call. Add: an existing ingress row for the job's id at worker time is Tier-1. |
| §3 `:180-194` | Replace "so the three guided routes keep identical renewal …" with "the two freeform routes keep identical behaviour until cutover". The durable-completion signal moves to the lifecycle handle. |
| §3 `:204-220` | Delete `:211-217` (guided bound). Keep the ceiling/headroom for the tutorial wait, proposal decisions and diagnostics. |
| §4 `:234-243` | Every settle records `settled_by` and keeps owner and attempt. Post-terminal writes under a lingering SOL fail (ruling 2). The LLM cohort is persisted `audit_only` before `request_cancelled`. |
| §4 `:244-252` | New row: "Head moved between admission and start → terminal 409 `stale_compose_state`; no user row; no provider call". |
| §4 `:254-258` | "A retry of the same ID never creates another originating user row" is now also enforced by the ingress PK = job id. **[rev]** |
| §5 `:262-282` | Delete "Guided callers are not changed" (`:266-267`). Add: Accept/Reject is disabled while a same-tab send is queued. A stale 409 shows today's copy and keeps the body. **Resending after a refusal uses a new id and the current head; only an ambiguous acknowledgement reuses the id and body** **[rev]**. Replace the client-deadline sentence (`:277-278`) with the monotonic `deadline_remaining_ms` rule. |
| §5 `:284-291` | The ingress 409 bodies are removed, not preserved. |
| Gates `:293-312` | Gate 1: transition guard, delete guard, positive predicate, golden vector. Gate 2: delete `:301-302` (guided synchronous check). Gate 3: stale-base window, PG ingress→job cascade. Gate 5: Accept/Reject guard, stale resend, deletion of the ingress recovery. |

### 3.4 Proposed re-based task list

The shape is kept: numbered tasks, TDD red then green, commits by pathspec. There is no interim merge between N03
(DTO swap) and N15 (SPA cutover). N-ids avoid confusion with the old T-ids.

**Before N00, one docs commit:** the §3.3 spec amendment, and a contract rewrite that folds the Adopted-deviations and
Review-pass rows (and VERIFY's body-drift list) into the body, with a single "Rulings 2026-09-28" table. After that,
re-run the plan review with a reality pass on N10–N14, which the 09-25 review never had (SYNTHESIS gap 1). **Q1 (wire
name) must be ruled before N03.**

| # | Task | Replaces | Depends on | Rationale |
|---|---|---|---|---|
| N00 | Re-baseline: worktree, M0–M10, gate-2 baseline, Appendix A from the live ladder plus the ruling 1/2/5 exits | T00 | docs commit | Appendix A is spec §5's deliverable, and the ladder changed (§1.6) |
| N01 | Settings: five knobs + sync cap for the remaining sync consumers | T01 (shrunk) | N00 | Guided consumers are gone |
| N02 | Shared normaliser + response hash, receipts delegate, golden vectors. **Go/no-go → C.** | from T02 | N00 | Touches landed receipts code, and its failure changes the design, so it goes first and alone |
| N03 | Owned types (`settled_by`, invariants, stale-base / precondition class), strict DTOs with the wire key + hashed `state_id`, transitional legacy DTOs for both routes, wire DTOs | T02 + T10 Step 1b | N02, Q1 | Consumers N05–N13 need the final DTOs |
| N04 | Job table: B′ schema, both triggers, indexes, single-authority `TablePolicy`, triggers 13, PG reflection, explicit FK actions. **No ingress DDL.** | T03 | N03 | The ingress FK would break the still-synchronous send |
| N05 | Authority: index-mapped D8, `settled_by`, queued-only CAS with negative controls, LIMITs, the poll read, recomputed manifest | T04 | N04 | — |
| N06 | Composite start: helper-hosted running CAS (`claim_expires_at` NULL), acquire refactor, **precondition gate (ruling 5 head check + recompose row check) before `running`**, receipts regression | T05 | N05 | Keeps "no side effect before running" structural and matches spec `:161-164` |
| N07 | Composite terminal: **post-terminal write inventory first**, then the positive predicate + `audit_only` + exemptions re-decided, helper-hosted terminal CAS, `settled_by` | T06 | N06 | Ruling 2's precondition is measured inside the task |
| N08 | Lifecycle extraction + durable-completion carrier | T07 | N00 | Behaviour-neutral refactor with a 3-file proof |
| N09 | `compose(budget_seconds=)` + planner threading + D10 | T08 | N01 | Anchors only |
| N10 | Error projection with handler parity (new arms; flat `stale_compose_state` + persisted id) | T09 | N03 | — |
| N11 | App services, D6, `run_composer_turn` rebuilt around the owned child custody; worker user-row write = chat row + `job.user_message_id` + ingress (ordered; Tier-1 on an existing row); recompose binding (if Q6) | T10 | N07–N10 | The largest redesign. Split into N11a (services + D6 + binding writer) and N11b (turn) if one commit is not reviewable (Q9). |
| N12 | Worker, reaper, lifespan (`settled_by`; precondition refusal settle) | T11 | N11 | — |
| N13 | Poll/cancel routes + helpers | T12 | N12 | Anchors only |
| N14 | Cutover: 202 routes; **ingress DDL**; delete the ingress arms, the lookup, the legacy DTOs and `_track_compose_inflight` (incl. the testcontainer mount); re-census migration incl. `state_id`; ACA P1 + controller + `acceptance.sh`; eval battery; gate 2 | T13 + the ingress half of T03 | N13 | The FK lands only when every ingress writer is the worker |
| N15 | SPA cutover (recovery deletion, ruling-5 guard, stale resend, M6/latch decision, custody vs `sessionOperationRetry`) | T14 | N14 | N14 and N15 land together before any deploy |
| N16 | Budget decoupling + mirrors | T15 | N14 | — |
| N17 | PG crash windows + 6 new tests (§3.1 T16) | T16 | N14 | — |
| N18 | Epoch 72 + doc sweep (CHANGELOG for rulings 1 and 5 + M8) | T17 | all | Spec edits moved up front |
| N19 | Gates + local acceptance | T18 | N18 | — |

**Index edits.**

- Delete the guided Global Constraints (`:45`, `:55`). Add the five rulings.
- Review Focus #3: reattach comes only from D8, because the transcript-matching fallback no longer exists.
- Add Review Focus #6: "head moved while queued (fork/revert in another tab)", owned by N06 + N15.
- Add Review Focus #7: "resend after a stale refusal must not replay the refused id".
- Drop or re-point the web-review contention note (untracked source).

---

## 4. Open questions

1. **Wire name (gates N03).** Ruling 1 fixes "one wire name, no alias" but does not name it. There are three
   surfaces:
   - the request DTO (`schemas.py:154`);
   - the transcript field `ChatMessageResponse.client_request_id` (`schemas.py:216`), which the SPA joins on;
   - the ingress column (`models.py:468`).

   The rename blast radius is §1.3: 15 non-test consumers, including ACA shell and eval tooling, and 21 test files.
   The panel majority recommends `operation_id` everywhere, with the column renamed in the epoch cut. Keeping
   `client_request_id` everywhere, with the job column named to match, is the lower-churn alternative. Either
   satisfies "no alias"; mixing does not.
2. **Transitional DTOs for both routes.** Is "legacy DTO until cutover, no interim merge" acceptable for recompose
   too, now that its body changes? Or should the DTO swap move wholly into N14?
3. **`/api/system/status` sync key and the readiness latch.** After cutover no SPA code reads the compose budget for
   a client timer (M6 was guided-only). Is the key published for operators, or dropped from D11? Does the readiness
   latch (`App.tsx:392-393`) survive?
4. **Ruling 5 placement.** This file recommends the start composite (N06): the check runs in the same locked
   transaction as the fence advance, the refusal settles via `settle_unstarted`, the row never runs, and this matches
   spec `:161-164`. The cost is a head read inside `repository.py`. The alternative is the worker preamble after
   adopt (the row shows a start that did nothing). The ruling's text ("under the COMPOSE SOL, before the user row")
   admits both.
5. **"Absent means no state" versus today's semantics.** Today an absent `state_id` means "provenance = head"
   (`messages.py:203-204`). Read literally, ruling 5 makes every send without `state_id` to a session with a head a
   terminal 409. The SPA always sends its head or null (`sessionStore.ts:1499-1501`). Tests (88
   `client_request_id` sites in `test_routes.py` alone), the ACA observation body (`:314`) and `acceptance.sh:484` do
   not. Is the literal reading intended (callers migrate), or does absent keep meaning "current head"? The second
   would weaken "base = what the operator saw" for non-SPA callers. The related case is `elspeth-e08063c3a5`
   (`messages.py:206-217`): under 202 + poll the SPA sees every terminal, but a cancelled turn that wrote state
   mid-turn leaves the SPA's head stale. Should the SPA reload state after every non-success terminal (spec `:257`
   already says so), so that the refusal is rare, not routine?
6. **Recompose forensics (ruling 3 spirit).** In the old plan a settled recompose row keeps neither its retried user
   row (`T10.md:961`) nor its bound base (`request_json` nulled, `contract.md:195-196`). A settled send keeps its base
   only through `ingress.requested_state_id`. Should recompose bind `user_message_id` while running (the trigger's
   NULL→value arm), and should the job keep a `base_state_id` column for both kinds? Also: does the recompose
   expected-row check move into the N06 precondition gate with the head check?
7. **FK actions inside the session cascade (N04 decides, N17 proves on PG).** Job→`chat_messages` is RESTRICT
   (contract). Ingress→`chat_messages` and ingress→`composition_states` are CASCADE (`models.py:472-483`). The new
   ingress→job FK needs an explicit action. The job's delete guard fires "while the session exists". On PostgreSQL,
   RESTRICT is checked per row during the cascade, so child-table order can fail a D7 session delete. Choose NO
   ACTION (or CASCADE) deliberately. The panel measured only SQLite.
8. **Web-review contention note.** Its source directory is untracked. Drop it or re-point it.
9. **Splitting N11.** Should the user-row/ingress binding writer and the turn function be separate tasks, or stay
   one task with two commits as T10 was?
10. **Inherited owner leftover (VERIFY `:162`).** T14's missing-session copy is still an owner decision.
