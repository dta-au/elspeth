# Consumer roster — tri-field / branch_name retirement inventory

**Date:** 2026-08-21. **Input to:** the unified-lineage implementation plan.
**Authority:** `docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md`
(rev 3.2, rulings 1–28 final).
**Measured at:** branch `release/0.7.2`, HEAD `add597342`. All line numbers verified by
reading the live tree, not carried from the spec.

Scope note: the prompt's "web/frontend/src/" root does not exist at the repo top level —
the frontend lives at `src/elspeth/web/frontend/src/`, so it is covered by the `src/`
grep. Anyone re-running these greps with a literal `web/frontend/src/` pathspec gets
zero hits and a false all-clear.

Aggregate (src/ only, tests excluded): `fork_group_id` 205 occurrences / 40 files;
`join_group_id` 223 / 42; `expand_group_id` 195 / 38; `branch_name` 568 / 56.
Union: **70 distinct files**. (The spec's "~623 src tri-field refs across ~45 files"
matches the three tri-field names, 623 = 205+223+195; `branch_name` is on top of that
and is majority config-vocabulary.)

---

## 1. Per-table column facts (`src/elspeth/core/landscape/schema.py`)

### 1.1 `tokens` (table def :605-636)

| Line | Column / constraint |
|---|---|
| :611 | `Column("fork_group_id", String(64))` — DELETE per spec §4.3 |
| :612 | `Column("join_group_id", String(64))` — **STAYS** (merged-token audit row; anchors the `coalesce_effects` FK, spec §4.1/§4.3) |
| :613 | `Column("expand_group_id", String(32), nullable=True, index=True)  # For deaggregation` — DELETE |
| :614 | `Column("branch_name", String(64))` — DELETE |
| :630-636 | `Index("uq_tokens_coalesce_result_identity", token_id, run_id, join_group_id, unique=True)` — the composite-FK **target** for `coalesce_effects`; must survive with the retained column |

### 1.2 `token_outcomes` (table def :640-673)

| Line | Column |
|---|---|
| :663 | `Column("fork_group_id", String(64))` — DELETE |
| :664 | `Column("join_group_id", String(64))` — DELETE |
| :665 | `Column("expand_group_id", String(64))` — DELETE |
| :670 | `Column("expected_branches_json", Text)` — "Branch contract for FORKED/EXPANDED outcomes (enables recovery validation)". **Not mentioned by the spec** — it is the durable fork-roster record that `_reconcile_fork_replay` reads; its disposition must be decided with the tri-columns (see Risk notes). |

No `branch_name` column on `token_outcomes`. `ix_token_outcomes_terminal_unique` (:677-683)
is keyed on `token_id` alone under `completed == 1` — unaffected by the retirement.

### 1.3 `token_work_items` (table def :705-767)

| Line | Column |
|---|---|
| :725 | `Column("branch_name", String(128))` — DELETE per §4.3 matrix |
| :726 | `Column("fork_group_id", String(128))` — DELETE |
| :727 | `Column("join_group_id", String(128))` — see the §4.1-vs-§4.3 tension in Risk notes |
| :728 | `Column("expand_group_id", String(128))` — DELETE |
| :729-731 | `coalesce_node_id` / `coalesce_name` / `row_union_name` — barrier BINDING, **KEEP** |
| :718 | `barrier_key` — KEEP |
| :740 | `barrier_adopted_epoch` — KEEP (CAS fence) |

**Live `join_group_id` query predicate — `schema.py:848-855`** (inside the pending-terminal
validity clause; this is the ":851 predicate" the spec re-points at the work item's
barrier fields):

```python
            and_(
                token_work_items_table.c.pending_outcome == TerminalOutcome.SUCCESS.value,
                token_work_items_table.c.pending_path == TerminalPath.COALESCED.value,
                token_work_items_table.c.join_group_id.is_not(None),
                token_work_items_table.c.join_group_id != "",
                no_error_evidence,
            ),
```

The COALESCED arm's prose contract also appears in
`core/landscape/scheduler/leases.py:83` ("...and join_group_id for COALESCED.") — a
message-text sibling of the same rule.

### 1.4 `coalesce_branch_losses` (table def :1037-1064) — replaced wholesale by `group_losses`

| Line | Fact |
|---|---|
| :1044 | `Column("coalesce_name", String(128), nullable=False)` |
| :1045 | `Column("row_id", String(64), nullable=False)` |
| :1046 | `Column("branch_name", String(128), nullable=False)` |
| :1051 | `Column("adopted_epoch", Integer)  # NULL = not yet replayed into leader memory` |
| :1057-1064 | `Index("uq_coalesce_branch_losses_natural", run_id, coalesce_name, row_id, branch_name, unique=True)` — the natural key the `group_losses` UNIQUE `(run_id, closer_name, group_id, member_key)` replaces |

### 1.5 `coalesce_effects` (table def :1193-1233)

| Line | Fact |
|---|---|
| :1206 | `Column("result_join_group_id", String(64), nullable=False)` |
| :1218 | `UniqueConstraint("result_join_group_id", name="uq_coalesce_effects_result_group")` |
| :1228-1232 | composite FK `["result_token_id", "run_id", "result_join_group_id"]` → `["tokens.token_id", "tokens.run_id", "tokens.join_group_id"]` (`fk_coalesce_effects_result_identity`) — the constraint that keeps `tokens.join_group_id` alive (spec §4.1) |

---

## 2. git grep rosters (src/, frontend included)

Columns: occurrence counts for **F**=`fork_group_id`, **J**=`join_group_id`,
**E**=`expand_group_id`, **B**=`branch_name`. Classification vocabulary per the plan
brief:

- **RETIRE-REWRITE** — reads/writes the stored field or column; must be rewritten
  against `lineage_path` / `token_lineage_frames` / work-item barrier fields.
- **ACCESSOR-OK** — reads the field off `TokenInfo`/`Token` by call syntax that the
  derived accessors preserve; no rewrite needed beyond the §4.1a delta review.
- **CONFIG-LEVEL-KEEP** — `branch_name` as config vocabulary (declared branch lists,
  builder maps, composer validation), never a token field; stays.
- **AUDIT-SURFACE** — queries/exporters/read-models over the audit columns; re-point at
  `token_lineage_frames` / `group_records` / `group_losses`.

### 2.1 Engine + contracts (the WS1/WS3 core)

| File | F | J | E | B | Class | Notes |
|---|---|---|---|---|---|---|
| `contracts/identity.py` | 4 | 4 | 4 | 4 | RETIRE-REWRITE | `TokenInfo` itself (:36-39 fields, :67-70 lineage tuple, docstrings :21-24, :92-93) |
| `contracts/scheduler.py` | 2 | 2 | 2 | 3 | RETIRE-REWRITE | `TokenWorkItem` :138-141; `BranchLossSpec` :70-86 (replaced by `GroupLossSpec`) |
| `contracts/audit.py` | 7 | 7 | 7 | 1 | RETIRE-REWRITE | `Token` :256-259, `TokenOutcome` :1399-1401, `_DISCRIMINATOR_FIELDS` :1460-1462, ADR-019 pair-constraint matrix :1471-1533, record-args :1551-1580 — see Risk note 2 |
| `contracts/export_records.py` | 2 | 2 | 2 | 1 | AUDIT-SURFACE | `TokenExportRecord` :192-195, `TokenOutcomeExportRecord` :218-220 (+`expected_branches_json` :223) |
| `contracts/sink_effects.py` | 1 | 1 | 1 | 0 | RETIRE-REWRITE | `SinkEffectFinalizationMember` tri-fields :572-574 — **not named by spec**, Risk note 7 |
| `contracts/engine.py` | 0 | 1 | 1 | 0 | KEEP (verify) | `CommittedAggregationChild.expand_group_id` :63, `CommittedCoalesceEffect.result_join_group_id` :106 — receipts bound to `batches.expansion_group_id` / `coalesce_effects`, both of which the spec KEEPS; must not be swept by a name-based guard (Risk note 8) |
| `contracts/union_merge.py` | 0 | 0 | 0 | 10 | CONFIG-LEVEL-KEEP | union merge vocabulary keyed by declared branch |
| `engine/processor.py` | 6 | 7 | 9 | 37 | RETIRE-REWRITE | resume-start dispatch :2881-2945 (excerpt §3.1); row_union retain-identity :3043-3048 (retired by ruling 27); branch maps `_branch_to_sink`/`_branch_to_coalesce` are config-derived (survive via registry views) |
| `engine/tokens.py` | 4 | 1 | 4 | 2 | RETIRE-REWRITE | `fork_token`/`expand_token`/`coalesce_tokens` destructive drop semantics → frame push/pop |
| `engine/token_traversal.py` | 0 | 0 | 1 | 13 | ACCESSOR-OK (pinned) | discriminators :782/:849 keep call syntax via accessor but decisions must be pinned per §4.1a; `expand_token` caller :241; binding-survives-expansion posture :254-262 becomes a §7 rule-5 rejection |
| `engine/executors/sink.py` | 0 | 1 | 0 | 0 | RETIRE-REWRITE | :760 — join context must ride the buffered entry per §4.1 (excerpt §3.5) |
| `engine/executors/gate.py` | 1 | 0 | 0 | 0 | ACCESSOR-OK | :214 `fork_token` caller unpacks the returned `fork_group_id`; frame logic stays inside `TokenManager` |
| `engine/orchestrator/outcomes.py` | 0 | 2 | 0 | 0 | RETIRE-REWRITE | :257-258 COALESCED invariant (hot path — must become work-item/RowResult carrier, never a DB read; excerpt §3.4) |
| `engine/orchestrator/resume.py` | 2 | 2 | 2 | 2 | RETIRE-REWRITE | :356 lineage-field filter (excerpt §3.2) |
| `engine/orchestrator/processor_factory.py` | 0 | 0 | 0 | 3 | CONFIG-LEVEL-KEEP | branch→schema-config maps :248-254 |
| `engine/scheduler_drain.py` | 3 | 3 | 3 | 5 | RETIRE-REWRITE | WS3: `take_claim_branch_loss` token-equality guard :996-1006 retired with `BranchLossSpec` |
| `engine/scheduler_work_codec.py` | 4 | 4 | 4 | 4 | RETIRE-REWRITE | codec field block :68-71, :108-111, :130-133, :146-149 — `lineage_path` serialization lands here |
| `engine/barrier_coordination.py` | 0 | 0 | 0 | 8 | RETIRE-REWRITE | `loss.branch_name` → `member_key` (:737, :778, :821, :1499); `scope_group_failed` emission sites :447-479/:1438 |
| `engine/coalesce_executor.py` | 0 | 0 | 0 | 43 | ACCESSOR-OK + CONFIG + WS4 | token reads (:789-799 arrival check, :877-896 duplicate/pending) survive via accessor; `settings.branches` loops are config; pending-state re-key `(coalesce_name, row_id)`→`(coalesce_name, fork_group_id)` at :511-512, :577, :803/:811, :1460-1472/:1516 per §5 |
| `engine/row_union_executor.py` | 1 | 0 | 0 | 34 | ACCESSOR-OK + WS4 | token.branch_name reads :220-238, :367-376, :412-428; `_recorded_losses` keyed `(name, row_id, branch_name)` :601-603/:692-694 re-keys with the ledger |
| `engine/journal_restore.py` | 0 | 0 | 0 | 13 | RETIRE-REWRITE | validates `item.branch_name` against `settings.branches` on barrier restore (:59, :175-232, :271) — reads the DELETED work-item column; **not named by spec**, Risk note 5 |
| `engine/work_items.py` | 0 | 0 | 0 | 6 | ACCESSOR-OK | `token.branch_name` → `resolve_branch_first_node` (:123-132); invariant message :127-128 |
| `engine/dag_navigator.py` | 0 | 0 | 0 | 4 | CONFIG-LEVEL-KEEP | `_branch_first_node` map :264-283 |
| `testing/__init__.py` | 0 | 0 | 0 | 2 | RETIRE-REWRITE | public test-factory kwargs mint tokens with `branch_name` (:403, :412) — Risk note 9 |

### 2.2 Landscape / persistence

| File | F | J | E | B | Class | Notes |
|---|---|---|---|---|---|---|
| `core/landscape/schema.py` | 3 | 10 | 3 | 4 | RETIRE-REWRITE | §1 above — the column deletions themselves |
| `core/landscape/data_flow/tokens.py` | 28 | 26 | 21 | 17 | RETIRE-REWRITE | token INSERT + all replay predicates: `create_token` invariants :363-378 (excerpt §3.6), `_reconcile_fork_replay` :546-587 (excerpt §3.7), expand-child INSERT :1375-1385 (excerpt §3.8), expand idempotency :1254-1330; becomes the `token_lineage_frames` writer |
| `core/landscape/data_flow/outcomes.py` | 9 | 9 | 9 | 0 | RETIRE-REWRITE | writes the `token_outcomes` tri-columns + enforces the ADR-019 matrix — **not named by spec**, Risk note 1 |
| `core/landscape/data_flow_repository.py` | 6 | 6 | 4 | 2 | RETIRE-REWRITE | facade pass-through of the same fields — not named, Risk note 12 |
| `core/landscape/scheduler/queue.py` | 12 | 12 | 12 | 12 | RETIRE-REWRITE | work-item column reads/writes (named in spec) |
| `core/landscape/scheduler_repository.py` | 10 | 10 | 10 | 10 | RETIRE-REWRITE | disposition plumbing incl. singular `branch_loss` param :492-620 (WS3) |
| `core/landscape/scheduler/work_items.py` | 4 | 4 | 4 | 4 | RETIRE-REWRITE | named in spec (journal restore set) |
| `core/landscape/scheduler/restore_read_model.py` | 0 | 5 | 0 | 4 | RETIRE-REWRITE | joins `token_outcomes.join_group_id` :330/:347 and `tokens.join_group_id` :355-401 against `coalesce_effects.result_join_group_id` — the outcomes side loses its column |
| `core/landscape/scheduler/payload_codec.py` | 1 | 1 | 1 | 1 | RETIRE-REWRITE | `token_from_journal_item` field block :96-99; codec-purity contract (§4.3) |
| `core/landscape/scheduler/dispositions.py` | 1 | 1 | 1 | 2 | RETIRE-REWRITE | `branch_loss` → per-frame collection :162-229 (WS3) |
| `core/landscape/scheduler/branch_losses.py` | 0 | 0 | 0 | 9 | RETIRE-REWRITE | replaced wholesale by the `group_losses` module; full-table restore pattern :193 is the stated requirement to carry |
| `core/landscape/scheduler/barrier.py` | 2 | 2 | 2 | 3 | RETIRE-REWRITE | barrier emission persistence dicts :703-706, :775-778; loss replay :487 — **not named by spec**, Risk note 6 |
| `core/landscape/scheduler/leases.py` | 0 | 1 | 0 | 0 | RETIRE-REWRITE (trivial) | error-message text :83 restating the :851 COALESCED rule |
| `core/landscape/database.py` | 1 | 3 | 2 | 2 | RETIRE-REWRITE | startup required-column verification lists :288, :394-397, :458, :591-593 — **not named by spec**, Risk note 4 |
| `core/landscape/model_loaders.py` | 3 | 3 | 3 | 1 | RETIRE-REWRITE | row→contract loaders :232-235, :588-590, :606-608 |
| `core/landscape/exporter.py` | 2 | 2 | 2 | 1 | AUDIT-SURFACE | token export :912-915, outcome export :950-952 (excerpt §3.9) |
| `core/landscape/lineage.py` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | explain projection `{"fork": ..., "join": ..., "expand": ...}` :79-81 |
| `core/landscape/lineage_text.py` | 0 | 0 | 0 | 2 | ACCESSOR-OK | `result.token.branch_name` render :29-30 |
| `core/landscape/query_repository.py` | 0 | 1 | 0 | 0 | AUDIT-SURFACE (trivial) | docstring :368 |
| `core/landscape/execution/sink_effect_identity.py` | 2 | 2 | 2 | 0 | RETIRE-REWRITE | `_Token` Protocol :49-56 (excerpt §3.10); identity inputs pinned pre/post per §4.1a |
| `core/landscape/execution/sink_effect_finalization.py` | 2 | 2 | 2 | 0 | RETIRE-REWRITE | writes finalization-member tri-fields into outcomes (named in spec) |
| `core/checkpoint/recovery.py` | 4 | 4 | 4 | 4 | RETIRE-REWRITE | `IncompleteTokenSpec` fields feeding the resume-start dispatch — a **WS1** consumer, not only the WS5 gate file (Risk note 13) |

### 2.3 DAG / config / composer (branch_name as vocabulary)

| File | F | J | E | B | Class | Notes |
|---|---|---|---|---|---|---|
| `core/dag/builder.py` | 1 | 0 | 0 | 58 | CONFIG-LEVEL-KEEP | branch declaration walks :554-572, :627-652, :681+; pairwise exclusivity checks subsumed by the binding registry (WS2); the row_union nested-fork walk :1462-1527 stays for unbound topologies |
| `core/dag/graph.py` | 0 | 0 | 0 | 29 | CONFIG-LEVEL-KEEP | |
| `core/dag/coalesce_merge.py` | 0 | 0 | 0 | 9 | CONFIG-LEVEL-KEEP | |
| `core/dag/coalesce_warnings.py` | 0 | 0 | 0 | 7 | CONFIG-LEVEL-KEEP | |
| `core/dag/row_union_warnings.py` | 0 | 0 | 0 | 2 | CONFIG-LEVEL-KEEP | |
| `core/config.py` | 0 | 0 | 0 | 10 | CONFIG-LEVEL-KEEP | `validate_branch_names` :1059-1067 (coalesce), :1254-1262 (row_union) |
| `web/composer/state.py` | 0 | 0 | 0 | 51 | CONFIG-LEVEL-KEEP | branch validation/lure prose (:339-348, :679-711, :1935-1943, :2095-2110, :3074+); WS2 lifts :6655-6699 to the builder. **Do not touch — maintainer is committing this file** |
| `web/composer/tools/_common.py` | 0 | 0 | 0 | 10 | CONFIG-LEVEL-KEEP | |
| `web/composer/guided/planning.py` | 0 | 0 | 0 | 7 | CONFIG-LEVEL-KEEP | |
| `web/composer/yaml_importer.py` | 0 | 0 | 0 | 2 | CONFIG-LEVEL-KEEP | |
| `web/composer/tools/transforms.py` | 0 | 0 | 0 | 1 | CONFIG-LEVEL-KEEP | |
| `web/composer/recipes.py` | 0 | 0 | 0 | 1 | CONFIG-LEVEL-KEEP | |
| `web/composer/skills/pipeline_capabilities.md` | 0 | 0 | 0 | 1 | CONFIG-LEVEL-KEEP | canonical-field-inventory doc; fires the three-pin when `collectors:`/`scopes:` land |
| `web/execution/_validation_diagnostics.py` | 0 | 0 | 0 | 2 | CONFIG-LEVEL-KEEP | imports `_coalesce_branch_names` :24/:477 |

### 2.4 Read surfaces (web / MCP / TUI / frontend)

| File | F | J | E | B | Class | Notes |
|---|---|---|---|---|---|---|
| `web/execution/schemas.py` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | wire schema :906-909 |
| `web/execution/diagnostics.py` | 2 | 2 | 2 | 2 | AUDIT-SURFACE | tokens-table select :417-420, record build :774-777 |
| `web/execution/accounting.py` | 2 | 2 | 2 | 0 | AUDIT-SURFACE | census shape flags on `token_outcomes` columns :240-242, :269-271 |
| `mcp/types.py` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | `TokenRecord` TypedDict :79-83 — not individually named (spec names `mcp/analyzers/*`), Risk note 12 |
| `mcp/analyzers/queries.py` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | list_tokens projection :199-211 (excerpt §3.11) |
| `mcp/analyzers/reports.py` | 3 | 3 | 0 | 0 | AUDIT-SURFACE | fork/join distinct counts :707-723 (excerpt §3.12) — re-derive from `token_lineage_frames` |
| `tui/widgets/lineage_tree.py` | 1 | 0 | 0 | 1 | AUDIT-SURFACE (trivial) | comment only :160-164 ("All data for DAG display is available (branch_name, fork_group_id, token_parents) but not yet consumed here") — doc update |
| `web/frontend/src/types/index.ts` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | token type :863-866 |
| `web/frontend/src/stores/executionStore.test.ts` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | fixture literal (the spec's "two test files") |
| `web/frontend/src/components/execution/RunsHistoryDrawer.test.tsx` | 1 | 1 | 1 | 1 | AUDIT-SURFACE | fixture literal |

---

## 3. Discriminator-site excerpts (live code, verified line numbers)

### 3.1 `engine/processor.py:2879-2945` — resume-start dispatch (the tri-field combination pattern)

```python
        branch = spec.branch_name

        if spec.expand_group_id is not None:
            # expand child: re-drive from the node AFTER the expand node.
            ...
            after = self._nav.resolve_next_node(self._resolve_step_node(spec))
            return self.process_token(token, ctx, current_node_id=after)

        if branch is not None and BranchName(branch) in self._branch_to_sink:
            # fork → sink terminal branch: straight to the sink via None-path routing.
            return self.process_token(token, ctx, current_node_id=None)

        if branch is not None and BranchName(branch) in self._branch_to_coalesce:
            # fork → coalesce, crashed BEFORE the barrier: ...
            coalesce_name = self._branch_to_coalesce[BranchName(branch)]
            first_node = self._nav.resolve_branch_first_node(branch)
            return self.process_token(token, ctx, current_node_id=first_node,
                                      coalesce_name=coalesce_name)

        if spec.join_group_id is not None and spec.fork_group_id is None and branch is None:
            # post-coalesce merged token, crashed AFTER the barrier (B1 review finding)
            ...

        raise OrchestrationInvariantError(
            f"Incomplete token {spec.token_id} has branch_name={branch!r}, "
            f"fork_group_id={spec.fork_group_id!r}, join_group_id={spec.join_group_id!r}, "
            f"expand_group_id={spec.expand_group_id!r} — no resume-start node resolvable. "
            f"Audit/DAG inconsistency.")
```

Also in this window: TokenInfo reconstruction from spec fields at :2868-2878 (all four
lineage kwargs). The fall-through raise is at :2940-2945 (the spec's ":2881/:2906 raises
on unknown patterns" refers to the two guarded arms plus this raise). Note the arm ORDER
is load-bearing: expand is checked FIRST because "Expanded children inherit branch_name
from fork branches" (:2883-2884) — under the preservative accessors both facts will be
simultaneously visible on more topologies, so a path-aware rewrite must pin arm
selection, not just field values (§4.1a).

### 3.2 `engine/orchestrator/resume.py:352-360` — the lineage-field filter

```python
        fork_expand_coalesce_specs = (
            [
                s
                for s in incomplete_by_row[row_id]
                if s.branch_name is not None or s.fork_group_id is not None
                   or s.expand_group_id is not None or s.join_group_id is not None
            ]
            if row_id in incomplete_by_row
            else []
        )
```

Comment block :341-348 documents the F1 regression this guards: a linear token with all
four fields None must go to `process_existing_row`, never `resume_incomplete_token`.
Path equivalent: `lineage_path == ()`.

### 3.3 `engine/token_traversal.py:781-793` and :847-855 — branch→sink routing on `branch_name is not None`

```python
        effective_sink = current_on_success_sink
        if current_token.branch_name is not None:
            branch = BranchName(current_token.branch_name)
            if branch in self._processor._branch_to_sink:
                effective_sink = self._processor._branch_to_sink[branch]

        if not effective_sink or not effective_sink.strip():
            raise OrchestrationInvariantError(
                f"No effective sink for token {current_token.token_id}: "
                f"last_on_success_sink={current_on_success_sink!r}, "
                f"branch_name={current_token.branch_name!r}. ...")
```

```python
        if current_node_id is None:
            has_branch_sink = (
                current_token.branch_name is not None
                and BranchName(current_token.branch_name) in self._processor._branch_to_sink
            )
            if on_success_sink is None and not has_branch_sink:
                raise OrchestrationInvariantError(...)
```

Both keep call syntax under the derived accessor, but the VALUE changes on two §4.1a
rows: an expand child inside a fork branch (durable row said None, accessor says the
branch) and a released row_union token (today retained, popped under ruling 27).

### 3.4 `engine/orchestrator/outcomes.py:257-258` — COALESCED accounting invariant (hot path)

```python
        elif pair == (TerminalOutcome.SUCCESS, TerminalPath.COALESCED) and result.token.join_group_id is None:
            raise OrchestrationInvariantError(f"(SUCCESS, COALESCED) result missing token.join_group_id. Token: {result.token}")
```

Reads `result.token.join_group_id` off `TokenInfo` — the field that LEAVES `TokenInfo`
(§4.1). Replacement: the in-memory carrier (`RowResult` field or `TokenWorkItem`);
pinned commitment: never a DB query here.

### 3.5 `engine/executors/sink.py:752-764` — sink finalization join context

```python
        finalization_members = tuple(
            SinkEffectFinalizationMember(
                ordinal=member.ordinal,
                output_data={"row": dict(member.row)},
                duration_ms=0.0,
                outcome=pending_outcome.outcome,
                path=pending_outcome.path,
                sink_name=sink_name,
                join_group_id=(token_by_id[member.token_id].join_group_id
                               if pending_outcome.path is TerminalPath.COALESCED else None),
                error_hash=pending_outcome.error_hash,
            )
            for member in identity.members
        )
```

`token_by_id` is built from SinkExecutor's buffered TokenInfos (:751) — the spec's "join
context rides the buffered entry" replacement lands exactly here.

### 3.6 `core/landscape/data_flow/tokens.py:362-378` — `create_token` mutual-exclusivity checks (retired with the columns)

```python
        # Validate lineage metadata invariants (Tier 1 write-side enforcement)
        # The read side (explain) assumes these are mutually exclusive.
        group_ids = [gid for gid in (fork_group_id, join_group_id) if gid is not None]
        if len(group_ids) > 1:
            raise AuditIntegrityError(
                f"create_token: conflicting lineage metadata — at most one of "
                f"fork_group_id, join_group_id may be set. ...")

        # branch_name requires fork_group_id (it names which fork branch this token is on)
        if branch_name is not None and fork_group_id is None:
            raise AuditIntegrityError(f"create_token: branch_name={branch_name!r} requires fork_group_id to be set")

        # Reject empty-string group IDs (should be None, not "")
        for name, value in [("fork_group_id", fork_group_id), ("join_group_id", join_group_id)]:
            if value is not None and not value.strip():
                raise AuditIntegrityError(f"create_token: {name} must be None or non-empty, got {value!r}")
```

Note the comment: "The read side (explain) assumes these are mutually exclusive" —
`core/landscape/lineage.py:79-81` is that read side; both move together.

### 3.7 `core/landscape/data_flow/tokens.py:557-587` — `_reconcile_fork_replay` (Tier-1 replay predicate)

```python
        fork_group_id = outcome["fork_group_id"]
        try:
            recorded_branches = json.loads(outcome["expected_branches_json"])
        except (TypeError, ValueError):
            recorded_branches = None
        children = self._load_children_for_parent(conn, parent_ref=parent_ref)
        exact = (
            outcome["outcome"] == TerminalOutcome.TRANSIENT.value
            and outcome["path"] == TerminalPath.FORK_PARENT.value
            and isinstance(fork_group_id, str) and bool(fork_group_id)
            and recorded_branches == list(branches)
            and len(children) == len(branches)
            and all(
                child.row_id == row_id
                and child.run_id == parent_ref.run_id
                and child.fork_group_id == fork_group_id
                and child.join_group_id is None
                and child.expand_group_id is None      # <- the :575 assertion retired WITH the column
                and child.branch_name == branch
                and child.step_in_pipeline == step_in_pipeline
                and ordinal == expected_ordinal
                for expected_ordinal, ((child, ordinal), branch) in enumerate(zip(children, branches, strict=True))
            )
        )
        if not exact:
            raise AuditIntegrityError(...)
```

This predicate reads BOTH the `token_outcomes` tri-columns (`outcome["fork_group_id"]`,
`expected_branches_json`) and the `tokens` tri-columns (child fields) — both sides are
deleted, so the rewrite compares persisted frames (parent's + own FORK frame) per §4.4.

### 3.8 `core/landscape/data_flow/tokens.py:1374-1385` — expand-child INSERT (durable row carries neither branch_name nor fork_group_id)

```python
                # Create child token with expand_group_id (run_id from parent -- already validated)
                result = conn.execute(
                    tokens_table.insert().values(
                        token_id=child_id,
                        row_id=row_id,
                        run_id=parent_ref.run_id,
                        expand_group_id=expand_group_id,
                        step_in_pipeline=step_in_pipeline,
                        created_at=timestamp,
                        token_data_ref=payload_ref,
                    )
                )
```

This is the verified fact behind §4.1a's "the durable tokens row for an expand child
carries neither" — the in-memory/durable disagreement the accessors resolve in favour of
the in-memory truth.

### 3.9 `core/landscape/exporter.py:906-917` and :938-956 — export records

```python
                token_record: TokenExportRecord = {
                    "record_type": "token", "run_id": run_id,
                    "token_id": token.token_id, "row_id": token.row_id,
                    "step_in_pipeline": token.step_in_pipeline,
                    "branch_name": token.branch_name,
                    "fork_group_id": token.fork_group_id,
                    "join_group_id": token.join_group_id,
                    "expand_group_id": token.expand_group_id,
                    "created_at": token.created_at.isoformat(),
                }
```

Outcome records at :950-952 also project the three `token_outcomes` columns plus
`expected_branches_json` (:955). Re-point at `token_lineage_frames` (and decide the
export record shape — the TypedDicts in `contracts/export_records.py:186-224` are the
wire contract).

### 3.10 `core/landscape/execution/sink_effect_identity.py:49-56` — the `_Token` Protocol

```python
class _Token(Protocol):
    token_id: str
    row_id: str
    run_id: str
    fork_group_id: str | None
    join_group_id: str | None
    expand_group_id: str | None
```

Structural type over the audit `Token` contract; sink-effect identity/lineage evidence
derives from it, so its identity inputs must be pinned pre/post per §4.1a (the spec's
":55" is this Protocol's field block).

### 3.11 `mcp/analyzers/queries.py:199-211` — list_tokens projection

```python
    return [
        {
            "token_id": row.token_id,
            "row_id": row.row_id,
            "branch_name": row.branch_name,
            "fork_group_id": row.fork_group_id,
            "join_group_id": row.join_group_id,
            "step_in_pipeline": row.step_in_pipeline,
            "expand_group_id": row.expand_group_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
```

### 3.12 `mcp/analyzers/reports.py:706-723` — fork/join counts over `token_outcomes`

```python
        # Fork/join counts (outcomes with fork_group_id or join_group_id, scoped to run)
        fork_count = (
            conn.execute(
                select(func.count(func.distinct(token_outcomes_table.c.fork_group_id)))
                .select_from(token_outcomes_table)
                .where((token_outcomes_table.c.run_id == run_id)
                       & (token_outcomes_table.c.fork_group_id.isnot(None)))
            ).scalar() or 0
        )
        join_count = (
            conn.execute(
                select(func.count(func.distinct(token_outcomes_table.c.join_group_id)))
                .select_from(token_outcomes_table)
                .where((token_outcomes_table.c.run_id == run_id)
                       & (token_outcomes_table.c.join_group_id.isnot(None)))
            ).scalar() or 0
        )
```

Re-derive: fork_count from `DISTINCT group_id WHERE kind=FORK` in
`token_lineage_frames`; join_count from `tokens.join_group_id` (which stays) or
`coalesce_effects`.

### 3.13 `contracts/scheduler.py:70-86` — `BranchLossSpec` (replaced by `GroupLossSpec`)

```python
@dataclass(frozen=True)
class BranchLossSpec:
    """Durable branch-loss record riding a lossy disposition (§E.5). ..."""

    coalesce_name: str
    row_id: str
    branch_name: str
    token_id: str
    reason: str
    recorded_by: str
```

---

## 4. `TokenWorkItem` field map (`contracts/scheduler.py:107-152`)

| Field (line) | Disposition | Why |
|---|---|---|
| `work_item_id` :117 … `updated_at` :129, `queue_key` :130, `on_success_sink` :132, `pending_sink_name`..`pending_error_message` :133-137 | KEEP | not lineage |
| `branch_name` :138 | **RETIRE** | lineage — derived from the path (§4.3 matrix) |
| `fork_group_id` :139 | **RETIRE** | lineage |
| `join_group_id` :140 | **KEEP** (contract field) | merge-EVENT attribute of a merged token's work item, not lineage (§4.1 replacement table; ruling 20). See Risk note 3 for the COLUMN-level tension |
| `expand_group_id` :141 | **RETIRE** | lineage |
| `barrier_key` :131 | KEEP | closer's address (barrier binding) |
| `coalesce_node_id` :142 | KEEP | barrier binding |
| `coalesce_name` :143 | KEEP | barrier binding |
| `row_union_name` :144 | KEEP | barrier binding |
| `lease_owner` :145, `lease_expires_at` :146 | KEEP | scheduling |
| `barrier_blocked_at` :147 | KEEP | barrier binding/journal |
| `barrier_adopted_epoch` :152 | KEEP | CAS fence (§5 duplicate-arrival skip) |
| *(new)* `lineage_path` | **ADD** | serializes onto the work item; `token_from_journal_item` reconstructs purely (§4.3, ruling 17) |

Codec twin: `engine/scheduler_work_codec.py:68-71` declares the same four fields in the
journal wire struct — it must move in the same change as the dataclass, plus
`core/landscape/scheduler/payload_codec.py:96-99` (decode side) and the
`token_work_items` column block `schema.py:725-728`.

---

## RISK NOTES — consumers found that the spec does NOT name

1. **`core/landscape/data_flow/outcomes.py`** (9 hits each tri-field) — the WRITER of
   the `token_outcomes` tri-columns and enforcement point of the ADR-019 pair
   constraints. Spec §9 WS1 lists `data_flow/tokens.py` but not this sibling. It is a
   mandatory WS1 file.
2. **`contracts/audit.py` `_TERMINAL_PAIR_FIELD_CONSTRAINTS` (:1471-1533)** — the spec
   names the file but not this matrix. `(TRANSIENT, FORK_PARENT)` REQUIRES
   `fork_group_id`, `(TRANSIENT, EXPAND_PARENT)` REQUIRES `expand_group_id`,
   `(SUCCESS, COALESCED)` REQUIRES `join_group_id`, and every other pair FORBIDS them
   (`_forbid_except`, :1467). Deleting the `token_outcomes` tri-columns guts the
   REQUIRED side of three ADR-019 pairs — the plan must re-specify what FORK_PARENT /
   EXPAND_PARENT outcomes carry (presumably the `group_records` group_id, or nothing
   plus a frames-table join) and update ADR-019, not just drop the fields.
3. **`token_work_items.join_group_id` — §4.3 vs §4.1 internal tension.** The §4.3
   retirement matrix says the work-item "tri-columns + branch_name DELETED" and the
   `:851` predicate "re-pointed at the work item's barrier fields", while §4.1 and the
   §4.3 closing sentence say "`join_group_id` stays on the work item for merged tokens".
   The CONTRACT field staying is unambiguous (ruling 20); whether the COLUMN stays (and
   whether :851 keeps using it vs. barrier fields) needs one explicit plan decision —
   both readings are currently supportable from the spec text.
4. **`core/landscape/database.py` required-column verification lists** (:288 tokens,
   :394-397 token_work_items, :458 coalesce_branch_losses, :591-593 the
   coalesce_effects composite-FK triple) — startup schema verification. Miss it and
   every run fails at open, or worse, the gate silently stops checking a live column.
   Not in any spec file list.
5. **`engine/journal_restore.py`** (13 `branch_name` hits) — barrier-hold restore
   validates `item.branch_name` against `settings.branches` (:175-232) and detects
   arrived-and-lost overlap (:271). It reads the DELETED work-item field. The spec's
   journal-restore list (`queue.py`, `work_items.py`, `restore_read_model.py`,
   `scheduler_work_codec.py`) omits this engine-side consumer.
6. **`core/landscape/scheduler/barrier.py`** — persists barrier emissions with all four
   fields (:703-706 dict form, :775-778 kwargs form) and replays losses by
   `loss.branch_name` (:487). Spec names `engine/barrier_coordination.py` but not this
   landscape-side sibling.
7. **`contracts/sink_effects.py` `SinkEffectFinalizationMember`** (:572-574) — carries
   all three tri-fields; only `join_group_id` is ever populated today
   (`executors/sink.py:760`, COALESCED only). Spec names the identity/finalization
   modules but not the contract dataclass they share. Decide: keep `join_group_id`
   (consistent with ruling 20), delete the other two.
8. **`contracts/engine.py`** — `CommittedAggregationChild.expand_group_id` (:63) and
   `CommittedCoalesceEffect.result_join_group_id` (:106) are RECEIPT fields bound to
   `batches.expansion_group_id` (which §4.4 KEEPS and extends to `group_records`) and
   `coalesce_effects` (kept). A grep-driven or lint-guard-driven sweep keyed on the
   NAMES `expand_group_id`/`join_group_id` must allowlist these, or it breaks
   idempotency machinery the spec deliberately preserves. Same class:
   `restore_read_model.py`'s joins against `coalesce_effects.result_join_group_id`
   partially survive (the tokens side stays, the token_outcomes side does not).
9. **`src/elspeth/testing/__init__.py`** (:403, :412) — the PUBLIC testing factory
   mints tokens with a `branch_name` kwarg. Every downstream test using it churns;
   the ~800 test-side references the spec prices ride partly on this seam.
10. **`schema.py:630-636` `uq_tokens_coalesce_result_identity`** — the tokens-side
    unique index that the `coalesce_effects` composite FK targets. Keeping
    `tokens.join_group_id` requires keeping this index; the retirement matrix mentions
    the column and FK but not the index.
11. **`token_outcomes.expected_branches_json` (:670)** — the durable fork-roster
    contract consumed by `_reconcile_fork_replay` (:559) and exported (:955,
    `export_records.py:223`). Under unification the fork roster's authority becomes
    config + `group_records`; this column's disposition (delete? repoint to
    `group_records.member_count`?) is unstated in the spec.
12. **Small unnamed consumers** to sweep, all trivial but each a silent-wrong-answer
    site if missed: `core/landscape/data_flow_repository.py` (facade pass-through),
    `core/landscape/scheduler/leases.py:83` (message text restating the :851 rule),
    `core/landscape/lineage_text.py:29-30` (accessor-ok), `mcp/types.py:79-83`
    (`TokenRecord` TypedDict — spec names `mcp/analyzers/*` only),
    `core/landscape/query_repository.py:368` (docstring),
    `tui/widgets/lineage_tree.py:162` (comment).
13. **`core/checkpoint/recovery.py` is a WS1 consumer, not only WS5** — its
    incomplete-token specs carry all four fields (4 hits each) and feed the
    `processor.py:2879` dispatch; the spec lists it only under WS5 (satisfiability
    gate). The resume-spec shape change must land with WS1's dispatcher rewrite.
14. **Accessor-value deltas hit ACCESSOR-OK files too.** `token_traversal.py:782/:849`,
    `work_items.py:123-132`, `coalesce_executor.py:789/:877`,
    `row_union_executor.py:367/:412` all branch on `token.branch_name`; the preservative
    accessors change the VALUE on the §4.1a delta rows (expand-inside-fork, post-release
    row_union) even where the syntax survives. "ACCESSOR-OK" therefore means "no
    rewrite", not "no review" — each is a §4.1a decision-pin candidate.
15. **Frontend greps need the real path.** `web/frontend/src/` resolves only as
    `src/elspeth/web/frontend/src/`; the three .ts/.tsx hits (types/index.ts:863-866 +
    two test fixtures) plus `guidedDecoder.ts`'s `exactRecord` lists (a §7 three-pin
    item, no tri-field hits today) are the whole frontend surface.
