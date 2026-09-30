# Raise-path inventory: `POST /{session_id}/recompose`

Measured against `release/0.8.1` at `d479eb2b4` (`src/` and `tests/` clean). Read-only.
Spec: `docs/specs/2026-09-16-composer-async-operations-design.md` (amended, freeform-only).
Route: `src/elspeth/web/sessions/routes/composer/compose.py:86-829` (`async def recompose`, def at :90).
Sibling: `src/elspeth/web/sessions/routes/messages.py:112-1178` (`async def send_message`, def at :116).

Abbreviations: `C` = `compose.py`, `M` = `messages.py`, `H` = `src/elspeth/web/sessions/routes/_helpers.py`,
`PS` = `src/elspeth/web/sessions/routes/composer/pipeline_settlement.py`, `SV` = `src/elspeth/web/sessions/service.py`,
`LC` = `src/elspeth/web/coordination/lifecycle.py`, `APP` = `src/elspeth/web/app.py`, `CS` = `src/elspeth/web/composer/service.py`.

---

## 0. Instruments and controls

1. **AST Raise walker.** A script at a private scratch path (not tracked) walks every `ast.Raise` node inside a named
   function or class, nested functions included.
   - Positive control: `C::recompose` must contain the `:145` 400 raise. It does (first node listed).
   - Negative control: `H::_publish_progress` (`H:406-411`, a one-line `await progress(event)`) must report 0. It does.
   - Not-found control: `H::_nonexistent_fn` reports `NOT FOUND` instead of a silent 0.
   - Result: `recompose` has **16 Raise nodes** (`C:145, 147, 241, 274, 308, 342, 381, 421, 447, 464, 489, 523, 654, 780, 822, 826`).
     `send_message` has **19** (`M:186, 191, 291, 406, 448, 487, 521, 583, 641, 682, 694, 719, 775, 911, 1050, 1059, 1084, 1150, 1154`).
     Both match a line-by-line read of the two handlers.
   - **Limitation, stated plainly:** zero Raise nodes does not mean "cannot raise". Helpers with 0 nodes, such as
     `_handle_convergence_error`, `_handle_plugin_crash`, `_handle_runtime_preflight_failure` and `_handle_planner_failure`
     (`H:3064`, `3246`, `3416`, `2995`), still raise through the service writes they call. The service methods delegate to
     sync implementations that the walker did not follow past one level. The typed-exception taxonomy in §3 therefore
     comes from class hierarchy plus the app-level handlers, not from a closed transitive walk of `SV`.
2. **Code-line diff between the two turn bodies.** `difflib.SequenceMatcher` over comment- and blank-stripped lines of
   `C:186-767` and `M:275-1034`.
   - Negative control: the file diffed against itself gives ratio 1.0.
   - Positive control: it detects the known planner-progress divergence (item D7 in §5).
   - Result: 496 recompose code lines against 533 send code lines. **460 lines are identical (ratio 0.894).** Every
     non-equal opcode is a label, a variable name (`pre_send_state_id` vs `compose_base_state_id`), a headline word
     ("retry" vs "request"), or one of the structural divergences listed in §5.
3. **Absence checks with a positive control.** `grep -n "freeform_planner_progress_reason\|GuidedCustodyIntegrityError\|_join_shielded_task_after_cancellation\|AuditIntegrityError" C` returns nothing (exit 1). The same `freeform_planner_progress_reason` pattern hits `M` twice.
4. **FastAPI dependency order**, read from the installed FastAPI 0.136.1 `solve_dependencies` source: sub-dependencies
   (`for sub_dependant in dependant.dependencies`) are solved **before** path, query, header, cookie and body params.
   So `_track_compose_inflight`'s pre-yield code, meaning the ownership check and `registry.start_request`, runs
   **before** body validation. That matters once recompose gains a body.

---

## 1. Request DTOs (both routes gain `operation_id`)

### Recompose: **no request body today**

- The signature at `C:90-98` is `recompose(session_id: UUID, request: Request, user = Depends(get_current_user), rate_limiter = Depends(get_rate_limiter), _inflight_tally: None = Depends(_track_compose_inflight))`. It has no body parameter.
- The frontend sends no body: `src/elspeth/web/frontend/src/api/client.ts:917-927` does `authFetch(.../recompose, {method: "POST", headers: authHeaders("application/json"), signal})`.
- **Consequence:** a new strict DTO has to be introduced (for example `RecomposeRequest`) whose only field is `operation_id`. The request hash therefore binds only `{schema, session_id, kind, request: {}}`. Every same-ID retry hash-matches by construction. Two different IDs are two recompose actions, and the second one normally ends as a worker-side 409 because the first one appended an assistant row (`C:146-152`).

### Send: `SendMessageRequest`, `src/elspeth/web/sessions/schemas.py:145-160`

```python
class SendMessageRequest(_RequestModel):
    content: str = pydantic.Field(min_length=1, max_length=65536)
    state_id: UUID | None = None
    @field_validator("content") ... _require_visible_content(value, field_label="Message content")
```

- The base class `_RequestModel` (`schemas.py:68-71`) has `model_config = ConfigDict(extra="forbid")`. It is **not strict**: its docstring says "Tier 3 request base: allow coercion, reject unknown keys."
- The existing strict retry-id base is `_GuidedOperationRequest` (`schemas.py:74-91`). It uses `ConfigDict(strict=True, extra="forbid")` and `operation_id: str = pydantic.Field(min_length=36, max_length=36)`, with a canonical-UUID `field_validator` that requires `str(UUID(v)) == v`.
- The hash model the spec names is `guided_operation_request_hash` (`src/elspeth/web/sessions/guided_operations.py:17-44`). It **raises `AuditIntegrityError` unless the DTO is `strict=True` and `extra="forbid"`** (`:26-27`) and has `operation_id` (`:28-29`). It dumps with `exclude={"operation_id"}, exclude_unset=False, exclude_defaults=False, exclude_none=False` (`:30-36`).
- **Strict-mode trap:** under `strict=True`, a JSON string posted for `state_id: UUID | None` is rejected in python-mode validation. The in-repo precedent for a strict DTO with a UUID field is `RevertStateRequest` (`schemas.py:417-436`), which uses a `mode="before"` validator that parses only canonical strings. `SendMessageRequest.state_id` needs the same treatment when it goes strict. Otherwise every existing client that sends `state_id` gets a 422.

### Success DTO: `MessageWithStateResponse`, `schemas.py:228-238`

- Fields: `message: ChatMessageResponse`, `state: CompositionStateResponse | None = None`, `proposals: list[CompositionProposalResponse]`.
- Base: `_StrictResponse` (`schemas.py:54-65`, `ConfigDict(strict=True, extra="forbid")`).
- Both routes build it identically: `C:760-765` and `M:1027-1032`.

---

## 2. Ordered side effects of `/recompose` today

"Lock/lease held" means: **T** = `_track_compose_inflight` progress lease (request-scoped), **L** = local `asyncio.Lock`, **F** = `SessionOperationLease` COMPOSE fence.

| # | file:line | Step | Held |
|---|---|---|---|
| 0 | FastAPI | `get_current_user` (401 on failure), then `get_rate_limiter` (`src/elspeth/web/middleware/rate_limit.py:174-177`, a pure getter) | none |
| 1 | `H:2480` | `_track_compose_inflight`: `_verify_session_ownership` (first ownership check) | none |
| 2 | `H:2485` | surface label computed from `request.url.path` (`"/guided/"`, so `freeform`) | none |
| 3 | `H:2491-2492` | `registry.start_request(sid, user_id)`: **progress lease write** (DB row on PostgreSQL via `composer_progress_authority.py:497-514`, in-memory otherwise); stored on `request.state.composer_request_lease` | T |
| 4 | `H:2493` | `begin_composer_request_metrics(surface=...)` | T |
| 5 | `H:2587` | heartbeat task `renew()` starts (every 15 s, `H:2338`; lease 60 s, `H:2340`) | T |
| 6 | `C:106` | `rate_limiter.check(user.user_id)`: **consumes a bucket slot** (`rate_limit.py:100-139` in-memory, `:152-168` shared DB `admit`) | T |
| 7 | `C:107` | `_verify_session_ownership` again (second ownership check) | T |
| 8 | `C:108-111` | reads service, settings, `_request_plugin_policy_context(request, user)` (`H:372-379`, calls `plugin_snapshot_factory(user)`, which only reads `user.user_id`: `plugin_policy/availability.py:263-264`), and the profile registry | T |
| 9 | `C:112` | get the per-session `asyncio.Lock` (`H:311-349`, `352-364`) | T |
| 10 | `C:113-114` | **acquire L** (unbounded wait) | T, L |
| 11 | `C:115-121` | `SessionOperationLease.acquire(... COMPOSE ...)`: **fence write, fail-fast** (`LC:326-399`, authority `repository.py:4756-4840`); starts the renewal task (`LC:320-323`) | T, L, F |
| 12 | `C:124-130` | `service.get_current_state`, then `pre_send_state_id` = head id or `None` | T, L, F |
| 13 | `C:135` | `parse_completion_gates(state_record.composer_meta)`: Tier-1, deliberately outside every `try` | T, L, F |
| 14 | `C:142-143` | `service.get_messages(limit=None)`, then `_composer_conversation_messages` | T, L, F |
| 15 | `C:144-152` | **precondition**: 400 if empty, 409 if last conversational row is not `user` | T, L, F |
| 16 | `C:154-157` | `last_user_content`, `compartment_ingress_record`, `_chat_ingress_inputs`; `request_id = str(last user msg id)` | T, L, F |
| 17 | `C:158-165` | `_composer_progress_sink(...)`, which does `registry.claim_request(lease=request.state.composer_request_lease)` (`H:382-403`): **progress bind write** | T, L, F |
| 18 | `C:166-174` | publish `starting` progress (**progress write**) | T, L, F |
| 19 | `C:179-182` | guided-terminal transition detection (`_guided_terminal_for_compose`) | T, L, F |
| 20 | `C:184-185` | `_COMPOSER_REQUESTS_INFLIGHT +1`, `terminal_status="failed"`; **outer `try` begins at `C:186`** | T, L, F |
| 21 | `C:190` | `chat_messages = _composer_chat_history(records minus last user row)` | T, L, F |
| 22 | `C:202-215` | `_cancel_on_client_disconnect(request)` watcher around `composer.compose(...)`. The **provider turn** includes admission/quota (`CS:4161`, `_require_chargeable_admission` `CS:4045-4063`), the deadline (`CS:4163`, `loop.time() + self._timeout_seconds`), the planner path (`CS:4208-4234`, return at `CS:4218`; `timeout_seconds=self._timeout_seconds` at `CS:4459`) or the compose loop with the advisor END gate (`completion_gates=` at `C:214`), and **mid-turn persists** (`SV:6650 persist_compose_turn`, stale-checked at `SV:6780`) | T, L, F |
| 23 | `C:216-492` | typed failure arms (§3, rows R6-R15): persist partial state, audit or disposition rows, then raise | T, L, F |
| 24 | `C:493-511` | inner `finally`: drains attached `llm_calls` on unclassified exceptions (`plugin_crash_pending=True`) | T, L, F |
| 25 | `C:518-544` | post-compose guided `transition_consumed` flip (`InvariantError` at `C:522-526`), then `merge_composer_meta_updates` | T, L, F |
| 26 | `C:554-577` | if `result.pipeline_commit_intent`: `settle_auto_commit_intent(request=..., user=...)` (`PS:447-496`). **Proposal settlement writes**; on revocation the result is rebound to `PIPELINE_STAGED_REVIEW_MESSAGE` | T, L, F |
| 27 | `C:584` | `_turn_end = composer_turn_end_assistant_row(result)` (Tier-1 checks `H:1547`, `1560`) | T, L, F |
| 28a | `C:585-591` | settlement branch: state and assistant come from the settlement | T, L, F |
| 28b | `C:592-685` | version changed **or** `completion_gate_decision_changes(...)` (the **advisor gate** fact) leads to `validating` progress (`:595`), `_state_data_from_composer_state(... preflight_exception_policy="raise")` (`:605-620`, runtime preflight), `saving` progress (`:655`), then `commit_transition_response` (`:665`, stale-checked `SV:10168`) or `save_composition_state` (`:676`) | T, L, F |
| 28c | `C:686-717` | version unchanged but transition flip: `commit_transition_response` (`:706`) | T, L, F |
| 29 | `C:720-729` | `service.add_message("assistant", ...)` if not already written by the settlement or transition (**assistant row**) | T, L, F |
| 30 | `C:734-744` | `_persist_turn_audit_cohort(... plugin_crash_pending=False)`: **turn audit**, one `add_messages_atomic` transaction (`H:2060-2066`) | T, L, F |
| 31 | `C:745-756` | publish `complete` progress | T, L, F |
| 32 | `C:760` | `_pending_proposal_responses` (**read** after all writes; `SV:8320`) | T, L, F |
| 33 | `C:761-765` | `MessageWithStateResponse(...)` construction (strict pydantic) | T, L, F |
| 34 | `C:766-767` | `terminal_status="completed"`; `return` | T, L, F |
| 35 | `C:827-829` | outer `finally`: inflight -1, `_record_composer_request_terminal` | T, L, F |
| 36 | `LC:1074-1091` | **F `__aexit__` → `close()`**: join owned tasks, stop renewal, `release`. A renewal loss or release failure **raises here, after the response object exists**; a `BaseExceptionGroup` is raised if the body also failed | T, L |
| 37 | `C:113` | release L | T |
| 38 | `H:2627-2645` | dependency teardown: cancel heartbeat, `registry.finish_request(lease)` (`H:2639`), `finish_composer_request_metrics` (`H:2641`) | none |
| 39 | FastAPI / `APP:2247-2266` | `response_model` serialisation; errors go through app handlers (§3.3) | none |

**Key ordering facts for the plan**

- Every step from 11 to 36 already runs under F. Steps 12-19 are the reads and checks the spec calls "worker checks". They must move to after the worker has claimed the job, acquired F and marked it `running`.
- The progress sink (step 17) is **bound to the request-scoped T lease through `request.state`** (`H:398-403`). A worker has no `Request`, so it must own a `ComposerRequestLease` (`registry.start_request`) and a renewal loop of its own. That is exactly the `_track_compose_inflight` policy (`H:2442-2645`) the spec says to extract.
- **There is no single "final" transaction today.** The success tail commits in up to four separate transactions: settlement or state save (28a/28b/28c), assistant row (29), audit cohort (30). The response is then built from a read (32). The spec (§4, "Persist the public final response and transport terminal in the same session transaction as the final assistant/result publication") therefore needs either a new combined write or an explicit ruling on which transaction carries the terminal CAS. See §6, R-1.
- F is released (step 36) **after** the response exists. Today a renewal loss detected at close turns an already-persisted success into a 404 (`SessionOperationFenceLost`, `session_operation_handlers.py:23-26`) or a 500. The worker must decide whether the terminal CAS precedes or follows the lease close. See §6, R-2.

---

## 3. Raise inventory: `/recompose` and transitive helpers

Column key for *Disposition*: **PRE** = stable admission before 202 (stays synchronous); **POST** = worker check or turn outcome, projected into the terminal envelope; **REQUEUE** = worker leaves the job queued (spec §3); **NEW** = introduced by the cutover.
"Held" uses T, L and F as in §2.

### 3.1 Before the lock (dependency plus route preamble)

| # | file:line | Exception, status and body | Condition | Held | Disposition |
|---|---|---|---|---|---|
| P1 | auth dependency `get_current_user` | 401 | missing or invalid credential | none | **PRE** |
| P2 | `H:2801` via `H:2480` | `HTTPException(404, "Session not found") from None` | `service.get_session` raises `ValueError` (`SessionNotFoundError(ValueError)`, `SV:7111`, `protocol.py:3164`) | none | **PRE**, plus a worker recheck (POST 404) |
| P3 | `H:2805` via `H:2480` | `HTTPException(404, "Session not found")` | archived, wrong user, or wrong `auth_provider_type` | none | **PRE**, plus a worker recheck |
| P4 | `H:2491` via `composer_progress_authority.py:153,155` | `ComposerProgressSessionUnavailable` or `ComposerProgressIdentityInactive`, both **`PermissionError` subclasses** (`:113`, `:122`). No compose-route mapping exists (only `sessions.py:884`, `state.py:546-548` catch them). `PermissionError` is an `OSError` subclass with `errno=None`, so `APP:2174` re-raises it: **bare 500** (code reading, not executed) | identity revoked or session unavailable between auth and progress admission (PostgreSQL registry only) | none | Moves to the worker's operation-owned progress lease, so **POST** (generic, or map explicitly) |
| P5 | `H:2498` | `RuntimeError("Composer lifecycle requires an owning task")` | no current task (unreachable under ASGI) | T | defect |
| P6 | `rate_limit.py:128` | `HTTPException(429, {"error_type":"rate_limited","detail":..., "retry_after":n}, headers={"Retry-After"})` | per-user in-memory bucket full | T | **PRE** (recommended; see Q1) |
| P7 | `rate_limit.py:157` | `HTTPException(503, "Rate limit service unavailable") from None` | shared limiter `SQLAlchemyError` / `AsyncWorkerAdmissionTimeoutError` | T | **PRE** |
| P8 | `rate_limit.py:160` | `HTTPException(429, same body shape as P6)` | shared limiter refuses | T | **PRE** |
| P9 | `H:2801/2805` via `C:107` | 404 `"Session not found"` (second ownership check) | as P2 and P3 | T | **PRE** (collapses into one admission check) |
| P10 | FastAPI | 422 `RequestValidationError`, redacted by `APP:2203-2219` to `{"detail":[{type,loc,msg}], "request_id"}` | **new** `RecomposeRequest` body invalid (missing or non-canonical `operation_id`, extra key) | T is already started (§0.4) | **PRE / NEW** |
| P11 | new | 409 bound-ID mismatch (kind, actor, or hash) | same `operation_id` with a different binding | none | **PRE / NEW** |
| P12 | new | 429 queue full plus retry hint | durable capacity admission refuses | none | **PRE / NEW** |

### 3.2 Under the lock (L, F): turn body

| # | file:line | Exception, status and body | Condition | Held | Disposition |
|---|---|---|---|---|---|
| R1 | `C:113-114` | none; unbounded `asyncio.Lock` wait | another same-process compose on the session | T | worker queueing aid only (spec §3) |
| R2 | `C:115-121`, `repository.py:4799/4803/4809` | `SessionOperationConflictError` → `session_operation_handlers.py:28-30` **409 `{"detail":"Session operation is already active"}`** | live COMPOSE (or other) lease on the session, cross-instance | T, L | **REQUEUE**. The worker releases its claim and leaves the job queued; this must **not** become a terminal 409 |
| R3 | `C:115-121`, `repository.py:4790/4793/4800/4833` | `SessionOperationFenceLost` → `session_operation_handlers.py:23-26` **404 `{"detail":"Session not found"}`** | session row or fence missing, session archived with no live lease, or stale epoch | T, L | **POST** 404 envelope |
| R4 | `LC:342-343` | `TypeError("operation_kind must be an exact SessionOperationKind")` | defect | T, L | defect (generic) |
| R5 | `LC:361-380`, `LC:381-399` | cancellation during acquire (context released, then the cancellation re-raised); invalid context gives a validation error or `BaseExceptionGroup` | cancel or defect during acquire | T, L | POST (cancel path) or generic |
| R6 | `C:124` `service.get_current_state` (`SV:10204-10224`, 0 direct Raise nodes) | DB errors: `OperationalError` → `APP:2132-2160` **503 `{"detail":"Database is currently unavailable...","error_type":"database_unavailable","request_id"}`** | DB outage | T, L, F | **POST** 503 envelope |
| R7 | `C:129` `_state_from_record`, `src/elspeth/web/sessions/converters.py:43` | `ValueError(msg)` → bare 500 | corrupt state row (Tier 1) | T, L, F | **POST** generic `operation_failed` |
| R8 | `C:135`, `src/elspeth/web/execution/completion_gates.py:286-331` (14 Raise nodes) | `ValueError("Tier 1: ...completion_gates...")` → bare 500 | corrupt `composer_meta.completion_gates` | T, L, F | **POST** generic |
| R9 | `C:142` `service.get_messages` (`SV:9741-9788`) | DB errors as R6 | outage | T, L, F | **POST** |
| **R10** | **`C:145`** | **`HTTPException(400, detail="No messages to recompose from")`** (string detail, so no `request_id` injection, `APP:2247-2251`) | no conversational rows | T, L, F | **POST** 400 envelope (spec §3 names it a worker check) |
| **R11** | **`C:147-152`** | **`HTTPException(409, detail="Cannot recompose: the last message is not a user message. Recompose is only valid when the most recent message is the user turn whose composition failed.")`** | last conversational row is not `user` | T, L, F | **POST** 409 envelope |
| R12 | `C:159-165` → `H:398` → `progress.py:118-129` or `composer_progress_authority.py:536-546` | `RuntimeError("Composer request lease ownership mismatch")` or `ValueError("Composer request lease does not match the request")`; authority `bind_request` `ComposerRequestLeaseLost` (`:320`) or `PermissionError` subclasses (`:306/308`), giving bare 500 | progress lease lost or mismatched | T, L, F | **POST** generic (worker owns this lease) |
| R13 | `C:166-174` and every later `_publish_progress` (`H:406-411`) | as R12 via `publish` (`composer_progress_authority.py:341/343/355`) | progress lease lost. **Inside an `except` arm this replaces the typed HTTPException being built**, an existing masking hazard | T, L, F | **POST** generic |

### 3.3 Provider turn: `composer.compose(...)` at `C:202-215` and its typed arms

| # | file:line | Exception, status and body | Condition | Held | Disposition |
|---|---|---|---|---|---|
| R14 | `C:216-241` | `ComposerConvergenceError` → `_handle_convergence_error(..., "recompose_convergence", ...)` (`H:3064-3243`) → **`HTTPException(422, {"error_type":"convergence","detail":str(exc),"turns_used","budget_exhausted","reason","recovery_text", ["timeout_seconds"], ["failed_turn"], ["partial_state" \| "partial_state_save_failed","partial_state_save_error"]})`**. `terminal_status = "timed_out"` if `budget_exhausted == "timeout"` (`C:217`). Writes: partial state (`H:3192-3196`), audit cohort (`H:3233-3242`) | composition, discovery, or wall-clock budget exhausted (the three convergence reasons) | T, L, F | **POST** 422. **Note:** `timeout_seconds` is `settings.composer_timeout_seconds` (`H:3143`); under queue-consumed deadlines the honest value may be the remaining budget (Q4) |
| R15 | `C:242-281` | `litellm.AuthenticationError` → **`HTTPException(502, _litellm_error_detail("llm_auth_error", exc, expose_provider_error=...))`**: `{"error_type":"llm_auth_error","detail":<class name>, [provider_detail], [provider_status_code]}` (`H:758-807`) | provider auth rejected | T, L, F | **POST** 502 |
| R16 | `C:282-315` | `litellm.APIError` → **502 `llm_unavailable`** (same builder) | provider unavailable | T, L, F | **POST** 502 |
| R17 | `C:316-349` | `_BadRequestLLMError` (`CS:757`, a `ComposerServiceError` subclass) → **502 `llm_unavailable`**; headline "rejected this retry" | provider 4xx bad request | T, L, F | **POST** 502 |
| R18 | `C:350-381` | `ComposerPluginCrashError` (`protocol.py:605`) → `_handle_plugin_crash(..., "recompose", ...)` → **`HTTPException(500, response_body) from crash.original_exc`** | tool or plugin crash | T, L, F | **POST** 500 (typed body, not the generic envelope) |
| R19 | `C:382-421` | `ComposerRuntimePreflightError` (path 1, cached) → telemetry (`:390`) → `_handle_runtime_preflight_failure` → **500 typed body** | cached preflight failure re-raised | T, L, F | **POST** 500 |
| R20 | `C:422-447` | `PipelinePlannerError` (`pipeline_planner.py:540`, a bare `RuntimeError`) → `_handle_planner_failure` (`H:2995-3061`, writes one audit disposition row) → **`HTTPException(status, {"error_type":"composer_planner_failure","failure_code","planner_code","detail"})`**. Status from `H:2893-2924`: `cost_unavailable` 503, `provider_timeout` 504, `provider_unavailable` 503, `invalid_provider_response` 502, `planner_repair_exhausted` 500, **`policy_blocked` 422**, `operation_failed` 500 | freeform planner (empty state, explicit mutation intent, `CS:4208-4234`) failed | T, L, F | **POST** at the mapped status. Carries the policy refusal |
| R21 | `C:448-467` | `ComposerAdmissionRefused` (`CS:2421`, raised `CS:4051`, `CS:4063`) → **`HTTPException(403, {"error_type":"composer_admission_refused","failure_code":"admission_refused","detail":str(exc)})`** | identity disabled or **quota** (`assess_chargeable_operation`) | T, L, F | **POST** 403. Quota is the spec's "changed quota" worker check |
| R22 | `C:468-492` | `ComposerServiceError` (`protocol.py:465`, catch-all; also `CS:4152` composer unavailable, `CS:2664`) → **`HTTPException(502, {"error_type":"composer_error","detail":str(exc)})`** | composer unavailable or prompt prep failed | T, L, F | **POST** 502 |
| R23 | `C:493-511` | inner `finally` persists `llm_calls` from any unclassified exception. `_persist_llm_calls` success-path `AuditIntegrityError` (`H:1981`) cannot fire here (`plugin_crash_pending=True` swallows `SQLAlchemyError`, `H:1960-1976`) | — | T, L, F | audit only |
| R24 | escapes compose (no arm) | **`StaleComposeStateError`** (`protocol.py:3220`, `RuntimeError`) from `persist_compose_turn` `SV:6780` → `APP:1501-1516` **409 `{"error_type":"stale_compose_state","detail":"The session changed while the compose turn was running.","request_id"}`** | head moved during the turn | T, L, F | **POST** 409 (the spec's stale-state conflict) |
| R25 | escapes compose | **`AuditIntegrityError`** incl. `GuidedCustodyIntegrityError` (`contracts/errors.py:972`, `:1002`); e.g. `CS:4159` (context targets a different session), `CS:4063` (refusal with no reason) → `APP:1363-1410` **500 `{"error_type":"audit_integrity_error","detail":"ELSPETH stopped before replying...", "diagnostic":"no_failed_turn_metadata","reason":...,"request_id"}`**, or with `failed_turn` when `exc.failed_turn` is set. **Recompose has no route arm for this; send has one (`M:1035-1066`)** | Tier-1 audit refusal | T, L, F | **POST** 500 (must reproduce the app-handler body, see R-4) |
| R26 | escapes compose | `SessionOperationFenceLost` from any fenced write → **404 `"Session not found"`** | F lost mid-turn | T, L, F | **POST** 404 |
| R27 | escapes compose | `OperationalError` → 503 (`APP:2132`); `SecretDecryptionError` → **409 `secret_decryption_failed`** (`APP:2110-2130`); `FingerprintKeyMissingError` → **503 `fingerprint_key_missing`** (`APP:2085-2108`); retryable `OSError` → **503 `storage_unavailable`** (`APP:2162-2195`, errno in `_RETRYABLE_STORAGE_ERRNOS` `APP:231`) | infrastructure faults reachable from tool execution or preflight | T, L, F | **POST**, projected at the app-handler status and body |
| R28 | escapes compose | `TypeError` / `ValueError` / `RuntimeError` (`CS:4155` TypeError, `CS:4157` ValueError, `CS:4278` RuntimeError, …) → bare 500 | defects | T, L, F | **POST** generic `operation_failed` with diagnostic id |

### 3.4 Post-compose (still inside outer `try` `C:186`, which only catches `InvariantError` and `CancelledError`)

| # | file:line | Exception, status and body | Condition | Held | Disposition |
|---|---|---|---|---|---|
| R29 | `C:522-526` → caught `C:768-786` | `InvariantError` (`web/composer/guided/errors.py:34`) → slog `guided.invariant_violated` `site="recompose"` → **`HTTPException(500, {"error_type":"server_invariant_violated","detail":"Server invariant violated. See application audit log for diagnostic detail."})`** | transition gate fired with `_guided is None` (impossible state) | T, L, F | **POST** 500 typed |
| R30 | `C:555-565` → `PS:473` → `PS:199`, `201`, `203`, `226` | `HTTPException(409, "The pipeline proposal draft hash is stale or mismatched." / "...must be accepted through its guided workflow." / "...reviewed anchor is stale or mismatched." / "Only pending proposals can be accepted.")` | auto-commit intent fails an authority check | T, L, F | **POST** 409 |
| R31 | `PS:229` → `PS:144`, `149` | `HTTPException(409, "Stored proposal references a non-user originating message; ..." / "...could not be recovered; ...")`. (`PS:155-170` `_proposal_chat_ingress_inputs` is **not** reached: recompose passes non-None `composer_meta`, `C:562`, and the branch runs only when it is `None`, `PS:231`.) | proposal origin row missing or wrong | T, L, F | **POST** 409 |
| R32 | `PS:283-331` | `PipelineCommitError` → persists tool rows and rejects the proposal → **`HTTPException(409 if code in {"BASE_CONFLICT","NOT_PENDING"} else 422, detail=str(exc))`** (`PS:330-331`) | commit validation or executor mismatch | T, L, F | **POST** 409/422 |
| R33 | `PS:206`, `PS:302`, `PS:368` | `RuntimeError(...)` → bare 500 | settlement invariant defects | T, L, F | **POST** generic |
| R34 | `PS:467` → `SV:7799` | `ValueError("proposal uses the current tool-proposal lifecycle contract")` → bare 500 | intent points at a non-pipeline proposal | T, L, F | **POST** generic |
| R35 | `PS:392-406` → `SV:7946/8064/8068/8072` | `StaleComposeStateError` → **409 `stale_compose_state`** (`APP:1501`) | base moved before settlement | T, L, F | **POST** 409 |
| R36 | `PS:484-496` | `TrustModeAutoCommitRevokedError` is **caught** → `AutoCommitRevoked` → `C:573-577` rebinds the result (no raise) | trust mode downgraded | T, L, F | success path |
| R37 | `PS:326/329/348/408/411` | `asyncio.CancelledError` re-raised **after** the dispatch and settlement critical section drains (`_await_with_deferred_cancellation`, `PS:107-129`) | cancel during settlement | T, L, F | cancellation path (§3.5) |
| R38 | `C:584` → `H:1547`, `H:1560` | `AuditIntegrityError("Tier 1: ... turn-end ...")` → `APP:1363` 500 | turn-end row invariant broken | T, L, F | **POST** 500 (app-handler body) |
| R39 | `C:605-620` → `H:2719` → caught `C:621-654` | `ComposerRuntimePreflightError.capture(...)` (path 2) → `_handle_runtime_preflight_failure` → **`HTTPException(500, response_body) from rpf_exc.original_exc`** | post-compose runtime preflight crashed | T, L, F | **POST** 500 typed |
| R40 | `C:665` / `C:706` → `SV:10123` | `AuditIntegrityError("commit_transition_response requires guided_session.transition_consumed=true")` → 500 | defect | T, L, F | **POST** 500 |
| R41 | `C:665` / `C:706` → `SV:10168` | `StaleComposeStateError("commit_composition_response: ...")` → **409 `stale_compose_state`** | head moved (transition commit only) | T, L, F | **POST** 409 |
| R42 | `C:676` → `SV:9975`, `9980` | `TypeError` → 500; `SessionOperationFenceLost(TOKEN_MISMATCH)` → **404** | bad context or fence lost | T, L, F | **POST** |
| R43 | `C:721` → `SV:9410/9412/9414` | `TypeError` / `ValueError` → 500 | defect | T, L, F | **POST** generic |
| R44 | `C:734` → `H:2092` | `AuditIntegrityError("composer_turn_audit_cohort_persist_failed: ... after assistant row was persisted ...")` → 500. **Fires after the assistant row is durable** | `SQLAlchemyError` on the success-path cohort | T, L, F | **POST** 500 (app-handler body); the assistant row stays visible on reload |
| R45 | `C:760` → `SV:8348`, `8354` | `AuditIntegrityError(...proposal creation event...)` → 500 | proposal integrity | T, L, F | **POST** 500 |
| R46 | `C:761-765` | pydantic `ValidationError` (strict `_StrictResponse`) → bare 500; `terminal_status` stays `failed` because the flip at `C:766` comes after construction | projection defect | T, L, F | **POST** generic. **Must run before the terminal CAS** (spec §1: "prepare the validated public response before its final commit") |

### 3.5 Cancellation and timeout exits

| # | file:line | Exit | Condition | Held | Disposition |
|---|---|---|---|---|---|
| X1 | `C:202`, `H:2209-2335` | disconnect watcher `task.cancel(_CLIENT_DISCONNECT_CANCEL_MARKER)` (`H:2274`); uncancel/marking `H:2294-2299`; completion-race absorb `H:2301-2327` (may `raise asyncio.CancelledError()` at `H:2327` on an external cancel) | client closed the POST | T, L, F | **Removed for the worker** (spec §3: not mounted around a detached turn) |
| X2 | `C:787-802` | `CancelledError` arm: persists `llm_calls` via **`asyncio.shield` + `contextlib.suppress(CancelledError)`** | any cancel of the turn | T, L, F | worker cancellation path; audit must land **before** the terminal publication |
| X3 | `C:808-818` | `_composer_heartbeat_cancel_of(exc)` (`H:2395-2402`) selects progress `failed` (`H:2405-2418`) vs `client_cancelled`; `terminal_status` `failed` or `cancelled` | heartbeat or other cancel | T, L, F | worker: lease-loss becomes `worker_lost`/failed, Stop becomes `request_cancelled` |
| X4 | `C:819-825` | `HTTPException(499, "Client disconnected while the compose turn was running.")` | `_is_client_disconnect_cancel(exc)` (`H:2187-2194`) | T, L, F | **Gone** in the worker (no socket) |
| X5 | `C:826` → `H:2590-2612` | bare `raise` → dependency: marker `None` gives `terminal_status="cancelled"` and re-raise (`H:2592-2594`); heartbeat marker with `uncancel()>0` re-raises (`H:2602-2604`); `RuntimeError(...without a renewal failure)` (`H:2607`); `renewal_defect` re-raises the defect (`H:2611`); otherwise **`HTTPException(503, {"error_type":"database_unavailable",...})`** or **`{"error_type":"composer_request_lease_lost",...}`** (`H:2612` → `H:2421-2439`) | server shutdown, lease lost, renewal defect | T | worker: lease-loss becomes a terminal 503-shaped envelope or `worker_lost`; **the guided routes keep exactly this path** |
| X6 | `H:2613-2615` | `TimeoutError` re-raise, `terminal_status="timed_out"`; reaches `APP:2162` as `OSError` with `errno=None`, re-raised as bare 500 | a `TimeoutError` escaping the route | T | POST generic / timeout |
| X7 | `LC:1074-1091` | lease close raises the renewal error (`_renew_forever` `LC:664-692` records it via `_record_renewal_error` `LC:694-701`, which cancels owned tasks at `LC:699-701`), release error, or `BaseExceptionGroup("Session operation body and cleanup both failed", ...)` | F renewal lost or release failed | T, L | **POST**. The worker must order this against the terminal CAS (R-2) |
| X8 | `CS:4163`, `CS:4459` | the wall-clock budget is **computed inside `compose()` from settings**: `deadline = loop.time() + self._timeout_seconds`, and the planner gets `timeout_seconds=self._timeout_seconds`. Exhaustion surfaces as R14 (`budget_exhausted="timeout"`) or R20 (`TIMEOUT` → 504) | budget exhausted | T, L, F | **POST**; `compose()` must accept the remaining operation budget (R-3) |

---

## 4. Code shared with `send_message`

- **Imports.** The same helper set from `routes/_helpers.py` (`C:13-80` vs `M:15-95`), plus `settle_auto_commit_intent` (`C:81`, `M:96`) and `SessionOperationLease` / `SessionOperationKind.COMPOSE`.
- **Lock and lease block.** Identical in shape: `C:112-122` vs `M:144-154`.
- **Typed arms.** The same ten arms in the same order: convergence, auth, APIError, BadRequest, PluginCrash, RuntimePreflight, Planner, AdmissionRefused, ServiceError, finally-drain (`C:216-511` vs `M:380-745`). All of them delegate to the same `_handle_*` helpers and `_litellm_error_detail`. The parameters differ only in `log_prefix` or `site` and in the state-id variable name.
- **Post-compose tail.** Identical: guided flip, meta merge, auto-commit settlement, `_turn_end`, the three state branches, assistant row, audit cohort, `complete` progress, proposals, response (`C:518-767` vs `M:767-1034`).
- **Diff instrument (§0.2).** 460 of 496 recompose code lines are identical to send. This supports **one worker turn function parameterised by a small per-kind label record plus a kind-specific preamble**:
  - labels: `log_prefix` / `site` (`"recompose"` / `"compose"`, `"recompose_convergence"` / `"convergence"`); slog events `recompose_llm_*` / `compose_llm_*`; `telemetry_source`; the `endpoint` metric label (`"recompose"` / `"send_message"`, `C:184`, `828-829` / `M:265`, `1156-1157`); the "retry" / "request" headline wording.
  - preamble: see §5.

## 5. Divergences (where the two routes differ materially)

| ID | Recompose | Send | Note for the plan |
|---|---|---|---|
| D1 | no body (`C:90-98`) | `SendMessageRequest` (`M:118`) | recompose needs a new strict DTO |
| D2 | plugin snapshot and profile registry read **before** the lock (`C:110-111`) | inside the lock (`M:217-218`) | the worker derives them after the claim in both cases |
| D3 | precondition: read transcript (`C:142`), then 400/409 (`C:144-152`) | client `state_id` check: 404 ×2 byte-identical (`M:183-194`), then **user-row insert with same-transaction transcript** (`M:238-245`, **before the outer try** at `M:275`), then Tier-1 snapshot guard `AuditIntegrityError` (`M:289-296`, inside the try) | kind-specific preamble. Send's user insert must happen **after `running`** and exactly once per `operation_id` (spec §4) |
| D4 | none | auto-title owned task (`M:321-341`) plus bounded 2 s join in `finally` (`M:1173-1178`) | send-only; runs under F's owned tasks (`LC:650-662`) |
| D5 | `pre_send_state_id` = head (`C:127/130`), used as both compose base and LLM-audit state id | `compose_base_state_id` = head (`M:211`); `pre_send_state_id` may be client-asserted (`M:195`), used only for user-row provenance | same semantics for compose; a unified worker should use a single "compose base = head" variable |
| D6 | **no `GuidedCustodyIntegrityError` arm**; the custody refusal reaches `APP:1363` (500, `failed_turn` only if already annotated) | arm `M:1035-1066` annotates `failed_turn` from `_compose_result` (`M:1043-1048`) and raises 500 with `failed_turn` built by `_failed_turn_response_body` | public-body divergence today. A shared worker either adopts send's arm for both (behaviour change for recompose) or keeps the split; needs a ruling (Q3) |
| D7 | planner progress hard-codes `reason="provider_unavailable"` and one evidence string (`C:430-439`) | `freeform_planner_progress_reason(exc.code)` plus a `COST_UNAVAILABLE` branch (`M:654-674`) | progress UX drift, affecting the advisory snapshot only (the HTTP body is identical via `_handle_planner_failure`). Sharing the code fixes it; call it out as a visible change |
| D8 | cancellation persists through `asyncio.shield(...)` inside `contextlib.suppress(CancelledError)` (`C:790-817`): a second cancel **abandons the join** | `_join_shielded_task_after_cancellation(asyncio.create_task(..., name=...))` (`M:1102-1136`; helper `guided_operations.py:205`) drains despite repeated cancels | the worker should use send's form for both; this strengthens the recompose audit guarantee |
| D9 | none | `_compose_result` tracking (`M:274`, `M:746`) | only feeds D6 |
| D10 | outer try starts **after** progress bind and `starting` publish (`C:186`); precondition raises R10/R11 happen **before** the inflight counter (`C:184`) | outer try at `M:275`, after the user insert and `starting` publish | the route-level `_record_composer_request_terminal` does not count R10/R11 today; `_track_compose_inflight` does (its HTTPException arm, `H:2616-2623`) |

## 6. Design risks (current code vs the spec)

- **R-1: no single final transaction.** The success tail is 3-4 commits (`C:555-565` or `676`/`665`, then `721`, then `734`) plus a read (`760`). The spec's "persist the public final response and transport terminal in the same session transaction as the final assistant/result publication" has no existing transaction to join. Options are to fold the terminal CAS into `_persist_turn_audit_cohort`'s `add_messages_atomic` (but the response needs `proposals`, which are read after it), or to add a new combined service method. Either way this is new service surface, not a relocation.
- **R-2: lease close follows response construction.** `LC:1074-1091` can raise after `C:767`. A renewal loss at close today converts a persisted success into 404/500. The worker must decide whether the terminal CAS runs before the F close (the result is then durable but F loss is unobserved) or after it (a success can then become `failed` after its rows are durable). The spec requires both fences to be live for the final write.
- **R-3: the budget lives inside `compose()`.** `CS:4163` (`deadline = loop.time() + self._timeout_seconds`) and the planner's `timeout_seconds=self._timeout_seconds` (`CS:4459`) are recomputed per call from settings. Spec §3 ("queue time consumes that budget; the worker uses only the remaining time") needs a new `compose()` parameter (for example an absolute deadline or remaining seconds) threaded into both the loop and the planner. Otherwise a queued job gets a full fresh budget.
- **R-4: part of the error projection lives in Request-bound app handlers.** Many post-202 outcomes are rendered by `APP` exception handlers, not by route `HTTPException`s: `StaleComposeStateError` 409, `AuditIntegrityError` 500 (with `failed_turn` or `diagnostic`), `SessionOperationFenceLost` 404, `SessionOperationConflictError` 409, `OperationalError` 503, `SecretDecryptionError` 409, `FingerprintKeyMissingError` 503, and retryable `OSError` 503. **Every dict-detail HTTPException also gains `request_id` from `APP:2247-2266`.** All of them take `Request` (they read `request_id` and path; `OperationalError` also reads `request.app.state.session_engine.pool`). The worker adapter must re-implement these projections without a `Request`, and decide what replaces `request_id` (the spec's server-side diagnostic ID?). Otherwise "preserve the current public HTTP error semantics" (spec §5) cannot hold.
- **R-5: request-bound inputs in the turn body.** The body reads `request.app.state.*` in about 12 places (`C:108-111`, `158`, `193`, `233-236`, `363-366`, `413-416`, `608-613`, `646-649`). `settle_auto_commit_intent` and `settle_pipeline_proposal_under_compose_lock` take **`request: Request` and `user: UserIdentity`** (`PS:449-450`, `PS:175-176`) and read `request.app.state.{session_service, composer_service, settings, plugin_snapshot_factory, catalog_service, operator_profile_registry, scoped_secret_resolver, session_engine}` (`PS:196-273`, `372-378`, `421`). The worker needs an app-state handle, or those functions need an explicit dependency bundle. **`settle_pipeline_proposal_under_compose_lock` is also called from the manual proposal-approval route** (`src/elspeth/web/sessions/routes/composer/proposals.py:318`; the other caller is `PS:473`), so a signature change touches that caller too.
- **R-6: the progress sink is coupled to the request lease.** `_composer_progress_sink` casts `request.state.composer_request_lease` (`H:398-403`), which `_track_compose_inflight` sets (`H:2492`). The worker needs its own `registry.start_request` / `renew_request` / `finish_request` lifecycle. That is the policy block at `H:2499-2645` the spec says to extract, and the guided mounts (`guided_plan.py:338`, `guided.py:2941`, `guided.py:5872`, found by `grep -rn _track_compose_inflight src/elspeth/web`, which also returns the two freeform mounts `C:97` and `M:124` as positive controls) must keep calling the same extracted code.
- **R-7: `SessionOperationConflictError` is fail-fast today.** `repository.py:4797-4809` raises at acquire and maps to a synchronous 409. The spec's worker must catch it specifically, release its claim, and leave the job **queued** (REQUEUE), never publishing it as a terminal 409. `SessionOperationFenceLost` from the same acquire is terminal (404).
- **R-8: send's strict-DTO conversion.** `SendMessageRequest` is non-strict (`schemas.py:68-71`, `145`), and `guided_operation_request_hash` refuses a non-strict DTO (`guided_operations.py:26-27`). Going strict needs a `RevertStateRequest`-style `mode="before"` validator for `state_id` (`schemas.py:422-436`). Without it, JSON strings are rejected.
- **R-9: rate-limit placement and replay.** `rate_limiter.check` (`C:106`, `M:138`) consumes a bucket slot on every POST. Spec §2 lists authentication, ownership, body, capacity and insert as the admission checks, and does not name rate limiting. If rate limiting stays pre-202, a same-ID replay after a lost 202 consumes a second slot and can be refused 429 for a job that already exists. Recommend the order: look up the existing row, then rate-limit only on a new reservation.
- **R-10: progress `PermissionError` subclasses reach a bare 500.** `ComposerProgressIdentityInactive` and `ComposerProgressSessionUnavailable` (`composer_progress_authority.py:113`, `122`), whose docstrings say "routes answer with the opaque 401 / non-disclosing not-found", are unmapped on the compose routes. `APP:2174` re-raises them (errno `None`), so they surface as a bare 500 (code reading, not executed). The worker's owned progress lease inherits this unless the adapter maps them.
- **R-11: the `timeout_seconds` field in the convergence body** (`H:3149`) reports `settings.composer_timeout_seconds`. Once queue time consumes the budget, the elapsed or permitted provider budget differs from the configured one, and the SPA timeout copy names this number.

## 7. Open questions

- **Q1.** Does per-user rate limiting stay a pre-202 admission check (spec §2 does not list it), and does a same-ID replay bypass it?
- **Q2.** Recompose's request hash binds only `{session, kind, {}}`. Should the binding also carry the target user-message id observed at admission? That would pin the retry to "recompose *this* failed turn" rather than "whatever the last user row is when the worker runs". The spec treats the transcript as a worker check, so as written the answer is no.
- **Q3.** D6: should the shared worker give recompose send's `GuidedCustodyIntegrityError` → `failed_turn` 500 arm (a public-body change for recompose), or keep recompose falling through to the app-handler shape?
- **Q4.** R-11: which number should `timeout_seconds` report once queue time is deducted?
- **Q5.** R-2: does the terminal CAS precede or follow the `SessionOperationLease` close, and what is the terminal status when F is found lost only at close?
- **Q6.** R-4: what replaces the `request_id` injected by `APP:2247-2266` into terminal error envelopes: the operation id, a new diagnostic id, or nothing?
