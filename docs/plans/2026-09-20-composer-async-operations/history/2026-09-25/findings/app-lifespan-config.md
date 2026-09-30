# Seam: app lifespan, background workers, timeout config

Explorer lane for the composer async-operations plan (spec
`docs/specs/2026-09-16-composer-async-operations-design.md`, amended 2026-09-25,
FREEFORM ONLY). Measured on `release/0.8.1` at `d479eb2b4`; `src/`, `tests/`,
`deploy/` clean at start (`git status --short src tests deploy` printed nothing).
All anchors are `path:line` in the current tree.

Note: the spec file itself is modified-uncommitted in the main checkout (git
status `M docs/specs/2026-09-16-composer-async-operations-design.md`). The
anchors below cite it as read today (§3 lines 150-220).

---

## 1. Application lifespan (`src/elspeth/web/app.py`)

### 1.1 Two nested lifespans

- `lifespan(app)` — `app.py:580-596`. Wraps `_service_lifespan`; starts
  `app.state.auth_audit_recorder` (585), and in `finally` runs the auth-audit
  finalizer then the session-engine finalizer (591-595).
- `_service_lifespan(app)` — `app.py:598-969`. Everything service-owned.
- `FastAPI(..., lifespan=lifespan)` — `app.py:1357`.

### 1.2 What `create_app` has already done before lifespan runs (session store readiness)

Session store readiness is established synchronously in `_create_app`, NOT in
the lifespan:

- External (PostgreSQL) mode: engine created `app.py:1576-1580`; finalizer
  registered 1583-1587; `profile.validate_only_schema_or_raise(settings, engine)`
  1588-1592 (validate only; ordinary startup never creates schema).
- SQLite mode: `create_session_engine` 1633-1634; `initialize_session_schema(session_engine)`
  1638 (creates schema); SQLite file engine disposed 1639-1641.
- `app.state.session_engine = session_engine` 1656.
- `app.state.session_service = SessionServiceImpl(...)` 1826-1853
  (`owner_instance_id=instance_id` 1847). `session_operation_lease_seconds` is
  NOT passed → default 30 s (`sessions/service.py:4480`
  `session_operation_lease_seconds: int = 30`, stored 4512, property 4540).
- Membership object chosen by dialect 1863-1876:
  `RegisteredWebInstanceMembership(... lease_seconds=session_service.session_operation_lease_seconds, process_recovery=...)`
  on postgresql, else `SingleProcessWebInstanceMembership()`.
  `app.state.process_recovery` 1865, `app.state.instance_draining = web_instance_membership.draining` 1876.
- `app.state.composer_service = ComposerServiceImpl(...)` 1885-1895 (singleton).
- Dialect split for cross-process UI state 1897-1922: postgres →
  `DatabaseComposerProgressRegistry(SessionComposerProgressAuthority(session_engine, owner_instance_id=instance_id))`
  (1900-1902), `SharedRateLimiter`s; sqlite → process-local
  `ComposerProgressRegistry()` (1917) and `ComposerRateLimiter`s. **This is the
  existing PG-vs-SQLite branching precedent** a worker claim strategy can mirror.

### 1.3 `_service_lifespan` startup order (exact)

| Step | Line | What |
|---|---|---|
| 1 | 613-621 | read settings/state_mode, resolve `landscape_url`, `session_service` |
| 2 | 625 | `await app.state.blob_service.reconcile_inline_custody_publications()` (boot fails if it fails) |
| 3 | 634-642 | SSO runtime resolution (`SystemExit` on discovery failure) |
| 4 | 648 | `await app.state.web_instance_membership.start()` — registers + starts heartbeat task (PG) |
| 5 | 652-671 | `ProgressBroadcaster(loop, ...)`, `ExecutionServiceImpl(...)` → `app.state.execution_service` |
| 6 | 680-690 | `app.state.readiness_service = ReadinessService(...)` |
| 7 | 706-736 | payload store, `library_authority`, `share_token_signer`, `shareable_review_service` |
| 8 | 742 | `await _boot_prime_openrouter_catalog(settings)` |
| 9 | 744-878 | composer boot probes (if `settings.composer_boot_probe_enabled`) |
| 10 | 884-891 | OpenRouter catalog snapshot id onto `app.state` + executor |
| 11 | 893-901 | `RunRecoveryCoordinator(...)`; `await recovery_coordinator.recover()` (startup reaper) |
| 12 | 904-922 | **`orphan_task = asyncio.create_task(_periodic_orphan_cleanup(...))`** + done callback |
| 13 | 924-925 | `yield` (serving) |

Comment at 644-647: "Join the deployment only after the startup sweeps have
settled". Membership start (648) happens BEFORE the run recovery sweep (901),
and the periodic sweeper is the last thing created before `yield`.

### 1.4 Shutdown order (`finally`, `app.py:926-969`)

1. `app.state.process_recovery.begin_shutdown()` — 927.
2. `await app.state.web_instance_membership.begin_drain()` — 932 (readiness
   fails at once; row says `draining`; outcome returned, never raised).
3. `orphan_task.cancel()` — 938; `with contextlib.suppress(asyncio.CancelledError): await orphan_task` — 939-941.
   A task that already died re-raises its stored failure here (comment 933-937).
4. nested `finally`: `app.state.readiness_probe_runner.close()` — 943.
5. `await execution_service.shutdown()` — 949 (nested try).
6. `finally: await app.state.web_instance_membership.stop()` — 955 (row
   `stopped`, lease expired at once; cancels heartbeat).
7. `finally: await app.state.operator_telemetry.shutdown()` — 962.
8. `finally: from elspeth.web.async_workers import shutdown_async_workers; await shutdown_async_workers()` — 967-969.

Every step is chained through nested `try/finally` so a failure cannot skip a
later step.

### 1.5 Existing background tasks / loops / reapers (inventory)

Instrument: `grep -rn "create_task\|ensure_future\|TaskGroup\|loop\.run_in_executor\|add_done_callback" src/elspeth/web --include=*.py`
plus `grep -rn "await asyncio.sleep(" src/elspeth/web`. Known-positive:
`app.py:904` (the orphan task) and `membership_lifecycle.py:140` both appear.
Every other `create_task` hit is request-scoped or lease-scoped (helpers in
`coordination/lifecycle.py`, guided_operations, routes, planner), not
lifespan-owned. Only two `while True: await asyncio.sleep(interval)` periodic
loops exist in `src/elspeth/web`: `app.py:409-410` and
`coordination/membership_lifecycle.py:152-153`.

**Process-lifetime (lifespan-owned) tasks:**

1. **Periodic orphan-run sweeper** — `_periodic_orphan_cleanup(...)`, `app.py:388-477`:
   ```python
   async def _periodic_orphan_cleanup(
       session_service: SessionServiceImpl,
       execution_service: ExecutionServiceImpl,
       telemetry: _SessionsTelemetry,
       *,
       interval_seconds: int,
       max_age_seconds: int,
       landscape_url: str | None = None,
       create_tables: bool = True,
       recovery_coordinator: RunRecoveryCoordinator | None = None,
   ) -> None:
   ```
   - loop `while True: await asyncio.sleep(interval_seconds)` 409-410;
     production path calls `recovery_coordinator.recover()` 415-416.
   - Retries only `OperationalError`, bounded by
     `_ORPHAN_CLEANUP_MAX_CONSECUTIVE_FAILURES = 5` (377); past the bound it
     logs `periodic_orphan_cleanup_escalating` and re-raises (437-464). All
     other exceptions propagate immediately (task dies). Logs omit `exc_info`
     deliberately (URL leak via SQLAlchemy cause chains, 446-452).
   - Telemetry: `telemetry.orphaned_runs_cancelled_total.add(...)` 470-474.
   - Created at 904-915 with `interval_seconds=settings.orphan_run_check_interval_seconds`,
     `max_age_seconds=settings.orphan_run_max_age_seconds`.
   - Done callback `_recover_process_on_orphan_failure` 917-922:
     ```python
     if not completed.cancelled() and completed.exception() is not None:
         app.state.instance_draining.set()
         app.state.process_recovery.request_shutdown()
     ```
2. **Web-instance membership heartbeat** — `RegisteredWebInstanceMembership`,
   `coordination/membership_lifecycle.py:100-240`:
   - `start()` 134-148: `run_sync_in_worker(self._authority.register, ...)`, then
     `task = asyncio.create_task(self._heartbeat_loop())` 140, done callback
     sets draining + `process_recovery.request_shutdown()` 142-147.
   - `_heartbeat_loop()` 150-176: `await asyncio.sleep(self._interval_seconds)`;
     `run_sync_in_worker(self._authority.heartbeat, ...)`; retries only
     `OperationalError` up to `_HEARTBEAT_MAX_CONSECUTIVE_FAILURES = 5` (34).
   - `heartbeat_interval_seconds(lease_seconds)` 44-48: `max(1, lease_seconds // 3)` —
     "Renew three times per lease so one missed beat never expires it."
   - `stop()` 200-240: `task.cancel()`; `await asyncio.wait({task})` (settle
     without surfacing its cancellation); re-raises a stored heartbeat failure
     after the stop write.
3. **Startup run reaper** — `RunRecoveryCoordinator(...).recover()` at
   `app.py:893-901` (one-shot at boot; same coordinator reused periodically).
4. **Execution thread pool** — `ExecutionServiceImpl.shutdown()`,
   `execution/service.py:1626-1658`: sets every shutdown event, drains the
   executor via `run_sync_in_worker(_shutdown_executor)`, then gathers
   `_lease_completion_futures` with `return_exceptions=True` and raises a
   `BaseExceptionGroup("Execution lease cleanup failed", failures)`.
5. **Process-wide sync worker pools** — `src/elspeth/web/async_workers.py`:
   `MAX_WORKERS = 16` (14), `MAX_QUEUED = 16` (18), `ADMISSION_CAPACITY` (19),
   `ADMISSION_WAIT_SECONDS = 1.0` (24); auth-audit pool
   `AUTH_AUDIT_MAX_WORKERS = 2`, `AUTH_AUDIT_ADMISSION_CAPACITY = 4` (34-35).
   `async def shutdown_async_workers() -> None` 107-133 drains both pools on a
   private shutdown pool. `run_sync_in_worker` 194 raises
   `AsyncWorkerAdmissionTimeoutError(TimeoutError)` (74-83) when saturated.
   A new worker that does its DB claims via `run_sync_in_worker` shares this
   16+16 admission budget with every request.
6. `ProcessRecovery` — `src/elspeth/web/process_recovery.py:9-32`: one
   shutdown request per process; `request_shutdown()` sends SIGTERM to self
   unless `begin_shutdown()` already ran. This is the escalation every required
   worker uses.

Readiness runner (`app.state.readiness_probe_runner`, closed at 943) and the
readiness cache task (`readiness.py:627`) are request-driven, not periodic.

**No composer-owned lifespan task exists today.** Composer request work is
request-scoped: `_track_compose_inflight` (`sessions/routes/_helpers.py:2442`)
spawns `heartbeat = asyncio.create_task(renew())` (2587) that renews every
`_COMPOSER_HEARTBEAT_SECONDS = 15.0` (2338) against
`_COMPOSER_REQUEST_LEASE_SECONDS = 60` (2340).

### 1.6 Recommended template for the new app-owned worker

Follow `orphan_task` exactly, with the membership heartbeat's `stop()` for
settle semantics:

- **Create** in `_service_lifespan` after `recovery_coordinator.recover()`
  (901) and beside/after `orphan_task` (904): at that point
  `execution_service`, `composer_service`, `session_service`, membership, and
  the progress registry all exist, and the process is a registered member.
  The worker's startup reap of expired `running` rows belongs here too (spec
  §3 "startup/periodic reaper"), mirroring 901→904.
- **Escalation**: done callback identical to 917-922 (a dead required worker →
  `instance_draining.set()` + `process_recovery.request_shutdown()`); bounded
  `OperationalError` retry identical to 437-464 and membership 34/158-176.
- **Shutdown slot**: cancel + drain after `begin_drain()` (932) and before
  `execution_service.shutdown()` (949) / `membership.stop()` (955). Draining
  before `membership.stop()` matters: `stop()` expires this instance's
  membership lease "so peers take over immediately", so owned `running` jobs
  must be settled (or their fences released) before that write. It must also
  finish before `shutdown_async_workers()` (969) if it uses `run_sync_in_worker`.
  Put its drain inside the same nested-`finally` chain so a worker failure
  cannot skip executor/telemetry/pool teardown (the comment at 933-937 is the
  rule).
- **Drain semantics**: signal cancellation to owned turn tasks, then gather
  them (`ExecutionServiceImpl.shutdown` 1626-1658 pattern: join everything,
  then raise a group) — spec §3 "Worker shutdown requests cancellation and
  drains owned tasks".
- **Exposure on app.state**: services are attached as plain attributes
  (`app.state.<name> = ...`); lifespan-built ones are set inside
  `_service_lifespan` (e.g. `app.state.execution_service` 671,
  `app.state.readiness_service` 680). Publish the worker (for the POST route's
  "local owner is signalled directly" cancel path, spec §4) as e.g.
  `app.state.composer_async_worker`. Routes read `request.app.state.<name>`.
- **Lease interval**: session-operation lease is 30 s (`sessions/service.py:4480`)
  and not configurable via WebSettings (`grep -n "session_operation_lease\|lease_seconds" src/elspeth/web/config.py`
  → no hits; control: the same grep on `app.py` finds 1870). Worker renewal
  must be well under 30 s; `heartbeat_interval_seconds(30)` = 10 s is the
  in-tree precedent.

**Test seams the worker must not break** (`tests/unit/web/test_app.py`):
- `test_lifespan_awaits_execution_service_shutdown` 2396 — patches
  `elspeth.web.app.ExecutionServiceImpl` with `_RecordingExecutionService`.
- `test_fatal_periodic_cleanup_requests_recovery_and_preserves_shutdown` 2410 —
  monkeypatches `app_module._periodic_orphan_cleanup` and
  `elspeth.web.async_workers.shutdown_async_workers`; asserts draining set and
  executor+telemetry shutdown still happen once. A new worker test should be
  its twin.
- `test_lifespan_starts_membership_and_drains_as_the_first_act_of_shutdown` 4643 —
  asserts `spy.order == ["start", "begin_drain", "stop"]`.
- Other lifespan tests 1586-2465 run the real lifespan with
  `composer_boot_probe_enabled=False`; a worker that needs a real provider or
  a PG dialect at construction would break them.

### 1.7 Compose-lock registry is request-lazy (worker-relevant)

`_SessionComposeLockRegistry` (`_helpers.py:311-349`) is attached lazily by
`_get_session_compose_lock_registry(request: Request)` (352-364):
```python
if "session_compose_lock_registry" not in request.app.state:
    request.app.state.session_compose_lock_registry = _SessionComposeLockRegistry()
```
The accessor takes a `Request`; the worker has no `Request` (spec §3). Callers:
`messages.py:144`, `guided_plan.py:462`, `proposals.py:283,693`,
`interpretation.py:128,272`. The plan must either create the registry
eagerly (lifespan) or add an `app`-keyed accessor, and keep the same object
for requests and worker so the local lock still serialises guided + freeform
on one process.

### 1.8 Instance-wide provider-turn limit (spec lines 49-52 asks the plan to state this)

Not found. Instrument: `grep -rn "Semaphore\|BoundedSemaphore\|max_concurrent\|concurrent_compos\|MAX_CONCURRENT" src/elspeth/web` → 0 hits.
Positive control: `grep -rln Semaphore src/elspeth` finds 3 files outside web
(`plugins/infrastructure/pooling/executor.py`, `core/security/web.py`,
`plugins/infrastructure/templates.py`). The only compose admission controls
found are the per-session `asyncio.Lock` registry (`_helpers.py:311-364`),
the durable `SessionOperationLease` COMPOSE exclusion, and the per-user
`composer_rate_limit_per_minute` limiter (`app.py:1909-1922`). The
process-wide `run_sync_in_worker` admission (16+16) bounds sync DB work, not
provider turns. So guided/tutorial and async freeform share no existing
instance-wide provider-turn cap.

---

## 2. `WebSettings` (`src/elspeth/web/config.py`)

### 2.1 Model config and fields

- `class WebSettings(BaseModel)` 179; `model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)` 190.
- `composer_timeout_seconds: float = Field(..., gt=0)` — **325** (required, no default).
- `composer_transport_idle_ceiling_seconds: float = Field(default=_DEFAULT_COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS, gt=0, description=...)` — 335-348.
- `composer_transport_headroom_seconds: float = Field(default=_DEFAULT_COMPOSER_TRANSPORT_HEADROOM_SECONDS, gt=0)` — 349-352.
- Defaults: `_DEFAULT_COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS = 300.0` 74,
  `_DEFAULT_COMPOSER_TRANSPORT_HEADROOM_SECONDS = 30.0` 75. The long comment
  53-73 ("Derive it, do not type it", Cloudflare 125 s incident
  elspeth-ad5628ecda) is a "mirrored comment" the spec §3 says to update.
- `_COMPOSER_PLANNING_SECONDS_PER_TURN = 15.0` 81.
- `composer_advisor_timeout_seconds: float = Field(default=60.0, gt=0)` 427.
- `composer_runtime_preflight_timeout_seconds: float = Field(default=5.0, gt=0)` 353.
- Worker-cadence precedents: `orphan_run_max_age_seconds: int = Field(default=3600, ge=60)` 510,
  `orphan_run_check_interval_seconds: int = Field(default=300, ge=30)` 511.

### 2.2 Boot validator (full source, `config.py:1186-1208`)

```python
    @model_validator(mode="after")
    def _validate_composer_timeout_transport_headroom(self) -> WebSettings:
        """Keep composer wall-clock failures ahead of browser/proxy aborts.

        This validates against the DECLARED ceiling, which nothing here can
        measure. It is therefore only as good as that declaration: a ceiling
        naming one hop while another sits in front passes this check and still
        loses the race it exists to win (elspeth-ad5628ecda — see the
        derivation rule on ``_DEFAULT_COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS``).
        When diagnosing a gateway error on a long compose, suspect the declared
        ceiling before the guard.
        """
        max_backend_timeout_seconds = self.composer_transport_idle_ceiling_seconds - self.composer_transport_headroom_seconds
        if max_backend_timeout_seconds <= 0:
            raise ValueError("composer_transport_headroom_seconds must be less than composer_transport_idle_ceiling_seconds")
        if self.composer_timeout_seconds > max_backend_timeout_seconds:
            raise ValueError(
                "composer_timeout_seconds must leave transport idle ceiling headroom: "
                f"got {self.composer_timeout_seconds}s, maximum {max_backend_timeout_seconds}s "
                f"(transport idle ceiling {self.composer_transport_idle_ceiling_seconds}s - "
                f"headroom {self.composer_transport_headroom_seconds}s)"
            )
        return self
```

Decoupling per spec §3: keep 1198-1200 (the ceiling/headroom internal
consistency check — spec "validate that those two settings remain internally
consistent"); remove 1201-1207 (the compose-budget coupling). The comparison
is `>`, so `composer_timeout_seconds == ceiling - headroom` was valid; hence
for every previously-valid config `min(composer_timeout_seconds, ceiling - headroom) == composer_timeout_seconds`,
which is the spec's "every configuration valid before this change keeps the
same guided bound".

### 2.3 Planning warning (keep, spec §3) — `config.py:1210-1233`

`_warn_composer_turn_budget_underfunded`: `fundable_turns = int(self.composer_timeout_seconds // _COMPOSER_PLANNING_SECONDS_PER_TURN)`
(1224); if `< composer_max_composition_turns + composer_max_discovery_turns`
logs `_slog.warning("composer_turn_budget_underfunded", composer_timeout_seconds=..., configured_turns=..., fundable_turns_estimate=..., planning_seconds_per_turn=...)` (1225-1232). Disclosure, not rejection.
Question for the plan: once guided is capped, this warning only describes the
freeform budget; guided turns funded by the capped value are not disclosed.

### 2.4 Env loader — HAND-ROLLED (verified)

`def settings_from_env() -> WebSettings:` `config.py:1538-1606`. It is not
pydantic-settings: it iterates `os.environ`, strips `ELSPETH_WEB__`, lowercases,
and:
- rejects `deployment_aws_region` (1552-1553) and any name not in
  `WebSettings.model_fields` → `RuntimeError(f"Unknown ELSPETH_WEB__ setting: {key}")` (1554-1555);
- JSON-decodes names in `_JSON_COLLECTION_FIELDS` (1438-1453, → tuple) and
  `_JSON_OBJECT_FIELDS` (1455-1457, → dict) (1556-1569);
- literal `"null"` → `None` (1570-1571);
- else `_coerce_env_scalar(value, WebSettings.model_fields[field_name].annotation)` (1572-1573);
  `_coerce_env_scalar` 1515-1535 converts to `int` / `float` by annotation
  (bool passes through), which is what makes `strict=True` int fields settable.
- Region from ambient `AWS_REGION`/`AWS_DEFAULT_REGION` 1575-1582.

**Adding a new setting (e.g. `composer_async_worker_capacity: int`,
`composer_async_poll_after_ms: int`, `composer_async_scan_interval_seconds: float`):**
add only the `WebSettings` field with a default and bounds. No loader edit
is needed for a scalar int/float. Only a tuple/dict field needs an entry in
`_JSON_COLLECTION_FIELDS` / `_JSON_OBJECT_FIELDS`. Guards that will see it:
`extra="forbid"` (190); `settings_from_env`'s `model_fields` check (1554);
`tests/unit/deployment/test_web_settings_exports_resolve.py:98` (every
`ELSPETH_WEB__*` name in tracked `deploy/` + `docs/runbooks` must resolve to a
field); `tests/unit/web/test_config.py:1533-1546` (`_optional_string_field_names`
— any new `str | None` field needs a blank-rejecting validator; not triggered
by int/float).
`ComposerSettings` Protocol (`src/elspeth/web/composer/protocol.py:1506`,
`composer_timeout_seconds` property at 1557) is the structural type
`ComposerServiceImpl` depends on. It does **not** declare the transport ceiling
or headroom (`grep -n "composer_transport\|ceiling" protocol.py` → no hits;
positive control: the same file shows `composer_timeout_seconds` at 1557). If
the composer service computes the guided cap itself, the Protocol needs new
properties. No test pins ComposerSettings/WebSettings alignment
(`grep -rn "ComposerSettings\b" tests` → no hits).

### 2.5 Other readers of `composer_timeout_seconds`

Instrument: `grep -rn composer_timeout_seconds src/elspeth --include=*.py`.
Outside `src/elspeth/web`: none (`exit=1`, negative). Inside (positive,
config.py:325 among them):

| Site | Surface | Sync/async after cutover |
|---|---|---|
| `app.py:2334` | `/api/system/status` publishes it; SPA derives compose abort ceiling (comment 2329-2333; `frontend/src/App.tsx:402-403`, `config/composer.ts:5,15`) | both |
| `composer/service.py:2542` `self._timeout_seconds = settings.composer_timeout_seconds` | single per-process attribute, see §3 | both |
| `sessions/routes/composer/pipeline_settlement.py:273` | `PipelineCommitConfig(timeout_seconds=...)` in `settle_pipeline_proposal_under_compose_lock` (173); called from `settle_auto_commit_intent` (447→473; freeform `messages.py:808`, `compose.py:555`) **and** `proposals.py:318` (proposal decisions — synchronous, outside cutover) | mixed |
| `sessions/routes/composer/guided_chat_atomic.py:729,790,836,874,897` | `run_guided_chat_provider_attempt` (663) → `/guided/chat` | sync (guided) |
| `sessions/routes/composer/guided.py:5042` | `PipelineCommitConfig(timeout_seconds=...)` inside `/guided/respond` (route at 2935) | sync (guided) |
| `sessions/routes/_helpers.py:3143` | 422 convergence body `timeout_seconds` for `convergence_wall_clock_timeout` | both |
| `config.py:1201-1228` | validators | — |

`PipelineCommitConfig.timeout_seconds: float` is declared at
`composer/pipeline_commit.py:284`; consumed as a deadline at 432.

---

## 3. Where guided routes get their compose budget (the cap surface)

The three guided routes and their `_track_compose_inflight` mounts:
`/guided/plan` `guided_plan.py:333` (dep 338), `/guided/respond`
`guided.py:2935` (dep 2941), `/guided/chat` `guided.py:5866` (dep 5872).
Freeform mounts: `/messages` `messages.py:112` (dep 124), `/recompose`
`compose.py:86-97`.

Budget sources reachable from them:

1. **`ComposerServiceImpl._timeout_seconds`** (`service.py:2542`) — every reader:
   `grep -n "self\._timeout_seconds" service.py` →
   2719 (`_run_one_turn_for_test`), 4099 (`explain_run_diagnostics`),
   4163 (`compose`: `deadline = asyncio.get_event_loop().time() + self._timeout_seconds`),
   4459 (`plan_guided_full_pipeline` 4381 → `PlannerModelConfig(timeout_seconds=...)`),
   4873 (`plan_guided_pipeline` 4543), 5382 (`_plan_and_stage_empty_pipeline`
   5289, reached from `compose` at 4218). Guided callers:
   `guided_plan.py:519` → `plan_guided_full_pipeline`;
   `guided.py:4276, 4770, 5451` → `plan_guided_pipeline`. Neither planner
   method takes a timeout/deadline parameter; `compose(...)` (4112-4126) takes
   none either. `PlannerModelConfig.timeout_seconds` is at
   `pipeline_planner.py:684`, consumed at 3775.
2. **Direct settings reads**: `guided_chat_atomic.py` ×5 and `guided.py:5042`
   (table above).

Consequences for the plan:
- A guided cap `min(composer_timeout_seconds, ceiling - headroom)` cannot be
  done by changing one setting: the service attribute is shared by freeform
  `compose` and the guided planners. It needs either a per-call
  budget/deadline parameter on `plan_guided_full_pipeline` /
  `plan_guided_pipeline`, or a second computed attribute
  (e.g. `_sync_timeout_seconds`) read by the guided methods, plus the six
  direct route reads switched to one computed value
  (e.g. a `WebSettings` property `composer_sync_timeout_seconds`).
- The freeform worker needs "only the remaining time" (spec §3), but
  `compose()` computes its own deadline from `self._timeout_seconds` (4163)
  with no deadline parameter — the worker cannot pass remaining budget
  without a signature change.
- **Other synchronous consumers not named in the spec** also inherit a
  budget that may now exceed the transport ceiling:
  `/api/runs/{run_id}/diagnostics/evaluate` (`execution/routes.py:1389-1393`,
  calls `composer.explain_run_diagnostics` at 1474, which waits
  `timeout=self._timeout_seconds` at `service.py:4099`); and proposal
  decisions via `proposals.py:318` → `pipeline_settlement.py:273`. These are
  synchronous after the cutover and need the same cap (or an explicit ruling).
- The SPA reads one `composer_timeout_seconds` from `/api/system/status`
  (`app.py:2334`) for its abort ceiling; guided client calls would then wait
  the full async budget while the server caps at the transport-safe value.
  The server still answers first, so this is not a correctness failure. It is
  stale published data, and ECS README lines 1030-1033 ("no frontend change is
  needed when the envelope moves") becomes stale too. The 422 body at
  `_helpers.py:3143` reports the uncapped setting for a guided wall-clock
  timeout.

## 4. `tutorial_service.py` use of the transport ceiling

- `composer/tutorial_service.py:413`:
  `run_timeout_seconds = settings.composer_transport_idle_ceiling_seconds - settings.composer_transport_headroom_seconds`
  → passed to `_wait_for_terminal_run(session_service, run_id, timeout_seconds=run_timeout_seconds)` (414-418).
- `_wait_for_terminal_run` 474-490: polls `session_service.get_run(run_id)`
  every `_TUTORIAL_RUN_POLL_SECONDS = 0.25` (70); past the deadline raises
  `HTTPException(504, {"error_type": "tutorial_run_timeout", ...})` (484-488).
- It does not read `composer_timeout_seconds` (grep of the file finds none).
  This is the "synchronous run wait" the spec keeps the transport settings for.
  It stays valid as long as `ceiling - headroom > 0`, which the retained check
  at `config.py:1199-1200` guarantees.
- Test: `tests/unit/web/composer/test_tutorial_service.py:688-693` builds
  settings with `composer_timeout_seconds=120.0, ceiling=300.0, headroom=30.0`
  and captures the `timeout_seconds` passed (monkeypatched
  `_wait_for_terminal_run`).

## 5. Deployment mirrors of the coupling

### 5.1 AWS ECS Terraform (plan-time cap)

- `deploy/aws-ecs/terraform/modules/scenario/variables.tf:611-620`
  `variable "alb_idle_timeout_seconds"` default 900, validation `[60, 4000]` (617-619).
- `variables.tf:622-638` `variable "composer_timeout_seconds"` default 840;
  comment 627-633; validation:
  ```hcl
  condition     = var.composer_timeout_seconds > 0 && var.composer_timeout_seconds <= var.alb_idle_timeout_seconds - 30
  error_message = "composer_timeout_seconds must be in (0, alb_idle_timeout_seconds - 30]: the web app rejects a wall clock inside the transport headroom, because the ALB would abort with an opaque 504 before the composer reported its honest 422."
  ```
  (635-636). This is the ECS plan-time cap the spec says to update.
- `modules/scenario/locals.tf:472-482`: comment "coupled three-leg chain"
  (472-480); `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS = tostring(var.alb_idle_timeout_seconds)` (481);
  `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS = tostring(var.composer_timeout_seconds)` (482).
  No headroom override is shipped.
- `modules/scenario/network.tf:159` `idle_timeout = var.alb_idle_timeout_seconds`.
- `deploy/aws-ecs/terraform/README.md` "### Composer wall-clock budget"
  993-1040: three-leg chain 996-1008 (names
  `WebSettings._validate_composer_timeout_transport_headroom` at 1002, the
  plan-time cap 1004-1008), history 1010-1024, 422 behaviour 1026-1029, SPA
  derivation 1031-1032, test enforcement 1033-1037.

### 5.2 Azure Container Apps (mirrored coupling, same shape)

- `deploy/azure-container-apps/scripts/validate-workload-parameters.jq:51`
  `(.composerTransportIdleCeilingSeconds | positive_integer and . <= 240)` and
  **:54 `(.composerTimeoutSeconds <= (.composerTransportIdleCeilingSeconds - 30))`**.
  Lines 19-20 make `..._TRANSPORT_IDLE_CEILING_SECONDS` / `..._TRANSPORT_HEADROOM_SECONDS`
  reserved (not overridable via `extraEnvironment`); 25 reserves `COMPOSER_TIMEOUT_SECONDS`.
- `deploy/azure-container-apps/workload.bicep:80-83` `param composerTransportIdleCeilingSeconds int` with `@maxValue(240)` and
  description "ingress request timeout is a fixed 240 seconds";
  `93-95` `param composerTimeoutSeconds int` with description
  "Composer request budget; must leave the runtime-required headroom below the transport ceiling." (93);
  env wiring 325-331.
- Param files: `workload.production.bicepparam:25,28` and
  `workload.acceptance.bicepparam:23,26` (ceiling 210, timeout 180);
  `application.example.json:4` (`composerTimeoutSeconds: 180`).
- Scripts requiring the ceiling: `scripts/resolve-workload-parameters.sh:11,79,89`,
  `scripts/bootstrap-acceptance.sh:15`, `scripts/acceptance.sh:127,232-234`
  (`-le 240 || fail transport_ceiling_invalid`), 254.
- Docs: `deploy/azure-container-apps/README.md:137-139,150-152`;
  `docs/runbooks/azure-container-apps-deployment.md:80-83,212-213,479,611`;
  `docs/runbooks/azure-container-apps-cold-install.md:118-121,136-137,397-399`
  (399: "The timeout must fit below the transport ceiling with the configured headroom.").
- ACA ingress is fixed at 240 s, so on ACA the async change is what makes a
  compose budget above 210 possible at all. The jq line 54 is the ACA
  equivalent of the ECS plan-time cap. The spec names only "ECS plan-time cap";
  the plan should state whether jq:54 and the bicep description move too.

### 5.3 Other deployment profiles pinning the inequality or the pair

- Docker Compose: `deploy/compose/web-postgres.yaml:16-19` (timeout 300,
  ceiling 360, headroom 30; comment "Matched to nginx.conf"),
  `deploy/compose/nginx.conf:33-34` `proxy_read_timeout 360s; proxy_send_timeout 360s;`.
- linux-systemd: `deploy/linux-systemd/elspeth-web.env.example:17-24` (180/240/30).
- `deploy/elspeth-web.env:71` (660.0) is gitignored (`.gitignore:302 deploy/**/*.env`), not release content.
- `docs/guides/docker.md:64-66` ("Composer timeout is five minutes, with a
  six-minute declared transport ceiling"), 80, 350-356 (headroom prose).
- `docs/reference/environment-variables.md:161` documents only
  `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` ("Required positive Composer timeout
  in seconds."). There are **no rows** for the transport ceiling/headroom
  (`grep -in "transport\|headroom\|idle"` → only line 161 matched, via
  `COMPOSER_TIMEOUT`) or the orphan intervals. Its test
  `tests/unit/docs/test_deployment_platform_docs.py:270-282`
  (`test_environment_reference_documents_deployment_state_settings`) pins only
  deployment-state names, not composer settings. Adding rows for new worker
  settings is optional hygiene, not gate-forced.

## 6. Tests that pin the coupling (exact)

| Test | Anchor | Pins |
|---|---|---|
| `TestWebSettingsValidation.test_composer_timeout_must_leave_transport_headroom` | `tests/unit/web/test_config.py:242-250` | `pytest.raises(ValidationError, match="transport idle ceiling")` for timeout 300 on default 300/30 — **must be inverted/rewritten** |
| `TestWebSettingsValidation.test_composer_timeout_allows_explicit_larger_transport_ceiling` | `test_config.py:252-263` | 300 accepted with ceiling 360 |
| `test_composer_timeout_zero_rejected` | `test_config.py:232-240` | `gt=0` (keep) |
| `test_underfunded_turn_budget_warns_with_fundable_estimate` / `test_fundable_turn_budget_does_not_warn` | `test_config.py:278-307`, `309-323` | planning warning (keep) |
| no test for `ceiling - headroom <= 0` | — | instrument: `grep -rn "must be less than composer_transport_idle_ceiling_seconds\|headroom_seconds must be less" tests` finds none (`exit=1`; positive control: the same pattern over `src` matches `config.py:1200`; the only `transport idle ceiling` match in tests is test_config.py:243). The retained internal-consistency check is unpinned; the plan should add one |
| `test_composer_wall_clock_fits_under_the_app_guard_and_the_alb` | `tests/unit/deployment/test_aws_ecs_terraform_package.py:3365-3468` | no headroom override shipped (3394-3397); headroom from `WebSettings.model_fields[...].default` (3398); ceiling env wired to ALB var verbatim (3400-3408); timeout env wired (3409-3412); defaults parsed (3414-3430); **plan-time cap regex `<=\s*var\.alb_idle_timeout_seconds\s*-\s*(?P<headroom>...)` must exist and equal WebSettings headroom (3432-3446)**; network idle_timeout wired (3448-3453); **`timeout_seconds + headroom <= alb_idle_seconds` (3455-3459)**; floor `timeout_seconds >= 840` (3463-3467, keep) |
| `test_shipped_nginx_scopes_forward_websocket_upgrade_and_match_transport_budget` → `_assert_websocket_contract` | `tests/unit/deployment/test_nginx_websocket_template.py:47-69,72-73` | nginx proxy timeouts == compose ceiling (61-62) **and `timeout <= ceiling - headroom` (63-65)** |
| `test_validator_rejects_mutated_resolved_parameters` | `tests/unit/deployment/test_azure_container_apps_launch_inputs.py:207-235` | param case `("composerTimeoutSeconds", 181)` at 214 (ceiling 210 from env at 84) must be rejected by jq:54; `("composerTransportIdleCeilingSeconds", 241)` 213; headroom override rejected 215 |
| `test_transport_ceiling_is_bound_below_the_ingress_timeout` | `tests/unit/web/test_azure_container_apps_runbook_contract.py:248-258` | runbook text: ceiling var present, `-le 240`, "the example parameter file uses 210" |
| `test_web_overlay_sets_production_composer_defaults` | `tests/unit/deployment/test_compose_bundle.py:289-298` | literal values 300/360/30 |
| `test_linux_environment_example_sets_production_composer_limits` | `tests/unit/deployment/test_linux_systemd_bundle.py:111-119` | literal values 180/240/30 |
| docker guide | `tests/unit/docs/test_docker_guide_release_examples.py:68` | `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=360.0` in the guide |
| system status | `tests/unit/web/test_app.py:760-780` | `/api/system/status` returns `composer_timeout_seconds` (300 with ceiling 400) |
| ACA bundle | `tests/unit/deployment/test_azure_container_apps_bundle.py:534,587,828` | doctor settings mirror `composerTimeoutSeconds`; env wiring of ceiling |
| testcontainer | `tests/testcontainer/web/test_composer_progress_quota_lock_order_postgres.py:75` | constructs settings with ceiling 360 |

Instrument for the inequality pins:
`grep -rn "ceiling - headroom\|<= ceiling\|timeout <= \|timeout_seconds + headroom\|headroom <=" tests`.
Positive: finds nginx:65 and ecs:3455. The other hits are unrelated
(`tests/e2e/recovery/harness.py:276,332`). The ACA jq inequality is pinned
only by the mutated-parameter case, not by a textual assertion.

## 7. Cross-seam gate the worker placement will hit (for the plan author)

`tests/unit/architecture/test_session_db_mutation_authority.py` is a
fail-closed, AST-fingerprinted inventory of every sessions-DB writer (header
1-8). `test_sessions_metadata_table_policy_is_exact_and_protected` (11534)
requires a `TablePolicy` for every sessions table, and writer identities carry
`line=` plus fingerprints (e.g. membership writers 12241-12331). `app.py` is
already listed as a **non-writer** (12330, for `web_instances`). A new
`composer_async_operations` table and its claim/renew/settle/reap writers need
a named authority. `RepositoryWebInstanceMembershipAuthority` (called via
`run_sync_in_worker` from `membership_lifecycle.py`) is the precedent for a
global, lifespan-driven writer. Keep DB writes out of `app.py`.

## 8. Old plan staleness (`docs/plans/2026-09-20-composer-async-operations.md`)

§3 (139-178) still says "the five route modules" (143) and does not cover the
guided cap, ACA `validate-workload-parameters.jq:54`, the nginx/compose
inequality test, the lazily attached compose-lock registry, or the
`explain_run_diagnostics` / proposal-decision synchronous consumers. Its step 5
(167-177) otherwise matches the amended spec's config decoupling.
