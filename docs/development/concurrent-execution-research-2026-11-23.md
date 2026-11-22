# Concurrent execution in the web server: runs, rows, long-lived sinks, replicas

Research and recommendation, 2026-11-23, for 0.8.17 (0.8.18 at the latest); scope item I13 in
`docs/development/0.8.17-deferred-scope.md`. No build is implied until the maintainer rules on it.
The tree was read at `06c9cd408`, read-only; nothing was executed except one library-default probe
and searches. Paths are under `src/elspeth/` unless stated. "The run-status bridge redesign" refers
to the separate fix that gives the bridge a 60 s give-up bound and hands unsettled runs to recovery.

Labels: **MEASURED** = read at the cited file:line, or a read-only command whose output is shown.
**INFERRED** = a conclusion from measured facts or general platform knowledge, not executed.
**OPEN** = not established; needs a measurement or a maintainer decision.

## Summary
1. One run thread per process, server-wide (MEASURED `service.py:1286`). The server already holds many runs at once (one per session, each with its lease), but runs them one after another.
2. Simply raising the thread count is unsafe because of three hidden couplings: quota admission refuses a call as "accounting unavailable" whenever another fence has a provider call in flight (under a token quota, concurrent runs would refuse each other, and so do concurrent composer users today); the provider rate limiter is per run and in memory, so N runs send N times the configured rate; database pools are sized for one run.
3. One session should never run two pipelines. The database enforces it (exclusive fence plus `uq_runs_one_active_per_session`), and a queued run keeps its session locked against editing.
4. Inside a run: rows go through transforms one at a time, sinks are written only after the whole source loop, and sink writes are leader-only. A minutes-per-row sink holds its worker for rows × minutes.
5. Long-life-save already exists in the engine: a durable sink-effect ledger, reconcile before commit, UNKNOWN refuses to resend, and a 5-minute per-effect lease with its own heartbeat thread.
6. The 60 s bridge, the 30 s lease and the 30 s pool timeout do not cut off the send itself. What does is the platform stop grace: 60 s on Azure Container Apps, 30 s watchdog, ECS default (INFERRED).
7. Multi-container: ECS is one replica, zero-overlap; Azure Container Apps is 2–4 sticky replicas. The database is the only cross-replica coordinator. The run queue and the provider limiter are per process.
8. Recommendation: a server-owned bounded run pool with fair per-user admission, default 1. Fix the three couplings before raising it, then add a fleet cap on PostgreSQL. The long-life-save track needs a sink-author contract now and an ADR later for in-loop sink writes with back-pressure.
9. Needs a ruling: quota semantics (may need a schema column), whether durable "pending until sent" counts as buffering, the in-loop sink ADR, and the default worker count.
10. Biggest gap: memory per run is unmeasured, and it decides the right number of workers per container.

## Executive summary (for non-specialists)

**Where we are.** The web server runs every pipeline on one worker thread, shared by every user and session (MEASURED, `web/execution/service.py:1286`). If one person starts a long run, everyone else's run waits behind it. The composer keeps working for other users; only runs queue.

**Why "just add more workers" is not enough.** The question covers four problems that affect each other:

1. **(a) Across runs.** Different users' runs should progress on their own; today they queue on one thread. The fix is a bounded pool of run workers with fair admission between users. Hidden couplings must be fixed first: token-quota admission, provider rate limits and database connection budgets all assume, without saying so, that only one run executes at a time.
2. **(b) Inside one run.** A run processes its rows one after another and writes to its destinations (sinks) only after every row has been processed. A destination that takes minutes per row, such as the fictional VLF relay, makes a run of dozens of rows take hours and holds its worker all that time. Extra workers help other users, not this run. With enough slow runs, every worker ends up waiting on a slow destination. Caps on concurrent slow sends and per-user limits decide who waits.
3. **(c) Durability ("a long life save").** A row is not done until its destination confirms it, and the engine already works this way. Every destination write is recorded before it is attempted and after it lands, and the row is finished in the same database step. After a crash, the engine first asks the destination whether the write landed and resends only if it did not. If the destination cannot answer, the engine refuses to guess and asks an operator. The timers recently tuned around 30–60 s do **not** cut off a slow destination call. But no shipped container platform waits minutes before force-stopping a process. A cut-off minutes-long send can be resolved safely only if the destination can be asked "did message X go out?".
4. **(d) Several containers.** AWS ECS/Fargate ships one container at a time. Azure Container Apps ships 2–4 replicas with sticky sessions. Each container already has its own single run thread, so N replicas already run N pipelines in parallel. Cross-container safety (who owns a session, a run, a destination write) already lives in the shared PostgreSQL database. In-memory things (the run queue, provider rate limits, live progress) do not cross containers. Capacity should grow both ways, more workers per container and more containers, under one database-enforced limit.

**Recommendation.** The **server, not the composer**, owns a bounded run-worker pool, sized by one operator setting. The default stays at today's 1, so an upgrade changes nothing until an operator opts in. Runs are admitted fairly (one active run per user by default), and today's rule of one active run per session stays. Waiting users can see their run is queued. Before raising the default, fix three couplings:
- quota admission refuses work whenever another session has a provider call in flight;
- per-run in-memory rate limiters multiply provider traffic by the number of runs;
- database pools are sized for one run.

Long-lived destinations are their own design: a written contract for long-life-save sinks first, then (with an ADR) sink writes that start before the source finishes, with a cap on sends in flight. Ship in stages; the first stage changes no behaviour.

## 0. Verified fixed values

| Value | Where | Label |
|---|---|---|
| `ThreadPoolExecutor(max_workers=1)`, hardcoded, server-wide | `web/execution/service.py:1286`; docstring `:1186-1188` states the thread model only | MEASURED |
| First introduced | `git log -S'max_workers=1'` → `de732358a 2026-03-30` Sub-5 execution layer | MEASURED |
| `_call_async` waits `future.result(timeout=30.0)` | `service.py:1901-1913` (the 60 s give-up bound is the run-status bridge redesign, not on this tree) | MEASURED |
| EXECUTE lease TTL 30 s | `web/sessions/service.py:832` | MEASURED |
| Renewal `min(lease/3, 30)` = 10 s, on the event loop | `web/coordination/lifecycle.py:288-290` (loop placement from the bridge redesign's evaluation) | MEASURED / READ |
| PostgreSQL pools 5+5 + pre_ping, for the session engine AND every per-run Landscape engine | `web/schema_probe.py:77,85-89`; `web/landscape_access.py:36-41` | MEASURED |
| QueuePool defaults | `.venv/bin/python -I -c ...QueuePool.__init__...` printed `2.0.45 30.0 5 10` | MEASURED |
| No pool/statement/lock timeout on the session or Landscape engines | grep hits only `readiness.py:215,228`, `schema_probe.py:256`, `core/landscape/run_coordination_repository.py:129-130` (SET LOCAL 4 s/5 s on one coordination statement) | MEASURED |
| SQLite `busy_timeout=5000` | `web/sessions/engine.py:139`; `core/landscape/database.py:119-125` | MEASURED |
| Async pool: 16 threads + 16 queued, 1.0 s admission wait | `web/async_workers.py:36-46` | MEASURED |
| `composer_async_worker_concurrency` default 4 (1..16) | `web/config.py:421` (the "32 slots" is the async pool, not this setting) | MEASURED |
| Recovery sweep every 300 s (minimum 30) | `web/config.py:622` | MEASURED |
| Landscape run heartbeat 15 s, liveness window 80 s, own thread | `contracts/coordination.py:51,59`; `engine/orchestrator/heartbeat.py:144` | MEASURED |
| Sink-effect lease 5 min, heartbeat thread every TTL/3 | `engine/executors/sink_effects.py:296,146`; `engine/executors/sink.py:913-925` does not override it | MEASURED |
| Watchdog SIGTERM then SIGKILL after 30 s | `web/process_watchdog.py:40` | MEASURED |

## 1. (a) Across runs

### 1.1 Already safe for more than one run
The server **already admits many concurrent runs**, one per session, and only their execution is serialised. Each queued run holds its own EXECUTE lease, obligation, loss watcher and shutdown event (MEASURED `service.py:2286-2418`). The new thing would be run bodies executing at the same time.
- Per-session exclusivity is in the DB. The `session_operation_fences` primary key is session_id (`web/sessions/models.py:339-365`). A live fence raises `SessionOperationConflictError` → 409 (`web/coordination/repository.py:5204-5232`; `web/session_operation_handlers.py:54-56`). Also `RunAlreadyActiveError` (`service.py:2435-2437`) and the partial unique index `uq_runs_one_active_per_session` on pending/running (`models.py:2151-2155`). MEASURED.
- Landscape writes: an exact `CoordinationToken`, fenced first (ADR-048 §1-4). Each run opens its own LandscapeDB (`service.py:3797,4389`). MEASURED.
- Progress broadcaster: keyed by run_id, threading.Lock, `call_soon_threadsafe` (`web/execution/progress.py:84-108,189`). MEASURED.
- Shutdown sets every run's event (`service.py:2022-2030`); `executor.shutdown(wait=True)` (`:1952-1956`) does not care about worker count. MEASURED. Queued futures starting and seeing their event: INFERRED.
- Lease-release registry is obligation-keyed and binds one pool object (`web/execution_lease_cleanup.py:660`). MEASURED. No change needed for N>1: INFERRED.
- ContextVars for the plugin manager, HTTP deadline and preflight are per thread (`plugins/infrastructure/manager.py:334`, `clients/deadline.py:317`, `preflight.py:9`). No signal handlers off the main thread (`service.py:4542-4544`). MEASURED.

### 1.2 Assumes one run, or degrades with N
- **A1 Quota.** `_daily_token_total_on_connection` returns None (unknown) if any unsettled provider attempt exists for the identity, or for any identity under a container ceiling, outside the caller's own fence (`web/coordination/quota_authority.py:414-443`). `assess` then refuses `TOKEN_ACCOUNTING_UNAVAILABLE` (`web/coordination/chargeable_admission_authority.py:141-150`). Runs begin an attempt before every LLM call and settle it after (`service.py:4530,5246-5283`); composer calls do too (`web/composer/provider_quota.py:235,489`). MEASURED.
  - Effect: with a token quota configured, run B's LLM call is refused while run A has one in flight; under a container ceiling this crosses users. INFERRED.
  - This already bites concurrent composer users today. INFERRED.
  - Fail-closed and deliberate per the docstring (`quota_authority.py:19-21`).
- **A2 Provider rate limiter is per run, in memory.** It is built in `_run_pipeline` (`service.py:4498-4508`), closed at `:5219`, persistence None by default (`contracts/config/runtime.py:55-58`). MEASURED. N runs (and N replicas) send N times the configured rate: INFERRED.
- **A3 DB budget sized for one run.**
  - Each run's Landscape engine is 5+5 on PostgreSQL. MEASURED.
  - The session engine (5+5) is shared by every lease renewal, bridged call and composer request. MEASURED.
  - Session-pool starvation lapses the 30 s lease (from the bridge redesign's evaluation).
  - The target's PostgreSQL max_connections: OPEN.
- **A4 SQLite Landscape is one writer at a time** (WAL, BEGIN IMMEDIATE, 5 s busy_timeout; ADR-030 D5). ADR-030 already supports N writer processes on one WAL file. Max lock hold with N independent runs: OPEN.
- **A5** The cost of a stuck bridged call becomes one slot instead of the whole server (from the bridge redesign). INFERRED.
- **A6** GIL: LLM-bound runs overlap well, CPU-heavy steps (hashing, canonical JSON, validation) do not. INFERRED.
- **A7** The Azure Monitor config is process-global and refuses a differing second config (`plugins/transforms/llm/providers/azure.py:378-410`). This already happens sequentially. MEASURED / INFERRED.
- **A8** The `os.environ` write in `core/security/config_secrets.py:283` has no web caller (grep). MEASURED.

### 1.3 Per session versus server-wide
- Do **not** allow two runs per session. The session is exclusively held for the whole run, including while queued, so a queued user cannot even edit (MEASURED §1.1; lease acquired at `web/execution/routes.py:1014`). Changing this needs the fence and unique index changed: a **schema/CHECK change needing approval**, and not recommended. Two sessions meet the need.
- Concurrency should be server-wide, with a per-user cap. The per-session cap is already 1.

## 2. (b) Inside one run
- Rows are processed one at a time. The BatchTransformMixin docstring says "ACROSS-ROW sequencing: The TransformExecutor blocks on each row's completion" (`plugins/infrastructure/batching/mixin.py:47-56`), and the executor waits on a waiter (`engine/executors/transform.py:590-600`). MEASURED.
- Within-row concurrency only: the multi-query LLM pool (`pool_size`, `plugins/transforms/llm/base.py:427`; `plugins/infrastructure/pooling/executor.py:134`), capped by `concurrency.max_workers` (default 4, `core/config.py:1902-1906`; `transform.py:364-367`). MEASURED.
- Cross-row parallelism exists only as ADR-030 follower processes, started from the CLI (`elspeth join`, `cli.py:4402`), SQLite one-host. The web starts none (grep of `service.py`). MEASURED.
- Sink I/O is leader-only (`engine/orchestrator/follower.py:6,69-71`). ADR-030 D2: distributing sinks "would each need its own ADR". MEASURED.
- Sinks are written after the whole source loop (`engine/orchestrator/leader_drain.py:293-312`; `source_iteration.py:530-534`). MEASURED.
- Sink-bound rows wait as durable PENDING_SINK work items carrying the row payload (`engine/scheduler_drain.py:762-797`; ADR-030 uniformity rule). MEASURED transition; that every web run takes this path is INFERRED.
- Effects within a stream are ordered (predecessor wait, `engine/executors/sink_effects.py:430-492`). MEASURED.
- Consequence for the VLF case (INFERRED): all LLM work happens first, then every row is sent serially, holding the run worker for rows × minutes. No back-pressure is needed today because nothing overtakes the sink. "No early ack" holds. "No buffering" does not, if durable pending counts as buffering. **OPEN: maintainer ruling.**
- Back-pressure, once sinks start during the loop: a PENDING_SINK high-water mark per sink that pauses upstream claims, plus a per-sink in-flight cap; stream ordering kept. This changes ADR-030 D2 timing, so **it needs an ADR**.

## 3. (c) Long-life save versus the fixed bounds

### 3.1 What exists
- Durable sink-effect protocol: "Every possibly observable call is bracketed by the durable sink-effect ledger and final publication is committed atomically with the pipeline audit outcome" (`engine/executors/sink_effects.py:1-7`).
  - States RESERVED/PREPARED/IN_FLIGHT/FINALIZED (`contracts/sink_effects.py:91-95`).
  - Attempts INTENT/RETURNED/RESPONSE_LOST/ERROR (`:177-181`), each recorded as an external call (`effect_call_type`, e.g. HTTP at `plugins/sinks/power_automate.py:77`).
  - MEASURED.
- Reconcile before commit: NOT_APPLIED allows a commit, APPLIED finalizes without resending, UNKNOWN refuses (`sink_effects.py:751-760`; `contracts/sink_effects.py:1517-1572`). MEASURED.
- Per-row member effects. The template is the HTTP flow sink: delivery-ID dedup plus a read-only status call for reconcile (`plugins/sinks/power_automate.py:66-82,351-389`). MEASURED.
- The per-effect lease is kept alive by its own heartbeat thread during adapter I/O, fenced on the run's Landscape token (`sink_effects.py:125-196`). MEASURED.
- Web sinks pass effect-capability admission before a run (`web/execution/preflight.py:385-414`; `engine/orchestrator/preflight.py:572-619`). MEASURED. That a sink without effect support is refused: INFERRED, not traced.
- HTTP calls run under an absolute per-request deadline (`plugins/infrastructure/clients/deadline.py:1-14`). A long send needs its timeout set above its send time. MEASURED / INFERRED.

### 3.2 Each bound against a minutes-long sink call
- **60 s bridge give-up:**
  - The send does not go through the bridge: the sink coordinator uses the run's Landscape factory directly (`sink_effects.py:290-330`). MEASURED; no bridged call inside the send is INFERRED.
  - Post-run web output finalisation does go through the bridge (from the bridge redesign).
- **30 s EXECUTE lease (10 s renewal on the loop):**
  - Renewal does not wait on the pipeline thread; it is affected only when the loop or the session pool stalls (A3).
  - A lapse during a send is noticed only after the send returns. The next `guard_external_effect` refuses (`web/coordination/lifecycle.py:915-934`). MEASURED guard; timing INFERRED.
- **Landscape heartbeat 15 s / 80 s:** own thread, unaffected. MEASURED.
- **Sink-effect lease 5 min:** beats during the call while the DB is reachable. MEASURED.
- **30 s pool checkout:** matters only through A3 contention. INFERRED.
- **Batch wait 3600 s:** applies to batch transforms, not sinks (`mixin.py:40`). MEASURED.
- **Platform stop grace: this is the one that cuts a send.**
  - Azure Container Apps `terminationGracePeriodSeconds = 60` (`deploy/azure-container-apps/workload.bicep:78`, `workload.production.bicepparam:23`). MEASURED.
  - Watchdog SIGKILL after 30 s. MEASURED.
  - The ECS task definition sets no stopTimeout (grep of `deploy/aws-ecs`), so the platform default applies: 30 s by default, at most 120 s on Fargate (INFERRED, general knowledge).

### 3.3 A cut-off send (INFERRED chain, MEASURED links)
1. SIGTERM → drain → every run's shutdown event is set (`web/app.py:1220-1232`; `service.py:2026-2030`). The in-flight adapter call continues.
2. SIGKILL at grace. The effect is IN_FLIGHT with an INTENT attempt and no result.
3. A successor takes the run seat after 80 s and the effect lease after 5 min (`takeover_expired`).
4. Reconcile:
   - a target that can be queried or dedups → exactly once;
   - otherwise → UNKNOWN → no resend → `recovery_required` (`docs/reference/deployment-platforms.md:205-208`).

This is audit-correct. **Missing:**
- a long-life-save sink-author contract: stable delivery ID sent with the message, target dedup or status query, an UNKNOWN procedure (whether one exists: OPEN);
- drain that stops new effects but lets in-flight ones finish within grace;
- the immediate post-give-up recovery pass (a 0.8.17 follow-up of the bridge redesign).

### 3.4 Audit and accounting
- Row completion is atomic with sink finalize, so counters stay exact. MEASURED.
- A long sink holds no provider attempt; only the upstream LLM calls do. MEASURED / INFERRED.
- Recovery projects status, counters and one terminal event, and refuses contradictions (`execution/recovery.py:74-132`, READ, not re-traced).

## 4. (d) Several containers
- **Repo support:**
  - ECS/Fargate: one replica, zero-overlap (min 0 / max 100 / desired 1), EFS (`deployment-platforms.md:24,118-123`).
  - Azure Container Apps: Single revision, sticky, 2–4 replicas, one process each, NFS 4.1 for files (never SQLite), grace 60 s (`deployment-platforms.md:25,148-151`; `workload.bicep:60,67,70,73,78`).
  - All other profiles: one replica, stop-before-start (`deployment-platforms.md:38-40,216-224`; `docs/guides/docker.md:356`).
  - Multiple web processes in one container are refused (`web/app.py:2373-2405`).
  - Sizes: Azure Container Apps 1.0 vCPU / 2 GiB (`workload.bicep:132,135`); ECS 1024–1536 CPU units / 2–3 GiB (`deploy/aws-ecs/terraform/modules/scenario/ecs.tf:159-160`).
  - All MEASURED.
- **Per process:**
  - The run executor: two Azure Container Apps replicas already run two pipelines in parallel; limits are per replica by accident.
  - The lease registry, loss watchers and shutdown events: correct; a dead process's runs go to DB recovery.
  - The async pool 16+16 and composer async 4 (`app.py:2329-2340`).
  - Per-session asyncio.Lock (`service.py:1263-1266`): advisory; the DB fence and index are the guard.
  - The validation memo: advisory; execution never reads it (`service.py:1259-1262`).
  - Progress: in-memory, plus on PostgreSQL durable ordered run events and tickets for peer replay (`app.py:2354`; `deployment-platforms.md:158-163`).
  - Admission rate-limit budgets are shared via PostgreSQL ("Adding replicas does not multiply those shared budgets"); the per-run provider limiter is not (A2).
  - MEASURED; consequences INFERRED.
- **The DB as coordinator** (MEASURED): session fence; one active run per session; Landscape seat with epoch; sink-effect leases; web-instance membership on PostgreSQL, where takeover of an expired fence waits for its owner's membership to lapse (`web/app.py:2277-2296`; `run_recovery_authority.py:118-136` READ, not re-traced); quota ledger. Missing: a fleet-wide run cap and fair queue. INFERRED.
- **Storage and scale-in:**
  - Post-run blob finalisation hashes outputs on NFS/EFS, and recovery re-hashes them (from the bridge redesign's evaluation). This is the step most likely to cross 60 s. INFERRED.
  - Autoscale-in or a stop during a send follows §3.3, and readiness drain cannot extend grace. INFERRED.
  - Zero-overlap ECS rollout interrupts every in-flight send on every deploy, so long-life-save sinks need reconcile-capable targets even without crashes. INFERRED.
- **Scale both ways (INFERRED):**
  - Workers per container are the only lever on single-replica platforms; cheap, but they share the process's pools, hence fix A1–A3 first.
  - Containers add isolation, but multi-replica is supported only on the Azure Container Apps shape.
  - The operator sets the per-container count, bounded by memory and connections, and on PostgreSQL a DB-enforced fleet cap.

## 5. Resource model
- **Threads per run:** pipeline + Landscape heartbeat + sink-effect heartbeat during a send + LLM pool + batch workers (≤ `concurrency.max_workers`). MEASURED parts.
- **PostgreSQL connections:** up to 10 Landscape per run, plus shares of 10 session. MEASURED.
- **Memory per run:** OPEN. Must be measured on a ~25-node graph with file-held prompts.
- **First ceilings (INFERRED):** 4 concurrent LLM-bound runs on 4 vCPU with PostgreSQL; 2 on a 1 vCPU / 2 GiB container.
- **Connections per replica:** about 10 + 10 × runs. 2 replicas × 4 runs ≈ 100, against max_connections (OPEN).

## 6. Saturation and fairness (today)
- Admission is FIFO across all users: no per-user fairness, no queue cap, no position shown. MEASURED.
- Queued runs show `pending` and flip to `running` only when picked up (`service.py:4342-4355`). MEASURED.
- A queued user's session is locked. MEASURED.
- One slow run blocks the replica for rows × sink time. INFERRED.
- Long queues trigger "binding changed while queued; submit again" refusals (`service.py:1875-1878`). MEASURED message; frequency INFERRED.

## 7. Who starts executors
- Runs start only on user request: `POST /api/sessions/{id}/execute` (`web/execution/routes.py:991-1023`), and the tutorial route calls the same `execution_service.execute` (`web/composer/tutorial_service.py:385`). The composer LLM does not start runs (grep; INFERRED absence).
- **Recommendation:** the server owns capacity, and the composer neither sets nor sees it.
  - This is execution capacity, not authoring, so invariant 1 holds.
  - The planner gets no tool or field for concurrency, priority or worker count. Any queue facts shown are a few lines.
  - Invariant 2: no tutorial slot or priority. If the tutorial is slow under load, that is a sizing defect.
  - Keep the YAML `concurrency.max_workers` (within-row) distinct by name from the server run-worker count.

## 8. Recommendation

### 8.1 Shape
1. **(a) Server-owned bounded run pool per process.** Fair admission: per-user cap (default 1); round-robin across users with queued runs; a queue-depth cap with a clear refusal; queue position and wait time surfaced to the user and to telemetry. On PostgreSQL, a DB-enforced fleet cap (a leased counted claim at dispatch, the composer async-claim pattern).
2. **Fix before the default exceeds 1:**
   - A1 quota under concurrency, **needs a ruling**: count in-flight attempts at a reserved estimate, or exclude attempts under any live validated fence of the same identity. Either may need a column, so **schema approval is flagged**.
   - A2: share the provider limiter per process and via PostgreSQL across replicas (the shared-budget authority exists).
   - A3: derive the session pool from N, shrink per-run Landscape pools, and refuse startup above an operator-declared DB connection budget.
3. **(b)+(c) Long-life-save track:**
   - **Now:** a sink-author contract (member effects, stable delivery ID, reconcile by dedup or status query, UNKNOWN goes to an operator) and an operator procedure for an unknown send.
   - **With an ADR amending ADR-030 D2 timing:** sink writes during the loop, a per-sink in-flight cap, a PENDING_SINK high-water back-pressure mark, and a drain that stops new effects but lets current ones finish within grace.
4. **(d):** both axes (workers bounded by container memory and connections; containers where the platform shape is supported) under one DB fleet cap.
5. **Per session:** keep one active run. No schema change.

### 8.2 Settings (proposed; INFERRED values)
- `execution_max_concurrent_runs`: default **1** (today, backwards compatible); 1..8; guidance 2 on 1 vCPU, 4 on 4 vCPU, after measurement.
- `execution_max_concurrent_runs_per_user`: default 1.
- `execution_max_queued_runs`: e.g. 32, refusing past it.
- `execution_fleet_max_concurrent_runs` (PostgreSQL): unset means per-process limits only.
- `execution_database_connection_budget`: unset means no check; otherwise startup refusal when exceeded.
- SQLite: cap at 2 until an ADR-030-D5-style contention measurement.

All new fields default to current behaviour. Persisted data is unchanged in stages 0–1, except the possible A1 column (approval).

### 8.3 Rejected alternatives
- **Raise the constant:** no fairness, A1 makes runs refuse each other, A2 multiplies provider traffic, A3 pools are unsized.
- **Process per run:** best isolation, but leases, the bridge, progress and shutdown custody all assume the run lives in the web event loop. A rebuild with its own ADR; keep it in reserve if memory per run proves large.
- **Containers only:** multi-replica is unsupported on most platforms, it does not fix in-replica queueing, and it multiplies the per-run limiter.
- **Per-session parallel runs:** a schema change for no need that two sessions do not already meet.

### 8.4 Stages
- **0 (smallest safe step, 0.8.17):** settings at default 1; queue position and wait telemetry and UI; slow bridged-call count; measure memory per run. No behaviour change.
- **1 (0.8.17 if A1 needs no schema change, else 0.8.18):** fix A1–A3; fair admission; allow N>1 by setting.
- **2 (0.8.18):** fleet DB cap.
- **3 (docs 0.8.17, test sink 0.8.18):** long-life-save contract, operator procedure and a reference test sink.
- **4 (after 0.8.18):** the ADR for in-loop sinks, back-pressure and drain.

### 8.5 Tests
- **Unit:** fairness, per-user cap, queue cap. Controls: with N=1 the concurrency assertion must go red; with fairness removed the starvation test must go red.
- **Integration (SQLite):** two users, a fake slow sink held past the bridge bound, on a controlled clock; both runs reach terminal status, with audit, counters and token ledger equal to the single-run baseline; mark `timing_sensitive` where wall clock is real.
- **SQLite contention:** maximum lock hold with N runs plus dashboard reads.
- **Testcontainer (`-m testcontainer -n 0`):** N runs under session-pool pressure (lease renewal survives or fails closed); concurrent quota attempts (extend `test_chargeable_admission_postgres.py`, `test_quota_integrity_postgres.py`); shared provider limiter across processes (extend `test_cross_process_rate_limit_postgres.py`); fleet cap across two app instances; kill during an IN_FLIGHT member effect, with reconcile APPLIED (no resend), NOT_APPLIED (one send) and UNKNOWN (recovery_required), building on `test_cross_process_execution_handoff_postgres.py` and `test_cross_process_run_reconciliation_postgres.py`.
- **Tutorial:** a live run under load against base, with no special path.

### 8.6 Approvals and rulings needed
1. A1 quota semantics (possible schema column).
2. Whether durable "pending until sent" counts as buffering.
3. The ADR for in-loop sink writes and back-pressure.
4. Any change to one-active-run-per-session (recommended: none).
5. The post-stage-1 default. **Ruled 2026-11-23: the default is 1, as policy;** operators raise it.
   Also ruled: 0.8.17 may ship the concurrent path built but not seriously tested, to be examined
   later. The default-1 path must keep working unchanged, and operators are told that values above 1
   are not yet validated.

### 8.7 Outcome of the technical review of §8.6 (2026-11-23)
A systems-thinking and solution-architecture review found four of the five items to be technical,
settled by the system's own contracts rather than by policy:
1. Quota semantics: technical. A design that needs no schema change is being prepared. Ruled
   2026-11-23: calls in flight settle against the quota as they already do within one run; no
   per-call reserve setting.
2. Durable "pending until sent" is not buffering. A row waiting for its sink is a non-terminal
   state, the run cannot complete with it outstanding, and the row's outcome is recorded only with
   the sink's confirmation (ADR-019). The long-life-save contract will say: a row waiting for its
   sink is recorded as waiting, never as done.
3. In-loop sink writes are not needed to meet the requirement: they change when sends start, not
   how long a slow sink holds its run slot. Deferred until the first real long-running sink; the
   sink-author contract is the part to do now.
4. Keep one active run per session; no schema change.
5. Ruled as policy (above).

The review also found: the quota coupling (A1) affects concurrent composer users today, not only
concurrent runs; every run LLM call uses the shared session database pool (admit and settle), so
pool load scales with LLM calls rather than runs; and the proposed fair-admission defaults in §8.2
(per-user cap 1, queue cap 32) would be new refusals at the default of 1. Ruled 2026-11-23: both
default to off; at the default, runs stay first-come first-served as today.

## Limits of this research
- Nothing was executed except the QueuePool default probe and greps.
- Memory per run, SQLite lock hold under N runs, and PostgreSQL max_connections are unmeasured.
- The ECS stop default is from general knowledge.
- Not traced: that sink-effect admission refuses sinks without effect support.
- Lines labelled "READ" or "not re-traced" come from the bridge redesign's documents, not a fresh reading.
