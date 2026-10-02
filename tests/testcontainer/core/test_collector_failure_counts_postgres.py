"""PostgreSQL proof of the collector group-verdict counting arm (elspeth-5887fb7928 S2).

The SQLite cases (``tests/integration/pipeline/test_collector_failure_counts.py``)
prove the semantics. These send the arm's SQL to PostgreSQL:

- the terminal-outcome selection and its bounded ``node_states`` token probes,
  plus verdict-row reads, over a database that holds a prior run of the same
  pipeline, so the collector node id is shared;
- the ``failure_reason`` CHECK folded into epoch 45, reflected by name at
  startup (``LandscapeDB.from_url`` validates it) and enforced on insert;
- the plan: with sequential scans disabled, the member query still reaches
  ``node_states`` by ``token_id`` and never through ``ix_node_states_node``.
  A PostgreSQL plan depends on statistics, so this pins which index the query
  OFFERS the planner, not which one a production-sized table would pick.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline
from tests.integration.pipeline.test_collector_failure_counts import _analysis, _surfaces
from tests.integration.pipeline.test_collector_failure_verdict import (
    _COLLECTOR_PIPELINE,
    _DOCS,
    _LOSSY_DOCS,
    _LOSSY_PIPELINE,
    _Crash,
    _flip_first_call,
    _inject,
)

from elspeth.contracts.enums import RunStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import collector_group_failures_table, nodes_table
from elspeth.core.landscape.terminal_transform_failures import deciding_collector_group_failures

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


def _collector_node(db: LandscapeDB, run_id: str) -> str:
    with db.connection() as conn:
        return str(
            conn.execute(
                select(nodes_table.c.node_id).where(nodes_table.c.run_id == run_id).where(nodes_table.c.node_type == "collector")
            ).scalar_one()
        )


def test_collector_member_counts_are_per_run_and_arm_exact_on_postgres(
    postgres_db: LandscapeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run, a failed run and a lost-members run into one database: each counts only its own members."""
    _flip_first_call(monkeypatch, "returned_error")  # only the first flush fails
    failed = run_pipeline(build_pipeline(tmp_path / "failed", _COLLECTOR_PIPELINE, _DOCS, db=postgres_db))
    clean = run_pipeline(build_pipeline(tmp_path / "clean", _COLLECTOR_PIPELINE, _DOCS, db=postgres_db))
    lossy = run_pipeline(build_pipeline(tmp_path / "lossy", _LOSSY_PIPELINE, _LOSSY_DOCS, db=postgres_db))
    assert (failed.status, clean.status) == (RunStatus.FAILED, RunStatus.COMPLETED)
    node = _collector_node(postgres_db, failed.run_id)
    assert _collector_node(postgres_db, clean.run_id) == node, "control: the runs share the collector node id"

    failed_surfaces = _surfaces(postgres_db, failed.run_id)
    clean_surfaces = _surfaces(postgres_db, clean.run_id)
    lossy_surfaces = _surfaces(postgres_db, lossy.run_id)

    assert failed_surfaces.categories == [("collector_group", node, "collector_transform_error", 3)]
    assert failed_surfaces.errors == {"validation": 0, "transform": 0, "collector_group": 3, "total": 3}
    assert failed_surfaces.groups_failed_mcp == failed_surfaces.groups_failed_projection == failed_surfaces.groups_failed_accounting == 1
    assert failed_surfaces.collector_analysis == _analysis("batch_stats", node, "collector_transform_error", groups=1, members=3)
    assert clean_surfaces.categories == []
    assert clean_surfaces.errors == {"validation": 0, "transform": 0, "collector_group": 0, "total": 0}
    assert clean_surfaces.groups_failed_projection == clean_surfaces.groups_failed_accounting == 0
    assert lossy_surfaces.errors == {"validation": 0, "transform": 1, "collector_group": 2, "total": 3}
    assert lossy_surfaces.groups_failed_projection == 1


def test_the_verdict_to_terminal_window_counts_no_member_until_resume_on_postgres(
    postgres_db: LandscapeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash after the verdict: G = 1, M = 0. Resume writes the terminals: M = 3."""
    env = build_pipeline(tmp_path, _COLLECTOR_PIPELINE, _DOCS, db=postgres_db)
    _flip_first_call(monkeypatch, "returned_error")
    with monkeypatch.context() as crash_patch:
        fired = _inject(crash_patch, "after_verdict")
        with pytest.raises(_Crash):
            run_pipeline(env)
    assert fired == ["after_verdict"]
    with postgres_db.connection() as conn:
        run_id = str(conn.execute(select(collector_group_failures_table.c.run_id)).scalar_one())

    crashed = _surfaces(postgres_db, run_id)
    assert crashed.errors["collector_group"] == 0
    assert crashed.groups_failed_projection == 1

    resume_pipeline(env)

    assert _surfaces(postgres_db, run_id).errors["collector_group"] == 3


def test_the_failure_reason_check_is_enforced_on_postgres(postgres_db: LandscapeDB) -> None:
    """The CHECK exists by name (startup validated it) and rejects a code outside the vocabulary."""
    row = {"run_id": "r", "group_id": "g", "collector_node_id": "n", "failure_reason": "plugin said so", "recorded_at": datetime.now(UTC)}
    with pytest.raises(IntegrityError, match="ck_collector_group_failures_failure_reason"), postgres_db.engine.begin() as conn:
        conn.execute(collector_group_failures_table.insert().values(**row))


def test_the_member_query_reaches_node_states_by_token_on_postgres(
    postgres_db: LandscapeDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With sequential scans off, the member query's node_states access is a token index, never the node index."""
    _flip_first_call(monkeypatch, "returned_error")
    failed = run_pipeline(build_pipeline(tmp_path / "failed", _COLLECTOR_PIPELINE, _DOCS, db=postgres_db))
    run_pipeline(build_pipeline(tmp_path / "prior", _COLLECTOR_PIPELINE, _DOCS, db=postgres_db))
    sent: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _executemany: bool) -> None:
        if "node_states" in statement:
            sent.append((statement, parameters))

    with postgres_db.connection() as conn:
        event.listen(postgres_db.engine, "before_cursor_execute", capture)
        try:
            assert len(deciding_collector_group_failures(conn, (failed.run_id,))) == 3
        finally:
            event.remove(postgres_db.engine, "before_cursor_execute", capture)
        assert len(sent) == 1, sent
        [(statement, parameters)] = sent
        conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = "\n".join(str(row[0]) for row in conn.exec_driver_sql(f"EXPLAIN {statement}", parameters))
        control = "\n".join(
            str(row[0])
            for row in conn.exec_driver_sql("EXPLAIN SELECT state_id FROM node_states WHERE node_id = %(node)s", {"node": "collector_x"})
        )

    assert "ix_node_states_node" in control, f"control: a node-driven read uses the node index:\n{control}"
    assert "FROM node_states" in statement and "JOIN" not in statement, statement
    assert "node_states.run_id =" not in statement, statement
    assert "ix_node_states_node" not in plan and "ix_node_states_run" not in plan, plan
    assert "Seq Scan on node_states" not in plan, plan
    assert "ix_node_states_token" in plan and "token_id" in plan, plan
