"""Settle-member seam, CloserKind.COLLECTOR dispatch (WS3+WS4 integration item 14).

Two layers. The SEAM (`RowProcessor._record_group_member_terminals`) against
a real Landscape store: the collector's EXPAND anchor/pop, the survivor
(SUCCESS, COALESCED) write, the failure (FAILURE, UNROUTED) write plus ONE
escalation walk over the members' shared remaining lineage, and the store's
own double-write detection — the mechanism the intake's missing-terminal
skip is load-bearing against. The DISPATCH
(`BarrierIntakeCoordinator._dispose_collector_outcome` /
`_replay_group_losses`) with recording doubles: which members reach the
seam with which kwargs, and what rides the fire / release transactions.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts import RowResult, TokenInfo
from elspeth.contracts.enums import FrameKind, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.contracts.types import CoalesceName, CollectorName, NodeID
from elspeth.core.dag.group_bindings import GroupBinding
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.engine.barrier_coordination import BarrierIntakeCoordinator, BarrierIntakeDispositionKind
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.collector import CollectorOutcome
from elspeth.engine.processor import CollectorRelease
from elspeth.engine.work_items import WorkItem, WorkItemFactory
from elspeth.testing import make_token_info
from tests.unit.engine.test_barrier_coordination import (
    FakeNav,
    RecordingAggregationExecutor,
    RecordingScheduler,
    _batch_aware_transform,
)
from tests.unit.engine.test_processor import _make_factory, _make_processor, _persist_token_for_scheduler
from tests.unit.engine.test_settle_member_seam import _StubGroupBindingRegistry, coalesce_binding

OUTER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg_outer", member_key="left")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="m")


def _member(token_id: str, *, path: tuple[LineageFrame, ...] = (OUTER_FORK, EXPAND)) -> TokenInfo:
    return make_token_info(row_id="row-1", token_id=token_id, lineage_path=path)


def _seam_processor(*, bindings: dict[tuple[str, str], GroupBinding] | None = None) -> tuple[Any, Any]:
    _db, factory = _make_factory()
    registry = _StubGroupBindingRegistry(bindings=dict(bindings or {}))
    coalesce_node_ids = {CoalesceName(b.closer_name): b.closer_node_id for b in (bindings or {}).values()}
    return factory, _make_processor(factory, group_bindings=registry, coalesce_node_ids=coalesce_node_ids)


# ---------------------------------------------------------------------------
# The seam against a real store
# ---------------------------------------------------------------------------


class TestSeamCollectorArm:
    def test_survivors_record_success_coalesced_with_no_escalation_walk(self) -> None:
        factory, proc = _seam_processor()
        members = (_member("tok-a"), _member("tok-b"))
        for token in members:
            _persist_token_for_scheduler(factory, token)

        with patch.object(proc, "_settle_member_losses", return_value=[]) as walk:
            cascaded = proc._record_group_member_terminals(
                members,
                failure_reason="",
                child_items=[],
                group_failed=False,
                frame_kind=FrameKind.EXPAND,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.COALESCED,
            )

        assert cascaded == []
        walk.assert_not_called()
        for token in members:
            recorded = factory.data_flow.get_token_outcome(token.token_id)
            assert recorded is not None
            assert (recorded.completed, recorded.outcome, recorded.path, recorded.sink_name, recorded.error_hash) == (
                True,
                TerminalOutcome.SUCCESS,
                TerminalPath.COALESCED,
                None,
                None,
            )

    def test_failed_group_records_failure_unrouted_and_walks_the_shared_remaining_lineage_once(self) -> None:
        factory, proc = _seam_processor()
        members = (_member("tok-a"), _member("tok-b"))
        for token in members:
            _persist_token_for_scheduler(factory, token)

        with patch.object(proc, "_settle_member_losses", return_value=[]) as walk:
            proc._record_group_member_terminals(
                members,
                failure_reason="collector_missing_members",
                child_items=[],
                group_failed=True,
                frame_kind=FrameKind.EXPAND,
            )

        assert walk.call_count == 1
        (remaining_token, reason, _child_items), kwargs = walk.call_args
        assert remaining_token.lineage_path == (OUTER_FORK,)
        assert reason == "collector_missing_members"
        assert kwargs == {"escalated": True}
        for token in members:
            recorded = factory.data_flow.get_token_outcome(token.token_id)
            assert recorded is not None
            assert (recorded.outcome, recorded.path) == (TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
            assert recorded.error_hash is not None

    def test_failed_group_escalates_a_group_failed_loss_against_the_enclosing_bound_frame(self) -> None:
        """The walk for real: the members' remaining lineage is an OUTER fork
        frame bound to a coalesce, so exactly one `group_failed` loss is
        staged for it (spec §6.3 item 5)."""
        factory, proc = _seam_processor(bindings={("fg_outer", "left"): coalesce_binding("merge_outer", member_key="left")})
        members = (_member("tok-a"), _member("tok-b"))
        for token in members:
            _persist_token_for_scheduler(factory, token)

        with patch.object(proc, "_resolve_member_token_id", return_value="tok-outer-left"):
            proc._record_group_member_terminals(
                members,
                failure_reason="collector_missing_members",
                child_items=[],
                group_failed=True,
                frame_kind=FrameKind.EXPAND,
            )

        assert proc._take_pending_group_losses() == (
            GroupLossSpec(
                closer_name="merge_outer", group_id="fg_outer", member_key="left", token_id="tok-outer-left", reason="group_failed"
            ),
        )

    @pytest.mark.parametrize(
        ("frame_kind", "innermost"),
        [
            pytest.param(FrameKind.EXPAND, OUTER_FORK, id="expand-kind-fork-anchor"),
            pytest.param(FrameKind.FORK, EXPAND, id="fork-kind-expand-anchor"),
        ],
    )
    def test_anchor_kind_must_match_the_closer_kind(self, frame_kind: FrameKind, innermost: LineageFrame) -> None:
        _factory, proc = _seam_processor()
        token = _member("tok-a", path=(innermost,))
        with pytest.raises(OrchestrationInvariantError, match=rf"has no innermost {frame_kind.name} frame"):
            proc._record_group_member_terminals((token,), failure_reason="x", child_items=[], group_failed=False, frame_kind=frame_kind)

    def test_the_store_rejects_a_second_terminal_for_the_same_member(self) -> None:
        """Why the intake filters already-terminal members BEFORE the seam:
        a member the executor quarantined already holds its terminal, and
        the store's uniqueness index — not the seam — is what refuses a
        second one."""
        factory, proc = _seam_processor()
        token = _member("tok-a")
        _persist_token_for_scheduler(factory, token)
        write: dict[str, Any] = {"failure_reason": "", "child_items": [], "group_failed": False, "frame_kind": FrameKind.EXPAND}
        proc._record_group_member_terminals((token,), outcome=TerminalOutcome.SUCCESS, path=TerminalPath.COALESCED, **write)
        with pytest.raises(LandscapeRecordError):
            proc._record_group_member_terminals((token,), outcome=TerminalOutcome.SUCCESS, path=TerminalPath.COALESCED, **write)


# ---------------------------------------------------------------------------
# The dispatch with recording doubles
# ---------------------------------------------------------------------------


class _RecordingDataFlow:
    def __init__(self, *, terminal_token_ids: frozenset[str] = frozenset()) -> None:
        self.terminal_token_ids = terminal_token_ids

    def get_token_outcome(self, token_id: str) -> SimpleNamespace | None:
        if token_id in self.terminal_token_ids:
            return SimpleNamespace(completed=True)
        return None

    def record_token_outcome(self, **kwargs: object) -> None:
        raise AssertionError("the intake never writes a terminal directly")


class _LossReplayExecutor:
    def __init__(self, *, outcome: CollectorOutcome | None, replayed: frozenset[tuple[str, str, str]] = frozenset()) -> None:
        self.outcome = outcome
        self.replayed = replayed
        self.notified: list[tuple[str, str, str, str, object]] = []

    def has_replayed_member_loss(self, collector_name: str, group_id: str, member_key: str) -> bool:
        return (collector_name, group_id, member_key) in self.replayed

    def notify_member_lost(self, collector_name: str, group_id: str, member_key: str, reason: str, ctx: object) -> CollectorOutcome | None:
        self.notified.append((collector_name, group_id, member_key, reason, ctx))
        return self.outcome

    def accept(self, *args: object, **kwargs: object) -> CollectorOutcome:
        raise AssertionError("not reached")


class _Recorder:
    def __init__(self, *, release: CollectorRelease) -> None:
        self.release = release
        self.seam_calls: list[dict[str, object]] = []
        self.fire_calls: list[dict[str, object]] = []
        self.route_calls: list[dict[str, object]] = []
        self.completed: list[tuple[str, TerminalOutcome, TerminalPath]] = []
        self.staged: list[GroupLossSpec] = []

    def record_group_member_terminals(self, consumed_tokens: tuple[TokenInfo, ...], **kwargs: object) -> list[RowResult]:
        self.seam_calls.append({"consumed": tuple(t.token_id for t in consumed_tokens), **kwargs})
        return []

    def complete_collector_fire(self, **kwargs: object) -> None:
        self.fire_calls.append(dict(kwargs))

    def route_collector_release(self, **kwargs: object) -> CollectorRelease:
        self.route_calls.append(dict(kwargs))
        return self.release

    def emit_token_completed(self, token: TokenInfo, *, outcome: TerminalOutcome, path: TerminalPath, sink_name: str | None = None) -> None:
        self.completed.append((token.token_id, outcome, path))

    def take_pending_group_losses(self) -> tuple[GroupLossSpec, ...]:
        staged = tuple(self.staged)
        self.staged.clear()
        return staged


def _coordinator(
    *,
    scheduler: RecordingScheduler,
    executor: object,
    recorder: _Recorder,
    data_flow: _RecordingDataFlow,
) -> BarrierIntakeCoordinator:
    nav = FakeNav(transform=_batch_aware_transform())
    return BarrierIntakeCoordinator(
        run_id="run-1",
        scheduler=scheduler,
        data_flow=data_flow,  # type: ignore[arg-type]
        execution=SimpleNamespace(),
        barrier_restore_reads=SimpleNamespace(
            get_max_node_state_attempts=lambda run_id, token_ids: {},
            row_id_for_token=lambda run_id, token_id: "row-1",
        ),
        aggregation_executor=RecordingAggregationExecutor(),
        coalesce_executor=None,
        nav=nav,
        work_items=WorkItemFactory(nav),
        clock=MockClock(start=100.0),
        aggregation_settings={},
        coalesce_node_ids={},
        branch_to_coalesce={},
        coordination_token=SimpleNamespace(worker_id="leader-1", epoch=1),
        scheduler_lease_owner="leader-1",
        live_barrier_holds={},
        resume_checkpoint_id=None,
        flush_batch=lambda node_id, transform, ctx, trigger_type: ((), []),
        complete_coalesce_fire=lambda **kwargs: None,
        terminal_coalesce_row_result=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("not reached")),
        emit_token_completed=recorder.emit_token_completed,
        mark_coalesce_consumed_terminal=lambda *, coalesce_name, consumed_tokens: None,
        record_group_member_terminals=recorder.record_group_member_terminals,
        take_pending_group_losses=recorder.take_pending_group_losses,
        collector_executor=executor,  # type: ignore[arg-type]
        collector_node_ids={CollectorName("stitch"): NodeID("collector-node")},
        complete_collector_fire=recorder.complete_collector_fire,
        route_collector_release=recorder.route_collector_release,
    )


def _released(*members: TokenInfo, released: tuple[TokenInfo, ...] = ()) -> CollectorOutcome:
    return CollectorOutcome(held=False, released_tokens=released, consumed_tokens=members, collector_name="stitch", group_id="eg-1")


class TestDispatchRelease:
    def test_only_unterminalized_members_reach_the_seam_and_the_fire_rides_after(self) -> None:
        survivors = (_member("tok-a"), _member("tok-b"))
        quarantined = _member("tok-q")
        released_token = make_token_info(row_id="row-1", token_id="tok-out", lineage_path=(OUTER_FORK,))
        continuation = WorkItem(token=released_token, current_node_id=NodeID("after"))
        recorder = _Recorder(release=CollectorRelease(items=(continuation,), sink_results=()))
        recorder.staged.append(
            GroupLossSpec(closer_name="merge_outer", group_id="fg_outer", member_key="left", token_id="t", reason="group_failed")
        )
        coordinator = _coordinator(
            scheduler=RecordingScheduler(pending=[]),
            executor=object(),
            recorder=recorder,
            data_flow=_RecordingDataFlow(terminal_token_ids=frozenset({"tok-q"})),
        )

        disposition = coordinator._dispose_collector_outcome(
            _released(survivors[0], quarantined, survivors[1], released=(released_token,)), scope_row_id="row-1"
        )

        assert recorder.seam_calls == [
            {
                "consumed": ("tok-a", "tok-b"),
                "failure_reason": "",
                "child_items": [],
                "group_failed": False,
                "frame_kind": FrameKind.EXPAND,
                "outcome": TerminalOutcome.SUCCESS,
                "path": TerminalPath.COALESCED,
            }
        ]
        assert recorder.route_calls == [{"collector_name": CollectorName("stitch"), "released_tokens": (released_token,)}]
        [fire] = recorder.fire_calls
        assert fire["collector_name"] == CollectorName("stitch")
        assert fire["group_id"] == "eg-1"
        assert tuple(t.token_id for t in fire["consumed_tokens"]) == ("tok-a", "tok-q", "tok-b")
        assert fire["release"] is recorder.release
        assert [spec.closer_name for spec in fire["group_losses"]] == ["merge_outer"]
        assert disposition is not None
        assert disposition.kind is BarrierIntakeDispositionKind.READY_CONTINUATION
        assert disposition.child_items == (continuation,)

    def test_terminal_collector_release_is_a_pending_sink_disposition(self) -> None:
        released_token = make_token_info(row_id="row-1", token_id="tok-out")
        sink_result = RowResult(
            token=released_token,
            final_data=released_token.row_data,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name="out",
        )
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=(sink_result,)))
        coordinator = _coordinator(
            scheduler=RecordingScheduler(pending=[]), executor=object(), recorder=recorder, data_flow=_RecordingDataFlow()
        )

        disposition = coordinator._dispose_collector_outcome(_released(_member("tok-a"), released=(released_token,)), scope_row_id="row-1")

        assert disposition is not None
        assert disposition.kind is BarrierIntakeDispositionKind.PENDING_SINK
        [result] = disposition.results
        assert (result.token.token_id, result.sink_name, result.scheduler_pending_sink) == ("tok-out", "out", True)


class TestDispatchFailure:
    def test_failed_group_terminalizes_members_through_the_seam_and_releases_their_rows(self) -> None:
        members = (_member("tok-a"), _member("tok-b"))
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=()))
        recorder.staged.append(
            GroupLossSpec(closer_name="merge_outer", group_id="fg_outer", member_key="left", token_id="t", reason="group_failed")
        )
        scheduler = RecordingScheduler(pending=[])
        coordinator = _coordinator(scheduler=scheduler, executor=object(), recorder=recorder, data_flow=_RecordingDataFlow())
        outcome = CollectorOutcome(
            held=False, consumed_tokens=members, collector_name="stitch", group_id="eg-1", failure_reason="collector_missing_members"
        )

        disposition = coordinator._dispose_collector_outcome(outcome, scope_row_id="row-1")

        assert recorder.seam_calls == [
            {
                "consumed": ("tok-a", "tok-b"),
                "failure_reason": "collector_missing_members",
                "child_items": [],
                "group_failed": True,
                "frame_kind": FrameKind.EXPAND,
            }
        ]
        assert scheduler.calls == ["release"]
        assert scheduler.release_contexts == [{"reason": "collector_missing_members", "released_by": "leader-1", "scope_row_id": "row-1"}]
        assert recorder.fire_calls == []
        assert recorder.completed == [
            ("tok-a", TerminalOutcome.FAILURE, TerminalPath.UNROUTED),
            ("tok-b", TerminalOutcome.FAILURE, TerminalPath.UNROUTED),
        ]
        assert coordinator._failed_group_notes == {("stitch", "eg-1"): "collector_missing_members"}
        assert disposition is not None
        assert disposition.kind is BarrierIntakeDispositionKind.TERMINAL
        assert [(r.token.token_id, r.outcome, r.path, r.error.exception_type if r.error else None) for r in disposition.results] == [
            ("tok-a", TerminalOutcome.FAILURE, TerminalPath.UNROUTED, "CollectorGroupFailure"),
            ("tok-b", TerminalOutcome.FAILURE, TerminalPath.UNROUTED, "CollectorGroupFailure"),
        ]

    def test_failure_release_carries_the_seams_staged_escalation(self) -> None:
        """Ruling 43 shape: the escalation the seam stages while terminalizing
        the members rides the SAME mark_blocked_barrier_terminal commit."""
        members = (_member("tok-a"),)
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=()))
        captured: list[dict[str, object]] = []

        class _Scheduler(RecordingScheduler):
            def mark_blocked_barrier_terminal(self, **kwargs: object) -> int:  # type: ignore[override]
                captured.append(dict(kwargs))
                return 1

        staged = GroupLossSpec(closer_name="merge_outer", group_id="fg_outer", member_key="left", token_id="t", reason="group_failed")
        recorder.staged.append(staged)
        coordinator = _coordinator(scheduler=_Scheduler(pending=[]), executor=object(), recorder=recorder, data_flow=_RecordingDataFlow())
        outcome = CollectorOutcome(
            held=False, consumed_tokens=members, collector_name="stitch", group_id="eg-1", failure_reason="collector_missing_members"
        )

        coordinator._dispose_collector_outcome(outcome, scope_row_id="row-1")

        [call] = captured
        assert call["barrier_key"] == collector_barrier_key("stitch", "eg-1")
        assert call["token_ids"] == ("tok-a",)
        assert call["group_losses"] == (staged,)

    def test_plugin_free_close_with_nothing_consumed_moves_no_rows(self) -> None:
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=()))
        scheduler = RecordingScheduler(pending=[])
        coordinator = _coordinator(scheduler=scheduler, executor=object(), recorder=recorder, data_flow=_RecordingDataFlow())
        outcome = CollectorOutcome(held=False, collector_name="stitch", group_id="eg-1", closed_without_plugin="all_members_lost")
        assert coordinator._dispose_collector_outcome(outcome, scope_row_id="row-1") is None
        assert scheduler.calls == []
        assert recorder.seam_calls == []


class TestLossReplayArm:
    def _loss(self) -> SimpleNamespace:
        return SimpleNamespace(
            loss_id="loss-1", closer_name="stitch", group_id="eg-1", member_key="m2", reason="quarantined", token_id="tok-lost"
        )

    def test_replay_notifies_with_the_intake_ctx_and_disposes_the_outcome(self) -> None:
        members = (_member("tok-a"),)
        outcome = CollectorOutcome(
            held=False, consumed_tokens=members, collector_name="stitch", group_id="eg-1", failure_reason="collector_missing_members"
        )
        executor = _LossReplayExecutor(outcome=outcome)
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=()))
        scheduler = RecordingScheduler(pending=[], losses=[self._loss()])
        coordinator = _coordinator(scheduler=scheduler, executor=executor, recorder=recorder, data_flow=_RecordingDataFlow())
        ctx = PluginContext(run_id="run-1", config={}, landscape=None)

        pass_outcome = coordinator.run_intake_pass(ctx)

        assert scheduler.calls == ["list_pending", "adopt_losses", "release"]
        assert executor.notified == [("stitch", "eg-1", "m2", "quarantined", ctx)]
        assert [d.kind for d in pass_outcome.dispositions] == [BarrierIntakeDispositionKind.TERMINAL]

    def test_replay_skips_a_loss_already_in_executor_memory(self) -> None:
        executor = _LossReplayExecutor(outcome=None, replayed=frozenset({("stitch", "eg-1", "m2")}))
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=()))
        scheduler = RecordingScheduler(pending=[], losses=[self._loss()])
        coordinator = _coordinator(scheduler=scheduler, executor=executor, recorder=recorder, data_flow=_RecordingDataFlow())

        coordinator.run_intake_pass(PluginContext(run_id="run-1", config={}, landscape=None))

        assert scheduler.calls == ["list_pending", "adopt_losses"]
        assert executor.notified == []

    def test_replay_without_a_context_fails_closed_on_a_collector_loss(self) -> None:
        executor = _LossReplayExecutor(outcome=None)
        recorder = _Recorder(release=CollectorRelease(items=(), sink_results=()))
        coordinator = _coordinator(
            scheduler=RecordingScheduler(pending=[], losses=[self._loss()]),
            executor=executor,
            recorder=recorder,
            data_flow=_RecordingDataFlow(),
        )
        with pytest.raises(OrchestrationInvariantError, match="no PluginContext"):
            coordinator.replay_durable_group_losses()
        assert executor.notified == []
