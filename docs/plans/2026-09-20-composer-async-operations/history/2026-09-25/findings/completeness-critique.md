# Completeness critique: composer async operations (freeform-only)

This lane is read-only. Measured on the main checkout, `release/0.8.1` at `d479eb2b4`, and `git status --short src tests` printed nothing.
Inputs:
- the amended spec `docs/specs/2026-09-16-composer-async-operations-design.md` (315 lines, read in full);
- all 8 explorer files in this directory (read in full).

Abbreviations:
- `M` = `src/elspeth/web/sessions/routes/messages.py`
- `C` = `src/elspeth/web/sessions/routes/composer/compose.py`
- `H` = `src/elspeth/web/sessions/routes/_helpers.py`
- `SV` = `src/elspeth/web/sessions/service.py`
- `R` = `src/elspeth/web/coordination/repository.py`
- `LC` = `src/elspeth/web/coordination/lifecycle.py`
- `CS` = `src/elspeth/web/composer/service.py`
- `A` = `src/elspeth/web/app.py`

Verdict in one line: the explorers give enough anchored fact to write most tasks. Four areas still need a design fork resolved before the tasks can be concrete: the dual-fence mechanism, the single terminal transaction, a worker context that must be rebuilt from the row alone, and the fate of `ComposerRequestLease`. The investigations below turn each one into a concrete choice with measured costs.

---

## (a) Anchor spot-check

I checked about 95 anchors with `sed -n` / `grep -n` against the tree: the route bodies, `_helpers.py`, `lifecycle.py`, `repository.py`, `schemas.py`, `models.py`/`schema.py`, `config.py`, `app.py`, `composer/service.py`, `async_workers.py`, `guided_operations.py`, the tests and the frontend. Nearly all matched on both line and content.

These were **right**, and are listed so the plan can cite them with confidence:
- `M`:112/116/124/138/140/143/144/147/186/216/238/265/275/291/322/357/378/406/694/808/845/933/978/994/1027/1035/1050/1154/1176
- `C`:86/90/97/106/107/110/112/115/135/142/145/147/186/190/202/214/555/592/721/734/760/768/787/792/826
- `H`:311/352/367/372/382/399/406/454/758/1380/1494/1902/1914/1988/2184/2210/2338/2340/2421/2442-2446/2491/2492/2587/2788-2807/2893/2995/3064
- `LC`:272/298/325/616/621/642/650/694/1035/1068/1074
- `R`:283/4756/4799/4803/4809/5064/5650
- `schemas.py`:54/65/68/74/145/154/155/228/417/420
- `models.py:366`; `schema.py:40,535-536`
- `config.py`:325/335/349/1186-1187/1199-1201/1210
- `A`:648/904/917/925/932/938/949/955/969/1357/1847/1900/1917/2132/2247/2334
- `CS`:2421/2542/4045/4051/4063/4099/4112/4151-4152/4161/4163/4218/4459/5382/9469
- `SV`:4480/4692/5459/6650/6780/9369/9524/10134/14706; `async_workers.py`:14/18/19/24/76/189/194
- `guided_operations.py:17,26-29`; `session_operation_handlers.py:23-30` (path is `src/elspeth/web/session_operation_handlers.py`)
- `test_routes.py`:351/453/779/853/3718/3724/3725/3863-3866/4752/4934/6219/11827; `test_compose_heartbeat_renewal.py`:137 and every test def line listed in lifecycle §9.1
- `_sync_asgi_client.py:41-57`; `test_operation_fence_wiring.py:94`; `test_session_db_mutation_authority.py:11534`; `test_config.py:242`
- `client.ts:896,917`; `guidedOperationRetry.ts:53-59`; `sessionStore.ts:1946,2184,2214,2215,2853`; `config/composer.ts:28-29`; `useComposer.ts:65-67`

These were **wrong or imprecise**. Use the corrected anchor.

| File | Claim | Tree | Severity |
|---|---|---|---|
| `lifecycle-anatomy.md` §2.2 step 3 | surface label at `_helpers.py:2484` | `:2485` (`:2484` is the comment) | minor |
| `app-lifespan-config.md` §1.3 row 11, §1.6 | `await recovery_coordinator.recover()` at `app.py:901` | `:900` (`:901` is blank) | minor |
| `tests-and-gates.md` §2 | `_run_fake_route` at `test_compose_heartbeat_renewal.py:120-164`; fakes `:59-104`; monkeypatch `:112-117` | `_run_fake_route` is at `:137`, `_FakeHeartbeatClock` at `:56`, `_ScriptedRegistry` at `:70`, the timer monkeypatch at about `:123`. lifecycle-anatomy's `137-183` is correct. | medium: a test-porting task will cite these |
| `recent-changes.md` §6 | recompose signature at `compose.py:92-104` | `compose.py:90-98` (decorator `86-89`) | medium |
| `recent-changes.md` §2.1 | `completion_gates=` at `messages.py:376` | `messages.py:378`; `compose.py:214` | minor |
| `recent-changes.md` §6 | `client.ts:917-929` | recompose is `917-927` | minor |
| `recent-changes.md` §4 | `_request_plugin_policy_context` `_helpers.py:373-381`; `_get_composer_progress_registry` `:367-369` | def at `:372`; registry `:367-372` | minor |
| `recent-changes.md` §2.4 | `messages.py:253` progress `request_id` | `messages.py:252` (`request_id=str(user_msg.id)`); recompose `compose.py:157` | minor |
| `app-lifespan-config.md` §1.2, `SV` anchors generally | `_session_process_locked_begin :4758`, `_session_write_lock :4800`, `_session_composer_mutation_transaction :4986` | these are the `@contextlib.contextmanager` decorator lines; the defs are at +1 (`:4987` confirmed) | trivial |

`recent-changes.md` uses `~` approximations throughout (for example `sessionStore.ts:~2611-2660`, `protocol.py ~:1487`). **Re-anchor any line you take from that file before citing it.** Its content claims held wherever I checked them.

---

## (b) Contradictions between explorers

The explorers mostly converge. They differ in four places. Three are real divergences of emphasis, and one is a factual line conflict.

1. **What happens to `ComposerRequestLease` (CRL) under the worker.** Each file assumes a different answer and none follows the consequences through.
   - lifecycle-anatomy §2.3 assumes the worker **keeps** one: it proposes a lease-parameter variant of `_composer_progress_sink`.
   - frontend §5 says to **re-key** abort/ambiguous resync onto the operation row.
   - recent-changes §5 leaves it open as (a) or (b).

   Consequences of **dropping** it for freeform, each checked in this pass:
   - Freeform turns vanish from `inflight_requests`. `waitForCancelledComposeToSettle` returns at once, so resync reads pre-turn state.
   - Freeform turns vanish from `GET /_active` (`routes/sessions.py:850`). That route has **no SPA consumer**. An untruncated grep for `_active` over non-test `frontend/src/**/*.ts(x)` gave 10 hits, and all 10 are unrelated identifiers (`sole_active_admin`, `no_active_session`, `run_already_active`); `/_active` itself gave 0. Positive control: the same tree does show the `composer-progress` consumer at `api/client.ts:782`.
   - `test_routes.py:4934` (inflight == 1 while parked) and `:12066` (gauge +1/−1) flip.
   - **The only mid-turn identity-revocation cancel is lost.** See investigation I4.

   Consequence of **keeping** it: the worker becomes a second owner of `registry.start_request/renew_request/finish_request`. Today `_track_compose_inflight` is the sole owner (lifecycle §4.3).
2. **`guidedOperationRetry.ts`.** recent-changes §6 calls it a reuse candidate. frontend §0.2 says it is a shape to copy, not a store to extend. It stores only a fingerprint (the tests pin this, `guidedOperationRetry.test.ts:89,147,169`), uses `sessionStorage`, caps at 8 KiB, and sweeps orphans on `selectSession`. **frontend wins.** The spec requires persisting the body, which reverses that module's privacy invariant.
3. **Rate-limit placement.** send-inventory classifies the per-user 429 as PRE-202 "capacity". The spec's §2 "capacity" means the new worker-queue 429. Both inventories (send R2, recompose R-9) flag the replay double-charge. This is an unresolved decision, not a contradiction.
4. **Line conflicts** for `_run_fake_route` (tests-and-gates vs lifecycle) and the recompose signature (recent-changes vs recompose-inventory). Resolved in (a).

No explorer contradicts another on a semantic claim I could check. Examples that agree across files:
- `ComposerAdmissionRefused` → 403 post-202 (send, recompose, recent-changes).
- `SessionOperationConflictError` → requeue, not a terminal (send, recompose, lifecycle).
- No instance-wide provider-turn limit (send, lifecycle, app-lifespan, recent-changes).

---

## (c) Coverage of spec requirements, and investigated gaps

### (c.1) Requirement → is there enough anchored fact for a concrete task?

`OK` means files, signatures and a test harness are all anchored. `PARTIAL` means one decision or measurement is missing. `GAP` means I investigated it below.

| Spec item | Status | Where the facts are / what is missing |
|---|---|---|
| Scope: cutover = 2 routes; guided 3 unchanged | OK | lifecycle §1 (5 mounts); guided pins tests-and-gates §1.2; guided heartbeat tests lifecycle §9.4 |
| Scope: state whether an instance-wide provider-turn limit exists | OK | Answer is **none**, from 4 instruments with controls (lifecycle §10, app-lifespan §1.8). The shared bound is only `run_sync_in_worker` 16+16 (`async_workers.py:14-24`) |
| §1 table, PK/FK, 2-value kind, status CHECKs | PARTIAL | Template in schema-persistence §3. Missing: the **status-shape for a claimed-but-queued row that settles `failed`**. It happens three ways: a precondition 400/409/404 under SOL before `running`, deadline expiry while queued, and cancel of an unclaimed row. The CHECK must admit `queued → failed` with or without a claim token, and admit `running → failed/completed`. Nothing in the findings says so |
| §1 fields: identity/queue/custody/input/output | PARTIAL | The actor FK mode is open (schema §7). The result-JSON size is **unmeasured** (task-0 M6). Input is bounded by `content ≤ 65536` (`schemas.py:154`) plus `state_id` |
| §1 request JSON cleared at settlement; delete cascades | OK / decision | Cascade via FK (schema §7). Archive with a *queued* job is undecided: `decide_and_soft_archive` refuses only guided `in_progress` (`R:729-744`, re-verified) |
| §1 result = full DTO; hash + validate on publish and read | OK | `guided_response_hash` (`routes/guided_operations.py:160-170`) and the `_replay_completed` verify-before-effect shape (`:183-202`) |
| §1 completed payload immutable | OK | 5-place trigger sweep (schema §3.2) |
| §1 queued reclaimable, running never taken over | OK | CAS precedents (schema §9); claim-lease setting is new |
| §1 **every worker write requires both fences** | **GAP → I3** | |
| §2 SPA mints and persists id + body | PARTIAL | frontend §0, §8. Decision needed: sessionStorage vs localStorage (same-tab only vs cross-tab), and the bound (≥64 KiB) |
| §2 DTOs gain `operation_id`; recompose gets its first DTO | OK | `_GuidedOperationRequest` (`schemas.py:74-90`); strict `state_id` needs the `RevertStateRequest` `mode="before"` parser (`schemas.py:422-437`, re-verified). Side effect: strict parsing newly rejects a non-canonical (for example upper-case) `state_id` string that coercion accepts today |
| §2 hash codec; replay only if kind+actor+hash agree | OK / new | The codec template refuses non-strict DTOs (`guided_operations.py:26-29`). **Actor binding has no guided precedent** (schema §7: `guided_operations` has no actor column) |
| §2 admission order → 202 | PARTIAL | FastAPI solves dependencies before body validation (send §1a). This forces the async POST to drop `_track_compose_inflight` and order its own checks. Rate-limit placement and replay bypass are undecided |
| §2 full queue → sync 429 | PARTIAL | No existing capacity setting or counter. Global vs per-instance scope is undecided |
| §2 poll GET contract | OK after I1/I2 | Templates in I1 and I2 below |
| §2 error envelope; `operation_failed` + server diagnostic id | **GAP → I6** | |
| §2 row is the settlement authority; pollers advisory | PARTIAL | Hinges on contradiction (b)1 |
| §3 worker on every instance; PG SKIP LOCKED / SQLite CAS | OK | schema §9 (SQLite drops `FOR UPDATE SKIP LOCKED` silently, measured). `elspeth web` runs single-process uvicorn (`src/elspeth/cli.py:5047-5054`, no `workers=`), which matches "single-instance SQLite" |
| §3 claim → SOL → recheck ownership/preconditions → `running` | PARTIAL → I4, I8 | `_verify_session_ownership` needs only `app.state` (`H:2797-2806`: `session_service`, `settings.auth_provider`). A request-free variant is trivial. But see I4 (revocation) and I8 (context) |
| §3 SOL conflict → release claim, stay queued | OK | `R:4799/4803/4809`; the worker must catch the class itself, not rely on the HTTP handler |
| §3 one absolute deadline; remaining budget | OK | `compose()` has no deadline parameter (`CS:4112-4127`); deadline computed at `CS:4163`; planner `CS:5382`. Needs a new parameter on `compose()` and `_plan_and_stage_empty_pipeline` |
| §3 worker checks → terminal envelope with today's meaning | OK | Both raise inventories. Their limit: the compose-loop closure is mapped by exception class, not per site |
| §3 extract `_track_compose_inflight` policy; guided identical | OK | lifecycle §2 and §9; the timer seam `_COMPOSER_HEARTBEAT_TIMER` (`H:2363`) |
| §3 typed worker context before 202; no `Request` | **GAP → I8** | |
| §3 disconnect watcher removed; audit + `uncancel` preserved | OK | lifecycle §3.1; recompose D8 asymmetry (`C:792` shield vs `M:1102` join) |
| §3 worker renews independently of polls | OK after I2 | Precedent: execution loss watcher |
| §3 shutdown drains; startup/periodic reaper; no resume | OK after I5 | app-lifespan §1.6 slots, plus the run-recovery template below |
| §3 config decoupling; guided cap; ECS/tests/docs | OK | app-lifespan §2-§6. Beyond the spec it adds: the ACA `validate-workload-parameters.jq:54`, and the synchronous `explain_run_diagnostics` (`CS:4099`) and `proposals.py:318` consumers |
| §4 cancel endpoint; queued-unclaimed settles in-tx; owned → 202 | OK after I2 | The run-cancel precedent returns `{status, cancel_requested}` |
| §4 local owner signalled; remote observes on renewal | OK after I2 | |
| §4 one token-checked terminal CAS; reaper same compare | OK | CAS precedents (schema §9) |
| §4 **public response + terminal in the same tx as final publication** | **GAP → I7** | |
| §4 a retry never creates another user row | OK | Follows from "no side effect before `running`" plus no running takeover. send R16 is a nice-to-have (record the user-message id) |
| §5 one transport; exact success reducers | OK | frontend §3, §4, including the send/retry differences table |
| §5 reload resume, reattach, second action disabled | PARTIAL | No per-session "active operation" lookup exists in the spec. `/_active` is unused by the SPA and returns no operation ids. Decide between a new lookup and an accepted limitation |
| §5 Stop → cancel; deadline = server deadline + grace; poll backoff | OK | frontend §5. `runComposeWithTimeout` is shared with guided (`ChatPanel.tsx:842-857`), so freeform should stop calling it rather than change it |
| §5 error semantics preserved; parse from body | OK | `parseResponse` must split into a pure `(status, body) → ApiError` without the 401-logout side effect (frontend §1) |
| §5 every raise path inventoried | OK (with limits) | Send 19 and recompose 16 Raise nodes, mutation-controlled |
| Crash window 1 (queue commit, 202 lost) | OK | PK reservation; client 404→null poll (frontend §1) |
| Crash window 2 (202 before claim) | OK | |
| Crash window 3 (dies before `running`) | PARTIAL | The claim-lease duration and scan interval are new settings with no default rationale. The in-tree cadence precedent is `heartbeat_interval_seconds = max(1, lease//3)` (`membership_lifecycle.py:44-48`) |
| Crash window 4 (dies after `running`) | OK after I5 | |
| Crash window 5 (race) | OK | |
| Gate 1 schema/tx on SQLite + serial PG | OK | tests-and-gates §3; `test_schema_probe_postgres.py:649` race template |
| Gate 2 provider held >125 s | **GAP → I9** | |
| Gate 3 kill a worker per window | OK | `test_cross_process_composer_postgres.py:507-537` kill template |
| Gate 4 cancel audit, `cancelling()` in the worker task, heartbeat tests adapted | OK | lifecycle §9 |
| Gate 5 frontend matrix | OK | frontend §10 (8 mock factories, `client.recovery.test.ts` parity) |
| Gate 6 full-suite gate + testcontainer | OK | tests-and-gates §6. Remember `--stages` (the defaults are `ruff,pytest` only) |

### (c.2) Investigated gaps (answers)

**I1. No explorer wrote up the server-side poll and failure templates.**
- `routes/guided_operations.py:678-724` `guided_operation_failure_error` projects a failed guided operation to `{"error_type":"guided_operation_terminal_failure","failure_code",detail}` from a closed `_SAFE_FAILURES` table.
- **This is NOT a template for the freeform envelope.** It collapses outcomes to one `error_type` plus a closed code. Spec §5 forbids that: "does not collapse these to one `operation_failed` code".
- The freeform terminal row must store the *full public error body* (the `detail` dict or string plus `http_status`) exactly as the route or app handler would have rendered it.
- The guided reconcile route `POST /{sid}/guided/start/{op}/reconcile` (`routes/composer/guided.py:1318-1360`) is a *read-mostly* reconcile. Ordinary reads never take the compose lock. It mutates only after taking the compose lock and SOL for an expired row. That is the right shape for an "expired queued row found on poll" repair, if the plan wants the poll to settle a deadline-expired queued row without waiting for a scan.
- **The poll-route template is `get_composer_progress`** (`routes/composer/state.py:524-549`, re-verified):
  - `_verify_session_ownership`, then the read;
  - `ComposerProgressIdentityInactive` → `401 "Invalid token"`;
  - `ComposerProgressSessionUnavailable` → `404 "Session not found"`;
  - never a 500.

**I2. There is an existing 202 + background-owner + remote-cancel precedent: run execution.** None of the eight files found it.
- `POST /api/sessions/{session_id}/execute` is declared `status_code=202` (`execution/routes.py:1009-1011`). It is the only 202 in `src/elspeth/web`: `grep -rn "status_code=202\|HTTP_202"` gives 1 hit, and the control `status_code=201` gives 3.
- The route acquires `SessionOperationLease(EXECUTE)` in the request task (`execution/routes.py:1032-1039`). It then **transfers** the lease to `ExecutionServiceImpl.execute(session_operation_lease=...)` (`execution/service.py:1661-1757`), guarded by a `transferred` flag.
- `execute()` spawns `loss_watcher = asyncio.create_task(self._signal_shutdown_on_operation_loss(...))` (`:1715`).
- The watcher (`:2663-2700`) does `asyncio.wait_for(lease.wait_until_lost(), timeout=poll)` with `_LOSS_WATCHER_POLL_SECONDS = 0.25` and `_LOSS_WATCHER_MAX_BACKOFF_SECONDS = 5.0` (`:229-230`). On timeout it reads the run row, "**the only path by which a cancel persisted on another replica reaches this worker**" (comment in `:2682-2690`). It retries transient DB errors with backoff and does not cancel the run on a flaky DB.
- This is exactly the spec §4 mechanism: "a remote owner observes the marker on its next bounded renewal", and "loss of either fence cancels work". The worker's per-job watcher should copy it (both `wait_until_lost` and a cancel-marker read).
- The status and cancel routes also give the poll/cancel wire precedent:
  - `GET /api/runs/{run_id}` (`execution/routes.py:1309-1343`) checks ownership, loads status, and projects integrity failures to a typed HTTP error;
  - `POST /api/runs/{run_id}/cancel` (`:1533-1549`) is "idempotent on terminal runs" and returns `{"status", "cancel_requested"}`.
- Shutdown precedent: `ExecutionServiceImpl.shutdown` (`execution/service.py:1626-1658`) joins `_lease_completion_futures`.

**I3. Dual fence ("every worker write requires SOL + the job running fence", spec §1).** Measured:
- `SessionOperationContext` is a 2-field frozen dataclass (`contracts/session_operation.py:47-50`: `fence`, `operation_kind`). It is constructed at **6** sites (`LC:144`; `R:4835,4911,5451,5479,5511`). It is type-checked with `type(x) is [not] SessionOperationContext` at **49** sites in `src/elspeth`.
- The DB-side check is one method, `SV._require_session_operation_context_on_connection` (`SV:4931-4965`), which already takes an optional secondary `guided_fence`. It is called at 10 sites. `_session_composer_mutation_transaction` (`SV:4987`) has 16 call sites.
- But turn writes also go through modules outside `sessions/service.py`. Counts of `session_operation_context` occurrences: `composer/service.py` 174, `blobs/service.py` 83, `composer/tool_batch.py` 15, `composer/tools/blobs.py` 10, `composer/pipeline_custody.py` 10, `advisor_audit.py` 8, `_auto_title.py` 9.
- The guided dual-fence precedent shows what a full sweep costs. `BlobGuidedOperationWriteFence` (`contracts/blobs.py:155`) plus `guided_fence` is threaded through `sessions/service.py` (67 hits), `blobs/service.py` (13), `coordination/repository.py` (9), `composer/pipeline_custody.py` (8), `sessions/protocol.py` (9), `composer/pipeline_planner.py` (4) and `composer/service.py` (3).
- **Design fork the plan must choose.**
  - (A) Thread a job fence beside the context through every writer (guided-style; large).
  - (B) Bind the job's running fence *to* the SOL fence. At `running`, record the SOL `(operation_id, lease_token, operation_epoch)` on the job row. Let job-lease liveness be SOL liveness: renew the job only while the SOL renews, and let the reaper settle `running` only after acquiring SOL COMPOSE itself, as in I5. Then every existing SOL check is also a job check, and only the terminal CAS needs the extra token compare.
  - (B) meets the spec's "loss of either fence cancels work" only if the job fence cannot be lost while the SOL is live. That holds if the job's running lease is never shorter than, or independent of, the SOL.
  - **The in-tree pattern that makes (B) concrete is `SessionOperationLease.adopt`** (`LC:426-440`, docstring: "Adopt a context atomically minted by a composite authority method. Cancellation cannot orphan a context after its compare-and-swap reached the worker"). Its only in-tree use is through `adopt_fork_child` (`routes/sessions.py:1063`), where a composite authority method stages the fork and mints the child context in one transaction. For compose, one composite authority method would do the job claim-token CAS, mint the SOL COMPOSE context, and write `running` with the SOL fence identity in a **single** transaction. The worker then `adopt`s the returned context. That is spec §3's "obtains `SessionOperationLease` ... then atomically marks the job `running`" without a gap in which SOL is held but the row is still `queued`. Caveat: the preconditions spec §3 wants checked *before* `running` (ownership, transcript, stale `state_id`) would then run inside that composite transaction, or `running` is written in a second CAS after the checks.

**I4. SOL renewal does NOT re-check identity or ownership; only the CRL heartbeat does (and only on PostgreSQL).**
- `R.renew` (`R:5064`) CAS-es on `_exact_active_predicates` (`R:4955-4968`): session_id, operation_id, lease_token, epoch, kind, `released_at IS NULL`, `lease_expires_at > now`. It checks no identity and no `sessions.user_id`/`archived_at`.
- `SessionComposerProgressAuthority.heartbeat_request` (`composer_progress_authority.py:267-287`) runs `_ownership_query` (`:32-38`: `sessions.user_id == user`, `archived_at IS NULL`, `FOR UPDATE`) and `_identity_query` (`:41-47`: `identities.access_state == 'active'`, `FOR UPDATE`). It raises `ComposerProgressSessionUnavailable` / `ComposerProgressIdentityInactive`, which the heartbeat maps to `LEASE_LOST` → cancel (`H:2566-2577`).
- The in-memory SQLite registry does neither (lifecycle §4.1).
- So today, on PG, deactivating a user or archiving the session mid-turn cancels the freeform turn within about 15 s. If the worker drops the CRL (contradiction (b)1), **that stops**. The turn then runs to its budget and fails only at a fenced write, if the archive takes the fence. The plan must either keep the CRL, add the same two queries to the job-renewal CAS, or record the behaviour change.
- Note also that `_ownership_query` does not compare `auth_provider_type`, while `_verify_session_ownership` does (`H:2804`).

**I5. Reaper template: `RunRecoveryCoordinator` (`execution/recovery.py:129`).**
- `recover()` (`:149-154`) lists candidates, skips runs live in this process (`get_live_run_ids()`), and calls `_recover_candidate`.
- `_recover_candidate` (`:236-300`):
  - **acquires the session's `SessionOperationLease` itself**, EXECUTE kind;
  - treats `SessionOperationConflictError` as "owner alive, skip" and `FenceLossReason.OWNER_INACTIVE` as "archived, skip";
  - re-reads the row, calls `lease.guard_external_effect()` before acting, and projects the terminal through `session_operation_authority.mutate(lease.context, lambda tx: ...)`.
- It is called at boot (`A:900`) and from the periodic sweeper (`A:388-477`, created at `A:904`).
- Applied to compose: a reaper that settles an expired `running` job as `worker_lost` should first acquire SOL COMPOSE on that session. Success proves the dead owner's fence has lapsed and fences the settle write. Conflict means a live owner, so skip. This fits design (B) in I3.
- `SessionOperationAuthority.mutate(context, fn)` (the `R` typed mutation transaction, `tx.runs.*`) is a second possible home for job-row writers besides `SV`. Choosing it changes which `TablePolicy`/authority the mutation-authority gate needs (tests-and-gates §1.1).

**I6. Error envelope identity fields.**
- **No diagnostic-id mechanism exists.** `grep -rn diagnostic_id src/elspeth/web` gives 0. Control: the `"diagnostic"` reason key exists at `A:1392`. The spec's "server-side diagnostic ID" is new surface.
- `request_id` in every dict-detail error comes from `RequestIdMiddleware` (`src/elspeth/web/middleware/request_id.py`; it may be caller-supplied via `X-Request-ID`, validated grammar). It is read by `_request_id(request)` (`A:2045-2060`) and injected by `handle_http_exception` (`A:2247-2266`).
- A worker has none. The SPA names `request_id` in audit-integrity copy (frontend §3, `formatAuditIntegrityError`). The plan must choose one of: the POST's `request_id` persisted on the row as "safe request metadata", the poll GET's `request_id`, or the new diagnostic id. If the POST's is chosen, it must be persisted, because a remote instance may run the job (see I8).

**I7. The single terminal transaction (spec §4) is new surface, not plumbing.**
- `SV.list_composition_proposals` (`SV:8320-8370`) opens **its own** `self._engine.connect()` inside `_sync`. It enforces Tier-1 proposal checks: exactly one creation event, and `_classify_authoritative_composition_proposal` plus `_verify_pipeline_lifecycle_authority`. There is no connection-parameterised variant.
- `_pending_proposal_responses` (`H:454-459`) is that read plus a pure projection (`sessions/guided_replay.py:69`).
- So "prepare the validated DTO, then commit terminal + final publication in one transaction" needs an `_on_connection` refactor of this read (plus `add_message` / `_persist_turn_audit_cohort` bodies), inside a new service method.
- **A fact that bears on the ruling:** the worker holds SOL COMPOSE exclusively for the whole turn, and every other session operation (PROPOSAL/EXECUTE/ARCHIVE/other COMPOSE) is refused while it is live (`R:4764-4768` docstring: BLOB_READ is the one shareable kind; lifecycle §5). So pending proposals cannot change between the last publication commit and a *separate* SOL-fenced terminal CAS.
- A separate terminal transaction therefore yields the same DTO. Its only cost is a crash window "published but not terminal" → reaper `worker_lost`, which the spec's crash table already tolerates ("existing fenced writes and audit remain visible on reload"). Spec §4's sentence, however, demands the same transaction.
- **The plan needs John's ruling:** build the composite method, or amend §4 to "the terminal CAS runs under the same live SOL fence after the final publication".

**I8. The worker context must be rebuildable from the job row alone.**
- The spec says to "build a typed worker context ... before returning 202". But §3 lets *any* instance claim a queued row, so a remote worker has only the row. Everything the turn needs must therefore be in the row or derivable server-side. Measured needs:
  - `UserIdentity` (`web/auth/models.py:21-31`) is `user_id: str`, `username: str`, both non-blank-validated. The turn body reads **only** `user.user_id`: `messages.py` 11 reads, `compose.py` 9, `pipeline_settlement.py` 5, `user.username` 0 in all four files. Control: `user.username` is found in `auth/routes.py`, `auth/admin_routes.py`.
  - But `settle_auto_commit_intent`/`settle_pipeline_proposal_under_compose_lock` take `user: UserIdentity` (`pipeline_settlement.py:450,176`), and `plugin_snapshot_factory(user)` delegates to `for_user_id(user.user_id)` (`plugin_policy/availability.py:263-266`). `compose()` builds its own plugin context from `user_id` (`CS:4169`, `self._plugin_policy_context(user_id)`).
  - So either persist a username on the row, or change the settlement signatures to `user_id` (a signature change also hits `proposals.py:318`, per recompose R-5).
  - `auth_provider_type` comes from `settings.auth_provider`, which is instance config and assumed equal across instances.
  - For send, `chat_ingress` is computable from the DTO (`M:143`). For recompose it comes from the transcript under the lease (`C:154-157`).
  - `request_id` / diagnostic: see I6.

**I9. Gate 2 ("provider held beyond 125 seconds") has no harness in the tree.**
- There is no middlebox or proxy-cut simulation: `grep -rn "Cloudflare|middlebox|proxy.*cut" tests` found only unrelated hits (a DNS literal, and a provider-name list in `test_llm_provider_served_audit.py:58`).
- The Playwright backend has **no LLM stub** for compose: `compose-happy-path.spec.ts:17-27` is marked "blocked: needs Playwright LLM stub server — tracked as elspeth-617e1ca703". The only e2e that touches `POST /messages` fulfils it with a mock (`composer-proposals.spec.ts:184`).
- `compose()` takes its deadline from `asyncio.get_event_loop().time()` (`CS:4163`), so a real 125 s hold means a real 125 s wait unless the loop clock is faked.
- Gate 2 therefore needs an explicit harness decision:
  - (i) an in-process single-loop `httpx.AsyncClient` test whose POST returns 202 while `_BlockingRecordingComposer` (`test_routes.py:453`) parks. "Beyond 125 s" is then proved structurally: the POST completes before the provider is released, and the client's POST connection is closed. This is not wall time.
  - (ii) a marked slow test with a real timed hold.
  - The plan should state which one satisfies "held beyond 125 seconds".
- Combine with tests-and-gates §0: `SyncASGITestClient` runs each request on a fresh loop (`_sync_asgi_client.py:41-57`), so the worker must be started explicitly in the test's loop.

**I10 (smaller). SQLite single-process assumption.**
- The packaged `elspeth web` calls `uvicorn.run("elspeth.web.app:create_app", host, port, reload, factory=True, access_log=False)` with no `workers` (`cli.py:5047-5054`). `SingleProcessWebInstanceMembership` (`coordination/membership_lifecycle.py:84`) documents "No peers".
- No guard stops an operator running raw `uvicorn --workers N` on SQLite. That is out of scope, but the SQLite CAS claim should not claim cross-process safety beyond what `BEGIN IMMEDIATE` gives.

---

## (d) Residual unknowns the plan must carry as task-0 measurements or rulings

Measurements (run immediately before task 1; record the raw output):

- **M1 Epoch.** Re-read `models.py:366` and `schema.py:40` (both 67 now). The new table takes 68 unless another lane landed first. Recheck again right before the bump commit, which is the last commit per precedent.
- **M2 Live-run branch.** Is `fix/session-ed3c015b-convergence` (recent-changes §9) merged into `release/0.8.1`? If so, re-anchor `ChatPanel.tsx`, `ComposingIndicator.tsx` and `composer/service.py` lines.
- **M3 Web-review remediation lanes.** Have they started? Lane B claims exclusive ownership of `messages.py`, `compose.py`, `_helpers.py` and `sessions/service.py`. R05 fixes a lock order: session → provider attempt → identity/ledger (`docs/plans/2026-09-23-web-review-remediation/persistence.md:33-35`). The new admission and claim transactions must fit that order and take no identity lock before the session lock. Note that that plan's epoch note (65) is stale.
- **M4 Lints baseline.** Take the finding *set* (not the count) at the landing tip, key-free (tests-and-gates §4). Expect new R5 findings when the route bodies' `isinstance` leave FastAPI handlers, and R7 churn from `C:792/809`.
- **M5 Mutation-authority gate.** Record the xfail drift numbers of `test_all_production_sessions_writers_are_reviewed_typed_authorities` at base (schema §6: 83 / 44 / 8) so drift is compared, not zeroed.
- **M6 Result-JSON size.** Measure the maximum serialized `MessageWithStateResponse` on a realistic session DB (state with `composer_meta`, plus all pending proposals) before choosing a `length(result_json) <= N` CHECK, or deliberately none. No bound is proposed by any explorer.
- **M7 POST-site count.** Re-count `/messages` and `/recompose` callers including variable-held URLs and raw-ASGI scopes (131 is a lower bound). Include the production acceptance probe `web/azure_container_apps_observations.py:306-313` and `evals/composer-battery/drive_battery.py:384`.
- **M8 Focused-gate green baseline.** Re-run the 129-test focused set from tests-and-gates §1.4 at the landing tip, as the negative control.

Rulings needed from John (not inferable from the tree):

- **R0 Direction.** 202 + poll vs the "streaming interface rewrite" named in lane notes 37 minutes earlier (recent-changes §0). Confirm before task 1.
- **R1 CRL** keep or drop for freeform. This includes the identity-revocation mid-turn cancel (I4), `inflight_requests`, and the `/_active` semantics.
- **R2 Terminal transaction**: a composite method, or a separate SOL-fenced CAS (I7).
- **R3 Dual fence**: design (A) or (B) (I3).
- **R4 Rate limit**: stays pre-202? A replay of an existing `operation_id` bypasses it (both inventories recommend lookup-before-charge).
- **R5 Capacity**: scope (global vs per-instance) and the setting names/defaults. Also the claim-lease and scan-interval settings.
- **R6 Error identity**: which `request_id` goes in terminal envelopes, and the diagnostic-id format (I6).
- **R7 Recompose custody arm**: adopt send's `GuidedCustodyIntegrityError` → `failed_turn` arm (a public-body change) or keep the app-handler shape (recompose D6).
- **R8 Row context fields**: persist username vs change settlement signatures to `user_id` (I8). Also the actor FK mode (RESTRICT / CASCADE / none).
- **R9 Archive vs a queued job**: refuse archive (mirroring guided) or let delete cascade.
- **R10 SPA custody**: sessionStorage (same tab) vs localStorage (cross-tab reattach), and whether a per-session "active operation" lookup endpoint is added.
- **R11 `state_id`** 404: keep it as a worker check (spec) or hoist it pre-202 (send R7).
- **R12 Convergence `timeout_seconds`**: configured budget vs remaining budget (recompose Q4).
- **R13 Guided cap reach**: whether `explain_run_diagnostics` (`CS:4099`) and proposal-decision settlement (`pipeline_settlement.py:273` via `proposals.py:318`) take the transport-safe cap, and whether the ACA `validate-workload-parameters.jq:54` and the bicep description move with the ECS cap.
- **R14 SOL lost only at close** after a success is built (send R4, recompose R-2): does the terminal CAS precede the lease close (success published) or follow it?

## Instruments used in this pass (with controls)

- 202 search: `grep -rn "status_code=202\|HTTP_202" src/elspeth/web` → 1 hit. Positive control `status_code=201` → 3 hits.
- `SessionOperationContext(` constructors → 6 (the class line excluded). Exact-type checks → 49.
- User-field reads per file: `user.user_id` hits (11/9/5/2) vs `user.username` 0. Positive control for `user.username` in `auth/routes.py`.
- `diagnostic_id` → 0. Positive control for the `"diagnostic"` key at `A:1392`.
- Frontend `_active` consumer → none (only unrelated identifiers matched: `sole_active_admin`, `no_active_session`, `run_already_active`).
- e2e specs touching `/messages`: 4 files matched; only `composer-proposals.spec.ts:184` handles a POST.
