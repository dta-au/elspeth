"""Audit database admission, encrypted options, and durable cursor refusals."""

from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import delete, event, inspect, select, update
from sqlalchemy.engine import Connection, ExecutionContext, make_url
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import BarrierEmission, TokenWorkItem
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape import database as database_module
from elspeth.core.landscape.database import (
    LandscapeDB,
    LandscapeSchemaShape,
    SchemaCompatibilityError,
    _missing_additive_indexes,
    _safe_database_descriptor,
    probe_schema_shape,
)
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.scheduler.work_items import insert_work_items_idempotent, prepare_fresh_pending_sink_item
from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH, schema_identity_table, token_work_items_table
from elspeth.core.schema_identity import SchemaStoreKind, insert_schema_identity
from tests.fixtures.landscape import (
    RecorderSetup,
    landscape_database_now,
    leader_coordination_token,
    leader_member_token,
    make_recorder_with_run,
    register_test_node,
)


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:", "sqlite:///file:audit.db?uri=true"])
def test_read_only_admission_requires_a_plain_existing_file_path(url: str) -> None:
    with pytest.raises(ValueError, match=r"file-backed|plain SQLite file path"):
        LandscapeDB.from_url(url, read_only=True, create_tables=False)


@pytest.mark.parametrize("create_tables,dump_to_jsonl,message", [(True, False, "create_tables=False"), (False, True, "dump_to_jsonl")])
def test_read_only_handle_rejects_mutating_construction_options(
    tmp_path: Path, create_tables: bool, dump_to_jsonl: bool, message: str
) -> None:
    path = tmp_path / "must-not-be-created.db"
    with pytest.raises(ValueError, match=message):
        LandscapeDB.from_url(f"sqlite:///{path}", read_only=True, create_tables=create_tables, dump_to_jsonl=dump_to_jsonl)
    assert not path.exists()


def test_relative_read_only_path_preserves_committed_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with LandscapeDB.from_url("sqlite:///audit.db"):
        pass
    with LandscapeDB.from_url("sqlite:///audit.db", read_only=True, create_tables=False) as db:
        assert db.is_read_only
        assert probe_schema_shape(db.engine) is LandscapeSchemaShape.MATCHES
        with pytest.raises(OperationalError, match=r"readonly|read-only"), db.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA query_only=OFF")
            conn.execute(delete(schema_identity_table))


def test_static_snapshot_in_read_only_directory_uses_immutable_file_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    path = snapshot_dir / "audit.db"
    with LandscapeDB.from_url(f"sqlite:///{path}"):
        pass
    assert not Path(f"{path}-wal").exists()
    snapshot_dir.chmod(0o555)
    real_access = database_module.os.access

    def directory_access(candidate: str | Path, mode: int) -> bool:
        # Root's DAC bypass in CI containers does not model a read-only mount.
        # Control only that filesystem observation; all database I/O is real.
        return False if Path(candidate) == snapshot_dir else real_access(candidate, mode)

    monkeypatch.setattr(database_module.os, "access", directory_access)
    try:
        readonly_url = make_url(LandscapeDB._sqlite_read_only_url(f"sqlite:///{path}"))
        assert readonly_url.query["immutable"] == "1"
        with LandscapeDB.from_url(f"sqlite:///{path}", read_only=True, create_tables=False) as db:
            assert probe_schema_shape(db.engine) is LandscapeSchemaShape.MATCHES
    finally:
        snapshot_dir.chmod(0o755)


def test_non_sqlite_read_only_url_helper_preserves_backend_options() -> None:
    url = "postgresql+psycopg://localhost/audit?sslmode=verify-full"
    assert LandscapeDB._sqlite_read_only_url(url) == url


def test_non_sqlite_journal_boundary_accepts_only_an_explicit_destination(tmp_path: Path) -> None:
    url = "postgresql+psycopg://localhost/audit"
    destination = str(tmp_path / "audit.jsonl")
    assert LandscapeDB._resolve_journal_path(url, explicit_path=destination) == destination
    with pytest.raises(ValueError, match="dump_to_jsonl_path"):
        LandscapeDB._derive_journal_path(url)
    assert list(tmp_path.iterdir()) == []


def test_database_constructor_publishes_a_real_committed_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "audit.db"
    journal = path.with_suffix(".journal.jsonl")
    with LandscapeDB(f"sqlite:///{path}", dump_to_jsonl=True, dump_to_jsonl_fail_on_error=True) as db:
        with db.write_connection() as conn:
            conn.execute(update(schema_identity_table).values(application_id="sidecar-control"))
        assert journal.exists()
        published = journal.read_text()
        assert "schema_identity" in published
        assert "sidecar-control" in published


@pytest.mark.parametrize("factory", [LandscapeDB, LandscapeDB.from_url], ids=["constructor", "factory"])
def test_encrypted_construction_rejects_engine_kwargs_before_opening_a_file(tmp_path: Path, factory: Callable[..., LandscapeDB]) -> None:
    path = tmp_path / "never-opened.db"
    with pytest.raises(ValueError, match="does not accept SQLAlchemy engine kwargs"):
        factory(f"sqlite:///{path}", passphrase="fixture-encryption-key", pool_pre_ping=True)
    assert not path.exists()


def test_real_sqlcipher_constructor_round_trips_numeric_and_uri_options(tmp_path: Path) -> None:
    sqlcipher3 = pytest.importorskip("sqlcipher3")
    path = tmp_path / "encrypted.db"
    url = f"sqlite:///{path}?timeout=1.75&detect_types=0&cached_statements=32&isolation_level=IMMEDIATE&mode=rwc&cache=private"
    engine = LandscapeDB._create_sqlcipher_engine(url, "fixture-encryption-key")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 1750
            conn.exec_driver_sql("CREATE TABLE option_control (value TEXT)")
            conn.exec_driver_sql("INSERT INTO option_control VALUES ('encrypted')")
            conn.commit()
    finally:
        engine.dispose()
    reopened = LandscapeDB._create_sqlcipher_engine(f"sqlite:///{path}?mode=ro", "fixture-encryption-key", read_only=True)
    try:
        with reopened.connect() as conn:
            assert conn.exec_driver_sql("SELECT value FROM option_control").scalar_one() == "encrypted"
            with pytest.raises(sqlcipher3.OperationalError, match=r"readonly|read-only"):
                conn.exec_driver_sql("DELETE FROM option_control")
    finally:
        reopened.dispose()


def test_real_sqlcipher_database_constructor_initializes_a_current_schema(tmp_path: Path) -> None:
    pytest.importorskip("sqlcipher3")
    path = tmp_path / "constructor-encrypted.db"
    with LandscapeDB(f"sqlite:///{path}", passphrase="fixture-constructor-key") as db:
        assert probe_schema_shape(db.engine) is LandscapeSchemaShape.MATCHES
        with db.connection() as conn:
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == SQLITE_SCHEMA_EPOCH


def test_corrupt_schema_identity_is_refused_without_replacing_its_evidence() -> None:
    with LandscapeDB.in_memory() as db:
        with db.write_connection() as conn:
            conn.execute(update(schema_identity_table).values(store_kind="session"))
        with pytest.raises(SchemaCompatibilityError, match=r"identity mismatch.*store_kind"):
            db._sync_schema_identity()
        with db.connection() as conn:
            assert conn.execute(select(schema_identity_table.c.store_kind)).scalar_one() == "session"


@pytest.mark.parametrize("competing_store_kind", ["landscape", "session"])
def test_schema_identity_sync_rechecks_a_competing_initializer_under_write_intent(
    tmp_path: Path, competing_store_kind: SchemaStoreKind
) -> None:
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'initializer-race.db'}") as db:
        with db.write_connection() as conn:
            conn.execute(delete(schema_identity_table))
        inserted: list[str] = []

        def initialize_after_first_probe(
            conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool
        ) -> None:
            if inserted or not statement.lstrip().startswith("SELECT") or "elspeth_schema_identity" not in statement:
                return
            inserted.append(competing_store_kind)
            # A second real pooled connection commits while the first reader
            # still holds its empty WAL snapshot. The writer must re-read.
            with db.write_connection() as peer:
                insert_schema_identity(peer, schema_identity_table, store_kind=competing_store_kind, schema_epoch=SQLITE_SCHEMA_EPOCH)

        event.listen(db.engine, "after_cursor_execute", initialize_after_first_probe)
        try:
            if competing_store_kind == "session":
                with pytest.raises(SchemaCompatibilityError, match=r"identity mismatch.*store_kind"):
                    db._sync_schema_identity()
            else:
                db._sync_schema_identity()
        finally:
            event.remove(db.engine, "after_cursor_execute", initialize_after_first_probe)
        assert inserted == [competing_store_kind]
        with db.connection() as conn:
            assert conn.execute(select(schema_identity_table.c.store_kind)).scalars().all() == [competing_store_kind]


def test_schema_probe_refuses_an_identity_table_with_the_wrong_column_shape(tmp_path: Path) -> None:
    with LandscapeDB.from_url(f"sqlite:///{tmp_path / 'audit.db'}") as db:
        assert probe_schema_shape(db.engine) is LandscapeSchemaShape.MATCHES
        with db.write_connection() as conn:
            conn.exec_driver_sql("DROP TABLE elspeth_schema_identity")
            conn.exec_driver_sql("CREATE TABLE elspeth_schema_identity (singleton_id INTEGER PRIMARY KEY)")
        assert probe_schema_shape(db.engine) is LandscapeSchemaShape.DIVERGENT


def test_missing_index_owner_cannot_satisfy_its_additive_indexes() -> None:
    with LandscapeDB.in_memory() as db:
        with db.write_connection() as conn:
            conn.exec_driver_sql("DROP TABLE tokens")
        inspector = inspect(db.engine)
        missing = _missing_additive_indexes(inspector, set(inspector.get_table_names()))
        assert "ix_tokens_run_id" in missing
        assert "ix_nodes_run_id" not in missing


def test_schema_epoch_sync_refuses_a_populated_old_store_without_migration() -> None:
    with LandscapeDB.in_memory() as db:
        db._set_sqlite_schema_epoch(1)
        with pytest.raises(SchemaCompatibilityError, match="does not migrate it in place"):
            db._sync_sqlite_schema_epoch()
        assert db._get_sqlite_schema_epoch() == 1


def test_schema_admission_refuses_an_empty_file_stamped_for_another_epoch(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "empty-stale.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 1")
    with pytest.raises(SchemaCompatibilityError, match="schema epoch is incompatible"):
        LandscapeDB.from_url(f"sqlite:///{path}")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_unparseable_database_diagnostic_is_fully_redacted() -> None:
    assert _safe_database_descriptor("this is not a database URL") == "<unparseable database URL redacted>"
    assert _safe_database_descriptor("sqlite:///audit.db") == "sqlite:///audit.db"


@pytest.fixture
def setup() -> Iterator[RecorderSetup]:
    setup = make_recorder_with_run()
    register_test_node(setup.factory.data_flow, setup.run_id, "normalize")
    try:
        yield setup
    finally:
        setup.db.close()


def _enqueue(setup: RecorderSetup) -> TokenWorkItem:
    row, token = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        0,
        {"id": 1},
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
    )
    return setup.factory.scheduler.enqueue_ready(
        member_token=leader_member_token(setup.factory, setup.run_id),
        token_id=token.token_id,
        row_id=row.row_id,
        node_id="normalize",
        step_index=1,
        ingest_sequence=0,
        row_payload_json=setup.factory.scheduler.serialize_row_payload(
            PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
        ),
    )


def test_scheduler_refuses_a_token_scheduled_against_another_owned_row(setup: RecorderSetup) -> None:
    item = _enqueue(setup)
    row, _ = setup.factory.data_flow.create_row_with_token(
        setup.source_node_id,
        1,
        {"id": 2},
        source_row_index=1,
        ingest_sequence=1,
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
    )
    with pytest.raises(AuditIntegrityError, match="belongs to row_id"):
        setup.factory.scheduler.enqueue_ready(
            member_token=leader_member_token(setup.factory, setup.run_id),
            token_id=item.token_id,
            row_id=row.row_id,
            node_id="normalize",
            step_index=1,
            ingest_sequence=1,
            row_payload_json=item.row_payload_json,
        )
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table.c.row_id)).scalars().all() == [item.row_id]


def test_scheduler_refuses_corrupt_lineage_when_hydrating_an_existing_cursor(setup: RecorderSetup) -> None:
    item = _enqueue(setup)
    with setup.db.write_connection() as conn:
        conn.execute(update(token_work_items_table).values(lineage_path_json="{}"))
    with pytest.raises(AuditIntegrityError, match=r"Corrupt token_work_items\.lineage_path_json"):
        setup.factory.scheduler.claim_ready(
            member_token=leader_member_token(setup.factory, setup.run_id),
            lease_owner=leader_member_token(setup.factory, setup.run_id).worker_id,
            lease_seconds=60,
        )
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table.c.work_item_id)).scalar_one() == item.work_item_id


def test_claiming_a_disappeared_cursor_snapshot_returns_no_work(setup: RecorderSetup) -> None:
    item = _enqueue(setup)
    member = leader_member_token(setup.factory, setup.run_id)
    with setup.db.write_connection() as conn:
        stale_snapshot = conn.execute(select(token_work_items_table)).mappings().one()
        conn.execute(delete(token_work_items_table))
        assert (
            setup.factory.scheduler.leases.claim_ready_row(
                conn,
                row=stale_snapshot,
                run_id=setup.run_id,
                lease_owner=member.worker_id,
                lease_seconds=60,
            )
            is None
        )
    with setup.db.connection() as conn:
        assert (
            conn.execute(
                select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.work_item_id == item.work_item_id)
            ).all()
            == []
        )


@pytest.mark.parametrize("operation", ["claim", "heartbeat"])
def test_lease_verbs_reject_a_claimant_that_does_not_own_its_authority(setup: RecorderSetup, operation: str) -> None:
    item = _enqueue(setup)
    member = leader_member_token(setup.factory, setup.run_id)
    with pytest.raises(ValueError, match="lease owner must match authority token"):
        if operation == "claim":
            setup.factory.scheduler.claim_ready(member_token=member, lease_owner="other-worker", lease_seconds=60)
        else:
            setup.factory.scheduler.heartbeat_lease(
                member_token=member, work_item_id=item.work_item_id, lease_owner="other-worker", lease_seconds=60
            )
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table.c.lease_owner)).scalar_one() is None


def test_empty_continuation_batch_does_not_emit_any_work(setup: RecorderSetup) -> None:
    with setup.db.write_connection() as conn:
        assert insert_work_items_idempotent(conn, values=[], operation="empty continuation") == frozenset()
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table)).all() == []


def test_duplicate_continuation_images_are_reconciled_once(setup: RecorderSetup) -> None:
    item = _enqueue(setup)
    with setup.db.write_connection() as conn:
        image = dict(conn.execute(select(token_work_items_table)).mappings().one())
        conn.execute(delete(token_work_items_table))
        inserted = insert_work_items_idempotent(conn, values=[image, dict(image)], operation="duplicate continuation")
        assert inserted == frozenset({item.work_item_id})
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table.c.work_item_id)).scalars().all() == [item.work_item_id]


def test_conflicting_duplicate_continuations_refuse_and_scrub_both_sensitive_images(setup: RecorderSetup) -> None:
    item = _enqueue(setup)
    with setup.db.write_connection() as conn:
        image = dict(conn.execute(select(token_work_items_table)).mappings().one())
        conflicting = {**image, "pending_error_message": "private diagnostic text"}
        with pytest.raises(LandscapeRecordError, match="incompatible existing work item") as caught:
            insert_work_items_idempotent(conn, values=[image, conflicting], operation="conflicting continuation")
    message = str(caught.value)
    assert "private diagnostic text" not in message
    assert "<redacted none>" in message
    assert "<redacted bytes=" in message
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table.c.work_item_id)).scalar_one() == item.work_item_id
        assert conn.execute(select(token_work_items_table.c.pending_error_message)).scalar_one() is None


@pytest.mark.parametrize("invalid_cursor", ["node", "row", "step", "sequence"])
def test_fresh_sink_emission_requires_a_complete_terminal_cursor(setup: RecorderSetup, invalid_cursor: str) -> None:
    item = _enqueue(setup)
    emission = BarrierEmission(
        token_id=item.token_id,
        row_payload_json=item.row_payload_json,
        node_id="normalize" if invalid_cursor == "node" else None,
        row_id=None if invalid_cursor == "row" else item.row_id,
        step_index=None if invalid_cursor == "step" else 1,
        ingest_sequence=None if invalid_cursor == "sequence" else 0,
    )
    database_now = landscape_database_now(setup.db.engine)
    with pytest.raises(AuditIntegrityError, match=r"terminal lane|complete resume cursor"), setup.db.write_connection() as conn:
        prepare_fresh_pending_sink_item(
            conn,
            run_id=setup.run_id,
            emission=emission,
            context={},
            database_now=database_now,
            parked_lease_owner=None,
            refusal_prefix="test sink emission",
        )
    with setup.db.connection() as conn:
        assert conn.execute(select(token_work_items_table.c.work_item_id)).scalars().all() == [item.work_item_id]
