"""Readers must not reconcile a live deletion as an abandoned filesystem stage."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import Connection, Engine, event, select, text
from tests.unit.web.blobs import test_fork_deletion_recovery_ack as fork_ack_proofs
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog

from elspeth.contracts.advisory_locks import ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, ELSPETH_SESSIONS_LOCK_CLASSID
from elspeth.contracts.blobs import BlobNotFoundError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl, _blob_custody_session_lock
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.tools import execute_tool
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["definite-rollback", "commit-ack-lost", "commit-event-pending"])
async def test_postgres_fork_cleanup_reconciles_actual_commit_outcome(
    blob_read_race_deployment: tuple[Engine, Engine, SessionServiceImpl, Path],
    monkeypatch: pytest.MonkeyPatch,
    outcome: fork_ack_proofs.ForkDeletionCommitOutcome,
) -> None:
    writer_engine, reader_engine, sessions, shared = blob_read_race_deployment
    session = await sessions.create_session("test-user", "Fork acknowledgement", "local")
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    deletion_seen = False
    custody_connection: Connection | None = None
    custody_backend_pid: int | None = None
    observed_retained_custody = False

    def observe_deletion(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal deletion_seen
        if statement.lstrip().upper().startswith("DELETE FROM BLOBS"):
            deletion_seen = True

    def remember_pending_backend(connection: Connection) -> None:
        nonlocal custody_connection, custody_backend_pid
        if deletion_seen and custody_connection is None:
            custody_connection = connection
            custody_backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()

    def observe_recovery_lock(connection, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal observed_retained_custody
        if custody_connection is None or observed_retained_custody or "pg_advisory_xact_lock" not in statement:
            return
        # The next phase is about to acquire SESSIONS for outcome observation.
        # Its preceding transaction reset must retain the original backend's
        # session-level BLOB_CUSTODY lock, visible from an independent backend.
        assert connection is custody_connection
        with reader_engine.connect() as observer:
            locks = observer.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :classid AND pid = :pid AND granted AND mode = 'ExclusiveLock'"
                ),
                {"classid": ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, "pid": custody_backend_pid},
            ).scalar_one()
        assert locks == 1
        observed_retained_custody = True

    if outcome == "commit-event-pending":
        event.listen(writer_engine, "after_cursor_execute", observe_deletion)
        event.listen(writer_engine, "commit", remember_pending_backend)
        event.listen(writer_engine, "before_cursor_execute", observe_recovery_lock)
    try:
        await fork_ack_proofs.test_fork_cleanup_recovers_exact_deletion_commit_outcome(
            outcome=outcome,
            blob_service=BlobServiceImpl(writer_engine, shared, session_operation_authority=authority),
            db_engine=writer_engine,
            compose_context=context,
            session_id=session.id,
            monkeypatch=monkeypatch,
        )
        if outcome == "commit-event-pending":
            assert observed_retained_custody
    finally:
        if outcome == "commit-event-pending":
            event.remove(writer_engine, "before_cursor_execute", observe_recovery_lock)
            event.remove(writer_engine, "commit", remember_pending_backend)
            event.remove(writer_engine, "after_cursor_execute", observe_deletion)
        authority.release(context)


@pytest.fixture
def blob_read_race_deployment(
    external_deployment_postgres_url: str,
    tmp_path: Path,
) -> Iterator[tuple[Engine, Engine, SessionServiceImpl, Path]]:
    writer_engine = create_session_engine(external_deployment_postgres_url)
    reader_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(writer_engine)
    sessions = SessionServiceImpl(
        writer_engine,
        tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.blob-read-custody-race"),
        session_operation_lease_seconds=60,
    )
    try:
        yield writer_engine, reader_engine, sessions, tmp_path
    finally:
        reader_engine.dispose()
        writer_engine.dispose()


@pytest.mark.parametrize("contender", ["public_read", "get_blob_content", "update_blob", "delete_blob"])
def test_postgres_content_reader_waits_for_live_deletion_before_reconciliation(
    blob_read_race_deployment: tuple[Engine, Engine, SessionServiceImpl, Path],
    monkeypatch: pytest.MonkeyPatch,
    contender: str,
) -> None:
    """Park deletion after rename: another replica must wait, then see deletion.

    Both workers carry the same live COMPOSE context, as concurrent tools can.
    The reader must wait in the BLOB_CUSTODY domain before taking SESSIONS;
    taking SESSIONS first would deadlock the writer's next fenced DB phase.
    """
    writer_engine, reader_engine, sessions, shared = blob_read_race_deployment
    writer = BlobServiceImpl(writer_engine, shared)
    reader = BlobServiceImpl(reader_engine, shared)
    session = asyncio.run(sessions.create_session(f"read-race-{uuid4()}", "Read race", "local"))
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    try:
        blob = asyncio.run(writer.create_blob(session.id, "source.csv", b"x\n1\n", "text/csv", session_operation_context=context))
        storage = Path(blob.storage_path)
        staged = threading.Event()
        resume_deletion = threading.Event()
        reader_lock_attempted = threading.Event()
        reader_backend_pids: set[int] = set()
        original_replace = os.replace
        catalog = _mock_catalog()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        policy_catalog = PolicyCatalogView.for_trained_operator(catalog, snapshot)

        def contend_for_blob() -> bytes | ToolResult:
            if contender == "public_read":
                return asyncio.run(reader.read_blob_content(blob.id, session_operation_context=context))
            arguments = {"blob_id": str(blob.id)}
            if contender == "update_blob":
                arguments["content"] = "x\n2\n"
            return execute_tool(
                contender,
                arguments,
                _empty_state(),
                policy_catalog,
                plugin_snapshot=snapshot,
                session_engine=reader_engine,
                session_id=str(session.id),
                data_dir=str(shared),
                session_operation_context=context,
                session_operation_authority=authority,
                user_message_id=str(uuid4()),
                user_message_content="x\n2\n",
            )

        def pause_after_deletion_rename(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            original_replace(source, target)
            if Path(source) == storage and Path(target).name.startswith(f".{blob.id}.delete-"):
                staged.set()
                assert resume_deletion.wait(timeout=15), "deletion barrier was never released"

        def observe_reader_lock(
            conn: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _execution_context: object,
            _executemany: bool,
        ) -> None:
            if "pg_advisory" in statement and "unlock" not in statement:
                reader_backend_pids.add(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                reader_lock_attempted.set()

        monkeypatch.setattr("elspeth.web.blobs.service.os.replace", pause_after_deletion_rename)
        event.listen(reader_engine, "before_cursor_execute", observe_reader_lock)
        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                delete_future = workers.submit(lambda: asyncio.run(writer.delete_blob(blob.id, session_operation_context=context)))
                try:
                    assert staged.wait(timeout=10), "deletion never reached the post-rename barrier"
                    assert not storage.exists()
                    read_future = workers.submit(contend_for_blob)
                    assert reader_lock_attempted.wait(timeout=10), "reader never requested an advisory lock"

                    waiting_on_custody = False
                    deadline = monotonic() + 5
                    with writer_engine.connect() as observer:
                        while monotonic() < deadline and not read_future.done():
                            waiting_on_custody = observer.exec_driver_sql(
                                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = ANY(%s) "
                                "AND locktype = 'advisory' AND classid = %s AND NOT granted)",
                                (list(reader_backend_pids), ELSPETH_BLOB_CUSTODY_LOCK_CLASSID),
                            ).scalar_one()
                            if waiting_on_custody:
                                holds_session_lock = observer.exec_driver_sql(
                                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = ANY(%s) "
                                    "AND locktype = 'advisory' AND classid = %s AND granted)",
                                    (list(reader_backend_pids), ELSPETH_SESSIONS_LOCK_CLASSID),
                                ).scalar_one()
                                assert not holds_session_lock, "reader held SESSIONS while waiting for BLOB_CUSTODY"
                                break
                            sleep(0.01)

                    assert waiting_on_custody, (
                        "reader crossed the live deletion's custody boundary instead of waiting: "
                        f"reader_done={read_future.done()}, canonical_restored={storage.exists()}"
                    )
                    assert not storage.exists(), "reader reconciled a live deletion tombstone"
                finally:
                    resume_deletion.set()

                delete_future.result(timeout=10)
                if contender == "public_read":
                    with pytest.raises(BlobNotFoundError):
                        read_future.result(timeout=10)
                else:
                    result = read_future.result(timeout=10)
                    assert isinstance(result, ToolResult)
                    assert not result.success
                    assert "not found" in result.data["error"].lower()
                assert not storage.exists()
                assert list(storage.parent.glob(f".{blob.id}.delete-*")) == []
        finally:
            event.remove(reader_engine, "before_cursor_execute", observe_reader_lock)
    finally:
        authority.release(context)


@pytest.mark.parametrize("file_present", [True, False], ids=["staged-bytes", "missing-bytes"])
def test_postgres_atomic_cleanup_producer_is_recoverable_by_public_delete(
    blob_read_race_deployment: tuple[Engine, Engine, SessionServiceImpl, Path],
    file_present: bool,
) -> None:
    """Retry real atomic-producer evidence, including NULL tombstone and timestamps."""
    writer_engine, reader_engine, sessions, shared = blob_read_race_deployment
    writer = BlobServiceImpl(writer_engine, shared)
    recovery = BlobServiceImpl(reader_engine, shared)
    session = asyncio.run(sessions.create_session(f"atomic-retry-{uuid4()}", "Atomic retry", "local"))
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    try:
        blob = asyncio.run(writer.create_blob(session.id, "target.csv", b"x\n1\n", "text/csv", session_operation_context=context))
        neighbor = asyncio.run(writer.create_blob(session.id, "neighbor.csv", b"x\n2\n", "text/csv", session_operation_context=context))
        storage = Path(blob.storage_path)
        if not file_present:
            storage.unlink()

        # This is the current fork-cleanup producer, not a synthesized nullable
        # ledger row. Stop after its real transaction commits, before its caller
        # would purge the stage: the same durable state as process interruption.
        with (
            _blob_custody_session_lock(writer_engine, str(session.id)),
            locked_session_transaction(writer_engine, str(session.id)) as conn,
        ):
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob.id))).one()
            stage = writer._delete_fork_blob_row_locked(conn, row=row, blob_id_str=str(blob.id))
        assert not storage.exists()
        if file_present:
            assert stage.tombstone is not None
            assert stage.tombstone.read_bytes() == b"x\n1\n"
        else:
            assert stage.tombstone is None

        authority.release(context)
        context = authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions.session_operation_owner_instance_id,
            lease_seconds=60,
        )
        obligation = authority.mutate(context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=blob.id))
        assert obligation is not None
        assert obligation.blob_id == blob.id
        assert obligation.storage_path == str(storage)
        assert obligation.tombstone_path == (str(stage.tombstone) if stage.tombstone is not None else None)
        assert obligation.created_at == obligation.updated_at
        assert obligation.created_at.tzinfo is not None

        asyncio.run(recovery.delete_blob(blob.id, session_operation_context=context))

        with reader_engine.connect() as conn:
            assert conn.execute(select(blobs_table.c.id).where(blobs_table.c.id == str(blob.id))).first() is None
            assert (
                conn.execute(
                    select(blob_deletion_cleanups_table.c.blob_id).where(blob_deletion_cleanups_table.c.blob_id == str(blob.id))
                ).first()
                is None
            )
        assert not storage.exists()
        if stage.tombstone is not None:
            assert not stage.tombstone.exists()
        assert asyncio.run(recovery.get_blob(neighbor.id, session_operation_context=context)) == neighbor
        assert asyncio.run(recovery.read_blob_content(neighbor.id, session_operation_context=context)) == b"x\n2\n"
    finally:
        authority.release(context)
