"""Atomic deletion evidence is distinct from operation-qualified phase plans."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs import BlobAtomicDeletionObligation, BlobRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blob_deletion_cleanups_table, blobs_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.helpers.session_fences import seed_live_operation_context
from tests.unit.web.blobs.test_service_fencing import _insert_session


@pytest.fixture
def db_engine() -> Engine:
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    return engine


@pytest.fixture
def session_id(db_engine: Engine) -> UUID:
    return _insert_session(db_engine)


@pytest.fixture
def blob_service(db_engine: Engine, tmp_path: Path) -> BlobServiceImpl:
    return BlobServiceImpl(db_engine, tmp_path)


@pytest.fixture
def compose_context(db_engine: Engine, session_id: UUID) -> SessionOperationContext:
    return seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.COMPOSE)


async def _committed_atomic_deletion(blob_service: BlobServiceImpl, db_engine: Engine, context: SessionOperationContext) -> BlobRecord:
    record = await blob_service.create_blob(
        UUID(context.fence.session_id), "delete.csv", b"original bytes\n", "text/csv", session_operation_context=context
    )
    # The current fork cleanup producer registers this unqualified evidence in
    # the same transaction that deletes the metadata. Leave its purge pending.
    with db_engine.begin() as connection:
        row = connection.execute(select(blobs_table).where(blobs_table.c.id == str(record.id))).one()
        blob_service._delete_fork_blob_row_locked(connection, row=row, blob_id_str=str(record.id))
    return record


def _obligation() -> BlobAtomicDeletionObligation:
    blob_id = uuid4()
    session_id = uuid4()
    now = datetime.now(UTC)
    return BlobAtomicDeletionObligation(
        blob_id=blob_id,
        session_id=session_id,
        storage_path=f"/data/blobs/{session_id}/{blob_id}_input.csv",
        tombstone_path=f"/data/blobs/{session_id}/.{blob_id}.delete-{uuid4().hex}",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"blob_id": "not-a-uuid"},
        {"session_id": "not-a-uuid"},
        {"storage_path": ""},
        {"tombstone_path": ""},
        {"created_at": datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)},
        {"updated_at": datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)},
        {"updated_at": datetime(2000, 1, 1, tzinfo=UTC)},
    ],
)
def test_atomic_obligation_rejects_malformed_owned_evidence(changes) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_obligation(), **changes)


def test_atomic_obligation_allows_absent_tombstone_without_invented_file_proof() -> None:
    obligation = replace(_obligation(), tombstone_path=None)
    assert obligation.tombstone_path is None


def test_atomic_obligation_rejects_identical_paths() -> None:
    obligation = _obligation()
    with pytest.raises(ValueError):
        replace(obligation, tombstone_path=obligation.storage_path)


@pytest.mark.asyncio
async def test_atomic_reader_rejects_mixed_operation_evidence(blob_service, db_engine, compose_context) -> None:
    record = await _committed_atomic_deletion(blob_service, db_engine, compose_context)
    with db_engine.begin() as connection:
        connection.execute(
            update(blob_deletion_cleanups_table)
            .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            .values(operation_id=compose_context.fence.operation_id)
        )
    with pytest.raises(AuditIntegrityError):
        blob_service._session_operation_authority.mutate(
            compose_context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=record.id)
        )


@pytest.mark.asyncio
async def test_atomic_reader_rejects_a_live_blob_row(blob_service, db_engine, compose_context, session_id) -> None:
    record = await _committed_atomic_deletion(blob_service, db_engine, compose_context)
    live = await blob_service.create_blob(session_id, "live.csv", b"still live\n", "text/csv", session_operation_context=compose_context)
    with db_engine.begin() as connection:
        connection.execute(
            update(blob_deletion_cleanups_table)
            .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            .values(blob_id=str(live.id), storage_path=live.storage_path)
        )
    with pytest.raises(AuditIntegrityError):
        blob_service._session_operation_authority.mutate(
            compose_context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=live.id)
        )
    assert Path(live.storage_path).read_bytes() == b"still live\n"


@pytest.mark.asyncio
async def test_atomic_retire_refuses_changed_timestamp(blob_service, db_engine, compose_context) -> None:
    record = await _committed_atomic_deletion(blob_service, db_engine, compose_context)
    authority = blob_service._session_operation_authority
    obligation = authority.mutate(compose_context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=record.id))
    assert obligation is not None
    with db_engine.begin() as connection:
        connection.execute(
            update(blob_deletion_cleanups_table)
            .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            .values(updated_at=obligation.updated_at + timedelta(seconds=1))
        )
    with pytest.raises(AuditIntegrityError):
        authority.mutate(compose_context, lambda transaction: transaction.blobs.retire_atomic_blob_deletion(obligation=obligation))
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).scalar_one() == str(record.id)


@pytest.mark.asyncio
async def test_atomic_retire_refuses_read_authority_and_keeps_bytes(blob_service, db_engine, compose_context, session_id) -> None:
    record = await _committed_atomic_deletion(blob_service, db_engine, compose_context)
    authority = blob_service._session_operation_authority
    obligation = authority.mutate(compose_context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=record.id))
    assert obligation is not None
    assert obligation.tombstone_path is not None
    read_context = authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="atomic-cleanup-reader",
        lease_seconds=60,
    )
    with pytest.raises(AuditIntegrityError):
        authority.mutate(read_context, lambda transaction: transaction.blobs.retire_atomic_blob_deletion(obligation=obligation))
    assert Path(obligation.tombstone_path).read_bytes() == b"original bytes\n"
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).scalar_one() == str(record.id)


@pytest.mark.asyncio
async def test_atomic_obligation_cannot_be_retired_from_a_foreign_session(blob_service, db_engine, compose_context) -> None:
    record = await _committed_atomic_deletion(blob_service, db_engine, compose_context)
    authority = blob_service._session_operation_authority
    obligation = authority.mutate(compose_context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=record.id))
    assert obligation is not None
    foreign_session_id = _insert_session(db_engine)
    foreign_context = seed_live_operation_context(db_engine, foreign_session_id, operation_kind=SessionOperationKind.COMPOSE)
    assert authority.mutate(foreign_context, lambda transaction: transaction.blobs.read_atomic_blob_deletion(blob_id=record.id)) is None
    with pytest.raises(AuditIntegrityError):
        authority.mutate(foreign_context, lambda transaction: transaction.blobs.retire_atomic_blob_deletion(obligation=obligation))
    with db_engine.connect() as connection:
        assert connection.execute(select(blob_deletion_cleanups_table.c.blob_id)).scalar_one() == str(record.id)
