"""RowProcessor durable draining, expansion ordering, and recovery tests.

This module owns the shared processor construction and durable-token helpers.
Row lifecycle and aggregation facades are covered in test_processor_row_lifecycle;
graph traversal, gates, and coalesce settlement in test_processor_routing;
transform execution and retry behavior in test_processor_transform_execution.

Test strategy:
- Use LandscapeDB.in_memory() for real audit recording (no mock factory)
- Use real SpanFactory (no tracer — no-op spans)
- Mock transforms/gates to control routing outcomes
- Verify outcomes via RowResult assertions

This avoids the anti-pattern of testing mocks instead of behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, create_autospec, patch

import pytest

# For node registration
from elspeth.contracts import (
    BatchTransformProtocol,
    NodeType,
    RouteDestination,
    RowResult,
    SourceRow,
    TokenInfo,
    TransformProtocol,
    TransformResult,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
from elspeth.contracts.data import PluginSchema as _PermissiveSchema
from elspeth.contracts.enums import (
    FrameKind,
    NodeStateStatus,
    TerminalOutcome,
    TerminalPath,
    TriggerType,
)
from elspeth.contracts.errors import (
    AuditIntegrityError,
    OrchestrationInvariantError,
)
from elspeth.contracts.identity import (
    LineageFrame,
    lineage_path_from_json,
    path_branch_name,
    path_expand_group_id,
    path_fork_group_id,
)
from elspeth.contracts.results import FailureInfo
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.types import BranchName, CoalesceName, CollectorName, GateName, NodeID, RowUnionName, SinkName
from elspeth.core.config import AggregationSettings, GateSettings
from elspeth.core.dag.group_bindings import GroupBindingRegistry
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor, CoalesceOutcome
from elspeth.engine.processor import (
    MAX_WORK_QUEUE_ITERATIONS,
    BarrierJournalRestoreContext,
    DAGTraversalContext,
    ProcessorMode,
    RowProcessor,
    _FlushContext,
)
from elspeth.engine.retry import RetryManager
from elspeth.engine.spans import SpanFactory
from elspeth.engine.work_items import WorkItem
from elspeth.plugins.transforms.batch_replicate import BatchReplicateConfig
from elspeth.testing import make_contract, make_pipeline_row, make_row, make_source_row, make_token_info
from tests.fixtures.audit_hashing import fake_error_hash
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import (
    await_database_time,
    landscape_database_now,
    leader_coordination_token,
    make_recorder_with_run,
    member_token_for,
)

# =============================================================================
# Helpers
# =============================================================================

_DYNAMIC_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
_DEFAULT_SCHEDULER = object()
_TEST_LEADER_WORKER_ID = "seeder"


def _make_contract() -> SchemaContract:
    """Create a minimal schema contract for testing."""
    return make_contract(fields={"value": int}, mode="OBSERVED")


def _make_source_row(data: dict[str, Any] | None = None) -> SourceRow:
    """Create a valid SourceRow with contract."""
    contract = _make_contract()
    return make_source_row(data or {"value": 42}, contract=contract)


def test_aggregation_restore_plan_freezes_sequence_fields() -> None:
    from elspeth.contracts.barrier_scalars import AggregationNodeScalars
    from elspeth.engine.barrier_coordination import _AggregationRestorePlan

    plan = _AggregationRestorePlan(
        node_id=NodeID("agg"),
        items=[],
        member_order=["left", "right"],
        batch_id="batch-1",
        accepted_count_total=2,
        completed_flush_count=0,
        scalars=AggregationNodeScalars(count_fire_offset=None, condition_fire_offset=None),
    )

    assert plan.items == ()
    assert plan.member_order == ("left", "right")


def _persist_token_for_scheduler(
    factory: RecorderFactory,
    token: TokenInfo,
    *,
    run_id: str = "test-run",
    source_node_id: str = "source-0",
    row_index: int | None = None,
    source_row_index: int | None = None,
    ingest_sequence: int = 0,
) -> None:
    """Persist a fabricated unit-test token before scheduler-backed processing."""
    try:
        factory.data_flow.resolve_row_ingest_sequence(token.row_id)
    except AuditIntegrityError:
        factory.data_flow.create_row_with_token(
            coordination_token=leader_coordination_token(factory, run_id),
            source_node_id=source_node_id,
            row_index=ingest_sequence if row_index is None else row_index,
            source_row_index=ingest_sequence if source_row_index is None else source_row_index,
            ingest_sequence=ingest_sequence,
            row_id=token.row_id,
            token_id=token.token_id if not token.lineage_path else None,
            data=token.row_data.to_dict(),
        )
    if factory.query.get_token(token.token_id) is None:
        # A fabricated branch token models a fork child — mint the matching
        # lineage frame(s) durably via the create_token(..., lineage_path=)
        # seam so coalesce_tokens' durable strict pop (spec rulings 24/28) has
        # a frame to pop. Never weaken the pop to accommodate a raw-seeded
        # fixture; fix the fixture instead.
        #
        # Prefer the token's OWN lineage_path when the fixture set one — that
        # is the real (possibly multi-frame) in-memory path threaded straight
        # through, per the create_token seam contract. Fall back to a
        # synthetic single-FORK reconstruction from branch_name for older
        # fixtures that never populated lineage_path.
        lineage_path = (
            token.lineage_path
            if token.lineage_path
            else (
                (LineageFrame(kind=FrameKind.FORK, group_id=f"test-fork:{token.row_id}", member_key=token.branch_name),)
                if token.branch_name is not None
                else ()
            )
        )
        factory.data_flow.create_token(
            token.row_id,
            coordination_token=leader_coordination_token(factory, run_id),
            token_id=token.token_id,
            lineage_path=lineage_path,
        )


def _claim_processor_token(factory: RecorderFactory, token: TokenInfo, *, node_id: str, run_id: str = "test-run") -> TokenWorkItem:
    """Give direct processor calls the durable claim normally supplied by the drain."""
    _persist_token_for_scheduler(factory, token, run_id=run_id)
    leader = leader_coordination_token(factory, run_id)
    return factory.scheduler.enqueue_ready_claimed(
        member_token=leader.membership,
        token_id=token.token_id,
        row_id=token.row_id,
        node_id=node_id,
        step_index=1,
        ingest_sequence=factory.data_flow.resolve_row_ingest_sequence(token.row_id),
        row_payload_json=factory.scheduler.serialize_row_payload(token.row_data),
        lineage_path=token.lineage_path,
        lease_owner=leader.worker_id,
        lease_seconds=300,
    )


def _persist_blocked_scheduler_work(
    factory: RecorderFactory,
    processor: RowProcessor,
    token: TokenInfo,
    *,
    node_id: NodeID,
    barrier_key: str,
    ingest_sequence: int = 0,
    adopted: bool = True,
    coalesce_name: str | None = None,
    collector_name: str | None = None,
) -> str:
    """Persist BLOCKED scheduler work matching a fabricated buffered token; return its work_item_id.

    Uses the production journal verbs (enqueue+claim, then mark_blocked) so
    the BLOCKED row carries ``barrier_blocked_at`` exactly as a live barrier
    hold would (F1: the journal is the only barrier-buffer truth), stamped
    from Landscape database time (ADR-047); a test that needs an older hold
    ages it with ``age_barrier_hold``.

    ``adopted=True`` (the default) stamps ``barrier_adopted_epoch`` — the
    post-adoption journal image (ADR-030 §E.2). Tests that fabricate executor
    memory directly need the adopted image so the journal-first intake does
    not try to re-adopt (and re-feed) the row; pass ``adopted=False`` to
    fabricate an intake-pending deposit instead.
    """
    _persist_token_for_scheduler(factory, token, ingest_sequence=ingest_sequence)
    member_token = processor._require_member_token()
    item = processor._scheduler.enqueue_ready_claimed(
        member_token=member_token,
        token_id=token.token_id,
        row_id=token.row_id,
        node_id=str(node_id),
        step_index=processor.resolve_node_step(node_id),
        ingest_sequence=factory.data_flow.resolve_row_ingest_sequence(token.row_id),
        row_payload_json=processor._scheduler.serialize_row_payload(token.row_data),
        lease_owner=member_token.worker_id,
        lease_seconds=60,
        lineage_path=token.lineage_path,
        coalesce_name=coalesce_name,
        collector_name=collector_name,
    )
    processor._scheduler.mark_blocked(
        member_token=member_token,
        work_item_id=item.work_item_id,
        row_payload_json=item.row_payload_json,
        queue_key=None,
        barrier_key=barrier_key,
        expected_lease_owner=member_token.worker_id,
    )
    if adopted:
        _stamp_blocked_rows_adopted(processor._scheduler, work_item_id=item.work_item_id)
    return item.work_item_id


def _stamp_blocked_rows_adopted(
    scheduler: Any,
    *,
    run_id: str | None = None,
    work_item_id: str | None = None,
    epoch: int = 1,
) -> None:
    """Stamp ``barrier_adopted_epoch`` on fabricated BLOCKED rows.

    ADR-030 §E.2 (slice 3): tests that fabricate the post-adoption journal
    image directly (BLOCKED rows + batch_members + BUFFERED outcomes, or
    coalesce holds + node_states) must carry the adoption marker — the
    journal restore restores ONLY adopted rows (intake-pending rows have no
    audit writes to restore and are left for the journal-first intake).
    """
    from sqlalchemy import update as _sa_update

    from elspeth.core.landscape.schema import token_work_items_table as _twi

    stmt = _sa_update(_twi).values(barrier_adopted_epoch=epoch)
    if work_item_id is not None:
        stmt = stmt.where(_twi.c.work_item_id == work_item_id)
    if run_id is not None:
        stmt = stmt.where(_twi.c.run_id == run_id)
    stmt = stmt.where(_twi.c.status == "blocked")
    with scheduler._engine.begin() as conn:
        conn.execute(stmt)


def _assert_outcome_pair(
    result: RowResult,
    outcome: TerminalOutcome | None,
    path: TerminalPath,
) -> None:
    assert result.outcome == outcome
    assert result.path == path


def _make_factory(
    *,
    run_id: str = "test-run",
    source_node_id: str = "source-0",
) -> tuple[LandscapeDB, RecorderFactory]:
    """Create an in-memory LandscapeDB with run and source node registered.

    This satisfies FK constraints: rows table references nodes(node_id, run_id).
    """
    setup = make_recorder_with_run(
        run_id=run_id,
        source_node_id=source_node_id,
        source_plugin_name="test-source",
        leader_worker_id=_TEST_LEADER_WORKER_ID,
    )
    return setup.db, setup.factory


def _register_test_worker(
    factory: RecorderFactory,
    worker_id: str,
    *,
    run_id: str = "test-run",
    heartbeat_expires_at: datetime | None = None,
) -> None:
    """Register a scheduler lease owner used by direct test claims."""
    from sqlalchemy import insert, select

    from elspeth.core.landscape.schema import run_workers_table

    with factory._db.engine.begin() as conn:
        exists = conn.execute(
            select(run_workers_table.c.worker_id)
            .where(run_workers_table.c.worker_id == worker_id)
            .where(run_workers_table.c.run_id == run_id)
        ).first()
        if exists is not None:
            return
        registered_at = datetime.now(UTC)
        conn.execute(
            insert(run_workers_table).values(
                worker_id=worker_id,
                run_id=run_id,
                role="follower",
                status="active",
                registered_at=registered_at,
                heartbeat_expires_at=heartbeat_expires_at or registered_at + timedelta(hours=1),
            )
        )


def _synthesize_group_bindings_from_legacy_maps(
    *,
    branch_to_coalesce: dict[BranchName, CoalesceName] | None,
    coalesce_node_ids: dict[CoalesceName, NodeID] | None,
    branch_to_row_union: dict[BranchName, RowUnionName] | None,
    row_union_node_ids: dict[RowUnionName, NodeID] | None,
) -> GroupBindingRegistry:
    """Build a real GroupBindingRegistry from the legacy per-branch maps this
    test module's callers already pass, so `_settle_member_losses` (which
    reads ONLY `self._group_bindings`, never the legacy maps) resolves the
    same bindings the hundreds of pre-WS3 tests here already set up via
    `branch_to_coalesce`/`branch_to_row_union` — without editing each of
    them individually. Only `_make_processor` synthesizes; RowProcessor
    itself never falls back to the legacy maps (see its `group_bindings`
    docstring). `member_roster` must be REAL (not `()`) — GroupBindingRegistry
    keys `binding_for`'s FORK resolution off it.
    """
    from elspeth.core.dag.group_bindings import CloserKind, GroupBinding

    bindings: list[GroupBinding] = []
    coalesce_members: dict[CoalesceName, list[str]] = {}
    for branch, coalesce_name in (branch_to_coalesce or {}).items():
        coalesce_members.setdefault(coalesce_name, []).append(str(branch))
    for coalesce_name, members in coalesce_members.items():
        # .get with a synthetic fallback, not direct indexing: callers that
        # set branch_to_coalesce without coalesce_node_ids already exercise
        # a path that never reads _coalesce_node_ids (e.g. a token without a
        # branch, or one whose branch resolves to no bound frame), so the
        # synthesized registry must not require an entry the real production
        # wiring wouldn't either.
        closer_node_id = (coalesce_node_ids or {}).get(coalesce_name, NodeID(f"__synth_closer__coalesce__{coalesce_name}"))
        bindings.append(
            GroupBinding(
                kind=FrameKind.FORK,
                opener_node_id=NodeID(f"__synth_opener__coalesce__{coalesce_name}"),
                opener_name=f"__synth_opener__coalesce__{coalesce_name}",
                closer_node_id=closer_node_id,
                closer_name=str(coalesce_name),
                closer_kind=CloserKind.COALESCE,
                policy="require_all",
                member_roster=tuple(members),
            )
        )
    row_union_members: dict[RowUnionName, list[str]] = {}
    for branch, row_union_name in (branch_to_row_union or {}).items():
        row_union_members.setdefault(row_union_name, []).append(str(branch))
    for row_union_name, members in row_union_members.items():
        # .get with a synthetic fallback (see the coalesce loop above): the
        # old _row_union_group_released already tolerated a missing
        # row_union_node_ids entry (defensive `if row_union_name not in
        # self._row_union_node_ids: return False`), so several pre-WS3 tests
        # here set branch_to_row_union without row_union_node_ids.
        closer_node_id = (row_union_node_ids or {}).get(row_union_name, NodeID(f"__synth_closer__row_union__{row_union_name}"))
        bindings.append(
            GroupBinding(
                kind=FrameKind.FORK,
                opener_node_id=NodeID(f"__synth_opener__row_union__{row_union_name}"),
                opener_name=f"__synth_opener__row_union__{row_union_name}",
                closer_node_id=closer_node_id,
                closer_name=str(row_union_name),
                closer_kind=CloserKind.ROW_UNION,
                policy="require_all",
                member_roster=tuple(members),
            )
        )
    return GroupBindingRegistry(bindings=tuple(bindings))


def _record_error_sink_outcome(factory: RecorderFactory, *, token_id: str, run_id: str) -> None:
    """Record the (FAILURE, ON_ERROR_ROUTED) outcome the error-sink write would record.

    These harnesses route a row to an error sink they never run. The sink
    write is what decides the token; a run is never stamped successful while
    a token lacks a recorded outcome (QR-4), so the harness records it as that
    write would before stamping the run for its immutable export.
    """
    factory.data_flow.record_token_outcome_leader(
        TokenRef(token_id=token_id, run_id=run_id),
        TerminalOutcome.FAILURE,
        TerminalPath.ON_ERROR_ROUTED,
        coordination_token=leader_coordination_token(factory, run_id),
        sink_name="error-sink",
        error_hash=compute_error_hash("routed to the error sink"),
    )


def _make_processor(
    factory: RecorderFactory,
    *,
    run_id: str = "test-run",
    source_node_id: str = "source-0",
    source_on_success: str = "default",
    source_plugin: Any | None = None,
    edge_map: dict[tuple[NodeID, str], str] | None = None,
    route_resolution_map: dict[tuple[NodeID, str], RouteDestination] | None = None,
    config_gates: list[GateSettings] | None = None,
    config_gate_id_map: dict[GateName, NodeID] | None = None,
    aggregation_settings: dict[NodeID, AggregationSettings] | None = None,
    retry_manager: RetryManager | None = None,
    coalesce_executor: Any = None,
    coalesce_node_ids: dict[CoalesceName, NodeID] | None = None,
    branch_to_coalesce: dict[BranchName, CoalesceName] | None = None,
    row_union_executor: Any = None,
    row_union_node_ids: dict[RowUnionName, NodeID] | None = None,
    branch_to_row_union: dict[BranchName, RowUnionName] | None = None,
    collector_executor: Any = None,
    collector_node_ids: dict[CollectorName, NodeID] | None = None,
    collector_on_success_map: dict[CollectorName, str] | None = None,
    group_bindings: GroupBindingRegistry | None = None,
    branch_to_sink: dict[BranchName, str] | None = None,
    node_step_map: dict[NodeID, int] | None = None,
    coalesce_on_success_map: dict[CoalesceName, str] | None = None,
    node_to_next: dict[NodeID, NodeID | None] | None = None,
    first_transform_node_id: NodeID | None = None,
    node_to_plugin: dict[NodeID, Any] | None = None,
    barrier_restore: BarrierJournalRestoreContext | None = None,
    telemetry_manager: Any = None,
    sink_names: frozenset[str] | None = None,
    scheduler: Any = _DEFAULT_SCHEDULER,
    scheduler_lease_owner: str | None = None,
    clock: Any = None,
    stamp_blocked_rows_adopted: bool = True,
    structural_node_ids: frozenset[NodeID] | None = None,
    mode: ProcessorMode = ProcessorMode.LEADER,
) -> RowProcessor:
    """Create a RowProcessor with sensible defaults.

    ``mode=ProcessorMode.FOLLOWER`` builds the follower half of a real
    multi-worker composition over the same DB: membership authority for the
    REQUIRED registered ``scheduler_lease_owner`` and no leader token.
    RowProcessor validates both authority and identity at construction.
    """
    if scheduler_lease_owner is not None:
        _register_test_worker(factory, scheduler_lease_owner, run_id=run_id)
    member_token = None
    if mode is ProcessorMode.FOLLOWER:
        if scheduler_lease_owner is None:
            raise ValueError("_make_processor(mode=FOLLOWER) requires a registered scheduler_lease_owner")
        member_token = member_token_for(factory._db.engine, run_id=run_id, worker_id=scheduler_lease_owner)

    coalesce_nodes = dict(coalesce_node_ids or {})
    traversal_steps = dict(node_step_map or {})
    source_node = NodeID(source_node_id)
    traversal_steps.setdefault(source_node, 0)
    for idx, coalesce_node in enumerate(coalesce_nodes.values(), start=1):
        traversal_steps.setdefault(coalesce_node, idx)

    traversal_node_to_plugin = (
        dict(node_to_plugin)
        if node_to_plugin is not None
        else {
            config_gate_id_map[GateName(gate.name)]: gate
            for gate in (config_gates or [])
            if config_gate_id_map and GateName(gate.name) in config_gate_id_map
        }
    )
    traversal_next = dict(node_to_next or {})
    traversal_next.setdefault(source_node, None)
    for coalesce_node in coalesce_nodes.values():
        traversal_next.setdefault(coalesce_node, None)

    node_ids_to_register: set[NodeID] = set(traversal_steps)
    node_ids_to_register.update(traversal_node_to_plugin)
    node_ids_to_register.update(traversal_next)
    node_ids_to_register.update(node_id for node_id in traversal_next.values() if node_id is not None)
    node_ids_to_register.update(coalesce_nodes.values())
    node_ids_to_register.update((row_union_node_ids or {}).values())
    node_ids_to_register.update((collector_node_ids or {}).values())
    node_ids_to_register.update((aggregation_settings or {}).keys())

    for node_id in sorted(node_ids_to_register, key=str):
        if node_id == source_node:
            continue
        if factory.data_flow.get_node(str(node_id), run_id) is not None:
            continue
        plugin = traversal_node_to_plugin.get(node_id)
        if node_id in coalesce_nodes.values():
            node_type = NodeType.COALESCE
            plugin_name = "coalesce"
        elif node_id in (row_union_node_ids or {}).values():
            node_type = NodeType.ROW_UNION
            plugin_name = "row_union"
        elif node_id in (collector_node_ids or {}).values():
            node_type = NodeType.COLLECTOR
            plugin_name = "collector"
        elif node_id in (aggregation_settings or {}):
            node_type = NodeType.AGGREGATION
            plugin_name = "aggregation"
        elif isinstance(plugin, GateSettings):
            node_type = NodeType.GATE
            plugin_name = "gate"
        elif isinstance(plugin, TransformProtocol):
            node_type = NodeType.TRANSFORM
            plugin_name = plugin.name
        else:
            # Bare traversal nodes (no plugin) and non-conforming
            # transform-shaped fakes register under the generic name. This is
            # harness-side registration bookkeeping only — the engine's own
            # dispatch is nominal on GateSettings (elspeth-8783933d99) and
            # never re-measures protocol conformance.
            node_type = NodeType.TRANSFORM
            plugin_name = "transform"
        factory.data_flow.register_node(
            coordination_token=leader_coordination_token(factory, run_id),
            plugin_name=str(plugin_name),
            node_type=node_type,
            plugin_version="1.0",
            config={},
            node_id=str(node_id),
            schema_config=_DYNAMIC_SCHEMA,
        )

    # Sources are structural in production (graph_wiring's type-based
    # allowlist); coalesce nodes are auto-unioned by the context itself.
    traversal_structural = structural_node_ids if structural_node_ids is not None else frozenset({NodeID(source_node_id)})
    traversal = DAGTraversalContext(
        node_step_map=traversal_steps,
        node_to_plugin=traversal_node_to_plugin,
        node_to_next=traversal_next,
        coalesce_node_map=coalesce_nodes,
        row_union_node_map=dict(row_union_node_ids or {}),
        collector_node_map=dict(collector_node_ids or {}),
        structural_node_ids=traversal_structural,
    )

    if barrier_restore is not None and stamp_blocked_rows_adopted:
        # ADR-030 §E.2: restore-shaped tests fabricate the post-adoption
        # journal image (BLOCKED rows + the audit writes adoption owns), so
        # stamp the adoption marker — the journal restore restores ONLY
        # adopted rows. Tests exercising intake-pending restores (rows the
        # dead leader deposited but never adopted) pass
        # ``stamp_blocked_rows_adopted=False`` and stamp selectively.
        _stamp_blocked_rows_adopted(factory.scheduler, run_id=run_id)

    return RowProcessor(
        execution=factory.execution,
        data_flow=factory.data_flow,
        span_factory=SpanFactory(),  # No tracer — no-op spans
        run_id=run_id,
        # Slice 3 (ADR-030 §E.2): the journal-first barrier intake's adoption
        # verbs are leader-fenced; bind the run's own epoch-1 seat token.
        # A follower never carries one.
        coordination_token=leader_coordination_token(factory, run_id) if mode is ProcessorMode.LEADER else None,
        member_token=member_token,
        mode=mode,
        source_node_id=NodeID(source_node_id),
        source_on_success=source_on_success,
        source_plugin=source_plugin,
        traversal=traversal,
        edge_map=edge_map,
        route_resolution_map=route_resolution_map,
        aggregation_settings=aggregation_settings,
        retry_manager=retry_manager,
        coalesce_executor=coalesce_executor,
        branch_to_coalesce=branch_to_coalesce,
        row_union_executor=row_union_executor,
        branch_to_row_union=branch_to_row_union,
        collector_executor=collector_executor,
        collector_on_success_map=collector_on_success_map,
        group_bindings=(
            group_bindings
            if group_bindings is not None
            else _synthesize_group_bindings_from_legacy_maps(
                branch_to_coalesce=branch_to_coalesce,
                coalesce_node_ids=coalesce_node_ids,
                branch_to_row_union=branch_to_row_union,
                row_union_node_ids=row_union_node_ids,
            )
        ),
        branch_to_sink={BranchName(k): SinkName(v) for k, v in (branch_to_sink or {}).items()},
        coalesce_on_success_map=coalesce_on_success_map,
        barrier_restore=barrier_restore,
        barrier_restore_reads=factory.barrier_restore,
        payload_store=factory.payload_store,
        telemetry_manager=telemetry_manager,
        sink_names=sink_names,
        scheduler=factory.scheduler if scheduler is _DEFAULT_SCHEDULER else scheduler,
        scheduler_lease_owner=scheduler_lease_owner,
        clock=clock,
    )


def _make_claimed_work_item(
    *,
    token_id: str = "tok-claimed",
    lineage_path: tuple[LineageFrame, ...] = (),
    run_id: str = "test-run",
    row_id: str = "row-1",
    node_id: str | None = "transform-1",
    lease_owner: str = "worker-1",
) -> Any:
    """A minimal LEASED TokenWorkItem stand-in for the group-loss claim guard
    (spec §6.2): callers only need ``token_id``/``lineage_path`` to shape, so
    every other field carries a harmless placeholder."""
    from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus

    now = datetime.now(UTC)
    return TokenWorkItem(
        work_item_id=f"wi-{token_id}",
        run_id=run_id,
        token_id=token_id,
        row_id=row_id,
        node_id=node_id,
        step_index=1,
        ingest_sequence=0,
        row_payload_json="{}",
        status=TokenWorkStatus.LEASED,
        attempt=1,
        available_at=now,
        created_at=now,
        updated_at=now,
        lineage_path=lineage_path,
        lease_owner=lease_owner,
        lease_expires_at=now + timedelta(seconds=60),
    )


def _make_mock_transform(
    *,
    node_id: str = "transform-1",
    name: str = "test-transform",
    on_error: str | None = "discard",
    on_success: str | None = None,
    is_batch_aware: bool = False,
    creates_tokens: bool = False,
    result: TransformResult | None = None,
) -> Mock:
    """Create a mock transform satisfying TransformProtocol, or BatchTransformProtocol when batch-aware."""
    transform = Mock(spec=BatchTransformProtocol if is_batch_aware else TransformProtocol)
    transform.node_id = node_id
    transform.name = name
    transform.on_error = on_error
    transform.on_success = on_success
    transform.is_batch_aware = is_batch_aware
    transform.creates_tokens = creates_tokens
    transform.declared_output_fields = frozenset()
    transform.declared_input_fields = frozenset()
    # ADR-009 §Clause 2: cross-check is gated on this flag. Default False
    # matches BaseTransform's default; tests opting into pass-through coverage
    # set it explicitly after construction.
    transform.passes_through_input = False
    transform.can_drop_rows = False
    # The aggregation executor validates batch rows against
    # transform.input_schema before flush; the base PluginSchema accepts any
    # row, so validation is a no-op (same pattern as test_executors.py).
    transform.input_schema = _PermissiveSchema
    transform.output_schema = _PermissiveSchema
    transform._output_schema_config = None
    transform.effective_static_contract.return_value = frozenset()
    # The field-name spelling surfaces (Transform/BatchTransformProtocol): this
    # mock declares no field names, forwards nothing it removes.
    transform.declared_read_fields = frozenset()
    transform.declared_created_fields = frozenset()
    transform.forwards_input_fields = False
    transform.removed_input_fields = frozenset()
    if is_batch_aware:
        # No declared required column: the flush preflight's presence check passes every row.
        transform.schema_required_input_fields.return_value = frozenset()
    if result is not None:
        transform.process.return_value = result
    return transform


class TestTransformModeOutcomeOrdering:
    """Regression tests for P0: parent outcomes must NOT be recorded before downstream validation.

    Bug: _route_transform_results recorded CONSUMED_IN_BATCH/QUARANTINED for parent
    tokens BEFORE validating expected_output_count and before expand_token(). If either
    of those failed, the audit trail showed terminal outcomes for parents with no child
    token — recovery would skip the row (silent data loss).

    Fix: outcome recording must happen AFTER both cardinality validation and expand_token
    succeed. On failure, parent tokens must remain BUFFERED (non-terminal) so recovery
    can retry them.
    """

    def _setup_batch_processor(
        self,
        *,
        expected_output_count: int | None = None,
    ) -> tuple[LandscapeDB, RecorderFactory, RowProcessor, Mock, NodeID]:
        """Create a transform-mode batch processor with optional expected_output_count."""
        db, factory = _make_factory()
        source_node = NodeID("source-0")
        agg_node = NodeID("agg-1")

        transform = _make_mock_transform(
            node_id=str(agg_node),
            name="agg-transform",
            is_batch_aware=True,
            on_success="agg_sink",
        )

        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, agg_node: 1},
            node_to_next={source_node: agg_node, agg_node: None},
            node_to_plugin={agg_node: transform},
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="batch_agg",
                    plugin="agg-transform",
                    input="default",
                    on_error="discard",
                    trigger={"count": 1},
                    output_mode="transform",
                    expected_output_count=expected_output_count,
                ),
            },
        )
        return db, factory, processor, transform, agg_node

    def test_cardinality_mismatch_does_not_record_parent_terminal_outcome(self) -> None:
        """Parent tokens must NOT be CONSUMED_IN_BATCH when expected_output_count fails.

        If the cardinality check raises after outcomes are recorded, recovery
        sees terminal parents with no children — silent data loss.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor(
            expected_output_count=5,  # Will mismatch: transform returns 1 row
        )
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}

        # Transform returns 1 output row but expected_output_count=5 → RuntimeError
        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={"action": "batch_processed"},
        )

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return flush_result, [captured["token"]], "batch-1"

        # The fake batch is empty until intake accepts its member, and drained
        # after its one flush. Readiness is also checked before intake now.
        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome") as record_outcome,
            patch.object(processor, "_emit_transform_completed"),
            patch.object(processor, "_emit_token_completed"),
            pytest.raises(RuntimeError, match="expected_output_count=5"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        # Parent tokens must NOT have been recorded as terminal before the error.
        # Only BUFFERED is acceptable (non-terminal, allows recovery retry).
        recorded_pairs = [(call.kwargs["outcome"], call.kwargs["path"]) for call in record_outcome.call_args_list]
        assert (TerminalOutcome.TRANSIENT, TerminalPath.BATCH_CONSUMED) not in recorded_pairs, (
            f"CONSUMED_IN_BATCH was recorded before cardinality check failed — "
            f"recovery would skip this row. Recorded pairs: {recorded_pairs}"
        )

    def test_expand_token_failure_does_not_record_parent_terminal_outcome(self) -> None:
        """Parent tokens must NOT be CONSUMED_IN_BATCH when expand_token raises.

        If expand_token fails after outcomes are recorded, the audit trail shows
        terminal parents with no expanded children — silent data loss on recovery.
        """
        _db, factory, processor, transform, _agg_node = self._setup_batch_processor()
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        captured: dict[str, TokenInfo] = {}

        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={"action": "batch_processed"},
        )

        def accept_side_effect(node_id: NodeID, token: TokenInfo, *, accept_time: float | None = None) -> None:
            captured["token"] = token

        def execute_flush_side_effect(*, node_id, transform, ctx, trigger_type, **kwargs):
            return flush_result, [captured["token"]], "batch-1"

        with (
            patch.object(processor._aggregation_executor, "accept_adopted_row", side_effect=accept_side_effect),
            patch.object(processor._aggregation_executor, "execute_flush", side_effect=execute_flush_side_effect) as execute_flush,
            patch.object(
                processor._aggregation_executor,
                "check_flush_status",
                side_effect=lambda _node_id: (True, TriggerType.COUNT) if captured and not execute_flush.called else (False, None),
            ),
            patch.object(factory.data_flow, "record_token_outcome") as record_outcome,
            patch.object(processor, "_emit_transform_completed"),
            patch.object(processor, "_emit_token_completed"),
            patch.object(
                processor._token_manager,
                "expand_token",
                side_effect=RuntimeError("expand_token DB integrity error"),
            ),
            pytest.raises(RuntimeError, match="expand_token DB integrity error"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        # Parent tokens must NOT have terminal outcomes recorded before expand_token.
        recorded_pairs = [(call.kwargs["outcome"], call.kwargs["path"]) for call in record_outcome.call_args_list]
        assert (TerminalOutcome.TRANSIENT, TerminalPath.BATCH_CONSUMED) not in recorded_pairs, (
            f"CONSUMED_IN_BATCH was recorded before expand_token failed — recovery would skip this row. Recorded pairs: {recorded_pairs}"
        )

    def test_parent_terminal_outcome_recorder_failure_raises_audit_integrity_error(self) -> None:
        """Recorder failure after expansion must surface as audit corruption, not a raw DB error."""
        _db, _factory, processor, transform, agg_node = self._setup_batch_processor()
        first_token = make_token_info(row_id="row-a", token_id="token-a", data={"value": 10})
        second_token = make_token_info(row_id="row-b", token_id="token-b", data={"value": 20})
        fctx = _FlushContext(
            node_id=agg_node,
            transform=transform,
            settings=AggregationSettings(
                name="batch_agg",
                plugin="agg-transform",
                input="default",
                on_error="discard",
                trigger={"count": 2},
                output_mode="transform",
            ),
            buffered_tokens=(first_token, second_token),
            batch_id="batch-1",
            expand_parent_token=first_token,
            triggering_token=second_token,
            coalesce_node_id=None,
            coalesce_name=None,
        )
        flush_result = TransformResult.success(
            make_row({"value": 999}, contract=_make_contract()),
            success_reason={
                "action": "batch_processed",
                "metadata": {"quarantined_indices": [1]},
            },
        )

        with (
            patch.object(
                processor._token_manager,
                "expand_token",
                side_effect=LandscapeRecordError("audit DB down"),
            ),
            patch.object(processor, "_emit_token_completed"),
            pytest.raises(AuditIntegrityError, match="Failed to atomically record aggregation expansion") as exc_info,
        ):
            processor._route_transform_results(
                fctx,
                flush_result,
                prepared=processor._prepare_transform_route(fctx, flush_result, quarantined_indices=frozenset({1})),
            )

        assert isinstance(exc_info.value.__cause__, LandscapeRecordError)


# =============================================================================
# process_row: Multi-row output (deaggregation)
# =============================================================================


class TestProcessRowMultiRowOutput:
    """Tests for deaggregation (1→N) in regular transforms."""

    def test_multi_row_with_creates_tokens_returns_expanded(self) -> None:
        """Transform with creates_tokens=True returning multi-row → EXPANDED."""
        _db, factory = _make_factory()
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        contract = _make_contract()
        output_rows = [
            make_row({"value": 1}, contract=contract),
            make_row({"value": 2}, contract=contract),
        ]
        multi_result = TransformResult.success_multi(
            output_rows,
            success_reason={"action": "expand"},
        )

        transform = _make_mock_transform(creates_tokens=True)
        source_node = NodeID("source-0")
        transform_node = NodeID(transform.node_id)
        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, transform_node: 1},
            node_to_next={source_node: transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (multi_result, token, None)

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=executor_side_effect,
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        # Parent should be EXPANDED, children should be COMPLETED
        pairs = {(r.outcome, r.path) for r in results}
        assert (TerminalOutcome.TRANSIENT, TerminalPath.EXPAND_PARENT) in pairs
        assert (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW) in pairs

    def test_multi_row_without_creates_tokens_raises(self) -> None:
        """Transform returning multi-row without creates_tokens=True → RuntimeError."""
        _db, factory = _make_factory()
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        contract = _make_contract()
        output_rows = [
            make_row({"value": 1}, contract=contract),
            make_row({"value": 2}, contract=contract),
        ]
        multi_result = TransformResult.success_multi(
            output_rows,
            success_reason={"action": "expand"},
        )

        transform = _make_mock_transform(creates_tokens=False)
        source_node = NodeID("source-0")
        transform_node = NodeID(transform.node_id)
        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, transform_node: 1},
            node_to_next={source_node: transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (multi_result, token, None)

        with (
            patch.object(
                processor._transform_executor,
                "execute_transform",
                side_effect=executor_side_effect,
            ),
            pytest.raises(RuntimeError, match="creates_tokens=False"),
        ):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

    def test_multi_row_with_inconsistent_contracts_raises(self) -> None:
        """Multi-row result with mixed contracts must crash at construction (plugin bug).

        Validation lives in TransformResult.success_multi() — it fires before
        the result can reach any consumer in the processor.
        """
        from elspeth.contracts.errors import PluginContractViolation

        contract_a = make_contract(fields={"value": int}, mode="OBSERVED")
        contract_b = make_contract(fields={"other": str}, mode="OBSERVED")
        output_rows = [
            make_row({"value": 1}, contract=contract_a),
            make_row({"other": "x"}, contract=contract_b),
        ]
        with pytest.raises(PluginContractViolation, match="inconsistent contracts"):
            TransformResult.success_multi(
                output_rows,
                success_reason={"action": "expand"},
            )

    def test_row_union_binding_survives_expansion_on_a_branch(self) -> None:
        """token_traversal.py:254-273 — the row_union binding must survive expansion.

        Direct processor-level witness of the fail-closed seam
        elspeth-a5b86149d4 fixed, modeled on
        TestGateSinkRoutingNotifiesCoalesce (hand-built TokenInfo/work-item
        context driving the real code path, no DAG builder involved).

        Ruling 28 (spec §7 rule 5) now rejects this exact TOPOLOGY at BUILD
        time when it reaches ``build_execution_graph`` — an undeclared
        multi-row transform inside a row_union-bound region — which is why
        ``test_row_union_branch_cardinality.py::
        test_expanding_transform_in_a_branch_does_not_bypass_the_barrier``
        was reclassified as a build-rejection test (Task 8). But the RUNTIME
        seam this test pins is a different, still-live fact: IF a fork-branch
        token reaches an expanding transform with row_union context threaded
        through (the only way that could happen post-ruling-28 is a topology
        the build-time check does not yet see, or a pre-ruling-28 persisted/
        resumed run), the binding must not be dropped. Dropping it is
        precisely how the ORIGINAL bug let expanded children walk straight
        through the row_union node instead of being adjudicated by it.

        This test asserts ONLY the plumbing (each expanded child's WorkItem
        carries the union binding forward) — it does NOT re-verify that
        RowUnionExecutor.accept() then correctly rejects a surplus arrival
        under that binding; that adjudication lives one layer further out
        (barrier_coordination.py, scheduler drain) and is already covered by
        RowUnionExecutor's own suite (test_row_union_executor.py) with no
        dependency on expansion. Driving THAT layer here would require
        reconstructing scheduler-drain machinery this unit test does not
        otherwise touch — out of scope for a processor-level plumbing
        witness; flagging rather than forcing a mock of it, per the
        controller's own instruction.

        WS3 Task 5 supersession note: this pins ``token_traversal.py:260-282``
        (the SUCCESSFUL, non-empty expansion branch — child WorkItems get
        ``row_union_name``/``coalesce_name`` threaded via
        ``_work_items.create_continuation``). It is companion to, not
        rewritten by, the settle-member seam: `_settle_member_losses` only
        fires on the ADJACENT empty-expansion branch a few lines earlier
        (``token_traversal.py:224``, the `TransformResult.success_empty()`
        path — a plain filter/deaggregation-to-nothing, not a lost fork
        branch). The mechanism this test pins — binding survives a
        SUCCESSFUL expansion so the barrier can later adjudicate it — is
        unchanged by Task 5's fold of the four branch-loss notifiers;
        mutation-verified green after that fold with no edits needed here
        beyond this note.
        """
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        contract = _make_contract()
        output_rows = [
            make_row({"value": 1}, contract=contract),
            make_row({"value": 2}, contract=contract),
        ]
        multi_result = TransformResult.success_multi(
            output_rows,
            success_reason={"action": "expand"},
        )

        transform = _make_mock_transform(node_id="explode-1", name="explode_treatment", creates_tokens=True, on_success="treatment_scored")
        source_node = NodeID("source-0")
        transform_node = NodeID(transform.node_id)
        union_node = NodeID("row_union::variant_union")

        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, transform_node: 1},
            node_to_next={source_node: transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            row_union_node_ids={RowUnionName("variant_union"): union_node},
            branch_to_row_union={BranchName("treatment_branch"): RowUnionName("variant_union")},
        )

        # A fork-branch token — the innermost FORK frame's member_key IS the
        # declared branch name (spec §4.1), which is what token.branch_name
        # derives from (ruling 21: derived accessors are the only read path).
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-treatment",
            row_data=make_row({"value": 10}, contract=contract),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-treatment_branch", member_key="treatment_branch"),),
        )
        # expand_token() re-checks Tier-1 token ownership against the audit
        # DB (data_flow/ownership.py) before minting children — a fabricated
        # token needs the same durable row+token rows a real fork would have
        # written, same helper the coalesce-side fixtures in this file use.
        _persist_token_for_scheduler(factory, token, ingest_sequence=0)

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (multi_result, token, None)

        with patch.object(processor._transform_executor, "execute_transform", side_effect=executor_side_effect):
            result, child_items = processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=transform_node,
                row_union_node_id=union_node,
                row_union_name=RowUnionName("variant_union"),
                attempt_offset=0,
            )

        # Parent is EXPANDED (transient — the real outcome rides expand_token()).
        assert isinstance(result, RowResult)
        assert (result.outcome, result.path) == (TerminalOutcome.TRANSIENT, TerminalPath.EXPAND_PARENT)

        # Both expanded children carry the union binding forward — the fix's
        # exact claim (token_traversal.py:268-272): "the barrier binding must
        # survive expansion... carrying it makes an unsatisfiable expansion
        # fail closed at the barrier." Losing it here is exactly the bug
        # elspeth-a5b86149d4 fixed (children walking through the union
        # structurally, unbound, instead of arriving at it).
        assert len(child_items) == 2
        for item in child_items:
            assert item.row_union_name == RowUnionName("variant_union")
            assert item.row_union_node_id == union_node
            assert item.token.branch_name == "treatment_branch"


# =============================================================================
# Mid-pipeline drain entry (a continuation cursor handed to the work queue)
# =============================================================================


class TestDrainContinuationEntry:
    """A continuation cursor drained from a mid-pipeline node resolves its terminal sink."""

    def test_drain_from_midpoint(self) -> None:
        """A cursor at a given node drains from that node."""
        _db, factory = _make_factory()

        processor = _make_processor(factory)
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # Create a token to process
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(factory, token)

        results = processor._drain_work_queue(processor._work_items.create(token=token, current_node_id=NodeID("source-0")), ctx)

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "default"

    def test_terminal_coalesce_continuation_uses_coalesce_on_success_sink(self) -> None:
        """A merged token continued at a terminal coalesce routes to the coalesce sink, not the source sink."""
        _db, factory = _make_factory()

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            coalesce_on_success_map={CoalesceName("merge"): "coalesce_sink"},
            node_step_map={NodeID("coalesce::merge"): 1},
            node_to_next={NodeID("coalesce::merge"): None},
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(factory, token)

        results = processor._drain_work_queue(
            processor._work_items.create(
                token=token,
                current_node_id=NodeID("coalesce::merge"),
                coalesce_node_id=NodeID("coalesce::merge"),
                coalesce_name=CoalesceName("merge"),
            ),
            ctx,
        )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "coalesce_sink"

    def test_non_terminal_coalesce_continuation_uses_downstream_sink(self) -> None:
        """Coalesce sink must not override downstream terminal transform routing."""
        _db, factory = _make_factory()
        transform = _make_mock_transform(node_id="transform-1")
        transform.on_success = "downstream_sink"

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            coalesce_on_success_map={CoalesceName("merge"): "coalesce_sink"},
            node_step_map={NodeID("coalesce::merge"): 1, NodeID("transform-1"): 2},
            node_to_next={NodeID("coalesce::merge"): NodeID("transform-1"), NodeID("transform-1"): None},
            node_to_plugin={NodeID("transform-1"): transform},
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(factory, token)

        with patch.object(
            processor,
            "_execute_transform_with_retry",
            return_value=(
                TransformResult.success(make_row({"value": 42}), success_reason={"action": "noop"}),
                token,
                None,
            ),
        ):
            results = processor._drain_work_queue(
                processor._work_items.create(
                    token=token,
                    current_node_id=NodeID("coalesce::merge"),
                    coalesce_node_id=NodeID("coalesce::merge"),
                    coalesce_name=CoalesceName("merge"),
                ),
                ctx,
            )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "downstream_sink"


# =============================================================================
# _drain_work_queue: Iteration Guard
# =============================================================================


class TestDrainWorkQueueIterationGuard:
    """Tests for the MAX_WORK_QUEUE_ITERATIONS safety limit."""

    def test_iteration_guard_prevents_infinite_loop(self) -> None:
        """Work queue exceeding MAX_WORK_QUEUE_ITERATIONS raises RuntimeError."""
        _db, factory = _make_factory()

        processor = _make_processor(factory)
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = make_token_info(data={"value": 1})
        _persist_token_for_scheduler(factory, token)
        produced = 0

        # Mock _process_single_token to always produce more work
        def infinite_loop_producer(token, ctx, current_node_id, **kwargs):
            nonlocal produced
            produced += 1
            new_token = make_token_info(row_id=token.row_id, token_id=f"loop-token-{produced}", data={"value": 1})
            _persist_token_for_scheduler(factory, new_token)
            return (None, [WorkItem(token=new_token, current_node_id=NodeID("source-0"))])

        with (
            patch("elspeth.engine.scheduler_drain.MAX_WORK_QUEUE_ITERATIONS", 3),
            patch.object(processor, "_process_single_token", side_effect=infinite_loop_producer),
            pytest.raises(RuntimeError, match=r"exceeded.*iterations"),
        ):
            processor._drain_work_queue(
                WorkItem(token=token, current_node_id=NodeID("source-0")),
                ctx=ctx,
            )
        assert produced == 3

    def test_iteration_guard_allows_supported_batch_replicate_fanout(self) -> None:
        """The outer queue guard must not reject the largest supported legal fan-out."""
        _db, factory = _make_factory()
        processor = _make_processor(factory)
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = make_token_info(data={"value": 1})
        _persist_token_for_scheduler(factory, token)
        supported_copies = 3
        remaining_children = supported_copies

        def supported_fanout_producer(token, ctx, current_node_id, **kwargs):
            nonlocal remaining_children
            if remaining_children == 0:
                return (None, [])

            remaining_children -= 1
            child = make_token_info(
                row_id=token.row_id, token_id=f"fanout-token-{remaining_children}", data={"remaining": remaining_children}
            )
            _persist_token_for_scheduler(factory, child)
            return (None, [WorkItem(token=child, current_node_id=NodeID("source-0"))])

        with (
            patch("elspeth.engine.scheduler_drain.MAX_WORK_QUEUE_ITERATIONS", supported_copies + 2),
            patch.object(processor, "_process_single_token", side_effect=supported_fanout_producer) as mock_process_single_token,
        ):
            results = processor._drain_work_queue(
                WorkItem(token=token, current_node_id=NodeID("source-0")),
                ctx=ctx,
            )

        assert results == []
        assert mock_process_single_token.call_count == supported_copies + 1

    def test_max_iterations_constant_is_reasonable(self) -> None:
        """MAX_WORK_QUEUE_ITERATIONS should be at least 1000."""
        assert MAX_WORK_QUEUE_ITERATIONS >= 1000
        max_supported_copies = BatchReplicateConfig.model_json_schema()["properties"]["max_copies"]["maximum"]
        assert max_supported_copies < MAX_WORK_QUEUE_ITERATIONS


class TestDurableSchedulerResumeDrain:
    """Tests for draining scheduler work created before this processor existed."""

    def test_drains_persisted_ready_work_without_source_replay(self) -> None:
        """A fresh processor rehydrates READY scheduler work from durable row payloads."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-ready",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        success_result = TransformResult.success(
            make_row({"value": 42, "resumed": True}),
            success_reason={"action": "resume_drain"},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            assert token.token_id == "token-ready"
            assert token.row_data.to_dict() == {"value": 42}
            return (success_result, token, None)

        with patch.object(processor._transform_executor, "execute_transform", side_effect=executor_side_effect):
            results = processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].token.token_id == "token-ready"
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            statuses = (
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == "test-run")).scalars().all()
            )
        assert statuses == ["pending_sink"]

    def _seed_parked_on_error_pending_sink(self, *, error_hash: str | None) -> tuple[Any, Any, Any]:
        """Seed a durable ON_ERROR_ROUTED PENDING_SINK row via production verbs.

        Returns (db, factory, processor) where processor is a fresh leader
        processor that has not yet drained. The crashed worker remains a
        distinct registered member; strict recovery identity is the leader
        identity carried by the coordination token.
        """
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-parked",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        _register_test_worker(factory, "crashed-worker")
        claimed = factory.scheduler.claim_ready(
            lease_owner="crashed-worker",
            lease_seconds=300,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="crashed-worker"),
        )
        assert claimed is not None
        persisted_error_hash = error_hash if error_hash is not None else fake_error_hash("valid-before-corruption")
        factory.scheduler.mark_pending_sink(
            work_item_id=claimed.work_item_id,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            sink_name="error_sink",
            outcome=TerminalOutcome.FAILURE.value,
            path=TerminalPath.ON_ERROR_ROUTED.value,
            error_hash=persisted_error_hash,
            # EMPTY original message: the class where a recomputed replay
            # hash diverges from the originally-audited one.
            error_message="",
            expected_lease_owner="crashed-worker",
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="crashed-worker"),
        )
        if error_hash is None:
            from sqlalchemy import update

            from elspeth.core.landscape.schema import token_work_items_table

            with db.engine.begin() as conn:
                conn.execute(
                    update(token_work_items_table)
                    .where(token_work_items_table.c.work_item_id == claimed.work_item_id)
                    .values(pending_error_hash=None)
                )
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        return db, factory, processor

    def test_pending_sink_replay_preserves_persisted_error_hash(self) -> None:
        """ON_ERROR_ROUTED replay carries the PERSISTED pending_error_hash
        (elspeth-d74d19f901) instead of letting the accumulator recompute one
        from the synthetic ResumedPendingSink failure evidence."""
        from elspeth.engine.orchestrator.outcomes import accumulate_row_outcomes
        from elspeth.engine.orchestrator.types import ExecutionCounters

        stored_hash = "deadbeefdeadbeef"
        _db, _factory, processor = self._seed_parked_on_error_pending_sink(error_hash=stored_hash)
        ctx = make_context(
            landscape=_factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(_factory, "test-run"),
        )

        results = processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        assert results[0].authoritative_error_hash == stored_hash
        # And the accumulator prefers it end-to-end: the audit PendingOutcome
        # carries the stored hash, not a recompute of 'ResumedPendingSink'/''.
        counters = ExecutionCounters()
        pending: dict[str, list[Any]] = {"error_sink": []}
        accumulate_row_outcomes(results, counters, pending)
        pending_outcome = pending["error_sink"][0][1]
        assert pending_outcome is not None
        assert pending_outcome.error_hash == stored_hash

    def test_pending_sink_replay_fails_closed_on_missing_error_hash(self) -> None:
        """An ON_ERROR_ROUTED pending sink with no persisted error hash is
        audit corruption — replay must refuse rather than fabricate a hash."""
        _db, _factory, processor = self._seed_parked_on_error_pending_sink(error_hash=None)
        ctx = make_context(
            landscape=_factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(_factory, "test-run"),
        )

        with pytest.raises(AuditIntegrityError, match="pending_error_hash"):
            processor.drain_scheduled_work(ctx)

    def test_evicted_worker_disposition_is_refused_mid_drain(self) -> None:
        """ADR-030 §G parity (elspeth-ba7b2cc25d): a registered worker whose
        run_workers row is evicted MID-PROCESSING cannot commit the claimed
        item's disposition — the drain raises RunWorkerEvictedError and the
        item stays LEASED (abandoned for the reaper) instead of landing in
        pending_sink under an evicted owner."""
        from sqlalchemy import select, update

        from elspeth.contracts.errors import RunMembershipLostError
        from elspeth.core.landscape.schema import run_workers_table, token_work_items_table

        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-ready",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        success_result = TransformResult.success(
            make_row({"value": 42, "resumed": True}),
            success_reason={"action": "resume_drain"},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            # Evict the worker AFTER its claim succeeded but BEFORE the
            # drain's disposition write — the ticket's exact window.
            with db.engine.begin() as conn:
                conn.execute(
                    update(run_workers_table)
                    .where(run_workers_table.c.worker_id == _TEST_LEADER_WORKER_ID)
                    .values(status="evicted", evicted_at=datetime.now(UTC))
                )
            return (success_result, token, None)

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=executor_side_effect),
            pytest.raises(RunMembershipLostError),
        ):
            processor.drain_scheduled_work(ctx)

        with db.connection() as conn:
            item_row = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.lease_owner).where(
                    token_work_items_table.c.run_id == "test-run"
                )
            ).one()
        assert item_row.status == "leased", "evicted worker's disposition must not commit"
        assert item_row.lease_owner == _TEST_LEADER_WORKER_ID

    def test_sink_bound_scheduler_work_terminalizes_only_after_sink_callback(self) -> None:
        """Sink-bound work remains durable until sink outcome recording completes."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-ready",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        success_result = TransformResult.success(
            make_row({"value": 42, "resumed": True}),
            success_reason={"action": "resume_drain"},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (success_result, token, None)

        with patch.object(processor._transform_executor, "execute_transform", side_effect=executor_side_effect):
            results = processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        processor.mark_sink_bound_scheduler_terminal("token-ready")

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, row_payload_json = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.row_payload_json).where(
                    token_work_items_table.c.token_id == "token-ready"
                )
            ).one()
        assert status == "terminal"
        assert "resumed" not in row_payload_json
        assert "purged" in row_payload_json

    def test_single_sink_bound_scheduler_terminalization_requires_matching_pending_sink(self) -> None:
        """Single-token callback path must not silently ignore missing scheduler rows."""
        _db, factory = _make_factory()
        processor = _make_processor(factory, scheduler=factory.scheduler)

        with pytest.raises(AuditIntegrityError, match="terminalized 0 rows"):
            processor.mark_sink_bound_scheduler_terminal("missing-token")

    def test_pending_sink_resume_does_not_rerun_transform(self) -> None:
        """A crash after transform success resumes at sink handoff, not at the transform."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-pending",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        first_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        final_token = TokenInfo(
            row_id=row.row_id,
            token_id=token.token_id,
            row_data=make_row({"value": 42, "resumed": True}),
        )
        sink_bound_result = RowResult(
            token=final_token,
            final_data=final_token.row_data,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="default",
        )

        with patch.object(first_processor, "_process_single_token", return_value=(sink_bound_result, [])):
            first_processor.drain_scheduled_work(ctx)

        resumed_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        with patch.object(resumed_processor._transform_executor, "execute_transform", side_effect=AssertionError("transform replayed")):
            results = resumed_processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        assert results[0].token.token_id == "token-pending"
        assert results[0].token.row_data.to_dict() == {"value": 42, "resumed": True}
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, lease_owner = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.lease_owner).where(
                    token_work_items_table.c.token_id == "token-pending"
                )
            ).one()
        assert status == "leased"
        assert lease_owner == _TEST_LEADER_WORKER_ID

    def test_pending_sink_resume_repairs_already_outcomed_row_without_reemitting_sink(self) -> None:
        """A terminal token outcome is the resume witness; do not emit the sink externally again."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-outcomed-pending",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        crashed_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        final_token = TokenInfo(
            row_id=row.row_id,
            token_id=token.token_id,
            row_data=make_row({"value": 42, "resumed": True}),
        )
        sink_bound_result = RowResult(
            token=final_token,
            final_data=final_token.row_data,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="default",
        )
        with patch.object(crashed_processor, "_process_single_token", return_value=(sink_bound_result, [])):
            crashed_processor.drain_scheduled_work(ctx)
        factory.data_flow.record_token_outcome_leader(
            ref=TokenRef(token_id=token.token_id, run_id="test-run"),
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="default",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        resumed_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        with patch.object(resumed_processor._transform_executor, "execute_transform", side_effect=AssertionError("transform replayed")):
            results = resumed_processor.drain_scheduled_work(ctx)

        assert results == []
        from sqlalchemy import select

        from elspeth.core.landscape.schema import scheduler_events_table, token_work_items_table

        with db.connection() as conn:
            status, lease_owner = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.lease_owner).where(
                    token_work_items_table.c.token_id == "token-outcomed-pending"
                )
            ).one()
            terminal_event_owner = conn.execute(
                select(scheduler_events_table.c.caller_owner)
                .where(scheduler_events_table.c.token_id == "token-outcomed-pending")
                .where(scheduler_events_table.c.event_type == "mark_pending_sink_terminal")
            ).scalar_one()
        assert status == "terminal"
        assert lease_owner is None
        assert terminal_event_owner == _TEST_LEADER_WORKER_ID

    def test_resume_drains_all_pending_sink_rows_in_single_call(self) -> None:
        """Multiple pre-existing PENDING_SINK rows must all drain in a single resume call.

        Regression for elspeth-5c5e88b071 (G3): the ``created_pending_sink_this_drain``
        gate previously short-circuited the ``claim_pending_sink`` fallback after
        the first pending-sink emission. When a prior crashed worker left N
        sink-bound rows durable, only one was recovered per ``drain_scheduled_work``
        call, leaving the rest stranded and tripping
        ``OrchestrationInvariantError`` at run completion.
        """
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        pending_token_ids: list[str] = []
        for idx in range(3):
            source_payload = make_row({"value": idx})
            row = factory.data_flow.create_row_with_token(
                source_node_id="source-0",
                row_index=idx,
                source_row_index=idx,
                ingest_sequence=idx,
                data=source_payload.to_dict(),
                coordination_token=leader_coordination_token(factory, "test-run"),
            )[0]
            token_id = f"token-pending-{idx}"
            pending_token_ids.append(token_id)
            token = factory.data_flow.create_token(
                row.row_id,
                token_id=token_id,
                coordination_token=leader_coordination_token(factory, "test-run"),
            )
            factory.scheduler.enqueue_ready(
                token_id=token.token_id,
                row_id=row.row_id,
                node_id=str(transform_node),
                step_index=1,
                ingest_sequence=idx,
                row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
                member_token=leader_coordination_token(factory, "test-run").membership,
            )

        # Stage 1: fabricate the durable image left by a leader that drove the
        # transform to success and then crashed before sink durability. This
        # test isolates pending-sink replay, so both processors use the
        # fixture's same token-bound leader identity.
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        crashed_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def crashed_executor(*, transform, token, ctx, attempt=0):
            return (
                TransformResult.success(
                    make_row({**token.row_data.to_dict(), "resumed": True}),
                    success_reason={"action": "crashed_worker_transform"},
                ),
                token,
                None,
            )

        with patch.object(crashed_processor._transform_executor, "execute_transform", side_effect=crashed_executor):
            crashed_processor.drain_scheduled_work(ctx)

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            statuses = (
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == "test-run")).scalars().all()
            )
        assert sorted(statuses) == ["pending_sink"] * 3, (
            f"Expected three PENDING_SINK rows after the pre-crash drain; got {sorted(statuses)}."
        )

        # Stage 2: fresh recovery processor. The bug: only one PENDING_SINK
        # row emits a RowResult; the other two stay PENDING_SINK because the
        # gate blocks the recovery branch after the first claim.
        resume_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        with patch.object(
            resume_processor._transform_executor,
            "execute_transform",
            side_effect=AssertionError("transform replayed during pending-sink recovery"),
        ):
            results = resume_processor.drain_scheduled_work(ctx)

        recovered_token_ids = sorted(result.token.token_id for result in results)
        assert recovered_token_ids == sorted(pending_token_ids), (
            f"All pre-existing PENDING_SINK rows must be re-emitted in a single drain call; got {recovered_token_ids}."
        )

        with db.connection() as conn:
            statuses_after = (
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == "test-run")).scalars().all()
            )
        # Every row is now LEASED by the token-bound leader awaiting the sink
        # callback; none remain in PENDING_SINK status.
        assert sorted(statuses_after) == ["leased"] * 3

    def test_drain_emits_pending_sink_recovery_without_duplicating_fresh_work(self) -> None:
        """Mixed pre-existing PENDING_SINK + new READY work must not double-emit.

        Discriminating regression for elspeth-5c5e88b071 (G3): the naive fix —
        deleting the gate and putting ``claim_pending_sink`` inside the main
        loop — would re-claim PENDING_SINK rows that the current drain just
        produced via ``mark_pending_sink`` and emit a duplicate RowResult via
        ``_row_result_from_pending_sink`` for work already represented by the
        original sink-bound RowResult. Pre-existing pending-sinks must be
        drained up front; newly-marked pending-sinks must only be terminalized
        by the sink callback, never re-claimed inside the same drain.
        """
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # Two pre-existing pending-sink rows representing a prior crash image.
        pre_existing_token_ids: list[str] = []
        for idx in range(2):
            source_payload = make_row({"value": idx})
            row = factory.data_flow.create_row_with_token(
                source_node_id="source-0",
                row_index=idx,
                source_row_index=idx,
                ingest_sequence=idx,
                data=source_payload.to_dict(),
                coordination_token=leader_coordination_token(factory, "test-run"),
            )[0]
            token_id = f"token-pre-{idx}"
            pre_existing_token_ids.append(token_id)
            token = factory.data_flow.create_token(
                row.row_id,
                token_id=token_id,
                coordination_token=leader_coordination_token(factory, "test-run"),
            )
            factory.scheduler.enqueue_ready(
                token_id=token.token_id,
                row_id=row.row_id,
                node_id=str(transform_node),
                step_index=1,
                ingest_sequence=idx,
                row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
                member_token=leader_coordination_token(factory, "test-run").membership,
            )

        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # Stage 1: the first processor pushes the pre-existing rows to
        # PENDING_SINK before the simulated crash window.
        crashed_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )

        def crashed_executor(*, transform, token, ctx, attempt=0):
            return (
                TransformResult.success(
                    make_row({**token.row_data.to_dict(), "resumed": True}),
                    success_reason={"action": "crashed_worker_transform"},
                ),
                token,
                None,
            )

        with patch.object(crashed_processor._transform_executor, "execute_transform", side_effect=crashed_executor):
            crashed_processor.drain_scheduled_work(ctx)

        # Stage 2: enqueue a brand new READY token that the recovery processor
        # will process to completion (ending in PENDING_SINK durably and
        # emitting one sink-bound RowResult).
        fresh_payload = make_row({"value": 99})
        fresh_row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=99,
            source_row_index=99,
            ingest_sequence=99,
            data=fresh_payload.to_dict(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        fresh_token_id = "token-fresh"
        fresh_token = factory.data_flow.create_token(
            fresh_row.row_id,
            token_id=fresh_token_id,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=fresh_token.token_id,
            row_id=fresh_row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=99,
            row_payload_json=factory.scheduler.serialize_row_payload(fresh_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        # Stage 3: a fresh recovery processor drains everything in one call.
        # Pre-existing rows recover via _row_result_from_pending_sink; the
        # fresh row processes through the transform and emits the sink-bound
        # RowResult directly. Neither path may emit the same token twice.
        resume_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )

        def resume_executor(*, transform, token, ctx, attempt=0):
            # Pre-existing tokens must NOT re-enter the transform (their
            # transform work is durable from the pre-crash drain).
            assert token.token_id == fresh_token_id, f"transform replayed for {token.token_id!r} during recovery drain"
            return (
                TransformResult.success(
                    make_row({**token.row_data.to_dict(), "fresh_processed": True}),
                    success_reason={"action": "resume_worker_transform"},
                ),
                token,
                None,
            )

        with patch.object(resume_processor._transform_executor, "execute_transform", side_effect=resume_executor):
            results = resume_processor.drain_scheduled_work(ctx)

        emitted_token_ids = [result.token.token_id for result in results]
        assert len(emitted_token_ids) == len(set(emitted_token_ids)), (
            f"Duplicate RowResults emitted for the same token_id: {emitted_token_ids}. "
            "claim_pending_sink must not re-claim rows produced by mark_pending_sink "
            "within the same drain call."
        )
        assert sorted(emitted_token_ids) == sorted([*pre_existing_token_ids, fresh_token_id])

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            statuses_after = (
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == "test-run")).scalars().all()
            )
        # All three rows now sit in non-terminal states awaiting the sink
        # callback. The pre-existing two are LEASED (claim_pending_sink
        # transition); the fresh one is PENDING_SINK (mark_pending_sink
        # transition without a subsequent claim).
        assert sorted(statuses_after) == ["leased", "leased", "pending_sink"]

    def test_drain_scheduler_transitions_stamp_database_time_not_the_injected_clock(self) -> None:
        """C6 stage 3 control (ADR-047): a disposition's stamp is Landscape database time.

        The processor's injected MockClock sits in 2023; the durable
        transition it drives must carry the database's own instant, so a
        worker whose process clock is wrong cannot mis-stamp the scheduler
        ledger.
        """
        from tests.fixtures.landscape import assert_stamped_between, landscape_database_now

        db, factory = _make_factory()
        clock = MockClock(start=1_700_000_000.0)
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 42})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-clock",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
            clock=clock,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        success_result = TransformResult.success(
            make_row({"value": 42, "resumed": True}),
            success_reason={"action": "resume_drain"},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            clock.advance(5.0)
            return (success_result, token, None)

        drained_from = landscape_database_now(db.engine)
        with patch.object(processor._transform_executor, "execute_transform", side_effect=executor_side_effect):
            processor.drain_scheduled_work(ctx)
        drained_until = landscape_database_now(db.engine)

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, updated_at = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.updated_at).where(
                    token_work_items_table.c.token_id == "token-clock"
                )
            ).one()
        assert status == "pending_sink"
        assert_stamped_between(updated_at, start=drained_from, end=drained_until, tolerance=timedelta(0))
        assert updated_at.replace(tzinfo=UTC) != clock.now_utc()

    def test_recovers_expired_lease_then_drains_without_source_replay(self) -> None:
        """Expired LEASED scheduler work is recovered and advanced by a fresh processor."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 43})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-expired",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        # The owner is registry-DEAD on the Landscape database clock (ADR-047):
        # its heartbeat lapsed more than the liveness grace window ago, so the
        # sweep's dead-owner arm reaps its lease once that lease has expired
        # on the same clock.
        _register_test_worker(
            factory,
            "dead-worker",
            heartbeat_expires_at=landscape_database_now(db.engine) - timedelta(seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS + 1),
        )
        claimed = factory.scheduler.claim_ready(
            lease_owner="dead-worker",
            lease_seconds=1,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="dead-worker"),
        )
        assert claimed is not None and claimed.lease_expires_at is not None
        # The drain reconciles the row against its CLAIM_READY witness, so the
        # one-second lease must expire the way it does in production: by
        # database time passing.
        await_database_time(db.engine, claimed.lease_expires_at)

        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        success_result = TransformResult.success(
            make_row({"value": 43, "resumed": True}),
            success_reason={"action": "expired_lease_recovered"},
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            assert token.token_id == "token-expired"
            assert token.row_data.to_dict() == {"value": 43}
            assert attempt == 1
            return (success_result, token, None)

        with patch.object(processor._transform_executor, "execute_transform", side_effect=executor_side_effect):
            results = processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        assert results[0].token.token_id == "token-expired"
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            statuses = (
                conn.execute(select(token_work_items_table.c.status).where(token_work_items_table.c.run_id == "test-run")).scalars().all()
            )
        assert statuses == ["pending_sink"]

    def test_drain_raises_when_resultless_work_has_no_scheduler_release_key(self) -> None:
        """Durable work cannot be BLOCKED unless resume has a release key for it."""
        _db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 99})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-stranded",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        work_item = factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with (
            patch.object(processor, "_process_single_token", return_value=(None, [])),
            pytest.raises(
                OrchestrationInvariantError,
                match=(
                    rf"Work item '{work_item.work_item_id}'.*token='token-stranded'.*"
                    rf"node={str(transform_node)!r}.*no queue or barrier key"
                ),
            ),
        ):
            processor.drain_scheduled_work(ctx)

    def test_resume_restores_branch_lineage_for_direct_sink_work(self) -> None:
        """Direct branch sink routing must survive durable scheduler resume."""
        _db, factory = _make_factory()
        source_payload = make_row({"value": 44})
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        token = TokenInfo(
            row_id=row.row_id,
            token_id="token-direct-branch",
            row_data=source_payload,
            lineage_path=(
                LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="direct"),
                LineageFrame(kind=FrameKind.EXPAND, group_id="expand-1", member_key="direct"),
            ),
        )
        factory.data_flow.create_token(
            row.row_id,
            token_id=token.token_id,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=None,
            step_index=99,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            on_success_sink="source_sink",
            lineage_path=token.lineage_path,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            branch_to_sink={BranchName("direct"): "branch_sink"},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        assert results[0].sink_name == "branch_sink"
        assert results[0].token.branch_name == "direct"
        assert results[0].token.fork_group_id == "fork-1"
        assert results[0].token.expand_group_id == "expand-1"

    def test_durable_scheduler_failure_result_marks_work_failed(self) -> None:
        """Durable failure outcomes remain distinguishable from successful terminal work."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 45})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-failed",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        failed_result = RowResult(
            token=TokenInfo(row_id=row.row_id, token_id=token.token_id, row_data=source_payload),
            final_data=source_payload,
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with patch.object(processor, "_process_single_token", return_value=(failed_result, [])):
            results = processor.drain_scheduled_work(ctx)

        assert results == [failed_result]
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, run_id, work_item_id = conn.execute(
                select(
                    token_work_items_table.c.status,
                    token_work_items_table.c.run_id,
                    token_work_items_table.c.work_item_id,
                ).where(token_work_items_table.c.token_id == "token-failed")
            ).one()
        assert status == "failed"
        assert run_id == "test-run"
        assert work_item_id

    def test_durable_scheduler_on_error_routed_result_marks_pending_sink(self) -> None:
        """ON_ERROR_ROUTED failures are sink-bound until error-sink durability."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 45})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-on-error-routed",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default", on_error="errors")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        raw_secret = "https://blob.example/path?sig=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        routed_result = RowResult(
            token=TokenInfo(row_id=row.row_id, token_id=token.token_id, row_data=source_payload),
            final_data=source_payload,
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.ON_ERROR_ROUTED,
            sink_name="errors",
            error=FailureInfo(exception_type="TransformError", message=f"provider failed: {raw_secret}"),
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with patch.object(processor, "_process_single_token", return_value=(routed_result, [])):
            results = processor.drain_scheduled_work(ctx)

        assert len(results) == 1
        assert results[0].outcome == TerminalOutcome.FAILURE
        assert results[0].path == TerminalPath.ON_ERROR_ROUTED
        assert results[0].sink_name == "errors"
        assert results[0].scheduler_pending_sink is True
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            row_state = conn.execute(
                select(
                    token_work_items_table.c.status,
                    token_work_items_table.c.pending_sink_name,
                    token_work_items_table.c.pending_outcome,
                    token_work_items_table.c.pending_path,
                    token_work_items_table.c.pending_error_message,
                    token_work_items_table.c.row_payload_json,
                ).where(token_work_items_table.c.token_id == "token-on-error-routed")
            ).one()
        assert row_state.status == "pending_sink"
        assert row_state.pending_sink_name == "errors"
        assert row_state.pending_outcome == TerminalOutcome.FAILURE.value
        assert row_state.pending_path == TerminalPath.ON_ERROR_ROUTED.value
        assert row_state.pending_error_message == "<redacted-secret>"
        assert raw_secret not in row_state.pending_error_message
        assert factory.scheduler.deserialize_row_payload(row_state.row_payload_json).to_dict() == source_payload.to_dict()

    def test_durable_scheduler_tuple_failure_for_claimed_token_marks_work_failed(self) -> None:
        """Tuple results must classify the claimed token, not fall through to terminal."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 45})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-failed-tuple",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        failed_result = RowResult(
            token=TokenInfo(row_id=row.row_id, token_id=token.token_id, row_data=source_payload),
            final_data=source_payload,
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
        )
        sibling_success = RowResult(
            token=TokenInfo(row_id=row.row_id, token_id="sibling-token", row_data=source_payload),
            final_data=source_payload,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="default",
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with patch.object(processor, "_process_single_token", return_value=((sibling_success, failed_result), [])):
            results = processor.drain_scheduled_work(ctx)

        assert results == [sibling_success, failed_result]
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status = conn.execute(
                select(token_work_items_table.c.status).where(token_work_items_table.c.token_id == "token-failed-tuple")
            ).scalar_one()
        assert status == "failed"

    def test_durable_scheduler_tuple_failure_for_sibling_keeps_claimed_work_terminal(self) -> None:
        """Sibling failures returned with a claimed token must not poison the claimed work item."""
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 45})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-claimed-success",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        claimed_success = RowResult(
            token=TokenInfo(row_id=row.row_id, token_id=token.token_id, row_data=source_payload),
            final_data=source_payload,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="default",
        )
        sibling_failure = RowResult(
            token=TokenInfo(row_id=row.row_id, token_id="sibling-token", row_data=source_payload),
            final_data=source_payload,
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with patch.object(processor, "_process_single_token", return_value=((sibling_failure, claimed_success), [])):
            results = processor.drain_scheduled_work(ctx)

        assert len(results) == 2
        assert results[0] == sibling_failure
        assert results[1].token.token_id == claimed_success.token.token_id
        assert results[1].outcome == claimed_success.outcome
        assert results[1].path == claimed_success.path
        assert results[1].sink_name == claimed_success.sink_name
        assert results[1].scheduler_pending_sink is True
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status = conn.execute(
                select(token_work_items_table.c.status).where(token_work_items_table.c.token_id == "token-claimed-success")
            ).scalar_one()
        assert status == "pending_sink"

    def test_scheduler_failure_write_preserves_processing_exception_context(self) -> None:
        """If marking scheduler work failed also fails, diagnostics keep both causes."""
        _db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 46})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-crash",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        transform = _make_mock_transform(node_id=str(transform_node), on_success="default")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: transform},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        with (
            patch.object(processor, "_process_single_token", side_effect=ValueError("transform exploded")),
            patch.object(factory.scheduler, "mark_failed", side_effect=AuditIntegrityError("scheduler write rejected")),
            pytest.raises(
                AuditIntegrityError,
                match=r"original processing exception ValueError: transform exploded.*scheduler failure write.*scheduler write rejected",
            ) as exc_info,
        ):
            processor.drain_scheduled_work(ctx)

        assert isinstance(exc_info.value.__cause__, AuditIntegrityError)

    def test_resume_restores_coalesce_cursor_for_held_branch_work(self) -> None:
        """Coalesce-held branch work must rehydrate both token lineage and work-item cursor."""
        db, factory = _make_factory()
        coalesce_node = NodeID("coalesce::merge")
        source_payload = make_row({"value": 45})
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        factory.data_flow.register_node(
            plugin_name="coalesce:merge",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id=str(coalesce_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = TokenInfo(
            row_id=row.row_id,
            token_id="token-held-branch",
            row_data=source_payload,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-2", member_key="path_a"),),
        )
        factory.data_flow.create_token(
            row.row_id,
            token_id=token.token_id,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(coalesce_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            lineage_path=token.lineage_path,
            coalesce_node_id=str(coalesce_node),
            coalesce_name="merge",
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = CoalesceOutcome(held=True, merged_token=None)
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            node_step_map={coalesce_node: 1},
            node_to_next={NodeID("source-0"): coalesce_node, coalesce_node: None},
            coalesce_on_success_map={CoalesceName("merge"): "merged_sink"},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor.drain_scheduled_work(ctx)

        assert results == []
        coalesce.accept.assert_called_once()
        accepted = coalesce.accept.call_args.kwargs["token"]
        assert accepted.token_id == "token-held-branch"
        assert accepted.branch_name == "path_a"
        assert coalesce.accept.call_args.kwargs["coalesce_name"] == CoalesceName("merge")
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, barrier_key = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.barrier_key).where(
                    token_work_items_table.c.token_id == "token-held-branch"
                )
            ).one()
        assert (status, barrier_key) == ("blocked", "merge")

    def _make_real_coalesce_executor(self, factory: RecorderFactory, coalesce_node: NodeID) -> Any:
        """Build a production CoalesceExecutor over the factory's real repositories."""
        from elspeth.core.config import CoalesceSettings
        from elspeth.engine.coalesce_executor import CoalesceExecutor
        from elspeth.engine.tokens import TokenManager

        def step_resolver(_node_id: NodeID) -> int:
            return 1

        executor = CoalesceExecutor(
            execution=factory.execution,
            span_factory=SpanFactory(),
            token_manager=TokenManager(factory.data_flow, step_resolver=step_resolver),
            run_id="test-run",
            step_resolver=step_resolver,
            data_flow=factory.data_flow,
            barrier_restore_reads=factory.barrier_restore,
        )
        executor.register_coalesce(
            CoalesceSettings(name="merge", branches=["path_a", "path_b"], policy="require_all", merge="union"),
            coalesce_node,
        )
        return executor

    def _seed_held_coalesce_branch(self, factory: RecorderFactory, coalesce_node: NodeID) -> str:
        """Register the coalesce node and enqueue a path_a branch token at it.

        Returns the row_id of the forked source row.
        """
        source_payload = make_row({"value": 45})
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        factory.data_flow.register_node(
            plugin_name="coalesce:merge",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id=str(coalesce_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.data_flow.create_token(
            row.row_id,
            token_id="token-held-a",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id="token-held-a",
            row_id=row.row_id,
            node_id=str(coalesce_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),),
            coalesce_node_id=str(coalesce_node),
            coalesce_name="merge",
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        return str(row.row_id)

    def test_resume_restores_coalesce_pending_from_blocked_rows(self) -> None:
        """F1 restore inversion: resume rebuilds coalesce pendings FROM journal BLOCKED rows.

        Arrange drives the PRODUCTION hold path (drain -> accept holds ->
        mark_blocked stamps barrier_blocked_at; accept writes the OPEN
        node_state hold at attempt 0). Act builds a fresh processor +
        executor with barrier_restore. The pending entry must carry the
        journal token, the audit-derived hold state_id, and the
        max_attempt+1 offset — with no new journal rows.
        """
        from sqlalchemy import func, select

        from elspeth.core.landscape.schema import token_work_items_table

        db, factory = _make_factory()
        coalesce_node = NodeID("coalesce::merge")
        # Return value (row_id) is unused: both restore and live-accept keys
        # are fork_group_id-based now (WS4 Task 8/10) — see "fork-1" below.
        self._seed_held_coalesce_branch(factory, coalesce_node)
        arrange_executor = self._make_real_coalesce_executor(factory, coalesce_node)
        processor_kwargs: dict[str, Any] = {
            "coalesce_node_ids": {CoalesceName("merge"): coalesce_node},
            "node_step_map": {NodeID("source-0"): 0, coalesce_node: 1},
            "node_to_next": {NodeID("source-0"): coalesce_node, coalesce_node: None},
            "coalesce_on_success_map": {CoalesceName("merge"): "merged_sink"},
        }
        processor1 = _make_processor(factory, coalesce_executor=arrange_executor, **processor_kwargs)
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor1.drain_scheduled_work(ctx)
        assert results == []
        # Live accept() path is fork_group_id-keyed (WS4 Task 8); the token's
        # fork_group_id is "fork-1" (see the resume assertion below, which
        # independently confirms it via entry.token.fork_group_id).
        assert ("merge", "fork-1") in arrange_executor._pending

        # The hold node_state written by accept() — the state_id the restore must rediscover.
        held_states = [
            state
            for state in factory.query.get_node_states_for_token("token-held-a")
            if str(state.node_id) == str(coalesce_node) and state.status is NodeStateStatus.OPEN
        ]
        assert len(held_states) == 1
        hold_state_id = held_states[0].state_id
        assert held_states[0].attempt == 0

        with db.connection() as conn:
            rows_before = conn.execute(select(func.count()).select_from(token_work_items_table)).scalar_one()
            status, barrier_key = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.barrier_key).where(
                    token_work_items_table.c.token_id == "token-held-a"
                )
            ).one()
        assert (status, barrier_key) == ("blocked", "merge")

        # ---- resume: fresh executor + processor rebuild FROM the journal ----
        resumed_executor = self._make_real_coalesce_executor(factory, coalesce_node)
        _make_processor(
            factory,
            coalesce_executor=resumed_executor,
            barrier_restore=BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-resume-1",
                barrier_scalars=None,
                batch_id_remap={},
            ),
            **processor_kwargs,
        )

        # restore_from_journal now groups by fork_group_id too (WS4 Task 10) —
        # both the live accept() path and the restore path agree on the key
        # shape, closing the premise-break window T8-alone would have left.
        pending = resumed_executor._pending[("merge", "fork-1")]
        assert set(pending.branches) == {"path_a"}
        entry = pending.branches["path_a"]
        assert entry.token.token_id == "token-held-a"
        assert entry.token.branch_name == "path_a"
        assert entry.token.fork_group_id == "fork-1"
        assert entry.state_id == hold_state_id  # state_id resolved from the OPEN node_state hold
        assert entry.token.resume_attempt_offset == 1  # max_attempt(0)+1
        assert entry.token.resume_checkpoint_id == "ckpt-resume-1"
        assert pending.lost_branches == {}
        # NO new journal rows: the BLOCKED row is reused as-is.
        with db.connection() as conn:
            rows_after = conn.execute(select(func.count()).select_from(token_work_items_table)).scalar_one()
        assert rows_after == rows_before

    def test_resume_restore_recovers_adopted_coalesce_row_without_hold_state(self) -> None:
        """Adopted BLOCKED coalesce row with no OPEN node_state hold is a crash-window state.

        Under ADR-030 §E.2 journal-first ordering the adoption CAS commits in
        one transaction and accept() (which writes the PENDING hold) runs in a
        separate transaction.  A leader crash between them leaves an adopted row
        with no OPEN state_id — a reachable, legitimate crash state, not
        corruption.

        The restore reconcile (§E.3/§E.4 crash-window recovery) must:
        - detect the holdless adopted row,
        - determine the key is NOT completed (normal adoption crash, not a late
          arrival), and
        - reset barrier_adopted_epoch to NULL so the row becomes intake-pending
          again and the first drain iteration's journal-first intake processes it
          via the normal adopt + accept + trigger path.

        The processor MUST be created successfully (no AuditIntegrityError).
        After creation, the row must be intake-pending (barrier_adopted_epoch IS
        NULL) so the next drain iteration's journal-first intake picks it up.

        Re-pin of test_resume_restore_rejects_blocked_coalesce_row_without_hold_state:
        that pin pre-dated §E.2 and claimed "hold written BEFORE journal row blocks",
        which is inverted under journal-first ordering — the adoption CAS commits
        first, accept() runs after. A refusal on this crash state permanently
        wedges every subsequent resume.
        """
        db, factory = _make_factory()
        coalesce_node = NodeID("coalesce::merge")
        self._seed_held_coalesce_branch(factory, coalesce_node)
        datetime.now(UTC)
        # Block the row through the production claim path WITHOUT the
        # accept()-written hold node_state (simulates a crash between adoption
        # CAS commit and accept() in _intake_adopt_coalesce_row).
        claimed = factory.scheduler.claim_ready(
            lease_owner="seeder",
            lease_seconds=60,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="seeder"),
        )
        assert claimed is not None
        factory.scheduler.mark_blocked(
            work_item_id=claimed.work_item_id,
            row_payload_json=claimed.row_payload_json,
            queue_key=None,
            barrier_key="merge",
            expected_lease_owner="seeder",
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="seeder"),
        )

        resumed_executor = self._make_real_coalesce_executor(factory, coalesce_node)
        # Under §E.2 crash-window recovery the processor MUST be created without
        # error; the holdless adopted row is reset to intake-pending.
        _make_processor(
            factory,
            coalesce_executor=resumed_executor,
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            node_step_map={NodeID("source-0"): 0, coalesce_node: 1},
            node_to_next={NodeID("source-0"): coalesce_node, coalesce_node: None},
            coalesce_on_success_map={CoalesceName("merge"): "merged_sink"},
            barrier_restore=BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-resume-1",
                barrier_scalars=None,
                batch_id_remap={},
            ),
        )
        # After crash-window recovery: barrier_adopted_epoch must be NULL
        # (reset by the restore reconcile) so the next intake re-adopts the row.
        from sqlalchemy import select as _select

        from elspeth.core.landscape.schema import token_work_items_table as _twi

        with db.connection() as conn:
            row = conn.execute(_select(_twi.c.barrier_adopted_epoch).where(_twi.c.work_item_id == claimed.work_item_id)).one()
        assert row.barrier_adopted_epoch is None, (
            "crash-window recovery must reset barrier_adopted_epoch to NULL so the intake re-processes the row"
        )

    def test_aggregation_buffering_leaves_scheduler_work_blocked(self) -> None:
        """Buffered aggregation tokens remain active until a flush consumes them."""
        db, factory = _make_factory()
        source_node = NodeID("source-0")
        agg_node = NodeID("agg-1")
        factory.data_flow.register_node(
            plugin_name="agg-transform",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id=str(agg_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        transform = _make_mock_transform(node_id=str(agg_node), name="agg-transform", is_batch_aware=True)
        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, agg_node: 1},
            node_to_next={source_node: agg_node, agg_node: None},
            node_to_plugin={agg_node: transform},
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="batch_agg",
                    plugin="agg-transform",
                    input="default",
                    on_error="discard",
                    trigger={"count": 2},
                ),
            },
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor.process_row(
            row_index=0,
            source_row=_make_source_row({"value": 1}),
            transforms=[transform],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )

        assert len(results) == 1
        _assert_outcome_pair(results[0], None, TerminalPath.BUFFERED)
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, barrier_key = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.barrier_key).where(
                    token_work_items_table.c.token_id == results[0].token.token_id
                )
            ).one()
        assert (status, barrier_key) == ("blocked", str(agg_node))

    def test_aggregation_intake_adopts_follower_blocked_work_before_flush(self) -> None:
        """A fresh leader journal-adopts a follower-blocked aggregation row."""
        db, factory = _make_factory()
        source_node = NodeID("source-0")
        agg_node = NodeID("agg-1")
        factory.data_flow.register_node(
            plugin_name="agg-transform",
            node_type=NodeType.AGGREGATION,
            plugin_version="1.0",
            config={},
            node_id=str(agg_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        transform = _make_mock_transform(
            node_id=str(agg_node),
            name="agg-transform",
            is_batch_aware=True,
            on_success="agg_sink",
            result=TransformResult.success(make_row({"total": 3}), success_reason={"action": "aggregate"}),
        )
        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, agg_node: 1},
            node_to_next={source_node: agg_node, agg_node: None},
            node_to_plugin={agg_node: transform},
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="batch_agg",
                    plugin="agg-transform",
                    input="default",
                    on_error="discard",
                    trigger={"count": 2},
                ),
            },
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        first_results = processor.process_row(
            row_index=0,
            source_row=_make_source_row({"value": 1}),
            transforms=[transform],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )
        first_token_id = first_results[0].token.token_id

        stray_payload = make_row({"value": 99})
        stray_row, stray_token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=99,
            source_row_index=99,
            ingest_sequence=99,
            data=stray_payload.to_dict(),
            token_id="stray-buffered-token",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        stray_work = factory.scheduler.enqueue_ready(
            token_id=stray_token.token_id,
            row_id=stray_row.row_id,
            node_id=str(agg_node),
            step_index=1,
            ingest_sequence=99,
            row_payload_json=factory.scheduler.serialize_row_payload(stray_payload),
            barrier_key=str(agg_node),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        _register_test_worker(factory, "test-worker")
        stray_claim = factory.scheduler.claim_ready(
            lease_owner="test-worker",
            lease_seconds=30,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="test-worker"),
        )
        assert stray_claim is not None
        assert stray_claim.work_item_id == stray_work.work_item_id
        factory.scheduler.mark_blocked(
            work_item_id=stray_work.work_item_id,
            row_payload_json=stray_work.row_payload_json,
            queue_key=None,
            barrier_key=str(agg_node),
            expected_lease_owner="test-worker",
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="test-worker"),
        )

        second_results = processor.process_row(
            row_index=1,
            source_row=_make_source_row({"value": 2}),
            transforms=[transform],
            ctx=ctx,
            source_row_index=1,
            ingest_sequence=1,
        )

        # The follower-shaped durable row is not an orphan: it is accepted
        # from the journal, triggers the count flush with the leader's own
        # buffered row, and the current row then buffers normally.
        assert [(r.outcome, r.path) for r in second_results] == [
            (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW),
            (None, TerminalPath.BUFFERED),
        ]
        assert second_results[0].sink_name == "agg_sink"
        assert second_results[0].scheduler_pending_sink is True
        flushed_token_id = second_results[0].token.token_id
        current_token_id = second_results[1].token.token_id

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        # The consumed journal rows are both terminalized by the atomic flush.
        with db.connection() as conn:
            rows = {
                row.token_id: row
                for row in conn.execute(
                    select(
                        token_work_items_table.c.token_id,
                        token_work_items_table.c.status,
                        token_work_items_table.c.barrier_adopted_epoch,
                    ).where(
                        token_work_items_table.c.token_id.in_(
                            [
                                first_token_id,
                                stray_token.token_id,
                            ]
                        )
                    )
                )
            }
        assert {token_id: (row.status, row.barrier_adopted_epoch) for token_id, row in rows.items()} == {
            first_token_id: ("terminal", 1),
            stray_token.token_id: ("terminal", 1),
        }

        with db.connection() as conn:
            statuses = {
                row.token_id: row.status
                for row in conn.execute(
                    select(token_work_items_table.c.token_id, token_work_items_table.c.status).where(
                        token_work_items_table.c.token_id.in_([flushed_token_id, current_token_id])
                    )
                )
            }
        assert statuses == {
            flushed_token_id: "pending_sink",
            current_token_id: "blocked",
        }

    def test_passthrough_aggregation_sink_flush_marks_all_outputs_pending_sink(self) -> None:
        """Every sink-bound passthrough flush token needs a durable sink handoff."""
        db, factory = _make_factory()
        source_node = NodeID("source-0")
        agg_node = NodeID("agg-1")
        contract = _make_contract()
        transform = _make_mock_transform(
            node_id=str(agg_node),
            name="agg-transform",
            is_batch_aware=True,
            on_success="agg_sink",
            result=TransformResult.success_multi(
                (
                    make_row({"value": 11}, contract=contract),
                    make_row({"value": 22}, contract=contract),
                ),
                success_reason={"action": "passthrough"},
            ),
        )
        transform.passes_through_input = True
        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, agg_node: 1},
            node_to_next={source_node: agg_node, agg_node: None},
            node_to_plugin={agg_node: transform},
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="batch_agg",
                    plugin="agg-transform",
                    input="default",
                    on_error="discard",
                    trigger={"count": 2},
                    output_mode="passthrough",
                ),
            },
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        first_results = processor.process_row(
            row_index=0,
            source_row=_make_source_row({"value": 1}),
            transforms=[transform],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )
        assert len(first_results) == 1
        _assert_outcome_pair(first_results[0], None, TerminalPath.BUFFERED)

        second_results = processor.process_row(
            row_index=1,
            source_row=_make_source_row({"value": 2}),
            transforms=[transform],
            ctx=ctx,
            source_row_index=1,
            ingest_sequence=1,
        )

        # F1 Task 4.3 (rows_buffered unification): the triggering token's
        # buffer-accept surfaces as a synthetic (None, BUFFERED) result
        # alongside the two terminal flush outputs, so every accepted batch
        # member contributes exactly one BUFFERED result to the live counter.
        assert len(second_results) == 3
        buffered_results = [result for result in second_results if result.path is TerminalPath.BUFFERED]
        assert len(buffered_results) == 1
        assert buffered_results[0].outcome is None
        flush_results = [result for result in second_results if result.path is not TerminalPath.BUFFERED]
        assert len(flush_results) == 2
        flushed_token_ids = tuple(result.token.token_id for result in flush_results)
        # The triggering token both buffers (synthetic) and flushes (terminal).
        assert buffered_results[0].token.token_id in flushed_token_ids
        assert first_results[0].token.token_id in flushed_token_ids
        assert len(frozenset(flushed_token_ids)) == 2
        assert all(result.sink_name == "agg_sink" for result in flush_results)
        assert all(result.scheduler_pending_sink for result in flush_results)

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            rows = conn.execute(
                select(
                    token_work_items_table.c.token_id,
                    token_work_items_table.c.status,
                    token_work_items_table.c.pending_sink_name,
                    token_work_items_table.c.pending_outcome,
                    token_work_items_table.c.pending_path,
                ).where(token_work_items_table.c.token_id.in_(flushed_token_ids))
            ).all()
        assert {(row.token_id, row.status, row.pending_sink_name, row.pending_outcome, row.pending_path) for row in rows} == {
            (token_id, "pending_sink", "agg_sink", TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value)
            for token_id in flushed_token_ids
        }

        resumed_processor = _make_processor(
            factory,
            node_step_map={source_node: 0, agg_node: 1},
            node_to_next={source_node: agg_node, agg_node: None},
            node_to_plugin={agg_node: transform},
            aggregation_settings={
                agg_node: AggregationSettings(
                    name="batch_agg",
                    plugin="agg-transform",
                    input="default",
                    on_error="discard",
                    trigger={"count": 2},
                    output_mode="passthrough",
                ),
            },
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        with patch.object(
            resumed_processor._transform_executor,
            "execute_transform",
            side_effect=AssertionError("transform replayed during aggregation pending-sink recovery"),
        ):
            recovered_results = resumed_processor.drain_scheduled_work(ctx)

        recovered_token_ids = tuple(result.token.token_id for result in recovered_results)
        assert len(recovered_token_ids) == len(frozenset(recovered_token_ids))
        assert sorted(recovered_token_ids) == sorted(flushed_token_ids)
        assert all(result.sink_name == "agg_sink" for result in recovered_results)
        assert all(result.scheduler_pending_sink for result in recovered_results)

        with db.connection() as conn:
            resumed_rows = conn.execute(
                select(
                    token_work_items_table.c.token_id,
                    token_work_items_table.c.status,
                    token_work_items_table.c.lease_owner,
                ).where(token_work_items_table.c.token_id.in_(flushed_token_ids))
            ).all()
        assert {(row.token_id, row.status, row.lease_owner) for row in resumed_rows} == {
            (token_id, "leased", _TEST_LEADER_WORKER_ID) for token_id in flushed_token_ids
        }

    def test_drain_scheduled_work_no_longer_refuses_on_peer_lease(self) -> None:
        """Slice 4 demotion: peer unexpired leases are diagnostic, not refusals.

        ADR-030 §G slice 4 demotes ``peer_active_leases`` from a hard refusal
        to a debug-level diagnostic. The correctness gate is now the
        ``active_worker_fence_clause`` membership fence compiled into the claim
        verbs — a non-member's claim CAS fails the EXISTS fence. A concurrent
        peer holding an unexpired lease is a NORMAL multi-worker state, not a
        precondition violation (archived issue elspeth-66be4216cd, G3).

        This test was previously ``test_drain_refuses_when_peer_worker_holds_active_lease``
        and asserted an ``AuditIntegrityError`` raise. It now asserts the
        OPPOSITE: ``drain_scheduled_work`` must NOT raise. The leader's drain
        enters the claim loop, finds the row still LEASED under peer-worker-A
        (``claim_ready`` returns None — no READY rows), and exits cleanly with
        an empty result list.
        """
        db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 1})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-peer-held",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        # Peer worker A claims the row under its own lease_owner. Lease window
        # is wide enough that ``peer_active_leases`` sees it as unexpired.
        _register_test_worker(factory, "peer-worker-A")
        peer_claim = factory.scheduler.claim_ready(
            lease_owner="peer-worker-A",
            lease_seconds=600,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="peer-worker-A"),
        )
        assert peer_claim is not None
        assert peer_claim.lease_owner == "peer-worker-A"

        # The token-bound leader logs the peer diagnostic but does NOT raise.
        leader_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: _make_mock_transform(node_id=str(transform_node), on_success="default")},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # No raise: peer lease is diagnostic, not a refusal.
        results = leader_processor.drain_scheduled_work(ctx)
        # claim_ready found no READY rows (row is LEASED under peer-worker-A).
        assert results == []

        # Peer's row is still owned by peer-worker-A — the leader drain was a no-op.
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, lease_owner = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.lease_owner).where(
                    token_work_items_table.c.token_id == "token-peer-held"
                )
            ).one()
        assert status == "leased"
        assert lease_owner == "peer-worker-A"

    def test_drain_proceeds_when_peer_lease_expired(self) -> None:
        """An expired peer lease is recoverable, not a precondition violation.

        Single-active-resume blocks unexpired peer leases. An expired peer lease
        is the normal crashed-prior-worker recovery path: the surviving worker
        sweeps it via ``recover_expired_leases`` and proceeds. This test pins
        that the precondition does NOT over-reject — only unexpired peers are
        treated as live.
        """
        clock = MockClock(start=1_700_000_000.0)
        _db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 1})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-stale-peer",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        # Crashed peer A held a short lease that has since expired.
        _register_test_worker(factory, "crashed-peer", heartbeat_expires_at=clock.now_utc() - timedelta(seconds=120))
        crashed_claim = factory.scheduler.claim_ready(
            lease_owner="crashed-peer",
            lease_seconds=1,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="crashed-peer"),
        )
        assert crashed_claim is not None and crashed_claim.lease_expires_at is not None
        # The drain reconciles the row against its CLAIM_READY witness, so the
        # one-second lease must expire the way it does in production: on the
        # Landscape database clock (ADR-047). Advancing the process MockClock
        # cannot expire a database-time lease.
        await_database_time(_db.engine, crashed_claim.lease_expires_at)
        clock.advance(60.0)

        success_result = TransformResult.success(
            make_row({"value": 1, "resumed": True}),
            success_reason={"action": "resume_drain"},
        )
        leader_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: _make_mock_transform(node_id=str(transform_node), on_success="default")},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
            clock=clock,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        def executor_side_effect(*, transform, token, ctx, attempt=0):
            return (success_result, token, None)

        with patch.object(leader_processor._transform_executor, "execute_transform", side_effect=executor_side_effect):
            results = leader_processor.drain_scheduled_work(ctx)

        # Expired peer lease was recovered; row drained under the token-bound leader.
        assert len(results) == 1
        assert results[0].token.token_id == "token-stale-peer"

    def test_drain_ignores_callers_own_active_lease(self) -> None:
        """A caller's own unexpired lease must not trigger the peer precondition.

        ``peer_active_leases`` filters ``lease_owner != caller_owner``. A worker
        re-entering its own drain (e.g. after a transform that left its own row
        LEASED in an earlier iteration) must not self-block.
        """
        _db, factory = _make_factory()
        transform_node = NodeID("transform-1")
        factory.data_flow.register_node(
            plugin_name="resume-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id=str(transform_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_payload = make_row({"value": 1})
        row, token = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=source_payload.to_dict(),
            token_id="token-self-held",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.scheduler.enqueue_ready(
            token_id=token.token_id,
            row_id=row.row_id,
            node_id=str(transform_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(source_payload),
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        # The caller's lease_owner pre-claims the row before the drain entry.
        factory.scheduler.claim_ready(
            lease_owner=_TEST_LEADER_WORKER_ID,
            lease_seconds=600,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id=_TEST_LEADER_WORKER_ID),
        )

        worker_processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 1},
            node_to_next={NodeID("source-0"): transform_node, transform_node: None},
            node_to_plugin={transform_node: _make_mock_transform(node_id=str(transform_node), on_success="default")},
            scheduler=factory.scheduler,
            scheduler_lease_owner=_TEST_LEADER_WORKER_ID,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # peer_active_leases must return () because the only LEASED row is
        # owned by the caller; drain must not raise. It will reach the
        # claim_ready loop, find no READY work (the row is LEASED by self),
        # and exit cleanly with an empty result list.
        results = worker_processor.drain_scheduled_work(ctx)
        assert results == []


class TestFlushContextImmutability:
    """_FlushContext.buffered_tokens must be truly immutable."""

    def test_buffered_tokens_is_immutable(self) -> None:
        """buffered_tokens must not be mutable after construction.

        Bug: frozen=True prevents reassignment but list contents remain mutable.
        Fix: store as tuple instead of list.
        """
        token = make_token_info(data={"value": 1})
        original_list = [token]

        fctx = _FlushContext(
            node_id=NodeID("node-1"),
            transform=SimpleNamespace(node_id="node-1"),
            settings=AggregationSettings(
                name="agg",
                plugin="batch-plugin",
                input="source",
                on_error="discard",
                trigger={"count": 1},
            ),
            buffered_tokens=tuple(original_list),
            batch_id="batch-1",
            expand_parent_token=token,
            triggering_token=None,
            coalesce_node_id=None,
            coalesce_name=None,
        )

        # buffered_tokens must be a tuple (immutable), not a list
        assert isinstance(fctx.buffered_tokens, tuple)

        # Mutating the original list must not affect the frozen context
        original_list.append(make_token_info(data={"value": 2}))
        assert len(fctx.buffered_tokens) == 1


# =============================================================================
# F1 derivation-parity pins (Task 3.4 review follow-ups)
# =============================================================================


class TestReadyEmissionEnqueueParity:
    """Pin: the READY emission mirrors ``_enqueue_scheduler_work_item``.

    The merged-coalesce READY emission (inserted atomically by
    ``complete_barrier`` via ``_insert_ready_emission``) and the drain loop's
    idempotent ``enqueue_ready`` for the SAME WorkItem must derive identical
    field values: the enqueue reconciles against the emission-inserted row by
    deterministic ``work_item_id`` + strict field equality, so any derivation
    drift fails in production at reconciliation time. This pin makes that
    drift fail in CI instead.

    Both lanes now derive from ONE ``SchedulerWorkCodec`` (the codec's own
    round-trip invariants live in test_scheduler_work_codec.py); this test
    pins the processor-side WIRING end-to-end — that the enqueue path passes
    the codec bundle through unmodified — using the repository mapper
    (``_ready_work_item_values``, shared by ``enqueue_ready`` AND
    ``_insert_ready_emission``) as the common projection onto the full
    journal-row column set.
    """

    @pytest.mark.parametrize(
        "flavor",
        [
            # Maximal coalesce-cursor item: full fork/join/expand lineage +
            # coalesce fields set (queue_key derives None — queue blocking and
            # coalesce barriers are mutually exclusive by derivation).
            "coalesce_cursor",
            # Structural-queue item: full lineage, no coalesce fields, current
            # node structural (in node_to_next, no plugin) so queue_key derives
            # the node id.
            "structural_queue",
            # row_union-cursor item: the third barrier kind. Without a flavor
            # that sets row_union_name non-None, both derivations project the
            # column as NULL and the parity assertion passes while a drop site
            # ships (elspeth-a5b86149d4 remediation).
            "row_union_cursor",
            # collector-cursor item: the fourth barrier kind (WS4 Task 6).
            # Same "a NULL-vs-NULL match masks a drop site" risk the
            # row_union_cursor comment above names — without a flavor that
            # sets collector_name non-None, the parity assertion would pass
            # trivially even if _ready_work_item_values silently dropped it.
            "collector_cursor",
        ],
    )
    def test_ready_emission_mirrors_enqueue_work_item_fields(self, flavor: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _, factory = _make_factory()
        coalesce_node = NodeID("coalesce::merge")
        continue_node = NodeID("after-merge")
        processor = _make_processor(
            factory,
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            node_step_map={NodeID("source-0"): 0, coalesce_node: 1, continue_node: 2},
            node_to_next={NodeID("source-0"): coalesce_node, coalesce_node: continue_node, continue_node: None},
            coalesce_on_success_map={CoalesceName("merge"): "merged_sink"},
            # after-merge models a QUEUE node: structural by explicit
            # allowlist (production classifies queues by node type).
            structural_node_ids=frozenset({NodeID("source-0"), continue_node}),
        )

        # Lineage note: the audit trail enforces fork_group_id XOR
        # join_group_id at token creation, so the two flavors split the
        # lineage fields between them — together they pin every lineage
        # column non-None at least once.
        if flavor == "coalesce_cursor":
            token = TokenInfo(
                row_id="row-1",
                token_id="token-merged-1",
                row_data=make_pipeline_row({"value": 42}),
                lineage_path=(
                    LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),
                    LineageFrame(kind=FrameKind.EXPAND, group_id="expand-1", member_key="path_a"),
                ),
            )
            _persist_token_for_scheduler(factory, token)
            item = WorkItem(
                token=token,
                current_node_id=continue_node,
                coalesce_node_id=coalesce_node,
                coalesce_name=CoalesceName("merge"),
                on_success_sink="merged_sink",
            )
        elif flavor == "row_union_cursor":
            token = TokenInfo(
                row_id="row-1",
                token_id="token-merged-1",
                row_data=make_pipeline_row({"value": 42}),
                lineage_path=(
                    LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),
                    LineageFrame(kind=FrameKind.EXPAND, group_id="expand-1", member_key="path_a"),
                ),
            )
            _persist_token_for_scheduler(factory, token)
            item = WorkItem(
                token=token,
                current_node_id=continue_node,
                row_union_node_id=NodeID("row_union::variants"),
                row_union_name=RowUnionName("variants"),
                on_success_sink="merged_sink",
            )
        elif flavor == "collector_cursor":
            token = TokenInfo(
                row_id="row-1",
                token_id="token-merged-1",
                row_data=make_pipeline_row({"value": 42}),
                lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="expand-1", member_key="token-merged-1"),),
            )
            _persist_token_for_scheduler(factory, token)
            item = WorkItem(
                token=token,
                current_node_id=continue_node,
                collector_name=CollectorName("stitch"),
                on_success_sink="merged_sink",
            )
        else:
            token = TokenInfo(
                row_id="row-1",
                token_id="token-merged-1",
                row_data=make_pipeline_row({"value": 42}),
                lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="expand-1", member_key="token-merged-1"),),
            )
            _persist_token_for_scheduler(factory, token)
            item = WorkItem(
                token=token,
                current_node_id=continue_node,
                on_success_sink="merged_sink",
                join_group_id="join-1",
            )

        emission = processor._work_codec.ready_emission(item)

        captured: dict[str, Any] = {}
        real_enqueue = factory.scheduler.enqueue_ready

        def _capturing_enqueue(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return real_enqueue(**kwargs)

        monkeypatch.setattr(factory.scheduler, "enqueue_ready", _capturing_enqueue)
        scheduled = processor._enqueue_scheduler_work_item(item, {})
        assert captured, "enqueue_ready was not invoked"

        # Project BOTH derivations through the production journal-row mapper
        # (_ready_work_item_values — the single mapper used by enqueue_ready
        # AND complete_barrier's _insert_ready_emission) with a pinned clock,
        # then require strict equality across the FULL column set.
        pinned_now = datetime(2026, 1, 1, tzinfo=UTC)
        enqueue_kwargs = dict(captured)
        enqueue_kwargs["available_at"] = pinned_now
        enqueue_kwargs.setdefault("attempt", 1)
        # Project the admitted authority into the journal's run identity.
        enqueue_kwargs["run_id"] = enqueue_kwargs.pop("member_token").run_id
        values_from_enqueue = factory.scheduler._ready_work_item_values(**enqueue_kwargs)

        # Mirror _insert_ready_emission's emission -> values mapping exactly.
        values_from_emission = factory.scheduler._ready_work_item_values(
            run_id=processor.run_id,
            token_id=emission.token_id,
            row_id=emission.row_id,
            node_id=emission.node_id,
            step_index=emission.step_index,
            ingest_sequence=emission.ingest_sequence,
            row_payload_json=emission.row_payload_json,
            available_at=pinned_now,
            attempt=emission.attempt,
            queue_key=emission.queue_key,
            barrier_key=emission.barrier_key,
            on_success_sink=emission.on_success_sink,
            join_group_id=emission.join_group_id,
            lineage_path=emission.lineage_path,
            coalesce_node_id=emission.coalesce_node_id,
            coalesce_name=emission.coalesce_name,
            row_union_name=emission.row_union_name,
            collector_name=emission.collector_name,
        )

        # collector_name was excluded here until scheduler_drain.py's
        # enqueue_ready / enqueue_ready_claimed call sites forwarded
        # fields.collector_name — that forwarding landed at 02aa2ba6b (WS3).
        # Both sides are now real derivations; assert full equality across
        # the whole column set, collector_name included, so a future
        # regression on either side (emission or enqueue) fails here.
        assert values_from_emission == values_from_enqueue
        # Pin the projected column count: adding a journal column to ONE of
        # the two builders (or to the mapper) must force this pin to be
        # revisited rather than silently desync the reconciliation contract.
        # Epoch 35 flip: tri-column lineage retirement (31 -> 28).
        # WS4 Task 6: collector_name cursor column (28 -> 29).
        assert len(values_from_emission) == 29

        # Spot-check the per-flavor derived keys so a failure localizes.
        if flavor == "coalesce_cursor":
            assert values_from_emission["queue_key"] is None
            assert values_from_emission["barrier_key"] == "merge"
            assert values_from_emission["coalesce_node_id"] == str(coalesce_node)
            assert values_from_emission["coalesce_name"] == "merge"
            emitted_path = lineage_path_from_json(values_from_emission["lineage_path_json"])
            assert path_branch_name(emitted_path) == "path_a"
            assert path_fork_group_id(emitted_path) == "fork-1"
            assert values_from_emission["row_union_name"] is None
        elif flavor == "row_union_cursor":
            # A barrier-bound item never derives a structural queue key, and
            # the row_union name IS the durable barrier key.
            assert values_from_emission["queue_key"] is None
            assert values_from_emission["barrier_key"] == "variants"
            assert values_from_emission["row_union_name"] == "variants"
            assert values_from_emission["coalesce_node_id"] is None
            assert values_from_emission["coalesce_name"] is None
            emitted_path = lineage_path_from_json(values_from_emission["lineage_path_json"])
            assert path_branch_name(emitted_path) == "path_a"
            assert path_fork_group_id(emitted_path) == "fork-1"
        elif flavor == "collector_cursor":
            # A barrier-bound item never derives a structural queue key, and
            # the collector's durable barrier key is the COMPOUND address
            # (collector_barrier_key: one collector spans many concurrent
            # EXPAND groups) built from the cursor and the token's own
            # innermost EXPAND frame — the writer the WS3+WS4 integration
            # item 1 landed in _barrier_key_for_blocked_item.
            #
            # collector_name forwarding landed at 02aa2ba6b (WS3): both the
            # emission side (ready_emission/_ready_work_item_values) and the
            # enqueue side (scheduler_drain.py's enqueue_ready call) now
            # derive the real value — assert both, not just the emission
            # side, so a regression on either lane fails here.
            assert values_from_emission["queue_key"] is None
            assert values_from_emission["barrier_key"] == collector_barrier_key("stitch", "expand-1")
            assert values_from_emission["coalesce_node_id"] is None
            assert values_from_emission["coalesce_name"] is None
            assert values_from_emission["row_union_name"] is None
            assert values_from_emission["collector_name"] == "stitch"
            assert values_from_enqueue["collector_name"] == "stitch"
        else:
            assert values_from_emission["queue_key"] == str(continue_node)
            assert values_from_emission["barrier_key"] is None
            assert values_from_emission["coalesce_node_id"] is None
            assert values_from_emission["coalesce_name"] is None
            assert values_from_emission["row_union_name"] is None
            assert values_from_emission["join_group_id"] == "join-1"
        assert path_expand_group_id(lineage_path_from_json(values_from_emission["lineage_path_json"])) == "expand-1"
        assert values_from_emission["step_index"] == 2

        # And the live reconciliation accepted the enqueue against the same
        # deterministic work_item_id (no drift at the idempotent insert).
        assert scheduled.work_item_id == values_from_emission["work_item_id"]


class TestPendingSinkAttemptOffsetSinkStepScoping:
    """Pin: PENDING_SINK re-drive attempt offsets are SINK-step scoped.

    A re-driven PENDING_SINK token only ever writes ONE further node_state —
    the sink write. Its attempt offset must therefore derive from
    ``get_max_node_state_attempts(..., step_index=sink_step)``: attempts
    recorded at PRODUCER steps (e.g. a transform retried twice before the
    crash) must not inflate the sink-write attempt number.
    """

    def test_producer_attempts_do_not_inflate_pending_sink_redrive_offset(self) -> None:
        from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus

        _, factory = _make_factory()
        producer_node = NodeID("transform-1")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, producer_node: 1},
            node_to_next={NodeID("source-0"): producer_node, producer_node: None},
            barrier_restore=BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-resume-1",
                barrier_scalars=None,
                batch_id_remap={},
            ),
        )
        sink_step = processor.resolve_sink_step()
        assert sink_step == 2

        token = TokenInfo(
            row_id="row-1",
            token_id="token-redrive-1",
            row_data=make_pipeline_row({"value": 42}),
        )
        _persist_token_for_scheduler(factory, token)

        # Register a sink node so sink-step node_states satisfy the FK.
        factory.data_flow.register_node(
            plugin_name="collect-sink",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="sink-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # Audited history: the PRODUCER step retried twice (attempts 0..2)...
        for attempt in range(3):
            factory.execution.begin_node_state(
                token_id=token.token_id,
                node_id=str(producer_node),
                step_index=1,
                input_data={"value": 42},
                attempt=attempt,
                member_token=leader_coordination_token(factory, "test-run").membership,
            )
        # ...while the SINK step only opened attempt 0 (the crashed write).
        factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id="sink-1",
            step_index=sink_step,
            input_data={"value": 42},
            attempt=0,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )

        # Discriminator precondition: an UNSCOPED max over all steps WOULD
        # see the producer's attempt 2 and over-bump the offset to 3.
        unscoped = factory.execution.get_max_node_state_attempts("test-run", [token.token_id])
        assert unscoped[token.token_id] == 2

        now = datetime.now(UTC)
        scheduled = TokenWorkItem(
            work_item_id="wi-redrive-1",
            run_id="test-run",
            token_id=token.token_id,
            row_id=token.row_id,
            node_id=None,
            step_index=sink_step,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(token.row_data),
            status=TokenWorkStatus.PENDING_SINK,
            attempt=1,
            available_at=now,
            created_at=now,
            updated_at=now,
            pending_sink_name="default",
            pending_outcome=TerminalOutcome.SUCCESS.value,
            pending_path=TerminalPath.DEFAULT_FLOW.value,
        )

        result = processor._row_result_from_pending_sink(scheduled)

        # Sink-step scoped: max sink attempt 0 -> offset 1. NOT the
        # producer-inflated 3.
        assert result.token.resume_attempt_offset == 1
        assert result.token.resume_checkpoint_id == "ckpt-resume-1"
        assert result.scheduler_pending_sink is True
        assert result.sink_name == "default"
