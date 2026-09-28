"""PostgreSQL proof of the ``rows_coalesce_failed`` audit derive's group key (elspeth-5887fb7928 B1).

The SQLite cases (``tests/unit/core/landscape/test_query_methods.py::
TestAuditRunStatusProjection``) prove the semantics. This sends the derive's SQL
to PostgreSQL: the outer join to ``token_lineage_frames`` on the token's
deepest FORK frame, selected by a correlated ``max(depth)`` subquery in the join
condition, must name the same groups there — sibling exploded members of one
row are two barriers, the innermost of nested forks names the group, and a
FAILED barrier state whose token has no FORK frame is refused, not dropped.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.fixtures.landscape import leader_coordination_token, make_recorder_with_run, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeStateStatus, NodeType
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError, CoalesceFailureReason
from elspeth.contracts.identity import LineageFrame
from elspeth.core.landscape.database import LandscapeDB

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_db() -> Iterator[LandscapeDB]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        db = LandscapeDB.from_url(postgres_url)
        try:
            assert db.engine.dialect.name == "postgresql"
            yield db
        finally:
            db.close()


def _fork(group_id: str, branch: str) -> LineageFrame:
    return LineageFrame(kind=FrameKind.FORK, group_id=group_id, member_key=branch)


def _run_with_failed_barrier_states(db: LandscapeDB, run_id: str, tokens: dict[str, tuple[LineageFrame, ...]]):
    setup = make_recorder_with_run(run_id=run_id, source_node_id=f"source-{run_id}", source_plugin_name="json", db=db)
    factory = setup.factory
    coordination = leader_coordination_token(factory, run_id)
    coalesce_node = register_test_node(factory.data_flow, run_id, f"coalesce-{run_id}", node_type=NodeType.COALESCE, plugin_name="coalesce")
    factory.data_flow.create_row_with_token(
        f"source-{run_id}",
        0,
        {"value": 1},
        row_id=f"row-{run_id}",
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=coordination,
    )
    for token_id, frames in tokens.items():
        factory.data_flow.create_token(f"row-{run_id}", token_id=token_id, lineage_path=frames, coordination_token=coordination)
        state_id = f"state-{token_id}"
        factory.execution.begin_node_state(
            token_id, coalesce_node, 0, {"value": 1}, state_id=state_id, member_token=coordination.membership
        )
        factory.execution.complete_node_state(
            state_id=state_id,
            status=NodeStateStatus.FAILED,
            error=CoalesceFailureReason(
                failure_reason="quorum_not_met_at_timeout",
                expected_branches=("a", "b"),
                branches_arrived=("a",),
                merge_policy="union",
            ),
            duration_ms=0.0,
            member_token=coordination.membership,
        )
    return factory


def test_failed_barrier_groups_are_keyed_by_the_innermost_fork_frame_on_postgres(postgres_db: LandscapeDB) -> None:
    """One source row: two exploded members each failing their own fork group (2), and nested forks
    whose two inner groups fail under one outer fork, one of them with two branches (2)."""
    explode_1 = LineageFrame(kind=FrameKind.EXPAND, group_id="xg", member_key="m1")
    explode_2 = LineageFrame(kind=FrameKind.EXPAND, group_id="xg", member_key="m2")
    exploded = _run_with_failed_barrier_states(
        postgres_db,
        "pg-exploded",
        {
            "pg-m1-a": (explode_1, _fork("fg-m1", "a")),
            "pg-m1-b": (explode_1, _fork("fg-m1", "b")),
            "pg-m2-a": (explode_2, _fork("fg-m2", "a")),
        },
    )
    nested = _run_with_failed_barrier_states(
        postgres_db,
        "pg-nested",
        {
            "pg-i1-a": (_fork("fg-outer", "left"), _fork("fg-inner-1", "a")),
            "pg-i1-b": (_fork("fg-outer", "left"), _fork("fg-inner-1", "b")),
            "pg-i2-a": (_fork("fg-outer", "right"), _fork("fg-inner-2", "a")),
        },
    )

    assert exploded.run_status_projection.count_failed_coalesce_barrier_rows("pg-exploded") == 2
    assert nested.run_status_projection.count_failed_coalesce_barrier_rows("pg-nested") == 2


def test_failed_barrier_state_without_a_fork_frame_is_refused_on_postgres(postgres_db: LandscapeDB) -> None:
    factory = _run_with_failed_barrier_states(postgres_db, "pg-no-frame", {"pg-no-frame-tok": ()})

    with pytest.raises(AuditIntegrityError, match="no FORK lineage frame"):
        factory.run_status_projection.count_failed_coalesce_barrier_rows("pg-no-frame")
