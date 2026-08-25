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
    """Executor double that holds every arrival and records the ctx it was fed.

    ``is_release_group`` models the real executor's contract (META-38): a
    durable read of the written fact through the run's data_flow, wired by
    ``_build`` once the recorder exists."""

    def __init__(self) -> None:
        self.accepted: list[tuple[str, str, PluginContext]] = []
        self.setup: RecorderSetup | None = None

    def is_release_group(self, group_id: str) -> bool:
        assert self.setup is not None, "_build wires the recorder before any arrival"
        return self.setup.data_flow.is_release_group(run_id=self.setup.run_id, group_id=group_id)

    def accept(self, token: TokenInfo, collector_name: str, ctx: PluginContext, *, arrival_time: float) -> CollectorOutcome:
        frame = token.lineage_path[-1]
        self.accepted.append((token.token_id, collector_name, ctx))
        return CollectorOutcome(held=True, collector_name=collector_name, group_id=frame.group_id)

    def notify_member_lost(self, *args: object, **kwargs: object) -> CollectorOutcome | None:
        raise AssertionError("not reached")


UNION_NODE = "row_union::variants"
AFTER_UNION = "after-union"


def _build(
    *,
    mode: ProcessorMode,
    executor: _HoldingCollectorExecutor | None,
    with_union_before_collector: bool = False,
    group_bindings: GroupBindingRegistry | None = None,
) -> tuple[RowProcessor, RecorderSetup, MockClock]:
    """Real RowProcessor over a real scheduler DB, one collector node in the traversal.

    ``with_union_before_collector`` adds source -> union -> after-union ->
    collector, the depth-2 shape a union INSIDE a scope releases into."""
    setup = make_recorder_with_run(run_id="run-collector-wiring", source_node_id=SOURCE_NODE, leader_worker_id=LEADER_OWNER)
    if executor is not None:
        executor.setup = setup
    register_test_node(setup.data_flow, setup.run_id, COLLECTOR_NODE)
    clock = MockClock(start=1_750_000_000.0)
    leader = mode is ProcessorMode.LEADER
    if with_union_before_collector:
        register_test_node(setup.data_flow, setup.run_id, UNION_NODE)
        register_test_node(setup.data_flow, setup.run_id, AFTER_UNION)
        traversal = DAGTraversalContext(
            node_step_map={NodeID(SOURCE_NODE): 0, NodeID(UNION_NODE): 1, NodeID(AFTER_UNION): 2, NodeID(COLLECTOR_NODE): 3},
            node_to_plugin={},
            node_to_next={
                NodeID(SOURCE_NODE): NodeID(UNION_NODE),
                NodeID(UNION_NODE): NodeID(AFTER_UNION),
                NodeID(AFTER_UNION): NodeID(COLLECTOR_NODE),
                NodeID(COLLECTOR_NODE): None,
            },
            coalesce_node_map={},
            row_union_node_map={RowUnionName("variants"): NodeID(UNION_NODE)},
            collector_node_map={STITCH: NodeID(COLLECTOR_NODE)},
            structural_node_ids=frozenset({NodeID(AFTER_UNION)}),
        )
    else:
        traversal = DAGTraversalContext(
            node_step_map={NodeID(SOURCE_NODE): 0, NodeID(COLLECTOR_NODE): 1},
            node_to_plugin={},
            node_to_next={NodeID(SOURCE_NODE): NodeID(COLLECTOR_NODE), NodeID(COLLECTOR_NODE): None},
            coalesce_node_map={},
            collector_node_map={STITCH: NodeID(COLLECTOR_NODE)},
        )
    processor = RowProcessor(
        execution=setup.execution,
        data_flow=setup.data_flow,
        span_factory=SpanFactory(),
        run_id=setup.run_id,
        source_node_id=NodeID(SOURCE_NODE),
        source_on_success="default",
        traversal=traversal,
        group_bindings=group_bindings,
        scheduler=setup.factory.scheduler,
        scheduler_lease_owner=LEADER_OWNER if leader else "follower-1",
        coordination_token=leader_coordination_token(setup.factory, setup.run_id) if leader else None,
        clock=clock,
        mode=mode,
        collector_executor=cast(Any, executor),
    )
    return processor, setup, clock


def _expand_member(setup: RecorderSetup, *, sequence: int) -> TokenInfo:
    """A REAL one-member expansion (expand_token) — its group id is minted,
    read back from the child's lineage. META-38: the collector arrival keying
    reads the written release fact for the member's frame, so a crafted frame
    naming an unminted group fails closed; the fixture mints, never the code."""
    from elspeth.contracts.audit import TokenRef

    row, parent = setup.data_flow.create_row_with_token(
        run_id=setup.run_id,
        source_node_id=setup.source_node_id,
        row_index=sequence,
        data={"id": sequence},
        source_row_index=sequence,
        ingest_sequence=sequence,
    )
    contract = make_contract()
    [child], _group_id = setup.data_flow.expand_token(
        parent_ref=TokenRef(token_id=parent.token_id, run_id=setup.run_id),
        row_id=row.row_id,
        child_payloads=[{"id": sequence}],
        output_contract=contract,
    )
    return TokenInfo(
        row_id=row.row_id, token_id=child.token_id, row_data=PipelineRow({"id": sequence}, contract), lineage_path=child.lineage_path
    )


def _group_of(token: TokenInfo) -> str:
    return token.lineage_path[-1].group_id


def _ctx(setup: RecorderSetup) -> PluginContext:
    return PluginContext(run_id=setup.run_id, config={}, landscape=None)


class TestArrivalHoldIsTheDurableWriter:
    def test_collector_arrival_journals_the_compound_key_and_intake_adopts_it(self) -> None:
        """Verification obligations 1 and 2: the value mark_blocked writes
        survives to the durable row and back through the REAL facade, and
        the executor receives the intake's own ctx unchanged."""
        executor = _HoldingCollectorExecutor()
        processor, setup, _clock = _build(mode=ProcessorMode.LEADER, executor=executor)
        token = _expand_member(setup, sequence=1)
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
            (collector_barrier_key("stitch", _group_of(token)), "stitch", TokenWorkStatus.BLOCKED),
        ]
        blocked = scheduler.list_blocked_barrier_items(run_id=setup.run_id)
        assert [(row.token_id, row.barrier_key, row.collector_name, row.barrier_adopted_epoch is not None) for row in blocked] == [
            (token.token_id, collector_barrier_key("stitch", _group_of(token)), "stitch", True),
        ]
        assert [(token_id, name) for token_id, name, _ctx in executor.accepted] == [(token.token_id, "stitch")]
        assert executor.accepted[0][2] is ctx

    def test_follower_holds_without_a_stash_and_still_derives_the_compound_key(self) -> None:
        """Verification obligation 3: the collector_executor is None path."""
        processor, setup, _clock = _build(mode=ProcessorMode.FOLLOWER, executor=None)
        assert processor.collector_executor is None
        token = _expand_member(setup, sequence=2)

        handled, result = processor._maybe_collector_token(token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)

        assert (handled, result) == (True, None)
        assert processor._live_barrier_holds == {}
        item = WorkItem(token=token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=STITCH)
        assert processor._barrier_key_for_blocked_item(item) == collector_barrier_key("stitch", _group_of(token))
        assert processor._queue_key_for_blocked_item(item) is None

    def test_arrival_elsewhere_is_not_a_collector_hold(self) -> None:
        processor, setup, _clock = _build(mode=ProcessorMode.LEADER, executor=_HoldingCollectorExecutor())
        token = _expand_member(setup, sequence=3)
        assert processor._maybe_collector_token(token, current_node_id=NodeID(SOURCE_NODE), collector_name=STITCH) == (False, None)
        assert processor._maybe_collector_token(token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=None) == (False, None)

    def test_collector_cursor_without_an_expand_frame_fails_closed(self) -> None:
        from elspeth.contracts.audit import TokenRef

        processor, setup, _clock = _build(mode=ProcessorMode.LEADER, executor=_HoldingCollectorExecutor())
        # A REAL fork child (minted FORK frame): the walk finds a non-release
        # frame that is not EXPAND and refuses.
        row, parent = setup.data_flow.create_row_with_token(
            run_id=setup.run_id, source_node_id=setup.source_node_id, row_index=9, data={"id": 9}, source_row_index=9, ingest_sequence=9
        )
        [branch, _other], _fg = setup.data_flow.fork_token(
            parent_ref=TokenRef(token_id=parent.token_id, run_id=setup.run_id), row_id=row.row_id, branches=["a", "b"]
        )
        token = make_token_info(row_id=row.row_id, token_id=branch.token_id, data={"id": 9}, lineage_path=branch.lineage_path)
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
        # An unregistered EXPAND frame is re-derived durably (META-9.1,
        # pinned in test_first_bound_frame_rederivation.py); here the
        # re-derivation's UNDECLARED verdict is what makes the frame inert.
        with patch.object(processor, "_rederive_expand_binding", return_value=None):
            assert processor._released_collector_cursor(inert) == (None, None, None)


class TestReleaseCursorWalk:
    """META-37 (elspeth-e00cb66b7d): a collector release carries its OWN
    release-group EXPAND frame INNERMOST (``collect_tokens`` mints
    ``(EXPAND, release_group_id, own token_id)``), so the cursor derivation
    must walk OUTWARD past it — reading ``lineage_path[-1]`` alone gave a
    release inside a fork no cursor at all, and the release then
    terminal-defaulted past the coalesce to a nonexistent sink. Release
    tokens here are minted through the REAL durable writers (fork_token /
    expand_token / collect_tokens), never synthesized, so the walk's inert
    classification runs against the release group's real group_records row
    (opener = the representative MEMBER, which visited no declared opener —
    the structural fact that makes the frame inert)."""

    INNER_OPENER = NodeID("inner-explode")
    OUTER_OPENER = NodeID("outer-explode")

    def _registry(self) -> GroupBindingRegistry:
        return GroupBindingRegistry(
            bindings=(
                GroupBinding(
                    kind=FrameKind.EXPAND,
                    opener_node_id=self.INNER_OPENER,
                    opener_name="inner_explode",
                    closer_node_id=NodeID(COLLECTOR_NODE),
                    closer_name=str(STITCH),
                    closer_kind=CloserKind.COLLECTOR,
                    policy="require_all",
                    on_group_failure=None,
                    member_roster=(),
                ),
                GroupBinding(
                    kind=FrameKind.EXPAND,
                    opener_node_id=self.OUTER_OPENER,
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

    @staticmethod
    def _seed_root(factory: Any, *, run_id: str = "test-run") -> tuple[str, str]:
        row = factory.data_flow.create_row(run_id, "source-0", 1, {"seed": 1}, source_row_index=1, ingest_sequence=1)
        root = factory.data_flow.create_token(row_id=row.row_id)
        return row.row_id, root.token_id

    @staticmethod
    def _collect_release(factory: Any, *, scope_parent_id: str, row_id: str, run_id: str = "test-run") -> TokenInfo:
        """Durably expand ``scope_parent_id`` into a one-member scope group and
        close it through collect_tokens; return the release child with its
        REAL durable lineage (outer frames + its own release-group frame)."""
        from elspeth.contracts.audit import TokenRef

        contract = make_contract()
        children, scope_gid = factory.data_flow.expand_token(
            parent_ref=TokenRef(token_id=scope_parent_id, run_id=run_id),
            row_id=row_id,
            child_payloads=[{"v": 1}],
            output_contract=contract,
        )
        committed = factory.data_flow.collect_tokens(
            member_refs=[TokenRef(token_id=children[0].token_id, run_id=run_id)],
            group_id=scope_gid,
            collector_node_id=COLLECTOR_NODE,
            output_payloads=[{"assembled": True}],
            output_contracts=[contract],
        )
        [release] = committed.children
        lineage = factory.data_flow.load_lineage_paths(run_id, [release.token_id])[release.token_id]
        return make_token_info(row_id=row_id, token_id=release.token_id, lineage_path=lineage)

    def test_release_inside_a_fork_walks_past_its_release_group_frame_to_the_branch_coalesce(self) -> None:
        from elspeth.contracts.audit import TokenRef

        _db, factory = _make_factory()
        processor = _make_processor(
            factory,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            group_bindings=self._registry(),
        )
        row_id, root_id = self._seed_root(factory)
        forked, _fork_gid = factory.data_flow.fork_token(TokenRef(token_id=root_id, run_id="test-run"), row_id, ["path_a", "path_b"])
        release = self._collect_release(factory, scope_parent_id=forked[0].token_id, row_id=row_id)

        # The release's innermost frame is its OWN release-group frame — the
        # META-37 shape — with the FORK frame one level out.
        assert [f.kind for f in release.lineage_path] == [FrameKind.FORK, FrameKind.EXPAND]
        assert release.lineage_path[-1].member_key == release.token_id
        assert processor._released_collector_cursor(release) == (CoalesceName("merge"), None, None)

    def test_release_inside_an_outer_scope_walks_to_the_enclosing_collector(self) -> None:
        _db, factory = _make_factory()
        registry = self._registry()
        processor = _make_processor(factory, group_bindings=registry)
        row_id, root_id = self._seed_root(factory)
        from elspeth.contracts.audit import TokenRef

        outer_members, outer_gid = factory.data_flow.expand_token(
            parent_ref=TokenRef(token_id=root_id, run_id="test-run"),
            row_id=row_id,
            child_payloads=[{"page": 1}],
            output_contract=make_contract(),
        )
        registry.register_expand_group(outer_gid, opener_name="outer_explode")
        release = self._collect_release(factory, scope_parent_id=outer_members[0].token_id, row_id=row_id)

        assert [f.kind for f in release.lineage_path] == [FrameKind.EXPAND, FrameKind.EXPAND]
        assert processor._released_collector_cursor(release) == (None, None, CollectorName("outer_stitch"))

    def test_release_in_an_unbarried_fork_branch_still_walks_to_the_enclosing_collector(self) -> None:
        """A FORK frame whose branch feeds no coalesce/row_union has no
        barrier of its own — the walk continues OUTWARD to the enclosing
        scope's collector rather than stopping cursorless at the branch."""
        from elspeth.contracts.audit import TokenRef

        _db, factory = _make_factory()
        registry = self._registry()
        processor = _make_processor(factory, group_bindings=registry)
        row_id, root_id = self._seed_root(factory)
        outer_members, outer_gid = factory.data_flow.expand_token(
            parent_ref=TokenRef(token_id=root_id, run_id="test-run"),
            row_id=row_id,
            child_payloads=[{"page": 1}],
            output_contract=make_contract(),
        )
        registry.register_expand_group(outer_gid, opener_name="outer_explode")
        forked, _fork_gid = factory.data_flow.fork_token(
            TokenRef(token_id=outer_members[0].token_id, run_id="test-run"), row_id, ["path_x", "path_y"]
        )
        release = self._collect_release(factory, scope_parent_id=forked[0].token_id, row_id=row_id)

        assert [f.kind for f in release.lineage_path] == [FrameKind.EXPAND, FrameKind.FORK, FrameKind.EXPAND]
        assert processor._released_collector_cursor(release) == (None, None, CollectorName("outer_stitch"))

    def test_flat_release_cursor_stays_none(self) -> None:
        """Zero-delta pin: a flat (terminal-collector) release carries ONLY
        its own release-group frame and derives no cursor — exactly the
        pre-walk behaviour, byte-identical."""
        _db, factory = _make_factory()
        processor = _make_processor(factory, group_bindings=self._registry())
        row_id, root_id = self._seed_root(factory)
        release = self._collect_release(factory, scope_parent_id=root_id, row_id=row_id)

        assert [f.kind for f in release.lineage_path] == [FrameKind.EXPAND]
        assert processor._released_collector_cursor(release) == (None, None, None)


class TestNestedReleaseCursor:
    """A coalesce or row_union INSIDE a scope releases the scope's member:
    the continuation's remaining innermost frame is the bound EXPAND frame,
    so it must carry the collector cursor (and hold at the collector) rather
    than the just-completed barrier's cursor (integration C5, spec §7 rules 2/5)."""

    OUTER_EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="g-outer", member_key="m1")

    def _processor(self, factory: Any) -> RowProcessor:
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
        merge_node = NodeID("coalesce::merge")
        union_node = NodeID("row_union::variants")
        after_merge = NodeID("after-merge")
        after_union = NodeID("after-union")
        return _make_processor(
            factory,
            coalesce_node_ids={CoalesceName("merge"): merge_node},
            row_union_node_ids={RowUnionName("variants"): union_node},
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            branch_to_row_union={BranchName("path_u"): RowUnionName("variants")},
            node_step_map={NodeID("source-0"): 0, merge_node: 1, after_merge: 2, union_node: 3, after_union: 4},
            node_to_next={
                NodeID("source-0"): merge_node,
                merge_node: after_merge,
                after_merge: None,
                union_node: after_union,
                after_union: None,
            },
            group_bindings=registry,
        )

    def test_merged_continuation_cursor_three_shapes(self) -> None:
        _db, factory = _make_factory()
        processor = self._processor(factory)
        in_scope = make_token_info(data={}, lineage_path=(self.OUTER_EXPAND,))
        flat = make_token_info(data={})
        nested_fork = make_token_info(data={}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg", member_key="path_a"),))
        assert processor._merged_continuation_cursor(in_scope, CoalesceName("inner")) == (None, None, CollectorName("outer_stitch"))
        assert processor._merged_continuation_cursor(flat, CoalesceName("inner")) == (CoalesceName("inner"), None, None)
        assert processor._merged_continuation_cursor(nested_fork, CoalesceName("inner")) == (CoalesceName("merge"), None, None)

    def test_row_union_release_inside_a_scope_carries_the_collector_cursor(self) -> None:
        _db, factory = _make_factory()
        processor = self._processor(factory)
        in_scope = make_token_info(data={}, lineage_path=(self.OUTER_EXPAND,))
        flat = make_token_info(data={})
        items = processor.released_row_union_items(row_union_name=RowUnionName("variants"), released_tokens=(in_scope, flat))
        assert [(item.current_node_id, item.collector_name, item.row_union_name) for item in items] == [
            (NodeID("after-union"), CollectorName("outer_stitch"), None),
            (NodeID("after-union"), None, None),
        ]

    @pytest.mark.parametrize("in_scope", [True, False], ids=["coalesce-inside-scope", "flat-coalesce"])
    def test_out_of_claim_merge_continuation_cursor(self, in_scope: bool) -> None:
        _db, factory = _make_factory()
        processor = self._processor(factory)
        merged = make_token_info(data={"v": 1}, lineage_path=(self.OUTER_EXPAND,) if in_scope else ())
        captured: list[WorkItem] = []
        with (
            patch.object(processor, "_complete_coalesce_fire"),
            patch.object(processor, "_drain_work_queue", side_effect=lambda item, ctx: captured.append(item) or []),
        ):
            processor.complete_coalesce_merge(
                coalesce_name=CoalesceName("merge"),
                consumed_tokens=(),
                merged_token=merged,
                coalesce_node_id=NodeID("coalesce::merge"),
                ctx=make_context(),
            )
        [item] = captured
        if in_scope:
            assert (item.collector_name, item.coalesce_name, item.coalesce_node_id) == (CollectorName("outer_stitch"), None, None)
        else:
            assert (item.collector_name, item.coalesce_name, item.coalesce_node_id) == (
                None,
                CoalesceName("merge"),
                NodeID("coalesce::merge"),
            )

    def test_intake_merge_fire_emits_the_collector_cursor_on_the_ready_row(self) -> None:
        """The coordinator's fire path consumes the processor's derivation:
        the READY emission written by complete_barrier carries the collector
        cursor for an in-scope merge."""
        from types import SimpleNamespace

        from elspeth.engine.coalesce_executor import CoalesceOutcome

        _db, factory = _make_factory()
        processor = self._processor(factory)
        merged = make_token_info(data={"v": 1}, lineage_path=(self.OUTER_EXPAND,))
        _persist_token_for_scheduler(factory, merged)
        outcome = CoalesceOutcome(held=False, merged_token=merged, consumed_tokens=(), coalesce_name="merge", join_group_id="jg-1")
        emitted: list[Any] = []
        with (
            patch.object(processor, "_require_coordination_token", return_value=SimpleNamespace(worker_id="leader", epoch=1)),
            patch.object(
                processor._scheduler, "complete_barrier", side_effect=lambda **kwargs: emitted.extend(kwargs["emitted_ready"]) or 0
            ),
        ):
            disposition = processor._barrier_intake._fire_coalesce_merge(CoalesceName("merge"), outcome, scope_row_id="row-1")
        assert disposition.child_items[0].collector_name == CollectorName("outer_stitch")
        assert [(e.collector_name, e.coalesce_name) for e in emitted] == [("outer_stitch", None)]


class TestCollectorCursorLookup:
    def test_cursor_naming_an_unconfigured_collector_is_a_named_integrity_error(self) -> None:
        """C1 review M-2: every collector-cursor node lookup goes through one
        helper that raises the intake/restore paths' named error, never a
        bare KeyError from a subscript."""
        from elspeth.contracts.errors import AuditIntegrityError

        processor, setup, _clock = _build(mode=ProcessorMode.LEADER, executor=_HoldingCollectorExecutor())
        token = _expand_member(setup, sequence=7)
        with pytest.raises(AuditIntegrityError, match="cursor names collector 'ghost'"):
            processor._maybe_collector_token(token, current_node_id=NodeID(COLLECTOR_NODE), collector_name=CollectorName("ghost"))
        with pytest.raises(AuditIntegrityError, match="cursor names collector 'ghost'"):
            processor.route_collector_release(collector_name=CollectorName("ghost"), released_tokens=(token,))
        with pytest.raises(AuditIntegrityError, match="cursor names collector 'ghost'"):
            processor._process_single_token(token, _ctx(setup), NodeID(COLLECTOR_NODE), collector_name=CollectorName("ghost"))


class TestDepthTwoReleaseHoldsAtTheCollector:
    def test_union_release_inside_a_scope_travels_to_the_collector_and_holds(self) -> None:
        """C5 review M-1: the cursor is not the point — the JOURNEY is. The
        token a union releases inside a scope must reach the collector node
        and hold there (durable BLOCKED row under the compound key), driven
        through the real drain from the release's own continuation item."""
        registry = GroupBindingRegistry(
            bindings=(
                GroupBinding(
                    kind=FrameKind.EXPAND,
                    opener_node_id=NodeID("explode"),
                    opener_name="explode",
                    closer_node_id=NodeID(COLLECTOR_NODE),
                    closer_name="stitch",
                    closer_kind=CloserKind.COLLECTOR,
                    policy="require_all",
                    on_group_failure=None,
                    member_roster=(),
                ),
            )
        )
        executor = _HoldingCollectorExecutor()
        processor, setup, _clock = _build(
            mode=ProcessorMode.LEADER, executor=executor, with_union_before_collector=True, group_bindings=registry
        )
        released = _expand_member(setup, sequence=11)
        registry.register_expand_group(_group_of(released), opener_name="explode")

        [item] = processor.released_row_union_items(row_union_name=RowUnionName("variants"), released_tokens=(released,))
        assert (item.current_node_id, item.collector_name) == (NodeID(AFTER_UNION), STITCH)

        results = processor._drain_durable_work_queue(item, _ctx(setup))

        assert results == []
        blocked = setup.factory.scheduler.list_blocked_barrier_items(run_id=setup.run_id)
        # node_id is the enqueue-time cursor (after-union), never the barrier
        # owner; the compound key and the executor accept prove the journey.
        assert [(row.token_id, row.barrier_key, row.collector_name, row.node_id) for row in blocked] == [
            (released.token_id, collector_barrier_key("stitch", _group_of(released)), "stitch", AFTER_UNION),
        ]
        assert [(token_id, name) for token_id, name, _ctx_seen in executor.accepted] == [(released.token_id, "stitch")]
