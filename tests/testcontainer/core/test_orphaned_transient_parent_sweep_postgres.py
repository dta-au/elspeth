"""PostgreSQL proof that the I1a run-end orphan sweep reads ONE snapshot.

PostgreSQL's default READ COMMITTED takes a new snapshot per statement, so the
two-step sweep (candidates, then witnesses) is only consistent because
``ReadOnlyDatabaseOps.read_snapshot`` sets REPEATABLE READ before its first
SELECT. A second connection commits between the steps through a SQLAlchemy
``after_cursor_execute`` listener (no sleeps); the sweep must return the
pre-write snapshot's answer, while the per-statement prototype (r8's T) mixes
snapshots. SQLite twins live in
tests/unit/core/landscape/test_orphaned_transient_parent_sweep.py.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import Select, column, event, insert, literal, select, table
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Row as SQLAlchemyRow
from tests.helpers.orphaned_transient_parent_query import (
    EXPECTED_ORPHANS,
    Edge,
    Outcome,
    base_orphan_query,
    insert_graph,
    insert_outcomes,
    make_database,
    populate_equivalence_cases,
)
from tests.helpers.postgres_target import postgres_test_target

from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape._database_ops import ReadOnlyDatabaseOps
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.schema import token_outcomes_table, token_parents_table

pytestmark = pytest.mark.testcontainer

_CANDIDATE_MARK = "ORDER BY token_outcomes.token_id"
_WITNESS_MARK = "SELECT DISTINCT token_parents.parent_token_id"


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


def _pairs(rows: list[SQLAlchemyRow[Any]]) -> list[tuple[str, str]]:
    return [(row.token_id, row.path) for row in rows]


@contextmanager
def _commit_after_first(db: LandscapeDB, mark: str, write: Callable[[], None]) -> Iterator[list[str]]:
    fired: list[str] = []

    def after_cursor_execute(_conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if not fired and mark in statement:
            fired.append(statement)
            write()

    event.listen(db.engine, "after_cursor_execute", after_cursor_execute)
    try:
        yield fired
    finally:
        event.remove(db.engine, "after_cursor_execute", after_cursor_execute)


def _per_statement_two_step(db: LandscapeDB, run_id: str, chunk: int = 900) -> list[SQLAlchemyRow[Any]]:
    """r8's T: the repository's SQL with each statement in its own READ COMMITTED transaction."""
    ops = ReadOnlyDatabaseOps(db)
    candidates = (
        select(token_outcomes_table.c.token_id, token_outcomes_table.c.path)
        .where(token_outcomes_table.c.run_id == run_id)
        .where(token_outcomes_table.c.path.in_(("fork_parent", "expand_parent")))
        .where(token_outcomes_table.c.outcome == "transient")
        .order_by(token_outcomes_table.c.token_id, token_outcomes_table.c.path)
    )
    rows = ops.execute_fetchall(candidates)
    ids = list(dict.fromkeys(row.token_id for row in rows))
    child = token_outcomes_table.alias("child_outcomes")
    witness = select(literal(1)).where(child.c.token_id == token_parents_table.c.token_id).where(child.c.run_id == run_id).exists()
    witnessed: set[str] = set()
    for start in range(0, len(ids), chunk):
        query = (
            select(token_parents_table.c.parent_token_id)
            .distinct()
            .where(token_parents_table.c.run_id == run_id)
            .where(token_parents_table.c.parent_token_id.in_(ids[start : start + chunk]))
            .where(witness)
        )
        witnessed.update(row.parent_token_id for row in ops.execute_fetchall(query))
    return [row for row in rows if row.token_id not in witnessed]


def _between_steps_case(db: LandscapeDB, writer: LandscapeDB, rows: dict[str, str]) -> Callable[[], None]:
    """Before: A orphaned. The write witnesses A and adds orphan C. A mixed read finds no orphan."""
    with db.engine.begin() as conn:
        insert_graph(conn, rows, [("a-parent", "run-A", "transient", "fork_parent")], [("a-child", "a-parent", "run-A")])
        insert_graph(conn, rows, [], [("c-child", "c-parent", "run-A")])

    def write() -> None:
        with writer.engine.begin() as conn:
            insert_outcomes(
                conn,
                [("a-child", "run-A", "success", "default_flow"), ("c-parent", "run-A", "transient", "fork_parent")],
                completed_tokens=set(),
            )

    return write


def _between_partitions_case(db: LandscapeDB, writer: LandscapeDB, rows: dict[str, str]) -> Callable[[], None]:
    """A in the first 900-row partition, B in the second; the write witnesses both."""
    outcomes: list[Outcome] = [("a-orphan", "run-A", "transient", "fork_parent"), ("z-orphan", "run-A", "transient", "fork_parent")]
    edges: list[Edge] = [("a-orphan-child", "a-orphan", "run-A"), ("z-orphan-child", "z-orphan", "run-A")]
    for index in range(899):
        outcomes.append((f"m-{index:04d}", "run-A", "transient", "fork_parent"))
        outcomes.append((f"m-{index:04d}-child", "run-A", "success", "default_flow"))
        edges.append((f"m-{index:04d}-child", f"m-{index:04d}", "run-A"))
    with db.engine.begin() as conn:
        insert_graph(conn, rows, outcomes, edges)

    def write() -> None:
        with writer.engine.begin() as conn:
            insert_outcomes(
                conn,
                [("a-orphan-child", "run-A", "success", "default_flow"), ("z-orphan-child", "run-A", "success", "default_flow")],
                completed_tokens=set(),
            )

    return write


def test_writer_commit_between_candidates_and_witnesses_returns_pre_write_snapshot(postgres_url: str) -> None:
    db, factory, rows = make_database(postgres_url)
    writer = LandscapeDB.from_url(postgres_url, create_tables=False)
    try:
        write = _between_steps_case(db, writer, rows)
        with _commit_after_first(db, _CANDIDATE_MARK, write) as fired:
            result = factory.data_flow.find_orphaned_transient_parents("run-A")
        assert fired
        assert _pairs(result) == [("a-parent", "fork_parent")]
        assert _pairs(factory.data_flow.find_orphaned_transient_parents("run-A")) == [("c-parent", "fork_parent")]
    finally:
        writer.close()
        db.close()


def test_writer_commit_between_witness_partitions_returns_pre_write_snapshot(postgres_url: str) -> None:
    db, factory, rows = make_database(postgres_url)
    writer = LandscapeDB.from_url(postgres_url, create_tables=False)
    try:
        write = _between_partitions_case(db, writer, rows)
        with _commit_after_first(db, _WITNESS_MARK, write) as fired:
            result = factory.data_flow.find_orphaned_transient_parents("run-A")
        assert fired
        assert _pairs(result) == [("a-orphan", "fork_parent"), ("z-orphan", "fork_parent")]
        assert factory.data_flow.find_orphaned_transient_parents("run-A") == []
    finally:
        writer.close()
        db.close()


def test_negative_control_per_statement_reads_mix_snapshots(postgres_url: str) -> None:
    db, _, rows = make_database(postgres_url)
    writer = LandscapeDB.from_url(postgres_url, create_tables=False)
    try:
        write = _between_steps_case(db, writer, rows)
        with _commit_after_first(db, _CANDIDATE_MARK, write) as fired:
            mixed = _per_statement_two_step(db, "run-A")
        assert fired
        assert mixed == []  # pre-write {a-parent}, post-write {c-parent}
        with db.engine.begin() as conn:
            conn.execute(token_outcomes_table.delete().where(token_outcomes_table.c.run_id == "run-A"))
            conn.execute(token_parents_table.delete().where(token_parents_table.c.run_id == "run-A"))
        write = _between_partitions_case(db, writer, rows)
        with _commit_after_first(db, _WITNESS_MARK, write) as fired:
            mixed = _per_statement_two_step(db, "run-A")
        assert fired
        assert _pairs(mixed) == [("a-orphan", "fork_parent")]  # "A orphan, B not"
    finally:
        writer.close()
        db.close()


def test_read_snapshot_is_repeatable_read_and_read_only(postgres_url: str) -> None:
    db, _, _ = make_database(postgres_url)
    try:
        ops = ReadOnlyDatabaseOps(db)
        isolation: Select[Any] = (
            select(column("setting")).select_from(table("pg_settings")).where(column("name") == "transaction_isolation")
        )
        read_only: Select[Any] = (
            select(column("setting")).select_from(table("pg_settings")).where(column("name") == "transaction_read_only")
        )
        with ops.read_snapshot() as snapshot:
            assert snapshot.select_all(isolation)[0].setting == "repeatable read"
            assert snapshot.select_all(read_only)[0].setting == "on"
        # A Select carrying a data-modifying CTE is refused by the READ ONLY transaction.
        writes = (
            insert(token_outcomes_table)
            .from_select([token_outcomes_table.c.outcome_id], select(literal("never")).where(literal(False)))
            .cte("writes")
        )
        with pytest.raises(LandscapeRecordError, match="read_snapshot failed"), ops.read_snapshot() as snapshot:
            snapshot.select_all(select(literal(1)).add_cte(writes))
    finally:
        db.close()


def test_multirun_fixture_bag_on_postgres(postgres_url: str) -> None:
    db, factory, rows = make_database(postgres_url)
    try:
        with db.engine.begin() as conn:
            populate_equivalence_cases(conn, rows)
        for run_id, expected in EXPECTED_ORPHANS.items():
            result = factory.data_flow.find_orphaned_transient_parents(run_id)
            with db.engine.connect() as conn:
                oracle = Counter((row.token_id, row.path) for row in conn.execute(base_orphan_query(run_id)))
            # Order is ORDER BY (token_id, path) under the database collation; only the bag is compared here.
            assert Counter(_pairs(result)) == oracle == Counter(expected)
    finally:
        db.close()
