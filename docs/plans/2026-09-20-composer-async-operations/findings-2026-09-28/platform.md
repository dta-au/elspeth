# Seam: app lifespan, test and gate infrastructure (re-survey 2026-09-28)

Read-only re-survey for re-basing the composer async-operations plan under the 2026-09-28 rulings
(`panel-2026-09-28/RULINGS.md`, `RECOMMENDATION.md`: B′, operation_id IS the ingress key, positive
fence predicate, no events table, forensic columns + delete guard, refuse a moved base).

- Tree: main checkout, `release/0.8.1`, `git rev-parse HEAD` = `1effedab2e0af7e09a5e8b30c66bc46ceaa130d5`
  (the pinned tip) before and after the survey. `git status --short src tests deploy evals scripts config`
  printed nothing before and after. HEAD did not move, so no anchor drifted during the survey.
- Old findings compared: `findings/app-lifespan-config.md` and `findings/tests-and-gates.md` (measured at
  `d479eb2b4`, 382 commits behind this tip; `git merge-base --is-ancestor d479eb2b4 HEAD` = true).
- Every anchor is `path:line` at `1effedab2`. Scratch instruments are in the session scratchpad
  (`post_sites.py`, `url_census.py`, `at_rev.py`, `mutation_control.py`, `loop_probe.py`). They are not repo tools.
- A sibling full suite from another worktree was running (load average about 6). I ran one serial focused
  selection (29 tests) and small scripts only. I did not run a broad suite or the lint corpus.

---

## 1. CURRENT FACTS

### 1(a) App lifespan (`src/elspeth/web/app.py`)

**Nesting.** `lifespan(app)` is at `app.py:580-595`. It starts `auth_audit_recorder` (`:585`), enters
`_service_lifespan` (`:586`), and in `finally` runs the auth-audit finalizer and then the session-engine
finalizer (`:591-595`). `_service_lifespan` is at `app.py:598-969`. `FastAPI(..., lifespan=lifespan)` is at `:1357`.

**Built in `_create_app` before the lifespan runs:**

- `SessionServiceImpl(...)` is at `app.py:1826-1851` with `owner_instance_id=instance_id` (`:1847`). No
  `session_operation_lease_seconds` is passed, so the default is 30 s (`sessions/service.py:566`, validated at
  `:595-596`, property `:626-627`).
- `process_recovery = ProcessRecovery()` is at `:1864-1865`.
- Membership (`:1863-1876`): PG → `RegisteredWebInstanceMembership(..., lease_seconds=session_service.session_operation_lease_seconds, process_recovery=process_recovery)`;
  otherwise → `SingleProcessWebInstanceMembership()`. `app.state.instance_draining = web_instance_membership.draining` (`:1876`).
- `app.state.composer_service = ComposerServiceImpl(...)` is at `:1885-1895`. **New since the old findings:**
  `app.state.interpretation_surfacing`, `app.state.planning_application` and `app.state.schema_disclosure` are
  lifted off the composer's private attributes (`:1896-1898`, commit `c38d50da6`).
- The dialect split for cross-process UI state is at `:1901-1924`: PG → `DatabaseComposerProgressRegistry(SessionComposerProgressAuthority(...))`, `SharedRateLimiter`s; SQLite → process-local `ComposerProgressRegistry()` and `ComposerRateLimiter`s.

**`_service_lifespan` startup order (exact):**

| Step | Line | What |
|---|---|---|
| 1 | 613-621 | settings, state_mode, `landscape_url`, `session_service` |
| 2 | 625 | `await app.state.blob_service.reconcile_inline_custody_publications()` |
| 3 | 634-642 | SSO runtime resolution (`SystemExit` on discovery failure) |
| 4 | 648 | `await app.state.web_instance_membership.start()` (register + heartbeat task) |
| 5 | 652-671 | `ProgressBroadcaster`, `ExecutionServiceImpl(...)` → `app.state.execution_service` |
| 6 | 680-690 | `app.state.readiness_service = ReadinessService(...)` |
| 7 | 706-736 | payload store, library authority, share signer, shareable review service |
| 8 | 741 | `await _boot_prime_openrouter_catalog(settings)` |
| 9 | 743-875 | composer boot probes, if `settings.composer_boot_probe_enabled` |
| 10 | 884-890 | catalog snapshot id onto `app.state` and the executor |
| 11 | 892-900 | `RunRecoveryCoordinator(...)`; `await recovery_coordinator.recover()` (startup reaper) |
| 12 | 904-915 | `orphan_task = asyncio.create_task(_periodic_orphan_cleanup(...))` |
| 13 | 917-922 | done callback `_recover_process_on_orphan_failure`: `instance_draining.set()` + `process_recovery.request_shutdown()` |
| 14 | 924-925 | `try: yield` |

Steps 12-13 sit **outside** the `try` at `:924`. An exception raised between `:904` and `:924` would leave
`orphan_task` running and uncancelled.

**Shutdown order (`finally`, `app.py:926-969`), each later step in a nested `try/finally`:**

1. `app.state.process_recovery.begin_shutdown()` — `:927`
2. `await app.state.web_instance_membership.begin_drain()` — `:932` (never raises; sets draining first, `membership_lifecycle.py:179-191`)
3. `orphan_task.cancel()`, then `await` under `contextlib.suppress(CancelledError)` — `:938-941` (a task that died re-raises here)
4. `app.state.readiness_probe_runner.close()` — `:943`
5. `await execution_service.shutdown()` — `:949`
6. `await app.state.web_instance_membership.stop()` — `:955` (row `stopped`, lease expired at once so peers take over)
7. `await app.state.operator_telemetry.shutdown()` — `:962`
8. `await shutdown_async_workers()` — `:967-969`

`git diff d479eb2b4 HEAD -- src/elspeth/web/app.py` touches only `:1656` (a comment), `:1896-1898` (above) and
the HTTPException-renderer comment at `:2224-2230`. The lifespan block itself is unchanged apart from a
one-line shift.

**The slot for a new worker and reaper (measured constraints, not a design):**

- Construct the worker and run the startup reap **between `:900` and `:904`**, after `recover()` and before
  `orphan_task` exists. A startup-reap failure there fails boot without leaking the orphan task. After `:904`
  and before the `try` at `:924`, a raise leaks `orphan_task`.
- Start the scan loop after `:922`. Use a done callback identical to `:917-921`.
- Stop the worker after `:941` (the orphan await) and before `:949` (executor shutdown). This also puts it before
  `membership.stop()` (`:955`), which expires this instance's lease so peers take over "immediately", and before
  `shutdown_async_workers()` (`:969`). The latter matters if the worker uses `run_sync_in_worker`. The stop must
  sit inside the same nested-`finally` chain (the rule is the comment at `:933-937`).
- **Drain is bounded by settlement, not by cancel.** The routes join their settlement continuation through
  `_join_freeform_owned_task` (`sessions/routes/_helpers.py:1428-1450`). It loops on `asyncio.shield(task)` and
  calls `owner.uncancel()` on every cancellation of the owning task (`:1440-1444`). Its callers are
  `messages.py:251,612` and `compose.py:413`. If the worker's turn keeps that join, `stop()` cancelling the
  turn task will not shorten the drain.

**`instance_draining` is a `threading.Event`.** It is typed at `coordination/membership_lifecycle.py:65-71`
(`self._draining = threading.Event()`, property `draining`). `readiness.py:663-664` raises `TypeError` unless it
is an exact `threading.Event`. Consumers: `readiness.py:391,432,653-665` and `app.py:2284`. Grep over
`src/elspeth` found no other readers.

**`process_recovery`** is at `src/elspeth/web/process_recovery.py:9-31`. `request_shutdown()` sends `SIGTERM` to
the process once, unless `begin_shutdown()` has already run. It is shared by the orphan task (`app.py:920`) and
the membership heartbeat (`membership_lifecycle.py:142-145`).

**Other process-lifetime machinery (unchanged since `d479eb2b4`, per `git diff --stat`):**

- The membership heartbeat is at `membership_lifecycle.py:135-176`:
  - `heartbeat_interval_seconds` = `max(1, lease//3)` (`:45-49`);
  - the bounded `OperationalError` retry is `_HEARTBEAT_MAX_CONSECUTIVE_FAILURES = 5` (`:34`);
  - `stop()` is at `:193-225`.
- The orphan sweeper is at `app.py:388-474`, with `_ORPHAN_CLEANUP_MAX_CONSECUTIVE_FAILURES = 5` at `:378`.
- `ExecutionServiceImpl.shutdown()` is at `execution/service.py:1549-1582`. It sets the shutdown events, drains
  the executor via `run_sync_in_worker`, gathers the lease completions, and raises
  `BaseExceptionGroup("Execution lease cleanup failed", …)`.
- `async_workers.py` has `MAX_WORKERS = 16` (`:14`), `MAX_QUEUED = 16` (`:18`), `ADMISSION_CAPACITY` (`:19`) and
  `ADMISSION_WAIT_SECONDS = 1.0` (`:24`). `shutdown_async_workers` is at `:107`, `run_sync_in_worker` at `:194`,
  and `AsyncWorkerAdmissionTimeoutError` at `:76`.

**Instance-wide provider-turn cap: not found.**
`grep -rn "Semaphore\|BoundedSemaphore\|max_concurrent\|concurrent_compos\|MAX_CONCURRENT" src/elspeth/web` → 0 hits.
Positive control: `grep -rln Semaphore src/elspeth` → 3 files outside web
(`plugins/infrastructure/pooling/executor.py`, `plugins/infrastructure/templates.py`, `core/security/web.py`).

**Async-operation symbols: not found.**
`git grep "composer_async\|ComposerAsyncWorker\|composer_async_operations\|ComposerAsyncOperationAuthority\|composer_sync_timeout_seconds" -- src tests`
→ 0 hits. Positive control: `session_operation_receipts` has 40 hits in `sessions/models.py`.

**The compose lock registry is still request-lazy.** `_SessionComposeLockRegistry` is at `_helpers.py:217-255`.
`_get_session_compose_lock_registry(request)` (`:258-270`) attaches it to `app.state` on first use. Its callers
are `messages.py:162`, `composer/compose.py:117`, `composer/proposals.py:280,689`, `interpretation.py:128,269`,
`sessions.py:857`, `composer/state.py:744,877,1080` and `execution/routes.py:1456`. No `app`-keyed accessor exists.

**The request lifecycle dependency is still route-owned.** `_track_compose_inflight` (`_helpers.py:2556-2762`) is
mounted only at `messages.py:142` and `composer/compose.py:102`. `surface` is now the one-valued
`Literal["freeform"] = "freeform"` (`:2599`). The knobs are `_COMPOSER_HEARTBEAT_SECONDS = 15.0` (`:2452`),
`_COMPOSER_REQUEST_LEASE_SECONDS = 60` (`:2454`) and `_COMPOSER_HEARTBEAT_TIMER` (`:2477`).
`_cancel_on_client_disconnect` (`:2324-2449`) now has exactly two production users, `messages.py:584` and
`compose.py:395` (`git grep` over `src`).

**Lifespan test seams (`tests/unit/web/test_app.py`):**

- `TestLifespanShutdown` (class `:1646`):
  - `test_lifespan_awaits_execution_service_shutdown` (`:2396-2407`) patches `elspeth.web.app.ExecutionServiceImpl`
    with `_RecordingExecutionService` (`:338`).
  - `test_fatal_periodic_cleanup_requests_recovery_and_preserves_shutdown` (`:2410-2445`) monkeypatches
    `app_module._periodic_orphan_cleanup`, `elspeth.web.async_workers.shutdown_async_workers` and
    `elspeth.web.process_recovery.os.kill`. It asserts `instance_draining` is set and that the executor and
    telemetry shut down exactly once.
- `TestWebInstanceMembershipWiring` (class `:4632`): `test_lifespan_starts_membership_and_drains_as_the_first_act_of_shutdown`
  (`:4643-4674`) asserts `spy.order == ["start", "begin_drain", "stop"]` (`:4673`).
- Lifespan tests run the real lifespan with `composer_boot_probe_enabled=False`: 14 occurrences, for example
  `:1652`, `:2397`, `:4665`.

All three named tests passed in the focused run (§1(e)).

### 1(b) Route-test builders, clients and event-held fakes

**`SyncASGITestClient`** (`tests/unit/web/_sync_asgi_client.py:14-96`; unchanged since `d479eb2b4`):

- `request()` (`:42-75`) builds a fresh `AsyncClient(ASGITransport(...))` inside `anyio.run(send)`. From async
  code it does the same on a helper thread (`:59-75`).
- `__enter__`/`__exit__` (`:31-40`) do nothing, so no lifespan runs.
- Re-measured today with a throwaway FastAPI app whose POST spawns `asyncio.create_task`, driven through the
  shim: `{'done': True, 'cancelled': True, 'ran': False} distinct loops: 2`. A background task created in one
  request is cancelled when that request's loop ends, and it never runs.

**Builders (none has a lifespan):**

| Builder | Anchor | Service | `app.state` notes | Client |
|---|---|---|---|---|
| `_route_client` | `tests/unit/web/conftest.py:104-163`; fixtures `test_client` `:167-179`, `closed_local_app` `:202-204` | `FencedSessionServiceHarness` (`:111`) | `composer_service=None`, `composer_progress_registry=None` (`:156`); **no `interpretation_surfacing`** | `SyncASGITestClient as TestClient` (`:58`) |
| `_make_app` | `tests/unit/web/sessions/test_routes.py:775-865` | `FencedSessionServiceHarness` | `ComposerProgressRegistry()`, `interpretation_surfacing` stub (`:847-849`), `_ExecutionServiceStub` | `TestClient` = shim (`test_routes.py:109`) |
| `_make_progress_route_app` | `test_routes.py:739-772` | `_ProgressRouteSessionService` (`:501`) | `interpretation_surfacing` stub (`:763-765`) | `httpx.AsyncClient(ASGITransport)` in the heartbeat tests |

`_make_app` is imported by **14** other test files. This was measured by AST: an `ImportFrom` of
`tests.unit.web.sessions.test_routes` naming `_make_app`, over `git grep -l -w _make_app -- tests`, which
resolves multi-line imports. The known-positive `test_freeform_route_custody.py` is found. The 14 are:

- `test_freeform_route_custody.py`, `test_recompose_admission_refused.py`, `test_freeform_mode_persistence.py`,
  `test_advisor_recovery_routes.py` and `test_state_reload_suggestions.py`;
- the integration `test_freeform_required_controls.py`;
- the testcontainer `test_composer_splice_concurrency.py`;
- seven `routes/composer/test_*` files.

`_make_progress_route_app` is imported by `test_compose_heartbeat_renewal.py` and `test_recompose_heartbeat_cancel.py`.

**Composer fakes (duck-typed `compose`).** The production signature is
`compose(self, message, messages, state, session_id=None, current_state_id=None, user_id=None, progress=None, user_message_id=None, session_operation_context=None, completion_gates=None)`
(`web/composer/protocol.py:1622-1634`). `guided_terminal` is gone.

| Fake | Anchor | Holds how |
|---|---|---|
| `_make_composer_mock` | `test_routes.py:312-325` | `SimpleNamespace` + `AsyncMock(spec=ComposerService.compose)`; returns at once |
| **`_BlockingRecordingComposer`** | `test_routes.py:413-454` | `asyncio.Event`s `first_call_started`, `second_call_started`, `release_first_call` (`:418-420`); records `calls` |
| `_ProgressAwareComposer` | `test_routes.py:457-498` | asserts `session_operation_context` and `progress`, then publishes one event; subclassed by `_AuditedComposer` in `test_freeform_route_custody.py:28` |
| `_HangingComposer` (inline) | `test_routes.py:4935-4950` inside `test_client_disconnect_cancels_compose_turn` (`:4913-5024`) | parks forever; records `cancelled` |
| `_ParkedComposer` (inline) | `test_routes.py:5116-5120` inside `test_composer_progress_reports_inflight_request_count` (`:5095-5146`) | `compose_started.set(); await release.wait()` |
| `_HangingComposer` | `tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py:31-46`; inline copy at `test_compose_heartbeat_renewal.py:374` | parks forever |
| `_FakeHeartbeatClock` / `_ScriptedRegistry` / `_run_fake_route` | `test_compose_heartbeat_renewal.py:56-66`, `:70-104`, `:137-183` | fake clock injected through `_helpers._COMPOSER_HEARTBEAT_TIMER` |

**No shared fixture home exists.** `grep -rln "class .*Composer" tests/fixtures tests/helpers` → 0 hits. The
positive control is the same pattern matching `test_routes.py:413`. All held fakes are module-local to
`test_routes.py` or inline.

**The one-loop pattern that exists today** is
`test_send_message_serializes_concurrent_requests_per_session` (`test_routes.py:5899-5943`,
`@pytest.mark.asyncio`, one `AsyncClient(ASGITransport)`). It spawns two sends, proves the second is held behind
the compose lock (`wait_for(second_call_started, 0.3)` raises `TimeoutError`), releases the first, and asserts
both return 200 (`:5929-5930`).

**POST sites to `/messages` or `/recompose` (AST instrument, per file).**

Instrument: `post_sites.py` walks `git ls-files 'tests/*.py' 'evals/*.py'` and counts the following when the URL
template ends in `/messages` or `/recompose` (optional `?query`):

- `.post(url)` / `.post(url=…)`;
- `.request("POST", url)` / `.stream("POST", url)`;
- `step(name, "POST", url)`;
- raw-ASGI or `Request` scope dicts with `"method": "POST"`.

The URL may be a literal, an f-string, a `+` concatenation, a name bound by a string assignment in the enclosing
function or module (variable-held), or a call to a module helper that returns such a string.

Controls:

- **Self-test** (6 must-hit, 4 must-miss): hits for literal, variable-held, helper-returned, `request("POST", concat)`,
  ASGI dict and `step`; misses for `GET /messages`, an ASGI `GET` scope, `POST /fork` and `POST /messages/extra`.
  Result: `selftest OK`.
- **Known positive:** it counts `test_send_message_serializes_concurrent_requests_per_session` (`test_routes.py:5912`)
  and the raw-ASGI drive in `test_client_disconnect_cancels_compose_turn` (`test_routes.py:4982`).
- **Known negative on the live tree:** a separate census of every string ending `/messages|/recompose`
  classified 46 `GET`-shaped sites (`.get` 23, `_get` 23), 20 `endswith`, 6 `Compare` and the audit-log
  `request_path=` keywords as *not* POST sites. None was counted.
- **Instrument reproduction:** the same instrument at `d479eb2b4` gives `post/literal = 131`, exactly the old
  findings' figure.
- **Unresolved POST calls:** 44 whose URL could not be resolved to a string. Each was read. They are proposal
  `accept` endpoints (tuple-unpacked from `_create_canonical_pipeline_route_proposal`, `test_routes.py:1237-1357`),
  interpretation `_post(url)` wrappers, respx `router.post(url)`, Azure Search, the fake IdP and
  `_sync_asgi_client.py:81`. None targets the two routes.

Test sites (`tests/` only; the eval driver is listed under 1(c)):

| Sites | messages | recompose | File | Kind breakdown |
|---:|---:|---:|---|---|
| 115 | 83 | 32 | `tests/unit/web/sessions/test_routes.py` | 114 literal `.post`, 1 raw-ASGI route drive (`:4982`) |
| 18 | 13 | 5 | `tests/unit/web/sessions/test_freeform_route_custody.py` (**new**, `8630db9b8`) | 6 literal, **12 variable-held** (`path = f".../messages"` at `:237,241,277,281,451,533,537,559`) |
| 8 | 7 | 1 | `tests/integration/web/composer/test_freeform_planner_failure_translation.py` | literal |
| 4 | 4 | 0 | `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py` | 3 literal, 1 raw `Request` scope driving the dependency directly (`:146`) |
| 2 | 2 | 0 | `tests/unit/evals/composer_battery/test_drive_battery_transport.py` | `:93` `request("POST")` against the eval fake server; `:98` is a recorded-request expectation dict |
| 2 | 0 | 2 | `tests/unit/web/sessions/routes/test_recompose_heartbeat_cancel.py` | literal |
| 2 | 1 | 1 | `tests/unit/web/sessions/test_freeform_mode_persistence.py` | literal |
| 2 | 1 | 1 | `tests/unit/web/sessions/test_recompose_admission_refused.py` | literal |
| 1 | 1 | 0 | `tests/integration/web/composer/parity/test_repair_and_deferral.py` | literal |
| 1 | 1 | 0 | `tests/testcontainer/web/test_composer_splice_concurrency.py` (`:225`) | literal |
| 1 | 1 | 0 | `tests/unit/web/test_app.py` (`:3483`, a 422 redaction test) | literal |
| 1 | 1 | 0 | `tests/unit/web/test_composer_exception_handlers.py` (`:59`) | a `Request` double for an exception handler, not a drive |
| **157** | **115** | **42** | **12 files** | |

The sites fall in 92 `test_*` functions for `messages` and 41 for `recompose` (a function with both is counted
in both). Pytest parametrisation multiplies the runtime ids. For example,
`test_gateway_error_has_safe_failed_progress_and_one_audit` (`test_routes.py:6038-6041`) is 2 routes × 3 errors × 2
flags.

This per-file table is the caller re-census that `findings-2026-09-28/plan-impact.md:350` asks T00 to produce:
157 test sites plus 1 eval driver site, at `1effedab2`.

**Adjacent sites the instrument deliberately excludes (each with anchors):**

1. **Dependency drives of `_track_compose_inflight`:** 6 sites in `tests/unit/web/sessions/routes/test_composer_request_telemetry.py`.
   They are `_request(path)` (a raw `POST` scope, `:38-39`) at `:183`, `:217`, `:237`, `:257`, the parametrised
   path `:95` and the `path=` keyword `:142`. They go dead when `_track_compose_inflight` is deleted.
2. **`ProbeRequest("POST", ".../messages", …)`:** 4 sites, in
   `tests/unit/web/azure_container_apps_acceptance/test_replica_probes.py:221,240,250` and
   `tests/unit/web/acceptance_common/test_replica_probes.py:511`. Separately, `test_single_revision.py:192,342`
   asserts exactly 40 `/messages` paths.
3. **Fake servers that answer `POST /messages` with a synchronous 200 body:**
   `tests/unit/web/azure_container_apps_acceptance/test_live_observations.py:123-128` and `:178-182`;
   `tests/unit/web/azure_container_apps_acceptance/test_single_revision.py:109`;
   `tests/unit/evals/composer_battery/fake_http.py:87` (the `POST /api/sessions/` prefix) → `:134-136`; and
   `tests/unit/scripts/test_composer_acceptance_runner.py:56-60`.

**Every existing site asserts a synchronous body.** The DTO is still `response_model=MessageWithStateResponse`
(`composer/compose.py:88-91`; `messages.py:131`). `/recompose` now takes a body, `RecomposeRequest`
(`schemas.py:162-165`, `expected_user_message_id: UUID`), built in tests by `_recompose_request`
(`test_routes.py:156-160`). The old "bodyless recompose" fact is obsolete.

### 1(c) Non-test callers of the two routes

Instrument: `git grep -n -E "/messages\b|/recompose\b"` outside `src/elspeth/web/frontend`, `tests`, `docs` and
`*.md`, with the provider `v1/messages` URLs excluded. Every hit outside the route modules is listed below, plus
the frontend e2e harness from a separate grep.

1. **ACA acceptance observations probe** — `src/elspeth/web/azure_container_apps_observations.py:297-330`
   (`_fresh_message_visibility`, called at `:459`):
   - it POSTs `/messages` with `{"content", "client_request_id": str(uuid4())}` (`:314`);
   - it requires `status == 200` and the owner instance (`:317`);
   - it parses the strict `_MessageWrite` (`:94-96`, `ConfigDict(strict=True)`, `message: _Message`) at `:319`;
   - it requires `role == "assistant"`;
   - it then polls `GET` on the reader replica.
2. **ACA replica probe P1** — `src/elspeth/web/_acceptance_common/replica_probes.py`:
   - `fence_conflict_trial` (`:626-649`) requires `client_request_id` in the body as a canonical UUID (`:630-638`);
     reads `fence_epoch` before and after; fires `fire_pair(..., expected_statuses={200, 202, 409})` (`:641`);
     and reads `message_ingress_receipt_rows(session_id, client_request_id=request_id)` (`:647`; observer
     protocol `:525`).
   - `decide_fence_conflict` (`:288-327`) demands per trial:
     - exactly one success (`succeeded` = `200 <= status < 300`, `:198-199`) and one `refused_by_fence`, meaning
       `status == 409 and detail == SESSION_OPERATION_CONFLICT_DETAIL` (`:202-203`; the constant
       `"Session operation is already active"` is at `:98`);
     - `fence_epoch_after == fence_epoch_before + 1` (`:312`);
     - `fence_owner_after == winner` (`:314`);
     - `message_ingress_receipt_rows == 1` (`:316`);
     - two distinct winners across the run.
   - Observer SQL: `MESSAGE_INGRESS_RECEIPT_ROWS_SQL` = `SELECT count(*) FROM message_ingress_receipts WHERE session_id = :session_id AND client_request_id = :client_request_id`
     (`_azure_container_apps_acceptance/controller.py:57-59`, used at `:296-305`).
   - Callers: `azure_container_apps_acceptance.py:577` and `azure_container_apps_single_revision.py:251`.
   - Shell preparation: `deploy/azure-container-apps/scripts/acceptance.sh:475-487` (`prepare_freeform_trials`
     merges `client_request_id` into `P1_BODY` at `:484`). The P4 message is posted at `:506` and `:603` from an
     operator-supplied `P4_MESSAGE_BODY` (`:122,493,595`).
3. **Composer eval battery** — `evals/composer-battery/drive_battery.py:385-391` POSTs
   `{"content", "client_request_id"}` with `timeout=CLIENT_TIMEOUT_S` (`:33`, 620.0). `r is None` (timeout) and
   `status_code != 200` branch into terminal classification (`:392-…`). Its fake server is `fake_http.py:134-136`.
4. **Eval shell harness** — `evals/lib/common.sh:348-360` curls `POST .../messages` with body `{content:$c}` only.
5. **Composer acceptance runner** — `scripts/composer_acceptance/runner.py:72-94` (`compose()`) POSTs
   `{"content": prompt}` only (`:77`) and polls `/composer-progress` while the POST is outstanding. Its test fake
   accepts that body (`tests/unit/scripts/test_composer_acceptance_runner.py:56-60`).
6. `scripts/acceptance_battery.py:12` mentions the route in a docstring only. No call was found there.
7. **Frontend e2e harness** (Playwright/vitest, `src/elspeth/web/frontend/tests/e2e/`):
   - `harness/transition-ledger.ts:29-38` classifies `POST .../messages` as the `"freeform/compose"` transition.
   - `helpers/transition-ledger-recorder.ts:108-110,147,188,226` **holds each authoring HTTP response back from
     the browser until it has re-read the durable audit rows** (file header `transition-ledger.ts:16-20`). This
     is the per-TRANSITION provider-call instrument named in AGENTS.md's composer standing review trigger.
   - `harness/classify.ts:42` names the compose step.
   - `tutorial-reliability.staging.spec.ts:235` captures the live POST.
   - `composer-proposals.spec.ts:172` and `tutorial.spec.ts:113` mock a synchronous POST.

`git grep` found no `/messages` or `/recompose` reference in `src/elspeth/web/_aws_ecs_acceptance/`.

**Pre-existing defect (report, not fix): two live callers already get 422.** Items 4 and 5 omit
`client_request_id`, which `SendMessageRequest` requires (`schemas.py:154`). Measured:
`SendMessageRequest.model_validate({'content':'hi'})` → rejected, loc `('client_request_id',)`. The control with a
canonical UUID was accepted. The runner's unit fake masks this.

### 1(d) Whole-tree gates the change trips (current anchors)

A focused serial baseline of these gates passed: 29 passed, `exit=0` (§1(e) lists the selection).
Mutation control (`mutation_control.py`): unmutated, both of the gates below PASS. After adding an in-memory
`composer_async_operations` table (`operation_id`, `request_hash`, `result_hash`) to `models.metadata`:

- `test_sessions_metadata_table_policy_is_exact_and_protected` → RED (bare `assert live_tables == reviewed_tables`, `:11270`);
- `test_every_digest_named_column_is_inventoried_or_excluded` → RED (`new digest-named columns need a shape in _INVENTORY or a reason in _EXCLUDED`).

| Gate / pin | Current anchor | Tripped by | Note |
|---|---|---|---|
| Sessions mutation authority: exact table-policy set | `tests/unit/architecture/test_session_db_mutation_authority.py:11267` (`TablePolicy` `:35-44`, `permits` matches authority + operation only; `_TABLE_POLICIES` `:78`; ingress `:139`; receipts `:189-194`) | new table | mutation-controlled RED above |
| Named authority registry | `_NAMED_AUTHORITY_SYMBOLS` `:238`; `test_named_authority_registry_is_explicit_extensible_and_exact` `:11387` | new authority class | |
| Receipts writer pin | `test_operation_receipt_writers_are_exact_and_fenced` `:12196-12222` (6 writer sites `:12211`; exact `operation_authorities` `:12218`) | only if receipts code moves (B′ extracts the request hash; that holds no SQL) | RECOMMENDATION's `:12200-12216` moved to `:12196-12222` |
| Shared-row boundaries | `test_shared_row_writers_are_fenced_session_authority_boundaries_and_no_fail_open_writer_remains` `:19280-19334`; `_insert_message_ingress_receipt` bound to `SessionMutationAuthority` (`:19292`); `add_message_with_transcript._sync` must own no raw ingress write (`:19314`) | ruling 1 (worker writes ingress) | |
| Ingress writer exactness | `test_message_ingress_receipt_writer_stays_under_its_exact_guarded_boundary` `:19337-19369`: exactly 1 live ingress writer (`:19344`) and **`source_text.count("                self._insert_message_ingress_receipt(\n") == 1`** (`:19360-19361`); the production call is `service.py:4680` inside `add_message_with_transcript` (`:4576`), and the writer is `:1396` | a second call site, or a move out of `service.py` | |
| Writer manifest (fingerprints, ordinals, lines) | `test_all_production_sessions_writers_are_reviewed_typed_authorities` `:18617` (`pytest.xfail` on drift `:18652`); `test_live_connection_domain_classification_is_exact` `:18327`; `test_session_schema_authority_is_exact_contained_and_bidirectional` `:19139`; 440 `line=N` literals; `app.py` listed as a non-writer `:12044` | new writers; any line shift in pinned files | keep DB writes out of `app.py` |
| Digest-column shape | `tests/unit/architecture/test_digest_column_shape_checks.py:227` (regex `_DIGEST_NAME` `:46`; `_INVENTORY` `:72`, receipts precedent `:113-114`); PG arm `tests/testcontainer/web/test_schema_probe_postgres.py:182` | `request_hash`, result hash | mutation-controlled RED above |
| Required audit triggers (exact sets) | `schema.py:96-112` (11 names); unit DDL pin `tests/unit/web/sessions/test_schema.py:603-627`; PG `assert names == {…}` `test_schema_probe_postgres.py:633-645`; dropped-trigger parametrisation `test_interpretation_events_table.py:1205-1218` (9 names, a subset) | ruling-driven triggers (widened guard + delete guard → 13) | 3 exact-set sites to extend |
| Coordination hard-cut sets | `schema.py:45-58`; `test_schema.py:342` (`expected_tables` `:370-385`), `:427` (`expected_checks` `:432`) | only if the table joins the hard-cut set (receipts did not) | |
| Epoch `== 71` literals | `test_web_blob_fencing.py:3205`; `test_blob_inline_resolutions_schema.py:73`; `test_interpretation_events_table.py:250`; `test_proposal_blob_effect_receipts_schema.py:27`; `test_schema.py:369` | epoch 72 | `git grep -E "SESSION_SCHEMA_EPOCH *== *[0-9]+" -- tests` → 5 files, 5 hits; control: `SESSION_SCHEMA_EPOCH *= *[0-9]+` over `src` → `models.py:59` |
| Epoch constants | `SESSION_SCHEMA_EPOCH = 71` `models.py:59`; `_COORDINATION_HARD_CUT_EPOCH = 71` `schema.py:45` | must move together | epoch 71 landed in `7001600fe` |
| Epoch doc/website tests (read the live constant) | `test_release_version_surfaces.py:125` (`docs/guides/sharing-pipelines.md:73`); `test_readme_release_surface.py:34` (`README.md:168`); `test_release_site_contract.py:59,61,166` (`CHANGELOG.md`, `website/get-started.html:106`); `test_staging_session_recreation_policy.py:21-35` (`docs/runbooks/staging-session-db-recreation.md:5,7,89,202,204,812`); `test_azure_container_apps_runbook_contract.py:189,194` (`azure-container-apps-deployment.md:109,451,464,538`, `azure-container-apps-cold-install.md:86`); `aws_ecs_acceptance/test_cleanup_control_service.py:111,121,179,184` (`aws-ecs-deployment.md:1621,1649,1654`) | epoch 72 | `test_release_site_contract.py:61` pins **prose**: `f"Session epoch {SESSION_SCHEMA_EPOCH} removes mode-specific operation state"` (`CHANGELOG.md:52`). A bump needs a new sentence, not a number swap |
| IDOR ownership inventory | `EXPECTED_SESSIONS_OWNERSHIP_ENDPOINTS` `test_routes.py:3979` (`send_message` `:3985`, `recompose` `:3986`); `test_sessions_routes_ownership_call_sites` `:4096-4110` walks a **fixed tuple of 7 modules** `(sessions, state, proposals, compose, messages, runs, interpretation)` (`:4102`) | new router module (GET operation, POST cancel) | a new module is invisible until it is added. Existing gap as a positive control: `workflow/approvals.py`, `workflow/library.py`, `workflow/reviews.py` call `_verify_session_ownership` and are not walked |
| IDOR cross-session walk | `TestIDORProtection.test_idor_session_crud` `test_routes.py:4234-4419` | new endpoints (by convention) | |
| Structural fence-wiring pins | `tests/unit/web/sessions/test_operation_fence_wiring.py:84-110` (`send_message` must hold `async with compose_lock, SessionOperationLease.acquire(...) as compose_operation_lease` before `service.get_current_state` and `service.add_message_with_transcript(session_operation_context=compose_operation_lease.context)`); `:323-341` (`maybe_auto_title_session(..., session_operation_context=compose_operation_lease.context)` wrapped in `compose_operation_lease.create_task`). Both parse `inspect.getsource(message_routes.register_message_routes)` | moving the turn out of the route | `next(...)` raises `StopIteration` once the scope leaves. No structural pin on `recompose` remains: the guided-era `test_no_chain_authoring_path.py` is deleted (`tests/unit/web/composer/guided/` holds only `__pycache__`) |
| Inflight mount pin | **new:** `test_message_route_mounts_request_lifecycle_dependency_exactly_once` `tests/unit/web/sessions/routes/test_composer_request_telemetry.py:111-124` (`dependencies.count(_helpers._track_compose_inflight) == 1`) | unmounting from `/messages` | |
| Attribute contracts | `tests/unit/web/test_sessions_composer_attribute_contracts.py:63` walks `src/elspeth/web/sessions` and `src/elspeth/web/composer` | any `getattr`/`hasattr` there | the contract's worker path `src/elspeth/web/sessions/composer_async_worker.py` (`contract.md:368`) is **inside** this walk |
| Masquerade baseline (tests included) | `tests/unit/elspeth_lints/test_masquerade_gate.py`; `config/cicd/masquerade_baseline.yaml` | any new probe in src or tests | `grep` for `operation_receipts`, `routes/messages`, `routes/composer/compose` and `web/app.py` in the baseline → 0 entries |
| Soft-mapping census | `config/cicd/soft-mapping-census.yaml`: `app.py` `dict[str, object]: 2` (`:696-697`); `operation_receipts.py` `dict[str, Any]: 2` (`:1001-1002`, one is `normalized` at `operation_receipts.py:46`); `routes/_helpers.py` (`:1014-1017`); `composer/compose.py` `dict[str, Any]: 1` (`:1018-1019`); `messages.py` `dict[str, Any]: 1` (`:1027-1028`); **`soft: 2482`** (`:1042`) | B′'s normaliser extraction moves `:46`'s annotation; new JSON parsing | `python -m scripts.check_contracts --write-census` in the same commit |
| Mock discipline, gate walker | `tests/unit/test_mock_discipline_baseline.py`; `tests/unit/elspeth_lints/test_python_file_walker_authority.py`; `tests/helpers/tree_gate.py` | new tests / gates | files exist |
| Route-split module list | `tests/unit/web/sessions/test_routes_split.py:12-13` imports a fixed module set | optional | passed in the baseline |

Trust-tier allowlist (not run, no count claimed): `config/cicd/enforce_tier_model/web.yaml:7401,7420` hold two
`compose.py:R7:recompose` keys. `grep -c "contextlib.suppress(asyncio.CancelledError)"` is 0 in both `compose.py`
and `messages.py`, and `grep -n "suppress\|except BaseException\|except Exception" compose.py` → no hits. So I
found no live construct those keys could bind. I did **not** run the lint corpus to confirm staleness.

### 1(e) `full-suite-gate.sh` and the testcontainer harness

- `scripts/full-suite-gate.sh`:
  - The default stages are `ruff,pytest` (`:17`, `:68`, `:84`). Valid stages are `ruff|mypy|contracts|lints|pytest|testcontainer` (`:117`).
  - The testcontainer command is `pytest tests/ -m testcontainer -n 0 -o 'pythonpath=<root>/src <root>/elspeth-lints/src'` (`:203`).
  - It refuses when Docker is unreachable and warns when sibling suites are running (`:173-175`), and caps workers when siblings exist (`:170`).
  - The frozen-tree verdict is written at `:277-282`.
  - For this change, pass `--stages ruff,mypy,contracts,lints,pytest,testcontainer` explicitly.
- CI: the `testcontainer` job "Testcontainer (PostgreSQL contention proofs)" is at `.github/workflows/ci.yaml:894-949`
  (`-m testcontainer` `:939`). `ci-success` requires it (`:1337`, `:1356`). The default addopts deselect
  `testcontainer` (`pyproject.toml:455`).
- `tests/testcontainer/web/conftest.py`: `_require_sequential_postgres_acceptance` (`:31-36`) raises
  `pytest.UsageError` under xdist. The session-scoped `external_deployment_postgres_url` starts at `:39`.
- Cross-process harness, `tests/testcontainer/web/test_cross_process_composer_postgres.py`:
  - `_http_lifecycle_process` / `_serve_lifecycle_commands` (`:42-116`): each spawned process has its own engine and
    a real FastAPI app that includes **only `state.router`** (`:63`, no guided router now). A probe route
    `/{session_id}/test-compose/{request_id}` mounts `Depends(_helpers._track_compose_inflight)` (`:66-76`) and
    parks forever.
  - `_COMPOSER_HEARTBEAT_SECONDS` is patched to 0.2 (`:83`). One long-lived `AsyncClient` serves the
    start/abort/poll commands, with a fresh client per poll (`:84-111`).
  - `_composer_process` is at `:145`; `_receive` at `:194`; `_workers` (spawn context) at `:205-229`.
  - `test_killed_publisher_leaves_committed_snapshot_for_fresh_peer` (`:501-532`, `owner.kill()`, `pg_sleep(2.1)`)
    is the crash-window template.
- `tests/testcontainer/web/test_operation_receipts_postgres.py` (**new**): its function-scoped `postgres_engine`
  (`:33-48`) creates a fresh database per test. It has 2 tests (`:51`, `:87`) and no takeover race.
- `test_schema_probe_postgres.py`: module-scoped `postgres_url` (`:72-77`), fresh database per test (`:79-92`),
  digest PG test `:182`, trigger exact set `:612-645`. The guided takeover precedents the old findings cited are
  gone: `grep takeover` finds none there, and `test_guided_atomic_settlement_postgres.py` is deleted.
- `test_session_operation_fence_postgres.py`: `postgres_engine` is module-scoped on the shared URL (`:132-133`).
  It has `_register_instance` (`:152-160`, hard-coded `session_epoch=37`), expiry takeover (`:275`) and
  `test_postgres_two_claimants_have_exactly_one_winner` (`:349`).
- The focused baseline run (serial, `-n 0 -p no:cacheprovider`) covered:
  - the attribute contracts;
  - the digest inventory;
  - 5 mutation-authority tests (table policy, registry, ingress writer, receipts writers, shared-row);
  - 3 `test_schema.py` tests (hard-cut tables, hard-cut checks, PG trigger DDL);
  - the 2 fence-wiring pins;
  - `TestIDORCoverageDrift`;
  - `test_routes_split.py`;
  - the inflight mount pin;
  - the 3 lifespan tests.

  Result: `29 passed in 19.36s`, `exit=0`.

---

## 2. DELTA vs the old findings and contract

**Moved (same fact, new line):**

- `app.py` lifespan anchors shifted by +1 or -1 (for example `recover()` 901→900, `yield` 924-925 unchanged). `_service_lifespan` is `598-969`.
- `sessions/service.py`: the `session_operation_lease_seconds` default is at `:566` (old `:4480`).
- The compose-lock registry is at `_helpers.py:217-270` (old `311-364`). `_track_compose_inflight` is at `:2556` (old `:2442`). The heartbeat knobs are at `:2452-2477` (old `:2338-2363`).
- `ExecutionServiceImpl.shutdown` is at `execution/service.py:1549-1582` (old `1626-1658`).
- In `test_routes.py`, every builder and fake moved up by about 40-80 lines (table in 1(b)). The IDOR inventory moved to `:3979`/`:4096-4110` (old `:3718`/`:3861-3874`, which `contract.md:400` cites as `:3863-3866`).
- Mutation authority: table policy `:11267` (old `:11534`), registry `:11387` (old `:11654`), manifest `:18617` (old `:19034`). The receipts pin is `:12196-12222` (RECOMMENDATION cites `:12200-12216`).
- Digest gate `:227` (old `:216`). The fence-wiring pins are at `:84` and `:323` (old `:94` and `:344`).

**Vanished:**

- Guided routes and every guided mount: `/guided/plan|respond|chat` and `guided_plan.py`/`guided.py` are gone (`7001600fe`). `_track_compose_inflight` now has 2 mounts (old 5).
- The guided pins the old findings called "guided behaviour unchanged" evidence are gone: `test_composer_request_telemetry.py`'s `/guided/respond` pin, the cross-process harness's `post_guided_plan` mount, `test_guided_heartbeat_cancel_classification.py` and `test_no_chain_authoring_path.py`.
- `tests/integration/web/composer/guided/test_progressive_disclosure.py` (4 POST sites) is gone.
- The guided PG takeover precedents are gone (`test_schema_probe_postgres.py:512,649,745` and `test_guided_atomic_settlement_postgres.py`).
- `surface` is no longer `Literal["freeform","guided"]` computed from the path. It is the constant `"freeform"` (`_helpers.py:2599`).
- `guided_terminal` is no longer a `compose()` parameter (`protocol.py:1622-1634`).
- The epoch literal pin `test_schema9_epoch.py` is deleted, leaving 5 literal pins (old 6 files / 7 hits).
- The old "bodyless `/recompose`" fact is obsolete: `RecomposeRequest` exists (`schemas.py:162-165`).

**New:**

- `app.state.interpretation_surfacing / planning_application / schema_disclosure` (`app.py:1896-1898`). `_route_client` does not set `interpretation_surfacing`; `_make_app` and `_make_progress_route_app` do.
- `_route_client` and `_make_app` use `FencedSessionServiceHarness`, not `DualFencedSessionServiceHarness`.
- `test_freeform_route_custody.py` (18 sites, 12 of them variable-held; `8630db9b8`).
- `_join_freeform_owned_task` (`_helpers.py:1428-1450`), a join that survives cancellation.
- The ingress receipt facility: `message_ingress_receipts` with its policy (`test_session_db_mutation_authority.py:139`), triggers `trg_message_ingress_receipts_no_update|no_delete` (`schema.py:104-105`), the exact-writer pins `:19280-19369`, and the P1 probe's ingress-count predicate (`replica_probes.py:316`, `controller.py:57-59`).
- The receipts facility: `session_operation_receipts` and its events, 3 more required triggers, `test_operation_receipts_postgres.py`.
- The `/messages` inflight-mount pin (`test_composer_request_telemetry.py:111-124`). The old findings said no such pin existed.
- `_cancel_on_client_disconnect` now has only the two freeform routes as users.

**Counts:**

- POST sites with the same instrument: `d479eb2b4` = 137 (tests+evals; `post/literal` 131, which reproduces the old count) → `1effedab2` = 158 (tests+evals). That is **157 test sites in 12 files** plus 1 eval driver site.
- By file: `test_routes.py` 108→115; `test_freeform_route_custody.py` +18 (new); the guided file −4. The old report's 3 for `test_compose_heartbeat_renewal.py` is 4 here because the instrument now counts the raw-scope drive.
- The soft census total fell 2844 → 2482. The required-trigger set grew to 11 (RECOMMENDATION fact 7 agrees).

**Contract inconsistencies found:**

- `contract.md:374` types `instance_draining: asyncio.Event`, while its own amendment `:32` says `threading.Event`. The tree says `threading.Event` (`membership_lifecycle.py:65-71`, `readiness.py:663-664`).
- `contract.md:39,204,471`: the epoch is 68/"Task 15". The tree is at 71, so the next cut is 72.
- `contract.md:386-387` places the worker "after `orphan_task`". The amendment `:34` splits construction + startup reap (after `recover()`) from `start()` (after the done callback). The measured leak window (`app.py:904-924`) supports the amendment.
- `contract.md:400` cites the IDOR tuple at `:3863-3866` with the guided module in it. It is now `:4102`, with 7 modules.
- `contract.md:419` says `settle_sync` serves "the 131 legacy sync call sites". The census is now 157 test sites. 12 of them hold the URL in a variable, and 18 sit in a file that already uses `httpx.AsyncClient(ASGITransport)` directly.

---

## 3. IMPLICATIONS for the plan under the rulings

1. **Lifespan (T11).**
   - Construct the worker and run `reap_once()` between `app.py:900` and `:904`.
   - `start()` after `:922`.
   - `stop()` inside the nested `finally` after `:941` and before `:949`.
   - Pass `app.state.instance_draining` as a `threading.Event`. Stop claiming while `is_set()`.
   - Escalate a dead loop via `process_recovery.request_shutdown()`, the same pattern as `:917-921`.
   - The drain wait is bounded by `_join_freeform_owned_task` settlement, not by the cancel. If the turn keeps
     that join, `stop()` needs its own bound or an explicit "joins to settlement" ruling.
   - A twin of `test_fatal_periodic_cleanup_requests_recovery_and_preserves_shutdown` must also monkeypatch the
     worker's loop. `test_lifespan_starts_membership_and_drains_as_the_first_act_of_shutdown` stays green only if
     the worker never calls membership.
2. **One lock registry.** The registry is still request-lazy (`_helpers.py:258-270`, 12 call sites). The worker has
   no `Request`, so the contract's app-keyed accessor plus eager lifespan creation is still required. Proposals,
   state import, interpretation and run diagnostics (`execution/routes.py:1456`) all serialise on that same object.
3. **Test harness (D16, T12/T13).**
   - The shim cannot host a worker (re-measured). `settle_sync` must run submit, drive and poll in one `anyio.run`.
   - Builders have no lifespan. `install_composer_async_worker(app)` must work on `_make_app` apps and on
     `_route_client` apps. A worker that reads `app.state.interpretation_surfacing` fails under the
     `test_client`/`closed_local_app` fixtures unless `_route_client` gains the stub.
   - Many tests swap `app.state.composer_service` after the app is built. The worker must therefore resolve the
     composer from `app.state` at turn time, not at construction.
   - Move `_BlockingRecordingComposer`, `_HangingComposer` and friends to a shared home. None exists: 0 hits under
     `tests/fixtures` and `tests/helpers`.
4. **D8 changes test semantics.** `test_send_message_serializes_concurrent_requests_per_session`
   (`test_routes.py:5899-5943`) proves that a second send waits and then returns 200. Under the D8 partial-unique
   index (one nonterminal job per session), that second send is a 409 `composer_operation_active`, so the test
   must be rewritten, not ported. `test_composer_progress_reports_inflight_request_count` (`:5095-5146`) and
   `test_client_disconnect_cancels_compose_turn` (`:4913-5024`, 499 on disconnect) encode contracts the cutover
   retires.
5. **Ruling 1 versus the ingress writer pins.** The worker must write ingress in the same transaction as the user
   row. Today that write is `self._insert_message_ingress_receipt(` at `service.py:4680`, inside
   `add_message_with_transcript`. `test_message_ingress_receipt_writer_stays_under_its_exact_guarded_boundary`
   (`:19337-19369`) requires exactly one such call text and exactly one live writer. So either:
   - (a) the worker reaches ingress through the existing `add_message_with_transcript` path, and the pins hold; or
   - (b) T06 moves or duplicates the write into the compose composite, and T06 must re-derive `:19292`, `:19314`,
     `:19344` and `:19361` plus the manifest entry.

   The composite FK from ingress to the job, and the rename of `client_request_id` to the single wire name, both
   touch `MESSAGE_INGRESS_RECEIPT_ROWS_SQL` (`controller.py:57-59`) and the observer protocol
   (`replica_probes.py:525`).
6. **P1 must be redefined (T13/T15). It cannot be ported.** Each predicate breaks as follows:
   - "one success + one fence refusal" (`:202-203`, `:303-305`) fails. Both replicas get 2xx for the same
     `operation_id` and body (the replay returns 202). A 409 carries `composer_operation_active` or a conflict
     body, never `SESSION_OPERATION_CONFLICT_DETAIL`.
   - `fence_epoch_after == before + 1` (`:312`) is read synchronously after `fire_pair`. The fence now advances
     at worker start.
   - `fence_owner_after == winner` (`:314`): the winner is now the replica whose worker claimed the job, not the
     one that answered.
   - `message_ingress_receipt_rows == 1` (`:316`) holds only after the terminal transaction.
   - The body-key check (`:630-638`) and the jq merge (`acceptance.sh:484`) use `client_request_id`.

   `_fresh_message_visibility` (`observations.py:317-319`) needs a poll-to-terminal step before `_MessageWrite`.
   It is also a single-replica POST, so under ruling 5 it must send the head's `state_id` whenever the session
   has one.
7. **Eval, acceptance and e2e callers.**
   - `drive_battery.py:385-391`: a 202 lands in the `!= 200` branch and is misread as a product failure; the
     battery needs a poll loop. `CLIENT_TIMEOUT_S = 620` becomes a poll budget.
   - `fake_http.py:134-136`, the ACA observation fakes and the single-revision fake must answer 202 plus a poll
     GET.
   - `runner.py` and `common.sh` already get 422 today. The cutover is the moment to fix them onto the single
     wire name.
   - The Playwright transition recorder attributes provider calls by holding the POST response until the
     cohort is durable (`transition-ledger.ts:16-20`). Under 202 the POST returns before any provider call, so
     the transition boundary must move to the terminal poll. Otherwise the per-TRANSITION gate that the
     composer standing review trigger relies on reads zero calls per compose, which is exactly the blindness it
     exists to catch.
8. **Gates the change will turn red** (re-pin in the same commits):
   - the table policy and registry, the writer manifest (line literals), and the digest `_INVENTORY`;
   - the 3 exact trigger sets (11 → 13 names);
   - the 5 `== 71` literals and 6 doc/website tests, including `CHANGELOG` prose at `test_release_site_contract.py:61`;
   - IDOR: add the new router module to the tuple at `:4102` and its handlers to `:3979`;
   - the fence-wiring pins (`:84-110`, `:323-341`) re-targeted at the worker's turn with the same intent (SOL
     before state read and before the transcript write; auto-title via `lease.create_task`);
   - the inflight mount pin (`:111-124`) and the 6 dependency drives, deleted with `_track_compose_inflight` (T07);
   - the soft census (B′ extraction moves `operation_receipts.py:46`'s `dict[str, Any]`).

   The attribute-contract walk covers the contract's worker path: no `getattr` in the worker. Masquerade covers
   test fixtures too: no `getattr(app.state, …, None)` in helpers.
9. **Testcontainer (T16).**
   - No guided takeover precedent remains. The closest templates are `test_postgres_two_claimants_have_exactly_one_winner`
     (`test_session_operation_fence_postgres.py:349`) for claim CAS races and
     `test_killed_publisher_leaves_committed_snapshot_for_fresh_peer` (`test_cross_process_composer_postgres.py:501-532`)
     for crash windows.
   - The cross-process harness mounts only `state.router` plus a probe route. A worker-level cross-process test
     needs its own app assembly with the worker installed in the child's loop.
   - The ingress→job composite FK under the D7 cascade and the widened trigger belong in
     `test_schema_probe_postgres.py`-style fresh-database fixtures (`:79-92`).
   - Run the gate with `--stages ruff,mypy,contracts,lints,pytest,testcontainer`. The default omits
     testcontainer, mypy, contracts and lints.

---

## 4. Open questions

1. **P1 redefinition.** Should P1 become a poll-to-terminal trial asserting one job row, one ingress row and one
   fence advance (the plan-impact wording)? Or should it be redefined around admission, checking that two
   replicas admitting the same `operation_id` yield one job? Either way, which status pair counts as "success"
   when both replicas answer 202?
2. **Ingress write path.** Is ingress written through the existing `add_message_with_transcript` →
   `_insert_message_ingress_receipt` path (pins hold), or through a new compose-authority method (pins
   `:19292/:19314/:19344/:19361` move)?
3. **Transition-ledger recorder.** Who owns the change to `transition-ledger-recorder.ts`: the frontend seam
   (T14) or the cutover (T13)? Its semantics gate the composer standing review trigger.
4. **Inflight mount pin.** Should `test_message_route_mounts_request_lifecycle_dependency_exactly_once` be
   deleted with `_track_compose_inflight`, or re-targeted at the worker's `_composer_request_lifecycle`
   (surface `"freeform"`)?
5. **Drain bound.** Does `stop()` join owned turns to settlement (unbounded by cancel, because of
   `_join_freeform_owned_task`'s `uncancel`), or impose a deadline after which the row is left for a peer's
   reaper as `worker_lost`?
6. **`_route_client` fixtures.** Should `_route_client` gain `interpretation_surfacing` (and whatever else the
   worker reads)? Or should the worker take explicit collaborators, so that `test_client` and `closed_local_app`
   tests can drive it?
7. **Pre-existing 422 callers.** Should `scripts/composer_acceptance/runner.py:77` and `evals/lib/common.sh:348` be
   fixed at cutover (T13), or reported separately? They are broken today regardless of this plan.
