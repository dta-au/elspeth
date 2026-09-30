# Interface contract — composer async operations (re-based 2026-09-28)

This contract is what every task file builds against. It fixes the names, signatures, columns, wire shapes and module
homes. It folds in:

- the 09-25 decisions;
- the 09-25 multi-lens review;
- the 09-28 storage panel (B′);
- owner rulings 1–7 (`panel-2026-09-28/RULINGS.md`).

There are no override tables. An executor who finds a clause impossible against the tree records a `CONTRACT DEVIATION:`
block in their task file, with the measured reason and the smallest change. They do not rename silently.

- **Tree:** `release/0.8.1`, pinned at `1effedab2` (surveyed; HEAD `6cb338f2f` differs only in `gateway/`).
- **Spec:** `docs/specs/2026-09-16-composer-async-operations-design.md` (second amendment 2026-09-28).
- **Evidence:** `findings-2026-09-28/` (`routes.md`, `persistence.md`, `composer.md`, `frontend.md`, `platform.md`,
  `plan-impact.md`, `critique.md`). The 09-25 plan, findings and review are kept in `history/2026-09-25/` for
  provenance only; none of their anchors is current.

## Glossary (binding)

| Term | Meaning |
|---|---|
| **operation id** / `operation_id` | The client-minted canonical UUID (36 chars) for one user action. It is the job's PK column and the `message_ingress_receipts` key. It is also the wire field on the request, the 202 ack, the poll URL, and `ChatMessageResponse.operation_id`. |
| **session operation id** / `session_operation_id` | The server-minted id inside a `SessionOperationFence` (`repository.py` `_new_operation_id()`). It appears on the job only as the bound-fence column `session_operation_id`. Never abbreviate it to "operation id". |
| **job** | A `composer_async_operations` row. |
| **bound base** | The `state_id` the operator saw when they authored the request (`None` = "no state"). It is stored as `base_state_id`. |
| **SOL** | `SessionOperationLease`, COMPOSE kind unless stated. |
| **CRL** | The composer request lease (progress registry row) that the worker opens per running job. |

## Owner rulings

| # | Ruling |
|---|---|
| Scope | Freeform is the only composer. The cutover set is `POST /{sid}/messages` (`compose_message`) and `POST /{sid}/recompose` (`compose_recompose`). The first-run tutorial's Build uses `/messages`, so the cutover covers it with no tutorial branch (composer invariant 2). |
| R0 | Ship 202 + poll now. A later streaming UI consumes the same durable job row. |
| R2 | The terminal is committed in one composite transaction with the final publication. |
| R3 | The job's running state is bound to the COMPOSE SOL. One composite transaction mints the SOL context and marks the job `running`, and the worker `adopt`s it. Job liveness == SOL liveness. |
| 1 | One id per send. The `operation_id` IS the ingress key. Ingress stays the immutable acceptance record, written by the worker in the user-row transaction. The ingress→job composite FK makes them agree. The ingress 409 arms and the SPA transcript-matching recovery are deleted. |
| 2 | Positive fence predicate: a write under a fence bound to a job requires that job `running` with `cancel_requested_at IS NULL`, unless the write is `audit_only`. |
| 3 | No compose events table. Forensic columns stay on the terminal row. |
| 4 | A delete guard applies while the session exists. Settled rows are retained with their session. |
| 5 | A base that moved between admission and start is refused before any side effect. |
| 6 | The wire name is `operation_id`, everywhere. |
| 7 | An absent `state_id` means "no state", read literally. |
| Panel B′ | A separate `composer_async_operations` table with its own single authority, and a request normaliser shared with receipts, pinned by a golden vector. If the vector moves, ship a separate codec (option C). |

## Decisions (plan defaults; each names what it rejects)

| Id | Decision | Rejected |
|---|---|---|
| E1 | **CRL kept.** The worker opens a CRL per running job through the extracted lifecycle (N08). Progress publishing, `inflight_requests` and PG identity/ownership revocation keep working. The SPA never uses `inflight_requests` as settlement. | Drop the CRL (loses the mid-turn revocation cancel). |
| E2 | **Admission order:** auth → body decode → ownership → strict DTO → PK lookup (same id and binding → replay 202, no charge; mismatch → 409) → per-user rate limit (new ids only) → capacity (soft) → insert. The D8 partial unique index enforces one nonterminal job per session (409 `composer_operation_active`). No ingress lookup happens pre-202. | Charge on replays; a racy read-then-insert active check. |
| E3 | **Capacity** is a soft cluster cap. The count and the insert run under one session lock, so concurrent admissions for different sessions can overshoot by at most the number of concurrent admitters. Worker concurrency is ≤ 16 (the shared `run_sync_in_worker` pool is 16 + 16). | A global counter row serialising every admission. |
| E4 | **Error identity.** The POST's `request_id` is persisted on the job. A dict-detail terminal body gets it inside `detail`, exactly as `handle_http_exception` does. A **string**-detail body (the ownership 404, the `State not found` 404, recompose's 400 and "last row not user" 409, the settlement 504) gets **no** request id, because `handle_http_exception` injects none into a string today; the stored body is byte-identical to today's. A flat handler body (`stale_compose_state`, the app handlers) gets it top-level, as those handlers do. The generic `operation_failed` body carries a `diagnostic_id` (uuid4), which is logged server-side with the exception class. | The poll GET's request id. |
| E5 | **Ruling 5 check site: the start composite (N06).** In the same locked transaction as the fence advance, and before the `running` CAS, the precondition gate checks, in order: ownership (session `user_id` against the job's `actor_user_id`, and `auth_provider_type` against the instance's `settings.auth_provider`, passed in) → a foreign or unknown base (`base_state_id` not NULL and not a state of this session → the byte-identical 404 `"State not found"`, ruling 5's "foreign stays 404") → the moved base (current head vs `base_state_id`, id-only; `None` requires no head) → for recompose, the transcript (the three existing refusals with their exact bodies). A refusal rolls the composite back, and the worker settles it through `settle_unstarted` with `settled_by='settle_unstarted'`. No user row, no provider call, no started quad. | Checking in the preamble after `running`, or inside the send `_sync` transaction (which misses recompose). |
| E6 | **Moved-base body.** `http_error` 409, flat: `{"error_type":"stale_compose_state","detail":"The session changed before this request started. Review the current pipeline and send again.","request_id":…}`. The error type matches today's handler; the detail is true for a pre-turn refusal. | Reusing "…while the compose turn was running" (false before the turn). |
| E7 | **In-turn seeding stays at the head.** Under ruling 5 the head equals the bound base at start, so `compose_base_state_id` is the head, as today. `elspeth-e08063c3a5`'s lag case cannot recur: a lagging client is refused at start, never mid-turn. | Seeding from the client id. |
| E8 | **Ingress write path.** The worker calls `add_message_with_transcript` (the existing single ingress writer path, so the writer pins hold). From N11a it takes `running: ComposerOperationRunning | None = None`; `None` is the still-synchronous route's path, which keeps today's behaviour exactly until N14, and N14 makes `running` required and deletes the `None` path. When `running` is given, inside its `_sync`, in this order: user row → `bind_composer_operation_user_message_on_connection(conn, running, user_message_id=…)` (a connection-taking helper in the authority module) → `_insert_message_ingress_receipt` keyed by the operation id, with `requested_state_id` equal to the job's `base_state_id` (the receipt and the hash bind the same base). A non-fresh outcome is a Tier-1 `AuditIntegrityError`. `lookup_message_ingress`, `MessageIngressAccepted/Conflict`, the route 409 arms and `_ingress_receipt_conflict` are deleted at cutover (N14). | A new ingress writer in the compose composite (moves 5 pins). |
| E9 | **Recompose forensics.** The start composite sets `user_message_id = expected_user_message_id` for recompose (the running arm's NULL→value). `base_state_id` is kept on every row at terminal, so the base survives `request_json` being cleared. | NULL `user_message_id` on recompose (which loses its target). |
| E10 | **Positive predicate sites.** (a) **Family S:** `_require_session_operation_context_on_connection` gains `audit_only: bool = False`, threaded through `_session_composer_mutation_transaction` → `_require_session_write_authority_on_connection` → `_insert_chat_message`, and `_SessionComposerMutationState._require_exact`. (b) **Family R:** `_SessionOperationAuthorityRepository.mutate` applies the same predicate for COMPOSE contexts (the blob tools). `_exact_active_predicates` is **not** changed, because `renew`, `release`, `adopt`'s CAS and `archive_delete` share it and must keep working after the terminal. The lookup keys on the full fence triple via the `(session_id, session_operation_epoch)` unique index. Refused writes raise `ComposerOperationCancelledDuringTurn` (marker set) or `ComposerOperationFenceLost` (job terminal). | The predicate in `_exact_active_predicates`. |
| E11 | **`audit_only` classification.** Audit-only writers: `_persist_llm_calls`, `_persist_turn_audit_cohort`, the planner audit writer (`planning_application.py:371`), `finish_provider_attempt`, `settle_provider_attempt`, `cancel_undispatched_provider_attempt`, and the cohort's own `record_token_usage_on_connection` + `mark_session_updated`. `begin_provider_attempt` is **fenced** (it is an admission: a cancel-marked turn must not dispatch). `update_session_title` is fenced. N07 measures this list before landing the predicate. | Treating ledger admission as audit. |
| E12 | **Auto-title.** The worker joins auto-title **before** the terminal composite, with the same 2 s bound as today. On timeout it cancels the task and joins that cancellation (its cancel arm settles the charge `audit_only`) before the terminal. So no non-audit write follows the terminal, and a completed response is never replaced. | Joining after the terminal (refused by ruling 2). |
| E13 | **SOL close after the terminal.** A close or renewal failure, including an `ExceptionGroup` from `__aexit__`, never relabels a committed terminal. It is logged as `composer_operation.lease_close_failed_after_settle`. Before the terminal, the worker unwraps a body-plus-close group: any lease-loss member (renewal error, `SessionOperationFenceLost`, the settlement-child cancel from `_freeform_child_result`) → `worker_lost`; otherwise it projects the body exception. | Letting the group escape as a bare 500. |
| E14 | **Budgets.** The job deadline is set at admission from `composer_timeout_seconds`. At `running` the worker records `ComposerBudgetAnchor`, and `compose(budget_seconds=…)` gets the time remaining immediately before the call. The planner gets the time remaining at the branch. Auto-commit settlement (`pipeline_settlement` `commit_timeout_seconds`) gets the remaining job budget. The synchronous Accept route gets `composer_sync_timeout_seconds`. D10: the convergence body's `timeout_seconds` reports the budget the turn received. | A fresh full budget per stage. |
| E15 | **Sync cap.** `WebSettings.composer_sync_timeout_seconds = min(composer_timeout_seconds, ceiling − headroom)` has these consumers: `explain_run_diagnostics`, proposal Accept settlement, and the tutorial run wait (which already uses `ceiling − headroom`). `ComposerSettings` gains the property. **No** new status key: no client reads one after cutover. | Publishing an unread key. |
| E16 | **SPA timers.** `runComposeWithTimeout`, `applyServerComposerTimeout` and the `composeTimeoutReady` latch lose their last caller at cutover and are deleted. The freeform deadline is the poll loop's monotonic `performance.now()` deadline from `deadline_remaining_ms + COMPOSE_CLIENT_GRACE_MS`, which only tightens across bodies. `/api/system/status` keeps `composer_timeout_seconds` (an existing public field, now informational). | Keeping dead timer plumbing. |
| E17 | **Drain.** `stop()` stops claiming, releases any claim that returns after the stop, cancels owned jobs with the `shutdown` marker, and joins each for up to `composer_async_drain_seconds` (default 10.0). A job still unsettled is left for a peer's reaper (`worker_lost`). Every deploy/drain therefore settles in-flight turns as 503 `composer_operation_worker_lost`; this is a documented behaviour (CHANGELOG + runbook). | An unbounded join. |
| E18 | **Run and diagnostics while a job exists.** No server change. A queued job holds no SOL, so Run/diagnostics proceed, and a start that then conflicts requeues (claim discovery skips live-fenced sessions). A running job makes them answer today's 409 `Session operation is already active`. The tutorial's Build/Run gating is the SPA reading the operation state (N15). | A new Run admission rule. |
| E19 | **FK actions.** job→`sessions` CASCADE; job→`identities` (actor) RESTRICT; job→`chat_messages(user_message_id, session_id)` **NO ACTION** (checked at statement end, so cascade order cannot fail a D7 delete on PG); job→`composition_states(base_state_id, session_id)` NO ACTION; ingress→job `(session_id, operation_id, user_message_id)` → job `(session_id, operation_id, user_message_id)` **CASCADE**. N00 measures a PG cascade with a RESTRICT sibling; N17 proves the final set. | RESTRICT on the job's own references. |
| E20 | **Progress.** The progress `request_id` is the persisted user-message id (send: the row the worker inserts; recompose: `expected_user_message_id`), never the operation id. Progress failures are advisory: they are logged and never decide the terminal. | — |
| E21 | **Failure codes** (a closed CHECK + Literal): `http_error`, `operation_failed`, `worker_lost`, `request_cancelled`, `deadline_expired`. **`settled_by`** (a closed CHECK + Literal): `owner_terminal`, `settle_unstarted`, `request_cancel`, `settle_lost`, `settle_own_lapsed`, `settle_lost_inactive_session`. | — |
| E22 | **Fixed bodies** (all get the request id per E4): deadline 504 `{"error_type":"composer_operation_deadline_expired","detail":"The composer request waited too long to start. Please resubmit.","timeout_seconds":<configured>}`; worker lost 503 `{"error_type":"composer_operation_worker_lost","detail":"The server stopped while composing this request. Reload to see what was saved, then resubmit."}`; cancel 499 `{"error_type":"request_cancelled","detail":"The composer request was stopped."}`. An **unmarked** `CancelledError` out of a detached turn is `worker_lost`. Only the cancel endpoint's marker is a user Stop. | — |
| E23 | **Test harness.** Tests drive the worker inline (`await worker.run_until_idle()`). Route tests use one `httpx.AsyncClient(ASGITransport)` per test via `tests/helpers/composer_operations.py`; legacy sync sites use `settle_sync` (one `anyio.run`). The worker resolves `app.state.composer_service` **per job** (tests swap it after build). The event-held composer fakes move to `tests/fixtures/composer_fakes.py`. | Running the real lifespan in route tests. |
| E24 | **Transitional DTOs.** From N03 until N14 the still-synchronous routes mount `LegacySendMessageRequest` / `LegacyRecomposeRequest` (today's bodies verbatim). N14 deletes both. There is **no interim merge between N03 and N15.** | Moving the DTO swap into N14 (worker tests need the strict DTOs earlier). |
| E25 | **Out-of-scope pre-existing breakage** (not touched): `scripts/composer_acceptance/runner.py:77` and `evals/lib/common.sh:348`. **Exception:** they call the cutover route, and N14 migrates every caller of the two routes to the new wire. Beyond that migration, nothing else in them changes. | — |

## Settings — `src/elspeth/web/config.py` `WebSettings` (N01)

```python
composer_async_max_queued_operations: int = Field(default=64, ge=1, le=10_000)
composer_async_worker_concurrency: int = Field(default=4, ge=1, le=16)      # shares the 16+16 run_sync_in_worker pool
composer_async_claim_lease_seconds: int = Field(default=30, ge=5, le=600)
composer_async_scan_interval_seconds: float = Field(default=1.0, gt=0, le=60)
composer_async_poll_after_ms: int = Field(default=1000, ge=100, le=60_000)
composer_async_drain_seconds: float = Field(default=10.0, gt=0, le=120)

@property
def composer_sync_timeout_seconds(self) -> float:
    return min(self.composer_timeout_seconds,
               self.composer_transport_idle_ceiling_seconds - self.composer_transport_headroom_seconds)
```
`ComposerSettings` (`composer/protocol.py`) gains `composer_sync_timeout_seconds: float`. The coupling validator's
compose-budget half is removed in N16, not N01, so N01 is behaviour-neutral for every config that is valid today.

## Shared normaliser — `src/elspeth/web/sessions/operation_codec.py` (N02, go/no-go)

```python
SessionOperationRequestSchema = Literal["session-operation-receipt-request.v1", "composer-operation-request.v1"]
def session_operation_request_hash(*, schema: SessionOperationRequestSchema, session_id: UUID, kind: str,
                                   request: BaseModel) -> str
    # body moved verbatim from operation_receipts.operation_receipt_request_hash (strict+forbid check, operation_id
    # field required and excluded, exclude_unset/defaults/none=False, stable_hash({"schema","session_id","kind","request"}))
def strict_response_hash(response: BaseModel) -> str
    # body moved verbatim from operation_receipt_response_hash
```
`operation_receipt_request_hash` / `operation_receipt_response_hash` keep their names, signatures and
`OperationReceiptKind`, and delegate. The golden vectors (fixed session UUID, both receipt kinds, a strict DTO with
defaulted and `None` fields, and one response) are computed from the **unmodified** functions and committed
**before** the move. If any vector changes, stop: ship C (a separate `composer_operation_request_hash` with the same
body) and record it.

## Owned types — `src/elspeth/web/sessions/composer_operations.py` (N03)

```python
ComposerOperationKind = Literal["compose_message", "compose_recompose"]
ComposerOperationStatus = Literal["queued", "running", "completed", "failed"]
ComposerOperationFailureCode = Literal["http_error", "operation_failed", "worker_lost", "request_cancelled", "deadline_expired"]
ComposerOperationSettledBy = Literal["owner_terminal", "settle_unstarted", "request_cancel", "settle_lost",
                                     "settle_own_lapsed", "settle_lost_inactive_session"]
# + a Final[frozenset[str]] of the values for each Literal
COMPOSER_OPERATION_REQUEST_SCHEMA: Final = "composer-operation-request.v1"
COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS: Final = "message_with_state.v1"
COMPOSER_OPERATION_RESULT_SCHEMA_ERROR: Final = "composer_operation_error.v1"
COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH: Final[int]   # measured in N03 for both strict DTOs at their field caps
                                                          # with worst-case escaping, plus framing

def composer_operation_request_hash(*, session_id: UUID, kind: ComposerOperationKind,
                                    request: SendMessageRequest | RecomposeRequest) -> str
    # = session_operation_request_hash(schema=COMPOSER_OPERATION_REQUEST_SCHEMA, ...)
def composer_operation_result_hash(result_json: str) -> str
    # strict re-validate result_json with the model its schema names, then strict_response_hash

@final @dataclass(frozen=True, slots=True)
class ComposerOperationClaim:   session_id: UUID; operation_id: str; claim_token: str; attempt: int
@final @dataclass(frozen=True, slots=True)
class ComposerOperationRunning: claim: ComposerOperationClaim; session_operation_context: SessionOperationContext
@final @dataclass(frozen=True, slots=True)
class ComposerOperationRecord:  # Tier-1 read; __post_init__ validates every status bundle below
    session_id: UUID; operation_id: str; kind: ComposerOperationKind; status: ComposerOperationStatus
    request_hash: str; actor_user_id: str; request_id: str | None; base_state_id: UUID | None
    deadline_at: datetime; created_at: datetime; updated_at: datetime
    started_at: datetime | None; settled_at: datetime | None; cancel_requested_at: datetime | None
    claim_token_present: bool; claim_owner_instance_id: str | None; claim_expires_at: datetime | None; attempt: int
    session_operation_id: str | None; session_operation_epoch: int | None
    user_message_id: UUID | None
    failure_code: ComposerOperationFailureCode | None; settled_by: ComposerOperationSettledBy | None
    result_schema: str | None; result_json: str | None; result_sha256: str | None
    request_json: str | None      # poll reads load it as None (never selected)

class ComposerOperationError(BaseModel):   # model_config = strict + forbid; re-bound in schemas.py
    http_status: int = Field(ge=400, le=599)
    failure_code: ComposerOperationFailureCode
    error_type: str | None
    body: dict[str, JsonValue]              # the EXACT JSON body the synchronous route/handler would have sent
    diagnostic_id: str | None               # only for operation_failed

class ComposerOperationCancelledFailure(Protocol):
    def __call__(self, *, request_id: str | None) -> ComposerOperationError: ...

# Plain RuntimeError subclasses (precedent: OperationReceipt*Error; @tier_1_error is refused outside contracts/engine/core):
class ComposerOperationConflictError(RuntimeError)          # (*, session_id, operation_id)
class ComposerOperationActiveError(RuntimeError)            # (*, session_id, operation_id, kind) — the EXISTING job
class ComposerOperationCapacityError(RuntimeError)          # (*, retry_after_seconds: int)
class ComposerOperationFenceLost(RuntimeError)              # (*, session_id, operation_id, attempt: int | None = None)
class ComposerOperationCancelledBeforeStart(RuntimeError)   # (claim)
class ComposerOperationCancelledDuringTurn(RuntimeError)    # (claim)
class ComposerOperationPreconditionRefused(RuntimeError)    # (*, error: ComposerOperationError) — E5 refusals
class ComposerTurnDeadlineExpired(RuntimeError)             # raised before compose() when the remaining budget is <= 0
```

## Wire DTOs — `src/elspeth/web/sessions/schemas.py` (N03 defines; N14 mounts)

```python
class SendMessageRequest(_SessionOperationRequest):          # strict+forbid; operation_id (36, canonical UUID)
    content: str = Field(min_length=1, max_length=65536)     # + the visible-content validator
    state_id: UUID | None = None                             # mode="before" canonical-UUID parser (RevertStateRequest shape)
class RecomposeRequest(_SessionOperationRequest):
    expected_user_message_id: UUID                           # mode="before" parser
    state_id: UUID | None = None                             # mode="before" parser (ruling 5; the head at the retry click)
class LegacySendMessageRequest(_RequestModel): ...           # today's SendMessageRequest verbatim; deleted in N14
class LegacyRecomposeRequest(_RequestModel): ...             # today's RecomposeRequest verbatim; deleted in N14

class ComposerOperationAcceptedResponse(_StrictResponse):    # the 202 body
    operation_id: str; kind: ComposerOperationKind; status: ComposerOperationStatus; poll_after_ms: int
class ComposerOperationStatusResponse(_StrictResponse):      # poll body and cancel body
    operation_id: str; kind: ComposerOperationKind; status: ComposerOperationStatus
    cancel_requested: bool; poll_after_ms: int; deadline_at: datetime
    deadline_remaining_ms: int = Field(ge=0)                 # DB clock; 0 on terminal rows
    result: MessageWithStateResponse | None = None
    error: ComposerOperationError | None = None
# ChatMessageResponse.client_request_id → operation_id: str | None = None (N14; ruling 6)
```
Absent and explicit-null `state_id` hash identically (measured). Both mean "no state" (ruling 7).

## Table — `composer_async_operations` in `sessions/models.py` (N04)

**Columns:**

| Group | Columns |
|---|---|
| Identity | `session_id String(128) NOT NULL`, `operation_id String(36) NOT NULL`, `kind String(32) NOT NULL`, `status String(16) NOT NULL`, `request_hash String(64) NOT NULL`, `actor_user_id String(128) NOT NULL`, `request_id String(128) NULL` |
| Request | `base_state_id String(128) NULL`, `request_json Text NULL`, `deadline_at DateTime(tz) NOT NULL` |
| Claim | `claim_token String(256) NULL`, `claim_owner_instance_id String(128) NULL`, `claim_expires_at DateTime(tz) NULL`, `attempt Integer NOT NULL` (≥ 0) |
| Bound fence | `session_operation_id String(128) NULL`, `session_operation_lease_token String(256) NULL`, `session_operation_epoch Integer NULL` |
| Outcome | `user_message_id String(128) NULL`, `cancel_requested_at DateTime(tz) NULL`, `failure_code String(32) NULL`, `settled_by String(32) NULL`, `result_schema String(64) NULL`, `result_json Text NULL`, `result_sha256 String(64) NULL` |
| Timestamps | `created_at`, `updated_at` NOT NULL; `started_at`, `settled_at` NULL |

**Constraints:**

- PK `(session_id, operation_id)`, plus `UNIQUE (session_id, operation_id, user_message_id)` as the target of the
  ingress FK.
- FKs per E19.
- CHECKs:
  - closed `kind`, `status`, `failure_code` and `settled_by`;
  - lower-hex-64 on `request_hash` and `result_sha256` (`_lower_sha256_constraints`);
  - length 36 on `operation_id`;
  - `length(request_json) <= COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH`;
  - `attempt >= 0`;
  - time ordering.

**Status bundles** are written as AND/OR arms only: no `(a) = (b)` equalities and no one-element `IN`.

| Status | Must hold |
|---|---|
| queued | `request_json` NOT NULL. Fence triple, `started_at`, `settled_at`, result, failure, `settled_by` and `user_message_id` NULL. `claim_token`, `claim_owner_instance_id` and `claim_expires_at` all NULL or all NOT NULL. |
| running | `request_json`, `claim_token`, `claim_owner_instance_id`, fence triple and `started_at` NOT NULL. **`claim_expires_at` NULL.** Result, failure, `settled_by` and `settled_at` NULL. |
| completed | `request_json`, `claim_token`, `claim_expires_at` and `cancel_requested_at` NULL. `claim_owner_instance_id`, `attempt`, fence triple and `started_at` **kept** (NOT NULL). `result_schema = 'message_with_state.v1'`. `result_json`, `result_sha256`, `settled_at` and `settled_by` NOT NULL. `failure_code` NULL. |
| failed | `request_json`, `claim_token` and `claim_expires_at` NULL. `result_schema = 'composer_operation_error.v1'`. `result_json`, `result_sha256`, `settled_at`, `failure_code` and `settled_by` NOT NULL. Fence triple: all NULL (never started) or all NOT NULL (started). `claim_owner_instance_id` may be NULL (never claimed) or kept. |

The fence triple is **load-bearing on terminal rows**. If a terminal settle nulled it, the positive predicate (E10)
could not find the row and would fail open.

**Triggers.** Both dialects, all 5 inventory places; `_REQUIRED_AUDIT_TRIGGERS` goes from 11 to 13.

- `trg_composer_async_operations_transition_guard` (UPDATE) forbids:
  - running→queued;
  - changing `kind`, `request_hash`, `actor_user_id`, `base_state_id`, `claim_owner_instance_id` or the fence triple
    on a running row;
  - clearing `cancel_requested_at`;
  - changing `user_message_id` once it is NOT NULL;
  - any UPDATE of a terminal row.
- `trg_composer_async_operations_no_delete_live` (DELETE) refuses while the session row exists (the
  `IF EXISTS sessions` pattern).

**Indexes:**

- `uq_composer_async_operations_one_nonterminal_per_session (session_id) WHERE status IN ('queued','running')`
  (D8; identical `sqlite_where` / `postgresql_where`);
- `ix_composer_async_operations_claimable (status, claim_expires_at, created_at) WHERE status = 'queued'`;
- `uq_composer_async_operations_bound_fence (session_id, session_operation_epoch) WHERE session_operation_epoch IS NOT NULL`;
- `(status, deadline_at)` only if N05 measures that `list_expired_queued` needs it.

**Policy.** `TablePolicy("composer_async_operations", "session", "ComposerAsyncOperationAuthority")` with **no** operation
grants. All SQL lives in the authority module; the composites call its connection-taking helpers. Digest inventory:
`request_hash`, `result_sha256`.

**Table comment** states:

- it never blocks archive (D7) and is not durable history in `decide_and_soft_archive`;
- it is retained with its session (ruling 4);
- job liveness == SOL liveness (R3);
- there is no length bound on `result_json` (a server-authored, validated DTO).

## Authority — `src/elspeth/web/coordination/composer_operation_authority.py` (N05)

Sync. It owns the engine, uses the DB clock (`database_now(conn)`), and runs every mutation in
`locked_session_transaction`. Callers go through `run_sync_in_worker`. Every scan and count carries an explicit
`LIMIT`; poll reads never select `request_json`.

```python
class ComposerAsyncOperationAuthority:
    def __init__(self, engine: Engine, *, owner_instance_id: str, claim_lease_seconds: int) -> None
    def admit(self, *, session_id: UUID, operation_id: str, kind: ComposerOperationKind, request_hash: str,
              actor_user_id: str, request_id: str | None, base_state_id: UUID | None, request_json: str,
              deadline_seconds: float, max_nonterminal: int) -> tuple[ComposerOperationRecord, bool]
        # PK read → binding compare (Conflict / replay); soft count → Capacity; INSERT queued. An IntegrityError from
        # the D8 index → re-read the session's nonterminal row → ComposerOperationActiveError(existing id and kind).
        # A racing same-PK insert → re-read and re-compare.
    def get(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None
    def claim_next(self, *, limit: int) -> tuple[ComposerOperationClaim, ...]
        # A discovery read (queued; claimable = unclaimed or claim expired; deadline not passed; no cancel marker;
        # session has NO live session_operation_fences row of any kind), then one locked tx per candidate:
        # a CAS with status='queued' in the WHERE. PG adds FOR UPDATE SKIP LOCKED; SQLite relies on BEGIN IMMEDIATE.
    def renew_claim(self, claim: ComposerOperationClaim) -> ComposerOperationRecord    # queued only; FenceLost
    def release_claim(self, claim: ComposerOperationClaim) -> None                      # queued only; back to unclaimed
    def request_cancel(self, *, session_id: UUID, operation_id: str,
                       cancelled_failure: ComposerOperationCancelledFailure) -> ComposerOperationRecord | None
        # sets the marker on queued/running; an UNCLAIMED queued row settles request_cancelled in-tx
        # (settled_by='request_cancel'); a terminal row is returned unchanged; None if the row is missing
    def settle_unstarted(self, claim_or_none: ComposerOperationClaim | None, *, session_id: UUID,
                         operation_id: str, failure: ComposerOperationError) -> ComposerOperationRecord
    def list_expired_queued(self, *, limit: int) -> tuple[ComposerOperationRecord, ...]   # deadline passed or marker set
    def list_expired_running(self, *, limit: int) -> tuple[ComposerOperationRecord, ...]  # bound fence not live
    def settle_lost(self, *, session_operation_context: SessionOperationContext, session_id: UUID,
                    operation_id: str, failure: ComposerOperationError,
                    cancelled_failure: ComposerOperationCancelledFailure) -> ComposerOperationRecord
    def settle_lost_inactive_session(self, *, session_id: UUID, operation_id: str, failure: ComposerOperationError,
                                     cancelled_failure: ComposerOperationCancelledFailure) -> ComposerOperationRecord
        # unfenced; guarded by sessions.archived_at IS NOT NULL AND status='running'
    def settle_own_lapsed(self, *, session_id: UUID, operation_id: str, owner_instance_id: str,
                          failure: ComposerOperationError,
                          cancelled_failure: ComposerOperationCancelledFailure) -> ComposerOperationRecord
        # running, claim_owner_instance_id = owner, and the bound fence has released_at IS NULL AND
        # lease_expires_at <= db_now; reads the fence, never advances it
    def count_nonterminal(self, *, limit: int) -> int

async def admit_composer_operation(authority: ComposerAsyncOperationAuthority, *,
                                   rate_limit: Callable[[], Awaitable[None]], **admit_kwargs: object
                                   ) -> tuple[ComposerOperationRecord, bool]
    # get → rate_limit() only when the row is absent → admit   (the rate limiter commits on its own connection)

# Connection-taking helpers (the ONLY other writers of the table; the composites call them):
def start_composer_operation_on_connection(conn, claim: ComposerOperationClaim, *, context: SessionOperationContext,
                                           user_message_id: UUID | None) -> None
    # queued → running CAS (status='queued', claim_token, claim not expired, cancel_requested_at IS NULL);
    # sets the fence triple and started_at; claim_expires_at = NULL; user_message_id only for recompose (E9)
def bind_composer_operation_user_message_on_connection(conn, running: ComposerOperationRunning, *,
                                                       user_message_id: UUID) -> None
def settle_composer_operation_on_connection(conn, running: ComposerOperationRunning, *,
                                            outcome: MessageWithStateResponse | ComposerOperationError) -> ComposerOperationRecord
    # terminal CAS keyed on (status='running', claim_token, fence triple, cancel_requested_at IS NULL);
    # settled_by='owner_terminal'; keeps the owner, attempt and fence triple; rowcount != 1 → re-read →
    # CancelledDuringTurn / FenceLost
```
Every settle writes `settled_by` and keeps `claim_owner_instance_id` and `attempt`. There is no path that returns a
running row to queued; the trigger and the Python CAS both enforce this, each with a negative control that goes red.

## Composite start (R3 + E5) — `coordination/repository.py` (N06)

```python
# _SessionOperationAuthorityRepository + the SessionOperationAuthority Protocol (+ every Protocol fake, measured):
def start_composer_async_operation(self, claim: ComposerOperationClaim, *, owner_instance_id: str,
                                   lease_seconds: int, auth_provider_type: str) -> SessionOperationContext
    # ONE _locked_transaction:
    # 1. re-read the job: queued, this claim, not expired, deadline not passed (→ settle-worthy
    #    ComposerOperationPreconditionRefused(deadline_expired_error…)), no marker (→ CancelledBeforeStart)
    # 2. _advance_exclusive_fence_on_connection(conn, …)   # extracted verbatim from acquire(); acquire() calls it
    #    (SessionOperationConflictError / SessionOperationFenceLost propagate unchanged)
    # 3. check_composer_operation_preconditions_on_connection(conn, job, auth_provider_type=…)   # E5; raises
    #    ComposerOperationPreconditionRefused(error=…)
    # 4. start_composer_operation_on_connection(conn, claim, context=…, user_message_id=<recompose only>)
    # A raise from any step rolls the whole tx back (the fence is not advanced).
```
`check_composer_operation_preconditions_on_connection(conn, job: ComposerOperationRecord, *, auth_provider_type: str) -> None`
lives in `sessions/composer_operation_preconditions.py`. It depends only on `sessions/models.py` tables, SQL and the
owned types, never on `coordination/`; N06 verifies there is no import cycle, since `coordination/repository.py` calls
it. It reads the session row, `base_state_id`'s membership in the session, the head (`ORDER BY version DESC LIMIT 1`)
and, for recompose, the last conversational row.
N06 measures what `_composer_conversation_messages` filters and pins parity with a test. Refusal bodies:

- ownership → the byte-identical 404 `{"detail":"Session not found"}` (the `_verify_session_ownership` shape);
- foreign or unknown base → the byte-identical 404 `{"detail":"State not found"}` (today's `messages.py:199/:201`);
- moved base → E6;
- recompose: no conversation → 400 `"No messages to recompose from"`; last row not user → today's 409 string; mismatch
  → 409 `recompose_user_message_mismatch` (dict detail, request id injected).

The worker adopts the returned context:
`await SessionOperationLease.adopt(service.session_operation_authority, context, lease_seconds=…)`.
This is `adopt`'s first production caller.

## Composite terminal (R2) — `sessions/service.py` (N07)

```python
async def complete_composer_async_operation(self, running: ComposerOperationRunning, *,
        assistant: ComposerOperationAssistantWrite | None, assistant_record: ChatMessageRecord | None,
        audit_cohort: tuple[AuditMessageDraft, ...], audit_composition_state_id: UUID | None,
        build_response: Callable[[ChatMessageRecord, tuple[CompositionProposalRecord, ...]], MessageWithStateResponse],
        ) -> ComposerOperationRecord
    # ONE transaction under _session_composer_mutation_transaction(COMPOSE):
    # assistant insert (if given) → the cohort via the extracted _write_audit_cohort_on_connection
    # (ledger + mark_session_updated kept) → _list_composition_proposals_on_connection (same Tier-1 checks) →
    # build_response → strict validate → canonical dump → settle_composer_operation_on_connection.
    # The post-commit telemetry projection is kept (_run_sync_with_post_commit_projection).
async def fail_composer_async_operation(self, running: ComposerOperationRunning, *,
                                        failure: ComposerOperationError) -> ComposerOperationRecord
    # one tx; audit_only=(failure.failure_code == "request_cancelled")
@final @dataclass(frozen=True, slots=True)
class ComposerOperationAssistantWrite:
    message_id: UUID; content: str; raw_content: str | None; composition_state_id: UUID | None
```
A cancel that wins the CAS rolls back the assistant row, but never an audit cohort persisted separately with
`audit_only` (E10/E11). N07 starts by measuring the post-terminal write inventory (ruling 2's precondition) and ships
two controls: a non-audit write under a lingering SOL after the terminal fails, and fork/revert writes under their
own SOL are unaffected.

## Request lifecycle (E1) — `sessions/routes/_helpers.py` (N08)

```python
@final
class ComposerRequestLifecycle:           # the handle the async CM yields
    lease: ComposerRequestLease
    durable_completed: bool               # replaces request.state.composer_durable_completed
@contextlib.asynccontextmanager
async def composer_request_lifecycle(registry, *, session_id: str, user_id: str,
                                     owner_task: asyncio.Task[object],
                                     timer: _ComposerHeartbeatTimer | None = None
                                     ) -> AsyncIterator[ComposerRequestLifecycle]
    # the body of _track_compose_inflight after ownership (surface "freeform"), with an identical renewal policy
async def composer_progress_sink_for_lease(registry, *, lease: ComposerRequestLease, session_id: str,
                                           request_id: str | None, user_id: str) -> ComposerProgressSink
def composer_session_lock_registry(app: Starlette) -> _SessionComposeLockRegistry   # created eagerly in the lifespan
```
Until N14, `_track_compose_inflight` becomes a thin wrapper over these, and both routes behave identically: the three
heartbeat/telemetry files pass unmodified. N14 deletes the wrapper, its mount pin and the testcontainer probe mount
(re-pointed at the lifecycle).

## Budget (E14) — composer (N09)

- `ComposerService.compose(…, budget_seconds: float | None = None)`, in the protocol and in `composer/service.py:898`.
  `None` keeps `self._timeout_seconds`.
- `PlanningApplication._plan_and_stage_empty_pipeline(…, budget_seconds: float | None)` →
  `PlannerModelConfig.timeout_seconds`.
- `settle_pipeline_proposal_under_compose_lock(…, commit_timeout_seconds: float)`.
- `explain_run_diagnostics` reads `composer_sync_timeout_seconds`.
- The gateway sidecar's 300 s per-call bound is documented as a relationship, not changed. N00 measures whether the
  sidecar is on any shipped composer path and how `UPSTREAM_TIMEOUT` classifies.

## Error projection (E4, E6, E22) — `sessions/composer_operation_errors.py` (N10)

```python
def project_composer_operation_error(exc: BaseException, *, request_id: str | None) -> ComposerOperationError
def deadline_expired_error(*, request_id: str | None, timeout_seconds: float) -> ComposerOperationError
def worker_lost_error(*, request_id: str | None) -> ComposerOperationError
def request_cancelled_error(*, request_id: str | None) -> ComposerOperationError
def stale_base_error(*, request_id: str | None) -> ComposerOperationError
```
**Parity instrument.** Build the real app's exception handlers (reuse the registration, never copy handler bodies),
raise each case, and compare `(status, json)` modulo the request-id value. The rows cover the whole live ladder:

- convergence ×3;
- `_handle_composer_provider_failure` 502/504 with `guidance`;
- `_BadRequestLLMError`;
- plugin crash;
- preflight paths 1 and 2;
- the planner map, incl. `cost_unavailable` and `policy_blocked`;
- `_handle_composer_chargeable_refusal` 503/403;
- `ComposerAdmissionRefused`;
- `ComposerServiceError`;
- `server_invariant_violated`;
- the recompose 400/409/409;
- settlement 409/422/504;
- the app handlers (`AuditIntegrityError`, `StaleComposeStateError` flat, `SessionOperationFenceLost`,
  `SessionOperationConflictError`, `FingerprintKeyMissingError`, `SecretDecryptionError`, `OperationalError`,
  retryable `OSError`);
- `operation_failed` for defects, `ExceptionGroup` and non-retryable `PermissionError`;
- `AsyncWorkerAdmissionTimeoutError` → 503 `database_unavailable`.

The ingress 409 and 499 rows are gone.

## Services and turn — `sessions/composer_app_services.py`, `sessions/composer_turn.py` (N11a, N11b)

```python
@final @dataclass(frozen=True, slots=True)
class ComposerAppServices:   # built PER JOB by composer_app_services(app) — the only app.state reads on the turn
    session_service; composer_service; settings; catalog_service; operator_profile_registry; scoped_secret_resolver
    session_engine; interpretation_surfacing; plugin_snapshot_for_user_id: Callable[[str], PluginAvailabilitySnapshot]
    progress_registry; compose_locks: _SessionComposeLockRegistry
def composer_app_services(app: Starlette) -> ComposerAppServices

# D6: settle_auto_commit_intent(*, services, user_id, service, session_id, intent, composer_meta, telemetry_source,
#     session_operation_context, commit_timeout_seconds) and
#     settle_pipeline_proposal_under_compose_lock(*, services, user_id, …, commit_timeout_seconds); both routes,
#     PS:429 and proposals.py:314 are updated in N11a.

@final @dataclass(frozen=True, slots=True)
class ComposerTurnInput:
    session_id: UUID; operation_id: str; kind: ComposerOperationKind; actor_user_id: str
    request: SendMessageRequest | RecomposeRequest; request_id: str | None; budget_seconds: float
@final @dataclass(frozen=True, slots=True)
class ComposerBudgetAnchor:
    remaining_at_running_seconds: float; monotonic_at_running: float
    def remaining_seconds(self, *, monotonic_now: float) -> float
@final
class ComposerTurnObservation:           # mutable, one per job
    compose_result: ComposerResult | None = None
    pending_exception_llm_calls: tuple[ComposerLLMCall, ...] = ()
    audit_cohort_durable: bool = False
async def run_composer_turn(services, turn: ComposerTurnInput, *, lease: SessionOperationLease,
                            running: ComposerOperationRunning, request_lifecycle: ComposerRequestLifecycle,
                            observation: ComposerTurnObservation, budget_anchor: ComposerBudgetAnchor
                            ) -> ComposerOperationRecord
async def persist_cancelled_turn_audit(services, running: ComposerOperationRunning,
                                       observation: ComposerTurnObservation) -> None
```
`run_composer_turn` **rebuilds** the live route bodies (`messages.py:134-1041`, `compose.py:94-750`); it does not
move them byte for byte. It keeps:

- the owned-child settlement custody (`_join_freeform_owned_task`) and its deferred-cancel semantics;
- the typed ladder arm for arm, with its per-kind labels (endpoint metric, slog prefixes, `route` strings,
  `telemetry_source`);
- D7's recompose planner progress copy, aligned with send's (a recorded visible change).

It also:

- awaits `compose()` **inline in the turn task**, which preserves the cancel-path `llm_calls`;
- makes the send preamble write the user row + job `user_message_id` + ingress (E8);
- runs the recompose preamble after the start composite has already verified the transcript (E5);
- has each typed arm record `observation.pending_exception_llm_calls` before its first session write;
- sets `audit_cohort_durable` after every cohort persist;
- joins auto-title before the terminal (E12);
- ends in `complete_composer_async_operation` on success.

The structural pins (`test_operation_fence_wiring.py:84-110`, `:323-341`) are retargeted at `run_composer_turn` with
the same intent: SOL before any state read or transcript write, and auto-title via `lease.create_task`.

## Worker — `sessions/composer_async_worker.py` (N12)

```python
class ComposerAsyncWorker:
    def __init__(self, *, app: Starlette, authority: ComposerAsyncOperationAuthority, concurrency: int,
                 scan_interval_seconds: float, claim_lease_seconds: int, drain_seconds: float,
                 owner_instance_id: str, process_recovery: ProcessRecovery, instance_draining: threading.Event) -> None
    def start(self) -> None
    async def stop(self) -> None
    async def run_until_idle(self) -> None          # test seam
    def signal_local_cancel(self, *, session_id: UUID, operation_id: str) -> bool
    async def reap_once(self) -> int
```
**Per job:**

1. Claim, then renew the claim while waiting for the in-process compose lock. The wait observes a local cancel, the
   marker and the deadline, and settles through `settle_unstarted` without the lock (F-C3).
2. Run the start composite:
   - `SessionOperationConflictError` → `release_claim` (requeue);
   - `ComposerOperationPreconditionRefused` → `settle_unstarted(error)`;
   - `CancelledBeforeStart` → `request_cancelled`;
   - any other exception → `operation_failed` via `settle_unstarted`.
3. `adopt`. A defect in `adopt` → a fenced fail write. The accepted residual: if `adopt` already released the
   context, the job settles as `worker_lost` through the reaper.
4. Open the CRL lifecycle.
5. Start a watcher (the `ExecutionServiceImpl._signal_shutdown_on_operation_loss` pattern: `wait_until_lost` with a
   0.25 s poll, plus a marker read with backoff). It cancels the turn task with a `_ComposerOperationCancel` identity
   marker whose kind is `cancel_requested`, `lease_lost` or `shutdown`.
6. Run the turn. `current_task().cancelling()` is read inside the turn task.
7. Every non-success exit joins `persist_cancelled_turn_audit` before the terminal.
8. The fail write retries the transient family (`OperationalError`, `AsyncWorkerAdmissionTimeoutError`) with a
   bounded backoff while the lease is live.
9. If the fail write is fenced out: `settle_own_lapsed`, otherwise leave the row to the reaper.
10. Log every settle as a structured event, asserted with `capture_logs` and redaction checks.

**Reaper:**

- `list_expired_queued` → `settle_unstarted`;
- `list_expired_running` → acquire SOL COMPOSE (a success proves the owner's fence lapsed) → `settle_lost`;
  - `OWNER_INACTIVE` → `settle_lost_inactive_session`;
  - `MISSING` → already terminal (the loop survives);
  - a conflict on a row this instance owns but no longer runs → `settle_own_lapsed`.

**Lifespan (`app.py`):**

- construct the worker and run `reap_once()` between `recover()` and `orphan_task`;
- `start()` after the orphan done-callback;
- `stop()` in the nested `finally` after `begin_drain()` and before `execution_service.shutdown()`;
- a dead loop escalates via `process_recovery.request_shutdown()`;
- the worker is published as `app.state.composer_async_worker`.

## HTTP (N13 poll/cancel; N14 cutover)

- **`GET /api/sessions/{session_id}/operations/{operation_id}`** → 200 `ComposerOperationStatusResponse`.
  - `Cache-Control: no-store`.
  - Ownership is checked on every call.
  - A foreign or missing session → `{"detail":"Session not found"}`; a missing row → `{"detail":"Operation not found"}`.
  - The read re-validates `result_sha256` and the strict DTO; a mismatch → `AuditIntegrityError`.
  - `deadline_remaining_ms` is computed from the DB clock; 0 when terminal.
- **`POST /api/sessions/{session_id}/operations/{operation_id}/cancel`** → 200 when terminal (a completed job wins);
  202 with `cancel_requested=true` when owned; 404 when missing. It also signals the local worker.
- **Router:** `routes/composer/operations.py`, added to the IDOR module tuple and the ownership inventory
  (`test_routes.py:4102-4103`, `:3979-4032`).
- **`POST /messages` and `POST /recompose`:**
  - `status_code=202`, `response_model=ComposerOperationAcceptedResponse`, with strict DTOs and no
    `_track_compose_inflight`;
  - admission per E2; a same id + body → 202 with the current status;
  - 409 conflict `{"detail":{"error_type":"composer_operation_conflict","detail":"This operation id was already used for a different request."}}`;
  - 409 active `{"detail":{"error_type":"composer_operation_active","detail":"This session already has a composer request in progress.","operation_id":…,"kind":…}}`;
  - 429 `{"detail":{"error_type":"composer_queue_full","detail":"The composer is busy. Retry shortly.","retry_after":n}}`
    with a `Retry-After` header.
- **N14 also:**
  - lands the ingress DDL: rename `client_request_id` → `operation_id`, `String(36)` + CHECK, and the composite FK
    to the job;
  - deletes the ingress 409 arms, `lookup_message_ingress` and the legacy DTOs;
  - renames `ChatMessageResponse.client_request_id` → `operation_id`.

## Test helpers — `tests/helpers/composer_operations.py` (N13)

`submit_and_settle(client, app, *, path, body, max_rounds=50) -> SettledComposerOperation`,
`settle_sync(test_client, app, *, path, body) -> SettledComposerOperation` (one `anyio.run`; a split-loop control must
go red), `install_composer_async_worker(app, **overrides) -> ComposerAsyncWorker` (no lifespan; `threading.Event`;
works on `_make_app` and `_route_client` apps; `_route_client` gains any stub the worker needs, such as
`interpretation_surfacing`). `SettledComposerOperation.result()` / `.error()`.

## Frontend — `src/elspeth/web/frontend/src` (N15)

- **`api/client.ts`:**
  - `apiErrorFromBody(status, body)` (no 401 logout), used by `parseResponse`;
  - `submitComposerOperation`, `fetchComposerOperation` and `cancelComposerOperation`, each bounded by
    `AbortSignal.timeout(15_000)`;
  - a 404 is `{kind:"session_missing"} | {kind:"operation_missing"}`, read from the body.
- **`api/composerOperationDecoder.ts`:** exact-record decoders.
- **`stores/composerOperationCustody.ts`:**
  - sessionStorage key `elspeth_composer_operations_v1`, schema `composer-operations.v1`;
  - one descriptor per session: `{sessionId, operationId, kind, body, createdAt}`;
  - 256 KiB, 24 h, a memory fallback, and no orphan sweep in `selectSession`;
  - it deliberately stores the body, reversing `sessionOperationRetry`'s fingerprint-only invariant
    (`sessionOperationRetry.ts:34-43`, `:181-198`) for this kind only.
- **`stores/sessionStore.ts`:**
  - send/retry: custody acquire → submit → on network ambiguity, poll by id and resubmit the same id/body only if
    the operation is missing → poll loop (`poll_after_ms`; backoff 1→2→4→8 s on transient errors, which never fail
    the turn) → the existing reducers;
  - reducers get the `result`, or `apiErrorFromBody(error.http_status, error.body)`;
  - pollers stop only on terminal; `isComposing` holds from custody acquire until terminal;
  - `resumeComposerOperation(sessionId)` runs on `selectSession`/boot before render;
  - 409 `composer_operation_active` → attach to that id;
  - a 404 `session_missing` → today's session-not-found path, clears custody, never resubmits;
  - after every non-success terminal → reload the authoritative state;
  - deletes: `reconcileAcceptedSend`, the ingress 409 arms, `local_accepted_user_message_id`, and
    `ApiError.client_request_id` / `user_message_id`;
  - the optimistic dedup re-keys onto `operation_id`.
- **Ruling 5 in the SPA:**
  - Send is held until `compositionStateLoaded`;
  - a `stale_compose_state` terminal gives the row `local_failure_code: "stale_compose_state"`; its Retry mints a
    **new** operation id with the reloaded head and the same content (network-ambiguous retries keep the id and body);
  - while a same-tab send is nonterminal, the head-moving controls are disabled: proposal Accept, revert,
    interpretation resolve and YAML import (Reject stays enabled);
  - recompose sends the head at the retry click.
- **`hooks/useComposer.ts`:** Stop → the cancel endpoint, then keep polling to terminal. The client deadline follows
  E16.
- **Tutorial:**
  - the shell's send guard and Continue gate read the restored `isComposing` / custody (no tutorial branch
    server-side);
  - the tutorial Stop test is rewritten for the cancel endpoint.
- **E2E:**
  - `composer-proposals.spec.ts` and `tutorial.spec.ts` mock 202 + poll;
  - the Playwright transition-ledger recorder moves its turn boundary to the terminal poll, so the per-TRANSITION
    provider-call gate (the composer standing review trigger) still counts calls.

## Task list (order = commit order; every task ends green on its own tests)

**Before N00, one docs commit:** this contract + the second spec amendment + the plan index. There is no interim
merge from N03 to N15.

| # | Task | Replaces | Depends on |
|---|---|---|---|
| N00 | Worktree; baseline measurements M0–M12 (below); gate-2 baseline test; **Appendix A**: every exit of both routes from the live ladder, plus the ruling 1/2/5 exits, each with its owning task | T00 | docs commit |
| N01 | Settings (6 knobs + the sync cap and its three consumers); behaviour-neutral | T01 | N00 |
| N02 | Shared normaliser + response hash; receipts delegate; golden vectors first. **Go/no-go → C** | T02 part | N00 |
| N03 | Owned types; strict + legacy DTOs; wire DTOs; request-JSON bound measured | T02 part + T10 1b | N02 |
| N04 | Job table per § Table (no ingress DDL) | T03 | N03 |
| N05 | Authority + connection-taking helpers; queued-only CAS negative controls; D8 index mapping; manifest | T04 | N04 |
| N06 | Composite start + acquire extraction + precondition gate (E5) + receipts regression | T05 | N05 |
| N07 | Post-terminal inventory → positive predicate (E10/E11) → composite terminal | T06 | N06 |
| N08 | Request lifecycle + durable carrier + app-keyed lock registry; routes behaviour-unchanged | T07 | N00 |
| N09 | Budget threading (compose, planner, settlement) + D10 | T08 | N01 |
| N10 | Error projection + parity | T09 | N03 |
| N11a | `ComposerAppServices` + D6 settlement signatures + the ingress/job binding in `add_message_with_transcript` | T10 part | N07, N08, N09, N10 |
| N11b | `run_composer_turn` + observation + retargeted structural pins | T10 part | N11a |
| N12 | Worker + reaper + lifespan + watcher (gate 4) | T11 | N11b |
| N13 | Poll/cancel routes + test helpers + shared fakes | T12 | N12 |
| N14 | Cutover: 202, ingress DDL + rename, deletions (409 arms, lookup, legacy DTOs, `_track_compose_inflight`), caller migration incl. `state_id`, ACA P1 redefinition + probes + `acceptance.sh`, eval battery, gate 2 | T13 + T03 part | N13 |
| N15 | SPA cutover (above) + e2e + transition-ledger boundary | T14 | N14 |
| N16 | Budget decoupling + deployment mirrors | T15 | N14 |
| N17 | PG crash windows + cross-instance + the new PG proofs (cascade, starvation, stale base, post-terminal write, `settled_by`, delete guard) | T16 | N14 |
| N18 | Epoch 72 (re-read first) + doc/website/CHANGELOG sweep | T17 | all |
| N19 | Full gates (all stages) + lints set diff + local short-idle-proxy browser acceptance | T18 | N18 |

**N00 measurements:**

- **M0** base SHA.
- **M1** epoch.
- **M2** live branches that touch these files.
- **M3** the lints finding set, key-free.
- **M4** writer-manifest before/after (`test_all_production_sessions_writers_are_reviewed_typed_authorities`).
- **M5** the focused-gate baseline.
- **M6** p50/p99 of `request_json` / `result_json`.
- **M7** census of the POST sites, **incl. callers that omit `state_id` on a session with a head**.
- **M8** the post-terminal write inventory with auto-title hot (a preview for N07).
- **M9** gateway sidecar presence + `UPSTREAM_TIMEOUT` classification.
- **M10** golden vectors from the unmodified receipts codec.
- **M11** a PG session cascade with a RESTRICT sibling and an ingress row.
- **M12** auto-title timing under a real or stub provider.
