# Hot-path recon — executor, composer and adjacent trunks

Status: **recon only, no fixes proposed or made.** Read and measured at
`1a7ff0e6` (merge of the 0.8.11 candidate) on 2026-11-11. This is a working
document: update or delete it as the system changes. Line numbers are valid
for that commit only.

"Hot path" here means the performance-heavy, load-bearing trunk code that is
called the most to get from start to end — a pipeline run for the executor, a
user message to a settled turn for the composer.

## Corrections since first written

[executor-hot-path-remediation.md](executor-hot-path-remediation.md)
re-verified the findings outside `web/` and overturned or refined several of
them. Its "Corrections to the earlier documents" table is the full list. The
summary below has been corrected where it was wrong; the appendices are
unchanged and keep the original claims.

- **Fork/coalesce is not super-linear.** Marginal CPU is flat at
  109–110 ms/row across 100 to 800 rows. The original 102 → 136 ms/row came
  from three single wall-clock runs.
- **The canonical JSON and hashing share is a cProfile bucket**, which
  over-weights small Python calls. Its real weight on narrow rows is 5% or
  less; it is large only on wide rows.
- **The retry wrapper and the per-node traversal loop are not hot**
  (Appendix A top-10 items 6 and 7).
- **The CSV sink effect rebuild is one effect per sink per run** in the
  common case, so it is downgraded.
- Several static counts were off by small amounts (ingest statements, thaws
  per transform, sink read transactions, canonical serialisations at the
  sink).

## How to read this

| Part | Evidence | Scope |
|---|---|---|
| Summary (this section onward) | Synthesis of the appendices | All |
| Appendix A — executor control flow | Static reading | `engine/` |
| Appendix B — executor persistence trunk | Static reading | `core/landscape/`, canonical hashing, payload store, checkpoints |
| Appendix C — composer turn loop | Static reading | HTTP entry to settled turn |
| Appendix D — composer data trunk | Static reading | State, validation, redaction, persistence per tool call |
| Appendix E — other hot paths | Static reading | LLM call trunk, web request and run trunks, sources and sinks |
| Appendix F — measured executor profile | **Measured** (cProfile, py-spy) | Four key-free example pipelines |

Each appendix is one investigator's report, reproduced with only heading
levels and machine-local paths changed. Each ends with its own list of what
was not verified; those lists are part of the finding.

Limits that apply to everything below:

- Only the executor was measured, and only on SQLite, one run per input
  size, 2,000 rows at most, on a shared 4-core cloud container. Ratios and
  per-row call counts are the durable result; absolute timings are
  indicative. PostgreSQL was not measured.
- No LLM-shaped workload was run, so the external-call trunk, where
  wall-clock goes in real runs, has static evidence only.
- Everything about the composer and the web tier is static reading. The only
  composer numbers are a few key-free timings in Appendix F § 9.
- The static counts in Appendices A and B (statements and commits per row on
  a linear pipeline) and the measured counts in Appendix F (per source row on
  real examples with gates and sinks) use different reference shapes and are
  not expected to match exactly.
- The profile artefacts (`.prof` files, collapsed stacks, logs, scratch
  settings) were written to an ignored lane directory and are not retained.
  Appendix F records the commands needed to regenerate them.

## Relationship to the composer remediation plan

[composer-performance-remediation.md](composer-performance-remediation.md) is
the plan of record for composer performance and already carries verified
mechanisms and fix statuses. This recon was done independently of it and has
not been reconciled item by item. Known overlaps:

| This recon | Remediation plan item |
|---|---|
| Heavy CPU on the event loop shared with HTTP (Appendix C) | S-3 |
| New HTTP client per prompt-guard inspection (Appendix C) | S-4 |
| Full transcript load at turn start (Appendix C) | S-5 |
| Two to three whole-graph validations per tool call (Appendix D) | S-6 |
| `advisor_transcript.observe_messages` rescans (Appendix C) | S-8 |
| Credential scan per provider call (Appendix C) | Fix 11 |
| Executor and repository both hashing each row (Appendix B) | Fix 9 — the three hashes per token × node counted here are the post-fix figure |
| Run-progress websocket poll cost (Appendix E) | Fix 3-T1 |

Where the two disagree, re-read the code; neither document is authoritative
over it.

## 1. Executor

### Spine

Paths are under `src/elspeth/`.

| Phase | Frequency | Path |
|---|---|---|
| Setup | once per run | `cli.py:1121 run` → `engine/orchestrator/run_lifecycle.py:416` → `leader_drain.py:102 execute_run` |
| Ingest | per source row | `source_iteration.py:515` → `engine/processor.py:2868 process_row` → `engine/scheduler_drain.py:421 drain_claims` |
| Traversal | per claim | `engine/token_traversal.py:1069 process_single_token` |
| Node execution | per token × node × attempt | `processor.py:2435 _execute_transform_with_retry` → `engine/executors/transform.py:811 execute_transform` (`state_guard.py:219` begin, `:430` complete) |
| Sink | once, after all sources | `leader_drain.py:300` → `executors/sink.py:1473 write`, then a per-token checkpoint |

The leader is one thread processing one row at a time; `max_workers` only
caps batch-transform pending slots. Followers are separate processes that can
only claim fork or expand children and barrier continuations, so a linear
pipeline gives them nothing. Sink writes are deferred to the end of the run,
so memory grows with total tokens and each sink receives the whole run as one
batch.

### Where the time goes (measured)

| Workload | Marginal rows/s | Audit DB | Canonical JSON + hashing | Engine | Plugins |
|---|---:|---:|---:|---:|---:|
| gate, 2,000 rows | 37.6 | 73.5% | 11.5% | 1.2% | 0.2% |
| transform, 2,000 rows | 33.5 | 63.1% | 16.2% | 1.7% | 1.3% |
| batch aggregation, 2,000 rows | 33.6 | 76.3% | 10.9% | 1.3% | 0.3% |
| fork/coalesce, 600 rows | 8.0 | 79.3% | 9.1% | 1.2% | 0.1% |

Percentages are cProfile self time on the marginal per-row cost, attributed
by module path. cProfile over-weights small Python calls, so the canonical
JSON column overstates its real share on narrow rows (see the corrections
above).

- **The audit-write trunk is the hot path, and within it SQLAlchemy rather
  than SQLite.** Roughly 50–60% of per-row time is SQLAlchemy's Python
  statement construction and compilation; sqlite3 C calls are 7–13%. A py-spy
  sample of the gate run agreed: 76.3% of samples had a SQLAlchemy frame and
  16.5% were inside sqlite3 `execute` or `commit`.
- **Statement volume per source row is high.** The gate workload issues
  about 81 SQL executes and 10 commits per source row; fork/coalesce about
  390 and 53.
- **Ingest is as heavy as node execution.** By py-spy phase: ingest plus
  initial claim (`core/landscape/scheduler/queue.py:353`) 40.9%, node
  execution 36.9%, sink flush 19.8%. Static reading puts ingest at 28
  statements per row.
- **Each node costs two commits.** `begin_node_state` and
  `complete_node_state` each open their own `BEGIN IMMEDIATE` transaction;
  there is no write batching on the transform path.
- **The same row is hashed repeatedly.** Each row version is canonically
  serialised three times per node — twice as output, once as the next node's
  input. Measured `canonical_json` calls were 16–52 per source row.
  `rfc8785` is a pure-Python implementation.
- **Fork/coalesce is about three times the per-row cost of the others, and
  linear.** The first measurement here suggested super-linear growth
  (102 → 136 ms/row); a four-size re-measurement found it flat at
  109–110 ms/row. See the corrections above.
- **Fixed startup is about 7 s**, roughly 70% of it module import.

### Findings that are not about ranking

- **Quadratic sink reservation.**
  `core/landscape/execution/sink_effect_reservation.py:358` and `:370`
  rebuild a tuple of every row locked so far inside the per-member loop. The
  profiler counted 44 iterations at 10 rows, 10,300 at 200 and 1,003,049 at
  2,000. It is about 1% of time at 2,000 rows and grows with rows pending per
  sink.
- **The DB-write micro-benchmarks do not run.** The four throughput tests in
  `tests/performance/benchmarks/test_db_write.py` fail with
  `TypeError: DataFlowRepository.register_node() got an unexpected keyword argument 'run_id'`,
  so the dominant cost bucket has no working micro-benchmark. The `performance`
  marker is deselected by default, which is how this went unnoticed.
- **Existing perf tests do not cover the dominant costs.** They use an
  in-memory database and a mock payload store, and none measures commits per
  row, `complete_node_state`, ingest, checkpoints or the sink path.
- **Payload store directory validation** runs 8 times per row, about 129
  `lstat` calls per row.

## 2. Composer

### Spine

Paths are under `src/elspeth/web/`.

1. **HTTP.** `sessions/routes/messages.py:47 send_message` admits a durable
   operation and returns 202. The turn does not run in the request.
2. **Worker.** `sessions/composer_async_worker.py:334` claims the job
   (asyncio task on the serving event loop, concurrency 4, per-session lock
   plus a DB-fenced lease) → `composer_turn.py:840 _run_composer_turn` →
   `composer/service.py:1110 compose`. An empty graph goes to the planner
   (`pipeline_planner.py:3364`); otherwise to `service.py:2352 _compose_loop`.
3. **Per LLM round.** `service.py:1452 _call_model_turn` →
   `provider_gateway.py:451 _litellm_acompletion` → `:569 litellm.acompletion`,
   the only physical provider call site (six callers).
4. **Per tool call.** `tool_batch.py:940 run_tool_batch` →
   `tools/_dispatch.py:795 execute_tool` on a 16-thread pool.
5. **Per tool round.** `turn_audit.py:212 persist_turn_audit`.
6. **Settle.** One transaction. The client makes one SSE attempt, then polls
   `GET /operations/{op}` every 1,000 ms.

### Load-bearing costs

- **Whole-graph validation runs at least three times per mutating tool
  call.** `composer/state.py:8443 validate()` is reached from three places in
  `tools/_common.py`, and each walk constructs a real plugin instance per
  node. A rejected mutation still pays two walks; a read-only call pays two
  even on a discovery-cache hit.
- **`_check_schema_contracts` (`composer/state.py:4790`, 3,362 lines) is the
  heavy half of every walk.**
- **A valid mutation with reviews pending also triggers full Stage-2
  validation** (`execution/validation.py:497`): a YAML round trip, every
  plugin instantiated, a real `ExecutionGraph`. Its cache misses on every new
  state version. `validation_memo.py` serves `/validate` and
  `/audit-readiness`, not the composer loop.
- **Each LLM round re-serialises the full context at least four times** and
  retains one canonical copy per call, so CPU and stored bytes grow with
  rounds squared over a turn.
- **Every provider call is wrapped by three DB writes and one prompt-guard
  HTTP call.**
- **Persistence is about 20–24 SQL statements per mutating call** (9–11
  otherwise), mostly repeated fence proofs, with a full state snapshot row
  per mutation.
- **Heavy CPU work — deepcopy, canonical JSON, redaction — runs on the event
  loop** shared with HTTP and every other turn.
- **The freeform loop has no discovery-turn cap**, only a deadline;
  `composer_max_discovery_turns` binds the planner alone.

For scale, the one key-free measurement: a single validate of a 50-node
state is about 65 ms and scales linearly.

No tutorial-only branch was found on the compose spine, and
`litellm.acompletion` is the single provider call site. Accept/reject and
YAML import run inline with no provider call; `composer_mcp` shares only
`execute_tool` and the tool definitions.

## 3. Other hot paths

Ranked by the Appendix E investigator; all static.

1. **LLM call trunk** — `plugins/transforms/llm/transform.py:289` →
   `plugins/infrastructure/clients/llm.py:695`. Per attempt: limiter, network
   call, two Landscape writes, two hashes. This is where wall-clock goes in
   real runs and it was not profiled.
2. **Web-run LLM governance** — `web/execution/service.py:4964-5002`. Each
   LLM call is settled by re-reading every LLM call in the run
   (`_run_token_usage_entries`, `:872`) and filtering for one call id, which
   is quadratic in calls per run. Each call also adds four sessions-DB write
   transactions. Only web-launched runs pay this.
3. **Row serialisation at the batch handoff** —
   `engine/executors/transform.py:603-624`. The engine blocks per row, so
   real concurrency is only `pool_size` queries within a row (default 1).
4. **Run-progress websocket poll** — `web/execution/routes.py:2166-2223`,
   PostgreSQL only: about 12 statements every 0.25 s per socket.
5. **Per-request auth** — `web/auth/middleware.py:80,158`: seven statements
   and four connection acquisitions before any route body.
6. **Web validation trunk** — `web/execution/validation.py:497-776`: about
   20 phases per call including full plugin instantiation and graph build.
7. **CSV sink effect rebuild** — `plugins/sinks/csv_sink.py:499-652`
   re-streams and re-hashes the whole output per effect. Later verification
   found one effect per sink per run in the common case, so this is
   downgraded.
8. **Per-row HTTP client construction** —
   `plugins/transforms/azure/base.py:560-580`; no connection reuse across
   rows.
9. **Rate limiter acquire** — `core/rate_limit/limiter.py:261-309`: one lock
   per service with a 10 ms sleep-poll.
10. **CSV source row emission** — `plugins/sources/csv_source.py:594-629`:
    pydantic validation plus contract validation per row.

Web runs execute on a single-worker thread pool
(`web/execution/service.py:1199`), so one web run at a time per process.
Nothing under `tests/performance` imports `elspeth.web`, so none of the
composer or web paths has performance coverage.

Rejected as not hot: ASGI middleware, plugin discovery, DAG build, the
exporter, lineage `explain`, MCP analyzers, purge, lease and heartbeat
timers, and progress-event persistence.

## 4. Verification status of the headline claims

| Claim | Status |
|---|---|
| Executor bucket split, per-row statement, commit and hash counts | Measured (Appendix F) |
| Quadratic tuple rebuild in sink reservation | Measured, and the loop was re-read at `1a7ff0e6` |
| Web-run governance rescans all LLM calls per call | Static; the function and its caller were re-read at `1a7ff0e6` |
| `test_db_write.py` benchmark failure | Measured (two runs, exit 1) |
| All composer and web-tier counts | Static, one reader each, not cross-checked |
| Other `path:line` citations in the appendices | As read by the investigator of that appendix |

## Appendix A — Executor hot path, control-flow side

HEAD `1a7ff0e6`. **Static analysis only** — no venv in this checkout, nothing was run, no timings are
measured. Every hop below was read in the code and is cited `path:line`. Paths are relative to
`src/elspeth/` unless they start with `tests/`. Orientation docs were not consulted; the code is the
authority. Landscape/canonical/payload/checkpoint internals are out of scope — only the call sites that
enter them are named, and anything about what happens inside is marked UNVERIFIED.

Frequency classes used: `run` (once per run) · `src` (per source) · `row` (per source row) ·
`claim` (per scheduler work item = per token segment between hand-offs) · `tok×node` ·
`attempt` (per retry attempt) · `batch` · `flush` (per sink × outcome-group) · `tick` (heartbeat) ·
`ext` (per external call).

### 0. Headline findings

1. **The leader is one thread and one row at a time.** Source `next()` → `process_row` → drain →
   `transform.process()` all run synchronously on the calling thread
   (`engine/orchestrator/source_iteration.py:747-897`). `concurrency.max_workers` only caps the
   pending-slot count of a row-pipelined batch transform (`engine/executors/transform.py:364-367`);
   it creates no engine worker pool.
2. **A claim carries a token through every consecutive node** until a hand-off (sink, fork, expand,
   barrier) — `engine/token_traversal.py:1146-1288`. Scheduler journal writes are therefore
   per-claim, not per-node; node-state audit writes are per token×node.
3. **Sink writes happen once, after all sources are exhausted.** `pending_tokens` is an in-memory
   `dict[sink -> list[(TokenInfo, PendingOutcome)]]` appended to per row
   (`engine/orchestrator/outcomes.py:98-108`) and only drained at
   `engine/orchestrator/leader_drain.py:300` (and `:344` for follower output). Memory is O(total
   tokens) and each sink receives the whole run as one batch per outcome group.
4. **Row data is deep-copied at almost every seam.** `PipelineRow.__init__` does `deep_freeze`
   (`contracts/schema_contract.py:647`) and `PipelineRow.to_dict()` does `deep_thaw`
   (`contracts/schema_contract.py:739`). The engine alone calls `to_dict()` 3x per token×transform.
5. **Linear happy path costs about `2T + 3` engine-initiated repository calls per row** (T =
   transforms), counted in §3. Whether each is its own commit is the landscape lane's question.

### 1. Call spine

#### Phase A — CLI setup / preflight (all `run`)

| # | Hop | Freq |
|---|-----|------|
| A1 | `cli.py:1121 run()` — load settings+secrets, instantiate plugins | run |
| A2 | `cli.py:1222 ExecutionGraph.from_plugin_instances` → `cli.py:1236 graph.validate()` | run |
| A3 | `cli.py:1312 resolve_preflight` (dependency runs / commencement gates) | run |
| A4 | `cli.py:1336` → `cli.py:1905 _execute_pipeline_with_instances` — `LandscapeDB.from_url`, payload store | run |
| A5 | `cli.py:1769 _orchestrator_context` — rate-limit registry, telemetry manager, `CheckpointManager` (`:1879`), `Orchestrator` (`:1881`) | run |
| A6 | `cli.py:2001 ctx.orchestrator.run(...)` | run |
| A7 | `engine/orchestrator/core.py:220 Orchestrator.run` → `:289 _run_lifecycle.run` | run |
| A8 | `engine/orchestrator/run_lifecycle.py:416 RunLifecycleCoordinator.run` — `:506 prepare_for_run()`, `:528 initialize_database_phase` (→ `:227 begin_run`, mints leader seat) | run |
| A9 | `run_lifecycle.py:571-575` start `RunHeartbeatThread` | run |
| A10 | `run_lifecycle.py:604 execute_run` → `core.py:345` → `engine/orchestrator/leader_drain.py:102 LeaderDrainCoordinator.execute_run` | run |
| A11 | `leader_drain.py:147 checkpoint_run_start` (sequence-0 checkpoint) | run |
| A12 | `leader_drain.py:150 register_graph_nodes_and_edges` → `engine/orchestrator/landscape_registration.py:203 register_node` per node, `engine/orchestrator/graph_registration.py:163 register_edge` per edge | run (×nodes, ×edges) |
| A13 | `leader_drain.py:155 initialize_run_context` → `engine/orchestrator/run_context_factory.py:254/270/275` plugin `on_start`, `:278 build_processor` → `engine/orchestrator/processor_factory.py:138 build_row_processor` → `RowProcessor.__init__` (`engine/processor.py:413`, 548 lines) | run |
| A14 | `leader_drain.py:168 run_transform_runtime_preflights` | run |

#### Phase B — source iteration

| # | Hop | Freq |
|---|-----|------|
| B1 | `leader_drain.py:204-230` loop over sources in YAML order → `engine/orchestrator/source_iteration.py:515 run_main_processing_loop` | src |
| B2 | `source_iteration.py:651 track_operation(source_load)` + `:661 source_span` + `:674 record_run_source_lifecycle(LOADING)` | src |
| B3 | `source_iteration.py:685 load_source_with_events` (`:451`, lazy generator over `active_source.load(ctx)`) | src |
| B4 | `source_iteration.py:747 while True` → `:766 next(source_iterator)` (or `:756` idle-pump fetch when timed barriers exist) | row |
| B5 | `source_iteration.py:787 record_field_resolution` (first row), `:864 _record_source_contract` (first valid row) | src |
| B6 | `source_iteration.py:878 check_aggregation_timeouts` → `engine/orchestrator/aggregation.py:103` (in-memory loop over aggregation nodes) | row |
| B7 | `source_iteration.py:887 processor.process_row(...)` | row |
| B8 | `source_iteration.py:898 accumulate_row_outcomes` → `engine/orchestrator/outcomes.py:263` → `:54 _route_to_sink` (append to `pending_tokens`) | row (×results) |
| B9 | `source_iteration.py:901 handle_coalesce_timeouts`, `:911 handle_row_union_timeouts` (only if those executors exist) | row |
| B10 | `source_iteration.py:919 maybe_emit_progress` (`:292`; emits on row 1, every 100 rows, or every 5 s) | row (emit: per 100 rows) |
| B11 | `source_iteration.py:934 check_coordination_latch()`, `:938 shutdown_event.is_set()`, `:943 restore_source_iteration_context` | row |
| B12 | Quarantined row short-circuit: `source_iteration.py:795-861` → `quarantine_router.route` → `engine/processor.py:2825 ingest_quarantined_row` (one fenced txn), then the same sweeps | row (quarantined only) |

#### Phase C — per-row ingest and drain

| # | Hop | Freq |
|---|-----|------|
| C1 | `engine/processor.py:2868 process_row` — `:2917 source_row.to_pipeline_row()` (deep_freeze), `:2918 TokenInfo(generate_id(), generate_id())` | row |
| C2 | `processor.py:2931 run_boundary_checks` (declaration dispatch, `engine/executors/declaration_dispatch.py:124 _dispatch`) | row |
| C3 | `processor.py:2962 _initial_work_item_for_source_token` (`:2739`) | row |
| C4 | `processor.py:2973 _ingest_source_row_with_initial_claim` (`:2772`) — `:2978 pipeline_row.to_dict()`, `:2979 checkpoint_dumps(contract.to_checkpoint_format())`, `:2793 _work_codec.ready_fields` (`engine/scheduler_work_codec.py:93`, serialises row payload JSON at `:105`), `:2794 _scheduler.ingest_row_with_initial_claim` — **one fenced txn: row + token + source completion + enqueue + claim** | row |
| C5 | `processor.py:3008 _drain_work_queue` (`:4017`) → `:5115 _drain_durable_work_queue` → `:5275 _drain_scheduler_claims` → `engine/scheduler_drain.py:421 drain_claims` | row |
| C6 | `scheduler_drain.py:472 _maintenance_due` (`:410`; true every 64th drain) → `:349 run_maintenance` (`:391 dead_non_leader_workers`, `:404 recover_expired_leases`) | row/64 |
| C7 | `scheduler_drain.py:475 while True` — one iteration per claim | claim |
| C8 | `scheduler_drain.py:514 _run_barrier_intake_pass` → `engine/barrier_coordination.py:419 run_intake_pass` (early return `:445-451` when no barrier is configured; else `:457 list_pending_blocked_barrier_items` DB read, `:1225 list_unadopted_group_losses`, `:508 pending_empty_expansion_groups`) | claim (+1 extra iteration whenever a token blocks) |
| C9 | `scheduler_drain.py:528-535` take preclaimed item, else `_scheduler.claim_ready` | claim (DB only for non-preclaimed) |
| C10 | `scheduler_drain.py:616-619` rehydrate live `WorkItem` from `pending_items` (or deserialise journal payload) | claim |
| C11 | `scheduler_drain.py:628-634` `plugin_context_scope` + `row_span` | claim |
| C12 | `scheduler_drain.py:639 _process_single_token` (`:646 claim_attempt_offset`, DB read only when `claimed.attempt > 1`, `:943`) | claim |
| C13 | `scheduler_drain.py:670 heartbeat_active_claim` (`:1129`; DB write at most once per `scheduler_heartbeat_seconds`=60, `processor_factory.py:161`) | claim (DB: per 60 s) |
| C14 | Disposition: sink-bound `scheduler_drain.py:763 serialize_row_payload` + `:786 mark_pending_sink` (or `:771 ..._with_ready_children`); blocked `:739/:754 _mark_claimed_scheduler_work_blocked` (`:1019`); failed `:813 mark_failed`; terminal `:832 mark_terminal` | claim (1 DB write) |
| C15 | `scheduler_drain.py:845` exit when `pending_items` empty | row |

#### Phase D — per-token traversal and per-node execution

| # | Hop | Freq |
|---|-----|------|
| D1 | `processor.py:5456 _process_single_token` → `engine/token_traversal.py:1069 process_single_token` | claim |
| D2 | `token_traversal.py:1146 while node_id is not None` — `:1157 _heartbeat_active_claim` (time check), `:1158 _maybe_coalesce_token`, `:1168 _maybe_row_union_token`, `:1177 _maybe_collector_token`, `:1185-1186 resolve_next_node / resolve_plugin_for_node` | tok×node |
| D3 | Transform arm: `token_traversal.py:1267 handle_transform_node` (`:118`) → `:156 _execute_transform_with_retry` | tok×node |
| D4 | `processor.py:2435 _execute_transform_with_retry` → (retry configured) `:2555 _retry_manager.execute_with_retry` → `engine/retry.py:72` (builds a tenacity `Retrying` at `:127` every call) → `processor.py:2519 execute_attempt` | tok×node; inner body per attempt |
| D5 | `engine/executors/transform.py:811 execute_transform` | attempt |
| D6 | `transform.py:881 token.row_data.to_dict()`; `:898 transform_span`; `:904 NodeStateGuard` → `engine/executors/state_guard.py:219 _execution.begin_node_state` | attempt (1 DB write) |
| D7 | `transform.py:922 _run_preflight` (`:372`) — `:496 derive_effective_input_fields`, `:536 run_pre_emission_checks`, `:564 input_schema.model_validate(input_dict, strict=True)` | attempt |
| D8 | `transform.py:934 plugin_context_scope`; `:946 _invoke_transform` (`:574`) → `:627 transform.process(row, ctx)` or batch path `:596-611` (`adapter.register` / `accept` / `waiter.wait`) | attempt (plugin = `ext` boundary) |
| D9 | `transform.py:989 _verify_ownership_before_terminal_audit` → `processor.py:686` wires `_heartbeat_active_claim` | attempt |
| D10 | `transform.py:999 _verify_success_emissions` (`:630`) — `:661 run_post_emission_checks`, `:696 output_schema.model_validate(emitted_row.to_dict(), strict=True)`, `:710 verify_produced_output_types` | attempt |
| D11 | `transform.py:1011 _populate_result_audit_fields` (`:712`) — `:737 stable_hash(result.row)` | attempt |
| D12 | `transform.py:1026 _prepare_success_completion` (`:751`) — `:781 result.row.to_dict()`, `:803 update_node_output_contract` when the output contract object `is not` the input contract object | attempt (conditional DB write) |
| D13 | `transform.py:1035 guard.complete(COMPLETED)` → `state_guard.py:430 _execution.complete_node_state` | attempt (1 DB write) |
| D14 | `transform.py:1047 token.with_updated_data(result.row)` | attempt |
| D15 | `token_traversal.py:163 _emit_transform_completed` (`processor.py:1024`; no-op without a telemetry manager) | tok×node |
| D16 | Gate arm: `token_traversal.py:1201 handle_gate_node` (`:511`) → `:544 execute_config_gate` → `engine/executors/gate.py:243` — `:287 to_dict()`, `:288 stable_hash(input_dict)`, `:297 gate_span`, `:303 NodeStateGuard` (begin), `:320 parser.evaluate`, routing event `:495/:512`, `guard.complete` | tok×gate (3 DB writes) |
| D17 | End of chain: `token_traversal.py:1290 handle_terminal_token` (`:1022`) builds the sink-bound `RowResult`; durable hand-off is C14 | claim |

#### Phase E — sink write / flush

| # | Hop | Freq |
|---|-----|------|
| E1 | `leader_drain.py:283 wait_for_peer_leases` (`engine/orchestrator/leader_follower_drain.py:98`; 0.5 s poll, immediate at N=1), `:285 has_unresolved_scheduler_work` | run |
| E2 | `leader_drain.py:300 flush_and_write_sinks` → `engine/orchestrator/sink_flush.py:335` → `:130 write_pending_to_sinks` | run |
| E3 | `sink_flush.py:217` per sink: `:265 sorted(pairs, key=(outcome, path, error_hash))`, `:267 groupby` | per sink |
| E4 | `sink_flush.py:298 sink_span` + `:304 sink_executor.write` → `engine/executors/sink.py:1473 SinkExecutor.write` | flush |
| E5 | `sink.py:1557 rows = [t.row_data.to_dict() ...]`; `:1566 _merge_batch_contract` (`:578`, loop over every token); `:1572 _open_or_reuse_effect_states` (`:671`: `get_open_node_state_ids`, `get_members_for_tokens`, then `:608 _open_primary_states` → `begin_node_states_many` bulk) | flush, O(tokens) inside |
| E6 | `sink.py:1583 _write_primary_effect` (`:748`) — `:770 _run_sink_boundary_checks`, `:778 _validate_sink_input`, `:810 canonical_json_loads(pipeline_canonical_json(row))` per row, reservation + `SinkEffectCoordinator(...).execute_with_lease_wait` (`:913-928`) → `engine/executors/sink_effects.py` `_execute` → adapter `prepare/commit/reconcile` → plugin sink | flush, O(tokens) inside |
| E7 | `sink.py:1616 on_token_written(token)` per accepted token → `sink_flush.py:102` composite → (a) `engine/orchestrator/checkpointing.py:133 maybe_checkpoint` → `:185 manager.create_checkpoint` when frequency matches; (b) `sink_flush.py:75` batch-of-64 → `processor.py:5315 mark_sink_bound_scheduler_terminal_many` | per token at sink phase |
| E8 | `sink_flush.py:321 consume_group` (rebuilds the sink's pending list, O(n) per group) | flush |
| E9 | `leader_drain.py:360 drain_pending_sink_work` (follower output re-drain loop; no-op at N=1) | run |

#### Phase F — finalisation (all `run`)

`leader_drain.py:383 sweep_deferred_invariants_or_crash` → `:428 cleanup_plugins` →
`run_lifecycle.py:638 assert_bound_groups_settled_from_audit` → `:639 derive_terminal_status_from_audit`
→ `:644 _finalize_led_run` (`:324 finalize_run`) → `:653 _delete_checkpoints_after_success` →
`:683 execute_export_phase` (if enabled) → `_heartbeat.stop()` → `:705 release_seat` → run summary event
→ `cli.py:1437 cli_completion_for` → exit code.

### 2. Frequency multipliers at a glance

For R source rows, T transforms, G gates on the path, F fork/expand children per row:

- `process_row`, ingest txn, one drain: **R**
- drain loop iterations / claims: **R × (1 + F)** plus one extra iteration per token that blocks at a barrier
- `execute_transform`: **R × T × attempts** (× (1+F) below a fork)
- node-state DB writes: **2 × R × (T + G)** plus 1 routing-event write per token×gate, plus sink states in bulk
- sink `write()` calls: **sinks × distinct (outcome, path, error_hash) groups** — each ON_ERROR_ROUTED error hash is its own group (`sink_flush.py:258-263`)
- heartbeat thread: every 15 s (`contracts/coordination.py:51`), independent of row count

### 3. Cost centres on the per-row and per-token×node hops

#### DB round-trips, linear happy path (source → T transforms → sink)

Counted by reading the repository call sites reachable on the path; each is one engine→repository
call (commit granularity inside is UNVERIFIED, landscape lane).

| Call site | Per row |
|---|---|
| `processor.py:2794 ingest_row_with_initial_claim` | 1 |
| `state_guard.py:219 begin_node_state` | T |
| `state_guard.py:430 complete_node_state` | T |
| `transform.py:803 update_node_output_contract` | 0..T (fires whenever a transform returns a row whose contract is a different object from the input's) |
| `scheduler_drain.py:786 mark_pending_sink` | 1 |
| `scheduler_drain.py:391 + 404` maintenance | 2/64 |
| `checkpointing.py:185 create_checkpoint` at sink phase | 1 at default `every_row` (`core/config.py:1940`; frequency logic `checkpointing.py:172-177`) |
| `processor.py:5317 mark_pending_sink_terminal_many` | 1/64 |
| sink batch calls (E5/E6) | amortised |

Total **≈ 2T + 3 per row**, up to 3T + 3 if contracts are re-minted per row. `claim_ready` is not
called at all on this path: the ingest pre-claims (`scheduler_drain.py:528-529`) and the loop exits
at `:845`.

Additions per shape:
- Gate: begin + routing event + complete = 3 per token×gate (`gate.py:303`, `:495`, guard complete).
- Fork/expand: 1 `fork_token`/`expand_token` txn (`engine/tokens.py:335`, `:636`), parent disposition with children in one write (`scheduler_drain.py:823`), then per child 1 `claim_ready` (`:531`) + its node states + 1 disposition.
- Any barrier configured anywhere in the pipeline: every drain iteration pays `list_pending_blocked_barrier_items` (`barrier_coordination.py:457`); with coalesce/row_union/collector also `list_unadopted_group_losses` (`:1225`).
- Token buffered at an aggregation: `mark_blocked` (`scheduler_drain.py:1041`), a second loop iteration (`:748 continue`) with another intake read, `adopt_blocked_barrier_item` (`barrier_coordination.py:532`), then a `claim_ready` miss before the loop exits.

#### Copies, serialisation, hashing

| Where | What | Freq |
|---|---|---|
| `processor.py:2917` | `to_pipeline_row()` → `deep_freeze` | row |
| `processor.py:2978` | `pipeline_row.to_dict()` → `deep_thaw` | row |
| `processor.py:2979` | `checkpoint_dumps(contract.to_checkpoint_format())` — the contract is re-serialised for every row | row |
| `scheduler_work_codec.py:105` | `serialize_row_payload` (row → JSON) | row, and per child continuation |
| `scheduler_work_codec.py:104` | `resolve_row_ingest_sequence(row_id)` for continuations (not ingest) — DB read or cache UNVERIFIED | per child |
| `transform.py:881`, `:696`, `:781` | three `to_dict()` deep thaws | attempt |
| `transform.py:737` | `stable_hash(result.row)` | attempt |
| `state_guard.py:219/430` | raw dicts handed to Landscape; `guard.state.input_hash` (`transform.py:1014`) shows the input is hashed inside begin; whether output is hashed again inside complete is UNVERIFIED | attempt |
| `gate.py:287-288` | `to_dict()` + `stable_hash` in the engine, then the same dict goes to `begin_node_state` | tok×gate |
| `scheduler_drain.py:763` | `serialize_row_payload` of the final row | claim |
| `tokens.py:367`, `:676` | `copy.deepcopy` per fork / expand child | per child |
| `sink.py:1557`, `:810`, `:894` | `to_dict()`, canonical-JSON round-trip, `dict(member.row)` | per token at sink phase |

#### Validation

- Pydantic strict validation twice per attempt: input `transform.py:564`, output `:696`.
- Declaration-contract dispatch three times per row×transform: boundary (`processor.py:2931`, row only), pre-emission `transform.py:536`, post-emission `:661`; each walks the registered contracts for the site (`declaration_dispatch.py:142`).
- `transform.py:496 derive_effective_input_fields`, `:710 verify_produced_output_types`, spelling-surface check `:413-422`.

#### Spans / telemetry

Context-manager spans per claim (`scheduler_drain.py:630`), per attempt (`transform.py:898`), per
gate (`gate.py:297`), per flush (`sink_flush.py:281`). `SpanFactory` returns a shared `NoOpSpan`
when no tracer is configured (`engine/spans.py:229`, `:266`); the context-manager overhead remains.
Telemetry events are skipped when no manager is set (`processor.py:1039`).

#### Locks, scans, polling

- No engine lock is taken on the linear per-row path. `SharedBatchAdapter._lock` (`engine/batch_adapter.py:171`) is taken per row only for batch-runtime transforms; its waiter wakes every 50 ms while waiting (`:227`).
- `coalesce_executor.py:1521` — `check_timeouts` scans all pending groups once per source row when a timeout is configured.
- `sink_flush.py:321` — list rebuild per group; `sink.py:600` — contract merge across all tokens.
- `retry.py:127` — a tenacity `Retrying` (with stop/wait/retry objects) is constructed per token×node when retry is configured (`processor_factory.py:227`).
- Poll loops: peer-lease wait 0.5 s (`leader_follower_drain.py:81`), sink-effect lease wait 0.5 s (`sink_effects.py:300`), follower idle poll (`follower.py:451`). None is on the N=1 per-row path.

### 4. Concurrency model

- **Leader:** main thread does everything in §1 B–E. Extra threads: `RunHeartbeatThread` (one DB write every 15 s; `heartbeat.py:338-341`), optional `IdleTimeoutPump` (`idle_timeout_pump.py:88`, only with timed aggregation/coalesce, `source_iteration.py:688-703`), sink-effect lease heartbeat during a sink write (`sink_effects.py:152`), and plugin-owned pools behind `BatchTransformRuntime`.
- **Batch-runtime transforms** still block the leader per row: `accept()` then `waiter.wait()` (`transform.py:603-611`). The pool only helps work a single row fans out internally.
- **Followers** are separate processes (`cli.py:4398 join`) running `follower.py:347 _drain_loop` → `processor.py:5243 drain_follower_ready_work` → the same `drain_claims`, `claim_ready` only, no sink work, no maintenance (`scheduler_drain.py:374`). Each claim is preceded by a forced `live_leader` DB read (`follower.py:371`, via `before_claim` at `scheduler_drain.py:479`).
- **What followers can actually take:** only READY journal rows. The leader pre-claims each source row inside the ingest transaction and keeps claiming its own children until `pending_items` is empty, so followers compete only for fork/expand children and barrier continuations. In a linear pipeline there is nothing for them to claim. (The ingest can return READY rather than LEASED — `processor.py:5131-5133` handles it — the conditions for that are inside the repository, UNVERIFIED.)
- **Serialisation points every row passes:** (1) the leader thread; (2) the Landscape DB writer, hit 2T+3 times per row and shared with the heartbeat thread and any followers; (3) the single end-of-run sink phase, leader-only.
- **Where rows queue:** in the scheduler journal (READY/LEASED/BLOCKED/PENDING_SINK), in barrier executor memory, and in `pending_tokens` until the end of the run.

### 5. Ranked top 10 hottest functions

Ranked by frequency × body weight as read from the code (not measured).

1. `TransformExecutor.execute_transform` — `engine/executors/transform.py:811` (297 lines). Per attempt: 2 DB writes, 3 deep thaws, 2 strict pydantic validations, 1 canonical hash, 2 declaration dispatches, 1 span.
2. `NodeStateGuard.__enter__` / `.complete` — `engine/executors/state_guard.py:217` / `:394`. The audit write pair for every token×node×attempt; this is where the engine enters Landscape most often.
3. `SchedulerDrainCoordinator.drain_claims` — `engine/scheduler_drain.py:421` (429 lines). Per row plus per child: intake pass, claim, payload serialisation, disposition write.
4. `RowProcessor.process_row` + `_ingest_source_row_with_initial_claim` — `engine/processor.py:2868` / `:2772`. Per row: deep freeze, deep thaw, contract JSON, row JSON, boundary dispatch, fenced ingest txn.
5. `TransformExecutor._run_preflight` + `_verify_success_emissions` — `transform.py:372` / `:630`. Per attempt: both schema validations and both declaration dispatches live here.
6. `TokenTraversalEngine.process_single_token` — `engine/token_traversal.py:1069` (222 lines). Per claim with a per-node loop: heartbeat time check, three barrier probes, two navigator lookups, dispatch.
7. `RowProcessor._execute_transform_with_retry` → `RetryManager.execute_with_retry` — `engine/processor.py:2435` / `engine/retry.py:72`. Per token×node: closures plus a fresh tenacity `Retrying`.
8. `SinkExecutor.write` + `_write_primary_effect` — `engine/executors/sink.py:1473` / `:748` (277 + 325 lines). Called few times but O(all tokens) inside: deep thaw, contract merge, canonical-JSON round-trip, per-token callback.
9. `CheckpointCoordinator.maybe_checkpoint` — `engine/orchestrator/checkpointing.py:133`. Per sink-written token; at the default `every_row` it is one checkpoint write per token, plus `get_barrier_scalars` (`processor.py:1202`) each call.
10. `BarrierIntakeCoordinator.run_intake_pass` — `engine/barrier_coordination.py:419`. Per drain iteration; free without barriers, at least one DB read per iteration once any barrier exists.

Also per token×gate: `GateExecutor.execute_config_gate` (`engine/executors/gate.py:243`, 231 lines).
Per result and cheap: `accumulate_row_outcomes` (`outcomes.py:263`).

#### Largest / most branch-dense functions on the spine

Line counts are def-to-next-def spans (docstrings included). "Branch lines" is a rough proxy: lines
starting with `if/elif/else/for/while/try/except/with` in that span.

| Function | Lines | Branch lines | Freq |
|---|---|---|---|
| `RowProcessor.__init__` `processor.py:413` | 548 | — | run |
| `SourceIterationDriver.run_main_processing_loop` `source_iteration.py:515` | 484 | 39 | src (loop body per row) |
| `SchedulerDrainCoordinator.drain_claims` `scheduler_drain.py:421` | 429 | 39 | row (loop body per claim) |
| `RunLifecycleCoordinator.run` `run_lifecycle.py:416` | 393 | 27 | run |
| `LeaderDrainCoordinator.execute_run` `leader_drain.py:102` | 343 | 16 | run |
| `SinkExecutor._write_primary_effect` `sink.py:748` | 325 | 38 | flush |
| `TransformExecutor.execute_transform` `transform.py:811` | 297 | 13 | attempt |
| `TokenTraversalEngine.handle_gate_node` `token_traversal.py:511` | 294 | 15 | tok×gate |
| `TokenTraversalEngine.handle_transform_node` `token_traversal.py:118` | 278 | 19 | tok×node |
| `SinkExecutor.write` `sink.py:1473` | 277 | 14 | flush |
| `TokenTraversalEngine.process_single_token` `token_traversal.py:1069` | 222 | 18 | claim |
| `SinkFlushCoordinator.write_pending_to_sinks` `sink_flush.py:130` | 205 | 15 | run |
| `TransformExecutor._run_preflight` `transform.py:372` | 202 | 12 | attempt |
| `RowProcessor._execute_transform_with_retry` `processor.py:2435` | 172 | 15 | tok×node |
| `RowProcessor.process_row` `processor.py:2868` | 142 | 4 | row |
| off-spine, resume only: `BarrierRecoveryCoordinator.restore_from_journal` `barrier_coordination.py:1679` | 949 | 111 | run |

### 6. Variants — where they diverge and rejoin

- **Fork (gate):** diverges at `token_traversal.py:1201` → `handle_gate_fork` (`:895`) → `tokens.py:302 fork_token` (1 txn, deepcopy per child). Parent ends FORKED; children enqueue READY in the parent's disposition (`scheduler_drain.py:823`) and rejoin at C7 as new claims in the same drain.
- **Expand (multi-row transform):** `token_traversal.py:317 expand_token` (`tokens.py:564`); children rejoin at C7 the same way.
- **Coalesce / row_union / collector:** the token stops at D2 (`_maybe_coalesce_token` `processor.py:3038` etc.), returns `None`, and the drain marks it BLOCKED (`scheduler_drain.py:754`). The next iteration's intake adopts it (`barrier_coordination.py:585/732/835`); on a merge the merged token's continuation is enqueued (`scheduler_drain.py:516-517`) and rejoins at C7. Timeouts are swept per row at B9; leftovers at EOF by `leader_drain.py:445 run_end_of_input_barrier_flush`.
- **Aggregation:** diverges at `token_traversal.py:1237` → `processor.py:2203` (BUFFERED) → `mark_blocked` → intake `_adopt_aggregation_row` (`barrier_coordination.py:515`) → on trigger `_flush_batch` → `processor.py:4589 _complete_aggregation_flush`; outputs rejoin as results or continuations. The batch transform runs once per batch. Timeouts: B6 per row; EOF: `aggregation.py:190`.
- **Multi-source:** sequential, YAML order (`leader_drain.py:204`); the same processor, counters and `pending_tokens` are shared. End-of-input barrier flush runs only after the last source (`:226 flush_end_of_input`).
- **Resume:** `core.py:359` → `engine/orchestrator/resume.py:786`; restores barrier memory via `restore_from_journal`, re-drives journal work with `drain_scheduled_work` (`resume.py:298`, `processor.py:4037`, which also re-claims PENDING_SINK rows at `scheduler_drain.py:850`), replays unprocessed rows through the same `process_row` (`resume.py:1347`), and rejoins at `flush_and_write_sinks` (`resume.py:1393`). Source `on_start` is skipped (`resume.py:1266`).
- **Replay / verify modes:** source rows come from audited snapshots (`source_iteration.py:565-581`); sinks use `VirtualReplaySinkEffect` (`sink.py:911`); requires `max_workers=1` (`engine/orchestrator/run_modes.py:59`).
- **Follower:** enters at C7 via `drain_follower_ready_work`; never ingests, never writes sinks, never runs intake adoption; at an aggregation node it only records the arrival and blocks (`token_traversal.py:1259-1268`). Its sink-bound results are PENDING_SINK rows the leader drains at `leader_drain.py:360`.

### 7. Existing in-tree perf evidence

All under `tests/performance/`, marker `performance`, **deselected by default**
(`pyproject.toml:486`), so none of it gates a normal run. Thresholds are generous smoke bounds,
not regression budgets.

| File | Measures | Asserted threshold |
|---|---|---|
| `benchmarks/test_throughput.py:57` | 100 rows, passthrough, full `Orchestrator.run`, in-memory DB | < 5.0 s (> 20 rows/s) |
| `benchmarks/test_throughput.py:80` | 1000 rows, passthrough | < 30 s (> 33 rows/s) |
| `benchmarks/test_throughput.py:102` | 100 rows × 5 transforms | < 10 s (> 10 rows/s) |
| `benchmarks/test_throughput.py:125` | 100 rows, fork pipeline | < 10 s (> 10 rows/s) |
| `benchmarks/test_db_write.py:65-240` | `begin_run` < 50 ms; create row > 500/s; create token > 1000/s; `begin_node_state` > 500/s; record outcome > 1000/s | as listed |
| `benchmarks/test_canonical_hashing.py:77-156` | `stable_hash` small > 5000/s, medium > 500/s, large > 50/s | as listed |
| `benchmarks/test_cross_check_overhead.py:80-551` | declaration dispatch: pass-through check median < 25 µs, tail < 50 µs; batch-flush median < 1500 µs | as listed |
| `benchmarks/test_token_expansion.py:88-267` | `deepcopy` cost and expand simulation (10× small < 1 ms, 100× small < 10 ms, 50× large < 500 ms) — simulates, does not call `expand_token` against a DB | as listed |
| `benchmarks/test_schema_validation.py:21-217` | plugin instantiation < 500 ms, graph build < 100 ms, end-to-end validation < 200 ms | as listed |
| `scalability/test_large_datasets.py:61-83` | 1k / 10k rows, 1k rows with a gate | **correctness only**; timing captured in `_timing` and never asserted |
| `scalability/test_deep_dag.py:66-88` | 10 / 20 / 50 transforms | correctness only |
| `scalability/test_many_sinks.py:85-136` | 10 / 50 / 100 sinks | correctness only |
| `scalability/test_wide_rows.py:60-101` | 100 / 500 / 1000 fields | only the 1000-field case asserts < 120 s |
| `memory/test_memory_baseline.py:60-121` | RSS delta per 1000 rows < 200 MB; scaling < 500 MB | as listed |
| `memory/test_leak_detection.py:60-128` | growth across repeated runs | bounded growth |
| `stress/` | telemetry backpressure, concurrent sinks, LLM retry, rate limiter, call-index concurrency | behavioural |

Gaps relative to this spine: no benchmark isolates per-row engine overhead from DB time; nothing
measures file-backed SQLite or PostgreSQL; nothing exercises aggregation/coalesce intake cost,
retry-configured transforms, the end-of-run sink batch at scale, or leader + follower throughput.
The implied floor (20–33 rows/s for a passthrough) is consistent with a per-row cost dominated by
several DB round-trips, but that is an inference from thresholds, not a measurement.

### 8. Not verified

- Commit/transaction granularity and hashing inside any `factory.*` / `_scheduler.*` / `_execution.*` call.
- Whether `resolve_row_ingest_sequence`, `open_batch_membership` and `merge_for_batch` hit the DB or allocate.
- Conditions under which the fenced ingest returns READY instead of LEASED.
- `sink_effects.py` internals beyond the entry call (`_execute`, lease, finalize) — read at outline level only.
- `handle_gate_node` body, coalesce/row_union/collector executor internals and `resume.py` — read at call-site level only.
- Docs under `docs/architecture/` were not cross-checked against this map.

## Appendix B — Executor data and persistence trunk

Tree at HEAD `1a7ff0e6`. **Static analysis only** — this checkout has no
Python venv, nothing was executed, and no timing in this document is measured. Every count
below was produced by reading the cited code and counting `conn.execute` / `conn.scalar` /
`_ops.execute_*` call sites on the happy path; BEGIN/COMMIT are excluded from statement counts
and stated separately. Anything I did not read end-to-end is marked UNVERIFIED.

Reference shape used for "happy path" counts: single leader (N=1), linear pipeline
source -> T transforms -> 1 sink, no gates/forks/aggregation, no external calls, payload store
configured, default checkpoint settings, journal off, SQLite file DB.

---

### 0. Structural facts that shape everything

- The run is driven through a **durable scheduler** that lives in the Landscape DB
  (`token_work_items` + `scheduler_events`), not only an in-memory queue. A token is
  claimed once and then traverses all its transform nodes inside that one claim
  (`engine/scheduler_drain.py:634` -> `_process_single_token`), so scheduler writes are
  **per token**, node-state writes are **per token x node**.
- **Every Landscape write method opens its own transaction and commits** (`begin_write`,
  `core/landscape/database.py:266-289`). There is no write batching, buffering or group
  commit anywhere on the transform path. The only bulk verbs are on the sink side
  (`begin_node_states_many`, sink-effect reserve/finalize, `mark_pending_sink_terminal_many`).
- Every write transaction starts with a **fence statement**:
  - member fence = 1 `UPDATE run_workers SET status='active' WHERE ... status='active'`
    (`run_coordination_repository.py:541-550`);
  - item fence = member fence + 1 `UPDATE token_work_items SET status='LEASED' WHERE ...`
    (`item_fencing.py:43-56`);
  - leader fence = CAS `UPDATE run_coordination` + clock `SELECT` + renew `UPDATE` on entry
    (`run_coordination_repository.py:404-466`), clock `SELECT` + renew `UPDATE` again on exit
    (`:491-492`), and one more clock `SELECT` in the commit listener because a deadline was
    issued (`lease_deadlines.py:113-120`). **6 overhead statements per leader-fenced
    transaction**, and the `*_on` helpers re-run the 3-statement entry fence when composed
    (`data_flow/tokens.py:456-458`, `execution/node_states.py:257-262`).
- Sinks are written **after the source loop ends** (`orchestrator/leader_drain.py:293`,
  `sink_flush.py:367`), one `sink_executor.write` per (sink, outcome, path, error_hash) group
  (`sink_flush.py:263-304`). All sink-bound tokens are held in `pending_tokens` until then.

---

### 1. Write-side inventory (engine-called Landscape writes)

Callers confirmed by grep over `src/elspeth/engine/` and `src/elspeth/plugins/infrastructure/`.
"Stmts" = SQL statements inside the transaction excluding BEGIN/COMMIT. "Own txn" = the
method opens `begin_write` itself, i.e. one commit per call.

#### Once per run
| Method | Where | Tables | Own txn |
|---|---|---|---|
| `run_lifecycle.begin_run` / `complete_run` / `finalize_run` / `record_run_source` / `update_run_source_contract` / `record_preflight_results` / `record_secret_resolutions` / `record_source_field_resolution` | `run_lifecycle_repository.py` (2207 lines; bodies NOT read — UNVERIFIED stmt counts) | `runs`, `run_sources`, ... | yes |
| `data_flow.register_node` / `register_edge` | `data_flow_repository.py:536,568` -> `data_flow/graph.py` | `nodes`, `edges` | yes (per node / edge) |
| `run_coordination.acquire_run_leadership` / `release_seat` / `admit_follower` | `run_coordination_repository.py:730,1122,1396` | `run_coordination`, `run_workers`, coordination events | yes |
| `checkpoint_run_start` | `orchestrator/checkpointing.py:99` -> `checkpoint/manager.py:101` | `checkpoints` | yes |
| `begin_operation` / `complete_operation` (source load, each sink effect) | `execution/operations.py` | `operations` | yes |
| background `worker_heartbeat` thread (time-based, not row-based) | `run_coordination_repository.py:1298`, `orchestrator/heartbeat.py:341` | `run_workers` | yes; competes for the SQLite write lock on a second connection |

#### Per source row — 1 call, 1 commit
`TokenSchedulerRepository.ingest_row_with_initial_claim`
(`scheduler_repository.py:207` -> `scheduler/queue.py:353-465`; caller `engine/processor.py:2795`).
One leader-fenced `BEGIN IMMEDIATE` transaction composing:

| Step | Code | Stmts |
|---|---|---|
| leader fence entry | `run_coordination_repository.py:445-466` | 3 |
| re-fence + INSERT `rows` + INSERT `tokens` | `data_flow/tokens.py:456-492` | 3 + 2 |
| re-fence + INSERT `node_states` (source, COMPLETED) + SELECT read-back | `execution/node_states.py:257-299` | 3 + 2 |
| transaction-time SELECT; validate refs (SELECT tokens, SELECT rows, SELECT nodes); INSERT `token_work_items` ON CONFLICT; INSERT `scheduler_events` (ENQUEUE); SELECT work item | `scheduler/queue.py:288-339`, `scheduler/work_items.py:185-222,269-300`, `scheduler/events.py:174` | 7 |
| claim: SELECT ... FOR UPDATE; clock SELECT; UPDATE -> LEASED; INSERT `scheduler_events` (CLAIM_READY); SELECT claimed | `scheduler/leases.py:212-340` | 5 |
| leader fence exit renew | `run_coordination_repository.py:491-492` | 2 |
| commit-guard clock read | `lease_deadlines.py:113-120` | 1 |
| **Total** | | **28 statements, 1 commit** |

Tables: `run_coordination`, `rows`, `tokens`, `node_states`, `nodes` (read), `token_work_items`,
`scheduler_events`. Of the 28, 9 are leader-fence/renew statements against the same
`run_coordination` row and 5 are clock reads.

The payload-store write and four canonical serializations of the row happen **inside this
transaction, while the SQLite write lock is held** (`data_flow/tokens.py:461-471` calls
`_prepare_source_row_record`, which hashes and calls `payload_store.store` at `:349-363`).

#### Per token x transform node — 2 commits (3 when the contract object changes)
| Method | Where | Tables | Stmts | Own txn |
|---|---|---|---|---|
| `execution.begin_node_state` | `execution/node_states.py:114-186`; caller `executors/state_guard.py:219` | `run_workers`, `node_states` | 2 (fence UPDATE, INSERT) | yes |
| `execution.complete_node_state` | `execution/node_states.py:541-675`; caller `state_guard.py:430` | `run_workers`, `node_states` | 3 (fence UPDATE, UPDATE, SELECT read-back) | yes |
| `data_flow.update_node_output_contract` (conditional) | `data_flow/graph.py:430-486`; caller `executors/transform.py:800-807` | `run_workers`, `nodes` | 2 on the no-change path (fence UPDATE, SELECT ... FOR UPDATE), 3 when it writes | yes |

The third row fires whenever `output_contract is not token.row_data.contract`
(`transform.py:800`, an **identity** test). `narrow_contract_to_output` always returns a new
`SchemaContract` (`contracts/contract_propagation.py:123-127`) and is called per row by
field_mapper, json_explode, line_explode, web_scrape, reference_join, blob_* and
pdf_rasterize (grep in `plugins/transforms/`). For those transforms every row pays a write
transaction that re-parses the stored contract JSON, rebuilds a `SchemaContract`, computes
`version_hash()` twice and then returns without writing (`graph.py:447-456`).

#### Per token (scheduler disposition) — 1 commit
`scheduler.mark_pending_sink` (`scheduler/dispositions.py:253-300` -> `_transition` `:703` ->
`_transition_on` `:853-1029`; caller `scheduler_drain.py:786`). Member-fenced, 7 statements:
fence UPDATE, clock SELECT, SELECT before, UPDATE, SELECT after, SELECT bundle-complete,
INSERT `scheduler_events`. Tables: `run_workers`, `token_work_items`, `scheduler_events`.
Siblings on non-sink-bound paths: `mark_terminal`, `mark_failed`, `mark_blocked`,
`*_with_ready_children` (same `_transition_on` core).

Only when a token is not pre-claimed (children, follower drain, resume):
`scheduler.claim_ready` (`scheduler/leases.py:114-169`) — member fence + clock + ordered
`SELECT ... LIMIT 1` + the 5 claim statements + commit-guard clock = 9 statements, 1 commit.
`scheduler.heartbeat_lease` is throttled to once per `scheduler_heartbeat_seconds` (default 60)
by `scheduler_drain.py:1171-1179`, so it is not per row.

#### Per external call — 3 commits + up to 2 payload files
| Method | Where | Tables | Stmts | Own txn |
|---|---|---|---|---|
| `execution.allocate_call_index` | `execution/calls.py:137-200` | `run_workers`, `token_work_items`, `calls` (MAX read, first call per state only) | 2-3 | yes |
| `execution.record_call` | `execution/calls.py:497-676` | + `node_states` (SELECT ... FOR UPDATE parent), `calls` (SELECT max/count, INSERT) | 5 (+ source-call verify in replay) | yes |
| `_record_call_payload_refs` (when a payload store is configured) | `execution/calls.py:458-476` | `node_states` (FOR UPDATE), `calls` (UPDATE refs) | 4 | yes |

`CallRepository._call_indices` (`calls.py:124`) gains one dict entry per `state_id` and I found
no code that removes entries (grep of `_call_indices` in the file shows only inserts/increments)
— unbounded growth over a long run, small per entry.

Operation-scoped variants (`allocate_operation_call_index`, `record_operation_call`,
`calls.py:202,679`) are leader-fenced, used by sources/sinks.

#### Per gate decision
`execution.record_routing_event` / `record_routing_events`
(`execution/node_states.py:969-1023`, `1133-1202`; callers `executors/gate.py:354,495,512`).
With a reason and a payload store: one extra **read transaction**
(`_assert_routing_targets_recordable`, `:1243`), `stable_hash(reason)` + `canonical_json(reason)`,
`payload_store.store` **plus a `retrieve` read-back** (`:1284-1290`), then one member-fenced
write transaction (`_insert_or_load_routing_decision`, `:1450-1536`; statement count not
tallied — UNVERIFIED, at least lock state + load existing + insert).

#### Per batch (aggregation / fork / expand / coalesce)
`create_batch`, `update_batch_status`, `complete_batch`, `complete_aggregation_result`
(`execution_repository.py:844-1260`), `fork_token`, `expand_token`, `coalesce_tokens`,
`collect_tokens`, `record_empty_expansion` (`data_flow/tokens.py:563,1372,804,1709,2030`),
`scheduler.complete_barrier` / `mark_blocked`. Each is its own transaction. `expand_token`
and `collect_tokens` store one payload blob per child inside the open write transaction
(`tokens.py:1602`, `:1893`). Bodies only skimmed — statement counts UNVERIFIED.

#### Per sink write (one batch per sink group, end of run)
| Step | Where | Notes |
|---|---|---|
| `get_open_node_state_ids` + `sink_effects.get_members_for_tokens` | `executors/sink.py:685-695` | 2 batch reads |
| `execution.begin_node_states_many` | `execution/node_states.py:476-539`; caller `sink.py:634` | 1 leader txn, 1 executemany INSERT; `stable_hash` per member (`:493`) |
| `resolve_sink_effect_members` | `execution/sink_effect_identity.py:239-300` | **3 separate read transactions per member** (see section 8) |
| `sink_effects.reserve` | `execution/sink_effect_reservation.py:290-320,883-921` | 1 leader txn, bulk member INSERT |
| `_persist_pipeline_member_payloads` | `executors/sink_effects.py:1353-1364` | 1 payload-store write per member |
| attempt/lease/commit bookkeeping | `executors/sink_effects.py:1395-1700`, `execution/sink_effect_lifecycle.py` | several txns per effect — UNVERIFIED count |
| `sink_effects.finalize` | `execution/sink_effect_finalization.py:138-398` | 1 leader txn: bulk token outcomes (`data_flow/outcomes.py:651-735`), executemany member UPDATE, node-state completion, artifact, operation |
| `scheduler.mark_pending_sink_terminal_many` | `scheduler/dispositions.py:436-598`; batched by 64 in `sink_flush.py:76-84` | 1 leader txn per 64 tokens |

#### Per checkpoint — default cadence is one per sink-written token
`CheckpointManager.create_checkpoint` (`core/checkpoint/manager.py:101-192`): leader-fenced,
8 statements (6 fence overhead + SELECT duplicate-sequence + INSERT `checkpoints`), 1 commit.
See section 5.

---

### 2. Transaction / commit model

- **Engine construction**: `LandscapeDB.from_url` -> `create_engine(url, echo=False,
  hide_parameters=True, **engine_kwargs)` (`database.py:2046`). The run path passes no pool
  arguments (`cli.py:1945-1957`), so the pool is SQLAlchemy's default for the dialect. The
  module comment states file SQLite gets `QueuePool`, one connection per thread
  (`database.py:216`); `DatabaseSettings.pool_size` (`core/config.py:1885`) is not passed to
  the Landscape engine by any call site I found — UNVERIFIED whether it is used elsewhere.
- **SQLite pragmas** set on every new DBAPI connection (`database.py:1137-1152`):
  `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`; verified
  at startup (`_verify_sqlite_pragmas`, `:1199-1252`). `synchronous=NORMAL` under WAL means a
  commit does not fsync (stated in the comment at `:92-96`), so per-commit cost on SQLite is
  dominated by SQLAlchemy/Python overhead and WAL appends, not disk flushes.
- **Begin semantics**: pysqlite's implicit BEGIN is disabled (`isolation_level=None`,
  `:1177`); an engine `begin` listener emits `BEGIN IMMEDIATE` for write-intent connections and
  plain `BEGIN` otherwise (`:1179-1196`). Every write therefore takes the single WAL write lock
  at BEGIN — SQLite writers are fully serialized, across threads and processes, with up to
  5 s of busy-wait.
- **Single-writer serialization in-process**: only for `StaticPool` engines
  (`LandscapeDB.in_memory()`, tests) via a per-engine `RLock` (`:221-262`). File-backed and
  PostgreSQL engines have no Python-level write lock.
- **PostgreSQL**: no pragmas, no `synchronous_commit` or isolation override on the run path
  (grep: the only isolation override is the exporter's REPEATABLE READ,
  `export_read_model.py:546`). Fences rely on row locks (`with_for_update`) and the verify-UPDATE
  form. Every statement counted in section 1 is a network round trip and every commit is a
  server-side WAL flush at default settings (UNVERIFIED — depends on server config).
- **Reads**: `read_only_connection` (`database.py:2229-2254`) wraps each read in its own
  transaction and, on SQLite, brackets it with `PRAGMA query_only = ON` / `OFF`; on PostgreSQL
  it issues `SET TRANSACTION READ ONLY`. `ReadOnlyDatabaseOps.execute_fetchone/fetchall`
  (`_database_ops.py:36-57`) opens one such transaction per query. A single-row lookup is
  therefore BEGIN + PRAGMA + SELECT + PRAGMA + COMMIT on SQLite.
- **Commit listeners**: `_check_deadlines_before_commit` runs on every commit
  (`lease_deadlines.py:113-128`); it is a dict lookup unless the transaction issued a deadline
  (leader fences and claims do). With the JSONL journal enabled (default off,
  `core/config.py:1805-1806`) every statement also passes `_after_cursor_execute` and every
  commit serializes an outbox batch and fsyncs the journal file (`journal.py:220-221,569-600`).
- **Batching/buffering of writes**: none on the per-row transform path. Present only at the
  sink (bulk begin, bulk finalize, terminalization in 64s).

#### Commits per token per node (happy path) and how I counted
Counted `begin_write` entries reached per call:
- transform node: `begin_node_state` (1) + `complete_node_state` (1) = **2 commits, 5 statements**;
  **3 commits, 7 statements** when the transform emits a new contract object.
- per token outside the nodes: ingest (1 commit, 28 stmts) + `mark_pending_sink`
  (1 commit, 7 stmts) + checkpoint at default `every_row` (1 commit, 8 stmts).
- Total write commits per token, linear T-transform pipeline: **2T + 3** (up to 3T + 3),
  plus amortized sink-batch transactions and 1/64 of a terminalization transaction, plus
  3 read transactions per token at the sink.
- Statements per token: **5T + 43** (7T + 43 with per-row contract objects), before sink-batch
  statements and the sink lineage reads.

---

### 3. Hashing / canonicalization

#### Cost of one `stable_hash` / `canonical_json`
`core/canonical.py:183-207`: `_normalize_for_canonical` (recursive, builds a full copy;
per leaf `_normalize_value` runs up to ~17 `isinstance` checks, numpy/pandas first,
`canonical.py:37-143`) -> `contracts/hashing.py:71-73`:
`_normalize_frozen_and_reject_non_finite` (second recursive full copy, `:37-56`) ->
`rfc8785.dumps` -> `.decode` -> `.encode` -> `sha256`. `rfc8785` 0.1.4 ships only a
`py3-none-any` wheel (`uv.lock:4364-4369`), i.e. a pure-Python serializer. So one hash is
**three Python-level tree walks and two full structure copies**, with a bytes->str->bytes
round trip. A `PipelineRow` argument adds a `deep_thaw` copy first (`canonical.py:163-164`).
numpy arrays are finiteness-checked vectorially then normalized element by element via
`tolist()` (`canonical.py:86-97`); pandas Timestamps go through `tz_localize/tz_convert`.

#### Per source row (inside the ingest transaction)
The same row dict is canonically serialized **4 times** and SHA-256'd 5 times:
1. `stable_hash(data)` -> `source_data_hash` (`data_flow/tokens.py:349`)
2. `canonical_json(data)` -> payload bytes (`tokens.py:362`); `payload_store.store` then
   computes `sha256(content)` (`payload_store.py:230`), which equals (1) by construction
3. `stable_hash(input_data)` (`execution/node_states.py:270`)
4. `stable_hash(output_data)` (`node_states.py:271`) — `input_data` and `output_data` are the
   same object (`scheduler/queue.py:442-443`)

Plus, before the transaction: `serialize_row_payload` (`engine/scheduler_work_codec.py:106`
-> `scheduler/payload_codec.py:20-40`): `deep_thaw` + `_reject_nan_infinity` walk +
`_escape_reserved_keys` copy + `json.dumps`, and `SchemaContract.version_hash()` twice;
`checkpoint_dumps(contract.to_checkpoint_format())` (`processor.py:2990`) -> a third
`version_hash()`. Two scheduler events add 2 x (2 small `canonical_json` + sha256)
(`scheduler/events.py:83-106`).

#### Per token x transform node — 3 canonical serializations, 2 of them redundant
1. input hash: `stable_hash(input_dict)` in `begin_node_state` (`node_states.py:154`). The
   executor reuses this value rather than hashing again (`transform.py:877-881,1014`).
   It is the same content the previous node just hashed as its output.
2. output hash for the result object: `stable_hash(result.row)` (`transform.py:737`).
3. output hash for the node state: `stable_hash(output_data)` in `complete_node_state_on`
   (`node_states.py:610`) — same content as (2), hashed again from a fresh `to_dict()` copy.

So each distinct row version is canonically serialized **3 times** (twice as output, once as
the next node's input). Config hashes (`stable_hash(node config)`) are per-run except in the
checkpoint path (section 5).

#### Per token at the sink — 5 canonical serializations of the row
`begin_node_states_many` `stable_hash` (`node_states.py:493`);
`canonical_json_loads(pipeline_canonical_json(row))` (`executors/sink.py:810`);
`type_faithful_stable_hash(candidate.row)` (`sink_effect_identity.py:271`);
`canonical_json(deep_thaw(member.row))` for the payload (`executors/sink_effects.py:1360`);
finalization output hash over `{"row": ...}` (`sink.py:887`, hashed in the finalize path —
exact line UNVERIFIED). Plus `stable_hash(pending_identity)` and `canonical_json(lineage)`
per member (`sink_effect_identity.py:267,272`) and a second `serialize_row_payload` at
`mark_pending_sink` (`scheduler_drain.py:763`).

#### Per external call — 4 canonical serializations
`_prepare_call_payloads` (`execution/calls.py:331-344`): `stable_hash(request)` +
`canonical_json(request)`, `stable_hash(response)` + `canonical_json(response)`. Each pair
serializes the same dict twice; the payload store then SHA-256s the bytes a third time.
LLM responses are the largest objects in the system, so this is the heaviest hashing site
per invocation.

---

### 4. Payload store (`core/payload_store.py`)

When written:
- every source row (`data_flow/tokens.py:363`) — inside the ingest write transaction;
- every external call request and response (`execution/calls.py:370`) — after the call row
  commits, before a second transaction attaches the refs;
- every sink member row (`executors/sink_effects.py:1362`);
- every expand/collect/coalesce child payload (`tokens.py:1010,1602,1893`);
- every gate routing reason, with read-back (`node_states.py:1286-1289`).
Not written per node state — node states store hashes only.

`store()` (`payload_store.py:221-273`), new blob: SHA-256; `_path_for_hash` does two
`Path.resolve()` calls (`:209-211`); `_open_payload_parent(create=False)` validates the base
dir (`lstat` + `resolve(strict=True)`, `:69-85`) and probes the shard dir; then a second
`_open_payload_parent(create=True)` re-validates, `mkdir`s, opens the shard fd with another
`lstat`/`fstat`/`resolve` (`:87-110`); temp file `O_EXCL` write + `flush` + **`fsync(file)`**
(`:171-174`); `os.replace`; **`fsync(dir)`** (`:264`). So **2 fsyncs and about 6 path
resolutions per new blob, synchronous, on the calling thread**. These fsyncs are the only
per-row forced disk flushes on a SQLite run (the DB itself runs `synchronous=NORMAL`).

Dedup check cost: there is no cheap existence test — on a hit the existing file is **read in
full and re-hashed** (`:239-247`), then returned. Repeated content (identical routing reasons,
identical LLM requests) pays a full read + SHA-256 each time.

---

### 5. Checkpointing

- Cadence (`orchestrator/checkpointing.py:133-193`): called from the post-sink callback once
  per sink-written token (`:231-238`). `frequency==1` (`every_row`, the default —
  `core/config.py:1939-1940`, `contracts/config/runtime.py:586`) checkpoints every token;
  `every_n` every N tokens; **`frequency==0` (`aggregation_only`) also checkpoints every
  token** (`checkpointing.py:172-177`, intentional per the comment there).
- Because sinks flush after the source loop, checkpoints are written in a burst at end of
  run, not interleaved with row processing.
- What each checkpoint computes/serializes:
  - `get_barrier_scalars()` is evaluated for **every** token before the frequency gate
    (`checkpointing.py:234-237` -> `processor.py:1203-1227`); in-memory only.
  - `_build_checkpoint_draft` recomputes `compute_full_topology_hash(graph)` **on every
    checkpoint** (`checkpointing.py:89` -> `core/canonical.py:210-243`): one `stable_hash` of
    every node's config, three `get_node_info` lookups per node, two sorts, then a
    `stable_hash` of the whole topology. The graph is immutable during a run; nothing caches it.
  - `checkpoint_dumps(barrier_scalars.to_dict())` only when barrier state exists
    (`manager.py:167-169`); otherwise NULL.
  - Row persisted: id, run_id, sequence number, scalars JSON, topology hash, format version
    (`manager.py:171-181`). No row data.
- DB cost: 8 statements, 1 commit (section 1).

Scheduler row payloads (`token_work_items.row_payload_json`) are the real per-token resume
state and are written twice per token (ingest, `mark_pending_sink`).

---

### 6. Row object churn

`PipelineRow` (`contracts/schema_contract.py:610-852`): `__slots__`, immutable.
- construction always `deep_freeze`s — a recursive rebuild into `MappingProxyType`/tuples
  (`:647`, `contracts/freeze.py:22-74`);
- `to_dict()` always `deep_thaw`s — a recursive rebuild into dict/list (`:733-742`);
- `__copy__`/`__deepcopy__` are thaw + re-freeze (`:807-833`);
- `to_checkpoint_format()` thaws and calls `contract.version_hash()` (`:835-850`).

`SchemaContract` (`:149-195`): frozen slots dataclass; `__post_init__` builds two index dicts
and runs duplicate checks. **`version_hash()` is not cached** (`:394-433`): each call sorts
the fields, builds a list of dicts, runs `canonical_json` and SHA-256. It is called 3 times
per source row at ingest and 2 more times at `mark_pending_sink`, and 2 times per
`update_node_output_contract`.

Per token x transform node, full-row structural copies (counted from `transform.py`):
1. `token.row_data.to_dict()` -> `input_dict` (`:881`)
2. plugin builds the output `PipelineRow` -> `deep_freeze`
3. `emitted_row.to_dict()` for strict pydantic output-schema validation, run for every
   emitted row of every success result (`:694-696`, no gating condition in the loop)
4. `stable_hash(result.row)` -> `to_dict()` inside the normalizer (`:737`)
5. `result.row.to_dict()` -> `output_data` (`:781`)
6-9. two normalize copies per `stable_hash` x 2 output hashes; 2 more for the input hash

`TokenInfo` (`contracts/identity.py:230-332`): frozen slots dataclass;
`with_updated_data` = `dataclasses.replace` re-running `__post_init__` type checks, once per
node (`transform.py:1047`). `NodeStateOpen`/`NodeStateCompleted` are constructed per write and
again by the loader on read-back (`node_states.py:158-168,668`).

---

### 7. Telemetry / spans / events per row

- `SpanFactory._make_span` (`engine/spans.py:305-431`): with no tracer and no telemetry
  callback it yields a shared `NoOpSpan` (`:327-329`) — cost is one generator context manager.
  With telemetry: `copy_context()`, a lock acquisition for the run trace, `datetime.now`,
  attribute bounding, a `ContextVar` set/reset and an `EngineSpanCompleted` event per span.
  Spans per token: 1 `row_span` (`scheduler_drain.py:625`) + 1 `transform_span` per node
  (`transform.py:898`); `sink_span` is per batch.
- Telemetry events: `TransformCompleted` per token x node (`processor.py:1024-1065`, skipped
  when no manager), `TokenCompleted` per token, `RowCreated` per row.
  `TelemetryManager.handle_event` (`telemetry/manager.py:409-497`) re-allocates a projected
  copy of every event (`_project_event_for_egress`, `:50-80`) and runs observers **before**
  the disabled / no-exporter / granularity checks (`:428-465`); export itself is a bounded
  queue to a background thread.
- `EventBus.emit` is synchronous (`core/events.py:64-90`); the per-row loop only emits
  progress every 100 rows or 5 s (`source_iteration.py:99-100,306-313`).
- Rate limiting (`core/rate_limit/limiter.py:261-309`) is per external call, not per row:
  a lock plus `pyrate_limiter.try_acquire`, polling with 10 ms sleeps when throttled; the
  optional SQLite-persisted bucket opens its own `sqlite3` connection (`:224`).

---

### 8. Read-side queries executed during a run

In-transaction reads (already counted in section 1):
- node-state read-back `SELECT *` after every completed insert/update
  (`node_states.py:299,660`) — 1 per token x node, feeding a loader whose result the guard
  largely discards;
- work-item `SELECT` before and after each disposition plus a bundle-complete `SELECT`
  (`dispositions.py:885-887,989-993`) — 3 per token;
- reference validation at ingest: tokens, rows, nodes (`work_items.py:195-222`) — 3 per row,
  re-reading rows the same transaction inserted a few statements earlier;
- database clock reads: 5 per ingest, 1 per disposition, 3 per checkpoint;
- call index: `SELECT MAX` once per state (`calls.py:193`), `SELECT max,count` per recorded
  call (`calls.py:262-267`), parent `SELECT ... FOR UPDATE` twice per call (`:401-408`).

Standalone read transactions:
- **Sink lineage resolution, 3 read transactions per sink member**:
  `resolve_sink_effect_members` (`sink_effect_identity.py:239-300`) calls
  `query.get_token` (`query_repository.py:320-330`), `query.get_token_parents` (`:370-380`)
  and `query.get_row` (`:158-168`) for each candidate. `_LineageQueryCache` only dedups
  repeats; for root tokens each of the three is a fresh single-row query in its own
  read-only transaction. A bulk `get_tokens_by_ids` exists (`:345`) but is only used for
  parents. For N sink-bound tokens this is 3N read transactions (15N driver statements on
  SQLite including BEGIN/PRAGMA/COMMIT) in the end-of-run burst.
- gate routing: 1 read transaction per decision with a reason (`node_states.py:1243`).
- barrier pipelines only: `list_pending_blocked_barrier_items` once per drain iteration
  (`barrier_coordination.py:457`), `resolve_row_ingest_sequence` per aggregation output
  (`processor.py:4227`), `list_blocked_barrier_items` (`processor.py:1266`). Linear pipelines
  short-circuit before any query (`barrier_coordination.py:445-451`).
- scheduler maintenance every 64 drains (`scheduler_drain.py:80,410-415`).
- `is_release_group` is memoized per group (`engine/tokens.py:231`).

Not on the hot path: `exporter.py`, `export_read_model.py`, `lineage.py`, `query_repository`
explain paths, `checkpoint/recovery.py` (resume only).

---

### 9. Ranked top 10 hottest functions in scope (frequency x body weight, static estimate)

1. **`core.canonical.stable_hash` / `canonical_json`** (`canonical.py:183-207` +
   `contracts/hashing.py:37-73`) — 4 per source row, 3 per token x node, 5 per sink member,
   4 per external call; each is 3 pure-Python tree walks with 2 full copies. Most calls
   re-serialize content already serialized.
2. **`NodeStateRepository.begin_node_state` + `complete_node_state(_on)`**
   (`node_states.py:114,541`) — 2 commits and 5 statements per token x node; the only
   frequency class that scales with rows x pipeline depth.
3. **`fenced_member_transaction` / `begin_write` / `fenced_leader_transaction`**
   (`run_coordination_repository.py:469,570`, `database.py:266`) — entered by every write;
   pool checkout + BEGIN IMMEDIATE + fence statement(s) + commit listener. Leader variant
   adds 6 statements each time.
4. **`SchedulerQueueRepository.ingest_row_with_initial_claim`** (`scheduler/queue.py:353`)
   — 28 statements per source row, with hashing and 2 fsyncs performed while holding the
   write lock.
5. **`PipelineRow.to_dict` / `deep_thaw` / `deep_freeze`** (`schema_contract.py:647,733`,
   `freeze.py:22,101`) — 3-4 full-row copies per token x node before hashing copies.
6. **`FilesystemPayloadStore.store`** (`payload_store.py:221`) — per source row, per call
   payload, per sink member; 2 fsyncs + ~6 path resolutions per new blob; full read + rehash
   on dedup hit.
7. **`SchedulerDispositionRepository._transition_on` + `SchedulerEventStore.record`**
   (`dispositions.py:853`, `events.py:131`) — 7 statements per token, plus 2 canonical_json +
   sha256 per event to derive `event_id`.
8. **`CheckpointManager.create_checkpoint` + `compute_full_topology_hash`**
   (`manager.py:101`, `canonical.py:210`) — once per sink-written token by default;
   re-hashes every node config each time and runs an 8-statement leader transaction.
9. **`serialize_row_payload` + `SchemaContract.version_hash`** (`payload_codec.py:20`,
   `schema_contract.py:394`) — twice per token; thaw + 2 walks + `json.dumps`, and an
   uncached contract hash computed 5 times per token.
10. **`resolve_sink_effect_members` -> `QueryRepository.get_token/get_row/get_token_parents`**
    (`sink_effect_identity.py:239`) — 3 single-row read transactions per sink-bound token.

Conditional entries that outrank several of the above when present:
- `DataFlowGraphRepository.update_node_output_contract` (`data_flow/graph.py:430`) — a third
  write transaction per token x node for any transform that builds its output contract per row.
- `CallRepository.record_call` (`calls.py:497`) — 3 commits + 2 payload files + 4 canonical
  serializations of LLM-sized payloads per external call.

---

### 10. Existing perf evidence in-tree

All under the `performance` marker, which the default pytest selection deselects (AGENTS.md).
Every one uses `LandscapeDB.in_memory()` (StaticPool, `journal_mode=memory`) and, where a
payload store is used, `MockPayloadStore` — so none exercises WAL, BEGIN IMMEDIATE lock
contention, PostgreSQL round trips, or payload fsyncs.

| File | Measures | Asserted threshold |
|---|---|---|
| `benchmarks/test_db_write.py` | `begin_run` latency; row creation; token creation; `begin_node_state`; outcome recording (1000 iterations each) | < 50 ms/run (`:86`); > 500 rows/s (`:116`); > 1000 tokens/s (`:153`); > 500 states/s (`:198`); > 1000 outcomes/s (`:240`) |
| `benchmarks/test_canonical_hashing.py` | `stable_hash` on small / medium / large rows, repeat-consistency | > 5000 (`:93`), > 500 (`:113`), > 50 (`:132`), > 200 rows/s (`:156`) |
| `benchmarks/test_cross_check_overhead.py` | pass-through cross-check and declaration dispatcher overhead (pytest-benchmark) | median < 25 us, tail < 50 us (`:115-116`); batch-flush median < 1500 us, tail < 3000 us (`:223-224`); parametrized dispatcher budgets (`:301-304,545-551`) |
| `benchmarks/test_schema_validation.py` | plugin instantiation, graph construction, end-to-end validation | < 500 ms (`:85`), < 100 ms (`:147`), < 200 ms (`:217`) |
| `benchmarks/test_token_expansion.py` | `deepcopy` of small/medium/large rows, simulated 10x/100x/50x-large expansion, memory amplification | < 10 us, < 200 us, < 5 ms (`:104,124,144`); < 1 ms, < 10 ms, < 500 ms (`:165,186,207`); deep < 500 us (`:235`) |
| `benchmarks/test_validator_walk.py` | DAG validator diamond walk | p99 and median < 5 ms (`:85,88`) |
| `benchmarks/test_throughput.py` | full orchestrator runs: 100 and 1000 passthrough rows, 5 transforms, fork | < 5 s (`:74`), < 30 s (`:96`), < 10 s (`:119`), < 10 s (`:168`) — i.e. a floor of roughly 20-33 rows/s |
| `memory/test_memory_baseline.py` | memory delta per 1000 rows, with transforms, scaling | < 200 MB (`:74,94`), < 500 MB (`:121`) |
| `memory/test_leak_detection.py` | growth across repeated runs and repeated Landscape recording | < 50 MB growth over 50 runs (`:84-85`); < 300 MB (`:127-128`) |
| `scalability/` (`test_deep_dag`, `test_large_datasets`, `test_many_sinks`, `test_wide_rows`) | 10/20/50-transform DAGs, 1k/10k rows, 10/50/100 sinks, 100/500/1000-field rows | correctness only, except 1000-field rows < 120 s (`test_wide_rows.py:101`) |
| `stress/test_call_index_concurrency.py` | 8 threads x 500 `allocate_call_index` calls | uniqueness, no timing |

Gaps: nothing measures commits or statements per row, `complete_node_state`,
`ingest_row_with_initial_claim`, the scheduler disposition verbs, `create_checkpoint`, the
payload store, or the sink-effect path; the `test_db_write.py` thresholds
(500-1000 ops/s on an in-memory DB) are loose enough that a doubling of per-write statements
would not trip them.

---

### Not verified / limits

- No code was run; all counts are from reading. Statement counts assume the non-contended,
  first-attempt, non-replay branch.
- `run_lifecycle_repository.py`, `scheduler/barrier.py`, `sink_effect_lifecycle.py`,
  `execution/batches.py` and the aggregation/coalesce verbs were skimmed, not counted.
- Which SQLAlchemy pool class and size the file-SQLite engine actually gets is taken from the
  in-code comment, not from an installed SQLAlchemy.
- PostgreSQL commit durability depends on server configuration I cannot see.
- The finalization-side output hash line for sink members and the statement count of
  `_insert_or_load_routing_decision` were not pinned down.

## Appendix C — Composer turn loop

Scope: request/turn control flow from HTTP arrival to a settled composer turn.
Method: **static reading only** (no venv in this checkout; nothing was executed, no timings measured).
Every hop below was opened and read unless marked UNVERIFIED. Paths are relative to `src/elspeth/web/` unless they start with `src/`.
Function sizes come from a stdlib `ast` walk (`end_lineno - lineno + 1`) over the in-scope files.
`docs/architecture/` and `ARCHITECTURE.md` were NOT consulted; only AGENTS.md § Composer invariants. Code is the authority for everything here.

---

### 0. Shape in one paragraph

`POST /api/sessions/{id}/messages` does **not** run the turn. It authenticates, admits a durable job row and returns **202** with an `operation_id`. A single in-process `ComposerAsyncWorker` (an asyncio task, not a thread) claims the job, takes a per-session `asyncio.Lock` plus a DB-fenced `SessionOperationLease`, and runs `run_composer_turn` as an asyncio task on the **same event loop** that serves HTTP. `ComposerServiceImpl.compose` then picks one of two authoring surfaces: the **planner** (`pipeline_planner.plan_pipeline`) when the graph is structurally empty, otherwise the **freeform compose loop** (`_compose_loop`). Both end in the same settlement (`settle_post_provider` → `complete_composer_async_operation`), which writes the assistant row, audit cohort and the terminal job result in one transaction. The client tries one SSE stream (status/progress only), then polls `GET /operations/{op}` every `poll_after_ms` (1000 ms) until the row is terminal; the result body rides on that GET.

---

### 1. End-to-end spine (one user message)

Frequency classes: **REQ** once per HTTP request · **TURN** once per turn · **ROUND** per LLM round (provider call) · **TOOL** per tool call · **MUT** per state mutation · **PROG** per progress event · **POLL** per client poll / watcher tick.

#### Phase A — HTTP / auth / admission (inline in the request)

| # | Hop | Freq | Notes |
|---|-----|------|-------|
| A1 | `app.py:1932-1963` middleware stack: `InstanceIdentityMiddleware` → `_BrowserDocumentHeadersMiddleware` → `_BodySizeLimitMiddleware` → `RequestIdMiddleware` → `CORSMiddleware` (LIFO, outermost first) | REQ | header-only work |
| A2 | `auth/middleware.py:80 get_current_user` → `auth_provider.authenticate(token)` | REQ | provider internals not opened |
| A3 | `auth/middleware.py:158 require_pipeline_user` → `run_sync_in_worker(authority.holds_active_human_role)` | REQ | 1 DB read on the shared thread pool |
| A4 | `sessions/routes/messages.py:47 send_message` | REQ | |
| A5 | `sessions/routes/composer/operations.py:111 parse_message_body` → `:101 _owned_json_body` (`json.loads` of body, then `_verify_session_ownership`) → `SendMessageRequest.model_validate_json` | REQ | body parsed twice (json.loads + pydantic) |
| A6 | `operations.py:127 admit` → `_verify_session_ownership` **again** (`_helpers.py:2971`, `service.get_session`) | REQ | second identical session read in the same request |
| A7 | `operations.py:137-141` `require_no_credential_material` + `compartment_ingress_record` on message text | REQ | |
| A8 | `operations.py:147` → `coordination/composer_operation_authority.py:847 admit_composer_operation` → `run_sync_in_worker(authority.admit, quota=rate_limiter.composer_admission, …)` | REQ | one transaction: SQL quota + idempotency (`request_hash`) + queue cap + one-active-per-session; internals out of scope |
| A9 | `operations.py:202` `composer_async_worker.notify_work()` then return `ComposerOperationAcceptedResponse` (202) | REQ | sets an `asyncio.Event` |

Refusals surface here: 429 quota, 429 `composer_queue_full`, 409 `composer_operation_active`, 409 conflict, 503 on `AsyncWorkerAdmissionTimeoutError`/SQL error (`operations.py:162-200`).

#### Phase B — claim, lock, lease (background worker)

| # | Hop | Freq | Notes |
|---|-----|------|-------|
| B1 | `sessions/composer_async_worker.py:326 _loop` — `reap_once()` + `_claim_once()` every wake or every `scan_interval` (1.0 s) | POLL (process-wide) | runs even when idle |
| B2 | `:334 _claim_once` → `run_sync_in_worker(authority.claim_next, limit=concurrency - len(jobs))` → `asyncio.create_task(self._job(claim))` | TURN | |
| B3 | `:462 _job` → `services.compose_locks.get_lock(session)` (`sessions/routes/_helpers.py:249`, process-local `asyncio.Lock` in a `WeakValueDictionary`) → `authority.get_for_turn` | TURN | |
| B4 | `:476-488` lock-wait loop: `_queued_outcome` (DB read) + `lock.acquire()` with 0.25 s timeout; claim renewed every `claim_lease/3` | POLL while queued behind a same-session holder | each spin is a DB read |
| B5 | `:489` `session_operation_authority.start_composer_async_operation` (start composite; E5/E7 ownership + head checks) | TURN | internals out of scope |
| B6 | `:505 SessionOperationLease.adopt` → `:563 _run_started` → `:731 _run_started_under_lease` | TURN | two more job-row reads (`get_for_turn`, `get_with_database_now`) at `:744-752` |
| B7 | `:778` `(SendMessageRequest|RecomposeRequest).model_validate_json(record.request_json)`; `:777` budget anchor from DB `deadline_at - now` | TURN | |
| B8 | `:799 composer_request_lifecycle` (`_helpers.py:2640`: `registry.start_request`, heartbeat task every 15 s) | TURN | |
| B9 | `:802 asyncio.create_task(self._watch(...))` | TURN | see D-watch below |
| B10 | `:813 asyncio.timeout(budget)` → `sessions/composer_turn.py:2022 run_composer_turn` → `:840 _run_composer_turn` | TURN | |

#### Phase C — turn start (state load, context assembly)

All in `sessions/composer_turn.py:_run_composer_turn` (1180 lines).

| # | Hop | Freq |
|---|-----|------|
| C1 | `:896 service.get_session` | TURN |
| C2 | `:902 service.load_graph_recovery_context` (head state row) → `:918-924 _state_from_record` | TURN |
| C3 | `:932 services.plugin_snapshot_for_user_id` | TURN |
| C4 | `:943-952 service.add_message_with_transcript(…)` — user row + ingress + **full transcript read** in one txn; `:968 records = list(ingress_result.transcript)` | TURN, O(session history) |
| C5 | `:1000-1001 compartment_ingress_record`, `_chat_ingress_inputs(records)` (`_helpers.py:1885`) | TURN, O(history) |
| C6 | `:1003 _advisory_progress_sink` → `_helpers.py:2815 composer_progress_sink_for_lease` → `registry.claim_request`; `:1012` first `_publish_progress("starting")` | TURN / PROG |
| C7 | `:1043 _composer_chat_history(records)` (`_helpers.py:1839`) | TURN, O(history) |
| C8 | `:1367-1376` retained-operation-history child task (`_read_retained_operation_context`, `:612`) | TURN, DB read |
| C9 | `:1417-1461` **auto-title** when first message + default title: `maybe_auto_title_session` run as a lease-owned task and **awaited to completion** (`_join_auto_title(..., wait_until_complete=True)`) *before* compose starts | once per session |
| C10 | `:1496-1502 AdvisorTranscriptHistory.from_records(records)` (`composer/advisor_transcript.py:126`, 273 lines) | TURN, O(history) |
| C11 | `:1486 composer.compose(...)` (`composer/service.py:1110`) | TURN |
| C12 | `service.py:1171-1179` `secret_service.list_refs(user_id)` + `require_no_credential_material_in_state(state)` | TURN, O(state) |
| C13 | `:1181 _chargeable_admission.require`; `:1182 composer_quota_scope` + `prompt_guard_user_scope` | TURN |
| C14 | `:1228 self._policy_context.build(user_id)` → `(plugin_snapshot, policy_catalog)` | TURN |
| C15 | `:1233-1236` branch: `_state_is_structurally_empty(state)` and session ids present → **planner** (`:1253 _planning_application._plan_and_stage_empty_pipeline`); else **compose loop** (`:1274 _compose_loop`) | TURN |

#### Phase D — freeform compose loop (`composer/service.py:2352 _compose_loop`, 836 lines)

Turn preamble (TURN): `:2452 _build_messages` → `composer/prompts.py:504 build_messages` (system prompt from instance cache `self._composer_skill_text`, `build_catalog_context_string`, history, `build_context_string(state)`, user message); `:2462 composer_loop_tool_definitions(dialect)` (`provider_gateway.py:391`); recovery + retained-operation system messages `:2463-2467`; `:2451 _maybe_upsert_skill_markdown_history`.

Per round (`while True` at `:2574`):

| # | Hop | Freq |
|---|-----|------|
| D1 | `service.py:1452 _call_model_turn` → `emit_progress(model_call_progress_event)` | ROUND / PROG |
| D2 | `:1423 _prepare_primary_delivery` — `deepcopy(messages)`, `deepcopy(tools)`, `transcript.observe_messages`, cache markers, `detach_prompt_guard_messages`, `turn_evidence.prepare_delivery` (`turn_effects.py:501`: `stable_hash` of every tool message + `stable_hash(messages)` + `stable_hash(tools)`) | ROUND, O(context) |
| D3 | `:3245 _call_llm_before_deadline` — retry loop; `:3243` mints PRIMARY custody; calls `provider_gateway._call_llm_with_audit` | ROUND (×≤3 attempts) |
| D4 | `provider_gateway.py:1124 _call_llm_with_audit` → `provider_quota.py provider_call_scope` → `:747 _call_llm_with_audit_body` — `detach_prompt_guard_evidence`, cache markers, **`capture_provider_context`** (`provider_context.py:267`: bounded copy + `canonical_json` of full messages+tools, retained on the call record) | ROUND, O(context) |
| D5 | `:599 _call_llm` → `:414 build_composer_loop_request_kwargs` → `:451 _litellm_acompletion` — `deepcopy(kwargs)` (messages+tools again), `detach_prompt_guard_evidence`, `require_no_credential_material(all messages)`, `enforce_prompt_guard` (`prompt_guard.py:413`: build request over all messages, external inspect, **audit row write** `:378`), `provider_custody.admit` (`provider_quota.py:222` → `service.begin_provider_attempt`, DB write) | ROUND, O(context) + 2 DB writes + 1 external HTTP |
| D6 | **`provider_gateway.py:569 litellm.acompletion(**kwargs)`** — the one physical provider call site | ROUND |
| D7 | `:640 _admit_composer_llm_completion` (`llm_response_parsing.py`), credential scans of tool wire + completion (`:644-656`), `asyncio.wait_for(…, timeout=remaining)` at `:812` | ROUND |
| D8 | finally `:895-957`: `build_llm_call_record` (`llm_response_parsing.py:706`; `_hash_and_measure_request` `:672` canonical-JSONs every message + tools again) → `recorder.record_llm_call`; scope exit → `custody.settle` (`provider_quota.py:290` → `service.finish_provider_attempt`, DB write) | ROUND, O(context) + 1 DB write |
| D9 | `service.py:1390 _enforce_tool_call_cap`; `:1511 turn_evidence.confirm_delivery` | ROUND |
| D10 | no tool calls → `composition_completion.py:1499 _try_terminate_no_tools` (Phase E) | once per no-tool reply |
| D11 | tool calls → `service.py:1586 _dispatch_tool_batch` — `get_composer_preferences` (DB read, `:1626`), builds `ToolBatchContext`, → `tool_batch.py:940 run_tool_batch` | ROUND |
| D12 | batch preamble `tool_batch.py:1010-1097`: credential scan of metadata + each tool wire, `_preflight_session_tool_call_ids` (`:346`), `emit_progress(tool_batch_progress_event)`, append assistant message to `llm_messages`, `transcript.observe_messages` | ROUND |
| D13 | per tool (`for tool_call in assistant_tool_calls`, `:1120`): arg decode/bounds, `canonical_json(arguments)` (`:1471`), `begin_dispatch_or_arg_error` (`composer/audit.py:733`), discovery-cache lookup (`:1588`), `redact_tool_call_arguments` (`:1837`), `emit_progress(tool_started)` (`:2260`) | TOOL |
| D14 | `:3082 dispatch_with_audit` (`audit.py:1069`) → `_do_dispatch` (`:2986`) → `run_sync_in_worker(execute_tool, …)` (`:3008`) | TOOL, 1 thread-pool hop |
| D15 | `tools/_dispatch.py:795 execute_tool` — credential scan, rebuild 6-registry handler union dict (`:917-924`), `_validate_tool_arguments` (JSON Schema), `ToolContext`, `handler(arguments, state, context)`, `_enforce_composition_interpretation_gate`, `finalize_tool_result` (`:752`: prior-validation inject, plugin-schema augment, `normalize_tool_result_validation`, guidance) | TOOL |
| D16 | mutation that makes the graph valid: `tool_batch.py:774 _finalize_completed_incremental_mutation` — `wire_required_controls_state` + `policy_catalog.validate_composition_state` (2 more thread-pool hops) | MUT |
| D17 | result projection: `_serialize_tool_result` / `serialize_admitted_discovery_result` (`:3344`), `emit_progress(tool_completed)` (`:3358`), `_append_tool_outcome`, discovery cache store (`:3368`), `llm_messages.append({"role":"tool",…})` (`:3372`) | TOOL |
| D18 | early advisor checkpoint `advisor_checkpoint.py:1531 _maybe_run_early_checkpoint` — fires only on the empty→non-empty transition | ≤ once per session |
| D19 | `service.py:1515 _persist_turn_audit` → `turn_audit.py:212 persist_turn_audit` — redact every call's args and result, `_state_payload_for_compose_turn` (`state.to_dict()` per version-advancing outcome), `sessions_service.persist_compose_turn_async` (1 txn) | ROUND (per-TOOL and per-MUT work inside) |
| D20 | `service.py:1697 _classify_and_budget_turn` (654 lines) — anti-anchor hint (extra audit row write `:1840`), cache-hit short-circuit, turn accounting, last-chance call | ROUND |

#### Phase D′ — planner surface (structurally empty graph)

`planning_application.py:1017 _plan_and_stage_empty_pipeline` → `:1089 _planner_preview_preflight` → `:1114 plan_pipeline` (`pipeline_planner.py:3204`) → `:3364 _plan_pipeline_inner` (1776 lines).

TURN preamble: `:3406 validate_composition_state`, `:3414 build_planner_authoring_aids` (memoised per snapshot hash, `planner_authoring_aids.py:2661`), `:3421` schema contract evidence, initial messages `:3498-3510`.

Per round (`while True` at `:4267`): `call_model` (`:4134`) → `call_model_body` (`:3570`, 546 lines) → cache markers, `detach_prompt_guard_evidence`, `json.loads(canonical_json({messages, tools}))` (`:3607`), `build_planner_capability_manifest`, `canonical_json(...)` again for the byte budget (`:3636`), `emit_progress`; per attempt (`:3739`): `build_planner_request_kwargs`, `observe_messages`, `capture_provider_context` (`:3762`), `prepare_delivery`, `_complete_planner_provider` (`:3175` → `_litellm_acompletion` `:3189`), `build_llm_call_record` (`:3917`). Discovery tool calls run through `execute_discovery_tool_with_context` on the thread pool (`:4906-4907`); a terminal candidate goes to `_build_valid_pipeline_plan` (`:4631`, def `:3048`).

Exit: `planning_application.py:1205 _persist_pipeline_planner_audit` → `:1290 _stage_pipeline_plan` (`:607`: `get_composer_preferences`, `cached_runtime_preflight`, `create_pipeline_composition_proposal_finish_once`) → `ComposerResult` carrying a `pipeline_commit_intent` or a staged proposal. A planner decline goes to `:893 _review_planner_decline` (advisor call at `:938`).

#### Phase E — loop termination

- No-tool reply: `composition_completion.py:1499 _try_terminate_no_tools` → source-proof repair (`:1652`), `_attempt_preflight_repair` (`:1680`, def `:1313`, uses `composer_preflight.py:299 turn_runtime_preflight`), `_surface_pt_and_gate_orphans_or_none` (`:1702`), `_evaluate_terminal_no_tool_advisor_gate` (`:1728`, def `:2191`, advisor call `:2315`), then `_surface_and_finalize_no_tools` / `_finalize_no_tool_response`. Returns `action="return"` with a `ComposerResult`, or `"continue"` with a repair/advisor injection appended to `llm_messages`.
- Budget exit: composition turns exhausted → one last-chance provider call (`service.py:2188`), else `ComposerConvergenceError` (`:2336`).
- Interpretation-review handoff: tools-off reply call (`service.py:2006`).
- Wall clock: `_call_llm_before_deadline` raises `ComposerConvergenceError(budget_exhausted="timeout")` (`:3301`, `:3331`); outer `asyncio.timeout(budget)` in the worker (`composer_async_worker.py:813`).
- Recovery control tools: `_terminal_recovery_result` (`service.py:1087`, checked `:2593`).

#### Phase F — completion / proposal / commit / persistence

`composer_turn.py:1511` records `observation.compose_result`, then runs `settle_post_provider` (`:1057`) as a lease-owned child task:

| # | Hop | Freq |
|---|-----|------|
| F1 | auto-commit: `:1146 settle_auto_commit_intent` (`sessions/routes/composer/pipeline_settlement.py:756` → `_settle_pipeline_proposal_under_compose_lock` `:284`, 376 lines; commit prep in `composer/pipeline_commit.py:429 prepare_pipeline_proposal_commit`) | TURN when planner/`set_pipeline` produced a commit intent |
| F2 | else if `result.state.version != state.version`: `_publish_progress("validating")` → `_state_data_from_composer_state` (`_helpers.py:2837`; authoring validation + runtime preflight — enters `web/execution/`, out of scope) → `_publish_progress("saving")` → `service.save_composition_state` (`:1256`) | TURN |
| F3 | else review-only projection: `composition_content_hash` twice (`:1276`), optional `AdvisorGraphReviewFact` | TURN |
| F4 | `:1321 _turn_audit_cohort_drafts` (`:687`) — per tool invocation and per LLM call: `private_diagnostic_drafts` (`composer/audit_storage.py:132`, includes the retained full provider-request canonical for every call) + public envelopes | TURN, O(rounds × context) bytes |
| F5 | `:1338 service.complete_composer_async_operation(running, assistant=…, audit_cohort=…, build_response=…)` — assistant row + cohort + proposals + terminal CAS + `result_json`, one transaction (internals out of scope) | TURN |
| F6 | `:1353 _publish_progress("complete")` | PROG |
| F7 | worker: `composer_async_worker.py:615 _verify_terminal_before_release` (read-back), lease close, lock release (`:558`), `_job_finished` wakes the loop | TURN |

#### Phase G — delivery to the client

- `GET /api/sessions/{id}/operations/{op}` (`operations.py:247`): auth (A2/A3) + `_verify_session_ownership` + `authority.get_with_database_now` (all job columns except `request_json`, with result digest check on terminal rows, `composer_operation_authority.py:116-119`) → `composer_operation_status_response` (`:80`; strict-parses `result_json` into `MessageWithStateResponse` on completed). POLL.
- `GET …/operations/{op}/stream` (`operations.py:295`): SSE (`composer_stream.py:69 BoundedComposerStreamResponse`, `text/event-stream`). The server generator `frames()` (`:349`) itself **polls the DB once per second** (`asyncio.sleep(1.0)` at `:421`) plus a progress-registry read, and emits status / progress / heartbeat (10 s) / terminal frames. It carries status and progress only, not the result body.
- Client (`frontend/src/api/composerOperationObserver.ts:137-180`): one stream attempt (`readStream`, 15 s idle / 120 s lifetime timers at `:194-195`), then `fetchComposerOperation` and `sleep(max(100, min(poll_after_ms, 60000)))` in a loop until terminal.
- Progress snapshot poll `GET /{id}/composer-progress` exists (`routes/composer/state.py:862`); its client cadence was not traced (UNVERIFIED).
- No WebSocket on the composer turn path (the websocket ticket store in `app.py` serves run progress; not traced further).

---

### 2. Frequency multipliers at a glance

With `R` = LLM rounds in a turn, `T` = tool calls in a round (cap 16), `D` = turn wall time in seconds:

| Cost | Count per turn |
|------|----------------|
| Physical provider calls (primary or planner) | `R` (+ retries, ≤3 attempts each) |
| Prompt-guard inspections (when a guard is mounted) | one per provider call of every family |
| DB writes tied to each provider call | 3: guard audit row, `begin_provider_attempt`, `finish_provider_attempt` |
| DB txn per tool round | 1 `persist_compose_turn_async` + 1 `get_composer_preferences` read |
| Thread-pool hops per tool call | 1 (`execute_tool`); +2 when a mutation completes a valid graph; +1–3 under `explicit_approve` |
| Progress events per round | 2 + 2·T (`model_call`, `tool_batch`, `tool_started`/`tool_completed` per tool); on PostgreSQL each is a thread-pool DB write (`coordination/composer_progress_authority.py:577-578`) |
| Watcher job-row reads | ≈ 4·D (0.25 s cadence, `composer_watch_reads.py:228`) |
| Request-lease heartbeats | D/15 |
| Client-driven reads | ≈ D (SSE server tick 1 s) or ≈ D (poll 1 s), each with auth role check + session read |
| Full-context serialisations per round | compose loop ≥ 4 (see §4); planner ≥ 5 |

---

### 3. Loop bounds, timeouts, budgets, retries

Settings live in `config.py`; three have **no default** (`Field(...)`) and must be configured.

| Bound | Constant / setting | Value | Where enforced |
|-------|--------------------|-------|----------------|
| Turn wall clock | `composer_timeout_seconds` | required; 180 (`deploy/linux-systemd/elspeth-web.env.example:20`), 300 (`deploy/compose/web-postgres.yaml:17`), 900 (`scripts/composer_acceptance/serve.py:166`) | job `deadline_at` at admission (`operations.py:157`); `asyncio.timeout(budget)` (`composer_async_worker.py:813`); cooperative deadline in loop (`service.py:1195`, `3299`) |
| Composition (mutating) rounds | `composer_max_composition_turns` | required; 15 in both shipped profiles | `service.py:2181` |
| Discovery rounds | `composer_max_discovery_turns` | required; 10 in both shipped profiles | **planner only** (`pipeline_planner.py:4858`). The freeform loop has no aggregate discovery cap; discovery rounds are bounded only by the deadline (`service.py:1741-1742`, `2344-2345`) |
| Tool calls per assistant message | `composer_max_tool_calls_per_turn` | 16 (`config.py:371`) | `service.py:1390` |
| Forced repair injections | `_MAX_REPAIR_TURNS` | 2 (`composition_completion.py:167`) | `:1076`, `:1378` |
| Provider retry (primary + planner) | `LLM_API_MAX_ATTEMPTS`, `LLM_API_RETRY_BASE_DELAY_SECONDS` | 3 attempts, 1.0 s base, doubling (1 s, 2 s) (`provider_config.py:6-7`) | `service.py:3364-3385` (only `retryable` failures; never 400s; never past the deadline); planner `pipeline_planner.py:3739`, `3886-3889` |
| SDK-level retries | `num_retries=0`, `max_retries=0` | disabled | `provider_gateway.py:436-437` |
| Planner physical calls | `composer_planner_max_provider_calls` | 75 (`config.py:377`) | `pipeline_planner.py:3740` |
| Planner request size | `composer_planner_max_request_bytes` | 2 MiB | `:3637` |
| Planner completion tokens | `composer_planner_max_completion_tokens` | 16 384 | request kwarg |
| Planner cumulative cost | `composer_planner_max_cumulative_provider_cost` | 5.00 (post-call continuation cap, not pre-spend) | `:4028` |
| Planner repair budget | `composer_planner_repair_budget` | 2 | `:4338`, `4439`, `4574`, `4656`, `4787` |
| Advisor hint tool | `composer_advisor_max_calls_per_compose` | 4 | `tool_batch.py` advisor branch (`:2276-2723`) |
| Advisor END gate passes | `composer_advisor_checkpoint_max_passes` | 2 | `composition_completion.py:2305` |
| Advisor call timeout / tokens | `composer_advisor_timeout_seconds`, `…_max_prompt_tokens`, `…_max_completion_tokens` | 60 s / 4000 / 8192 | `tool_batch.py:2430-2431`; token-limit enforcement UNVERIFIED |
| Runtime preflight | `composer_runtime_preflight_timeout_seconds` | 5.0 s | `composer_preflight.py:102`, `245` |
| Auto-title | `_AUTO_TITLE_PROVIDER_TIMEOUT_SECONDS`, `_AUTO_TITLE_MAX_TOKENS` | 5.0 s, 40 | `sessions/_auto_title.py:80-82` |
| Inline (held-request) composer work | `composer_sync_timeout_seconds` = min(timeout, idle ceiling 300 − headroom 30) | derived | proposal accept (`routes/composer/proposals.py`) |
| Admission rate | `composer_rate_limit_per_minute` | required; 10 in shipped profiles | SQL quota inside `authority.admit` |
| Queue depth | `composer_async_max_queued_operations` | 64 | `operations.py:158` |
| Worker concurrency | `composer_async_worker_concurrency` | 4 (max 16) | `composer_async_worker.py:335` |
| Claim lease / scan / poll / drain | `composer_async_claim_lease_seconds` / `…scan_interval…` / `…poll_after_ms` / `…drain_seconds` | 30 s / 1.0 s / 1000 ms / 10 s | `config.py:403-406` |
| Session operation lease | `session_operation_lease_seconds` | 30 (`sessions/service.py:818`) | renewal cadence not traced |
| Request (progress) lease / heartbeat | `_COMPOSER_REQUEST_LEASE_SECONDS`, `_COMPOSER_HEARTBEAT_SECONDS` | 60 s / 15 s (`_helpers.py:2527-2529`) | |
| SSE permits | hard-coded | 2 per operation, 4 per principal, 8 total (`composer_stream.py:47`) | 503 beyond |
| Shared sync pool | `MAX_WORKERS`, `MAX_QUEUED`, `ADMISSION_WAIT_SECONDS` | 16 / 16 / 1.0 s (`async_workers.py:36-40`) | `AsyncWorkerAdmissionTimeoutError` |

`config.py:1311-1320` logs `composer_turn_budget_underfunded` when `(composition + discovery turns) × 15 s` exceeds the timeout: the two shipped profiles configure 25 turns against 180 s (funds 12) and 300 s (funds 20).

---

### 4. What is rebuilt on every LLM round

#### Built once per turn (not per round) in the freeform loop

- System prompt: `self._composer_skill_text`, rendered at service construction (`service.py:3232`); `prompts.py:103` `build_system_prompt` is also `lru_cache(8)`.
- Catalog context message and state context message: `build_messages` runs once (`service.py:2452`). The state snapshot in the prompt is **not** refreshed between rounds; the model sees mutations only through tool results.
- Tool definitions: `composer_loop_tool_definitions` once (`service.py:2462`); `wire_tool_definitions` thaws a fresh list of dicts per turn (`tools/wire_projection.py:852`).
- `llm_messages` is one list appended in place across rounds.
- No tokenizer call was found on the spine: requests are sized in bytes (`_hash_and_measure_request`), token counts come from provider usage.

#### Redone every round, each O(full context)

Freeform loop, one provider call:

1. `deepcopy(messages)` + `deepcopy(tools)` — `service.py:1434`.
2. `turn_evidence.transcript.observe_messages(messages)` — `service.py:1437` (and again at `tool_batch.py:1097`, `service.py:2825`, `advisor_checkpoint.py:1554`).
3. `apply_anthropic_cache_markers` — twice (`service.py:1440`, `provider_gateway.py:762`) on Anthropic-marker models.
4. `detach_prompt_guard_*` — three times (`service.py:1445`, `provider_gateway.py:758`, `:482`).
5. `TurnEvidence.prepare_delivery` — `stable_hash(messages)`, `stable_hash(tools)`, and `stable_hash(content)` of every prior tool message (`turn_effects.py:516-550`).
6. `capture_provider_context` — bounded copy + `canonical_json` of messages+tools, **retained** on the call record (`provider_gateway.py:765`, `provider_context.py:267-292`).
7. `deepcopy(kwargs)` — messages + tools again (`provider_gateway.py:480`).
8. `require_no_credential_material(credential_messages)` over the whole request (`provider_gateway.py:530`).
9. `enforce_prompt_guard` → `_request(...)` over all messages/tools, then the external inspection (`prompt_guard.py:460-480`). `AzurePromptGuardClient.inspect` opens a **new `httpx.AsyncClient` per inspection** and POSTs document batches (`prompt_guard_clients.py:136-150`).
10. `_hash_and_measure_request` — `canonical_json` of every message and of the tool list (`llm_response_parsing.py:687-697`).

Planner, one round: cache markers, `detach_prompt_guard_evidence`, `json.loads(canonical_json({messages, tools}))` (`pipeline_planner.py:3607`), `build_planner_capability_manifest` (calls `canonical_set_pipeline_schema()` twice, each a fresh decode — `tools/schema_contract.py:426-434`), `canonical_json` for the byte budget (`:3636`), `observe_messages`, `capture_provider_context` (`:3762`), `prepare_delivery`, then items 7–10 above.

#### Superlinear terms over a turn

Context grows roughly linearly with rounds, so every O(context)-per-round item above is **O(R²) over a turn**. Specific ones worth naming:

- **Retained provider context**: each `ComposerLLMCall.private_request` holds the canonical JSON of the whole request for that round. At settlement `_turn_audit_cohort_drafts` turns each into a `composer_provider_context` diagnostic draft (`audit_storage.py:147-166`) and they are written in the terminal transaction. Memory and bytes persisted are O(R × context) — quadratic in rounds, including one copy of the system prompt, catalog message and tool schemas per round. Each capture is individually bounded (`JSON_MAX_TOTAL_UTF8_BYTES`; over-bound captures degrade to `disposition="unavailable"`).
- **`observe_messages`** (`advisor_transcript.py:676-701`): for each message, a linear identity scan of `_observed` (`next(entry for entry in self._observed if entry.message is message)`) plus a scan of `_excluded`. O(M²) per call, called ≥3 times per tool round → O(M³) identity compares over a turn. Cheap constants, but the only cubic term found.
- **Prompt-guard payload**: every round re-submits all untrusted history to the guard service.
- **`prepare_delivery`** rehashes every earlier tool result each round.
- **`BufferingRecorder.invocations` / `.llm_calls`** return a fresh `tuple(...)` on every access (`composer/audit.py:383-393`); `tool_batch.py:3401-3403` filters the request-cumulative invocations with a set comprehension that is **rebuilt for every element** (`… if invocation.tool_call_id in {outcome.call.id for outcome in tool_outcomes}`), i.e. O(cumulative invocations × batch size) per round.
- `turn_audit.py:347-354` grows `redacted_assistant_tool_calls` by tuple re-concatenation and `visible_event_for` does linear `next(...)` scans — O(T²) per batch, T ≤ 16.

#### O(session history) once per turn

Full transcript load in the ingress transaction (`composer_turn.py:943-968`), then three passes over it: `_chat_ingress_inputs`, `_composer_chat_history`, `AdvisorTranscriptHistory.from_records`. Prior-turn history is replayed into every provider call of the turn (prompt cache markers aside), so long sessions raise the per-round constant.

#### O(state) per mutation

`_state_payload_for_compose_turn` → `state.to_dict()` per version-advancing tool outcome (`turn_audit.py:125`, gated at `:411-413`); `policy_catalog.validate_composition_state` after each mutation inside `execute_tool` / `finalize_tool_result`, and again in `_finalize_completed_incremental_mutation`; `require_no_credential_material_for_tool(..., credential_state=state)` per tool call (`_dispatch.py:913`); `composition_content_hash(state)` at proposal creation (`tool_batch.py:2137`) and at settlement (`composer_turn.py:1276`). Internals of `state.py` / validation are the other agent's.

---

### 5. Provider-call accounting per transition

**One physical provider call site**: `provider_gateway.py:569 litellm.acompletion`, reached only through `_litellm_acompletion` (`:451`), which refuses to run without an exact `ProviderCallCustody` (`:473-474`).

How counted: `grep` for `_litellm_acompletion(`, `litellm.acompletion`, `.acompletion(` over `src/elspeth/web` and `src/elspeth/composer_mcp`. Positive control: it matched the definition and all sites below. Negative control: zero hits in `composer_mcp`. Not a registry-derived count.

Six call sites enter `_litellm_acompletion`:

| Caller | Family | Fires |
|--------|--------|-------|
| `provider_gateway.py:623` (`_call_llm`, tools) | PRIMARY | freeform loop: **per round** (`service.py:1487`); last-chance call once (`:2188`); recovery turn (`:1057`) |
| `provider_gateway.py:680` (`_call_text_llm`, prose) | PRIMARY | run-diagnostics explanation (`service.py:1005`, off the turn spine); tools-off handoff reply goes through `_call_llm_with_audit` at `service.py:2006` |
| `pipeline_planner.py:3189` (`_complete_planner_provider`) | PLANNER | planner: **per round**, ≤75 physical calls |
| `pipeline_planner.py:3789` | PLANNER (alternate branch of the same `wait_for`) | same round; which branch applies was not traced (UNVERIFIED) |
| `advisor_checkpoint.py:525` (`_call_advisor_with_audit_body`) | ADVISOR | see below |
| `sessions/_auto_title.py:477` | TITLE | first message of a default-titled session only |

Per-transition picture:

| Transition | Primary/planner calls | Other provider calls |
|------------|-----------------------|----------------------|
| First message, empty graph (rootless) | planner: 1 per round until a valid candidate, decline or budget | +1 TITLE call, serial, before the planner starts (`composer_turn.py:1455-1461`); advisor only on decline (`planning_application.py:938`) or escape hatch (planner call with `model_override = composer_advisor_model`, `planning_application.py:1152`) |
| Later message, non-empty graph | freeform: 1 per round; minimum 1 (a no-tool reply) | advisor END gate ≤2 passes (`composition_completion.py:2315`); early checkpoint ≤1 per session (`advisor_checkpoint.py:1531-1553`); `request_advisor_hint` tool ≤4 (`tool_batch.py:2458`); ≤2 repair rounds each add a primary call |
| Recompose | same as the surface the graph selects | no title call (`should_auto_title` requires `SendMessageRequest`, `composer_turn.py:1049-1054`) |
| Malformed head | `compose_recovery`: 1 call (`service.py:1057`) | — |
| Proposal accept / reject, YAML import, revert | **0** | — |

Every provider call of every family also triggers one prompt-guard inspection (external HTTP when a guard is mounted) and three DB writes (§2). Exact skip conditions for the END advisor gate (already-blocked graph, genuine orphans, stalled repair) were read only partially — treat "≤2 per turn" as the bound, not the typical count.

Invariant check, read-only: no code path was found on the spine that returns pipeline structure without a provider call. `compose` routes by graph emptiness alone (`service.py:1233-1236`), the tutorial has no branch in `service.py`, `composer_turn.py`, `pipeline_planner.py`, `planning_application.py`, `tool_batch.py`, `provider_gateway.py`, `operations.py` or `composer_async_worker.py` (`grep -ci tutorial` = 0 in each; one comment hit in `composition_completion.py:1848`). The server-side `wire_required_controls_state` splice (`tool_batch.py:774`, `2093`) edits a provider-authored graph after validation; whether that sits inside "required-control admission" is a policy question, not something this recon rules on.

---

### 6. Cost centres wrapping each tool call

In execution order, for one tool call in the freeform loop (`tool_batch.py:1120-3390`):

1. Credential scan of the raw wire arguments (`:1030-1036`, batch preamble) and again inside `execute_tool` (`_dispatch.py:913`).
2. Bounded JSON decode + `canonical_json(arguments)` for private authoring evidence (`:1471-1475`); `recorder.record_private_tool_authoring` (`:1482`).
3. `begin_dispatch_or_arg_error` — audit record open with arguments hash (`:1497`, `audit.py:733`).
4. Discovery cache lookup keyed by `(tool, canonical args)` (`:1588`); a hit skips dispatch and does not charge a turn.
5. Redaction: `redact_tool_call_arguments` before dispatch (`:1837`), then again in P4 for the persisted row (`turn_audit.py:322`), plus `_serialize_response_via_walker` on the result (`turn_audit.py:398`). Internals out of scope.
6. `set_pipeline` only: `_finalize_complete_set_pipeline_candidate` (`:699`, called `:1775`/`:1882`) — pre-validation + required-control splice before custody.
7. `explicit_approve` trust mode only: the mutation is executed once as a **preview** (`run_sync_in_worker(execute_tool, …)` `:2059`) to build the proposal, +`wire_required_controls_state` and re-validation (`:2093`, `:2100`); the real execution happens later on accept.
8. Progress emit `tool_started` (`:2260`).
9. `preview_pipeline` only: `cached_runtime_preflight` (`:2845`, enters `web/execution/`; 5 s timeout; cached per compose call by state version+content hash).
10. `dispatch_with_audit` → thread-pool hop → `execute_tool` (JSON-Schema argument validation, handler, interpretation gate, `finalize_tool_result` with validation normalisation).
11. Mutation completing a valid graph: required-control wiring + validation (2 thread-pool hops).
12. Audit close (`finish_success` / `finish_arg_error` / `finish_plugin_crash` → `recorder.record`), anti-anchor tracking (`:3336-3343`).
13. Result serialisation to the provider message (`:3344`), blob-page registration for read tools (`:3351`), progress emit `tool_completed` (`:3358`).
14. After the batch: `persist_turn_audit` — one transaction for the assistant row, all tool rows, private evidence, visible events and, per mutation, a full state payload.

There is no state deep-copy per tool call: `CompositionState` is immutable and handlers return a new instance (stated at `service.py:2497-2500`; internals not opened).

---

### 7. Concurrency model

- **One asyncio event loop** runs HTTP handlers, the worker loop, every turn task, watchers, heartbeats and SSE generators. Turns are tasks, not threads (`composer_async_worker.py:345`).
- **CPU work on the loop**: `deepcopy`, `canonical_json`, `stable_hash`, credential scans, redaction in `persist_turn_audit`, `build_messages`, `_turn_audit_cohort_drafts`, pydantic parsing of results. None of these is offloaded, so a long turn's per-round serialisation stalls every other request on the process. (Static inference; not measured.)
- **Sync work off the loop**: one shared `ThreadPoolExecutor`, 16 workers + 16 queued admissions, 1 s admission wait (`async_workers.py:36-40`, `:634`). It serves all session-DB access, `execute_tool`, validation, runtime preflight, the auth role check on every request, progress writes on PostgreSQL, the 4 Hz watcher read per active turn and the 1 Hz SSE read per stream. `config.py:400` notes "Durable jobs share the bounded 16-thread synchronous worker pool". Tool handlers are Python, so pool threads still contend on the GIL.
- **Worker pool**: `composer_async_worker_concurrency` = 4 concurrent turns per process (`_claim_once`, `:335`). Other replicas claim from the same job table (`claim_next` with claim leases; cross-process semantics are in `web/coordination/`, out of scope).
- **Per-session serialisation**, three layers: (1) admission refuses a second non-terminal operation per session (409); (2) process-local `asyncio.Lock` per session (`_helpers.py:227-259`), shared with proposal accept/reject, YAML import and other state routes; (3) DB-fenced `SessionOperationLease` (30 s, renewed) for cross-process exclusion. `sessions/locking.py` (flock / `pg_advisory_xact_lock`, `:260-299`) sits under the session service's transactions; only its entry points were noted.
- **DB session handling**: SQLAlchemy Core connections opened per call inside thread-pool functions (`with self._engine.connect()`, e.g. `composer_operation_authority.py:433`). No connection is held across a provider call on the paths read. Pool sizing lives in `sessions/engine.py` (out of scope).
- **Where requests queue**: SQL rate limit → queue-depth cap (64) → worker concurrency (4) → same-session lock (0.25 s spin with a DB read each spin) → thread-pool admission (32 slots, 1 s wait) → provider latency.
- **Cancellation**: cooperative. Tool execution and P4 persistence are shielded (`_await_tool_turn_with_deferred_cancellation`, `service.py:187`); provider calls are cancellable under `asyncio.wait_for`. The watcher cancels the owner task on user cancel, deadline, lease loss or shutdown (`composer_async_worker.py:884-960`).
- **Background per-turn tasks**: watcher (0.25 s), loss-watch child, request-lease heartbeat (15 s), session-lease renewal.

---

### 8. Ranking

#### Top 10 hottest (frequency × body weight; static judgment, not profiled)

| # | Function | Why |
|---|----------|-----|
| 1 | `composer/provider_gateway.py:451 _litellm_acompletion` | Every provider call of every family. Deep-copies the full request, scans all messages for credentials, runs the prompt guard (external HTTP + audit write) and quota admission (DB write) before the SDK call. |
| 2 | `composer/provider_gateway.py:747 ProviderGateway._call_llm_with_audit_body` | Once per freeform round. Detach + cache markers + `capture_provider_context` (copy and canonical JSON of the whole context, retained) + `build_llm_call_record` hash; its retained output drives the O(R²) persistence. |
| 3 | `composer/tool_batch.py:940 run_tool_batch` | Once per tool round with a per-tool loop body; 2478 lines; owns redaction, audit open/close, cache, proposals, advisor interception, 2+2T progress emits and all thread-pool hops. |
| 4 | `composer/tools/_dispatch.py:795 execute_tool` (+ `finalize_tool_result` `:752`) | Once per tool call (twice for explicit-approve mutations). Schema validation, handler, state validation; rebuilds the six-registry handler dict on every call. |
| 5 | `composer/turn_audit.py:212 persist_turn_audit` | Once per tool round. Redacts every argument set and result, serialises full state per mutation, one fenced transaction. |
| 6 | `composer/pipeline_planner.py:3570 call_model_body` (inside `_plan_pipeline_inner`) | Once per planner round, up to 75. At least five whole-context canonical serialisations before the SDK call. |
| 7 | `composer/turn_effects.py:501 TurnEvidence.prepare_delivery` + `composer/llm_response_parsing.py:672 _hash_and_measure_request` + `composer/provider_context.py:267 capture_provider_context` | Each once per round on both surfaces, each a full canonical-JSON pass; together most of the per-round CPU that is not the provider. |
| 8 | `composer/advisor_transcript.py:676 observe_messages` | ≥3 calls per tool round, each O(M²) identity scans over the growing message list. |
| 9 | `composer/progress.py:489 emit_progress` → registry publish (`coordination/composer_progress_authority.py:577`) | 2 + 2T per round; a thread-pool DB write per event on PostgreSQL. |
| 10 | `sessions/composer_async_worker.py:884 _watch` → `composer_watch_reads.py read` | 4 job-row reads per second per active turn for the whole turn, through the shared pool; with the SSE tick and client poll, ~6 reads/s per active turn that do no authoring work. |

Just outside: `service.py:1697 _classify_and_budget_turn` (per tool round, mostly branching), `sessions/composer_turn.py:840 _run_composer_turn` (once per turn but loads and walks the full transcript three times), `prompt_guard.py:413 enforce_prompt_guard` (counted under #1).

#### Largest functions on the spine (lines, by `ast`)

| Lines | Function |
|------:|----------|
| 2478 | `composer/tool_batch.py:940 run_tool_batch` |
| 1776 | `composer/pipeline_planner.py:3364 _plan_pipeline_inner` |
| 1180 | `sessions/composer_turn.py:840 _run_composer_turn` |
| 836 | `composer/service.py:2352 _compose_loop` |
| 654 | `composer/service.py:1697 _classify_and_budget_turn` |
| 546 | `composer/pipeline_planner.py:3570 call_model_body` (nested) |
| 433 | `composer/composition_completion.py:2191 _evaluate_terminal_no_tool_advisor_gate` |
| 428 | `composer/turn_audit.py:212 persist_turn_audit` |
| 428 | `sessions/routes/composer/proposals.py:290 accept_composition_proposal` |
| 397 | `composer/composition_completion.py:1499 _try_terminate_no_tools` |
| 376 | `sessions/routes/composer/pipeline_settlement.py:284 _settle_pipeline_proposal_under_compose_lock` |
| 318 | `composer/advisor_checkpoint.py:1212 _run_advisor_checkpoint` |
| 292 | `composer/planning_application.py:1017 _plan_and_stage_empty_pipeline` |
| 285 | `composer/planning_application.py:607 _stage_pipeline_plan` |
| 279 | `composer/service.py:1110 compose` |
| 273 | `composer/advisor_transcript.py:126 AdvisorTranscriptHistory.from_records` |
| 268 | `composer/advisor_checkpoint.py:407 _call_advisor_with_audit_body` |
| 216 | `composer/provider_gateway.py:747 _call_llm_with_audit_body` |
| 216 | `composer/audit.py:1069 dispatch_with_audit` |
| 202 | `composer/tools/_dispatch.py:795 execute_tool` |

(`app.py:1671 _create_app`, 1299 lines, is startup only.)

---

### 9. Entry-path variants

| Variant | Diverges at | Rejoins at |
|---------|-------------|------------|
| **Rootless / first message** | `composer_turn.py:1049` (`should_auto_title`) adds a serial TITLE provider call; `service.py:1233` routes the empty graph to the planner instead of the freeform loop | `composer_turn.py:1511` `settle_post_provider` with a `ComposerResult` (commit intent → F1, or staged proposal) |
| **Tutorial** | Nowhere on the compose spine. `composer/tutorial_service.py` and `tutorial_run_routes.py` cover sample, readiness, **run**, cancel and orphan cleanup of the tutorial pipeline, not authoring. A tutorial session's messages go through `POST /messages` like any other; as a new session it takes the rootless path above | — |
| **Recompose** (`POST /{id}/recompose`, `routes/composer/compose.py:16`) | Same `admit` with kind `compose_recompose`; `composer_turn.py:971-999` re-reads the transcript and proposals instead of inserting a user row; no auto-title | `composer_turn.py:1000` |
| **Malformed head** | `composer_turn.py:1474` → `compose_recovery` (`service.py:1028`), one recovery-tools-only call | `settle_post_provider` recovery branch (`:1058-1113`) |
| **YAML import** (`POST /{id}/state/yaml`, `routes/composer/state.py:1217`) | Inline in the HTTP request under the same per-session compose lock; no job, no worker, no provider call | Shares only the lock and state persistence (`save_composition_state_with_interpretations`) |
| **Proposal accept / reject** (`routes/composer/proposals.py:286`, `:720`) | Inline in the request: compose lock + own `SessionOperationLease`; accept runs `execute_tool` on the thread pool (`:492-493`) or `settle_pipeline_proposal_under_compose_lock` for pipeline proposals, bounded by `composer_sync_timeout_seconds`; no provider call | Shares `execute_tool` and `pipeline_settlement` with the turn |
| **composer_mcp** (`src/elspeth/composer_mcp/server.py`) | Separate stdio MCP server. Shares `execute_tool`, `get_tool_definitions`, `_validate_tool_arguments`, `yaml_generator`, `validate_pipeline` and `run_sync_in_worker` (imports `:47-78`, dispatch `:402`). The external agent is the LLM | Does **not** share the turn trunk: no `ComposerServiceImpl`, provider gateway, planner, async worker, session DB or redaction walker; audit is a JSONL recorder (`composer_mcp/audit.py`) |

---

### 10. Existing perf / latency evidence and instrumentation in-tree

Instrumentation (OpenTelemetry meters, `composer/provider_telemetry.py:26-45`):

- `composer.llm_call.total`, `composer.llm_call.duration` — per audited provider call, by surface and status.
- `composer.request.duration`, `composer.request.provider_calls` — per composer request; started by `begin_composer_request_metrics` (`:85`) inside `composer_request_lifecycle`.
- `_COMPOSER_REQUESTS_INFLIGHT` up/down counter and `_COMPOSER_REQUEST_TERMINAL_COUNTER` (`_helpers.py:1537`, `1542`); runtime-preflight and authoring-validation counters (`:1403`, `:1408`).
- `composer.tool_call_cap_exceeded_total` and audit counters (`sessions/telemetry.py:292-303`), advisor checkpoint counters (`composer/advisor_checkpoint_telemetry.py:35`, `130`), auto-title counters (`sessions/_auto_title.py:90-92`), pipeline proposal counters (`sessions/service.py:456-458`).
- Per-call audit rows carry `latency_ms`, token usage, cache read/creation tokens, provider cost and `request_byte_split` (per-message and tools bytes) (`llm_response_parsing.py:780-828`) — the durable source for a per-round cost breakdown.
- `slog` events: `composer_authoring_surface_selected` (`service.py:1236`), `composer_turn_budget_underfunded` (`config.py:1314`).

No timer around tool execution, redaction, persistence or the per-round serialisation work was found; the only durations recorded are the provider call and the whole request.

Measured numbers quoted in code comments (not re-verified here): ~20 s per turn on the acceptance burst and single thinking-heavy calls over 120 s (`config.py:1306-1308`); a 4-plugin pipeline that took 13 tool calls and 18 LLM rounds (`tools/_dispatch.py:696-697`); "98.4%" catalog share of the old combined context message (`prompts.py:220`).

Tests and harnesses:

- `tests/unit/web/composer/test_provider_telemetry.py:179` — request projection records duration and settled provider-call count.
- Provider-call accounting / custody: `tests/unit/web/composer/test_provider_quota.py`, `test_provider_quota_end_to_end.py`, `test_explicit_provider_custody.py`, `test_quota_scope_failure_identity.py`.
- Planner call budgets: `tests/unit/web/composer/test_pipeline_planner.py`, `tests/integration/web/composer/test_freeform_pipeline_planner.py`.
- Cache-prefix stability: `tests/unit/web/composer/test_advisor_cache_prefix.py`.
- Invariant guard referencing both prior bypass incidents: `tests/unit/web/composer/test_advisor_checkpoint.py:466`; the per-transition ledger for the e2e harness is `src/elspeth/web/frontend/tests/e2e/harness/transition-ledger.ts` (`:6`, `:260`).
- Worker: `tests/unit/web/sessions/test_composer_async_worker.py`.
- `tests/performance/` (benchmarks, memory, scalability, stress) has **no file mentioning the composer** (`grep -rl composer tests/performance` returned nothing; the same grep form matched under `tests/unit`).
- `evals/composer-battery/` (`drive_battery.py`, `planner_probe.py`, `report.py`, scenarios, calibration), `evals/composer-standard-battery/`, `evals/composer-parity/live_acceptance.py` — live-provider batteries; `evals/composer-battery/README.md:81,120` discusses a 600 s job timeout. Contents were listed, not read.

---

### 11. Not verified

- Internals of `web/sessions/service.py`, `engine.py`, `schema.py`, `web/coordination/`, `web/execution/`, `state.py`, `redaction.py`, `yaml_generator.py` (out of scope; only call sites named).
- `_preflight_session_tool_call_ids` (`tool_batch.py:346`) — assumed to be a DB read from its signature; body not opened.
- Which condition selects `pipeline_planner.py:3789` over `:3773`.
- Exact skip conditions of the END advisor gate, and advisor prompt-token cap enforcement.
- Session-lease renewal cadence; connection-pool sizing; cross-replica claim behaviour.
- Number and byte size of LLM-visible tool definitions (needs an import of the registry; no venv).
- Client cadence for `GET /composer-progress` and `GET /messages`.
- All timings. Nothing here was run.

## Appendix D — Composer data trunk: per-tool-call and per-turn cost map

Recon at HEAD `1a7ff0e6`. **Static analysis only**: there is no Python venv in this
checkout, so nothing here is a measured timing. Every claim cites a line I read. Items I did
not read end-to-end are marked **UNVERIFIED**. Counts state how they were obtained.
Paths are relative to `src/elspeth/web/` unless they start with `src/` or `tests/`.

Notation: **N** = nodes, **E** = edges, **S** = sources, **O** = outputs, **P** = size of one
tool call's argument payload, **R** = size of one serialized `ToolResult`, **k** = tool calls
in one LLM round (batch), **m** = of those, calls that advanced the state version.
**W1** = one Stage-1 whole-graph walk (`CompositionState.validate()`).

---

### 0. Headline findings

1. **Stage-1 validation is whole-graph, never local, and runs several times per tool call.**
   `CompositionState.validate()` (`composer/state.py:8443`) re-walks the full state and
   constructs one real transform plugin instance per plugin node per walk (pydantic parse of
   the node's options). A successful `upsert_node` pays **3 W1 inside `execute_tool`**; one of
   them is computed and discarded by design.
2. **`interpretation_sites(state)` hides 1–3 further W1 per eligible source.** It is called
   after every successful valid mutation and again per version-advancing tool row at persist.
3. **A successful, valid mutation on a state with pending interpretation reviews also runs
   full Stage-2** — YAML generation, settings load, instantiation of every runtime plugin, a
   real `ExecutionGraph` build and `graph.validate()`. The per-turn cache is keyed by state
   version + content hash, so each new version misses.
4. **A discovery-cache hit still pays 2 W1.** The cache stores only the tool's `data`.
5. **`ToolResult.to_dict()` is rebuilt 3 times per tool call** (audit record, LLM tool message,
   persist-time redaction walker) and a 4th for failed mutations.
6. **Persistence is full snapshots, not deltas.** Each version-advancing tool call writes a
   complete `composition_states` row; turn end writes one more `post_compose` snapshot.
   One transaction per LLM round, but roughly 20+ SQL statements per mutating tool call,
   dominated by repeated fence/authority proofs.
7. `validation_memo.py` does **not** serve the composer loop. It memoises
   `ExecutionServiceImpl.validate_state` for `POST /validate` and `GET /audit-readiness`.

---

### 1. Per-tool-call cost stack

#### 1a. One mutating call (`upsert_node`, success path, web principal)

Pre-dispatch, in `composer/tool_batch.py::run_tool_batch` (loop at `:1120`):

| # | Site | Work | Cost |
|---|------|------|------|
| 1 | `tool_batch.py:1238` `bounded_json_loads` | decode raw arguments | O(P) |
| 2 | `tool_batch.py:1323` `canonical_json(decoded_arguments)` | private receipt canonicalisation | O(P) |
| 3 | `tool_batch.py:1405` `decode_wire_arguments` (`tools/wire_projection.py:930`) | wire→semantic decode; validators precompiled at import (`wire_projection.py:885`) | O(P) |
| 4 | `tool_batch.py:1465` `deep_thaw(decoded.semantic)` | copy of args | O(P) |
| 5 | `tool_batch.py:1471` `canonical_json(arguments)` | 2nd canonicalisation, semantic form | O(P) |
| 6 | `tool_batch.py:1497` → `composer/audit.py:713` `canonical_json_and_hash(arguments)` | 3rd canonicalisation + SHA; a 4th over the authority projection for `set_pipeline` (`audit.py:715`) | O(P) |
| 7 | `tool_batch.py:1587` `_make_cache_key` (`discovery_cache.py:121`) | discovery tools only: `json.dumps(sort_keys=True)` | O(P) |
| 8 | `tool_batch.py:1679-1680` `_find_missing_required_paths` | required-path walk | O(P) |

Dispatch, `tools/_dispatch.py::execute_tool` (`:795`), run in a worker thread
(`tool_batch.py:3008`):

| # | Site | Work | Cost |
|---|------|------|------|
| 9 | `_dispatch.py:908` `require_no_credential_material_for_tool` (`credential_guard.py:306`) | credential detector over argument projection. For `patch_node_options` on a `field_mapper` node it also calls `credential_state.to_dict()` (`credential_guard.py:245`) | O(P); O(N) in that one case |
| 10 | `_dispatch.py:912-919` | rebuilds `all_handlers` by merging 6 registry dicts on every call | O(#tools), about 40 |
| 11 | `_dispatch.py:923` | `prior_validation or catalog.validate_composition_state(state)`; a W1 only when the caller passed none (`tool_batch.py:3023` threads `last_validation`) | 0 or 1 W1 |
| 12 | `_dispatch.py:926` → `:625` `_schema_errors` | `Draft202012Validator(_closed_root_schema(tool))`; `_closed_root_schema` deep-thaws the schema (`:434`). The validator is **constructed per call**, not cached | O(schema)+O(P) |
| 13 | `_dispatch.py:952-979` `ToolContext(...)` | per-call dataclass | O(1) |
| 14 | `_dispatch.py:981` `resolve_tool_effects` (`tools/_registry.py:127`) | **UNVERIFIED** body; registry lookup by name | small |
| 15 | `tools/transforms.py:401` → `:653` `_UpsertNodeArgumentsModel.model_validate` | pydantic parse | O(P) |
| 16 | `transforms.py:669` | linear scan for existing node | O(N) |
| 17 | `transforms.py:703-739` | requirement echo normalise, canonicalise, default LLM reviews, canonical check | O(P) |
| 18 | `transforms.py:747` credential-wiring contract check | **UNVERIFIED** body | — |
| 19 | `transforms.py:765-768` `_plugin_selection_violation` + `catalog.get_schema` | snapshot set lookup + cached schema (`catalog/service.py:256`). The result is discarded; it serves as an existence check. With operator profiles it calls `profiles.public_schema` (`catalog/policy_view.py:197`), **UNVERIFIED** cost | small |
| 20 | `transforms.py:797` `_prevalidate_transform_for_context` (`tools/_common.py:3588`) → `_prevalidate_plugin_options` (`:2534`) | `deep_thaw(options)` (`:2593`) + `config_cls.from_dict` (`:2627`), retried with placeholders on secret-ref failure (`:2648`) + value-source check (`:2706`). First lowers a one-node probe state through profile policy (`:3588-3648`) | 1–2 pydantic parses of this node |
| 21 | `transforms.py:858` `NodeSpec(...)` → `state.py:784-836` | `freeze_fields` deep-freezes this node's options, routes, branches, trigger | O(P) |
| 22 | `transforms.py:882` `_row_union_node_contract_error` (`_common.py:3963`) | `row_union` only: builds a 1-node probe state and runs `validate()` (`:3994`) | small W1 |
| 23 | `transforms.py:890` `state.with_node` (`state.py:8236`) | id list + new tuple, then `replace` → `__init__` | O(N) shallow |
| 24 | `transforms.py:894` `_reconcile_node_sink_mirror_edges` | edge reconcile | O(E) |
| 25 | **`transforms.py:895` `_post_mutation_invariant_error` → `_common.py:3956` `proposed_state.validate()`** | **W1 #1**, used only to filter for `_MUTATION_BLOCKING_INVARIANT_CODES` | W1 |
| 26 | `transforms.py:900` `reconcile_authoritative_reviews` (`interpretation_state.py:3485`) | reconciles every source and node (`:3499`, `:3509`); returns `proposed` when nothing changed (`:3518-3521`) | O(S+N) × per-component reconcile |
| 27 | `transforms.py:908` `composition_review_contract_error` (`interpretation_state.py:611`) | two whole-state scans | O(N) |
| 28 | `transforms.py:916` affected set | edge scan | O(E) |
| 29 | **`transforms.py:922` `_mutation_result` → `_common.py:1756` `new_state.validate()`** | **W1 #2**. No `catalog=` passed, so this provisional result is discarded at step 33; the docstring at `_common.py:1741-1748` says so | W1 |
| 30 | `_common.py:1768` `_applied_component_echo` (`:4108`) | `_serialize_set_pipeline_arguments(state)` serialises **every** node/edge/output (`:4047-4064`), `redact_source_storage_path` over the full payload (`:4150`), narrows to affected ids, then `canonical_json(echo)` for a 16 KiB cap (`:4197`) | O(state bytes) |
| 31 | `transforms.py:404` second `_UpsertNodeArgumentsModel.model_validate(arguments)` | same model, same args, to read `validated.id` | O(P) |
| 32 | `transforms.py:414` `_attach_post_call_hints` → `catalog.post_call_hints` | plugin hint lookup | small |
| 33 | `_dispatch.py:983` `_enforce_composition_interpretation_gate` → `_common.py:3088` | scan of sources, nodes, outputs | O(S+N+O) |
| 34 | **`_dispatch.py:764` `normalize_tool_result_validation` (`_common.py:3820`) → `:3849` `catalog.validate_composition_state`** → `plugin_policy/validation.py:725` | `validate_plugin_policy` (component scan, profile lowering, control coverage, `:149-241`) then `policy.executable_state.validate()` (`:759`) = **W1 #3**. Skipped only when the result is already stamped with the snapshot hash (`_common.py:3835`) | policy O(N) + W1 |
| 35 | `_dispatch.py:769` `_augment_with_validation_guidance` | no-op on success | — |

Post-dispatch, back in `tool_batch.py`, only when the mutation succeeded, advanced the
version and `validation.is_valid`:

| # | Site | Work | Cost |
|---|------|------|------|
| 36 | `tool_batch.py:3046` `_finalize_completed_incremental_mutation` (`:774`) → `required_controls.py:913` `wire_required_controls_state` | `state.to_dict()` (`:930`) + `wire_required_controls`; if controls were spliced, `CompositionState.from_dict` (`:993`) and another `validate_composition_state` (`tool_batch.py:798`) | O(state); +policy+W1 if changed |
| 37 | `tool_batch.py:3278` `_pending_review_runtime_findings` (`:3420`) → `interpretation_sites(state)` (`interpretation_state.py:1112`) | whole-state scan. `:1128` → `_pending_source_data_contract_sites` → `current_source_data_contract_demand` (`:1523`) → `backtraced_source_demand` (`composer/source_demand.py:264`), which runs `state.validate()` through `_unsatisfied_edge_misses` (`:210`): 1 W1 baseline (`:313`), +1 H_all (`:338`), +1 H_not_S when more than one eligible source (`:345`) | **1–3 W1 per eligible source**; 0 when the source is ineligible or carries `SOURCE_AUTHORING_KEY` |
| 38 | `tool_batch.py:3429` `cached_runtime_preflight(..., interpretation_tolerant=True)` (`composer_preflight.py:189`) | only if sources, outputs and at least one interpretation site exist (`tool_batch.py:3427`). Key build memoises the content hash on the state (`composer_preflight.py:184`). On a miss, **full Stage-2** (§3) | Stage-2, about once per new valid version |

Epilogue, every call:

| # | Site | Work | Cost |
|---|------|------|------|
| 39 | `composer/audit.py:1259-1263` `finish_success(result_payload=_result_to_audit_payload(result))` | `ToolResult.to_dict()` **#1** (`_common.py:1079`) → `_normalize_audit_payload` (`audit.py:862`) → `canonical_json_and_hash` (`:873`) | O(R) ×3 passes |
| 40 | `tool_batch.py:3345` `_serialize_tool_result` (`discovery_cache.py:54`) | `ToolResult.to_dict()` **#2** + `json.dumps` | O(R) |
| 41 | `tool_batch.py:3377` `tool_result_content_parts` | prompt-guard content parts over the JSON | O(R) |
| 42 | `tool_batch.py:3359` `_append_tool_outcome` | carrier append | O(1) |

`ToolResult.to_dict()` itself: every validation entry serialised, `_semantic_contracts_payload`,
`_graph_repair_suggestions(state, validation)` (early-exits unless a
`duplicate_connection_consumer` error exists, `_common.py:791-793`), `_compute_validation_delta`
and `deep_thaw` of `data` / `plugin_schemas` / `applied_component` (`_common.py:1091-1133`).

Persist (P4), once per LLM round, per tool outcome — see §5.

**Stage-1 walk count per successful `upsert_node`** (counted by reading each call site above):
3 inside `execute_tool` (steps 25, 29, 34), +1 if no prior validation (11), +1 for `row_union`
(22, small), +1 if controls were spliced (36), +1–3 per eligible source (37), plus the same
1–3 per source again at persist (`composer/turn_audit.py:125`). Minimum 3; for a one-source
observed-schema pipeline with misses, typically 3 + 2 + 2 = 7, plus Stage-2.

**Rejected mutation** (the common repair-loop shape): `_saved_graph_rejection_result` →
`state.validate()` (`_common.py:1373`) = 1 W1, then `normalize_tool_result_validation`
re-validates (`:3849`) = 1 W1, then `_augment_with_plugin_schemas` (`_dispatch.py:683`) and
`_augment_with_validation_guidance` (`:721`). So **2 W1 to say no**, even when the rejection
was decided by argument shape alone. `set_pipeline` rejections avoid this: `CANDIDATE`
rejections withhold state validation (`_common.py:1380-1395`, `:3838-3848`).

**`set_pipeline`**: `build_set_pipeline_candidate` (`tools/sessions.py:652`, 1,404 lines) passes
`catalog=` to `_mutation_result` (`sessions.py:2053`), so it pays one authoritative walk
instead of two. I did not trace the candidate builder line by line (**UNVERIFIED** internals).

#### 1b. One read-only / inspection call

Cache miss (`list_sources`, `get_plugin_schema`, `get_pipeline_state`, …):

1. Steps 1–8 as above.
2. `execute_tool` steps 9–13.
3. Handler builds its `data`. Catalog reads are cached (§6).
4. `_discovery_result` → **`state.validate()`** (`_common.py:1281`) — W1 #1, for a call that
   did not touch the state. Three handlers call `state.validate()` directly
   (`tools/sessions.py:3106`, `:3375`, `:3389`); `explain_validation_error`, `preview_pipeline`
   and `diff_pipeline` call `context.catalog.validate_composition_state`
   (`tools/generation.py:2178`, `:4534`, `:4672`).
5. `finalize_tool_result` → `normalize_tool_result_validation` → policy + **W1 #2**
   (`_common.py:3849`), because `_discovery_result` does not stamp a snapshot hash.
6. `admit_discovery_result` (`tool_batch.py:3053`) when a response contract exists.
7. Audit `to_dict` + canonical + hash; LLM JSON `to_dict` + `json.dumps`; cache store
   (`tool_batch.py:3370`).
8. At P4: `to_dict` #3 + response redaction + canonical JSON (§4).

Cache hit (`tool_batch.py:1586-1667`): `result_from_cached_discovery_payload` →
**`state.validate()`** (`discovery_cache.py:101`), then `normalize_tool_result_validation`
(`tool_batch.py:1617`) → a second W1, then serialise. **A cache hit skips only the handler; it
still costs 2 W1.**

`preview_pipeline` additionally awaits Stage-2 before dispatch (`tool_batch.py:2845`) and a
second, interpretation-tolerant Stage-2 when the strict result is handoff-shaped (`:2894`).

Unlike mutations, a discovery call threads `last_validation` as `prior_validation`, but that
only saves step 11; steps 4 and 5 ignore it.

---

### 2. State model mechanics

- **Immutable with structural sharing.** `CompositionState` is
  `@dataclass(frozen=True, slots=True, init=False)` (`state.py:8154`). Every `with_*` /
  `without_*` method returns `dataclasses.replace(self, ..., version=self.version + 1)`
  (`:8213-8306`). `replace` re-runs `__init__` (`:8186`), which assigns the tuples as given and
  calls `freeze_fields(self, "sources")` (`:8209`).
- **What is copied per mutation.** No deep copy of the graph. `with_node` builds an id list
  and a new outer tuple (`:8238-8248`); untouched `NodeSpec` objects are shared. The `sources`
  mapping is rebuilt on every construction because `deep_freeze` always re-wraps a
  `MappingProxyType` (`src/elspeth/contracts/freeze.py:62-63`); values are `SourceSpec`
  dataclasses, returned as-is (`:88-89`), so this is O(S) shallow.
- **Per-spec freezing.** `NodeSpec.__post_init__` deep-freezes `options`, `routes`,
  `branches`, `trigger` (`state.py:830-836`): O(size of that node), paid once per constructed
  node. `from_dict` (`:8408`) constructs every spec, so it is O(state).
- **Membership is linear.** Nodes, edges and outputs are tuples; lookups by id are
  `next(...)` / list `.index` scans (`:8238`, `transforms.py:669`). No id→spec index is kept
  on the state.
- **Mutation methods validate nothing** beyond `validate_composer_source_name`
  (`:8223`) and `version >= 1` (`:8197`). All real validation is the handler calling
  `validate()` on the result.
- **Re-validated per mutation: the whole graph.** There is no incremental or local
  validation path. `_post_mutation_invariant_error` documents that it reuses the whole walk
  to select a few intrinsic codes (`_common.py:3944-3960`).
- **Re-serialised per mutation (whole state):** `_applied_component_echo` (step 30),
  `wire_required_controls_state` (step 36), `_state_payload_for_compose_turn`
  (`turn_audit.py:124`) and the `ToolResult.to_dict` calls (validation only, not the graph).
- **Hashing/versioning.** `version` is a per-instance counter bumped by each mutation; the DB
  re-allocates the persisted version under lock (`sessions/service.py:1830`).
  `composition_content_hash` (`composer/pipeline_proposal.py:746`) is `to_dict()` + authority
  hash, memoised in the write-once `_content_hash_memo` slot (`state.py:8177-8184`, reset in
  `__init__` at `:8208`). `reconcile_authoritative_reviews` deliberately returns the same
  instance on a no-op so the memo survives (`interpretation_state.py:3489-3521`).
- **Not memoised:** `composer/turn_effects.py` hashes `stable_hash(state.to_dict())` directly
  at `:55-57`, `:632`, `:680`, `:799` and projects two states at `:86`. That module belongs to
  the other lane; frequency per turn is **UNVERIFIED** here.

**Whole-state work per tool call, flagged:** W1 ×3 (steps 25/29/34), policy component scan
(34), review reconcile (26), authoring-payload serialise + redact (30), `to_dict` for control
wiring (36), `interpretation_sites` and its nested walks (37), full snapshot `to_dict` at
persist.

---

### 3. Validation trunk

#### Tiers

| Tier | Entry | What it does | Instantiates plugins? | Builds `ExecutionGraph`? |
|------|-------|--------------|-----------------------|--------------------------|
| S — argument schema | `_dispatch.py:663` `_validate_tool_arguments`; per-tool pydantic models | closed-root JSON Schema, then model parse | no | no |
| Prevalidation | `_common.py:2534` `_prevalidate_plugin_options` | the touched plugin's config model only | config model parse, no instance | no |
| Stage 1 | `state.py:8443` `validate()` → `_validate_with_probe_cache` (`:8453`, 1,674 lines) → `_check_schema_contracts` (`:4790`, 3,362 lines) + `_semantic_validator.validate_semantic_contracts` (`state.py:9746`) | whole-graph structural, routing, schema-contract, semantic and warning rules | **yes**, under `plugin_preflight_mode`: one transform per plugin node via `ValidationProbeCache` (`state.py:2158-2256`), one sink per output (`_probe_sink_declarations`, `:2278`, memoised per walk at `:6616`), sources at `:6833` and `:7807` (memoised per source name at `:7783`) | **no** (`state.py:8456`) |
| Policy wrapper | `plugin_policy/validation.py:725` `validate_authored_composition_state` | plugin enablement, profile lowering, required-control coverage, then Stage 1 on the lowered state | via Stage 1 | no |
| Stage 2 | `execution/validation.py:455` `validate_pipeline` → `_validate_pipeline_impl` (`:497`) | 20+ ordered phases ending in the real engine path | **yes, every runtime plugin**: `instantiate_runtime_plugins` → `instantiate_plugins_from_config(settings, preflight_mode=True)` (`execution/preflight.py:344-351`) | **yes**: `ExecutionGraph.from_plugin_instances` (`preflight.py:727-744`), then `graph.validate()` (`execution/_validation_runtime.py:396`) and edge compatibility |

Stage-2 phase order, from `validation.py:595-776`: `lower_plugin_policy` (re-runs
`validate_plugin_policy`, `_validation_authoring.py:190`) → path, network, resource, secret
policy → `validate_semantic_evidence` (re-runs `validate_semantic_contracts` with its own probe
cache, `_validation_authoring.py:948`, so every transform is constructed again) → batch options
→ interpretation review → `materialize_validation_yaml` (`generate_yaml` at
`_validation_materialization.py:292`, then parses the YAML back) → four LLM/S3 policy phases →
`load_runtime_settings` → `validate_runtime_plugins` → value-source compliance →
`validate_graph_structure` → route targets → schema compatibility → three advisory passes.
With operator-profiled plugins it generates and parses a second identity YAML
(`validation.py:716-722`, `:437`).

A fresh `ValidationDependencies` and `ValidationPipeline` are built per call
(`validation.py:472-480`); that is cheap.

#### When each runs

- **Per tool call:** S, prevalidation, policy wrapper + Stage 1 (multiple times, §1).
- **Per tool call, conditionally:** Stage 2 after a successful valid mutation with pending
  interpretation sites (`tool_batch.py:3278`); Stage 2 before every `preview_pipeline`
  (`:2845`, `:2894`).
- **Per turn / completion:** `ComposerPreflight.turn_runtime_preflight` and
  `reuse_or_recompute_runtime_preflight` (`composer_preflight.py:299-381`). I counted 9 call
  sites of the preflight entry points in `composition_completion.py`, 3 in `service.py`, 3 in
  `planning_application.py`, 1 in `no_tool_finalize.py` (grep of the method names;
  how many fire per turn is the other lane's control flow, **UNVERIFIED** here).
  `reuse_or_recompute` also runs a bare `state.validate()` at `:370`.
- **Message build:** `prompts.py:414-421` `build_context_string` runs `state.to_dict()`,
  `redact_source_storage_path` and `catalog.validate_composition_state(state)` (one W1).
  One call site (`prompts.py:601`); calls per turn **UNVERIFIED**.
- **Explicit validate / run:** `ExecutionServiceImpl.validate_state`
  (`execution/service.py:3365`) and the run path.

#### Caches on this trunk

| Cache | Scope / lifetime | Key | Notes |
|-------|------------------|-----|-------|
| `ValidationProbeCache` (`state.py:2158`) | one `validate()` call | `(plugin, id(holder.options))` (`:2201`) | failures are cached and re-raised; closed on exit. Not shared across the 3+ walks of one tool call |
| Per-walk memos in `_check_schema_contracts` | one walk | `_producer_vote_memo` `:5417`, `_structural_vote_memo` `:5888`, `_producer_emit_profile_memo` `:6147`, `source_resolution_memo` `:7783`, `live_reach_memo` / `producer_resolution_memo` `:7853-7854`, `connection_memo` `:2531`, `runtime_lineage_cache` `:8462` | protect against the n(n+1)/2 upstream re-derivation described at `:5406-5415` |
| `RuntimePreflightCache` (`composer_preflight.py:152`, a dict) | one compose turn | `RuntimePreflightKey(session_scope, state_version, state_content_hash, settings_hash[:snapshot_hash], interpretation_tolerant)` (`:170-187`) | **bypassed entirely when the state contains a `blob_ref`** (`:212-215`, `:249`). Failures are cached too (`:218`). `runtime_preflight_settings_hash` resolves a path and SHA-256s it on every key build (`preflight.py:236-247`) |
| `RuntimePreflightCoordinator` (`execution/runtime_preflight.py:43`) | in-flight only | same key + session-operation context | coalesces concurrent identical work; evicts on completion |
| `ValidationResultMemo` (`execution/validation_memo.py`) | per `ExecutionServiceImpl`, **TTL 10 s, max 64 entries** (`:46-47`) | `ValidationMemoKey(user_id, session_id, state_content_hash, state_version, plugin_snapshot_hash, secret_inventory)` (`:54-69`) | single-flight per event loop; a hit is served only after re-reading the blob rows the run consulted (`:171-173`); failures never stored; results deep-copied. Skipped when `fresh` or the state has secret-ref sites (`execution/service.py:3362`). **Not used by the composer tool loop** |

---

### 4. Redaction

`composer/redaction.py` (4,509 lines) is manifest-driven: each tool has either a pydantic
`argument_model` (type-driven) or a declarative `policy`. None of it walks message history.
The unit of work is always one tool call's arguments or one tool result.

| # | Invocation | When | Walks | Repeated on the same data? |
|---|------------|------|-------|----------------------------|
| R1 | `redact_source_storage_path` in `_applied_component_echo` (`_common.py:4150`) | every successful incremental mutation | the full authoring payload's sources (shallow; O(S)), after the whole state was serialised | — |
| R2 | `redact_source_storage_path` in `build_context_string` (`prompts.py:420`) | message build | full `state.to_dict()` sources | — |
| R3 | `redact_tool_call_arguments` in the explicit-approval and `set_pipeline` branches (`tool_batch.py:1790`, `:1837`, `:1896`, `:1934`, `:2109`) | only in `explicit_approve` trust mode or when control finalisation changed a `set_pipeline` with an inline blob | arguments, O(P) | yes — the same arguments are redacted again at R4 |
| R4 | `redact_tool_call_arguments` in `persist_turn_audit` (`turn_audit.py:332`) | once per tool call at P4 | arguments after `deep_thaw` (`:288`). Type-driven: `model_validate` (`redaction.py:2574`) → `model_dump` → `copy.deepcopy` (`:2314`) → `walk_model_schema` (`:2315`) | third parse of the same arguments by a pydantic model (after steps 15 and 31) |
| R5 | `_serialize_response_via_walker` → `redact_tool_call_response` (`turn_audit.py:69-77`) | once per tool call at P4 | **`ToolResult.to_dict()` #3**, then per top-level key `_response_within_projection_budget` (iterative node/depth/width bound, `redaction.py:280`), `model_validate` against the response model (`:4287`), `_redact_via_schema` (deepcopy + schema walk), `_project_validated_response_scalars`, and `json.dumps` for the output byte cap (`:312`); then `canonical_json` | the result was already fully serialised and canonicalised at steps 39–40 |
| R6 | `redact_arg_error_response` / `redact_failure_response` (`turn_audit.py:81`, `:90`, `:317`, `:345`) | error outcomes | fixed-shape scalars | — |
| R7 | `build_rejection_records` → `serialize_tool_result` (`turn_audit.py:202`) | each failed mutation at P4 | `ToolResult.to_dict()` **#4** + `json.dumps`, stored unredacted as the planner payload | — |
| R8 | `redact_source_storage_path` in `advisor_context.py:1200`, `sessions/routes/_helpers.py:983`, `tools/sessions.py:2516`, `planning_application.py:662`; `audit_storage.py` (8 sites); `composer_mcp/server.py:925` | advisor context, HTTP state responses, planner path, audit storage | varies | other lanes / **UNVERIFIED** frequency |

`walk_model_schema` (`redaction.py:859`) re-reflects the pydantic model's `model_fields` on
every call and rebuilds its `value_provider` / `substitute_provider` closures; it is not
cached per model class. Cost scales with model size, not state size, but for
`SetPipelineArgumentsModel` the walk descends nested node/edge/output models.

Message-history redaction (`_redact_sensitive_content` in `turn_effects.py:72-837`) is the
other lane's; not traced here.

---

### 5. Persistence

#### Per LLM round: `persist_compose_turn` (`sessions/service.py:1921`, 479 lines)

Called exactly once per batch from `persist_turn_audit` (`turn_audit.py:495` / `:511`), in a
worker thread (`service.py:2470`). **One transaction, one commit** (`:2079`).

Before the transaction (`turn_audit.py:276-421`), per tool outcome: `deep_thaw` args, R4,
`json.dumps(persisted_arguments)` (`:364`), R5, and for version-advancing calls
`_state_payload_for_compose_turn` (`:98`), which does `updated_state.to_dict()` (`:124`) and
`pending_execution_interpretation_sites` (`:125`) — the hidden 1–3 W1 per eligible source
again. Two small quadratic-in-k constructs: tuple re-concatenation in a loop (`:357`) and
`next(...)` scans in `visible_event_for` (`:377`, `:383`). k is small; noted, not hot.

Inside the transaction. Let **A** = one authority proof =
`database_now` (1) + fence `SELECT` (`service.py:1238`) + jobs `SELECT`
(`coordination/composer_operation_authority.py:955`) + a second `database_now` when a job row
exists (`:980`) = **3–4 statements**. Counted by reading each helper:

| Step | Site | Statements |
|------|------|------------|
| lock | `process_session_lock` + `engine.begin()` (`:1085`); PG `pg_advisory_xact_lock` (`:1074`), SQLite per-session RLock (`:1139`) | 0–1 |
| mutation capability | `_session_composer_mutation_transaction` (`:1268-1276`) | A |
| parent check, head read, sequence reserve | `:2093`, `:2100`, `:2115` → `:1541` | 3 |
| assistant row | `_insert_chat_message` (`:1546`): `_require_session_write_authority_on_connection` (`:10138`) + `INSERT` | A + 1 |
| **per tool call** — tool row | A + parent-assistant check (`:1624`, **UNVERIFIED** as 1 statement) + `INSERT` | A + 2 |
| **per tool call** — private evidence audit row (`:2183`) | A + `INSERT` | A + 1 |
| **per version-advancing call** — `_insert_composition_checkpoint` (`:6353`) | A (`:6368-6374`), then `_insert_composition_state` (`:1723`): A again (`:1814`) + `MAX(version)` (`:1830`) + `INSERT` (`:1836`) + pending-interpretation `SELECT` (`sessions/dead_site_supersession.py:86`) + `supersede_open_approvals` (`database_now` + `UPDATE`, `coordination/approval_authority.py:438`) | 2A + 5 |
| per rejection | `record_composition_rejection` (`:2206-2219`) | **UNVERIFIED** |
| per private failure | audit row (`:2222`) | A + 1 |
| checkpoint witness | re-`SELECT` of the last inserted state row + `_row_to_state_record` (`:2239-2247`) | 1 |

So about **2A+3 ≈ 9–11 statements per non-mutating tool call and 4A+8 ≈ 20–24 per
version-advancing call**, most of them the same fence proof repeated on the same connection
in the same transaction. Static count; not measured.

**What is serialised.** A full snapshot per version-advancing tool call: `sources`, `nodes`,
`edges`, `outputs`, `metadata_`, `composer_meta` each go through `envelope_state_column`
(`sessions/state_envelope.py:32`), which `deep_thaw`s the value again and wraps it as
`{"_version": 1, "data": ...}`; the columns are SQLAlchemy `JSON` (`sessions/models.py:533-540`),
so the driver serialises each. No delta encoding, no compression, no encryption on this path
(`composer/diagnostic_evidence.py:4` states it is not an encrypted store). Hashing happens
before the transaction: argument and result canonical hashes (`audit.py:713`, `:873`), and
`_validated_canonical_mapping_pair` re-verifies each stored result hash at P4
(`turn_audit.py:456`; body **UNVERIFIED**, by name it re-parses and re-hashes the canonical
result).

**Row count per round:** 1 assistant row + 2k chat rows (tool + private-evidence audit) +
m `composition_states` rows + rejections + failures. The tool row's `content` is the redacted
result JSON; the private audit row carries `arguments_canonical`, `raw_arguments` and
`result_canonical` (`turn_audit.py:460-472`), so each tool call's arguments and result are
stored two to three times in different projections.

**Size drivers:** node `options` (prompt templates, schemas, inline requirement rows) × m
snapshots per round; `result_canonical` per call (carries the full validation summary, and
`plugin_schemas` on failures); the redacted tool row.

#### Per turn

- Turn start (`sessions/composer_turn.py`): `get_session` (`:896`),
  `load_graph_recovery_context` (`:902`), `get_messages(limit=None)` (`:979`, full history
  load), `list_composition_proposals` (`:990`). Head state load goes through
  `_row_to_state_record` (`service.py:7005`) and then `CompositionState.from_dict` — O(state).
- Turn end: `save_composition_state(provenance="post_compose")` (`composer_turn.py:1256`) →
  `session_operation_authority.mutate(... append_state)` (`service.py:6435-6446`) — **one more
  full snapshot row** in its own transaction, even when its content equals the last
  `tool_call` checkpoint. `append_state` internals **UNVERIFIED**.
- Other per-turn writes (user message, LLM-call audit cohort, provider attempts, token usage,
  operation receipts, async-job completion) exist as methods on `SessionServiceImpl`; their
  per-turn counts belong to the control-flow lane and are **UNVERIFIED** here.

#### SQLite vs PostgreSQL

- SQLite: WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`
  (`sessions/engine.py:133-139`); manual `BEGIN` via a listener (`:146-150`); writers
  serialised by a process-level session lock plus a per-session RLock
  (`service.py:1085`, `:1139-1141`). `database_now` returns a string that is parsed with
  `datetime.fromisoformat` (`coordination/database_clock.py:91-92`).
- PostgreSQL: `pg_advisory_xact_lock(classid, hashtext(session_id))` (`service.py:1044-1075`);
  `clock_timestamp()` for `database_now` (`database_clock.py:103-106`). Every A-proof
  statement is a network round trip, so the 20+ statements per mutating call cost far more
  here than on SQLite.
- `sessions/schema.py`, `operation_codec.py` and `audit_checkpoint.py` are not on the
  per-tool-call path as far as I traced: `operation_codec.py` exposes two request/response
  hash helpers (`:16`, `:27`), `audit_checkpoint.py` one function (`:20`).
  `proposal_authority.py` is reached only in explicit-approval mode. **UNVERIFIED** beyond
  their signatures.

---

### 6. Catalog / plugin schema / policy lookups

- **Catalog: built once.** `CatalogServiceImpl.__init__` (`catalog/service.py:220-236`) builds
  every plugin's JSON schema and summary at construction. `get_schema` is a dict lookup
  (`:251-257`); `list_*` returns a fresh list over shared summaries (`:242-249`). The comments
  at `:231-233` and `:411-412` record that rebuilding per call used to re-run
  `model_json_schema` on every composer request.
- **Policy view: per request.** `PolicyCatalogView` (`catalog/policy_view.py:20`) caches the
  unrestricted listing per kind for the view's lifetime (`:48-67`). Not cached:
  `_usable_profile_aliases` rebuilds `dict(self.snapshot.usable_profile_aliases)` on every call
  (`:91`); `unavailable_reason` rebuilds a dict and, on a miss, a set of every installed plugin
  (`:179-186`); `_visible` re-projects each profiled plugin through `profiles.public_summary`
  on every `list_*` (`:96-115`); `get_schema` re-projects through `profiles.public_schema`
  (`:197`). `profiles.py` internals **UNVERIFIED**.
- **Plugin policy validation: recomputed per call**, no cache
  (`plugin_policy/validation.py:149-241`): one component list, 1–4 linear passes over it,
  profile lowering, then `control_coverage_findings(state, capability)` per REQUIRED control
  (`:232-234`; `coverage.py:377` body **UNVERIFIED**).
- **Plugin registry:** `get_shared_plugin_manager()` is a locked process singleton
  (`src/elspeth/plugins/infrastructure/manager.py:334`). The seven `_known_*_plugins()` helpers
  in `state.py:1709-1763` rebuild a frozenset from `get_transforms()` on every call; how many
  times each is called per walk is **UNVERIFIED**.
- **Tool definitions:** `get_tool_definitions()` deep-thaws all definitions per call
  (`_dispatch.py:342-348`); the comment at `:340-341` says once per compose turn.
  `_TOOL_SCHEMA_BY_NAME` is frozen at import (`:399`), but the S validator is rebuilt per call
  (step 12). By contrast the wire validators (`wire_projection.py:885`) and the canonical
  `set_pipeline` schema and validator (`tools/schema_contract.py:407`, `:438`, both `@cache`)
  are process-wide.

---

### 7. Ranking

#### Top-10 hottest functions (frequency × body weight; static judgement, not measured)

| # | Function | Why |
|---|----------|-----|
| 1 | `composer/state.py:8453` `CompositionState._validate_with_probe_cache` (with `validate` `:8443`) | Whole-graph; ≥3 calls per mutation, 2 per read-only call, 2 per cache hit, plus hidden calls via #4. 1,674 lines |
| 2 | `composer/state.py:4790` `_check_schema_contracts` | The heavy half of every W1: 3,362 lines, producer resolution, propagation votes, per-node plugin probes |
| 3 | `composer/state.py:2211` `ValidationProbeCache._construct` (→ `PluginManager.create_transform`) | One pydantic config parse + plugin construction per plugin node per W1; cache does not outlive a walk, so N × (walks per tool call) |
| 4 | `composer/source_demand.py:264` `backtraced_source_demand` (via `interpretation_state.py:1112` `interpretation_sites`) | 1–3 extra W1 per eligible source, on every successful mutation and again at persist; invisible from the call sites |
| 5 | `tools/_common.py:3820` `normalize_tool_result_validation` → `plugin_policy/validation.py:725` `validate_authored_composition_state` | Runs on every tool call including cache hits; policy scan + W1; makes the handler's own W1 redundant |
| 6 | `execution/validation.py:497` `_validate_pipeline_impl` | Full Stage-2: YAML round trip, all plugins instantiated, real `ExecutionGraph`. Fires per successful valid mutation while reviews are pending, and per preview |
| 7 | `tools/_common.py:1079` `ToolResult.to_dict` | 3–4× per tool call; serialises the full validation summary each time |
| 8 | `sessions/service.py:1921` `persist_compose_turn` (with `_insert_chat_message` `:1546`, `_insert_composition_state` `:1723`) | 9–24 SQL statements per tool call, mostly repeated authority proofs; full snapshot per mutation |
| 9 | `composer/redaction.py:4178` `redact_tool_call_response` (+ `_redact_via_schema` `:2286`, `walk_model_schema` `:859`) | Per tool call: re-serialise, budget walk, model validate, deepcopy, uncached schema reflection, two JSON encodes |
| 10 | `tools/_common.py:4108` `_applied_component_echo` | Serialises and redacts the whole authoring payload to echo one component, on every successful incremental mutation |

Runners-up: `_dispatch.py:625` `_schema_errors` (validator rebuilt per call);
`composer/state.py:8310` `to_dict` (called from at least 5 per-call sites);
`composer/audit.py:816` `finish_success`; `_semantic_validator.py:524`
`validate_semantic_contracts`; `interpretation_state.py:3485` `reconcile_authoritative_reviews`.

#### Largest functions and classes

Measured with `ast` `end_lineno - lineno + 1` over the in-scope files (system `python3 -I`,
no project imports):

| Lines | Kind | Location |
|-------|------|----------|
| 9,371 | class | `sessions/service.py:799` `SessionServiceImpl` |
| 3,362 | function | `composer/state.py:4790` `_check_schema_contracts` |
| 1,972 | class | `composer/state.py:8155` `CompositionState` (dominated by the next row) |
| 1,674 | method | `composer/state.py:8453` `_validate_with_probe_cache` |
| 1,404 | function | `composer/tools/sessions.py:652` `build_set_pipeline_candidate` |
| 495 | function | `composer/tools/generation.py:3720` `_compute_proof_diagnostics_for_source` |
| 479 | method | `sessions/service.py:1921` `persist_compose_turn` |
| 470 | method | `sessions/service.py:3582` `_settle_pipeline_composition_proposal` |
| 463 | method | `sessions/service.py:5141` `resolve_interpretation_event` |
| 428 | function | `composer/turn_audit.py:212` `persist_turn_audit` |
| 305 | function | `execution/_validation_materialization.py:267` `materialize_validation_yaml` |
| 280 | function | `execution/validation.py:497` `_validate_pipeline_impl` |
| 280 | function | `composer/tools/transforms.py:647` `_execute_upsert_node` |

`run_tool_batch` (`composer/tool_batch.py:940` to about `:3417`) is roughly 2,480 lines but is
the other lane's file.

---

### 8. Existing caching, memoisation and perf notes in the code

Found by grepping the in-scope files for `hot path|quadratic|O(n|latency|memo|lru_cache|@cache|
performance|cache hit|single-flight|per validate|event loop`, then reading each hit.

| Site | What it protects |
|------|------------------|
| `composer/state.py:2158-2180` `ValidationProbeCache` (elspeth-97b15928bc) | Six probe questions per node used to construct six instances: "an 80-node llm chain paid for 480 parses per validate, and the composer's review-debt check runs validate four times per import" |
| `composer/state.py:5406-5417` `_producer_vote_memo` (elspeth-e5a38115a6) | Pass-through vote recursed upstream unmemoised: n(n+1)/2 constructions per validate, 820 at n=40 |
| `composer/state.py:5888`, `:6147`, `:7783`, `:7853-7854`, `:2531`, `:8462` | per-walk memos for structural votes, emit profiles, source and producer name resolution, live reach, connection lineage |
| `composer/state.py:6613-6616` | one sink construction per output per validate, shared by two rules |
| `composer/state.py:8177-8184` + `pipeline_proposal.py:754-775` | content-hash memo: "preflight identity keys rebuild it several times per composer turn" |
| `interpretation_state.py:3489-3495` | identity-preserving reconcile so the content-hash memo survives |
| `tools/_common.py:1741-1752` | `_mutation_result(catalog=...)`: avoids "a whole-graph walk whose output no one reads" — applied to `set_pipeline` only (1 of 21 `_mutation_result(` call sites; grep count of 22 lines minus the `def`) |
| `tools/_common.py:3824-3836` | snapshot-hash stamp lets the dispatch boundary reuse a validation already produced for the same snapshot |
| `tools/_dispatch.py:841-845`, `:862-866` | `prior_validation` threading so the delta needs no extra `validate()` |
| `tools/_dispatch.py:683-700` | inline `plugin_schemas` on failures to save an LLM round trip ("13 tool calls + 18 LLM rounds to converge a 4-plugin pipeline") |
| `tools/_dispatch.py:721-731`, `_common.py:4108-4128` | inline validation guidance and applied-component echo, each to save a `get_pipeline_state` / `explain_validation_error` turn |
| `tools/_dispatch.py:335-341`, `:351-368` | per-turn deep-thaw of tool defs judged negligible; trailing-tool pin protects the Anthropic prompt cache |
| `tools/schema_contract.py:407`, `:438` | `@cache` canonical `set_pipeline` schema text and compiled validator |
| `tools/wire_projection.py:885` | wire validators compiled once at import |
| `tools/generation.py:4326` | `_memoized_resolver` for proof-blob resolution inside one preview |
| `catalog/service.py:226-236`, `:411-413` | schema and summary caches; rebuilding "regenerated every plugin's JSON schema on every composer request" |
| `catalog/policy_view.py:48-59` | per-view listing cache so one `list_sources` does not derive every summary twice |
| `composer/discovery_cache.py` + `tool_batch.py:1586` | per-turn discovery cache of handler `data` |
| `composer_preflight.py:152`, `:210-215`; `execution/runtime_preflight.py` | per-turn Stage-2 cache and in-flight coalescing; blob-ref states excluded |
| `execution/validation_memo.py` | 10 s / 64-entry memo for `/validate` + `/audit-readiness` |
| `sessions/service.py:1887-1891` | dead-site sweep takes a builder so the common no-pending case skips a row re-read and record parse |
| `source_demand.py:339-342` | single eligible source skips the third validation run |
| `sessions/engine.py:66-69` | WAL + `synchronous=NORMAL` durability/perf trade |
| `composer/skills/__init__.py:39`, `prompts.py:103` (`lru_cache(maxsize=8)`), `advisor_audit.py:81` (`@cache`) | skill/prompt text loading (other lane) |

No `# perf` TODOs or open perf tickets were found in the in-scope files beyond the ticket ids
cited above.

---

### 9. Existing perf evidence in-tree

| Test | Measures | Threshold | Covers the composer trunk? |
|------|----------|-----------|----------------------------|
| `tests/unit/web/composer/test_schema_contract_walk_cost.py` | `create_transform` **construction count** (not wall time) for `validate()` over llm chains of n=10/20/40, via a tracking plugin manager | second difference zero (`:121`); exactly 1 construction per node per validate (`:44`, `:122`); exactly 4 per node per review-debt check (`:48`) | **Yes — the only one.** Its docstring records the prior incident: an 80-node import "stalled the whole event loop for ~50 s", and after the first fix "sat 0.8 s from the shared 5 s preflight bound" |
| `tests/performance/benchmarks/test_validator_walk.py` | `ExecutionGraph._walk_effective_guaranteed_fields` on a 16-node diamond | P99 bound < 5 ms and median < 5 ms (`:85-88`) | Engine DAG only (reached through Stage 2) |
| `tests/performance/benchmarks/test_schema_validation.py` | CLI-path plugin instantiation, graph construction, end-to-end validation for a 3-passthrough pipeline | < 500 ms (`:85`), < 100 ms (`:147`), < 200 ms (`:217`) | Same engine functions Stage 2 calls; not the composer wrapper |
| `tests/performance/benchmarks/test_cross_check_overhead.py` | per-row `verify_pass_through` and dispatcher overhead | median ≤ 25 µs, tail ≤ 50 µs; batch median ≤ 1,500 µs, tail ≤ 3,000 µs | No (run-time row path) |
| `tests/performance/benchmarks/test_canonical_hashing.py` | `stable_hash` throughput by row size | > 5,000 / > 500 / > 50 rows/s for small / ~5 KB / large rows (`:93`, `:113`, `:132`) | Indirect: the same canonicaliser hashes tool arguments and results; the ~5 KB floor implies up to 2 ms per medium payload at the threshold |
| `tests/performance/benchmarks/test_db_write.py` | Landscape recorder writes on in-memory SQLite | e.g. `begin_run` < 50 ms, > 500 rows/s | No (Landscape, not the sessions DB) |

The `performance` marker is deselected by default and xdist disables `pytest-benchmark`
(AGENTS.md), so none of the benchmark files run in the default suite. `grep` for
`composer|CompositionState|validate_pipeline|redact_tool` under `tests/performance` returned
no file: **there is no wall-time benchmark for `CompositionState.validate`, `execute_tool`,
redaction, or `persist_compose_turn`.**

---

### 10. Not verified / limits

- No timings. All "cost" entries are complexity classes and static call counts.
- `resolve_tool_effects`, `_saved_graph_credential_wiring_contract_failure`,
  `control_coverage_findings`, `OperatorProfileRegistry.*`, `append_state`,
  `record_composition_rejection`, `_assert_parent_assistant_message`,
  `_validated_canonical_mapping_pair`: bodies not read.
- Wall-time complexity of one W1 in N is not pinned by any test; only its plugin-construction
  count is pinned linear for chains. Branching topologies (coalesce, row_union, fan-in) were
  not analysed for super-linear behaviour.
- `_eligible_source_stamp_size` (which sources trigger the hidden walks in §1a step 37) was
  not read; the docstring at `interpretation_state.py:1486-1488` says "a
  not-preflight-checkable source".
- How many times per turn the turn-level preflight, `build_context_string` and
  `turn_effects` hashing fire is the control-flow lane's question.
- `tools/sources.py`, `tools/blobs.py`, `tools/sessions.py` handlers were sampled by grep for
  whole-state calls, not read in full. `source_inspection.py` and `yaml_importer.py` are
  blob-content and import paths, not per-tool-call state handling; not traced.

## Appendix E — Hot paths outside the engine row loop and the composer turn loop

Method: static analysis only. No Python venv exists in this checkout, so nothing was
executed and **no timing in this file is measured**. Every hop below was read in the
source at the cited `path:line`. Anything I did not read is marked UNVERIFIED.
Frequency classes are derived from control flow, not from profiling.

Scope reminder: the engine row loop / Landscape writes and the composer turn loop /
composer state are owned by other lanes. Where my paths enter them I name the entry
point only.

All paths are relative to `src/elspeth/` unless they start with `tests/` or `evals/`.

---

### Ranked top 10 (hottest non-headline paths)

| # | Path | Why it is hot |
|---|------|---------------|
| 1 | **LLM call trunk** `plugins/transforms/llm/transform.py` → `providers/*.py` → `plugins/infrastructure/clients/llm.py` | Wall-clock of every real run lives here; per query: 1 render + 3 canonical-JSON hashes + rate-limit poll + network + 2 Landscape writes + 2 telemetry hashes. |
| 2 | **Web-run per-LLM-call governance** `web/execution/service.py:4964-5002` | Adds 2 lease CAS transactions + 1 admit transaction + **2 full-run `calls` SELECTs** + 1 settle transaction to every LLM call of a web run. The full-run rescan makes settlement O(calls so far) per call, O(N²) per run. |
| 3 | **Row-level serialization at the batch handoff** `engine/executors/transform.py:603-624` + `plugins/infrastructure/batching/mixin.py` | Engine blocks on each row's waiter; the 30-thread mixin pool holds one row at a time per engine worker. Real concurrency = `pool_size` queries within one row. 3 thread handoffs per row. |
| 4 | **Durable run-progress websocket poll** `web/execution/routes.py:2166-2223` | Per open socket, every 0.25 s: 2 worker hops of auth (about 7 SQL statements) + 1 `read_after` (4 statements) + `get_run`; auth is repeated per event sent. PostgreSQL deployments only. |
| 5 | **Per-request auth + role dependency** `web/auth/middleware.py:80,158` | Paid by every pipeline route: 2 worker-thread hops, 7 SQL statements, 4 connection acquisitions, one of them a fresh owner-only SQLite open. |
| 6 | **Web validation trunk** `web/execution/validation.py:497-776` | About 20 phases per call incl. YAML generation, Pydantic settings load, full plugin instantiation, graph build + 2 graph validations; a second YAML+settings pass when plugins are operator-profiled. Called from validate route, execute, audit-readiness snapshot and composer preflight. |
| 7 | **Local-file sink effect rebuild** `plugins/sinks/csv_sink.py:499-652` + `_local_file_effects.py:465-530` | Each sink effect re-streams the whole target (append: copies existing file bytes; replace: re-serializes the cumulative member snapshot), SHA-256s all of it, fsyncs file + directory twice. Cost per effect is O(output so far). |
| 8 | **Per-row HTTP client construction** `plugins/transforms/azure/base.py:560-580,327-330`, `providers/openrouter.py:648-657`, `clients/http.py:1913` | A new `AuditedHTTPClient` (own `httpx.Client` pool) per row/state, closed after the row; SSRF-safe requests build a new `httpx.Client` per request by design. No TCP/TLS reuse across rows. |
| 9 | **Rate limiter acquire** `core/rate_limit/limiter.py:261-309` | Every external call; one process-wide lock per service, 10 ms sleep-poll while limited; default budget 60 requests/minute per service. |
| 10 | **CSV/JSON source row emission** `plugins/sources/csv_source.py:594-629` | Per source row: Pydantic `model_validate` + contract validation; the only per-row cost on runs with no external calls. |

Close runners-up: session-operation lease renewal + `guard_external_effect` (F), frontend
1.5 s composer polls (H), `_call_async` pipeline-thread → event-loop bridge (D).

---

### A. External-call trunk (deepest)

#### A.1 Call spine, one LLM transform row (single-query, Azure provider)

| Hop | `path:line function` | Frequency |
|-----|----------------------|-----------|
| 0 | `engine/executors/transform.py:365-368` caps `max_pending = min(transform.batch_pool_size, concurrency.max_workers)` and calls `connect_output` | per node, per run |
| 1 | `engine/executors/transform.py:600-604` `adapter.register` → `batch_runtime.accept(...)` | per row (per attempt) |
| 2 | `engine/executors/transform.py:624` `waiter.wait(timeout=batch_wait_timeout)` — orchestrator thread blocks here | per row |
| 3 | `plugins/transforms/llm/transform.py:1978` `LLMTransform.accept` → `plugins/infrastructure/batching/mixin.py:197` `accept_row` | per row |
| 4 | `batching/mixin.py:233` `RowReorderBuffer.submit` (`batching/row_reorder_buffer.py:168`, blocks at `:188` when `len(pending) >= max_pending`) | per row |
| 5 | `batching/mixin.py:242` `ThreadPoolExecutor.submit(_process_and_complete)` — thread handoff 1 | per row |
| 6 | `batching/mixin.py:326` `processor(row, ctx)` → `llm/transform.py:1990` `_process_row` (blank-input check, field-collision loop over contract fields, input checks) | per row |
| 7 | `llm/transform.py:289` `SingleQueryStrategy.execute` | per row |
| 8 | `llm/transform.py:309` `template.render_with_metadata` → `plugins/transforms/llm/templates.py:181-253`: Jinja render (`:204`), `canonical_json` + SHA-256 of visible row (`:219`), SHA-256 of prompt (`:224`), `canonical_json` + SHA-256 of the sorted contract (`:229-239`) | per query |
| 9 | `llm/transform.py:325` `resolve_image_parts` (payload-store reads; only when `image_inputs` configured) | per query |
| 10 | `llm/transform.py:340` `build_structured_response_directive`; `:350-358` message build | per query |
| 11 | `llm/transform.py:369` `provider.execute_query` → `plugins/transforms/llm/providers/azure.py:185` | per query |
| 12 | `providers/azure.py:236` `_get_llm_client` (`:344-364`): lock + construct an `AuditedLLMClient` per audit-parent cache key; popped again in `finally` at `:273-274` | per query |
| 13 | `plugins/infrastructure/clients/llm.py:695` `chat_completion`; `:735` `_next_call_index` → `clients/base.py:137-153` → `execution.allocate_call_index` (**enters Landscape lane**) | per attempt |
| 14 | `clients/llm.py:749` `request_dto.to_dict()` | per attempt |
| 15 | `clients/llm.py:769` `_acquire_rate_limit` → `clients/base.py:231-241` → `core/rate_limit/limiter.py:261` `acquire` | per attempt |
| 16 | `clients/llm.py:783` `_before_llm_call` → `clients/base.py:104-108` → `LLMCallGovernance.before_call` (web runs only, see A.4) | per attempt |
| 17 | `clients/llm.py:788` `self._client.chat.completions.create(**sdk_kwargs)` — the network call; SDK built with `max_retries=0` (`providers/azure.py:338`) | per attempt |
| 18 | `clients/llm.py:869` `response.model_dump()`; validation branches `:870-1197` each record + raise | per attempt |
| 19 | `clients/llm.py:1208-1220` `_record_image_call` → `clients/base.py:155-229` `_record_call` → `execution.record_call` (**enters Landscape lane**), then `after_call` governance at `base.py:225-228` | per attempt |
| 20 | `clients/llm.py:1225` `_emit_telemetry_after_audit` (`:635-694`): `stable_hash(request_data)` and `stable_hash(response_data)` (`:673-674`) then the telemetry callback | per attempt |
| 21 | back in `llm/transform.py:415` finish-reason check, `:433` `strip_markdown_fences`, `:436` Langfuse `tracer.record_success` | per query |
| 22 | `llm/transform.py:453-486`: `row.to_dict()`, `extract_structured_fields` (`plugins/transforms/llm/validation.py:268`, `json.loads` at `:288`), `propagate_contract` (`contracts/contract_propagation.py:16`, loops every output field), two contract alignment passes, new `PipelineRow` | per row |
| 23 | `batching/mixin.py:341` `_complete_ticket` → `RowReorderBuffer.complete` (`row_reorder_buffer.py:226`, `notify` at `:268`) — thread handoff 2 | per row |
| 24 | `batching/mixin.py:377-405` `_release_loop`: `wait_for_next_release(timeout=1.0)` then `self._batch_output.emit(...)` — thread handoff 3, wakes the waiter of hop 2 | per row |

Multi-query variant replaces hops 7-22:

- `llm/transform.py:554` `MultiQueryStrategy.execute` → `:580` `_execute_sequential` (`:904`) or `:577` `_execute_parallel` (`:1047`).
- Per query: `:602` `_execute_one_query` does the same render/hash/call/parse sequence (`:632` context build, `:655` render, `:724` `provider.execute_query`, `:820` structured extraction).
- Sequential retry loop: `:939-988`, exponential backoff 0.05 s → 1.0 s cap (`:106-107`), bounded by `max_capacity_retry_seconds` (default 3600, `plugins/infrastructure/pooling/config.py:32`).
- Ordered mode adds per query a full-row merge + `propagate_contract` before (`:934-937`) and after (`:1016-1023`), plus `verify_intermediate` (`:1023` → `:2030`).
- Parallel: `:1143` `PooledExecutor.execute_batch` (`plugins/infrastructure/pooling/executor.py:221`), per query a worker runs `_execute_single` (`:538`) with semaphore acquire polled at 50 ms (`:423`), global dispatch gate (`:478-534`), AIMD throttle on `CapacityError` (`:620-643`).

#### A.2 Multipliers

- Calls per row = queries per row (1, or `len(queries)`) × attempts per query.
- Attempts: the audited client itself never retries (SDK `max_retries=0`). Retries come from (a) the sequential loop `transform.py:939`, (b) the pool's AIMD loop `pooling/executor.py:538-660`, (c) engine-level row retry for a re-raised retryable error at `transform.py:405-406` (single-query; engine lane).
- Per attempt, fixed overhead visible in code: 2 Landscape round-trips (index allocate + call record), 2 `stable_hash` over request and full response, 1 `model_dump`, 1 limiter acquisition.
- Per query: 3 SHA-256 over canonical JSON / prompt text. The contract hash at `templates.py:229-239` is recomputed for every row although the contract is normally identical row to row (not cached in this function).
- Per row: `row.to_dict()` + `propagate_contract` + 2 alignment passes + a deep-freezing `PipelineRow` constructor.

#### A.3 Concurrency model and the chokepoint

- **Rows are sequential per engine worker.** `engine/executors/transform.py:603-624` calls `accept` and immediately blocks on that row's waiter; the comment at `:606-617` states this is deliberate. I found no row-level thread pool in `src/elspeth/engine` (grep for `ThreadPoolExecutor(`, controlled against a known hit in `web/execution/service.py:1199`; the only engine threads are heartbeat, idle-timeout pump and sink-effect helper). Multi-process followers, if any, are the engine lane's subject.
- Consequence: `BatchTransformMixin`'s pool (`mixin.py:184`, `max_workers = max_pending`, default 30, capped by `concurrency.max_workers` whose default is 4 at `core/config.py:1872-1876`) and its `RowReorderBuffer` never hold more than one row per engine worker. The row pays three thread handoffs and a release-thread wake for no cross-row parallelism.
- **Intra-row concurrency** exists only in multi-query parallel mode: `PooledExecutor` has `ThreadPoolExecutor(max_workers=pool_size)` and a `Semaphore(pool_size)` (`pooling/executor.py:121-124`); `pool_size` defaults to 1 (`pooling/config.py:27`), and `pool_config` is `None` unless `pool_size > 1` (`llm/base.py:451-468`).
- `PooledExecutor.execute_batch` takes `self._batch_lock` for the whole batch (`pooling/executor.py:256-260`, docstring `:234-235`): one row's query batch at a time per transform instance. Given rows are already sequential this lock is not the binding constraint today, but it would be the next one if rows were ever parallelised.
- Ordered multi-query requires `pool_size=1` (`llm/base.py:269`), so it is strictly serial: N network calls back to back per row.
- Backpressure: `RowReorderBuffer.submit` blocks at `max_pending`; `flush_batch_processing` polls every 100 ms (`mixin.py:461`); release loop wakes every 1.0 s when idle (`mixin.py:379`).
- Client cache churn: `providers/azure.py:273-274` pops the per-parent `AuditedLLMClient` in `finally` after every query, so in parallel multi-query mode queries sharing one `state_id` can each construct a client. Call indices stay unique because allocation is delegated to the repository (`clients/base.py:140-141`). The underlying `AzureOpenAI` SDK client is shared for the run (`providers/azure.py:328-342`), so HTTP connection reuse is preserved for Azure.

#### A.4 Web-run governance multiplier (rank 2)

Wired at `web/execution/service.py:4253-4263` for every web-launched run:

| Step | `path:line` | Cost |
|------|-------------|------|
| before | `service.py:4972` `guard_external_effect` → `web/coordination/lifecycle.py:840-859` → `repository.py:5323` `compare_and_swap` → `mutate` (`:5474`, locks the session row `FOR UPDATE` at `:5250-5251`, reads DB clock, UPDATE at `:5235`) | 1 write transaction |
| before | `service.py:4973` `begin_run_provider_attempt_sync` → `web/sessions/service.py:7510-7531`: process lock + session write lock, context re-proof, `begin_provider_attempt_on_connection` (**enters sessions lane**) | 1 write transaction |
| after | `service.py:4992` `guard_external_effect` | 1 write transaction |
| after | `service.py:4993-4995` `_run_token_usage_entries(landscape_db, landscape_run_id=...)` then filter to one `call_id` | **2 SELECTs returning every LLM call of the run** (`service.py:896-915`, three-table joins), plus `json.loads(config_json)` and a sort per returned row (`:917-936`) |
| after | `service.py:4998` `settle_run_provider_attempt_sync` → `sessions/service.py:7706-7722` | 1 write transaction |

So a web run adds 4 sessions-DB write transactions and 2 Landscape scans per LLM attempt, all on the pipeline thread, synchronously inside the audited client (`clients/base.py:104-108` and `:225-228`). The scan is the one super-linear cost I found in this lane: call k reads k rows, so a run with N LLM calls reads about N²/2 rows in total and parses a node config JSON for each. CLI runs do not pay this (`llm_call_governance` is `None`).

#### A.5 Other external-call clients

- `plugins/infrastructure/clients/http.py:1420` `_execute_request`: `:1468` limiter, `:1469` call index, request, `_record_and_emit` (`:444`) with `stable_hash` of request and response (`:847-848`). Same shape as the LLM client.
- SSRF-safe path `:1750` `request_ssrf_safe`: a fresh `httpx.Client` per request (`:1913`, rationale at `:1908-1912`: IP-pinned URLs would alias pool keys); each redirect hop is its own call index, limiter acquire, client and audit record (`:2190`, `:2267`, `:2286`, `:2341`). Used by `plugins/transforms/web_scrape.py:1878,1894,1905`.
- Azure safety transforms (`plugins/transforms/azure/base.py`): same `BatchTransformMixin` row model (`:218-222`); `_get_http_client` builds an `AuditedHTTPClient` per `state_id` (`:560-580`) and `:327-330` closes it after the row. Each instance builds its own `httpx.Client` (`clients/http.py:318-323`), so there is no connection reuse across rows.
- OpenRouter provider builds an `AuditedHTTPClient` per audit parent with refcounted release (`providers/openrouter.py:648-692`); same per-row connection churn. Bedrock and Gateway providers: UNVERIFIED (not read).
- Replay/verify (`clients/replayer.py`, `clients/verifier.py`): entered at `clients/llm.py:752` and `:759-760`; they substitute a Landscape read for the network call. Not read in depth.

---

### E. Web request-level trunk

#### E.1 What every request pays

Middleware stack (`web/app.py:1932-1963`, outermost first): `InstanceIdentityMiddleware` (`web/middleware/instance_identity.py:52`, wraps `send` only), `_BrowserDocumentHeadersMiddleware`, `_BodySizeLimitMiddleware`, `RequestIdMiddleware` (`web/middleware/request_id.py:93`), `CORSMiddleware`. None of these touches a database. **Rejected as hot.**

There is no global rate-limit middleware: `web/middleware/rate_limit.py` holds route dependencies (`get_rate_limiter` etc.), in-memory per process on SQLite (`ComposerRateLimiter.check`, pure Python under an `asyncio.Lock`) and SQL-backed on PostgreSQL (`SharedRateLimiter.check` → one worker hop → `RepositoryRateLimitAuthority.admit`; wiring at `web/app.py:2338-2366`).

#### E.2 Authenticated pipeline request, local auth provider

| Hop | `path:line` | DB work |
|-----|-------------|---------|
| 1 | `web/auth/middleware.py:80` `get_current_user` → `:133` `auth_provider.authenticate` | — |
| 2 | `web/auth/local.py:1310` `run_sync_in_worker(_authenticate_sync)` — worker hop 1 (`web/async_workers.py:634`; shared pool `MAX_WORKERS = 16` at `:37`; awaiter polls `asyncio.wait(timeout=0.1)` at `:705`) | — |
| 3 | `local.py:1314` → `web/auth/session_token.py:294` `authenticate` → `decode` (`:259` `jwt.decode`) | CPU only |
| 4 | `session_token.py:306` `_principal_is_active` → `web/app.py:1463-1464` → `web/coordination/identity_authority.py:2500-2505` | 1 SELECT, 1 connection checkout |
| 5 | `session_token.py:308` `sessions.is_active` → `web/coordination/auth_session_authority.py:48-62` | clock read + 1 SELECT, 1 checkout |
| 6 | `local.py:1315` `_credential_is_current` (`:1274-1286`) via `_connect` → `_get_conn` (`:385-386`) → `_connect_owner_only_database` (`:280-294`) | **new SQLite connection per call** (descriptor open, `fstat`, `stat`, connect via `/proc/self/fd`), 1 SELECT |
| 7 | `web/auth/middleware.py:158` `require_pipeline_user` → `:165` worker hop 2 → `identity_authority.py:2437-2453` `holds_active_human_role` | 3 statements (identity, roles, DB clock), 1 checkout |
| 8 | route body, e.g. `web/execution/routes.py:1306` `get_run_status` → `:578` `_verify_run_ownership` (`get_run` `:590`, `get_session` `:595`) → `:641` `_load_run_status_snapshot_with_accounting` (`get_run` again at `:650`) | ≥3 more reads (**enter sessions lane**; internals UNVERIFIED) |

Count (how: one row per `conn.execute`/`exec_driver_sql` call read in the cited functions): **7 SQL statements, 4 connection acquisitions, 2 worker-thread hops before the route body runs**; the same identity row is read twice (hops 4 and 7). With `pool_pre_ping` (mentioned in the comment at `local.py:1304`; engine configuration UNVERIFIED) each checkout adds a ping. SSO provider path (`web/auth/sso.py:1420`) not read: UNVERIFIED.

Failure path only: `_record_auth_failure_after_rate_limit` (`middleware.py:62-77`) does an auth rate-limit check and a durable audit write. Not on the common path.

#### E.3 Concurrency and chokepoint

- All sync DB work funnels through one process-wide `ThreadPoolExecutor(max_workers=16)` with bounded admission (`async_workers.py:37,42,47-48`: capacity `MAX_WORKERS + MAX_QUEUED`, 1.0 s wait, 20 ms poll at `:625`). Auth hops, websocket polls, lease renewals (`run_stream_read_in_worker`, `:653`) and composer reads share it. Sixteen threads is the request-level concurrency ceiling; saturation surfaces as `AsyncWorkerAdmissionTimeoutError` → 503 (e.g. `rate_limit.py` `SharedRateLimiter.check`).
- Session-operation mutations serialise per session on the session row lock (`repository.py:5250-5251`).
- `web/coordination/` as a whole (19k lines) is mostly per-operation authority code, not per-request. The per-request slice is `identity_authority.py:2437-2505` and `auth_session_authority.py:48-62`. `sqlite_authority.py`, `mutation_connection_registry.py`, `database_clock.py` were not read: UNVERIFIED.
- Audit access logging (`audit_access_log_authority.py`): only on audit-grade transcript reads (error handler text at `web/app.py:1925-1929`). Not per request; internals UNVERIFIED.

---

### D. Web run-execution trunk

#### D.1 Launch

- `web/execution/service.py:2126` `execute` → `:2107` `executor.submit(...)` → `:3460` `_run_pipeline` on a **`ThreadPoolExecutor(max_workers=1)`** (`:1199`, docstring `:1099-1105`). In-process thread, not a subprocess. One web run executes at a time per web process; a second run queues behind it.
- Inside `_run_pipeline`, per run: payload store setup and blob staging (`:4171-4193`), `RateLimitRegistry`, telemetry manager, checkpoint manager, `Orchestrator` construction (`:4227-4264`), then the engine (**enters engine lane**).
- Every sessions-DB call from the pipeline thread crosses to the event loop via `_call_async` (`:1703-1715`): `run_coroutine_threadsafe(...).result(timeout=30.0)`. Two thread hops per call (pipeline thread → loop → usually a worker thread for the SQL).

#### D.2 Progress production

- Engine emits `ProgressEvent` every 100 rows or 5.0 s (`engine/orchestrator/source_iteration.py:99-100,306-318`; drain loop `engine/orchestrator/leader_drain.py:390-396` uses 100 rows / 1.0 s).
- Per event: `service.py:5163` `_broadcast_progress_event` → `:5183-5205` `_persist_and_broadcast_run_event`: lease CAS (`:5193`), `append_run_event` through `_call_async` (`:5194-5202`, **enters sessions lane**), lease CAS again (`:5204`), then `ProgressBroadcaster.broadcast` (`web/execution/progress.py:145`, bounded per-subscriber queue, drop-oldest at `:249-287`).
- About 3 write transactions per progress event, at most roughly one event per 100 rows. Warm, not hot. The per-LLM-call governance in A.4 dominates this trunk.

#### D.3 Progress delivery (two modes, chosen at `web/app.py:2345-2364`)

**SQLite / single process** (`run_progress_reader is None`): `routes.py:1620` handler subscribes to the in-memory queue. Loop at `:1772-1845`: per iteration `_run_stream_authorized` (`:2119-2130`, 2 worker hops, same statements as E.2 hops 4-7 minus JWT decode), then `queue.get()` with a 1.0 s timeout (`:1777`); on timeout it reloads the run status snapshot (`:1784`); per delivered event another `_run_stream_authorized` (`:1835`) then `send_json`. Idle cost: about 7 statements + a `get_run` per second per socket.

**PostgreSQL / multi-replica** (`RepositoryRunProgressReader`): `routes.py:2135` → `:2166` `_poll_durable_run_progress`, per tick:

1. `:2179` `_run_stream_authorized` — 2 worker hops, about 7 statements.
2. `:2182` `reader.read_after` (`web/execution/run_progress_reader.py:25-70`): owner check (`:31`), identity check (`:46`), count/min/max (`:57`), page SELECT limit 256 (`:66`) — 4 statements.
3. per record: `_run_stream_authorized` again (`:2189`) then `send_json`.
4. when no records: `:2202` status snapshot (`get_run`; accounting only when terminal, `:655`).
5. `:2223` `asyncio.sleep(0.25)`.

Tick interval is the literal `0.25` at `routes.py:2223` (no named constant). Per open socket that is 4 ticks/s × roughly 12 statements ≈ 48 statements/s plus 3 worker-pool submissions per tick against the 16-thread pool. A burst page of 256 events triggers 256 extra authorisation round-trips (2 hops each) before the next read.

#### D.4 Other files in this trunk

- `accounting.py` `load_run_accounting_for_settings`: only for terminal runs (`routes.py:655-661`) and results views. Per request, scales with run size; body UNVERIFIED.
- `outputs.py`, `preview.py`: per user click (`routes.py:1881-2100`). Not hot.
- `get_run_status` adds a discard-summary Landscape read for terminal runs (`routes.py:1320-1326`).

---

### C. Startup / build trunk

#### C.1 CLI run (per process)

- Plugin discovery: `plugins/infrastructure/manager.py:334` `get_shared_plugin_manager` (double-checked singleton) → `register_builtin_plugins` (`:94`) → `plugins/infrastructure/discovery.py:294` `discover_all_plugins` → `:60,:85` glob each plugin directory and import every plugin module (`:158-190`). **Per process, once.** Cost is import time of all plugin modules (heavy optional SDKs are imported lazily in places, e.g. `providers/azure.py:332`).
- Settings: `config_loading.py:380` `load_settings` → `Dynaconf(...)` (`:412`), a second raw `yaml.safe_load` of the same file (`:430`), secret/env placeholder screening (`:258`), `ElspethSettings(**raw_config)` (`:476`). Per process.
- Graph: `core/dag/graph.py:825` `from_plugin_instances` → `core/dag/builder.py:319` `build_execution_graph` (node ids from `canonical_json(config)` at `:383`), then `graph.validate()` (`graph.py:398`: cycle check, `nx.descendants` reachability at `:440`) and `validate_edge_compatibility` (`core/dag/schema_validation.py`). Per run. Not per row: the engine precomputes next-node maps once (`engine/orchestrator/graph_wiring.py:194-202`); `get_nx_graph()` copies the graph (`graph.py:157`) but has a single caller (`core/canonical.py:246`).
- `core/expression_parser.py`: `ast.parse` + validation once per expression in `ExpressionParser.__init__` (`:970-996`); `evaluate` (`:1309`) is an AST tree walk per row per gate (called from the engine lane).
- `core/templates.py` (3163 lines): not read; UNVERIFIED. The LLM render path uses `plugins/infrastructure/templates.py` sandboxed environments compiled at transform init (`llm/transform.py:547-551` for per-query overrides).

Verdict: startup is per-process/per-run and not hot for the CLI.

#### C.2 Web validation (per request — hot, rank 6)

`web/execution/validation.py:455` `validate_pipeline` → `:497` `_validate_pipeline_impl`. Phases in order, each wrapped by `_apply_phase`:

1. policy, path, network, resource, secret, semantic, batch checks (`:595-633`)
2. interpretation review (`:637-639`)
3. `materialize_validation_yaml` (`:644-653`) — YAML generation + parse
4. LLM retry-budget, base-URL, tracing, endpoint, provider checks (`:656-675`)
5. `load_runtime_settings` (`:682`; `_validation_runtime.py:179-220` → `config_loading.py:556` `load_settings_from_yaml_string`, full Pydantic `ElspethSettings`)
6. `validate_runtime_plugins` (`:695`; `_validation_runtime.py:232-241`) — **instantiates every plugin**, including each `LLMTransform.__init__` (template compilation, dynamic output schema construction at `llm/transform.py:1646-1704`)
7. value-source compliance (`:703`)
8. when not trained-operator and any plugin is operator-profiled: `_compiled_identity_settings` (`:717`; `:411-441`) — a **second** YAML generation + settings load
9. `validate_graph_structure` (`:733`, graph build + `graph.validate()`), `validate_route_targets` (`:743`), `validate_schema_compatibility` (`:747`)
10. three advisory passes over the state (`:755-769`)

Callers (each a full pass unless cached by the caller): validate route / `ExecutionService` (`web/execution/service.py:1617`, async wrapper at `:3244`), session runtime preflight (`web/app.py:2221`), audit-readiness snapshot (`web/audit_readiness/service.py:567` `validate_state`), and composer preflight (`web/composer/composer_preflight.py:136`, reached via `cached_runtime_preflight` at `composer/service.py:1885`, `planning_application.py:566,587,700`, `tool_batch.py:2845,2894,3429` — **composer lane; its cache was not read**).

Nothing is reused across requests except the plugin class registry. Cost is CPU (Pydantic, Jinja compile, `create_model`-style schema building, networkx) plus secret resolution and blob metadata reads; no network.

---

### B. Source and sink I/O trunks

#### B.1 CSV source (per row)

`plugins/sources/csv_source.py:196` `load` → `:275` `_load_from_file`: `csv.reader` over the open handle (`:294`), header normalisation once per file, then per record: schema `model_validate(row)` (`:594`), `contract.validate(validated_row)` (`:611`), `SourceRow.valid(...)` (`:629`). Pure CPU, single-threaded generator pulled by the engine. Hot only relative to runs with no external calls. JSON, blob, Dataverse, S3 sources not read: UNVERIFIED (expected same per-row validate shape).

#### B.2 CSV sink (per sink effect — rank 7)

Entered from the engine at `engine/executors/sink_effects.py:1481` (`inspect_effect`) and `:1501` (`prepare_effect`); how many effects a sink gets per run is decided in `engine/orchestrator/sink_flush.py:130,304` (**engine lane; effect count per run UNVERIFIED**).

Per effect:

- `inspect_local_effect` (`plugins/sinks/_local_file_effects.py:305`) snapshots the target: full-file SHA-256 (`:275-285`).
- `csv_sink.py:499` `prepare_effect`: `deep_thaw` every member row (`:551`), or `canonical_json(deep_thaw(row))` per row in audit-context mode (`:536`); append mode with an existing file re-reads its header (`:557-558`) and **streams the whole existing file into the staging file** (`:583-584` → `_local_file_effects.py:830-833`); replace mode serialises the full cumulative snapshot (`_local_file_effects.py:823-824`). A new `StringIO` + `csv.DictWriter` is built per row (`csv_sink.py:605-606`) and each row is encoded twice (`:610` check, `:635` emit).
- `prepare_local_effect` (`_local_file_effects.py:465`): SHA-256 over all chunks (`:507-512`), `fsync` (`:524`), `os.replace` to staging (`:525`), directory `fsync` (`:530`).
- `commit_local_effect` (`:714`): `fsync` (`:743`), `os.replace` (`:744`), directory `fsync` (`:746`).

Each effect is O(bytes written so far), with 2 file fsyncs and at least 2 directory fsyncs. If a sink receives K effects over a run, total I/O is O(K × final size). `json_sink.py`, `text_sink.py`, remote object sinks (`_remote_object_effects.py`) not read: UNVERIFIED. `database_sink.py:771-794` inserts batched, with a per-row fallback insert at `:794`.

---

### G. Read-side audit paths

All on demand, none on the run path.

- Exporter `core/landscape/exporter.py`: batched, not N+1. `iter_rows_for_run(batch_size)` (`:199-200`), then per batch `get_tokens_for_rows`, `get_node_states_for_tokens`, `get_calls_for_states`, `get_routing_events_for_states` etc. (`:202-224`). Scales linearly with run size; per record cost is `canonical_json` + optional HMAC (`:450-462`). Whole-run list reads (`get_all_operation_calls_for_run`, `get_all_batch_members_for_run`, `:187,:144`) hold a full table slice in memory.
- Lineage `core/landscape/lineage.py:203` `explain`: one token, about 10 batched queries (`:245`, `:294`, `:314-338`); explicit N+1 avoidance comment at `:244`; parents hydrated in one `get_tokens_by_ids` (`:121`).
- MCP `mcp/analyzers/reports.py`: aggregate SQL. `get_run_summary` issues about 12 sequential `COUNT`/`AVG` statements over run-scoped tables (`:68-167`). Full scans per call, no N+1.
- Retention `core/retention/purge.py`: chunked `IN` queries (`:118`, `:484`), one payload-store `delete` per ref (`:627-633`) and an `exists` per child ref (`:599`). Linear in refs.
- `web/sessions/audit_story_service.py` (98 lines): about 3 queries per call (`:43-66`).
- `web/audit_readiness/service.py:535` `compute_snapshot`: its cost is the full validation trunk (C.2) at `:567`, plus interpretation-event listings (`:595-604`). Has its own rate-limit bucket (`web/app.py:2354-2357`).
- TUI (`tui/`): interactive; not read. UNVERIFIED, not hot by construction.

**N+1 shapes found in this lane:** none in G. The N+1-like shapes are elsewhere: per-call full-run rescan (A.4), per-event re-authorisation on the websocket (D.3), per-row HTTP client construction (A.5).

---

### F. Coordination / heartbeat / lease loops

| Loop | `path:line` | Interval | Per-tick cost |
|------|-------------|----------|---------------|
| Session-operation lease renewal | `web/coordination/lifecycle.py:901-963` `_renew_forever` | `min(lease_seconds/3, 30.0)` (`:110`, `:214`) | 1 worker hop + 1 lease UPDATE transaction (`authority.renew`); one loop per live composer/execute operation |
| `guard_external_effect` (not a timer; fires before each external effect) | `lifecycle.py:840-859` | per effect | 1 CAS write transaction; multiplied by LLM calls and progress events in web runs (A.4, D.2) |
| Web instance membership heartbeat | `web/coordination/membership_lifecycle.py:171-174` `_heartbeat_loop` | `heartbeat_interval_seconds(lease_seconds)` (`:47`, `:137-140`) | body UNVERIFIED (one membership UPDATE expected) |
| Orphan-run recovery sweep | `web/app.py:438-445` | `interval_seconds` parameter (value UNVERIFIED) | `reconcile_consumed_archives` + recovery classification |
| Operation receipt long-poll | `web/sessions/routes/operation_receipts.py:311` | `_POLL_SECONDS = 0.1` (`:40`) | 1 `get_operation_receipt` read per 100 ms while a client waits (**sessions lane**) |
| Composer watch reads | `web/composer_watch_reads.py:186,228` | 0.25 s | SQL read per tick per open composer stream (**composer lane entry**) |
| Composer async worker scan | `web/sessions/composer_async_worker.py:1348` | `min(scan_interval, 0.05)` | **composer lane** |
| Batch release thread | `plugins/infrastructure/batching/mixin.py:379` | 1.0 s idle wake | condition wait only |
| Engine run heartbeat | `engine/orchestrator/heartbeat.py:243` | engine lane | web consumer is the recovery sweep above |

Lease renewal and membership are low-frequency and not hot. The 0.1 s and 0.25 s polls are hot only in proportion to concurrently waiting clients. Many `asyncio.sleep(0.01)` loops in `lifecycle.py` and `async_workers.py` are shutdown/join paths, not steady state.

---

### H. Frontend (brief)

Source: `web/frontend/src/` (Vite/React, `web/frontend/package.json`).

| Loop | `path:line` | Interval | Server path driven |
|------|-------------|----------|--------------------|
| Composer progress poll | `stores/sessionStore.ts:49,1592` | 1500 ms | composer progress read (composer lane) + E.2 auth each time |
| In-flight messages poll | `stores/sessionStore.ts:370,1722` | 1500 ms | sessions read + E.2 auth |
| Run recovery poll after stream end | `stores/executionStore.ts:232,1108` | 3000 ms (stale guard 30 s at `:246`) | `GET /api/runs/{id}` (E.2 + D.4) |
| Run websocket reconnect | `api/websocket.ts:104,107,196` | 1 s backoff to 30 s | each reconnect mints a ticket (`routes.py:2104`) then full stream setup |
| Mailbox poll | `stores/mailboxStore.ts:6,197` | 30 s | cheap |
| Health check | `App.tsx:78,560` | 30 s | unauthenticated |

While a composer turn is in flight a tab issues two authenticated requests every 1.5 s, each paying the 7-statement auth dependency. The run websocket's server-side poll (D.3) is the larger multiplier.

---

### Rejected as not hot

- **ASGI middleware stack** (`web/middleware/*`): header and body-size work only, no I/O.
- **Plugin discovery / pluggy registration**: process-wide singleton (`manager.py:334-362`); not repeated per request or per run.
- **DAG construction and validation for the row path**: per run; engine precomputes traversal maps.
- **`core/landscape` exporter, lineage `explain`, MCP analyzers, retention purge**: on demand, batched or aggregate SQL, no N+1.
- **Run progress event persistence**: at most one event per 100 rows.
- **Lease renewal and membership heartbeat timers**: ≥ seconds apart, one statement each.
- **`web/execution/outputs.py`, `preview.py`, `accounting.py`**: per user click or terminal-state only.
- **Auth failure auditing / audit access log**: off the common path.
- **Dynaconf settings load**: once per CLI process.
- **`cli_plugins.py`**: catalog listing commands only.

---

### Existing perf evidence in-tree

Counts are `grep -c 'def test_'` per file (source-text count, parametrisation not expanded; no venv to collect with pytest).

| File | Tests | Covers | Relevance here |
|------|-------|--------|----------------|
| `tests/performance/stress/test_llm_retry.py` | 19 | LLM transform against a ChaosLLM HTTP server (retry, pooling) | Path A, the only end-to-end evidence |
| `tests/performance/stress/test_rate_limiter.py` | 3 | `RateLimiter` under concurrent load | rank 9 |
| `tests/performance/stress/test_call_index_concurrency.py` | 4 | `allocate_call_index` thread safety | A.1 hop 13 |
| `tests/performance/stress/test_backpressure.py` | 2 | telemetry backpressure | A.1 hop 20 |
| `tests/performance/stress/test_concurrent_sinks.py` | 2 | concurrent sink writes | B.2 |
| `tests/performance/scalability/test_large_datasets.py`, `test_many_sinks.py`, `test_deep_dag.py`, `test_wide_rows.py` | 3 each | engine scaling | B and C indirectly |
| `tests/performance/benchmarks/test_schema_validation.py` | 3 | schema validation pipeline | B.1, C |
| `tests/performance/benchmarks/test_canonical_hashing.py` | 4 | canonical JSON hashing | the hashes in A.1 hops 8 and 20 |
| `tests/performance/benchmarks/test_db_write.py`, `test_throughput.py`, `test_token_expansion.py`, `test_cross_check_overhead.py`, `test_validator_walk.py` | 5/4/8/5/1 | engine and Landscape | other lanes |
| `tests/performance/memory/test_leak_detection.py`, `test_memory_baseline.py` | 2/3 | memory | general |
| `evals/composer-battery`, `evals/composer-parity`, `evals/composer-standard-battery`, `evals/lib` | — | composer quality batteries | composer lane, not performance |

Gaps (grep of `tests/performance` for `elspeth.web` returned no files; the same grep matched `PooledExecutor`/`load_settings`-style terms in 6 files, so the instrument was live):

- **No performance test touches `elspeth.web` at all**: nothing covers the auth dependency, the websocket poll, the 16-thread worker pool, web validation latency, or the per-LLM-call governance of A.4.
- No test exercises the O(N²) settlement scan with a large call count.
- No benchmark for sink effect rebuild cost versus output size or effect count.
- No benchmark for `validate_pipeline` end to end.
- AGENTS.md notes xdist disables `pytest-benchmark` and the `performance` marker is deselected by default, so these suites do not run in the default selection.

---

### UNVERIFIED list (not read; do not rely on)

- Bedrock and Gateway provider internals; SSO authenticate path; `sqlite_authority.py`, `mutation_connection_registry.py`, `database_clock.py`.
- Session-service internals behind `get_run`, `get_session`, `append_run_event`, `begin_provider_attempt_on_connection`, `settle_provider_attempt_on_connection`.
- Number of sink effects per sink per run (engine lane).
- Whether any multi-process follower model gives cross-row parallelism (engine lane).
- Composer preflight cache behaviour.
- `core/templates.py`, JSON/blob/DB sources, JSON/text/remote sinks, TUI, `accounting.py` body.
- SQLAlchemy pool settings (`pool_pre_ping`, pool size) for the sessions engine.
- Orphan-recovery sweep interval value.

## Appendix F — Measured profile of the pipeline executor (cProfile + py-spy)

Scope: empirical complement to the static call-path analysis. Everything below is MEASURED unless a line is tagged *(inferred)*.
All artefacts are under `.claude/lanes/hot-path-recon/profile/` (gitignored); no tracked file was modified.

### 0. Machine, tree, environment

- Shared cloud container: `nproc` = 4, 15 GiB RAM, Linux 6.18. Absolute numbers are indicative only; ratios and per-row call counts are the durable result.
- Tree: branch `claude/admiring-bardeen-oj57gv`, HEAD `1a7ff0e6`. CPython 3.13.18 (uv-managed `.venv`).
- Install (the command CONTRIBUTING.md line 12 documents): `uv sync --frozen --all-extras` -> exit 0 in 57 s (log `profile/uv-sync.log`). Additionally `uv pip install --python .venv/bin/python py-spy` (py-spy 0.4.3; not in the lock, venv only).
- `python -c "import elspeth; print(elspeth.__file__)"` -> `<repo>/src/elspeth/__init__.py` (this checkout).
- Audit DB: file-backed SQLite, `journal_mode=wal` (read back from the produced DB), `synchronous=NORMAL` per `core/landscape/database.py:102-103`. Payload store defaulted to `<repo>/.elspeth/payloads` (gitignored; I deleted that directory afterwards — the pre-existing `.elspeth/rotations.log` was left alone).

### 1. Workloads (no network, no keys)

Each is a copy of a shipped example's `settings.yaml` with every `examples/<name>/` path repointed into `profile/work/<workload>_<n>/` (script: `profile/setup.py`), and a generated `input.csv` with the example's columns.

| id | copied from | shape | sizes run |
|----|-------------|-------|-----------|
| gate | `examples/large_scale_test` | csv source (fixed schema) -> 1 config gate -> 2 csv sinks; checkpoint every 100 | 10 / 200 / 2000 |
| transform | `examples/transform_pipeline` | csv (observed) -> `type_coerce` -> `value_transform` -> csv sink | 10 / 200 / 2000 |
| batch | `examples/batch_aggregation` | csv -> `batch_stats` aggregation (count=5, output_mode transform) -> csv sink | 10 / 200 / 2000 |
| fork | `examples/fork_coalesce` | csv -> `truncate` -> fork gate (2 branches) -> coalesce (require_all, nested) -> gate -> jsonl sink | 10 / 200 / 600 |

Input size deviates from the brief's 5k-20k rows on purpose: the brief's governing constraint was "a run takes roughly 10-90 s", and measured throughput is ~33 rows/s (fork: ~8 rows/s), so 2000 rows (fork: 600) is what lands at 60-80 s unprofiled. All runs exited 0 with the expected row counts (`profile/plain-runs.log`, `profile/prof-runs.log`).

Not run: a ChaosLLM/LLM-shaped workload (skipped for time; it needs a side server + fingerprint key and its hot path is HTTP wait, which cProfile on the main thread would not attribute anyway).

Commands (driver `profile/run.sh <workload>_<n> plain|prof`, run from repo root):

```
plain: .venv/bin/elspeth run --settings <dir>/settings.yaml --execute
prof : .venv/bin/python -m cProfile -o <dir>/run.prof -m elspeth.cli run --settings <dir>/settings.yaml --execute
pyspy: .venv/bin/py-spy record --format raw --rate 100 --threads -o profile/pyspy-gate_2000.raw -- .venv/bin/python -m elspeth.cli run --settings <dir>/settings.yaml --execute
```

### 2. Wall time, throughput, fixed vs marginal cost (UNPROFILED runs, bash `time`)

Raw (`real` / `user` / `sys` seconds):

```

gate_10 plain real=6.970 user=6.918 sys=0.267
gate_10 plain exit=0 :: ✓ Run COMPLETED: 10 rows processed | ✓10 succeeded | ✗0 failed | ⚠0 quarantined | →10 routed (high_value:7, output:3) | 0.51s total
gate_200 plain real=11.844 user=11.510 sys=0.395
gate_200 plain exit=0 :: ✓ Run COMPLETED: 200 rows processed | ✓200 succeeded | ✗0 failed | ⚠0 quarantined | →200 routed (output:100, high_value:100) | 5.24s total
gate_2000 plain real=59.835 user=52.975 sys=2.423
gate_2000 plain exit=0 :: ✓ Run COMPLETED: 2,000 rows processed | ✓2,000 succeeded | ✗0 failed | ⚠0 quarantined | →2,000 routed (high_value:1007, output:993) | 52.79s total
transform_10 plain real=7.281 user=7.143 sys=0.376
transform_10 plain exit=0 :: ✓ Run COMPLETED: 10 rows processed | ✓10 succeeded | ✗0 failed | ⚠0 quarantined | 0.54s total
transform_200 plain real=12.678 user=11.867 sys=0.537
transform_200 plain exit=0 :: ✓ Run COMPLETED: 200 rows processed | ✓200 succeeded | ✗0 failed | ⚠0 quarantined | 6.09s total
transform_2000 plain real=66.650 user=58.303 sys=2.995
transform_2000 plain exit=0 :: ✓ Run COMPLETED: 2,000 rows processed | ✓2,000 succeeded | ✗0 failed | ⚠0 quarantined | 60.45s total
batch_10 plain real=7.033 user=6.964 sys=0.363
batch_10 plain exit=0 :: ✓ Run COMPLETED: 10 rows processed | ✓6 succeeded | ✗0 failed | ⚠0 quarantined | 0.52s total
batch_200 plain real=13.424 user=12.556 sys=0.653
batch_200 plain exit=0 :: ✓ Run COMPLETED: 200 rows processed | ✓106 succeeded | ✗0 failed | ⚠0 quarantined | 5.64s total
batch_2000 plain real=66.319 user=57.088 sys=3.035
batch_2000 plain exit=0 :: ✓ Run COMPLETED: 2,000 rows processed | ✓1,021 succeeded | ✗0 failed | ⚠0 quarantined | 59.84s total
fork_10 plain real=8.049 user=7.899 sys=0.338
fork_10 plain exit=0 :: ✓ Run COMPLETED: 10 rows processed | ✓10 succeeded | ✗0 failed | ⚠0 quarantined | →10 routed (output:10) | 1.30s total
fork_200 plain real=27.442 user=25.212 sys=1.021
fork_200 plain exit=0 :: ✓ Run COMPLETED: 200 rows processed | ✓200 succeeded | ✗0 failed | ⚠0 quarantined | →200 routed (output:200) | 20.97s total
fork_600 plain real=81.903 user=72.085 sys=3.178
fork_600 plain exit=0 :: ✓ Run COMPLETED: 600 rows processed | ✓600 succeeded | ✗0 failed | ⚠0 quarantined | →600 routed (output:600) | 74.43s total
```

Derived (marginal = (T_big - T_10) / (N_big - 10); fixed = T_10 - 10 x marginal):

| workload | big N | wall big | overall rows/s | marginal ms/row (10->big) | marginal rows/s | 10->200 ms/row | 200->big ms/row | fixed startup s |
|---|---|---|---|---|---|---|---|---|
| gate | 2000 | 59.84 s | 2000/59.84 = 33.4 | (59.835-6.970)/1990 = 26.57 | 37.6 | (11.844-6.970)/190 = 25.65 | (59.835-11.844)/1800 = 26.66 | 6.970-0.266 = 6.70 |
| transform | 2000 | 66.65 s | 30.0 | (66.650-7.281)/1990 = 29.83 | 33.5 | 28.41 | 29.98 | 6.98 |
| batch | 2000 | 66.32 s | 30.2 | (66.319-7.033)/1990 = 29.79 | 33.6 | 33.64 | 29.39 | 6.74 |
| fork | 600 | 81.90 s | 600/81.90 = 7.3 | (81.903-8.049)/590 = 125.18 | 8.0 | (27.442-8.049)/190 = 102.07 | (81.903-27.442)/400 = 136.15 | 6.80 |

Observations (measured):
- The run is CPU-bound in one thread: user+sys is 92-99% of wall in every run (lowest: fork_600 at 75.26/81.90 = 91.9%) (e.g. gate_2000: 52.98+2.42 = 55.40 of 59.84 s). It is not waiting on fsync.
- Fixed startup is ~6.7-7.0 s regardless of workload; ~27-30 ms of CPU per source row for a 1-3 node linear pipeline; ~125 ms per source row for fork/coalesce (4 tokens per source row).
- fork is visibly super-linear between the two segments (102 -> 136 ms/row); the others are flat within noise. See section 6 for one measured quadratic term.
- The in-tree benchmark `test_throughput_1000_rows_passthrough` took 17.12 s call time (section 8), i.e. the same order (~58 rows/s) with an in-memory setup — the per-row cost is not an artefact of my scratch configs.

Startup composition (measured): `python -c "import elspeth.cli"` = 1.51 s wall; under cProfile on gate_10, `importlib._find_and_load` cumulative = 8.66 s of 11.97 s total (4708 calls) — i.e. ~70% of the fixed cost is module import (much of it lazy imports inside `cli.run` / `_execute_pipeline_with_instances`, incl. pandas 0.2 s and pydantic model class construction), `load_settings` 0.33 s, DB connect/schema 0.44 s (CLI's own `[DATABASE]` line), `ExecutionGraph.from_plugin_instances` 0.01 s, plugin instantiation 0.01 s. `-X importtime` log: `profile/importtime.log`.

### 3. What cProfile covers and what it does not

- cProfile instruments only the thread it is enabled on (MainThread). Threads that exist during these runs: `run-heartbeat:<run>` (seen by py-spy: 263 of 6534 samples, 260 of them parked in `Condition.wait`), plus the idle-timeout pump / sink-effect threads defined in `engine/orchestrator/idle_timeout_pump.py`, `engine/executors/sink_effects.py`. The py-spy `--threads` sample shows 6271/6534 samples (96.0%) on MainThread and essentially zero CPU work elsewhere, so main-thread cProfile covers the row loop for these workloads. It would NOT cover pooled LLM/HTTP transforms (`plugins/infrastructure/pooling/executor.py`, `batching/mixin.py` use ThreadPoolExecutor) — none of my workloads use those.
- cProfile overhead is large and non-uniform: wall inflation gate 109.6/59.8 = 1.83x, transform 136.8/66.7 = 2.05x, batch 121.9/66.3 = 1.84x, fork 136.2/81.9 = 1.66x. It over-weights code made of many tiny Python calls (SQLAlchemy expression construction, rfc8785) relative to C calls (sqlite3). The py-spy sample (section 5, ~no overhead) is the correction: use cProfile for call COUNTS and ranking, py-spy for time SHARES.

### 4. Bucket split (cProfile, self time attributed by module path)

Method (`profile/analyze.py`): each function's `tottime` goes to the bucket of its file; generic C builtins (`isinstance`, `dict.get`, `BytesIO.write`, ...) are redistributed to the buckets of their callers in proportion to the per-caller time pstats records; sqlite3 C methods -> landscape, hashlib -> canonical. `elspeth/contracts/hashing.py` (the rfc8785 wrapper) is counted under canonical+hash, not contracts. The "marg" columns are profile(big) minus profile(10 rows), i.e. the per-row loop with startup subtracted — that is the split to read. "startup" in the marginal column is mostly stdlib helpers (`typing`, `re`, `enum`, `dataclasses`, `functools`) used per row, not real startup.

```
## gate: profiled total tottime small(n=10)=11.94s big(n=2000)=105.81s; marginal=93.87s over 1990 rows = 47.17 ms/row (under cProfile)
bucket                              small s  small%    big s    big%   marg s   marg%  ms/row
landscape/audit DB                     1.14    9.6%    70.16   66.3%    69.02   73.5%  34.684
canonical+hash                         0.10    0.9%    10.88   10.3%    10.78   11.5%   5.419
payload/file IO                        0.31    2.6%     3.11    2.9%     2.80    3.0%   1.407
engine                                 0.02    0.2%     1.16    1.1%     1.14    1.2%   0.573
contracts                              0.58    4.9%     3.46    3.3%     2.88    3.1%   1.446
plugins                                0.04    0.3%     0.20    0.2%     0.16    0.2%   0.081
telemetry/logging                      0.02    0.2%     0.04    0.0%     0.02    0.0%   0.010
startup(import/config/DAG/CLI)         5.13   43.0%     9.49    9.0%     4.37    4.7%   2.194
pydantic                               2.73   22.9%     2.93    2.8%     0.20    0.2%   0.099
other                                  1.86   15.6%     4.36    4.1%     2.50    2.7%   1.257
  elspeth.core.landscape (self)        0.07             6.23             6.16    6.6%   3.094
  sqlalchemy (self)                    0.95            53.96            53.01   56.5%  26.637
  sqlite3 C calls                      0.11             9.97             9.86   10.5%   4.954

```

```
## transform: profiled total tottime small(n=10)=10.82s big(n=2000)=132.52s; marginal=121.70s over 1990 rows = 61.15 ms/row (under cProfile)
bucket                              small s  small%    big s    big%   marg s   marg%  ms/row
landscape/audit DB                     0.92    8.5%    77.70   58.6%    76.79   63.1%  38.586
canonical+hash                         0.13    1.2%    19.80   14.9%    19.68   16.2%   9.889
payload/file IO                        0.29    2.6%     1.84    1.4%     1.56    1.3%   0.781
engine                                 0.02    0.2%     2.12    1.6%     2.11    1.7%   1.058
contracts                              0.55    5.0%     8.98    6.8%     8.43    6.9%   4.237
plugins                                0.04    0.4%     1.62    1.2%     1.57    1.3%   0.790
telemetry/logging                      0.02    0.2%     0.04    0.0%     0.02    0.0%   0.012
startup(import/config/DAG/CLI)         4.77   44.1%    11.05    8.3%     6.28    5.2%   3.157
pydantic                               2.43   22.5%     3.05    2.3%     0.62    0.5%   0.313
other                                  1.67   15.4%     6.30    4.8%     4.64    3.8%   2.330
  elspeth.core.landscape (self)        0.05             7.40             7.35    6.0%   3.691
  sqlalchemy (self)                    0.78            61.31            60.53   49.7%  30.418
  sqlite3 C calls                      0.08             8.99             8.91    7.3%   4.477

```

```
## batch: profiled total tottime small(n=10)=11.76s big(n=2000)=117.89s; marginal=106.13s over 1990 rows = 53.33 ms/row (under cProfile)
bucket                              small s  small%    big s    big%   marg s   marg%  ms/row
landscape/audit DB                     1.31   11.1%    82.26   69.8%    80.95   76.3%  40.679
canonical+hash                         0.10    0.8%    11.63    9.9%    11.53   10.9%   5.794
payload/file IO                        0.29    2.4%     2.24    1.9%     1.95    1.8%   0.981
engine                                 0.02    0.1%     1.37    1.2%     1.35    1.3%   0.679
contracts                              0.57    4.8%     2.94    2.5%     2.37    2.2%   1.193
plugins                                0.04    0.3%     0.35    0.3%     0.31    0.3%   0.157
telemetry/logging                      0.02    0.2%     0.04    0.0%     0.02    0.0%   0.010
startup(import/config/DAG/CLI)         5.04   42.8%     9.37    7.9%     4.34    4.1%   2.179
pydantic                               2.60   22.1%     2.86    2.4%     0.26    0.2%   0.130
other                                  1.80   15.3%     4.83    4.1%     3.04    2.9%   1.527
  elspeth.core.landscape (self)        0.06             6.76             6.70    6.3%   3.367
  sqlalchemy (self)                    1.15            63.94            62.78   59.2%  31.549
  sqlite3 C calls                      0.09            11.56            11.47   10.8%   5.763

```

```
## fork: profiled total tottime small(n=10)=12.99s big(n=600)=132.07s; marginal=119.08s over 590 rows = 201.82 ms/row (under cProfile)
bucket                              small s  small%    big s    big%   marg s   marg%  ms/row
landscape/audit DB                     2.49   19.2%    96.98   73.4%    94.48   79.3% 160.143
canonical+hash                         0.22    1.7%    11.11    8.4%    10.89    9.1%  18.457
payload/file IO                        0.30    2.3%     1.95    1.5%     1.66    1.4%   2.805
engine                                 0.04    0.3%     1.41    1.1%     1.38    1.2%   2.332
contracts                              0.57    4.4%     2.80    2.1%     2.23    1.9%   3.780
plugins                                0.04    0.3%     0.19    0.1%     0.15    0.1%   0.254
telemetry/logging                      0.02    0.2%     0.06    0.0%     0.03    0.0%   0.057
startup(import/config/DAG/CLI)         4.98   38.3%     9.61    7.3%     4.63    3.9%   7.852
pydantic                               2.59   19.9%     2.66    2.0%     0.07    0.1%   0.126
other                                  1.75   13.5%     5.30    4.0%     3.55    3.0%   6.018
  elspeth.core.landscape (self)        0.16             7.27             7.12    6.0%  12.062
  sqlalchemy (self)                    2.09            74.21            72.12   60.6% 122.241
  sqlite3 C calls                      0.25            15.50            15.25   12.8%  25.840

```

Marginal (per-row) split, summarised — cProfile self-time %, all four workloads:

| bucket | gate | transform | batch | fork |
|---|---|---|---|---|
| Landscape/audit DB (elspeth.core.landscape + sqlalchemy + sqlite3) | 73.5 | 63.1 | 76.3 | 79.3 |
| ... of which SQLAlchemy Python self time | 56.5 | 49.7 | 59.2 | 60.6 |
| ... of which sqlite3 C calls (execute/commit/fetch) | 10.5 | 7.3 | 10.8 | 12.8 |
| ... of which elspeth.core.landscape own code | 6.6 | 6.0 | 6.3 | 6.0 |
| canonical JSON + hashing (core/canonical, contracts/hashing, rfc8785, hashlib) | 11.5 | 16.2 | 10.9 | 9.1 |
| payload store / file I/O (payload_store, pathlib/posixpath, csv, open) | 3.0 | 1.3 | 1.8 | 1.4 |
| engine control flow (elspeth/engine) | 1.2 | 1.7 | 1.3 | 1.2 |
| contracts / row objects (elspeth/contracts minus hashing) | 3.1 | 6.9 | 2.2 | 1.9 |
| plugin code (elspeth/plugins) | 0.2 | 1.3 | 0.3 | 0.1 |
| telemetry/logging (structlog, opentelemetry, elspeth/telemetry) | 0.0 | 0.0 | 0.0 | 0.0 |
| pydantic | 0.2 | 0.5 | 0.2 | 0.1 |
| stdlib helpers (typing/re/enum/dataclasses..., labelled startup) | 4.7 | 5.2 | 4.1 | 3.9 |
| other | 2.7 | 3.8 | 2.9 | 3.0 |

Whole-run split at the 10-row size (i.e. fixed cost, gate_10, 11.94 s profiled): import/config/CLI 43.0%, pydantic 22.9% (model class construction at import), other 15.6%, landscape 9.6%, contracts 4.9%, payload/file 2.6%, canonical 0.9%.

### 5. py-spy cross-check (sampling, low overhead) — gate, 2000 rows

py-spy 0.4.3 ran fine in this container (no ptrace problem when it launches the child itself). 6534 samples @100 Hz, 0 errors. Speedscope format was not produced; I recorded `--format raw` (collapsed stacks, `profile/pyspy-gate_2000.raw`) because it is trivially bucketable. Classification = nearest classifiable frame walking up from the leaf (`profile/pyspy_analyze.py`). "in-run" = samples whose stack contains `Orchestrator._execute_run` (5502 samples = 55 s).

```

total samples 6534
threads: {'thread (12210): MainThread': 6271, 'thread (12218): run-heartbeat:b77f3671': 263}

self-ish bucket (nearest classifiable frame from leaf), all samples / samples inside Orchestrator._execute_run (n=5502):
  landscape/audit DB                         4667  71.4%   in-run   4667  84.8%
  startup                                     524   8.0%   in-run      0   0.0%
  canonical+hash                              355   5.4%   in-run    354   6.4%
  startup-phase: engine                       260   4.0%   in-run      0   0.0%
  startup-phase: other                        165   2.5%   in-run      0   0.0%
  payload/file IO                             145   2.2%   in-run    145   2.6%
  contracts                                   142   2.2%   in-run    142   2.6%
  engine                                      109   1.7%   in-run    109   2.0%
  elspeth other                                59   0.9%   in-run     59   1.1%
  startup-phase: contracts                     45   0.7%   in-run      0   0.0%
  startup-phase: landscape/audit DB            21   0.3%   in-run      0   0.0%
  pydantic                                     14   0.2%   in-run     14   0.3%
  plugins                                      11   0.2%   in-run     11   0.2%
  startup/config/CLI                           10   0.2%   in-run      0   0.0%
  startup-phase: plugins                        6   0.1%   in-run      0   0.0%
  telemetry/logging                             1   0.0%   in-run      1   0.0%

inclusive (stack contains):
  sqlalchemy (any frame)                                                        4196  64.2% of all,  76.3% of in-run
  elspeth.core.landscape (any frame)                                            5272  80.7% of all,  95.8% of in-run
  canonical/rfc8785/contracts.hashing (any frame)                                398   6.1% of all,   7.2% of in-run
  process_row                                                                   4351  66.6% of all,  79.1% of in-run
    _ingest_source_row_with_initial_claim                                       2253  34.5% of all,  40.9% of in-run
    _drain_work_queue                                                           2032  31.1% of all,  36.9% of in-run
  flush_and_write_sinks                                                         1092  16.7% of all,  19.8% of in-run
  run_main_processing_loop                                                      4406  67.4% of all,  80.1% of in-run
  payload_store                                                                  145   2.2% of all,   2.6% of in-run
  checkpoint                                                                     108   1.7% of all,   2.0% of in-run
  SA statement build+cache key (_gen_cache_key/_generate_cache_key any frame)    579   8.9% of all,  10.5% of in-run
  SA _compile_w_cache                                                            887  13.6% of all,  16.1% of in-run
  SA do_execute (sqlite3 execute)                                                639   9.8% of all,  11.6% of in-run
  SA do_commit                                                                   272   4.2% of all,   4.9% of in-run
  elspeth/contracts (any frame)                                                  647   9.9% of all,  11.8% of in-run
  pydantic (any frame)                                                           206   3.2% of all,   3.7% of in-run

top 25 leaf frames:
     639   9.8%  DefaultDialect.do_execute (sqlalchemy/engine/default.py:952)
     260   4.0%  Condition.wait (threading.py:363)
     217   3.3%  DefaultDialect.do_commit (sqlalchemy/engine/default.py:715)
     165   2.5%  thread (12210): MainThread
      76   1.2%  sub (re/__init__.py:208)
      62   0.9%  _compile_bytecode (<frozen importlib._bootstrap_external>:781)
      61   0.9%  CursorResult.__init__ (sqlalchemy/engine/cursor.py:1529)
      59   0.9%  LRUCache.get (sqlalchemy/util/_collections.py:531)
      58   0.9%  expect (sqlalchemy/sql/coercions.py:396)
      58   0.9%  _serialize_str (rfc8785/_impl.py:91)
      50   0.8%  HasCacheKey._gen_cache_key (sqlalchemy/sql/cache_key.py:246)
      47   0.7%  _anonymous_label.safe_construct (sqlalchemy/sql/elements.py:5551)
      45   0.7%  realpath (<frozen posixpath>:451)
      44   0.7%  expect (sqlalchemy/sql/coercions.py:415)
      39   0.6%  expect (sqlalchemy/sql/coercions.py:417)
      39   0.6%  SinkEffectReservation._lock_and_validate_pipeline_witness.<locals>.<genexpr> (elspeth/core/landscape/execution/sink_effect_reservation.py:370)
      34   0.5%  BindParameter._gen_cache_key (sqlalchemy/sql/elements.py:2180)
      32   0.5%  _FuncBuilder.add_fns_to_class (dataclasses.py:498)
      32   0.5%  HasCacheKey._gen_cache_key (sqlalchemy/sql/cache_key.py:272)
      32   0.5%  SinkEffectReservation._lock_and_validate_pipeline_witness.<locals>.<genexpr> (elspeth/core/landscape/execution/sink_effect_reservation.py:358)
      29   0.4%  JSONEncoder.iterencode (json/encoder.py:263)
      29   0.4%  _WeakValueDictionary.__init__.<locals>.KeyedRef.__new__ (<frozen importlib._bootstrap>:75)
      28   0.4%  CursorFetchStrategy.fetchone (sqlalchemy/engine/cursor.py:1166)
      26   0.4%  Connection._execute_clauseelement (sqlalchemy/engine/base.py:1599)
      25   0.4%  get_cls_kwargs (sqlalchemy/util/langhelpers.py:527)
```

Reading: the sampling profiler agrees with and sharpens the cProfile ranking — inside the run, 84.8% of main-thread samples are in audit-DB code by nearest frame, 95.8% have an `elspeth.core.landscape` frame somewhere on the stack, 76.3% have a SQLAlchemy frame, and only 11.6% + 4.9% = 16.5% are inside the actual sqlite3 `execute`/`commit` C calls. So roughly 60% of in-run wall time is SQLAlchemy Core Python overhead around the SQL (statement construction, coercion, cache-key generation, compile-cache lookup, execution-context and result-proxy setup), not SQLite itself. Canonical/hash is 6.4-7.2% (cProfile said 11.5% — the expected over-weighting of rfc8785's tiny calls). Engine own frames 2.0%, plugins 0.2%, telemetry 0.0%.
Phase split of in-run samples: `process_row` 79.1% (source ingest + initial scheduler claim 40.9%, drain work queue / gate execution 36.9%), `flush_and_write_sinks` 19.8%, checkpoint code 2.0%. (`startup-phase: engine` 260 samples is the heartbeat thread sleeping in `Condition.wait`, not CPU.)

### 6. Per-row call counts (cProfile, exact counts; arithmetic shown)

Counts are (calls at big N - calls at N=10) / (big N - 10). Full table per workload:

**gate**
```
### key per-row counts
sqlite3 Cursor.execute                                  (   164125 -    2014) / 1990 =     81.46 per row; self   2947.3 us/row
sqlite3 Connection.commit                               (    20199 -     209) / 1990 =     10.05 per row; self   1631.8 us/row
sqlite3 executemany                                     (       42 -      12) / 1990 =      0.02 per row; self     59.2 us/row
sqlite3 fetch*                                          (   109179 -    1192) / 1990 =     54.26 per row; self    216.6 us/row
SA Connection.execute                                   (   125263 -    1375) / 1990 =     62.26 per row; self     88.5 us/row
SA _execute_clauseelement                               (   141682 -    1527) / 1990 =     70.43 per row; self    601.1 us/row
SA begin (Engine.begin)                                 (    40348 -     421) / 1990 =     20.06 per row; self     35.5 us/row
SA SQLCompiler.__init__ (stmt compiles = cache misses)  (     8705 -     741) / 1990 =      4.00 per row; self     34.8 us/row
SA _gen_cache_key                                       (  1731155 -   17376) / 1990 =    861.20 per row; self   2407.8 us/row
canonical_json                                          (    32028 -     188) / 1990 =     16.00 per row; self     21.7 us/row
stable_hash                                             (    24118 -     138) / 1990 =     12.05 per row; self     20.7 us/row
_normalize_value                                        (   269634 -    1834) / 1990 =    134.57 per row; self    139.7 us/row
rfc8785 dumps                                           (   106279 -     709) / 1990 =     53.05 per row; self     70.5 us/row
rfc8785 dump (recursive)                                (   955904 -    7104) / 1990 =    476.78 per row; self   1124.7 us/row
hashlib sha256 ctor                                     (   100651 -    3041) / 1990 =     49.05 per row; self     60.3 us/row
json encoder iterencode                                 (    16933 -    8973) / 1990 =      4.00 per row; self     66.4 us/row
json.loads/decode                                       (    20038 -     138) / 1990 =     10.00 per row; self     25.2 us/row
payload_store.store                                     (     6001 -      31) / 1990 =      3.00 per row; self     27.7 us/row
fsync                                                   (       12 -      12) / 1990 =      0.00 per row; self      0.0 us/row
posix.replace/rename                                    (        4 -       4) / 1990 =      0.00 per row; self      0.0 us/row
io.open                                                 (     8966 -    1006) / 1990 =      4.00 per row; self     31.1 us/row
pydantic validate                                       (     8066 -     106) / 1990 =      4.00 per row; self     37.5 us/row
structlog _proxy_to_logger                              (        2 -       2) / 1990 =      0.00 per row; self      0.0 us/row
uuid4                                                   (    12031 -      71) / 1990 =      6.01 per row; self     20.2 us/row
deepcopy                                                (      625 -     625) / 1990 =      0.00 per row; self      0.0 us/row
isinstance                                              ( 22311435 - 2921160) / 1990 =   9743.86 per row; self   1782.8 us/row
```

**transform**
```
### key per-row counts
sqlite3 Cursor.execute                                  (   181658 -    1790) / 1990 =     90.39 per row; self   2222.5 us/row
sqlite3 Connection.commit                               (    26148 -     205) / 1990 =     13.04 per row; self   1871.6 us/row
sqlite3 executemany                                     (       37 -       6) / 1990 =      0.02 per row; self     61.4 us/row
sqlite3 fetch*                                          (   108925 -     997) / 1990 =     54.24 per row; self    212.5 us/row
SA Connection.execute                                   (   130956 -    1180) / 1990 =     65.21 per row; self     99.2 us/row
SA _execute_clauseelement                               (   153261 -    1300) / 1990 =     76.36 per row; self    634.5 us/row
SA begin (Engine.begin)                                 (    42259 -     359) / 1990 =     21.06 per row; self     34.0 us/row
SA SQLCompiler.__init__ (stmt compiles = cache misses)  (     4687 -     703) / 1990 =      2.00 per row; self     19.6 us/row
SA _gen_cache_key                                       (  1956918 -   15627) / 1990 =    975.52 per row; self   2790.5 us/row
canonical_json                                          (    54024 -     294) / 1990 =     27.00 per row; self     31.8 us/row
stable_hash                                             (    36018 -     198) / 1990 =     18.00 per row; self     30.4 us/row
_normalize_value                                        (   474388 -    2758) / 1990 =    237.00 per row; self    228.9 us/row
rfc8785 dumps                                           (   152118 -     878) / 1990 =     76.00 per row; self    105.7 us/row
rfc8785 dump (recursive)                                (  1793720 -   10680) / 1990 =    896.00 per row; self   2058.1 us/row
hashlib sha256 ctor                                     (   118500 -    3080) / 1990 =     58.00 per row; self     80.8 us/row
json encoder iterencode                                 (    16933 -    8973) / 1990 =      4.00 per row; self     75.9 us/row
json.loads/decode                                       (    24029 -     149) / 1990 =     12.00 per row; self     33.0 us/row
payload_store.store                                     (     4001 -      21) / 1990 =      2.00 per row; self     20.3 us/row
fsync                                                   (        6 -       6) / 1990 =      0.00 per row; self      0.1 us/row
posix.replace/rename                                    (        2 -       2) / 1990 =      0.00 per row; self      0.0 us/row
io.open                                                 (     4959 -     979) / 1990 =      2.00 per row; self     18.3 us/row
pydantic validate                                       (    24073 -     193) / 1990 =     12.00 per row; self     75.4 us/row
structlog _proxy_to_logger                              (        2 -       2) / 1990 =      0.00 per row; self     -0.0 us/row
uuid4                                                   (    16014 -      94) / 1990 =      8.00 per row; self     26.2 us/row
deepcopy                                                (    36657 -     837) / 1990 =     18.00 per row; self     28.9 us/row
isinstance                                              ( 33660202 - 2961557) / 1990 =  15426.45 per row; self   2875.7 us/row
```

**batch**
```
### key per-row counts
sqlite3 Cursor.execute                                  (   195184 -    1848) / 1990 =     97.15 per row; self   3184.1 us/row
sqlite3 Connection.commit                               (    18429 -     165) / 1990 =      9.18 per row; self   2134.7 us/row
sqlite3 executemany                                     (      421 -       8) / 1990 =      0.21 per row; self     70.6 us/row
sqlite3 fetch*                                          (   139054 -    1154) / 1990 =     69.30 per row; self    245.5 us/row
SA Connection.execute                                   (   134744 -    1200) / 1990 =     67.11 per row; self    123.4 us/row
SA _execute_clauseelement                               (   169228 -    1366) / 1990 =     84.35 per row; self    661.0 us/row
SA begin (Engine.begin)                                 (    36710 -     341) / 1990 =     18.28 per row; self     31.0 us/row
SA SQLCompiler.__init__ (stmt compiles = cache misses)  (     4777 -     781) / 1990 =      2.01 per row; self     19.9 us/row
SA _gen_cache_key                                       (  2076328 -   16064) / 1990 =   1035.31 per row; self   3051.7 us/row
canonical_json                                          (    40232 -     232) / 1990 =     20.10 per row; self     28.3 us/row
stable_hash                                             (    15161 -      96) / 1990 =      7.57 per row; self     14.3 us/row
_normalize_value                                        (   330983 -    2183) / 1990 =    165.23 per row; self    146.4 us/row
rfc8785 dumps                                           (    89348 -     590) / 1990 =     44.60 per row; self     64.0 us/row
rfc8785 dump (recursive)                                (  1073619 -    7439) / 1990 =    535.77 per row; self   1222.2 us/row
hashlib sha256 ctor                                     (    80132 -    2916) / 1990 =     38.80 per row; self     59.1 us/row
json encoder iterencode                                 (    21017 -    8997) / 1990 =      6.04 per row; self     87.2 us/row
json.loads/decode                                       (    16199 -     119) / 1990 =      8.08 per row; self     23.6 us/row
payload_store.store                                     (     5064 -      29) / 1990 =      2.53 per row; self     27.1 us/row
fsync                                                   (        6 -       6) / 1990 =      0.00 per row; self      1.4 us/row
posix.replace/rename                                    (        2 -       2) / 1990 =      0.00 per row; self      0.0 us/row
io.open                                                 (     6022 -     987) / 1990 =      2.53 per row; self     22.6 us/row
pydantic validate                                       (    12145 -     125) / 1990 =      6.04 per row; self     42.3 us/row
structlog _proxy_to_logger                              (        2 -       2) / 1990 =      0.00 per row; self     -0.0 us/row
uuid4                                                   (    15294 -      90) / 1990 =      7.64 per row; self     26.8 us/row
deepcopy                                                (    11855 -     690) / 1990 =      5.61 per row; self      8.1 us/row
isinstance                                              ( 24670100 - 2918423) / 1990 =  10930.49 per row; self   2017.5 us/row
```

**fork**
```
### key per-row counts
sqlite3 Cursor.execute                                  (   235200 -    4823) / 590 =    390.47 per row; self  16753.4 us/row
sqlite3 Connection.commit                               (    31905 -     608) / 590 =     53.05 per row; self   7283.6 us/row
sqlite3 executemany                                     (     1815 -      36) / 590 =      3.02 per row; self    405.1 us/row
sqlite3 fetch*                                          (   152453 -    3002) / 590 =    253.31 per row; self    888.3 us/row
SA Connection.execute                                   (   171104 -    3386) / 590 =    284.27 per row; self    418.5 us/row
SA _execute_clauseelement                               (   198220 -    3855) / 590 =    329.43 per row; self   2661.3 us/row
SA begin (Engine.begin)                                 (    67995 -    1287) / 590 =    113.06 per row; self    218.8 us/row
SA SQLCompiler.__init__ (stmt compiles = cache misses)  (     6780 -     876) / 590 =     10.01 per row; self     92.9 us/row
SA _gen_cache_key                                       (  2420718 -   46437) / 590 =   4024.21 per row; self  11499.3 us/row
canonical_json                                          (    31229 -     549) / 590 =     52.00 per row; self     75.1 us/row
stable_hash                                             (    21020 -     370) / 590 =     35.00 per row; self     62.1 us/row
_normalize_value                                        (   317901 -    5791) / 590 =    529.00 per row; self    498.0 us/row
rfc8785 dumps                                           (    76922 -    1402) / 590 =    128.00 per row; self    201.3 us/row
rfc8785 dump (recursive)                                (   965485 -   17945) / 590 =   1606.00 per row; self   3891.0 us/row
hashlib sha256 ctor                                     (    69701 -    3621) / 590 =    112.00 per row; self    190.2 us/row
json encoder iterencode                                 (    18533 -    9093) / 590 =     16.00 per row; self    245.4 us/row
json.loads/decode                                       (    13230 -     250) / 590 =     22.00 per row; self     80.0 us/row
payload_store.store                                     (     3001 -      51) / 590 =      5.00 per row; self     67.3 us/row
fsync                                                   (        6 -       6) / 590 =      0.00 per row; self     -0.3 us/row
posix.replace/rename                                    (        2 -       2) / 590 =      0.00 per row; self      0.0 us/row
io.open                                                 (     5159 -    1029) / 590 =      7.00 per row; self     69.8 us/row
pydantic validate                                       (     4861 -     141) / 590 =      8.00 per row; self     72.6 us/row
structlog _proxy_to_logger                              (        2 -       2) / 590 =      0.00 per row; self      0.0 us/row
uuid4                                                   (    12016 -     216) / 590 =     20.00 per row; self     77.8 us/row
deepcopy                                                (     8462 -     792) / 590 =     13.00 per row; self     28.6 us/row
isinstance                                              ( 22986966 - 3148071) / 590 =  33625.25 per row; self   6702.6 us/row
```

Headline per source row:

| per source row | gate | transform | batch | fork |
|---|---|---|---|---|
| sqlite3 `Cursor.execute` | 81.5 | 90.4 | 97.2 | 390.5 |
| sqlite3 `Connection.commit` | 10.05 | 13.04 | 9.18 | 53.05 |
| SQLAlchemy `Engine.begin` context entries | 20.1 | 21.1 | 18.3 | 113.1 |
| SQLAlchemy `_execute_clauseelement` (Core statements) | 70.4 | 76.4 | 84.4 | 329.4 |
| SQLAlchemy `_gen_cache_key` calls | 861 | 976 | 1035 | 4024 |
| `core.canonical.canonical_json` | 16.0 | 27.0 | 20.1 | 52.0 |
| `core.canonical.stable_hash` | 12.05 | 18.0 | 7.6 | 35.0 |
| `rfc8785.dumps` (all callers, via `contracts/hashing.canonical_json`) | 53.05 | 76.0 | 44.6 | 128.0 |
| sha256 constructions | 49.05 | 58.0 | 38.8 | 112.0 |
| `payload_store.store` (file writes) | 3.0 | 2.0 | 2.5 | 5.0 |
| `uuid4` | 6.0 | 8.0 | 7.6 | 20.0 |
| `isinstance` | 9,744 | 15,426 | 10,930 | 33,625 |
| Python function calls, total (pstats, 200->big segment) | 50,956 | 75,790 | 55,624 | 189,818 |

Context for the SQL count (measured from the produced audit DBs): gate_2000 persisted 26,039 rows across the 50 tables (scheduler_events 8000, node_states 6000, tokens/rows/token_work_items/token_outcomes/sink_effect_members/routing_events 2000 each) = 13.0 audit rows per source row, written with 81.5 statements and 10 commits per source row, i.e. ~6.3 SQL statements and ~0.77 commits per audit row persisted. Statement mix (gate): 54.3 fetches per row vs 81.5 executes — most statements are reads/locks (`SELECT ... FOR UPDATE`-style guards) around the inserts; `executemany` is essentially unused in the row loop (0.02/row).
Commit self time: 1.63 ms/row profiled for 10.05 commits = ~162 us per commit (WAL + synchronous=NORMAL; no fsync showed up per row: `fsync` count is identical at N=10 and N=2000).

Callers (gate_2000, exact pstats caller edges):
- `canonical_json` 32,028 calls: `landscape/scheduler/events.py:83 _event_values` 16,000 (8 per row — one per scheduler event), `canonical.is_non_canonical_number` 6,000, `node_states._materialize_routing_reason_before_insert` 2,000, `data_flow/tokens._prepare_source_row_record` 2,000.
- `stable_hash` 12/row: `record_completed_node_state_on` 4,000, `begin_node_state` 2,000, `begin_node_states_many` 2,000, `_prepare_source_row_record` 2,000 (+ others).
- `Path.resolve` -> `posixpath.realpath`: 20.0 calls/row and 258,113 `lstat` syscalls total (~129 per row), all from `core/payload_store.py:69 _validate_store_directory` (16,001 calls = 8 per row) via `_open_payload_parent`/`_path_for_hash` on every `store` (3/row) and `retrieve` (1/row). 0.43 s self + lstat 0.36 s profiled; 0.7% of py-spy samples sit in `realpath` leaf alone.

#### A measured quadratic term (sink flush)

`core/landscape/execution/sink_effect_reservation.py:358` and `:370` build `tuple(item.token_id for item in locked_tokens)` / `tuple(item.state_id for item in locked_states)` inside the per-member lock loop of `_lock_and_validate_pipeline_witness`, so the genexpr body runs n(n+1)/2 times per sink flush of n members. Measured genexpr iterations (each of the two lines): N=10 -> 44; N=200 -> 10,300; N=2000 -> 1,003,049 (gate: two sinks of ~1000 members: 2 x 1000 x 1001/2 = 1,001,000, matches). Segment-wise this is 116 calls/row at 10->200 but 1,111 calls/row at 200->2000 (`profile/scaling.txt`); transform (one sink of 2000): 2,211 calls/row. Cost at 2000 rows: 0.83 s profiled self time, 71 py-spy samples (1.1%). *(inferred)* at the 10k-row shipped `large_scale_test` this term alone is ~25x larger (~25 M iterations per line per 5000-member sink), and it grows with the number of tokens pending per sink at flush time. The same loop issues one `SELECT ... FOR UPDATE` per member token and one per member state (2 statements per row out of the 81).

Otherwise per-row call counts are constant across segments (sqlite execute 81.38 vs 81.47 calls/row; canonical_json 16.00 vs 16.00), while per-call sqlite time drifts up with DB size (execute self 2,587 -> 2,985 us/row; commit 1,286 -> 1,668 us/row for gate; fork 15.3 -> 17.4 ms/row).

### 7. Top functions

#### 7.1 Marginal top by tottime (startup subtracted), with calls per row — gate

```

### per-row marginal call counts (calls_big - calls_small)/(big-small), top by marginal tottime
marg tottime s  calls/row us/row self marg cum s  function   [calls big - small]
         5.865      81.46      2947.3       5.86  <method 'execute' of 'sqlite3.Cursor' objects>   [164125-2014]
         3.978     675.48      1999.2      12.77  sqlalchemy/sql/coercions.py:314(expect)   [1359085-14879]
         3.560     405.89      1789.1      10.18  sqlalchemy/sql/cache_key.py:221(_gen_cache_key)   [816109-8397]
         3.548    9743.86      1782.8       4.53  <built-in method builtins.isinstance>   [22311435-2921160]
         3.247      10.05      1631.8       3.25  <method 'commit' of 'sqlite3.Connection' objects>   [20199-209]
         2.238     476.78      1124.7       6.98  rfc8785/_impl.py:188(dump)   [955904-7104]
         1.728     751.99       868.2       2.37  sqlalchemy/util/langhelpers.py:505(get_cls_kwargs)   [1509961-13494]
         1.562      70.43       785.0       4.46  sqlalchemy/engine/default.py:1313(_init_compiled)   [141682-1527]
         1.468     608.09       737.7       2.72  rfc8785/_impl.py:79(_serialize_str)   [1219321-9231]
         1.338     282.83       672.2       6.36  sqlalchemy/sql/elements.py:1975(__init__)   [568201-5362]
         1.196      70.43       601.1      35.47  sqlalchemy/engine/base.py:1591(_execute_clauseelement)   [141682-1527]
         1.185      78.40       595.5       1.34  sqlalchemy/sql/compiler.py:1838(construct_params)   [157606-1591]
         1.177     243.77       591.6       4.07  sqlalchemy/sql/elements.py:5535(safe_construct)   [489914-4805]
         1.175      80.48       590.5      12.97  sqlalchemy/engine/base.py:1850(_exec_single_context)   [162155-2009]
         1.129     243.83       567.4       4.21  sqlalchemy/sql/elements.py:2161(_gen_cache_key)   [489909-4695]
         1.121     868.90       563.6       1.22  <method 'sub' of 're.Pattern' objects>   [1746250-17132]
         1.114     476.78       560.0       2.27  elspeth/contracts/hashing.py:38(_normalize_frozen_and_reject_non_finite)   [955904-7104]
         1.072    1093.90       538.5       1.13  <method 'get' of 'dict' objects>   [3047851-870994]
         0.986    2879.71       495.4       0.99  <method 'write' of '_io.BytesIO' objects>   [5774001-43371]
         0.851     115.59       427.7       1.40  sqlalchemy/sql/elements.py:3934(__init__)   [232294-2276]
         0.819    1302.61       411.7       0.94  <built-in method builtins.getattr>   [3296873-704678]
         0.806      80.49       405.1      18.67  sqlalchemy/engine/base.py:1791(_execute_context)   [162189-2013]
         0.692    1138.65       347.8       0.69  <method 'encode' of 'str' objects>   [2289719-23809]
         0.590     115.59       296.5       7.67  sqlalchemy/sql/default_comparator.py:50(_boolean_compare)   [232293-2275]
         0.550      80.49       276.5       4.49  sqlalchemy/engine/default.py:1850(_setup_result_proxy)   [162189-2013]
         0.542     160.78       272.2       1.43  elspeth/core/canonical.py:159(_normalize_for_canonical)   [322253-2293]
         0.506      40.27       254.1       2.41  sqlalchemy/engine/cursor.py:297(_adapt_to_context)   [80921-785]
         0.504     243.77       253.4       1.23  sqlalchemy/sql/elements.py:5459(__new__)   [490172-5063]
         0.483     250.81       242.5       1.76  /usr/local/lib/python3.13/re/__init__.py:183(sub)   [505746-6644]
         0.478     187.13       240.1       3.29  sqlalchemy/event/attr.py:509(__call__)   [376945-4552]
         0.460     791.87       231.1       0.95  <frozen abc>:117(__instancecheck__)   [1616529-40698]
         0.460     245.19       231.0       4.13  sqlalchemy/util/langhelpers.py:1223(__get__)   [495848-7915]
         0.456      58.43       229.0       0.51  sqlalchemy/sql/base.py:581(dialect_options)   [118382-2113]
         0.443     138.00       222.7       0.84  elspeth/contracts/freeze.py:32(deep_freeze)   [277318-2698]
         0.441     119.61       221.6       8.83  sqlalchemy/sql/type_api.py:202(operate)   [240362-2346]
         0.437     657.95       219.7       0.71  sqlalchemy/event/attr.py:395(__iter__)   [1325191-15865]
         0.431     504.02       216.7       0.43  elspeth/core/landscape/execution/sink_effect_reservation.py:370(<genexpr>)   [1003049-44]
         0.430      70.43       216.0      15.49  sqlalchemy/sql/elements.py:680(_compile_w_cache)   [141682-1527]
         0.422     998.82       212.1       0.42  <method 'get' of 'mappingproxy' objects>   [2071339-83688]
         0.415     115.59       208.3       1.88  sqlalchemy/sql/elements.py:2871(_construct_for_op)   [232294-2276]
         0.413      70.43       207.5      10.83  sqlalchemy/sql/cache_key.py:347(_generate_cache_key)   [141682-1527]
         0.407     791.87       204.5       0.49  <built-in method _abc._abc_instancecheck>   [1616529-40698]
         0.401     602.99       201.6       0.40  <built-in method __new__ of type object at 0x1320620>   [1337619-137661]
         0.399     243.77       200.4       0.64  sqlalchemy/sql/elements.py:5313(__new__)   [491044-5935]
         0.397      20.00       199.4       1.16  <frozen posixpath>:388(realpath)   [42751-2951]

```

#### 7.2 Marginal top by cumulative time, elspeth-rooted call tree — gate

(typer/click wrappers trimmed; `heartbeat.py:338(_run)`/`254(stop)` appear only because the run executes inside the heartbeat context manager.)

```
cum    95.51s ( 47.996 ms/row) self  -0.000s calls/row     0.00  elspeth/cli.py:1120(run)
cum    95.40s ( 47.939 ms/row) self  -0.000s calls/row     0.00  elspeth/cli.py:1905(_execute_pipeline_with_instances)
cum    95.40s ( 47.939 ms/row) self  -0.000s calls/row     0.00  elspeth/engine/orchestrator/run_lifecycle.py:416(run)
cum    95.40s ( 47.939 ms/row) self  -0.000s calls/row     0.00  elspeth/engine/orchestrator/core.py:220(run)
cum    95.39s ( 47.934 ms/row) self   0.000s calls/row     0.00  elspeth/engine/orchestrator/heartbeat.py:338(_run)
cum    95.39s ( 47.934 ms/row) self  -0.000s calls/row     0.00  elspeth/engine/orchestrator/heartbeat.py:254(stop)
cum    95.35s ( 47.914 ms/row) self   0.000s calls/row     0.00  elspeth/engine/orchestrator/leader_drain.py:102(execute_run)
cum    95.35s ( 47.914 ms/row) self  -0.000s calls/row     0.00  elspeth/engine/orchestrator/core.py:326(_execute_run)
cum    76.29s ( 38.339 ms/row) self   0.050s calls/row     0.00  elspeth/engine/orchestrator/source_iteration.py:515(run_main_processing_loop)
cum    75.56s ( 37.971 ms/row) self   0.068s calls/row     1.00  elspeth/engine/processor.py:2868(process_row)
cum    37.95s ( 19.069 ms/row) self   0.028s calls/row     1.00  elspeth/engine/processor.py:2772(_ingest_source_row_with_initial_claim)
cum    36.03s ( 18.104 ms/row) self   0.009s calls/row     1.00  elspeth/engine/processor.py:4017(_drain_work_queue)
cum    36.02s ( 18.099 ms/row) self   0.012s calls/row     1.00  elspeth/engine/processor.py:5115(_drain_durable_work_queue)
cum    36.00s ( 18.093 ms/row) self   0.008s calls/row     1.00  elspeth/engine/processor.py:5275(_drain_scheduler_claims)
cum    35.99s ( 18.083 ms/row) self   0.116s calls/row     1.00  elspeth/engine/scheduler_drain.py:421(drain_claims)
cum    35.94s ( 18.063 ms/row) self   0.022s calls/row     1.00  elspeth/core/landscape/scheduler_repository.py:207(ingest_row_with_initial_claim)
cum    35.92s ( 18.052 ms/row) self   0.058s calls/row     1.00  elspeth/core/landscape/scheduler/queue.py:353(ingest_row_with_initial_claim)
cum    35.47s ( 17.826 ms/row) self   1.196s calls/row    70.43  sqlalchemy/engine/base.py:1591(_execute_clauseelement)
cum    32.99s ( 16.580 ms/row) self   0.176s calls/row    62.26  sqlalchemy/engine/base.py:1375(execute)
cum    32.81s ( 16.487 ms/row) self   0.191s calls/row    62.26  sqlalchemy/sql/elements.py:518(_execute_on_connection)
cum    21.14s ( 10.623 ms/row) self   0.007s calls/row     1.00  elspeth/engine/processor.py:5456(_process_single_token)
cum    21.13s ( 10.619 ms/row) self   0.039s calls/row     1.00  elspeth/engine/token_traversal.py:1069(process_single_token)
cum    21.05s ( 10.580 ms/row) self   0.052s calls/row     1.00  elspeth/engine/token_traversal.py:511(handle_gate_node)
cum    20.94s ( 10.521 ms/row) self   0.118s calls/row     1.00  elspeth/engine/executors/gate.py:243(execute_config_gate)
cum    19.07s (  9.585 ms/row) self   0.001s calls/row     0.00  elspeth/engine/orchestrator/sink_flush.py:335(flush_and_write_sinks)
cum    19.07s (  9.584 ms/row) self   0.003s calls/row     0.00  elspeth/engine/orchestrator/sink_flush.py:130(write_pending_to_sinks)
cum    19.05s (  9.571 ms/row) self   0.007s calls/row     0.00  elspeth/engine/executors/sink.py:1473(write)
cum    18.67s (  9.380 ms/row) self   0.806s calls/row    80.49  sqlalchemy/engine/base.py:1791(_execute_context)
cum    17.26s (  8.675 ms/row) self   0.014s calls/row     0.00  elspeth/engine/executors/sink.py:748(_write_primary_effect)
cum    16.82s (  8.453 ms/row) self   0.110s calls/row     1.00  elspeth/core/landscape/scheduler/queue.py:245(enqueue_ready_claimed_on)
cum    15.49s (  7.783 ms/row) self   0.430s calls/row    70.43  sqlalchemy/sql/elements.py:680(_compile_w_cache)
cum    12.97s (  6.515 ms/row) self   1.175s calls/row    80.48  sqlalchemy/engine/base.py:1850(_exec_single_context)
cum    12.77s (  6.419 ms/row) self   3.978s calls/row   675.48  sqlalchemy/sql/coercions.py:314(expect)
cum    12.28s (  6.168 ms/row) self   0.006s calls/row     1.00  elspeth/core/landscape/scheduler_repository.py:490(mark_pending_sink)
cum    12.27s (  6.165 ms/row) self   0.028s calls/row     1.00  elspeth/core/landscape/scheduler/dispositions.py:253(mark_pending_sink)
cum    12.23s (  6.147 ms/row) self   0.057s calls/row     1.00  elspeth/core/landscape/scheduler/dispositions.py:703(_transition)
```

#### 7.3 Same, transform workload (cumulative, elspeth frames)

```
cum   123.31s ( 61.964 ms/row) self  -0.000s calls/row     0.00  elspeth/engine/orchestrator/leader_drain.py:102(execute_run)
cum    90.80s ( 45.630 ms/row) self   0.054s calls/row     0.00  elspeth/engine/orchestrator/source_iteration.py:515(run_main_processing_loop)
cum    90.24s ( 45.346 ms/row) self   0.068s calls/row     1.00  elspeth/engine/processor.py:2868(process_row)
cum    52.81s ( 26.536 ms/row) self   0.006s calls/row     1.00  elspeth/engine/processor.py:4017(_drain_work_queue)
cum    52.80s ( 26.532 ms/row) self   0.012s calls/row     1.00  elspeth/engine/processor.py:5115(_drain_durable_work_queue)
cum    52.79s ( 26.526 ms/row) self   0.008s calls/row     1.00  elspeth/engine/processor.py:5275(_drain_scheduler_claims)
cum    52.76s ( 26.515 ms/row) self   0.108s calls/row     1.00  elspeth/engine/scheduler_drain.py:421(drain_claims)
cum    38.09s ( 19.139 ms/row) self   1.263s calls/row    76.36  sqlalchemy/engine/base.py:1591(_execute_clauseelement)
cum    37.56s ( 18.875 ms/row) self   0.005s calls/row     1.00  elspeth/engine/processor.py:5456(_process_single_token)
cum    37.55s ( 18.872 ms/row) self   0.075s calls/row     1.00  elspeth/engine/token_traversal.py:1069(process_single_token)
cum    37.37s ( 18.778 ms/row) self   0.043s calls/row     2.00  elspeth/engine/token_traversal.py:118(handle_transform_node)
cum    37.32s ( 18.752 ms/row) self   0.054s calls/row     2.00  elspeth/engine/processor.py:2435(_execute_transform_with_retry)
cum    37.26s ( 18.725 ms/row) self   0.103s calls/row     2.00  elspeth/engine/retry.py:72(execute_with_retry)
cum    36.80s ( 18.493 ms/row) self   0.020s calls/row     2.00  elspeth/engine/processor.py:2519(execute_attempt)
cum    36.78s ( 18.482 ms/row) self   0.168s calls/row     2.00  elspeth/engine/executors/transform.py:811(execute_transform)
cum    36.09s ( 18.134 ms/row) self   0.027s calls/row     1.00  elspeth/engine/processor.py:2772(_ingest_source_row_with_initial_claim)
cum    34.64s ( 17.406 ms/row) self   0.197s calls/row    65.21  sqlalchemy/engine/base.py:1375(execute)
cum    34.44s ( 17.304 ms/row) self   0.152s calls/row    65.21  sqlalchemy/sql/elements.py:518(_execute_on_connection)
cum    34.38s ( 17.278 ms/row) self   0.022s calls/row     1.00  elspeth/core/landscape/scheduler_repository.py:207(ingest_row_with_initial_claim)
cum    34.36s ( 17.267 ms/row) self   0.058s calls/row     1.00  elspeth/core/landscape/scheduler/queue.py:353(ingest_row_with_initial_claim)
cum    32.50s ( 16.333 ms/row) self   0.003s calls/row     0.00  elspeth/engine/orchestrator/sink_flush.py:335(flush_and_write_sinks)
cum    32.50s ( 16.331 ms/row) self   0.003s calls/row     0.00  elspeth/engine/orchestrator/sink_flush.py:130(write_pending_to_sinks)
cum    32.49s ( 16.327 ms/row) self   0.012s calls/row     0.00  elspeth/engine/executors/sink.py:1473(write)
cum    19.99s ( 10.044 ms/row) self   0.876s calls/row    89.41  sqlalchemy/engine/base.py:1791(_execute_context)
cum    19.88s (  9.988 ms/row) self   0.015s calls/row     0.00  elspeth/engine/executors/sink.py:748(_write_primary_effect)
cum    17.70s (  8.894 ms/row) self   0.231s calls/row    76.00  elspeth/contracts/hashing.py:67(canonical_json)
cum    17.04s (  8.565 ms/row) self   0.548s calls/row    76.36  sqlalchemy/sql/elements.py:680(_compile_w_cache)
cum    15.71s (  7.892 ms/row) self   0.112s calls/row     1.00  elspeth/core/landscape/scheduler/queue.py:245(enqueue_ready_claimed_on)
cum    15.25s (  7.663 ms/row) self   4.593s calls/row   772.92  sqlalchemy/sql/coercions.py:314(expect)
```

#### 7.4 Same, fork workload — marginal tottime top 25 and cumulative

```
### per-row marginal call counts (calls_big - calls_small)/(big-small), top by marginal tottime
marg tottime s  calls/row us/row self marg cum s  function   [calls big - small]
         9.884     390.47     16753.4       9.86  <method 'execute' of 'sqlite3.Cursor' objects>   [235200-4823]
         5.246    1955.94      8891.7      12.79  sqlalchemy/sql/cache_key.py:221(_gen_cache_key)   [1176595-22590]
         5.162    2773.30      8748.9      16.77  sqlalchemy/sql/coercions.py:314(expect)   [1670506-34259]
         4.297      53.05      7283.6       4.30  <method 'commit' of 'sqlite3.Connection' objects>   [31905-608]
         3.955   33625.25      6702.6       4.96  <built-in method builtins.isinstance>   [22986966-3148071]
         2.296    1606.00      3891.0       6.94  rfc8785/_impl.py:188(dump)   [965485-17945]
         2.277     329.43      3858.8       6.08  sqlalchemy/engine/default.py:1313(_init_compiled)   [198220-3855]
         1.807    2506.58      3062.9       2.49  sqlalchemy/util/langhelpers.py:505(get_cls_kwargs)   [1509160-30280]
         1.726     381.48      2926.2      19.10  sqlalchemy/engine/base.py:1850(_exec_single_context)   [229805-4733]
         1.707    1091.74      2892.8       8.15  sqlalchemy/sql/elements.py:1975(__init__)   [657007-12879]
         1.600     349.40      2711.9       1.79  sqlalchemy/sql/compiler.py:1838(construct_params)   [210194-4047]
         1.570     329.43      2661.3      49.00  sqlalchemy/engine/base.py:1591(_execute_clauseelement)   [198220-3855]
         1.501    4716.79      2544.7       1.60  <method 'get' of 'dict' objects>   [3687551-904646]
         1.499    2047.00      2541.4       2.80  rfc8785/_impl.py:79(_serialize_str)   [1230759-23029]
         1.489     975.68      2523.7       5.16  sqlalchemy/sql/elements.py:5535(safe_construct)   [587154-11502]
         1.386     984.74      2348.5       4.60  sqlalchemy/sql/elements.py:2161(_gen_cache_key)   [592519-11521]
         1.334    3132.76      2261.5       1.46  <method 'sub' of 're.Pattern' objects>   [1886809-38481]
         1.183     534.59      2004.8       1.94  sqlalchemy/sql/elements.py:3934(__init__)   [321541-6132]
         1.164    5464.45      1972.6       1.38  <built-in method builtins.getattr>   [3968237-744210]
         1.152    1606.00      1952.7       2.37  elspeth/contracts/hashing.py:38(_normalize_frozen_and_reject_non_finite)   [965485-17945]
         1.152     390.49      1952.2      28.57  sqlalchemy/engine/base.py:1791(_execute_context)   [235216-4825]
         1.009    9660.00      1710.5       1.01  <method 'write' of '_io.BytesIO' objects>   [5807836-108436]
         0.849     390.49      1438.9       6.38  sqlalchemy/engine/default.py:1850(_setup_result_proxy)   [235216-4825]
         0.815     534.59      1382.0      10.71  sqlalchemy/sql/default_comparator.py:50(_boolean_compare)   [321540-6131]
         0.738     196.28      1251.4       3.30  sqlalchemy/engine/cursor.py:297(_adapt_to_context)   [117955-2151]
         0.738     933.14      1250.8       6.13  sqlalchemy/event/attr.py:509(__call__)   [561925-11373]
cum   118.70s (201.178 ms/row) self   0.000s calls/row     0.00  elspeth/engine/orchestrator/leader_drain.py:102(execute_run)
cum   104.86s (177.726 ms/row) self   0.019s calls/row     0.00  elspeth/engine/orchestrator/source_iteration.py:515(run_main_processing_loop)
cum   104.65s (177.375 ms/row) self   0.024s calls/row     1.00  elspeth/engine/processor.py:2868(process_row)
cum    92.38s (156.574 ms/row) self   0.002s calls/row     1.00  elspeth/engine/processor.py:4017(_drain_work_queue)
cum    92.38s (156.570 ms/row) self   0.004s calls/row     1.00  elspeth/engine/processor.py:5115(_drain_durable_work_queue)
cum    92.37s (156.564 ms/row) self   0.006s calls/row     1.00  elspeth/engine/processor.py:5275(_drain_scheduler_claims)
cum    92.37s (156.553 ms/row) self   0.115s calls/row     1.00  elspeth/engine/scheduler_drain.py:421(drain_claims)
cum    49.00s ( 83.057 ms/row) self   1.570s calls/row   329.43  sqlalchemy/engine/base.py:1591(_execute_clauseelement)
cum    44.92s ( 76.140 ms/row) self   0.247s calls/row   284.27  sqlalchemy/engine/base.py:1375(execute)
cum    44.66s ( 75.689 ms/row) self   0.210s calls/row   284.27  sqlalchemy/sql/elements.py:518(_execute_on_connection)
cum    32.60s ( 55.257 ms/row) self   0.019s calls/row     4.00  elspeth/engine/processor.py:5175(_run_barrier_intake_pass)
cum    32.58s ( 55.221 ms/row) self   0.053s calls/row     4.00  elspeth/engine/barrier_coordination.py:419(run_intake_pass)
cum    28.59s ( 48.451 ms/row) self   0.025s calls/row     2.00  elspeth/engine/barrier_coordination.py:585(_adopt_coalesce_row)
cum    28.57s ( 48.417 ms/row) self   1.152s calls/row   390.49  sqlalchemy/engine/base.py:1791(_execute_context)
cum    23.40s ( 39.664 ms/row) self   0.007s calls/row     4.00  elspeth/engine/processor.py:5456(_process_single_token)
cum    23.39s ( 39.650 ms/row) self   0.052s calls/row     4.00  elspeth/engine/token_traversal.py:1069(process_single_token)
cum    19.10s ( 32.375 ms/row) self   1.726s calls/row   381.48  sqlalchemy/engine/base.py:1850(_exec_single_context)
cum    18.66s ( 31.620 ms/row) self   0.690s calls/row   329.43  sqlalchemy/sql/elements.py:680(_compile_w_cache)
cum    18.55s ( 31.444 ms/row) self   0.030s calls/row     2.00  elspeth/engine/token_traversal.py:511(handle_gate_node)
cum    18.43s ( 31.230 ms/row) self   0.077s calls/row     2.00  elspeth/engine/executors/gate.py:243(execute_config_gate)
cum    17.87s ( 30.289 ms/row) self   0.046s calls/row     2.00  elspeth/engine/coalesce_executor.py:798(accept)
cum    16.77s ( 28.419 ms/row) self   5.162s calls/row  2773.30  sqlalchemy/sql/coercions.py:314(expect)
cum    14.48s ( 24.549 ms/row) self   0.504s calls/row   329.43  sqlalchemy/util/langhelpers.py:1350(oneshot)
cum    14.14s ( 23.965 ms/row) self   0.028s calls/row     1.00  elspeth/engine/coalesce_executor.py:1178(_execute_merge)
cum    13.98s ( 23.695 ms/row) self   0.267s calls/row   329.43  sqlalchemy/sql/cache_key.py:411(_generate_cache_key)
cum    13.91s ( 23.582 ms/row) self   0.021s calls/row     1.00  elspeth/engine/tokens.py:373(coalesce_tokens)
cum    13.71s ( 23.242 ms/row) self   0.581s calls/row   329.43  sqlalchemy/sql/cache_key.py:347(_generate_cache_key)
cum    13.68s ( 23.191 ms/row) self   0.001s calls/row     0.00  elspeth/engine/orchestrator/sink_flush.py:335(flush_and_write_sinks)
cum    13.68s ( 23.189 ms/row) self   0.001s calls/row     0.00  elspeth/engine/orchestrator/sink_flush.py:130(write_pending_to_sinks)
```

#### 7.5 Raw pstats, whole run (startup included), gate_2000 — top 40 by tottime

```
         115966820 function calls (109776736 primitive calls) in 105.806 seconds

   Ordered by: internal time
   List reduced from 22946 to 40 due to restriction <40>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
164125/163968    5.939    0.000    5.930    0.000 {method 'execute' of 'sqlite3.Cursor' objects}
  1359085    4.027    0.000   12.929    0.000 sqlalchemy/sql/coercions.py:314(expect)
22311435/22311284    4.000    0.000    5.065    0.000 {built-in method builtins.isinstance}
816109/141682    3.612    0.000   10.312    0.000 sqlalchemy/sql/cache_key.py:221(_gen_cache_key)
20199/20196    3.269    0.000    3.269    0.000 {method 'commit' of 'sqlite3.Connection' objects}
955904/106279    2.256    0.000    7.039    0.000 rfc8785/_impl.py:188(dump)
1509961/262379    1.746    0.000    2.392    0.000 sqlalchemy/util/langhelpers.py:505(get_cls_kwargs)
   141682    1.581    0.000    4.508    0.000 sqlalchemy/engine/default.py:1313(_init_compiled)
  1219321    1.480    0.000    2.746    0.000 rfc8785/_impl.py:79(_serialize_str)
   568201    1.351    0.000    6.432    0.000 sqlalchemy/sql/elements.py:1975(__init__)
  3047851    1.220    0.000    1.279    0.000 {method 'get' of 'dict' objects}
141682/141653    1.210    0.000   35.982    0.000 sqlalchemy/engine/base.py:1591(_execute_clauseelement)
   157606    1.197    0.000    1.356    0.000 sqlalchemy/sql/compiler.py:1838(construct_params)
162155/162006    1.192    0.000   13.134    0.000 sqlalchemy/engine/base.py:1850(_exec_single_context)
   489914    1.190    0.000    4.118    0.000 sqlalchemy/sql/elements.py:5535(safe_construct)
  1746250    1.146    0.000    1.246    0.000 {method 'sub' of 're.Pattern' objects}
   489909    1.142    0.000    4.254    0.000 sqlalchemy/sql/elements.py:2161(_gen_cache_key)
955904/106279    1.123    0.000    2.287    0.000 elspeth/contracts/hashing.py:38(_normalize_frozen_and_reject_non_finite)
3296873/3292844    1.006    0.000    1.254    0.000 {built-in method builtins.getattr}
  5774001    0.994    0.000    0.994    0.000 {method 'write' of '_io.BytesIO' objects}
   232294    0.863    0.000    1.420    0.000 sqlalchemy/sql/elements.py:3934(__init__)
162189/161993    0.816    0.000   18.906    0.000 sqlalchemy/engine/base.py:1791(_execute_context)
  2289719    0.701    0.000    0.702    0.000 {method 'encode' of 'str' objects}
   232293    0.597    0.000    7.758    0.000 sqlalchemy/sql/default_comparator.py:50(_boolean_compare)
   162189    0.557    0.000    4.556    0.000 sqlalchemy/engine/default.py:1850(_setup_result_proxy)
322253/56146    0.545    0.000    1.444    0.000 elspeth/core/canonical.py:159(_normalize_for_canonical)
1337619/1334690    0.521    0.000    0.580    0.000 {built-in method __new__ of type object at 0x1320620}
    80921    0.511    0.000    2.443    0.000 sqlalchemy/engine/cursor.py:297(_adapt_to_context)
   490172    0.510    0.000    1.239    0.000 sqlalchemy/sql/elements.py:5459(__new__)
   505746    0.489    0.000    1.796    0.000 /usr/local/lib/python3.13/re/__init__.py:183(sub)
376945/352147    0.485    0.000    3.365    0.000 sqlalchemy/event/attr.py:509(__call__)
1616529/1616374    0.472    0.000    0.997    0.000 <frozen abc>:117(__instancecheck__)
495848/495549    0.468    0.000    4.184    0.000 sqlalchemy/util/langhelpers.py:1223(__get__)
     4151    0.458    0.000    0.458    0.000 {built-in method marshal.loads}
   118382    0.458    0.000    0.512    0.000 sqlalchemy/sql/base.py:581(dialect_options)
277318/62227    0.449    0.000    0.848    0.000 elspeth/contracts/freeze.py:32(deep_freeze)
   240362    0.446    0.000    8.935    0.000 sqlalchemy/sql/type_api.py:202(operate)
  2071339    0.444    0.000    0.444    0.000 {method 'get' of 'mappingproxy' objects}
  1325191    0.443    0.000    0.723    0.000 sqlalchemy/event/attr.py:395(__iter__)
   141682    0.435    0.000   15.763    0.000 sqlalchemy/sql/elements.py:680(_compile_w_cache)


```

#### 7.6 Raw pstats, gate_2000 — top 40 by cumulative

```
         115966820 function calls (109776736 primitive calls) in 105.806 seconds

   Ordered by: cumulative time
   List reduced from 22946 to 40 due to restriction <40>

   ncalls  tottime  percall  cumtime  percall filename:lineno(function)
   5243/1    0.384    0.000  107.596  107.596 {built-in method builtins.exec}
        1    0.000    0.000  107.596  107.596 <string>:1(<module>)
        1    0.000    0.000  107.596  107.596 <frozen runpy>:206(run_module)
        1    0.000    0.000  107.539  107.539 <frozen runpy>:65(_run_code)
        1    0.000    0.000  107.539  107.539 elspeth/cli.py:1(<module>)
        1    0.000    0.000  105.712  105.712 typer/main.py:1131(__call__)
        1    0.000    0.000  105.694  105.694 click/core.py:1629(__call__)
        1    0.000    0.000  105.694  105.694 typer/core.py:785(main)
        1    0.000    0.000  105.693  105.693 typer/core.py:156(_main)
        1    0.000    0.000  105.692  105.692 click/core.py:1998(invoke)
        2    0.000    0.000  105.692   52.846 click/core.py:1401(invoke)
        2    0.000    0.000  105.692   52.846 click/core.py:857(invoke)
        2    0.000    0.000  105.692   52.846 typer/main.py:1505(wrapper)
        1    0.000    0.000  105.688  105.688 elspeth/cli.py:1120(run)
        1    0.000    0.000  102.230  102.230 elspeth/cli.py:1905(_execute_pipeline_with_instances)
        1    0.000    0.000   97.525   97.525 elspeth/engine/orchestrator/core.py:220(run)
        1    0.000    0.000   97.525   97.525 elspeth/engine/orchestrator/run_lifecycle.py:416(run)
        9    0.000    0.000   96.830   10.759 /usr/local/lib/python3.13/threading.py:1059(join)
        9    0.001    0.000   96.829   10.759 {method 'join' of '_thread._ThreadHandle' objects}
      6/2    0.000    0.000   96.517   48.259 /usr/local/lib/python3.13/threading.py:1001(_bootstrap)
        2    0.000    0.000   96.517   48.258 elspeth/engine/orchestrator/heartbeat.py:254(stop)
      6/2    0.001    0.000   96.516   48.258 /usr/local/lib/python3.13/threading.py:1028(_bootstrap_inner)
      6/2    0.000    0.000   96.516   48.258 /usr/local/lib/python3.13/threading.py:984(run)
        1    0.000    0.000   96.516   96.516 elspeth/engine/orchestrator/heartbeat.py:338(_run)
        1    0.000    0.000   96.432   96.432 elspeth/engine/orchestrator/core.py:326(_execute_run)
        1    0.000    0.000   96.432   96.432 elspeth/engine/orchestrator/leader_drain.py:102(execute_run)
        1    0.050    0.050   76.745   76.745 elspeth/engine/orchestrator/source_iteration.py:515(run_main_processing_loop)
     2000    0.068    0.000   75.965    0.038 elspeth/engine/processor.py:2868(process_row)
     2000    0.028    0.000   38.143    0.019 elspeth/engine/processor.py:2772(_ingest_source_row_with_initial_claim)
     2000    0.009    0.000   36.225    0.018 elspeth/engine/processor.py:4017(_drain_work_queue)
     2000    0.012    0.000   36.214    0.018 elspeth/engine/processor.py:5115(_drain_durable_work_queue)
     2000    0.008    0.000   36.202    0.018 elspeth/engine/processor.py:5275(_drain_scheduler_claims)
     2000    0.117    0.000   36.183    0.018 elspeth/engine/scheduler_drain.py:421(drain_claims)
     2000    0.022    0.000   36.131    0.018 elspeth/core/landscape/scheduler_repository.py:207(ingest_row_with_initial_claim)
     2000    0.059    0.000   36.108    0.018 elspeth/core/landscape/scheduler/queue.py:353(ingest_row_with_initial_claim)
141682/141653    1.210    0.000   35.982    0.000 sqlalchemy/engine/base.py:1591(_execute_clauseelement)
125263/125239    0.179    0.000   33.493    0.000 sqlalchemy/engine/base.py:1375(execute)
125139/125115    0.193    0.000   33.262    0.000 sqlalchemy/sql/elements.py:518(_execute_on_connection)
     2000    0.007    0.000   21.247    0.011 elspeth/engine/processor.py:5456(_process_single_token)
     2000    0.039    0.000   21.239    0.011 elspeth/engine/token_traversal.py:1069(process_single_token)


```

Raw top-40 tables for the other workloads: `profile/pstats-{transform_2000,batch_2000,fork_600}-{tottime,cumulative}.txt`; full marginal tables: `profile/analysis-{gate,transform,batch,fork}.txt`; `.prof` dumps: `profile/work/<workload>_<n>/run.prof`.

### 8. In-tree benchmarks (`tests/performance/benchmarks`)

Run 1 (as briefed): `.venv/bin/python -m pytest tests/performance/benchmarks -m performance -n 0 -p no:cacheprovider --benchmark-only --benchmark-columns=min,mean,median,stddev,rounds,ops > profile/benchmarks.log 2>&1` -> **exit=1**: `1 failed, 9 passed, 24 skipped in 10.67s`. The 24 skips are tests that do not use the `benchmark` fixture (`--benchmark-only` skips them). The failure is a budget assertion, not a crash: `test_batch_flush_cross_check_within_budget` — `Batch-flush median 2133.1us exceeds 1500us budget` on this box.

```

------------------------------------------------------------------------ benchmark: 4 tests -----------------------------------------------------------------------
Name (time in us)                                     Min                  Mean                Median              StdDev            Rounds           OPS
-------------------------------------------------------------------------------------------------------------------------------------------------------------------
test_diamond_walk_p99_within_budget                1.5160 (1.0)          2.0000 (1.0)          1.8620 (1.0)        1.8074 (1.0)       26157  499,994.1795 (1.0)
test_cross_check_p99_within_budget                12.9050 (8.51)        15.1076 (7.55)        13.9300 (7.48)       6.6794 (3.70)      13391   66,191.6491 (0.13)
test_cross_check_raises_on_drop                  165.4780 (109.15)     195.0690 (97.53)      181.1330 (97.28)     65.7911 (36.40)      2980    5,126.3906 (0.01)
test_batch_flush_cross_check_within_budget     1,976.7700 (>1000.0)  2,296.2808 (>1000.0)  2,133.0530 (>1000.0)  453.0087 (250.65)      383      435.4868 (0.00)
-------------------------------------------------------------------------------------------------------------------------------------------------------------------

------------------------------------- benchmark 'dispatcher-overhead': 1 tests ------------------------------------
Name (time in us)                                              Min     Mean   Median   StdDev  Rounds  OPS (Kops/s)
-------------------------------------------------------------------------------------------------------------------
test_dispatcher_overhead_vs_direct_verify_pass_through     16.6290  25.6549  24.7730  10.0965    6336       38.9789
-------------------------------------------------------------------------------------------------------------------

---------------------------------------------------------------------- benchmark 'dispatcher-overhead-scaling': 5 tests ----------------------------------------------------------------------
Name (time in us)                                                                      Min               Mean             Median              StdDev            Rounds  OPS (Kops/s)
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
test_dispatcher_overhead_scales_with_registered_and_applicable_contracts[1-1]      14.9920 (1.0)      23.2988 (1.31)     21.3190 (1.32)      26.3223 (4.89)      14811       42.9206 (0.76)
test_dispatcher_overhead_scales_with_registered_and_applicable_contracts[2-1]      15.2440 (1.02)     17.7953 (1.0)      16.2060 (1.0)        5.3779 (1.0)       22573       56.1947 (1.0)
test_dispatcher_overhead_scales_with_registered_and_applicable_contracts[4-2]      27.4710 (1.83)     35.1384 (1.97)     30.2580 (1.87)     107.4479 (19.98)     14513       28.4589 (0.51)
test_dispatcher_overhead_scales_with_registered_and_applicable_contracts[8-4]      54.7510 (3.65)     73.4685 (4.13)     61.0375 (3.77)      39.3098 (7.31)      10446       13.6113 (0.24)
test_dispatcher_overhead_scales_with_registered_and_applicable_contracts[16-5]     69.6560 (4.65)     81.8965 (4.60)     73.8340 (4.56)      26.5329 (4.93)       4973       12.2105 (0.22)
----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

```

Run 2 (the 24 non-fixture tests, own timers): `... pytest tests/performance/benchmarks -m performance -n 0 -p no:cacheprovider -v -s --durations=40 --deselect .../test_cross_check_overhead.py --deselect .../test_validator_walk.py > profile/benchmarks-nonfixture.log 2>&1` -> **exit=1**: `5 failed, 19 passed, 10 deselected in 36.53s`.
- 4 failures are a stale benchmark, not a perf result: `test_db_write.py::{test_create_row_throughput,test_create_token_throughput,test_begin_node_state_throughput,test_record_outcome_throughput}` all raise `TypeError: DataFlowRepository.register_node() got an unexpected keyword argument 'run_id'` — the DB-write benchmarks no longer match the repository API, so the suite that would measure exactly the dominant bucket currently measures nothing.
- 1 budget failure: `test_deepcopy_medium_rows_baseline` — `266.80us (expected < 200us)` (machine-speed sensitive).
- These tests assert thresholds and do not print rates; pytest call durations are the only numbers: `test_throughput_1000_rows_passthrough` 17.12 s (=> ~58 rows/s incl. setup), `test_throughput_100_rows_passthrough` 2.13 s, `test_throughput_with_multiple_transforms` (100 rows, 5 transforms) 2.47 s, `test_throughput_fork_pipeline` (100 rows) 2.56 s, `test_begin_run_latency` 4.34 s. Their pass thresholds are > 20-33 rows/s, i.e. the suite's own bar is the throughput measured here.

### 9. Composer, key-free unit (optional step)

No composer LLM turn was attempted. Timed in isolation (`profile/composer_time.py`, `time.perf_counter`, warm, log `profile/composer-time.log`): a synthetic runtime YAML of csv source -> N chained `value_transform` nodes -> csv sink, imported with `composition_state_from_runtime_yaml`, then `CompositionState.validate()` (Stage-1 validation incl. plugin probes), `to_dict`/`from_dict`, `with_node`, `yaml_generator.generate_yaml`. All states validated (`is_valid=True`).

```

N=5 transform nodes: nodes=5 edges=0 outputs=1 is_valid=True errors=0 yaml_chars=1378
  composition_state_from_runtime_yaml        iters=  20 median=    5.828 ms  min=    5.695 ms  mean=    5.849 ms
  state.validate()                           iters=  10 median=    8.962 ms  min=    8.335 ms  mean=    9.314 ms
  state.to_dict()                            iters=  50 median=    0.010 ms  min=    0.009 ms  mean=    0.012 ms
  CompositionState.from_dict(d)              iters=  50 median=    0.055 ms  min=    0.051 ms  mean=    0.057 ms
  state.with_node(replace last)              iters=  50 median=    0.005 ms  min=    0.005 ms  mean=    0.006 ms
  yaml_generator.generate_yaml(state)        iters=  10 median=    1.695 ms  min=    1.627 ms  mean=    1.887 ms
N=20 transform nodes: nodes=20 edges=0 outputs=1 is_valid=True errors=0 yaml_chars=4429
  composition_state_from_runtime_yaml        iters=  20 median=   17.985 ms  min=   17.751 ms  mean=   18.008 ms
  state.validate()                           iters=  10 median=   26.816 ms  min=   24.469 ms  mean=   27.093 ms
  state.to_dict()                            iters=  50 median=    0.032 ms  min=    0.031 ms  mean=    0.035 ms
  CompositionState.from_dict(d)              iters=  50 median=    0.180 ms  min=    0.167 ms  mean=    0.185 ms
  state.with_node(replace last)              iters=  50 median=    0.006 ms  min=    0.005 ms  mean=    0.007 ms
  yaml_generator.generate_yaml(state)        iters=  10 median=    5.590 ms  min=    5.500 ms  mean=    5.607 ms
N=50 transform nodes: nodes=50 edges=0 outputs=1 is_valid=True errors=0 yaml_chars=10579
  composition_state_from_runtime_yaml        iters=  20 median=   44.643 ms  min=   42.751 ms  mean=   44.578 ms
  state.validate()                           iters=   5 median=   65.259 ms  min=   59.987 ms  mean=   66.972 ms
  state.to_dict()                            iters=  50 median=    0.106 ms  min=    0.097 ms  mean=    0.112 ms
  CompositionState.from_dict(d)              iters=  50 median=    0.644 ms  min=    0.401 ms  mean=    0.630 ms
  state.with_node(replace last)              iters=  50 median=    0.007 ms  min=    0.006 ms  mean=    0.008 ms
  yaml_generator.generate_yaml(state)        iters=  10 median=   15.747 ms  min=   13.036 ms  mean=   15.907 ms
```

Reading: every operation is linear in node count; at 50 nodes validate ~65 ms, YAML import ~45 ms, YAML generation ~16 ms, state (de)serialisation < 1 ms, mutation ~7 us. Server-side composer state work is two to three orders of magnitude below one LLM round trip *(inferred: no provider call was measured)* and is not a hot path next to the executor's per-row cost.

### 10. Conclusions (measured, in priority order)

1. The executor's hot path is the Landscape audit write path, and inside it SQLAlchemy Core per-statement overhead, not SQLite and not engine logic: 73-79% of marginal cProfile self time / 85% of py-spy in-run samples are audit-DB code; only ~16% of in-run samples are inside sqlite3 `execute`+`commit`. ~70-84 Core statements, ~81-97 `Cursor.execute`, ~9-13 commits and ~18-21 `Engine.begin` blocks per source row for a 1-3 node linear pipeline (390 / 53 / 113 for fork-coalesce). Each statement is rebuilt as an expression tree and cache-keyed every time (~860-1035 `_gen_cache_key` calls and ~675+ `coercions.expect` calls per row).
2. Two halves of `process_row` cost about the same (gate workload figures): source-row ingest + initial scheduler claim (`scheduler/queue.py:353 ingest_row_with_initial_claim`, 18.1 ms/row profiled; `enqueue_ready_claimed_on` 8.5 ms of it) and the durable work-queue drain that executes the node (`scheduler_drain.py:421 drain_claims`, 18.1 ms/row; `mark_pending_sink`/`_transition` 6.2 ms of it). Sink flush (`sink_flush.py:335`, `executors/sink.py:1473 write`) is ~20% (9.6 ms/row), nearly all sink-effect ledger bookkeeping, not the CSV write.
3. Canonical JSON + hashing is the clear second bucket (6-7% by sampling, 9-16% by cProfile): 53-128 `rfc8785.dumps` and 39-112 sha256 per source row, of which 8 `canonical_json` per row come from scheduler event recording (`scheduler/events.py:83`).
4. Engine control flow (1.2-2.0%), plugin code (0.1-1.3%), pydantic (<= 0.5% per row), telemetry/logging (0.0%) are not hot in these workloads. Row/contract objects are 2-7% (highest with real transforms: `deep_freeze`, contract checks).
5. Fixed startup ~6.7-7.0 s per `elspeth run`, ~70% module import.
6. One measured O(n^2) in sink flush member count (`sink_effect_reservation.py:358/370`), small at 2000 rows (~1%) but growing quadratically with rows pending per sink; plus avoidable per-row syscall churn in `payload_store._validate_store_directory` (~129 `lstat` per row).
7. Benchmarks: the in-tree DB-write benchmarks are broken by an API drift (`register_node(run_id=...)`), so the dominant bucket has no working micro-benchmark today.

Limits: one machine, single runs per size (no repeat-variance estimate), SQLite only (PostgreSQL would shift time from Python to network/driver waits and make the statement COUNT matter more), no LLM/pooled-thread workload, cProfile shares are biased as described in section 3.
