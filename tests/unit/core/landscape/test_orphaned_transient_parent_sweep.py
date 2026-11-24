"""ADR-019 I1a run-end orphan sweep: one snapshot, linear work, unchanged result.

The sweep reads candidates and then witnesses in two steps. These tests prove
the two steps read ONE snapshot when a writer commits between them (SQLite
here; PostgreSQL in tests/testcontainer/core/test_orphaned_transient_parent_sweep_postgres.py),
that the work stays linear where the former single correlated statement was
quadratic, and that the result is the former predicate's bag.

The interleaving seam is a SQLAlchemy ``after_cursor_execute`` listener on the
reading engine: when the named statement has executed (SQLite has stepped it,
so its read snapshot exists), a second connection commits. No sleeps.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Select, column, event, literal, select, table
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Row as SQLAlchemyRow

from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape._database_ops import ReadOnlyDatabaseOps
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import token_outcomes_table, token_parents_table
from tests.fixtures.landscape import make_factory
from tests.helpers.orphaned_transient_parent_query import (
    EXPECTED_ORPHANS,
    Edge,
    Outcome,
    base_orphan_query,
    insert_graph,
    insert_outcomes,
    insert_tokens,
    make_database,
    make_stale_statistics,
    populate_equivalence_cases,
    populate_shape,
)

_CANDIDATE_MARK = "ORDER BY token_outcomes.token_id"
_WITNESS_MARK = "SELECT DISTINCT token_parents.parent_token_id"


def _bag(rows: list[SQLAlchemyRow[Any]]) -> Counter[tuple[str, str]]:
    return Counter((row.token_id, row.path) for row in rows)


def _pairs(rows: list[SQLAlchemyRow[Any]]) -> list[tuple[str, str]]:
    return [(row.token_id, row.path) for row in rows]


# ---------------------------------------------------------------------------
# The per-statement prototype ("T", r8): the negative control. Same SQL as the
# repository, but every statement runs in its own read transaction.
# ---------------------------------------------------------------------------


def _per_statement_two_step(db: LandscapeDB, run_id: str, chunk: int = 900) -> list[SQLAlchemyRow[Any]]:
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


# ---------------------------------------------------------------------------
# Concurrent-writer proof (SQLite, file-backed WAL as in production)
# ---------------------------------------------------------------------------


@contextmanager
def _commit_after_first(db: LandscapeDB, mark: str, write: Callable[[], None]) -> Iterator[list[str]]:
    """Run ``write`` (another connection) once, right after the first statement containing ``mark`` executes."""
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


@contextmanager
def _witness_id_counts(db: LandscapeDB, run_id: str) -> Iterator[list[int]]:
    """Record, per executed witness SELECT, how many parent ids it bound (string values other than ``run_id``; ``SELECT 1`` binds an int)."""
    counts: list[int] = []

    def after_cursor_execute(_conn: Connection, _cursor: Any, statement: str, parameters: Any, _context: Any, _executemany: bool) -> None:
        if _WITNESS_MARK in statement:
            counts.append(sum(1 for value in parameters if isinstance(value, str) and value != run_id))

    event.listen(db.engine, "after_cursor_execute", after_cursor_execute)
    try:
        yield counts
    finally:
        event.remove(db.engine, "after_cursor_execute", after_cursor_execute)


def _file_database(tmp_path: Path) -> tuple[str, LandscapeDB, RecorderFactory, dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{tmp_path / 'audit.db'}"
    db, factory, rows = make_database(url)
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    return url, db, factory, rows


def _reader(url: str, writer: LandscapeDB, factory: RecorderFactory, read_only: bool) -> tuple[LandscapeDB, RecorderFactory]:
    if not read_only:
        return writer, factory
    reader = LandscapeDB.from_url(url, create_tables=False, read_only=True)
    assert reader.is_read_only
    return reader, make_factory(reader)


def _write_outcomes(writer: LandscapeDB, outcomes: list[Outcome]) -> Callable[[], None]:
    def write() -> None:
        with writer.engine.begin() as conn:
            insert_outcomes(conn, outcomes, completed_tokens=set())

    return write


def _between_steps_case(db: LandscapeDB, rows: dict[str, str]) -> Callable[[], None]:
    """Before: A orphaned. The write witnesses A and adds orphan C. After: C orphaned.

    A mixed read (candidates before, witnesses after) returns NO orphan, an
    answer no snapshot has: the integrity sweep would pass a violated run.
    """
    with db.engine.begin() as conn:
        insert_graph(conn, rows, [("a-parent", "run-A", "transient", "fork_parent")], [("a-child", "a-parent", "run-A")])
        insert_graph(conn, rows, [], [("c-child", "c-parent", "run-A")])

    def write() -> None:
        with db.engine.begin() as conn:
            insert_outcomes(
                conn,
                [("a-child", "run-A", "success", "default_flow"), ("c-parent", "run-A", "transient", "fork_parent")],
                completed_tokens=set(),
            )

    return write


def _between_partitions_case(db: LandscapeDB, rows: dict[str, str]) -> Callable[[], None]:
    """901 candidates: A, 899 witnessed fillers, B. A and B are orphaned before the write.

    Candidates sort by token id, so A is in the first 900-row partition and B
    in the second. The write witnesses both; a mixed read returns "A orphan,
    B not", which no snapshot has (r8's impossible result).
    """
    outcomes: list[Outcome] = [("a-orphan", "run-A", "transient", "fork_parent"), ("z-orphan", "run-A", "transient", "fork_parent")]
    edges: list[Edge] = [("a-orphan-child", "a-orphan", "run-A"), ("z-orphan-child", "z-orphan", "run-A")]
    for index in range(899):
        outcomes.append((f"m-{index:04d}", "run-A", "transient", "fork_parent"))
        outcomes.append((f"m-{index:04d}-child", "run-A", "success", "default_flow"))
        edges.append((f"m-{index:04d}-child", f"m-{index:04d}", "run-A"))
    with db.engine.begin() as conn:
        insert_graph(conn, rows, outcomes, edges)
    return _write_outcomes(
        db, [("a-orphan-child", "run-A", "success", "default_flow"), ("z-orphan-child", "run-A", "success", "default_flow")]
    )


@pytest.mark.parametrize("read_only", [False, True], ids=["writable-engine", "read-only-engine"])
def test_writer_commit_between_candidates_and_witnesses_returns_pre_write_snapshot(tmp_path: Path, read_only: bool) -> None:
    url, db, factory, rows = _file_database(tmp_path)
    try:
        write = _between_steps_case(db, rows)
        reader, reader_factory = _reader(url, db, factory, read_only)
        try:
            with _commit_after_first(reader, _CANDIDATE_MARK, write) as fired:
                result = reader_factory.data_flow.find_orphaned_transient_parents("run-A")
            assert fired, "the writer never committed between candidate and witness reads"
            assert _pairs(result) == [("a-parent", "fork_parent")]  # the pre-write snapshot's answer
            assert _pairs(reader_factory.data_flow.find_orphaned_transient_parents("run-A")) == [("c-parent", "fork_parent")]
        finally:
            if reader is not db:
                reader.close()
    finally:
        db.close()


@pytest.mark.parametrize("read_only", [False, True], ids=["writable-engine", "read-only-engine"])
def test_writer_commit_between_witness_partitions_returns_pre_write_snapshot(tmp_path: Path, read_only: bool) -> None:
    url, db, factory, rows = _file_database(tmp_path)
    try:
        write = _between_partitions_case(db, rows)
        reader, reader_factory = _reader(url, db, factory, read_only)
        try:
            with _witness_id_counts(reader, "run-A") as bound, _commit_after_first(reader, _WITNESS_MARK, write) as fired:
                result = reader_factory.data_flow.find_orphaned_transient_parents("run-A")
            assert fired, "the writer never committed between witness partitions"
            # 901 distinct candidates: exactly two witness SELECTs, the first binding
            # the 900-id maximum, so the write above really fell between partitions.
            assert bound == [900, 1]
            assert _pairs(result) == [("a-orphan", "fork_parent"), ("z-orphan", "fork_parent")]
            assert reader_factory.data_flow.find_orphaned_transient_parents("run-A") == []
        finally:
            if reader is not db:
                reader.close()
    finally:
        db.close()


def test_negative_control_per_statement_reads_return_results_no_snapshot_has(tmp_path: Path) -> None:
    """The same two interleavings through per-statement transactions (r8's T) mix snapshots."""
    _, db, _, rows = _file_database(tmp_path)
    try:
        write = _between_steps_case(db, rows)
        with _commit_after_first(db, _CANDIDATE_MARK, write) as fired:
            mixed = _per_statement_two_step(db, "run-A")
        assert fired
        assert mixed == []  # pre-write {a-parent}, post-write {c-parent}: neither is empty
    finally:
        db.close()

    _, db, _, rows = _file_database(tmp_path / "partitions")
    try:
        write = _between_partitions_case(db, rows)
        with _commit_after_first(db, _WITNESS_MARK, write) as fired:
            mixed = _per_statement_two_step(db, "run-A")
        assert fired
        assert _pairs(mixed) == [("a-orphan", "fork_parent")]  # "A orphan, B not"
    finally:
        db.close()


def test_read_snapshot_maps_database_errors_without_statement_or_bound_values() -> None:
    sentinel = "SECRET_BOUND_VALUE_SHOULD_NOT_LEAK"
    db = LandscapeDB.in_memory()
    try:
        missing: Select[Any] = select(column("value")).select_from(table("missing_table")).where(column("value") == sentinel)
        with pytest.raises(LandscapeRecordError) as exc_info, ReadOnlyDatabaseOps(db).read_snapshot() as snapshot:
            snapshot.select_all(missing)
        message = str(exc_info.value)
        assert "read_snapshot failed" in message and "OperationalError" in message
        assert sentinel not in message and "missing_table" not in message
    finally:
        db.close()


def test_read_snapshot_holds_one_snapshot_and_its_write_barrier(tmp_path: Path) -> None:
    """Direct API proof: two SELECTs around a concurrent commit agree; query_only is armed."""
    url, db, _, rows = _file_database(tmp_path)
    reader = LandscapeDB.from_url(url, create_tables=False, read_only=True)
    try:
        count = select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.run_id == "run-A")
        query_only: Select[Any] = select(column("query_only")).select_from(table("pragma_query_only"))
        for handle in (db, reader):
            with ReadOnlyDatabaseOps(handle).read_snapshot() as snapshot:
                before = len(snapshot.select_all(count))
                with db.engine.begin() as conn:
                    insert_graph(conn, rows, [(f"t-{id(handle)}", "run-A", "success", "default_flow")], [])
                assert len(snapshot.select_all(count)) == before
                assert snapshot.select_all(query_only)[0].query_only == 1
            assert len(ReadOnlyDatabaseOps(handle).execute_fetchall(count)) == before + 1
    finally:
        reader.close()
        db.close()


# ---------------------------------------------------------------------------
# Scaling guard: SQLite VM steps on the shapes where the base was quadratic
# ---------------------------------------------------------------------------


def _sweep_steps(db: LandscapeDB, factory: RecorderFactory) -> tuple[int, list[SQLAlchemyRow[Any]]]:
    """VM steps of one production sweep on the in-memory database's single DBAPI connection."""
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 0

    with db.engine.connect() as conn:
        raw = conn.connection.driver_connection
        assert raw is not None
        raw.set_progress_handler(progress, 1)
        try:
            result = factory.data_flow.find_orphaned_transient_parents("run-A")
        finally:
            raw.set_progress_handler(None, 0)
    return steps, result


def _stored_rows(db: LandscapeDB) -> int:
    with db.engine.connect() as conn:
        return sum(conn.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one() for name in ("token_outcomes", "token_parents"))


# Measured lane maximum over the r8 matrix is recorded in the lane REPORT;
# the bound keeps headroom over it and sits orders of magnitude under base.
_STEP_INTERCEPT = 2_000
_STEPS_PER_STORED_ROW = 40


def _measure_shape(shape: str, sizes: tuple[int, int], *, stale: bool) -> list[tuple[int, int, int]]:
    """VM steps of one sweep per size, each checked against the base bag and the per-row bound."""
    measured = []
    for n in sizes:
        db, factory, rows = make_database()
        try:
            if stale:
                make_stale_statistics(db, rows)
            populate_shape(db, rows, shape, n)
            stored = _stored_rows(db)
            steps, result = _sweep_steps(db, factory)
            with db.engine.connect() as conn:
                assert _bag(result) == Counter((row.token_id, row.path) for row in conn.execute(base_orphan_query("run-A")))
        finally:
            db.close()
        measured.append((n, stored, steps))
        assert steps <= _STEP_INTERCEPT + _STEPS_PER_STORED_ROW * stored, f"{shape} {measured}"
    return measured


@pytest.mark.parametrize(
    ("shape", "sizes"),
    [
        pytest.param("fork", (250, 500), id="no-stats-fork-run"),
        pytest.param("childless", (250, 500), id="no-stats-childless"),
        pytest.param("star-plus-500", (60, 120), id="no-stats-star-plus-500"),
    ],
)
def test_sweep_vm_steps_are_linear_in_stored_rows(shape: str, sizes: tuple[int, int]) -> None:
    (_, small_rows, small_steps), (_, large_rows, large_steps) = _measure_shape(shape, sizes, stale=False)
    # Doubling the shape must not raise the work per stored row (base: ~2x per doubling).
    assert large_steps / large_rows <= 1.3 * small_steps / small_rows, (
        f"{shape} superlinear: {(small_rows, small_steps, large_rows, large_steps)}"
    )


def test_stale_statistics_sweep_stays_far_below_base_at_one_and_two_partitions() -> None:
    """Base-regression guard under stale statistics, NOT a linearity proof.

    With ``sqlite_stat1`` recording ``token_parents`` as tiny, each witness
    partition scans all of ``token_parents``, so cost per row grows with the
    number of 900-row partitions (measured 1.6-1.75x per doubling at 8k-32k
    parents). At 500 and 1,000 fork parents (one and two partitions) the bound
    still holds; base, and a witness that selects an uncovered column, exceed
    it by more than 15x.
    """
    (_, small_rows, small_steps), (_, large_rows, large_steps) = _measure_shape("fork", (500, 1000), stale=True)
    assert large_steps / large_rows <= 1.3 * small_steps / small_rows, (small_rows, small_steps, large_rows, large_steps)


# ---------------------------------------------------------------------------
# Equivalence with the former single-statement predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("read_only", [False, True], ids=["writable-engine", "read-only-engine"])
def test_multirun_fixture_bag_order_and_row_type(tmp_path: Path, read_only: bool) -> None:
    url, db, factory, rows = _file_database(tmp_path)
    try:
        with db.engine.begin() as conn:
            populate_equivalence_cases(conn, rows)
        reader, reader_factory = _reader(url, db, factory, read_only)
        try:
            for run_id, expected in EXPECTED_ORPHANS.items():
                result = reader_factory.data_flow.find_orphaned_transient_parents(run_id)
                with db.engine.connect() as conn:
                    oracle = list(conn.execute(base_orphan_query(run_id)))
                assert _bag(result) == _bag(oracle) == Counter(expected)
                assert _pairs(result) == sorted(_pairs(result))
                assert all(row._fields == ("token_id", "path") for row in result)
                assert all(type(row) is type(oracle[0]) for row in result)
            assert _bag(reader_factory.data_flow.find_orphaned_transient_parents("run-A"))[("a-fork-orphan", "fork_parent")] == 2
        finally:
            if reader is not db:
                reader.close()
    finally:
        db.close()


def test_partition_boundary_keeps_duplicates_and_order(tmp_path: Path) -> None:
    """Duplicate outcome rows straddling the 900-row partition boundary are each kept, in order."""
    db, factory, rows = make_database()
    try:
        outcomes: list[Outcome] = [(f"p-{index:04d}", "run-A", "transient", "fork_parent") for index in range(899)]
        outcomes += [("p-0899", "run-A", "transient", "fork_parent")] * 3 + [("p-0899", "run-A", "transient", "expand_parent")]
        edges: list[Edge] = [(f"p-{index:04d}-child", f"p-{index:04d}", "run-A") for index in range(0, 899, 2)]
        with db.engine.begin() as conn:
            insert_graph(conn, rows, outcomes, edges)
            insert_outcomes(
                conn, [(f"p-{index:04d}-child", "run-A", "success", "default_flow") for index in range(0, 899, 4)], completed_tokens=set()
            )
        result = factory.data_flow.find_orphaned_transient_parents("run-A")
        with db.engine.connect() as conn:
            oracle = list(conn.execute(base_orphan_query("run-A")))
        assert _bag(result) == _bag(oracle)
        assert _pairs(result) == sorted(_pairs(oracle))
        assert _pairs(result)[-4:] == [("p-0899", "expand_parent")] + [("p-0899", "fork_parent")] * 3
    finally:
        db.close()


_RANDOM_STATES = 300


def test_random_states_through_repository_match_former_predicate() -> None:
    """Deterministic random states (the r8 generator) through the repository path; controls fire."""
    db, factory, rows = make_database()
    rng = random.Random(110019)
    tokens = [(name, "run-A" if name < "d" else "run-B") for name in "abcdef"]
    try:
        with db.engine.begin() as conn:
            insert_tokens(conn, rows, dict(tokens))

        def compare(outcomes: list[Outcome], edges: list[Edge]) -> bool:
            with db.engine.begin() as conn:
                insert_outcomes(conn, outcomes, completed_tokens=set())
                if edges:
                    conn.execute(
                        token_parents_table.insert(),
                        [{"token_id": c, "parent_token_id": p, "run_id": r, "ordinal": ord(p)} for c, p, r in edges],
                    )
            try:
                agree = True
                for run_id in ("run-A", "run-B"):
                    with db.engine.connect() as conn:
                        oracle = Counter((row.token_id, row.path) for row in conn.execute(base_orphan_query(run_id)))
                    agree &= _bag(factory.data_flow.find_orphaned_transient_parents(run_id)) == oracle
                return agree
            finally:
                with db.engine.begin() as conn:
                    conn.execute(token_parents_table.delete())
                    conn.execute(token_outcomes_table.delete().where(token_outcomes_table.c.token_id.in_(list("abcdef"))))

        # Oracle controls: an orphan and its witnessed twin must differ, and both must agree.
        assert compare([("a", "run-A", "transient", "fork_parent")], [])
        assert compare([("a", "run-A", "transient", "fork_parent"), ("b", "run-A", None, "buffered")], [("b", "a", "run-A")])
        for _ in range(_RANDOM_STATES):
            outcomes: list[Outcome] = []
            edges: list[Edge] = []
            for name, run in tokens:
                for _ in range(rng.randrange(4)):
                    outcomes.append(
                        (name, run, rng.choice([None, "transient", "success"]), rng.choice(["fork_parent", "expand_parent", "buffered"]))
                    )
                for parent in "abc" if run == "run-A" else "def":
                    if rng.randrange(2):
                        edges.append((name, parent, run))
            assert compare(outcomes, edges), (outcomes, edges)
    finally:
        db.close()
