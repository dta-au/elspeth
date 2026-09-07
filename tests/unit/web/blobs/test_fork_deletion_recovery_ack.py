"""Fork deletion distinguishes rollback, committed acknowledgement loss, and pending commit failure."""

from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine, event, select

from elspeth.contracts.blobs import BlobForkFenceLostError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table, guided_operations_table
from tests.unit.web.blobs import test_service as fixtures

blob_service = fixtures.blob_service
compose_context = fixtures.compose_context
db_engine = fixtures.db_engine
session_id = fixtures.session_id

type ForkDeletionCommitOutcome = Literal["definite-rollback", "commit-ack-lost", "commit-event-pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["definite-rollback", "commit-ack-lost", "commit-event-pending"])
async def test_fork_cleanup_recovers_exact_deletion_commit_outcome(
    outcome: ForkDeletionCommitOutcome,
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    session_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = fixtures.TestCopyBlobsForFork
    target_session = helper._insert_session(db_engine, forked_from_session_id=session_id)
    source = await blob_service.create_blob(
        session_id, "source.csv", b"source bytes", "text/csv", session_operation_context=compose_context
    )
    copied = await helper._copy(blob_service, session_id, target_session)
    operation = helper._fail_fork(blob_service, session_id, target_session)
    target = copied[source.id]
    storage = Path(target.storage_path)
    original_commit = db_engine.dialect.do_commit
    armed = False
    injected = False

    def arm_delete(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal armed
        if statement.lstrip().upper().startswith("DELETE FROM BLOBS"):
            armed = True

    def interrupt_commit(connection) -> None:
        nonlocal injected
        if armed and not injected:
            injected = True
            if outcome == "commit-ack-lost":
                original_commit(connection)
            else:
                connection.rollback()
            raise OSError("fork deletion commit interruption")
        original_commit(connection)

    def interrupt_pending_commit(connection: Connection) -> None:
        nonlocal injected
        if armed and not injected:
            injected = True
            assert connection.in_transaction()
            assert not storage.exists()
            raise OSError("fork deletion commit event interruption")

    event.listen(db_engine, "after_cursor_execute", arm_delete)
    try:
        with monkeypatch.context() as faults:
            if outcome == "commit-event-pending":
                event.listen(db_engine, "commit", interrupt_pending_commit)
            else:
                faults.setattr(db_engine.dialect, "do_commit", interrupt_commit)
            result = await blob_service.cleanup_blobs_for_fork(session_id, target_session, operation)
    finally:
        event.remove(db_engine, "after_cursor_execute", arm_delete)
        if outcome == "commit-event-pending":
            event.remove(db_engine, "commit", interrupt_pending_commit)
    assert injected
    assert result.errors
    with db_engine.connect() as conn:
        live = conn.execute(select(blobs_table).where(blobs_table.c.id == str(target.id))).one_or_none()
        cleanup = conn.execute(
            select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(target.id))
        ).one_or_none()
    if outcome == "commit-ack-lost":
        assert live is None
        assert cleanup is not None
        assert not storage.exists()
        assert Path(cleanup.tombstone_path).read_bytes() == b"source bytes"
    else:
        assert live is not None
        assert cleanup is None
        assert storage.read_bytes() == b"source bytes"
        assert list(storage.parent.iterdir()) == [storage]
    assert Path(source.storage_path).read_bytes() == b"source bytes"
    retried = await blob_service.cleanup_blobs_for_fork(session_id, target_session, operation)
    assert not retried.errors
    assert not storage.exists()
    assert list(storage.parent.iterdir()) == []
    with db_engine.connect() as conn:
        assert conn.execute(select(blobs_table.c.id).where(blobs_table.c.id == str(target.id))).first() is None
        assert (
            conn.execute(
                select(blob_deletion_cleanups_table.c.blob_id).where(blob_deletion_cleanups_table.c.blob_id == str(target.id))
            ).first()
            is None
        )
    assert Path(source.storage_path).read_bytes() == b"source bytes"


@pytest.mark.asyncio
async def test_fork_commit_ack_loss_with_takeover_preserves_stage_and_fence_error(
    blob_service: BlobServiceImpl,
    db_engine: Engine,
    compose_context: SessionOperationContext,
    session_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = fixtures.TestCopyBlobsForFork
    target_session = helper._insert_session(db_engine, forked_from_session_id=session_id)
    source = await blob_service.create_blob(
        session_id, "source.csv", b"source bytes", "text/csv", session_operation_context=compose_context
    )
    plan = await helper._plan(blob_service, session_id, target_session)
    stale_fence = await helper._authorize_copy(blob_service, session_id, target_session, plan)
    copied = await blob_service.copy_blobs_for_fork(session_id, target_session, plan, stale_fence, checkpoint=helper._checkpoint)
    target = copied[source.id]
    successor = replace(stale_fence, lease_token="successor-cleanup-lease", attempt=stale_fence.attempt + 1)
    original_commit = db_engine.dialect.do_commit
    armed = False
    injected = False

    def arm_delete(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        nonlocal armed
        if statement.lstrip().upper().startswith("DELETE FROM BLOBS"):
            armed = True

    def lose_ack_after_takeover(connection) -> None:
        nonlocal injected
        original_commit(connection)
        if armed and not injected:
            injected = True
            with db_engine.begin() as takeover:
                changed = takeover.execute(
                    guided_operations_table.update()
                    .where(
                        guided_operations_table.c.session_id == str(session_id),
                        guided_operations_table.c.operation_id == stale_fence.operation_id,
                        guided_operations_table.c.lease_token == stale_fence.lease_token,
                        guided_operations_table.c.attempt == stale_fence.attempt,
                    )
                    .values(lease_token=successor.lease_token, attempt=successor.attempt)
                ).rowcount
                assert changed == 1
            raise OSError("fork delete acknowledgement lost after takeover")

    event.listen(db_engine, "after_cursor_execute", arm_delete)
    try:
        with monkeypatch.context() as faults:
            faults.setattr(db_engine.dialect, "do_commit", lose_ack_after_takeover)
            with pytest.raises(BlobForkFenceLostError):
                await blob_service.cleanup_blobs_for_fork(
                    session_id, target_session, stale_fence.operation_id, live_write_fence=stale_fence
                )
    finally:
        event.remove(db_engine, "after_cursor_execute", arm_delete)
    assert injected
    with db_engine.connect() as conn:
        assert conn.execute(select(blobs_table.c.id).where(blobs_table.c.id == str(target.id))).first() is None
        cleanup = conn.execute(select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(target.id))).one()
    assert not Path(target.storage_path).exists()
    assert Path(cleanup.tombstone_path).read_bytes() == b"source bytes"
    recovered = await blob_service.cleanup_blobs_for_fork(session_id, target_session, successor.operation_id, live_write_fence=successor)
    assert not recovered.errors
    assert not Path(cleanup.tombstone_path).exists()
    assert Path(source.storage_path).read_bytes() == b"source bytes"
