"""In-memory lineage-path push/pop pins (WS1b flip; spec §4.1a).

branch_name/fork_group_id/expand_group_id are DERIVED, read-only accessors
over TokenInfo.lineage_path (ruling 21) — non-destructive: an expand nested
inside a fork branch still reports the outer branch identity even though the
innermost frame is the EXPAND. A row_union release is the one sanctioned
divergence (ruling 27): it pops the shared innermost FORK frame off every
released token, so a released token whose ONLY frame was that FORK frame
reports all-None accessors afterward.
"""

from __future__ import annotations

import pytest

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import FrameKind, NodeType
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.engine.tokens import TokenManager
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)


def _manager() -> tuple[TokenManager, str]:
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(setup.data_flow, "run-1", "gate-0", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
    manager = TokenManager(setup.factory.data_flow, step_resolver=lambda node_id: 1)
    return manager, "run-1"


def _root(manager: TokenManager, run_id: str) -> TokenInfo:
    from elspeth.contracts import SourceRow

    source_row = SourceRow.valid({"col": "v"}, contract=_CONTRACT, source_row_index=0)
    return manager.create_initial_token(
        run_id=run_id,
        source_node_id="source-0",
        row_index=0,
        source_row=source_row,
        source_row_index=0,
        ingest_sequence=0,
    )


class TestForkPush:
    def test_fork_children_stack_a_fork_frame(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        for child, branch in zip(children, ["a", "b"], strict=True):
            assert child.lineage_path == (LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch),)
            assert child.branch_name == branch
            assert child.fork_group_id == fork_group_id


class TestExpandPush:
    def test_expand_inside_fork_branch_stacks_and_accessors_read_the_full_path(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        (child_a, _child_b), fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        grandchildren, expand_group_id = manager.expand_token(
            child_a,
            [{"v": 1}, {"v": 2}],
            _CONTRACT,
            NodeID("gate-0"),
            run_id,
        )
        for grandchild in grandchildren:
            assert grandchild.lineage_path == (
                LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key="a"),
                LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=grandchild.token_id),
            )
            # WS1b flip: branch_name/fork_group_id/expand_group_id are derived
            # accessors over the WHOLE path (ruling 21) — non-destructive, unlike
            # the pre-flip stored tri-fields. An expand nested inside a fork
            # branch retains the outer FORK frame's branch identity even though
            # the innermost frame is the EXPAND.
            assert grandchild.branch_name == "a"
            assert grandchild.fork_group_id == fork_group_id
            assert grandchild.expand_group_id == expand_group_id


class TestCoalesceStrictPop:
    def test_merge_pops_exactly_the_shared_fork_frame(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        merged, join_group_id = manager.coalesce_tokens(children, PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)
        assert merged.lineage_path == ()
        # join_group_id is a merge-event carrier (ruling 20): the tuple's second
        # element is the only in-memory truth — TokenInfo no longer stores it.
        assert isinstance(join_group_id, str) and join_group_id

    def test_merge_refuses_a_parent_with_no_fork_frame(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fg = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        stray = root  # lineage_path == ()
        with pytest.raises(OrchestrationInvariantError, match="innermost FORK"):
            manager.coalesce_tokens([children[0], stray], PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)


class TestMemoryDurableConsistency:
    def test_in_memory_path_equals_durable_frames_after_each_primitive(self) -> None:
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_lineage_frames_table

        setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
        register_test_node(setup.data_flow, "run-1", "gate-0", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        manager = TokenManager(setup.factory.data_flow, step_resolver=lambda node_id: 1)
        root = _root(manager, "run-1")
        children, _fg = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), "run-1")
        grandchildren, _eg = manager.expand_token(children[1], [{"v": 1}], _CONTRACT, NodeID("gate-0"), "run-1")
        for token in (root, *children, *grandchildren):
            with setup.db.engine.connect() as conn:
                rows = conn.execute(
                    select(
                        token_lineage_frames_table.c.kind, token_lineage_frames_table.c.group_id, token_lineage_frames_table.c.member_key
                    )
                    .where(token_lineage_frames_table.c.token_id == token.token_id)
                    .where(token_lineage_frames_table.c.run_id == "run-1")
                    .order_by(token_lineage_frames_table.c.depth)
                ).fetchall()
            durable = tuple(LineageFrame(kind=FrameKind(r.kind), group_id=r.group_id, member_key=r.member_key) for r in rows)
            assert durable == token.lineage_path


class TestJoinCarriers:
    def test_coalesce_tokens_returns_merged_token_and_join_group_id(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fg = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        merged, join_group_id = manager.coalesce_tokens(children, PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)
        assert merged.token_id
        # join_group_id is a merge-event carrier (ruling 20): the tuple's second
        # element is the only in-memory truth — TokenInfo no longer stores it.
        assert isinstance(join_group_id, str) and join_group_id

    def test_row_result_requires_join_group_id_exactly_for_coalesced(self) -> None:
        from elspeth.contracts.enums import TerminalOutcome, TerminalPath
        from elspeth.contracts.errors import OrchestrationInvariantError
        from elspeth.contracts.results import RowResult

        manager, run_id = _manager()
        token = _root(manager, run_id)
        with pytest.raises(OrchestrationInvariantError, match="join_group_id"):
            RowResult(token=token, final_data=token.row_data, outcome=TerminalOutcome.SUCCESS, path=TerminalPath.COALESCED, sink_name="out")
        ok = RowResult(
            token=token,
            final_data=token.row_data,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.COALESCED,
            sink_name="out",
            join_group_id="jg-1",
        )
        assert ok.join_group_id == "jg-1"
        with pytest.raises(OrchestrationInvariantError, match="join_group_id"):
            RowResult(
                token=token,
                final_data=token.row_data,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.DEFAULT_FLOW,
                sink_name="out",
                join_group_id="jg-1",
            )


class TestRowUnionReleasePop:
    """Ruling 27: a row_union release pops the shared FORK frame off every
    released token — the one sanctioned divergence from "accessors read the
    whole path" (deliberately excluded from WS1a's prep pins; landed here at
    the WS1b flip). Exercises RowUnionExecutor._pop_released_group directly
    — the shared mechanism both the live accept() and restore_from_journal()
    release paths route through — rather than the full accept/release
    machinery, which test_row_union_executor.py already covers end to end.

    The popped FORK frame need not be the innermost/last frame: a
    row-multiplying transform inside a branch (e.g. an expand) stacks an
    EXPAND frame on top of the branch's FORK frame before the token reaches
    the union (elspeth-a5b86149d4;
    tests/integration/pipeline/test_row_union_branch_cardinality.py) — that
    shape must pop the FORK frame and preserve the EXPAND frame, not raise."""

    def test_released_token_with_only_the_fork_frame_has_all_none_accessors(self) -> None:
        from elspeth.engine.row_union_executor import RowUnionExecutor

        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        for child in children:
            assert child.lineage_path == (LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=child.branch_name),)

        released = RowUnionExecutor._pop_released_group(list(children))

        assert len(released) == 2
        for token in released:
            assert token.lineage_path == ()
            assert token.branch_name is None
            assert token.fork_group_id is None
            assert token.expand_group_id is None
            # token identity is preserved — only lineage_path changes.
        assert [t.token_id for t in released] == [c.token_id for c in children]

    def test_refuses_a_group_with_no_fork_frame_anywhere(self) -> None:
        from elspeth.engine.row_union_executor import RowUnionExecutor

        manager, run_id = _manager()
        root = _root(manager, run_id)
        grandchildren, _eg = manager.expand_token(root, [{"v": 1}], _CONTRACT, NodeID("gate-0"), run_id)
        with pytest.raises(OrchestrationInvariantError, match="no FORK frame"):
            RowUnionExecutor._pop_released_group(list(grandchildren))

    def test_pops_the_fork_frame_from_beneath_a_surviving_expand_frame(self) -> None:
        """A branch member that passed through a mid-branch expand has
        (FORK, EXPAND) lineage on arrival. Release pops the FORK frame from
        underneath the EXPAND frame — the expand genuinely happened, so its
        frame must survive the union closing the branch scope above it."""
        from elspeth.engine.row_union_executor import RowUnionExecutor

        manager, run_id = _manager()
        root = _root(manager, run_id)
        (control, treatment), fork_group_id = manager.fork_token(root, ["control", "treatment"], NodeID("gate-0"), run_id)
        (treatment_child,), expand_group_id = manager.expand_token(treatment, [{"v": 1}], _CONTRACT, NodeID("gate-0"), run_id)
        assert treatment_child.lineage_path == (
            LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key="treatment"),
            LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=treatment_child.token_id),
        )

        released = RowUnionExecutor._pop_released_group([control, treatment_child])

        assert len(released) == 2
        popped_control, popped_treatment = released
        assert popped_control.lineage_path == ()
        assert popped_control.branch_name is None
        assert popped_treatment.lineage_path == (
            LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=treatment_child.token_id),
        )
        assert popped_treatment.branch_name is None
        assert popped_treatment.fork_group_id is None
        assert popped_treatment.expand_group_id == expand_group_id
        assert [t.token_id for t in released] == [control.token_id, treatment_child.token_id]
