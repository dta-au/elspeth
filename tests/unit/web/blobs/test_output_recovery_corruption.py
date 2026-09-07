"""Output recovery distinguishes valid terminal races from corrupt locked rows."""

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, update

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.sessions.models import blobs_table
from tests.helpers.session_fences import seed_live_operation_context
from tests.unit.web.blobs import test_service as service_fixtures
from tests.unit.web.blobs.test_service import _seed_active_run
from tests.unit.web.blobs.test_service_fencing import _reserve_output_blob

blob_service = service_fixtures.blob_service
compose_context = service_fixtures.compose_context
db_engine = service_fixtures.db_engine
session_id = service_fixtures.session_id


async def _pending_output(
    service: BlobServiceImpl, engine: Engine, compose: SessionOperationContext
) -> tuple[UUID, SessionOperationContext, BlobRecord]:
    session = UUID(compose.fence.session_id)
    run_id = UUID(
        await _seed_active_run(
            engine,
            session,
            session_operation_context=compose,
            source={"plugin": "csv", "on_success": "out", "options": {"path": "input.csv"}},
            status="running",
        )
    )
    execute = seed_live_operation_context(engine, session, operation_kind=SessionOperationKind.EXECUTE)
    record = _reserve_output_blob(service, execute, run_id)
    Path(record.storage_path).write_bytes(b"output bytes\n")
    return run_id, execute, record


@pytest.mark.asyncio
async def test_post_snapshot_corruption_is_not_an_already_finalized_recovery(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, execute, record = await _pending_output(blob_service, db_engine, compose_context)
    storage = Path(record.storage_path)
    original_read = Path.read_bytes
    injected = False

    def corrupt_after_snapshot(path: Path) -> bytes:
        nonlocal injected
        if path == storage and not injected:
            injected = True
            # Simulate Tier-1 durable corruption AFTER initial batch validation,
            # not an invalid producer fixture. The locked recovery row must be
            # validated even when its status is no longer pending.
            with db_engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
                connection.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(status="corrupted"))
                connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", corrupt_after_snapshot)
    with pytest.raises(AuditIntegrityError, match="status"):
        await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)
    assert injected
    assert original_read(storage) == b"output bytes\n"
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.status).where(blobs_table.c.id == str(record.id))).scalar_one() == "corrupted"


@pytest.mark.asyncio
async def test_valid_terminal_race_remains_an_explicit_per_blob_error(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, execute, record = await _pending_output(blob_service, db_engine, compose_context)
    storage = Path(record.storage_path)
    original_read = Path.read_bytes
    injected = False

    def finalize_after_snapshot(path: Path) -> bytes:
        nonlocal injected
        data = original_read(path)
        if path == storage and not injected:
            injected = True
            blob_service._session_operation_authority.mutate(
                execute,
                lambda transaction: transaction.blobs.mark_run_output_blob_ready(
                    run_id=run_id,
                    blob_id=record.id,
                    size_bytes=len(data),
                    content_hash=content_hash(data),
                    max_storage_per_session=1024,
                ),
            )
        return data

    monkeypatch.setattr(Path, "read_bytes", finalize_after_snapshot)
    result = await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)
    assert injected
    assert result.finalized == ()
    assert [(error.blob_id, error.exc_type) for error in result.errors] == [(record.id, "BlobStateError")]
    assert original_read(storage) == b"output bytes\n"
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.status).where(blobs_table.c.id == str(record.id))).scalar_one() == "ready"


@pytest.mark.asyncio
async def test_recovery_refuses_a_real_predecessor_owned_pending_output(
    blob_service: BlobServiceImpl, db_engine: Engine, compose_context: SessionOperationContext
) -> None:
    run_id, predecessor, record = await _pending_output(blob_service, db_engine, compose_context)
    successor = seed_live_operation_context(db_engine, UUID(predecessor.fence.session_id), operation_kind=SessionOperationKind.EXECUTE)
    with pytest.raises(AuditIntegrityError, match="exact EXECUTE"):
        blob_service._mark_run_output_error(successor, run_id=run_id, blob_id=record.id)
    assert Path(record.storage_path).read_bytes() == b"output bytes\n"
    with db_engine.connect() as connection:
        assert connection.execute(select(blobs_table.c.status).where(blobs_table.c.id == str(record.id))).scalar_one() == "pending"
