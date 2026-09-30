# Raise-path inventory: `POST /{session_id}/messages` (`send_message`)

Tree: `release/0.8.1` @ `d479eb2b4` (src/ and tests/ clean at start). Read-only survey.
Spec: `docs/specs/2026-09-16-composer-async-operations-design.md` (freeform-only amendment, 2026-09-25).
All paths are repo-relative. `M` = `src/elspeth/web/sessions/routes/messages.py`, `H` = `src/elspeth/web/sessions/routes/_helpers.py`.

Handler: `M:116` `async def send_message(session_id: UUID, body: SendMessageRequest, request: Request, user: UserIdentity = Depends(get_current_user), rate_limiter: WebRateLimiter = Depends(get_rate_limiter), _inflight_tally: None = Depends(_track_compose_inflight)) -> MessageWithStateResponse` (decorator `M:112-115`, `response_model=MessageWithStateResponse`). Body ends `M:1178`.

Classification key (from spec §2/§3):
- **PRE-202** — stable admission: auth, ownership, strict DTO 422, bound-ID mismatch, capacity. Answer cannot change while waiting for the session lease.
- **POST-202** — terminal envelope: anything whose answer can change while waiting for the session lease, and everything after it.
- **INFRA** — transport/DB-fault classes that can fire at any await; they land wherever the await is (pre-202 if in admission code, terminal envelope if in the worker).

Lock column: `none` / `CRL` = composer request lease (`request.state.composer_request_lease`, opened by `_track_compose_inflight`) / `AL` = per-session `asyncio.Lock` / `SOL` = `SessionOperationLease` COMPOSE.

---

## 1. Exact order of side effects

### 1a. FastAPI dependency phase (before the handler body runs)

FastAPI 0.136.1 (`.venv`): `fastapi/routing.py:409-439` reads and JSON-decodes the body first (a JSON decode error raises `RequestValidationError` `json_invalid` → 422; any other body-parse failure → `HTTPException(400, "There was an error parsing the body")`). Then `solve_dependencies` (`fastapi/dependencies/utils.py:628` `for sub_dependant in dependant.dependencies:`) runs **every `Depends` first**, then path params (`:685`) and the pydantic body model (`:706` `await request_body_to_args`). So **strict-DTO 422 is decided AFTER auth, ownership, and the composer request lease have already run.** The spec's "auth → ownership → body 422 → capacity" order is not the current order.

| # | Step | Anchor | Side effect |
|---|------|--------|-------------|
| D0 | JSON decode of body | `fastapi/routing.py:409-439` | none (422 json_invalid / 400) |
| D1 | `get_current_user` | `src/elspeth/web/auth/middleware.py:78` | on failure: auth-failure audit write via `_record_auth_failure_after_rate_limit` (`middleware.py:60-75`); success stashes `request.state.auth_token`/`auth_claims` |
| D2 | `get_rate_limiter` (returns `app.state.rate_limiter`, no check) | `src/elspeth/web/middleware/rate_limit.py:174-177` | none |
| D3 | `_track_compose_inflight` pre-yield | `H:2442` | see D3a-D3e |
| D3a | ownership check #1 | `H:2480` `await _verify_session_ownership(session_id, user, request)` | read |
| D3b | open composer request lease | `H:2491` `lease = await registry.start_request(sid, user.user_id)`; `H:2492` stores it on `request.state.composer_request_lease` | PG: INSERT into `composer_inflight_requests` (`composer_progress_authority.py:146-167`); SQLite: in-memory `_leases`/`_inflight` (`progress.py:98-104`) |
| D3c | metrics begin | `H:2493` `begin_composer_request_metrics(surface=surface)` | telemetry |
| D3d | heartbeat task spawned | `H:2587` `heartbeat = asyncio.create_task(renew())` (renews every `_COMPOSER_HEARTBEAT_SECONDS=15.0`, `H:2338`) | periodic lease renew |
| D3e | `yield` | `H:2589` | — |
| D4 | path param + `SendMessageRequest` validation | `utils.py:685`, `:706`; DTO `src/elspeth/web/sessions/schemas.py:145-160` | none (422 after D3 already opened the CRL) |

### 1b. Handler body

| # | Step | Anchor | Lock held |
|---|------|--------|-----------|
| 0 | rate-limit check (consumes a bucket slot on success: `rate_limit.py:138` `bucket.append(now)`; PG path writes via `RepositoryRateLimitAuthority.admit`, `rate_limit.py:155`) | `M:138` | CRL |
| 1 | ownership check #2 (duplicate of D3a) | `M:140` | CRL |
| 2 | read `service`, `settings`; compute `chat_ingress` (pure) | `M:141-143` | CRL |
| 3 | get per-session lock object | `M:144` | CRL |
| 4 | **acquire `asyncio.Lock`** (first item of tuple `async with`) | `M:145-146` | CRL → AL |
| 5 | **acquire `SessionOperationLease` COMPOSE** (evaluated only after the AL is held) | `M:147-153` → `src/elspeth/web/coordination/lifecycle.py:326` → `repository.py:4756` (row UPDATE advancing `operation_epoch`) | CRL+AL → SOL |
| 6 | read current state | `M:157` `service.get_current_state` | CRL+AL+SOL |
| 7 | project state (`_initial_composition_state` `M:159` / `_state_from_record` `M:162`) | `M:158-162` | all |
| 8 | client `state_id` check (only if a current state exists **and** `body.state_id` is set) | `M:172-195` (`service.get_state` `M:184`) | all |
| 9 | `compose_base_state_id` | `M:211` | all |
| 10 | parse prior advisor completion gates (Tier-1, outside every `try`) | `M:216` `parse_completion_gates(...)` | all |
| 11 | plugin policy snapshot from `request.app.state` | `M:217-218` | all |
| 12 | guided→freeform transition detection | `M:224-227` | all |
| 13 | **user-message INSERT + transcript snapshot, one transaction** | `M:238-245` `service.add_message_with_transcript(... writer_principal="route_user_message", session_operation_context=...)` (`service.py:9524`) | all |
| 14 | chat ingress inputs (pure) | `M:246` | all |
| 15 | claim progress sink (PG: DELETE+INSERT snapshot row via `bind_request`) | `M:247-254` → `H:382-403` → `progress_authority.py:302-336` | all |
| 16 | publish `starting` progress | `M:255-264` | all |
| 17 | `_COMPOSER_REQUESTS_INFLIGHT.add(1)` — **outer `try` starts at `M:275`**; everything above this line is outside the route's own exception arms | `M:265`, `M:275` | all |
| 18 | transcript snapshot guard (Tier-1) | `M:289-296` | all |
| 19 | chat history projection (pure) | `M:303` | all |
| 20 | auto-title task spawned as an SOL-owned child (first message + default title only) | `M:321-341` `compose_operation_lease.create_task(maybe_auto_title_session(...))` | all |
| 21 | **provider/composer call**, inline in route task, inside the disconnect watcher | `M:357-379` `async with _cancel_on_client_disconnect(request): result = await composer.compose(...)` | all |
| 22 | typed failure ladder (see §2.5) | `M:380-722` | all |
| 23 | inner `finally`: drain LLM sidecars attached to the in-flight exception (not on CancelledError) | `M:723-745` | all |
| 24 | `_compose_result = result` | `M:746` | all |
| 25 | transition_consumed flip (guided terminal only) | `M:768-787` | all |
| 26 | build `_post_compose_meta` | `M:793-801` | all |
| 27 | **auto-commit settlement** when planner minted an intent (fenced writes: tool rows, proposal settle, state append, interpretation surfacing) | `M:807-830` → `routes/composer/pipeline_settlement.py:447` → `:173` | all |
| 28 | turn-end assistant draft (Tier-1 checks) | `M:837` `composer_turn_end_assistant_row(result)` (`H:1494`) | all |
| 29a | branch A: settlement already wrote state + message | `M:838-844` | all |
| 29b | branch B: version changed or advisor gate changed → `validating` progress `M:848-856`; build state data + runtime preflight `M:857-873`; path-2 preflight failure `M:874-911`; `saving` progress `M:912-920`; **state persist** — `commit_transition_response` (state+assistant in one txn) `M:921-930` or `save_composition_state(provenance="post_compose")` `M:932-939` | `M:845-941` | all |
| 29c | branch C: version unchanged, transition flip only → `commit_transition_response` | `M:943-975` | all |
| 30 | **assistant message INSERT** (if 29a/b/c did not already write it) | `M:977-986` `service.add_message(... "assistant" ..., writer_principal="compose_loop")` | all |
| 31 | **turn audit cohort** (tool rows + LLM sidecars, one txn, success-path Tier-1) | `M:994-1004` `_persist_turn_audit_cohort(... plugin_crash_pending=False ...)` | all |
| 32 | publish `complete` progress | `M:1005-1016` | all |
| 33 | **read pending proposals** (separate read, after the assistant commit) | `M:1027` | all |
| 34 | construct `MessageWithStateResponse` | `M:1028-1032` | all |
| 35 | `terminal_status = "completed"`; `return response` | `M:1033-1034` | all |
| 36 | outer `finally`: inflight counter -1, terminal metric, **bounded 2 s join of auto-title task; `.result()` re-raises its failure** | `M:1155-1178` (`.result()` at `M:1176`) | all |
| 37 | exit `SessionOperationLease` (joins owned tasks, stops renewal, releases fence) | `lifecycle.py:1074-1091` → `_close` `:734-761` | SOL → released |
| 38 | exit `asyncio.Lock` | `M:145` | AL → released |
| 39 | FastAPI response-model serialisation of `MessageWithStateResponse` | FastAPI | CRL |
| 40 | `_track_compose_inflight` teardown: cancel heartbeat, `registry.finish_request(lease)` `H:2639`, `finish_composer_request_metrics` `H:2641` | `H:2627-2645` | CRL → released |

"Advisor gate": there is no separate advisor call in the route. The advisor gate runs inside `composer.compose()`; the route consumes `result.advisor_gate_decision` at `M:846` (`completion_gate_decision_changes`) and `M:872` (passed into `_state_data_from_composer_state`), and seeds the prior fact at `M:216`/`M:378`.

---

## 2. Raise table

### 2.1 Dependency phase (before handler)

| file:line | exception / HTTP shape | trigger | lock held | class |
|---|---|---|---|---|
| `fastapi/routing.py:426-439` | `RequestValidationError` → 422 `{"detail":[{type,loc,msg}], "request_id"}` (redacted by `app.py:2203-2223`) | body not valid JSON | none | PRE-202 (DTO) |
| `fastapi/routing.py:443-447` | `HTTPException(400, "There was an error parsing the body")` | other body parse error | none | PRE-202 (DTO) |
| `auth/middleware.py:101`, `:118` | `HTTPException(401, "Missing or invalid Authorization header")` | header missing / malformed | none | PRE-202 (auth) |
| `auth/middleware.py:163` | `HTTPException(401, exc.detail)` | `AuthenticationError` | none | PRE-202 (auth) |
| `auth/middleware.py:152` | `HTTPException(503, exc.detail)` | `AuthProviderUnavailable` | none | PRE-202 (auth; retryable) |
| `auth/middleware.py:60-66` | `HTTPException` 429 re-raised from `check_auth_rate_limit` | auth-failure audit rate limit | none | PRE-202 (auth) |
| `H:2801`, `H:2805` (via `H:2480`) | `HTTPException(404, "Session not found")` | `get_session` `ValueError`/`SessionNotFoundError(ValueError)` (`sessions/protocol.py:3164`, raised `service.py:7111`); archived / other user / other auth provider | none | PRE-202 (ownership) |
| `H:2498` | `RuntimeError("Composer lifecycle requires an owning task")` → bare 500 | no current task | CRL | PRE-202 (defect) |
| `progress_authority.py:153`/`:155` via `H:2491` (PG only) | `ComposerProgressSessionUnavailable` / `ComposerProgressIdentityInactive` (both `PermissionError` → `OSError`; `app.py:2162-2176` re-raises non-retryable errno → **bare 500**) | ownership/identity revoked between D3a and the INSERT | none (lease being created) | PRE-202 (ownership race; currently 500, not 404/401) |
| `progress_authority.py:514` | bare `raise` of `CancelledError` after joining/cleaning a committed admission | cancel during start_request | none | INFRA |
| `utils.py:706` | `RequestValidationError` → 422 (same redacted body) | `SendMessageRequest` rules: `content` 1..65536 chars (`schemas.py:153`), visible-content validator (`schemas.py:156-159`), `state_id: UUID \| None`, `extra="forbid"` (`_RequestModel`, `schemas.py:68-71` — coercing, **not** `strict=True`) | **CRL already open** | PRE-202 (DTO) |

Heartbeat (CRL renewal, runs during all later steps, `H:2531-2585`): on failure it cancels the route task with a marker; `_track_compose_inflight` converts at `H:2590-2612`:

| file:line | shape | trigger | class |
|---|---|---|---|
| `H:2612` → `H:2421-2439` | `HTTPException(503, {"error_type":"database_unavailable", ...})` | transient renewal failures exhaust lease headroom (`H:2552-2562`) | POST-202 (worker owns this policy under the spec) |
| `H:2612` → `H:2434-2439` | `HTTPException(503, {"error_type":"composer_request_lease_lost", ...})` | `ComposerRequestLeaseLost`/`PermissionError` on renew (`H:2566-2577`) | POST-202 |
| `H:2611` | the renewal defect itself re-raised → bare 500 | any other renewal exception (`H:2578-2582`) | POST-202 |
| `H:2607` | `RuntimeError(... without a renewal failure)` | invariant | POST-202 |
| `H:2605`/`H:2594` | bare re-raise `CancelledError` | external cancel racing | INFRA |

### 2.2 Handler, before the locks (`M:138-144`)

| file:line | shape | trigger | lock | class |
|---|---|---|---|---|
| `rate_limit.py:128-136` (SQLite `ComposerRateLimiter`) / `:160-168` (PG `SharedRateLimiter`) | `HTTPException(429, {"error_type":"rate_limited","detail":..., "retry_after":n}, headers={"Retry-After": n})` | per-user composer bucket full (`settings.composer_rate_limit_per_minute`, `app.py:1908-1910`) | CRL | PRE-202 (capacity; per-user, not per-session) |
| `rate_limit.py:157` | `HTTPException(503, "Rate limit service unavailable")` | PG authority `SQLAlchemyError` / `AsyncWorkerAdmissionTimeoutError` | CRL | PRE-202 (INFRA) |
| `H:2801`/`H:2805` via `M:140` | 404 "Session not found" | same as D3a (second check, same request) | CRL | PRE-202 (ownership) |

### 2.3 Under AL, acquiring SOL (`M:145-153`)

| file:line | shape | trigger | lock | class |
|---|---|---|---|---|
| (asyncio) `M:146` | `CancelledError` while waiting on the AL (client disconnect is **not** watched here — the watcher starts at `M:357`; only heartbeat/external cancel can land) | heartbeat marker or shutdown | CRL | POST-202 (wait) |
| `repository.py:4799`, `:4803`, `:4809` | `SessionOperationConflictError` → `session_operation_handlers.py:28-30` → 409 `{"detail":"Session operation is already active"}` | another live COMPOSE/PROPOSAL/other exclusive lease on this session (other instance, other route family) | CRL+AL | POST-202 (spec §3: worker releases claim and leaves job queued — **not** a terminal) |
| `repository.py:4790`, `:4793`, `:4800` (`OWNER_INACTIVE`, archived), `:4833` (`STALE_EPOCH`) | `SessionOperationFenceLost` → `session_operation_handlers.py:23-26` → 404 `{"detail":"Session not found"}` | session deleted/archived after admission; fence race | CRL+AL | POST-202 |
| `repository.py:4772` | `ValueError("session_id must be a UUID")` → bare 500 | defect | CRL+AL | POST-202 (defect) |
| `lifecycle.py:343` | `TypeError(...)` → 500 | defect | CRL+AL | POST-202 (defect) |
| `lifecycle.py:379-380`, `:407-417` | cancellation / context-validation failures (possibly `BaseExceptionGroup`) → 500 | cancel during acquire; invalid minted context | CRL+AL | POST-202 |
| `lifecycle.py:1070` (`__aenter__`) | `RuntimeError("session operation lease is closing or closed")` | defect | CRL+AL | POST-202 |
| any `run_sync_in_worker` | `AsyncWorkerAdmissionTimeoutError(TimeoutError)` (`async_workers.py:76`, raised `:189`) — **no app handler for TimeoutError** → bare 500; `_track_compose_inflight` records `timed_out` (`H:2613-2615`) | worker pool saturated >1 s (`ADMISSION_WAIT_SECONDS`, `async_workers.py:24`) | wherever | INFRA |
| any DB call | `OperationalError` → `app.py:2132-2160` → 503 `{"error_type":"database_unavailable","request_id"}`; other `SQLAlchemyError` → bare 500 | DB outage | wherever | INFRA |

### 2.4 Under AL+SOL, before and around the user-message insert (`M:157-265`) — all outside the outer `try` (`M:275`)

| file:line | shape | trigger | lock | class |
|---|---|---|---|---|
| `M:186` | `HTTPException(404, "State not found")` | `body.state_id` unknown (`service.get_state` `ValueError`, `service.py:10799`) | all | POST-202 as coded (see risk R7) |
| `M:191` | `HTTPException(404, "State not found")` (byte-identical, IDOR) | `body.state_id` belongs to another session | all | POST-202 as coded (see R7) |
| `converters.py:43` via `M:162` | `ValueError` → bare 500 | corrupt state record (Tier 1) | all | POST-202 |
| `completion_gates.py:286-331` (14 sites) via `M:216` | `ValueError("Tier 1: ...")` → bare 500 | corrupt `composer_meta.completion_gates` | all | POST-202 (Tier-1, deliberate propagate) |
| `service.py:9644` via `M:238` | `AuditIntegrityError` → `app.py:1363-1410` → 500 `{"error_type":"audit_integrity_error", "diagnostic":"no_failed_turn_metadata", ...}` — **raised after commit: user row is durable** | read-your-own-write violated | all | POST-202 |
| `service.py:9636` (`_verify_guided_failure_audit_cohort`) via `M:238` | `AuditIntegrityError` family → 500 — also after commit | poisoned guided-failure cohort | all | POST-202 |
| fenced write in `add_message_with_transcript` | `SessionOperationFenceLost` → 404 "Session not found" | SOL lost | all | POST-202 |
| `progress.py:127` (SQLite) / `progress_authority.py:540` (PG) via `M:248` | `RuntimeError` / `ValueError` "lease ... mismatch" → 500 | CRL mismatch (defect) | all | POST-202 |
| `progress_authority.py:306`/`:308`/`:320` via `M:248` (PG) | `ComposerProgressSessionUnavailable` / `IdentityInactive` (→ bare 500 via OSError handler re-raise) / `ComposerRequestLeaseLost` (→ bare 500) | ownership revoked / CRL expired | all | POST-202 (**user row already committed**) |
| `progress_authority.py:341`/`:343`/`:355` via `M:255` and **every later `_publish_progress`** | same three classes → bare 500 | same | all | POST-202 (see R9) |

### 2.5 Inside the outer `try` (`M:275-1034`)

Pre-compose:

| file:line | shape | trigger | class |
|---|---|---|---|
| `M:291-296` | `AuditIntegrityError("Tier 1 audit anomaly: send_message transcript snapshot ...")` → not caught by `M:1035` (plain AuditIntegrityError, not Guided*) → app handler 500 `no_failed_turn_metadata` | snapshot does not end at the inserted user row | POST-202 |
| `lifecycle.py:659` via `M:322` | `RuntimeError("session operation lease is closing or closed")` → 500 | defect | POST-202 |

`composer.compose()` entry (`src/elspeth/web/composer/service.py:4112`) raises before the loop — walked:

| file:line | exception | route arm → HTTP | class |
|---|---|---|---|
| `service.py:4152` | `ComposerServiceError(availability.reason)` | `M:698-722` → 502 `{"error_type":"composer_error","detail":str(exc)}` | POST-202 (instance-local availability; see Q3) |
| `service.py:4155`, `:4157` | `TypeError`, `ValueError` | none → bare 500 | POST-202 (defect) |
| `service.py:4159` | `AuditIntegrityError("Composer session authority targets a different session")` | app handler 500 | POST-202 |
| `service.py:4051`, `:4063` (`_require_chargeable_admission`, called `:4161`) | `ComposerAdmissionRefused` (subclass of `ComposerServiceError`, `service.py:2421`) | `M:683-697` → **403** `{"error_type":"composer_admission_refused","failure_code":"admission_refused","detail":str(exc)}` | POST-202 ("changed quota" — note it is 403, not 429 as the spec's example list implies) |
| `service.py:4053`/`:4055` | `TypeError`/`ValueError` | bare 500 | POST-202 |
| `service.py:4062` | `AuditIntegrityError` | app 500 | POST-202 |
| `service.py:4278` | `RuntimeError("plugin crash breadcrumb requires the COMPOSE ...")` from `ComposerPluginCrashError` | bare 500 | POST-202 |
| `service.py:4255`, `:4322`, `:4338` | bare re-raise of `ComposerConvergenceError` / `ComposerPluginCrashError` / `(ComposerServiceError, LiteLLMAPIError)` after emitting progress | typed ladder below | POST-202 |

Typed ladder at the route (`M:380-722`), order matters (narrow before generic `ComposerServiceError`):

| arm (file:line) | raises | HTTP body | pre-raise side effects | class |
|---|---|---|---|---|
| `ComposerConvergenceError` `M:380` | `M:406` `HTTPException(422, detail=response_body)` | built by `H:3064` `_handle_convergence_error`: `{"error_type":"convergence","detail":str(exc),"turns_used","budget_exhausted","reason","recovery_text"}` + `timeout_seconds` (wall-clock reason only, `H:3142-3143`) + `failed_turn` (`H:3144-3145`) + `partial_state` / `partial_state_save_failed` / `partial_state_save_error` | progress `M:386`; partial-state save (`save_composition_state provenance="convergence_persist"`); audit cohort (`plugin_crash_pending=True`) | POST-202. Three reasons: composition / discovery / `convergence_wall_clock_timeout` (spec's "three convergence reasons") |
| `LiteLLMAuthError` `M:407` | `M:448` 502 | `_litellm_error_detail("llm_auth_error", ...)` `H:758`: `{"error_type","detail":<class name>}` + optional `provider_detail`/`provider_status_code` when `composer_expose_provider_errors` | slog; progress `provider_auth_failed`; LLM sidecars `M:439-446` | POST-202 |
| `LiteLLMAPIError` `M:456` | `M:487` 502 `llm_unavailable` | same builder | progress `provider_unavailable`; sidecars | POST-202 |
| `_BadRequestLLMError` `M:495` (`service.py:757`) | `M:521` 502 `llm_unavailable` | same builder (scrubbed `provider_detail`) | sidecars | POST-202 |
| `ComposerPluginCrashError` `M:529` | `M:583` 500 | `H:3246`: `{"error_type":"composer_plugin_error","detail":<static>}` + `failed_turn` + partial_state fields | partial save `plugin_crash_persist`; audit cohort; progress `plugin_crash` | POST-202 |
| `ComposerRuntimePreflightError` (path 1, cached) `M:584` | `M:641` 500 | `H:3416` `_handle_runtime_preflight_failure` (same shape family) | telemetry `M:607`; progress; partial save | POST-202 |
| `PipelinePlannerError` `M:642` (`pipeline_planner.py:540`, `RuntimeError` subclass) | `M:682` `HTTPException(status_code, detail)` | `H:2995` `_handle_planner_failure` → `{"error_type":"composer_planner_failure","failure_code","planner_code","detail"}`; status from `_FREEFORM_PLANNER_FAILURE_HTTP` `H:2893-2924`: cost_unavailable 503, provider_timeout 504, provider_unavailable 503, invalid_provider_response 502, planner_repair_exhausted 500, policy_blocked 422, operation_failed 500 | progress; one `role="audit"` disposition row (`H:3040-3048`) | POST-202 ("policy refusals") |
| `ComposerAdmissionRefused` `M:683` | `M:694` 403 | see above | progress `admission_refused` | POST-202 |
| `ComposerServiceError` (generic) `M:698` | `M:719` 502 `{"error_type":"composer_error","detail":str(exc)}` | — | progress `service_setup_failed`; sidecars | POST-202 |
| inner `finally` `M:723-745` | no explicit raise; `_persist_llm_calls` at `M:738` can raise `SessionOperationFenceLost` (404) / `AuditIntegrityError` (only when `plugin_crash_pending=False`; here it is `True`, so `SQLAlchemyError` is swallowed at `H:1959-1976`) — an escaping exception **replaces** whatever the ladder raised | — | — | POST-202 |

Secondary raises inside each `_handle_*` helper (not `ast.Raise` in the helper, but awaited calls): `_failed_turn_response_body` (`H:2810`, DB count), `_durable_completion_gates`, `save_composition_state` (`service.py:9975` TypeError, `:9980` `SessionOperationFenceLost(TOKEN_MISMATCH)`, `assert_guided_custody_persistable` → `GuidedCustodyIntegrityError`), `_persist_turn_audit_cohort` (`H:2092` AuditIntegrityError only on success path). `SQLAlchemyError` from the partial save is contained (`H:3200`, `H:3358`); `GuidedCustodyIntegrityError` deliberately is **not** (`H:3160-3169`). Any escaping secondary exception replaces the intended 422/500 typed body.

Post-compose (`M:746-1034`):

| file:line | shape | trigger | class |
|---|---|---|---|
| `M:775` | `InvariantError` (`web/composer/guided/errors.py:34`) → caught `M:1067` → `M:1084` 500 `{"error_type":"server_invariant_violated", ...}` | impossible transition state | POST-202 |
| `pipeline_settlement.py:199`, `:201`, `:203`, `:226` | `HTTPException(409, <string detail>)` (draft hash stale / guided surface / reviewed anchor stale / not pending) | auto-commit settlement (`M:808`) | POST-202 ("stale conflicts") |
| `pipeline_settlement.py:144`, `:149`, `:168`, `:170` | `HTTPException(409, <string>)` | originating message unrecoverable | POST-202 |
| `pipeline_settlement.py:331` | `HTTPException(409 if code in {BASE_CONFLICT, NOT_PENDING} else 422, detail=str(exc))` from `PipelineCommitError` | commit preparation rejected | POST-202 |
| `pipeline_settlement.py:206`, `:302`, `:368` | `RuntimeError` → bare 500 | Tier-1 invariants | POST-202 |
| `pipeline_settlement.py:326`, `:329`, `:348`, `:408`, `:411` | `asyncio.CancelledError` (deferred cancellation re-delivered after durable writes) → route `M:1091` | cancel during settlement | POST-202 |
| `service.py:7946`, `:8064-8072` (settle path) | `StaleComposeStateError` → `app.py:1501-1516` 409 `{"error_type":"stale_compose_state", ...}` | settlement base moved | POST-202 |
| `M:808` returns `AutoCommitRevoked` (not a raise) | response continues as review-path; `result.message` rebound `M:825-829` | trust mode downgraded | — |
| `H:1547`, `H:1560` via `M:837` | `AuditIntegrityError("Tier 1: ...")` → app 500 (not the `M:1035` arm: plain class) | persisted-assistant/turn-end mismatch | POST-202 |
| `H:2719` via `M:858` | `ComposerRuntimePreflightError.capture(...)` → rewrapped `M:875-880` → `M:911` 500 (path 2) | post-compose runtime preflight crashed | POST-202 |
| `service.py:10123` via `M:922`/`M:963` | `AuditIntegrityError("commit_transition_response requires ...")` → app 500 | defect | POST-202 |
| `commit_composition_response` (via `M:922`/`M:963`) | `StaleComposeStateError` (it takes `expected_current_state_id`) → 409 | head moved | POST-202 |
| `service.py:9975`/`:9980` via `M:933` | `TypeError` 500 / `SessionOperationFenceLost` 404 | defect / SOL mismatch | POST-202 |
| `assert_guided_custody_persistable` via `M:933` | `GuidedCustodyIntegrityError` → `M:1035` → if `failed_turn` derivable: `M:1059` 500 `{"error_type":"audit_integrity_error","detail":...,"failed_turn":{...}}`; else bare `raise` `M:1050` → app handler 500 | custody gate refused the tip | POST-202 |
| `service.py:9410`/`:9412`/`:9414` via `M:978` | `TypeError`/`ValueError` → 500 | defect | POST-202 |
| fenced write via `M:978`, `M:994` | `SessionOperationFenceLost` → 404 | SOL lost mid-turn | POST-202 |
| `H:2092` via `M:994` | `AuditIntegrityError("composer_turn_audit_cohort_persist_failed ...")` → app 500 | `SQLAlchemyError` on success path | POST-202 |
| `service.py:8348`, `:8354` via `M:1027` | `AuditIntegrityError` → app 500 | proposal row lacks exactly one creation event | POST-202 |
| pydantic at `M:1028-1032` | `ValidationError` → bare 500 | projection defect | POST-202 |

Inside `_compose_loop` / `_plan_and_stage_empty_pipeline` (**not walked** — see §6): known escaping classes by grep, with their current terminal handler: `StaleComposeStateError` from `persist_compose_turn` `service.py:6780` → 409 `stale_compose_state` (app `:1501`); `AuditIntegrityError` (24 sites in `composer/service.py`) → app 500; `InvariantError` (18 sites) → route `M:1067` 500; `SessionOperationFenceLost` → 404; `SecretDecryptionError` → 409 `secret_decryption_failed` (`app.py:2110`); `FingerprintKeyMissingError` → 503 (`app.py:2085`); `TypeError`/`ValueError`/`RuntimeError`/`BlobNotFoundError` → bare 500. `PipelineCandidatePolicyRejection` raise sites (`service.py:4814`, `:4818`, `:4845`) are inside `plan_guided_pipeline` (`service.py:4543`) — guided-only, not reachable from `send_message` by that function. Wall-clock: `compose()` sets its own deadline at `service.py:4163` from `self._timeout_seconds`; provider `wait_for` `TimeoutError`s are converted to `ComposerConvergenceError(budget_exhausted="timeout")` (`service.py:8285`, `:8438`, `:9282`, `:9403`, `:9541`), so **no `TimeoutError` escapes the walked callees** except `AsyncWorkerAdmissionTimeoutError` from DB offload (INFRA row above).

### 2.6 Route exception arms and exits (`M:1035-1178`, `lifecycle.py:1074`)

| file:line | shape | trigger | class |
|---|---|---|---|
| `M:1050` | bare re-raise of `GuidedCustodyIntegrityError` → app 500 | no `failed_turn` and no compose result | POST-202 |
| `M:1059-1066` | 500 `audit_integrity_error` + `failed_turn` | custody refusal after compose | POST-202 |
| `M:1084-1090` | 500 `server_invariant_violated` | any `InvariantError` in the try | POST-202 |
| `M:1150-1153` | `HTTPException(499, "Client disconnected while the compose turn was running.")` | CancelledError marked by the disconnect watcher (`H:2187`, `H:2293-2299`); sidecars + `cancelled` progress joined first (`M:1103-1136`) | POST-202 (disappears in async: no socket to watch) |
| `M:1154` | bare re-raise `CancelledError` → if heartbeat marker, `_track_compose_inflight` converts to 503 (`H:2612`); else genuine cancellation | heartbeat lease loss / shutdown | POST-202 |
| `H:2327` (`_cancel_on_client_disconnect` else-branch) | `asyncio.CancelledError()` re-raised after a completed compose if an external cancel raced | shutdown | INFRA |
| `M:1176` | `auto_title_task.result()` re-raises the task's exception from the outer `finally` — **replaces a successful return or the in-flight exception**: `AuditIntegrityError` (`_auto_title.py:328`, `:346`), DB write failure from `update_session_title` (`_auto_title.py:385`), `CancelledError` if the SOL renewal cancelled it (`lifecycle.py:694-701`) | first message only | POST-202 |
| `lifecycle.py:1085` | `__aexit__` re-raises cleanup failure when the body **succeeded**: renewal error (`SessionOperationFenceLost` → 404) or release error → **replaces the completed response after all rows committed** | SOL renewal lost late / release failed | POST-202 (see R4) |
| `lifecycle.py:1087` | `BaseExceptionGroup("Session operation body and cleanup both failed", [...])` → bare 500 (no handler for groups) | body raised AND close failed | POST-202 |
| `lifecycle.py:756` | `_preserve_failures(...)` (renewal loss, release, owned-task errors — including an auto-title failure a second time) | close | POST-202 |

---

## 3. App-level handlers that give non-`HTTPException` raises their public shape

A worker has no FastAPI exception handler; every row below must be reproduced by the envelope adapter (spec §5 "existing safe error projection").

| class | handler | status + body |
|---|---|---|
| `SessionOperationFenceLost` | `session_operation_handlers.py:23-26` | 404 `{"detail":"Session not found"}` |
| `SessionOperationConflictError` | `session_operation_handlers.py:28-30` | 409 `{"detail":"Session operation is already active"}` |
| `AuditIntegrityError` (incl. `GuidedCustodyIntegrityError`) | `app.py:1363-1410` | 500 `{"error_type":"audit_integrity_error","detail":...,("diagnostic","reason")\|("failed_turn"),"request_id"}` |
| `StaleComposeStateError` | `app.py:1501-1516` | 409 `{"error_type":"stale_compose_state","detail":"The session changed while the compose turn was running.","request_id"}` |
| `FingerprintKeyMissingError` | `app.py:2085-2108` | 503 `fingerprint_key_missing` |
| `SecretDecryptionError` | `app.py:2110-2130` | 409 `secret_decryption_failed` |
| `OperationalError` | `app.py:2132-2160` | 503 `database_unavailable` (reads `request.app.state.session_engine.pool`) |
| `OSError` (retryable errno only, `app.py:231`) | `app.py:2162-2201` | 503 `storage_unavailable`; any other errno (incl. `PermissionError` subclasses with no errno) re-raised → bare 500 |
| `RequestValidationError` | `app.py:2203-2223` | 422 `{"detail":[{type,loc,msg}],"request_id"}` |
| `StarletteHTTPException` | `app.py:2247-2266` | dict `detail` gets `request_id` injected; string `detail` passed through unchanged |

`CorruptPreferencesError` (`app.py:1412`), `AuditStory*` (`:1452`, `:1477`), `AuditAccessLogWriteError` (`:1518`), `RunAlreadyActiveError` (`:1981`) have handlers but no path from `send_message` was found.

---

## 4. `MessageWithStateResponse` construction

Schema `src/elspeth/web/sessions/schemas.py:228-238` (`_StrictResponse`: `strict=True, extra="forbid"`, `schemas.py:65`):
```
message: ChatMessageResponse
state: CompositionStateResponse | None = None
proposals: list[CompositionProposalResponse]
```

| field | built at | source |
|---|---|---|
| `message` | `M:1029` `_message_response(assistant_msg)` (`H:618`, defaults: `include_raw_content=False` → `raw_content` null, no tool outcomes, no rejections) | `assistant_msg` declared `M:805`; assigned by settlement `M:844` (`route_settlement.settlement.transition_message`, may be `None`), `commit_transition_response` `M:930` / `M:970`, or `add_message` `M:978` |
| `state` | `M:1030` `state_response` | declared `None` `M:803`; filled `M:839-842` (settlement state + `live_validation=route_settlement.validation`), `M:940` (`_state_response(new_state_record, live_validation=validation)`), `M:976` (`_state_response(_transition_record)`, no live validation); stays `None` when version and gate facts are unchanged |
| `proposals` | `M:1031` | `M:1027` `_pending_proposal_responses(service, session.id)` (`H:454-459`: `list_composition_proposals(status="pending")` → `project_composition_proposal`) — **a fresh read after the assistant and audit commits**, all pending proposals of the session, not just this turn's |

`live_validation` (warnings/suggestions from the in-request `validate()`) is transient — it is not persisted with the state row, so a completed operation must store the constructed response JSON rather than rebuild it from rows (consistent with spec §1).

---

## 5. `Request` coupling on the send path (input for the typed worker context)

The spec says the worker receives no `Request`. Current reads inside `send_message` (`awk` over `M:116-1178` for `request[.,)]|request=`):
`M:140` ownership (`_verify_session_ownership(session_id, user, request)` reads `request.app.state.session_service`/`settings`), `M:141`, `M:142`, `M:144` (lock registry on `app.state`), `M:217` (`_request_plugin_policy_context(request, user)` → `app.state.catalog_service`, `plugin_snapshot_factory(user)`, `operator_profile_registry`), `M:218`, `M:247` (progress registry), `M:250` (`_composer_progress_sink(..., request=request)` reads **`request.state.composer_request_lease`** at `H:398` — per-request state set by the dependency), `M:344` (`composer_service`), `M:357` (`_cancel_on_client_disconnect(request)` calls `request.receive()`), `M:398/401`, `M:565/568`, `M:633/636`, `M:861/866`, `M:903/906` (`scoped_secret_resolver`, `catalog_service`), `M:809` (`settle_auto_commit_intent(request=request, ...)`).
`settle_pipeline_proposal_under_compose_lock` (`pipeline_settlement.py:173`) takes `request: Request` and reads only `request.app.state.*` (`:196`, `:215`, `:234`, `:239`, `:245-249`, `:264-273`, `:372-378`, `:421`) — never `receive()`. The app-level handlers in §3 read `request.url.path`, `request.method`, `request_id` from scope.

---

## 6. AST completeness cross-check (instrument + controls)

Instrument: `ast.walk` over each named `def` (suffix-matched qualname, nested defs included) collecting every `ast.Raise` (`exc` unparsed; bare raise shown as `<bare raise>`). Script and spec kept at the session scratchpad (`raises.py`, `spec.json`; not in the repo). 81 functions across 17 files; **TOTAL 152 raise nodes**; every name resolved (no `NOT FOUND`). `acquire` in `coordination/repository.py` resolved to exactly one def (`4756-4843`); `get_session`/`get_state`/`add_message`/... in `sessions/service.py` each resolved to exactly one def.

Controls run:
- **Known-positive:** `send_message` output includes `M:186` (`HTTPException(404, 'State not found')`) and `M:291` (`AuditIntegrityError(...)`).
- **Known-negative:** `_publish_progress` (`H:406-411`, read by hand, no raise) → `raises=0`; a nonexistent name `does_not_exist_fn` → `!! NOT FOUND` (silent-empty is not possible).
- **Mutation:** copied `messages.py` to the scratchpad, inserted `raise RuntimeError('MUTATION_CONTROL')` after line 138 → `send_message` count went 19 → 20 and the inserted line was listed.
- **Cross-instrument:** `awk 'NR>=116 && NR<=1178 && /^[[:space:]]*raise( |$)/'` over `messages.py` → 19, equal to the AST count.

`send_message` raise nodes (19): `186, 191, 291, 406, 448, 487, 521, 583, 641, 682, 694, 719, 775, 911, 1050, 1059, 1084, 1150, 1154` — each appears in §2.

Walked callees with ≥1 raise (all rows in §2): `_track_compose_inflight` (14), `_verify_session_ownership` (2), `_cancel_on_client_disconnect` (2), `_persist_llm_calls` (1), `_persist_turn_audit_cohort` (1), `_state_data_from_composer_state` (1), `composer_turn_end_assistant_row` (2), `settle_pipeline_proposal_under_compose_lock` (16), `_proposal_user_message_content` (2), `_proposal_chat_ingress_inputs` (2), `SessionOperationLease.acquire` (6) / `__aenter__` (1) / `__aexit__` (2) / `_close` (1) / `create_task` (1) / `_renew_forever` (2), `repository.acquire` (8), `maybe_auto_title_session` (3), `ComposerServiceImpl.compose` (8), `_require_chargeable_admission` (5), `ComposerRateLimiter.check` (1), `SharedRateLimiter.check` (2), `get_current_user` (4), in-memory registry `claim/renew/finish_request` (1 each), DB registry `start_request` (1) / `claim_request` (1) / `finish_request` (1), progress authority `begin_request` (2) / `bind_request` (3) / `publish` (3) / `heartbeat_request` (3), session service `get_session` (1) / `get_state` (1) / `add_message_with_transcript` (1) / `add_message` (3) / `save_composition_state` (2) / `commit_transition_response` (1) / `list_composition_proposals` (2) / `add_messages_atomic` (3), `parse_completion_gates` (14), `state_from_record` (1).
Walked with 0 raises: `get_lock`, `_get_session_compose_lock_registry`, `_get_composer_progress_registry`, `_request_plugin_policy_context`, `_composer_progress_sink`, `_publish_progress`, `_chat_ingress_inputs`, `_composer_chat_history`, `_composer_conversation_messages`, `_is_client_disconnect_cancel`, `_composer_heartbeat_cancel_of`, `_composer_heartbeat_failed_progress_event`, `_composer_heartbeat_http_error`, all four `_handle_*`, `_freeform_planner_failure_code`, `freeform_planner_progress_reason`, `_llm_calls_from_exception`, `_litellm_error_detail`, `_state_response`, `_initial_composition_state`, `_message_response`, `_pending_proposal_responses`, `_failed_turn_response_body`, `_record_composer_request_terminal`, `_durable_completion_gates`, `merge_composer_meta_updates`, `_recovery_partial_state_response`, `_record_composer_runtime_preflight_telemetry`, `settle_auto_commit_intent`, `_join_shielded_task_after_cancellation`, `close`, `_record_renewal_error`, `get_rate_limiter`, `get_current_state`, `count_tool_responses_for_assistant_async`, `completion_gate_decision_changes`, `compartment_ingress_record`, `validation_errors_for_composer_surface`, registry `start_request`/`bind_request`/`publish` (in-memory), `DatabaseComposerProgressRegistry.renew_request`.

**Limits (stated plainly):**
- `ast.Raise` only. Implicit raise sites are listed by hand in §2: `auto_title_task.result()` `M:1176`; `__aexit__` cleanup failures; `run_sync_in_worker` admission timeouts; DB-driver exceptions from every `_run_sync` (`service.py:4692-4694`); pydantic `ValidationError` at `M:1028`; asyncio `CancelledError` at every await.
- **Not walked:** `ComposerServiceImpl._compose_loop` (`service.py:7110`), `_plan_and_stage_empty_pipeline`, `persist_compose_turn(_async)` (`service.py:6650`, `7006`), the session service's private transaction helpers (`_session_composer_mutation_transaction`, `_assert_state_in_session`, `commit_composition_response`, `settle_pipeline_composition_proposal`, `get_authoritative_pipeline_proposal`, `update_session_title`, `begin_provider_attempt`), `prepare_pipeline_proposal_commit`, `_persist_tool_invocations`, `_runtime_preflight_for_state`, `surface_pending_interpretation_reviews`. For that closure §2.5 gives a class → terminal-handler map, not per-site completeness. The whole-file raise tally for `composer/service.py` (43 bare, 24 `AuditIntegrityError`, 19 `TypeError`, 18 `InvariantError`, 14 `_MalformedLLMResponseError`, 9 `ComposerConvergenceError.capture`, 9 `RuntimeError`, 9 `ComposerServiceError`, 6 `ValueError`, 3 `PipelineCandidatePolicyRejection`, 2 `ComposerAdmissionRefused`, 2 `BlobNotFoundError`, 2 `_BadRequestLLMError`, 2 `_AdvisorCheckpointComposeDeadlineExpired`, ...) is a file-level count, not the reachable closure of `compose()`.
- Instance-wide provider-turn limit (spec asks): Python-only grep `grep -rn 'Semaphore' src/elspeth/web --include=*.py` → 0 hits (positive control `asyncio.Lock()` same scope → 9 hits). The only bounded pools found are the DB offload admission (`async_workers.py:19` `ADMISSION_CAPACITY = MAX_WORKERS + MAX_QUEUED`, 1 s wait) and the per-user rate limiter. No compose-specific concurrency cap was found.

---

## 7. Design risks (current code vs spec)

- **R1 — 422 is not before the lease.** Body validation runs after `_track_compose_inflight` has checked ownership and opened a composer request lease (FastAPI order, §1a). A 202 admission route that keeps these dependencies will open (and on PG, INSERT) a CRL for requests later rejected 422. The async POST should drop `_track_compose_inflight` (spec §3 says so) and must decide its own order.
- **R2 — Rate limit consumes on every call.** `rate_limit.py:138` appends on success; PG `admit` is a write. A same-ID retry/replay that re-runs `rate_limiter.check` will be double-charged and can 429 an idempotent replay of an already-accepted operation. Replay lookup must precede (or bypass) the rate-limit check, or the check must key on the operation.
- **R3 — Conflict 409 is synchronous today.** `SessionOperationConflictError` at acquire (`repository.py:4799/4803/4809`) answers 409 "Session operation is already active" (`session_operation_handlers.py:30`) while holding the AL. Under spec §3 the worker leaves the job queued instead. Tests asserting 409 on concurrent send need re-homing; the SPA no longer sees that 409 from `/messages`.
- **R4 — Success can be replaced after commit.** `__aexit__` (`lifecycle.py:1083-1085`) re-raises a cleanup failure when the body returned normally; the outer `finally` re-raises an auto-title failure (`M:1176`). Both happen after the assistant row, audit cohort, and state are committed. The spec requires the public response and transport terminal in the same transaction as the final publication; the worker must decide what a post-commit close/auto-title failure does to an already-built success.
- **R5 — SOL renewal loss does not cancel the provider call.** `_record_renewal_error` (`lifecycle.py:694-701`) cancels only `_owned_tasks` (the auto-title task). The route learns of loss at the next fenced write (`SessionOperationFenceLost` → 404, e.g. `service.py:9980`) or at close (`lifecycle.py:748-756`). Spec §1 "loss of either fence cancels work" is not current behaviour; the worker must wire `wait_until_lost()` (`lifecycle.py:642`) to the compose task.
- **R6 — Ownership is never rechecked under the lease.** Checked at `H:2480` and `M:140`, both before the AL. `_verify_session_ownership` needs a `Request` (`H:2788-2807`). Only the PG progress authority rechecks ownership on each publish (`progress_authority.py:306`, `:341`), and it answers with a bare 500. Spec §3 "rechecks session ownership ... under that authority" needs a request-free helper; archived/deleted is caught by `repository.acquire` (FenceLost → 404) but a different-user owner is not re-proven.
- **R7 — `state_id` check placement.** The client `state_id` 404 (`M:186`/`M:191`) runs only when a current state exists at lock time (`M:156-172`); with no current state `body.state_id` is ignored. The state→session binding is immutable, so it could be hoisted pre-202, but hoisting would newly reject a `state_id` that today is silently ignored when the session has no state. Plan must pick.
- **R8 — Response is not built in one transaction.** Assistant insert (`M:978`), audit cohort (`M:994`), and proposals read (`M:1027`) are separate transactions, and `state_response` is built from a record committed earlier (`M:933`/`M:922`). Spec §4 "persist the public final response and transport terminal in the same session transaction as the final assistant/result publication" requires restructuring (e.g. a final transaction that writes the terminal row only, after the response is built from committed reads, fenced by the running token).
- **R9 — Progress-publish failures are bare 500s.** Every `_publish_progress` (14 awaited call sites in `send_message`, plus one shielded task in the CancelledError arm `M:1124-1136`) and the sink claim can raise `ComposerRequestLeaseLost`/`ComposerProgressSessionUnavailable`/`ComposerProgressIdentityInactive` on PG with no handler on this route (they are handled only in `routes/composer/state.py:546-548` and `routes/sessions.py:884`). Spec §2 says progress is advisory; if the worker keeps publishing through the PG authority, a progress failure must not decide the terminal.
- **R10 — Compose owns its own deadline.** `compose()` computes `deadline = now + self._timeout_seconds` at `service.py:4163`; there is no parameter to pass a remaining budget. The planner path (`_plan_and_stage_empty_pipeline`) does not receive `deadline` at all. Spec §3's "worker uses only the remaining time" needs a new `compose()` parameter.
- **R11 — `request` in settlement.** `settle_auto_commit_intent` / `settle_pipeline_proposal_under_compose_lock` require a `Request` (`pipeline_settlement.py:449`, `:175`) though they read only `app.state`. The worker needs a typed app-state context threaded through them (also shared with `/recompose` and manual proposal approval, so a signature change touches those callers).
- **R12 — Disconnect watcher and 499 vanish.** `_cancel_on_client_disconnect` (`H:2210`) and the 499 arm (`M:1138-1153`) are request-socket concepts. The cancelled-path bookkeeping (`M:1091-1136`: shielded sidecar persist + cancelled progress, `uncancel` accounting at `H:2293-2299`) must move to the operation-owned cancellation path; the "client cancelled" terminal becomes `request_cancelled` from the cancel endpoint.
- **R13 — Heartbeat 503 policy.** `_track_compose_inflight` (`H:2442-2645`) owns the 503 `database_unavailable` / `composer_request_lease_lost` conversions (`H:2421-2439`, `H:2590-2612`) and metrics. The guided routes keep it (spec §3); the freeform worker needs the same failure semantics without a yield dependency.
- **R14 — Admission refusal is 403, not 429.** `ComposerAdmissionRefused` → 403 `composer_admission_refused` (`M:694`). The spec's example list ("400/404/409/429") does not include it; the envelope must preserve 403.
- **R15 — `AsyncWorkerAdmissionTimeoutError` is a `TimeoutError`.** It can escape any service DB call as a bare 500 while metrics record `timed_out` (`H:2613-2615`). Under a worker it must be classified (retryable infra vs terminal), not confused with the compose wall-clock timeout.
- **R16 — User row is durable before several failure points.** `add_message_with_transcript` commits before its own Tier-1 checks (`service.py:9636`, `:9644`), and the progress claim/publish (`M:248`, `M:255`) run before the outer `try` — a failure there leaves a committed user row with none of the route's cancelled/terminal bookkeeping. Spec §4 "a retry of the same ID never creates another originating user row" needs the operation row to record the inserted user message id (or equivalent) so a reclaimed/expired job never re-inserts.

## 8. Open questions

- Q1: Should the async `/messages` POST keep a composer request lease at all (it currently backs `inflight_requests`, the SPA's settlement signal), or should `inflight_requests` be driven by the operation row for freeform?
- Q2: Is the per-user composer rate limit (429) the spec's "capacity" admission, a separate pre-202 check, or replaced by the worker-queue 429? And does a replay of an existing `operation_id` bypass it?
- Q3: `ComposerServiceError` from `self._availability.available` (`service.py:4151-4152`) is an instance-local fact. Admission on instance A may pass while the worker on instance B is unavailable (and vice versa). Pre-202 check, worker terminal 502, or both?
- Q4: When `SessionOperationLease.__aexit__` fails after a successful compose (R4), what terminal does the operation publish — the built success (rows are committed) or the 404/500?
- Q5: Should `state_id` validation be hoisted pre-202 (R7), accepting the behaviour change for sessions with no current state?
- Q6: The guided-terminal transition branch (`M:224-227`, `M:768-787`, `M:921-975`) runs on the freeform `/messages` route. Does it stay in the async worker unchanged while guided is being retired, or is it out of scope?

Recompose contrast (one line, out of scope here): `/recompose` adds pre-compose transcript preconditions — 400 at `routes/composer/compose.py:145`, 409 at `:147-148` and has the same typed ladder shape.
