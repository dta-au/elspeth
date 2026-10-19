"""Archiving run history preserves its physical output quota obligations."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.plugins.sinks._local_file_effects import commit_local_effect
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.quota_authority import storage_bytes_used_on_connection
from elspeth.web.sessions.models import output_storage_reservations_table
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.blobs import test_output_storage_admission as outputs

blob_service = outputs.blob_service
compose_context = outputs.compose_context
db_engine = outputs.db_engine
session_id = outputs.session_id


def test_output_reservation_requires_durable_run_history(
    db_engine: Engine, compose_context: SessionOperationContext, tmp_path: Path
) -> None:
    with pytest.raises(IntegrityError), db_engine.begin() as conn:
        conn.execute(
            insert(output_storage_reservations_table).values(
                target_path=str(tmp_path / "outputs" / compose_context.fence.session_id / "result.txt"),
                session_id=compose_context.fence.session_id,
                run_id=str(uuid4()),
                size_bytes=5,
                custody_operation_id=compose_context.fence.operation_id,
                custody_operation_epoch=compose_context.fence.operation_epoch,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True], ids=["crashed-stage", "committed-output"])
async def test_archived_run_retains_output_bytes_and_storage_charge_across_recovery(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    run_id, execute = await outputs._run(db_engine, compose_context)
    outputs._cap(db_engine, "test-user", 10)
    target = tmp_path / "outputs" / execute.fence.session_id / "result.txt"
    with blob_service.run_output_write_admission(run_id, session_operation_context=execute):
        prepared = outputs._prepare(target, (b"hello",))
        if committed:
            commit_local_effect(prepared)
    assert outputs._reserved(db_engine, target) == 5
    retained = target if committed else next(target.parent.glob("*.stage"))
    assert retained.read_bytes() == b"hello"
    assert sum(path.stat().st_size for path in target.parent.iterdir() if path.is_file()) == 5

    sessions = SessionServiceImpl(
        db_engine,
        tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.output-archive"),
        owner_instance_id="archive-owner",
    )
    await sessions.update_run_status(run_id, "failed", error="worker stopped", session_operation_context=execute)
    sessions.session_operation_authority.release(execute)

    def unexpected_physical_delete(_context: SessionOperationContext) -> None:
        pytest.fail("A run-backed session must be soft archived before physical deletion is considered")

    monkeypatch.setattr(sessions.session_operation_authority, "archive_delete", unexpected_physical_delete)
    archived_session = UUID(execute.fence.session_id)
    await sessions.archive_session(archived_session)
    assert (await sessions.get_session(archived_session)).archived_at is not None
    assert outputs._reserved(db_engine, target) == 5
    assert retained.read_bytes() == b"hello"
    with db_engine.connect() as conn:
        assert storage_bytes_used_on_connection(conn, identity_id="test-user") == 5
        assert storage_bytes_used_on_connection(conn, identity_id=None) == 5

    replica = SessionServiceImpl(
        db_engine,
        tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.output-archive-recovery"),
        owner_instance_id="recovery-owner",
    )
    assert await replica.reconcile_consumed_archives() == 0
    assert outputs._reserved(db_engine, target) == 5
    assert retained.read_bytes() == b"hello"
    assert not (tmp_path / ".archive_quarantine").exists()
