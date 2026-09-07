"""Public deletion recovers actual fork cleanup journals across commit faults."""

from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import Engine, event, select

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.blobs import service as blob_service_module
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table
from tests.helpers.session_fences import seed_live_compose_context
from tests.unit.web.blobs import test_service as service_fixtures

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id


async def _fork_cleanup_obligation(
    service: BlobServiceImpl,
    engine: Engine,
    context: SessionOperationContext,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> BlobRecord:
    record = await service.create_blob(
        UUID(context.fence.session_id), "atomic.csv", b"committed deletion", "text/csv", session_operation_context=context
    )
    original_unlink = Path.unlink
    injected = False

    def fail_purge(path: Path, missing_ok: bool = False) -> None:
        nonlocal injected
        if path.name.startswith(f".{record.id}.delete-"):
            injected = True
            raise OSError("leave fork purge obligation")
        original_unlink(path, missing_ok=missing_ok)

    with blob_service_module._blob_custody_session_lock(engine, context.fence.session_id) as held, monkeypatch.context() as faults:
        faults.setattr(Path, "unlink", fail_purge)
        with blob_service_module._blob_phase_transaction(engine, held) as connection:
            row = connection.execute(select(blobs_table).where(blobs_table.c.id == str(record.id))).one()
            stage = service._delete_fork_blob_row_locked(connection, row=row, blob_id_str=str(record.id))
        with pytest.raises(OSError, match="leave fork purge obligation"):
            service._finalize_registered_fork_blob_deletion(
                held_connection=held,
                blob_id_str=str(record.id),
                session_id_str=context.fence.session_id,
                stage=stage,
            )
    assert injected
    with engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id)).all() == []
        obligation = connection.execute(select(blob_deletion_cleanups_table)).one()
    assert obligation.phase is None
    assert obligation.operation_id is None
    assert Path(obligation.tombstone_path).read_bytes() == b"committed deletion"
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["before-commit", "after-commit"])
async def test_atomic_retirement_retries_one_shot_commit_failure(
    fault: Literal["before-commit", "after-commit"],
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _fork_cleanup_obligation(blob_service, db_engine, compose_context, tmp_path, monkeypatch)
    original_commit = db_engine.dialect.do_commit
    armed = False
    injected = False

    def arm_retirement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal armed
        if statement.lstrip().upper().startswith("DELETE FROM BLOB_DELETION_CLEANUPS"):
            armed = True

    def fail_commit(connection) -> None:
        nonlocal injected
        if armed and not injected:
            injected = True
            if fault == "after-commit":
                original_commit(connection)
            raise OSError("atomic retirement commit transport failure")
        original_commit(connection)

    event.listen(db_engine, "after_cursor_execute", arm_retirement)
    monkeypatch.setattr(db_engine.dialect, "do_commit", fail_commit)
    try:
        await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    finally:
        event.remove(db_engine, "after_cursor_execute", arm_retirement)
    assert injected
    assert list(Path(record.storage_path).parent.iterdir()) == []
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id)).all() == []
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).all() == []


@pytest.mark.asyncio
async def test_atomic_retirement_persistent_failure_preserves_successor_retry(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _fork_cleanup_obligation(blob_service, db_engine, compose_context, tmp_path, monkeypatch)
    failures = 0

    def fail_retirement(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal failures
        if statement.lstrip().upper().startswith("DELETE FROM BLOB_DELETION_CLEANUPS"):
            failures += 1
            raise OSError("retirement database unavailable")

    event.listen(db_engine, "before_cursor_execute", fail_retirement)
    try:
        with pytest.raises(OSError, match="retirement database unavailable"):
            await blob_service.delete_blob(record.id, session_operation_context=compose_context)
    finally:
        event.remove(db_engine, "before_cursor_execute", fail_retirement)
    assert failures > 0
    assert list(Path(record.storage_path).parent.iterdir()) == []
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id)).all() == []
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).scalar_one() == str(record.id)

    successor = seed_live_compose_context(db_engine, session_id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    await restarted.delete_blob(record.id, session_operation_context=successor)
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).all() == []


@pytest.mark.asyncio
async def test_atomic_retirement_custody_loss_preserves_obligation_for_successor(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    session_id: UUID,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = await _fork_cleanup_obligation(blob_service, db_engine, compose_context, tmp_path, monkeypatch)
    original_purge = blob_service_module._finalize_staged_blob_deletion
    successor: SessionOperationContext | None = None

    def purge_then_take_over(stage: blob_service_module._StagedBlobDeletion) -> None:
        nonlocal successor
        original_purge(stage)
        successor = seed_live_compose_context(db_engine, session_id)

    with monkeypatch.context() as faults:
        faults.setattr(blob_service_module, "_finalize_staged_blob_deletion", purge_then_take_over)
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.delete_blob(record.id, session_operation_context=compose_context)

    assert successor is not None
    assert list(Path(record.storage_path).parent.iterdir()) == []
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.id)).all() == []
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).scalar_one() == str(record.id)
    restarted = BlobServiceImpl(db_engine, tmp_path)
    await restarted.delete_blob(record.id, session_operation_context=successor)
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).all() == []
