"""BarrierIntakeCoordinator boundary tests (elspeth-e76a186916).

Barrier adoption used to be choreography spread across RowProcessor,
the scheduler repository, and the aggregation/coalesce executors, with the
crash-window ordering (open batch -> fenced adopt -> feed memory -> evaluate
trigger) preserved only by caller convention and docstring prose. The
coordinator owns that ordered sequence behind one intake contract that
returns typed dispositions.

These tests pin the boundary with hand-rolled recording fakes:

* the disposition taxonomy (held / terminal / pending-sink /
  ready-continuation / flush-fired);
* the ordering invariants — batch membership opens BEFORE the fenced
  adoption, executor memory is fed ONLY on the adopted=True arm, and the
  aggregation trigger is evaluated from the same intake step as the
  triggering arrival's adoption;
* fail-closed on orphan barrier keys.

Behavioral (repository-backed) coverage lives in
tests/unit/engine/test_adr030_slice3_intake.py and
tests/integration/pipeline/test_barrier_intake_dispositions.py — this file
is the coordinator-level contract net.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elspeth.contracts import TokenInfo, TransformProtocol
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import TerminalOutcome, TerminalPath, TriggerType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.results import RowResult
from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import CoalesceName, NodeID, RowUnionName
from elspeth.core.config import RowUnionSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.scheduler import BarrierRestoreReadModel
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository, token_from_journal_item
from elspeth.engine.barrier_coordination import (
    BarrierIntakeCoordinator,
    BarrierIntakeDispositionKind,
    BarrierJournalRestoreContext,
    BarrierRecoveryCoordinator,
    _LiveBarrierHold,
)
from elspeth.engine.clock import MockClock
from elspeth.engine.coalesce_executor import CoalesceExecutor, CoalesceOutcome
from elspeth.engine.row_union_executor import RowUnionExecutor, RowUnionOutcome
from elspeth.engine.work_items import WorkItem, WorkItemFactory

_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)
_NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
_AGG_NODE = NodeID("agg-node")
_COALESCE = CoalesceName("merge")


def _payload() -> str:
    return TokenSchedulerRepository.serialize_row_payload(PipelineRow({"id": 1}, _CONTRACT))


def _token(token_id: str = "tok-1", row_id: str = "row-1") -> TokenInfo:
    return TokenInfo(row_id=row_id, token_id=token_id, row_data=PipelineRow({"id": 1}, _CONTRACT))


def _blocked_row(
    *,
    barrier_key: str,
    token_id: str = "tok-1",
    row_id: str = "row-1",
    branch_name: str | None = None,
    adopted_epoch: int | None = None,
    blocked_at: datetime | None = _NOW,
) -> TokenWorkItem:
    return TokenWorkItem(
        work_item_id=f"wi-{token_id}",
        run_id="run-1",
        token_id=token_id,
        row_id=row_id,
        node_id=str(_AGG_NODE),
        step_index=1,
        ingest_sequence=1,
        row_payload_json=_payload(),
        status=TokenWorkStatus.BLOCKED,
        attempt=1,
        available_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
        barrier_key=barrier_key,
        barrier_blocked_at=blocked_at,
        barrier_adopted_epoch=adopted_epoch,
        branch_name=branch_name,
        coalesce_name=barrier_key if barrier_key == str(_COALESCE) else None,
        row_union_name=barrier_key if barrier_key == "variant_union" else None,
    )


class RecordingScheduler:
    """Scheduler fake recording the fenced-verb call sequence."""

    def __init__(self, *, pending: list[TokenWorkItem], adopted: bool = True, losses: list[object] | None = None) -> None:
        self.pending = pending
        self.adopted = adopted
        self.losses = losses or []
        self.calls: list[str] = []
        self.release_contexts: list[dict[str, object]] = []

    def list_pending_blocked_barrier_items(self, *, run_id: str) -> list[TokenWorkItem]:
        self.calls.append("list_pending")
        return list(self.pending)

    def adopt_blocked_barrier_item(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append("adopt")
        return SimpleNamespace(adopted=self.adopted)

    def mark_blocked_barrier_terminal(self, *, token_ids, release_context=None, **kwargs: object) -> int:
        self.calls.append("release")
        if release_context is not None:
            self.release_contexts.append(dict(release_context))
        return len(tuple(token_ids))

    def list_unadopted_coalesce_branch_losses(self, *, run_id: str) -> list[object]:
        return list(self.losses)

    def adopt_coalesce_branch_losses(self, **kwargs: object) -> None:
        self.calls.append("adopt_losses")


class RecordingAggregationExecutor:
    def __init__(self, *, should_flush: bool = False) -> None:
        self.should_flush = should_flush
        self.calls: list[str] = []
        self.accepted: list[TokenInfo] = []

    def open_batch_membership(self, node_id: NodeID) -> tuple[str, int]:
        self.calls.append("open_batch")
        return ("batch-1", 0)

    def accept_adopted_row(self, node_id: NodeID, token: TokenInfo, *, accept_time: float) -> None:
        self.calls.append("accept")
        self.accepted.append(token)

    def check_flush_status(self, node_id: NodeID) -> tuple[bool, TriggerType | None]:
        self.calls.append("check_flush")
        return (self.should_flush, TriggerType.COUNT if self.should_flush else None)


class RecordingCoalesceExecutor:
    def __init__(self, outcome: CoalesceOutcome) -> None:
        self.outcome = outcome
        self.accepted: list[str] = []

    def accept(self, *, token: TokenInfo, coalesce_name: str, arrival_time: float) -> CoalesceOutcome:
        self.accepted.append(token.token_id)
        return self.outcome

    def has_recorded_branch_loss(self, coalesce_name: str, row_id: str, branch_name: str) -> bool:
        return True

    def notify_branch_lost(self, **kwargs: object) -> CoalesceOutcome | None:
        return None


class RecordingRowUnionExecutor:
    def __init__(self, outcome: RowUnionOutcome | None) -> None:
        self.outcome = outcome
        self.notifications: list[dict[str, object]] = []

    def has_recorded_branch_loss(self, row_union_name: str, row_id: str, branch_name: str) -> bool:
        return False

    def notify_branch_lost(self, **kwargs: object) -> RowUnionOutcome | None:
        self.notifications.append(dict(kwargs))
        return self.outcome


_DEFAULT_NEXT_NODE = NodeID("after-merge")


class FakeNav:
    def __init__(self, *, next_node: NodeID | None = _DEFAULT_NEXT_NODE, transform: object | None = None) -> None:
        self.next_node = next_node
        self.transform = transform

    def resolve_plugin_for_node(self, node_id: NodeID) -> object | None:
        return self.transform

    def resolve_next_node(self, node_id: NodeID) -> NodeID | None:
        return self.next_node


def _batch_aware_transform() -> Mock:
    """Specced protocol mock — satisfies the runtime TransformProtocol check."""
    transform = Mock(spec=TransformProtocol)
    transform.is_batch_aware = True
    return transform


def _make_coordinator(
    *,
    scheduler: RecordingScheduler,
    aggregation_executor: RecordingAggregationExecutor | None = None,
    coalesce_executor: RecordingCoalesceExecutor | None = None,
    row_union_executor: RecordingRowUnionExecutor | None = None,
    nav: FakeNav | None = None,
    live_holds: dict[str, _LiveBarrierHold] | None = None,
    flush_calls: list[tuple[NodeID, TriggerType]] | None = None,
    fire_calls: list[dict[str, object]] | None = None,
) -> BarrierIntakeCoordinator:
    def _flush_batch(node_id: NodeID, transform: object, ctx: object, trigger_type: TriggerType):
        if flush_calls is not None:
            flush_calls.append((node_id, trigger_type))
        flush_token = _token(token_id="tok-flush", row_id="row-flush")
        result = RowResult(
            token=flush_token,
            final_data=flush_token.row_data,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="default",
        )
        return (result,), [WorkItem(token=flush_token, current_node_id=NodeID("after-merge"))]

    def _complete_coalesce_fire(**kwargs: object) -> None:
        if fire_calls is not None:
            fire_calls.append(dict(kwargs))

    def _terminal_coalesce_row_result(token: TokenInfo, coalesce_name: CoalesceName, *, context: str) -> RowResult:
        return RowResult(
            token=token,
            final_data=token.row_data,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.COALESCED,
            sink_name="merged_sink",
        )

    resolved_nav = nav or FakeNav(transform=_batch_aware_transform())
    restore_reads = SimpleNamespace(get_max_node_state_attempts=lambda run_id, token_ids: {})
    return BarrierIntakeCoordinator(
        run_id="run-1",
        scheduler=scheduler,
        data_flow=SimpleNamespace(record_token_outcome=lambda **kwargs: None),
        execution=SimpleNamespace(),
        barrier_restore_reads=restore_reads,
        aggregation_executor=aggregation_executor or RecordingAggregationExecutor(),
        coalesce_executor=coalesce_executor,
        nav=resolved_nav,
        work_items=WorkItemFactory(resolved_nav),
        clock=MockClock(start=100.0),
        aggregation_settings={_AGG_NODE: object()} if aggregation_executor is not None else {},
        coalesce_node_ids={_COALESCE: NodeID("coalesce-node")} if coalesce_executor is not None else {},
        coordination_token=SimpleNamespace(worker_id="leader-1", epoch=1),
        scheduler_lease_owner="leader-1",
        live_barrier_holds=live_holds if live_holds is not None else {},
        resume_checkpoint_id=None,
        flush_batch=_flush_batch,
        complete_coalesce_fire=_complete_coalesce_fire,
        terminal_coalesce_row_result=_terminal_coalesce_row_result,
        emit_token_completed=lambda token, *, outcome, path, sink_name=None: None,
        mark_coalesce_consumed_terminal=lambda *, coalesce_name, consumed_tokens: None,
        row_union_executor=row_union_executor,
        row_union_node_ids=({RowUnionName("variant_union"): NodeID("row_union::variant_union")} if row_union_executor is not None else {}),
        complete_row_union_fire=lambda **kwargs: None,
        released_row_union_items=lambda **kwargs: (),
    )


def _ctx() -> SimpleNamespace:
    return SimpleNamespace()


class TestAggregationIntakeOrdering:
    def test_held_arrival_opens_batch_before_adopt_and_accepts_after(self) -> None:
        row = _blocked_row(barrier_key=str(_AGG_NODE))
        # Share ONE call log between the scheduler and executor fakes so the
        # cross-object ordering (the invariant this ticket moves behind the
        # coordinator boundary) is directly assertable.
        combined: list[str] = []
        scheduler = RecordingScheduler(pending=[row])
        scheduler.calls = combined
        agg = RecordingAggregationExecutor(should_flush=False)
        agg.calls = combined
        holds = {row.token_id: _LiveBarrierHold(token=_token(), barrier_key=str(_AGG_NODE))}
        coordinator = _make_coordinator(scheduler=scheduler, aggregation_executor=agg, live_holds=holds)

        outcome = coordinator.run_intake_pass(_ctx())

        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.HELD]
        assert outcome.results == []
        assert outcome.child_items == []
        # Ordering by construction: open batch -> fenced adopt -> feed memory
        # -> trigger evaluation, all within one intake step.
        assert combined.index("open_batch") < combined.index("adopt") < combined.index("accept") < combined.index("check_flush")

    def test_idempotent_skip_arm_does_not_feed_memory(self) -> None:
        row = _blocked_row(barrier_key=str(_AGG_NODE))
        scheduler = RecordingScheduler(pending=[row], adopted=False)
        agg = RecordingAggregationExecutor()
        coordinator = _make_coordinator(scheduler=scheduler, aggregation_executor=agg)

        outcome = coordinator.run_intake_pass(_ctx())

        assert outcome.dispositions == ()
        assert "accept" not in agg.calls
        assert "check_flush" not in agg.calls

    def test_count_trigger_fires_flush_in_same_intake_step(self) -> None:
        row = _blocked_row(barrier_key=str(_AGG_NODE))
        scheduler = RecordingScheduler(pending=[row])
        agg = RecordingAggregationExecutor(should_flush=True)
        holds = {row.token_id: _LiveBarrierHold(token=_token(), barrier_key=str(_AGG_NODE))}
        flush_calls: list[tuple[NodeID, TriggerType]] = []
        coordinator = _make_coordinator(
            scheduler=scheduler,
            aggregation_executor=agg,
            live_holds=holds,
            flush_calls=flush_calls,
        )

        outcome = coordinator.run_intake_pass(_ctx())

        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.FLUSH_FIRED]
        assert flush_calls == [(_AGG_NODE, TriggerType.COUNT)]
        assert len(outcome.results) == 1
        assert len(outcome.child_items) == 1


class TestCoalesceIntakeTaxonomy:
    def test_held_arrival(self) -> None:
        row = _blocked_row(barrier_key=str(_COALESCE))
        scheduler = RecordingScheduler(pending=[row])
        coalesce = RecordingCoalesceExecutor(CoalesceOutcome(held=True))
        holds = {row.token_id: _LiveBarrierHold(token=_token(), barrier_key=str(_COALESCE))}
        coordinator = _make_coordinator(scheduler=scheduler, coalesce_executor=coalesce, live_holds=holds)

        outcome = coordinator.run_intake_pass(_ctx())

        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.HELD]
        assert coalesce.accepted == [row.token_id]

    def test_late_arrival_releases_row_and_returns_terminal(self) -> None:
        row = _blocked_row(barrier_key=str(_COALESCE))
        scheduler = RecordingScheduler(pending=[row])
        coalesce = RecordingCoalesceExecutor(
            CoalesceOutcome(
                held=False,
                failure_reason="late_arrival_after_merge",
                outcomes_recorded=True,
                late_arrival=True,
            )
        )
        holds = {row.token_id: _LiveBarrierHold(token=_token(), barrier_key=str(_COALESCE))}
        coordinator = _make_coordinator(scheduler=scheduler, coalesce_executor=coalesce, live_holds=holds)

        outcome = coordinator.run_intake_pass(_ctx())

        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.TERMINAL]
        assert len(outcome.results) == 1
        assert outcome.results[0].outcome is TerminalOutcome.FAILURE
        assert scheduler.release_contexts and scheduler.release_contexts[0]["late_arrival"] is True

    def test_nonterminal_merge_returns_ready_continuation(self) -> None:
        row = _blocked_row(barrier_key=str(_COALESCE))
        scheduler = RecordingScheduler(pending=[row])
        merged = _token(token_id="tok-merged", row_id="row-1")
        consumed = (_token(token_id="tok-a"), _token(token_id="tok-b"))
        coalesce = RecordingCoalesceExecutor(CoalesceOutcome(held=False, merged_token=merged, consumed_tokens=consumed))
        holds = {row.token_id: _LiveBarrierHold(token=_token(), barrier_key=str(_COALESCE))}
        fire_calls: list[dict[str, object]] = []
        coordinator = _make_coordinator(
            scheduler=scheduler,
            coalesce_executor=coalesce,
            live_holds=holds,
            fire_calls=fire_calls,
        )

        outcome = coordinator.run_intake_pass(_ctx())

        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.READY_CONTINUATION]
        assert len(outcome.child_items) == 1
        assert outcome.child_items[0].token.token_id == "tok-merged"
        assert len(fire_calls) == 1
        assert fire_calls[0]["merged_item"] is outcome.child_items[0]

    def test_terminal_merge_returns_pending_sink(self) -> None:
        row = _blocked_row(barrier_key=str(_COALESCE))
        scheduler = RecordingScheduler(pending=[row])
        merged = _token(token_id="tok-merged", row_id="row-1")
        coalesce = RecordingCoalesceExecutor(CoalesceOutcome(held=False, merged_token=merged, consumed_tokens=(_token(token_id="tok-a"),)))
        holds = {row.token_id: _LiveBarrierHold(token=_token(), barrier_key=str(_COALESCE))}
        fire_calls: list[dict[str, object]] = []
        coordinator = _make_coordinator(
            scheduler=scheduler,
            coalesce_executor=coalesce,
            live_holds=holds,
            nav=FakeNav(next_node=None),
            fire_calls=fire_calls,
        )

        outcome = coordinator.run_intake_pass(_ctx())

        assert [d.kind for d in outcome.dispositions] == [BarrierIntakeDispositionKind.PENDING_SINK]
        assert len(outcome.results) == 1
        assert outcome.results[0].scheduler_pending_sink is True
        assert outcome.child_items == []
        assert len(fire_calls) == 1 and "merged_sink_result" in fire_calls[0]


class TestIntakeFailClosed:
    def test_orphan_barrier_key_raises(self) -> None:
        row = _blocked_row(barrier_key="not-a-barrier")
        scheduler = RecordingScheduler(pending=[row])
        coordinator = _make_coordinator(
            scheduler=scheduler,
            aggregation_executor=RecordingAggregationExecutor(),
        )

        with pytest.raises(AuditIntegrityError, match="orphan barrier_key"):
            coordinator.run_intake_pass(_ctx())


class TestRowUnionLossReplay:
    def test_follower_loss_fails_leader_held_sibling(self) -> None:
        held = _token(token_id="held-token", row_id="row-1")
        loss = SimpleNamespace(
            loss_id="loss-1",
            coalesce_name="variant_union",
            row_id="row-1",
            branch_name="control",
            reason="error_routed",
        )
        scheduler = RecordingScheduler(pending=[], losses=[loss])
        row_union = RecordingRowUnionExecutor(
            RowUnionOutcome(
                held=False,
                consumed_tokens=(held,),
                failure_reason="row_union_branch_lost",
                row_union_name="variant_union",
                outcomes_recorded=True,
            )
        )
        coordinator = _make_coordinator(scheduler=scheduler, row_union_executor=row_union)

        outcome = coordinator.run_intake_pass(_ctx())

        assert row_union.notifications == [
            {
                "row_union_name": "variant_union",
                "row_id": "row-1",
                "lost_branch": "control",
                "reason": "error_routed",
            }
        ]
        assert [item.kind for item in outcome.dispositions] == [BarrierIntakeDispositionKind.TERMINAL]
        assert [result.token.token_id for result in outcome.results] == ["held-token"]


class TestRowUnionRecovery:
    def test_mixed_topology_scopes_loss_restore_read_to_configured_coalesces(self) -> None:
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = []
        scheduler.list_coalesce_branch_losses.return_value = []
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        coalesce = Mock(spec=CoalesceExecutor)
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()
        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=coalesce,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={_COALESCE: NodeID("coalesce-node")},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        scheduler.list_coalesce_branch_losses.assert_called_once_with(
            run_id="run-1",
            coalesce_names=frozenset({"merge"}),
        )

    def test_intake_pending_row_union_group_is_left_for_next_intake(self) -> None:
        row = _blocked_row(barrier_key="variant_union")
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-1",
                barrier_scalars=None,
                batch_id_remap={},
            )
        )

        reads.get_open_node_state_ids.assert_not_called()

    def test_adopted_holdless_row_is_reset_for_journal_first_intake(self) -> None:
        row = _blocked_row(barrier_key="variant_union", branch_name="control", adopted_epoch=1)
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        scheduler.list_coalesce_branch_losses.return_value = []
        scheduler.reset_adoption_marker_to_pending.return_value = 1
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset()
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        scheduler.reset_adoption_marker_to_pending.assert_called_once_with(
            work_item_ids=[row.work_item_id],
            run_id="run-1",
        )
        row_union.restore_from_journal.assert_called_once_with(entries=[])

    def test_failed_closure_holdless_group_resets_to_intake_instead_of_release_reconcile(self) -> None:
        # Crash window: _fail_pending committed FAILED node states (which have
        # completed_at) but the BLOCKED scheduler rows were never terminalized.
        # These holdless rows must NOT classify as a released group —
        # reconcile_released_group would refuse them and wedge the resume.
        rows = [
            _blocked_row(barrier_key="variant_union", token_id="tok-control", branch_name="control", adopted_epoch=1),
            _blocked_row(barrier_key="variant_union", token_id="tok-treatment", branch_name="treatment", adopted_epoch=1),
        ]
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = rows
        scheduler.list_coalesce_branch_losses.return_value = []
        scheduler.reset_adoption_marker_to_pending.return_value = 2
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 1 for row in rows}
        reads.get_open_node_state_ids.return_value = {}
        # FAILED closures are completed_at-stamped but not released.
        reads.get_released_row_ids_for_nodes.return_value = frozenset()
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        row_union.reconcile_released_group.assert_not_called()
        scheduler.reset_adoption_marker_to_pending.assert_called_once_with(
            work_item_ids=[row.work_item_id for row in rows],
            run_id="run-1",
        )
        row_union.restore_from_journal.assert_called_once_with(entries=[])

    def test_late_arrival_residual_is_journal_released_not_replayed_as_group(self) -> None:
        # Crash window: a surplus branch token failed late after its group
        # released (_fail_late_arrival committed the FAILED node state and the
        # terminal FAILURE/UNROUTED outcome) but the process died before
        # mark_blocked_barrier_terminal. Release evidence for the shared row id
        # must NOT reconstruct the lone residual as the original group —
        # reconcile_released_group would refuse the incomplete branch set and
        # wedge the resume. The residual's audit trail is already terminal;
        # only its BLOCKED journal row still needs releasing.
        row = _blocked_row(barrier_key="variant_union", token_id="tok-late", branch_name="control", adopted_epoch=1)
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        scheduler.list_coalesce_branch_losses.return_value = []
        scheduler.mark_blocked_barrier_terminal.return_value = 1
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 1}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset({("row_union::variant_union", "row-1")})
        reads.find_released_node_state_token_ids.return_value = frozenset()
        reads.find_failed_unrouted_terminal_token_ids.return_value = frozenset({"tok-late"})
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        row_union.reconcile_released_group.assert_not_called()
        scheduler.reset_adoption_marker_to_pending.assert_not_called()
        scheduler.mark_blocked_barrier_terminal.assert_called_once()
        call = scheduler.mark_blocked_barrier_terminal.call_args
        assert call.kwargs["barrier_key"] == "variant_union"
        assert call.kwargs["token_ids"] == ("tok-late",)
        assert call.kwargs["release_context"]["late_arrival"] is True
        assert call.kwargs["release_context"]["restore_reconcile"] is True
        assert call.kwargs["release_context"]["scope_row_id"] == "row-1"
        row_union.restore_from_journal.assert_called_once_with(entries=[])

    def test_late_arrival_residual_without_terminal_outcome_resets_to_intake(self) -> None:
        # Narrower slice of the same window: the FAILED node state committed
        # but the process died before record_token_outcome (or adoption
        # committed and accept() never ran at all). The token holds no
        # COMPLETED state so it is not a member of the released group, and its
        # audit trail is incomplete, so the journal row must NOT be
        # terminalized here — reset it to intake-pending and let the live
        # late-arrival arm replay state + outcome + release on re-accept.
        row = _blocked_row(barrier_key="variant_union", token_id="tok-late", branch_name="control", adopted_epoch=1)
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        scheduler.list_coalesce_branch_losses.return_value = []
        scheduler.reset_adoption_marker_to_pending.return_value = 1
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 1}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset({("row_union::variant_union", "row-1")})
        reads.find_released_node_state_token_ids.return_value = frozenset()
        reads.find_failed_unrouted_terminal_token_ids.return_value = frozenset()
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        row_union.reconcile_released_group.assert_not_called()
        scheduler.mark_blocked_barrier_terminal.assert_not_called()
        scheduler.reset_adoption_marker_to_pending.assert_called_once_with(
            work_item_ids=[row.work_item_id],
            run_id="run-1",
        )
        row_union.restore_from_journal.assert_called_once_with(entries=[])

    def test_residual_partition_preserves_genuine_released_group_reconcile(self) -> None:
        # A genuine post-release crash group (row-1: every branch row is still
        # journalled because complete_barrier never committed) and a stranded
        # late-arrival residual (row-2) restored together: the group must
        # reconcile exactly as before and the residual must be journal-released
        # without contaminating the group's entries.
        group_rows = [
            _blocked_row(barrier_key="variant_union", token_id="tok-control", branch_name="control", adopted_epoch=1),
            _blocked_row(barrier_key="variant_union", token_id="tok-treatment", branch_name="treatment", adopted_epoch=1),
        ]
        residual = _blocked_row(
            barrier_key="variant_union",
            token_id="tok-late",
            row_id="row-2",
            branch_name="control",
            adopted_epoch=1,
        )
        rows = [*group_rows, residual]
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = rows
        scheduler.list_coalesce_branch_losses.return_value = []
        scheduler.mark_blocked_barrier_terminal.return_value = 1
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0 for row in rows}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset(
            {("row_union::variant_union", "row-1"), ("row_union::variant_union", "row-2")}
        )
        reads.find_released_node_state_token_ids.return_value = frozenset({"tok-control", "tok-treatment"})
        reads.find_failed_unrouted_terminal_token_ids.return_value = frozenset({"tok-late"})
        row_union = Mock(spec=RowUnionExecutor)
        restored_tokens = tuple(token_from_journal_item(row, attempt_offset=1, resume_checkpoint_id="ckpt-1") for row in group_rows)
        row_union.reconcile_released_group.return_value = RowUnionOutcome(
            held=False,
            released_tokens=restored_tokens,
            consumed_tokens=restored_tokens,
            row_union_name="variant_union",
        )
        row_union.restore_from_journal.return_value = ()
        completions: list[dict[str, object]] = []

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
            released_row_union_items=lambda **kwargs: (),
            complete_row_union_fire=lambda **kwargs: completions.append(dict(kwargs)),
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        row_union.reconcile_released_group.assert_called_once()
        entries = row_union.reconcile_released_group.call_args.kwargs["entries"]
        assert {entry.token.token_id for entry in entries} == {"tok-control", "tok-treatment"}
        scheduler.mark_blocked_barrier_terminal.assert_called_once()
        assert scheduler.mark_blocked_barrier_terminal.call_args.kwargs["token_ids"] == ("tok-late",)
        assert scheduler.mark_blocked_barrier_terminal.call_args.kwargs["release_context"]["scope_row_id"] == "row-2"
        scheduler.reset_adoption_marker_to_pending.assert_not_called()
        assert len(completions) == 1
        assert completions[0]["consumed_tokens"] == restored_tokens

    def test_residual_membership_is_scoped_to_its_own_union_node(self) -> None:
        # Chained unions: released tokens keep their token ids downstream, so
        # a token that released at an upstream union holds a COMPLETED state
        # there. When it strands as a late-arrival residual at a DOWNSTREAM
        # union, membership classification must consult only the downstream
        # union's node — the upstream state must not promote the residual
        # into the released group.
        row = _blocked_row(barrier_key="union_b", token_id="tok-chained", branch_name="control", adopted_epoch=1)
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        scheduler.list_coalesce_branch_losses.return_value = []
        scheduler.mark_blocked_barrier_terminal.return_value = 1
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 1}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset({("row_union::union_b", "row-1")})

        def _members_at(run_id: str, *, node_ids: list[str], token_ids: list[str]) -> frozenset[str]:
            # tok-chained's COMPLETED state lives at upstream union A only.
            return frozenset({"tok-chained"}) if "row_union::union_a" in node_ids else frozenset()

        reads.find_released_node_state_token_ids.side_effect = _members_at
        reads.find_failed_unrouted_terminal_token_ids.return_value = frozenset({"tok-chained"})
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={
                RowUnionName("union_a"): NodeID("row_union::union_a"),
                RowUnionName("union_b"): NodeID("row_union::union_b"),
            },
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        reads.find_released_node_state_token_ids.assert_called_once()
        assert reads.find_released_node_state_token_ids.call_args.kwargs["node_ids"] == ["row_union::union_b"]
        row_union.reconcile_released_group.assert_not_called()
        scheduler.mark_blocked_barrier_terminal.assert_called_once()
        assert scheduler.mark_blocked_barrier_terminal.call_args.kwargs["barrier_key"] == "union_b"

    def test_post_release_crash_reconciles_completed_group_and_continuation(self) -> None:
        rows = [
            _blocked_row(barrier_key="variant_union", token_id="tok-control", branch_name="control", adopted_epoch=1),
            _blocked_row(barrier_key="variant_union", token_id="tok-treatment", branch_name="treatment", adopted_epoch=1),
        ]
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = rows
        scheduler.list_coalesce_branch_losses.return_value = []
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0 for row in rows}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset({("row_union::variant_union", "row-1")})
        reads.find_released_node_state_token_ids.return_value = frozenset({"tok-control", "tok-treatment"})
        row_union = Mock(spec=RowUnionExecutor)
        restored_tokens = tuple(token_from_journal_item(row, attempt_offset=1, resume_checkpoint_id="ckpt-1") for row in rows)
        row_union.reconcile_released_group.return_value = RowUnionOutcome(
            held=False,
            released_tokens=restored_tokens,
            consumed_tokens=restored_tokens,
            row_union_name="variant_union",
        )
        row_union.restore_from_journal.return_value = ()
        completions: list[dict[str, object]] = []

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
            released_row_union_items=lambda **kwargs: (),
            complete_row_union_fire=lambda **kwargs: completions.append(dict(kwargs)),
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        row_union.reconcile_released_group.assert_called_once()
        assert len(completions) == 1
        assert completions[0]["consumed_tokens"] == restored_tokens

    def test_released_group_item_with_null_barrier_blocked_at_raises(self) -> None:
        # The sibling restore loop below (and journal_restore / barrier.py)
        # refuse NULL barrier_blocked_at on the same journal-row shape; the
        # released-group reconcile loop must not silently substitute "now".
        rows = [
            _blocked_row(
                barrier_key="variant_union",
                token_id="tok-control",
                branch_name="control",
                adopted_epoch=1,
                blocked_at=None,
            ),
            _blocked_row(barrier_key="variant_union", token_id="tok-treatment", branch_name="treatment", adopted_epoch=1),
        ]
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = rows
        scheduler.list_coalesce_branch_losses.return_value = []
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0 for row in rows}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset({("row_union::variant_union", "row-1")})
        reads.find_released_node_state_token_ids.return_value = frozenset({"tok-control", "tok-treatment"})
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
            released_row_union_items=lambda **kwargs: (),
            complete_row_union_fire=lambda **kwargs: None,
        )

        with pytest.raises(AuditIntegrityError, match="NULL barrier_blocked_at"):
            coordinator.restore_from_journal(
                BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
            )

    def test_released_group_reconciles_without_preloading_the_loss_ledger(self) -> None:
        # Durable release evidence is authoritative during recovery. The
        # coordinator must reconcile it without loading the append-only loss
        # history into the executor at all.
        rows = [
            _blocked_row(barrier_key="variant_union", token_id="tok-control", branch_name="control", adopted_epoch=1),
            _blocked_row(barrier_key="variant_union", token_id="tok-treatment", branch_name="treatment", adopted_epoch=1),
        ]
        loss = SimpleNamespace(
            coalesce_name="variant_union",
            row_id="row-1",
            branch_name="treatment",
            reason="error_routed",
        )
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = rows
        scheduler.list_coalesce_branch_losses.return_value = [loss]
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0 for row in rows}
        reads.get_open_node_state_ids.return_value = {}
        reads.get_released_row_ids_for_nodes.return_value = frozenset({("row_union::variant_union", "row-1")})
        reads.find_released_node_state_token_ids.return_value = frozenset({"tok-control", "tok-treatment"})
        row_union = RowUnionExecutor(
            Mock(spec=ExecutionRepository),
            object(),
            "run-1",
            step_resolver=lambda node_id: 5,
            clock=MockClock(start=100.0),
            data_flow=Mock(spec=DataFlowRepository),
            barrier_restore_reads=reads,
        )
        row_union.register_row_union(
            RowUnionSettings(name="variant_union", branches=["control", "treatment"], on_success="union_out"),
            NodeID("row_union::variant_union"),
        )
        completions: list[dict[str, object]] = []

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
            released_row_union_items=lambda **kwargs: (),
            complete_row_union_fire=lambda **kwargs: completions.append(dict(kwargs)),
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        scheduler.list_coalesce_branch_losses.assert_not_called()
        assert row_union.has_recorded_branch_loss("variant_union", "row-1", "treatment") is False
        assert len(completions) == 1
        assert {token.token_id for token in completions[0]["consumed_tokens"]} == {"tok-control", "tok-treatment"}

    def test_durable_loss_fails_restored_sibling_and_emits_completion(self) -> None:
        row = _blocked_row(barrier_key="variant_union", branch_name="control", adopted_epoch=1)
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0}
        reads.get_open_node_state_ids.return_value = {row.token_id: "state-1"}
        reads.has_branch_loss_for_group.return_value = True
        execution = Mock(spec=ExecutionRepository)
        data_flow = Mock(spec=DataFlowRepository)
        row_union = RowUnionExecutor(
            execution,
            object(),
            "run-1",
            step_resolver=lambda node_id: 5,
            clock=MockClock(start=100.0),
            data_flow=data_flow,
            barrier_restore_reads=reads,
        )
        row_union.register_row_union(
            RowUnionSettings(name="variant_union", branches=["control", "treatment"], on_success="union_out"),
            NodeID("row_union::variant_union"),
        )
        completions: list[dict[str, object]] = []
        emitted: list[tuple[str, TerminalOutcome | None, TerminalPath]] = []

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=execution,
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
            complete_row_union_fire=lambda **kwargs: completions.append(dict(kwargs)),
            emit_token_completed=lambda token, *, outcome, path: emitted.append((token.token_id, outcome, path)),
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(resume_checkpoint_id="ckpt-1", barrier_scalars=None, batch_id_remap={})
        )

        scheduler.list_coalesce_branch_losses.assert_not_called()
        reads.has_branch_loss_for_group.assert_called_once_with(
            run_id="run-1",
            barrier_name="variant_union",
            row_id="row-1",
        )
        assert len(completions) == 1
        assert emitted == [(row.token_id, TerminalOutcome.FAILURE, TerminalPath.UNROUTED)]

    def test_adopted_partial_group_restores_executor_memory(self) -> None:
        row = _blocked_row(
            barrier_key="variant_union",
            branch_name="control",
            adopted_epoch=1,
        )
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = [row]
        scheduler.list_coalesce_branch_losses.return_value = []
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0}
        reads.get_open_node_state_ids.return_value = {row.token_id: "state-1"}
        row_union = Mock(spec=RowUnionExecutor)
        row_union.restore_from_journal.return_value = ()

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-1",
                barrier_scalars=None,
                batch_id_remap={},
            )
        )

        row_union.restore_from_journal.assert_called_once()
        entries = row_union.restore_from_journal.call_args.kwargs["entries"]
        assert len(entries) == 1
        assert entries[0].token.token_id == row.token_id
        assert entries[0].token.resume_attempt_offset == 1
        assert entries[0].state_id == "state-1"
        scheduler.list_coalesce_branch_losses.assert_not_called()
        row_union.restore_branch_losses.assert_not_called()

    def test_fully_adopted_group_commits_released_continuations(self) -> None:
        rows = [
            _blocked_row(
                barrier_key="variant_union",
                token_id="tok-control",
                branch_name="control",
                adopted_epoch=1,
            ),
            _blocked_row(
                barrier_key="variant_union",
                token_id="tok-treatment",
                branch_name="treatment",
                adopted_epoch=1,
            ),
        ]
        scheduler = Mock(spec=TokenSchedulerRepository)
        scheduler.list_blocked_barrier_items.return_value = rows
        scheduler.list_coalesce_branch_losses.return_value = []
        reads = Mock(spec=BarrierRestoreReadModel)
        reads.find_duplicate_live_buffered_acceptances.return_value = []
        reads.get_max_node_state_attempts.return_value = {row.token_id: 0 for row in rows}
        reads.get_open_node_state_ids.return_value = {row.token_id: f"state-{row.token_id}" for row in rows}
        row_union = Mock(spec=RowUnionExecutor)
        restored_tokens = tuple(token_from_journal_item(row, attempt_offset=1, resume_checkpoint_id="ckpt-1") for row in rows)
        row_union.restore_from_journal.return_value = (
            RowUnionOutcome(
                held=False,
                released_tokens=restored_tokens,
                consumed_tokens=restored_tokens,
                row_union_name="variant_union",
            ),
        )
        releases: list[tuple[str, ...]] = []
        completions: list[dict[str, object]] = []

        coordinator = BarrierRecoveryCoordinator(
            run_id="run-1",
            scheduler=scheduler,
            barrier_restore_reads=reads,
            execution=Mock(spec=ExecutionRepository),
            aggregation_executor=RecordingAggregationExecutor(),
            coalesce_executor=None,
            clock=MockClock(start=100.0),
            aggregation_settings={},
            coalesce_node_ids={},
            coordination_token=CoordinationToken(run_id="run-1", worker_id="worker-1", leader_epoch=1),
            scheduler_lease_owner="worker-1",
            row_union_executor=row_union,
            row_union_node_ids={RowUnionName("variant_union"): NodeID("row_union::variant_union")},
            released_row_union_items=lambda *, row_union_name, released_tokens: (
                releases.append(tuple(token.token_id for token in released_tokens)) or ()
            ),
            complete_row_union_fire=lambda **kwargs: completions.append(dict(kwargs)),
        )

        coordinator.restore_from_journal(
            BarrierJournalRestoreContext(
                resume_checkpoint_id="ckpt-1",
                barrier_scalars=None,
                batch_id_remap={},
            )
        )

        assert releases == [("tok-control", "tok-treatment")]
        assert len(completions) == 1
        assert completions[0]["consumed_tokens"] == restored_tokens
