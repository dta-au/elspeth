# Unified Lineage WS5+WS6 — Resume Protection & Group Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land Workstream 5 (the fail-closed group-satisfiability resume gate on both the advisory and enforcing surfaces, collector-aware drain, the resume-mid-group happy-path e2e, and the depth-5 mid-unwrap crash+resume e2e) and Workstream 6 (the group-settlement disposition vocabulary, the landscape-MCP query surface for `group_records`/`group_losses`/`token_lineage_frames`, and the depth-3 forensics acceptance test) of the unified group-lineage campaign.

**Architecture:** WS5 adds one shared gate function in `core/checkpoint/recovery.py` — the exact two-surface pattern already used by `check_run_status_resumable` (recovery.py:118) and `check_source_lifecycle_resumable` (recovery.py:209) — consumed by the advisory `RecoveryManager.can_resume` (recovery.py:403) and by a new read-only entry-guard arm in `ResumeCoordinator.resume()`. WS6 closes the settlement-reason vocabulary into one `StrEnum`, discriminates the two `late_arrival_after_merge` emission sites into merged-vs-group-failed, and re-points the MCP analyzer read surfaces onto the three new audit tables so an operator can reconstruct a nested group failure from audit rows alone.

**Tech Stack:** Python 3.12+, SQLAlchemy Core over the Landscape audit schema (SQLite + PostgreSQL), pytest, the `tests/e2e/recovery/` harness, the `mcp/` analyzer facade.

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md (rev 3.2 — rulings 1–28 final; §8 is WS5, §9 row 6 is WS6)

## Global Constraints

Copied from the campaign's standing constraints. Every task's requirements implicitly include this section.

- **Shared checkout:** stage by explicit pathspec ONLY (`git add <exact file paths>`, never `git add -A`/`-u`/`.`); commit only your own hunks. A sibling agent's files may be dirty in the same tree — most notably `src/elspeth/web/composer/state.py` and `tests/unit/web/composer/test_state.py`, which you must NEVER edit or stage.
- **Hooks:** never bypass pre-commit hooks except under the documented `--no-verify`-with-end-of-slice-reconciliation grant; `git stash` is blocked by hook — never attempt it.
- **Full suite at slice boundaries:** run the full `pytest tests/` (the CI-equivalent selection) at each slice boundary — whole-tree AST gates (attribute-contracts, masquerade, rejection-parity, redaction) miss scoped runs. Record `git rev-parse HEAD` BEFORE and AFTER the run; if they differ, the result is uninterpretable — re-run rather than diagnose.
- **Trust-tier corpus diff, add nothing:** before and after each slice run `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth` and diff the finding corpus. The gate exits 1 with a large corpus BY DESIGN (fail-closed state, elspeth-13f0cc04fb) — the obligation is a zero-added-findings diff, never a green exit.
- **Wardline gate:** `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` must exit 0 before handing back code that touches external input.
- **Judge signatures:** never hand-edit a `judge_metadata_signature`; do not stage judge-signature bundles during this campaign — the churn invalidates them; 0.7.2 allowlist signing sequences AFTER the campaign settles.
- **Depth cap rule:** the supported guarantee is 5 layers of bound-region nesting, enforced fail-closed by the builder (`GraphValidationError`), config-overridable. Deeper nesting is model-correct but unsupported.
- **Fixpoint-bound rule:** the escalation fixpoint's non-convergence bound is DERIVED at build from the actual configured depth (+ margin), never a constant — the ONE formula is WS2's `derive_escalation_fixpoint_bound(depth) = 1000 + 8 * depth` (`core/dag/bound_regions.py`), consumed via `graph.get_max_bound_region_depth()` and threaded as `PipelineConfig.escalation_fixpoint_bound`; `MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000` (leader_drain.py:417) must not be re-hardcoded anywhere WS5 touches.
- **Pre-1.0 break posture (ruling 22):** no backward compat, no shims, no dual reads; ruling casualties are migrated, not preserved.
- **Rolling doc:** any new convention or whole-tree trap you land goes into `docs/agents/recent-code-hints.md` IN THE SAME COMMIT.
- Standing procedures: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle sequencing, and the WS1 STOP rule.

## Cross-plan interfaces consumed (READ THIS FIRST)

WS5/WS6 run LAST in the campaign sequence (WS0 → WS1 → WS2 → WS3∥WS4 → WS3+WS4 integration → **WS5 → WS6**). Everything below is either a canonical contract (fixed by the spec — copy verbatim, do not rename) or a sibling-plan interface. The sibling plans, by file:

- `2026-08-21-unified-lineage-ws1a-model-core.md` / `2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint.md` — `LineageFrame`, `lineage_path`, `GroupLossSpec` (WS1a Task 3), `token_lineage_frames`, `group_records`, `group_losses_table` + `uq_group_losses_natural` (WS1a Task 4), the migrated shared test builders.
- `2026-08-21-unified-lineage-ws3-settlement.md` — `core/landscape/scheduler/group_losses.py` (`GroupLossRepository.list_group_losses(*, run_id: str, closer_names: frozenset[str] | None = None) -> list[GroupLoss]` and siblings, WS3 Tasks 2/7); the `BranchLossSpec`/`coalesce_branch_losses` retirement (WS3 Task 1).
- `2026-08-21-unified-lineage-ws4-collector.md` — Task 1 (consume-and-verify only; the ONE authored copy is WS2 Task 2): `CollectorSettings(name, plugin, input, on_success, on_error=None, options={})` — `input` and `on_success` REQUIRED, `on_error: str | None = None` (None = derives from structure per spec §7 rule 9) — and `ScopeSettings(name, opener, closer, policy, on_group_failure="quarantine")` (frozen, `extra="forbid"`); Tasks 4–5: `CollectorExecutor`; Task 6: `TokenWorkItem.collector_name: str | None` — the collector's barrier-binding address column — with collector rows' `barrier_key` formatted `collector:<name>:<group-id>` (NOT the bare closer name; Task 1 below depends on this fact), plus `tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py` and the leader_drain early-return guard extension; Task 7: `CollectorExecutor.restore_from_journal`.
- The **WS3+WS4 integration line item** (spec §11; recorded at the end of the WS4 plan as "Handed to the WS3+WS4 integration line item") — processor wiring, `_real_collector_executor`/`_collector_processor` shared builders, and the `_complete_collector_fire` pause seam. Task 5 of THIS plan implements its handed-off item (3) (the e2e death-matrix `collector` family); coordinate with that line item's owner so the family lands exactly once.
- `2026-08-21-unified-lineage-ws2-config-validation.md` (spellings verified against its Produces blocks 2026-08-22) — Task 2: the ONE authored `CollectorSettings`/`ScopeSettings` copy (fields as in the WS4 bullet above; `ScopeSettings.policy` REQUIRED with no default); Task 3: `ExecutionGraph.from_plugin_instances(..., collectors: Mapping[str, tuple[TransformProtocol, CollectorSettings]] | None = None, scope_settings: Sequence[ScopeSettings] | None = None, max_bound_region_depth: int = 5)`, `NodeType.COLLECTOR`, `graph.get_collector_id_map()`; Task 4: `graph.get_group_bindings() -> GroupBindingRegistry` (`CloserKind` StrEnum with `COALESCE="coalesce"`/`ROW_UNION="row_union"`/`COLLECTOR="collector"`; `GroupBinding.member_roster`; `binding_for(frame)` keyed lookup); Task 5: `derive_escalation_fixpoint_bound(depth) = 1000 + 8 * depth` (`core/dag/bound_regions.py` — the ONE fixpoint formula), `graph.set_max_bound_region_depth`/`get_max_bound_region_depth`, `PipelineConfig.escalation_fixpoint_bound` (the value `run_end_of_input_barrier_flush` iterates to; WS5's Task 5a clamps it to inject a mid-unwrap crash).

**Canonical contracts (fixed, spec §2/§4 — verbatim):**

```python
# contracts/enums.py (landed by WS1)
class FrameKind(StrEnum):
    FORK = "fork"
    EXPAND = "expand"

# contracts/identity.py (landed by WS1)
@dataclass(frozen=True, slots=True)
class LineageFrame:
    kind: FrameKind
    group_id: str        # non-empty; minted at the opening operation
    member_key: str      # FORK: branch name; EXPAND: member token_id

TokenInfo.lineage_path: tuple[LineageFrame, ...] = ()   # outermost first
# Derived read-only accessors on TokenInfo: branch_name / fork_group_id
# (innermost FORK frame), expand_group_id (innermost EXPAND frame).
# join_group_id has LEFT TokenInfo (rides TokenWorkItem / RowResult).

# contracts/scheduler.py (landed by WS1a Task 3)
GroupLossSpec(closer_name, group_id, member_key, token_id, reason)   # replaces BranchLossSpec (retired by WS3 Task 1)
```

**Canonical tables in `core/landscape/schema.py` (all landed by WS1a Task 4; `group_losses` is written from WS3):**

- `token_lineage_frames_table` — `(token_id, run_id, depth, kind, group_id, member_key)`, PK `(token_id, run_id, depth)`, INDEX `(run_id, group_id, member_key)`. Append-only mint record: one row per frame per token; a closer's pop is in-memory model semantics, it never deletes frames rows.
- `group_records_table` — `(run_id, group_id, kind, opener_token_id, member_count, created_at)`, PK `(run_id, group_id)`; minted for EVERY expansion including empty (`member_count=0`).
- `group_losses_table` — `(loss_id PK, run_id FK, closer_name, group_id, member_key, token_id, reason, recorded_by, recorded_at, adopted_epoch)`, UNIQUE `(run_id, closer_name, group_id, member_key)`.
- Kept columns: `tokens.join_group_id` (only), `token_work_items` barrier-binding fields `barrier_key` / `coalesce_name` / `row_union_name` / `collector_name` (the last added by WS4 Task 6), `token_work_items.join_group_id`.

**WS2 plan interface (binding registry — WS2 Task 4's Produces block, verified 2026-08-22):** the builder resolves ONE registry of bound groups (spec §3). This plan consumes it through exactly one seam, `group_binding_view_from_graph(graph)` (Task 2):

```python
graph.get_group_bindings() -> GroupBindingRegistry   # empty registry when no bound group exists
GroupBindingRegistry.bindings: tuple[GroupBinding, ...]
# GroupBinding fields consumed here (core/dag/group_bindings.py):
#   kind: FrameKind                 # FORK | EXPAND — the discriminator
#   opener_node_id: NodeID          # FORK: the fork gate node; EXPAND: the opener transform node
#   closer_name: str
#   closer_kind: CloserKind         # StrEnum: COALESCE="coalesce" | ROW_UNION="row_union" | COLLECTOR="collector"
#   member_roster: tuple[str, ...]
#       FORK binding: the fork's full declared branch list (== fork_to == closer's branches, ruling 23)
#       EXPAND binding: () — empty by contract (roster authority is group_records)
```

**WS4 + integration interfaces (collector executor):** `CollectorExecutor` (WS4 Tasks 4–5), registered via `register_collector(settings: CollectorSettings, scope: ScopeSettings, ...)`; collector arrivals persisted as BLOCKED `token_work_items` rows (with `collector_name` set — WS4 Task 6) through the same §E.2 journal-first adoption as coalesce; the `RowProcessor._complete_collector_fire` continuation seam (the analogue of `_complete_coalesce_fire`) and the shared test builders (`_real_collector_executor` / `_collector_processor` / `_seed_expand_group` / `_arrive_collector` / `_load_expand_members`, in `tests/integration/pipeline/` beside `test_barrier_intake_dispositions.py`) land with the WS3+WS4 integration line item — reconcile the exact names against that plan when it exists; if it exports no shared builders, Tasks 4–5 below say what to define locally. Also from WS4 (Tasks 8–11): the coalesce pending/dedup keys are re-keyed `(coalesce_name, row_id)` → `(coalesce_name, fork_group_id)` at coalesce_executor.py:512/:577/:803/:811/:1470/:1516.

**WS1/WS3 test-builder interfaces:** `tests/integration/pipeline/test_barrier_intake_dispositions.py` (`RUN_ID`, `_branch_token`, `_arrive_via_intake`, `_coalesce_processor`, `_real_coalesce_executor`, `_usurp_seat`) and `tests/unit/engine/test_processor.py` (`_make_processor`, `_persist_blocked_scheduler_work`) — migrated to `lineage_path` construction by WS1's early builder slice. `_branch_token("a")` returns a `TokenInfo` whose path is `(LineageFrame(FrameKind.FORK, <group>, "a"),)`.

**Verified-at-HEAD anchors used below** (HEAD 3ca6516e82; WS1–WS4 will shift absolute line numbers — the anchors name the code, re-locate by content): `recovery.py:118/:209/:403`, `resume.py:56-59/:736/:920/:963`, `leader_drain.py:417/:464/:511-518`, `barrier_coordination.py:447-482/:1415-1449`, `coalesce_executor.py:806-860/:1470/:1516`, `node_states.py:766-875`, `mcp/analyzers/queries.py:168-211`, `mcp/analyzers/reports.py:706-723`, `mcp/types.py:74-84`, `mcp/server.py:122/:185-203`, `mcp/analyzer.py:86-87`.

---

### Task 1: The shared group-satisfiability gate (`check_group_satisfiability_resumable`)

**Files:**
- Modify: `src/elspeth/core/checkpoint/recovery.py` (new section directly after `check_source_lifecycle_resumable`, currently ending :243; new exports in `__all__` :73-83; new imports in the schema import block :45-53)
- Modify: `src/elspeth/core/checkpoint/__init__.py` (re-export the new names alongside `NonResumableRunError`)
- Test: `tests/unit/core/checkpoint/test_group_satisfiability_gate.py` (new)

**Interfaces:**
- Consumes: canonical tables `token_lineage_frames_table`, `group_records_table`, `group_losses_table`, plus existing `token_outcomes_table`, `token_work_items_table`, `node_states_table` (all from `core/landscape/schema.py`); `FrameKind` from `contracts/enums.py` (WS1); `ResumeCheck` from `contracts/checkpoint.py`; `AuditIntegrityError` from `contracts/errors.py`; `freeze_fields` from `contracts/freeze.py`.
- Produces (Tasks 2, 3, 5, and the WS6 acceptance test rely on these exact names):
  - `GroupBindingView(fork_branch_closers: Mapping[str, str], fork_branch_rosters: Mapping[str, tuple[str, ...]], scope_opener_closers: Mapping[str, str])`
  - `UnsatisfiableGroupMember(closer_name: str, group_id: str, member_key: str, kind: FrameKind)`
  - `GroupSatisfiabilityResumeGate(unsatisfiable_members: tuple[UnsatisfiableGroupMember, ...], check: ResumeCheck)`
  - `check_group_satisfiability_resumable(db: LandscapeDB, run_id: str, bindings: GroupBindingView) -> GroupSatisfiabilityResumeGate`
  - `GroupUnsatisfiableResumeError(run_id: str, members: Sequence[UnsatisfiableGroupMember])` (defined beside `NonResumableRunError` — same file, same Tier-2 operator-refusal register)

**Semantics (spec §8, restated as the implementable rule):** for every minted member of every *bound* group of the run, at least ONE of three durable facts must hold, else refuse naming closer + group + member:

1. **lost** — a `group_losses` row `(run_id, closer_name, group_id, member_key)` exists, **regardless of `adopted_epoch`** (the §6.2 full-table-read discipline: adoption is a leader-memory cursor, not a truth filter);
2. **live** — some token bearing frame `(group_id, member_key)` has NO completed terminal outcome (work can still arrive; covers BUFFERED barrier arrivals in open groups);
3. **arrived** — some token bearing frame `(group_id, member_key)` has a `token_work_items` row addressed to the group's closer (`coalesce_name`, `row_union_name`, or `collector_name` equals the closer name, or `barrier_key` does). CAUTION (WS4 Task 6, verified in its plan): collector rows format `barrier_key` as `collector:<name>:<group-id>` — the bare-equality `barrier_key` disjunct does NOT match collector arrivals; `collector_name` is their address column and MUST be its own disjunct. This limb may rely on collector arrivals ALWAYS journaling: that is a ratified contract, pinned by WS4's `tests/unit/engine/test_collector_executor.py::test_every_arrival_journals_a_durable_hold` (WS4 Task 4) and `tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py::test_blocked_collector_item_counts_as_blocked_barrier_work` (WS4 Task 6) — the pins exist precisely because this gate's arrived limb depends on them. This limb is what keeps members of already-closed groups (consumed by a merge, released by a row_union, flushed by a collector — all terminal by then) from false-refusing: their journal rows at the closer persist (ADR-029, journal rows are marked, never deleted).

Unbound groups (inert frames — the batch posture, legal outside bound regions) are ignored: no roster is watching. Boundness comes from config via `GroupBindingView`, never from durable state — `group_records` deliberately carries no binding column (spec §4.3, WS1 lands it binding-blind).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/core/checkpoint/test_group_satisfiability_gate.py`. Raw-seed the audit DB exactly the way `tests/unit/core/landscape/test_scheduler_repository_coalesce_branch_losses.py:58-97` does (read that file first — the runs/nodes insert column sets below are copied from it and verified at HEAD; extend, don't invent):

```python
"""Fail-closed group-satisfiability resume gate (spec §8, ADR-038 amendment).

Every minted member of every bound group must be lost (group_losses row,
adopted or not), live (a frame-bearing token without a completed terminal),
or arrived (a journal row at the group's closer). A member with none of the
three can never settle, so the roster can never close: refuse resume naming
closer, group, and member. Unbound groups are inert provenance and never
refuse (ADR-020 batch posture, made structural).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, update

from elspeth.contracts import NodeType, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.checkpoint.recovery import (
    GroupBindingView,
    check_group_satisfiability_resumable,
)
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    group_losses_table,
    group_records_table,
    node_states_table,
    nodes_table,
    rows_table,
    runs_table,
    token_lineage_frames_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import make_landscape_db

RUN_ID = "run-satisfiability-1"
NOW = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
SOURCE_NODE_ID = "source-1"
COALESCE_NODE_ID = "coalesce::merger"
OPENER_NODE_ID = "transform::exploder"
FORK_GROUP = "fg-1"
EXPAND_GROUP = "eg-1"

_FORK_BINDINGS = GroupBindingView(
    fork_branch_closers={"path_a": "merger", "path_b": "merger"},
    fork_branch_rosters={
        "path_a": ("path_a", "path_b"),
        "path_b": ("path_a", "path_b"),
    },
    scope_opener_closers={},
)
_UNBOUND = GroupBindingView(fork_branch_closers={}, fork_branch_rosters={}, scope_opener_closers={})


def _payload_json() -> str:
    return TokenSchedulerRepository.serialize_row_payload(
        PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    )


def _seed_run(db: LandscapeDB) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=RUN_ID,
                started_at=NOW,
                config_hash="cfg",
                settings_json="{}",
                canonical_version="v1",
                status=RunStatus.FAILED.value,
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        for node_id, node_type in (
            (SOURCE_NODE_ID, NodeType.SOURCE),
            (COALESCE_NODE_ID, NodeType.COALESCE),
            (OPENER_NODE_ID, NodeType.TRANSFORM),
        ):
            conn.execute(
                insert(nodes_table).values(
                    run_id=RUN_ID,
                    node_id=node_id,
                    plugin_name="test",
                    node_type=node_type.value,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="cfg",
                    config_json="{}",
                    registered_at=NOW,
                )
            )
        conn.execute(
            insert(rows_table).values(
                row_id="row-1",
                run_id=RUN_ID,
                source_node_id=SOURCE_NODE_ID,
                row_index=0,
                source_row_index=0,
                ingest_sequence=0,
                source_data_hash="hash-row-1",
                created_at=NOW,
            )
        )


def _seed_fork_member(db: LandscapeDB, *, token_id: str, member_key: str, group_id: str = FORK_GROUP) -> None:
    """Mint one fork-branch token with its lineage frame (depth 0)."""
    with db.engine.begin() as conn:
        conn.execute(insert(tokens_table).values(token_id=token_id, row_id="row-1", run_id=RUN_ID, created_at=NOW))
        conn.execute(
            insert(token_lineage_frames_table).values(
                token_id=token_id,
                run_id=RUN_ID,
                depth=0,
                kind=FrameKind.FORK.value,
                group_id=group_id,
                member_key=member_key,
            )
        )


def _terminalize(db: LandscapeDB, token_id: str) -> None:
    RecorderFactory(db).data_flow.record_token_outcome(
        ref=TokenRef(token_id=token_id, run_id=RUN_ID),
        outcome=TerminalOutcome.FAILURE,
        path=TerminalPath.UNROUTED,
        error_hash="0" * 16,
    )


def _seed_loss(db: LandscapeDB, *, member_key: str, token_id: str, adopted_epoch: int | None) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            insert(group_losses_table).values(
                loss_id=f"loss-{member_key}",
                run_id=RUN_ID,
                closer_name="merger",
                group_id=FORK_GROUP,
                member_key=member_key,
                token_id=token_id,
                reason="quarantined",
                recorded_by="worker:test",
                recorded_at=NOW,
                adopted_epoch=adopted_epoch,
            )
        )


def _seed_arrival(db: LandscapeDB, *, token_id: str) -> None:
    """Journal evidence that the member's token reached the closer."""
    TokenSchedulerRepository(db.engine).enqueue_ready(
        run_id=RUN_ID,
        token_id=token_id,
        row_id="row-1",
        node_id=COALESCE_NODE_ID,
        step_index=1,
        ingest_sequence=0,
        row_payload_json=_payload_json(),
        available_at=NOW,
    )
    with db.engine.begin() as conn:
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.token_id == token_id)
            .values(coalesce_name="merger", barrier_key="merger")
        )


class TestForkGroups:
    def test_both_members_live_is_satisfiable(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        _seed_fork_member(db, token_id="tok-a", member_key="path_a")
        _seed_fork_member(db, token_id="tok-b", member_key="path_b")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert gate.check.can_resume
        assert gate.unsatisfiable_members == ()

    def test_terminal_member_without_loss_or_arrival_refuses_naming_the_member(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        _seed_fork_member(db, token_id="tok-a", member_key="path_a")
        _seed_fork_member(db, token_id="tok-b", member_key="path_b")
        _terminalize(db, "tok-b")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert not gate.check.can_resume
        assert len(gate.unsatisfiable_members) == 1
        member = gate.unsatisfiable_members[0]
        assert member.closer_name == "merger"
        assert member.group_id == FORK_GROUP
        assert member.member_key == "path_b"
        assert member.kind is FrameKind.FORK
        # Exact-reason refusal (spec §9 row 5: "prefer exact-reason refusal").
        assert gate.check.reason is not None
        for needle in ("merger", FORK_GROUP, "path_b", "group_losses"):
            assert needle in gate.check.reason

    def test_group_losses_row_settles_the_member_adopted_or_not(self) -> None:
        for adopted_epoch in (None, 7):
            db = make_landscape_db()
            _seed_run(db)
            _seed_fork_member(db, token_id="tok-a", member_key="path_a")
            _seed_fork_member(db, token_id="tok-b", member_key="path_b")
            _terminalize(db, "tok-b")
            _seed_loss(db, member_key="path_b", token_id="tok-b", adopted_epoch=adopted_epoch)
            gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
            assert gate.check.can_resume, f"adopted_epoch={adopted_epoch} must not filter the loss read"

    def test_arrived_terminal_member_is_settled(self) -> None:
        """A consumed member of a closed group: terminal, no loss, journal row at the closer."""
        db = make_landscape_db()
        _seed_run(db)
        _seed_fork_member(db, token_id="tok-a", member_key="path_a")
        _seed_fork_member(db, token_id="tok-b", member_key="path_b")
        _seed_arrival(db, token_id="tok-a")
        _seed_arrival(db, token_id="tok-b")
        _terminalize(db, "tok-a")
        _terminalize(db, "tok-b")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert gate.check.can_resume

    def test_unbound_fork_group_never_refuses(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        _seed_fork_member(db, token_id="tok-a", member_key="path_a")
        _terminalize(db, "tok-a")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _UNBOUND)
        assert gate.check.can_resume

    def test_declared_roster_member_never_minted_refuses(self) -> None:
        """Fail-closed: a declared branch with no frames rows can never settle either."""
        db = make_landscape_db()
        _seed_run(db)
        _seed_fork_member(db, token_id="tok-a", member_key="path_a")
        _terminalize(db, "tok-a")
        _seed_arrival(db, token_id="tok-a")
        gate = check_group_satisfiability_resumable(db, RUN_ID, _FORK_BINDINGS)
        assert not gate.check.can_resume
        assert [m.member_key for m in gate.unsatisfiable_members] == ["path_b"]

    def test_mixed_binding_within_one_group_is_integrity_error(self) -> None:
        """Ruling 23 whole-roster: one group half-bound is corrupt config/audit state."""
        db = make_landscape_db()
        _seed_run(db)
        _seed_fork_member(db, token_id="tok-a", member_key="path_a")
        _seed_fork_member(db, token_id="tok-b", member_key="path_b")
        half_bound = GroupBindingView(
            fork_branch_closers={"path_a": "merger"},
            fork_branch_rosters={"path_a": ("path_a", "path_b")},
            scope_opener_closers={},
        )
        with pytest.raises(AuditIntegrityError, match="whole-roster"):
            check_group_satisfiability_resumable(db, RUN_ID, half_bound)


def _seed_expand_group(db: LandscapeDB, *, member_count: int, opener_node: str = OPENER_NODE_ID) -> list[str]:
    """Mint an opener token with a node_state at the opener node, the group
    record, and ``member_count`` member tokens with EXPAND frames."""
    with db.engine.begin() as conn:
        conn.execute(insert(tokens_table).values(token_id="tok-opener", row_id="row-1", run_id=RUN_ID, created_at=NOW))
        conn.execute(
            insert(node_states_table).values(
                state_id="state-opener",
                token_id="tok-opener",
                run_id=RUN_ID,
                node_id=opener_node,
                step_index=1,
                attempt=0,
                status="completed",
                input_hash="0" * 64,
                started_at=NOW,
                completed_at=NOW,
            )
        )
        conn.execute(
            insert(group_records_table).values(
                run_id=RUN_ID,
                group_id=EXPAND_GROUP,
                kind=FrameKind.EXPAND.value,
                opener_token_id="tok-opener",
                member_count=member_count,
                created_at=NOW,
            )
        )
        member_ids = [f"tok-member-{i}" for i in range(member_count)]
        for member_id in member_ids:
            conn.execute(insert(tokens_table).values(token_id=member_id, row_id="row-1", run_id=RUN_ID, created_at=NOW))
            conn.execute(
                insert(token_lineage_frames_table).values(
                    token_id=member_id,
                    run_id=RUN_ID,
                    depth=0,
                    kind=FrameKind.EXPAND.value,
                    group_id=EXPAND_GROUP,
                    member_key=member_id,
                )
            )
    return member_ids


_SCOPE_BINDINGS = GroupBindingView(
    fork_branch_closers={},
    fork_branch_rosters={},
    scope_opener_closers={OPENER_NODE_ID: "page_stitcher"},
)


class TestExpandGroups:
    def test_bound_expand_member_terminal_unsettled_refuses(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        members = _seed_expand_group(db, member_count=2)
        _terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert not gate.check.can_resume
        member = gate.unsatisfiable_members[0]
        assert member.kind is FrameKind.EXPAND
        assert member.closer_name == "page_stitcher"
        assert member.member_key == members[0]

    def test_unbound_expand_group_is_inert(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        members = _seed_expand_group(db, member_count=2)
        _terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _UNBOUND)
        assert gate.check.can_resume

    def test_arrived_collector_member_is_settled_via_collector_name(self) -> None:
        """A flushed collector member: terminal, no loss, journal row whose
        collector_name (NOT bare barrier_key — WS4 formats collector
        barrier_key as 'collector:<name>:<group-id>') names the closer."""
        db = make_landscape_db()
        _seed_run(db)
        members = _seed_expand_group(db, member_count=1)
        TokenSchedulerRepository(db.engine).enqueue_ready(
            run_id=RUN_ID,
            token_id=members[0],
            row_id="row-1",
            node_id=OPENER_NODE_ID,
            step_index=1,
            ingest_sequence=0,
            row_payload_json=_payload_json(),
            available_at=NOW,
        )
        with db.engine.begin() as conn:
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.token_id == members[0])
                .values(collector_name="page_stitcher", barrier_key=f"collector:page_stitcher:{EXPAND_GROUP}")
            )
        _terminalize(db, members[0])
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert gate.check.can_resume, f"collector_name disjunct missing: {gate.check.reason}"

    def test_empty_expand_group_is_vacuously_satisfiable(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        _seed_expand_group(db, member_count=0)
        gate = check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
        assert gate.check.can_resume

    def test_member_count_mismatch_is_integrity_error(self) -> None:
        db = make_landscape_db()
        _seed_run(db)
        _seed_expand_group(db, member_count=2)
        with db.engine.begin() as conn:
            conn.execute(
                update(group_records_table)
                .where(group_records_table.c.group_id == EXPAND_GROUP)
                .values(member_count=3)
            )
        with pytest.raises(AuditIntegrityError, match="member_count"):
            check_group_satisfiability_resumable(db, RUN_ID, _SCOPE_BINDINGS)
```

Adjust seed column sets ONLY if the WS1 schema slice changed a NOT NULL set — verify against `core/landscape/schema.py` as landed, never by trial-and-error.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/core/checkpoint/test_group_satisfiability_gate.py -v`
Expected: FAIL at import — `ImportError: cannot import name 'GroupBindingView' from 'elspeth.core.checkpoint.recovery'`.

- [ ] **Step 3: Implement the gate in `recovery.py`**

Add after `check_source_lifecycle_resumable` (:243). Extend the existing schema import block (:45-53, verified: `node_states_table`/`token_outcomes_table`/`tokens_table` already imported there) with `group_losses_table`, `group_records_table`, `token_lineage_frames_table`, `token_work_items_table`, and import `FrameKind` from `elspeth.contracts.enums` and `or_` from `sqlalchemy`.

```python
@dataclass(frozen=True, slots=True)
class GroupBindingView:
    """Config-derived binding facts for the group-satisfiability gate.

    Built from the builder's group-binding registry by
    :func:`group_binding_view_from_graph` (the ONE seam coupling this gate to
    the DAG layer); unit tests construct it directly. Boundness is a config
    fact, never a durable one — ``group_records`` deliberately carries no
    binding column (spec §4.3).
    """

    fork_branch_closers: Mapping[str, str]
    fork_branch_rosters: Mapping[str, tuple[str, ...]]
    scope_opener_closers: Mapping[str, str]

    def __post_init__(self) -> None:
        freeze_fields(self, "fork_branch_closers", "fork_branch_rosters", "scope_opener_closers")


@dataclass(frozen=True, slots=True)
class UnsatisfiableGroupMember:
    """One minted member of a bound group that no resume can ever settle."""

    closer_name: str
    group_id: str
    member_key: str
    kind: FrameKind


@dataclass(frozen=True, slots=True)
class GroupSatisfiabilityResumeGate:
    """Facts + verdict from the shared group-satisfiability resume gate.

    SINGLE shared implementation for the advisory ``can_resume`` surface and
    the enforcing entry guard in ``ResumeCoordinator.resume()`` — the two
    must never drift (the check_source_lifecycle_resumable precedent,
    elspeth-1f5b83cd28; spec §8).
    """

    unsatisfiable_members: tuple[UnsatisfiableGroupMember, ...]
    check: ResumeCheck


# TIER-2: same operator-refusal register as NonResumableRunError above — the
# audit DB is intact; the durable group state proves the roster can never
# close, so resuming would wedge at the barrier forever (the B3 dishonesty
# spec §5 names). Carries the members so CLI/API callers surface the exact
# scope/group/member without parsing text.
class GroupUnsatisfiableResumeError(Exception):
    """Raised by ``ResumeCoordinator.resume()`` when a bound group can never settle."""

    def __init__(self, run_id: str, members: Sequence[UnsatisfiableGroupMember]) -> None:
        if not members:
            raise ValueError("GroupUnsatisfiableResumeError requires at least one member")
        self.run_id = run_id
        self.members = tuple(members)
        summary = "; ".join(
            f"closer {m.closer_name!r} group {m.group_id!r} member {m.member_key!r}" for m in self.members
        )
        super().__init__(
            f"Cannot resume run {run_id!r}: {len(self.members)} bound-group member(s) are terminal "
            f"without settlement — neither arrived at their closer nor named in group_losses ({summary}). "
            "The group roster can never close; investigate the audit evidence instead of resuming over it."
        )


def _group_member_is_settled_or_live(
    conn: Any,
    *,
    run_id: str,
    closer_name: str,
    group_id: str,
    member_key: str,
) -> bool:
    """The three-limb satisfiability check for one minted member (spec §8)."""
    lost = conn.execute(
        select(group_losses_table.c.loss_id)
        .where(
            group_losses_table.c.run_id == run_id,
            group_losses_table.c.closer_name == closer_name,
            group_losses_table.c.group_id == group_id,
            group_losses_table.c.member_key == member_key,
            # NO adopted_epoch filter: §6.2 full-table-read discipline.
        )
        .limit(1)
    ).fetchone()
    if lost is not None:
        return True

    frames = token_lineage_frames_table
    live = conn.execute(
        select(frames.c.token_id)
        .where(
            frames.c.run_id == run_id,
            frames.c.group_id == group_id,
            frames.c.member_key == member_key,
            ~select(token_outcomes_table.c.outcome_id)
            .where(
                token_outcomes_table.c.run_id == run_id,
                token_outcomes_table.c.token_id == frames.c.token_id,
                token_outcomes_table.c.completed == 1,
            )
            .exists(),
        )
        .limit(1)
    ).fetchone()
    if live is not None:
        return True

    arrived = conn.execute(
        select(token_work_items_table.c.work_item_id)
        .select_from(
            token_work_items_table.join(
                frames,
                (token_work_items_table.c.token_id == frames.c.token_id)
                & (token_work_items_table.c.run_id == frames.c.run_id),
            )
        )
        .where(
            token_work_items_table.c.run_id == run_id,
            frames.c.group_id == group_id,
            frames.c.member_key == member_key,
            or_(
                token_work_items_table.c.coalesce_name == closer_name,
                token_work_items_table.c.row_union_name == closer_name,
                # Collector rows: collector_name is the address column; their
                # barrier_key is "collector:<name>:<group-id>" (WS4 Task 6),
                # so the bare-equality barrier_key disjunct below cannot
                # match them — do not drop this disjunct.
                token_work_items_table.c.collector_name == closer_name,
                token_work_items_table.c.barrier_key == closer_name,
            ),
        )
        .limit(1)
    ).fetchone()
    return arrived is not None


def check_group_satisfiability_resumable(
    db: LandscapeDB,
    run_id: str,
    bindings: GroupBindingView,
) -> GroupSatisfiabilityResumeGate:
    """Group-satisfiability portion of :meth:`RecoveryManager.can_resume` (spec §8).

    SINGLE shared implementation for the advisory ``can_resume`` surface and
    the enforcing ``GroupUnsatisfiableResumeError`` guard in
    ``ResumeCoordinator.resume()`` — the two must never drift (the
    check_source_lifecycle_resumable two-surface precedent). Every minted
    member of every bound group must be non-terminal, arrived at its closer,
    or named in ``group_losses``; otherwise refuse with closer, group, and
    member named. Unbound groups are inert provenance and never refuse.
    """
    unsatisfiable: list[UnsatisfiableGroupMember] = []
    with db.engine.connect() as conn:
        # --- FORK groups: roster authority is the declared branch list. ---
        fork_rows = conn.execute(
            select(token_lineage_frames_table.c.group_id, token_lineage_frames_table.c.member_key)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.kind == FrameKind.FORK.value,
            )
            .distinct()
        ).fetchall()
        fork_members_seen: dict[str, set[str]] = {}
        for row in fork_rows:
            fork_members_seen.setdefault(str(row.group_id), set()).add(str(row.member_key))

        for group_id in sorted(fork_members_seen):
            seen = fork_members_seen[group_id]
            bound = {member for member in seen if member in bindings.fork_branch_closers}
            if not bound:
                continue  # fully unbound fork: pure fan-out, no roster watching
            if bound != seen:
                raise AuditIntegrityError(
                    f"Fork group {group_id!r} in run {run_id!r} violates whole-roster closure "
                    f"(ruling 23): members {sorted(seen - bound)} are unbound while "
                    f"{sorted(bound)} bind a closer. Config/audit disagreement."
                )
            sample = next(iter(bound))
            closer_name = bindings.fork_branch_closers[sample]
            roster = bindings.fork_branch_rosters[sample]
            for member_key in roster:
                if not _group_member_is_settled_or_live(
                    conn, run_id=run_id, closer_name=closer_name, group_id=group_id, member_key=member_key
                ):
                    unsatisfiable.append(
                        UnsatisfiableGroupMember(
                            closer_name=closer_name, group_id=group_id, member_key=member_key, kind=FrameKind.FORK
                        )
                    )

        # --- EXPAND groups: roster authority is group_records + frames. ---
        if bindings.scope_opener_closers:
            expand_rows = conn.execute(
                select(
                    group_records_table.c.group_id,
                    group_records_table.c.member_count,
                    node_states_table.c.node_id,
                )
                .select_from(
                    group_records_table.join(
                        node_states_table,
                        (group_records_table.c.opener_token_id == node_states_table.c.token_id)
                        & (group_records_table.c.run_id == node_states_table.c.run_id),
                    )
                )
                .where(
                    group_records_table.c.run_id == run_id,
                    group_records_table.c.kind == FrameKind.EXPAND.value,
                    node_states_table.c.node_id.in_(sorted(bindings.scope_opener_closers)),
                )
                .distinct()
            ).fetchall()
            for row in expand_rows:
                group_id = str(row.group_id)
                closer_name = bindings.scope_opener_closers[str(row.node_id)]
                minted = {
                    str(r.member_key)
                    for r in conn.execute(
                        select(token_lineage_frames_table.c.member_key)
                        .where(
                            token_lineage_frames_table.c.run_id == run_id,
                            token_lineage_frames_table.c.group_id == group_id,
                            token_lineage_frames_table.c.kind == FrameKind.EXPAND.value,
                        )
                        .distinct()
                    )
                }
                if len(minted) != int(row.member_count):
                    raise AuditIntegrityError(
                        f"Expand group {group_id!r} in run {run_id!r}: group_records.member_count="
                        f"{int(row.member_count)} but {len(minted)} distinct member frames exist. "
                        "Roster cross-check failed (spec §5)."
                    )
                for member_key in sorted(minted):
                    if not _group_member_is_settled_or_live(
                        conn, run_id=run_id, closer_name=closer_name, group_id=group_id, member_key=member_key
                    ):
                        unsatisfiable.append(
                            UnsatisfiableGroupMember(
                                closer_name=closer_name, group_id=group_id, member_key=member_key, kind=FrameKind.EXPAND
                            )
                        )

    if unsatisfiable:
        shown = unsatisfiable[:5]
        detail = "; ".join(
            f"{m.kind.value} group {m.group_id!r} member {m.member_key!r} at closer {m.closer_name!r}" for m in shown
        )
        suffix = "" if len(unsatisfiable) <= 5 else f" (+{len(unsatisfiable) - 5} more)"
        reason = (
            f"{len(unsatisfiable)} bound-group member(s) can never settle — each is terminal without "
            f"arriving at its closer and without a group_losses record: {detail}{suffix}"
        )
        return GroupSatisfiabilityResumeGate(
            unsatisfiable_members=tuple(unsatisfiable),
            check=ResumeCheck(can_resume=False, reason=reason),
        )
    return GroupSatisfiabilityResumeGate(unsatisfiable_members=(), check=ResumeCheck(can_resume=True))
```

Add to `__all__`: `"GroupBindingView"`, `"GroupSatisfiabilityResumeGate"`, `"GroupUnsatisfiableResumeError"`, `"UnsatisfiableGroupMember"`, `"check_group_satisfiability_resumable"` (and, after Task 2, `"group_binding_view_from_graph"`). Mirror the re-exports in `src/elspeth/core/checkpoint/__init__.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/core/checkpoint/test_group_satisfiability_gate.py -v`
Expected: all PASS.

- [ ] **Step 5: Limb-mutation check (manual, `-n 0`)**

For each limb of `_group_member_is_settled_or_live`, temporarily disable it (early-`continue`/`return False`) and confirm the mapped test fails, then restore:
- lost limb → `test_group_losses_row_settles_the_member_adopted_or_not`
- live limb → `test_both_members_live_is_satisfiable`
- arrived limb (whole) → `test_arrived_terminal_member_is_settled`
- the `collector_name` disjunct alone → `test_arrived_collector_member_is_settled_via_collector_name`
- add `adopted_epoch IS NULL` to the loss query → the `adopted_epoch=7` half of the loss test fails

Run each check with `.venv/bin/pytest tests/unit/core/checkpoint/test_group_satisfiability_gate.py -n 0`. Restore the code exactly; `git diff src/elspeth/core/checkpoint/recovery.py` must show only the intended implementation before committing.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/core/checkpoint/recovery.py src/elspeth/core/checkpoint/__init__.py tests/unit/core/checkpoint/test_group_satisfiability_gate.py
git commit -m "feat(recovery): add the shared group-satisfiability resume gate (spec §8)"
```

---

### Task 2: Wire both surfaces — advisory `can_resume` and enforcing `resume()` — plus the third sibling test

**Files:**
- Modify: `src/elspeth/core/checkpoint/recovery.py` (`group_binding_view_from_graph` new function; `can_resume` at :403 — insert after the lifecycle gate at :459-461)
- Modify: `src/elspeth/engine/orchestrator/resume.py` (import block :56-59; new entry-guard part 3 after the topology check that ends at :968)
- Test: `tests/integration/audit/test_contract_violation_token_outcomes.py` (the spec-commissioned third sibling, beside the aggregation pair at :255/:290)

**Interfaces:**
- Consumes: Task 1's `check_group_satisfiability_resumable` / `GroupBindingView` / `GroupUnsatisfiableResumeError`; WS2's `graph.get_group_bindings()` registry (`GroupBindingRegistry` / `GroupBinding`, WS2 Task 4 — this adapter is the ONLY call site); the fork/coalesce full-run recipe of `tests/property/audit/test_fork_coalesce_flow.py:257-310` (`GateSettings` + `CoalesceSettings` + `ExecutionGraph.from_plugin_instances` + `ElspethSettings`); `RowProcessor._complete_coalesce_fire` (the death-matrix seam, `tests/e2e/recovery/test_barrier_process_death_matrix.py:180-186`).
- Produces: `group_binding_view_from_graph(graph: ExecutionGraph) -> GroupBindingView` (recovery.py; Tasks 5/6 and the WS6 acceptance test reuse it); the enforcing raise `GroupUnsatisfiableResumeError` from `resume()` before any mutation.

**Construction note (measured 2026-08-21, probe in scratchpad):** a crash *inside row processing* of a fork pipeline leaves the source lifecycle `loading`, so the source-lifecycle gate masks the group gate AND the ADR-038 sweep abandons the tokens. The third sibling therefore crashes at the **EOF coalesce flush** under a `best_effort` + `timeout_seconds` coalesce: the group stays open through load (no early merge), the source reaches `exhausted`, and a raise injected at `RowProcessor._complete_coalesce_fire` fails the run mid-group with checkpoints and complete sources — the exact state where only the group gate speaks.

- [ ] **Step 1: Write the failing third-sibling test**

Append to `tests/integration/audit/test_contract_violation_token_outcomes.py` (imports to add: `CoalesceSettings`, `GateSettings`, `ElspethSettings` from `elspeth.core.config`; `delete` from `sqlalchemy`; `TokenRef` from `elspeth.contracts.audit`; `group_losses_table`, `token_lineage_frames_table` from `elspeth.core.landscape.schema`; `GroupUnsatisfiableResumeError` from `elspeth.core.checkpoint.recovery`; `RowProcessor` from `elspeth.engine.processor`; `RecorderFactory` from `elspeth.core.landscape.factory`):

```python
def _run_fork_coalesce_to_eof_flush_crash(
    db: LandscapeDB, tmp_path: Path
) -> tuple[str, RecoveryManager, ExecutionGraph, CheckpointManager, PipelineConfig, ElspethSettings]:
    """Crash a fork→best_effort-coalesce run at the EOF flush, mid-group.

    best_effort + a long timeout defers the merge past load, so the source
    reaches ``exhausted`` before the injected `_complete_coalesce_fire` raise
    fails the run — the one deterministic construction where the crashed
    image has complete sources, a checkpoint, and an OPEN bound fork group.
    (An in-row crash leaves the source ``loading``: the lifecycle gate masks
    the group gate and ADR-038 abandons the tokens — measured 2026-08-21.)
    """
    checkpoint_mgr = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    source = ListSource([{"value": 1}], name="list_source", on_success="to_gate")
    sink = CollectSink("default")
    gate = GateSettings(
        name="fork_gate",
        input="to_gate",
        condition="True",
        routes={"true": "fork", "false": "default"},
        fork_to=["path_a", "path_b"],
    )
    coalesce = CoalesceSettings(
        name="merger",
        branches=["path_a", "path_b"],
        policy="best_effort",
        timeout_seconds=3600,
        merge="union",
        on_success="default",
    )
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(sink)},
        gates=[gate],
        coalesce_settings=[coalesce],
        sink_effect_modes={"default": "write"},
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="to_gate", options={})},
        transforms=[],
        sinks={"default": as_sink(sink)},
        gates=[gate],
        aggregations={},
        coalesce_settings=[coalesce],
    )
    settings_obj = ElspethSettings(
        sources={"primary": {"plugin": "test", "on_success": "default", "options": {}}},
        sinks={"default": {"plugin": "test", "on_write_failure": "discard"}},
        gates=[gate],
        coalesce=[coalesce],
    )

    real_fire = RowProcessor._complete_coalesce_fire

    def crash_before_completion(self: RowProcessor, **kwargs: Any) -> None:
        raise RuntimeError("test: injected crash at EOF coalesce flush (mid-group)")

    RowProcessor._complete_coalesce_fire = crash_before_completion  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="injected crash at EOF coalesce flush"):
            Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config).run(
                config,
                graph=graph,
                settings=settings_obj,
                payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
    finally:
        RowProcessor._complete_coalesce_fire = real_fire  # type: ignore[method-assign]

    with db.connection() as conn:
        run_rows = conn.execute(select(runs_table)).fetchall()
    assert len(run_rows) == 1
    run_id = str(run_rows[0].run_id)
    # Preconditions the construction exists to guarantee: without them the
    # earlier gates mask the group gate and this test proves nothing.
    assert run_rows[0].status == RunStatus.FAILED
    assert _source_lifecycle_states(db, run_id) == {"primary": "exhausted"}
    return run_id, RecoveryManager(db, checkpoint_mgr), graph, checkpoint_mgr, config, settings_obj


def test_group_satisfiability_gate_passes_on_an_honest_mid_group_crash(tmp_path: Path) -> None:
    """HAPPY control (spec §9 row 5: false-refuse is WS5's dominant risk).

    Every member of the open fork group is live or arrived, so both surfaces
    must treat the crashed run as resumable — the gate exists for the
    terminal-without-settlement anomaly, not for ordinary mid-group crashes.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    run_id, recovery, graph, _ckpt, _config, _settings = _run_fork_coalesce_to_eof_flush_crash(db, tmp_path)
    check = recovery.can_resume(run_id, graph)
    assert check.can_resume, f"false refuse: {check.reason}"


def test_group_satisfiability_refusal_names_scope_group_and_member(tmp_path: Path) -> None:
    """Third sibling to the aggregation-violation pair (spec §8).

    Adversarial construction for a fail-closed gate: terminalize one branch
    token through the raw outcome writer (the exact bypass class WS3
    retired) and wipe the run's group_losses rows — the member is now
    terminal, never arrived, and unnamed in the ledger. Both the advisory
    and enforcing surfaces must refuse, naming closer, group, and member.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    run_id, recovery, graph, checkpoint_mgr, config, _settings = _run_fork_coalesce_to_eof_flush_crash(db, tmp_path)

    with db.connection() as conn:
        frame = conn.execute(
            select(token_lineage_frames_table)
            .where(token_lineage_frames_table.c.run_id == run_id)
            .where(token_lineage_frames_table.c.member_key == "path_b")
        ).fetchone()
    assert frame is not None
    lost_token_id = str(frame.token_id)
    fork_group_id = str(frame.group_id)

    # The retired-bypass shape: a terminal outcome with no settlement. The
    # crashed branch token is BUFFERED at the barrier; overwrite nothing —
    # write the terminal the bypass class used to write.
    RecorderFactory(db).data_flow.record_token_outcome(
        ref=TokenRef(token_id=lost_token_id, run_id=run_id),
        outcome=TerminalOutcome.FAILURE,
        path=TerminalPath.UNROUTED,
        error_hash="0" * 16,
    )
    with db.write_connection() as conn:
        conn.execute(delete(group_losses_table).where(group_losses_table.c.run_id == run_id))
        # Defeat the arrived limb for path_b: the bypass class terminalized
        # tokens that never reached the closer, so its journal row must not
        # point at the closer either.
        conn.execute(
            update(token_work_items_table)
            .where(token_work_items_table.c.token_id == lost_token_id)
            .values(coalesce_name=None, row_union_name=None, barrier_key=None)
        )

    # Advisory surface.
    check = recovery.can_resume(run_id, graph)
    assert not check.can_resume
    assert check.reason is not None
    for needle in ("merger", fork_group_id, "path_b", "group_losses"):
        assert needle in check.reason, f"refusal must name the evidence; missing {needle!r} in: {check.reason}"

    # Enforcing surface — SAME shared implementation, before any mutation.
    latest = checkpoint_mgr.get_latest_checkpoint(run_id)
    assert latest is not None
    resume_point = ResumePoint(checkpoint=latest, sequence_number=latest.sequence_number)
    with pytest.raises(GroupUnsatisfiableResumeError) as excinfo:
        Orchestrator(db, checkpoint_manager=checkpoint_mgr).resume(
            resume_point=resume_point,
            config=config,
            graph=graph,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
        )
    assert excinfo.value.run_id == run_id
    assert [(m.group_id, m.member_key) for m in excinfo.value.members] == [(fork_group_id, "path_b")]

    # Settling the member (a group_losses row, unadopted) clears the refusal:
    # the gate reads the FULL ledger, not the adoption cursor.
    with db.write_connection() as conn:
        conn.execute(
            group_losses_table.insert().values(
                loss_id="loss-restored",
                run_id=run_id,
                closer_name="merger",
                group_id=fork_group_id,
                member_key="path_b",
                token_id=lost_token_id,
                reason="quarantined",
                recorded_by="worker:test",
                recorded_at=datetime.now(UTC),
                adopted_epoch=None,
            )
        )
    assert recovery.can_resume(run_id, graph).can_resume
```

(`update`, `datetime`, `UTC`, `token_work_items_table` need importing; `SourceSettings` and `ExecutionGraph` are already imported in this file. If `db.write_connection()` is not the file's idiom, use `with db.engine.begin() as conn:` — match the surrounding file.)

- [ ] **Step 2: Run to verify the right failure**

Run: `.venv/bin/pytest "tests/integration/audit/test_contract_violation_token_outcomes.py::test_group_satisfiability_refusal_names_scope_group_and_member" "tests/integration/audit/test_contract_violation_token_outcomes.py::test_group_satisfiability_gate_passes_on_an_honest_mid_group_crash" -v`
Expected: the refusal test FAILS at `assert not check.can_resume` (the gate is not wired yet — `can_resume` returns True); the happy control may already PASS. If instead the construction helper fails (source not `exhausted`, run not FAILED), STOP and fix the construction against the live WS3/WS4 engine before touching production code — the preconditions assertions exist precisely to catch that.

- [ ] **Step 3: Implement the adapter and wire the advisory surface**

In `recovery.py`, add beside the gate (this is the ONLY place WS2's registry spelling appears; spellings are WS2 Task 4's Produces block, verified 2026-08-22):

```python
def group_binding_view_from_graph(graph: ExecutionGraph) -> GroupBindingView:
    """Project the builder's group-binding registry into the gate's input.

    THE single seam coupling the satisfiability gate to the DAG layer: a
    FORK binding contributes every declared branch (whole-roster, ruling
    23); an EXPAND binding contributes its opener node. The discriminator
    is ``GroupBinding.kind`` — an EXPAND binding's ``member_roster`` is
    ``()`` by contract (runtime roster authority is ``group_records``),
    so roster emptiness must never be used to tell the kinds apart.
    """
    fork_branch_closers: dict[str, str] = {}
    fork_branch_rosters: dict[str, tuple[str, ...]] = {}
    scope_opener_closers: dict[str, str] = {}
    for binding in graph.get_group_bindings().bindings:
        if binding.kind is FrameKind.FORK:
            roster = tuple(binding.member_roster)
            for branch in roster:
                fork_branch_closers[branch] = binding.closer_name
                fork_branch_rosters[branch] = roster
        else:  # FrameKind.EXPAND
            scope_opener_closers[str(binding.opener_node_id)] = binding.closer_name
    return GroupBindingView(
        fork_branch_closers=fork_branch_closers,
        fork_branch_rosters=fork_branch_rosters,
        scope_opener_closers=scope_opener_closers,
    )
```

In `can_resume` (:403), after the source-lifecycle gate block (:456-461) and before `verify_contract_integrity` (:467):

```python
        # Group satisfiability (spec §8; ADR-038 amendment): every minted
        # member of every bound group must be non-terminal, arrived at its
        # closer, or named in group_losses — otherwise no resume can ever
        # close the roster and the run would wedge at the barrier. Same
        # shared implementation as resume()'s enforcing guard.
        group_gate = check_group_satisfiability_resumable(self._db, run_id, group_binding_view_from_graph(graph))
        if not group_gate.check.can_resume:
            return group_gate.check
```

Also update the `can_resume` docstring's bullet list (:406-414) with a sixth bullet naming the group gate.

- [ ] **Step 4: Wire the enforcing surface**

In `resume.py`, extend the `from elspeth.core.checkpoint.recovery import (...)` block (:56-59) with `GroupUnsatisfiableResumeError`, `check_group_satisfiability_resumable`, `group_binding_view_from_graph`. After the topology check that ends at :963-968 (still before `prepare_for_run()` at :976 — the first mutation):

```python
        # ---- resume() entry guard, part 3: group satisfiability (spec §8) ----
        # SAME shared implementation as the advisory can_resume() — the
        # check_source_lifecycle_resumable two-surface precedent
        # (elspeth-1f5b83cd28). READ-ONLY refusal before the first mutation:
        # a bound-group member that is terminal without settlement can never
        # settle, so the resumed run would wedge at its closer forever.
        group_gate = check_group_satisfiability_resumable(
            self._db, guarded_run_id, group_binding_view_from_graph(graph)
        )
        if not group_gate.check.can_resume:
            raise GroupUnsatisfiableResumeError(guarded_run_id, group_gate.unsatisfiable_members)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/integration/audit/test_contract_violation_token_outcomes.py tests/unit/core/checkpoint/test_group_satisfiability_gate.py -v`
Expected: all PASS, including the two pre-existing aggregation siblings (the EOF sibling doubles as the no-bound-group control: its `can_resume` assertion at :287 must stay green with the new gate in the chain).

- [ ] **Step 6: Scoped regression on the resume surfaces**

Run: `.venv/bin/pytest tests/unit/core/checkpoint/ tests/e2e/recovery/test_resume_rejection.py tests/e2e/recovery/test_concurrent_resume.py -v`
Expected: PASS (linear-pipeline runs have no bound groups: the gate must be a no-op for them).

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/core/checkpoint/recovery.py src/elspeth/core/checkpoint/__init__.py src/elspeth/engine/orchestrator/resume.py tests/integration/audit/test_contract_violation_token_outcomes.py
git commit -m "feat(resume): enforce group satisfiability on both resume surfaces (spec §8)"
```

---

### Task 3: ADR-038 amendment + rolling-doc entry

**Files:**
- Modify: `docs/architecture/adr/038-non-terminal-abandoned-path.md` (§3 "The non-resumability predicate mirrors the resume gates, arm for arm", :151-179; Related Decisions, :348)
- Modify: `docs/agents/recent-code-hints.md` (new dated entry at the top of the entry list)

**Interfaces:**
- Consumes: Tasks 1–2 as landed (the amendment documents them).
- Produces: the recorded decision that the abandonment sweep does NOT mirror the group-satisfiability arm (consumed by any future editor of `run_lifecycle_repository.py`).

- [ ] **Step 1: Amend ADR-038 §3**

Append after the two review-verified notes (:180-203), before §4:

```markdown
### 3a. Amendment (2026-08-21, unified-lineage WS5): the group-satisfiability arm is deliberately NOT mirrored

The unified-lineage campaign added a fourth structural resume refusal:
`check_group_satisfiability_resumable` (`core/checkpoint/recovery.py`),
enforced by `resume()` as `GroupUnsatisfiableResumeError` — a minted member
of a bound group that is terminal without arriving at its closer and
without a `group_losses` record can never settle, so the roster can never
close (spec 2026-08-21-barrier-scopes-full-nesting §8).

This arm intentionally breaks the "sweep fires iff a hypothetical resume
would be refused" symmetry. The three original arms describe *ordinary
operational states* (no checkpoint, no sources, incomplete lifecycle) —
honest run-death shapes the sweep may tidy into `ABANDONED`. A
group-unsatisfiable state is different in kind: after WS3, loss staging
rides the claim's disposition transaction, so terminal-without-settlement
is reachable only through an audit-integrity anomaly or the retired bypass
class. Sweeping such a run's tokens to `(NULL, ABANDONED)` would launder
an integrity anomaly into a routine closure and destroy the investigation
signal the fail-closed refusal exists to preserve. Such runs therefore
keep `closure='open'` and the refusal names closer, group, and member.

If a legitimate operational path to terminal-without-settlement is ever
found, revisit this asymmetry rather than working around the refusal.
```

Add to Related Decisions: `- Amended by: unified-lineage WS5 (2026-08-21) — group-satisfiability resume arm, deliberately unmirrored in the sweep (§3a).`

- [ ] **Step 2: Add the rolling-doc entry**

Add at the top of the dated entries in `docs/agents/recent-code-hints.md`:

```markdown
- **2026-08-21 — a new structural resume refusal has TWO surfaces and ONE
  implementation, and its gate-order matters** (landed with the unified-lineage
  WS5 slice). `check_group_satisfiability_resumable` follows the
  `check_run_status_resumable`/`check_source_lifecycle_resumable` precedent:
  the advisory `RecoveryManager.can_resume` returns the shared `ResumeCheck`,
  the enforcing `ResumeCoordinator.resume()` raises
  `GroupUnsatisfiableResumeError` from the SAME function — never fork the
  logic. Three traps, all measured: (a) an in-row crash of a fork pipeline
  leaves the source lifecycle `loading`, so the source-lifecycle gate masks
  the group gate AND the ADR-038 sweep abandons the tokens — a test that
  wants the group gate reachable must crash at the EOF flush under a
  `best_effort`+timeout coalesce; (b) the gate reads `group_losses` with NO
  `adopted_epoch` filter (§6.2 full-table discipline) — adding one turns
  every takeover into a false refuse; (c) "arrived" evidence is the journal
  row at the closer (`coalesce_name`/`row_union_name`/`barrier_key`), which
  persists after release — dropping that limb false-refuses every member of
  every CLOSED group. The ADR-038 abandonment sweep deliberately does NOT
  mirror this arm (ADR-038 §3a): do not "complete" the symmetry.
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/adr/038-non-terminal-abandoned-path.md docs/agents/recent-code-hints.md
git commit -m "docs(adr-038): record the group-satisfiability resume arm and its deliberate sweep asymmetry"
```

---

### Task 4: `has_blocked_barrier_work` covers collector holds; EOF drain flushes collectors

**Files:**
- Modify (only if red — see Step 2): `src/elspeth/engine/orchestrator/leader_drain.py` (the EOF fixpoint, :464-518)
- Test: `tests/unit/engine/test_processor_collector_barrier_work.py` (new)

**Interfaces:**
- Consumes: WS4 Task 6's landed drain facts (its plan, verified 2026-08-22: `has_blocked_barrier_work` is non-filtered BY DESIGN — `processor.py:4199` → `list_blocked_barrier_items`, pinned at the repository level by `tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py`, and the leader_drain early-return guard already gained `collector_executor is None`); the WS3+WS4 integration item's collector flush arm; the shared builders `_make_processor` / `_persist_blocked_scheduler_work` (`tests/unit/engine/test_processor.py`).
- Produces: the pinned property spec §9 row 5 assigns to WS5 — a BLOCKED collector member is barrier work at the PROCESSOR surface, so `run_end_of_input_barrier_flush` cannot exit its fixpoint early.

**Scope note:** WS4 Task 6 pins this at the repository/drain level; the WS4 plan's closing handoff names "WS5's `has_blocked_barrier_work`/satisfiability interactions" as still owed. This task's deliverable is therefore the PROCESSOR-surface pin (through `RowProcessor.has_blocked_barrier_work` itself, which the repo-level test does not drive) plus verification that the EOF loop's collector flush arm actually landed with the WS3+WS4 integration item. Read `leader_drain.py:464-518` and `tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py` as landed before writing code; if the integration item already added the flush arm, Step 3 is a no-op and only the test lands.

- [ ] **Step 1: Write the pinning test**

Create `tests/unit/engine/test_processor_collector_barrier_work.py`. Model the crafting on the row-union arm of the death matrix (`test_barrier_process_death_matrix.py:249-261`), substituting the WS4 collector builders:

```python
"""A BLOCKED collector member is barrier work (spec §5 / §9 row 5).

`has_blocked_barrier_work` is the §D step-3 loop condition: if a buffered
collector member does not count, `run_end_of_input_barrier_flush` exits its
fixpoint before the group closes and the member is silently stranded.
"""

from __future__ import annotations

from tests.integration.pipeline.test_collector_intake_dispositions import (  # WS4 shared builders
    _collector_processor,
    _expand_member_token,
    _real_collector_executor,
    _seed_expand_group,
)
from tests.integration.pipeline.test_barrier_intake_dispositions import RUN_ID  # noqa: F401
from tests.unit.engine.test_processor import _persist_blocked_scheduler_work


def test_blocked_collector_member_counts_as_barrier_work(collector_harness) -> None:
    factory, processor, executor = collector_harness  # fixture built from the WS4 builders
    member = _expand_member_token(ordinal=0)
    _persist_blocked_scheduler_work(
        factory,
        processor,
        member,
        node_id=executor.node_id,
        barrier_key=executor.closer_name,
        adopted=False,
        ingest_sequence=0,
    )
    assert processor.has_blocked_barrier_work() is True
```

The import spellings above are this plan's best guess at the WS3+WS4 integration item's shared builder module — **reconcile the module path and builder names against that plan's Produces block first** (WS4 proper hands the builders to it; see its "Handed to the WS3+WS4 integration line item" list), then write the test against the real names (including the fixture: if no `collector_harness` fixture is exposed, compose `factory`/`executor`/`processor` inline exactly as the death-matrix row-union arm composes its own at :264-278). The assertion body — one BLOCKED collector row ⇒ `has_blocked_barrier_work() is True` — is the fixed deliverable.

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/unit/engine/test_processor_collector_barrier_work.py -v`
Expected: PASS if WS4 wired collector arrivals through the standard BLOCKED journal path (the pin still lands — it is what keeps a future "optimize the loop condition by barrier kind" change honest). FAIL means the collector holds live outside the journal read: fix by extending `RowProcessor.has_blocked_barrier_work` (:4199) to OR in the collector executor's buffered-member count — and treat that as a WS4-contract deviation to flag to the campaign coordinator, not silently absorb.

- [ ] **Step 3: Verify the EOF loop flushes collectors**

Read `leader_drain.py:464-518` as landed. The fixpoint must contain a collector arm parallel to `flush_coalesce_pending` (:493-501) / `flush_row_union_pending` (:503-509). If WS4 landed it, done. If absent, add (mirroring the row-union arm's shape and WS4's `flush_collector_pending` signature):

```python
        if collector_executor is not None:
            flush_collector_pending(
                collector_executor=collector_executor,
                processor=processor,
                ctx=ctx,
                counters=counters,
                pending_tokens=pending_tokens,
            )
```

and thread `collector_executor` through `run_end_of_input_barrier_flush`'s parameters the same way `row_union_executor` is threaded. Do NOT touch `MAX_END_OF_INPUT_FLUSH_ITERATIONS` (:417) and do NOT introduce a bound of your own — the ONE depth-derived formula is WS2's `derive_escalation_fixpoint_bound(depth) = 1000 + 8 * depth`, consumed off `graph.get_max_bound_region_depth()` and threaded by WS3 as `flush_iteration_bound` / `PipelineConfig.escalation_fixpoint_bound`.

- [ ] **Step 4: Run the drain suites**

Run: `.venv/bin/pytest tests/unit/engine/test_processor_collector_barrier_work.py tests/e2e/recovery/test_multi_worker_leader_finalize.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/engine/test_processor_collector_barrier_work.py
# plus src/elspeth/engine/orchestrator/leader_drain.py and/or src/elspeth/engine/processor.py ONLY if Steps 2-3 changed them
git commit -m "test(drain): pin collector-buffered members as blocked barrier work (spec §9 row 5)"
```

---

### Task 5: Resume-mid-group happy path e2e — the `collector` death-matrix family

**Coordination note:** the WS4 plan's closing section hands "the e2e death-matrix `collector` family" to the WS3+WS4 integration line item; the campaign assigns the resume-mid-group happy path to WS5 (spec §11 owed item). This task IS that deliverable — before starting, check whether the integration item already landed the family (`git log --oneline -- tests/e2e/recovery/test_barrier_process_death_matrix.py` + read the `exercises` dict); if it did, this task shrinks to reviewing that family against the assertions below and adding whichever are missing (the satisfiability-gate happy-path check and the ordinal-vs-arrival oracle are WS5's, and likely absent).

**Files:**
- Modify: `tests/e2e/recovery/test_barrier_process_death_matrix.py` (new `_exercise_collector`, two module-level seam actions, `collector` added to the `exercises` dict at :440-444 and the parametrize list at :434)

**Interfaces:**
- Consumes: the file's own machinery (`_kill_at_seam` :300, `_run_fresh_recovery` :314, `_new_barrier_factory` :89, `spawn_database_process_*` from `harness.py`); WS4's `CollectorExecutor`, `RowProcessor._complete_collector_fire` pause seam, and shared collector builders (reconcile names against the WS4 plan — the analogue of `_real_row_union_executor`/`_row_union_processor` defined in this very file for row_union, so if WS4 exports no shared builders, define `_real_collector_executor`/`_collector_processor` locally in this file exactly as row_union does at :212-246); Task 2's `group_binding_view_from_graph` + `check_group_satisfiability_resumable`.
- Produces: the campaign's "resume-mid-group happy path incl. collector-buffer takeover with ordinal flush" evidence (spec §11 owed item; test-harness scout §6 row 7).

**Scenario (decision 11 — flush order is opener expansion ordinal, never arrival order, because arrival order is unrecoverable after takeover):** an EXPAND group of three members; members arrive at the collector out of ordinal order (1, then 0); the child process is SIGKILLed after both arrivals are durably BLOCKED (pause seam inside `_complete_collector_fire`-adjacent intake, i.e. before any flush continuation); a fresh process restores the collector buffers from the journal, member 2 arrives, the group closes, and the flush presents members in ordinal order 0, 1, 2.

- [ ] **Step 1: Write the exercise (failing until wired)**

Add to `test_barrier_process_death_matrix.py`, following the row-union family's structure verbatim (kill action, killed-image assertions, recovery action, recovered-image assertions):

```python
def _run_collector_to_buffer_seam(db: LandscapeDB, pause: Callable[[], None], payload_path: str) -> None:
    """Buffer two of three expand members at the collector, then pause.

    Arrival order is DELIBERATELY 1 then 0: the recovered flush must present
    opener-ordinal order regardless (decision 11).
    """
    factory = _new_barrier_factory(db, payload_path)
    clock = MockClock(start=_T0)
    executor = _real_collector_executor(factory, clock)                      # WS4 builder
    processor = _collector_processor(factory, executor, clock)               # WS4 builder
    members = _seed_expand_group(factory, member_count=3)                    # WS4 builder: opener token,
    #                              group_records row, three member tokens with EXPAND frames + ordinals
    assert _arrive_collector(factory, processor, members[1], ingest_sequence=0) == []
    _arrive_collector(factory, processor, members[0], ingest_sequence=1)
    pause()


def _resume_collector_after_death(db: LandscapeDB, payload_path: str) -> None:
    """Takeover: restore buffers from the journal, close the group, flush by ordinal."""
    factory = RecorderFactory(db, payload_store=FilesystemPayloadStore(Path(payload_path)))
    clock = MockClock(start=_T0 + 10)
    _usurp_seat(db, clock)
    executor = _real_collector_executor(factory, clock)
    processor = _collector_processor(
        factory,
        executor,
        clock,
        barrier_restore=BarrierJournalRestoreContext(
            resume_checkpoint_id="process-death-collector",
            barrier_scalars=None,
            batch_id_remap={},
        ),
    )
    # Buffer composition survived the takeover (the timing-invariance shape).
    assert processor.has_blocked_barrier_work() is True
    members = _load_expand_members(factory)                                  # WS4 builder (or local reader)
    _arrive_collector(factory, processor, members[2], ingest_sequence=2)
    assert processor.has_blocked_barrier_work() is False


def _exercise_collector(tmp_path: Path) -> LandscapeDB:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / 'collector.db'}"
    payload_path = tmp_path / "payloads"
    with LandscapeDB(database_url):
        pass
    _kill_at_seam(database_url, _run_collector_to_buffer_seam, (str(payload_path),))
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db, killed_db.connection() as conn:
        assert set(conn.execute(select(token_work_items_table.c.status)).scalars()) == {TokenWorkStatus.BLOCKED.value}
        group_row = conn.execute(select(group_records_table)).mappings().one()
        assert group_row["member_count"] == 3
        frames = conn.execute(
            select(token_lineage_frames_table.c.member_key).where(
                token_lineage_frames_table.c.group_id == group_row["group_id"]
            )
        ).scalars().all()
        assert len(set(frames)) == 3
    # WS5 gate happy path on a REAL killed mid-group image: two members
    # arrived (BLOCKED), one live — never a false refuse.
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        gate = check_group_satisfiability_resumable(killed_db, RUN_ID, _collector_binding_view())
        assert gate.check.can_resume, f"false refuse on an honest mid-group image: {gate.check.reason}"
    _run_fresh_recovery(database_url, _resume_collector_after_death, (str(payload_path),))
    recovered = LandscapeDB.from_url(database_url, create_tables=False)
    with recovered.connection() as conn:
        rows = conn.execute(
            select(token_work_items_table.c.token_id, token_work_items_table.c.status).order_by(
                token_work_items_table.c.created_at, token_work_items_table.c.work_item_id
            )
        ).all()
        assert {row.status for row in rows} <= {TokenWorkStatus.TERMINAL.value, TokenWorkStatus.PENDING_SINK.value, TokenWorkStatus.READY.value}
        # Ordinal-flush oracle: the collector's flush presented members 0,1,2
        # in opener ordinal order. Read the durable flush evidence WS4
        # produces (batch_members ordinals for the collector's batch) and
        # assert the ordinal sequence, NOT the arrival sequence:
        ordinals = conn.execute(
            select(batch_members_table.c.ordinal).order_by(batch_members_table.c.ordinal)
        ).scalars().all()
        assert ordinals == [0, 1, 2]
    return recovered


def _collector_binding_view() -> GroupBindingView:
    return GroupBindingView(
        fork_branch_closers={},
        fork_branch_rosters={},
        scope_opener_closers={str(_COLLECTOR_OPENER_NODE): str(_COLLECTOR_NAME)},
    )
```

Add imports (`GroupBindingView`, `check_group_satisfiability_resumable` from `elspeth.core.checkpoint.recovery`; `batch_members_table`, `group_records_table`, `token_lineage_frames_table` from the schema module), module constants `_COLLECTOR_NAME` / `_COLLECTOR_OPENER_NODE` matching the WS4 builders, and register the family:

```python
@pytest.mark.parametrize("barrier_family", ("aggregation", "coalesce", "row_union", "collector"))
```
```python
        "collector": _exercise_collector,
```

Reconcile every `# WS4 builder` call against the WS4 plan's Produces block before running; the killed-image and recovered-image assertions and the arrival-order-vs-ordinal-order oracle are this plan's fixed deliverable. If WS4's flush evidence is not `batch_members` ordinals, read its plan for the durable flush record and assert ordinal order on THAT surface — the oracle must be durable, not in-memory.

- [ ] **Step 2: Run the new family**

Run: `.venv/bin/pytest "tests/e2e/recovery/test_barrier_process_death_matrix.py::test_single_process_leader_barrier_process_death_matrix[collector]" -v`
Expected: FAIL initially on whichever builder/wiring gap exists; iterate against the WS4-landed code until PASS. The three pre-existing families must stay green: `.venv/bin/pytest tests/e2e/recovery/test_barrier_process_death_matrix.py -v`.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/recovery/test_barrier_process_death_matrix.py
git commit -m "test(e2e): collector process-death family — buffer takeover, ordinal flush, no false refuse (spec §11)"
```

---

### Task 5a: Depth-5 nested crash + resume — no false refuse, same terminal outcome

**Files:**
- Test: `tests/e2e/recovery/test_depth5_resume_mid_chain.py` (new)

**Interfaces:**
- Consumes: WS3 Task 10's `_nested_settings(depth)` settings-document builder and `DEPTH` (import from `tests.integration.pipeline.test_depth5_group_unwrap` — the module WS3's plan creates; its live acceptance test `test_depth5_single_failure_unwraps_to_outermost_quarantine` is the outcome oracle this task must match); WS2's `PipelineConfig.escalation_fixpoint_bound` (Task 5 of its plan — the value the EOF fixpoint iterates to) and WS3's non-convergence raise in `run_end_of_input_barrier_flush`; the production assembly chain at HEAD (`load_settings_from_config_dict` → `instantiate_plugins_from_config` → `ExecutionGraph.from_plugin_instances` → `assemble_and_validate_pipeline_config`, exactly the sequence `tests/fixtures/dag_scenario_corpus/harness.py::build_scenario` runs at :324-388); Tasks 1–2's gate on both surfaces.
- Produces: the campaign's depth-5 crash+resume evidence (test-harness scout §6 depth-5 row, crash+resume variant) — a run crashed mid-unwrap of a depth-5 nested bound-region chain resumes without a false refuse and reaches the SAME terminal outcome as WS3's live acceptance run.

**Harness choice (per the test-harness scout):** the e2e `harness._build_pipeline` is fixed-linear (scout risk note 3) so `_CrashedRun` cannot author nested regions, and the spawn/SIGKILL machinery buys nothing here because the crash is a deterministic in-process raise. This task therefore uses the Task-2 construction style (real Orchestrator run, injected failure, then real `resume()`), with the injection done through CONFIG, not monkeypatching: the crash run's `PipelineConfig.escalation_fixpoint_bound` is clamped to 2, so the EOF escalation fixpoint raises its non-convergence `OrchestrationInvariantError` (WS3 Task 8's raise) before the 5-level unwrap completes — sources exhausted, checkpoints written, the innermost `quarantined` loss durable, and the un-unwrapped levels' ok-branch arrivals durably BLOCKED at their coalesces. That is exactly the mid-chain image spec §8 exists to protect: the gate must not false-refuse it.

- [ ] **Step 1: Write the test**

Create `tests/e2e/recovery/test_depth5_resume_mid_chain.py` (run it from the MAIN checkout — scout risk note 4: e2e recovery suites fail on capture-root binding in worktrees):

```python
"""Depth-5 nested crash + resume (spec §8 + §6.3, WS5).

Crash a depth-5 all-require_all nested run MID-UNWRAP — inner groups durably
BLOCKED, escalation partially staged — then resume. The satisfiability gate
must not false-refuse (every minted member is lost, live, or arrived), and
the resumed run must reach the SAME terminal outcome as WS3's live
acceptance run (test_depth5_group_unwrap.py): one quarantined loss, one
escalated loss per enclosing level (merge_4..merge_1), outermost quarantine,
COMPLETED-family run status.

The crash is injected through config, not monkeypatching: the crash run's
PipelineConfig.escalation_fixpoint_bound is clamped to 2, so the EOF
escalation fixpoint raises OrchestrationInvariantError before the unwrap
completes. The resume leg runs with the real graph-derived bound.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import ResumePoint, RunStatus, TokenWorkStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import ElspethSettings, load_settings_from_config_dict
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import group_losses_table, runs_table, token_work_items_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from tests.integration.pipeline.test_depth5_group_unwrap import DEPTH, _nested_settings

_CRASH_BOUND = 2  # < the rounds a 5-level unwrap needs; >= 1 so drain work begins


def _assemble(
    settings: ElspethSettings, *, purpose: SinkEffectExecutionPurpose
) -> tuple[ExecutionGraph, PipelineConfig, Any]:
    """The production assembly sequence (build_scenario, harness.py:324-388)."""
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=purpose)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    if purpose is SinkEffectExecutionPurpose.RESUME:
        for sink_name, sink in execution_sinks.items():
            assert sink.supports_resume, f"depth-5 fixture sink {sink_name!r} must support resume"
            sink.configure_for_resume()
    execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    sink_effect_modes = sink_effect_modes_from_runtime_bindings(
        execution_sinks,
        execution_bindings,
        purpose=purpose,
        configured_options={name: settings.sinks[name].options for name in execution_sinks},
    )
    sink_effect_admission = validate_pipeline_sink_effect_capabilities(
        execution_sinks,
        configured_modes=sink_effect_modes,
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        queues=settings.queues,
    )
    graph.validate()
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )
    return graph, config, bundle


def test_depth5_mid_unwrap_crash_resumes_to_the_acceptance_outcome(tmp_path: Path) -> None:
    document = _nested_settings(DEPTH)
    settings = load_settings_from_config_dict(document)
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    checkpoint_mgr = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(settings.checkpoint)
    payloads = FilesystemPayloadStore(tmp_path / "payloads")

    # ---- Crash leg: clamp the fixpoint bound; the EOF drain dies mid-unwrap. ----
    graph, config, _bundle = _assemble(settings, purpose=SinkEffectExecutionPurpose.FRESH)
    crash_config = dataclasses.replace(config, escalation_fixpoint_bound=_CRASH_BOUND)
    with pytest.raises(OrchestrationInvariantError, match="did not converge"):
        Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config).run(
            crash_config, graph=graph, settings=settings, payload_store=payloads,
            openrouter_catalog_sha256="0" * 64, openrouter_catalog_source="bundled",
        )

    with db.connection() as conn:
        run_row = conn.execute(select(runs_table)).one()
        run_id = str(run_row.run_id)
        # Crashed-image preconditions — the mid-chain shape this test exists for.
        assert RunStatus(run_row.status) is RunStatus.FAILED
        blocked_closers = set(
            conn.execute(
                select(token_work_items_table.c.coalesce_name).where(
                    token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value
                )
            ).scalars()
        ) - {None}
        assert blocked_closers, "an inner group must be durably BLOCKED at the crash instant"
        reasons = list(conn.execute(select(group_losses_table.c.reason)).scalars())
        assert "quarantined" in reasons  # innermost loss rode the claim disposition — durable pre-crash
        assert len([r for r in reasons if r == "group_failed"]) < DEPTH - 1  # unwrap genuinely unfinished

    # ---- Gate, advisory surface: an honest mid-unwrap image never false-refuses. ----
    check = RecoveryManager(db, checkpoint_mgr).can_resume(run_id, graph)
    assert check.can_resume, f"false refuse on a mid-unwrap depth-5 image: {check.reason}"

    # ---- Resume leg: real bound, real Orchestrator.resume — the enforcing surface
    # passing is the absence of GroupUnsatisfiableResumeError here. ----
    latest = checkpoint_mgr.get_latest_checkpoint(run_id)
    assert latest is not None
    resume_point = ResumePoint(checkpoint=latest, sequence_number=latest.sequence_number)
    resume_graph, resume_config, _ = _assemble(settings, purpose=SinkEffectExecutionPurpose.RESUME)
    Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config).resume(
        resume_point=resume_point, config=resume_config, graph=resume_graph, payload_store=payloads,
    )

    # ---- Terminal outcome: identical to WS3's live acceptance run. Keep these
    # assertions in literal lockstep with
    # test_depth5_single_failure_unwraps_to_outermost_quarantine. ----
    with db.connection() as conn:
        run_row = conn.execute(select(runs_table)).one()
        # COMPLETED-family, ratified: outermost on_group_failure: quarantine is
        # HANDLED failure — the run terminates by settlement, never FAILED.
        assert RunStatus(run_row.status) is RunStatus.COMPLETED_WITH_FAILURES, run_row.status
        losses = conn.execute(select(group_losses_table)).fetchall()
        assert len([l for l in losses if l.reason == "quarantined"]) == 1
        escalated = [l for l in losses if l.reason == "group_failed"]
        assert len(escalated) == DEPTH - 1
        assert {l.closer_name for l in escalated} == {"merge_4", "merge_3", "merge_2", "merge_1"}
        statuses = set(conn.execute(select(token_work_items_table.c.status)).scalars())
        assert TokenWorkStatus.BLOCKED.value not in statuses  # every roster closed
```

Reconcile before running: (a) WS3's landed `run_settings` fixture wraps this same assembly chain — if its plumbing diverged from `build_scenario`'s sequence (extra kwargs, a different sink-effect purpose default), mirror the landed fixture, not this sketch; (b) the non-convergence `match=` string is WS3 Task 8's raise — pin whatever it landed; (c) if `_CRASH_BOUND = 2` turns out to complete the unwrap (the fixpoint advances more than one level per iteration), drop it to 1 — the crashed-image precondition asserts (`group_failed` count < DEPTH-1, BLOCKED closers non-empty) exist precisely to catch a construction that no longer crashes mid-chain. The crash-leg preconditions, the no-false-refuse checks on BOTH surfaces, and the lockstep terminal-outcome assertions are this plan's fixed deliverable.

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/e2e/recovery/test_depth5_resume_mid_chain.py -v`
Expected: PASS with no production-code change — this is pure acceptance over Tasks 1–2 plus WS2/WS3's landed machinery. A false refuse here is a Task 1/2 defect (fix at the gate); a wrong terminal outcome is a WS3 escalation/resume defect — surface it to the campaign coordinator, never bend the assertions.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/recovery/test_depth5_resume_mid_chain.py
git commit -m "test(e2e): depth-5 mid-unwrap crash + resume — no false refuse, acceptance-identical outcome (spec §8)"
```

- [ ] **Step 4: WS5 SLICE BOUNDARY**

1. `git rev-parse HEAD` (record).
2. Full suite: `.venv/bin/pytest tests/` — must be green.
3. `git rev-parse HEAD` again — must equal the recorded value, else re-run.
4. Trust-tier corpus diff vs the pre-slice baseline (command in Global Constraints) — zero added findings.
5. Wardline gate (command in Global Constraints) — exit 0.

---

### Task 6: WS6 — the closed group-settlement reason vocabulary, and `scope_group_failed` at both emission sites

**Files:**
- Modify: `src/elspeth/contracts/enums.py` (new `GroupSettlementReason` StrEnum after `TerminalPath`, :203ff)
- Modify: `src/elspeth/engine/coalesce_executor.py` (late-arrival arm, :806-860; completed-key dedup value at the two set-sites, :1470/:1516 as re-keyed by WS4)
- Modify: `src/elspeth/core/landscape/execution/node_states.py` (two new group-keyed reads beside :766-875)
- Modify: `src/elspeth/core/landscape/factory.py` (pass-through of the new reads beside `get_completed_row_ids_for_nodes` :167-168)
- Modify: `src/elspeth/engine/barrier_coordination.py` (restore reconcile, :1415-1449)
- Modify (re-point only): WS3/WS4 emission sites for `empty_expansion` / `all_members_lost` string literals → the enum
- Test: `tests/unit/engine/test_group_settlement_reasons.py` (new), plus extensions named below

**Interfaces:**
- Consumes: WS4's re-keyed coalesce completion state `(coalesce_name, fork_group_id)` and the WS3/WS4 emission sites for group failure (`empty_expansion`, `all_members_lost`); `TokenInfo.fork_group_id` derived accessor (canonical); `NodeStateStatus` (`contracts/enums.py:38`).
- Produces (Tasks 7–8 and the corpus schema extension rely on these):
  - `GroupSettlementReason(StrEnum)` with EXACTLY: `LATE_ARRIVAL_AFTER_MERGE = "late_arrival_after_merge"`, `SCOPE_GROUP_FAILED = "scope_group_failed"`, `EMPTY_EXPANSION = "empty_expansion"`, `ALL_MEMBERS_LOST = "all_members_lost"`
  - `NodeStateRepository.has_released_group_for_node(*, run_id: str, node_id: str, group_id: str) -> bool`
  - `NodeStateRepository.get_released_group_ids_for_nodes(run_id: str, node_ids: frozenset[str]) -> set[tuple[str, str]]` (pairs `(node_id, group_id)`)

**The rule being implemented (spec §2 + §6.4):** `scope_group_failed` is the reason for a member terminated *because its group failed* — **never** `late_arrival_after_merge`. Today one string covers both flavors at `barrier_coordination.py:447-482` (live release — reason originates in the coalesce executor's late arm at :814) and `:1438` (restore reconcile). The discriminator is durable: a group that closed by MERGE has a status-`COMPLETED` node_state at the closer for its consumed members (the `get_released_row_ids_for_nodes` insight, node_states.py:833-874 — a FAILED closure sets `completed_at` too, so "completed" alone cannot discriminate); a group that closed by FAILURE has completed-but-not-released states.

- [ ] **Step 1: Write the failing vocabulary + discrimination tests**

Create `tests/unit/engine/test_group_settlement_reasons.py`:

```python
"""Closed group-settlement reason vocabulary (spec §2/§6.4, WS6).

Reasons are categorical tokens from ONE StrEnum — never free prose, never
hand-written strings at emission sites. `scope_group_failed` is the reason
for a member terminated because its group failed; `late_arrival_after_merge`
is reserved for arrival after a SUCCESSFUL merge.
"""

from __future__ import annotations

from elspeth.contracts.enums import GroupSettlementReason


def test_vocabulary_is_exactly_the_specced_four() -> None:
    assert {r.value for r in GroupSettlementReason} == {
        "late_arrival_after_merge",
        "scope_group_failed",
        "empty_expansion",
        "all_members_lost",
    }
```

Then add the two flavor cases to `tests/integration/pipeline/test_barrier_intake_dispositions.py`, inside `TestLateBranchRelease` (:221), modeled line-for-line on `test_late_branch_released_in_same_iteration_run_finalize_ready` (:224 — verified at HEAD; the builders below are that module's own: `_make_factory`, `_real_coalesce_executor(factory, clock, *, policy=...)` :98, `_coalesce_processor` :127, `_branch_token` :148 (WS1-migrated to mint a shared-group FORK frame), `_arrive_via_intake` :157, `_record_foreign_loss` :192 (WS3-migrated onto `group_losses`), `_release_events` :180). Add the import `from elspeth.contracts.enums import GroupSettlementReason`:

```python
    def test_late_arrival_after_successful_merge_keeps_late_arrival_reason(self) -> None:
        """Group closed by MERGE: the late sibling's reason stays late_arrival_after_merge."""
        clock = MockClock(start=_T0)
        db, factory = _make_factory()
        executor = _real_coalesce_executor(factory, clock, policy="first")
        processor = _coalesce_processor(factory, executor, clock)

        # policy=first: branch a's arrival completes the group by MERGE.
        results = _arrive_via_intake(factory, processor, _branch_token("a"))
        assert len(results) == 1
        assert results[0].scheduler_pending_sink is True  # merged, not failed

        clock.advance(2.0)
        _arrive_via_intake(factory, processor, _branch_token("b"), ingest_sequence=1)
        events = _release_events(db, "tok-branch-b")
        assert len(events) == 1
        context = json.loads(str(events[0]["context_json"]))
        assert context["late_arrival"] is True
        assert context["reason"] == GroupSettlementReason.LATE_ARRIVAL_AFTER_MERGE.value

    def test_survivor_arriving_after_group_failure_gets_scope_group_failed(self) -> None:
        """Group closed by FAILURE: a member arriving after it must carry
        scope_group_failed — NEVER late_arrival_after_merge (spec §2)."""
        clock = MockClock(start=_T0)
        db, factory = _make_factory()
        executor = _real_coalesce_executor(factory, clock, policy="require_all")
        processor = _coalesce_processor(factory, executor, clock)

        # Branch b is durably lost; branch a's arrival completes the group as
        # FAILURE (the must-fail-within-replay-iteration shape, :538).
        _record_foreign_loss(db, clock, branch="b", token_id="tok-branch-b", reason="quarantined")
        failed_results = _arrive_via_intake(factory, processor, _branch_token("a"))
        assert [(r.outcome, r.path) for r in failed_results] == [(TerminalOutcome.FAILURE, TerminalPath.UNROUTED)]

        # The lost member's token is re-presented late (lease-expiry
        # redelivery) against the FAILED-completed key.
        clock.advance(2.0)
        late_results = _arrive_via_intake(factory, processor, _branch_token("b"), ingest_sequence=1)
        assert [(r.outcome, r.path) for r in late_results] == [(TerminalOutcome.FAILURE, TerminalPath.UNROUTED)]
        events = _release_events(db, "tok-branch-b")
        assert len(events) == 1
        context = json.loads(str(events[0]["context_json"]))
        assert context["late_arrival"] is True
        assert context["reason"] == GroupSettlementReason.SCOPE_GROUP_FAILED.value
```

Two reconcile points before running: (a) `_record_foreign_loss` is WS3-migrated — its post-migration signature takes a `GroupLossSpec`-shaped set of kwargs (`closer_name`/`group_id`/`member_key`); adapt the call to the landed builder, keeping branch "b" as the lost member; (b) post-WS4 the completion key is `(coalesce_name, fork_group_id)`, so both `_branch_token` calls in one test must mint the SAME fork group (the WS1-migrated builder's default for same-row branch tokens — verify, don't assume).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/engine/test_group_settlement_reasons.py -v`
Expected: FAIL — `ImportError: cannot import name 'GroupSettlementReason'`.

- [ ] **Step 3: Implement**

(a) `contracts/enums.py`, after `TerminalPath`:

```python
class GroupSettlementReason(StrEnum):
    """Closed vocabulary for group-settlement dispositions (spec §2/§6.4).

    The StrEnum IS the vocabulary: emission sites reference these members,
    never string literals. `SCOPE_GROUP_FAILED` = member terminated because
    its group failed — never `LATE_ARRIVAL_AFTER_MERGE`, which is reserved
    for arrival after a successful merge.
    """

    LATE_ARRIVAL_AFTER_MERGE = "late_arrival_after_merge"
    SCOPE_GROUP_FAILED = "scope_group_failed"
    EMPTY_EXPANSION = "empty_expansion"
    ALL_MEMBERS_LOST = "all_members_lost"
```

(b) `node_states.py` — two group-keyed siblings of the row-keyed reads (:766/:833), joining `token_lineage_frames` instead of `tokens`:

```python
    def has_released_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool:
        """Point lookup: did this group close at this node by a SUCCESSFUL
        release? A FAILED closure sets completed_at too, so status COMPLETED
        is the discriminator (see get_released_row_ids_for_nodes)."""
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
                token_lineage_frames_table.c.group_id == group_id,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def get_released_group_ids_for_nodes(self, run_id: str, node_ids: frozenset[str]) -> set[tuple[str, str]]:
        """(node_id, group_id) pairs whose group closed at the node by a
        status-COMPLETED release — the merged/released flavor of the
        completed set the restore reconcile consumes."""
        if not node_ids:
            return set()
        query = (
            select(node_states_table.c.node_id, token_lineage_frames_table.c.group_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.group_id) for row in rows}
```

Add the two pass-throughs in `core/landscape/factory.py` beside `get_completed_row_ids_for_nodes` (:167-168), and mirror them on whatever facade `barrier_restore_reads` exposes (grep `get_completed_row_ids_for_nodes` for the full delegation chain: `factory.py:167`, `execution_repository.py:360-366`).

(c) `coalesce_executor.py` late arm (:806-860 as re-keyed by WS4): replace the hardcoded reason with the flavor discriminator. The completed-key cache value carries the flavor — change the two dedup set-sites (:1470/:1516 as re-keyed) from marking bare completion to storing `merged: bool` (True at the merge-completion site, False at the failure-completion site), then:

```python
        if key in self._completed_keys or self._check_landscape_for_completion(coalesce_name, token.fork_group_id):
            merged = (
                self._completed_keys[key]
                if key in self._completed_keys
                else self._execution.has_released_group_for_node(
                    run_id=self._run_id, node_id=node_id, group_id=token.fork_group_id
                )
            )
            failure_reason = (
                GroupSettlementReason.LATE_ARRIVAL_AFTER_MERGE.value
                if merged
                else GroupSettlementReason.SCOPE_GROUP_FAILED.value
            )
```

(the rest of the arm — node_state, `CoalesceFailureReason`, `record_token_outcome`, the returned `CoalesceOutcome` — is unchanged: `failure_reason` flows through it and through `barrier_coordination.py:460/:479` to the released row). Keep `CoalesceMetadata.for_late_arrival`'s prose accurate for both flavors.

(d) `barrier_coordination.py` restore reconcile (:1415-1449 as re-keyed by WS4): alongside the completed set, fetch the released set and discriminate:

```python
                released_keys_set = {
                    (node_id_to_coalesce_name[node_id_str], group_id)
                    for node_id_str, group_id in self._barrier_restore_reads.get_released_group_ids_for_nodes(
                        self._run_id, frozenset(node_id_to_coalesce_name.keys())
                    )
                    if node_id_str in node_id_to_coalesce_name
                }
```

and in the release_context: `"reason": GroupSettlementReason.LATE_ARRIVAL_AFTER_MERGE.value if key in released_keys_set else GroupSettlementReason.SCOPE_GROUP_FAILED.value`.

(e) Re-point the WS3/WS4 emission sites for `"empty_expansion"` and `"all_members_lost"` string literals onto the enum: `git grep -n '"empty_expansion"\|"all_members_lost"\|"scope_group_failed"\|"late_arrival_after_merge"' -- src/elspeth/` and replace every emission-site literal with the enum member's `.value` (comparison sites in tests may keep literals — they pin the wire value).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/unit/engine/test_group_settlement_reasons.py tests/integration/pipeline/test_barrier_intake_dispositions.py tests/unit/engine/test_barrier_coordination.py tests/property/engine/test_coalesce_properties.py -v`
Expected: PASS. If a pre-existing test pins the OLD undiscriminated reason for a group-failure survivor, that pin is a locked-in-buggy-expectation under spec §2 — re-point it to `scope_group_failed` and say so in the commit message.

- [ ] **Step 5: Postgres reason-width sanity**

`group_losses.reason` and the release-context reasons stay categorical; the new tokens are ≤ 64 chars by inspection (`late_arrival_after_merge` = 23). Confirm the WS3-migrated Postgres suite (`tests/testcontainer/core/` successor of `test_coalesce_branch_loss_reason_postgres.py`) covers the longest new token; if its parametrization is literal-based, add `GroupSettlementReason.SCOPE_GROUP_FAILED.value` to it.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/contracts/enums.py src/elspeth/engine/coalesce_executor.py src/elspeth/engine/barrier_coordination.py src/elspeth/core/landscape/execution/node_states.py src/elspeth/core/landscape/factory.py src/elspeth/core/landscape/execution_repository.py tests/unit/engine/test_group_settlement_reasons.py
# plus the intake-disposition / emission-site test files actually touched
git commit -m "feat(dispositions): closed group-settlement vocabulary; scope_group_failed at both emission sites (spec §2/§6.4)"
```

---

### Task 7: Landscape-MCP query surface for groups and lineage

**Files:**
- Modify: `src/elspeth/mcp/analyzers/queries.py` (re-point `list_tokens` :168-211; new `list_group_records`, `list_group_losses`, `get_token_lineage` after `get_token_children` :242)
- Modify: `src/elspeth/mcp/analyzers/reports.py` (fork/join counts :706-723)
- Modify: `src/elspeth/mcp/types.py` (`TokenRecord` :74-84; new TypedDicts)
- Modify: `src/elspeth/mcp/analyzer.py` (facade methods beside `list_tokens` :86-87)
- Modify: `src/elspeth/mcp/server.py` (three `_ToolDef` entries in `_TOOLS` :122)
- Test: `tests/unit/mcp/test_group_queries.py` (new); update `tests/unit/mcp/test_analyzer_queries.py` / `test_arg_validation.py` / `test_type_literals.py` / `test_server_call_tool.py` where they pin the tool set or `TokenRecord` keys

**Interfaces:**
- Consumes: the three canonical tables; `tokens.join_group_id` (kept column); `FrameKind`.
- Produces (the Task 8 acceptance test drives EXACTLY these):
  - `LandscapeAnalyzer.list_group_records(run_id: str, kind: str | None = None) -> list[GroupRecordEntry]`
  - `LandscapeAnalyzer.list_group_losses(run_id: str, group_id: str | None = None) -> list[GroupLossEntry]`
  - `LandscapeAnalyzer.get_token_lineage(run_id: str, token_id: str) -> list[LineageFrameEntry]`
  - `TokenRecord` = `{token_id, row_id, join_group_id, step_in_pipeline, created_at, lineage_path: list[LineageFrameEntry], branch_name, fork_group_id, expand_group_id}` — the last three DERIVED from the path (ruling 21: derived accessors are the only read path for the legacy names; deriving in the projection is that rule applied to the wire, never a column read)
  - MCP tools `list_group_records`, `list_group_losses`, `get_token_lineage`

**Sequencing note:** WS1's atomic flip may already have re-pointed `list_tokens`/`reports.py` minimally to keep the tree green. This task lands the FINAL contract; diff the current state of both files against HEAD-of-WS1 expectations before editing, and replace any WS1 shim rather than layering on it.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/mcp/test_group_queries.py`, seeding with the same raw-seed helpers as Task 1 (factor the shared `_seed_run`/`_seed_fork_member`/`_seed_expand_group` helpers into `tests/fixtures/group_lineage.py` and import them from BOTH files — do that extraction in this step and re-point Task 1's file):

```python
"""Group/lineage read surface of the Landscape MCP analyzer (spec §9 row 6)."""

from __future__ import annotations

from elspeth.mcp.analyzers import queries
from tests.fixtures.group_lineage import (
    EXPAND_GROUP,
    FORK_GROUP,
    RUN_ID,
    make_seeded_db_and_factory,  # returns (db, AnalyzerRepositories-compatible factory)
    seed_expand_group,
    seed_fork_member,
    seed_loss,
)


def test_list_group_records_returns_expand_roster_facts() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_expand_group(db, member_count=3)
    records = queries.list_group_records(db, factory, RUN_ID)
    assert [(r["group_id"], r["kind"], r["member_count"]) for r in records] == [(EXPAND_GROUP, "expand", 3)]
    assert records[0]["opener_token_id"] == "tok-opener"


def test_list_group_losses_projects_the_full_ledger_row() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-b", member_key="path_b")
    seed_loss(db, member_key="path_b", token_id="tok-b", adopted_epoch=None)
    losses = queries.list_group_losses(db, factory, RUN_ID)
    assert len(losses) == 1
    loss = losses[0]
    assert loss["closer_name"] == "merger"
    assert loss["group_id"] == FORK_GROUP
    assert loss["member_key"] == "path_b"
    assert loss["token_id"] == "tok-b"
    assert loss["reason"] == "quarantined"


def test_get_token_lineage_is_depth_ordered_outermost_first() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-nested", member_key="path_a")  # depth 0 FORK frame
    # add a depth-1 EXPAND frame on the same token (fork-then-expand nesting)
    seed_expand_frame(db, token_id="tok-nested", depth=1, group_id=EXPAND_GROUP)
    frames = queries.get_token_lineage(db, factory, RUN_ID, "tok-nested")
    assert [f["depth"] for f in frames] == [0, 1]
    assert [f["kind"] for f in frames] == ["fork", "expand"]


def test_list_tokens_projects_lineage_path_and_derived_names() -> None:
    db, factory = make_seeded_db_and_factory()
    seed_fork_member(db, token_id="tok-a", member_key="path_a")
    tokens = {t["token_id"]: t for t in queries.list_tokens(db, factory, RUN_ID)}
    record = tokens["tok-a"]
    assert record["lineage_path"] == [
        {"depth": 0, "kind": "fork", "group_id": FORK_GROUP, "member_key": "path_a"}
    ]
    # Derived names (ruling 21): innermost FORK frame.
    assert record["branch_name"] == "path_a"
    assert record["fork_group_id"] == FORK_GROUP
    assert record["expand_group_id"] is None
```

(`seed_expand_frame` is a trivial addition to the shared fixture module: one `token_lineage_frames` insert at the given depth.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/unit/mcp/test_group_queries.py -v`
Expected: FAIL — `AttributeError: module 'elspeth.mcp.analyzers.queries' has no attribute 'list_group_records'` (and/or the fixture module import error until it is extracted).

- [ ] **Step 3: Implement**

(a) `mcp/types.py` — replace `TokenRecord` (:74-84) and add:

```python
class LineageFrameEntry(TypedDict):
    """One lineage frame of a token's path (outermost first, depth 0 = outermost)."""

    depth: int
    kind: str          # FrameKind value: "fork" | "expand"
    group_id: str
    member_key: str


class TokenRecord(TypedDict):
    """A token record as returned by ``list_tokens``.

    ``lineage_path`` is the stored truth (token_lineage_frames);
    ``branch_name``/``fork_group_id``/``expand_group_id`` are DERIVED from it
    (innermost FORK / innermost EXPAND frame — ruling 21's accessor rule
    applied to the wire). ``join_group_id`` is the kept merged-token column.
    """

    token_id: str
    row_id: str
    join_group_id: str | None
    step_in_pipeline: int | None
    created_at: str | None
    lineage_path: list[LineageFrameEntry]
    branch_name: str | None
    fork_group_id: str | None
    expand_group_id: str | None


class GroupRecordEntry(TypedDict):
    """A group roster record as returned by ``list_group_records``."""

    group_id: str
    kind: str
    opener_token_id: str
    member_count: int
    created_at: str | None


class GroupLossEntry(TypedDict):
    """A group-loss ledger row as returned by ``list_group_losses``."""

    loss_id: str
    closer_name: str
    group_id: str
    member_key: str
    token_id: str
    reason: str
    recorded_by: str
    recorded_at: str | None
    adopted_epoch: int | None
```

(b) `queries.py` — rewrite the `list_tokens` projection (:199-211): after fetching token rows, fetch all frames for the run's selected tokens in ONE query ordered by `(token_id, depth)`, group in Python, and derive:

```python
    frames_by_token: dict[str, list[LineageFrameEntry]] = {}
    with db.connection() as conn:
        frame_rows = conn.execute(
            select(token_lineage_frames_table)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.token_id.in_([row.token_id for row in rows]),
            )
            .order_by(token_lineage_frames_table.c.token_id, token_lineage_frames_table.c.depth)
        ).fetchall()
    for frame in frame_rows:
        frames_by_token.setdefault(str(frame.token_id), []).append(
            {
                "depth": int(frame.depth),
                "kind": str(frame.kind),
                "group_id": str(frame.group_id),
                "member_key": str(frame.member_key),
            }
        )

    def _derived(path: list[LineageFrameEntry], kind: str) -> LineageFrameEntry | None:
        innermost = None
        for entry in path:
            if entry["kind"] == kind:
                innermost = entry
        return innermost

    records: list[TokenRecord] = []
    for row in rows:
        path = frames_by_token.get(str(row.token_id), [])
        fork = _derived(path, FrameKind.FORK.value)
        expand = _derived(path, FrameKind.EXPAND.value)
        records.append(
            {
                "token_id": row.token_id,
                "row_id": row.row_id,
                "join_group_id": row.join_group_id,
                "step_in_pipeline": row.step_in_pipeline,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "lineage_path": path,
                "branch_name": fork["member_key"] if fork else None,
                "fork_group_id": fork["group_id"] if fork else None,
                "expand_group_id": expand["group_id"] if expand else None,
            }
        )
    return records
```

Add the three new query functions following the module's `(db, factory, ...)` convention with inline imports like `list_tokens` (:187-189):

```python
def list_group_records(db: LandscapeDB, factory: AnalyzerRepositories, run_id: str, kind: str | None = None) -> list[GroupRecordEntry]:
    """List group roster records (fork/expand openings) for a run."""
    from sqlalchemy import select

    from elspeth.core.landscape.schema import group_records_table

    with db.connection() as conn:
        query = select(group_records_table).where(group_records_table.c.run_id == run_id).order_by(group_records_table.c.created_at, group_records_table.c.group_id)
        if kind is not None:
            query = query.where(group_records_table.c.kind == kind)
        rows = conn.execute(query).fetchall()
    return [
        {
            "group_id": row.group_id,
            "kind": row.kind,
            "opener_token_id": row.opener_token_id,
            "member_count": int(row.member_count),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def list_group_losses(db: LandscapeDB, factory: AnalyzerRepositories, run_id: str, group_id: str | None = None) -> list[GroupLossEntry]:
    """List the group-loss ledger for a run (append-only; adopted or not)."""
    from sqlalchemy import select

    from elspeth.core.landscape.schema import group_losses_table

    with db.connection() as conn:
        query = select(group_losses_table).where(group_losses_table.c.run_id == run_id).order_by(group_losses_table.c.recorded_at, group_losses_table.c.loss_id)
        if group_id is not None:
            query = query.where(group_losses_table.c.group_id == group_id)
        rows = conn.execute(query).fetchall()
    return [
        {
            "loss_id": row.loss_id,
            "closer_name": row.closer_name,
            "group_id": row.group_id,
            "member_key": row.member_key,
            "token_id": row.token_id,
            "reason": row.reason,
            "recorded_by": row.recorded_by,
            "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
            "adopted_epoch": int(row.adopted_epoch) if row.adopted_epoch is not None else None,
        }
        for row in rows
    ]


def get_token_lineage(db: LandscapeDB, factory: AnalyzerRepositories, run_id: str, token_id: str) -> list[LineageFrameEntry]:
    """A token's full lineage path, outermost (depth 0) first."""
    from sqlalchemy import select

    from elspeth.core.landscape.schema import token_lineage_frames_table

    with db.connection() as conn:
        rows = conn.execute(
            select(token_lineage_frames_table)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.token_id == token_id,
            )
            .order_by(token_lineage_frames_table.c.depth)
        ).fetchall()
    return [
        {"depth": int(row.depth), "kind": str(row.kind), "group_id": str(row.group_id), "member_key": str(row.member_key)}
        for row in rows
    ]
```

(c) `reports.py` (:706-723) — re-derive: `fork_count` = `COUNT(DISTINCT group_id)` from `token_lineage_frames` where `kind == FrameKind.FORK.value` scoped to the run; `join_count` = `COUNT(DISTINCT tokens_table.c.join_group_id)` where not null (the KEPT column — token_outcomes no longer carries it); add `expand_operations` the same way from `kind == "expand"` frames and extend the `RunSummaryReport` summary TypedDict in `types.py` with `expand_operations: int`.

(d) `analyzer.py` — three facade methods beside `list_tokens` (:86-87):

```python
    def list_group_records(self, run_id: str, kind: str | None = None) -> list[GroupRecordEntry]:
        return queries.list_group_records(self._db, self._factory, run_id, kind=kind)

    def list_group_losses(self, run_id: str, group_id: str | None = None) -> list[GroupLossEntry]:
        return queries.list_group_losses(self._db, self._factory, run_id, group_id=group_id)

    def get_token_lineage(self, run_id: str, token_id: str) -> list[LineageFrameEntry]:
        return queries.get_token_lineage(self._db, self._factory, run_id, token_id)
```

(e) `server.py` — three `_ToolDef` entries beside `list_tokens` (:185-203), same `_ArgSpec` idiom:

```python
    "list_group_records": _ToolDef(
        description="List group roster records (fork/expand openings) for a run — the audit authority for group membership counts",
        args=_ArgSpec(required_str=("run_id",), optional_str=("kind",)),
        handler=lambda a, args: a.list_group_records(run_id=args["run_id"], kind=args["kind"]),
        schema_properties={
            "run_id": {"type": "string", "description": "Run ID to query"},
            "kind": {"type": ["string", "null"], "description": "Optional group kind filter", "enum": ["fork", "expand", None]},
        },
    ),
    "list_group_losses": _ToolDef(
        description="List the group-loss ledger for a run: which member of which group was lost, why, and who recorded it",
        args=_ArgSpec(required_str=("run_id",), optional_str=("group_id",)),
        handler=lambda a, args: a.list_group_losses(run_id=args["run_id"], group_id=args["group_id"]),
        schema_properties={
            "run_id": {"type": "string", "description": "Run ID to query"},
            "group_id": {"type": ["string", "null"], "description": "Optional group ID to filter by"},
        },
    ),
    "get_token_lineage": _ToolDef(
        description="A token's full lineage path (outermost frame first): every fork branch and expansion membership it carries",
        args=_ArgSpec(required_str=("run_id", "token_id")),
        handler=lambda a, args: a.get_token_lineage(run_id=args["run_id"], token_id=args["token_id"]),
        schema_properties={
            "run_id": {"type": "string", "description": "Run ID to query"},
            "token_id": {"type": "string", "description": "Token ID whose lineage to read"},
        },
    ),
```

- [ ] **Step 4: Run and repair the pinning suites**

Run: `.venv/bin/pytest tests/unit/mcp/ -v`
Expected: the new tests PASS; `test_type_literals.py` / `test_arg_validation.py` / `test_server_call_tool.py` / `test_analyzer_read_only.py` may redden where they enumerate `_TOOLS` or `TokenRecord` keys — extend their expected sets deliberately (that is the gate working; never loosen an assertion to a subset check).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/mcp/analyzers/queries.py src/elspeth/mcp/analyzers/reports.py src/elspeth/mcp/types.py src/elspeth/mcp/analyzer.py src/elspeth/mcp/server.py tests/unit/mcp/test_group_queries.py tests/fixtures/group_lineage.py tests/unit/core/checkpoint/test_group_satisfiability_gate.py
# plus any pinning suites updated in Step 4
git commit -m "feat(mcp): group_records/group_losses/lineage query surface; frames-derived token projection (spec §9 row 6)"
```

---

### Task 8: WS6 ACCEPTANCE — reconstruct a failed depth-3 nested group via MCP tools alone

**Files:**
- Create: `tests/integration/mcp/__init__.py` (empty)
- Test: `tests/integration/mcp/test_group_failure_forensics.py` (new)

**Interfaces:**
- Consumes: Task 7's analyzer surface (`list_group_records`, `list_group_losses`, `get_token_lineage`, `list_tokens`, plus existing `explain_token`/`get_node_states`); Task 6's `GroupSettlementReason`; WS2's config surface — `CollectorSettings`/`ScopeSettings` (WS2 Task 2, the ONE authored copy) and `ExecutionGraph.from_plugin_instances(..., collectors: Mapping[str, tuple[TransformProtocol, CollectorSettings]], scope_settings: Sequence[ScopeSettings], max_bound_region_depth: int = 5)` (WS2 Task 3's Produces, verified 2026-08-22; a collector plugin must be `is_batch_aware`, which Task 3 enforces); WS3's escalation + WS4's collector runtime, complete, plus the WS3+WS4 integration item's PipelineConfig collector plumbing (marked inline — the one spelling no on-disk plan owns).
- Produces: the spec §9 row 6 acceptance evidence — "given a failed nested group, an operator reconstructs from audit rows via the landscape MCP tools alone: which member failed, with what reason, through which escalation chain, why each survivor terminated."

**Topology (depth 3, all `require_all`):** source (1 row) → **outer scope** `document_pages` (opener `explode_pages`, 2 members) → inside each member: **fork** `section_fork` to `analysis` / `summary`, closing at coalesce `section_merge` → inside branch `analysis`: **inner scope** `sentence_scope` (opener `explode_sentences`, 2 members; closer collector `sentence_stitcher`) → coalesce → **outer collector** `page_stitcher` → sink. One innermost member (page 0, sentence 1) fails via an erroring transform with `on_error: discard`. Expected settlement: inner loss (the failing sentence) → inner collector FAILs → escalation loss against fork branch `analysis` at `section_merge` → coalesce FAILs → escalation loss against page-0's member at `page_stitcher` → outer group FAILs → `on_group_failure: quarantine`. Survivors (sentence 0, branch `summary`'s token, page 1's subtree) terminate `scope_group_failed`.

- [ ] **Step 1: Write the test**

```python
"""WS6 acceptance: nested-group failure forensics via the MCP analyzer alone.

Spec §9 row 6 acceptance criterion — from a failed depth-3 nested group, an
operator reconstructs from audit rows via the landscape MCP tools alone:
which member failed, with what reason, through which escalation chain, and
why each survivor terminated. Every assertion below reads ONLY
LandscapeAnalyzer methods (the MCP tool handlers' exact targets).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from elspeth.contracts.enums import FrameKind, GroupSettlementReason
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import runs_table
from elspeth.mcp.analyzer import LandscapeAnalyzer

# Pipeline construction: mirrors the fork/coalesce recipe of
# tests/property/audit/test_fork_coalesce_flow.py:257-310 and the coalesce
# branch-map wiring of tests/fixtures/dag_scenario_corpus/v1/
# fork-coalesce-policies/first-union.yaml (branches: {branch: connection}).
# Graph spellings are WS2 Task 3's Produces block, verified 2026-08-22:
# from_plugin_instances(collectors=Mapping[str, tuple[TransformProtocol,
# CollectorSettings]], scope_settings=Sequence[ScopeSettings],
# max_bound_region_depth=int). PipelineConfig's collector/scope runtime
# plumbing is the WS3+WS4 integration item's deliverable (WS2 adds ONLY
# escalation_fixpoint_bound to PipelineConfig) — the two config kwargs
# below are the sole remaining reconcile point, marked inline.

from elspeth.contracts import Determinism, PluginSchema, RunStatus
from elspeth.core.config import (
    CoalesceSettings,
    CollectorSettings,
    GateSettings,
    ScopeSettings,
    SourceSettings,
    TransformSettings,
)
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.contracts.schema_contract import PipelineRow
from sqlalchemy import select as sa_select
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.plugins import CollectSink, ListSource, PassTransform


class _TwoWayExploder(BaseTransform):
    """Multi-row expander (declared scope opener): one row in, two out."""

    name = "two_way_exploder"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = None
    input_schema: type[PluginSchema] = _TestSchema
    output_schema: type[PluginSchema] = _TestSchema
    creates_tokens = True  # the json_explode shape: success_multi mints an EXPAND group

    def __init__(self, name: str, child_field: str) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.name = name
        self._child_field = child_field

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:  # type: ignore[override]
        children = tuple(PipelineRow({**row.to_dict(), self._child_field: index}, row.contract) for index in range(2))
        return TransformResult.success_multi(children, success_reason={"action": "explode"})


class _FailOnMarkedSentence(BaseTransform):
    """Raises for exactly (page 0, sentence 1); on_error: discard stages the loss."""

    name = "sentence_probe"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = None
    input_schema: type[PluginSchema] = _TestSchema
    output_schema: type[PluginSchema] = _TestSchema

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:  # type: ignore[override]
        data = row.to_dict()
        if data.get("page") == 0 and data.get("sentence") == 1:
            raise RuntimeError("forensics fixture: innermost member failure (page 0, sentence 1)")
        return TransformResult.success(row, success_reason={"action": "pass"})


class _JoinBatch(BaseTransform):
    """Batch-aware collector plugin: joins a group's members into one row.

    WS2 Task 3 rejects a non-batch-aware collector plugin at graph build, so
    the collector stub must set is_batch_aware (the _SumBatchTransform shape,
    tests/integration/pipeline/test_aggregation_recovery.py:122)."""

    name = "join_batch"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = None
    input_schema: type[PluginSchema] = _TestSchema
    output_schema: type[PluginSchema] = _TestSchema
    is_batch_aware = True

    def __init__(self, name: str) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self.name = name

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:  # type: ignore[override]
        if isinstance(row, list):
            return TransformResult.success(
                PipelineRow({"joined": len(row)}, row[0].contract),
                success_reason={"action": "join"},
            )
        return TransformResult.success(row, success_reason={"action": "buffer"})


def _run_depth3_failure(tmp_path: Path) -> tuple[str, str]:
    """Run the depth-3 pipeline with one innermost member failing.

    Returns (database_url, run_id). Built with the real Orchestrator against a
    file-backed LandscapeDB so LandscapeAnalyzer (URL-opened, read-only) can
    attach afterwards.
    """
    database_url = f"sqlite:///{tmp_path / 'forensics.db'}"
    db = LandscapeDB(database_url)
    source = ListSource([{"value": 1}], name="list_source", on_success="pages_in")
    sink = CollectSink("default")
    explode_pages = _TwoWayExploder("explode_pages", child_field="page")
    explode_sentences = _TwoWayExploder("explode_sentences", child_field="sentence")
    sentence_probe = _FailOnMarkedSentence()
    summarize = PassTransform(name="summarize")
    wired = [
        WiredTransform(
            plugin=as_transform(explode_pages),
            settings=TransformSettings(name="explode_pages", plugin="explode_pages", input="pages_in", on_success="to_section_gate"),
        ),
        # Branch "analysis": inner scope opener, then the per-sentence probe.
        WiredTransform(
            plugin=as_transform(explode_sentences),
            settings=TransformSettings(name="explode_sentences", plugin="explode_sentences", input="analysis", on_success="sentences_in"),
        ),
        WiredTransform(
            plugin=as_transform(sentence_probe),
            settings=TransformSettings(name="sentence_probe", plugin="sentence_probe", input="sentences_in", on_success="stitch_in", on_error="discard"),
        ),
        # Branch "summary": plain passthrough to the coalesce.
        WiredTransform(
            plugin=as_transform(summarize),
            settings=TransformSettings(name="summarize", plugin="pass_transform", input="summary", on_success="merge_summary"),
        ),
    ]
    gate = GateSettings(
        name="section_fork",
        input="to_section_gate",
        condition="True",
        routes={"true": "fork", "false": "default"},
        fork_to=["analysis", "summary"],
    )
    coalesce = CoalesceSettings(
        name="section_merge",
        branches={"analysis": "stitched_sentences", "summary": "merge_summary"},
        policy="require_all",
        merge="union",
        on_success="pages_out",
    )
    stitch_sentences = _JoinBatch("stitch_sentences")
    stitch_pages = _JoinBatch("stitch_pages")
    sentence_stitcher_settings = CollectorSettings(
        name="sentence_stitcher", plugin="join_batch", input="stitch_in", on_success="stitched_sentences"
    )
    page_stitcher_settings = CollectorSettings(
        name="page_stitcher", plugin="join_batch", input="pages_out", on_success="default"
    )
    # WS2 Task 3's collectors shape: Mapping[str, tuple[TransformProtocol, CollectorSettings]].
    collectors = {
        "sentence_stitcher": (as_transform(stitch_sentences), sentence_stitcher_settings),
        "page_stitcher": (as_transform(stitch_pages), page_stitcher_settings),
    }
    scopes = [
        ScopeSettings(name="document_pages", opener="explode_pages", closer="page_stitcher", policy="require_all", on_group_failure="quarantine"),
        ScopeSettings(name="sentence_scope", opener="explode_sentences", closer="sentence_stitcher", policy="require_all", on_group_failure="escalate"),
    ]
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="pages_in", options={})},
        transforms=wired,
        sinks={"default": as_sink(sink)},
        gates=[gate],
        aggregations={},
        coalesce_settings=[coalesce],
        collectors=collectors,           # WS2 Task 3 spelling (verified)
        scope_settings=scopes,           # WS2 Task 3 spelling (verified)
    )
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(explode_pages), as_transform(explode_sentences), as_transform(sentence_probe), as_transform(summarize)],
        sinks={"default": as_sink(sink)},
        gates=[gate],
        coalesce_settings=[coalesce],
        collector_settings=[sentence_stitcher_settings, page_stitcher_settings],  # integration-item spelling — reconcile (see note below)
        scope_settings=scopes,                                                    # integration-item spelling — reconcile (see note below)
        escalation_fixpoint_bound=graph.escalation_fixpoint_bound,                # WS2 Task 5: sites with a graph in hand thread it
    )
    Orchestrator(db).run(config, graph=graph, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    with db.connection() as conn:
        run_row = conn.execute(sa_select(runs_table)).fetchall()[0]
    # COMPLETED-family, ratified: an outermost on_group_failure: quarantine is
    # HANDLED failure (as today), so the run terminates by SETTLEMENT — and
    # with this run's only source row quarantined the exact family member is
    # COMPLETED_WITH_FAILURES. A FAILED status here means the construction
    # crashed instead of settling: a fixture bug, never forensics data.
    assert RunStatus(run_row.status) is RunStatus.COMPLETED_WITH_FAILURES, run_row.status
    return database_url, str(run_row.run_id)


def test_depth3_group_failure_is_reconstructible_from_mcp_tools_alone(tmp_path: Path) -> None:
    database_url, run_id = _run_depth3_failure(tmp_path)
    analyzer = LandscapeAnalyzer(database_url)
    try:
        # --- 1. Which member failed, with what reason. ---
        losses = analyzer.list_group_losses(run_id)
        assert len(losses) == 3, f"expected the 3-level escalation chain, got {losses}"
        by_closer = {loss["closer_name"]: loss for loss in losses}
        assert set(by_closer) == {"sentence_stitcher", "section_merge", "page_stitcher"}
        innermost = by_closer["sentence_stitcher"]
        # The failing member's identity and its categorical reason.
        failing_token = innermost["token_id"]
        assert innermost["reason"] not in ("", None)

        # --- 2. The escalation chain, linked through the failing token's path. ---
        path = analyzer.get_token_lineage(run_id, failing_token)
        assert [frame["kind"] for frame in path] == [
            FrameKind.EXPAND.value,  # outer scope member (page 0)
            FrameKind.FORK.value,    # branch "analysis"
            FrameKind.EXPAND.value,  # inner scope member (the sentence)
        ]
        # Each escalation loss names the NEXT frame outward of the same path.
        assert by_closer["sentence_stitcher"]["group_id"] == path[2]["group_id"]
        assert by_closer["section_merge"]["group_id"] == path[1]["group_id"]
        assert by_closer["section_merge"]["member_key"] == "analysis"
        assert by_closer["page_stitcher"]["group_id"] == path[0]["group_id"]
        assert by_closer["page_stitcher"]["member_key"] == path[0]["member_key"]

        # --- 3. The rosters those groups were accountable to. ---
        group_records = {r["group_id"]: r for r in analyzer.list_group_records(run_id)}
        assert group_records[path[0]["group_id"]]["member_count"] == 2  # pages
        assert group_records[path[2]["group_id"]]["member_count"] == 2  # sentences

        # --- 4. Why each survivor terminated: scope_group_failed. ---
        tokens = analyzer.list_tokens(run_id, limit=500)
        survivor_ids = [
            t["token_id"]
            for t in tokens
            if any(f["group_id"] == path[2]["group_id"] for f in t["lineage_path"])
            and t["token_id"] != failing_token
        ]
        assert survivor_ids, "the inner group must have a surviving sibling"
        for survivor in survivor_ids:
            states = analyzer.get_node_states(run_id, limit=500, include_context=True)
            survivor_states = [s for s in states if s["token_id"] == survivor]
            rendered = str(survivor_states)
            assert GroupSettlementReason.SCOPE_GROUP_FAILED.value in rendered, (
                f"survivor {survivor} must carry a scope_group_failed disposition in its audit trail"
            )
    finally:
        analyzer.close()
```

The construction above is complete except for ONE deliberately-marked reconcile point: the two `# integration-item spelling — reconcile` kwargs on `PipelineConfig`. WS2 adds only `escalation_fixpoint_bound` to `PipelineConfig` (its Task 5), so the runtime plumbing that hands collectors/scopes to the processor is the WS3+WS4 integration line item's deliverable — reconcile the two field names against that item as landed, and if it wires collectors purely from the graph (no `PipelineConfig` field at all), delete the two kwargs. The graph kwargs are WS2 Task 3's Produces verbatim and need no reconciliation. The topology, names (`document_pages`/`sentence_scope`/`section_merge`/`sentence_stitcher`/`page_stitcher`), the terminal-status pin (`COMPLETED_WITH_FAILURES` — ratified), and the failure marker (page 0, sentence 1) are this plan's fixed deliverable.

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/integration/mcp/test_group_failure_forensics.py -v`
Expected: FAIL first on construction gaps against the WS2/WS4 surfaces; iterate the construction (not the assertions) until green. If an ASSERTION is unsatisfiable — e.g. the escalation losses don't link the way §6.3 describes — STOP: that is an acceptance failure of an earlier workstream; surface it to the campaign coordinator rather than bending the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/mcp/__init__.py tests/integration/mcp/test_group_failure_forensics.py
git commit -m "test(acceptance): depth-3 nested group failure reconstructed via MCP tools alone (spec §9 row 6)"
```

---

### Task 9: ADR-042, docs, and the WS6 slice boundary

**Files:**
- Create: `docs/architecture/adr/042-group-settlement-observability.md`
- Modify: `docs/architecture/adr/README.md` (index entry, matching its existing format)
- Modify: `docs/agents/recent-code-hints.md` (new dated entry)

**Interfaces:**
- Consumes: Tasks 6–8 as landed.
- Produces: the record future agents get pointed at by ADR-019/ADR-038 cross-references.

- [ ] **Step 1: Write ADR-042**

Follow the house ADR format (Context / Decision / Consequences / Alternatives / Related Decisions — model on ADR-038's structure). Required content, in the author's own prose:

- **Context:** one lineage truth (spec rev 3.2); tri-columns deleted; the observability question — how an operator reconstructs a nested group failure from audit rows alone.
- **Decision 1 — closed settlement vocabulary:** `GroupSettlementReason` StrEnum is THE vocabulary (`late_arrival_after_merge`, `scope_group_failed`, `empty_expansion`, `all_members_lost`); emission sites reference members, never literals; `scope_group_failed` never doubles as `late_arrival_after_merge` (the merged-vs-failed discriminator is the status-COMPLETED node_state at the closer).
- **Decision 2 — read surface:** `token_lineage_frames` is the sole lineage read authority; the legacy names (`branch_name`/`fork_group_id`/`expand_group_id`) exist on wire projections only as path-derived fields (ruling 21); `tokens.join_group_id` is the one surviving column (coalesce_effects FK anchor). MCP tools `list_group_records` / `list_group_losses` / `get_token_lineage` + the frames-bearing `list_tokens` are the operator surface; the acceptance criterion (depth-3 reconstruction, tools alone) is pinned by `tests/integration/mcp/test_group_failure_forensics.py`.
- **Decision 3 — resume protection (cross-ref):** the group-satisfiability gate and its two surfaces (ADR-038 §3a carries the sweep-asymmetry ruling).
- **Related:** ADR-019 (as amended by WS1), ADR-020 (batch posture, now structural via inert frames), ADR-029/030 (journal truths the gate's arrived-limb reads), ADR-038 §3a, the spec.

- [ ] **Step 2: Rolling-doc entry**

Add at the top of `docs/agents/recent-code-hints.md`:

```markdown
- **2026-08-21 — group-settlement reasons are a CLOSED StrEnum, and the
  merged-vs-failed discriminator is release status, not completion** (landed
  with the unified-lineage WS6 slice). Every settlement reason
  (`late_arrival_after_merge`, `scope_group_failed`, `empty_expansion`,
  `all_members_lost`) comes from `contracts.enums.GroupSettlementReason` —
  never write the string at an emission site. A group that closed by FAILURE
  has `completed_at` set on its closer node_states just like a merge does;
  only `status == COMPLETED` discriminates (that is why
  `has_released_group_for_node` / `get_released_group_ids_for_nodes` exist —
  do not "simplify" them onto `completed_at`). Read lineage ONLY from
  `token_lineage_frames` (or the TokenInfo accessors): the MCP `list_tokens`
  projection derives `branch_name`/`fork_group_id`/`expand_group_id` from
  the path, and `tests/unit/mcp/test_type_literals.py` +
  `test_arg_validation.py` pin the tool set — adding an analyzer tool means
  extending those expected sets in the same change.
```

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/adr/042-group-settlement-observability.md docs/architecture/adr/README.md docs/agents/recent-code-hints.md
git commit -m "docs(adr-042): group settlement vocabulary and observability surface"
```

- [ ] **Step 4: WS6 SLICE BOUNDARY**

Repeat Task 5a Step 4's checklist verbatim (HEAD-recorded full `pytest tests/`, trust-tier corpus diff with zero added findings, wardline gate exit 0).

---

## Execution-time reconciliation points (not open decisions)

The 2026-08-22 synthesis ratified the four decisions previously listed under Open Questions — the ADR-038 sweep asymmetry (Task 3), the arrived-limb reliance on collector arrivals always journaling (Task 1, now backed by WS4's two pins — `tests/unit/engine/test_collector_executor.py::test_every_arrival_journals_a_durable_hold` (its Task 4) and `tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py::test_blocked_collector_item_counts_as_blocked_barrier_work` (its Task 6)), the path-derived legacy names on the MCP wire projection (Task 7, ruling 21), and the outermost-quarantine COMPLETED-family run status (Task 8, pinned `COMPLETED_WITH_FAILURES`). They are settled canon. What remains below is reconciliation work performed at execution time, not a decision awaiting a ruling:

- **Cross-plan spellings:** WS4-owned names are reconciled against `2026-08-21-unified-lineage-ws4-collector.md` (verified 2026-08-22: `collector_name` + the `collector:<name>:<group-id>` barrier_key format per its Task 6) and WS2-owned names against `2026-08-21-unified-lineage-ws2-config-validation.md` (verified 2026-08-22: `graph.get_group_bindings()`, the `collectors=`/`scope_settings=` construction kwargs, the ONE authored `CollectorSettings`/`ScopeSettings` copy in its Task 2). Still assumed, behind single-seam adapters: the WS3+WS4 integration item's shared collector test builders (`_real_collector_executor` and siblings) and its `PipelineConfig` collector plumbing — that plan is not on disk at drafting time. The reconcile authority is the inline marks in this plan plus the WS3+WS4 integration plan's Interfaces blocks: the executor reconciles every marked use against those blocks before each consuming task.

## Open Questions

None — the campaign's last open item (`row_union_ab_experiment` pedagogy) was RULED 2026-08-22 (two variants; see protocols RC-4). No open decisions remain anywhere in the plan set.
