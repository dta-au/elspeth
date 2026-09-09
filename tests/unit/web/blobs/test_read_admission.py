"""A shareable BLOB_READ admission binds to the blob it asked for (P4-A-3, elspeth-bf52d495a2).

Readers no longer take the exclusive session fence, so a read can overlap a
writer's COMPOSE. What a reader may see is therefore pinned here: exactly the
blob id it named, as it is, or a missing-content failure — never "the
session's current blob" and never a replacement written under the writer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.protocol import BlobContentMissingError, BlobNotFoundError
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture()
def db_engine():
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    return engine


@pytest.fixture()
def authority(db_engine) -> SQLiteLocalSessionOperationAuthority:
    return SQLiteLocalSessionOperationAuthority(db_engine)


@pytest.fixture()
def session_id(authority) -> UUID:
    created = authority.create_session_with_initial_fence(
        user_id="alice",
        title="read admission",
        auth_provider_type="local",
        owner_instance_id="test-instance",
        lease_seconds=30,
    )
    return created.id


@pytest.fixture()
def blob_service(db_engine, tmp_path) -> BlobServiceImpl:
    return BlobServiceImpl(db_engine, tmp_path)


def _acquire(authority, session_id: UUID, kind: SessionOperationKind, owner: str):
    return authority.acquire(session_id=session_id, operation_kind=kind, owner_instance_id=owner, lease_seconds=30)


class TestReadBindsToItsBlobId:
    @pytest.mark.asyncio
    async def test_reader_sees_the_blob_it_named_while_a_writer_adds_another(self, authority, blob_service, session_id) -> None:
        compose = _acquire(authority, session_id, SessionOperationKind.COMPOSE, "writer")
        original = await blob_service.create_blob(session_id, "data.csv", b"a,b\n1,2\n", "text/csv", session_operation_context=compose)
        authority.release(compose)

        reader = _acquire(authority, session_id, SessionOperationKind.BLOB_READ, "reader")
        before = await blob_service.get_blob(original.id, session_operation_context=reader)

        # A writer works underneath the open reader and adds a newer blob.
        compose_again = _acquire(authority, session_id, SessionOperationKind.COMPOSE, "writer")
        newer = await blob_service.create_blob(session_id, "data.csv", b"a,b\n9,9\n", "text/csv", session_operation_context=compose_again)
        assert newer.id != original.id

        after = await blob_service.get_blob(original.id, session_operation_context=reader)
        assert after == before
        assert await blob_service.read_blob_content(original.id, session_operation_context=reader) == b"a,b\n1,2\n"
        authority.release(compose_again)
        authority.release(reader)

    @pytest.mark.asyncio
    async def test_reader_never_receives_a_blob_it_did_not_name(self, authority, blob_service, session_id) -> None:
        reader = _acquire(authority, session_id, SessionOperationKind.BLOB_READ, "reader")
        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(uuid4(), session_operation_context=reader)

    @pytest.mark.asyncio
    async def test_reader_gets_missing_content_when_the_file_is_gone_underneath_it(
        self, authority, blob_service, session_id, tmp_path
    ) -> None:
        compose = _acquire(authority, session_id, SessionOperationKind.COMPOSE, "writer")
        record = await blob_service.create_blob(session_id, "data.csv", b"a,b\n1,2\n", "text/csv", session_operation_context=compose)
        authority.release(compose)
        reader = _acquire(authority, session_id, SessionOperationKind.BLOB_READ, "reader")
        Path(record.storage_path).unlink()
        with pytest.raises(BlobContentMissingError):
            await blob_service.read_blob_content(record.id, session_operation_context=reader)

    @pytest.mark.asyncio
    async def test_reader_loses_custody_when_the_session_archives(self, authority, blob_service, session_id, db_engine) -> None:
        compose = _acquire(authority, session_id, SessionOperationKind.COMPOSE, "writer")
        record = await blob_service.create_blob(session_id, "data.csv", b"a,b\n1,2\n", "text/csv", session_operation_context=compose)
        authority.release(compose)
        reader = _acquire(authority, session_id, SessionOperationKind.BLOB_READ, "reader")
        with db_engine.begin() as conn:
            conn.execute(update(sessions_table).where(sessions_table.c.id == str(session_id)).values(archived_at=datetime.now(UTC)))
        with pytest.raises(SessionOperationFenceLost) as lost:
            await blob_service.get_blob(record.id, session_operation_context=reader)
        assert lost.value.reason is FenceLossReason.OWNER_INACTIVE
