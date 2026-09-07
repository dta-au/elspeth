"""Durable blob recovery after a committed mutation loses its acknowledgement."""

from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.blobs import BlobDeletionPlan, BlobRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.blobs import service as blob_service_module
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table
from elspeth.web.sessions.protocol import SessionOperationMutationTransaction
from tests.helpers.session_fences import seed_live_compose_context
from tests.unit.web.blobs import test_service as service_fixtures
from tests.unit.web.blobs.test_output_recovery_corruption import _pending_output
from tests.unit.web.blobs.test_service_fencing import _reserve_output_blob

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id


@pytest.mark.parametrize("lost_connection", ["closed", "invalidated"])
def test_blob_phase_rejects_lost_custody_without_reconnecting(db_engine, lost_connection: str) -> None:
    opened = 0

    def count_connect(_connection, _record) -> None:
        nonlocal opened
        opened += 1

    connection = db_engine.connect()
    if lost_connection == "closed":
        connection.close()
    else:
        connection.invalidate()
    event.listen(db_engine, "connect", count_connect)
    try:
        with (
            pytest.raises(AuditIntegrityError, match="without its custody lock"),
            blob_service_module._blob_phase_transaction(db_engine, connection),
        ):
            pytest.fail("a phase must not start after losing custody")
        assert connection.closed
        assert opened == 0
    finally:
        event.remove(db_engine, "connect", count_connect)
        connection.close()


@pytest.mark.parametrize("fault", ["reset-failure", "invalidation"])
@pytest.mark.parametrize("primary_type", [OSError, KeyboardInterrupt])
def test_blob_phase_reset_failure_is_fatal_and_preserves_primary(
    db_engine, monkeypatch: pytest.MonkeyPatch, fault: str, primary_type: type[BaseException]
) -> None:
    primary = primary_type("primary phase failure")
    opened = 0

    def count_connect(_connection, _record) -> None:
        nonlocal opened
        opened += 1

    def fail_reset() -> None:
        raise OSError("DBAPI transaction reset failed")

    with db_engine.connect() as connection:
        proxy = connection.connection
        event.listen(db_engine, "connect", count_connect)
        try:
            with monkeypatch.context() as faults:
                if fault == "reset-failure":
                    faults.setattr(proxy, "rollback", fail_reset)
                expected = AuditIntegrityError if primary_type is OSError else KeyboardInterrupt
                with pytest.raises(expected) as raised, blob_service_module._blob_phase_transaction(db_engine, connection):
                    if fault == "invalidation":
                        connection.invalidate()
                    raise primary
            if primary_type is OSError:
                assert raised.value.__cause__ is primary
                assert any("Blob transaction reset failed" in note for note in primary.__notes__)
            else:
                assert raised.value is primary
            assert connection.closed
            with (
                pytest.raises(AuditIntegrityError, match="without its custody lock"),
                blob_service_module._blob_phase_transaction(db_engine, connection),
            ):
                pytest.fail("recovery must not continue on a failed custody connection")
            assert opened == 0
        finally:
            event.remove(db_engine, "connect", count_connect)


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False], ids=["ready-commit", "error-commit"])
async def test_output_commit_ack_loss_records_error_and_continues_batch(
    success: bool,
    blob_service: BlobServiceImpl,
    db_engine,
    compose_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, execute, target = await _pending_output(blob_service, db_engine, compose_context)
    neighbor = _reserve_output_blob(blob_service, execute, run_id, filename="neighbor.csv")
    Path(neighbor.storage_path).write_bytes(b"neighbor output\n")
    authority = blob_service._session_operation_authority
    original_mutate = authority.mutate
    injected = False
    expected_status = "ready" if success else "error"

    def lose_commit_ack[T](context: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        nonlocal injected
        result = original_mutate(context, mutation)
        if isinstance(result, BlobRecord) and result.id == target.id and result.status == expected_status and not injected:
            injected = True
            raise OperationalError("COMMIT", {}, RuntimeError("output acknowledgement lost"))
        return result

    monkeypatch.setattr(authority, "mutate", lose_commit_ack)
    outcome = await blob_service.finalize_run_output_blobs(run_id, success=success, session_operation_context=execute)
    assert injected
    assert {record.id for record in outcome.finalized} == {neighbor.id}
    assert outcome.errors
    assert {error.blob_id for error in outcome.errors} == {target.id}
    assert outcome.errors[0].exc_type == "OperationalError"
    current = authority.mutate(execute, lambda transaction: transaction.blobs.read_blob(blob_id=target.id))
    assert current.status == expected_status
    if success:
        assert Path(target.storage_path).read_bytes() == b"output bytes\n"
        assert Path(neighbor.storage_path).read_bytes() == b"neighbor output\n"
        assert current.size_bytes == len(b"output bytes\n")
        assert current.content_hash == blob_service_module.content_hash(b"output bytes\n")
    else:
        assert not Path(target.storage_path).exists()
        assert not Path(neighbor.storage_path).exists()
        assert current.size_bytes == 0
        assert current.content_hash is None
    assert list(Path(target.storage_path).parent.glob(".*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("write_completed", [False, True], ids=["before-write", "after-publication"])
async def test_failed_create_releases_own_reservation_before_same_operation_retry(
    write_completed: bool,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A definite failed write cannot exhaust quota for the still-live caller."""
    service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=5)
    original_write = blob_service_module._atomic_write_blob

    def fail_writing(storage: Path, content: bytes, *, write_guard: Callable[[], None] | None = None) -> None:
        if write_guard is not None:
            write_guard()
        if write_completed:
            original_write(storage, content, write_guard=write_guard)
        raise OSError("transient file persistence failure")

    with monkeypatch.context() as faults:
        faults.setattr(blob_service_module, "_atomic_write_blob", fail_writing)
        with pytest.raises(OSError, match="transient file persistence failure"):
            await service.create_blob(session_id, "failed.csv", b"12345", "text/csv", session_operation_context=compose_context)

    recovered = await service.create_blob(session_id, "retry.csv", b"1", "text/csv", session_operation_context=compose_context)
    assert Path(recovered.storage_path).read_bytes() == b"1"
    assert list(Path(recovered.storage_path).parent.iterdir()) == [Path(recovered.storage_path)]
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id, blobs_table.c.status, blobs_table.c.size_bytes)).all() == [
            (str(recovered.id), "ready", 1)
        ]


@pytest.mark.asyncio
async def test_create_ready_commit_ack_loss_preserves_published_bytes(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed ready row must never lose bytes during failure cleanup."""
    original_commit = db_engine.dialect.do_commit
    armed = False
    injected = False

    def arm_ready_commit(_conn, _cursor, statement, parameters, _context, _many) -> None:
        nonlocal armed
        if statement.lstrip().upper().startswith("UPDATE BLOBS SET") and parameters[0] == "ready":
            armed = True

    def lose_ready_ack(connection) -> None:
        nonlocal injected
        original_commit(connection)
        if armed and not injected:
            injected = True
            raise ConnectionError("ready commit acknowledgement lost")

    event.listen(db_engine, "after_cursor_execute", arm_ready_commit)
    monkeypatch.setattr(db_engine.dialect, "do_commit", lose_ready_ack)
    try:
        record = await blob_service.create_blob(
            session_id,
            "ack.csv",
            b"committed content\n",
            "text/csv",
            session_operation_context=compose_context,
        )
    finally:
        event.remove(db_engine, "after_cursor_execute", arm_ready_commit)

    assert injected, "the ready metadata commit must have completed before the injected exception"
    assert record.status == "ready"
    assert Path(record.storage_path).read_bytes() == b"committed content\n"
    assert await blob_service.read_blob_content(record.id, session_operation_context=compose_context) == b"committed content\n"
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id, blobs_table.c.status)).all() == [(str(record.id), "ready")]


@pytest.mark.asyncio
async def test_create_reservation_ack_loss_keeps_same_operation_quota_usable(
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost pending commit acknowledgement cannot strand an unknown reservation."""
    service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=5)
    original_commit = db_engine.dialect.do_commit
    armed = False
    injected = False

    def arm_reservation_commit(_conn, _cursor, statement, _parameters, context, _many) -> None:
        nonlocal armed
        if statement.lstrip().upper().startswith("INSERT INTO BLOBS "):
            assert context.compiled_parameters[0]["status"] == "pending"
            armed = True

    def lose_reservation_ack(connection) -> None:
        nonlocal injected
        original_commit(connection)
        if armed and not injected:
            injected = True
            raise ConnectionError("pending commit acknowledgement lost")

    first = None
    event.listen(db_engine, "after_cursor_execute", arm_reservation_commit)
    with monkeypatch.context() as faults:
        faults.setattr(db_engine.dialect, "do_commit", lose_reservation_ack)
        try:
            first = await service.create_blob(session_id, "first.csv", b"1234", "text/csv", session_operation_context=compose_context)
        except ConnectionError as exc:
            assert str(exc) == "pending commit acknowledgement lost"
        finally:
            event.remove(db_engine, "after_cursor_execute", arm_reservation_commit)

    assert injected
    second = await service.create_blob(session_id, "second.csv", b"1", "text/csv", session_operation_context=compose_context)
    expected_ids = {str(second.id)}
    if first is not None:
        assert Path(first.storage_path).read_bytes() == b"1234"
        expected_ids.add(str(first.id))
    assert Path(second.storage_path).read_bytes() == b"1"
    with db_engine.connect() as connection:
        rows = connection.execute(select(blobs_table.c.id, blobs_table.c.status)).all()
    assert {row.id for row in rows} == expected_ids, "a failed create must not leave an unknown quota-consuming row"
    assert all(row.status == "ready" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["intent", "staged", "purge_pending", "retired"])
async def test_delete_recovers_each_committed_phase_after_ack_loss(
    phase: Literal["intent", "staged", "purge_pending", "retired"],
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await blob_service.create_blob(
        session_id, "delete.csv", b"exact deletion bytes", "text/csv", session_operation_context=compose_context
    )
    authority = blob_service._session_operation_authority
    original_mutate = authority.mutate
    injected = False

    def lose_phase_ack[T](context: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        nonlocal injected
        result = original_mutate(context, mutation)
        matches_phase = isinstance(result, BlobDeletionPlan) and result.phase == phase
        if not injected and (matches_phase or (phase == "retired" and result is True)):
            injected = True
            raise ConnectionError("deletion commit acknowledgement lost")
        return result

    monkeypatch.setattr(authority, "mutate", lose_phase_ack)
    await blob_service.delete_blob(record.id, session_operation_context=compose_context)

    assert injected, "the selected phase must commit before the injected exception"
    assert list(Path(record.storage_path).parent.iterdir()) == []
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id)).all() == []
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).all() == []


@pytest.mark.asyncio
async def test_delete_unobservable_committed_phase_preserves_retry_obligation(
    blob_service: BlobServiceImpl,
    db_engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable reconciliation must leave committed tombstones for a successor."""
    content = b"retain until purge can be proven"
    record = await blob_service.create_blob(session_id, "retry.csv", content, "text/csv", session_operation_context=compose_context)
    authority = blob_service._session_operation_authority
    original_mutate = authority.mutate
    original_cas = authority.compare_and_swap
    committed = False
    observation_refused = False

    def lose_committed_ack[T](context: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        nonlocal committed
        result = original_mutate(context, mutation)
        if isinstance(result, BlobDeletionPlan) and result.phase == "purge_pending" and not committed:
            committed = True
            raise ConnectionError("delete committed but acknowledgement lost")
        return result

    def refuse_observation(context: SessionOperationContext) -> None:
        nonlocal observation_refused
        if committed:
            observation_refused = True
            raise ConnectionError("database unavailable for reconciliation")
        original_cas(context)

    monkeypatch.setattr(authority, "mutate", lose_committed_ack)
    monkeypatch.setattr(authority, "compare_and_swap", refuse_observation)
    with pytest.raises(ConnectionError, match="delete committed but acknowledgement lost"):
        await blob_service.delete_blob(record.id, session_operation_context=compose_context)

    assert committed and observation_refused
    storage = Path(record.storage_path)
    assert not storage.exists(), "uncertain committed deletion must not resurrect canonical bytes"
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id)).all() == []
        obligation = connection.execute(select(blob_deletion_cleanups_table)).one()
    assert obligation.phase == "purge_pending"
    assert Path(obligation.tombstone_path).read_bytes() == content

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    await restarted.delete_blob(record.id, session_operation_context=successor)

    assert list(storage.parent.iterdir()) == []
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).all() == []
