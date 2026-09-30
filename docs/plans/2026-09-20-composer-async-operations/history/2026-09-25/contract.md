# Interface contract for the composer async operations plan (freeform only)

This is the authority every task was drafted against. Names, signatures, table
columns, wire shapes and module homes below are fixed, **except where the
"Adopted deviations" table immediately below overrides them**. That table wins
over any later section of this file; the task files already use the adopted
form. An executor who finds a further impossibility records it in the task file
with the measured reason rather than renaming silently.

Tree: `release/0.8.1` @ `d479eb2b4` (tasks re-measured at `ea5fa50d5`; no change
under the cited web paths). Spec: `docs/specs/2026-09-16-composer-async-operations-design.md`
(freeform-only amendment 2026-09-25).

## Adopted deviations (override the sections below)

| Name / area | Contract text below | Adopted (owning task) | Why |
|---|---|---|---|
| `admit` | takes `rate_limit: Callable[[], None]` | no `rate_limit`; coroutine `admit_composer_operation(authority, *, rate_limit: Callable[[], Awaitable[None]], ...)` does get → charge-if-absent → `admit` (T04) | both limiters are coroutines and the shared one commits on its own connection (`rate_limit.py:100,152-155`, `rate_limit_authority.py:85`) |
| `request_cancel`, `settle_lost` | no factory argument | required `cancelled_failure: ComposerOperationCancelledFailure` (T04) | the D14 cancel body with the row's `request_id` is built inside the locked transaction |
| authority read API | no queued lister | `list_expired_queued(*, limit)` (T04); the reaper settles those rows through `settle_unstarted(None, ...)` (T11) | `claim_next` skips deadline-passed and cancel-marked rows, so only this lister finds them |
| archived-session `running` row | not specified | `settle_lost_inactive_session(*, session_id, operation_id, failure, cancelled_failure)` (T04), an unfenced settle guarded inside the locked transaction by `sessions.archived_at IS NOT NULL`; the reaper's `OWNER_INACTIVE` arm calls it (T11) | the reaper's COMPOSE acquire raises `OWNER_INACTIVE` for an archived session, so without it the row stays `running` and charges capacity forever; the archive already proves the owner's fence is dead |
| `claim_next` | one batch transaction with SKIP LOCKED | discovery read, then one `locked_session_transaction` per candidate (T04) | lock order advisory → row, as every other session writer |
| Tier-1 decoration of the operation exceptions | decorated | plain `RuntimeError` subclasses (T02) | `@tier_1_error` raises `PermissionError` outside `elspeth.contracts/engine/core` (`tier_registry.py:73,149,161`) |
| `ComposerOperationError` home | `schemas.py` or re-export | `composer_operations.py`, a `BaseModel` with `_StrictResponse`'s config, re-bound in `schemas` (T02) | avoids an import cycle |
| record invariants | table list | adds: queued ⇒ `user_message_id` NULL; completed ⇒ `started_at` set and `cancel_requested_at` NULL; six time-ordering checks; canonical result JSON (T02, mirrored by T03) | forced by other clauses of this contract |
| `ComposerOperationFenceLost` constructor | unspecified | `(*, session_id, operation_id, attempt=None)` (T02) | carries facts only; the reaper holds no claim |
| `ComposerOperationCancelledDuringTurn` constructor | unspecified | `(claim)` (T06) | |
| `ComposerOperationAssistantWrite` | content, tool_calls, composition_state_id, writer_principal, raw | `(message_id: UUID, content, raw_content, composition_state_id)` (T06, T10) | cohort tool rows need the parent id before insert |
| `run_composer_turn` | `(services, turn, *, lease, running)` | adds `request_lease: ComposerRequestLease` (T10, T11) | the progress sink needs the worker-owned composer request lease |
| `_ComposerOperationCancel` | worker module, 2 kinds | `composer_turn.py`, 3 kinds incl. `shutdown` (T10 creates, T11 extends) | import direction; worker `stop()` must not read as a user Stop |
| unmarked `CancelledError` out of a detached turn | not specified | server fault: 503 `worker_lost`, progress `failed`/`service_setup_failed` (T11); only the cancel endpoint's marker is a user Stop (499 `request_cancelled`) | a detached turn has no client socket |
| `instance_draining` | `asyncio.Event` | `threading.Event` (T11; T12 helper matches) | readiness refuses anything else (`readiness.py:663-664`) |
| cancelled-turn terminal write | the turn's cancel arm | the worker's job frame, after the sidecar join (T11) | the heartbeat 503 is produced after the arm re-raises |
| lifespan placement | after `orphan_task` | construct + startup `reap_once` after `recover()`, `start()` after the orphan done-callback (T11) | a failing startup reap would otherwise leak the orphan task |
| strict `SendMessageRequest` | Task 10 | Task 10, with a transitional `LegacySendMessageRequest` keeping the still-synchronous route on today's body until T13 deletes it | commit order: T11/T12 need the strict DTO before the cutover |
| settlement signature (D6) | `services`, `user_id` | as stated, plus T01's required `commit_timeout_seconds: float` | T01 names the commit budget per caller |
| poll/cancel `PermissionError` arms | 401/404 translation | none (T12) | the authority methods take no actor and never raise those classes; ownership is re-checked on every call |
| client deadline | `useComposer.ts` | the `sessionStore` poll loop (T14) | `deadline_at` exists only on the poll body and must survive reload and A→B→A |
| epoch bump | "Task 15 (last)" in §Table | Task 17 (next free epoch, 68 at plan time) | this file's own task table |
| spec §2 D8 line | "Task 16 edits the spec" | Task 17 | Task 16 is test-only |

### Review-pass decisions (2026-09-25; override everything above and below)

| Id | Decision | Owning tasks |
|---|---|---|
| F-B2/C2 | **A committed cancel fences non-audit session writes.** `SessionServiceImpl._require_session_operation_context_on_connection` (and every other DB-side COMPOSE context check the composer turn's writes pass through, measured) gains a predicate: when a `composer_async_operations` row with `status='running'` is bound to this exact fence triple (`session_operation_id`, `session_operation_lease_token`, `session_operation_epoch`) and has `cancel_requested_at IS NOT NULL`, raise `ComposerOperationCancelledDuringTurn` unless the caller passes `audit_only=True`. Only the LLM-call/tool audit cohort writers pass `audit_only=True`. The owner, on any cancel after a compose result exists (marker delivered in the tail, or the terminal CAS losing to a committed cancel), persists that result's `llm_calls` (and tool rows) through the audit-only path, joined, **before** writing the `request_cancelled` terminal. T06's composite rolls back the assistant row but not the separately persisted audit cohort. | T06 (predicate + test), T10 (expose the compose result to the job frame), T11 (join + ordering), T16 (PG assertion) |
| F-B3 | `ComposerOperationStatusResponse` gains `deadline_remaining_ms: int` (≥ 0), computed by the poll and cancel routes from the **database** clock (`max(0, deadline_at − db_now)`). The SPA arms `localDeadline = performance.now() + deadline_remaining_ms + COMPOSE_CLIENT_GRACE_MS` on every non-terminal body, tighten-only (`Math.min` with the current deadline), and never compares `deadline_at` to `Date.now()`. | T02, T12, T14 |
| F-B4 | The worker's terminal fail write retries the transient family (`OperationalError`, `AsyncWorkerAdmissionTimeoutError`) with bounded backoff while the adopted lease is live, re-reading `status` between attempts. Any unexpected exception from the start composite or from `adopt` settles `operation_failed` 500 (with `diagnostic_id`) through `settle_unstarted` or the fenced fail write, never by claim-expiry looping to 504 (except an adopt defect after `adopt` released its context; see `B4-residual`). | T11, T00 |
| F-M3 | Owner-side settle for its own job whose bound fence lapsed while this instance lives: `ComposerAsyncOperationAuthority.settle_own_lapsed(*, session_id, operation_id, owner_instance_id, failure, cancelled_failure)`, guarded under the session lock by `status='running' AND claim_owner_instance_id = owner AND` the bound fence row has the same triple with `released_at IS NULL AND lease_expires_at <= db_now`. It reads the fence and never advances or takes it over. | T04, T11, T16 |
| F-C3 | While a claimed job waits for the in-process compose lock it observes local cancel, the committed cancel marker (via claim renewal, which returns the row's `cancel_requested_at` and `deadline_at`) and its deadline, and settles `request_cancelled` / `deadline_expired` through `settle_unstarted(claim, ...)` without needing the lock. | T04 (`renew_claim` returns state), T11 |
| F-C5 | The budget handed to `compose()` is the remaining time measured immediately before the call: the worker records a monotonic anchor and `remaining_at_running = deadline_at − started_at` (DB) at the start composite, and passes `remaining_at_running − (monotonic_now − anchor)`; ≤ 0 settles `deadline_expired` before any provider call. | T10, T11 |
| F-C7 | D3 is a **soft** cluster cap: the count and insert run under one session's lock, so concurrent admissions for different sessions can overshoot by at most the number of concurrent admitters. Accepted; a global counter row would serialize every admission in the cluster. The authority docstring and a test state the bound. | T04 |
| F-M2 | `claim_next` discovery skips sessions whose `session_operation_fences` row is live under any kind (`released_at IS NULL AND lease_expires_at > now`); the start composite still decides. | T04 |
| F-m1 | `request_json` bound is measured, not guessed: the maximum `model_dump_json()` length of each strict request DTO at its field caps with worst-case escaping (`'\x01' * 65536` → 393 230 chars of content, 393 334 with the DTO's UUID and key framing, measured); the constant `COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH` and the T03 CHECK use it. | T02, T03, T04 |
| F-m5 | `composer_async_worker_concurrency` is capped at `le=16` (the shared `run_sync_in_worker` pool is 16 + 16). | T01 |
| B4-residual | An `adopt` **defect** after `SessionOperationLease.adopt` has already released the minted context (`lifecycle.py` `_raise_adopt_failure_after_release`) can only settle through the reaper as 503 `composer_operation_worker_lost`, not `operation_failed` 500. Accepted: releasing the context is the correct lease behaviour and the path is a defect path. Start-composite defects and unreleased adopt defects still settle `operation_failed`. | T11 (test), T00 (W6/W9) |

## Rulings (John, 2026-09-25)

| Id | Ruling |
|---|---|
| Scope | Freeform only: `POST /{sid}/messages`, `POST /{sid}/recompose`. Guided routes unchanged; guided is being removed long-term. |
| R0 | Land 202 + poll now; a later streaming UI consumes the same durable operation row. |
| R2 | Composite terminal transaction (spec §4 as written). |
| R3 | Job running fence is BOUND to the COMPOSE `SessionOperationLease` (one composite authority transaction mints the SOL context and marks the job `running`; the worker `adopt`s it). |

## Defaults (plan decisions; each row names its alternative)

| Id | Decision | Alternative rejected |
|---|---|---|
| D1 (R1) | The worker owns a `ComposerRequestLease` per running job through the extracted lifecycle, so progress publishing, `inflight_requests` and the PG identity/ownership revocation cancel keep working. The SPA stops using `inflight_requests` as a freeform settlement signal. | Drop the CRL for freeform (loses mid-turn revocation cancel, empties `inflight_requests`). |
| D2 (R4) | Admission order: auth → ownership → strict DTO → existing-row lookup (replay returns 202 without charging) → per-user rate limit (new operations only) → active-operation check → capacity → insert. | Charge the rate limit on replays (double charge, 429 on an accepted op). |
| D3 (R5) | Capacity = count of nonterminal rows across the cluster at admission (`composer_async_max_queued_operations`, 429 + `Retry-After`) plus per-instance claim concurrency (`composer_async_worker_concurrency`). | Per-instance queue only (cannot bound the cluster). |
| D4 (R6) | The POST's `request_id` (RequestIdMiddleware) is persisted on the row and injected into every dict-detail terminal error body exactly as `handle_http_exception` does. The generic `operation_failed` envelope carries `diagnostic_id` = a uuid4 minted at settlement and written to the server slog with the exception class. | Poll GET's request id (not stable across polls). |
| D5 (R7) | Both kinds use send's `GuidedCustodyIntegrityError` → 500 `audit_integrity_error` + `failed_turn` arm, and send's `_join_shielded_task_after_cancellation` form for the cancelled-path audit persist (fixes recompose D6/D8). Recorded as a visible recompose change. | Keep the divergences. |
| D6 (R8) | `settle_auto_commit_intent` / `settle_pipeline_proposal_under_compose_lock` take `services: ComposerAppServices` and `user_id: str` instead of `request: Request` and `user: UserIdentity`; `proposals.py:318` is updated in the same task. | Persist a username on the row. |
| D7 (R9) | No archive refusal. `sessions.id` FK `ON DELETE CASCADE`. An archive that wins the SOL makes the worker's start composite raise `SessionOperationFenceLost` → terminal 404 `Session not found`. | Refuse archive while a job is nonterminal. |
| D8 (R10) | SPA custody in `sessionStorage` (same-tab reload resumes). Cross-tab and lost-custody reattach come from the server: admission refuses a NEW operation id while the session has a nonterminal job with 409 `{"error_type":"composer_operation_active","operation_id":"<existing>","kind":"<kind>"}`; the SPA then attaches to that id. The spec §2 admission list gains this check (Task 16 edits the spec). | Add a list/active endpoint. |
| D9 (R11) | Client `state_id` 404 stays a worker check (spec). | Hoist pre-202. |
| D10 (R12) | The convergence body's `timeout_seconds` reports the budget the turn actually received (remaining seconds at `running`), rounded to 0.1 s. | Keep the configured value. |
| D11 (R13) | `WebSettings.composer_sync_timeout_seconds` (= `min(composer_timeout_seconds, ceiling - headroom)`) bounds every synchronous consumer: the three guided routes, the guided planners, `explain_run_diagnostics`, proposal-decision settlement from `proposals.py`, and `/api/system/status` publishes it as `composer_sync_timeout_seconds` beside `composer_timeout_seconds`. ACA `validate-workload-parameters.jq:54` and the bicep description move with the ECS cap. | Cap guided only. |
| D12 (R14) | The terminal success CAS commits inside the composite before the SOL closes; a close/renewal failure after it is logged (`composer_operation.lease_close_failed_after_settle`) and never relabels the settled row. | Close first, then settle. |
| D13 | Terminal failure codes (closed set, CHECK + Literal): `http_error` (a preserved public HTTP error), `operation_failed` (generic, diagnostic id), `worker_lost`, `request_cancelled`, `deadline_expired`. | — |
| D14 | Queued-deadline expiry → 504 `{"error_type":"composer_operation_deadline_expired","detail":"The composer request waited too long to start. Please resubmit.","timeout_seconds":<configured>}`; worker loss → 503 `{"error_type":"composer_operation_worker_lost","detail":"The server stopped while composing this request. Reload to see what was saved, then resubmit."}`; cancel → 499 `{"error_type":"request_cancelled","detail":"The composer request was stopped."}`. All three bodies get `request_id` injected (D4). | — |
| D15 | Progress `request_id` stays the persisted user-message id (send: the row the worker inserts; recompose: the last user row), never the operation id. Progress publish failures are advisory: the worker logs and continues; they never decide the terminal. | — |
| D16 | Test harness: the worker is driven inline in the test's event loop (`await worker.run_until_idle()`); route tests use one `httpx.AsyncClient(ASGITransport)` per test through `tests/helpers/composer_operations.py`. | Real lifespan in every route test. |

## Settings (Task 1 and Task 11) — `src/elspeth/web/config.py`, `WebSettings`

```python
composer_async_max_queued_operations: int = Field(default=64, ge=1, le=10_000)
composer_async_worker_concurrency: int = Field(default=4, ge=1, le=16)   # shared run_sync_in_worker pool is 16+16 (F-m5)
composer_async_claim_lease_seconds: int = Field(default=30, ge=5, le=600)
composer_async_scan_interval_seconds: float = Field(default=1.0, gt=0, le=60)
composer_async_poll_after_ms: int = Field(default=1000, ge=100, le=60_000)

@property
def composer_sync_timeout_seconds(self) -> float:
    return min(self.composer_timeout_seconds,
               self.composer_transport_idle_ceiling_seconds - self.composer_transport_headroom_seconds)
```
`ComposerSettings` Protocol (`composer/protocol.py:1506`) gains `composer_sync_timeout_seconds: float` (property).

## Owned types — NEW `src/elspeth/web/sessions/composer_operations.py`

```python
ComposerOperationKind = Literal["compose_message", "compose_recompose"]
COMPOSER_OPERATION_KIND_VALUES: Final[frozenset[str]]
ComposerOperationStatus = Literal["queued", "running", "completed", "failed"]
ComposerOperationFailureCode = Literal["http_error", "operation_failed", "worker_lost", "request_cancelled", "deadline_expired"]
COMPOSER_OPERATION_FAILURE_CODE_VALUES: Final[frozenset[str]]
COMPOSER_OPERATION_REQUEST_SCHEMA: Final = "composer-operation-request.v1"
COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS: Final = "message_with_state.v1"
COMPOSER_OPERATION_RESULT_SCHEMA_ERROR: Final = "composer_operation_error.v1"

def composer_operation_request_hash(*, session_id: UUID, kind: ComposerOperationKind, request: SendMessageRequest | RecomposeRequest) -> str
    # same shape as guided_operation_request_hash (strict+forbid check, exclude operation_id,
    # materialize defaults/None), schema string above. Never imports the guided codec.
def composer_operation_result_hash(result_json: str) -> str   # stable_hash of json.loads of canonical text

@final @dataclass(frozen=True, slots=True)
class ComposerOperationClaim:            # the queued-claim fence
    session_id: UUID; operation_id: str; claim_token: str; attempt: int

@final @dataclass(frozen=True, slots=True)
class ComposerOperationRunning:          # the bound running fence (R3)
    claim: ComposerOperationClaim
    session_operation_context: SessionOperationContext

@final @dataclass(frozen=True, slots=True)
class ComposerOperationRecord:           # Tier-1 read of one row, validated in __post_init__
    session_id: UUID; operation_id: str; kind: ComposerOperationKind; status: ComposerOperationStatus
    request_hash: str; actor_user_id: str; request_id: str | None
    deadline_at: datetime; created_at: datetime; updated_at: datetime
    started_at: datetime | None; settled_at: datetime | None
    cancel_requested_at: datetime | None
    claim_owner_instance_id: str | None
    user_message_id: UUID | None
    failure_code: ComposerOperationFailureCode | None
    result_schema: str | None; result_json: str | None; result_sha256: str | None
    request_json: str | None

class ComposerOperationError(_StrictResponse):     # public terminal error envelope (also stored)
    http_status: int = Field(ge=400, le=599)
    failure_code: ComposerOperationFailureCode
    error_type: str | None
    body: dict[str, JsonValue]          # EXACT JSON body the synchronous route would have sent: {"detail": ...} (+request_id per D4)
    diagnostic_id: str | None           # set only for failure_code == "operation_failed"

class ComposerOperationConflictError(RuntimeError)      # same id, different kind/actor/hash → 409 (Tier-1 decorated)
class ComposerOperationActiveError(RuntimeError)        # D8 → 409 composer_operation_active; carries operation_id, kind
class ComposerOperationCapacityError(RuntimeError)      # D3 → 429; carries retry_after_seconds: int
class ComposerOperationFenceLost(RuntimeError)          # claim/running token mismatch; never carries the token
```
Wire DTOs live in `src/elspeth/web/sessions/schemas.py`:

```python
class SendMessageRequest(_GuidedOperationRequest):   # CHANGED (Task 10): strict, operation_id required
    content: str = Field(min_length=1, max_length=65536)
    state_id: UUID | None = None      # mode="before" canonical-UUID parser as RevertStateRequest
class RecomposeRequest(_GuidedOperationRequest):     # NEW (Task 10): only operation_id

class ComposerOperationAcceptedResponse(_StrictResponse):   # 202 body
    operation_id: str; kind: ComposerOperationKind; status: ComposerOperationStatus; poll_after_ms: int
class ComposerOperationStatusResponse(_StrictResponse):     # GET poll body and cancel body
    operation_id: str; kind: ComposerOperationKind; status: ComposerOperationStatus
    cancel_requested: bool; poll_after_ms: int; deadline_at: datetime
    deadline_remaining_ms: int = Field(ge=0)   # DB clock; 0 on terminal rows (F-B3)
    result: MessageWithStateResponse | None = None
    error: ComposerOperationError | None = None
```
(`ComposerOperationError` may live in `schemas.py` and be re-exported from `composer_operations.py`; the writer of Task 2 picks one home and states it.)

## Table — `composer_async_operations` in `src/elspeth/web/sessions/models.py` (Task 3)

Columns: `session_id String(128) NOT NULL`, `operation_id String(36) NOT NULL`, `kind String(32) NOT NULL`,
`status String(16) NOT NULL`, `request_hash String(64) NOT NULL`, `actor_user_id String(128) NOT NULL`,
`request_id String(128) NULL`, `request_json Text NULL`, `deadline_at DateTime(tz) NOT NULL`,
`claim_token String(256) NULL`, `claim_owner_instance_id String(128) NULL`, `claim_expires_at DateTime(tz) NULL`,
`attempt Integer NOT NULL` (claims taken, ≥ 0), `session_operation_id String(128) NULL`,
`session_operation_lease_token String(256) NULL`, `session_operation_epoch Integer NULL`,
`user_message_id String(128) NULL`, `cancel_requested_at DateTime(tz) NULL`, `failure_code String(32) NULL`,
`result_schema String(64) NULL`, `result_json Text NULL`, `result_sha256 String(64) NULL`,
`created_at`, `updated_at` NOT NULL, `started_at`, `settled_at` NULL.

Constraints: PK `pk_composer_async_operations (session_id, operation_id)`; FK session → `sessions.id` CASCADE;
FK `actor_user_id` → `identities.identity_id` RESTRICT; composite FK `(user_message_id, session_id)` →
`chat_messages(id, session_id)` RESTRICT; `kind IN ('compose_message','compose_recompose')`; status CHECK;
failure_code CHECK over D13; `request_hash`/`result_sha256` lower-hex-64 via `_lower_sha256_constraints`;
canonical-UUID length 36 on `operation_id`; `length(request_json) <= COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH` (393 334 at plan time, F-m1); no length bound on `result_json`
(server-authored validated DTO; stated in the table comment); `attempt >= 0`; time ordering CHECKs;
status bundles written as AND/OR arms (no `(a) = (b)` equalities — PG reflection lesson from `889913b1a`):
- queued: request_json NOT NULL, all three `session_operation_*` NULL, started/settled NULL, all result/failure NULL, and claim triple all-NULL or all-NOT-NULL;
- running: request_json, claim triple, SOL triple, started_at NOT NULL; result/failure/settled NULL;
- completed: request_json NULL, claim triple NULL, `result_schema = 'message_with_state.v1'`, result_json + result_sha256 + settled_at NOT NULL, failure_code NULL;
- failed: request_json NULL, claim triple NULL, `result_schema = 'composer_operation_error.v1'`, result_json + result_sha256 + settled_at + failure_code NOT NULL.
Indexes: `ix_composer_async_operations_claimable (status, claim_expires_at, created_at)`,
`ix_composer_async_operations_session_status (session_id, status)`, partial unique
`uq_composer_async_operations_one_running_per_session (session_id) WHERE status = 'running'`
(identical `sqlite_where`/`postgresql_where`). Terminal-immutable trigger
`trg_composer_async_operations_terminal_immutable` on both dialects (5 places, schema-persistence §3.2).
Mutation-authority `TablePolicy("composer_async_operations", "session", "ComposerAsyncOperationAuthority",
(("SessionOperationAuthority", {"update"}), ("SessionComposerOperationTerminalAuthority", {"update"})))`.
No epoch bump in Task 3; the bump is Task 15 (last).

## Authority — NEW `src/elspeth/web/coordination/composer_operation_authority.py` (Task 4)

Sync, engine-owning, one connection per method, DB clock (`_DATABASE_CLOCK_SQL`), same-session locked
transaction (`locked_session_transaction`) for every mutation. Called through `run_sync_in_worker`.

```python
class ComposerAsyncOperationAuthority:
    def __init__(self, engine: Engine, *, owner_instance_id: str, claim_lease_seconds: int) -> None
    def admit(self, *, session_id: UUID, operation_id: str, kind: ComposerOperationKind, request_hash: str,
              actor_user_id: str, request_id: str | None, request_json: str, deadline_seconds: float,
              max_nonterminal: int) -> tuple[ComposerOperationRecord, bool]
        # bool = created. Inside ONE locked tx: existing row by PK → compare kind/actor/hash (mismatch
        # → ComposerOperationConflictError; match → (row, False)); active nonterminal row for the session
        # → ComposerOperationActiveError; cluster nonterminal count ≥ max_nonterminal → ComposerOperationCapacityError
        # (a SOFT cap: F-C7); INSERT queued. Duplicate-PK IntegrityError from a racing insert re-reads and re-compares.
        # The per-user rate limit is charged OUTSIDE this tx by the coroutine
        # admit_composer_operation(authority, *, rate_limit: Callable[[], Awaitable[None]], ...):
        # get → charge only when absent → admit (lookup-before-charge, D2).
    def get(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None
    def claim_next(self, *, limit: int) -> tuple[ComposerOperationClaim, ...]
        # oldest queued per session, skipping sessions with a running row; unclaimed OR claim expired;
        # PG: FOR UPDATE SKIP LOCKED; SQLite: CAS UPDATE … WHERE claim_token IS NULL OR claim_expires_at <= now, rowcount==1
    def renew_claim(self, claim: ComposerOperationClaim) -> ComposerOperationRecord
        # queued claim only; FenceLost on mismatch; returns the row so a lock-waiting job sees
        # cancel_requested_at and deadline_at (F-C3)
    def release_claim(self, claim: ComposerOperationClaim) -> None        # back to unclaimed queued (SOL conflict path)
    def request_cancel(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None
        # sets cancel_requested_at on queued/running; an UNCLAIMED queued row settles request_cancelled in the same tx;
        # a terminal row is returned unchanged (completed wins); None when missing
    def settle_unstarted(self, claim_or_none: ComposerOperationClaim | None, *, session_id: UUID, operation_id: str,
                         failure: ComposerOperationError) -> ComposerOperationRecord
        # queued → failed (deadline, precondition failures decided before running, cancel of a claimed queued row)
    def list_expired_running(self, *, limit: int) -> tuple[ComposerOperationRecord, ...]
    def settle_lost(self, *, session_operation_context: SessionOperationContext, session_id: UUID,
                    operation_id: str, failure: ComposerOperationError) -> ComposerOperationRecord
        # reaper, AFTER it acquired SOL COMPOSE itself: running → failed(worker_lost | request_cancelled if marker)
        # (takes cancelled_failure: ComposerOperationCancelledFailure — see Adopted deviations)
    def list_expired_queued(self, *, limit: int) -> tuple[ComposerOperationRecord, ...]
    def settle_lost_inactive_session(self, *, session_id: UUID, operation_id: str, failure: ComposerOperationError,
                                     cancelled_failure: ComposerOperationCancelledFailure) -> ComposerOperationRecord
        # unfenced; guarded by sessions.archived_at IS NOT NULL AND status='running'
    def settle_own_lapsed(self, *, session_id: UUID, operation_id: str, owner_instance_id: str,
                          failure: ComposerOperationError,
                          cancelled_failure: ComposerOperationCancelledFailure) -> ComposerOperationRecord
        # F-M3: this instance's own running job whose bound fence lapsed; reads the fence, never takes it over
    def count_nonterminal(self) -> int
```

## Composite start (R3) — `src/elspeth/web/coordination/repository.py` (Task 5)

```python
# _SessionOperationAuthorityRepository and the SessionOperationAuthority Protocol (sessions/protocol.py:3875):
def start_composer_async_operation(self, claim: ComposerOperationClaim, *, owner_instance_id: str,
                                   lease_seconds: int) -> SessionOperationContext
    # ONE _locked_transaction: re-verify the claim token + queued + not expired + cancel not requested
    # (cancel → raise ComposerOperationCancelledBeforeStart, a new class in composer_operations.py),
    # advance the COMPOSE fence exactly as acquire() does (refactor acquire's body into
    # _advance_exclusive_fence_on_connection(conn, ...) used by both), then UPDATE the job to running with
    # session_operation_{id,lease_token,epoch}, started_at, claim kept. SessionOperationConflictError and
    # SessionOperationFenceLost propagate unchanged. Returns the minted context for SessionOperationLease.adopt.
```
The worker then: `lease = await SessionOperationLease.adopt(service.session_operation_authority, context, lease_seconds=service.session_operation_lease_seconds)`.
Job-lease liveness == SOL liveness: the running row has no separate lease expiry; the reaper settles a
`running` row only after acquiring SOL COMPOSE on that session (success proves the owner's fence lapsed).

## Composite terminal (R2) — `src/elspeth/web/sessions/service.py` (Task 6)

```python
async def complete_composer_async_operation(
    self, running: ComposerOperationRunning, *,
    assistant: ComposerOperationAssistantWrite | None,   # None when a state commit already wrote the assistant row
    audit_cohort: tuple[AuditMessageDraft, ...], audit_composition_state_id: UUID | None,
    build_response: Callable[[ChatMessageRecord, tuple[CompositionProposalRecord, ...]], MessageWithStateResponse],
    assistant_record: ChatMessageRecord | None,          # the already-written assistant row when assistant is None
) -> ComposerOperationRecord
    # ONE transaction under _session_composer_mutation_transaction(expected_kind=COMPOSE): insert assistant
    # (if given) + the audit cohort (same rows add_messages_atomic writes), read pending proposals via a new
    # _list_composition_proposals_on_connection (same Tier-1 checks as list_composition_proposals), call
    # build_response, validate strict, canonical-dump, hash, then the terminal CAS on the job keyed by
    # (session_id, operation_id, status='running', claim_token, session_operation_{id,lease_token,epoch},
    # cancel_requested_at IS NULL). rowcount != 1 → re-read: cancel committed first → raise
    # ComposerOperationCancelledDuringTurn (worker then settles request_cancelled); otherwise ComposerOperationFenceLost.
async def fail_composer_async_operation(self, running: ComposerOperationRunning, *,
                                        failure: ComposerOperationError) -> ComposerOperationRecord
    # single-tx terminal CAS under the SOL fence; used after the cancelled/failed-path audit persist has joined
```
`ComposerOperationAssistantWrite` = the args `add_message` takes today for the assistant row (content, tool_calls,
composition_state_id, writer_principal="compose_loop", raw content fields) — Task 6 defines it as a frozen dataclass.
Mutation authority name for these two writers: `SessionComposerOperationTerminalAuthority`.

## Extracted request lifecycle (Task 7) — `src/elspeth/web/sessions/routes/_helpers.py`

```python
@contextlib.asynccontextmanager
async def _composer_request_lifecycle(
    registry: ComposerProgressRegistry | DatabaseComposerProgressRegistry, *,
    session_id: str, user_id: str, surface: ComposerTelemetrySurface, owner_task: asyncio.Task[object],
    timer: _ComposerHeartbeatTimer | None = None,     # None → read module _COMPOSER_HEARTBEAT_TIMER at entry
) -> AsyncIterator[ComposerRequestLease]
    # the whole of _track_compose_inflight after ownership: start_request, metrics begin, heartbeat renew()
    # with the identical failure policy, the CancelledError/uncancel/503 conversion, finish_request, metrics finish.
# _track_compose_inflight keeps its signature and mounts; its body becomes ownership check +
# `async with _composer_request_lifecycle(..., surface=<path rule>, owner_task=asyncio.current_task()) as lease:
#      request.state.composer_request_lease = lease; yield`
async def _composer_progress_sink_for_lease(registry, *, lease: ComposerRequestLease, session_id: str,
                                            request_id: str | None, user_id: str) -> ComposerProgressSink
    # _composer_progress_sink(registry, request, ...) delegates to this; guided callers unchanged
def composer_session_lock_registry(app: FastAPI | Starlette) -> _SessionComposeLockRegistry
    # app-keyed accessor; the lifespan creates the registry eagerly; _get_session_compose_lock_registry(request)
    # delegates to it so routes and worker share ONE object
```

## Budget (Task 8) — `src/elspeth/web/composer/service.py` + protocol

`ComposerService.compose(..., budget_seconds: float | None = None)`; `None` keeps today's
`self._timeout_seconds`. `_plan_and_stage_empty_pipeline(..., budget_seconds)` threads it to
`PlannerModelConfig.timeout_seconds`. Guided planners and `explain_run_diagnostics` read
`settings.composer_sync_timeout_seconds` (Task 1 already made that value exist).

## Error projection (Task 9) — NEW `src/elspeth/web/sessions/composer_operation_errors.py`

```python
def project_composer_operation_error(exc: BaseException, *, request_id: str | None,
                                     session_engine_dialect: str) -> ComposerOperationError
    # HTTPException → http_error with {"detail": detail} (+request_id injected into a dict detail, as
    # app.handle_http_exception does); each app-handler class in raise-inventory-send.md §3 → its exact status+body;
    # PermissionError-family progress errors, TypeError/ValueError/RuntimeError/pydantic ValidationError/unknown →
    # operation_failed 500 {"detail":{"error_type":"operation_failed","detail":"The composer request failed. Reference <diagnostic_id>.","diagnostic_id":...,"request_id":...}}
    # AsyncWorkerAdmissionTimeoutError → 503 database_unavailable (never confused with the compose wall clock)
def deadline_expired_error(*, request_id: str | None, timeout_seconds: float) -> ComposerOperationError
def worker_lost_error(*, request_id: str | None) -> ComposerOperationError
def request_cancelled_error(*, request_id: str | None) -> ComposerOperationError
```
Parity instrument: for every row, a tiny FastAPI app with the REAL app exception handlers raises the exception
and the test asserts `(status, response.json())` == `(err.http_status, err.body)` modulo the request id value.

## App services bundle and turn function (Task 10)

```python
# NEW src/elspeth/web/sessions/composer_app_services.py
@final @dataclass(frozen=True, slots=True)
class ComposerAppServices:
    session_service: SessionServiceImpl; composer_service: ComposerService; settings: WebSettings
    catalog_service: ...; operator_profile_registry: ...; scoped_secret_resolver: ...; session_engine: Engine
    plugin_snapshot_for_user_id: Callable[[str], PluginSnapshot]
    progress_registry: ComposerProgressRegistry | DatabaseComposerProgressRegistry
    compose_locks: _SessionComposeLockRegistry
def composer_app_services(app: Starlette) -> ComposerAppServices   # the ONLY place app.state is read for the turn

# NEW src/elspeth/web/sessions/composer_turn.py
@final @dataclass(frozen=True, slots=True)
class ComposerTurnInput:
    session_id: UUID; operation_id: str; kind: ComposerOperationKind; actor_user_id: str
    request: SendMessageRequest | RecomposeRequest; request_id: str | None; budget_seconds: float
async def run_composer_turn(services: ComposerAppServices, turn: ComposerTurnInput, *,
                            lease: SessionOperationLease, running: ComposerOperationRunning) -> ComposerOperationRecord
    # the moved bodies of send_message / recompose from the lock onward, one function with a per-kind preamble
    # (send: state_id check, user-row insert recorded on the job's user_message_id; recompose: transcript 400/409);
    # typed ladder unchanged (still raises HTTPException bodies); success via complete_composer_async_operation;
    # the cancelled arm persists sidecars with _join_shielded_task_after_cancellation BEFORE fail_composer_async_operation.
```

## Worker (Task 11) — NEW `src/elspeth/web/sessions/composer_async_worker.py`

```python
class ComposerAsyncWorker:
    def __init__(self, *, services: ComposerAppServices, authority: ComposerAsyncOperationAuthority,
                 concurrency: int, scan_interval_seconds: float, claim_lease_seconds: int,
                 owner_instance_id: str, process_recovery: ProcessRecovery, instance_draining: asyncio.Event) -> None
    def start(self) -> None                 # scan loop task + reaper; done-callback escalation like orphan_task
    async def stop(self) -> None            # request cancellation of owned jobs, join them, then stop loops
    async def run_until_idle(self) -> None  # TEST SEAM: reap once, then claim+run every claimable job to terminal in this loop
    def signal_local_cancel(self, *, session_id: UUID, operation_id: str) -> bool
    async def reap_once(self) -> int        # queued deadline expiry + expired claims + running rows whose SOL lapsed
```
Per job: renew the queued claim until `start_composer_async_operation`; on `SessionOperationConflictError`
`release_claim` and move on; adopt the SOL; open `_composer_request_lifecycle(surface="freeform")` for the CRL;
spawn a watcher like `ExecutionServiceImpl._signal_shutdown_on_operation_loss` (`execution/service.py:2663-2700`:
`wait_for(lease.wait_until_lost(), 0.25)` + a cancel-marker read with backoff) that cancels the turn task with a
`_ComposerOperationCancel(kind="lease_lost" | "cancel_requested")` identity marker; `current_task().cancelling()` is
read in the turn task itself. Lifespan wiring in `app.py`: create after `orphan_task`, publish
`app.state.composer_async_worker`, stop after `begin_drain()` and before `execution_service.shutdown()`.

## HTTP (Task 12 routes, Task 13 cutover)

- `GET /api/sessions/{session_id}/operations/{operation_id}` → 200 `ComposerOperationStatusResponse`,
  `Cache-Control: no-store`; ownership each call (`_verify_session_ownership`); missing → 404 `Session not found`
  shape for foreign sessions, `{"detail":"Operation not found"}` for a missing row in an owned session;
  progress-authority `PermissionError` family → 401 `Invalid token` / 404 as `routes/composer/state.py:546-549`.
  Read validates `result_sha256` and the strict DTO again; mismatch → `AuditIntegrityError` (500 via app handler).
- `POST /api/sessions/{session_id}/operations/{operation_id}/cancel` → 200 `ComposerOperationStatusResponse`
  when terminal (completed wins, not relabelled), 202 same body with `cancel_requested=true` when owned,
  404 when missing. Signals `app.state.composer_async_worker.signal_local_cancel(...)`.
- Router module `src/elspeth/web/sessions/routes/composer/operations.py`, registered in
  `routes/composer/__init__.py`; added to the IDOR module tuple (`test_routes.py:3863-3866`) and ownership inventory.
- `POST /messages` and `POST /recompose`: `status_code=202`, `response_model=ComposerOperationAcceptedResponse`,
  NO `_track_compose_inflight`, body `SendMessageRequest` / `RecomposeRequest`; admission per D2 via
  `ComposerAsyncOperationAuthority.admit`; replay of the same id/body → 202 with the current status.
  409 bodies: conflict `{"detail":{"error_type":"composer_operation_conflict","detail":"This operation id was already used for a different request."}}`;
  active `{"detail":{"error_type":"composer_operation_active","detail":"...","operation_id":...,"kind":...}}`;
  429 capacity `{"detail":{"error_type":"composer_queue_full","detail":"...","retry_after":n}}` + `Retry-After`.

## Test helpers — NEW `tests/helpers/composer_operations.py` (Task 12)

```python
async def submit_and_settle(client: httpx.AsyncClient, app: FastAPI, *, path: str, body: dict[str, object],
                            max_rounds: int = 50) -> SettledComposerOperation
    # POST → assert 202 → await app.state.composer_async_worker.run_until_idle() → GET until terminal
@final @dataclass(frozen=True)
class SettledComposerOperation:
    accepted: httpx.Response; final: dict[str, object]
    def result(self) -> dict[str, object]           # asserts completed; returns the MessageWithStateResponse JSON
    def error(self) -> tuple[int, dict[str, object]]  # asserts failed; returns (http_status, body)
def settle_sync(test_client: SyncASGITestClient, app: FastAPI, *, path: str, body: dict[str, object]) -> SettledComposerOperation
    # runs the whole submit/drive/poll inside ONE anyio.run on the app — for the 131 legacy sync call sites
def install_composer_async_worker(app: FastAPI, **overrides: object) -> ComposerAsyncWorker   # builds on app.state without lifespan
```

## Frontend (Task 14) — `src/elspeth/web/frontend/src`

- `api/client.ts`: extract `apiErrorFromBody(status: number, body: unknown): ApiError` (no 401 logout) used by
  `parseResponse`; `submitComposerOperation(sessionId, kind, body, signal)` → `ComposerOperationAccepted`;
  `fetchComposerOperation(sessionId, operationId, signal)` → `ComposerOperationStatus | ComposerOperationNotFound`
  (404 → `{kind: "session_missing"} | {kind: "operation_missing"}` from the body, M4);
  `cancelComposerOperation(sessionId, operationId, signal)` → `ComposerOperationStatus | ComposerOperationNotFound`. Each call bounded by
  `AbortSignal.timeout(COMPOSER_OPERATION_HTTP_TIMEOUT_MS = 15_000)` combined with the caller's signal.
- `api/composerOperationDecoder.ts`: exact-record decoders (pattern `guidedDecoder.ts:2297-2340`) for accepted/status;
  `result` decoded with `decodeCompositionState` for `state`.
- `stores/composerOperationCustody.ts`: sessionStorage key `elspeth_composer_operations_v1`, envelope schema
  `composer-operations.v1`, one descriptor per session `{sessionId, operationId, kind, body, createdAt}`, bound
  256 KiB total, 24 h age; no orphan sweep on `selectSession`; memory fallback when storage refuses.
  Deliberately stores the body (spec §2) — reverses `guidedOperationRetry`'s fingerprint-only invariant for this kind only.
- `stores/sessionStore.ts`: `sendMessage`/`retryMessage` → custody acquire (same body for retry) → submit → on
  network ambiguity poll-by-id then resubmit same id/body only on null → poll loop (`poll_after_ms`, backoff
  1s→2s→4s→8s cap on transient errors, never declares failure on a poll error) → existing success reducer with
  `result` / existing error reducer with `apiErrorFromBody(error.http_status, error.body)`; pollers stop only on
  terminal; `resumeComposerOperation(sessionId)` on `selectSession`/boot when custody holds an id;
  409 `composer_operation_active` → attach to the returned id.
- `hooks/useComposer.ts`: Stop → `cancelActiveComposerOperation()` (cancel endpoint, then keep polling to terminal);
  freeform no longer calls `runComposeWithTimeout` (guided keeps it, with its ceiling read from
  `composer_sync_timeout_seconds`, M6). The freeform client deadline lives in the `sessionStore` poll loop: a
  monotonic `performance.now()` deadline armed from `deadline_remaining_ms + COMPOSE_CLIENT_GRACE_MS`, tighten-only
  across bodies (F-B3), then explicit cancel and reconcile.

## Task list (order = commit order; each task ends green on its own tests)

| # | Task | Depends on |
|---|---|---|
| 0 | Baseline measurements (M1–M8), raise-path appendix | — |
| 1 | Settings: async knobs + `composer_sync_timeout_seconds`; cap every sync consumer (behaviour-neutral for valid configs) | 0 |
| 2 | Owned types + codecs + `ComposerOperationError` + new DTO classes (not yet mounted) | 0 |
| 3 | Table, CHECKs, indexes, trigger, TablePolicy, digest inventory, PG reflection (no epoch bump) | 2 |
| 4 | `ComposerAsyncOperationAuthority` (admit/get/claim/renew/release/cancel/settle_unstarted/list_expired/settle_lost/count) + SQLite & PG race tests | 3 |
| 5 | Composite start `start_composer_async_operation` + acquire refactor + adopt test | 4 |
| 6 | Composite terminal `complete_/fail_composer_async_operation` + `_list_composition_proposals_on_connection` | 5 |
| 7 | Extract `_composer_request_lifecycle`, lease-param progress sink, app-keyed lock registry; guided byte-unchanged | 0 |
| 8 | `compose(budget_seconds=)` + planner threading + D10 convergence body | 1 |
| 9 | Error projection adapter + parity tests | 2 |
| 10 | `ComposerAppServices`, settlement signature change (D6), `run_composer_turn` (moved bodies) + retargeted structural pins | 6, 7, 8, 9 |
| 11 | `ComposerAsyncWorker` + reaper + lifespan wiring + cancellation/lease-loss watcher + gate-4 tests | 10 |
| 12 | Operations router (GET/cancel) + test helpers | 11 |
| 13 | Route cutover (202, DTOs, admission) + migrate 131+ test sites + IDOR + other callers (ACA probe, eval battery) + gate-2 test | 12 |
| 14 | Frontend cutover + tests + e2e mock | 13 |
| 15 | Config decoupling + deploy mirrors (ECS, ACA, compose/nginx, systemd, docs) | 13 |
| 16 | Crash-window gate-3 PG kill tests + cross-instance poll/cancel | 13 |
| 17 | Epoch 68 bump + doc/website/CHANGELOG sweep + spec §2 D8 line | all |
| 18 | Integration: gates, lints diff, testcontainer, local short-idle proxy browser acceptance, deploy note | 17 |
