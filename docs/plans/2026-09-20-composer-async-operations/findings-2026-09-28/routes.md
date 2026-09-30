# Re-survey: the two freeform compose routes (`POST /messages`, `POST /recompose`)

- **Tree:** `release/0.8.1`, pinned tip `1effedab2`. `git rev-parse HEAD` returned
  `1effedab2e0af7e09a5e8b30c66bc46ceaa130d5` at the start of this run and again immediately before writing, so no
  anchor moved between the pin and HEAD.
- **Provenance of this file.** An earlier run of this same task left a draft here (mtime 19:17). This run treated
  it as a draft only and re-measured it: every backticked `file:line` anchor in it (740 tokens) was resolved and
  printed by an anchor checker, both raise counts and both `request`-use counts were rebuilt with fresh AST
  instruments, and the corrections listed in §2.5 were made. Instruments (`raises.py`, `requses.py`,
  `anchors.py`) live in the session scratchpad under `rs0928/`, not in the repo.
  - Anchor-checker controls: known-positive `M:181` printed `raise _ingress_receipt_conflict(existing_ingress)`;
    known-negative `M:9999` printed `!! MISS`, and an unknown file printed `??`. On the draft the only three misses
    were deliberate old-tree anchors (`M:1178`, `CS:4112`, `CS:4163`, all in §2.1's "old" column).
- **Read-only.** This file is the only write.
- **Inputs read:** spec `docs/specs/2026-09-16-composer-async-operations-design.md` (the **working-tree** copy,
  which carries uncommitted edits; its line anchors below refer to that copy), `contract.md`,
  `panel-2026-09-28/RULINGS.md`, `panel-2026-09-28/RECOMMENDATION.md`, and the old
  `findings/raise-inventory-send.md` and `findings/raise-inventory-recompose.md` (measured at `d479eb2b4`, used
  here only as a map).
- **Abbreviations:** `M` = `src/elspeth/web/sessions/routes/messages.py`, `C` =
  `src/elspeth/web/sessions/routes/composer/compose.py`, `H` = `src/elspeth/web/sessions/routes/_helpers.py`,
  `PS` = `src/elspeth/web/sessions/routes/composer/pipeline_settlement.py`, `SV` =
  `src/elspeth/web/sessions/service.py`, `LC` = `src/elspeth/web/coordination/lifecycle.py`, `RP` =
  `src/elspeth/web/coordination/repository.py`, `CS` = `src/elspeth/web/composer/service.py`, `APP` =
  `src/elspeth/web/app.py`, `SCH` = `src/elspeth/web/sessions/schemas.py`.
- **Held column:** `CRL` = composer request lease opened by `_track_compose_inflight`; `AL` = per-session
  `asyncio.Lock`; `SOL` = `SessionOperationLease` COMPOSE.

Commits that touched this seam since the old findings' tree `d479eb2b4` (an ancestor of HEAD, checked with
`git merge-base --is-ancestor`; list from `git log d479eb2b4..1effedab2 -- M C H PS SCH`):

```
7001600fe Remove Guided Composer and retain freeform onboarding
01d96af9a Move first-run tutorial Build to freeform Composer
166a83620 refactor(composer): extract provider gateway and migrate callers
ed84adf51 refactor(composer): extract admission and interpretation owners
8630db9b8 fix(composer): own provider and durable boundary outcomes
04c11a713 fix(composer): settle gateway failures and cancellation custody
```

---

## 1. Current facts

### 1.1 Signatures and DTOs

```python
# M:130-143
@router.post("/{session_id}/messages", response_model=MessageWithStateResponse)
async def send_message(
    session_id: UUID,
    body: SendMessageRequest,
    request: Request,
    user: UserIdentity = Depends(get_current_user),
    rate_limiter: WebRateLimiter = Depends(get_rate_limiter),
    _inflight_tally: None = Depends(_track_compose_inflight),
) -> MessageWithStateResponse:            # nested in register_message_routes (M:128)

# C:90-103
@router.post("/{session_id}/recompose", response_model=MessageWithStateResponse)
async def recompose(
    session_id: UUID,
    body: RecomposeRequest,
    request: Request,
    user: UserIdentity = Depends(get_current_user),
    rate_limiter: WebRateLimiter = Depends(get_rate_limiter),
    _inflight_tally: None = Depends(_track_compose_inflight),
) -> MessageWithStateResponse:
```

DTOs (`SCH`):

- `_RequestModel` (`SCH:66-69`) has `ConfigDict(extra="forbid")`, which coerces and is **not strict**.
- `_SessionOperationRequest` (`SCH:72-87`) has `ConfigDict(strict=True, extra="forbid")`,
  `operation_id: str = Field(min_length=36, max_length=36)` and a canonical-UUID validator. It is the base of
  `ForkSessionRequest` (`SCH:367`) and `RevertStateRequest` (`SCH:400`).
- `SendMessageRequest(_RequestModel)` (`SCH:143-159`): `content: str = Field(min_length=1, max_length=65536)`,
  `state_id: UUID | None = None`, `client_request_id: UUID` (`SCH:154`), and a visible-content validator.
- `RecomposeRequest(_RequestModel)` (`SCH:162-165`) has one field, `expected_user_message_id: UUID`. It has no
  `state_id`.
- `MessageWithStateResponse(_StrictResponse)` (`SCH:234-243`) has `message`, `state: ... | None = None`,
  and `proposals: list[...]`.
- `ChatMessageResponse.client_request_id: str | None = None` (`SCH:216`) projects the ingress key onto every chat
  row (`H:593`).

### 1.2 FastAPI phase (unchanged mechanism)

FastAPI is `0.136.1` (measured: `.venv/bin/python -c "import fastapi; print(fastapi.__version__)"`). The request
path in `.venv/lib/python3.13/site-packages/fastapi/` runs in this order on **both** routes:

0. **Body JSON decode first** (`routing.py:409-449`): a `JSONDecodeError` raises `RequestValidationError`
   `json_invalid` (422, `:427-441`); any other body-read failure raises `HTTPException(400, "There was an error
   parsing the body")` (`:445-449`). No dependency has run yet.
1. `solve_dependencies` (`routing.py:457`) walks `dependant.dependencies` first (`dependencies/utils.py:628`):
   1. `get_current_user` (`src/elspeth/web/auth/middleware.py:78`); on failure it may write an auth-failure
      audit row through `_record_auth_failure_after_rate_limit` (def `middleware.py:40`, body through `:75`);
   2. `get_rate_limiter`, a getter only (`src/elspeth/web/middleware/rate_limit.py:174-177`);
   3. `_track_compose_inflight` pre-yield:
      - ownership check #1 (`H:2594`);
      - `registry.start_request` (`H:2605`), which INSERTs on PG;
      - `request.state.composer_request_lease = lease` (`H:2606`);
      - `request.state.composer_durable_completed = False` (`H:2607`);
      - metrics begin (`H:2608`);
      - `heartbeat = asyncio.create_task(renew())` (`H:2702`; every `_COMPOSER_HEARTBEAT_SECONDS = 15.0`,
        `H:2452`, against a 60 s lease, `H:2454`).
2. Only then is the body validated (`dependencies/utils.py:706` `request_body_to_args`).

A 422 from `SendMessageRequest` / `RecomposeRequest` field validation is therefore decided **after** a CRL was
opened, as before. A malformed-JSON 422 is decided before any dependency.

### 1.3 `send_message`: ordered side effects (`M:134-1041`)

| # | Step | Anchor | Held |
|---|---|---|---|
| 0 | per-user rate limit; **consumes a slot** (memory: `bucket.append(now)` `rate_limit.py:139`) | `M:156` → `rate_limit.py:100-139` (memory) / `:152-168` (PG `admit`) | CRL |
| 1 | ownership check #2 | `M:158` → `H:2898-2917` | CRL |
| 2 | read `session_service`, `settings`; compute `chat_ingress` (pure) | `M:159-161` | CRL |
| 3 | get per-session lock object (lazy app-state registry) | `M:162` → `H:258-270`, `H:239-248` | CRL |
| 4 | **acquire AL** (first `async with` item) | `M:163-164` | CRL→AL |
| 5 | **acquire SOL COMPOSE** (fail-fast) | `M:165-171` → `LC:326-423` → `RP:4646-4733` (UPDATE of the fence row, epoch +1) | AL→SOL |
| 6 | **ingress receipt lookup** in a write-locked, SOL-fenced transaction | `M:173-179` → `SV:4539-4574` → `_existing_message_ingress_result` `SV:4508-4537` | all |
| 7 | 409 `_ingress_receipt_conflict` if a receipt exists (same content and same `requested_state_id` → `message_already_accepted`; otherwise `message_idempotency_conflict`, `SV:4532-4536`) | `M:180-181`, body built `M:111-125` | all |
| 8 | read the current head (unfenced), then project it | `M:184-188` (`SV:5157`) | all |
| 9 | client `state_id` check: **always runs when `state_id` is sent**, including when there is no head (comment `M:189-191`); missing or foreign → byte-identical 404 `"State not found"` | `M:192-202` (`SV:5857-5869`) | all |
| 10 | `pre_send_state_id` = client id, or head id when omitted/null | `M:202-204` | all |
| 11 | `compose_base_state_id` = actual head | `M:218` | all |
| 12 | parse prior completion gates (Tier-1, outside every `try`) | `M:223` → `completion_gates.py:264-334` | all |
| 13 | plugin policy snapshot and profile registry from `app.state` | `M:224-225` | all |
| 14 | **user row + ingress receipt + `sessions.updated_at` bump + transcript read, ONE transaction**, run as an **SOL-owned child task** (`name="send-message-ingress"`) joined through `_join_freeform_owned_task` | `M:236-251` → `SV:4576-4718`: receipt re-check `SV:4638-4646`; in-txn state asserts `SV:4648`, `SV:4655`; user row `SV:4662-4679`; receipt `SV:4680-4688` (→ `SV:1396-1423`); `updated_at` `SV:4692`; transcript `SV:4696-4698` | all |
| 15 | 409 on a racing receipt (`M:253-254`); `AuditIntegrityError` on an unknown outcome (`M:255-256`); re-raise any deferred caller cancel (`M:258-259`) — all **before** the outer `try` and before any progress sink exists | `M:253-259` | all |
| 16 | chat ingress inputs (pure) | `M:260` | all |
| 17 | progress sink: `registry.claim_request(lease=request.state.composer_request_lease, ...)` (PG: bind write) | `M:261-268` → `H:288-309` | all |
| 18 | publish `starting` | `M:269-277` | all |
| 19 | inflight gauge +1; `terminal_status="failed"`; **outer `try` at `M:289`** | `M:279-289` | all |
| 20 | Tier-1 transcript-snapshot guard | `M:303-310` | all |
| 21 | chat history projection | `M:317` | all |
| 22 | auto-title SOL-owned child (first message and default title only; not wrapped in `_capture_freeform_child`) | `M:335-355` | all |
| 23 | `_cancel_on_client_disconnect(request)` opens around the provider call | `M:584` → `H:2324-2449` | all |
| 24 | **provider turn** `composer.compose(...)`, inline in the route task | `M:585-605` → `CS:898-1113` (chargeable admission `CS:942`, deadline `CS:944`, planner `CS:994`, loop `CS:1008`) | all |
| 25 | post-provider settlement `settle_post_provider` spawned as an **SOL-owned child** (`name="send-message-post-provider-settlement"`), joined inside the watcher | `M:607-619` (closure `M:361-570`) | all |
| 25a | … `merge_composer_meta_updates` | `M:376-384` | all |
| 25b | … auto-commit settlement when an intent was minted (tool rows, proposal settle, state append, interpretation surfacing) | `M:390-412` → `PS:405-451` → `PS:131-386` | all |
| 25c | … turn-end assistant draft (Tier-1) | `M:419` → `H:1608-1681` | all |
| 25d | … branch: state from the settlement **or** `validating` progress, runtime preflight, `saving` progress, `save_composition_state(provenance="post_compose")` | `M:420-512` (save `M:503-510`) | all |
| 25e | … assistant row, unless the settlement wrote it | `M:514-523` | all |
| 25f | … turn audit cohort (tool rows and LLM sidecars in one txn) | `M:531-541` → `H:2102-2211` | all |
| 25g | … **read pending proposals, then build `MessageWithStateResponse`** | `M:551-556` | all |
| 25h | … publish `complete` | `M:557-568` | all |
| 26 | on a clean join: `request.state.composer_durable_completed = True`; `terminal_status = receipt.terminal_status` (a `Literal["completed"] = "completed"` field, `H:1352`) | `M:617-619` | all |
| 27 | typed failure ladder (see §1.6) | `M:620-895` | all |
| 28 | inner `finally`: persist the LLM sidecars attached to an unclassified in-flight exception (not on `CancelledError`) | `M:896-918` | all |
| 29 | re-raise `post_provider_error`; `InvariantError` when there is no receipt; **re-raise the deferred caller cancel**; `return` | `M:919-925` | all |
| 30 | outer `except InvariantError` → 500; `except CancelledError` arm | `M:926-1017` | all |
| 31 | outer `finally`: gauge −1, terminal counter, **2 s wait on auto-title; `.result()` re-raises; on timeout `.cancel()` without awaiting** | `M:1018-1041` (`.result()` `M:1039`, `.cancel()` `M:1041`) | all |
| 32 | exit SOL: `_close` gathers every owned task (`return_exceptions=True`, `CancelledError` results dropped, `LC:703-716`), stops renewal, releases; may raise | `LC:1074-1102` → `LC:734-761` | SOL→released |
| 33 | exit AL | `M:163` | AL→released |
| 34 | dependency teardown: cancel heartbeat, `registry.finish_request(lease)`, finish metrics | `H:2744-2762` | CRL→released |

### 1.4 `recompose`: ordered side effects (`C:94-750`)

| # | Step | Anchor | Held |
|---|---|---|---|
| 0 | rate limit (consumes a slot) | `C:111` | CRL |
| 1 | ownership #2 | `C:112` | CRL |
| 2 | service, settings, **plugin policy and profile registry, read before the lock** | `C:113-116` | CRL |
| 3 | lock object, **AL**, **SOL COMPOSE** | `C:117-127` | CRL→AL→SOL |
| 4 | head read and projection; `pre_send_state_id` = head id or `None` | `C:129-135` | all |
| 5 | parse completion gates (Tier-1) | `C:140` | all |
| 6 | full transcript read (unfenced), conversation projection | `C:147-148` | all |
| 7 | 400 no conversation / 409 last row not `user` / **409 `recompose_user_message_mismatch`** (last user row id ≠ `body.expected_user_message_id`) | `C:149-165` | all |
| 8 | ingress inputs; progress `request_id` = last user row id | `C:167-170` | all |
| 9 | progress sink claim (request-state coupled); publish `starting` | `C:171-187` | all |
| 10 | gauge +1; `terminal_status="failed"`; **outer `try` at `C:190`** | `C:188-190` | all |
| 11 | chat history (all rows except the retried user row) | `C:194` | all |
| 12 | disconnect watcher; **provider turn** inline; post-provider settlement as an SOL-owned child `recompose-post-provider-settlement` | `C:395-420` (closure `C:200-385`) | all |
| 13 | closure body mirrors send `25a-25h`: settlement `C:219-241`, turn-end `C:248`, state branch `C:249-337`, assistant `C:339-348`, cohort `C:353-363`, proposals and response `C:366-371`, `complete` `C:372-383` | | all |
| 14 | typed ladder `C:421-647`, inner `finally` `C:648-666`, re-raise and return `C:668-674` | | all |
| 15 | `except InvariantError` → 500 `C:675-693`; `except CancelledError` `C:694-747` | | all |
| 16 | outer `finally`: gauge −1, terminal counter (no auto-title) | `C:748-750` | all |
| 17 | SOL exit, AL exit, dependency teardown | as send 32-34 | |

Recompose writes **no ingress row** and inserts no user row. Its user-row target exists only as
`expected_user_message_id` (`C:158`) and as the progress `request_id` (`C:170`).

### 1.5 Ownership, lock, lease and fence facts that bear on the plan

- **Ownership** runs twice, both times before AL (`H:2594`, then `M:158` / `C:112`). It is never rechecked under
  SOL. `_verify_session_ownership(session_id: UUID, user: UserIdentity, request: Request) -> SessionRecord`
  (`H:2898-2917`) reads `request.app.state.session_service` and `.settings` (`H:2907`, `H:2913`) and 404s on a
  missing session (`SessionNotFoundError` is a `ValueError`, `src/elspeth/web/sessions/protocol.py:1651`), `archived_at`, a different
  `user_id`, or a different `auth_provider_type` (`H:2914-2915`).
- **SOL acquire outcomes** (`RP:4646-4733`):
  - `SessionOperationConflictError` when a live lease exists (`RP:4689`, `:4693`, `:4699`), mapped to 409
    `{"detail":"Session operation is already active"}` (`src/elspeth/web/session_operation_handlers.py:28-30`);
  - `SessionOperationFenceLost` for MISSING (`RP:4680`, `:4683`), OWNER_INACTIVE (archived, `RP:4690`) or
    STALE_EPOCH (`RP:4723`), mapped to 404 `{"detail":"Session not found"}` (`session_operation_handlers.py:23-26`);
  - `ValueError` defect (`RP:4662`).
- **Fence predicates on this path (corrected; the draft said "every fenced session write passes one site").**
  - In `SV`, every fenced write reaches `_require_session_operation_context_on_connection` (`SV:970-999`; today:
    exact triple + kind + `released_at IS NULL` + `lease_expires_at > now`, `SV:986-996`, nothing else) through
    one of three entry points: directly (`SV:1063`, `:1117`, `:4921`, `:5047`, `:5420`, `:5481`, `:5545`,
    `:5614`), via `_session_composer_mutation_transaction` (`SV:1002-1031`, call at `SV:1014`), or via
    `_require_session_write_authority_on_connection` (`SV:7165-7194`, call at `SV:7188`; callers `SV:1360`,
    `SV:1414`, `SV:1554`). Instrument: `grep -n "_require_session_write_authority_on_connection(\|_require_session_operation_context_on_connection(" SV`.
  - The send ingress transaction crosses the predicate **twice**: the outer `_session_composer_mutation_transaction`
    (`SV:4631-4636`) and again inside `_insert_message_ingress_receipt` (`SV:1414`).
  - **Not every write on this route funnels there.** Blob writes made by composer tools during the provider turn
    (inside `M:585` / `C:396`) are fenced by the repository predicate `_exact_active_predicates` (`RP:4845-4858`)
    through `_compare_and_swap_on_connection` (`RP:4889-4911`); callers include
    `src/elspeth/web/composer/tools/blobs.py:1440` and many sites in `src/elspeth/web/blobs/service.py`
    (`:2507` onwards). `persistence.md` in this folder owns the full family map (its §"Family R").
- **SOL renewal loss cancels real work.** `_record_renewal_error` cancels every owned task (`LC:694-701`). On
  both routes the owned tasks are:
  - the send ingress child (`M:236`);
  - the post-provider settlement child (`M:607`, `C:408`);
  - auto-title (`M:336`).

  A cancel inside the settlement child is captured by `_capture_freeform_child` (`H:1362-1372`) and re-raised by
  `_freeform_child_result` as `AuditIntegrityError("Freeform continuation cancelled before settlement")`
  (`H:1378-1379`). A child cancelled before it started takes the same path (`H:1447-1449`).

  The provider call itself (`M:585`, `C:396`) is **not** an owned task. SOL loss does not cancel it. It surfaces
  at the next fenced write, or at close (`LC:748-756`).
- **Body failure plus close failure escapes as an ungrouped-handler 500 (by reading; not executed against the
  route).** `__aexit__` (`LC:1074-1102`) re-raises a close failure when the body succeeded (`LC:1083-1085`), and
  when the body failed with a *different* exception it raises
  `BaseExceptionGroup("Session operation body and cleanup both failed", [body, cleanup])` (`LC:1097-1101`). With
  all-`Exception` members Python constructs an `ExceptionGroup` (measured:
  `type(BaseExceptionGroup('x',[AuditIntegrityError('a'),RuntimeError('b')])).__name__ == 'ExceptionGroup'`), and
  no `ExceptionGroup` handler is registered (the full `exception_handler(` list in `APP` and
  `session_operation_handlers.py` was read). So SOL loss **mid-settlement** most likely answers a bare 500, not
  `APP:1363`'s `audit_integrity_error` body: the route raises the `AuditIntegrityError` above, then `_close`
  raises the renewal error first (`LC:751-760`). The same holds for any typed `HTTPException` whose SOL close then
  fails. No test names this group (`grep -rln "body and cleanup both failed" tests/` → 0 files; the same pattern
  hits `LC:1099` as the positive control), and none was found asserting a 500 from a body-plus-close failure on
  either route.
- **Caller cancellation during settlement is deferred, not obeyed.** `_join_freeform_owned_task` (`H:1428-1450`)
  shields the child. On each caller cancel it calls `owner.uncancel()` and keeps the first `CancelledError`
  (`H:1443-1446`). After the settlement has committed, the route sets `composer_durable_completed = True`
  (`M:618`, `C:419`) and only then re-raises the deferred cancel (`M:923-924`, `C:672-673`).
- **A durable completion can still answer non-200.** Three paths:
  - (a) a heartbeat cancel delivered after `M:618` reaches the arm at `M:945-952` (bare `raise`); the dependency
    then sets `terminal_status="completed"` (`H:2720`) **but raises the 503** (`H:2727`). That a 503 raised in
    dependency teardown reaches the client is pinned by
    `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py:323-353`;
  - (b) an SOL close failure after a normal return is re-raised by `__aexit__` (`LC:1083-1085`);
  - (c) an auto-title failure `.result()` (`M:1039`) replaces a completed response.
- **Auto-title after the route's 2 s wait.** On timeout the route calls `auto_title_task.cancel()` (`M:1041`)
  without awaiting; the SOL close then gathers it (`LC:708`) and drops the `CancelledError` result
  (`LC:711-712`), so the timeout does not fail the close. The auto-title cancel arm still settles its provider
  charge under the same SOL before re-raising (`src/elspeth/web/sessions/_auto_title.py:435-446`). Its session writes
  funnel into `SV:970`: `begin_provider_attempt` (`SV:5443`; predicate `SV:5481` inside
  `_begin_provider_attempt_sync` `SV:5469`), `cancel_undispatched_provider_attempt` (`SV:5528`, predicate
  `:5545`), `settle_provider_attempt` (`SV:5581`, predicate `:5614`), and `update_session_title` (`SV:2124`, via
  `_session_composer_mutation_transaction`), called at `_auto_title.py:486`. The exception is the quota-refusal
  recorder on the refusal path, `_record_provider_attempt_quota_refusal_sync` (`SV:5502-5513`, called from
  `SV:5458`), which reads the session unfenced and writes through `_quota_exceeded_recorder`, not through `SV:970`.

### 1.6 Typed failure ladder (identical arm order on both routes)

| Arm | send | recompose | HTTP |
|---|---|---|---|
| `CancelledError` (watcher) → re-raise `post_provider_error from watcher_cancel`, else bare | `M:620-623` | `C:421-424` | — |
| `ComposerConvergenceError` → `_handle_convergence_error` (`H:3144`) | `M:624-650` | `C:425-450` | 422 |
| **`OpenAIError`** → `_handle_composer_provider_failure` (`H:717-799`) | `M:651-661` | `C:451-461` | **502, or 504 when `failure.kind == "timeout"` (`H:799`)**; `guidance` added and provider detail suppressed for gateway/timeout (`H:791-798`) |
| `_BadRequestLLMError` (now from `composer/provider_gateway.py`) | `M:662-695` | `C:462-495` | 502 `llm_unavailable` |
| `ComposerPluginCrashError` → `_handle_plugin_crash` (`H:3318`) | `M:696-750` | `C:496-527` | 500 typed |
| `ComposerRuntimePreflightError` path 1 → `_handle_runtime_preflight_failure` (`H:3488`) | `M:751-808` | `C:528-567` | 500 typed |
| `PipelinePlannerError` → `_handle_planner_failure` (`H:3077`); status map `_FREEFORM_PLANNER_FAILURE_HTTP` (`H:2988`) | `M:809-846` | `C:568-593` | mapped (422/500/502/503/504) |
| **`ChargeableAdmissionRefused`** (raised `src/elspeth/web/coordination/quota_authority.py:165`) → `_handle_composer_chargeable_refusal` (`H:802-858`) | `M:847-855` | `C:594-602` | **503 `token_accounting_unavailable`** (`H:835-843`) or 403 `admission_refused` (`H:855-858`) |
| `ComposerAdmissionRefused` (`CS:942` → `composer/chargeable_admission.py:25,37`) | `M:856-870` | `C:603-622` | 403 |
| `ComposerServiceError` (generic, also `CS:933` availability) | `M:871-895` | `C:623-647` | 502 `composer_error` |
| path-2 preflight (inside the settlement child) | `M:456-493` | `C:285-318` | 500 typed |
| `InvariantError` (outer) | `M:926-943` | `C:675-693` | 500 `server_invariant_violated` |
| `CancelledError` (outer): `composer_durable_completed` → 499 "after completed" or bare; else shielded LLM-sidecar persist + shielded `cancelled`/heartbeat-failed progress, then 499 (disconnect) or bare | `M:944-1017` | `C:694-747` | 499 / bare |

Classes with no route arm reach the app handlers. None of these handlers exists in a worker:

- `AuditIntegrityError` (`APP:1363`), 500 `audit_integrity_error`;
- `StaleComposeStateError` (`APP:1501-1516`), 409 flat `{"error_type":"stale_compose_state","detail":"The session changed while the compose turn was running.","request_id"}`;
- `FingerprintKeyMissingError` (`APP:2088`);
- `SecretDecryptionError` (`APP:2113`);
- `OperationalError` (`APP:2135`), 503 `database_unavailable`;
- `OSError` (`APP:2165`);
- `RequestValidationError` (`APP:2206`);
- `StarletteHTTPException` (`APP:2245-2264`), which injects `request_id` into any dict `detail`;
- `SessionOperationFenceLost` and `SessionOperationConflictError` (`session_operation_handlers.py:23`, `:28`);
- **no handler** for `ExceptionGroup`, `RuntimeError` or `ValueError` (bare 500).

### 1.7 Lifecycle helpers: map at the pinned tip

| Helper | Anchor | Facts |
|---|---|---|
| `_track_compose_inflight(session_id: UUID, request: Request, user: Annotated[UserIdentity, Depends(get_current_user)]) -> AsyncIterator[None]` | `H:2556-2762` | Exactly **two** mounts: `M:142`, `C:102`. Instrument: `grep -rn _track_compose_inflight src/`; positive control: it hits the def `H:2556`. The rest are an import each (`M:85`, `C:74`), comments (`M:983`, `C:724`, `composer/progress.py:135`) and `H:3920` (the export list). `surface` is hard-coded `"freeform"` (`H:2599`). Raise nodes: 14 (AST). Conversions: heartbeat cancel → 503 (`H:2727`) or the renewal defect itself (`H:2726`); `RuntimeError` guards `H:2613`, `H:2722`. Terminal status honours `request.state.composer_durable_completed` in every arm (`H:2708-2742`). |
| heartbeat constants and markers | `H:2452-2553` | `_COMPOSER_HEARTBEAT_SECONDS = 15.0` `H:2452`; `_COMPOSER_REQUEST_LEASE_SECONDS = 60` `H:2454`; `_ComposerHeartbeatTimer` `H:2464-2477`; `_ComposerHeartbeatCancel` + 3 singletons `H:2480-2506`; `_composer_heartbeat_cancel_of` `H:2509`; `_composer_heartbeat_failed_progress_event` `H:2519`; `_composer_heartbeat_http_error` `H:2535` (503 `database_unavailable` for transient exhaustion, else 503 `composer_request_lease_lost`) |
| `_cancel_on_client_disconnect(request: Request) -> AsyncIterator[None]` | `H:2323-2449` | Calls `request.receive()` (`H:2371`). Marker `_CLIENT_DISCONNECT_CANCEL_MARKER` (`H:2298`). Uses `uncancel()` (`H:2409`, `H:2436`). Raise nodes (AST, 2): `H:2414` (bare), `H:2441` `CancelledError()`. |
| `_composer_progress_sink(registry, request: Request, *, session_id: str, request_id: str \| None, user_id: str) -> ComposerProgressSink` | `H:288-309` | The only request coupling is `cast(ComposerRequestLease, request.state.composer_request_lease)` at `H:305`, which the dependency sets at `H:2606`. The testcontainer probe calls it positionally (`tests/testcontainer/web/test_cross_process_composer_postgres.py:74`). |
| `_get_session_compose_lock_registry(request) -> _SessionComposeLockRegistry` | `H:258-270` | **Lazy**, created on first use under `request.app.state.session_compose_lock_registry` (`H:268-269`). |
| `_get_composer_progress_registry(request)` | `H:273-275` | `request.app.state.composer_progress_registry` |
| `_request_plugin_policy_context(request, user) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]` | `H:278-285` | `app.state.catalog_service`, `plugin_snapshot_factory(user)`, `operator_profile_registry` |
| freeform custody helpers (NEW since `d479eb2b4`) | `H:1347-1450` | `_FreeformContinuationReceipt` `H:1347-1352`, `_FreeformChildFailure` `H:1355-1359`, `_capture_freeform_child` `H:1362`, `_freeform_child_result` `H:1375`, `_join_shielded_task_after_cancellation` (overloads `H:1384-1392`, impl `H:1392-1425`), `_join_freeform_owned_task` `H:1428` |
| `_failure_log_request_id(request)` | `H:2311-2320` | Defined; no caller in `src/` (`grep -rn "_failure_log_request_id(" src/` hits only the def). Pinned by `tests/unit/web/test_sessions_composer_attribute_contracts.py:165-169`. |

### 1.8 Every `Request` use in the two handlers (AST instrument)

The instrument walks every `Name('request')` in the handler, climbs its attribute chain, and classifies it as a
chain load or store, a positional argument, or a keyword argument.

- Negative control: `_publish_progress` → `COUNT 0`.
- Handler counts: `send_message` **23**, `recompose` **23** (re-run this session; identical to the draft).

| Use | send (`M`) | recompose (`C`) |
|---|---|---|
| `_verify_session_ownership(..., request)` | 158 | 112 |
| `request.app.state.session_service` | 159 | 113 |
| `request.app.state.settings` | 160 | 114 |
| `_get_session_compose_lock_registry(request)` | 162 | 117 |
| `_request_plugin_policy_context(request, user)` | 224 | 115 |
| `request.app.state.operator_profile_registry` | 225 | 116 |
| `_get_composer_progress_registry(request)` | 261 | 171 |
| `_composer_progress_sink(..., request=request)` (reads `request.state.composer_request_lease`) | 264 | 174 |
| `request.app.state.composer_service` | 358 | 197 |
| `settle_auto_commit_intent(request=request, user=user, ...)` | 392 | 221 |
| `request.app.state.scoped_secret_resolver` | 443, 485, 642, 732, 800 | 272, 310, 442, 509, 559 |
| `request.app.state.catalog_service` | 448, 488, 645, 735, 803 | 277, 313, 445, 512, 562 |
| `_cancel_on_client_disconnect(request)` (`request.receive()`) | 584 | 395 |
| `request.state.composer_durable_completed` store / load | 618 / 945 | 419 / 695 |

Plus the dependency: `request.state.composer_request_lease` / `composer_durable_completed` stores (`H:2606-2607`)
and loads (`H:2708-2742`), and `request.app.state` reads through `_verify_session_ownership` and
`_get_composer_progress_registry` (`H:2594-2605`).

Transitively, through `settle_pipeline_proposal_under_compose_lock(*, request: Request, user: UserIdentity, ...)`
(`PS:131-141`), the route also reads these `request.app.state` members:

- `session_service` (`PS:152`);
- `interpretation_surfacing` (`PS:168`, `PS:374`);
- `settings` (`PS:190`, `:195`, `:219`, `:226`, `:228`, `:330`);
- `plugin_snapshot_factory(user)` (`PS:201`);
- `catalog_service` (`PS:203`, `:336`);
- `operator_profile_registry` (`PS:205`, `:335`);
- `session_engine` (`PS:220`);
- `scoped_secret_resolver` (`PS:223`, `:331`).

`settle_auto_commit_intent(*, request, user, service, session_id, intent, composer_meta, telemetry_source, session_operation_context)`
(`PS:405-415`) has two callers, `M:391` and `C:220`. `settle_pipeline_proposal_under_compose_lock` has two
callers: `PS:429` and the manual approval route `routes/composer/proposals.py:314`.

### 1.9 Raise inventory (AST `Raise` walker)

**Instrument controls.** All re-run at `1effedab2` in this session.

- **Known-positive:** `send_message` lists `M:199` (`HTTPException(404, 'State not found')`); `recompose` lists
  `C:150` (400).
- **Known-negative:** `_publish_progress` → `raises=0`; `does_not_exist_fn` → `!! NOT FOUND`, so the walker
  cannot return a silent empty result.
- **Mutation:** in scratch copies, `raise RuntimeError("MUTATION_CONTROL")` inserted before `M:158` and `C:111`
  moved the counts 27 → 28 and 23 → 24, and the inserted line was listed each time.
- **Cross-instrument:** `awk` counting `^\s*raise( |$)` over `M:134-1041` gives 27 and over `C:94-750` gives 23.
  Both equal the AST counts.

**Limits.** The walker sees `ast.Raise` nodes only and was run one level into callees. Implicit raise sites are
listed by hand: `.result()` at `M:1039`; `__aexit__` cleanup (§1.5); `run_sync_in_worker` admission timeouts;
DB-driver errors; pydantic `ValidationError` at `M:552` / `C:367`; `CancelledError` at every `await`. For the
following closures the class → handler map in §1.6 stands in for per-site completeness:
`ComposerServiceImpl._compose_loop`, `PlanningApplication._plan_and_stage_empty_pipeline`,
`persist_compose_turn`, `prepare_pipeline_proposal_commit`, the private service transaction helpers,
`surface_pending_interpretation_reviews`, and auto-title's inner helpers (`_auto_title.py:278-356`).

Classification key (under the rulings):

- **PRE-202:** a stable admission check that stays in the POST.
- **POST-202:** a terminal envelope published by the worker.
- **REQUEUE:** the worker releases its claim and the job stays queued.
- **REMOVED:** the exit disappears at cutover.

#### `send_message`: 27 nodes

| Line | Raise | Held | Class under the rulings |
|---|---|---|---|
| 181 | `_ingress_receipt_conflict(existing_ingress)` → 409 `message_already_accepted` / `message_idempotency_conflict` (body `M:113-125` carries `client_request_id`, `user_message_id`) | CRL+AL+SOL | **REMOVED** (Ruling 1). Replaced pre-202 by the job-row PK replay (202, current status) or a bound-request-hash mismatch 409 |
| 199, 201 | `HTTPException(404, "State not found")` (missing / foreign) | all | **POST-202** terminal 404 (Ruling 5: "a foreign `state_id` stays a 404") |
| 254 | `_ingress_receipt_conflict(ingress_result)` (race inside the insert txn) | all | **REMOVED** as a 409. The panel: "an ingress conflict found by the worker is Tier-1", so this becomes a Tier-1 raise → POST-202 500 |
| 256 | `AuditIntegrityError("Message ingress returned an unrecognized outcome")` | all | POST-202 500 |
| 259 | `raise ingress_cancellation` (deferred caller cancel; user row + receipt **already committed**; outside the outer `try`, no progress sink yet) | all | POST-202: worker cancel path (`request_cancelled`, or `worker_lost` for an unmarked cancel) |
| 305 | `AuditIntegrityError("Tier 1 audit anomaly: send_message transcript snapshot ...")` | all | POST-202 500 |
| 493 | `HTTPException(500, response_body) from rpf_exc.original_exc` (path-2 preflight, in the settlement child) | all | POST-202 500 typed |
| 622 | `post_provider_error from watcher_cancel` | all | watcher is REMOVED; the carried `post_provider_error` stays POST-202 |
| 623 | bare `raise` (watcher cancellation) | all | **REMOVED** (disconnect); other cancels go to the worker cancel path |
| 650 | `HTTPException(422, convergence body)` | all | POST-202 422 |
| 652 | `await _handle_composer_provider_failure(...)` → 502/504 | all | POST-202 502/504 |
| 688 | `HTTPException(502, _litellm_error_detail("llm_unavailable", ...))` | all | POST-202 502 |
| 750 | `HTTPException(500, plugin-crash body)` | all | POST-202 500 typed |
| 808 | `HTTPException(500, preflight path-1 body)` | all | POST-202 500 typed |
| 846 | `HTTPException(status_code, planner body)` | all | POST-202 mapped |
| 848 | `await _handle_composer_chargeable_refusal(...)` → 503/403 | all | POST-202 503/403 |
| 867 | `HTTPException(403, composer_admission_refused)` | all | POST-202 403 |
| 892 | `HTTPException(502, composer_error)` | all | POST-202 502 |
| 920 | `raise post_provider_error` | all | POST-202 (per carried class) |
| 922 | `InvariantError("Provider returned without a freeform continuation receipt")` → `M:937` | all | POST-202 500 |
| 924 | `raise deferred_cancellation` (after the durable settle) | all | POST-202, but see §3.4: under R2 the terminal CAS commits with the settle, so "completed wins" |
| 937 | `HTTPException(500, server_invariant_violated)` | all | POST-202 500 |
| 948 | `HTTPException(499, "Client disconnected after the compose turn completed.")` | all | **REMOVED** |
| 952 | bare `raise` (durable completed, non-disconnect cancel) → dependency 503 while rows are durable | all | **REMOVED** in this form; the worker's terminal is the committed `completed` row |
| 1013 | `HTTPException(499, "Client disconnected while the compose turn was running.")` | all | **REMOVED** (a user Stop becomes `request_cancelled` via the cancel endpoint) |
| 1017 | bare `raise` → dependency: heartbeat marker → 503 (`H:2727`), else cancelled | all | POST-202: the worker lease-loss / shutdown terminal. **The body is a plan decision**: today 503 `composer_request_lease_lost`; the contract (D14) picks 503 `composer_operation_worker_lost` |

#### `recompose`: 23 nodes

| Line | Raise | Class |
|---|---|---|
| 150 | `HTTPException(400, "No messages to recompose from")` | POST-202 400 |
| 152 | `HTTPException(409, "Cannot recompose: the last message is not a user message. ...")` | POST-202 409 |
| 159 | `HTTPException(409, {"error_type":"recompose_user_message_mismatch","detail":"The latest user message changed. Refresh the session before retrying composition."})` | POST-202 409 (Ruling 5 cites it as the transcript-drift guard; the recommendation puts `expected_user_message_id` in the hash) |
| 318, 450, 452, 488, 527, 567, 593, 595, 619, 644 | same shapes as send 493, 650, 652, 688, 750, 808, 846, 848, 867, 892 | POST-202 at the same statuses |
| 423 / 424 | watcher re-raise / bare | watcher REMOVED |
| 669, 671, 673, 687 | as send 920, 922, 924, 937 | POST-202 |
| 698, 743 | 499 ×2 | **REMOVED** |
| 702, 747 | bare `raise` | as send 952 / 1017 |

#### Callee raise nodes that reach the handlers

| Callee | Anchor : raise | Class under the rulings |
|---|---|---|
| FastAPI body decode | `routing.py:441` `RequestValidationError` (json_invalid 422), `:449` 400 | PRE-202 |
| `get_current_user` | `auth/middleware.py:101`, `:118` (401), `:152` (503), `:163` (401) | PRE-202 |
| `_verify_session_ownership` | `H:2911`, `H:2915` (404) | PRE-202 (+ a worker recheck, spec §3; see Q7) |
| rate limiter | `rate_limit.py:128` (429, memory), `:157` (503), `:160` (429, PG) | PRE-202, charged only for a new id (contract D2) |
| `_track_compose_inflight` | `H:2613` `RuntimeError`, `H:2722` `RuntimeError`, `H:2726` renewal defect, `H:2727` 503, + 10 bare pass-throughs (`H:2670-2743`) | **REMOVED from both routes** (dependency unmounted); its policy moves into the worker → POST-202 |
| `_cancel_on_client_disconnect` | `H:2414` (bare), `H:2441` `CancelledError()` | **REMOVED** |
| `SessionOperationLease.acquire` | `LC:343` `TypeError`; `LC:379`, `:380`, `:407`, `:408`, `:417` cancel/validation | worker start composite; defect → POST-202 `operation_failed` |
| repository `acquire` | `RP:4689`, `:4693`, `:4699` `SessionOperationConflictError` | **REQUEUE** (spec §3; never a terminal 409) |
| repository `acquire` | `RP:4680`, `:4683`, `:4690`, `:4723` `SessionOperationFenceLost` | POST-202 404 (contract D7: an archive that wins → 404) |
| repository `acquire` | `RP:4662` `ValueError` | defect → POST-202 500 |
| `SessionOperationLease.__aenter__` / `create_task` / `_renew_forever` / `_close` / `__aexit__` | `LC:1070`, `LC:659`, `LC:687`/`:689`, `LC:756`, `LC:1085`/`:1098` (the group) | POST-202; ordering vs the terminal CAS is contract D12 |
| `lookup_message_ingress` → `_existing_message_ingress_result` | `SV:4531` `AuditIntegrityError` | **REMOVED with its only caller** (`M:173`; `grep -rn "lookup_message_ingress(" src/` → the protocol def, the impl def, `M:173`) |
| `add_message_with_transcript` | `SV:4616`, `:4618` `ValueError` (defect); `SV:4711` `AuditIntegrityError`; **returns** `MessageIngressAccepted` / `MessageIngressConflict` at `SV:4645-4646` and `SV:4702-4703` | the two non-fresh returns become a worker **Tier-1 raise** (Ruling 1); the others POST-202 500 |
| `_assert_state_in_session` (in the ingress txn, `SV:4648`, `SV:4655`) | `src/elspeth/web/sessions/proposal_authority.py:547`, `:549` `RuntimeError` | POST-202 bare 500; reachable only if the state vanished between `M:197` and the insert (NEW to this inventory) |
| `_insert_message_ingress_receipt` | `SV:1410` `TypeError`, `SV:1412` FenceLost | POST-202 |
| `_require_session_write_authority_on_connection` | `SV:7181` `TypeError`, `SV:7187` FenceLost | POST-202 (NEW to this inventory) |
| `_session_composer_mutation_transaction` / `_require_session_operation_context_on_connection` | `SV:1012` `TypeError`; `SV:985`, `SV:999` FenceLost(TOKEN_MISMATCH) → 404 | POST-202. Ruling 2 adds a refusal here for writes after the job leaves `running` |
| `get_session` / `get_state` | `SV:2120` `SessionNotFoundError`; `SV:5867` `ValueError` → route 404 | PRE-202 / POST-202 |
| `add_message` / `add_messages_atomic` / `save_composition_state` | `SV:4393`, `:4395`, `:4397`; `SV:6933`, `:6935`, `:6937`; `SV:4966`, `SV:4971` FenceLost | POST-202 |
| `list_composition_proposals` | `SV:3322`, `:3328` `AuditIntegrityError` | POST-202 500; **must run before the terminal CAS** (spec §1) |
| `parse_completion_gates` | `completion_gates.py` 14 nodes in `:264-334` | POST-202 500 (Tier-1) |
| `state_from_record` | `converters.py:33` `ValueError` | POST-202 500 |
| `ComposerServiceImpl.compose` | `CS:933` `ComposerServiceError` (instance-local availability) → 502; `CS:936` `TypeError`; `CS:938` `ValueError`; `CS:940` `AuditIntegrityError`; `CS:1053` `RuntimeError`; `CS:1030`, `:1097`, `:1113` bare | POST-202 |
| `ComposerChargeableAdmission.require` (`CS:942`) | `composer/chargeable_admission.py:25`, `:37` `ComposerAdmissionRefused`; `:27` `TypeError`; `:29` `ValueError`; `:36` `AuditIntegrityError` | POST-202 403 / 500 |
| `_handle_composer_provider_failure` | `H:733` `TypeError` (unclassified SDK exc) | POST-202 500 |
| `_persist_llm_calls` / `_persist_turn_audit_cohort` | `H:2095` / `H:2206` `AuditIntegrityError` | POST-202 500 |
| `_state_data_from_composer_state` | `H:2836` `ComposerRuntimePreflightError.capture` | POST-202 500 typed |
| `composer_turn_end_assistant_row` | `H:1661`, `H:1674` `AuditIntegrityError` | POST-202 500 |
| `_freeform_child_result` / `_join_freeform_owned_task` | `H:1379`, `H:1449` `AuditIntegrityError("Freeform continuation cancelled before settlement")`; `H:1380` re-raise; `H:1438` `RuntimeError` | POST-202 500. Today this is the route-level outcome of **SOL loss mid-settlement**, which the SOL close then likely groups (§1.5) |
| `_join_shielded_task_after_cancellation` | `H:1403`, `:1408`, `:1419` bare; `H:1415` Tier-1 child error; `H:1417` `AuditIntegrityError` | worker cancel path |
| `settle_pipeline_proposal_under_compose_lock` | 409s `PS:155`, `:182`; `RuntimeError` `PS:158`, `:256`, `:326`; `CancelledError` `PS:176`, `:279`, `:282`, `:306`, `:363`, `:385`; **504 `PS:284`** (`PipelineCommitError` `TIMEOUT`, new vs old); 409/422 `PS:289`; bare `PS:280`, `:307`, `:364` | POST-202 at those statuses |
| `maybe_auto_title_session` (via `.result()` `M:1039`) | `_auto_title.py:387`, `:411` `AuditIntegrityError`; `:427` interrupted; `:433` `_AutoTitleProviderTransportError`; `:446`, `:474` bare | POST-202; replaces a completed response today |
| progress, in-memory | `composer/progress.py:109`, `:114`, `:127` `RuntimeError` | POST-202 500 (a defect) |
| progress, PG registry / authority | `composer_progress_authority.py:514`, `:531` bare; `:540` `ValueError`; `begin_request` `:153`/`:155`; `bind_request` `:306`/`:308`/`:320`; `publish` `:341`/`:343`/`:355`; `heartbeat_request` `:270`/`:272`/`:287` | Today these escape as a bare 500. `ComposerProgressSessionUnavailable` and `ComposerProgressIdentityInactive` are `PermissionError` subclasses (`:113`, `:122`), and `APP:2165` re-raises a non-retryable `OSError`. The contract (D15) makes progress advisory in the worker |

---

## 2. Delta against the old findings (`d479eb2b4`) and the contract

### 2.1 What moved

| Old anchor | Now | Note |
|---|---|---|
| `send_message` def `M:116`, body ends `M:1178` | `M:134-1041` | −137 lines net |
| `recompose` def `C:90`, `C:86-829` | `C:94-750` | |
| mounts `M:124`, `C:97` | `M:142`, `C:102` | |
| `_track_compose_inflight` `H:2442-2645` | `H:2556-2762` | |
| `_cancel_on_client_disconnect` `H:2210` | `H:2324` | |
| `_composer_progress_sink` `H:382-403` | `H:288-309` | |
| heartbeat constants `H:2338-2439` | `H:2452-2553` | |
| `_verify_session_ownership` `H:2788-2807` | `H:2898-2917` | |
| `SessionOperationLease.acquire` `LC:326` | `LC:326` | unchanged |
| repository `acquire` `RP:4756-4843` | `RP:4646-4733` | |
| `compose()` `CS:4112`, deadline `CS:4163` | `CS:898`, deadline `CS:944` | `composer/service.py` was split (`166a83620`, `ed84adf51`) |
| app handlers `APP:1363`, `:1501`, `:2085`, `:2110`, `:2132`, `:2162`, `:2203`, `:2247` | `APP:1363`, `:1501`, `:2088`, `:2113`, `:2135`, `:2165`, `:2206`, `:2245` | |
| IDOR module tuple `test_routes.py:3863-3866` (contract §HTTP) | `tests/unit/web/sessions/test_routes.py:4102-4103`; inventory entries `:3985-3986` | contract anchor is stale |
| contract D6 `proposals.py:318` | `routes/composer/proposals.py:314` | |
| raise counts: send 19, recompose 16 (`findings/raise-inventory-send.md:294`, `raise-inventory-recompose.md:21`) | send **27**, recompose **23** | |

### 2.2 What vanished

- **The guided routes.** `routes/composer/` now holds `compose.py`, `__init__.py`, `pipeline_settlement.py`,
  `proposals.py` and `state.py`, and nothing else; `grep -rn guided src/elspeth/web/sessions/routes/` → 0 lines.
  - Instrument: `grep -rn '"/{session_id}/guided' src/elspeth/web` → 0 hits. Positive control: the
    `"/{session_id}/recompose"` pattern hits `C:91`.
  - Every spec and contract clause about keeping "the three guided routes" synchronous with their
    `_track_compose_inflight` mount now binds nothing: spec (working tree) `:6-12`, `:41-52`, `:181-187`,
    `:210-217`, `:301-302`; contract D11 `:81`; contract T07 "guided byte-unchanged".
- **The guided artefacts.** `GuidedCustodyIntegrityError`, `guided_operation_request_hash`,
  `_GuidedOperationRequest`, `_guided_terminal_for_compose` and `transition_consumed` together have **1 hit in
  `src/`**, a comment at `src/elspeth/web/sessions/models.py:609`. Positive control: `_SessionOperationRequest`
  hits `SCH:72`.
  - Consequences: old send arm `M:1035-1066` (the custody `failed_turn` 500), the transition flip `M:768-787`,
    and branch 29c `M:943-975` (old-tree anchors) are gone.
  - Old Q6 (the guided-terminal branch on `/messages`) is moot.
- **Inline LiteLLM arms.** Old send `M:407-487` and recompose `C:242-315` are replaced by one `except OpenAIError`
  arm delegating to `_handle_composer_provider_failure`.

### 2.3 What is new

1. **Send idempotency (`8630db9b8`, epoch 69).**
   - `client_request_id: UUID` is required (`SCH:154`);
   - a pre-preflight receipt lookup runs under SOL (`M:173-181`);
   - an atomic user row + receipt insert (`SV:4576-4718`);
   - two 409 arms: `_ingress_receipt_conflict` (`M:111-125`) raised at `M:181` and `M:254`.

   The receipt binds `(content, requested_state_id)` (`SV:4532-4536`). A replay with the same id but different
   content or state is `message_idempotency_conflict`, even when the new state is foreign or unknown
   (`tests/unit/web/sessions/test_freeform_route_custody.py:553-583`). The ACA probe P1 and the acceptance tooling
   count receipt rows:
   - `src/elspeth/web/_acceptance_common/replica_probes.py:525`, `:630-647`;
   - `src/elspeth/web/_azure_container_apps_acceptance/controller.py:58`;
   - `src/elspeth/web/azure_container_apps_observations.py:314`;
   - `src/elspeth/web/azure_container_apps_acceptance.py:315-325`.
2. **Recompose has a body.** `RecomposeRequest.expected_user_message_id: UUID` (`SCH:162-165`) and a 409
   `recompose_user_message_mismatch` (`C:158-165`). Old D1 ("no body") and the contract's "`RecomposeRequest` NEW …
   only `operation_id`" (`contract.md:161`) are both stale.
3. **SOL-owned continuation custody (`04c11a713`).** Post-provider settlement moved into an SOL-owned child joined
   through repeated caller cancellation (`M:607-619`, `C:408-420`). Also new:
   `request.state.composer_durable_completed` (`H:2607`, `M:618`, `C:419`) and the "after completed" 499 arms
   (`M:945-952`, `C:695-702`).
4. **Provider gateway classification (`166a83620`).** `_handle_composer_provider_failure` (`H:717-799`) adds a
   **504** timeout status and a `guidance` field. The planner settlement adds **504** (`PS:283-287`).
5. **Chargeable admission split (`ed84adf51`).** A `ChargeableAdmissionRefused` arm on both routes, with **503**
   `token_accounting_unavailable` (`H:822-843`).
6. **`state_id` is always checked when sent** (`M:189-202`). Before, it was checked only when a head existed. Old
   R7/Q5 is resolved in the code.
7. **The tutorial now rides `/messages`** (`01d96af9a`). `grep -ci tutorial` gives 0 in both `M` and `C`, and 41 in
   `src/elspeth/web/composer/tutorial_run_routes.py` (the positive control). No tutorial branch exists in either
   handler. The spec's `:6-12` "the tutorial still rides its machinery" is stale: the async cutover covers the
   tutorial by construction (composer invariant 2).

### 2.4 Old divergences (findings D1-D10) and the contract's D5/D6/D8, measured now

| Id | Status | Evidence |
|---|---|---|
| D1 (recompose had no body) | **changed** | `RecomposeRequest(_RequestModel)` exists, non-strict, one field (`SCH:162-165`) |
| D2 (policy read placement) | **still divergent** | recompose reads before AL (`C:115-116`); send reads under SOL (`M:224-225`) |
| D3 (kind-specific preamble) | **still divergent, wider** | send: receipt lookup → `state_id` 404s → atomic insert **before** the outer `try` (`M:173-259`); recompose: transcript → 400/409/409 (`C:147-165`) |
| D4 (auto-title) | **unchanged, send-only** | `M:335-355`, 2 s wait `M:1036-1041` |
| D5 (base-state naming) | **still divergent, now load-bearing** | send carries `pre_send_state_id` (client-asserted, `M:202-204`) and `compose_base_state_id` (head, `M:218`); recompose has one variable (`C:132`/`C:135`). Under Ruling 5 this split *is* the bound-base vs current-head pair |
| D6 (custody-refusal arm) | **converged by removal** | 0 hits for `GuidedCustodyIntegrityError` in `src/`; both routes fall through to `APP:1363` |
| D7 (planner progress reason) | **still divergent** | recompose hard-codes `reason="provider_unavailable"` and one evidence string (`C:576-585`); send has the `COST_UNAVAILABLE` branch and `freeform_planner_progress_reason(exc.code)` (`M:819-837`) |
| D8 (cancel-path audit join) | **converged** | recompose uses `_join_shielded_task_after_cancellation` (`C:707`, `C:727`), as send does (`M:965`, `M:986`) |
| D9 (`_compose_result` tracking) | **dead** | assigned at `M:288` and `M:606`, never read (`grep -n _compose_result M C` → those 2 lines only); its only reader was the removed D6 arm |
| D10 (counter placement) | **still divergent** | recompose's 400/409 raises (`C:150-165`) precede its gauge +1 (`C:188`); send's ingress 409s and state 404s (`M:181-201`) precede its gauge (`M:279`). Both are counted only by the dependency |

Contract rows that cited these:

- **Contract D5** ("send's `GuidedCustodyIntegrityError` arm and send's shielded join for both",
  `contract.md:75`): the first half names a class with 0 hits in `src/`; the second is already true (D8 above). D5
  is **moot as written**.
- **Contract D6** (settlement takes `services` + `user_id` instead of `request` + `user`, `contract.md:76`): still
  needed; the request reads it must replace are §1.8's `PS` list, and the fourth caller is now
  `routes/composer/proposals.py:314`.
- **Contract D8** (admission 409 `composer_operation_active`, `contract.md:78`): nothing on the route today
  corresponds to it. The nearest existing refusal is the SOL conflict 409 `{"detail":"Session operation is already
  active"}` (`session_operation_handlers.py:28-30`), which the worker must turn into REQUEUE, not a terminal.

### 2.5 Corrections this run made to the earlier draft

- The fence-chokepoint claim was too strong: three entry points into `SV:970`, and blob writes use the repository
  predicate instead (§1.5).
- `M:619` / `C:420` assign `receipt.terminal_status` (a `Literal["completed"]` default, `H:1352`), not a string
  literal.
- The ingress transaction anchors were off by a few lines (user row `SV:4662-4679`, receipt `SV:4680-4688`).
- Added: the FastAPI body-decode step before dependencies (§1.2); the `ExceptionGroup` outcome of a body failure
  plus a close failure (§1.5); `_assert_state_in_session` and `_require_session_write_authority_on_connection`
  raise rows; D1/D3/D4/D9 rows; the auto-title writer list.

### 2.6 Old design risks, rechecked

- **R1** (422 decided after the CRL): unchanged (§1.2), except malformed JSON, which is decided first.
- **R2 / R-9** (rate limit charges every call): unchanged (`M:156`, `C:111`).
- **R3 / R-7** (conflict 409 is fail-fast): unchanged (`RP:4689`, `:4693`, `:4699`).
- **R4 / R-2** (a success can be replaced after commit): **still true and widened** — three paths (§1.5).
- **R5** (SOL loss does not cancel the provider call): still true for the provider call. **Changed** for
  settlement: SOL loss now cancels the settlement child; the route raises `AuditIntegrityError`, which the SOL
  close likely wraps into an unhandled `ExceptionGroup` (§1.5), where the old finding had a 404 at the next write.
- **R6** (no ownership recheck under SOL): unchanged.
- **R8 / R-1** (no single final transaction): unchanged. State save, assistant row, cohort and the proposals read
  are still separate (`M:503`, `:515`, `:531`, `:551`).
- **R9 / R-10** (progress `PermissionError` → bare 500): unchanged. There is still no mapping on either route.
- **R10 / R-3** (budget inside `compose()`): unchanged (`CS:944`). The planner path gets no deadline argument
  (`CS:994-1007`).
- **R11 / R-5** (request-bound settlement): unchanged (§1.8).
- **R12, R13** (watcher and heartbeat in the request): unchanged in kind.
- **R16** (user row durable before failure points): **worse in one respect**. A caller cancel during the ingress
  join re-raises at `M:258-259`, after the user row and receipt commit and before the outer `try` and the progress
  sink, so there is no cancelled progress and no sidecar bookkeeping.

---

## 3. Implications for the plan under the rulings

### 3.1 Identity (Ruling 1)

- **What is deleted.** `_ingress_receipt_conflict` (`M:111-125`), both raise sites (`M:181`, `M:254`), and the
  pre-preflight `lookup_message_ingress` call (`M:173-179`). That call is the method's only caller, so
  `SV:4539-4574` and its protocol entry (`src/elspeth/web/sessions/protocol.py:2944`) lose all callers.
- **The ingress write moves.** `add_message_with_transcript` becomes the worker's user-row write, in **one
  transaction** with `job.user_message_id` (T06). Its non-fresh returns (`SV:4645-4646`, `SV:4702-4703`) must
  become Tier-1 in the worker, because the job PK already arbitrated the id. Its in-transaction receipt re-check
  (`SV:4638-4646`) then has only a Tier-1 outcome.
- **The wire name changes.** `client_request_id` appears on the wire in `SCH:154`, in the 409 body `M:117`, and in
  the transcript projection `H:593` / `SCH:216`. Ruling 1's single wire name forces all three to change. The
  ingress key column is renamed or bound to `operation_id` in the epoch cut.
- **The receipt stays bound to `requested_state_id`.** The receipt binds it (`SV:4532-4536`, `SV:4685`). With
  Ruling 5 putting `state_id` in the request hash, the job hash and the receipt column must carry the same value.
- **SPA and tests to delete or retarget** (the frontend seam owns the detail; anchors resolved this run):
  - `src/elspeth/web/frontend/src/stores/sessionStore.ts:676-803` (transcript matching), `:1622-1631`
    (`message_already_accepted`), `:1684-1685` (`message_idempotency_conflict`) and `:2310-2311`
    (`recompose_user_message_mismatch` → a local failure code);
  - `MessageBubble.tsx:283` (the local-failure-code list);
  - tests `test_freeform_route_custody.py:199-200`, `:458-466`, `:573-583`.

### 3.2 Moved base (Ruling 5): where the check slots in

- **Insertion point.** On send, the refusal belongs after the head read (`M:184`), after `state_id` resolution
  (`M:192-204`) and **before** the ingress child (`M:236`). `compose_base_state_id` (head, `M:218`) and
  `pre_send_state_id` (client) are both computed there already, so the check compares values that exist. The body
  is today's `StaleComposeStateError` projection (`APP:1501-1516`), a flat 409 with `request_id`. In a worker it
  must be published as that envelope, since `APP:1501` will not run.
- **Absent versus null.** The DTO cannot tell an omitted `state_id` from an explicit null: `state_id: UUID | None =
  None` sits on the coercing `_RequestModel` (`SCH:143-155`), and the route treats both as "use the head as
  provenance" (`M:203-204`). Ruling 5 defines absent as "no state", which reverses that fallback.
  - The SPA sends the key: `stateId` resolves to `compositionState?.id ?? null` (`sessionStore.ts:1499-1501`), and
    `client.ts:959-964` includes it whenever it is not `undefined`.
  - **The ACA probe body is exactly `{"content","client_request_id"}`**
    (`azure_container_apps_observations.py:314`), and the acceptance parser rejects any other key set
    (`azure_container_apps_acceptance.py:315`). Under Ruling 5 the probe would be refused 409 on any session that
    has a head. This is a concrete reason to fold the probe into the T13/T15 re-scope.
- **Recompose gets a new field.** `RecomposeRequest` has no `state_id` today (`SCH:162-165`). Ruling 5 adds one.
  The SPA's `recompose()` sends only `expected_user_message_id` (`client.ts:985`). The recompose compare slots in
  after `C:129-135` and before the transcript read (`C:147`).
- **Foreign `state_id`.** It stays 404 at `M:196-201`.
- **Race window.** Today the head read (`M:184`) and the ingress insert (`M:236`) run under the same SOL, so the
  compare-then-insert is already serialized against every other COMPOSE-SOL writer; the in-transaction
  `_assert_state_in_session` (`SV:4648`) checks only that the state exists in the session, not that it is the head.

### 3.3 Positive fence predicate (Ruling 2)

- **Where it lands.** `_require_session_operation_context_on_connection` (`SV:970-999`) is the one chokepoint in
  `SV` (three entry points, §1.5). It does **not** cover the repository-fenced blob writes the provider turn makes
  through `RP:4889-4911`; `persistence.md` records that a Family-S-only predicate misses them and needs its own arm.
- **Writers that run after the durable settle under the same SOL (T06's post-terminal inventory, route side):**
  - the auto-title chain (§1.5 last bullet): `update_session_title` (`SV:2124`), `settle_provider_attempt`
    (`SV:5581`), `cancel_undispatched_provider_attempt` (`SV:5528`). It can run up to 2 s past settlement
    (`M:1037`), and past that after `.cancel()`, because the cancel arm still settles the charge
    (`_auto_title.py:435-446`) while the SOL close gathers it (`LC:708`);
  - the cancel-path audit-only LLM sidecar persists (`M:963-978`, `C:705-720`), which must pass `audit_only=True`;
  - the SOL release itself (`LC:723-732`), a fence-row write that is not a session write.

  Under the positive predicate, the auto-title's `update_session_title` is refused once the job is terminal.
  T06 must choose: join auto-title **before** the terminal CAS, classify its writes as `audit_only` (only the charge
  settlement plausibly qualifies), or move auto-title out of the compose operation.
- `lookup_message_ingress` is a fenced **read** (`SV:4557-4565`) and disappears with Ruling 1.

### 3.4 Terminal ordering: what the new composites replace

- **The completion-versus-cancel race today** is decided by `_join_freeform_owned_task` deferring the cancel
  (`H:1443-1446`) and re-raising it after `composer_durable_completed` (`M:618` → `M:923-924`). Under R2 and
  F-B2/C2 the terminal CAS (`cancel_requested_at IS NULL` in its `WHERE`) replaces this. A committed cancel must
  fence the non-audit writes *inside* the settlement child (`M:503`, `M:515`, `PS:345-360`), not merely be
  deferred past them.
- **Why the worker needs its own watcher.** SOL loss mid-settlement already cancels the child; the route turns
  that into `AuditIntegrityError` (`H:1378-1379`) and the close likely groups it (§1.5). A worker that owns a
  `wait_until_lost()` watcher (`LC:642-648`) should classify that outcome as `worker_lost` / lease-lost; today
  `_capture_freeform_child` erases the difference between a lease-loss cancel and any other child cancel.
- **Sites that need an explicit ruling.** Three places today turn a durable completion into a non-200 (§1.5):
  `LC:1083-1085`, `H:2720-2727`, and `M:1039`. Contract D12 decides the first (CAS before SOL close; close failure
  logged only). The second disappears with the dependency. The third needs the auto-title decision (Q1). The
  body-plus-close `ExceptionGroup` (`LC:1097-1101`) needs the same D12 treatment on the failure side: once the
  terminal is committed, a close failure must never regroup it.
- **The response read must stay inside the composite.** The response is built from `_pending_proposal_responses`
  (`M:551`) after the cohort commit. T06's connection-taking proposal read must carry
  `list_composition_proposals`' two Tier-1 raises (`SV:3322`, `SV:3328`).

### 3.5 Where send and recompose converge

The typed ladder is identical arm-for-arm (§1.6). The remaining per-kind differences are:

- the preamble (D3): send runs receipt → `state_id` → ingress; recompose runs transcript →
  `expected_user_message_id`;
- D2 policy-read placement;
- D5 naming;
- D7 progress copy;
- labels: `route="messages"` / `"recompose"` (`M:654`, `C:454`), `telemetry_source` (`M:399`, `C:228`), endpoint
  metric labels (`M:279`, `M:1019-1020`, `C:188`, `C:749-750`), slog `compose_llm_bad_request` /
  `recompose_llm_bad_request` (`M:664`, `C:464`), failure-handler `route` strings (`"compose"`/`"convergence"` vs
  `"recompose"`/`"recompose_convergence"`, e.g. `M:639`, `C:439`);
- send-only auto-title (D4) and the dead `_compose_result` (D9).

Contract D5 is **already satisfied or moot**. T10 should cite D7 and D2 as the visible recompose changes instead.

### 3.6 Request decoupling (T07 and T10)

- **What the worker must replace.** Everything in §1.8, plus `request.state.composer_request_lease` (`H:305`),
  `request.state.composer_durable_completed`, and the lazily created lock registry (`H:268-270`). A worker cannot
  create the registry from a `Request`; contract T07's app-keyed accessor still applies.
- **The extraction collapses to a move.** With the guided mounts gone, the "extract so guided keeps identical
  behaviour" clause has no second consumer. Once both freeform routes stop mounting `_track_compose_inflight`, the
  dependency has **zero** mounts, and the recommendation's T07 note ("delete it after cutover") follows. The
  renewal policy in `H:2615-2700` still has to move into the worker's CRL lifecycle if contract D1 (keep a CRL)
  stands; the heartbeat tests in §4 are its specification.
- **Callers of a changed `_composer_progress_sink` signature:** `M:262`, `C:172` and the testcontainer probe
  (`test_cross_process_composer_postgres.py:74`).
- **Callers of the contract-D6 settlement signature change:** `M:391`, `C:220`, `PS:429` and `proposals.py:314`.

### 3.7 Error projection (T09): rows to add against the old parity list

- 504 from `_handle_composer_provider_failure` (timeout, `H:799`), with its `guidance` key (`H:797-798`).
- 503 `token_accounting_unavailable` (`H:835-843`).
- 409 `recompose_user_message_mismatch` (`C:159-165`), a dict detail, so `request_id` is injected (`APP:2245-2264`).
- 504 settlement timeout, a string detail (`PS:284-287`).
- 500 `AuditIntegrityError("Freeform continuation cancelled before settlement")` (`H:1379`, `H:1449`), which must
  not survive as the published lease-loss terminal (§3.4).
- 409 `stale_compose_state` as the Ruling 5 moved-base refusal (flat body, `APP:1501-1516`).
- Remove the ingress 409 rows and the 499 rows.

---

## 4. Tests that pin these routes (anchors at `1effedab2`)

Instrument: `grep -rln "_track_compose_inflight|_cancel_on_client_disconnect|_COMPOSER_HEARTBEAT|composer_durable_completed|_join_freeform_owned_task|_capture_freeform_child" tests/`
(the draft's search; every row's anchors were re-resolved this run by the anchor checker and each named function
exists at the stated line).

| Area | Test | What it pins / fate at cutover |
|---|---|---|
| heartbeat policy (dependency) | `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py`: harness `_run_fake_route` `:137-183`; tests `:188`, `:202`, `:218` (lease arithmetic), `:232`, `:263`, `:288`, `:305`, `:317`, `:323` (`test_dependency_teardown_503_reaches_the_http_client`: 503 `composer_request_lease_lost`, finished status `failed`), `:356` (**route-level**: POST `/messages` with `client_request_id` `:388-391`), `:415`, `:468`, `:499` | policy tests adapt to the worker lifecycle; `:323` and `:356` become worker terminal-envelope tests |
| recompose heartbeat | `tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py:96` (503 body `:112-117`), `:125` control (plain cancel → client cancel) | adapt; the plain-cancel control becomes a cancel-endpoint `request_cancelled` test |
| telemetry / mount | `tests/unit/web/sessions/routes/test_composer_request_telemetry.py:92-108` (surface `freeform`, parametrised on `/messages` only), **`:111-122` `test_message_route_mounts_request_lifecycle_dependency_exactly_once`** (asserts `dependencies.count(_helpers._track_compose_inflight) == 1` on `POST /messages`), `:125-151`, `:154`, `:193`, `:209`, `:229`, `:244` | `:111-122` must flip to "count == 0" or be deleted at cutover; the rest follow the lifecycle |
| custody (NEW) | `tests/unit/web/sessions/test_freeform_route_custody.py`: `:65-66` (6 sites; exact ingress retry → 409 `message_already_accepted` `:199-200`), `:220-221` (both routes: caller cancel after provider return still settles), `:258-259` (post-provider invariant beats caller cancel, 500 `:291`), `:301-302` (recompose post-commit joins), `:433` (`recompose_user_message_mismatch` `:439-440`), `:446` (receipt reuses original null state after head advances: same-key replays `:457-466` give both 409 arms; then a **new** key with no `state_id` after the head advanced answers 200 `:475-477`), `:483`, `:493` (join semantics), `:515-540` (projection fault cannot publish false complete), `:553` (reused key conflicts before state validation; `:573-583`), `:592` (completed stays completed when caller cancels during auto-title join) | ingress-409 assertions are **deleted** (Ruling 1); `:446`'s same-key replays re-home to the job-PK replay (Ruling 1), while its new-key tail `:475-477` is **reversed** by Ruling 5 (an absent `state_id` means "no state", so a session with a head must answer 409 `stale_compose_state`); the join and settlement tests re-home to the worker composite |
| fence-wiring structural | `tests/unit/web/sessions/test_operation_fence_wiring.py:84-110` (`send_message` AST: `compose_lock` + `SessionOperationLease.acquire` precede `service.get_current_state` precede `service.add_message_with_transcript(..., session_operation_context=compose_operation_lease.context)`), `:323-341` (auto-title call is a child of `compose_operation_lease.create_task` and passes the lease context) | both parse the route source; they retarget to the worker turn function (contract T10 "retargeted structural pins") |
| IDOR inventory | `tests/unit/web/sessions/test_routes.py:3937` `TestIDORCoverageDrift`; inventory `:3979-4032` incl. `"send_message"` `:3985`, `"recompose"` `:3986`; walker `_collect_ownership_call_site_identities` `:3881`; module tuple `:4102-4103` `(sessions, state, proposals, compose, messages, runs, interpretation)` | the new operations router joins the tuple and its handlers join the inventory; `send_message`/`recompose` stay only if the POSTs still call `_verify_session_ownership` |
| disconnect | `test_routes.py:4913` `test_client_disconnect_cancels_compose_turn`, `:5026` `test_external_cancel_racing_disconnect_keeps_unwinding` | removed with the watcher |
| inflight / concurrency | `test_routes.py:5095` `test_composer_progress_reports_inflight_request_count`, `:5899` `test_send_message_serializes_concurrent_requests_per_session`, `:11256` `test_inflight_gauge_increments_then_decrements_across_request` | re-home onto the worker |
| cancellation lifecycle | `test_routes.py:11011` `TestComposerCancellationLifecycle`: `:11019`, `:11056`, `:11158`, `:11186`, `:11221`; terminal-counter assertions `:11122-11126`, `:11154-11155` | cancel source changes from socket to cancel endpoint |
| progress routes | `test_routes.py:10656` `TestComposerProgressRoutes` (`:10891` recompose terminal progress keyed by last user id), `:10909` `TestComposerInFlightEndpoint` | contract D15 keeps progress `request_id` = user-message id |
| state_id / transcript | `test_routes.py:4422` `TestSendMessageStateIdValidation`; `:14597` `TestSendMessageTranscriptSnapshot` (fake 409 handler `:14616`) | Ruling 5 adds a stale-base class next to these |
| provider/accounting ladder | `test_routes.py:6038-6045` (both routes × gateway error classes), `:6096` (accounting refusal after failed attempt), `:6483-7241` recompose ladder tests, `:15123` `test_recompose_repeat_cancel_joins_cleanup_before_releasing_lease` | parity rows for T09 |
| attribute contracts | `tests/unit/web/test_sessions_composer_attribute_contracts.py:18`, `:165-169` (`_failure_log_request_id`, `_is_client_disconnect_cancel`) | follow the helpers |
| module split | `tests/unit/web/sessions/test_routes_split.py:8-19` (imports `routes.messages`, `routes.composer`) | unaffected unless modules move |
| PostgreSQL | `tests/testcontainer/web/test_cross_process_composer_postgres.py:67-80` (mounts the real dependency on a probe route; calls `_composer_progress_sink` positionally `:74`); `tests/testcontainer/web/test_add_message_transcript_postgres.py:59`, `:128` (two instances accept one receipt for the same key; session cascade) | the probe follows the dependency's fate; `:128` is the PG proof of today's ingress arbitration and must be restated for the job-PK + ingress FK design |
| acceptance tooling | `tests/unit/web/acceptance_common/test_replica_probes.py:66`, `:167-170`, `:422` | T13/T15 probe re-scope (see §3.2 for the `state_id` reason) |
| **gap** | no test names the body-plus-close `ExceptionGroup` (`grep -rln "body and cleanup both failed" tests/` → 0; positive control `LC:1099`), and none was found asserting its 500 on either route | the worker composite should pin its replacement (§3.4) |

---

## 5. Open questions

1. **Auto-title after the terminal.** Under Ruling 2, does `maybe_auto_title_session` join **before** the
   terminal CAS, run its charge settlement as `audit_only`, or leave the compose operation? Today it can write the
   title up to 2 s after settlement, keeps settling its charge after the route cancels it, and its `.result()`
   can replace a completed response (`M:1036-1041`).
2. **The SOL-loss-mid-settlement terminal.** Today the route raises `AuditIntegrityError("Freeform continuation
   cancelled before settlement")` (`H:1378-1379`) and the SOL close most likely wraps it with the renewal error in
   an unhandled `ExceptionGroup` (`LC:1097-1101`; by reading, untested). Should the worker publish `worker_lost`
   (503) for it, and how does it tell a lease-loss cancel of the child from any other child cancel, given that
   `_capture_freeform_child` erases the difference?
3. **The ingress-race return.** Is a worker-side `MessageIngressAccepted` / `MessageIngressConflict`
   (`SV:4645-4646`, `SV:4702-4703`) Tier-1 in both cases? The job PK should make both unreachable; an `Accepted`
   could only mean a second worker start for the same operation, which the transition guard forbids.
4. **The settlement's own budget.** `pipeline_settlement.py:228` passes
   `timeout_seconds=request.app.state.settings.composer_timeout_seconds` into the post-compose tail. Under F-C5,
   should it draw from the remaining operation budget or stay a fixed setting? Neither the contract nor the
   recommendation names this consumer.
5. **`state_id` absent versus explicit null.** Ruling 5 needs "absent means no state". Does the strict DTO make the
   key required-nullable (an omitted key is 422), or keep the default `None`? The ACA probe (§3.2) and any non-SPA
   caller depend on the answer.
6. **The recompose `state_id` source.** When the recompose DTO gains `state_id`, is it the head the client saw at
   the *failed* send, or at the retry click? The frontend seam must confirm what the retry path supplies.
7. **The ownership recheck under SOL** (spec §3). It is still absent (`H:2898-2917` needs a `Request`). Does the
   worker's start composite recheck `sessions.user_id` and `auth_provider_type`, or rely only on `RP:4687-4690`
   (archived → OWNER_INACTIVE), which does not check the owner?
8. **Blob writes under a terminal job.** Ruling 2 names the predicate at `SV:970`, but the provider turn's blob
   writes are fenced in `RP:4889-4911`. Does Ruling 2 extend to that family, and does T06's negative control
   include a blob write after the terminal CAS?
