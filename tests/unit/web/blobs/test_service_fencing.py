"""Session-operation fencing of ``BlobServiceImpl`` (P4-C1) and the raced-deletion read seam (P4-C9).

Every blob effect takes an exact ``session_operation_context``; the service
refuses a context of the wrong operation kind before any database or
filesystem access, refuses a create whose context does not own the session,
reads as the fence's session so a foreign blob is indistinguishable from a
missing one, and compare-and-swaps the fence through the real
``SQLiteLocalSessionOperationAuthority`` on every effect. The content reads
type a raced deletion (``ENOENT`` and, on NFS, ``ESTALE``) as
``BlobContentMissingError`` and let every other ``OSError`` propagate.
"""

from __future__ import annotations

import errno
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.pool import StaticPool

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.protocol import BlobContentMissingError, BlobNotFoundError
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.helpers.session_fences import seed_live_compose_context, seed_live_operation_context


@pytest.fixture()
def db_engine():
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    return engine


def _insert_session(db_engine) -> UUID:
    sid = str(uuid4())
    now = datetime.now(UTC)
    with db_engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=sid,
                user_id="test-user",
                auth_provider_type="local",
                title="Fenced blobs",
                created_at=now,
                updated_at=now,
            )
        )
    return UUID(sid)


@pytest.fixture()
def session_id(db_engine) -> UUID:
    return _insert_session(db_engine)


@pytest.fixture()
def blob_service(db_engine, tmp_path) -> BlobServiceImpl:
    return BlobServiceImpl(db_engine, tmp_path)


@pytest.fixture()
def compose_context(db_engine, session_id) -> SessionOperationContext:
    return seed_live_compose_context(db_engine, session_id)


async def _ready_blob(blob_service: BlobServiceImpl, session_id: UUID, context: SessionOperationContext, content: bytes = b"a,b\n1,2\n"):
    return await blob_service.create_blob(session_id, "data.csv", content, "text/csv", session_operation_context=context)


class TestOperationKindRefusal:
    """A context of the wrong kind is refused before any effect (kind sets from 4c59c9d02)."""

    @pytest.mark.asyncio
    async def test_create_refuses_a_read_context(self, blob_service, db_engine, session_id) -> None:
        read_context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.BLOB_READ)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.create_blob(session_id, "data.csv", b"x", "text/csv", session_operation_context=read_context)
        assert await blob_service.list_blobs(session_id) == []

    @pytest.mark.asyncio
    async def test_reads_refuse_a_create_context(self, blob_service, db_engine, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        create_context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.CREATE)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.get_blob(record.id, session_operation_context=create_context)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.read_blob_content(record.id, session_operation_context=create_context)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.read_blob_preview(record.id, limit_bytes=4, session_operation_context=create_context)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.read_blob_content_prefix_verified(record.id, prefix_bytes=4, session_operation_context=create_context)

    @pytest.mark.asyncio
    async def test_delete_refuses_a_read_context(self, blob_service, db_engine, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        read_context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.BLOB_READ)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.delete_blob(record.id, session_operation_context=read_context)
        assert Path(record.storage_path).exists()

    @pytest.mark.asyncio
    async def test_run_effects_refuse_a_compose_context(self, blob_service, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.link_blob_to_run(record.id, uuid4(), "input", session_operation_context=compose_context)
        with pytest.raises(ValueError, match="invalid operation kind"):
            await blob_service.finalize_run_output_blobs(uuid4(), success=True, session_operation_context=compose_context)

    @pytest.mark.asyncio
    async def test_inexact_context_type_is_refused(self, blob_service, session_id) -> None:
        with pytest.raises(TypeError, match="exact SessionOperationContext"):
            await blob_service.get_blob(uuid4(), session_operation_context=object())  # type: ignore[arg-type]


class TestSessionCustody:
    @pytest.mark.asyncio
    async def test_create_refuses_a_context_for_another_session(self, blob_service, db_engine, session_id) -> None:
        other = _insert_session(db_engine)
        other_context = seed_live_compose_context(db_engine, other)
        with pytest.raises(ValueError, match="does not own the blob session"):
            await blob_service.create_blob(session_id, "data.csv", b"x", "text/csv", session_operation_context=other_context)
        assert await blob_service.list_blobs(session_id) == []

    @pytest.mark.asyncio
    async def test_foreign_blob_reads_as_missing(self, blob_service, db_engine, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        other = _insert_session(db_engine)
        other_context = seed_live_compose_context(db_engine, other)
        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=other_context)
        with pytest.raises(BlobNotFoundError):
            await blob_service.read_blob_content(record.id, session_operation_context=other_context)
        with pytest.raises(BlobNotFoundError):
            await blob_service.delete_blob(record.id, session_operation_context=other_context)
        assert Path(record.storage_path).exists()

    @pytest.mark.asyncio
    async def test_record_from_create_equals_record_from_fenced_read(self, blob_service, session_id, compose_context) -> None:
        # ``created_at`` is aware UTC on both sides; the fenced content reads
        # compare a before/after record and depend on that equality.
        record = await _ready_blob(blob_service, session_id, compose_context)
        assert record.created_at.tzinfo is not None
        assert await blob_service.get_blob(record.id, session_operation_context=compose_context) == record

    @pytest.mark.asyncio
    async def test_stale_fence_is_refused_on_every_effect(self, blob_service, db_engine, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        stale = compose_context
        seed_live_compose_context(db_engine, session_id)  # a newer operation replaced the fence
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.create_blob(session_id, "b.csv", b"x", "text/csv", session_operation_context=stale)
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.get_blob(record.id, session_operation_context=stale)
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.read_blob_content(record.id, session_operation_context=stale)
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.delete_blob(record.id, session_operation_context=stale)
        assert Path(record.storage_path).exists()
        assert len(await blob_service.list_blobs(session_id)) == 1


class TestRacedDeletionReadSeam:
    """P4-C9: ESTALE and ENOENT type as missing content; every other errno propagates."""

    @pytest.mark.asyncio
    async def test_estale_on_read_bytes_types_as_missing_content(self, blob_service, session_id, compose_context, monkeypatch) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)

        def _stale(self: Path) -> bytes:
            raise OSError(errno.ESTALE, "Stale file handle")

        monkeypatch.setattr(Path, "read_bytes", _stale)
        with pytest.raises(BlobContentMissingError):
            await blob_service.read_blob_content(record.id, session_operation_context=compose_context)

    @pytest.mark.asyncio
    async def test_eacces_on_read_bytes_propagates(self, blob_service, session_id, compose_context, monkeypatch) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)

        def _denied(self: Path) -> bytes:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(Path, "read_bytes", _denied)
        with pytest.raises(OSError, match="Permission denied") as exc_info:
            await blob_service.read_blob_content(record.id, session_operation_context=compose_context)
        assert exc_info.value.errno == errno.EACCES

    @pytest.mark.asyncio
    async def test_estale_on_open_types_as_missing_for_preview_and_prefix(
        self, blob_service, session_id, compose_context, monkeypatch
    ) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        original_open = Path.open

        def _stale_open(self: Path, *args: object, **kwargs: object):
            if self == Path(record.storage_path):
                raise OSError(errno.ESTALE, "Stale file handle")
            return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", _stale_open)
        with pytest.raises(BlobContentMissingError):
            await blob_service.read_blob_preview(record.id, limit_bytes=4, session_operation_context=compose_context)
        with pytest.raises(BlobContentMissingError):
            await blob_service.read_blob_content_prefix_verified(record.id, prefix_bytes=4, session_operation_context=compose_context)

    @pytest.mark.asyncio
    async def test_eacces_on_open_propagates_for_preview_and_prefix(self, blob_service, session_id, compose_context, monkeypatch) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        original_open = Path.open

        def _denied_open(self: Path, *args: object, **kwargs: object):
            if self == Path(record.storage_path):
                raise OSError(errno.EACCES, "Permission denied")
            return original_open(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", _denied_open)
        with pytest.raises(OSError, match="Permission denied"):
            await blob_service.read_blob_preview(record.id, limit_bytes=4, session_operation_context=compose_context)
        with pytest.raises(OSError, match="Permission denied"):
            await blob_service.read_blob_content_prefix_verified(record.id, prefix_bytes=4, session_operation_context=compose_context)
