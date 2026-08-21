# Test-harness scout — unified lineage / barrier scopes (spec rev 3.2)

**Date:** 2026-08-21 · **Scout for:** `docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md`
**Line numbers verified at HEAD `3ca6516e82`** (release/0.7.2). All paths repo-relative;
`src/` prefix omitted for `src/elspeth/**`.

---

## 1. tests/e2e/recovery/ — the six suites the plan will extend

**Shared foundation: `tests/e2e/recovery/harness.py` (972 lines).** Two distinct machines
live here and every suite composes them:

- **Crashed-run construction** (`_run_to_interrupted_checkpoint` → `_CrashedRun` →
  `_craft_crashed_lease` → `_resume`): runs the REAL pipeline (real Orchestrator, real
  checkpoint writer, real scheduler journal, injected `MockClock` at `_T0 =
  1_750_000_000.0`) to an interrupted-but-checkpointed state, then crafts the kill
  instant through production Tier-1 writers only (`RecorderFactory.data_flow.create_row`
  / `create_token`, `TokenSchedulerRepository.enqueue_ready` + `claim_ready`). The
  pipeline it builds is **hardcoded linear** (`_build_pipeline`: `_InterruptibleSource` →
  queue `inbound` → `_PassthroughTransform` → `CollectSink`). The checkpoint-coupled
  entry seams are deliberately confined to `_recovery_manager` / `_resume_point` /
  `_resume` so their shape can change without touching test assertions.
- **Process-seam spawn machinery** (`spawn_database_process_at_seam` /
  `spawn_database_process_with_pause` → `SpawnedProcessAtSeam`): spawns a child
  interpreter (spawn start method — the seam action MUST be a module-level synchronous
  picklable function), the child opens its own DB, runs the action, signals a named
  seam over a pipe, and the parent SIGKILLs or releases it. Bounded readiness/exit
  oracles; `was_killed` classification is POSIX-specific.
- **Durable-surface snapshot helpers**: `_work_items_by_token`,
  `_duplicate_terminal_outcome_tokens` (the universal no-double-terminal oracle),
  `_completed_outcome_tokens`, `_node_state_identities`, `_recovery_events`, plus the
  epoch-21 coordination helpers `_coord`, `_usurp_seat`, `_coordination_row`,
  `_coordination_events`, `_run_workers`.

### 1.1 test_barrier_process_death_matrix.py (452)

- **Exercises:** real process death at the narrow window "barrier durable result
  committed, continuation not yet emitted", per barrier family
  (`aggregation` | `coalesce` | `row_union` — one parametrized test,
  `test_single_process_leader_barrier_process_death_matrix`). Child pauses via
  monkeypatched `RowProcessor._complete_coalesce_fire` /
  `_complete_row_union_fire` / `AggregationExecutor.execute_flush`, parent SIGKILLs,
  fresh process resumes. Aggregation resumes through the maintained Orchestrator
  entry; coalesce and row_union drive the executor/repository restore composition
  directly (`BarrierJournalRestoreContext` + `has_blocked_barrier_work()` oracle).
- **How a scenario is added:** write two module-level seam actions (run-to-seam and
  fresh-process-recovery), an `_exercise_<family>` function asserting the killed-DB
  image and the recovered image, and add the family to the `exercises` dict. Pipeline
  authoring comes from imports: `tests/integration/pipeline/test_aggregation_recovery`
  (`_build_eof_aggregation_pipeline`, `_SumBatchTransform`, `_LoadCountingSource`) and
  `tests/integration/pipeline/test_barrier_intake_dispositions` (`RUN_ID`,
  `_arrive_via_intake`, `_branch_token`, `_coalesce_processor`,
  `_real_coalesce_executor`, `_usurp_seat`, `_work_item_row`) plus
  `tests/unit/engine/test_processor` (`_make_processor`,
  `_persist_blocked_scheduler_work`).
- **Collector/scope hosting:** the spawn/kill/restore machinery hosts a collector
  scenario **as-is**; what does not exist is the builder layer — a
  `_real_collector_executor` + processor wiring (the analogue of
  `_real_row_union_executor` / `_row_union_processor` defined in this very file for
  row_union) and a `_complete_collector_fire`-style pause point in the new collector
  executor. That is a shared-builder extension, not a harness change. Expect the
  killed-image assertions to read `group_records` / `token_lineage_frames` instead of
  the tri-columns after WS1.

### 1.2 test_barrier_timing_invariance.py (272)

- **Exercises:** ADR-030 §E.2 backdated adoption — a barrier's timeout fire instant is
  a pure function of durable `barrier_blocked_at`, invariant across leader takeover.
  Two classes (aggregation `TriggerEvaluator._first_accept_time`, coalesce
  `first_arrival`), each a single in-process MockClock test: leader A blocks rows,
  advance to T_b+timeout−ε (no fire), `_usurp_seat`, restore leader B with
  `BarrierJournalRestoreContext`, assert the same anchor, advance +ε, both frames fire.
- **How a scenario is added:** new test class per barrier kind, built on the
  **unit-engine builders** (`tests/unit/engine/test_processor._make_factory` /
  `_make_processor` / `_persist_blocked_scheduler_work`,
  `tests/unit/engine/test_adr030_slice3_intake._agg_processor`). No process spawn, no
  fixture files.
- **Collector/scope hosting:** collector has **no timeout by design**
  (`end_of_group` only, spec §5), so the timeout-anchor arm does not transfer. What
  DOES belong here is the takeover-restore invariance shape: collector buffer
  composition and opener-ordinal flush order identical across `_usurp_seat` + restore
  (the "collector-buffer takeover with ordinal flush" edge case) — needs the new
  executor's registration/wiring analogous to `_real_coalesce_executor`, then the
  file's frame-A/frame-B pattern applies unchanged.

### 1.3 test_concurrent_resume.py (1446)

- **Exercises:** the three crash+resume gaps of elspeth-40886ef9f8 over the durable
  scheduler journal: (1) mid-claim crash → lease expiry → recovery sweep reclaim
  (attempt bump + work_item_id rotation) → completion with exactly-once terminal
  outcomes; (2) expired-lease reclaim under contention (atomic refusal while ANY peer
  lease is live); (3) two `resume()` calls racing (seat-acquisition CAS loser refusal,
  terminal-run entry-guard refusal, RUNNING-under-live-seat refusal). Every assertion
  reads public durable surfaces only (journal columns, `scheduler_events`,
  `token_outcomes`, `node_states`, `runs`, coordination tables); resume points are
  opaque.
- **How a scenario is added:** a new test method on the harness `_CrashedRun` object —
  `_run_to_interrupted_checkpoint(tmp_path, clock)`, then craft state with
  `_craft_crashed_lease` / raw production writers, then `_resume(crashed)` and snapshot
  helpers. **Pipeline-authoring is the limitation:** `harness._build_pipeline` is the
  fixed linear pipeline, so any scenario whose crashed image must contain a BLOCKED
  barrier row inside a bound group needs either a harness extension (a
  `_build_pipeline` variant with fork/expand/closer) or direct crafting of the
  barrier work item (the `_persist_blocked_scheduler_work` route the barrier suites
  use). **Needs pipeline-authoring extension** for collector/scope scenarios.
- Companion `test_resume_rejection.py` (273) holds pure refusal cases — the natural
  home for the WS5 group-satisfiability **refusal** message pins (scope, group, member
  named), while the enforcing-vs-advisory parity test pattern lives in
  `tests/integration/audit/test_contract_violation_token_outcomes.py` (spec §8 names
  it for the third sibling).

### 1.4 test_follower_coordination_chaos.py (731)

- **Exercises:** follower isolation (I1–I4: lease_owner CAS fence, membership fence,
  disjoint claims, audit attribution) and chaos (C1–C5: leader/follower claim race,
  follower crash → lease lapse → reaper, heartbeat-latch eviction, liveness-aware reap
  skip, empty-queue idle poll). Scheduler verbs driven DIRECTLY via
  `TokenSchedulerRepository`; run state from the harness plus
  `test_follower_join_and_drain` helpers (`_seat_run_with_live_leader`,
  `_join_follower`, `_orchestrator`, `_seed_ready_row`); local
  `_seed_ready_row_direct` seeds READY rows with no claim side effects. Cross-cutting
  invariants every test asserts: `_duplicate_terminal_outcome_tokens == []`, no
  unexpected coordination events, seat epoch stable.
- **How a scenario is added:** new method in the relevant class; craft journal rows
  via repo verbs; drive `FollowerProcessor` or the repo directly.
- **Collector/scope hosting:** **as-is** for the follower half of settlement — a
  follower staging the innermost bound loss only (`processor.py:3216-3217` shape,
  carried per §6.2) and lease-expiry redelivery of a barrier arrival (the CAS-fenced
  idempotent-skip edge case) are journal-level scenarios this file's technique already
  covers; the collector work item is crafted the same way coalesce BLOCKED rows are.

### 1.5 test_suspended_winner_fences.py (1020)

- **Exercises:** the deterministic stale-token fence matrix (§C.4: "every fence is a
  DB CAS against an injectable stale token"). Per fenced verb: build run state on a
  real crashed run, acquire a real `CoordinationToken` at epoch E, `_usurp_seat` to
  E+1, call the verb with the stale token, assert the four-part contract —
  (i) `RunLeadershipLostError`, (ii) exactly one `fence_refusal` coordination event
  naming the verb, (iii) ZERO payload mutation + no seat-expiry extension,
  (iv) positive control under a current token succeeds and extends. Already covers
  `adopt_blocked_barrier_item` and `adopt_coalesce_branch_losses`.
- **How a scenario is added:** one new test per verb following the four-part template.
- **Collector/scope hosting:** **as-is** — WS3/WS4's new fenced verbs (group-loss
  adoption over `group_losses`, collector arrival CAS, escalation staging in the
  adoption transaction) each get a matrix arm here. The `adopt_coalesce_branch_losses`
  arm is the direct template for its `group_losses` successor and must be **migrated,
  not duplicated**, when `BranchLossSpec` is retired.

### 1.6 test_multi_worker_leader_finalize.py (780)

- **Exercises:** the REAL leader `Orchestrator._execute_run` finalize phase — H1
  bounded peer-lease wait, in-loop `reap_expired_peer_leases`, eviction-latch break,
  M3 looped follower PENDING_SINK drain, double-flush guard. Peer scheduler rows are
  crafted mid-run by a `_SeedingSource` callback (fires after all rows journal, before
  the leader enters 4b-pre); `time.monotonic`/`time.sleep` are patched **in
  `elspeth.engine.orchestrator.leader_drain`** for a scripted deadline.
- **How a scenario is added:** new source-callback crafting the peer image + new test
  asserting finalize behaviour.
- **Collector/scope hosting:** **as-is** for the WS5 drain-loop change — this is the
  natural host for "`has_blocked_barrier_work` counts collector-buffered members or
  the fixpoint exits early" (`leader_drain.py:511`) and for the escalation fixpoint's
  bounded-iterations raise (`:514-518`), because it already drives the real
  end-of-input flush loop with a scripted clock.

---

## 2. Postgres testcontainer suites (tests/testcontainer/core/)

All three: `pytestmark = pytest.mark.testcontainer`; module-scoped fixture
`PostgresContainer("postgres:16-alpine", driver="psycopg")` from the `testcontainers`
package yielding `postgres.get_connection_url()`; `LandscapeDB.from_url(postgres_url)`
creates the full schema against real PostgreSQL. `@pytest.mark.timeout(120)` per test.

- **test_barrier_recovery_postgres.py** (253): local backend qualification for
  committed barrier-result recovery — three tests:
  `test_postgres_recovers_completed_coalesce_effect_without_remerge` (injected crash
  before `_complete_coalesce_fire`; the `coalesce_effects` row is already `completed`;
  restore reconciles WITHOUT re-merging), `..._committed_aggregation_result_without_plugin_replay`,
  and `..._empty_and_passthrough_aggregation_receipts`. Reuses the same shared
  builders as the e2e matrix (`test_barrier_intake_dispositions`,
  `test_aggregation_recovery` imports). **Pins:** dialect-real recovery of committed
  barrier evidence — the suite the collector's committed-flush-recovery twin belongs
  in.
- **test_coalesce_effect_lock_order_postgres.py** (167): two `RecorderFactory`
  instances over two connections; monkeypatched `_lock_coalesce_dependencies`
  interleaves a winner (holds parent-token authority at a real `pg_backend_pid`) and a
  loser (provably blocked, then admitted). **Pins:** identical concurrent
  `coalesce_tokens` writers serialize on parent authority and converge on ONE
  `coalesce_effects` row / 3 tokens / 2 `token_parents` — the idempotent-materialization
  model that `group_records` minting under concurrent re-drive must replicate.
- **test_coalesce_branch_loss_reason_postgres.py** (188): battery-round-7 regression
  (String(64) `reason` column). **Pins**, in dependency order: (1) the real quarantine
  arm (`_handle_transform_error_status` driven through the unit-engine builders)
  stages the bare `quarantined` category token and it records durably on PostgreSQL;
  (2) `record_coalesce_branch_loss` refuses an over-wide reason with
  `AuditIntegrityError` BEFORE the INSERT (dialect-uniform fail-closed);
  (3) a raw INSERT of >64 chars genuinely raises `DataError` at the column. The
  `group_losses` ledger keeps the categorical-reason vocabulary (spec §4.3), so this
  suite is migrated onto the new table, not deleted.

---

## 3. Unit seams and property suites

- **tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py**:
  ADR-030 §E.2 journal-first intake verb. Pattern: `make_landscape_db()` (from
  `tests/fixtures/landscape`), raw-SQL seeding of runs/nodes/rows/tokens/DRAFT batch,
  epoch-1 seat minted via `RunCoordinationRepository`, then verb-level assertions:
  epoch fence → `barrier_adopted_epoch NULL→epoch` CAS → `batch_members` +
  BUFFERED `token_outcomes` in ONE `BEGIN IMMEDIATE` transaction. Documents that the
  adoption CAS is the ONLY double-BUFFERED guard (no non-terminal uniqueness on
  `token_outcomes`) — the exact seam the spec's duplicate-arrival CAS-fenced skip
  (§5) generalizes. Stale-epoch arm mirrors `test_leader_fence_stale_token.py`.
- **tests/unit/core/landscape/test_scheduler_repository_coalesce_branch_losses.py**:
  the §E.5 verbs — `record_coalesce_branch_loss` (rides the caller's lease-fenced
  disposition transaction), `list_unadopted_coalesce_branch_losses` (per-iteration
  replay read), `adopt_coalesce_branch_losses` (fenced cursor mark),
  `list_coalesce_branch_losses` (§E.4 takeover full-table read — the pattern §6.2
  says gets its own stated-requirement test for `group_losses`). Same raw-seed +
  seat-token pattern. This whole file maps 1:1 onto the `group_losses` replacement
  suite; the append-only + `adopted_epoch`-cursor + full-read-on-takeover assertions
  carry over with the widened key `(run_id, closer_name, group_id, member_key)`.
- **tests/property/audit/test_fork_coalesce_flow.py**: hypothesis over FULL
  Orchestrator runs (real `make_landscape_db`, `MockPayloadStore`); invariants
  asserted by SQL over the audit DB (`get_outcome_counts`,
  `get_fork_coalesce_stats`) — exactly-one FORKED parent outcome, per-branch child
  reaches coalesce, COALESCED outcomes, token conservation. **WS1 blast radius:** its
  SQL reads `token_outcomes.fork_group_id` directly (e.g. the
  `COUNT(DISTINCT fork_group_id) ... path='fork_parent'` query), so it re-points at
  `token_lineage_frames` when the tri-columns are deleted.
- **tests/property/engine/test_coalesce_properties.py**: hypothesis over
  `CoalesceExecutor` DIRECTLY, with `_FakeExecutionRepository` /
  `_TestCoalesceExecutor` (auto-provides observed-mode output schema); pins merge
  policies (`require_all`/`first`/`quorum`/`best_effort`), merge strategies
  (`union`/`nested`/`select`), `_max_completed_keys` FIFO memory bound, late-arrival
  consistency. **WS4 blast radius:** everything here is keyed by
  `(coalesce_name, row_id)` implicitly through the executor internals — the
  pending-state re-keying to `(coalesce_name, fork_group_id)` runs straight through
  this suite, and plugin-visible merge behaviour "unchanged" (spec §5) is measured by
  it staying green modulo the key shape.
- Sibling unit seams the plan will touch: `tests/unit/engine/test_processor.py` (the
  builder module everything imports: `_make_factory`, `_make_processor`,
  `_persist_blocked_scheduler_work`, `_persist_token_for_scheduler`),
  `tests/unit/engine/test_adr030_slice3_intake.py` (`_agg_processor`, intake
  adoption), `tests/integration/pipeline/test_barrier_intake_dispositions.py` (the
  shared coalesce-intake builder imported by BOTH the e2e death matrix and the
  Postgres suites — extend it once, three tiers inherit),
  `tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py` /
  `..._tier1_engine.py`, and `tests/unit/engine/test_barrier_coordination.py`.

---

## 4. Replay fixtures and their regeneration

There are **no stored binary replay fixtures**. Three families, all rebuilt by
different mechanisms:

1. **Mint-replay predicate tests** — `tests/unit/core/landscape/test_token_recording.py`
   (`test_exact_replay_returns_existing_children_without_reminting` :413 fork /
   :764 expand, `test_incompatible_replay_refuses_without_mutation` :439/:793,
   batch-expansion idempotency + divergent-replay refusal :588/:637). These construct
   the replay in code by calling `fork_token`/`expand_token` twice through the real
   `RecorderFactory` — **rebuilt by editing the test, no fixture files**. The §4.4
   rewrite (frames-equality predicates) lands here first.
2. **DAG scenario corpus** — `tests/fixtures/dag_scenario_corpus/` (loader.py,
   harness.py, schema.py with `StableTokenProjection` / `SinkOutputProjection` /
   `StableTerminalDisposition` / `StableExpansionProjection`, per-scenario
   `recovery_*.py` assertion modules) + YAML/CSV inputs under `v1/<scenario>/` (14
   scenario dirs incl. `fork-coalesce-policies` (21 files), `parallel-coalesces`,
   `checkpoint-deterministic-resume`, `row-expansion-parent-child-recovery`) + the
   authoritative manifest `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`
   (49 harness cases; 22 `projection_sha256` pins plus a
   `resumed_full_projection_sha256`). Consumed by
   `tests/integration/core/dag/test_dag_scenario_production_path.py` and the
   `test_dag_recovery_*.py` files. **Regeneration is BY HAND, adjudicated:** the
   harness computes `semantic_runtime_projection_sha256` and the failure message
   prints expected vs observed; the operator hand-edits manifest.yaml and appends a
   dated A/B-verified rotation note to the comment ledger in
   `tests/unit/architecture/test_dag_scenario_corpus_contract.py` (which also pins
   `EXPECTED_CASE_REGISTRY_SHA256` over the whole case registry). There is NO regen
   script. The spec's frozen-oracle protocol (§11) formalizes exactly this practice —
   record group-id-normalized projections pre-WS1, classify frozen vs regenerated
   per surface, adjudicate each rotation.
3. **The 56 golden JSONs** (spec §11's "each individually adjudicated") —
   `tests/golden/`: 55 under `web/catalog/{knob_schema,policy_view}/` (pinned by
   `tests/unit/web/catalog/test_knob_schema_golden.py` / `test_policy_view_golden.py`,
   no in-test regen flag — updated by hand against the live catalog) plus
   `state_engine/plugin_lifecycle_matrix.json` (checked/rendered by
   `scripts/state_engine_plugin_matrix.py check` / `render-skeleton`; the golden is
   reviewed evidence, never authority). These move only if `collectors:`/`scopes:`
   surface in catalog output (WS2 three-pin territory), not in WS1.

Also fixture-like: `tests/unit/web/composer/redaction_policy_snapshot.json`
(regenerated ONLY via `scripts/cicd/bootstrap_redaction_snapshot.py --write`) and
`config/cicd/runtime_rejection_parity.yaml` (seeded via
`scripts/cicd/runtime_rejection_parity.py --write`) — both fire on WS2's new config
surface and rejection sites.

---

## 5. Line-number verification (fresh, HEAD 3ca6516e82)

Verdicts: ✅ exact · ≈ correct span, exact anchor a line or two off (noted).

| Cited site | Verdict | What is actually there |
|---|---|---|
| `engine/coalesce_executor.py:511-517` pending dict | ≈ | comment :511, `self._pending: dict[tuple[str,str], _PendingCoalesce]` **:512**; `self._completed_keys` OrderedDict **:517** |
| `coalesce_executor.py:577` scalars | ≈ | `get_barrier_scalars` def :560; docstring naming `CoalescePendingScalars` :577; dict built :580-583. Class lives in `contracts/barrier_scalars.py:169` |
| `coalesce_executor.py:803/:811` completion check | ✅ | `key = (coalesce_name, token.row_id)` :803; `if key in self._completed_keys or self._check_landscape_for_completion(...)` :811 |
| `coalesce_executor.py:841/:1054/:1254` raw `record_token_outcome` | ✅ | all three exact — each `FAILURE`/`UNROUTED` with `data_flow is None` guard immediately above |
| `coalesce_executor.py:1470/:1516` dedup keys | ✅ | `key = (coalesce_name, row_id)` at :1470 and :1516; the two-level lookups at :1475 and :1520 |
| `engine/scheduler_drain.py:996-1006` token-equality guard | ✅ | `take_claim_branch_loss` def :983; ">1 staged" raise :996-999; `spec.token_id != claimed_token_id` raise :1001-1006 |
| `dispositions.py:162-229` | ✅ (path!) | file is **`core/landscape/scheduler/dispositions.py`** (not engine/). `mark_failed` def :162 with `branch_loss: BranchLossSpec | None = None` :168; `mark_pending_sink` params through :228-229. Full singular-param roster: :103, :139, :168, :199, :228, :278, :645, :673, :698 |
| `core/landscape/scheduler_repository.py:492-620` | ✅ | `branch_loss` kwarg on `mark_failed` wrapper :492, threaded through the delegation chain to :620; `mark_pending_sink_terminal` def :624 |
| `engine/orchestrator/leader_drain.py:417` | ✅ | `MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000` :417 |
| `leader_drain.py:464-518` | ✅ | fixpoint `for _ in range(...)` :464; EOF buffer-abort raise :486-491 (comment from :481); non-convergence raise :514-518 |
| `leader_drain.py:511` | ✅ | `if not processor.has_blocked_barrier_work(): return` :511-512 |
| `engine/barrier_coordination.py:447-482` | ✅ | `if outcome.late_arrival:` :447; §E.3a single-row release via `mark_blocked_barrier_terminal` with `late_arrival_after_merge` FailureInfo through :482 |
| `barrier_coordination.py:1438` | ✅ | restore-reconcile `release_context` with `"reason": "late_arrival_after_merge"` :1438 (block :1434-1442, `restore_reconcile: True`) |
| `engine/token_traversal.py:198-226` | ✅ | `is_multi_row` zero-row path: `len(rows)==0` :201 → `FILTER_DROPPED` + `_notify_barrier_of_lost_branch("dropped_by_filter")` → returns :226. Note: `success_empty()` never calls `expand_token` here — the WS1 empty-`group_records` mint lands in this block |
| `token_traversal.py:241` | ✅ | `self._processor._token_manager.expand_token(` :241 (one of the two TokenManager-layer callers) |
| `token_traversal.py:254-262` | ✅ | binding-survives-expansion loop: `on_branch = child_token.branch_name is not None` :255; trap comment :256-262 |
| `engine/processor.py:483-499` | ✅ | branch-keyed maps + the two pairwise overlap raises (coalesce∩sink :488-493, coalesce∩row_union :494-499) that ruling 23's whole-roster rule subsumes |
| `processor.py:1623` | ✅ | aggregation-flush `expand_token` call :1623 (the second TokenManager-layer caller); the `(TRANSIENT, BATCH_CONSUMED)` / `QUARANTINED_AT_SOURCE` emission loop :1642-1652 |
| `processor.py:2856-2942` resume-start dispatch | ✅ | tri-field combination patterns: docstring case 4 :2856-2860; `spec.expand_group_id is not None` arm :2881; `join_group_id ... fork_group_id is None ... branch is None` arm :2906 |
| `processor.py:3028-3103` row_union in-line failure | ✅ | `_notify_row_union_of_lost_branch` def :3028; retain-branch-identity doctrine (ruling 27 retires it) :3042-3048; FAILURE/UNROUTED RowResults :3095-3103; `_row_union_group_released` :3105 |
| `processor.py:3121-3155` | ✅ | `_notify_barrier_of_lost_branch` def :3121; the FALSE "THE single seam" docstring :3131; "at most one arm yields results" :3139-3141 |
| `processor.py:3191-3218` | ✅ | §E.5 record-then-notify staging comment :3191; `_pending_branch_losses.append(BranchLossSpec(...))` :3202-3211; follower in-memory-notify skip :3216-3217 |
| `processor.py:3265-3290` | ✅ | coalesce `failure_reason` arm: consumed tokens terminalized FAILURE/UNROUTED, sibling RowResults built :3263-3290 |
| `core/dag/builder.py:743` | ✅ | the subset-direction check: "VALIDATE COALESCE BRANCHES ARE PRODUCED BY GATES" :742-743 (the only closure direction checked today — ruling 23's counterpart) |
| `builder.py:1144-1184` | ✅ | transform `on_error` error edges :1144-1150; config-gate row-error edges (comment) :1164-1170; DIVERT `add_edge` :1180-1186 |
| `builder.py:1462-1528` | ✅ | row_union walk: ancestor/descendant fork-generation raise :1462-1471; transform-mode-aggregation-in-branch raise :1504-1517; nested-fork raise :1519-1528 (the precedents §7 rules 4/6 cite) |
| `engine/tokens.py` primitives | ✅ | `fork_token` def :251, `coalesce_tokens` def :307, `expand_token` def :371 |
| `core/checkpoint/recovery.py:403` | ✅ | `RecoveryManager.can_resume` def :403 (advisory surface) |
| `recovery.py:121/:212` | ≈ | shared-implementation defs are :118 (`check_run_status_resumable`) and :209 (`check_source_lifecycle_resumable`); :121/:212 land in their docstrings ("SINGLE shared implementation … must never drift") |
| `engine/orchestrator/resume.py:733-736` | ✅ | enforcing arm of the shared source-lifecycle gate: `check_source_lifecycle_resumable` call :736, `EmptyResumeStateError` :738 (comment from :732) |
| `resume.py:894-896` | ✅ | resume() entry guard part 1 (shared run-status re-check, §B.3 live-seat precision) — comment block from :892; part 2 (checkpoint currency) :928 |
| `resume.py:356` (spec §4.1) | ✅ | the barrier-lineage discriminator comprehension `branch_name is not None or fork_group_id ... or expand_group_id ... or join_group_id ...` :356 |

---

## 6. Edge-case → harness matrix (spec §11's owed item)

| Edge case | Harness file(s) to extend | Why this one |
|---|---|---|
| **Empty expansion, unbound** (universal `group_records` mint, `member_count=0`, parent keeps SUCCESS/FILTER_DROPPED) | `tests/unit/core/landscape/test_token_recording.py` (the mint + idempotency, beside the existing expand-replay tests) and `tests/unit/engine/test_token_traversal_characterization.py` (the `success_empty()` zero-row path at `token_traversal.py:198-226` is currently exercised there via FILTER_DROPPED) | WS1-testable before `scopes:` exists (quality F8); the zero-row path never calls `expand_token`, so the mint is a new write in exactly this block — pin it where the block is already characterized |
| **Empty expansion, bound** (`require_all` ⇒ `empty_expansion` group failure without plugin invocation; `best_effort` ⇒ silent close) | `tests/integration/pipeline/` — new sibling beside `test_deaggregation.py`/`test_barrier_intake_dispositions.py` using the shared coalesce-intake builders extended with a collector; corpus scenario added under `tests/fixtures/dag_scenario_corpus/v1/` for the audit projection | needs a real closer bound to the opener plus the engine-performed disposition — an intake/settlement behaviour, which that module's builders (`_arrive_via_intake` etc.) already model for coalesce |
| **All-members-lost** (roster settles with zero arrivals; engine closes WITHOUT plugin; `all_members_lost`; not a failure under `best_effort`) | `tests/integration/pipeline/test_barrier_intake_dispositions.py` (leader intake replaying durable losses to closure) + `tests/property/engine/test_coalesce_properties.py` gains the collector-policy analogue; corpus already has the shape for coalesce (`fork-coalesce-policies/best-effort-all-lost.yaml`, `first-all-lost.yaml`) — add the collector twins | the coalesce all-lost case is ALREADY a corpus fixture family; extending the same family keeps the frozen-oracle diff honest |
| **Duplicate same-token arrival via lease expiry** (CAS-fenced idempotent skip on `barrier_adopted_epoch`) | unit: `tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py` (the CAS and the `adopted=False` skip live here today — extend to collector work items); e2e: `tests/e2e/recovery/test_follower_coordination_chaos.py` (C2's crash→lapse→reaper redelivery pattern, redirected at a BLOCKED collector row) | the unit file documents the adoption CAS as the ONLY double-accept guard; the chaos file is the only suite that drives real lease-expiry redelivery against real journal rows |
| **Escalation fixpoint, nested** (inner FAIL verdict → one loss against enclosing frame → drain-cycle iteration to fixpoint; bounded-raise) | `tests/e2e/recovery/test_multi_worker_leader_finalize.py` (drives the real `run_end_of_input_barrier_flush` loop with scripted clock; the non-convergence raise :514-518 and `has_blocked_barrier_work` :511 are its subject matter) + a unit fixpoint test beside `tests/unit/engine/test_adr030_slice3_intake.py` for the one-pass-per-drain-cycle latency claim (§6.3 item 2) | the EOF fixpoint IS the leader-drain loop; this is the only suite that runs it against a crafted multi-worker durable image |
| **Resume-mid-group refusal** (satisfiability gate: minted member neither non-terminal, arrived, nor in `group_losses` ⇒ refuse naming scope/group/member) | `tests/e2e/recovery/test_resume_rejection.py` (the refusal-message home) + the third sibling the spec commissions in `tests/integration/audit/test_contract_violation_token_outcomes.py` (beside `test_aggregation_eof_flush_violation_leaves_genuinely_retryable_tokens` / `test_aggregation_count_flush_violation_abandons_tokens_at_finalization`) + a both-surfaces parity test on `can_resume` (`recovery.py:403`) vs `ResumeCoordinator.resume()` following the `check_source_lifecycle_resumable` two-surface precedent (:209 / resume.py:736) | rejection cases were deliberately split into test_resume_rejection.py; the audit-integration file is named by spec §8; the shared-gate parity pattern already exists twice in recovery.py and is the stated implementation shape |
| **Resume-mid-group happy path incl. collector-buffer takeover + ordinal flush** | `tests/e2e/recovery/test_barrier_process_death_matrix.py` (new `collector` family: kill after buffered arrivals, fresh-process restore, flush in opener-ordinal order) + `tests/e2e/recovery/test_barrier_timing_invariance.py` (frame-A/frame-B buffer-composition equality across `_usurp_seat` — arrival order is unrecoverable after takeover, ordinal order is not, which is the point of decision 11) + `tests/testcontainer/core/test_barrier_recovery_postgres.py` (dialect-real committed-flush recovery twin) | the death matrix already proves per-family "committed evidence, no replay" for the other three families; timing-invariance already proves takeover-composition equality for batches; the Postgres suite is where committed-receipt recovery gets backend qualification |
| **Nested fork-in-collector runtime settlement** (fork closes at in-region coalesce; member presents ONE merged token; outer roster settles by member_key resolved through the OPENER's `token_parents.ordinal`, not the merged token's parent chain) | `tests/integration/pipeline/` new module built on the shared builders (coalesce intake + collector intake composed), plus a corpus scenario extending the nested family the spec names as the §4.1a differential oracle (`sequential-nested-fork-coalesce` — NOTE: not yet a `v1/` scenario dir at HEAD, see risk notes — and `parallel-coalesces`); the pending-state re-keying collision (two sibling members forking into the same coalesce NODE, same `row_id`, distinct `fork_group_id`s) gets a dedicated test against `coalesce_executor.py:511-517/:577/:803/:811/:1470/:1516` | the merged-token-arrival member resolution (arch minor 3) and the re-key collision (arch M1) are both in-executor behaviours; the property suite pins merge semantics stay fixed while the key changes |
| **Depth-5 full unwrap to quarantine, live AND crash+resume** (all-`require_all` chain: innermost failure escalates one frame per level; survivors `scope_group_failed`; outermost terminal handling flags parent source row; builder rejects depth 6 without override; fixpoint bound derived from depth; PLUS the crash+resume variant — crash mid-unwrap with an inner group durably BLOCKED, resume, no false-refuse, same terminal outcome as the live run) | live unwrap: WS3's `tests/integration/pipeline/test_depth5_group_unwrap.py` (its Task 10; `_nested_settings(depth)` is the reusable builder) + build-time depth-cap rejection in `tests/unit/core/dag/test_builder_validation.py` + a runtime_rejection_parity adjudication for the new `GraphValidationError`. Crash+resume variant (WS5 Task 5a): `tests/e2e/recovery/test_depth5_resume_mid_chain.py`, importing `_nested_settings` from the WS3 module — clamp `PipelineConfig.escalation_fixpoint_bound` on the crash run so the EOF fixpoint's non-convergence raise kills the run mid-unwrap in-process, then resume with the real derived bound; no spawn machinery (the crash is a deterministic raise, and the fixed-linear `_CrashedRun` harness cannot author nested regions — risk note 3) | depth is a new axis: no current harness composes nested bound regions past depth 2 (`sequential-nested`); the builder-rule half belongs with the other graph-validation rejections, and every new rejection site fires the 2026-08-17 parity gate; the resume half is explicitly owned by WS5 Task 5a so the crash+resume gap is not re-inherited |

Frame-guard mutant enumeration (spec §11: guard on `group_id` alone / `member_key`
alone; walk stopping at innermost instead of first-bound; walking outermost-first;
escalation against failing rather than enclosing frame; CAS fence removed; restore
filtered by `adopted_epoch`) — host these beside the guard's unit home, i.e. the
`group_losses` successor of `test_scheduler_repository_coalesce_branch_losses.py` and
the settle-member unit tests, run with `-n 0` per the standing parallelism rule.

---

## RISK NOTES

1. **The three shared builder modules are cross-tier load-bearing.**
   `tests/integration/pipeline/test_barrier_intake_dispositions.py`,
   `tests/integration/pipeline/test_aggregation_recovery.py`, and
   `tests/unit/engine/test_processor.py` are imported by the e2e death matrix, the
   timing-invariance suite, AND two Postgres suites. WS1's tri-field retirement
   (`_branch_token` constructs `TokenInfo(..., branch_name=...)`;
   `_persist_blocked_scheduler_work` writes work-item lineage columns) breaks all
   consumers at once — plan the builder migration as ONE early slice, not per-suite.
2. **`_persist_blocked_scheduler_work` / `_craft_crashed_lease` write the durable
   shapes being retired.** Any crafted-image test is silently coupled to the
   `token_work_items` tri-columns (`schema.py:725-728`) and to
   `serialize_row_payload`'s codec. The codec-purity + bidirectional cross-check
   (§4.3) means every crafted image needs frames rows too, or restore integrity
   checks will fail the HARNESS, not the code under test. Budget for a harness
   `craft_lineage` helper.
3. **`test_concurrent_resume`/harness pipeline is fixed-linear.** No existing e2e
   full-pipeline crash construction contains a barrier at the crash instant — the
   barrier suites craft barrier state directly instead. If the plan wants a
   full-Orchestrator crashed-mid-group image (resume-mid-group happy path), that is a
   new `_build_pipeline` variant, and the harness docstring's seat assumptions
   (epoch-21 note) must be re-verified for it.
4. **Worktree trap for these exact suites** (recent-code-hints 2026-08-17): e2e
   recovery tests fail on capture-root binding in worktrees, and worktrees
   under-collect suites that glob `evals/*`. A/B oracle runs for the WS1 checkpoint
   must run in the main checkout with HEAD recorded before/after (~18 min full
   suite; four sibling commits inside one window once produced 456 phantom failures).
5. **Frozen-oracle gap: `sequential-nested-fork-coalesce` is cited by the spec as a
   differential-equivalence fixture but no `v1/` scenario dir of that name exists at
   HEAD** (present: `parallel-coalesces`, `fork-coalesce-policies`, …). Either the
   spec means a manifest case id inside an existing dir or the fixture must be
   AUTHORED pre-WS1 — verify before freezing the oracle set; a fixture created after
   the rewrite cannot be its own oracle.
6. **`fork-multiple-terminals-partial-failure` is a ruling-23 casualty** (mixed fork
   closure). Spec §11 keeps it frozen through the WS1 diff and migrates it at WS2
   with an adjudicated manifest rotation — the rotation ledger in
   `test_dag_scenario_corpus_contract.py` (and `EXPECTED_CASE_REGISTRY_SHA256`) is
   the mechanism; budget the adjudication comment as real work, it is the
   tamper-vs-migration discriminator.
7. **Manifest hash pins are hand-rotated with A/B narratives.** There is no regen
   script; the WS1 fixture rebuild will rotate MANY `projection_sha256` values at
   once. The existing discipline (token-normalized diff + single-occurrence
   assertion + field-by-field projection diff) does not scale to a whole-corpus
   rotation by hand — the plan should commission a one-shot diff tool for the flip
   slice, while keeping the pins themselves hand-adjudicated.
8. **Postgres suites are the only dialect-real coverage for the new tables.**
   SQLite does not enforce VARCHAR bounds (the round-7 lesson pinned in
   `test_coalesce_branch_loss_reason_postgres.py`); `group_losses.reason` and any
   bounded columns on `group_records`/`token_lineage_frames` need the same
   three-proof treatment or the local suite goes green over a truncation crash.
9. **Duplicate-terminal detection is asymmetric** (recent-code-hints 2026-08-21):
   `ix_token_outcomes_terminal_unique` self-detects double writes on both
   destinations, but the ZERO-write direction has no automatic detection — the
   settle-member rewrite (§6.1 retiring three bypass arms) must add explicit
   completeness assertions (`_completed_outcome_tokens` set equality), not rely on
   the unique index.
10. **`-n 0` for mutation runs, capped parallelism on fan-outs** (standing rule).
    The frame-guard mutant matrix and any timing-sensitive e2e additions
    (`test_multi_worker_leader_finalize` patches `time` inside `leader_drain`)
    must not share workers with the wider suite.
