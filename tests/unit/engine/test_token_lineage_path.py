"""In-memory lineage-path push/pop pins (WS1a prep; spec §4.1a differential).

These tests pin BOTH truths during the prep phase: lineage_path is the
corrected (preservative) representation, while the stored tri-fields keep
today's destructive semantics until the WS1b flip. If a stored-field
assertion here reddens, a prep slice has leaked the flip early — stop.
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
    def test_fork_children_stack_a_fork_frame_and_keep_destructive_stored_fields(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        for child, branch in zip(children, ["a", "b"], strict=True):
            assert child.lineage_path == (LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch),)
            assert child.branch_name == branch  # stored field: unchanged semantics
            assert child.fork_group_id == fork_group_id


class TestExpandPush:
    def test_expand_inside_fork_branch_stacks_and_stored_fields_stay_destructive(self) -> None:
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
            # §4.1a row 2 pinned at PREP: destructive stored semantics until WS1b —
            # expand_token drops fork_group_id, inherits branch_name in memory only.
            assert grandchild.branch_name == "a"
            assert grandchild.fork_group_id is None
            assert grandchild.expand_group_id == expand_group_id


class TestCoalesceStrictPop:
    def test_merge_pops_exactly_the_shared_fork_frame(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        merged = manager.coalesce_tokens(children, PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)
        assert merged.lineage_path == ()
        assert merged.join_group_id is not None  # stored field until Task 10

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
