"""RowProcessor collector wiring (WS3+WS4 integration item 1).

The arrival hold, THE mark_blocked writer of collector barrier_keys, the
opener-child cursor, and the release cursor derivation — each driven through
the real drain / traversal / recorder where the brief demands it (a green
Task 6/7 suite seeds ``collector_name`` through fixtures; these do not).
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from elspeth.contracts import TokenInfo, TransformResult
from elspeth.contracts.enums import FrameKind, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.types import BranchName, CoalesceName, CollectorName, NodeID, RowUnionName
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
from elspeth.core.landscape.scheduler.work_items import collector_barrier_key
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.collector import CollectorOutcome
from elspeth.engine.processor import DAGTraversalContext, RowProcessor
from elspeth.engine.scheduler_drain import ProcessorMode
from elspeth.engine.spans import SpanFactory
from elspeth.engine.token_traversal import _TransformTerminal
from elspeth.engine.work_items import WorkItem
from elspeth.testing import make_contract, make_row, make_token_info
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import RecorderSetup, leader_coordination_token, make_recorder_with_run, register_test_node
from tests.unit.engine.test_processor import (
    _make_factory,
    _make_mock_transform,
    _make_processor,
    _persist_token_for_scheduler,
)
from tests.unit.engine.test_scheduler_drain_characterization import LEADER_OWNER

COLLECTOR_NODE = "collector-stitch"
SOURCE_NODE = "source-1"
STITCH = CollectorName("stitch")


class _HoldingCollectorExecutor:
    """Executor double that holds every arrival and records the ctx it was fed."""

    def __init__(self) -> None:
        self.accepted: list[tuple[str, str, PluginContext]] = []

    def accept(self, token: TokenInfo, collector_name: str, ctx: PluginContext, *, arrival_time: float) -> CollectorOutcome:
        frame = token.lineage_path[-1]
        self.accepted.append((token.token_id, collector_name, ctx))
        return CollectorOutcome(held=True, collector_name=collector_name, group_id=frame.group_id)

    def notify_member_lost(self, *args: object, **kwargs: object) -> CollectorOutcome | None:
        raise AssertionError("not reached")


def _build(*, mode: ProcessorMode, executor: _HoldingCollectorExecutor | None) -> tuple[RowProcessor, RecorderSetup, MockClock]:
    """Real RowProcessor over a real scheduler DB, one collector node in the traversal."""
    setup = make_recorder_with_run(run_id="run-collector-wiring", source_node_id=SOURCE_NODE, leader_worker_id=LEADER_OWNER)
    register_test_node(setup.data_flow, setup.run_id, COLLECTOR_NODE)
    clock = MockClock(start=1_750_000_000.0)
    leader = mode is ProcessorMode.LEADER
    processor = RowProcessor(
        execution=setup.execution,
        data_flow=setup.data_flow,
        span_factory=SpanFactory(),
        run_id=setup.run_id,
        source_node_id=NodeID(SOURCE_NODE),
        source_on_success="default",
        traversal=DAGTraversalContext(
            node_step_map={NodeID(SOURCE_NODE): 0, NodeID(COLLECTOR_NODE): 1},
            node_to_plugin={},
            node_to_next={NodeID(SOURCE_NODE): NodeID(COLLECTOR_NODE), NodeID(COLLECTOR_NODE): None},
            coalesce_node_map={},
            collector_node_map={STITCH: NodeID(COLLECTOR_NODE)},
        ),
        scheduler=setup.factory.scheduler,
        scheduler_lease_owner=LEADER_OWNER if leader else "follower-1",
        coordination_token=leader_coordination_token(setup.factory, setup.run_id) if leader else None,
        clock=clock,
        mode=mode,
        collector_executor=cast(Any, executor),
    )
    return processor, setup, clock


def _expand_member(setup: RecorderSetup, *, sequence: int, group_id: str) -> TokenInfo:
    row, token = setup.data_flow.create_row_with_token(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        row_index=sequence,
        data={"id": sequence},
        source_row_index=sequence,
        ingest_sequence=sequence,
    )
    return TokenInfo(
        row_id=row.row_id,
        token_id=token.token_id,
        row_data=PipelineRow({"id": sequence}, make_contract()),
        lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id=group_id, member_key=token.token_id),),
    )


def _ctx(setup: RecorderSetup) -> PluginContext:
    return PluginContext(run_id=setup.run_id, config={}, landscape=None)


class TestArrivalHoldIsTheDurableWriter:
    def test_collector_arrival_journals_the_compound_key_and_intake_adopts_it(self) -> None:
        """Verification obligations 1 and 2: the value mark_blocked writes
        survives to the durable row and back through the REAL facade, and
        the executor receives the intake's own ctx unchanged."""
        executor = _HoldingCollectorExecutor()
        processor, setup, _clock = _build(mode=ProcessorMode.LEADER, executor=executor)
        token = _expand_member(setup, sequence=1, group_id="g-1")
        item = WorkItem(token=token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)
        ctx = _ctx(setup)
        scheduler = setup.factory.scheduler
        seen_pending: list[Any] = []
        real_list_pending = scheduler.list_pending_blocked_barrier_items

        def observe_pending(*, run_id: str) -> list[Any]:
            rows = real_list_pending(run_id=run_id)
            seen_pending.extend(rows)
            return rows

        with patch.object(scheduler, "list_pending_blocked_barrier_items", side_effect=observe_pending):
            results = processor._drain_durable_work_queue(item, ctx)

        assert results == []
        # The BLOCKED row the drain wrote, as the intake read it back (durable
        # row -> TokenWorkItem through list_pending_blocked_barrier_items).
        assert [(row.barrier_key, row.collector_name, row.status) for row in seen_pending] == [
            (collector_barrier_key("stitch", "g-1"), "stitch", TokenWorkStatus.BLOCKED),
        ]
        blocked = scheduler.list_blocked_barrier_items(run_id=setup.run_id)
        assert [(row.token_id, row.barrier_key, row.collector_name, row.barrier_adopted_epoch is not None) for row in blocked] == [
            (token.token_id, collector_barrier_key("stitch", "g-1"), "stitch", True),
        ]
        assert [(token_id, name) for token_id, name, _ctx in executor.accepted] == [(token.token_id, "stitch")]
        assert executor.accepted[0][2] is ctx

    def test_follower_holds_without_a_stash_and_still_derives_the_compound_key(self) -> None:
        """Verification obligation 3: the collector_executor is None path."""
        processor, setup, _clock = _build(mode=ProcessorMode.FOLLOWER, executor=None)
        assert processor.collector_executor is None
        token = _expand_member(setup, sequence=2, group_id="g-2")

        handled, result = processor._maybe_collector_token(token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)

        assert (handled, result) == (True, None)
        assert processor._live_barrier_holds == {}
        item = WorkItem(token=token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)
        assert processor._barrier_key_for_blocked_item(item) == collector_barrier_key("stitch", "g-2")
        assert processor._queue_key_for_blocked_item(item) is None

    def test_arrival_elsewhere_is_not_a_collector_hold(self) -> None:
        processor, setup, _clock = _build(mode=ProcessorMode.LEADER, executor=_HoldingCollectorExecutor())
        token = _expand_member(setup, sequence=3, group_id="g-3")
        assert processor._maybe_collector_token(token, current_node_id=NodeID(SOURCE_NODE), collector_name=STITCH) == (False, None)
        assert processor._maybe_collector_token(token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=None) == (False, None)

    def test_collector_cursor_without_an_expand_frame_fails_closed(self) -> None:
        processor, _setup, _clock = _build(mode=ProcessorMode.LEADER, executor=_HoldingCollectorExecutor())
        token = make_token_info(data={"id": 9}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg", member_key="a"),))
        with pytest.raises(OrchestrationInvariantError, match="without an innermost EXPAND frame"):
            processor._maybe_collector_token(token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)
        item = WorkItem(token=token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)
        with pytest.raises(OrchestrationInvariantError, match="no innermost EXPAND frame"):
            processor._barrier_key_for_blocked_item(item)


def _expand_binding(opener_node: NodeID) -> GroupBindingRegistry:
    return GroupBindingRegistry(
        bindings=(
            GroupBinding(
                kind=FrameKind.EXPAND,
                opener_node_id=opener_node,
                opener_name="explode",
                closer_node_id=NodeID("collector-stitch"),
                closer_name="stitch",
                closer_kind=CloserKind.COLLECTOR,
                policy="require_all",
                on_group_failure=None,
                member_roster=(),
            ),
        )
    )


class TestOpenerChildrenCarryTheCollectorCursor:
    @pytest.mark.parametrize("bound", [True, False], ids=["bound-opener", "unbound-expansion"])
    def test_expand_children_cursor(self, bound: bool) -> None:
        _db, factory = _make_factory()
        transform = _make_mock_transform(node_id="explode-node", creates_tokens=True)
        opener_node = NodeID("explode-node")
        next_node = NodeID("after-explode")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, opener_node: 1, next_node: 2},
            node_to_next={NodeID("source-0"): opener_node, opener_node: next_node, next_node: None},
            node_to_plugin={opener_node: transform},
            group_bindings=_expand_binding(opener_node) if bound else None,
        )
        contract = make_contract()
        multi = TransformResult.success_multi(
            [make_row({"value": 1}, contract=contract), make_row({"value": 2}, contract=contract)],
            success_reason={"action": "expand"},
        )
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(factory, token)
        child_items: list[WorkItem] = []

        with patch.object(processor._transform_executor, "execute_transform", side_effect=lambda **kw: (multi, kw["token"], None)):
            outcome = processor._handle_transform_node(
                transform=transform,
                current_token=token,
                ctx=make_context(),
                node_id=opener_node,
                child_items=child_items,
                coalesce_node_id=None,
                coalesce_name=None,
                current_on_success_sink="default",
            )

        assert isinstance(outcome, _TransformTerminal)
        assert (outcome.result.outcome, outcome.result.path) == (TerminalOutcome.TRANSIENT, TerminalPath.EXPAND_PARENT)
        assert len(child_items) == 2
        expected_cursor = STITCH if bound else None
        assert [(item.current_node_id, item.collector_name, item.coalesce_name, item.row_union_name) for item in child_items] == [
            (next_node, expected_cursor, None, None),
            (next_node, expected_cursor, None, None),
        ]
        assert all(item.token.lineage_path[-1].kind is FrameKind.EXPAND for item in child_items)


class TestReleaseCursorDerivation:
    def _processor(self) -> RowProcessor:
        _db, factory = _make_factory()
        registry = GroupBindingRegistry(
            bindings=(
                GroupBinding(
                    kind=FrameKind.EXPAND,
                    opener_node_id=NodeID("outer-explode"),
                    opener_name="outer_explode",
                    closer_node_id=NodeID("collector-outer"),
                    closer_name="outer_stitch",
                    closer_kind=CloserKind.COLLECTOR,
                    policy="require_all",
                    on_group_failure=None,
                    member_roster=(),
                ),
            )
        )
        registry.register_expand_group("g-outer", opener_name="outer_explode")
        return _make_processor(
            factory,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            branch_to_row_union={BranchName("path_u"): RowUnionName("union")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            group_bindings=registry,
        )

    def test_no_remaining_frame_carries_no_cursor(self) -> None:
        token = make_token_info(data={})
        assert self._processor()._released_collector_cursor(token) == (None, None, None)

    def test_outer_fork_frame_resolves_the_branch_barrier(self) -> None:
        processor = self._processor()
        coalesce_token = make_token_info(data={}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg", member_key="path_a"),))
        union_token = make_token_info(data={}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg", member_key="path_u"),))
        sink_token = make_token_info(data={}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg", member_key="path_sink"),))
        assert processor._released_collector_cursor(coalesce_token) == (CoalesceName("merge"), None, None)
        assert processor._released_collector_cursor(union_token) == (None, RowUnionName("union"), None)
        assert processor._released_collector_cursor(sink_token) == (None, None, None)

    def test_outer_expand_frame_resolves_the_enclosing_collector_when_bound(self) -> None:
        processor = self._processor()
        bound = make_token_info(data={}, lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="g-outer", member_key="m"),))
        inert = make_token_info(data={}, lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="g-unbound", member_key="m"),))
        assert processor._released_collector_cursor(bound) == (None, None, CollectorName("outer_stitch"))
        assert processor._released_collector_cursor(inert) == (None, None, None)
