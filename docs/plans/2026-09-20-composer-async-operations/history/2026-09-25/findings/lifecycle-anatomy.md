# Request lifecycle anatomy — composer async operations (freeform-only cutover)

Measured on `release/0.8.1` @ `d479eb2b4`, main checkout, `src/` and `tests/` clean
(`git status --short src tests` empty). Every anchor below is `file:line` in that tree.
Read-only explorer; no source was modified.

Scope reminder (spec `docs/specs/2026-09-16-composer-async-operations-design.md`, 2026-09-25
ruling): only `POST /{session_id}/messages` and `POST /{session_id}/recompose` move to 202 +
durable poll. `/guided/plan`, `/guided/respond`, `/guided/chat` keep `_track_compose_inflight`
and must behave identically.

---

## 1. Who mounts the request lifecycle today (all five mount sites)

Instrument: `grep -rn "_track_compose_inflight" src/` — known-positive: it must hit the
definition `_helpers.py:2442` (it does); it also hits docstrings (`progress.py:135`,
`compose.py:806`, `messages.py:1121`, `guided.py:5690`, `guided_plan.py:765`,
`guided_chat_atomic.py:2419`) which are comments, not mounts. `Depends(_track_compose_inflight)`
mount sites (exactly five):

| Route | Mount | Handler def |
|---|---|---|
| `POST /{session_id}/messages` (freeform, **cutover**) | `src/elspeth/web/sessions/routes/messages.py:124` | `send_message` `messages.py:116`, registered inside `register_message_routes` `messages.py:110` |
| `POST /{session_id}/recompose` (freeform, **cutover**) | `src/elspeth/web/sessions/routes/composer/compose.py:97` | `recompose` `compose.py:90` |
| `POST /{session_id}/guided/plan` (stays sync) | `src/elspeth/web/sessions/routes/composer/guided_plan.py:338` (`Annotated[None, Depends(...)]`, declared BEFORE `user`) | `post_guided_plan` `guided_plan.py:334` |
| `POST /{session_id}/guided/respond` (stays sync) | `src/elspeth/web/sessions/routes/composer/guided.py:2941` | `post_guided_respond` `guided.py:2936` |
| `POST /{session_id}/guided/chat` (stays sync) | `src/elspeth/web/sessions/routes/composer/guided.py:5872` | `post_guided_chat` `guided.py:5867` (delegates to `guided_chat_atomic.post_guided_chat_schema8`) |

Note: `recompose` has **no request body parameter today** (`compose.py:90-98`: `session_id,
request, user, rate_limiter, _inflight_tally`). The spec's "recompose DTO gains a required
`operation_id`" therefore means a *new* request model, not a field addition.
`SendMessageRequest` is `src/elspeth/web/sessions/schemas.py:145` (`content: str` max 65536,
`state_id: UUID | None = None`).

---

## 2. `_track_compose_inflight` and its helpers (`src/elspeth/web/sessions/routes/_helpers.py`)

### 2.1 Constants, timer, cancel markers

| Symbol | Anchor | Exact definition |
|---|---|---|
| `_COMPOSER_HEARTBEAT_SECONDS` | `_helpers.py:2338` | `= 15.0` |
| `_COMPOSER_REQUEST_LEASE_SECONDS` | `_helpers.py:2340` | `= 60` — docstring (`2341-2348`) says it is a *copy* of `SessionComposerProgressAuthority(lease_seconds=60)` default (`coordination/composer_progress_authority.py:133`), which `web/app.py:1900-1902` does not override (verified: `SessionComposerProgressAuthority(session_engine, owner_instance_id=instance_id)`). |
| `_ComposerHeartbeatTimer` | `_helpers.py:2350-2360` | `@dataclass(frozen=True, slots=True)`; fields `now: Callable[[], float]`, `wait: Callable[[float], Awaitable[None]]` |
| `_COMPOSER_HEARTBEAT_TIMER` | `_helpers.py:2363` | `_ComposerHeartbeatTimer(now=time.monotonic, wait=asyncio.sleep)` — the test seam (monkeypatched by every heartbeat test) |
| `_ComposerHeartbeatCancel` | `_helpers.py:2366-2377` | `@dataclass(frozen=True, slots=True)`; `kind: Literal["transient_exhausted", "lease_lost", "renewal_defect"]` |
| singletons | `_helpers.py:2380-2382` | `_COMPOSER_HEARTBEAT_TRANSIENT_EXHAUSTED`, `_COMPOSER_HEARTBEAT_LEASE_LOST`, `_COMPOSER_HEARTBEAT_RENEWAL_DEFECT` |
| `_COMPOSER_HEARTBEAT_CANCELS_BY_ID` | `_helpers.py:2385-2392` | `Final[dict[int, _ComposerHeartbeatCancel]]` keyed by `id(marker)` |
| `_composer_heartbeat_cancel_of(exc: asyncio.CancelledError) -> _ComposerHeartbeatCancel \| None` | `_helpers.py:2395` | identity lookup of `exc.args[0]` |
| `_composer_heartbeat_failed_progress_event() -> ComposerProgressEvent` | `_helpers.py:2405` | `phase="failed"`, `reason="service_setup_failed"` (never `client_cancelled`) |
| `_composer_heartbeat_http_error(cancel: _ComposerHeartbeatCancel) -> HTTPException` | `_helpers.py:2421` | `transient_exhausted` → 503 `{"error_type":"database_unavailable", ...}`; else → 503 `{"error_type":"composer_request_lease_lost", "detail":"The server lost this composer request's lease before it finished. Please resubmit."}` |

### 2.2 The dependency

```python
async def _track_compose_inflight(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(get_current_user)],
) -> AsyncIterator[None]:
```
`_helpers.py:2442-2446`. Sequence:

1. `await _verify_session_ownership(session_id, user, request)` — `2480` (def `_verify_session_ownership(session_id: UUID, user: UserIdentity, request: Request) -> SessionRecord` at `2788`). The route body calls it *again* (`messages.py:140`, `compose.py:107`, `guided.py:2944`, `guided.py:5876`).
2. `registry = _get_composer_progress_registry(request)` — `2481` (def `367`, casts `request.app.state.composer_progress_registry`).
3. **Surface from URL path:** `surface: Literal["freeform","guided"] = "guided" if "/guided/" in request.url.path else "freeform"` — `2484`. A worker has no URL; the extracted lifecycle must take `surface` explicitly.
4. `lease_started_at = timer.now()` (`2489`) then `lease = await registry.start_request(sid, user.user_id)` (`2491`), stored as `request.state.composer_request_lease = lease` (`2492`).
5. `metrics_token = begin_composer_request_metrics(surface=surface)` (`2493`); `terminal_status = "completed"` (`2494`).
6. `owner_task = asyncio.current_task()`; `RuntimeError` if None (`2496-2498`).
7. `renew()` inner coroutine (`2500-2587`): loop `await timer.wait(15)`; per attempt `asyncio.timeout(renewed_at + 60 - attempt_started_at)` (`2544`) around `await registry.renew_request(lease)` (`2547`). Arms:
   - `(OperationalError, SQLAlchemyPoolTimeoutError)` → retry while `seconds_since_renewal + 15 < 60` (`lease_headroom_exhausted`, `2513-2538`); else `owner_task.cancel(_COMPOSER_HEARTBEAT_TRANSIENT_EXHAUSTED)` + `raise` (`2548-2555`).
   - `TimeoutError`: if the lease deadline did NOT expire → `RENEWAL_DEFECT` (`2556-2561`); if it expired → same headroom rule as transient (`2562-2565`).
   - `(ComposerRequestLeaseLost, PermissionError)` → `LEASE_LOST` immediately (`2566-2577`).
   - any other `Exception` → `RENEWAL_DEFECT` (`2578-2582`).
   - success resets `renewed_at`, `consecutive_failures` (`2583-2585`).
8. `heartbeat = asyncio.create_task(renew())` (`2587`) then `yield` (`2589`).
9. Exception mapping around the yield (`2590-2629`):
   - `CancelledError` without heartbeat marker → `terminal_status="cancelled"`, re-raise (`2591-2594`).
   - with marker: `renewal_failure = heartbeat.exception()` (`2598`); `if owner_task.uncancel() > 0` → `"cancelled"`, re-raise (external cancel racing) (`2602-2604`); else `"failed"`; `RENEWAL_DEFECT` re-raises the defect itself `from renewal_failure.__cause__` (`2608-2611`); else `raise _composer_heartbeat_http_error(...) from exc` (`2612`).
   - `TimeoutError` → `"timed_out"` (`2613-2615`).
   - `HTTPException`: `{408, 504}` → `"timed_out"`, `499` → `"cancelled"`, else `"failed"` (`2616-2623`).
   - other `Exception` → `"failed"` (`2624-2626`).
10. `finally` (`2627-2645`): `primary_error = sys.exception()`; cancel+await heartbeat only if not done (never re-raises a stored renewal failure); `await registry.finish_request(lease)`; then `finish_composer_request_metrics(metrics_token, status=terminal_status, primary_error=primary_error)`.

**Coupling that blocks direct reuse by a worker** (spec §3 says extract, not reuse):
- Request-scoped inputs: `request.url.path` (surface), `request.state.composer_request_lease` (read back by `_composer_progress_sink` at `_helpers.py:398-399` and by `guided_plan.py:430`), `request.app.state` (registry).
- The heartbeat cancels `asyncio.current_task()` of the *dependency*, relying on FastAPI running the yield-dependency and the endpoint in the same task. A worker must bind `owner_task` to the worker task explicitly.
- The HTTP 503 conversion (`_composer_heartbeat_http_error`) is an HTTP response; for the worker it must become a terminal-envelope `error` with the same body (`http_status: 503`, same `error_type`/`detail`).

### 2.3 Progress sink binding (depends on the request lease)

```python
async def _composer_progress_sink(
    registry: ComposerProgressRegistry | DatabaseComposerProgressRegistry,
    request: Request,
    *, session_id: str, request_id: str | None, user_id: str,
) -> ComposerProgressSink:
```
`_helpers.py:382-403`; body is `registry.claim_request(lease=cast(ComposerRequestLease, request.state.composer_request_lease), ...)`. Callers: `messages.py:248`, `compose.py:159`, `guided_plan.py:447`, `guided_chat_atomic.py:1486`, `guided.py:4267`, `guided.py:4761`, `guided.py:5442`. The worker needs a lease-parameter variant (the guided callers must keep the request-state form or be moved to the new parameter without behaviour change).

---

## 3. `_cancel_on_client_disconnect` (`_helpers.py`)

| Symbol | Anchor |
|---|---|
| `_CLIENT_DISCONNECT_CANCEL_MARKER = object()` | `2184` |
| `_is_client_disconnect_cancel(exc: asyncio.CancelledError) -> bool` | `2187` (`len(exc.args)==1 and exc.args[0] is marker`) |
| `_failure_log_request_id(request: Request) -> str \| None` | `2197` |
| `@contextlib.asynccontextmanager async def _cancel_on_client_disconnect(request: Request) -> AsyncIterator[None]` | `2209-2210` |

Mechanics (`2256-2335`):
- `task = asyncio.current_task()`; `None` → bare yield (`2256-2259`).
- Watcher task `_watch_disconnect` loops `await request.receive()`; a receive exception logs `compose.disconnect_watcher_receive_failed` and stops watching (`2266-2280`); `http.disconnect` → `triggered=True; task.cancel(_CLIENT_DISCONNECT_CANCEL_MARKER)` (`2281-2284`).
- `except CancelledError`: if `triggered`, `remaining = task.uncancel()`; marker set only when `remaining == 0`, else marker stripped so an external cancel keeps unwinding (`2289-2308`).
- `else` (normal exit): cancel+await watcher; if `task.cancelling() > 0` and `triggered`, flush with `await asyncio.sleep(0)` under suppress and `task.uncancel()`; if still `cancelling() > 0` raise `CancelledError()` (external cancel raced completion) (`2309-2327`).
- `finally`: cancel watcher without awaiting (`2328-2335`).
- Contract in docstring: the guarded block MUST be awaited inline in the route task, otherwise the `CancelledError` instance (carrying `attach_llm_calls` data) is laundered at a task boundary (`2224-2227`).

Mount sites (instrument `grep -rn "_cancel_on_client_disconnect(request)" src/`; known-positive `messages.py:357`):
- `messages.py:357` around `composer.compose(...)` (`358-379`).
- `compose.py:202` around `composer.compose(...)` (`203-215`).
- `guided_plan.py:518` around `composer_service.plan_guided_full_pipeline(...)` (`519`).
- `guided_chat_atomic.py:1505`.
- NOT on `/guided/respond` (`guided.py` imports only `_composer_heartbeat_cancel_of` and `_track_compose_inflight` from `_helpers`, `guided.py:143,156`; the guided classification test docstring confirms "where the route runs the disconnect watcher (PLAN, CHAT)").

### 3.1 CancelledError audit evidence + uncancel bookkeeping in the two freeform routes

`send_message` cancelled arm, `messages.py:1091-1154`:
- `llm_calls = _llm_calls_from_exception(exc)` (`1101`; def `_helpers.py:1902`: reads `exc.__dict__["llm_calls"]`, returns `()` if `llm_calls_durable is True` or absent/not tuple).
- If any: `await _join_shielded_task_after_cancellation(asyncio.create_task(_persist_llm_calls(service, session.id, llm_calls, compose_base_state_id, plugin_crash_pending=True, session_operation_context=compose_operation_lease.context), name="send-message-cancelled-llm-call-persist"))` (`1102-1115`). `_join_shielded_task_after_cancellation` is imported from `routes/guided_operations.py:205` (`messages.py:97`); an identical helper lives at `coordination/lifecycle.py:261`.
- `heartbeat_cancel = _composer_heartbeat_cancel_of(exc)` (`1123`); publishes `client_cancelled_progress_event()` or `_composer_heartbeat_failed_progress_event()` via the same shielded join (`1124-1135`).
- `terminal_status = "cancelled" if heartbeat_cancel is None else "failed"` (`1136`).
- `_is_client_disconnect_cancel(exc)` → `raise HTTPException(status_code=499, detail="Client disconnected while the compose turn was running.") from exc` (`1137-1153`); else bare `raise` (`1154`) — the heartbeat 503 conversion is left to `_track_compose_inflight`.
- `finally` (`1155-1178`): `_COMPOSER_REQUESTS_INFLIGHT.add(-1, {"endpoint":"send_message"})`, `_record_composer_request_terminal(terminal_status, endpoint="send_message")`, then bounded `asyncio.wait({auto_title_task}, timeout=2.0)`.

`recompose` cancelled arm, `compose.py:786-829` — **asymmetric with send_message**:
- Persists LLM calls with `with contextlib.suppress(asyncio.CancelledError): await asyncio.shield(_persist_llm_calls(...))` (`790-799`) and publishes progress the same way (`806-815`), NOT with `_join_shielded_task_after_cancellation`. A second cancel delivered during the shield lets the route continue before the persist is durable (the shielded inner task keeps running but is not joined). The spec's "LLM-call audit cohort persisted before terminal publication" must use the joined form for both kinds in the worker.
- 499 conversion `compose.py:819-825`; `finally` `827-829`.

LLM-call persistence signatures (`_helpers.py`):
```python
async def _persist_llm_calls(service: SessionServiceProtocol, session_id: UUID, llm_calls: tuple[ComposerLLMCall, ...], composition_state_id: UUID | None, *, plugin_crash_pending: bool, session_operation_context: SessionOperationContext) -> None   # 1914
async def _persist_turn_audit_cohort(service, session_id: UUID, tool_invocations: tuple[ComposerToolInvocation, ...], llm_calls: tuple[ComposerLLMCall, ...], *, tool_composition_state_id: UUID | None, llm_composition_state_id: UUID | None, parent_assistant_id: UUID | None = None, plugin_crash_pending: bool, session_operation_context: SessionOperationContext) -> tuple[PipelineDispatchAuditBinding, ...]   # 1988
```
Both write via `service.add_messages_atomic(..., writer_principal="compose_loop", session_operation_context=...)` (`1950-1956`, `2064-2070`) — i.e. **fenced by the SessionOperationLease context**. With `plugin_crash_pending=True` a `SQLAlchemyError` is counted (`_COMPOSER_PERSIST_FAILED_DURING_UNWIND_COUNTER`) and logged, not raised (`1958-1975`, `2072-2090`); with `False` it raises `AuditIntegrityError`.

Guided cancel arms that read the heartbeat marker (must stay byte-identical):
`guided.py:5684-5700` (`request_cancelled = caller_task.cancelling() > 0`, `heartbeat_cancelled = _composer_heartbeat_cancel_of(exc) is not None`), `guided_plan.py:757-770`, `guided_chat_atomic.py:2408-2425` (+ progress event `2470`).

---

## 4. Composer progress registry (`src/elspeth/web/composer/progress.py` + durable variant)

Two implementations selected at `web/app.py:1899-1917`: PostgreSQL → `DatabaseComposerProgressRegistry(SessionComposerProgressAuthority(session_engine, owner_instance_id=instance_id))` (`1900-1902`); otherwise in-memory `ComposerProgressRegistry()` (`1917`). There is **no Protocol class**; callers type it as the union `ComposerProgressRegistry | DatabaseComposerProgressRegistry` (`_helpers.py:367`, `383`).

`ComposerRequestLease` — `progress.py:48-54`: `@dataclass(frozen=True, slots=True)`; `request_token: str`, `session_id: str`, `user_id: str`. Docstring: "Server-owned identity of exactly one HTTP request lifecycle."

`ComposerProgressSnapshot.inflight_requests: int = 0` — `progress.py:70`.

### 4.1 In-memory `ComposerProgressRegistry` (`progress.py:73`)
- `async def start_request(self, session_id: str, user_id: str) -> ComposerRequestLease` — `98`: uuid4 token, stores lease, `begin_request` (+1 in `_inflight`).
- `async def renew_request(self, lease: ComposerRequestLease) -> None` — `106`: `self._leases[lease.request_token] != lease` → `RuntimeError("Composer request lease mismatch")`; an absent token raises `KeyError`. **Both classify as `renewal_defect`, not `lease_lost`**, in the heartbeat (only `ComposerRequestLeaseLost`/`PermissionError` are lease-lost, `_helpers.py:2566`).
- `async def finish_request(self, lease) -> None` — `111`; `end_request` decrements (`143`), `KeyError` on unmatched teardown (tested `test_progress.py:331,338`).
- `async def claim_request(self, *, session_id, request_id, user_id, lease) -> ComposerProgressSink` — `118`: ownership check, `renew_request`, then `bind_request` (generation fencing, `203-235`).
- `get_latest` overlays live `_inflight` count (`261-283`); `list_active` (`285`) includes sessions with `inflight>0` even before first publish.
- `publish_replay_if_unclaimed(...)` — `175` (used by guided replay).

### 4.2 Durable `SessionComposerProgressAuthority` / `DatabaseComposerProgressRegistry` (`src/elspeth/web/coordination/composer_progress_authority.py`)
- `class ComposerRequestLeaseLost(RuntimeError)` — `109`.
- `ComposerProgressIdentityInactive(PermissionError)` `113`, `ComposerProgressSessionUnavailable(PermissionError)` `122` — both classify as **lease_lost** in the heartbeat (PermissionError arm).
- `SessionComposerProgressAuthority.__init__(self, engine: Engine, *, owner_instance_id: str, lease_seconds: int = 60, snapshot_ttl_seconds: int = 86400)` — `133`; refuses non-PostgreSQL (`134-135`).
- `begin_request(session_id, user_id) -> ComposerRequestLease` `146`: `cleanup_expired()`, ownership + identity re-check, inserts `composer_inflight_requests` row with `expires_at = now + lease_seconds`.
- `heartbeat_request(lease)` `267`: ownership/identity re-check then CAS update on `(request_token, session_id, identity_id, owner_instance_id, expires_at > now)`; no row → `ComposerRequestLeaseLost("Composer request lease cannot be renewed")` (`286-287`).
- `end_request(lease)` `289`: exact-token delete, works after archival.
- `bind_request(lease, request_id) -> str` `302`: requires a live inflight row else `ComposerRequestLeaseLost` (`320-321`); replaces the session's snapshot row with a new generation.
- `DatabaseComposerProgressRegistry` `491`: async facade over `run_sync_in_worker`; `start_request` `497` shields admission and releases the exact lease if cancelled; `finish_request` `516` shielded exact-token delete; `renew_request` `533`; `claim_request` `536`.

### 4.3 Who calls start/renew/finish (instrument + control)
`grep -rn "\.start_request(\|\.renew_request(\|\.finish_request(\|\.claim_request(\|composer_request_lease\b" src/elspeth` → only `_helpers.py:2491,2492,2547,2639,398-399`, `guided_plan.py:430`, plus intra-registry self-calls `progress.py:103,116,128`. Known-positive: `_helpers.py:2491` (definition site of the call) matched. Therefore **`_track_compose_inflight` is the sole owner of request-lease lifecycle**; the worker will be a second owner.

Readers of the inflight count: `GET /{session_id}/composer-progress` (`routes/composer/state.py:524-545`, `registry.get_latest`) and `GET /_active` (`routes/sessions.py:850-885`, `registry.list_active`). Frontend consumers: `frontend/src/stores/sessionStore.ts`, `types/index.ts` (spec: may continue as UX, cannot declare settlement).

---

## 5. `SessionOperationLease` (`src/elspeth/web/coordination/lifecycle.py`)

`@final class SessionOperationLease` — `272`. Slots `285-296`.

```python
def __init__(self, authority: SessionOperationAuthority, context: SessionOperationContext, *, lease_seconds: int, renew_interval_seconds: float, fork_authority: SessionForkAuthority | None = None) -> None   # 298; starts _renew_forever task at 321-324
@classmethod
async def acquire(cls, authority: SessionOperationAuthority, *, session_id: UUID, operation_kind: SessionOperationKind, owner_instance_id: str, lease_seconds: int, renew_interval_seconds: float | None = None) -> SessionOperationLease   # 325-335
@classmethod async def adopt(...)             # 426
@classmethod async def adopt_fork_child(...)  # 506
@property fence -> SessionOperationFence      # 593
@property context -> SessionOperationContext  # 598
@property renewal_error -> BaseException | None  # 603
@property closed -> bool                      # 608
@property disposition -> SessionOperationLeaseDisposition  # 612
def raise_if_lost(self) -> None               # 616
def guard_external_effect(self) -> None       # 621 (authoritative CAS)
async def wait_until_lost(self) -> BaseException  # 642
def create_task[T](self, coroutine, *, name=None) -> asyncio.Task[T]  # 650 (owned child; cancelled on renewal loss)
async def close(self) -> None                 # 1035
async def __aenter__(self) -> SessionOperationLease  # 1068 (raise_if_lost)
async def __aexit__(...) -> Literal[False]    # 1074 (close; body+cleanup failures → BaseExceptionGroup)
```

Timing: `_validate_lifecycle_timing` `106-117`: `lease_seconds` exact int 1..3600; default interval `min(lease_seconds / 3, 30.0)` (`_MAX_RENEW_INTERVAL_SECONDS = 30.0` at `36`). Routes pass `lease_seconds=service.session_operation_lease_seconds`; `SessionServiceImpl.__init__(..., session_operation_lease_seconds: int = 30, ...)` `sessions/service.py:4480`; `web/app.py:1826-1851` does **not** override it → **30 s lease, 10 s renewal** in production.
`owner_instance_id=service.session_operation_owner_instance_id` (`service.py:4536`), set from `instance_id` at `app.py:1847`.

`SessionOperationKind` (`src/elspeth/contracts/session_operation.py:10-18`): `CREATE, COMPOSE, PROPOSAL, EXECUTE, ARCHIVE, PROGRESS, BLOB_READ, SESSION_FORK`. `SessionOperationFence` `33-43` (`session_id, operation_id, lease_token, operation_epoch`); `SessionOperationContext` `48-56` (`fence, operation_kind`).

**How freeform routes obtain it** — identical shape in both, nested INSIDE the in-process lock:
```python
compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
async with (
    compose_lock,
    await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as compose_operation_lease,
):
```
`messages.py:144-154`, `compose.py:112-122`. Its `.context` is threaded into every session write (`add_message_with_transcript` `messages.py:231-238`, `composer.compose(session_operation_context=...)` `messages.py:367`/`compose.py:212`, `_persist_*` helpers, `_handle_*` helpers). `send_message` auto-title uses `compose_operation_lease.create_task(...)` (`messages.py:322`).

**Exclusion semantics:** `authority.acquire` (repository impl `coordination/repository.py:4756-4832`) is **non-blocking**: a live, unreleased, unexpired fence → `SessionOperationConflictError` (`4799-4809`); class `repository.py:283-287` ("session operation is already active"). The fence row is one per session and every kind except `BLOB_READ` is exclusive (`4764-4767`), so a COMPOSE is refused while EXECUTE/PROPOSAL/ARCHIVE/another COMPOSE holds it. HTTP mapping: `session_operation_handlers.py:28-30` → **409 `{"detail":"Session operation is already active"}`**; `SessionOperationFenceLost` → **404 `{"detail":"Session not found"}`** (`23-26`). The spec's "if another session operation owns the lease, the worker releases its claim and leaves the job queued" must catch `SessionOperationConflictError` (not rely on the HTTP handler).

**Fencing of writes:** `repository.py:5254-5283` `mutate` CASes the exact context under `_locked_transaction` before running the mutation; `compare_and_swap` `5103`; `renew` `5064`; `release` `5620`.

**Expiry / loss:** `_renew_forever` (`664-692`) records the first renewal error via `_record_renewal_error` (`694-701`), which sets `_lost_event` and **cancels only tasks created via `lease.create_task`** — it does NOT cancel the route/owner task. A lost COMPOSE lease therefore surfaces as a `SessionOperationFenceLost` on the next fenced write (→ 404 today), not as an immediate cancel. The spec's "Loss of either fence cancels work" requires the worker to add `wait_until_lost()` (`642`) → cancel-owner wiring that the routes do not have today. Precedent for that wiring: `execution/service.py:2679` (`asyncio.wait_for(session_operation_lease.wait_until_lost(), timeout=poll_seconds)`).

---

## 6. Per-session `asyncio.Lock`

```python
class _SessionComposeLockRegistry:            # _helpers.py:311
    _session_locks: WeakValueDictionary[str, asyncio.Lock]   # 325
    async def get_lock(self, session_id: str) -> asyncio.Lock          # 333
    async def cleanup_session_lock(self, session_id: str) -> None      # 344
def _get_session_compose_lock_registry(request: Request) -> _SessionComposeLockRegistry   # 352 (lazily on app.state.session_compose_lock_registry)
```
Process-local only. Shared by far more than compose (instrument `grep -rn "_get_session_compose_lock_registry(" src/elspeth`; known-positive `messages.py:144`): `messages.py:144`, `compose.py:112`, `interpretation.py:128,272`, `composer/proposals.py:283,693`, `composer/state.py:751,886,1090`, `composer/guided.py:730,1028,1334,1637,2050,3032` (+ namespaced admission lock `3701` `f"{session_id}:guided-respond-admission"`), `guided_chat_atomic.py:1282` (+ `1419` `:guided-chat-admission`), `guided_plan.py:462`, `execution/routes.py:1456`; archive cleanup `routes/sessions.py:967-968`. Also `guided_operations.py:73` waits `asyncio.wait_for(lock.acquire(), timeout=GUIDED_RESPOND_ADMISSION_WAIT_SECONDS)`.

Consequence for the worker: freeform work today holds this lock across the whole provider turn, so state edits/proposals/interpretation/execution in the same process queue behind it. If the worker keeps taking it (spec: "queueing aid"), that serialisation is preserved; if it drops it, those routes rely only on the SessionOperationLease 409.

---

## 7. Composer wall-clock budget

- Setting: `composer_timeout_seconds: float = Field(..., gt=0)` — `web/config.py:325`. Transport coupling validator `_validate_composer_timeout_transport_headroom` `config.py:1186-1208` (`composer_timeout_seconds > ceiling - headroom` → ValueError, `1201-1207`); fields `composer_transport_idle_ceiling_seconds` `335`, `composer_transport_headroom_seconds` `349`. Underfunded-turn warning `1210-1233`.
- `ComposerServiceImpl.__init__` stores ONE `self._timeout_seconds = settings.composer_timeout_seconds` (`composer/service.py:2542`) used by both surfaces:
  - freeform `compose()` (`service.py:4112-4127` signature — **no `deadline` parameter**) computes `deadline = asyncio.get_event_loop().time() + self._timeout_seconds` at `4163`, i.e. at call time, **after** the compose lock and SessionOperationLease were acquired and the user row inserted. Queue/lock-wait time is NOT charged today. The spec ("Queue time consumes that budget; the worker uses only the remaining time") needs a new deadline input on `compose()`.
  - freeform empty-pipeline planner `_plan_and_stage_empty_pipeline` `timeout_seconds=self._timeout_seconds` (`5382`).
  - guided: `plan_guided_full_pipeline` (`4459`), `plan_guided_pipeline` (`4873`), plus route-level reads `guided_chat_atomic.py:729,790,836,874,897`, `guided.py:5042`, `pipeline_settlement.py:273`. The spec's guided cap `min(composer_timeout_seconds, ceiling − headroom)` has to reach all of these; today there is a single service-level value.
  - Cooperative enforcement: `_call_llm_before_deadline` `service.py:9469` (per-call `asyncio.wait_for(remaining)`), raising `ComposerConvergenceError.capture(budget_exhausted="timeout", ...)`; planner: `pipeline_planner.py:3776-3784` (`PipelinePlannerError(code="TIMEOUT")`), `4073`.
- **HTTP mapping of timeouts in the freeform routes:**
  - Wall-clock exhaustion → `ComposerConvergenceError(budget_exhausted="timeout")` → route sets `terminal_status="timed_out"` (`messages.py:381`, `compose.py:217`) and raises **HTTP 422** with body from `_handle_convergence_error` (`messages.py:406`, `compose.py:241`), carrying `reason="convergence_wall_clock_timeout"` and `timeout_seconds=settings.composer_timeout_seconds` (`_helpers.py:3134-3143`). Not 504.
  - Planner provider timeout → `_FREEFORM_PLANNER_FAILURE_HTTP["provider_timeout"] = (504, ...)` (`_helpers.py:2898`) raised at `messages.py:682` and `compose.py:447`. This is the only 504 reachable in the two routes (instrument `grep -rn "504\|408" src/elspeth/web/sessions/routes/ src/elspeth/web/composer/guided/`; also hits `state.py:849` [not a compose route], `guided_operations.py:98` [guided]).
  - No 408 is raised anywhere in `src/elspeth/web/sessions/routes/` (same instrument); the `{408, 504}` arm in `_track_compose_inflight` (`_helpers.py:2617`) is reached only by 504.

---

## 8. Request metrics and terminal status labels

`src/elspeth/web/composer/provider_telemetry.py`:
- `ComposerTelemetrySurface = Literal["freeform", "guided"]` (`18`); `ComposerRequestTerminalStatus = Literal["completed", "failed", "timed_out", "cancelled"]` (`19`).
- `_REQUEST_METRICS_STATE: ContextVar[_RequestMetricsState | None]` (`62-65`) — **task-local via ContextVar**; `_RequestMetricsState` (`48-54`) is mutable and accumulates `provider_call_count` / `provider_terminal_status` as audit rows commit.
- `def begin_composer_request_metrics(*, surface: ComposerTelemetrySurface) -> Token[_RequestMetricsState | None]` — `84`.
- `def mark_composer_request_terminal(status: ComposerRequestTerminalStatus) -> None` — `191`.
- `def finish_composer_request_metrics(token, *, status, primary_error: BaseException | None = None) -> None` — `199`; precedence `state.terminal_status or (status if status != "completed" else state.provider_terminal_status) or status` (`226`). Emits `composer.request.duration` and `composer.request.provider_calls` histograms with `{surface, status}`.
- Provider-call projection is fed post-commit from `sessions/service.py:9475` (`record_settled_composer_audit_message`, freeform), `11366`, `14670` (guided), `14829`. Because the aggregate is found through the ContextVar, **the worker must call `begin_...` inside the worker task (or a parent whose context the task copies) that performs the audit commits**; the value is picked up only when the commit's projection runs in that context.

Route-level metrics in `_helpers.py`:
- `_ComposerRequestEndpoint = Literal["send_message", "recompose"]` (`1337`); `_ComposerRequestTerminalStatus` (`1338`).
- `_COMPOSER_REQUESTS_INFLIGHT` up-down counter `composer.requests.inflight` (`1339`); `_COMPOSER_REQUEST_TERMINAL_COUNTER` `composer.request.terminal.total` (`1344`).
- `def _record_composer_request_terminal(status, *, endpoint) -> None` (`1380-1386`): `mark_composer_request_terminal(status)` + counter add.
- Route usage: `messages.py:265` (+1) / `1156` (−1) / `1157`; `compose.py:184` / `828` / `829`. Route-local `terminal_status` initialised `"failed"` (`messages.py:266`, `compose.py:185`).

---

## 9. Tests that encode lifecycle failure intent (to adapt, not weaken)

Test-file instrument: a symbol grep over `tests/` for the lifecycle symbols; known-positive `test_compose_heartbeat_renewal.py` matched; known false positives excluded by per-file symbol counts (`test_textract_*`, `test_jwks_cache_lifecycle.py` hit only the generic `start_request`; `test_session_db_mutation_authority.py`, `test_schema.py`, `fake_http.py` hit only `inflight_requests` schema/fixture names).

### 9.1 `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py` (512 lines) — drives the real dependency via `_run_fake_route` (`137-183`, path `/api/sessions/1/messages` → freeform)
Harness asserts teardown never leaks: `assert not child.cancelled(); assert child.exception() is None` (`181-182`).
- `test_one_transient_renew_failure_then_success_keeps_the_turn_alive` `188`: events `["begin", "renew_failed:<T>", "renew_ok"]`, last `"end"`, `finished == [("completed", None)]`.
- `test_transient_failures_within_headroom_are_retried` `202`.
- `test_headroom_matches_lease_arithmetic` `218`: pins `_COMPOSER_REQUEST_LEASE_SECONDS == SessionComposerProgressAuthority lease default`, `15.0`, `60` (`224-228`).
- `test_repeated_transient_failures_past_headroom_cancel_as_server_fault` `232`: `cancel.kind == "transient_exhausted"`, 503 body, `not isinstance(error.__cause__, OperationalError)` (no DB URL leak), `cancelling_after_teardown == 0`, metrics `["failed"]`.
- `test_lease_lost_cancels_immediately_as_server_fault` `263` (parametrised `ComposerRequestLeaseLost`/`PermissionError`): `renew_calls == 1`, `kind == "lease_lost"`, 503 `composer_request_lease_lost`, `cancelling_after_teardown == 0`.
- `test_renewal_defect_cancels_immediately_and_surfaces_the_defect` `288`: `dependency_error is defect`, `finished == [("failed", defect)]`.
- `test_external_cancel_racing_the_heartbeat_keeps_unwinding_cancelled` `305`: result is `CancelledError`, metrics `["cancelled"]`.
- `test_plain_cancelled_error_is_not_a_heartbeat_cancel` `317`.
- `test_dependency_teardown_503_reaches_the_http_client` `323`: real FastAPI app; JSON 503 exact body.
- `test_send_message_heartbeat_cancel_publishes_server_fault_not_client_cancelled` `356` (**freeform route-level**): composer cancelled, 503 `composer_request_lease_lost`, snapshot `phase=="failed"`, `reason=="service_setup_failed"`, `!= "client_cancelled"`, equals `_composer_heartbeat_failed_progress_event()`.
- `test_slow_failing_renewals_cancel_before_the_lease_lapses` `415` (params `(0.0,3),(10.0,2),(30.0,1)`): cancels while `clock.now_seconds < 60`.
- `test_hung_renewal_is_abandoned_when_the_lease_runs_out` `468`: `registry.abandoned is True`, `kind == "transient_exhausted"`.
- `test_timeout_error_raised_by_the_renewal_itself_stays_a_renewal_defect` `499`.
Adaptation note: every assertion except the two HTTP-level tests (`323`, `356`) is about the *renewal policy*; the spec requires it be extracted so these can run against both owners. `356` must become a worker test whose terminal envelope has `http_status: 503` and the identical `detail` body, plus the same progress assertions.

### 9.2 `tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py` (143 lines) — freeform recompose
- `test_recompose_heartbeat_cancel_records_failed_and_answers_503` `97`: composer cancelled; 503 exact body; snapshot equals heartbeat-failed event; `snapshot.inflight_requests == 0`; terminal counter `[{"endpoint":"recompose","status":"failed"}]`.
- `test_recompose_plain_task_cancel_is_still_a_client_cancel` `120` (control): `phase == "cancelled"`, `reason == "client_cancelled"`, counter status `cancelled`.

### 9.3 `tests/unit/web/sessions/routes/test_composer_request_telemetry.py` (261 lines)
- `test_request_dependency_projects_closed_surface_and_success` (parametrised paths `/messages`→freeform, `/guided/plan|respond|chat`→guided, `95-113`): `lifecycle[0] == ("begin", surface)`, finish `"completed"`, registry `["begin","end"]`.
- `test_guided_respond_route_mounts_request_lifecycle_dependency_exactly_once` `116`: `dependencies.count(_helpers._track_compose_inflight) == 1` on `/guided/respond` — a guard the guided side must keep.
- `test_request_dependency_projects_closed_failure_status` `135`: `RuntimeError→failed`, `TimeoutError→timed_out`, `HTTPException(504)→timed_out`, `HTTPException(499)→cancelled`, `CancelledError→cancelled` (guided path).
- `test_metrics_token_pairing_failure_does_not_replace_request_failure` `154`: primary `LookupError` survives a spent token; registry `["begin","end"]`.
- `test_existing_terminal_counter_also_marks_request_aggregate` `192`.
- `test_request_renews_while_provider_waits_and_stops_after_teardown` `209`: no renew after teardown.
- `test_unauthorized_request_never_enters_cluster_inflight` `229`: ownership 404 → `registry.events == []`.
- `test_failed_durable_admission_never_opens_metrics_scope` `245` (`PermissionError`, `CancelledError`): `begun == []`.

### 9.4 `tests/integration/web/composer/guided/test_guided_heartbeat_cancel_classification.py` (355 lines) — guided, must stay unchanged
PLAN `178/199/216`, CHAT `244/265/282`, RESPOND `325/343`: heartbeat cancel → 503 `_LEASE_LOST_503`, operation row `failure_code == "operation_failed"`, metrics `["failed"]`, `inflight_requests == 0`; plain cancel → `request_cancelled`, `("cancelled","client_cancelled")`; disconnect (PLAN, CHAT only) → 499 + `request_cancelled`.

### 9.5 `tests/unit/web/sessions/test_routes.py` (16292 lines)
- `test_client_disconnect_cancels_compose_turn` `4752` (freeform `/messages`, raw ASGI `receive` yields `http.disconnect` at `4816`): `composer.cancelled is True`; status **499**; messages `["user"]` only (no zombie assistant row); follow-up send 200 (session not wedged).
- `test_external_cancel_racing_disconnect_keeps_unwinding` `4865`: `task.cancelling() == 2`, `marked is False`, task ends cancelled.
- `test_composer_progress_reports_inflight_request_count` `4934`: `/composer-progress` `inflight_requests == 1` while parked, `0` after.
- `test_client_disconnect_cancels_guided_chat_turn` `5766` (guided).
- `class TestComposerCancellationLifecycle` `11827`: `test_send_message_publishes_cancelled_snapshot_on_cancellation` `11835` (`phase cancelled`, `reason client_cancelled`, `list_active == ()`); `test_send_message_persists_cancelled_llm_call_audit_sidecar` `11872` (exactly one LLM audit row, `status == "cancelled"`, `messages_hash` preserved); `..._terminal_counter_with_cancelled_status` `11907`; `..._completed_status` `11945`; recompose mirrors `11974`, `12000`, `12033`; `test_inflight_gauge_increments_then_decrements_across_request` `12066` (gauge `+1` then `-1`).
- `TestComposerProgressRoutes` `11383` / `TestComposerInFlightEndpoint` `11725`: progress/`_active` ownership and revocation translation (401/404).

### 9.6 Others
- `tests/unit/web/test_sessions_composer_attribute_contracts.py:202` `test_disconnect_marker_requires_the_private_token_by_identity`; `:195` `_failure_log_request_id` parsing.
- `tests/unit/web/composer/test_provider_telemetry.py` (`169`–`348`): aggregate precedence, pairing bugs propagate, emission failures subordinate.
- `tests/unit/web/sessions/test_composer_provider_telemetry.py:144` `test_freeform_cancellation_projects_worker_commit_before_reraising` (`request_calls.points == [(1, {"surface":"freeform","status":"cancelled"})]`), `:224/:271` guided.
- `tests/unit/web/composer/test_progress.py`: `286` inflight enrichment, `331/338` unmatched/double teardown rejected, `639` queued request active before progress, `657` DB admission cancellation joins+releases exact lease, `718` finish survives caller cancellation.
- `tests/testcontainer/web/test_cross_process_composer_postgres.py`: mounts the real dependency on a guided route in spawned processes (`74`, `85`, heartbeat `0.2` s at `89`); `242` two processes, `pg_sleep(2.5)` past initial lease, `inflight_requests` 2→1→0; `425` expired owner cannot renew/publish (`ComposerRequestLeaseLost`); `492` heartbeat keeps long request live.
- `tests/unit/web/sessions/routes/test_compose_lock_registry.py:10,36` weak reclamation of locks.
- `tests/testcontainer/web/test_composer_progress_quota_lock_order_postgres.py` (lock-order/deadlock; uses `inflight_requests` once).

---

## 10. Is there ANY instance-wide limit on concurrent provider turns / compose requests today?

**Answer: No application-level limit exists.** Measured:

1. Semaphores. `grep -rn --include=*.py 'Semaphore' src/elspeth/web | wc -l` → **0**. Positive control, same pattern over `src/elspeth`: `plugins/infrastructure/templates.py:55` (`threading.BoundedSemaphore(2)`), `plugins/infrastructure/pooling/executor.py:124`, `core/security/web.py:95` — so the instrument finds semaphores where they exist; none is in the web tier.
2. Capacity counters compared against a threshold. Regex `inflight[a-z_\[\]\.]* *(>=|>|<=|<) *...` over `src/elspeth/web` matches only `composer_inflight_requests_table.c.expires_at` expiry predicates in `composer_progress_authority.py:183-459` (instrument fires — non-empty — and no match is a count cap). `_COMPOSER_REQUESTS_INFLIGHT` (`_helpers.py:1339`) is an OTel gauge only; `ComposerProgressRegistry._inflight` (`progress.py:92`) is read only for `inflight_requests` display (`progress.py:280`).
3. 429 sites in web: only `middleware/rate_limit.py:129,161` — **per-user per-minute request rate** (`ComposerRateLimiter` / `SharedRateLimiter`, wired `app.py:1909-1922`, `composer_rate_limit_per_minute` `config.py:354`), checked at route start (`messages.py:138`, `compose.py:106`; `guided_plan.py` via `rate_limiter`). It bounds admissions per minute per user, not concurrency, and is not instance-wide.
4. 503-on-load: none for compose. The 503s in `app.py:2099-2437` are readiness/draining; `rate_limit.py:157` is "Rate limit service unavailable".
5. Provider calls are async `litellm.acompletion` under `asyncio.wait_for` (`service.py:8373-8376`, `9255`, `9380`; `pipeline_planner.py:4073`) — not routed through a bounded pool.
6. The only process-wide bound on the path is **`run_sync_in_worker`'s thread pool** (`web/async_workers.py:14-24`: `MAX_WORKERS = 16`, `MAX_QUEUED = 16`, `ADMISSION_CAPACITY = 32`, `ADMISSION_WAIT_SECONDS = 1.0`; saturation raises `AsyncWorkerAdmissionTimeoutError(TimeoutError)` at `async_workers.py:189`). All session DB work, lease acquire/renew, and progress writes go through it, so many concurrent turns can starve each other's DB calls (a heartbeat renewal hitting this surfaces as `TimeoutError` → classified `renewal_defect` unless it came from the lease deadline). This is shared by guided, freeform and everything else — it is not a compose-turn limit.
7. Server level: packaged launch `elspeth web` → `uvicorn.run(...)` at `src/elspeth/cli.py:5047-5054` with no `limit_concurrency`. The only `--limit-concurrency 100` in the tree is `deploy/elspeth-web.service:34`, which is **untracked and gitignored** (`.gitignore:310`); the tracked unit `deploy/linux-systemd/elspeth-web.service:20` runs `elspeth web` without it. `grep -rn "limit-concurrency" deploy/ Dockerfile src/ docs/` → only that untracked line. Even there it caps all HTTP connections (uvicorn 503), not provider turns.
8. Per-session serialisation (not instance-wide): the in-process `asyncio.Lock` (§6) and the durable `SessionOperationLease` fence (§5, 409 on conflict). Guided admission locks are per-session too (`guided_operations.py:73`).

Implication for the spec's §3 requirement ("the plan must state … whether any existing instance-wide provider-turn limit exists that both paths would share"): **none exists**; the new worker capacity will be the first, and it will bound freeform only. Guided/tutorial turns remain unbounded except by per-user rate and per-session exclusion; both still share the 16+16 `run_sync_in_worker` pool with the worker.

---

## 11. Staleness of the old plan
`docs/plans/2026-09-20-composer-async-operations.md` §5 is titled "Cut over all five HTTP routes" (`:209`) and step 3 removes `_track_compose_inflight` from "these five route signatures" (`:231`) — contradicted by the 2026-09-25 freeform-only ruling. Its §3 (`:139-161`) extraction guidance for the heartbeat is still the right shape.
