"""Real PostgreSQL proofs for run-scoped audit foreign keys."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import NodeType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import rows_table, token_parents_table, tokens_table, validation_errors_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


@pytest.fixture
def postgres_db(postgres_url: str) -> Iterator[LandscapeDB]:
    db = LandscapeDB.from_url(postgres_url)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.timeout(120)
def test_postgres_rejects_cross_run_token_parent(postgres_db: LandscapeDB) -> None:
    factory = make_factory(postgres_db)
    child_run = factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id="token-parent-child-run",
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    child_source = register_test_node(
        factory.data_flow,
        child_run.run_id,
        "token-parent-child-source",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    _child_row, child = factory.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(factory, child_run.run_id),
        source_node_id=child_source,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"side": "child"},
    )

    parent_run = factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id="token-parent-parent-run",
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    parent_source = register_test_node(
        factory.data_flow,
        parent_run.run_id,
        "token-parent-parent-source",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    _parent_row, parent = factory.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(factory, parent_run.run_id),
        source_node_id=parent_source,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"side": "parent"},
    )

    values: dict[str, object] = {
        "token_id": child.token_id,
        "parent_token_id": parent.token_id,
        "ordinal": 0,
        "run_id": child_run.run_id,
    }

    with pytest.raises(IntegrityError), postgres_db.write_connection() as conn:
        conn.execute(token_parents_table.insert().values(**values))


def _seed_cross_run_validation_row(factory: RecorderFactory, *, suffix: str) -> tuple[str, str, str]:
    run_a = f"validation-{suffix}-run-a"
    run_b = f"validation-{suffix}-run-b"
    for run_id in (run_a, run_b):
        factory.run_lifecycle.begin_run(
            config={},
            canonical_version="v1",
            run_id=run_id,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
    node_a = register_test_node(
        factory.data_flow,
        run_a,
        f"validation-{suffix}-node-a",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    node_b = register_test_node(
        factory.data_flow,
        run_b,
        f"validation-{suffix}-node-b",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    row_b, _token_b = factory.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(factory, run_b),
        source_node_id=node_b,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"invalid": True},
    )
    return run_a, node_a, row_b.row_id


@pytest.mark.timeout(120)
def test_postgres_rejects_raw_cross_run_validation_error_row_link(postgres_db: LandscapeDB) -> None:
    factory = make_factory(postgres_db)
    run_a, node_a, row_b = _seed_cross_run_validation_row(factory, suffix="raw")
    error_id = "verr_pg_cross_run_raw"

    with pytest.raises(IntegrityError), postgres_db.write_connection() as conn:
        conn.execute(
            validation_errors_table.insert().values(
                error_id=error_id,
                run_id=run_a,
                node_id=node_a,
                row_id=row_b,
                row_hash="0" * 64,
                row_data_json="{}",
                error="invalid row",
                schema_mode="fixed",
                destination="discard",
                created_at=datetime.now(UTC),
            )
        )

    with postgres_db.read_only_connection() as conn:
        assert (
            conn.execute(select(validation_errors_table.c.error_id).where(validation_errors_table.c.error_id == error_id)).fetchone()
            is None
        )


@pytest.mark.timeout(120)
def test_postgres_public_writer_rejects_cross_run_validation_error_row_link(postgres_db: LandscapeDB) -> None:
    factory = make_factory(postgres_db)
    run_a, node_a, row_b = _seed_cross_run_validation_row(factory, suffix="writer")

    with pytest.raises(AuditIntegrityError, match="cross-run contamination"):
        factory.data_flow.record_validation_error(
            coordination_token=leader_coordination_token(factory, run_a),
            node_id=node_a,
            row_id=row_b,
            row_data={"invalid": True},
            error="invalid row",
            schema_mode="fixed",
            destination="discard",
        )

    with postgres_db.read_only_connection() as conn:
        assert conn.execute(select(validation_errors_table.c.error_id).where(validation_errors_table.c.run_id == run_a)).fetchone() is None


@pytest.mark.timeout(120)
def test_postgres_validation_error_link_is_compare_and_set(
    postgres_db: LandscapeDB,
    postgres_url: str,
) -> None:
    """Concurrent same-run linkers cannot silently overwrite row lineage."""
    first_factory = make_factory(postgres_db)
    run_id = "validation-link-cas-run"
    first_factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id=run_id,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    node_id = register_test_node(
        first_factory.data_flow,
        run_id,
        "validation-link-cas-node",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    coordination_token = leader_coordination_token(first_factory, run_id)
    error_id = first_factory.data_flow.record_validation_error(
        coordination_token=coordination_token,
        node_id=node_id,
        row_data={"invalid": True},
        error="invalid row",
        schema_mode="fixed",
        destination="discard",
    )

    second_db = LandscapeDB.from_url(postgres_url)
    second_factory = make_factory(second_db)
    winner_holds_seat = threading.Event()
    loser_reached_seat = threading.Event()
    release_winner = threading.Event()
    backends: dict[str, int] = {}
    outcomes: dict[str, str | BaseException] = {}
    lock = threading.Lock()

    def before_execute(conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if not statement.upper().startswith("UPDATE RUN_COORDINATION SET"):
            return
        name = threading.current_thread().name
        with lock:
            backends[name] = int(conn.connection.driver_connection.info.backend_pid)
        if name == "validation-link-second":
            loser_reached_seat.set()

    def after_execute(_conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if statement.upper().startswith("UPDATE RUN_COORDINATION SET") and threading.current_thread().name == "validation-link-first":
            winner_holds_seat.set()
            if not release_winner.wait(timeout=30):
                raise TimeoutError("test did not release validation-link leader seat")

    def link(factory: RecorderFactory, index: int) -> None:
        try:
            row, _token = factory.data_flow.create_quarantine_row_with_token(
                source_node_id=node_id,
                row_index=index,
                source_row_index=index,
                ingest_sequence=index,
                data={"candidate": index},
                validation_error_id=error_id,
                coordination_token=coordination_token,
            )
            result: str | BaseException = row.row_id
        except BaseException as exc:
            result = exc
        with lock:
            outcomes[threading.current_thread().name] = result

    threads = [
        threading.Thread(target=link, name="validation-link-first", args=(first_factory, 0)),
        threading.Thread(target=link, name="validation-link-second", args=(second_factory, 1)),
    ]
    for db in (postgres_db, second_db):
        event.listen(db.engine, "before_cursor_execute", before_execute)
        event.listen(db.engine, "after_cursor_execute", after_execute)
    try:
        threads[0].start()
        assert winner_holds_seat.wait(timeout=10)
        threads[1].start()
        assert loser_reached_seat.wait(timeout=10)
        winner_pid = backends["validation-link-first"]
        loser_pid = backends["validation-link-second"]
        assert winner_pid != loser_pid
        with postgres_db.read_only_connection() as conn:
            for _ in range(200):
                blocked = conn.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (winner_pid, loser_pid)).scalar_one()
                if blocked:
                    break
                threading.Event().wait(0.01)
        assert blocked, "PostgreSQL did not report the contender waiting on the incumbent seat lock"
        assert "validation-link-second" not in outcomes
        release_winner.set()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()

        winner = outcomes["validation-link-first"]
        loser = outcomes["validation-link-second"]
        assert isinstance(winner, str)
        assert isinstance(loser, AuditIntegrityError)
        assert "already linked to row" in str(loser)
        with postgres_db.read_only_connection() as conn:
            linked_row_id = conn.execute(
                select(validation_errors_table.c.row_id).where(validation_errors_table.c.error_id == error_id)
            ).scalar_one()
            durable_rows = conn.execute(select(rows_table.c.row_id).where(rows_table.c.run_id == run_id)).scalars().all()
            durable_tokens = conn.execute(select(tokens_table.c.row_id).where(tokens_table.c.run_id == run_id)).scalars().all()
        assert linked_row_id == winner
        assert durable_rows == [winner]
        assert durable_tokens == [winner]
    finally:
        release_winner.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=30)
        for db in (postgres_db, second_db):
            event.remove(db.engine, "before_cursor_execute", before_execute)
            event.remove(db.engine, "after_cursor_execute", after_execute)
        second_db.close()
