"""tests/unit/core/landscape/test_load_lineage_paths.py"""

from __future__ import annotations

import pytest

from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.core.landscape.schema import token_lineage_frames_table
from tests.unit.core.landscape.test_token_recording import _make_row, _setup


def test_load_lineage_paths_batches_and_orders_by_depth() -> None:
    _db, factory = _setup()
    row, root = _make_row(factory)
    children, fork_group = factory.data_flow.fork_token(
        parent_ref=TokenRef(token_id=root.token_id, run_id="run-1"),
        row_id=row.row_id,
        branches=["path-a", "path-b"],
        step_in_pipeline=1,
    )
    paths = factory.data_flow.load_lineage_paths("run-1", [root.token_id, children[0].token_id, children[1].token_id, "tok-absent"])
    assert paths[root.token_id] == ()  # root token: no frames rows
    assert paths["tok-absent"] == ()  # unknown id: still present, empty
    assert paths[children[0].token_id] == (LineageFrame(kind=FrameKind.FORK, group_id=fork_group, member_key="path-a"),)
    assert paths[children[1].token_id] == (LineageFrame(kind=FrameKind.FORK, group_id=fork_group, member_key="path-b"),)


def test_load_lineage_paths_rejects_non_dense_depths() -> None:
    db, factory = _setup()
    _row, root = _make_row(factory)
    with db.write_connection() as conn:
        conn.execute(
            token_lineage_frames_table.insert().values(
                token_id=root.token_id,
                run_id="run-1",
                depth=1,  # gap: no depth 0
                kind="fork",
                group_id="fg-x",
                member_key="a",
            )
        )
    with pytest.raises(AuditIntegrityError, match="non-dense"):
        factory.data_flow.load_lineage_paths("run-1", [root.token_id])
