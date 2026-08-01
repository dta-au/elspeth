"""Tests for BlobServiceImpl — audit-critical blob persistence and lifecycle.

Security boundaries tested:
- Content hash integrity (AD-5/AD-7: hash must match for lineage verification)
- Session-scoped isolation (blobs cannot leak across sessions)
- Active-run deletion guard (cannot destroy evidence during a live run)
- Filename sanitization (path traversal defense at the storage layer)
- Status lifecycle (pending -> ready/error only; no backwards transitions)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import multiprocessing
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import delete, event, func, insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.blobs import service as blob_service_module
from elspeth.web.blobs.protocol import (
    BlobActiveRunError,
    BlobForkCleanupError,
    BlobForkCleanupResult,
    BlobForkFenceLostError,
    BlobForkPlanEntry,
    BlobGuidedOperationFenceLostError,
    BlobGuidedOperationWriteFence,
    BlobInProgressForkError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobQuotaExceededError,
    BlobStateError,
    fork_blob_id,
)
from elspeth.web.blobs.service import (
    BlobServiceImpl,
    content_hash,
    sanitize_filename,
)
from elspeth.web.coordination import repository as coordination_repository_module
from elspeth.web.coordination.contracts import (
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blob_deletion_cleanups_table,
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    guided_operations_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import (
    GuidedOperationFence,
    SessionForkAuthority,
    SessionForkParentAuthority,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import _FakeCounter, build_sessions_telemetry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_operation_context_factory: Callable[[UUID, SessionOperationKind], SessionOperationContext] | None = None


def _operation_context(
    session_id: UUID,
    operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE,
) -> SessionOperationContext:
    """Return this test's real authority context for one sequential operation."""
    if _operation_context_factory is None:
        raise RuntimeError("session operation context fixture is not active")
    return _operation_context_factory(session_id, operation_kind)


@pytest.fixture()
def db_engine():
    """In-memory SQLite engine with all session tables created."""
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    return engine


@pytest.fixture()
def session_operation_authority(db_engine) -> SQLiteLocalSessionOperationAuthority:
    return SQLiteLocalSessionOperationAuthority(db_engine)


@pytest.fixture(autouse=True)
def _operation_context_lifecycle(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Provide one live, real authority context per touched session and test."""
    global _operation_context_factory
    authority: SQLiteLocalSessionOperationAuthority | None = None
    contexts: dict[UUID, SessionOperationContext] = {}

    def acquire_context(
        session_id: UUID,
        operation_kind: SessionOperationKind,
    ) -> SessionOperationContext:
        nonlocal authority
        if authority is None:
            resolved_authority = request.getfixturevalue("session_operation_authority")
            if type(resolved_authority) is not SQLiteLocalSessionOperationAuthority:
                raise TypeError("session operation authority fixture returned the wrong type")
            authority = resolved_authority
        context = contexts.get(session_id)
        if context is not None and context.operation_kind is not operation_kind:
            with contextlib.suppress(SessionOperationFenceLost):
                authority.release(context)
            context = None
        if context is None:
            context = authority.acquire(
                session_id=session_id,
                operation_kind=operation_kind,
                owner_instance_id=f"blob-test-{uuid4().hex}",
                lease_seconds=30,
            )
            contexts[session_id] = context
        else:
            authority.compare_and_swap(context)
        return context

    if _operation_context_factory is not None:
        raise RuntimeError("session operation context fixture leaked between tests")
    _operation_context_factory = acquire_context
    try:
        yield
    finally:
        _operation_context_factory = None
        if authority is not None:
            for context in contexts.values():
                with contextlib.suppress(SessionOperationFenceLost):
                    authority.release(context)


@pytest.fixture()
def session_id(session_operation_authority: SQLiteLocalSessionOperationAuthority) -> UUID:
    """Create a real fenced session and return its public identity."""
    created_id = session_operation_authority.create_session_with_initial_fence(
        user_id="test-user",
        title="Test Session",
        auth_provider_type="local",
        owner_instance_id="blob-test-owner",
        lease_seconds=30,
    ).id
    if type(created_id) is not UUID:
        raise TypeError("session authority returned a non-UUID identity")
    return created_id


@pytest.fixture()
def compose_context(
    session_id: UUID,
) -> SessionOperationContext:
    return _operation_context(session_id)


@pytest.fixture()
def blob_service(db_engine, tmp_path, session_operation_authority) -> BlobServiceImpl:
    """BlobServiceImpl backed by the shared engine and a temp directory."""
    return BlobServiceImpl(
        db_engine,
        tmp_path,
        session_operation_authority=session_operation_authority,
    )


# ---------------------------------------------------------------------------
# sanitize_filename — path traversal defense
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    """B5: filename sanitization prevents path traversal at the storage layer."""

    def test_path_traversal_strips_directory_components(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"

    def test_absolute_path_strips_to_basename(self) -> None:
        assert sanitize_filename("/absolute/path/file.csv") == "file.csv"

    def test_normal_filename_passes_through(self) -> None:
        assert sanitize_filename("normal.csv") == "normal.csv"

    def test_dot_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename(".")

    def test_dotdot_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename("..")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid filename"):
            sanitize_filename("")

    def test_long_filename_truncated(self) -> None:
        long_name = "a" * 300 + ".csv"
        result = sanitize_filename(long_name)
        assert len(result.encode("utf-8")) <= 200


# ---------------------------------------------------------------------------
# content_hash — audit integrity
# ---------------------------------------------------------------------------


class TestContentHash:
    """AD-5/AD-7: content hash must be SHA-256 for lineage verification."""

    def test_known_input(self) -> None:
        expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        assert content_hash(b"hello") == expected

    def test_stability(self) -> None:
        data = b"audit-critical-content"
        assert content_hash(data) == content_hash(data)

    def test_empty_bytes(self) -> None:
        expected = hashlib.sha256(b"").hexdigest()
        assert content_hash(b"") == expected


# ---------------------------------------------------------------------------
# create_blob + read_blob_content — round-trip integrity
# ---------------------------------------------------------------------------


class TestCreateAndRead:
    """Blob creation writes to filesystem and DB; read returns identical bytes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method_name", "operation_kind", "arguments"),
        (
            (
                "create_blob",
                SessionOperationKind.BLOB_READ,
                {
                    "filename": "wrong-kind.txt",
                    "content": b"must not persist",
                    "mime_type": "text/plain",
                },
            ),
            (
                "create_blob",
                SessionOperationKind.PROPOSAL,
                {
                    "filename": "proposal-forbidden.txt",
                    "content": b"must not persist",
                    "mime_type": "text/plain",
                },
            ),
            ("get_blob", SessionOperationKind.CREATE, {}),
            ("get_blob", SessionOperationKind.PROPOSAL, {}),
            ("read_blob_content", SessionOperationKind.CREATE, {}),
            ("read_blob_content", SessionOperationKind.PROPOSAL, {}),
            ("read_blob_preview", SessionOperationKind.CREATE, {"limit_bytes": 8}),
            ("read_blob_preview", SessionOperationKind.PROPOSAL, {"limit_bytes": 8}),
            ("delete_blob", SessionOperationKind.BLOB_READ, {}),
            ("delete_blob", SessionOperationKind.PROPOSAL, {}),
        ),
    )
    async def test_standalone_blob_methods_reject_wrong_live_operation_kind_before_io(
        self,
        db_engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        method_name: str,
        operation_kind: SessionOperationKind,
        arguments: dict[str, object],
    ) -> None:
        authority = SQLiteLocalSessionOperationAuthority(db_engine)
        session = authority.create_session_with_initial_fence(
            user_id="test-user",
            title="Wrong kind",
            auth_provider_type="local",
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        )
        context = authority.acquire(
            session_id=session.id,
            operation_kind=operation_kind,
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(db_engine, tmp_path, session_operation_authority=authority)
        io_attempted = False

        async def fail_on_io(_func) -> None:
            nonlocal io_attempted
            io_attempted = True
            raise AssertionError("wrong operation kind reached database or filesystem I/O")

        monkeypatch.setattr(service, "_run_sync", fail_on_io)
        blob_id = uuid4()
        call_arguments: dict[str, object]
        if method_name == "create_blob":
            call_arguments = {"session_id": session.id, **arguments}
        else:
            call_arguments = {"blob_id": blob_id, **arguments}

        with pytest.raises(ValueError, match="operation kind"):
            await getattr(service, method_name)(
                **call_arguments,
                session_operation_context=context,
            )

        assert io_attempted is False
        assert not (tmp_path / "blobs").exists()

    @pytest.mark.asyncio
    async def test_create_loss_after_publish_is_retired_only_by_current_winner(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stale = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )
        original_compare_and_swap = session_operation_authority.compare_and_swap
        guard_calls = 0
        winner_context: SessionOperationContext | None = None

        def lose_after_publish(context: SessionOperationContext) -> None:
            nonlocal guard_calls, winner_context
            guard_calls += 1
            if guard_calls == 6:
                session_operation_authority.release(stale)
                winner_context = session_operation_authority.acquire(
                    session_id=session_id,
                    operation_kind=SessionOperationKind.CREATE,
                    owner_instance_id="winner-owner",
                    lease_seconds=30,
                )
            original_compare_and_swap(context)

        monkeypatch.setattr(session_operation_authority, "compare_and_swap", lose_after_publish)

        with pytest.raises(SessionOperationFenceLost):
            await service.create_blob(
                session_id=session_id,
                filename="lost.txt",
                content=b"published then fenced",
                mime_type="text/plain",
                session_operation_context=stale,
            )

        with db_engine.connect() as conn:
            stale_row = conn.execute(select(blobs_table).where(blobs_table.c.session_id == str(session_id))).one()
        assert stale_row.status == "pending"
        assert stale_row.custody_operation_id == stale.fence.operation_id
        blob_dir = tmp_path / "blobs" / str(session_id)
        assert len(list(blob_dir.iterdir())) == 1
        stale_storage = Path(stale_row.storage_path)
        legacy_custody_temp = stale_storage.with_name(f".{stale_storage.name}.custody.tmp")
        legacy_orphan_temp = stale_storage.with_name(f".{stale_storage.name}.orphan.tmp")
        legacy_custody_temp.write_bytes(b"legacy custody bytes")
        legacy_orphan_temp.write_bytes(b"legacy orphan bytes")

        assert winner_context is not None
        winner = await service.create_blob(
            session_id=session_id,
            filename="winner.txt",
            content=b"winner bytes",
            mime_type="text/plain",
            session_operation_context=winner_context,
        )
        assert Path(winner.storage_path).read_bytes() == b"winner bytes"
        with db_engine.connect() as conn:
            rows = conn.execute(select(blobs_table).where(blobs_table.c.session_id == str(session_id))).all()
        assert [row.id for row in rows] == [str(winner.id)]
        assert not stale_storage.exists()
        assert not legacy_custody_temp.exists()
        assert not legacy_orphan_temp.exists()

        with pytest.raises(SessionOperationFenceLost):
            await service.create_blob(
                session_id=session_id,
                filename="late.txt",
                content=b"late stale bytes",
                mime_type="text/plain",
                session_operation_context=stale,
            )
        assert Path(winner.storage_path).read_bytes() == b"winner bytes"

    @pytest.mark.asyncio
    async def test_successor_reconciliation_waits_for_paused_stale_writer_and_removes_its_temp(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """File exclusion closes the stale-writer/reconciliation orphan window."""
        stale = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id="stale-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )

        @contextlib.contextmanager
        def unlocked_transaction(_self, _session_id: str):
            with db_engine.begin() as conn:
                yield conn

        @contextlib.contextmanager
        def no_process_lock(_engine, _session_id: str):
            yield

        monkeypatch.setattr(
            SQLiteLocalSessionOperationAuthority,
            "_locked_transaction",
            unlocked_transaction,
        )
        monkeypatch.setattr(blob_service_module, "process_session_lock", no_process_lock)

        original_compare_and_swap = session_operation_authority.compare_and_swap
        stale_paused = threading.Event()
        release_stale = threading.Event()
        paused_once = False

        def pause_after_first_successful_write_guard(context: SessionOperationContext) -> None:
            nonlocal paused_once
            original_compare_and_swap(context)
            if context == stale and not paused_once:
                paused_once = True
                stale_paused.set()
                if not release_stale.wait(timeout=5):
                    raise TimeoutError("test did not release paused stale blob writer")

        monkeypatch.setattr(
            session_operation_authority,
            "compare_and_swap",
            pause_after_first_successful_write_guard,
        )

        stale_task = asyncio.create_task(
            service.create_blob(
                session_id=session_id,
                filename="stale.txt",
                content=b"stale bytes",
                mime_type="text/plain",
                session_operation_context=stale,
            )
        )
        assert await asyncio.to_thread(stale_paused.wait, 5)

        session_operation_authority.release(stale)
        winner = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id="winner-owner",
            lease_seconds=30,
        )
        reconciliation_task = asyncio.create_task(service._run_sync(lambda: service._reconcile_abandoned_creations(winner)))
        await asyncio.sleep(0.05)
        assert not reconciliation_task.done()

        release_stale.set()
        with pytest.raises(SessionOperationFenceLost):
            await stale_task
        await reconciliation_task

        session_operation_authority.compare_and_swap(winner)
        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 0
        blob_dir = tmp_path / "blobs" / str(session_id)
        assert not blob_dir.exists() or tuple(blob_dir.iterdir()) == ()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("raise_after_call", "committed"), ((1, False), (2, True)))
    async def test_create_reconciles_commit_then_raise_at_reserve_and_ready(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
        raise_after_call: int,
        committed: bool,
    ) -> None:
        context = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.CREATE,
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )
        original_mutate = session_operation_authority.mutate
        injected = False

        phase_name = "reserve_standalone_blob" if raise_after_call == 1 else "mark_standalone_blob_ready"

        def commit_then_raise(operation_context, mutation):
            nonlocal injected
            result = original_mutate(operation_context, mutation)
            if not injected and mutation.__name__ == phase_name:
                injected = True
                raise RuntimeError("injected return-path failure after commit")
            return result

        monkeypatch.setattr(session_operation_authority, "mutate", commit_then_raise)
        if committed:
            record = await service.create_blob(
                session_id=session_id,
                filename="unknown.txt",
                content=b"reconciled bytes",
                mime_type="text/plain",
                session_operation_context=context,
            )
            assert record.status == "ready"
            assert Path(record.storage_path).read_bytes() == b"reconciled bytes"
        else:
            with pytest.raises(RuntimeError, match="after commit"):
                await service.create_blob(
                    session_id=session_id,
                    filename="unknown.txt",
                    content=b"must roll back",
                    mime_type="text/plain",
                    session_operation_context=context,
                )
            with db_engine.connect() as conn:
                pending = conn.execute(select(blobs_table).where(blobs_table.c.session_id == str(session_id))).one()
            assert pending.status == "pending"
            assert pending.custody_operation_id == context.fence.operation_id
            assert pending.custody_operation_epoch == context.fence.operation_epoch
            assert pending.custody_operation_kind == context.operation_kind.value
            session_operation_authority.release(context)
            winner_context = session_operation_authority.acquire(
                session_id=session_id,
                operation_kind=SessionOperationKind.CREATE,
                owner_instance_id="winner-owner",
                lease_seconds=30,
            )
            winner = await service.create_blob(
                session_id=session_id,
                filename="winner.txt",
                content=b"winner",
                mime_type="text/plain",
                session_operation_context=winner_context,
            )
            with db_engine.connect() as conn:
                rows = conn.execute(select(blobs_table).where(blobs_table.c.session_id == str(session_id))).all()
            assert [row.id for row in rows] == [str(winner.id)]
            assert not Path(pending.storage_path).exists()

    @pytest.mark.asyncio
    async def test_create_blob_and_read(self, blob_service, session_id, compose_context, tmp_path) -> None:
        content = b"col1,col2\na,b\nc,d"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="data.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=compose_context,
        )

        # Record fields
        assert isinstance(record.id, UUID)
        assert record.session_id == session_id
        assert record.filename == "data.csv"
        assert record.mime_type == "text/csv"
        assert record.size_bytes == len(content)
        assert record.status == "ready"
        assert record.created_by == "user"

        # Read back content
        read_back = await blob_service.read_blob_content(
            record.id,
            session_operation_context=compose_context,
        )
        assert read_back == content

        # File exists on disk
        assert Path(record.storage_path).exists()

    @pytest.mark.asyncio
    async def test_create_blob_with_relative_data_dir_stores_absolute_storage_path(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Blob storage paths are internal paths, not data-dir-relative source paths."""
        monkeypatch.chdir(tmp_path)
        blob_service = BlobServiceImpl(db_engine, Path("data"))

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="tickets.csv",
            content=b"ticket_id\nT-001\n",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        storage_path = Path(record.storage_path)
        assert storage_path.is_absolute()
        assert storage_path == tmp_path / "data" / "blobs" / str(session_id) / f"{record.id}_tickets.csv"
        assert storage_path.exists()

    @pytest.mark.asyncio
    async def test_create_blob_stores_correct_hash(self, blob_service, session_id) -> None:
        """AD-7: stored hash must match content_hash() for the same bytes."""
        content = b"audit-trail-integrity-check"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="audit.txt",
            content=content,
            mime_type="text/plain",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        assert record.content_hash == content_hash(content)


# ---------------------------------------------------------------------------
# list_blobs — session-scoped isolation
# ---------------------------------------------------------------------------


class TestListBlobs:
    """Session scoping: blobs from one session must not leak into another."""

    @pytest.mark.asyncio
    async def test_list_blobs_returns_session_scoped(
        self,
        blob_service,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
    ) -> None:
        s1_id = session_operation_authority.create_session_with_initial_fence(
            user_id="user-a",
            title="Session 1",
            auth_provider_type="local",
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        ).id
        s2_id = session_operation_authority.create_session_with_initial_fence(
            user_id="user-b",
            title="Session 2",
            auth_provider_type="local",
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        ).id

        await blob_service.create_blob(
            session_id=s1_id,
            filename="s1.csv",
            content=b"session-1",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(s1_id),
        )
        await blob_service.create_blob(
            session_id=s2_id,
            filename="s2.csv",
            content=b"session-2",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(s2_id),
        )

        s1_blobs = await blob_service.list_blobs(s1_id)
        s2_blobs = await blob_service.list_blobs(s2_id)

        assert len(s1_blobs) == 1
        assert s1_blobs[0].filename == "s1.csv"
        assert len(s2_blobs) == 1
        assert s2_blobs[0].filename == "s2.csv"


# ---------------------------------------------------------------------------
# delete_blob — file cleanup and active-run guard
# ---------------------------------------------------------------------------


class TestDeleteBlob:
    """Deletion removes file and record; active-run guard prevents evidence destruction."""

    @pytest.mark.asyncio
    async def test_delete_blob_removes_file_and_record(self, blob_service, session_id) -> None:
        from pathlib import Path

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="delete-me.csv",
            content=b"temporary",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        storage = Path(record.storage_path)
        assert storage.exists()

        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert not storage.exists()
        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_commit_failure_restores_file_and_row_after_restart(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        db_engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed metadata commit must not strand a live row without bytes."""
        content = b"commit-boundary-content"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="commit-boundary.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)
        original_do_commit = db_engine.dialect.do_commit
        fail_next_commit = True

        def fail_delete_commit(dbapi_connection) -> None:
            nonlocal fail_next_commit
            if fail_next_commit:
                fail_next_commit = False
                raise RuntimeError("injected blob delete commit failure")
            original_do_commit(dbapi_connection)

        monkeypatch.setattr(db_engine.dialect, "do_commit", fail_delete_commit)

        with pytest.raises(RuntimeError, match="injected blob delete commit failure"):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        restarted = BlobServiceImpl(db_engine, tmp_path)
        restored = await restarted.get_blob(record.id, session_operation_context=_operation_context(session_id))
        assert restored.id == record.id
        assert storage.read_bytes() == content
        assert await restarted.read_blob_content(record.id, session_operation_context=_operation_context(session_id)) == content
        assert list(storage.parent.glob(f".{storage.name}.delete-*")) == []
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
                ).scalar_one()
                == 0
            )

    @pytest.mark.asyncio
    async def test_delete_blob_sql_failure_restores_file_before_stage_escapes(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        db_engine,
        tmp_path: Path,
    ) -> None:
        """A DELETE failure inside the helper restores its unreturned stage."""
        content = b"delete-statement-boundary-content"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="delete-statement-boundary.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)

        def fail_blob_delete(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            if statement.lstrip().upper().startswith("DELETE FROM BLOBS"):
                raise RuntimeError("injected blob DELETE failure")

        event.listen(db_engine, "before_cursor_execute", fail_blob_delete)
        try:
            with pytest.raises(RuntimeError, match="injected blob DELETE failure"):
                await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))
        finally:
            event.remove(db_engine, "before_cursor_execute", fail_blob_delete)

        restarted = BlobServiceImpl(db_engine, tmp_path)
        assert (await restarted.get_blob(record.id, session_operation_context=_operation_context(session_id))).id == record.id
        assert await restarted.read_blob_content(record.id, session_operation_context=_operation_context(session_id)) == content
        assert list(storage.parent.glob(f".{storage.name}.delete-*")) == []

    @pytest.mark.asyncio
    async def test_delete_blob_staging_fsync_failure_restores_file_before_stage_escapes(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        db_engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A staging fsync failure must not strand live metadata without bytes."""
        content = b"staging-fsync-boundary-content"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="staging-fsync-boundary.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)
        fsync_calls = 0

        def fail_staging_fsync(_directory: Path) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 1:
                raise OSError("injected staging directory fsync failure")

        monkeypatch.setattr(blob_service_module, "_fsync_parent_directory", fail_staging_fsync)

        with pytest.raises(OSError, match="injected staging directory fsync failure"):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        restarted = BlobServiceImpl(db_engine, tmp_path)
        assert (await restarted.get_blob(record.id, session_operation_context=_operation_context(session_id))).id == record.id
        assert storage.read_bytes() == content
        assert await restarted.read_blob_content(record.id, session_operation_context=_operation_context(session_id)) == content
        assert list(storage.parent.glob(f".{storage.name}.delete-*")) == []

    @pytest.mark.asyncio
    async def test_delete_blob_stage_mutation_failure_restores_and_aborts_observed_intent(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        db_engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A definite pre-commit stage failure must reverse its filesystem rename."""
        content = b"stage-mutation-boundary-content"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="stage-mutation-boundary.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)

        def fail_mark_staged(_self, *, plan):
            raise RuntimeError("injected blob stage mutation failure")

        monkeypatch.setattr(
            coordination_repository_module._RepositoryBlobMutations,
            "mark_blob_deletion_staged",
            fail_mark_staged,
        )

        with pytest.raises(RuntimeError, match="injected blob stage mutation failure"):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert storage.read_bytes() == content
        assert tuple(storage.parent.glob(f".{storage.name}.delete-*")) == ()
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
                ).scalar_one()
                == 0
            )
            assert conn.execute(select(func.count()).select_from(blobs_table).where(blobs_table.c.id == str(record.id))).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_stale_delete_cannot_race_successor_creation_across_the_file_boundary(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A successor creation recovers a stale delete without losing old bytes."""
        stale = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="stale-delete-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )
        original = await service.create_blob(
            session_id=session_id,
            filename="original.csv",
            content=b"original durable bytes",
            mime_type="text/csv",
            session_operation_context=stale,
        )
        original_storage = Path(original.storage_path)

        @contextlib.contextmanager
        def unlocked_transaction(_self, _session_id: str):
            with db_engine.begin() as conn:
                yield conn

        @contextlib.contextmanager
        def no_process_lock(_engine, _session_id: str):
            yield

        monkeypatch.setattr(
            SQLiteLocalSessionOperationAuthority,
            "_locked_transaction",
            unlocked_transaction,
        )
        monkeypatch.setattr(blob_service_module, "process_session_lock", no_process_lock)

        original_compare_and_swap = session_operation_authority.compare_and_swap
        stale_at_pre_fs_guard = threading.Event()
        release_stale = threading.Event()
        stale_guard_calls = 0

        def pause_after_successful_pre_fs_guard(context: SessionOperationContext) -> None:
            nonlocal stale_guard_calls
            original_compare_and_swap(context)
            if context != stale:
                return
            stale_guard_calls += 1
            if stale_guard_calls == 2:
                stale_at_pre_fs_guard.set()
                if not release_stale.wait(timeout=5):
                    raise TimeoutError("test did not release stale blob deletion")

        monkeypatch.setattr(
            session_operation_authority,
            "compare_and_swap",
            pause_after_successful_pre_fs_guard,
        )

        stale_delete = asyncio.create_task(
            service.delete_blob(
                original.id,
                session_operation_context=stale,
            )
        )
        assert await asyncio.to_thread(stale_at_pre_fs_guard.wait, 5)

        session_operation_authority.release(stale)
        winner_context = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="winner-create-owner",
            lease_seconds=30,
        )
        winner_reconciliation_started = threading.Event()
        original_reconcile = service._reconcile_abandoned_creations

        def observe_winner_reconciliation(context: SessionOperationContext) -> None:
            if context == winner_context:
                winner_reconciliation_started.set()
            original_reconcile(context)

        monkeypatch.setattr(service, "_reconcile_abandoned_creations", observe_winner_reconciliation)
        winner_create = asyncio.create_task(
            service.create_blob(
                session_id=session_id,
                filename="winner.csv",
                content=b"winner durable bytes",
                mime_type="text/csv",
                session_operation_context=winner_context,
            )
        )
        assert await asyncio.to_thread(winner_reconciliation_started.wait, 5)

        release_stale.set()
        with pytest.raises(SessionOperationFenceLost):
            await stale_delete
        winner = await winner_create

        session_operation_authority.compare_and_swap(winner_context)
        assert original_storage.read_bytes() == b"original durable bytes"
        assert Path(winner.storage_path).read_bytes() == b"winner durable bytes"
        assert tuple(original_storage.parent.glob(f".{original_storage.name}.delete-*")) == ()
        with db_engine.connect() as conn:
            rows = conn.execute(select(blobs_table).where(blobs_table.c.session_id == str(session_id))).all()
            cleanup_count = conn.execute(
                select(func.count())
                .select_from(blob_deletion_cleanups_table)
                .where(blob_deletion_cleanups_table.c.blob_id == str(original.id))
            ).scalar_one()
        assert {row.id for row in rows} == {str(original.id), str(winner.id)}
        assert all(row.status == "ready" for row in rows)
        assert cleanup_count == 0

    @pytest.mark.asyncio
    async def test_successor_reads_wait_for_stale_delete_rollback_and_return_exact_bytes(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Full and preview reads never observe the delete tombstone window."""
        stale = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="stale-delete-reader-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )
        content = b"readers must see these exact durable bytes"
        record = await service.create_blob(
            session_id=session_id,
            filename="read-during-delete.txt",
            content=content,
            mime_type="text/plain",
            session_operation_context=stale,
        )
        storage = Path(record.storage_path)

        @contextlib.contextmanager
        def unlocked_transaction(_self, _session_id: str):
            with db_engine.begin() as conn:
                yield conn

        @contextlib.contextmanager
        def no_process_lock(_engine, _session_id: str):
            yield

        monkeypatch.setattr(
            SQLiteLocalSessionOperationAuthority,
            "_locked_transaction",
            unlocked_transaction,
        )
        monkeypatch.setattr(blob_service_module, "process_session_lock", no_process_lock)

        original_file_lock = blob_service_module.filesystem_session_lock
        readers_requested = threading.Event()
        both_readers_attempted = threading.Event()
        reader_lock_acquired = threading.Event()
        reader_attempts = 0

        @contextlib.contextmanager
        def observe_file_lock(root: Path, locked_session_id: str):
            nonlocal reader_attempts
            is_reader = readers_requested.is_set() and locked_session_id == str(session_id)
            if is_reader:
                reader_attempts += 1
                if reader_attempts == 2:
                    both_readers_attempted.set()
            with original_file_lock(root, locked_session_id):
                if is_reader:
                    reader_lock_acquired.set()
                yield

        monkeypatch.setattr(blob_service_module, "filesystem_session_lock", observe_file_lock)

        original_compare_and_swap = session_operation_authority.compare_and_swap
        stale_at_pre_fs_guard = threading.Event()
        release_stale = threading.Event()
        stale_guard_calls = 0

        def pause_after_successful_pre_fs_guard(context: SessionOperationContext) -> None:
            nonlocal stale_guard_calls
            original_compare_and_swap(context)
            if context != stale:
                return
            stale_guard_calls += 1
            if stale_guard_calls == 2:
                stale_at_pre_fs_guard.set()
                if not release_stale.wait(timeout=5):
                    raise TimeoutError("test did not release stale delete before reads")

        monkeypatch.setattr(
            session_operation_authority,
            "compare_and_swap",
            pause_after_successful_pre_fs_guard,
        )

        stale_delete = asyncio.create_task(
            service.delete_blob(
                record.id,
                session_operation_context=stale,
            )
        )
        assert await asyncio.to_thread(stale_at_pre_fs_guard.wait, 5)

        session_operation_authority.release(stale)
        reader_context = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id="successor-reader-owner",
            lease_seconds=30,
        )
        readers_requested.set()
        full_read = asyncio.create_task(
            service.read_blob_content(
                record.id,
                session_operation_context=reader_context,
            )
        )
        preview_read = asyncio.create_task(
            service.read_blob_preview(
                record.id,
                limit_bytes=12,
                session_operation_context=reader_context,
            )
        )
        assert await asyncio.to_thread(both_readers_attempted.wait, 5)
        acquired_before_stale_release = reader_lock_acquired.is_set()

        release_stale.set()
        with pytest.raises(SessionOperationFenceLost):
            await stale_delete
        read_content, preview = await asyncio.gather(full_read, preview_read)

        assert acquired_before_stale_release is False
        assert read_content == content
        assert preview == (content[:12], True)
        session_operation_authority.compare_and_swap(reader_context)
        assert storage.read_bytes() == content
        assert tuple(storage.parent.glob(f".{storage.name}.delete-*")) == ()

    @pytest.mark.asyncio
    async def test_successor_reads_recover_crash_left_delete_intent_and_tombstone(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A crash after rename is recovered before either read surface observes it."""

        class SimulatedWorkerCrash(BaseException):
            pass

        stale = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="crashed-delete-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )
        content = b"crash recovery must preserve these bytes"
        record = await service.create_blob(
            session_id=session_id,
            filename="crash-left-delete.txt",
            content=content,
            mime_type="text/plain",
            session_operation_context=stale,
        )
        storage = Path(record.storage_path)
        original_compare_and_swap = session_operation_authority.compare_and_swap
        stale_guard_calls = 0

        def crash_at_post_fs_guard(context: SessionOperationContext) -> None:
            nonlocal stale_guard_calls
            if context == stale:
                stale_guard_calls += 1
                if stale_guard_calls == 3:
                    raise SimulatedWorkerCrash
            original_compare_and_swap(context)

        monkeypatch.setattr(
            session_operation_authority,
            "compare_and_swap",
            crash_at_post_fs_guard,
        )

        with pytest.raises(SimulatedWorkerCrash):
            await service.delete_blob(
                record.id,
                session_operation_context=stale,
            )

        assert not storage.exists()
        tombstones = tuple(storage.parent.glob(f".{storage.name}.delete-*"))
        assert len(tombstones) == 1
        assert tombstones[0].read_bytes() == content
        with db_engine.connect() as conn:
            cleanup = conn.execute(
                select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            ).one()
        assert cleanup.phase == "intent"

        session_operation_authority.release(stale)
        reader_context = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.BLOB_READ,
            owner_instance_id="crash-recovery-reader",
            lease_seconds=30,
        )
        read_content = await service.read_blob_content(
            record.id,
            session_operation_context=reader_context,
        )
        preview = await service.read_blob_preview(
            record.id,
            limit_bytes=9,
            session_operation_context=reader_context,
        )

        assert read_content == content
        assert preview == (content[:9], True)
        session_operation_authority.compare_and_swap(reader_context)
        assert storage.read_bytes() == content
        assert tuple(storage.parent.glob(f".{storage.name}.delete-*")) == ()
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
                ).scalar_one()
                == 0
            )

    @pytest.mark.asyncio
    async def test_delete_blob_tombstone_unlink_failure_is_retryable_after_restart(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        db_engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A committed delete retains enough state to retry a failed unlink."""
        content = b"retry-tombstone-unlink-content"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="retry-tombstone-unlink.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)
        original_unlink = Path.unlink
        fail_tombstone_unlink = True

        def fail_first_tombstone_unlink(path: Path, missing_ok: bool = False) -> None:
            nonlocal fail_tombstone_unlink
            if fail_tombstone_unlink and ".delete-" in path.name:
                fail_tombstone_unlink = False
                raise OSError("injected tombstone unlink failure")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_first_tombstone_unlink)

        delete_context = _operation_context(session_id)
        with pytest.raises(OSError, match="injected tombstone unlink failure"):
            await blob_service.delete_blob(record.id, session_operation_context=delete_context)

        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))
        tombstones = list(storage.parent.glob(f".{storage.name}.delete-*"))
        assert len(tombstones) == 1
        assert tombstones[0].read_bytes() == content
        with db_engine.connect() as conn:
            cleanup = conn.execute(
                select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            ).one()
            assert conn.execute(select(func.count()).select_from(blobs_table).where(blobs_table.c.id == str(record.id))).scalar_one() == 0
        assert cleanup.phase == "purge_pending"
        assert cleanup.operation_id == delete_context.fence.operation_id
        assert cleanup.operation_epoch == delete_context.fence.operation_epoch
        assert cleanup.operation_kind == delete_context.operation_kind.value

        restarted = BlobServiceImpl(db_engine, tmp_path)
        await restarted.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert list(storage.parent.glob(f".{storage.name}.delete-*")) == []
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
                ).scalar_one()
                == 0
            )

    @pytest.mark.asyncio
    async def test_delete_blob_post_unlink_fsync_failure_is_retryable_after_restart(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        db_engine,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A retry repeats directory fsync after unlink succeeded but fsync failed."""
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="retry-post-unlink-fsync.csv",
            content=b"retry-post-unlink-fsync-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)
        fsync_calls = 0

        def fail_first_purge_fsync(_directory: Path) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError("injected post-unlink directory fsync failure")

        monkeypatch.setattr(blob_service_module, "_fsync_parent_directory", fail_first_purge_fsync)

        delete_context = _operation_context(session_id)
        with pytest.raises(OSError, match="injected post-unlink directory fsync failure"):
            await blob_service.delete_blob(record.id, session_operation_context=delete_context)

        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))
        assert not storage.exists()
        assert list(storage.parent.glob(f".{storage.name}.delete-*")) == []
        with db_engine.connect() as conn:
            cleanup = conn.execute(
                select(blob_deletion_cleanups_table).where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
            ).one()
        assert cleanup.phase == "purge_pending"
        assert cleanup.operation_id == delete_context.fence.operation_id
        assert cleanup.operation_epoch == delete_context.fence.operation_epoch
        assert cleanup.operation_kind == delete_context.operation_kind.value

        restarted = BlobServiceImpl(db_engine, tmp_path)
        await restarted.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert fsync_calls == 3
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(record.id))
                ).scalar_one()
                == 0
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            ("set_pipeline", lambda blob_id: {"source": {"blob_id": blob_id}}),
            ("set_source_from_blob", lambda blob_id: {"blob_id": blob_id}),
            ("update_blob", lambda blob_id: {"blob_id": blob_id}),
            ("wire_blob_inline_ref", lambda blob_id: {"blob_id": blob_id}),
        ],
    )
    async def test_delete_blob_rejects_blob_referenced_by_pending_proposal(
        self,
        blob_service,
        session_id,
        db_engine,
        tool_name,
        arguments,
    ) -> None:
        contracts = importlib.import_module("elspeth.contracts.blobs")
        pending_error = contracts.BlobPendingProposalError
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="pending-review.csv",
            content=b"review me",
            mime_type="text/csv",
            created_by="assistant",
            session_operation_context=_operation_context(session_id),
        )
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                insert(composition_proposals_table).values(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    tool_call_id="call_pending_blob_delete_guard",
                    tool_name=tool_name,
                    status="pending",
                    summary="Review blob-backed pipeline",
                    rationale="Server generated",
                    affects=["source"],
                    arguments_json=arguments(str(record.id)),
                    arguments_redacted_json=arguments(str(record.id)),
                    base_state_id=None,
                    committed_state_id=None,
                    audit_event_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        with pytest.raises(pending_error):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert Path(record.storage_path).exists()
        assert await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id)) == record

    @pytest.mark.asyncio
    async def test_delete_blob_allows_blob_only_referenced_by_rejected_proposal(self, blob_service, session_id, db_engine) -> None:
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="rejected-review.csv",
            content=b"no longer retained",
            mime_type="text/csv",
            created_by="assistant",
            session_operation_context=_operation_context(session_id),
        )
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                insert(composition_proposals_table).values(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    tool_call_id="call_rejected_blob_delete_guard",
                    tool_name="set_pipeline",
                    status="rejected",
                    summary="Rejected blob-backed pipeline",
                    rationale="Server generated",
                    affects=["source"],
                    arguments_json={"source": {"blob_id": str(record.id)}},
                    arguments_redacted_json={"source": {"blob_id": str(record.id)}},
                    base_state_id=None,
                    committed_state_id=None,
                    audit_event_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_pending_delete_proposal_blocks_direct_compose_deletion_without_accepting_binding(
        self,
        blob_service,
        session_id,
        db_engine,
    ) -> None:
        contracts = importlib.import_module("elspeth.contracts.blobs")
        pending_error = contracts.BlobPendingProposalError
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="delete-target.csv",
            content=b"delete me",
            mime_type="text/csv",
            created_by="assistant",
            session_operation_context=_operation_context(session_id),
        )
        now = datetime.now(UTC)
        proposal_id = uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                insert(composition_proposals_table).values(
                    id=str(proposal_id),
                    session_id=str(session_id),
                    tool_call_id="call_delete_blob_proposal",
                    tool_name="delete_blob",
                    status="pending",
                    summary="Delete blob",
                    rationale="Server generated",
                    affects=["blob"],
                    arguments_json={"blob_id": str(record.id)},
                    arguments_redacted_json={"blob_id": str(record.id)},
                    base_state_id=None,
                    committed_state_id=None,
                    audit_event_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        with pytest.raises(pending_error, match=str(proposal_id)):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert Path(record.storage_path).read_bytes() == b"delete me"
        assert await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id)) == record

    @pytest.mark.asyncio
    async def test_unrelated_nested_blob_id_does_not_create_pending_retention(self, blob_service, session_id, db_engine) -> None:
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="unrelated.csv",
            content=b"not a source binding",
            mime_type="text/csv",
            created_by="assistant",
            session_operation_context=_operation_context(session_id),
        )
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                insert(composition_proposals_table).values(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    tool_call_id="call_unrelated_blob_id",
                    tool_name="set_pipeline",
                    status="pending",
                    summary="Unrelated nested value",
                    rationale="Server generated",
                    affects=["node"],
                    arguments_json={
                        "source": {"plugin": "csv", "options": {}},
                        "nodes": [{"options": {"blob_id": str(record.id)}}],
                    },
                    arguments_redacted_json={"source": {"plugin": "csv", "options": {}}},
                    base_state_id=None,
                    committed_state_id=None,
                    audit_event_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))
        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_pending_proposal_retention_does_not_block_blob_finalization(self, blob_service, session_id, db_engine) -> None:
        record = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="pending-output.csv",
            mime_type="text/csv",
            created_by="assistant",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        content = b"ready\n1\n"
        Path(record.storage_path).write_bytes(content)
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                insert(composition_proposals_table).values(
                    id=str(uuid4()),
                    session_id=str(session_id),
                    tool_call_id="call_pending_finalize",
                    tool_name="set_pipeline",
                    status="pending",
                    summary="Retain pending output",
                    rationale="Server generated",
                    affects=["source"],
                    arguments_json={"source": {"blob_id": str(record.id)}},
                    arguments_redacted_json={"source": {"blob_id": str(record.id)}},
                    base_state_id=None,
                    committed_state_id=None,
                    audit_event_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        finalized = await blob_service.finalize_blob(
            record.id,
            "ready",
            size_bytes=len(content),
            content_hash=content_hash(content),
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        assert finalized.status == "ready"

    @pytest.mark.asyncio
    async def test_delete_blob_rejects_when_active_run_linked(self, blob_service, session_id, db_engine) -> None:
        """Active-run guard: cannot delete a blob that is evidence for a live run."""
        from elspeth.web.sessions.models import (
            blob_run_links_table,
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="evidence.csv",
            content=b"important",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        # Insert a composition state (runs FK to composition_states)
        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="running",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(record.id),
                    run_id=run_id,
                    direction="input",
                )
            )

        with pytest.raises(BlobActiveRunError):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_allows_when_completed_run_linked(self, blob_service, session_id, db_engine) -> None:
        """Completed runs do not block deletion — evidence is already recorded."""
        from elspeth.web.sessions.models import (
            blob_run_links_table,
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="done.csv",
            content=b"finished",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="completed",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=10,
                    rows_failed=0,
                )
            )
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(record.id),
                    run_id=run_id,
                    direction="input",
                )
            )

        # Should succeed — completed run does not block deletion
        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_preserves_completed_inline_resolution_audit_rows(self, blob_service, session_id, db_engine) -> None:
        """Completed inline-content audit rows must not turn blob deletion into a 500."""
        from elspeth.web.sessions.models import (
            blob_inline_resolutions_table,
            blob_run_links_table,
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="prompt.txt",
            content=b"finished prompt",
            mime_type="text/plain",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())
        now = datetime(2026, 1, 1, tzinfo=UTC)

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="completed",
                    started_at=now,
                    rows_processed=10,
                    rows_failed=0,
                )
            )
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(record.id),
                    run_id=run_id,
                    direction="input",
                )
            )
            conn.execute(
                blob_inline_resolutions_table.insert().values(
                    run_id=run_id,
                    attempt=1,
                    field_path="node:classify.options.system_prompt",
                    blob_id=str(record.id),
                    content_hash=record.content_hash,
                    byte_length=record.size_bytes,
                    mime_type=record.mime_type,
                    encoding="utf-8",
                    resolved_at=now,
                )
            )

        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        with db_engine.connect() as conn:
            rows = conn.execute(select(blob_inline_resolutions_table)).fetchall()

        assert len(rows) == 1
        assert rows[0].blob_id == str(record.id)
        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_rejects_when_active_run_exists_without_link(self, blob_service, session_id, db_engine) -> None:
        """Pre-link window: active run exists but blob_run_links row hasn't been created yet.

        _execute_locked() creates the run record before link_blob_to_run()
        inserts the link row.  During that gap, the explicit-link guard sees
        nothing.  The composition-state guard must block deletion because
        the run's source references this blob via blob_ref.
        """
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="pre-link.csv",
            content=b"important",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    # Source references this blob via blob_ref — the run is
                    # about to link it once link_blob_to_run() fires.
                    source={
                        "plugin": "csv",
                        "on_success": "output",
                        "on_validation_failure": "quarantine",
                        "options": {"blob_ref": str(record.id), "path": str(record.storage_path)},
                    },
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "Test", "description": ""},
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="pending",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )
            # Deliberately NO blob_run_links row — simulating the pre-link window

        with pytest.raises(BlobActiveRunError):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_allows_when_active_run_uses_different_source(self, blob_service, session_id, db_engine) -> None:
        """Active run using source.path (no blob_ref) must not block unrelated blob deletion.

        Regression test: the original session-level guard blocked ALL blobs
        when ANY run was active, even if that run used a file-path source
        with no blob_ref.  The scoped guard checks the composition state's
        source.options.blob_ref and only blocks if it matches this blob.
        """
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="unrelated.csv",
            content=b"not used by run",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    # Source uses file path, NOT blob_ref — run is unrelated
                    # to the blob being deleted.
                    source={
                        "plugin": "csv",
                        "on_success": "output",
                        "on_validation_failure": "quarantine",
                        "options": {"path": "/data/external/other.csv"},
                    },
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "Test", "description": ""},
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="pending",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )

        # Should succeed — active run does not reference this blob
        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_rejects_when_transform_option_references_blob(self, blob_service, session_id, db_engine) -> None:
        """Pre-link guard walks canonical pipeline sections beyond source.options."""
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="prompt.txt",
            content=b"Classify the row.",
            mime_type="text/plain",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    source={
                        "plugin": "csv",
                        "on_success": "classify",
                        "on_validation_failure": "quarantine",
                        "options": {"path": "/data/external/other.csv"},
                    },
                    nodes=[
                        {
                            "id": "classify",
                            "node_type": "transform",
                            "plugin": "llm",
                            "input": "source_out",
                            "on_success": "output",
                            "on_error": "discard",
                            "options": {
                                "system_prompt": {
                                    "blob_ref": str(record.id),
                                    "mode": "inline_content",
                                    "sha256": record.content_hash,
                                }
                            },
                        }
                    ],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "Test", "description": ""},
                    is_valid=True,
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="pending",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )

        with pytest.raises(BlobActiveRunError):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_rejects_when_active_run_path_matches_storage(self, blob_service, session_id, db_engine) -> None:
        """Active run using source.path matching this blob's storage_path must block.

        A run can read a blob's backing file via plain set_source with
        options.path (no blob_ref).  The guard must check path/file matches
        in addition to blob_ref.
        """
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="path-backed.csv",
            content=b"path match",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    # Source references this blob via path, NOT blob_ref.
                    source={
                        "plugin": "csv",
                        "on_success": "output",
                        "on_validation_failure": "quarantine",
                        "options": {"path": record.storage_path},
                    },
                    nodes=[],
                    edges=[],
                    outputs=[],
                    metadata_={"name": "Test", "description": ""},
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="pending",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )

        with pytest.raises(BlobActiveRunError):
            await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_delete_blob_allows_when_completed_run_exists_without_link(self, blob_service, session_id, db_engine) -> None:
        """Completed runs (without link row) must not block deletion."""
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="completed-no-link.csv",
            content=b"done",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="completed",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )

        # Should succeed — completed run does not block deletion
        await blob_service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        with pytest.raises(BlobNotFoundError):
            await blob_service.get_blob(record.id, session_operation_context=_operation_context(session_id))


# ---------------------------------------------------------------------------
# finalize_blob — pending lifecycle transitions
# ---------------------------------------------------------------------------


class TestCreatePendingBlob:
    """Pending blob reservation must enforce the same literal guards as ready writes."""

    @pytest.mark.asyncio
    async def test_create_pending_blob_rejects_disallowed_mime_type(self, blob_service, session_id) -> None:
        """Pending rows must not persist MIME values that read guards classify as corruption."""
        with pytest.raises(RuntimeError, match="Invalid mime_type"):
            await blob_service.create_pending_blob(
                session_id=session_id,
                filename="output.png",
                mime_type="image/png",  # type: ignore[arg-type]
                created_by="pipeline",
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )


class TestFinalizeBlob:
    """Pending -> ready/error lifecycle: only valid transitions allowed."""

    @pytest.mark.asyncio
    async def test_finalize_blob_transitions_pending_to_ready(self, blob_service, session_id) -> None:
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        assert pending.status == "pending"

        # Valid SHA-256 hex is required when transitioning to 'ready' —
        # see _validate_finalize_hash().  Using content_hash() here
        # anchors the test to the same helper production code uses.
        valid_hash = content_hash(b"pretend-output-bytes")
        finalized = await blob_service.finalize_blob(
            blob_id=pending.id,
            status="ready",
            size_bytes=42,
            content_hash=valid_hash,
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        assert finalized.status == "ready"
        assert finalized.size_bytes == 42
        assert finalized.content_hash == valid_hash

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_missing_hash_for_ready(self, blob_service, session_id) -> None:
        """Tier 1 invariant: finalizing as 'ready' without a hash is refused."""
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        from elspeth.web.blobs.protocol import BlobStateError

        with pytest.raises(BlobStateError, match="content_hash"):
            await blob_service.finalize_blob(
                blob_id=pending.id,
                status="ready",
                size_bytes=42,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_non_sha256_hash(self, blob_service, session_id) -> None:
        """Tier 1 invariant: content_hash must be 64 lowercase hex chars."""
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        from elspeth.web.blobs.protocol import BlobStateError

        with pytest.raises(BlobStateError, match="64 lowercase hex"):
            await blob_service.finalize_blob(
                blob_id=pending.id,
                status="ready",
                size_bytes=42,
                content_hash="abc123",  # too short, not SHA-256
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_uppercase_hex_hash(self, blob_service, session_id) -> None:
        """Canonical form is lowercase — uppercase hex is a bifurcation risk.

        FilesystemPayloadStore writes the lowercase form, and
        read_blob_content compares via hmac.compare_digest byte-for-byte.
        Admitting uppercase at the write side would silently create
        blobs whose hash does not match the stored form anywhere else
        in the audit trail.  Mirrors the same assertion on the sync
        path (TestFinalizeBlobSyncHashValidation) so both entry points
        are pinned.
        """
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        from elspeth.web.blobs.protocol import BlobStateError

        uppercase_hash = content_hash(b"real-bytes").upper()
        with pytest.raises(BlobStateError, match="64 lowercase hex"):
            await blob_service.finalize_blob(
                blob_id=pending.id,
                status="ready",
                size_bytes=10,
                content_hash=uppercase_hash,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_trailing_newline_hash(self, blob_service, session_id) -> None:
        """``^[a-f0-9]{64}$`` + ``re.match`` accepts trailing ``\\n``; fullmatch rejects it.

        Python's ``$`` anchor matches either end-of-string OR just
        before a final newline.  A 64-hex hash followed by a single
        ``\\n`` therefore slipped through the service-layer pre-check
        under the old regex and landed at the DB, where the CHECK
        constraint rejected it as an IntegrityError — the wrong failure
        surface (opaque DB error rather than the clean BlobStateError
        this validator is supposed to raise, and coverage on the
        DB-authoritative guard only).  The service pre-check uses
        ``fullmatch`` so the error path is always the structured
        BlobStateError, and the DB CHECK remains the belt for any
        writer that bypasses the service entirely.
        """
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        from elspeth.web.blobs.protocol import BlobStateError

        trailing_newline_hash = content_hash(b"real-bytes") + "\n"
        with pytest.raises(BlobStateError, match="64 lowercase hex"):
            await blob_service.finalize_blob(
                blob_id=pending.id,
                status="ready",
                size_bytes=10,
                content_hash=trailing_newline_hash,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_finalize_blob_as_error_without_hash_succeeds(self, blob_service, session_id) -> None:
        """The hash invariant applies only to 'ready' — 'error' needs no hash.

        Pins the ``status != 'ready'`` exemption branch of
        _validate_finalize_hash.  A regression that tightened the
        invariant to require hashes for error blobs would break every
        failed-run cleanup path, and the failure mode would be
        non-obvious (pipeline-level errors finalizing per-blob errors).
        This positive test keeps the exemption honest.
        """
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="failed-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        record = await blob_service.finalize_blob(
            blob_id=pending.id,
            status="error",
            # deliberately no content_hash, no size_bytes
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        assert record.status == "error"
        assert record.content_hash is None

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_non_pending(self, blob_service, session_id) -> None:
        """Cannot finalize a blob that is already ready — status rollback is forbidden."""
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="already-ready.csv",
            content=b"done",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        assert record.status == "ready"

        from elspeth.web.blobs.protocol import BlobStateError

        with pytest.raises(BlobStateError, match="expected 'pending'"):
            await blob_service.finalize_blob(
                blob_id=record.id,
                status="ready",
                size_bytes=4,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_invalid_status(self, blob_service, session_id) -> None:
        """Only 'ready' and 'error' are valid finalize targets."""
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        # Deliberate type-contract violation: we're exercising the
        # runtime guard for dynamic callers that bypass static typing.
        # `blob_service` is a pytest fixture whose type mypy treats as
        # Any, so no `# type: ignore` is needed here to suppress the
        # arg-type error.
        with pytest.raises(RuntimeError, match="Invalid finalize status"):
            await blob_service.finalize_blob(
                blob_id=pending.id,
                status="deleted",
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )


# ---------------------------------------------------------------------------
# Blob quota — per-session storage limit (AD-10)
# ---------------------------------------------------------------------------


class TestBlobQuota:
    """Per-session cumulative storage quota prevents unbounded disk growth."""

    def test_quota_lock_statement_serializes_session_writers_on_postgresql(self) -> None:
        """Quota writers must lock the session row on MVCC databases."""
        statement = blob_service_module._session_quota_lock_statement("session-1")

        compiled = str(statement.compile(dialect=postgresql.dialect()))

        assert "FROM sessions" in compiled
        assert "WHERE sessions.id = " in compiled
        assert "FOR UPDATE" in compiled

    @pytest.mark.asyncio
    async def test_create_blob_reserves_quota_inside_fenced_uow(
        self,
        db_engine,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id,
        tmp_path,
        monkeypatch,
    ) -> None:
        """create_blob must calculate quota inside the exact fenced reservation UoW."""
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            max_storage_per_session=200,
            session_operation_authority=session_operation_authority,
        )
        reservation_contexts: list[SessionOperationContext] = []
        original_mutate = session_operation_authority.mutate

        def recording_mutate(context, mutation):
            if mutation.__name__ == "reserve_standalone_blob":
                reservation_contexts.append(context)
            return original_mutate(context, mutation)

        monkeypatch.setattr(session_operation_authority, "mutate", recording_mutate)
        context = _operation_context(session_id)

        await service.create_blob(
            session_id=session_id,
            filename="serialized.csv",
            content=b"x" * 50,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=context,
        )

        assert reservation_contexts == [context]

    @pytest.mark.asyncio
    async def test_finalize_blob_calculates_quota_inside_exact_fenced_uow(
        self,
        db_engine,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id,
        tmp_path,
        monkeypatch,
    ) -> None:
        """finalize_blob must calculate quota inside its exact fenced mutation UoW."""
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            max_storage_per_session=200,
            session_operation_authority=session_operation_authority,
        )
        context = _operation_context(session_id, SessionOperationKind.EXECUTE)
        pending = await service.create_pending_blob(
            session_id=session_id,
            filename="serialized-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=context,
        )
        finalization_contexts: list[SessionOperationContext] = []
        original_mutate = session_operation_authority.mutate

        def recording_mutate(operation_context, mutation):
            if mutation.__name__ == "finalize_pending":
                finalization_contexts.append(operation_context)
            return original_mutate(operation_context, mutation)

        monkeypatch.setattr(session_operation_authority, "mutate", recording_mutate)

        await service.finalize_blob(
            blob_id=pending.id,
            status="ready",
            size_bytes=50,
            content_hash=content_hash(b"finalized"),
            session_operation_context=context,
        )

        assert finalization_contexts == [context]

    @pytest.mark.asyncio
    async def test_quota_rejects_when_exceeded(self, db_engine, session_id, tmp_path) -> None:
        """Upload that would exceed the session quota returns BlobQuotaExceededError."""
        from elspeth.web.blobs.protocol import BlobQuotaExceededError

        # Tiny quota: 100 bytes
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)

        # First blob: 60 bytes — fits
        await service.create_blob(
            session_id=session_id,
            filename="a.csv",
            content=b"x" * 60,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        # Second blob: 60 bytes — total would be 120 > 100
        with pytest.raises(BlobQuotaExceededError):
            await service.create_blob(
                session_id=session_id,
                filename="b.csv",
                content=b"x" * 60,
                mime_type="text/csv",
                created_by="user",
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_quota_allows_within_limit(self, db_engine, session_id, tmp_path) -> None:
        """Uploads within the quota succeed."""
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=200)

        await service.create_blob(
            session_id=session_id,
            filename="a.csv",
            content=b"x" * 90,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        record = await service.create_blob(
            session_id=session_id,
            filename="b.csv",
            content=b"x" * 90,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        assert record.status == "ready"

    @pytest.mark.asyncio
    async def test_finalize_blob_rejects_ready_size_that_exceeds_quota(self, db_engine, session_id, tmp_path) -> None:
        """Public finalize_blob must enforce quota when pending output size becomes known."""
        from elspeth.web.blobs.protocol import BlobQuotaExceededError

        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=10)
        pending = await service.create_pending_blob(
            session_id=session_id,
            filename="oversized-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        with pytest.raises(BlobQuotaExceededError):
            await service.finalize_blob(
                blob_id=pending.id,
                status="ready",
                size_bytes=100,
                content_hash=content_hash(b"oversized-output"),
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

        record = await service.get_blob(pending.id, session_operation_context=_operation_context(session_id))
        assert record.status == "pending"
        assert record.size_bytes == 0
        assert record.content_hash is None


# ---------------------------------------------------------------------------
# inline custody — deterministic identity, recovery, and exactly-once quota
# ---------------------------------------------------------------------------


def _inline_custody_contract() -> tuple[type[object], object]:
    """Load the desired Task-2 contract inside the test for a clean RED."""
    contracts = importlib.import_module("elspeth.contracts.blobs")
    return contracts.InlineCustodyRequest, blob_service_module.inline_custody_blob_id


def _seed_custody_message(db_engine, session_id: UUID) -> str:
    message_id = str(uuid4())
    now = datetime.now(UTC)
    with db_engine.begin() as conn:
        conn.execute(
            insert(chat_messages_table).values(
                id=message_id,
                session_id=str(session_id),
                role="user",
                content="Create the inline source.",
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=now,
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    return message_id


def _custody_request(db_engine, session_id: UUID, *, content: bytes = b"value\n42\n", description: str | None = "candidate") -> object:
    request_type, _ = _inline_custody_contract()
    return request_type(
        session_id=session_id,
        filename="candidate.csv",
        content=content,
        mime_type="text/csv",
        source_description=description,
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=_seed_custody_message(db_engine, session_id),
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def _custody_process(
    database_url: str,
    data_dir: str,
    request_fields: dict[str, object],
    session_operation_context: SessionOperationContext,
    start_event: object,
    result_queue: object,
) -> None:
    """Spawn-safe worker proving PostgreSQL exclusion crosses processes."""
    request_type, _ = _inline_custody_contract()
    engine = create_session_engine(database_url)
    normalized_fields = dict(request_fields)
    normalized_fields["session_id"] = UUID(str(request_fields["session_id"]))
    normalized_fields["creation_modality"] = CreationModality(str(request_fields["creation_modality"]))
    request = request_type(**normalized_fields)
    try:
        if not start_event.wait(timeout=15):  # type: ignore[attr-defined]
            raise RuntimeError("PostgreSQL custody process start barrier timed out")
        record = asyncio.run(
            BlobServiceImpl(engine, Path(data_dir), max_storage_per_session=100).reserve_inline_custody(
                request,
                session_operation_context=session_operation_context,
            )
        )
        result_queue.put(("ok", str(record.id)))  # type: ignore[attr-defined]
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))  # type: ignore[attr-defined]
    finally:
        engine.dispose()


class TestInlineCustody:
    @staticmethod
    def _guided_operation_write_fence(
        db_engine,
        session_id: UUID,
        *,
        kind: str = "guided_plan",
    ) -> BlobGuidedOperationWriteFence:
        operation_id = str(uuid4())
        lease_token = uuid4().hex
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                guided_operations_table.insert().values(
                    session_id=str(session_id),
                    operation_id=operation_id,
                    kind=kind,
                    status="in_progress",
                    request_hash="a" * 64,
                    lease_token=lease_token,
                    lease_expires_at=now + timedelta(hours=1),
                    attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        return BlobGuidedOperationWriteFence(
            session_id=session_id,
            operation_id=operation_id,
            lease_token=lease_token,
            attempt=1,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["guided_plan", "guided_respond"])
    async def test_guided_inline_custody_accepts_closed_planning_operation_kinds(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        kind: str,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        fence = self._guided_operation_write_fence(db_engine, session_id, kind=kind)

        record = await service.reserve_inline_custody(
            request, write_fence=fence, session_operation_context=_operation_context(request.session_id)
        )

        assert record.status == "ready"
        assert Path(record.storage_path).read_bytes() == request.content

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalidity", ["wrong_kind", "wrong_token", "wrong_attempt"])
    async def test_guided_inline_custody_requires_live_fence_at_reservation(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        invalidity: str,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        fence = self._guided_operation_write_fence(
            db_engine,
            session_id,
            kind="guided_chat" if invalidity == "wrong_kind" else "guided_plan",
        )
        if invalidity == "wrong_token":
            fence = replace(fence, lease_token="wrong-token")
        elif invalidity == "wrong_attempt":
            fence = replace(fence, attempt=2)

        with pytest.raises(BlobGuidedOperationFenceLostError):
            await service.reserve_inline_custody(
                request, write_fence=fence, session_operation_context=_operation_context(request.session_id)
            )

        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 0
        assert tuple(path for path in (tmp_path / "blobs").rglob("*") if path.is_file()) == ()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "takeover_values",
        [
            {"kind": "guided_chat"},
            {"lease_token": "takeover-lease"},
            {"attempt": 2},
        ],
        ids=["wrong-kind", "wrong-token", "wrong-attempt"],
    )
    async def test_guided_inline_custody_rechecks_fence_at_ready_write(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        takeover_values: dict[str, object],
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        fence = self._guided_operation_write_fence(db_engine, session_id)
        original_write = blob_service_module._write_or_validate_reserved_blob

        def _write_after_takeover(**kwargs):
            wrote = original_write(**kwargs)
            with db_engine.begin() as conn:
                changed = conn.execute(
                    guided_operations_table.update()
                    .where(guided_operations_table.c.session_id == str(session_id))
                    .where(guided_operations_table.c.operation_id == fence.operation_id)
                    .where(guided_operations_table.c.lease_token == fence.lease_token)
                    .where(guided_operations_table.c.attempt == fence.attempt)
                    .values(**takeover_values, updated_at=datetime.now(UTC))
                ).rowcount
            assert changed == 1
            return wrote

        monkeypatch.setattr(blob_service_module, "_write_or_validate_reserved_blob", _write_after_takeover)

        with pytest.raises(BlobGuidedOperationFenceLostError):
            await service.reserve_inline_custody(
                request, write_fence=fence, session_operation_context=_operation_context(request.session_id)
            )

        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table.c.status).where(blobs_table.c.session_id == str(session_id))).one()
        assert row.status == "pending"

    @pytest.mark.asyncio
    async def test_nonidempotent_duplicate_does_not_delete_existing_ready_file(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        fixed_blob_id = uuid4()
        monkeypatch.setattr(blob_service_module, "uuid4", lambda: fixed_blob_id)

        winner = await service.create_blob(
            session_id=session_id,
            filename="winner.csv",
            content=b"value\n42\n",
            mime_type="text/csv",
            session_operation_context=_operation_context(session_id),
        )

        with pytest.raises(BlobIntegrityError, match="content integrity failure"):
            await service.create_blob(
                session_id=session_id,
                filename="winner.csv",
                content=b"value\n42\n",
                mime_type="text/csv",
                session_operation_context=_operation_context(session_id),
            )

        assert Path(winner.storage_path).read_bytes() == b"value\n42\n"
        assert (await service.get_blob(fixed_blob_id, session_operation_context=_operation_context(session_id))).status == "ready"

    @pytest.mark.asyncio
    async def test_nonidempotent_failure_preserves_preexisting_orphan_file(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from elspeth.web.blobs.protocol import BlobIntegrityError

        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        fixed_blob_id = uuid4()
        monkeypatch.setattr(blob_service_module, "uuid4", lambda: fixed_blob_id)
        storage = tmp_path.resolve() / "blobs" / str(session_id) / f"{fixed_blob_id}_orphan.csv"
        storage.parent.mkdir(parents=True)
        storage.write_bytes(b"preexisting integrity evidence")

        with pytest.raises(BlobIntegrityError):
            await service.create_blob(
                session_id=session_id,
                filename="orphan.csv",
                content=b"new bytes",
                mime_type="text/csv",
                session_operation_context=_operation_context(session_id),
            )

        assert storage.read_bytes() == b"preexisting integrity evidence"
        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 0

    def test_atomic_write_fsyncs_parent_directory_after_replace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        storage = tmp_path / "blobs" / "session" / "blob_candidate.csv"
        fsynced: list[Path] = []
        monkeypatch.setattr(
            blob_service_module,
            "_fsync_parent_directory",
            lambda path: fsynced.append(path),
            raising=False,
        )

        blob_service_module._atomic_write_blob(storage, b"value\n42\n")

        assert storage.read_bytes() == b"value\n42\n"
        assert fsynced == [storage.parent]

    def test_uuid5_identity_covers_every_authority_field_except_final_arguments_hash(
        self,
        db_engine,
        session_id: UUID,
    ) -> None:
        request_type, derive_blob_id = _inline_custody_contract()
        base = request_type(
            session_id=session_id,
            filename="candidate.csv",
            content=b"value\n42\n",
            mime_type="text/csv",
            source_description="candidate",
            creation_modality=CreationModality.LLM_GENERATED,
            created_from_message_id=_seed_custody_message(db_engine, session_id),
            creating_model_identifier="model-a",
            creating_model_version="version-a",
            creating_provider="provider-a",
            creating_composer_skill_hash="a" * 64,
            creating_arguments_hash="b" * 64,
        )
        baseline = derive_blob_id(base)
        second_session = uuid4()
        with db_engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=str(second_session),
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Second Session",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        variants = (
            replace(base, session_id=second_session),
            replace(base, created_from_message_id=str(uuid4())),
            replace(base, content=b"value\n43\n"),
            replace(base, mime_type="text/plain"),
            replace(base, filename="different.csv"),
            replace(base, source_description="different purpose"),
            replace(base, creation_modality=CreationModality.DISAMBIGUATED),
            replace(base, creating_model_identifier="model-b"),
            replace(base, creating_model_version="version-b"),
            replace(base, creating_provider="provider-b"),
            replace(base, creating_composer_skill_hash="c" * 64),
        )

        assert all(derive_blob_id(variant) != baseline for variant in variants)
        assert derive_blob_id(replace(base, filename="nested/candidate.csv")) == baseline
        assert derive_blob_id(replace(base, creating_arguments_hash="d" * 64)) == baseline

    @pytest.mark.parametrize("field_name", ["creating_composer_skill_hash", "creating_arguments_hash"])
    @pytest.mark.parametrize("invalid_hash", ["A" * 64, "a" * 63, "g" * 64, ""])
    def test_custody_rejects_noncanonical_provenance_hashes_without_echoing_values(
        self,
        db_engine,
        session_id: UUID,
        field_name: str,
        invalid_hash: str,
    ) -> None:
        request_type, derive_blob_id = _inline_custody_contract()
        values = {
            "session_id": session_id,
            "filename": "candidate.csv",
            "content": b"value\n42\n",
            "mime_type": "text/csv",
            "source_description": "candidate",
            "creation_modality": CreationModality.LLM_GENERATED,
            "created_from_message_id": _seed_custody_message(db_engine, session_id),
            "creating_model_identifier": "model-a",
            "creating_model_version": "version-a",
            "creating_provider": "provider-a",
            "creating_composer_skill_hash": "a" * 64,
            "creating_arguments_hash": "b" * 64,
        }
        values[field_name] = invalid_hash

        with pytest.raises(AuditIntegrityError) as exc_info:
            derive_blob_id(request_type(**values))

        assert field_name in str(exc_info.value)
        if invalid_hash:
            assert invalid_hash not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_retry_reuses_uuid5_blob_and_charges_quota_once(self, db_engine, session_id: UUID, tmp_path: Path) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)

        first = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))
        retried = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        assert first == retried
        assert first.id.version == 5
        assert first.status == "ready"
        assert Path(first.storage_path).read_bytes() == request.content
        with db_engine.connect() as conn:
            rows = conn.execute(select(func.count()).select_from(blobs_table)).scalar_one()
            charged = conn.execute(
                select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == str(session_id))
            ).scalar_one()
        assert rows == 1
        assert charged == len(request.content)

    @pytest.mark.asyncio
    async def test_concurrent_retries_converge_on_one_ready_blob(self, db_engine, session_id: UUID, tmp_path: Path) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)

        records = await asyncio.gather(
            *(service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id)) for _ in range(8))
        )

        assert {record.id for record in records} == {records[0].id}
        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_matching_pending_file_is_adopted_and_finalized(self, db_engine, session_id: UUID, tmp_path: Path) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        _, derive_blob_id = _inline_custody_contract()
        blob_id = derive_blob_id(request)
        storage = tmp_path.resolve() / "blobs" / str(session_id) / f"{blob_id}_candidate.csv"
        storage.parent.mkdir(parents=True)
        storage.write_bytes(request.content)
        with db_engine.begin() as conn:
            conn.execute(
                insert(blobs_table).values(
                    id=str(blob_id),
                    session_id=str(session_id),
                    filename=request.filename,
                    mime_type=request.mime_type,
                    size_bytes=len(request.content),
                    content_hash=content_hash(request.content),
                    storage_path=str(storage),
                    created_at=datetime.now(UTC),
                    created_by="assistant",
                    source_description=request.source_description,
                    status="pending",
                    creation_modality=request.creation_modality.value,
                    created_from_message_id=request.created_from_message_id,
                    creating_model_identifier=None,
                    creating_model_version=None,
                    creating_provider=None,
                    creating_composer_skill_hash=None,
                    creating_arguments_hash=None,
                )
            )

        record = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        assert record.status == "ready"
        assert record.id == blob_id

    @pytest.mark.asyncio
    async def test_retry_reconciles_deterministic_and_legacy_stale_temp_files(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        _, derive_blob_id = _inline_custody_contract()
        blob_id = derive_blob_id(request)
        storage = tmp_path.resolve() / "blobs" / str(session_id) / f"{blob_id}_candidate.csv"
        storage.parent.mkdir(parents=True)
        deterministic_temp = storage.with_name(f".{storage.name}.custody.tmp")
        legacy_temp = storage.with_name(f".{storage.name}.orphan.tmp")
        deterministic_temp.write_bytes(b"partial")
        legacy_temp.write_bytes(b"partial")

        record = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        assert record.status == "ready"
        assert storage.read_bytes() == request.content
        assert not deterministic_temp.exists()
        assert not legacy_temp.exists()

    @pytest.mark.asyncio
    async def test_delete_preserves_unqualified_temp_artifacts(self, db_engine, session_id: UUID, tmp_path: Path) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        record = await service.create_blob(
            session_id=session_id,
            filename="candidate.csv",
            content=b"value\n42\n",
            mime_type="text/csv",
            session_operation_context=_operation_context(session_id),
        )
        storage = Path(record.storage_path)
        deterministic_temp = storage.with_name(f".{storage.name}.custody.tmp")
        deterministic_temp.write_bytes(b"partial")

        await service.delete_blob(record.id, session_operation_context=_operation_context(session_id))

        assert not storage.exists()
        assert deterministic_temp.read_bytes() == b"partial"

    @pytest.mark.asyncio
    async def test_mismatched_pending_file_fails_closed(self, db_engine, session_id: UUID, tmp_path: Path) -> None:
        from elspeth.web.blobs.protocol import BlobIntegrityError

        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        _, derive_blob_id = _inline_custody_contract()
        blob_id = derive_blob_id(request)
        storage = tmp_path.resolve() / "blobs" / str(session_id) / f"{blob_id}_candidate.csv"
        storage.parent.mkdir(parents=True)
        storage.write_bytes(b"tampered")
        with db_engine.begin() as conn:
            conn.execute(
                insert(blobs_table).values(
                    id=str(blob_id),
                    session_id=str(session_id),
                    filename=request.filename,
                    mime_type=request.mime_type,
                    size_bytes=len(request.content),
                    content_hash=content_hash(request.content),
                    storage_path=str(storage),
                    created_at=datetime.now(UTC),
                    created_by="assistant",
                    source_description=request.source_description,
                    status="pending",
                    creation_modality=request.creation_modality.value,
                    created_from_message_id=request.created_from_message_id,
                    creating_model_identifier=None,
                    creating_model_version=None,
                    creating_provider=None,
                    creating_composer_skill_hash=None,
                    creating_arguments_hash=None,
                )
            )

        with pytest.raises(BlobIntegrityError):
            await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        assert storage.read_bytes() == b"tampered"

    @pytest.mark.asyncio
    async def test_retry_recovers_orphan_file_after_interruption_before_row_finalization(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        _, derive_blob_id = _inline_custody_contract()
        blob_id = derive_blob_id(request)
        storage = tmp_path.resolve() / "blobs" / str(session_id) / f"{blob_id}_candidate.csv"
        original_write = blob_service_module._atomic_write_blob

        def _write_then_interrupt(
            path: Path,
            content: bytes,
            *,
            write_guard=None,
            temp_identity: str | None = None,
            preserve_on_guard_failure: bool = False,
        ) -> None:
            if type(temp_identity) is not str or not temp_identity:
                raise AssertionError("inline custody write requires an operation-qualified temp identity")
            if preserve_on_guard_failure is not True:
                raise AssertionError("inline custody must preserve bytes for fenced outcome reconciliation")
            original_write(
                path,
                content,
                write_guard=write_guard,
                temp_identity=temp_identity,
                preserve_on_guard_failure=preserve_on_guard_failure,
            )
            raise RuntimeError("simulated interruption after file write")

        monkeypatch.setattr(blob_service_module, "_atomic_write_blob", _write_then_interrupt)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        assert storage.read_bytes() == request.content
        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table)).one()
        assert row.status == "pending"

        monkeypatch.setattr(blob_service_module, "_atomic_write_blob", original_write)
        recovered = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))
        assert recovered.id == blob_id
        assert recovered.status == "ready"
        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_failed_file_write_leaves_durable_pending_reservation_for_retry(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        original_write = blob_service_module._atomic_write_blob

        def _interrupt_before_write(
            _path: Path,
            _content: bytes,
            *,
            write_guard=None,
            temp_identity: str | None = None,
            preserve_on_guard_failure: bool = False,
        ) -> None:
            del write_guard
            if type(temp_identity) is not str or not temp_identity:
                raise AssertionError("inline custody write requires an operation-qualified temp identity")
            if preserve_on_guard_failure is not True:
                raise AssertionError("inline custody must preserve bytes for fenced outcome reconciliation")
            raise RuntimeError("simulated interruption before file write")

        monkeypatch.setattr(blob_service_module, "_atomic_write_blob", _interrupt_before_write)
        with pytest.raises(RuntimeError, match="before file write"):
            await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table)).one()
        assert row.status == "pending"
        assert not Path(row.storage_path).exists()

        monkeypatch.setattr(blob_service_module, "_atomic_write_blob", original_write)
        recovered = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))
        assert recovered.status == "ready"
        assert Path(recovered.storage_path).read_bytes() == request.content
        with db_engine.connect() as conn:
            charged = conn.execute(
                select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == str(session_id))
            ).scalar_one()
        assert charged == len(request.content)

    @pytest.mark.asyncio
    async def test_failed_ready_transition_leaves_pending_row_and_file_for_retry(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        authority = service._session_operation_authority
        original_mutate = authority.mutate

        def _interrupt_before_ready_update(context, mutation):
            if mutation.__name__ == "mark_standalone_blob_ready":
                raise RuntimeError("simulated interruption before ready finalization")
            return original_mutate(context, mutation)

        monkeypatch.setattr(authority, "mutate", _interrupt_before_ready_update)
        with pytest.raises(RuntimeError, match="before ready finalization"):
            await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table)).one()
        assert row.status == "pending"
        assert Path(row.storage_path).read_bytes() == request.content

        monkeypatch.setattr(authority, "mutate", original_mutate)
        recovered = await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))
        assert recovered.status == "ready"
        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_matching_bytes_with_mismatched_reservation_metadata_fail_closed(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
    ) -> None:
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        request = _custody_request(db_engine, session_id)
        _, derive_blob_id = _inline_custody_contract()
        blob_id = derive_blob_id(request)
        storage = tmp_path.resolve() / "blobs" / str(session_id) / f"{blob_id}_candidate.csv"
        storage.parent.mkdir(parents=True)
        storage.write_bytes(request.content)
        with db_engine.begin() as conn:
            conn.execute(
                insert(blobs_table).values(
                    id=str(blob_id),
                    session_id=str(session_id),
                    filename=request.filename,
                    mime_type=request.mime_type,
                    size_bytes=len(request.content),
                    content_hash=content_hash(request.content),
                    storage_path=str(storage),
                    created_at=datetime.now(UTC),
                    created_by="assistant",
                    source_description="different description",
                    status="pending",
                    creation_modality=request.creation_modality.value,
                    created_from_message_id=request.created_from_message_id,
                    creating_model_identifier=None,
                    creating_model_version=None,
                    creating_provider=None,
                    creating_composer_skill_hash=None,
                    creating_arguments_hash=None,
                )
            )

        with pytest.raises(AuditIntegrityError, match="mismatched source_description"):
            await service.reserve_inline_custody(request, session_operation_context=_operation_context(request.session_id))

        assert storage.read_bytes() == request.content

    @pytest.mark.asyncio
    async def test_separate_engines_for_same_sqlite_database_share_custody_lock(self, tmp_path: Path) -> None:
        database_path = tmp_path / "custody.sqlite3"
        database_url = f"sqlite:///{database_path}"
        first_engine = create_session_engine(database_url)
        second_engine = create_session_engine(database_url)
        initialize_session_schema(first_engine)
        first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
        second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
        shared_session_id = first_authority.create_session_with_initial_fence(
            user_id="test-user",
            title="Shared database session",
            auth_provider_type="local",
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        ).id
        session_operation_context = first_authority.acquire(
            session_id=shared_session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="blob-test-owner",
            lease_seconds=30,
        )
        request = _custody_request(first_engine, shared_session_id)
        first_service = BlobServiceImpl(
            first_engine,
            tmp_path / "data",
            max_storage_per_session=100,
            session_operation_authority=first_authority,
        )
        second_service = BlobServiceImpl(
            second_engine,
            tmp_path / "data",
            max_storage_per_session=100,
            session_operation_authority=second_authority,
        )

        try:
            first, second = await asyncio.gather(
                first_service.reserve_inline_custody(request, session_operation_context=session_operation_context),
                second_service.reserve_inline_custody(request, session_operation_context=session_operation_context),
            )
            assert first == second
            with second_engine.connect() as conn:
                assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1
        finally:
            first_engine.dispose()
            second_engine.dispose()

    def test_sqlite_separate_processes_converge_on_one_blob_and_quota_charge(self, tmp_path: Path) -> None:
        database_path = tmp_path / "custody.sqlite3"
        database_url = f"sqlite:///{database_path}"
        engine = create_session_engine(database_url)
        try:
            initialize_session_schema(engine)
            authority = SQLiteLocalSessionOperationAuthority(engine)
            shared_session_id = authority.create_session_with_initial_fence(
                user_id="sqlite-custody-test",
                title="SQLite custody concurrency",
                auth_provider_type="local",
                owner_instance_id="blob-test-owner",
                lease_seconds=30,
            ).id
            session_operation_context = authority.acquire(
                session_id=shared_session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id="blob-test-owner",
                lease_seconds=30,
            )
            request = _custody_request(engine, shared_session_id)
            request_fields = {
                "session_id": str(request.session_id),
                "filename": request.filename,
                "content": request.content,
                "mime_type": request.mime_type,
                "source_description": request.source_description,
                "creation_modality": request.creation_modality.value,
                "created_from_message_id": request.created_from_message_id,
                "creating_model_identifier": request.creating_model_identifier,
                "creating_model_version": request.creating_model_version,
                "creating_provider": request.creating_provider,
                "creating_composer_skill_hash": request.creating_composer_skill_hash,
                "creating_arguments_hash": request.creating_arguments_hash,
            }
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_custody_process,
                    args=(
                        database_url,
                        str(tmp_path / "data"),
                        request_fields,
                        session_operation_context,
                        start_event,
                        result_queue,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=30)
                assert process.exitcode == 0

            results = [result_queue.get(timeout=5) for _ in processes]
            assert all(result[0] == "ok" for result in results), results
            assert results[0][1] == results[1][1]
            with engine.connect() as conn:
                assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1
                charged = conn.execute(
                    select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == str(shared_session_id))
                ).scalar_one()
            assert charged == len(request.content)
        finally:
            engine.dispose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mismatch", [False, True], ids=["matching-winner", "mismatched-winner"])
    async def test_idempotent_retry_reloads_and_validates_winner(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
        mismatch: bool,
    ) -> None:
        request_type, _ = _inline_custody_contract()
        message_id = _seed_custody_message(db_engine, session_id)
        first_request = request_type(
            session_id=session_id,
            filename="candidate.csv",
            content=b"value\n42\n",
            mime_type="text/csv",
            source_description="candidate",
            creation_modality=CreationModality.LLM_GENERATED,
            created_from_message_id=message_id,
            creating_model_identifier="model-a",
            creating_model_version="version-a",
            creating_provider="provider-a",
            creating_composer_skill_hash="a" * 64,
            creating_arguments_hash="b" * 64,
        )
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        winner = await service.reserve_inline_custody(first_request, session_operation_context=_operation_context(first_request.session_id))
        retry = replace(first_request, creating_arguments_hash="c" * 64) if mismatch else first_request

        if mismatch:
            with pytest.raises(AuditIntegrityError, match="mismatched creating_arguments_hash"):
                await service.reserve_inline_custody(retry, session_operation_context=_operation_context(retry.session_id))
        else:
            assert await service.reserve_inline_custody(retry, session_operation_context=_operation_context(retry.session_id)) == winner

    @pytest.mark.skipif(
        not os.environ.get("ELSPETH_TEST_POSTGRES_URL"),
        reason="ELSPETH_TEST_POSTGRES_URL is required for the server-backend custody exercise",
    )
    def test_postgres_separate_processes_converge_on_one_blob_and_quota_charge(self, tmp_path: Path) -> None:
        database_url = os.environ["ELSPETH_TEST_POSTGRES_URL"]
        first_engine = create_session_engine(database_url)
        try:
            initialize_session_schema(first_engine)
            setup_service = BlobServiceImpl(first_engine, tmp_path / "data", max_storage_per_session=100)
            authority = setup_service._session_operation_authority
            shared_session_id = authority.create_session_with_initial_fence(
                user_id="postgres-custody-test",
                title="Postgres custody concurrency",
                auth_provider_type="local",
                owner_instance_id="blob-test-owner",
                lease_seconds=30,
            ).id
            session_operation_context = authority.acquire(
                session_id=shared_session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id="blob-test-owner",
                lease_seconds=30,
            )
            request = _custody_request(first_engine, shared_session_id)
            request_fields = {
                "session_id": str(request.session_id),
                "filename": request.filename,
                "content": request.content,
                "mime_type": request.mime_type,
                "source_description": request.source_description,
                "creation_modality": request.creation_modality.value,
                "created_from_message_id": request.created_from_message_id,
                "creating_model_identifier": request.creating_model_identifier,
                "creating_model_version": request.creating_model_version,
                "creating_provider": request.creating_provider,
                "creating_composer_skill_hash": request.creating_composer_skill_hash,
                "creating_arguments_hash": request.creating_arguments_hash,
            }
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_custody_process,
                    args=(
                        database_url,
                        str(tmp_path / "data"),
                        request_fields,
                        session_operation_context,
                        start_event,
                        result_queue,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=30)
                assert process.exitcode == 0

            results = [result_queue.get(timeout=5) for _ in processes]
            assert all(result[0] == "ok" for result in results), results
            assert results[0][1] == results[1][1]
            with first_engine.connect() as conn:
                assert (
                    conn.execute(
                        select(func.count()).select_from(blobs_table).where(blobs_table.c.session_id == str(shared_session_id))
                    ).scalar_one()
                    == 1
                )
                charged = conn.execute(
                    select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == str(shared_session_id))
                ).scalar_one()
            assert charged == len(request.content)
        finally:
            with first_engine.begin() as conn:
                conn.execute(delete(sessions_table).where(sessions_table.c.id == str(shared_session_id)))
            first_engine.dispose()

    @pytest.mark.asyncio
    async def test_arguments_hash_is_excluded_from_identity_but_mismatched_reuse_fails_closed(
        self,
        db_engine,
        session_id: UUID,
        tmp_path: Path,
    ) -> None:
        request_type, derive_blob_id = _inline_custody_contract()
        message_id = _seed_custody_message(db_engine, session_id)
        first_request = request_type(
            session_id=session_id,
            filename="candidate.csv",
            content=b"value\n42\n",
            mime_type="text/csv",
            source_description="candidate",
            creation_modality=CreationModality.LLM_GENERATED,
            created_from_message_id=message_id,
            creating_model_identifier="model-a",
            creating_model_version="version-a",
            creating_provider="provider-a",
            creating_composer_skill_hash="a" * 64,
            creating_arguments_hash="b" * 64,
        )
        changed_hash = replace(first_request, creating_arguments_hash="c" * 64)
        service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)

        assert derive_blob_id(first_request) == derive_blob_id(changed_hash)
        await service.reserve_inline_custody(first_request, session_operation_context=_operation_context(first_request.session_id))
        with pytest.raises(AuditIntegrityError, match="mismatched creating_arguments_hash"):
            await service.reserve_inline_custody(changed_hash, session_operation_context=_operation_context(changed_hash.session_id))

        with db_engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(blobs_table)).scalar_one() == 1


# ---------------------------------------------------------------------------
# copy_blobs_for_fork — deterministic replay and whole-child cleanup
# ---------------------------------------------------------------------------


class TestCopyBlobsForFork:
    """Fork copies converge on deterministic rows and clean up explicitly."""

    @staticmethod
    async def _checkpoint() -> None:
        return None

    @classmethod
    async def _plan(
        cls,
        service: BlobServiceImpl,
        source_session_id: UUID,
        target_session_id: UUID,
    ) -> tuple[BlobForkPlanEntry, ...]:
        ready = [blob for blob in await service.list_blobs(source_session_id, limit=None) if blob.status == "ready"]
        return tuple(
            BlobForkPlanEntry(
                source_blob_id=blob.id,
                target_blob_id=fork_blob_id(target_session_id=target_session_id, source_blob_id=blob.id),
                source_storage_path=blob.storage_path,
                content_hash=blob.content_hash,
                size_bytes=blob.size_bytes,
            )
            for blob in sorted(ready, key=lambda blob: str(blob.id))
        )

    @classmethod
    async def _copy(cls, service: BlobServiceImpl, source_session_id, target_session_id):
        if type(source_session_id) is UUID and type(target_session_id) is UUID and source_session_id != target_session_id:
            plan = await cls._plan(service, source_session_id, target_session_id)
            write_fence = await cls._authorize_copy(service, source_session_id, target_session_id, plan)
        else:
            plan = ()
            write_fence = object()
        return await service.copy_blobs_for_fork(
            source_session_id,
            target_session_id,
            plan,
            write_fence,  # type: ignore[arg-type]
            checkpoint=cls._checkpoint,
        )

    @staticmethod
    async def _authorize_copy(
        service: BlobServiceImpl,
        source_session_id: UUID,
        target_session_id: UUID,
        plan: tuple[BlobForkPlanEntry, ...],
    ) -> SessionForkAuthority:
        operation_id = f"test-fork-{target_session_id}"
        guided_lease_token = f"test-guided-lease-{target_session_id}"
        parent_operation_id = f"test-parent-operation-{target_session_id}"
        parent_lease_token = f"test-parent-lease-{target_session_id}"
        child_operation_id = f"test-child-operation-{target_session_id}"
        child_lease_token = f"test-child-lease-{target_session_id}"
        now = datetime.now(UTC)
        with service._engine.connect() as conn:
            if conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == str(target_session_id))).one_or_none() is None:
                return TestCopyBlobsForFork._fork_authority(
                    source_session_id=source_session_id,
                    target_session_id=target_session_id,
                    operation_id=operation_id,
                    guided_lease_token=guided_lease_token,
                    parent_operation_id=parent_operation_id,
                    parent_lease_token=parent_lease_token,
                    child_operation_id=child_operation_id,
                    child_lease_token=child_lease_token,
                )
            operation = conn.execute(
                select(guided_operations_table.c.operation_id).where(
                    guided_operations_table.c.session_id == str(source_session_id),
                    guided_operations_table.c.operation_id == operation_id,
                )
            ).one_or_none()
        if operation is None:
            session_service = SessionServiceImpl(
                service._engine,
                telemetry=build_sessions_telemetry(),
                log=structlog.get_logger("test.blob-fork-custody"),
            )
            await session_service.add_message(
                target_session_id,
                "audit",
                json.dumps(
                    {
                        "schema": "session-fork-blob-plan.v1",
                        "source_session_id": str(source_session_id),
                        "child_session_id": str(target_session_id),
                        "operation_id": operation_id,
                        "source_blobs": [
                            {
                                "source_blob_id": str(entry.source_blob_id),
                                "target_blob_id": str(entry.target_blob_id),
                                "content_hash": entry.content_hash,
                                "size_bytes": entry.size_bytes,
                            }
                            for entry in plan
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                writer_principal="session_fork",
            )
        with service._engine.begin() as conn:
            conn.execute(sessions_table.update().where(sessions_table.c.id == str(target_session_id)).values(archived_at=now))
            if operation is None:
                conn.execute(
                    guided_operations_table.insert().values(
                        session_id=str(source_session_id),
                        operation_id=operation_id,
                        kind="session_fork",
                        status="in_progress",
                        request_hash="a" * 64,
                        lease_token=guided_lease_token,
                        lease_expires_at=now + timedelta(hours=1),
                        attempt=1,
                        result_session_id=str(target_session_id),
                        created_at=now,
                        updated_at=now,
                    )
                )
            for (
                session_id,
                session_operation_id,
                session_lease_token,
            ) in (
                (source_session_id, parent_operation_id, parent_lease_token),
                (target_session_id, child_operation_id, child_lease_token),
            ):
                existing = conn.execute(
                    select(session_operation_fences_table.c.session_id).where(
                        session_operation_fences_table.c.session_id == str(session_id)
                    )
                ).one_or_none()
                values = {
                    "operation_id": session_operation_id,
                    "lease_token": session_lease_token,
                    "operation_kind": SessionOperationKind.SESSION_FORK.value,
                    "owner_instance_id": "test-fork-owner",
                    "operation_epoch": 2,
                    "lease_expires_at": now + timedelta(hours=1),
                    "released_at": None,
                }
                if existing is None:
                    conn.execute(
                        session_operation_fences_table.insert().values(
                            session_id=str(session_id),
                            **values,
                        )
                    )
                else:
                    conn.execute(
                        session_operation_fences_table.update()
                        .where(session_operation_fences_table.c.session_id == str(session_id))
                        .values(**values)
                    )
        return TestCopyBlobsForFork._fork_authority(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            operation_id=operation_id,
            guided_lease_token=guided_lease_token,
            parent_operation_id=parent_operation_id,
            parent_lease_token=parent_lease_token,
            child_operation_id=child_operation_id,
            child_lease_token=child_lease_token,
        )

    @staticmethod
    def _fork_authority(
        *,
        source_session_id: UUID,
        target_session_id: UUID,
        operation_id: str,
        guided_lease_token: str,
        parent_operation_id: str,
        parent_lease_token: str,
        child_operation_id: str,
        child_lease_token: str,
        attempt: int = 1,
    ) -> SessionForkAuthority:
        parent_context = SessionOperationContext(
            fence=SessionOperationFence(
                session_id=str(source_session_id),
                operation_id=parent_operation_id,
                lease_token=parent_lease_token,
                operation_epoch=2,
            ),
            operation_kind=SessionOperationKind.SESSION_FORK,
        )
        return SessionForkAuthority(
            parent=SessionForkParentAuthority(
                parent_context=parent_context,
                guided_fence=GuidedOperationFence(
                    session_id=source_session_id,
                    operation_id=operation_id,
                    lease_token=guided_lease_token,
                    attempt=attempt,
                ),
            ),
            child_context=SessionOperationContext(
                fence=SessionOperationFence(
                    session_id=str(target_session_id),
                    operation_id=child_operation_id,
                    lease_token=child_lease_token,
                    operation_epoch=2,
                ),
                operation_kind=SessionOperationKind.SESSION_FORK,
            ),
        )

    @staticmethod
    def _fail_fork(service: BlobServiceImpl, source_session_id: UUID, target_session_id: UUID) -> SessionForkAuthority:
        operation_id = f"test-fork-{target_session_id}"
        now = datetime.now(UTC)
        with service._engine.begin() as conn:
            changed = conn.execute(
                guided_operations_table.update()
                .where(
                    guided_operations_table.c.session_id == str(source_session_id),
                    guided_operations_table.c.operation_id == operation_id,
                    guided_operations_table.c.status == "in_progress",
                )
                .values(
                    status="failed",
                    lease_token=None,
                    lease_expires_at=None,
                    result_session_id=None,
                    failure_code="operation_failed",
                    settled_at=now,
                    updated_at=now,
                )
            ).rowcount
        assert changed == 1
        return TestCopyBlobsForFork._fork_authority(
            source_session_id=source_session_id,
            target_session_id=target_session_id,
            operation_id=operation_id,
            guided_lease_token=f"test-guided-lease-{target_session_id}",
            parent_operation_id=f"test-parent-operation-{target_session_id}",
            parent_lease_token=f"test-parent-lease-{target_session_id}",
            child_operation_id=f"test-child-operation-{target_session_id}",
            child_lease_token=f"test-child-lease-{target_session_id}",
        )

    @pytest.fixture()
    def target_session_id(self, db_engine, session_id: UUID) -> UUID:
        """Second session for the fork target."""
        sid = str(uuid4())
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=sid,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Forked Session",
                    created_at=now,
                    updated_at=now,
                    archived_at=now,
                    forked_from_session_id=str(session_id),
                )
            )
        return UUID(sid)

    @staticmethod
    def _insert_session(
        db_engine,
        *,
        user_id: str = "test-user",
        auth_provider_type: str = "local",
        forked_from_session_id: UUID | None = None,
    ) -> UUID:
        session_id = uuid4()
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=str(session_id),
                    user_id=user_id,
                    auth_provider_type=auth_provider_type,
                    title="Test Session",
                    created_at=now,
                    updated_at=now,
                    archived_at=now if forked_from_session_id is not None else None,
                    forked_from_session_id=(str(forked_from_session_id) if forked_from_session_id is not None else None),
                )
            )
        return session_id

    def test_fork_blob_id_has_frozen_uuid5_contract_vector(self) -> None:
        target = UUID("11111111-1111-4111-8111-111111111111")
        source = UUID("22222222-2222-4222-8222-222222222222")

        expected = fork_blob_id(
            target_session_id=target,
            source_blob_id=source,
        )

        assert expected == UUID("7db1b79c-2cad-5fc0-87c8-45652bd6cfd4")
        assert (
            fork_blob_id(
                target_session_id=UUID("33333333-3333-4333-8333-333333333333"),
                source_blob_id=source,
            )
            != expected
        )
        assert (
            fork_blob_id(
                target_session_id=target,
                source_blob_id=UUID("44444444-4444-4444-8444-444444444444"),
            )
            != expected
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_argument", ["source", "target"])
    async def test_copy_requires_exact_uuid_session_ids(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        invalid_argument: str,
    ) -> None:
        source: UUID | str = session_id
        target: UUID | str = target_session_id
        if invalid_argument == "source":
            source = str(source)
        else:
            target = str(target)

        with pytest.raises(TypeError, match=f"{invalid_argument}_session_id must be UUID"):
            await self._copy(blob_service, source, target)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_copy_rejects_source_as_its_own_target(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
    ) -> None:
        with pytest.raises(ValueError, match="source and target sessions must differ"):
            await self._copy(blob_service, session_id, session_id)

    @pytest.mark.asyncio
    async def test_copy_rejects_unrelated_target_before_blob_work(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        unrelated_target = self._insert_session(db_engine)
        source = await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        plan = (
            BlobForkPlanEntry(
                source_blob_id=source.id,
                target_blob_id=fork_blob_id(target_session_id=unrelated_target, source_blob_id=source.id),
                source_storage_path=source.storage_path,
                content_hash=source.content_hash,
                size_bytes=source.size_bytes,
            ),
        )

        async def _unexpected_blob_list(*_args, **_kwargs):
            pytest.fail("fork custody must be verified before listing blobs")

        monkeypatch.setattr(blob_service, "list_blobs", _unexpected_blob_list)

        with pytest.raises(AuditIntegrityError, match="not a fork child"):
            await blob_service.copy_blobs_for_fork(
                session_id,
                unrelated_target,
                plan,
                await self._authorize_copy(blob_service, session_id, unrelated_target, plan),
                checkpoint=self._checkpoint,
            )

        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count()).select_from(blobs_table).where(blobs_table.c.session_id == str(unrelated_target))
                ).scalar_one()
                == 0
            )

    @pytest.mark.asyncio
    async def test_copy_rejects_missing_target_before_blob_work(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
    ) -> None:
        await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )

        with pytest.raises(BlobForkFenceLostError):
            await self._copy(blob_service, session_id, uuid4())

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("user_id", "auth_provider_type"),
        [("different-user", "local"), ("test-user", "oidc")],
    )
    async def test_copy_rejects_cross_principal_fork_child(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        user_id: str,
        auth_provider_type: str,
    ) -> None:
        target = self._insert_session(
            db_engine,
            user_id=user_id,
            auth_provider_type=auth_provider_type,
            forked_from_session_id=session_id,
        )
        await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )

        with pytest.raises(AuditIntegrityError, match="principal does not match"):
            await self._copy(blob_service, session_id, target)

        assert await blob_service.list_blobs(target, limit=None) == []

    @pytest.mark.asyncio
    async def test_repeat_copy_returns_same_deterministic_ids(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        first = await blob_service.create_blob(
            session_id=session_id,
            filename="first.csv",
            content=b"first",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        second = await blob_service.create_blob(
            session_id=session_id,
            filename="second.csv",
            content=b"second",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        first_result = await self._copy(blob_service, session_id, target_session_id)
        second_result = await self._copy(blob_service, session_id, target_session_id)

        assert set(first_result) == {first.id, second.id}
        assert {source_id: record.id for source_id, record in first_result.items()} == {
            source_id: record.id for source_id, record in second_result.items()
        }
        assert all(record.session_id == target_session_id for record in second_result.values())
        assert len(await blob_service.list_blobs(target_session_id, limit=None)) == 2

    @pytest.mark.asyncio
    async def test_copy_runs_checkpoint_before_plan_or_quota_reads(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        plan = await self._plan(blob_service, session_id, target_session_id)

        def _unexpected_custody(*_args, **_kwargs):
            pytest.fail("expired fence must stop before plan/quota reads")

        async def _lost_fence() -> None:
            raise RuntimeError("fence lost")

        monkeypatch.setattr(blob_service_module, "_verify_fork_child_custody", _unexpected_custody)
        with pytest.raises(RuntimeError, match="fence lost"):
            await blob_service.copy_blobs_for_fork(
                session_id,
                target_session_id,
                plan,
                await self._authorize_copy(blob_service, session_id, target_session_id, plan),
                checkpoint=_lost_fence,
            )
        assert source.id not in {blob.id for blob in await blob_service.list_blobs(target_session_id, limit=None)}

    @pytest.mark.asyncio
    async def test_copy_checkpoints_while_source_read_is_blocked(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Slow source reads renew instead of consuming the whole fork lease."""
        source = await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        plan = await self._plan(blob_service, session_id, target_session_id)
        fence = await self._authorize_copy(blob_service, session_id, target_session_id, plan)
        source_path = Path(source.storage_path)
        read_entered = threading.Event()
        release_read = threading.Event()
        checkpoint_seen = threading.Event()
        original_read_bytes = Path.read_bytes

        monkeypatch.setattr(blob_service_module, "_FORK_COPY_LEASE_CHECKPOINT_INTERVAL_SECONDS", 0.01)

        def _blocked_source_read(path: Path) -> bytes:
            if path == source_path and not read_entered.is_set():
                read_entered.set()
                if not release_read.wait(timeout=5):
                    raise TimeoutError("test did not release blocked fork source read")
            return original_read_bytes(path)

        async def _checkpoint() -> None:
            if read_entered.is_set():
                checkpoint_seen.set()

        monkeypatch.setattr(Path, "read_bytes", _blocked_source_read)
        copy_task = asyncio.create_task(
            blob_service.copy_blobs_for_fork(
                session_id,
                target_session_id,
                plan,
                fence,
                checkpoint=_checkpoint,
            )
        )
        assert await asyncio.to_thread(read_entered.wait, 5)
        checkpoint_during_read = await asyncio.to_thread(checkpoint_seen.wait, 1)
        release_read.set()
        copied = await copy_task

        assert checkpoint_during_read, "fork copy did not checkpoint while its source read was blocked"
        assert copied[source.id].status == "ready"

    @pytest.mark.asyncio
    async def test_copy_aborts_blocked_fsync_when_periodic_checkpoint_loses_fence_and_takeover_retries(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A lease loss during blocked fsync leaves no stale canonical bytes."""
        source = await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        plan = await self._plan(blob_service, session_id, target_session_id)
        stale_fence = await self._authorize_copy(blob_service, session_id, target_session_id, plan)
        takeover_token = "takeover-lease"
        fsync_entered = threading.Event()
        release_fsync = threading.Event()
        takeover_started = threading.Event()
        checkpoint_calls = 0
        original_fsync = blob_service_module.os.fsync

        monkeypatch.setattr(
            blob_service_module,
            "_FORK_COPY_LEASE_CHECKPOINT_INTERVAL_SECONDS",
            0.01,
            raising=False,
        )

        def _blocked_first_fsync(descriptor: int) -> None:
            if not fsync_entered.is_set():
                fsync_entered.set()
                if not release_fsync.wait(timeout=5):
                    raise TimeoutError("test did not release blocked fork fsync")
            original_fsync(descriptor)

        async def _checkpoint_then_takeover() -> None:
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            if not fsync_entered.is_set() or takeover_started.is_set():
                return
            now = datetime.now(UTC)
            with db_engine.begin() as conn:
                changed = conn.execute(
                    guided_operations_table.update()
                    .where(guided_operations_table.c.session_id == str(session_id))
                    .where(guided_operations_table.c.operation_id == stale_fence.parent.guided_fence.operation_id)
                    .where(guided_operations_table.c.status == "in_progress")
                    .where(guided_operations_table.c.lease_token == stale_fence.parent.guided_fence.lease_token)
                    .where(guided_operations_table.c.attempt == stale_fence.parent.guided_fence.attempt)
                    .values(
                        lease_token=takeover_token,
                        lease_expires_at=now + timedelta(hours=1),
                        attempt=stale_fence.parent.guided_fence.attempt + 1,
                        updated_at=now,
                    )
                ).rowcount
            assert changed == 1
            takeover_started.set()
            raise BlobForkFenceLostError(
                stale_fence.parent.guided_fence.operation_id,
                attempt=stale_fence.parent.guided_fence.attempt,
            )

        monkeypatch.setattr(blob_service_module.os, "fsync", _blocked_first_fsync)
        stale_copy = asyncio.create_task(
            blob_service.copy_blobs_for_fork(
                session_id,
                target_session_id,
                plan,
                stale_fence,
                checkpoint=_checkpoint_then_takeover,
            )
        )
        assert await asyncio.to_thread(fsync_entered.wait, 5)
        takeover_during_fsync = await asyncio.to_thread(takeover_started.wait, 1)
        release_fsync.set()

        with pytest.raises(BlobForkFenceLostError):
            await stale_copy
        assert takeover_during_fsync, "fork copy did not checkpoint while fsync was blocked"
        assert checkpoint_calls >= 3

        target_storage = tmp_path.resolve() / "blobs" / str(target_session_id) / f"{plan[0].target_blob_id}_{source.filename}"
        assert not target_storage.exists()
        with db_engine.connect() as conn:
            pending = conn.execute(select(blobs_table).where(blobs_table.c.id == str(plan[0].target_blob_id))).one()
        assert pending.status == "pending"

        winner_fence = replace(
            stale_fence,
            parent=replace(
                stale_fence.parent,
                guided_fence=replace(
                    stale_fence.parent.guided_fence,
                    lease_token=takeover_token,
                    attempt=stale_fence.parent.guided_fence.attempt + 1,
                ),
            ),
        )
        copied = await blob_service.copy_blobs_for_fork(
            session_id,
            target_session_id,
            plan,
            winner_fence,
            checkpoint=self._checkpoint,
        )

        assert copied[source.id].status == "ready"
        assert target_storage.read_bytes() == b"source"
        with db_engine.connect() as conn:
            rows = conn.execute(select(blobs_table).where(blobs_table.c.session_id == str(target_session_id))).all()
        assert len(rows) == 1
        assert rows[0].status == "ready"

    @pytest.mark.asyncio
    async def test_copy_uses_frozen_plan_and_ignores_newly_finalized_parent_blob(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        frozen = await blob_service.create_blob(
            session_id, "frozen.csv", b"frozen", "text/csv", session_operation_context=_operation_context(session_id)
        )
        plan = await self._plan(blob_service, session_id, target_session_id)
        late = await blob_service.create_blob(
            session_id, "late.csv", b"late", "text/csv", session_operation_context=_operation_context(session_id)
        )

        copied = await blob_service.copy_blobs_for_fork(
            session_id,
            target_session_id,
            plan,
            await self._authorize_copy(blob_service, session_id, target_session_id, plan),
            checkpoint=self._checkpoint,
        )

        assert set(copied) == {frozen.id}
        assert late.id not in copied

    @pytest.mark.asyncio
    async def test_translates_source_deleted_between_exists_check_and_read(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch,
    ) -> None:
        """A source blob deleted mid-copy keeps the blob lifecycle error contract."""
        from elspeth.web.blobs.protocol import BlobContentMissingError

        source = await blob_service.create_blob(
            session_id,
            "source.csv",
            b"source",
            "text/csv",
            session_operation_context=_operation_context(session_id),
        )
        plan = await self._plan(blob_service, session_id, target_session_id)
        write_fence = await self._authorize_copy(blob_service, session_id, target_session_id, plan)

        target = Path(source.storage_path)
        real_open = Path.open
        deleted = False

        def delete_then_open(self: Path, *args: object, **kwargs: object) -> object:
            nonlocal deleted
            if self == target and not deleted:
                deleted = True
                self.unlink()
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", delete_then_open)

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.copy_blobs_for_fork(
                session_id,
                target_session_id,
                plan,
                write_fence,
                checkpoint=self._checkpoint,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fork_status", ["in_progress", "completed", "failed"])
    async def test_delete_enforces_session_fork_retention_lifecycle(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        fork_status: str,
    ) -> None:
        source = await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        now = datetime.now(UTC)
        operation_id = str(uuid4())
        values: dict[str, object] = {
            "session_id": str(session_id),
            "operation_id": operation_id,
            "kind": "session_fork",
            "status": fork_status,
            "request_hash": "a" * 64,
            "attempt": 1,
            "created_at": now,
            "updated_at": now,
        }
        if fork_status == "in_progress":
            values.update(lease_token="lease", lease_expires_at=now + timedelta(hours=1))
        elif fork_status == "completed":
            values.update(
                settled_at=now,
                result_kind="session",
                result_session_id=str(target_session_id),
                response_hash="b" * 64,
            )
        else:
            values.update(settled_at=now, failure_code="operation_failed")
        with db_engine.begin() as conn:
            conn.execute(guided_operations_table.insert().values(**values))

        if fork_status == "in_progress":
            with pytest.raises(BlobInProgressForkError, match=operation_id):
                await blob_service.delete_blob(source.id, session_operation_context=_operation_context(session_id))

            assert await blob_service.read_blob_content(source.id, session_operation_context=_operation_context(session_id)) == b"source"
        else:
            await blob_service.delete_blob(source.id, session_operation_context=_operation_context(session_id))
            with pytest.raises(BlobNotFoundError):
                await blob_service.read_blob_content(source.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_partial_prior_success_resumes_without_duplicate(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        first = await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        first_plan = await self._plan(blob_service, session_id, target_session_id)
        second = await blob_service.create_blob(
            session_id, "second.csv", b"second", "text/csv", session_operation_context=_operation_context(session_id)
        )
        first_pass = await blob_service.copy_blobs_for_fork(
            session_id,
            target_session_id,
            first_plan,
            await self._authorize_copy(blob_service, session_id, target_session_id, first_plan),
            checkpoint=self._checkpoint,
        )

        resumed = await self._copy(blob_service, session_id, target_session_id)

        assert resumed[first.id].id == first_pass[first.id].id
        assert set(resumed) == {first.id, second.id}
        assert len(await blob_service.list_blobs(target_session_id, limit=None)) == 2

    @pytest.mark.asyncio
    async def test_quota_preflight_charges_only_missing_expected_copies(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path: Path,
    ) -> None:
        first = await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        quota_service = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=11)
        first_plan = await self._plan(quota_service, session_id, target_session_id)
        second = await blob_service.create_blob(
            session_id, "second.csv", b"second", "text/csv", session_operation_context=_operation_context(session_id)
        )
        first_pass = await quota_service.copy_blobs_for_fork(
            session_id,
            target_session_id,
            first_plan,
            await self._authorize_copy(quota_service, session_id, target_session_id, first_plan),
            checkpoint=self._checkpoint,
        )

        resumed = await self._copy(quota_service, session_id, target_session_id)

        assert resumed[first.id].id == first_pass[first.id].id
        assert second.id in resumed
        assert sum(blob.size_bytes for blob in await quota_service.list_blobs(target_session_id, limit=None)) == 11

    @pytest.mark.asyncio
    async def test_zero_write_replay_succeeds_after_quota_is_lowered_below_current_usage(
        self,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path: Path,
    ) -> None:
        high_quota = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=100)
        source = await high_quota.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        first = await self._copy(high_quota, session_id, target_session_id)
        low_quota = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=1)

        replay = await self._copy(low_quota, session_id, target_session_id)

        assert replay[source.id].id == first[source.id].id
        assert [blob.id for blob in await low_quota.list_blobs(target_session_id, limit=None)] == [first[source.id].id]

    @pytest.mark.asyncio
    async def test_copy_supports_more_than_fifty_ready_blobs(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        for index in range(51):
            await blob_service.create_blob(
                session_id, f"item-{index}.csv", str(index).encode(), "text/csv", session_operation_context=_operation_context(session_id)
            )

        result = await self._copy(blob_service, session_id, target_session_id)

        assert len(result) == 51
        assert len(await blob_service.list_blobs(target_session_id, limit=None)) == 51

    @pytest.mark.asyncio
    async def test_copy_ignores_non_ready_source_blobs(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        ready = await blob_service.create_blob(
            session_id, "ready.csv", b"ready", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await blob_service.create_pending_blob(
            session_id,
            "pending.csv",
            "text/csv",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        result = await self._copy(blob_service, session_id, target_session_id)

        assert set(result) == {ready.id}

    @pytest.mark.asyncio
    async def test_copy_preserves_source_content_integrity_gate(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        source = await blob_service.create_blob(
            session_id, "ready.csv", b"ready", "text/csv", session_operation_context=_operation_context(session_id)
        )
        Path(source.storage_path).write_bytes(b"tampered")

        with pytest.raises(BlobIntegrityError):
            await self._copy(blob_service, session_id, target_session_id)

        assert await blob_service.list_blobs(target_session_id, limit=None) == []

    @pytest.mark.asyncio
    async def test_empty_source_returns_empty_map(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        """No blobs in source session → empty mapping, no errors."""
        result = await self._copy(blob_service, session_id, target_session_id)
        assert result == {}

    @pytest.mark.asyncio
    async def test_quota_exceeded_before_any_copy(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path,
    ) -> None:
        """Quota check happens before copying — no partial writes."""
        await blob_service.create_blob(
            session_id=session_id,
            filename="big.csv",
            content=b"x" * 100,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        small_quota = BlobServiceImpl(db_engine, tmp_path, max_storage_per_session=10)

        with pytest.raises(BlobQuotaExceededError):
            await self._copy(small_quota, session_id, target_session_id)

        target_blobs = await blob_service.list_blobs(target_session_id)
        assert target_blobs == []

    @pytest.mark.asyncio
    async def test_cleanup_is_idempotent_and_typed(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        source = await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        copied = await self._copy(blob_service, session_id, target_session_id)
        authority = self._fail_fork(blob_service, session_id, target_session_id)

        first = await blob_service.cleanup_blobs_for_fork(authority)
        second = await blob_service.cleanup_blobs_for_fork(authority)

        assert type(first) is BlobForkCleanupResult
        assert type(first.deleted_ids) is tuple
        assert type(first.errors) is tuple
        assert tuple(first.deleted_ids) == (copied[source.id].id,)
        assert tuple(first.errors) == ()
        assert tuple(second.deleted_ids) == ()
        assert tuple(second.errors) == ()
        assert await blob_service.list_blobs(target_session_id, limit=None) == []

    @pytest.mark.asyncio
    async def test_cleanup_retries_committed_tombstone_after_unlink_failure(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        copied = await self._copy(blob_service, session_id, target_session_id)
        target = copied[source.id]
        storage = Path(target.storage_path)
        authority = self._fail_fork(blob_service, session_id, target_session_id)
        original_unlink = Path.unlink
        fail_tombstone_unlink = True
        cleanup_secret = "tombstone-token=super-secret-credential"  # secret-scan: allow-this-line

        def fail_first_tombstone_unlink(path: Path, missing_ok: bool = False) -> None:
            nonlocal fail_tombstone_unlink
            if fail_tombstone_unlink and ".delete-" in path.name:
                fail_tombstone_unlink = False
                raise PermissionError(f"injected fork tombstone unlink failure ({cleanup_secret})")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_first_tombstone_unlink)

        first = await blob_service.cleanup_blobs_for_fork(authority)

        assert tuple(first.deleted_ids) == ()
        assert len(first.errors) == 1
        assert first.errors[0].blob_id == target.id
        assert first.errors[0].exc_type == "PermissionError"
        assert first.errors[0].detail == "RecoveryFailed[PermissionError]"
        assert cleanup_secret not in repr(first.errors[0])
        assert len(list(storage.parent.glob(f".{storage.name}.delete-*"))) == 1

        restarted = BlobServiceImpl(db_engine, tmp_path)
        second = await restarted.cleanup_blobs_for_fork(authority)

        assert tuple(second.deleted_ids) == (target.id,)
        assert tuple(second.errors) == ()
        assert list(storage.parent.glob(f".{storage.name}.delete-*")) == []

    @pytest.mark.asyncio
    async def test_cleanup_restores_bytes_and_aborts_intent_after_definite_stage_failure(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = await blob_service.create_blob(
            session_id,
            "first.csv",
            b"first",
            "text/csv",
            session_operation_context=_operation_context(session_id),
        )
        copied = await self._copy(blob_service, session_id, target_session_id)
        target = copied[source.id]
        storage = Path(target.storage_path)
        authority = self._fail_fork(blob_service, session_id, target_session_id)
        original_fsync = blob_service_module._fsync_parent_directory
        fail_stage_fsync = True

        def fail_first_stage_fsync(directory: Path) -> None:
            nonlocal fail_stage_fsync
            if fail_stage_fsync and tuple(directory.glob(f".{storage.name}.delete-*")):
                fail_stage_fsync = False
                raise PermissionError("injected definite stage fsync failure")
            original_fsync(directory)

        monkeypatch.setattr(blob_service_module, "_fsync_parent_directory", fail_first_stage_fsync)

        result = await blob_service.cleanup_blobs_for_fork(authority)

        assert tuple(result.deleted_ids) == ()
        assert len(result.errors) == 1
        assert result.errors[0].exc_type == "PermissionError"
        assert storage.read_bytes() == b"first"
        assert tuple(storage.parent.glob(f".{storage.name}.delete-*")) == ()
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(target.id))
                ).scalar_one()
                == 0
            )
            assert conn.execute(select(func.count()).select_from(blobs_table).where(blobs_table.c.id == str(target.id))).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_cleanup_restores_bytes_and_aborts_intent_after_stage_mutation_failure(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source = await blob_service.create_blob(
            session_id,
            "first.csv",
            b"first",
            "text/csv",
            session_operation_context=_operation_context(session_id),
        )
        copied = await self._copy(blob_service, session_id, target_session_id)
        target = copied[source.id]
        storage = Path(target.storage_path)
        authority = self._fail_fork(blob_service, session_id, target_session_id)

        def fail_mark_staged(*, authority: SessionForkAuthority, plan):
            del authority, plan
            raise OSError("injected fork stage mutation failure")

        monkeypatch.setattr(blob_service, "_mark_fork_deletion_staged", fail_mark_staged)

        result = await blob_service.cleanup_blobs_for_fork(authority)

        assert tuple(result.deleted_ids) == ()
        assert len(result.errors) == 1
        assert result.errors[0].exc_type == "OSError"
        assert storage.read_bytes() == b"first"
        assert tuple(storage.parent.glob(f".{storage.name}.delete-*")) == ()
        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(target.id))
                ).scalar_one()
                == 0
            )
            assert conn.execute(select(func.count()).select_from(blobs_table).where(blobs_table.c.id == str(target.id))).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_cleanup_rejects_wrong_parent_and_preserves_child_blobs(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        wrong_parent = self._insert_session(db_engine)
        source = await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        copied = await self._copy(blob_service, session_id, target_session_id)
        authority = self._fail_fork(blob_service, session_id, target_session_id)

        with pytest.raises(AuditIntegrityError, match="not a fork child"):
            await blob_service.cleanup_blobs_for_fork(
                replace(
                    authority,
                    parent=replace(
                        authority.parent,
                        parent_context=replace(
                            authority.parent.parent_context,
                            fence=replace(
                                authority.parent.parent_context.fence,
                                session_id=str(wrong_parent),
                            ),
                        ),
                        guided_fence=replace(
                            authority.parent.guided_fence,
                            session_id=wrong_parent,
                        ),
                    ),
                )
            )

        assert [blob.id for blob in await blob_service.list_blobs(target_session_id, limit=None)] == [copied[source.id].id]

    @pytest.mark.asyncio
    async def test_cleanup_rejects_active_completed_child_and_preserves_blobs(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
    ) -> None:
        await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await self._copy(blob_service, session_id, target_session_id)
        authority = self._fail_fork(blob_service, session_id, target_session_id)
        before = await blob_service.list_blobs(target_session_id, limit=None)
        with db_engine.begin() as conn:
            conn.execute(sessions_table.update().where(sessions_table.c.id == str(target_session_id)).values(archived_at=None))

        with pytest.raises(AuditIntegrityError, match="not an archived staged fork child"):
            await blob_service.cleanup_blobs_for_fork(authority)

        assert await blob_service.list_blobs(target_session_id, limit=None) == before

    @pytest.mark.asyncio
    async def test_cleanup_treats_already_missing_snapshot_row_as_success(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await self._copy(blob_service, session_id, target_session_id)
        stale_snapshot = await blob_service.list_blobs(target_session_id, limit=None)
        authority = self._fail_fork(blob_service, session_id, target_session_id)
        with blob_service._engine.begin() as conn:
            conn.execute(delete(blobs_table).where(blobs_table.c.id == str(stale_snapshot[0].id)))
        Path(stale_snapshot[0].storage_path).unlink()
        result = await blob_service.cleanup_blobs_for_fork(authority)

        assert tuple(result.deleted_ids) == ()
        assert tuple(result.errors) == ()

    @pytest.mark.asyncio
    async def test_cleanup_continues_after_item_failure_and_records_residual_metric(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await blob_service.create_blob(
            session_id, "second.csv", b"second", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await self._copy(blob_service, session_id, target_session_id)
        authority = self._fail_fork(blob_service, session_id, target_session_id)
        target = await blob_service.list_blobs(target_session_id, limit=None)
        failing_id = target[0].id
        cleanup_secret = "blob-row-token=super-secret-credential"  # secret-scan: allow-this-line

        orphan_counter = _FakeCounter()
        monkeypatch.setattr(blob_service_module, "_BLOB_COPY_FORK_ORPHAN_ROWS_COUNTER", orphan_counter)
        original_delete = blob_service._delete_fork_blob_with_ledger

        def _fail_one(
            *,
            authority: SessionForkAuthority,
            blob_id: UUID,
        ) -> bool:
            if blob_id == failing_id:
                raise PermissionError(13, f"cleanup failed ({cleanup_secret})")
            return original_delete(
                authority=authority,
                blob_id=blob_id,
            )

        monkeypatch.setattr(blob_service, "_delete_fork_blob_with_ledger", _fail_one)
        result = await blob_service.cleanup_blobs_for_fork(authority)

        assert type(result) is BlobForkCleanupResult
        assert set(result.deleted_ids) == {record.id for record in target if record.id != failing_id}
        assert len(result.errors) == 1
        error = result.errors[0]
        assert type(error) is BlobForkCleanupError
        assert error.blob_id == failing_id
        assert error.exc_type == "PermissionError"
        assert error.detail == "RecoveryFailed[PermissionError]"
        assert cleanup_secret not in repr(error)
        assert [blob.id for blob in await blob_service.list_blobs(target_session_id, limit=None)] == [failing_id]
        assert len(orphan_counter.calls) == 1
        amount, attrs, context = orphan_counter.calls[0]
        assert amount == 1
        assert attrs == {
            "orphan_blob_id": str(failing_id),
            "target_session_id": str(target_session_id),
            "exc_type": "PermissionError",
        }
        assert context is None

    @pytest.mark.asyncio
    async def test_mid_copy_failure_retains_partial_rows_for_exact_plan_takeover(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await blob_service.create_blob(
            session_id, "second.csv", b"second", "text/csv", session_operation_context=_operation_context(session_id)
        )
        original_persist = blob_service_module._persist_blob_content
        target_copy_calls = 0

        def _fail_second_target_copy(**kwargs):
            nonlocal target_copy_calls
            if (
                str(kwargs["session_id"]) == str(target_session_id)
                and kwargs["idempotent"] is True
                and kwargs.get("_filesystem_lock_held") is True
            ):
                target_copy_calls += 1
                if target_copy_calls == 2:
                    raise RuntimeError("mid-copy failure")
            return original_persist(**kwargs)

        monkeypatch.setattr(blob_service_module, "_persist_blob_content", _fail_second_target_copy)

        with pytest.raises(RuntimeError, match="mid-copy failure"):
            await self._copy(blob_service, session_id, target_session_id)

        assert len(await blob_service.list_blobs(target_session_id, limit=None)) == 1
        target_dir = tmp_path.resolve() / "blobs" / str(target_session_id)
        assert len(list(target_dir.iterdir())) == 1

    @pytest.mark.asyncio
    async def test_copy_failure_does_not_invoke_automatic_cleanup(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await blob_service.create_blob(
            session_id, "first.csv", b"first", "text/csv", session_operation_context=_operation_context(session_id)
        )
        await blob_service.create_blob(
            session_id, "second.csv", b"second", "text/csv", session_operation_context=_operation_context(session_id)
        )
        original_persist = blob_service_module._persist_blob_content
        target_copy_calls = 0

        def _fail_second_target_copy(**kwargs):
            nonlocal target_copy_calls
            if (
                str(kwargs["session_id"]) == str(target_session_id)
                and kwargs["idempotent"] is True
                and kwargs.get("_filesystem_lock_held") is True
            ):
                target_copy_calls += 1
                if target_copy_calls == 2:
                    raise RuntimeError("primary copy failure")
            return original_persist(**kwargs)

        async def _unexpected_cleanup(*_args, **_kwargs) -> None:
            pytest.fail("copy service must leave cleanup ownership to the fail-CAS winner")

        monkeypatch.setattr(blob_service_module, "_persist_blob_content", _fail_second_target_copy)
        monkeypatch.setattr(blob_service, "cleanup_blobs_for_fork", _unexpected_cleanup)

        with pytest.raises(RuntimeError, match="primary copy failure") as exc_info:
            await self._copy(blob_service, session_id, target_session_id)

        assert type(exc_info.value) is RuntimeError
        assert getattr(exc_info.value, "__notes__", []) == []
        assert len(await blob_service.list_blobs(target_session_id, limit=None)) == 1

    @pytest.mark.asyncio
    async def test_failed_fork_cleanup_waits_for_guarded_copy_file_section_and_converges(
        self,
        blob_service: BlobServiceImpl,
        db_engine,
        session_id: UUID,
        target_session_id: UUID,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cleanup cannot interleave with a copy paused inside its file lock."""
        source = await blob_service.create_blob(
            session_id,
            "guarded-source.csv",
            b"guarded source bytes",
            "text/csv",
            session_operation_context=_operation_context(session_id),
        )
        plan = await self._plan(blob_service, session_id, target_session_id)
        stale_authority = await self._authorize_copy(
            blob_service,
            session_id,
            target_session_id,
            plan,
        )
        target_blob_id = plan[0].target_blob_id
        target_storage = tmp_path.resolve() / "blobs" / str(target_session_id) / f"{target_blob_id}_{source.filename}"

        @contextlib.contextmanager
        def no_process_lock(_engine, _session_id: str):
            yield

        monkeypatch.setattr(blob_service_module, "process_session_lock", no_process_lock)

        original_file_lock = blob_service_module.filesystem_session_lock
        cleanup_requested = threading.Event()
        cleanup_lock_attempted = threading.Event()
        cleanup_lock_acquired = threading.Event()

        @contextlib.contextmanager
        def observe_file_lock(root: Path, locked_session_id: str):
            is_cleanup = cleanup_requested.is_set() and locked_session_id == str(target_session_id)
            if is_cleanup:
                cleanup_lock_attempted.set()
            with original_file_lock(root, locked_session_id):
                if is_cleanup:
                    cleanup_lock_acquired.set()
                yield

        monkeypatch.setattr(blob_service_module, "filesystem_session_lock", observe_file_lock)

        original_require = blob_service_module._ForkCopyWriteAuthority.require
        stale_at_first_write_guard = threading.Event()
        release_stale = threading.Event()
        paused = False

        def pause_after_first_successful_write_guard(authority) -> None:
            nonlocal paused
            original_require(authority)
            if not paused:
                paused = True
                stale_at_first_write_guard.set()
                if not release_stale.wait(timeout=5):
                    raise TimeoutError("test did not release guarded fork copy")

        monkeypatch.setattr(
            blob_service_module._ForkCopyWriteAuthority,
            "require",
            pause_after_first_successful_write_guard,
        )

        stale_copy = asyncio.create_task(
            blob_service.copy_blobs_for_fork(
                session_id,
                target_session_id,
                plan,
                stale_authority,
                checkpoint=self._checkpoint,
            )
        )
        assert await asyncio.to_thread(stale_at_first_write_guard.wait, 5)

        cleanup_authority = self._fail_fork(blob_service, session_id, target_session_id)
        cleanup_requested.set()
        cleanup_started = asyncio.Event()

        async def run_cleanup() -> BlobForkCleanupResult:
            cleanup_started.set()
            return await blob_service.cleanup_blobs_for_fork(cleanup_authority)

        cleanup_task = asyncio.create_task(run_cleanup())
        await cleanup_started.wait()
        lock_attempted_before_release = await asyncio.to_thread(cleanup_lock_attempted.wait, 1)
        lock_acquired_before_release = cleanup_lock_acquired.is_set()

        release_stale.set()
        copy_outcome = await asyncio.gather(stale_copy, return_exceptions=True)
        cleanup = await cleanup_task

        assert lock_attempted_before_release is True
        assert lock_acquired_before_release is False
        assert cleanup_lock_acquired.is_set()
        assert len(copy_outcome) == 1
        assert isinstance(copy_outcome[0], BlobForkFenceLostError)
        assert tuple(cleanup.deleted_ids) == (target_blob_id,)
        assert tuple(cleanup.errors) == ()
        assert not target_storage.exists()
        assert tuple(target_storage.parent.glob(f".{target_storage.name}*.tmp")) == ()
        assert tuple(target_storage.parent.glob(f".{target_storage.name}.delete-*")) == ()
        with db_engine.connect() as conn:
            assert (
                conn.execute(select(func.count()).select_from(blobs_table).where(blobs_table.c.id == str(target_blob_id))).scalar_one() == 0
            )
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_deletion_cleanups_table)
                    .where(blob_deletion_cleanups_table.c.blob_id == str(target_blob_id))
                ).scalar_one()
                == 0
            )

    @pytest.mark.asyncio
    async def test_fail_cleanup_wins_pause_before_persist_and_stale_writer_leaves_no_blob(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        target_session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await blob_service.create_blob(
            session_id, "source.csv", b"source", "text/csv", session_operation_context=_operation_context(session_id)
        )
        reached_persist = threading.Event()
        release_persist = threading.Event()
        original_persist = blob_service_module._persist_blob_content

        def _pause_before_persist(**kwargs):
            reached_persist.set()
            if not release_persist.wait(timeout=5):
                raise TimeoutError("test did not release paused fork writer")
            return original_persist(**kwargs)

        monkeypatch.setattr(blob_service_module, "_persist_blob_content", _pause_before_persist)
        copy_task = asyncio.create_task(self._copy(blob_service, session_id, target_session_id))
        assert await asyncio.to_thread(reached_persist.wait, 5)
        authority = self._fail_fork(blob_service, session_id, target_session_id)
        cleanup = await blob_service.cleanup_blobs_for_fork(authority)
        assert cleanup.errors == ()
        release_persist.set()

        with pytest.raises(BlobForkFenceLostError):
            await copy_task
        assert await blob_service.list_blobs(target_session_id, limit=None) == []


# ---------------------------------------------------------------------------
# finalize_run_output_blobs — run-level batch finalization
# ---------------------------------------------------------------------------


class TestFinalizeRunOutputBlobs:
    """Batch finalization of pending output blobs when a run completes or fails."""

    @pytest.fixture()
    def run_env(self, blob_service, session_id, db_engine):
        """Set up a composition state and run, return (run_id, session_id_str)."""
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="running",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )
        return UUID(run_id), session_id_str

    @pytest.mark.asyncio
    async def test_success_path_sets_ready_with_size_and_hash(self, blob_service, session_id, db_engine, run_env) -> None:
        """Pending blob with file written -> ready with size_bytes and content_hash."""
        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        assert pending.status == "pending"

        # Write content to the storage path (simulating sink output)
        from pathlib import Path as _Path

        file_content = b"col1,col2\na,b\nc,d"
        _Path(pending.storage_path).write_bytes(file_content)

        # Link blob to run as output
        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )

        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        assert len(result.finalized) == 1
        assert len(result.errors) == 0
        assert result.finalized[0].status == "ready"
        assert result.finalized[0].size_bytes == len(file_content)
        assert result.finalized[0].content_hash == content_hash(file_content)

    @pytest.mark.asyncio
    async def test_stale_execute_context_cannot_finalize_output_or_change_bytes(
        self,
        blob_service,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A superseded execution cannot finalize output metadata or bytes."""
        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="stale-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        storage = Path(pending.storage_path)
        storage.write_bytes(b"winner-owned output")
        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )

        stale = _operation_context(session_id, SessionOperationKind.EXECUTE)
        session_operation_authority.release(stale)
        winner = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="blob-test-winner",
            lease_seconds=30,
        )
        try:
            with pytest.raises(SessionOperationFenceLost):
                await blob_service.finalize_run_output_blobs(
                    run_id,
                    success=True,
                    session_operation_context=stale,
                )
        finally:
            session_operation_authority.release(winner)

        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(pending.id))).one()
        assert row.status == "pending"
        assert row.size_bytes == 0
        assert row.content_hash is None
        assert storage.read_bytes() == b"winner-owned output"

    @pytest.mark.asyncio
    async def test_file_not_written_sets_error(self, blob_service, session_id, db_engine, run_env) -> None:
        """Pending blob without file on disk -> error status on success=True."""
        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="missing.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        # Do NOT write any file — simulate sink that didn't produce output

        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )

        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        assert len(result.finalized) == 1
        assert len(result.errors) == 0
        assert result.finalized[0].status == "error"

    @pytest.mark.asyncio
    async def test_run_failed_sets_error(self, blob_service, session_id, db_engine, run_env) -> None:
        """Pending blob with success=False -> error regardless of file state."""
        from pathlib import Path as _Path

        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        # Write file — but the run failed, so it should still be marked error
        _Path(pending.storage_path).write_bytes(b"partial-output")

        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )

        result = await blob_service.finalize_run_output_blobs(
            run_id, success=False, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        assert len(result.finalized) == 1
        assert len(result.errors) == 0
        assert result.finalized[0].status == "error"

    @pytest.mark.asyncio
    async def test_public_finalize_recovers_crash_left_output_tombstone_from_old_operation(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
    ) -> None:
        """Public terminal finalization restores the sole crash-left output tombstone."""
        successor = _operation_context(session_id, SessionOperationKind.EXECUTE)
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="crash-left-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=successor,
        )
        storage = Path(pending.storage_path)
        expected = b"crash-left pending output"
        storage.write_bytes(expected)
        stale_token = "0" * 64
        assert stale_token != blob_service_module._blob_operation_path_token(
            operation_id=successor.fence.operation_id,
            operation_epoch=successor.fence.operation_epoch,
            operation_kind=successor.operation_kind,
        )
        tombstone = storage.with_name(f".{storage.name}.output-delete-{stale_token}")
        os.replace(storage, tombstone)

        finalized = await blob_service.finalize_blob(
            pending.id,
            "ready",
            size_bytes=len(expected),
            content_hash=content_hash(expected),
            session_operation_context=successor,
        )

        assert finalized.status == "ready"
        assert storage.read_bytes() == expected
        assert not tombstone.exists()
        assert (
            await blob_service.read_blob_content(
                pending.id,
                session_operation_context=successor,
            )
            == expected
        )

    @pytest.mark.asyncio
    async def test_public_error_finalize_purges_crash_left_output_tombstone_from_old_operation(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
    ) -> None:
        """Public error finalization adopts and purges predecessor-staged bytes."""
        successor = _operation_context(session_id, SessionOperationKind.EXECUTE)
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="crash-left-error-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=successor,
        )
        storage = Path(pending.storage_path)
        storage.write_bytes(b"crash-left failed output")
        stale_token = "0" * 64
        tombstone = storage.with_name(f".{storage.name}.output-delete-{stale_token}")
        os.replace(storage, tombstone)

        finalized = await blob_service.finalize_blob(
            pending.id,
            "error",
            session_operation_context=successor,
        )

        assert finalized.status == "error"
        assert finalized.size_bytes == 0
        assert finalized.content_hash is None
        assert not storage.exists()
        assert not tombstone.exists()

    @pytest.mark.asyncio
    async def test_public_error_finalize_removes_canonical_output_bytes(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
    ) -> None:
        """Public error finalization cannot leave unaccounted canonical bytes."""
        context = _operation_context(session_id, SessionOperationKind.EXECUTE)
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="canonical-error-output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=context,
        )
        storage = Path(pending.storage_path)
        storage.write_bytes(b"failed output")

        finalized = await blob_service.finalize_blob(
            pending.id,
            "error",
            session_operation_context=context,
        )

        assert finalized.status == "error"
        assert not storage.exists()
        assert list(storage.parent.glob(f".{storage.name}.output-delete-*")) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recovery_surface", ["retry", "content_read"])
    async def test_public_error_post_commit_purge_failure_is_recovered(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
        monkeypatch: pytest.MonkeyPatch,
        recovery_surface: str,
    ) -> None:
        """A committed error tombstone is purged by either public recovery surface."""
        context = _operation_context(session_id, SessionOperationKind.EXECUTE)
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename=f"purge-failure-{recovery_surface}.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=context,
        )
        storage = Path(pending.storage_path)
        storage.write_bytes(b"failed output awaiting purge")
        original_unlink = Path.unlink
        failed_target_purge = False

        def fail_target_purge_once(path: Path, missing_ok: bool = False) -> None:
            nonlocal failed_target_purge
            if ".output-delete-" in path.name and not failed_target_purge:
                failed_target_purge = True
                raise OSError("injected output tombstone purge failure")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_target_purge_once)
        with pytest.raises(OSError, match="injected output tombstone purge failure"):
            await blob_service.finalize_blob(
                pending.id,
                "error",
                session_operation_context=context,
            )

        tombstones = list(storage.parent.glob(f".{storage.name}.output-delete-*"))
        assert failed_target_purge is True
        assert not storage.exists()
        assert len(tombstones) == 1

        if recovery_surface == "retry":
            with pytest.raises(BlobStateError, match="expected 'pending'"):
                await blob_service.finalize_blob(
                    pending.id,
                    "error",
                    session_operation_context=context,
                )
        else:
            with pytest.raises(BlobStateError, match="expected 'ready'"):
                await blob_service.read_blob_content(
                    pending.id,
                    session_operation_context=context,
                )

        assert not tombstones[0].exists()

    @pytest.mark.asyncio
    async def test_stale_failed_run_finalization_cannot_unlink_successor_owned_output(
        self,
        db_engine,
        tmp_path: Path,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lease loss after a pre-file guard preserves pending output exactly."""
        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env
        stale = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="stale-finalize-owner",
            lease_seconds=30,
        )
        service = BlobServiceImpl(
            db_engine,
            tmp_path,
            session_operation_authority=session_operation_authority,
        )
        pending = await service.create_pending_blob(
            session_id=session_id,
            filename="stale-failed-run.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=stale,
        )
        storage = Path(pending.storage_path)
        original_bytes = b"successor-owned pending output"
        storage.write_bytes(original_bytes)
        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )
            before = dict(conn.execute(select(blobs_table).where(blobs_table.c.id == str(pending.id))).one()._mapping)

        @contextlib.contextmanager
        def unlocked_transaction(_self, _session_id: str):
            with db_engine.begin() as conn:
                yield conn

        monkeypatch.setattr(
            SQLiteLocalSessionOperationAuthority,
            "_locked_transaction",
            unlocked_transaction,
        )

        original_compare_and_swap = session_operation_authority.compare_and_swap
        stale_at_pre_fs_guard = threading.Event()
        release_stale = threading.Event()
        paused = False

        def pause_after_successful_pre_fs_guard(context: SessionOperationContext) -> None:
            nonlocal paused
            original_compare_and_swap(context)
            if context == stale and not paused:
                paused = True
                stale_at_pre_fs_guard.set()
                if not release_stale.wait(timeout=5):
                    raise TimeoutError("test did not release stale output finalizer")

        monkeypatch.setattr(
            session_operation_authority,
            "compare_and_swap",
            pause_after_successful_pre_fs_guard,
        )

        stale_finalize = asyncio.create_task(
            service.finalize_run_output_blobs(
                run_id,
                success=False,
                session_operation_context=stale,
            )
        )
        assert await asyncio.to_thread(stale_at_pre_fs_guard.wait, 5)

        session_operation_authority.release(stale)
        winner_context = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="winner-finalize-owner",
            lease_seconds=30,
        )
        release_stale.set()

        with pytest.raises(SessionOperationFenceLost):
            await stale_finalize

        session_operation_authority.compare_and_swap(winner_context)
        with db_engine.connect() as conn:
            after = dict(conn.execute(select(blobs_table).where(blobs_table.c.id == str(pending.id))).one()._mapping)
        assert after == before
        assert storage.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Partial-failure resilience — elspeth-9f31c32cce
# ---------------------------------------------------------------------------


class TestFinalizeRunOutputBlobsPartialFailure:
    """Per-blob errors must not abort finalization of remaining blobs.

    Bug: elspeth-9f31c32cce — finalize_run_output_blobs aborts on per-blob
    failure, leaving remaining blobs permanently pending for terminal runs.
    """

    @pytest.fixture()
    def run_env(self, blob_service, session_id, db_engine):
        """Set up a composition state and run, return (run_id, session_id_str)."""
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="running",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )
        return UUID(run_id), session_id_str

    async def _create_linked_blob(
        self,
        blob_service,
        session_id: UUID,
        run_id: UUID,
        db_engine,
        filename: str,
        content: bytes | None = None,
    ):
        """Create a pending blob, optionally write content, and link to run."""
        from elspeth.web.sessions.models import blob_run_links_table

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename=filename,
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        if content is not None:
            from pathlib import Path as _Path

            _Path(pending.storage_path).write_bytes(content)

        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )
        return pending

    @staticmethod
    def _deny_read_bytes(monkeypatch: pytest.MonkeyPatch, denied_path: Path) -> None:
        original_read_bytes = Path.read_bytes

        def _read_bytes_or_permission_error(path: Path) -> bytes:
            if path == denied_path:
                raise PermissionError(f"Permission denied: '{denied_path}'")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", _read_bytes_or_permission_error)

    @pytest.mark.asyncio
    async def test_continues_after_concurrent_deletion(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When blob 2 of 3 is concurrently deleted (between initial query
        and per-blob finalize), blobs 1 and 3 still finalize."""
        from elspeth.web.blobs.protocol import BlobNotFoundError

        run_id, _ = run_env

        b1 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")
        b2 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b2.csv", b"data2")
        b3 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b3.csv", b"data3")

        # Inject at the typed repository mutation used inside the per-blob
        # suppression boundary, in the window after the initial listing.
        original = coordination_repository_module._RepositoryBlobMutations.mark_run_output_blob_ready

        def _patched(repository, *, run_id, blob_id, size_bytes, content_hash, max_storage_per_session):
            if blob_id == b2.id:
                raise BlobNotFoundError(str(blob_id))
            return original(
                repository,
                run_id=run_id,
                blob_id=blob_id,
                size_bytes=size_bytes,
                content_hash=content_hash,
                max_storage_per_session=max_storage_per_session,
            )

        monkeypatch.setattr(coordination_repository_module._RepositoryBlobMutations, "mark_run_output_blob_ready", _patched)
        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        assert len(result.finalized) == 2, f"Expected 2 finalized, got {len(result.finalized)}"
        assert len(result.errors) == 1, f"Expected 1 error, got {len(result.errors)}"
        assert result.errors[0].blob_id == b2.id
        assert result.errors[0].exc_type == "BlobNotFoundError"
        finalized_ids = {r.id for r in result.finalized}
        assert b1.id in finalized_ids
        assert b3.id in finalized_ids

    @pytest.mark.asyncio
    async def test_continues_after_already_finalized(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When blob 2 raises BlobStateError (already finalized), loop continues."""
        from elspeth.web.blobs.protocol import BlobStateError

        run_id, _ = run_env

        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")
        b2 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b2.csv", b"data2")
        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b3.csv", b"data3")

        original = coordination_repository_module._RepositoryBlobMutations.mark_run_output_blob_ready

        def _patched(repository, *, run_id, blob_id, size_bytes, content_hash, max_storage_per_session):
            if blob_id == b2.id:
                raise BlobStateError(str(blob_id), message="Cannot finalize — status is 'ready', expected 'pending'")
            return original(
                repository,
                run_id=run_id,
                blob_id=blob_id,
                size_bytes=size_bytes,
                content_hash=content_hash,
                max_storage_per_session=max_storage_per_session,
            )

        monkeypatch.setattr(coordination_repository_module._RepositoryBlobMutations, "mark_run_output_blob_ready", _patched)
        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        assert len(result.finalized) == 2
        assert len(result.errors) == 1
        assert result.errors[0].blob_id == b2.id
        assert result.errors[0].exc_type == "BlobStateError"

    @pytest.mark.asyncio
    async def test_continues_after_os_error_reading_file(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When file read raises OSError, loop continues to next blob."""
        run_id, _ = run_env

        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")
        b2 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b2.csv", b"data2")

        self._deny_read_bytes(monkeypatch, Path(b2.storage_path))
        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        assert len(result.finalized) == 1
        assert len(result.errors) == 1
        assert result.errors[0].blob_id == b2.id
        assert "OSError" in result.errors[0].exc_type or "PermissionError" in result.errors[0].exc_type
        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(b2.id))).one()
        assert row.status == "error"
        assert row.size_bytes == 0
        assert row.content_hash is None
        storage = Path(b2.storage_path)
        assert not storage.exists()
        assert list(storage.parent.glob(f".{storage.name}.output-delete-*")) == []

    @pytest.mark.asyncio
    async def test_ready_commit_unknown_restores_bytes_for_successor_retry_and_read(
        self,
        blob_service,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ready commit with a lost return cannot strand bytes in an error tombstone."""
        run_id, _ = run_env
        expected = b"ready bytes survive commit-unknown"
        pending = await self._create_linked_blob(
            blob_service,
            session_id,
            run_id,
            db_engine,
            "ready-commit-unknown.csv",
            expected,
        )
        storage = Path(pending.storage_path)
        context = _operation_context(session_id, SessionOperationKind.EXECUTE)
        original_mark_ready = blob_service._mark_run_output_blob_ready

        def commit_ready_then_lose_return(**kwargs):
            original_mark_ready(**kwargs)
            raise SQLAlchemyError("injected ready commit return loss")

        monkeypatch.setattr(blob_service, "_mark_run_output_blob_ready", commit_ready_then_lose_return)
        result = await blob_service.finalize_run_output_blobs(
            run_id,
            success=True,
            session_operation_context=context,
        )

        assert result.finalized == ()
        assert len(result.errors) == 1
        assert result.errors[0].exc_type == "SQLAlchemyError"
        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(pending.id))).one()
        assert row.status == "ready"
        assert row.size_bytes == len(expected)
        assert row.content_hash == content_hash(expected)
        assert storage.read_bytes() == expected
        assert list(storage.parent.glob(f".{storage.name}.output-delete-*")) == []

        session_operation_authority.release(context)
        successor = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="ready-commit-unknown-successor",
            lease_seconds=30,
        )
        try:
            retry = await blob_service.finalize_run_output_blobs(
                run_id,
                success=True,
                session_operation_context=successor,
            )
            assert retry.finalized == ()
            assert retry.errors == ()
            assert (
                await blob_service.read_blob_content(
                    pending.id,
                    session_operation_context=successor,
                )
                == expected
            )
        finally:
            session_operation_authority.release(successor)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("first_surface", ["retry", "read"])
    async def test_ready_commit_unknown_staging_fsync_failure_recovers_for_successor(
        self,
        blob_service,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id: UUID,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
        first_surface: str,
    ) -> None:
        """A successor restores exact ready bytes left staged by a failed fsync."""
        run_id, _ = run_env
        expected = b"ready bytes survive staged fsync failure"
        pending = await self._create_linked_blob(
            blob_service,
            session_id,
            run_id,
            db_engine,
            f"ready-fsync-{first_surface}.csv",
            expected,
        )
        storage = Path(pending.storage_path)
        context = _operation_context(session_id, SessionOperationKind.EXECUTE)
        original_mark_ready = blob_service._mark_run_output_blob_ready
        original_fsync_parent = blob_service_module._fsync_parent_directory
        failed_staging_fsync = False

        def commit_ready_then_lose_return(**kwargs):
            original_mark_ready(**kwargs)
            raise SQLAlchemyError("injected ready commit return loss")

        def fail_staging_fsync_once(parent: Path) -> None:
            nonlocal failed_staging_fsync
            if not failed_staging_fsync:
                failed_staging_fsync = True
                raise OSError("injected staged output fsync failure")
            original_fsync_parent(parent)

        monkeypatch.setattr(blob_service, "_mark_run_output_blob_ready", commit_ready_then_lose_return)
        monkeypatch.setattr(blob_service_module, "_fsync_parent_directory", fail_staging_fsync_once)
        result = await blob_service.finalize_run_output_blobs(
            run_id,
            success=True,
            session_operation_context=context,
        )

        assert result.finalized == ()
        assert [error.exc_type for error in result.errors] == [
            "SQLAlchemyError",
            "RecoveryFailed[OSError]",
        ]
        assert failed_staging_fsync is True
        with db_engine.connect() as conn:
            row = conn.execute(select(blobs_table).where(blobs_table.c.id == str(pending.id))).one()
        assert row.status == "ready"
        assert row.size_bytes == len(expected)
        assert row.content_hash == content_hash(expected)
        assert not storage.exists()
        tombstones = list(storage.parent.glob(f".{storage.name}.output-delete-*"))
        assert len(tombstones) == 1
        assert tombstones[0].read_bytes() == expected

        session_operation_authority.release(context)
        successor = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=f"ready-fsync-{first_surface}-successor",
            lease_seconds=30,
        )
        try:
            if first_surface == "retry":
                retry = await blob_service.finalize_run_output_blobs(
                    run_id,
                    success=True,
                    session_operation_context=successor,
                )
                assert retry.finalized == ()
                assert retry.errors == ()
            else:
                assert (
                    await blob_service.read_blob_content(
                        pending.id,
                        session_operation_context=successor,
                    )
                    == expected
                )

            assert storage.read_bytes() == expected
            assert list(storage.parent.glob(f".{storage.name}.output-delete-*")) == []
            assert (
                await blob_service.read_blob_content(
                    pending.id,
                    session_operation_context=successor,
                )
                == expected
            )
            retry = await blob_service.finalize_run_output_blobs(
                run_id,
                success=True,
                session_operation_context=successor,
            )
            assert retry.finalized == ()
            assert retry.errors == ()
        finally:
            session_operation_authority.release(successor)

    @pytest.mark.asyncio
    async def test_ready_tombstone_with_mismatched_bytes_fails_closed(
        self,
        blob_service: BlobServiceImpl,
        session_id: UUID,
    ) -> None:
        """Ready reconciliation never blesses tombstone bytes that metadata did not commit."""
        context = _operation_context(session_id, SessionOperationKind.EXECUTE)
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="ready-mismatched-tombstone.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=context,
        )
        storage = Path(pending.storage_path)
        expected = b"committed ready output bytes"
        storage.write_bytes(expected)
        await blob_service.finalize_blob(
            pending.id,
            "ready",
            size_bytes=len(expected),
            content_hash=content_hash(expected),
            session_operation_context=context,
        )
        operation_token = blob_service_module._blob_operation_path_token(
            operation_id=context.fence.operation_id,
            operation_epoch=context.fence.operation_epoch,
            operation_kind=context.operation_kind,
        )
        tombstone = storage.with_name(f".{storage.name}.output-delete-{operation_token}")
        os.replace(storage, tombstone)
        tampered = bytes([expected[0] ^ 1]) + expected[1:]
        tombstone.write_bytes(tampered)

        with pytest.raises(BlobIntegrityError):
            await blob_service.read_blob_content(
                pending.id,
                session_operation_context=context,
            )

        assert not storage.exists()
        assert tombstone.read_bytes() == tampered

    @pytest.mark.asyncio
    async def test_propagates_type_error(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Programmer bugs (TypeError) must crash, not be caught."""
        run_id, _ = run_env

        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")

        def _broken_finalize(*args, **kwargs):
            raise TypeError("unexpected keyword argument")

        monkeypatch.setattr(
            coordination_repository_module._RepositoryBlobMutations,
            "mark_run_output_blob_ready",
            _broken_finalize,
        )
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            await blob_service.finalize_run_output_blobs(
                run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
            )

    @pytest.mark.asyncio
    async def test_all_blobs_fail_returns_empty_finalized_with_errors(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When all blobs fail, result has empty finalized and N errors."""
        from elspeth.web.blobs.protocol import BlobNotFoundError

        run_id, _ = run_env

        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")
        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b2.csv", b"data2")

        def _all_missing(_repository, *, blob_id, **_kwargs):
            raise BlobNotFoundError(str(blob_id))

        monkeypatch.setattr(
            coordination_repository_module._RepositoryBlobMutations,
            "mark_run_output_blob_ready",
            _all_missing,
        )
        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        assert len(result.finalized) == 0
        assert len(result.errors) == 2

    @pytest.mark.asyncio
    async def test_zero_pending_blobs_returns_empty_result(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
    ) -> None:
        """Run with no pending output blobs returns empty result."""
        run_id, _ = run_env

        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        assert len(result.finalized) == 0
        assert len(result.errors) == 0

    @pytest.mark.asyncio
    async def test_best_effort_error_recovery_marks_blob_as_error(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When per-blob catch fires, the failed blob is set to 'error' status."""
        from elspeth.web.sessions.models import blobs_table as bt

        run_id, _ = run_env

        b1 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")
        b2 = await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b2.csv", b"data2")

        self._deny_read_bytes(monkeypatch, Path(b1.storage_path))
        result = await blob_service.finalize_run_output_blobs(
            run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        # b1 should have been moved to "error" by the best-effort recovery
        with db_engine.connect() as conn:
            row = conn.execute(bt.select().where(bt.c.id == str(b1.id))).first()
        assert row is not None
        assert row.status == "error", f"Expected 'error', got '{row.status}' — recovery should mark failed blobs"

        # b2 should be finalized normally
        assert len(result.finalized) == 1
        assert result.finalized[0].id == b2.id

    @pytest.mark.asyncio
    async def test_runtime_error_from_vanished_blob_propagates(
        self,
        blob_service,
        session_id,
        db_engine,
        run_env,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RuntimeError (Tier 1 anomaly: blob vanished mid-transaction) propagates."""
        run_id, _ = run_env

        await self._create_linked_blob(blob_service, session_id, run_id, db_engine, "b1.csv", b"data1")

        def _vanishing_finalize(*args, **kwargs):
            raise RuntimeError("Blob abc vanished during finalize — concurrent deletion?")

        monkeypatch.setattr(
            coordination_repository_module._RepositoryBlobMutations,
            "mark_run_output_blob_ready",
            _vanishing_finalize,
        )
        with pytest.raises(RuntimeError, match="vanished during finalize"):
            await blob_service.finalize_run_output_blobs(
                run_id, success=True, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
            )


# ---------------------------------------------------------------------------
# read_blob_content — lifecycle and integrity guards (elspeth-6082ad9636)
# ---------------------------------------------------------------------------


class TestReadBlobContentLifecycleGuard:
    """read_blob_content must enforce blob lifecycle state and content integrity.

    Bug: elspeth-6082ad9636 — read_blob_content() returns bytes without
    checking blob status or verifying the stored content_hash.
    """

    @pytest.mark.asyncio
    async def test_rejects_pending_blob(self, blob_service, session_id) -> None:
        """Pending blobs have no finalized content — reading must fail."""
        from pathlib import Path as _Path

        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        # Write a file so the only guard is status, not file existence
        _Path(pending.storage_path).write_bytes(b"partial-content")

        with pytest.raises(BlobStateError):
            await blob_service.read_blob_content(pending.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_rejects_error_blob(self, blob_service, session_id) -> None:
        """Error blobs represent failed runs — content must not be served."""
        from pathlib import Path as _Path

        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        _Path(pending.storage_path).write_bytes(b"partial-content")
        await blob_service.finalize_blob(
            pending.id,
            status="error",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        with pytest.raises(BlobStateError):
            await blob_service.read_blob_content(pending.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_detects_content_hash_mismatch(self, blob_service, session_id) -> None:
        """Tier 1 integrity: if stored hash doesn't match file bytes, crash."""
        from pathlib import Path as _Path

        from elspeth.web.blobs.protocol import BlobIntegrityError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="tampered.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        assert record.status == "ready"
        assert record.content_hash is not None

        # Tamper with the file on disk after creation
        _Path(record.storage_path).write_bytes(b"tampered-content")

        with pytest.raises(BlobIntegrityError):
            await blob_service.read_blob_content(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_rejects_ready_blob_with_missing_backing_file(self, blob_service, session_id) -> None:
        """Ready metadata without backing bytes is an integrity failure, not 404."""
        from pathlib import Path as _Path

        from elspeth.web.blobs.protocol import BlobContentMissingError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="missing.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        _Path(record.storage_path).unlink()

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.read_blob_content(record.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_translates_file_deleted_between_exists_check_and_read(
        self,
        blob_service,
        session_id,
        monkeypatch,
    ) -> None:
        """Raced deletion after the existence guard keeps the lifecycle contract.

        Parity with read_blob_content_prefix_verified, whose docstring
        claims it mirrors this method's guards exactly.
        """
        from elspeth.web.blobs.protocol import BlobContentMissingError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="raced-delete.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        target = Path(record.storage_path)
        real_open = Path.open
        deleted = False

        def delete_then_open(self: Path, *args: object, **kwargs: object) -> object:
            nonlocal deleted
            if self == target and not deleted:
                deleted = True
                self.unlink()
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", delete_then_open)

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.read_blob_content(
                record.id,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_rejects_pending_blob_without_file(self, blob_service, session_id) -> None:
        """Pending blob with no file must raise BlobStateError, not BlobNotFoundError.

        Guards exception ordering: the status check must fire before
        the file-existence check, otherwise a missing file would mask
        the lifecycle violation.
        """
        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="no-file.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        # Deliberately do NOT write a file

        with pytest.raises(BlobStateError, match="expected 'ready'"):
            await blob_service.read_blob_content(pending.id, session_operation_context=_operation_context(session_id))

    @pytest.mark.asyncio
    async def test_ready_blob_with_valid_hash_succeeds(self, blob_service, session_id) -> None:
        """Ready blob with matching hash returns content normally."""
        content = b"valid-content"
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="good.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        result = await blob_service.read_blob_content(record.id, session_operation_context=_operation_context(session_id))
        assert result == content


# ---------------------------------------------------------------------------
# read_blob_content_prefix_verified — bounded-memory streamed read + verify
# ---------------------------------------------------------------------------


class _ReadSizeSpyFile:
    """Wraps a real file handle, recording every ``read(size)`` argument.

    Lets a test prove a read path is genuinely chunked — never one big
    slurp — without needing a multi-hundred-MiB fixture to make the
    difference observable.
    """

    def __init__(self, handle) -> None:
        self._handle = handle
        self.requested_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.requested_sizes.append(size)
        return self._handle.read(size)

    def __enter__(self) -> _ReadSizeSpyFile:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._handle.__exit__(exc_type, exc, tb)


class TestReadBlobContentPrefixVerifiedStreaming:
    """read_blob_content_prefix_verified must stream + verify without holding
    the full blob in memory, mirroring read_blob_content's guards exactly.

    Finding B (memory half): a 100 MiB blob was fully materialized in RAM
    per guided selection just to serve an 8 KiB bounded preview, because the
    only way to verify the full-content hash was `storage.read_bytes()`.
    This method reads and hashes in bounded chunks instead.
    """

    @pytest.mark.asyncio
    async def test_reads_in_bounded_chunks_not_one_full_read(self, blob_service, session_id, monkeypatch) -> None:
        # Shrink the chunk size so a modest fixture (20 KiB) still spans many
        # chunks — proves chunking structurally without a huge fixture.
        monkeypatch.setattr(blob_service_module, "_STREAM_CHUNK_BYTES", 4096)
        content = bytes(range(256)) * 80  # 20480 bytes, > 4096 * 4
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="large.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        spies: list[_ReadSizeSpyFile] = []
        real_open = Path.open
        target = str(Path(record.storage_path))

        def spy_open(self: Path, *args: object, **kwargs: object) -> object:
            handle = real_open(self, *args, **kwargs)
            if str(self) == target:
                spy = _ReadSizeSpyFile(handle)
                spies.append(spy)
                return spy
            return handle

        monkeypatch.setattr(Path, "open", spy_open)

        prefix, verified_hash, total_size = await blob_service.read_blob_content_prefix_verified(
            record.id,
            prefix_bytes=100,
            session_operation_context=_operation_context(session_id),
        )

        assert prefix == content[:100]
        assert verified_hash == hashlib.sha256(content).hexdigest()
        assert verified_hash == record.content_hash
        assert total_size == len(content)

        assert len(spies) == 1
        sizes = spies[0].requested_sizes
        # More than one chunk read (proves streaming, not a single slurp),
        # and no single read() call requested more than the chunk bound.
        assert len(sizes) > 1
        assert all(size <= 4096 for size in sizes)

    @pytest.mark.asyncio
    async def test_rejects_pending_blob(self, blob_service, session_id) -> None:
        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        Path(pending.storage_path).write_bytes(b"partial-content")

        with pytest.raises(BlobStateError):
            await blob_service.read_blob_content_prefix_verified(
                pending.id,
                prefix_bytes=8,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_rejects_ready_blob_with_missing_backing_file(self, blob_service, session_id) -> None:
        from elspeth.web.blobs.protocol import BlobContentMissingError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="missing.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        Path(record.storage_path).unlink()

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.read_blob_content_prefix_verified(
                record.id,
                prefix_bytes=8,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_translates_file_deleted_between_exists_check_and_open(
        self,
        blob_service,
        session_id,
        monkeypatch,
    ) -> None:
        from elspeth.web.blobs.protocol import BlobContentMissingError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="raced-delete.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        target = Path(record.storage_path)
        real_open = Path.open
        deleted = False

        def delete_then_open(self: Path, *args: object, **kwargs: object) -> object:
            nonlocal deleted
            if self == target and not deleted:
                deleted = True
                self.unlink()
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", delete_then_open)

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.read_blob_content_prefix_verified(
                record.id,
                prefix_bytes=8,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_detects_content_hash_mismatch_fail_closed(self, blob_service, session_id) -> None:
        """Same Tier 1 fail-closed guarantee as read_blob_content: a tampered
        file must never be served, even for a bounded preview."""
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="tampered.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        Path(record.storage_path).write_bytes(b"tampered-content")

        with pytest.raises(BlobIntegrityError):
            await blob_service.read_blob_content_prefix_verified(
                record.id,
                prefix_bytes=8,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_prefix_shorter_than_content_still_verifies_full_hash(self, blob_service, session_id) -> None:
        """The verified hash covers the FULL blob even though only a short
        prefix is retained — a partial digest could never validate the
        stored full-content hash."""
        content = b"0123456789" * 2000  # 20000 bytes
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="big.csv",
            content=content,
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        prefix, verified_hash, total_size = await blob_service.read_blob_content_prefix_verified(
            record.id,
            prefix_bytes=8 * 1024,
            session_operation_context=_operation_context(session_id),
        )

        assert len(prefix) == 8 * 1024
        assert prefix == content[: 8 * 1024]
        assert total_size == len(content)
        assert verified_hash == content_hash(content)


class TestReadBlobPreviewLifecycleGuard:
    """The bounded preview shares read_blob_content's missing-file contract."""

    @pytest.mark.asyncio
    async def test_rejects_ready_blob_with_missing_backing_file(self, blob_service, session_id) -> None:
        from elspeth.web.blobs.protocol import BlobContentMissingError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="missing.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        Path(record.storage_path).unlink()

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.read_blob_preview(
                record.id,
                limit_bytes=8,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_translates_file_deleted_between_exists_check_and_open(
        self,
        blob_service,
        session_id,
        monkeypatch,
    ) -> None:
        from elspeth.web.blobs.protocol import BlobContentMissingError

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="raced-delete.csv",
            content=b"original-content",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )
        target = Path(record.storage_path)
        real_open = Path.open
        deleted = False

        def delete_then_open(self: Path, *args: object, **kwargs: object) -> object:
            nonlocal deleted
            if self == target and not deleted:
                deleted = True
                self.unlink()
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", delete_then_open)

        with pytest.raises(BlobContentMissingError, match="backing file"):
            await blob_service.read_blob_preview(
                record.id,
                limit_bytes=8,
                session_operation_context=_operation_context(session_id),
            )

    @pytest.mark.asyncio
    async def test_previews_bounded_prefix_and_flags_truncation(self, blob_service, session_id) -> None:
        record = await blob_service.create_blob(
            session_id=session_id,
            filename="preview.csv",
            content=b"0123456789",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        data, truncated = await blob_service.read_blob_preview(
            record.id,
            limit_bytes=4,
            session_operation_context=_operation_context(session_id),
        )

        assert data == b"0123"
        assert truncated is True


# ---------------------------------------------------------------------------
# finalize_run_output_blobs — error path file cleanup (elspeth-0a2644dcb9)
# ---------------------------------------------------------------------------


class TestFinalizeRunOutputBlobsErrorCleanup:
    """Failed run outputs must not leave orphaned backing files.

    Bug: elspeth-0a2644dcb9 — finalize to "error" only updates metadata,
    leaving the backing file on disk while size_bytes=0 and content_hash=None.
    """

    @pytest.fixture()
    def run_env(self, blob_service, session_id, db_engine):
        """Set up a composition state and run, return (run_id, session_id_str)."""
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        state_id = str(uuid4())
        session_id_str = str(session_id)
        run_id = str(uuid4())

        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: every test-side direct composition_states
                    # insert must supply provenance after Task 3's CHECK
                    # constraint. ``session_seed`` is the broadened-semantics
                    # default for setup-only rows that don't model a real
                    # compose-loop transition.
                    provenance="session_seed",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="running",
                    started_at=datetime(2026, 1, 1, tzinfo=UTC),
                    rows_processed=0,
                    rows_failed=0,
                )
            )
        return UUID(run_id), session_id_str

    @pytest.mark.asyncio
    async def test_failure_deletes_backing_file(self, blob_service, session_id, db_engine, run_env) -> None:
        """When run fails, backing file must be deleted — not left orphaned."""
        from pathlib import Path as _Path

        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="output.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        # Simulate sink writing partial output before run failure
        storage = _Path(pending.storage_path)
        storage.write_bytes(b"partial-output-before-crash")
        assert storage.exists()

        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )

        result = await blob_service.finalize_run_output_blobs(
            run_id, success=False, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        assert len(result.finalized) == 1
        blob_result = result.finalized[0]
        assert blob_result.status == "error"

        # THE BUG: file must NOT exist after error finalization
        assert not storage.exists(), "Backing file still exists after error finalization — orphaned file will escape quota accounting"

        # Metadata must reflect no content — size_bytes=0, content_hash=None.
        # If these don't match, quota accounting diverges from filesystem.
        assert blob_result.size_bytes == 0, f"Expected size_bytes=0 for error blob, got {blob_result.size_bytes}"
        assert blob_result.content_hash is None, f"Expected content_hash=None for error blob, got {blob_result.content_hash}"

    @pytest.mark.asyncio
    async def test_failure_without_file_still_sets_error(self, blob_service, session_id, db_engine, run_env) -> None:
        """When run fails and no file was written, status is still error (no crash)."""
        from elspeth.web.sessions.models import blob_run_links_table

        run_id, _ = run_env

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="never-written.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        with db_engine.begin() as conn:
            conn.execute(
                blob_run_links_table.insert().values(
                    blob_id=str(pending.id),
                    run_id=str(run_id),
                    direction="output",
                )
            )

        result = await blob_service.finalize_run_output_blobs(
            run_id, success=False, session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        assert len(result.finalized) == 1
        assert result.finalized[0].status == "error"


# ---------------------------------------------------------------------------
# Database-level integrity constraint — ck_blobs_ready_hash (elspeth-e435b147b7)
# ---------------------------------------------------------------------------


class TestBlobsReadyHashDBConstraint:
    """The DB refuses status='ready' rows without a content_hash.

    Service-level validation in _validate_finalize_hash is the first line
    of defence, but the CHECK constraint in the current schema is the belt:
    even raw SQL / direct ORM writes that bypass the service cannot
    commit a violating row.
    """

    def test_inserting_ready_without_hash_raises(self, db_engine, session_id) -> None:
        """Direct INSERT violating the invariant is rejected at commit time."""
        from datetime import UTC, datetime

        from sqlalchemy.exc import IntegrityError

        from elspeth.web.sessions.models import blobs_table

        session_id_str = str(session_id)
        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=str(uuid4()),
                    session_id=session_id_str,
                    filename="illegal.csv",
                    mime_type="text/csv",
                    size_bytes=1,
                    content_hash=None,  # <-- the violation
                    storage_path="/tmp/never",
                    created_at=datetime.now(UTC),
                    created_by="user",
                    status="ready",
                )
            )

    def test_inserting_pending_without_hash_is_allowed(self, db_engine, session_id) -> None:
        """Pending and error rows may carry NULL hashes — only 'ready' is constrained."""
        from datetime import UTC, datetime

        from elspeth.web.sessions.models import blobs_table

        session_id_str = str(session_id)
        with db_engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=str(uuid4()),
                    session_id=session_id_str,
                    filename="pending.csv",
                    mime_type="text/csv",
                    size_bytes=0,
                    content_hash=None,
                    storage_path="/tmp/pending",
                    created_at=datetime.now(UTC),
                    created_by="pipeline",
                    status="pending",
                )
            )

    @pytest.mark.asyncio
    async def test_update_ready_hash_to_null_rejected(self, blob_service, db_engine, session_id) -> None:
        """Can't bypass the guard by mutating an existing ready row."""
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError

        from elspeth.web.sessions.models import blobs_table

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="legit.csv",
            content=b"a,b,c\n1,2,3\n",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(content_hash=None))

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "abc123",  # too short
            "a" * 63,  # off-by-one: 63 chars
            "a" * 65,  # off-by-one: 65 chars
            "A" * 64,  # uppercase
            "g" * 64,  # non-hex letter
            "a" * 63 + "Z",  # mostly-hex with one non-hex char
            "",  # empty
            "a" * 64 + "\n",  # trailing newline — ``^...$`` regex accepts this, ``fullmatch`` rejects
        ],
    )
    @pytest.mark.asyncio
    async def test_update_ready_hash_to_malformed_rejected(self, blob_service, db_engine, session_id, bad_hash: str) -> None:
        """Updating a ready row's hash to a malformed value is rejected.

        The service-level write path goes through ``_validate_finalize_hash``
        which rejects malformed hashes before SQL.  This test bypasses the
        service entirely and asserts the database CHECK is the second wall
        — so a future caller that builds an UPDATE statement directly (or
        a migration script that touches content_hash) cannot leave the row
        in a "ready but unverifiable" state.
        """
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError

        from elspeth.web.sessions.models import blobs_table

        record = await blob_service.create_blob(
            session_id=session_id,
            filename="legit.csv",
            content=b"a,b,c\n1,2,3\n",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            conn.execute(update(blobs_table).where(blobs_table.c.id == str(record.id)).values(content_hash=bad_hash))


# ---------------------------------------------------------------------------
# Public pending-output finalization — validates hashes and lifecycle state
# inside the same exact EXECUTE-authorized repository transaction.
# ---------------------------------------------------------------------------


class TestFinalizeBlobPublicHashValidation:
    """The public fenced output lifecycle must enforce hash invariants."""

    @pytest.mark.asyncio
    async def test_public_path_rejects_missing_hash_for_ready(self, blob_service, session_id) -> None:
        """Finalizing ready with no hash raises BlobStateError."""
        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="pipe.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        with pytest.raises(BlobStateError, match="content_hash"):
            await blob_service.finalize_blob(
                pending.id,
                "ready",
                size_bytes=42,
                content_hash=None,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_public_path_rejects_non_sha256_hash(self, blob_service, session_id) -> None:
        """A malformed ready hash raises BlobStateError."""
        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="pipe.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        with pytest.raises(BlobStateError, match="64 lowercase hex"):
            await blob_service.finalize_blob(
                pending.id,
                "ready",
                size_bytes=42,
                content_hash="abc123",  # too short
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_public_path_rejects_uppercase_hex_hash(self, blob_service, session_id) -> None:
        """The canonical form is lowercase; uppercase hex is a bifurcation risk.

        FilesystemPayloadStore writes lowercase, and read_blob_content
        compares via hmac.compare_digest — byte-for-byte.  If the
        write-side validator silently admitted uppercase, a pipeline
        could commit a blob whose hash does not match the stored form
        anywhere else in the audit trail.
        """
        from elspeth.web.blobs.protocol import BlobStateError

        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="pipe.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        uppercase_hash = content_hash(b"real-bytes").upper()

        with pytest.raises(BlobStateError, match="64 lowercase hex"):
            await blob_service.finalize_blob(
                pending.id,
                "ready",
                size_bytes=10,
                content_hash=uppercase_hash,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_public_path_allows_error_status_without_hash(self, blob_service, session_id) -> None:
        """The hash invariant applies only to 'ready'; 'error' requires nothing."""
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="pipe.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        record = await blob_service.finalize_blob(
            pending.id,
            "error",
            size_bytes=None,
            content_hash=None,
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )
        assert record.status == "error"
        assert record.content_hash is None

    @pytest.mark.asyncio
    async def test_public_path_invalid_status_raises_runtime_error(self, blob_service, session_id) -> None:
        """Invalid status on the public path must propagate as RuntimeError.

        _PER_BLOB_SUPPRESSED deliberately excludes RuntimeError so a
        programmer bug (typo'd status literal) crashes the pipeline
        finalization loop rather than being converted silently into a
        per-blob 'error' record.  BlobStateError would have been
        suppressed — so this test pins the crash-not-suppress contract.
        """
        pending = await blob_service.create_pending_blob(
            session_id=session_id,
            filename="pipe.csv",
            mime_type="text/csv",
            created_by="pipeline",
            session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
        )

        with pytest.raises(RuntimeError, match="Invalid finalize status"):
            await blob_service.finalize_blob(
                pending.id,
                "deleted",
                size_bytes=None,
                content_hash=None,
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )


# ---------------------------------------------------------------------------
# link_blob_to_run — runtime guard on BlobRunLinkDirection (elspeth-b6ac739b83)
# ---------------------------------------------------------------------------


class TestLinkBlobToRunDirectionGuard:
    """link_blob_to_run rejects direction values outside the Literal set."""

    @staticmethod
    def _make_run(db_engine, session_id: UUID) -> UUID:
        """Seed a composition state and run for FK satisfaction."""
        from elspeth.web.sessions.models import (
            composition_states_table,
            runs_table,
        )

        state_id = str(uuid4())
        run_id = str(uuid4())
        session_id_str = str(session_id)
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                composition_states_table.insert().values(
                    id=state_id,
                    session_id=session_id_str,
                    version=1,
                    is_valid=True,
                    # Plan §2294: setup-only row; provenance required.
                    provenance="session_seed",
                    created_at=now,
                )
            )
            conn.execute(
                runs_table.insert().values(
                    id=run_id,
                    session_id=session_id_str,
                    state_id=state_id,
                    status="running",
                    started_at=now,
                    rows_processed=0,
                    rows_failed=0,
                )
            )
        return UUID(run_id)

    @pytest.mark.asyncio
    async def test_rejects_invalid_direction(self, blob_service, session_id, db_engine) -> None:
        """A typo'd direction must raise RuntimeError before touching the DB.

        Mirrors finalize_blob's invariant: the Literal alias narrows
        static callers, but the runtime guard catches dynamic / untyped
        call sites.  RuntimeError is the crash-not-suppress classification
        for "caller passed a value outside the Literal set."
        """
        run_id = self._make_run(db_engine, session_id)
        blob = await blob_service.create_blob(
            session_id=session_id,
            filename="input.csv",
            content=b"a,b,c\n1,2,3\n",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        with pytest.raises(RuntimeError, match="Invalid link direction"):
            await blob_service.link_blob_to_run(
                blob_id=blob.id,
                run_id=run_id,
                direction="inout",
                session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE),
            )

    @pytest.mark.asyncio
    async def test_accepts_input_and_output(self, blob_service, session_id, db_engine) -> None:
        """Positive control: both valid directions commit without error."""
        run_id = self._make_run(db_engine, session_id)
        blob = await blob_service.create_blob(
            session_id=session_id,
            filename="input.csv",
            content=b"a,b,c\n1,2,3\n",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        await blob_service.link_blob_to_run(
            blob.id, run_id, "input", session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        await blob_service.link_blob_to_run(
            blob.id, run_id, "output", session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        links = await blob_service.get_blob_run_links(blob.id)
        directions = sorted(link.direction for link in links)
        assert directions == ["input", "output"]

    @pytest.mark.asyncio
    async def test_duplicate_same_direction_link_is_idempotent(self, blob_service, session_id, db_engine) -> None:
        """A source bind and inline-content ref can share the same input blob."""
        run_id = self._make_run(db_engine, session_id)
        blob = await blob_service.create_blob(
            session_id=session_id,
            filename="prompt.csv",
            content=b"prompt",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        await blob_service.link_blob_to_run(
            blob.id, run_id, "input", session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )
        await blob_service.link_blob_to_run(
            blob.id, run_id, "input", session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
        )

        links = await blob_service.get_blob_run_links(blob.id)
        assert [(link.run_id, link.direction) for link in links] == [(run_id, "input")]

    @pytest.mark.asyncio
    async def test_stale_execute_context_cannot_insert_blob_run_link(
        self,
        blob_service,
        session_operation_authority: SQLiteLocalSessionOperationAuthority,
        session_id,
        db_engine,
    ) -> None:
        """A superseded execution context performs zero linkage DML."""
        from elspeth.web.sessions.models import blob_run_links_table

        run_id = self._make_run(db_engine, session_id)
        blob = await blob_service.create_blob(
            session_id=session_id,
            filename="stale-input.csv",
            content=b"input",
            mime_type="text/csv",
            session_operation_context=_operation_context(session_id),
        )
        stale = _operation_context(session_id, SessionOperationKind.EXECUTE)
        session_operation_authority.release(stale)
        winner = session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id="blob-test-winner",
            lease_seconds=30,
        )
        try:
            with pytest.raises(SessionOperationFenceLost):
                await blob_service.link_blob_to_run(
                    blob.id,
                    run_id,
                    "input",
                    session_operation_context=stale,
                )
        finally:
            session_operation_authority.release(winner)

        with db_engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count())
                    .select_from(blob_run_links_table)
                    .where(blob_run_links_table.c.blob_id == str(blob.id))
                    .where(blob_run_links_table.c.run_id == str(run_id))
                ).scalar_one()
                == 0
            )


class TestLinkBlobToRunSessionGuard:
    """link_blob_to_run must reject cross-session references at the write boundary."""

    @pytest.mark.asyncio
    async def test_rejects_cross_session_link(self, blob_service, session_id, db_engine) -> None:
        """Blob and run from different sessions must not be linkable."""
        session_b = UUID(str(uuid4()))
        now = datetime.now(UTC)
        with db_engine.begin() as conn:
            conn.execute(
                sessions_table.insert().values(
                    id=str(session_b),
                    user_id="test-user-b",
                    auth_provider_type="local",
                    title="Session B",
                    created_at=now,
                    updated_at=now,
                )
            )

        foreign_run_id = TestLinkBlobToRunDirectionGuard._make_run(db_engine, session_b)
        blob = await blob_service.create_blob(
            session_id=session_id,
            filename="input.csv",
            content=b"a,b,c\n1,2,3\n",
            mime_type="text/csv",
            created_by="user",
            session_operation_context=_operation_context(session_id),
        )

        with pytest.raises(SessionDerivedCustodyError, match=r"^session-scoped derived record is unavailable$"):
            await blob_service.link_blob_to_run(
                blob.id, foreign_run_id, "input", session_operation_context=_operation_context(session_id, SessionOperationKind.EXECUTE)
            )

        assert await blob_service.get_blob_run_links(blob.id) == []


# ---------------------------------------------------------------------------
# Tier-1 read guards — audit-trail integrity for DB-sourced rows
# ---------------------------------------------------------------------------


class TestRowToRecordTierOneGuards:
    """Tier-1 read guards in ``_row_to_record`` / ``_row_to_link_record``.

    Context
    -------
    ``BlobRecord.status``, ``BlobRecord.created_by``, ``BlobRecord.mime_type``,
    and ``BlobRunLinkRecord.direction`` are declared as closed ``Literal``
    types. The write paths enforce this via CHECK constraints
    (``ck_blobs_status``, ``ck_blobs_created_by``, ``ck_blob_run_links_direction``)
    and an ``ALLOWED_MIME_TYPES`` membership check at create time.

    The read paths add a second line of defence: assertions inside
    ``_row_to_record`` / ``_row_to_link_record`` that crash if a row ever
    reaches Python with a value outside the declared enum. This matters
    because CHECK constraints can be bypassed by:

    - Direct driver writes (raw SQL, another service writing to the file)
    - A migration bug that drops or loosens the constraint
    - ``PRAGMA ignore_check_constraints`` during maintenance
    - Binary corruption of the sqlite file

    Without the Python-side guard, the returned ``BlobRecord`` would carry
    a ``status`` value that is a lie about its static type, and the
    audit trail would confidently return fabricated data.

    These tests synthesise raw row-like objects (``SimpleNamespace``) and
    feed them through the private helpers to confirm the guard trips. The
    tests deliberately do *not* route through the DB — the point is that
    even a row that somehow slipped past the write-side constraints is
    caught at the read boundary. If anyone weakens the guards (deletes an
    assertion, loosens a membership set, swaps ``in`` for an always-true
    comparison), these tests will fail.

    Note on ``python -O``: the guards are implemented with explicit
    ``raise AuditIntegrityError(...)`` (not ``assert``) so they survive
    optimised interpreter execution.  The Tier-1 DB-corruption contract
    is AuditIntegrityError; tests below pin that type so a silent
    downgrade back to ``assert`` (which ``-O`` strips) would fail here.
    """

    @staticmethod
    def _fake_blob_row(**overrides) -> SimpleNamespace:
        """Build a SQLAlchemy-Row-shaped stand-in with valid defaults.

        Any field can be overridden to force the guard under test.
        """
        defaults = {
            "id": str(uuid4()),
            "session_id": str(uuid4()),
            "filename": "data.csv",
            "mime_type": "text/csv",
            "size_bytes": 42,
            "content_hash": hashlib.sha256(b"x").hexdigest(),
            "storage_path": "/tmp/blobs/x.csv",
            "created_at": datetime.now(UTC),
            "created_by": "user",
            "source_description": None,
            "status": "ready",
            # Inline-blob provenance defaults (Phase 5a Task 2.5): the
            # synthetic row mirrors a verbatim row produced by the
            # user-upload write path (creation_modality='verbatim',
            # everything else NULL).
            "creation_modality": "verbatim",
            "created_from_message_id": None,
            "creating_model_identifier": None,
            "creating_model_version": None,
            "creating_provider": None,
            "creating_composer_skill_hash": None,
            "creating_arguments_hash": None,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @staticmethod
    def _fake_link_row(**overrides) -> SimpleNamespace:
        defaults = {
            "blob_id": str(uuid4()),
            "run_id": str(uuid4()),
            "direction": "input",
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    # ---- positive control -------------------------------------------------

    def test_valid_row_returns_record(self, blob_service) -> None:
        """Positive control: a row with all-valid values round-trips.

        Without this, a bug that makes every row fail would be
        indistinguishable from the guard tripping correctly.
        """
        row = self._fake_blob_row()
        record = blob_service._row_to_record(row)
        assert record.status == "ready"
        assert record.created_by == "user"
        assert record.mime_type == "text/csv"

    def test_valid_link_row_returns_record(self, blob_service) -> None:
        row = self._fake_link_row(direction="output")
        record = blob_service._row_to_link_record(row)
        assert record.direction == "output"

    # ---- status guard -----------------------------------------------------

    def test_status_outside_enum_trips_guard(self, blob_service) -> None:
        """A tampered/corrupt row with ``status`` outside BLOB_STATUSES
        must crash with a Tier-1 assertion message before the BlobRecord
        is constructed with the lie."""
        row = self._fake_blob_row(status="corrupted")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.status is 'corrupted'"):
            blob_service._row_to_record(row)

    def test_status_none_trips_guard(self, blob_service) -> None:
        """NULL status — e.g. from a dropped NOT NULL + DEFAULT during
        migration — is outside the enum and must crash."""
        row = self._fake_blob_row(status=None)
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.status"):
            blob_service._row_to_record(row)

    # ---- created_by guard ------------------------------------------------

    def test_created_by_outside_enum_trips_guard(self, blob_service) -> None:
        """An attacker who inserted a row directly (bypassing CHECK) with
        ``created_by = 'root'`` would otherwise surface as a valid record
        whose audit attribution is fabricated."""
        row = self._fake_blob_row(created_by="root")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.created_by is 'root'"):
            blob_service._row_to_record(row)

    def test_created_by_empty_string_trips_guard(self, blob_service) -> None:
        row = self._fake_blob_row(created_by="")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.created_by"):
            blob_service._row_to_record(row)

    # ---- mime_type guard -------------------------------------------------

    def test_mime_type_outside_allowlist_trips_guard(self, blob_service) -> None:
        """A row with an unallowed MIME type (e.g. ``application/x-sh``) must
        crash — the allowlist exists to constrain what the composer/pipeline
        layer will accept, and a laundered MIME would silently bypass it."""
        row = self._fake_blob_row(mime_type="application/x-sh")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.mime_type is 'application/x-sh'"):
            blob_service._row_to_record(row)

    def test_mime_type_case_mismatch_trips_guard(self, blob_service) -> None:
        """Membership in ``ALLOWED_MIME_TYPES`` is case-sensitive by
        construction (the Literal values are lowercase). A row with
        ``TEXT/CSV`` has the wrong casing and must be rejected — not
        coerced, because coercion at the Tier-1 boundary is forbidden."""
        row = self._fake_blob_row(mime_type="TEXT/CSV")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.mime_type"):
            blob_service._row_to_record(row)

    # ---- direction guard -------------------------------------------------

    def test_link_direction_outside_enum_trips_guard(self, blob_service) -> None:
        """``BlobRunLinkRecord.direction`` is typed as the Literal pair
        ``('input', 'output')``. A row with ``direction='inout'`` (the exact
        value the write-side test rejects) must also be rejected on read."""
        row = self._fake_link_row(direction="inout")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blob_run_links\.direction is 'inout'"):
            blob_service._row_to_link_record(row)

    def test_link_direction_none_trips_guard(self, blob_service) -> None:
        row = self._fake_link_row(direction=None)
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blob_run_links\.direction"):
            blob_service._row_to_link_record(row)

    # ---- guard-fires-before-record-construction --------------------------

    def test_bad_status_crashes_before_uuid_parse(self, blob_service) -> None:
        """The Tier-1 guard must fire before any field coercion (e.g.
        ``UUID(row.id)``). This pins the guard's position at the top of
        ``_row_to_record`` — a refactor that moves assertions after the
        ``BlobRecord(...)`` call would pass through a fabricated record to
        anything that catches the later error."""
        # ``id`` is a non-parseable string; if the guard were moved, the
        # UUID constructor would raise ValueError first and mask the
        # tampered-status condition.
        row = self._fake_blob_row(status="corrupted", id="not-a-uuid")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blobs\.status"):
            blob_service._row_to_record(row)

    def test_bad_direction_crashes_before_uuid_parse(self, blob_service) -> None:
        row = self._fake_link_row(direction="inout", blob_id="not-a-uuid")
        with pytest.raises(AuditIntegrityError, match=r"Tier 1: blob_run_links\.direction"):
            blob_service._row_to_link_record(row)
