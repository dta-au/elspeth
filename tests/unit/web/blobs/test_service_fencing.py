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
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs import service as blob_service_module
from elspeth.web.blobs.protocol import BlobContentMissingError, BlobNotFoundError, BlobQuotaExceededError
from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blob_run_links_table, blobs_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.session_fences import seed_live_compose_context, seed_live_operation_context
from tests.unit.web.blobs.test_service import _seed_active_run, reserve_output_blob


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
        ensure_test_identity(conn, identity_id="test-user")
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


def _reserve_output_blob(
    service: BlobServiceImpl, context: SessionOperationContext, run_id: UUID, *, filename: str = "output.csv"
) -> BlobRecord:
    blob_id = uuid4()
    storage = service._storage_path(context.fence.session_id, str(blob_id), filename)
    storage.parent.mkdir(parents=True, exist_ok=True)
    record = BlobRecord(
        id=blob_id,
        session_id=UUID(context.fence.session_id),
        filename=filename,
        mime_type="text/csv",
        size_bytes=0,
        content_hash=None,
        storage_path=str(storage),
        created_at=datetime.now(UTC),
        created_by="pipeline",
        source_description=None,
        status="pending",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )
    authority = service._session_operation_authority
    authority.mutate(context, lambda transaction: transaction.blobs.reserve_pending_output_blob(record=record))
    authority.mutate(
        context, lambda transaction: transaction.blobs.insert_blob_run_link(blob_id=blob_id, run_id=run_id, direction="output")
    )
    return record


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
    async def test_run_link_refuses_a_blob_outside_the_fence_session(self, blob_service, db_engine, session_id, compose_context) -> None:
        """The fence's session owns the blob it links, or the blob reads as missing.

        ``link_blob_to_run`` checks the blob's custody against the EXECUTE
        fence's session inside the link transaction (verification finding,
        comment 9599 on elspeth-f4a4a3d000). A blob of session A linked under
        session B's run fence is refused as missing — the non-leaking answer —
        before the blob/run same-session guard can even name the mismatch.
        """
        record = await _ready_blob(blob_service, session_id, compose_context)
        other = _insert_session(db_engine)
        other_run_id = await _seed_active_run(
            db_engine,
            other,
            session_operation_context=seed_live_compose_context(db_engine, other),
            source={
                "plugin": "csv",
                "on_success": "output",
                "on_validation_failure": "quarantine",
                "options": {"path": "/data/external/other.csv"},
            },
        )
        other_execute = seed_live_operation_context(db_engine, other, operation_kind=SessionOperationKind.EXECUTE)
        with pytest.raises(BlobNotFoundError):
            await blob_service.link_blob_to_run(record.id, UUID(other_run_id), "input", session_operation_context=other_execute)
        with db_engine.connect() as conn:
            assert conn.execute(select(blob_run_links_table).where(blob_run_links_table.c.blob_id == str(record.id))).all() == []
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


class TestTakeoverDuringBlobMutation:
    """A proof detached from its later effect does not authorize publication."""

    @pytest.mark.asyncio
    async def test_create_refuses_takeover_before_file_publication(
        self,
        blob_service,
        db_engine,
        session_id,
        compose_context,
        monkeypatch,
    ) -> None:
        original_write = blob_service_module._atomic_write_blob
        successor: SessionOperationContext | None = None
        attempted_storage: Path | None = None

        def takeover_then_write(
            storage: Path,
            content: bytes,
            *,
            write_guard: Any = None,
            directory_fd: int | None = None,
        ) -> None:
            nonlocal successor, attempted_storage
            attempted_storage = storage
            successor = seed_live_compose_context(db_engine, session_id, owner_instance_id="successor")
            original_write(storage, content, write_guard=write_guard, directory_fd=directory_fd)

        monkeypatch.setattr(blob_service_module, "_atomic_write_blob", takeover_then_write)

        with pytest.raises(SessionOperationFenceLost):
            await blob_service.create_blob(
                session_id,
                "stale.csv",
                b"x\n1\n",
                "text/csv",
                session_operation_context=compose_context,
            )

        assert successor is not None
        assert attempted_storage is not None
        assert not attempted_storage.exists()
        records = await blob_service.list_blobs(session_id)
        assert all(record.status != "ready" for record in records)

    @pytest.mark.asyncio
    async def test_delete_refuses_takeover_before_file_removal(
        self,
        blob_service,
        db_engine,
        session_id,
        compose_context,
        monkeypatch,
    ) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        authority = blob_service._session_operation_authority
        original_compare_and_swap = authority.compare_and_swap
        successor: SessionOperationContext | None = None

        def takeover_after_proof(context: SessionOperationContext) -> None:
            nonlocal successor
            original_compare_and_swap(context)
            if successor is None:
                successor = seed_live_compose_context(db_engine, session_id, owner_instance_id="successor")

        monkeypatch.setattr(authority, "compare_and_swap", takeover_after_proof)

        with pytest.raises(SessionOperationFenceLost):
            await blob_service.delete_blob(record.id, session_operation_context=compose_context)

        assert successor is not None
        assert Path(record.storage_path).exists()
        with db_engine.connect() as conn:
            assert conn.execute(select(blobs_table.c.status).where(blobs_table.c.id == str(record.id))).scalar_one() == "ready"

    @pytest.mark.asyncio
    async def test_link_refuses_takeover_between_proof_and_insert(
        self,
        blob_service,
        db_engine,
        session_id,
        compose_context,
        monkeypatch,
    ) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        run_id = UUID(
            await _seed_active_run(
                db_engine,
                session_id,
                session_operation_context=compose_context,
                source={"plugin": "csv", "on_success": "out", "options": {"path": "input.csv"}},
            )
        )
        execute_context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        authority = blob_service._session_operation_authority
        original_compare_and_swap = authority.compare_and_swap
        original_mutate = authority.mutate
        injected = False
        proving = False

        def takeover() -> None:
            nonlocal injected
            if not injected:
                seed_live_operation_context(
                    db_engine,
                    session_id,
                    operation_kind=SessionOperationKind.EXECUTE,
                    owner_instance_id="successor",
                )
                injected = True

        def raced_compare_and_swap(context: SessionOperationContext) -> None:
            nonlocal proving
            proving = True
            try:
                original_compare_and_swap(context)
            finally:
                proving = False
            takeover()

        def raced_mutate(context: SessionOperationContext, mutation: Any) -> Any:
            if not proving:
                takeover()
            return original_mutate(context, mutation)

        monkeypatch.setattr(authority, "compare_and_swap", raced_compare_and_swap)
        monkeypatch.setattr(authority, "mutate", raced_mutate)

        with pytest.raises(SessionOperationFenceLost):
            await blob_service.link_blob_to_run(
                record.id,
                run_id,
                "input",
                session_operation_context=execute_context,
            )

        assert injected
        with db_engine.connect() as conn:
            assert conn.execute(select(blob_run_links_table).where(blob_run_links_table.c.blob_id == str(record.id))).all() == []

    @pytest.mark.asyncio
    async def test_finalize_refuses_takeover_during_output_read(
        self,
        blob_service,
        db_engine,
        session_id,
        compose_context,
        monkeypatch,
    ) -> None:
        run_id = UUID(
            await _seed_active_run(
                db_engine,
                session_id,
                session_operation_context=compose_context,
                source={"plugin": "csv", "on_success": "out", "options": {"path": "input.csv"}},
                status="running",
            )
        )
        execute_context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        pending = _reserve_output_blob(blob_service, execute_context, run_id)
        storage = Path(pending.storage_path)
        storage.write_bytes(b"result\n")
        original_read_bytes = Path.read_bytes
        successor: SessionOperationContext | None = None

        def takeover_then_read(path: Path) -> bytes:
            nonlocal successor
            if successor is None and path == storage:
                successor = seed_live_operation_context(
                    db_engine,
                    session_id,
                    operation_kind=SessionOperationKind.EXECUTE,
                    owner_instance_id="successor",
                )
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", takeover_then_read)

        with pytest.raises(SessionOperationFenceLost):
            await blob_service.finalize_run_output_blobs(
                run_id,
                success=True,
                session_operation_context=execute_context,
            )

        assert successor is not None
        with db_engine.connect() as conn:
            assert conn.execute(select(blobs_table.c.status).where(blobs_table.c.id == str(pending.id))).scalar_one() == "pending"
        assert storage.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unbound", [False, True], ids=["predecessor-owned", "unbound"])
    async def test_rejected_output_custody_never_restores_tombstone(
        self, blob_service, db_engine, session_id, compose_context, unbound
    ) -> None:
        run_id = UUID(
            await _seed_active_run(
                db_engine,
                session_id,
                session_operation_context=compose_context,
                source={"plugin": "csv", "on_success": "out", "options": {"path": "input.csv"}},
                status="running",
            )
        )
        predecessor = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        record = _reserve_output_blob(blob_service, predecessor, run_id)
        if unbound:
            with db_engine.begin() as conn:
                conn.execute(
                    update(blobs_table)
                    .where(blobs_table.c.id == str(record.id))
                    .values(
                        custody_operation_id=None,
                        custody_operation_epoch=None,
                        custody_operation_kind=None,
                    )
                )
        token = blob_service_module._blob_operation_path_token(
            operation_id=predecessor.fence.operation_id,
            operation_epoch=predecessor.fence.operation_epoch,
            operation_kind=predecessor.operation_kind,
        )
        storage = Path(record.storage_path)
        tombstone = storage.with_name(f".{record.id}.output-delete-{token}")
        tombstone.write_bytes(b"predecessor output")
        successor = seed_live_operation_context(
            db_engine,
            session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="successor",
        )

        with pytest.raises(AuditIntegrityError, match="exact EXECUTE"):
            await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=successor)

        assert not storage.exists()
        assert tombstone.read_bytes() == b"predecessor output"
        with db_engine.connect() as conn:
            assert conn.execute(select(blobs_table.c.status).where(blobs_table.c.id == str(record.id))).scalar_one() == "pending"


class TestBoundedBlobMutationArtifacts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("filename", ["x" * 200, "é" * 100], ids=["ascii", "utf8"])
    async def test_delete_accepts_maximum_valid_filename(self, blob_service, session_id, compose_context, filename) -> None:
        record = await blob_service.create_blob(
            session_id,
            filename,
            b"output",
            "text/csv",
            session_operation_context=compose_context,
        )
        await blob_service.delete_blob(record.id, session_operation_context=compose_context)
        assert await blob_service.list_blobs(session_id) == []
        assert list(Path(record.storage_path).parent.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("filename", ["x" * 200, "é" * 100], ids=["ascii", "utf8"])
    async def test_failed_output_cleanup_accepts_maximum_valid_filename(
        self,
        blob_service,
        db_engine,
        session_id,
        compose_context,
        filename,
    ) -> None:
        run_id = UUID(
            await _seed_active_run(
                db_engine,
                session_id,
                session_operation_context=compose_context,
                source={"plugin": "csv", "on_success": "out", "options": {"path": "input.csv"}},
                status="running",
            )
        )
        context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        record = _reserve_output_blob(blob_service, context, run_id, filename=filename)
        storage = Path(record.storage_path)
        storage.write_bytes(b"partial output")
        result = await blob_service.finalize_run_output_blobs(run_id, success=False, session_operation_context=context)
        assert result.errors == ()
        assert len(result.finalized) == 1
        assert result.finalized[0].status == "error"
        assert list(storage.parent.iterdir()) == []


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


class TestStorageMimeGuard:
    """The fenced read's Tier-1 row guard admits the STORAGE union and refuses anything outside it.

    Every fenced read goes through ``_RepositoryBlobMutations.read_blob`` →
    ``_blob_record``; until e1d6d9ce4 that guard named the text/data set
    only, so a stored binary document (elspeth-0c6a343921) could be created
    but never read back through the fence.
    """

    @pytest.mark.asyncio
    async def test_binary_document_reads_back_through_the_fence(self, blob_service, session_id, compose_context) -> None:
        pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
        record = await blob_service.create_blob(session_id, "doc.pdf", pdf, "application/pdf", session_operation_context=compose_context)
        assert record.mime_type == "application/pdf"
        assert await blob_service.get_blob(record.id, session_operation_context=compose_context) == record
        assert await blob_service.read_blob_content(record.id, session_operation_context=compose_context) == pdf

    @pytest.mark.asyncio
    async def test_row_outside_the_storage_union_is_refused_on_read(self, blob_service, db_engine, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        with db_engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(mime_type="application/x-msdownload"))
        with pytest.raises(AuditIntegrityError, match="not in the storage MIME set"):
            await blob_service.get_blob(record.id, session_operation_context=compose_context)
        with pytest.raises(AuditIntegrityError, match="not in the storage MIME set"):
            await blob_service.read_blob_content(record.id, session_operation_context=compose_context)


# ---------------------------------------------------------------------------
# Run-scoped writes go through the authority facet (D6 family B, elspeth-af0fdc3cc6)
# ---------------------------------------------------------------------------


class _RecordingAuthority:
    """The real authority, with every entry the blob service takes written down.

    Explicit forwarding only: the blob service reaches the authority through
    ``mutate`` and ``compare_and_swap``, so those are the two seams this
    recorder exposes. A third entry would be a new production dependency and
    fails loudly here instead of being forwarded unseen.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.mutations: list[SessionOperationContext] = []
        self.standalone_cas: list[SessionOperationContext] = []

    def mutate(self, context, mutation):
        self.mutations.append(context)
        return self._inner.mutate(context, mutation)

    def compare_and_swap(self, context) -> None:
        self.standalone_cas.append(context)
        self._inner.compare_and_swap(context)


async def _seed_run(db_engine, session_id: UUID, compose_context: SessionOperationContext) -> UUID:
    run_id = await _seed_active_run(
        db_engine,
        session_id,
        session_operation_context=compose_context,
        source={
            "plugin": "csv",
            "on_success": "output",
            "on_validation_failure": "quarantine",
            "options": {"path": "/data/external/input.csv"},
        },
        status="running",
    )
    return UUID(run_id)


class TestRunWritesGoThroughTheAuthorityFacet:
    """``link_blob_to_run`` and run-output finalization are authority mutations, not raw session writes.

    Until D6 family B both verbs compare-and-swapped the fence and then opened
    their own ``engine.begin()`` to write ``blob_run_links`` / ``blobs``
    directly. The facet (``insert_blob_run_link``,
    ``mark_run_output_blob_ready`` / ``_error``) existed with no production
    caller. Now every such write is one ``mutate`` under the EXECUTE context,
    so the facet's custody checks are the ones that decide.
    """

    @pytest.mark.asyncio
    async def test_run_link_is_one_authority_mutation(self, blob_service, db_engine, session_id, compose_context) -> None:
        record = await _ready_blob(blob_service, session_id, compose_context)
        run_id = await _seed_run(db_engine, session_id, compose_context)
        execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        recorder = _RecordingAuthority(blob_service._session_operation_authority)
        blob_service._session_operation_authority = recorder

        await blob_service.link_blob_to_run(record.id, run_id, "input", session_operation_context=execute)

        assert recorder.mutations == [execute]
        assert recorder.standalone_cas == []
        with db_engine.connect() as conn:
            links = conn.execute(
                select(blob_run_links_table.c.run_id, blob_run_links_table.c.direction).where(
                    blob_run_links_table.c.blob_id == str(record.id)
                )
            ).all()
        assert [tuple(link) for link in links] == [(str(run_id), "input")]

    @pytest.mark.asyncio
    async def test_run_link_still_names_a_cross_session_run_as_a_contract_violation(
        self, blob_service, db_engine, session_id, compose_context
    ) -> None:
        """A blob in custody but a run outside it is a caller bug, not a missing blob."""
        record = await _ready_blob(blob_service, session_id, compose_context)
        other = _insert_session(db_engine)
        foreign_run_id = await _seed_run(db_engine, other, seed_live_compose_context(db_engine, other))
        execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)

        with pytest.raises(RuntimeError, match="cross-session reference"):
            await blob_service.link_blob_to_run(record.id, foreign_run_id, "input", session_operation_context=execute)
        with db_engine.connect() as conn:
            assert conn.execute(select(blob_run_links_table).where(blob_run_links_table.c.blob_id == str(record.id))).all() == []

    @pytest.mark.asyncio
    async def test_output_finalization_is_one_authority_mutation_per_blob(
        self, blob_service, db_engine, session_id, compose_context
    ) -> None:
        run_id = await _seed_run(db_engine, session_id, compose_context)
        execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        written = reserve_output_blob(blob_service, session_id, run_id, execute, filename="written.csv")
        unwritten = reserve_output_blob(blob_service, session_id, run_id, execute, filename="unwritten.csv")
        content = b"col\n1\n2\n"
        Path(written.storage_path).write_bytes(content)
        recorder = _RecordingAuthority(blob_service._session_operation_authority)
        blob_service._session_operation_authority = recorder

        blob_updates: list[str] = []

        def record_blob_update(conn, cursor, statement, parameters, context, executemany) -> None:
            if statement.lstrip().upper().startswith("UPDATE BLOBS SET"):
                blob_updates.append(statement)

        event.listen(db_engine, "after_cursor_execute", record_blob_update)
        try:
            result = await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)
        finally:
            event.remove(db_engine, "after_cursor_execute", record_blob_update)

        assert list(result.errors) == []
        by_id = {record.id: record for record in result.finalized}
        assert by_id[written.id].status == "ready"
        assert by_id[written.id].size_bytes == len(content)
        assert by_id[written.id].content_hash == content_hash(content)
        assert by_id[unwritten.id].status == "error"
        # Two fenced output-set reads precede one metadata write per blob.
        # File guards also revalidate the same authority between byte effects.
        assert len(blob_updates) == 2
        assert recorder.mutations == [execute, execute, execute, execute]
        assert recorder.standalone_cas
        assert all(context == execute for context in recorder.standalone_cas)
        with db_engine.connect() as conn:
            custody = (
                conn.execute(select(blobs_table.c.custody_operation_id).where(blobs_table.c.id.in_([str(written.id), str(unwritten.id)])))
                .scalars()
                .all()
            )
        assert custody == [None, None]

    @pytest.mark.asyncio
    async def test_output_reserved_under_another_execute_operation_is_an_integrity_anomaly(
        self, blob_service, db_engine, session_id, compose_context
    ) -> None:
        """The facet binds a reservation to the EXECUTE operation that made it.

        A later operation finding that row among its run's pending outputs is
        a Tier-1 anomaly: the facet raises ``AuditIntegrityError``, which is
        not in the per-blob suppressed set, so the batch aborts and the row
        keeps its original custody instead of being finalized out from under
        it. Until family B the service updated the row unconditionally.
        """
        run_id = await _seed_run(db_engine, session_id, compose_context)
        first_execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        reserved = reserve_output_blob(blob_service, session_id, run_id, first_execute)
        Path(reserved.storage_path).write_bytes(b"late\n")
        second_execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)

        with pytest.raises(AuditIntegrityError, match="exact EXECUTE operation"):
            await blob_service.finalize_run_output_blobs(run_id, success=True, session_operation_context=second_execute)

        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(reserved.id))).one()
        assert row.status == "pending"
        assert row.custody_operation_id == first_execute.fence.operation_id
        assert Path(reserved.storage_path).read_bytes() == b"late\n"

    @pytest.mark.asyncio
    async def test_output_over_quota_is_marked_error_and_its_file_removed(self, db_engine, session_id, tmp_path, compose_context) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=8)
        run_id = await _seed_run(db_engine, session_id, compose_context)
        execute = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        reserved = reserve_output_blob(service, session_id, run_id, execute)
        storage = Path(reserved.storage_path)
        storage.write_bytes(b"x" * 64)
        recorder = _RecordingAuthority(service._session_operation_authority)
        service._session_operation_authority = recorder

        result = await service.finalize_run_output_blobs(run_id, success=True, session_operation_context=execute)

        assert list(result.finalized) == []
        assert len(result.errors) == 1
        failure = result.errors[0]
        assert failure.blob_id == reserved.id
        assert failure.exc_type == "BlobQuotaExceededError"
        assert failure.detail == str(BlobQuotaExceededError(str(session_id), current_bytes=0, limit_bytes=8))
        # Both output listings, the quota-refused ready mutation and the error
        # mutation use the reserving EXECUTE authority. Every filesystem guard
        # also revalidates that exact context.
        assert recorder.mutations == [execute, execute, execute, execute]
        assert recorder.standalone_cas
        assert all(context == execute for context in recorder.standalone_cas)
        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(reserved.id))).one()
        assert (row.status, row.size_bytes, row.content_hash, row.custody_operation_id) == ("error", 0, None, None)
        assert not storage.exists()
