# row_union Design Spike — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Status: SPIKE.** This branch (`spike/row-union`) proves the v1 contract shape end-to-end in the engine. It is NOT the production delivery: composer/web/frontend exposure, resume restore of pending groups, multi-worker distributed finalization, TUI/explain surfaces, and docs are explicitly deferred (see "Deferred surfaces").

**Goal:** Implement the `row_union` DAG primitive — a correlated, same-`row_id`, N→N UNION ALL barrier over declared fork branches — far enough that the exact two-variant A/B scenario executes end to end into `batch_experiment_compare` under the normative contract recorded on filigree elspeth-a5b86149d4 (comment 1286, 2026-07-16).

**Architecture:** `row_union` is a third barrier kind beside coalesce (correlated N→1 merge) and queue (uncorrelated interleave). It reuses the ADR-030 journal-first barrier machinery unchanged: arrivals hold as durable BLOCKED rows under `barrier_key = row_union name`, the `BarrierIntakeCoordinator` gains a third dispatch arm, and the fire is one existing F1 `complete_barrier` transaction that consumes the group's BLOCKED rows and emits N READY continuations atomically (the API already accepts a sequence of emissions — coalesce just passes one). Released tokens keep their identity, `row_id`, `branch_name`, and `fork_group_id`; the barrier never touches payloads.

**Tech stack:** Python 3.12, pytest, existing engine/orchestrator/Landscape stack. No new dependencies.

## Global constraints (from the recorded product contract)

- `row_union` is correlated on `row_id`; durable barrier identity is `(row_union node, row_id)`; contributions unique per declared branch.
- v1 policy is `require_all` ONLY. No field merge, no dedup, no fabricated rows, no wide intermediate row.
- Within a group, release order is declared branch order. Cross-group ordering must come from a durable sequence, not arrival timing.
- Lost/duplicate/late branches fail closed with NO partial release.
- Barrier satisfaction + audit evidence + downstream enqueue = ONE atomic, fail-closed durable transition (met by F1 `complete_barrier`). NOT idempotent, and the original wording here said otherwise: `_insert_ready_emission` uses the non-idempotent `insert_work_item`, and a replay of an already-committed `complete_barrier` never reaches it — the consumed rows are already TERMINAL, so the `missing_token_ids` exhaustiveness check (`barrier.py:283-292`) raises `AuditIntegrityError` first. The durable outcome is safe (nothing is double-emitted) but a crash between commit and in-memory advance aborts the run on a diagnostic that reads like journal corruption rather than "already done". Same shape as the shipped `complete_coalesce_merge`; do not read this line as licence to retry.
- Downstream batch/window code must never split a released group. **Spike posture:** enforced by graph-validation rejection — an aggregation fed (transitively) by a `row_union` must use an `end_of_source` trigger; `count`/`timeout`/`condition` triggers are rejected at build time with a diagnostic naming the group-split hazard and the supported alternative. Group-aware trigger firing is production follow-up, not spike scope.
- Every surface must either support the scenario or reject it with the same guidance (DAG corpus exit gate, scenario `variant-union` in `docs/architecture/dag/scenario-corpus/v1/manifest.yaml:4060`).

## Deconfliction record (2026-07-29)

- `codex/dag-corpus-wave-a` and `-wave-b` are already merged into `release/0.7.2`; they contribute docs/tests/fixtures only — **no `row_union` source exists on any branch** (verified by `git grep row_union` across all local branch tips).
- The paused `codex/deferred-platform-completion` branch (13-commit replay, held-barrier distributed finalization) overlaps this spike ONLY at `coalesce_executor.py` (2 lines), `leader_drain.py` (2 lines), `resume.py`, `sink_flush.py`, `sink.py`/`sink_effects.py`, `export.py`. **Spike rule: do not modify `resume.py`, `sink_flush.py`, `sink_effects.py`, or `export.py`.** The fail-closed resume guard lands in `barrier_coordination.py` (not on that branch's diff).
- Scenario-corpus manifest cells for row-union stay `fail` until the production delivery; the spike does not edit the manifest (single-writer integration rule from the Wave B handoff).

## Key integration facts (verified against tree at bbad4a9ef)

| Seam | Anchor | Change |
|---|---|---|
| Node vocabulary | `src/elspeth/contracts/enums.py:91` | add `ROW_UNION = "row_union"` |
| Name type | `src/elspeth/contracts/types.py:18` | add `RowUnionName = NewType(...)` |
| Fork destination rule | `src/elspeth/core/dag/builder.py:572-607` | third arm: branch → row_union (COPY edge), before the no-destination error |
| Structural fan-in whitelist | `src/elspeth/core/dag/graph.py:296` | add `NodeType.ROW_UNION` |
| Structural traversal set | `src/elspeth/engine/orchestrator/graph_wiring.py:31` | add to `_STRUCTURAL_NODE_TYPES`; mirror `get_coalesce_id_map` plumbing |
| Barrier hold (worker side) | `src/elspeth/engine/processor.py:2743` `_LiveBarrierHold` | reuse verbatim, `barrier_key = row_union name` |
| Intake dispatch | `src/elspeth/engine/barrier_coordination.py:323-330` | third membership set + `_adopt_row_union_row` |
| Atomic fire | `TokenSchedulerRepository.complete_barrier` (`core/landscape/scheduler/barrier.py`) | call with N `emitted_ready` — no journal changes |
| Timeout/EOF pump | `src/elspeth/engine/orchestrator/outcomes.py:377,427` | pump `RowUnionExecutor.check_timeouts` / `flush_pending` beside coalesce |
| Recovery guard | `barrier_coordination.py:708` barrier-kind dispatch | fail-closed: refuse restore when a BLOCKED row carries a row_union key |
| Token identity | `src/elspeth/contracts/identity.py` | NO change — `fork_group_id` + `row_id` already identify the group |

## Design: RowUnionExecutor (`src/elspeth/engine/row_union_executor.py`, new)

Mirrors `CoalesceExecutor`'s barrier mechanics (pending map keyed `(row_union_name, row_id)`, per-branch `_BranchEntry` with Landscape `begin_node_state` on arrival, completed-keys FIFO + Landscape fallback for late arrivals, duplicate-arrival crash) with these deliberate differences:

- **No merge machinery.** No merge plans, no schema synthesis, no TokenManager call. Release returns the original `TokenInfo`s untouched.
- **Outcome type** `RowUnionOutcome`: `held: bool`, `released_tokens: tuple[TokenInfo, ...]` (declared branch order), `consumed_tokens`, `failure_reason`, `row_union_name`, `outcomes_recorded`, `late_arrival`. Mutual-exclusivity `__post_init__` like `CoalesceOutcome` (`coalesce_executor.py:52-89`).
- **Release** (`_execute_release`): for each held branch in declared order, `complete_node_state(status=COMPLETED, output_data=token.row_data)`; NO `record_token_outcome` (tokens are not terminal — they continue downstream). Drop pending, mark completed, return released group.
- **Failure** (`_fail_pending` mirror of `coalesce_executor.py:996-1083`): complete held node states FAILED with a typed `RowUnionFailureReason` (sibling of `CoalesceFailureReason`), record `TerminalOutcome.FAILURE` per held token, fail closed — never a partial release.
- **`check_timeouts` / `flush_pending` / `notify_branch_lost`**: same shapes as coalesce (`coalesce_executor.py:1347,1406,1484`), but v1 semantics are single-arm: any timeout, EOF with incomplete group, or lost branch → whole-group failure. No first/quorum/best_effort arms.
- **`restore_from_journal`**: NOT implemented in the spike — see fail-closed resume guard below.

## Design: engine wiring

- **Worker-side hold:** a `_maybe_row_union_token` twin of `processor.py:2702` `_maybe_coalesce_token` — stash `_LiveBarrierHold(token, barrier_key=row_union_name)`; follower arm identical (return held with no child items → drain marks BLOCKED).
- **Intake:** third membership set `row_union_keys` in `BarrierIntakeCoordinator.run_intake_pass` (`barrier_coordination.py:323-330`) + `_adopt_row_union_row` twin of `_adopt_coalesce_row` (`:396`). On release, build N READY continuations (one per released token, `current_node_id = row_union node`) via `WorkItemFactory` and complete the barrier once.
- **Atomic fire:** `_complete_row_union_fire` twin of `processor.py:3207` — single `self._scheduler.complete_barrier(barrier_key=row_union_name, consumed_token_ids=<group>, emitted_ready=<N codec emissions in declared branch order>, intake_snapshot_token_ids=<group>, scope_row_id=...)`. Cross-group determinism rides the journal's insertion sequence (same durable ordering authority coalesce continuations use); declared-branch order within the group is preserved by emission order.
- **Timeout/EOF pump:** twin arms beside `outcomes.py:377` (`check_timeouts`) and `outcomes.py:427` (`flush_pending`); failures consume BLOCKED rows via `mark_blocked_barrier_terminal`.
- **Branch loss:** processor `_notify_coalesce_of_lost_branch` (`processor.py:2749`) gains a row_union twin writing the same durable `BranchLossSpec` lane (barrier-key generic) and notifying the executor; outcome is always whole-group failure.
- **Fail-closed resume guard:** in `BarrierRecoveryCoordinator.restore_from_journal` (`barrier_coordination.py:708` kind dispatch), a BLOCKED row whose `barrier_key` names a registered row_union raises `OrchestrationInvariantError` with "resume across pending row_union groups is not yet supported" — honest v0 posture, keeps us off `resume.py` (deferred-platform overlap).

## Design: group-indivisibility guard (build-time)

New validation in graph construction: walk successors of each row_union node; every AGGREGATION node reachable without passing through a SINK must have trigger type `end_of_source`. Otherwise `GraphValidationError`: names the aggregation, the row_union, the group-split hazard, and the supported alternative (`end_of_source` trigger or move the aggregation upstream of the fork). This is the spike's honest enforcement of "triggers may fire only between complete groups".

## Deferred surfaces (production follow-up, tracked on elspeth-a5b86149d4)

Composer state/importer/generator/MCP tools/guided authoring; frontend graph+wire surfaces; group-aware count/timeout triggers; resume restore of pending groups (resume with row_union BLOCKED rows is unwired — `resume.py` untouched per the deferred-platform deconfliction rule); the durable §E.5 branch-loss lane for row_union (planned as in-memory leader notify only in the spike — but see the outcome record's open list: the notify method has no call sites, so nothing runs); multi-worker/distributed finalization (must rebase over deferred-platform); TUI/explain; scenario-corpus manifest promotion; docs (elspeth-c4eff6c8cb).

## Outcome record (2026-07-29 spike; corrected 2026-07-30 after review)

The original record here claimed "full engine wiring" shipped. It was written before cross-model review, which found several gaps. Most are now fixed (58269867a, 4d44801bf, 922cb3078, ad42dd5a0, 0f6159bcc, 36696f6d0, e317abafb, 136b5e3cb, 10cc7b9c0, d24dc8bf7); two remain open, and two that this section previously recorded as open are narrower than first stated. This section states what is true now.

### Landed on release/0.7.2

- **Contracts:** `NodeType.ROW_UNION`, `RowUnionName`, `RowUnionFailureReason` in the node-state error union.
- **Config:** `RowUnionSettings` + the `row_unions:` section, with the CoalesceSettings-parity field validators it originally shipped without — identifier rejection (empty/whitespace/untrimmed, `__`-prefixed, reserved edge labels, over-long), no trim-collapse of two declared branches into one, and `allow_inf_nan=False` on `timeout_seconds` (`gt=0` alone let `inf` through, which silently disabled the sweep).
- **DAG build/validation:** node construction and fork-branch wiring, the group-indivisibility trigger guard, and the correlated-barrier reachability guard — a COALESCE or ROW_UNION reachable from a row_union without an intervening SINK is now rejected at build time (both barriers key their pending map on `(barrier, row_id)` with no `fork_group_id`, so neither can consume an N-to-N group). `DIVERT_ROW_UNION_GROUP_LOSS` build warning.
- **`RowUnionExecutor`:** hold/release, duplicate and late arrival fail-closed, timeout and EOF-flush group failure (18 unit tests in `tests/unit/engine/test_row_union_executor.py`).
- **Engine drain path:** work items, traversal threading, intake third arm, and atomic N-ready release through one `complete_barrier`. Identity-form (list-form) branches execute — they previously built and validated but aborted every run. Released continuations advance to the node *after* the barrier, since a row_union releases the ORIGINAL tokens rather than a fresh merged one.
- **Durable `row_union_name` journal column:** now written by all three writers (`complete_barrier`, the ordinary `enqueue_ready`/`enqueue_ready_claimed` path, and the atomic child-enqueue in dispositions) and read back on rehydration, so a follower claim, peer takeover or crash re-drive holds at the barrier instead of walking through it. It is also in `insert_work_item_idempotent`'s strict field-equality set, so the sites cannot drift apart again. The binding survives a row-multiplying transform inside a branch.
- **Landscape epoch 30:** `SQLITE_SCHEMA_EPOCH` advanced 29→30 for the new column (operator-visible — an epoch-29 store is now refused at open with the recreate-not-migrate message, and the runbooks/README/CHANGELOG moved with it).
- **Timeout sweep:** `run_end_of_input_barrier_flush` no longer early-returns past the row_union flush; idle-source polling accounts for row_union timeouts; both the fresh-run row boundary and the resume loop sweep them. `complete_barrier` stamps each READY emission with a distinct `created_at` so `claim_ready` reproduces the declared branch order durably.
- **Acceptance:** two-variant A/B tests driving `batch_experiment_compare` and `batch_paired_preference` end to end.

### Still open

- **Branch-loss notification is wired, but leader-only.** `136b5e3cb` gave
  `Processor._notify_row_union_of_lost_branch` a live call site: every early-exit path now
  routes through one barrier-agnostic seam, `_notify_barrier_of_lost_branch`
  (`processor.py:2857`), so retry exhaustion, filter drop, quarantine, error routing, gate
  routing and gate discard all reach the row_union executor. The earlier record here — that
  the method had zero call sites and never ran — is superseded. What remains open is the
  durable §E.5 lane: the record-then-notify path is still coalesce-only
  (`coalesce_branch_losses`, `record_coalesce_branch_loss`, and `_replay_branch_losses` at
  `barrier_coordination.py:702` have no row_union arm; `coalesce_branch_losses.coalesce_name`
  is `nullable=False`, so the table structurally cannot represent a row_union loss), and
  `_notify_row_union_of_lost_branch` returns early when the worker holds no executor. On a
  single worker the in-memory notify is authoritative and the group fails closed naming the
  loss; under ADR-030's leader plus claim-only followers, a branch lost on a follower is
  recorded nowhere and the group instead waits for the end-of-source flush, failing with the
  generic `row_union_incomplete_at_flush` — the exact misattribution 136b5e3cb set out to
  kill. `examples/row_union_ab_experiment/README.md` states the fail-closed guarantee
  unconditionally and should be scoped until this lands.
- **Resume refuses pending row_union groups accurately, by design.** `d24dc8bf7` gave
  `BarrierRecoveryCoordinator.restore_from_journal` the registered row_union names solely so
  it can recognise this case: a BLOCKED row whose `barrier_key` names a registered row_union
  now raises `OrchestrationInvariantError` with "Resume across pending row_union groups is
  not yet supported" (`barrier_coordination.py:882`) instead of falling through to the
  orphan-`barrier_key` `AuditIntegrityError`, whose message blamed a barrier the pipeline
  "no longer has" and sent operators hunting a config change that never happened. Restore of
  pending groups remains genuinely unimplemented — it would have to rebuild per-branch
  executor state and reconcile the crash window where a released group's node states are
  already COMPLETED while its journal rows are still BLOCKED. The run cannot resume mid-group
  and must be re-run. This restriction is not yet covered in
  `docs/runbooks/resume-failed-run.md`.
- **Batch-flush continuations drop the binding — PROVEN REACHABLE.** `_FlushContext`
  (`processor.py:202-226`) carries `coalesce_node_id`/`coalesce_name` and no row_union fields,
  and the flush routers' two `create_continuation` calls (`processor.py:1434`, `:1613`) thread
  `coalesce_name` only. This is the same defect class 922cb3078 fixed for expanding
  transforms, at the aggregation-flush sites. Reachability is no longer open: a fork branch
  containing `aggregation(plugin: batch_replicate, trigger: {count: 2}, output_mode:
  transform)` feeding a row_union **builds clean** — it passes `graph.validate()` and
  `validate_edge_compatibility()`. The group-indivisibility guard does not catch it because
  that guard walks FORWARD from the row_union and this aggregation is UPSTREAM of it. The
  binding is field-carried, not map-derived (traversal reads `item.row_union_name` at
  `scheduler_drain.py:597`; `_maybe_row_union_token` bails when it is None at
  `processor.py:2791`).

  **Runtime behaviour measured, not inferred — it fails closed.** Static tracing predicted a
  silent half-group reaching `batch_experiment_compare`. Running the topology end to end
  disproves that: the run raises `OrchestrationInvariantError("Aggregation continuation work
  item missing current_node_id")` from `orchestrator/aggregation.py:94`, reached via
  `run_end_of_input_barrier_flush` -> `flush_remaining_aggregation_buffers`
  (`aggregation.py:250`) -> `_process_flush_results`. **No sink file is created at all** — no
  partial group, no wrong statistic, nothing leaked. The `current_node_id is None` guard at
  `:94` fires immediately before the line that would have passed coalesce-only fields
  (`:95-101`), so the drop is caught rather than acted on.

  What remains wrong is the DIAGNOSTIC, not the durability: an author who writes a buildable
  YAML gets an internal invariant error naming neither the row_union, nor the aggregation, nor
  the fact that a branch-internal aggregation feeding a row_union is unsupported. Preferred fix
  is therefore a build-time rejection in the shape of the existing group-split guard — name the
  aggregation, the row_union, the hazard and the supported alternative — rather than threading
  the binding through six flush sites, which is production work for a surface the spike defers.
  The six sites, for whoever does the production delivery: `_FlushContext`
  (`processor.py:202-226`) and its `__post_init__` (`:237-244`), `_derive_coalesce_from_tokens`
  (`:1002-1016`), the two `create_continuation` calls (`:1430-1437`, `:1610-1617`),
  `_process_batch_aggregation_node` (`:2162-2163`), and `_process_flush_results`
  (`orchestrator/aggregation.py:95-101`). Note `AggregationProcessorPort.process_token` is
  typed `(*args: Any, **kwargs: Any)` (`ports.py:74`), so mypy cannot see a dropped kwarg on
  that seam.
- **No composer/web authoring surface.** `src/elspeth/composer_mcp/` contains no occurrence of `row_union` at all, and in `src/elspeth/web/` (including `web/frontend/`) it appears only in `preflight.py`'s runtime-graph threading and an acceptance-receipt label — nothing in composer state, importer, generator, MCP tools, guided authoring, or the frontend graph/wire surfaces. YAML is the only authoring path. (Tracked with the rest of the deferred surfaces on elspeth-a5b86149d4.)

Notable verification finding: adding `row_unions` to `ElspethSettings` rotated `semantic_settings_sha256` for every recorded run (full-dump settings material). Resolved in the corpus semantic lens (`tests/fixtures/dag_scenario_corpus/harness.py::_semantic_run_settings` drops post-pin empty sections) so the Wave-owned manifest pins stay untouched; the corpus process owns any holistic re-baseline (elspeth-ef29ef6ba4).

## Settings + YAML shape

`RowUnionSettings` (new, in `core/config.py` beside `CoalesceSettings:855`), `frozen`, `extra: forbid`:

```python
name: str
branches: dict[str, str]      # ordered; list form normalizes to identity dict (mode="before"), min 2 —
                              # same shape as CoalesceSettings.branches: branch_name -> input connection
                              # the union reads for that branch (enables per-variant transform chains)
on_success: str               # REQUIRED: the connection the released group continues on
timeout_seconds: float | None = None   # gt=0; None = wait until EOF flush
```

No policy/merge/quorum/select fields — v1 is `require_all` + pass-through by definition. `ElspethSettings` gains `row_unions: list[RowUnionSettings]` (`max_length=100`, default empty, YAML key `row_unions:`) + a line in `validate_globally_unique_node_names` (`config.py:1824-1846`). `_NODE_COLLECTION_KEYS` in `core/blobs_inline.py:38` is NOT extended (row_union carries no blob-bearing options).

Acceptance-scenario YAML (also the e2e test body):

```yaml
gates:
- name: variant_fork
  input: rows
  condition: "True"
  routes: {'true': fork}
  fork_to: [control_branch, treatment_branch]
transforms:
- name: tag_control     # per-variant chain: tags prompt_variant + score
  input: control_branch
  on_success: control_scored
  ...
- name: tag_treatment
  input: treatment_branch
  on_success: treatment_scored
  ...
row_unions:
- name: variant_union
  branches: {control_branch: control_scored, treatment_branch: treatment_scored}
  on_success: experiment_in
aggregations:
- name: prompt_experiment
  plugin: batch_experiment_compare
  input: experiment_in
  on_success: output
  on_error: discard
  trigger: {}            # end-of-source only — count/timeout/condition rejected downstream of row_union
  options: {variant_field: prompt_variant, score_field: score, baseline_variant: control}
```

## Task breakdown (TDD inside each task; aggressive-spike granularity)

### Task 1: contracts
**Files:** modify `src/elspeth/contracts/enums.py:91` (+`ROW_UNION = "row_union"`), `src/elspeth/contracts/types.py:18` (+`RowUnionName = NewType("RowUnionName", str)`), `src/elspeth/contracts/__init__.py` re-exports, new `src/elspeth/contracts/row_union_metadata.py` (`RowUnionFailureReason` sibling of `CoalesceFailureReason`).
**Produces:** `NodeType.ROW_UNION`, `RowUnionName`, `RowUnionFailureReason(failure_reason, expected_branches, branches_arrived, timeout_ms)`.
Tests: `tests/unit/contracts/test_row_union_contracts.py`.

### Task 2: config
**Files:** modify `src/elspeth/core/config.py` (RowUnionSettings + `ElspethSettings.row_unions` + unique-names).
**Produces:** `RowUnionSettings` as specified above; `load_settings_from_yaml_string` accepts `row_unions:`.
Tests: `tests/unit/core/test_row_union_config.py` — list normalization, <2 branches rejected, missing on_success rejected, duplicate node name across types rejected, timeout gt0.

### Task 3: graph build + validation
**Files:** modify `src/elspeth/core/dag/builder.py` (node creation mirroring coalesce block `:480-539`; fork-branch third arm at `:572`; branch-plan handling incl. transform-chain form; producer registration for `on_success`; update the no-destination error text at `:598` to name row_unions), `src/elspeth/core/dag/graph.py` (`set/get_row_union_id_map`, `get_branch_to_row_union_map`, structural whitelist `:296`, `from_plugin_instances` facade param `:640-695`), `src/elspeth/core/dag/schema_validation.py` (skip-arm like coalesce `:99`; pass-through observed schema — NO merged-schema synthesis), aggregation-trigger guard (reject `has_count/has_timeout/has_condition` triggers on aggregations reachable from a row_union without an intervening SINK), and `settings.row_unions` threading at every production graph-construction site. (Correction, 0f6159bcc: that is six sites, not the two named here — `src/elspeth/cli.py`'s run, `bootstrap_and_run` and validate paths, BOTH resume graphs, and the web factory `src/elspeth/web/execution/preflight.py::build_runtime_graph`. `src/elspeth/plugins/infrastructure/runtime_factory.py` constructs no graph and was never the right site. Both resume graphs matter beyond consistency: each must reproduce the original run's topology, so omitting the row_union nodes would silently break resume for any run that used one.)
**Produces:** `build_execution_graph(..., row_union_settings=...)`; graph maps for wiring.
Tests: `tests/unit/core/test_dag_row_union.py` — build succeeds (identity + chain forms), fan-in allowed, branch-not-produced rejected, no-destination message lists row_unions, trigger guard fires for `count` trigger, `end_of_source` accepted.

### Task 4: RowUnionExecutor
**Files:** new `src/elspeth/engine/row_union_executor.py` per the executor design section above.
**Produces:** `RowUnionOutcome`, `RowUnionExecutor(register_row_union, accept, check_timeouts, flush_pending, notify_branch_lost, get_registered_names)` — signatures mirror `CoalesceExecutor` (`coalesce_executor.py:523,745,1347,1406,1484`) minus merge/policy params.
Tests: `tests/unit/engine/test_row_union_executor.py` (fakes patterned on `tests/unit/engine/test_coalesce_executor.py`) — hold-then-release order, duplicate arrival crash, late arrival fail-closed, timeout fails whole group, EOF flush fails incomplete group, lost branch fails group, released tokens are the SAME TokenInfo objects (identity preserved), node states COMPLETED on release with no terminal token outcomes.

### Task 5: engine wiring
**Files:** modify `src/elspeth/engine/orchestrator/graph_wiring.py:31,178` (structural set + node_to_next), `src/elspeth/engine/dag_navigator.py` (row_union map lookups), `src/elspeth/engine/processor.py` (constructor params, `_maybe_row_union_token`, `_complete_row_union_fire`, branch-loss twin), `src/elspeth/engine/barrier_coordination.py` (intake third arm + `_adopt_row_union_row` + `_fire_row_union_release`; recovery fail-closed guard), `src/elspeth/engine/orchestrator/outcomes.py` (timeout/EOF pump beside `:377`/`:427`), `src/elspeth/engine/orchestrator/processor_factory.py` (construct+inject executor), `run_state.py`/`graph_registration.py`/`source_iteration.py` (map threading).
**Produces:** end-to-end drain path: hold → BLOCKED → intake adopt → accept → atomic N-ready release via one `complete_barrier`.
Tests: covered primarily by Task 6 e2e; unit tests only for the recovery guard (BLOCKED row with row_union barrier_key → `OrchestrationInvariantError`).

### Task 6: e2e acceptance
**Files:** new `tests/integration/pipeline/test_row_union_ab_experiment.py` on the `load_settings_from_yaml_string` → `instantiate_plugins_from_config` → `ExecutionGraph.from_plugin_instances` → `assemble_and_validate_pipeline_config` → `Orchestrator(db).run(...)` harness (pattern: `tests/integration/pipeline/test_field_resolution_union.py`).
Cases: (1) two-variant compare — N rows fork → per-variant tag chains → row_union → `batch_experiment_compare` (end_of_source) → sink; assert comparison row fields (`baseline_variant`, `variant_mean`, `mean_delta`, …) and audit integrity (every released token completes; `rows_succeeded` consistent; no partial groups). (2) same topology into `batch_paired_preference` (`pair_field: id`). (3) rejection case — `trigger: {count: N}` downstream of row_union fails at build with the group-split diagnostic.

### Task 7: verify + record
Focused suites (`tests/unit/contracts`, `tests/unit/core/test_row_union*`, `tests/unit/engine/test_row_union*`, the new integration file, plus `tests/unit/engine/test_coalesce_executor.py` and `tests/property/audit/test_fork_coalesce_flow.py` as adjacency), `ruff check` + `mypy` on touched files, commit series, filigree comment on elspeth-a5b86149d4.
