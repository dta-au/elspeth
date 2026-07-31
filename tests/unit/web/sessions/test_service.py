"""Tests for SessionServiceImpl -- CRUD, state versioning, active run enforcement."""

from __future__ import annotations

import asyncio
import gc
import threading
import traceback
import uuid
import warnings
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog
from sqlalchemy import event, func, insert, select
from sqlalchemy.pool import StaticPool

from elspeth.web.coordination.contracts import (
    ArchiveDeleteReconciliation,
    ArchiveManifestRelation,
    FenceLossReason,
    SessionOperationContext,
    SessionOperationFenceLost,
    SessionOperationKind,
    SessionOperationTerminalOutcomeUnknown,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.schemas import (
    RunAccounting,
    RunAccountingIntegrity,
    RunAccountingRouting,
    RunAccountingSource,
    RunAccountingTokens,
    RunStatusResponse,
)
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.archive_quarantine import (
    ArchiveQuarantineCollisionError,
    ArchiveQuarantineIdentity,
    archive_quarantine_paths,
    list_archive_quarantine_manifests,
    prepare_archive_quarantine,
    purge_archive_quarantine,
    restore_archive_quarantine,
    retire_archive_quarantine,
    stage_archive_quarantine,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composer_completion_events_table,
    composition_states_table,
    run_events_table,
    runs_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import (
    LANDSCAPE_RECONCILIATION_ABSENT_SUFFIX,
    LANDSCAPE_RECONCILIATION_COMPLETE_SUFFIX,
    LANDSCAPE_RECONCILIATION_PENDING_SUFFIX,
    ChatMessageRecord,
    CompositionStateData,
    CompositionStateRecord,
    RunAlreadyActiveError,
    RunRecord,
    SessionGuidedOperationInProgressError,
    SessionRecord,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import QuarantineCleanupError, SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


class _RecordingSessionOperationAuthority:
    def __init__(self, delegate: SQLiteLocalSessionOperationAuthority) -> None:
        self._delegate = delegate
        self.acquire_calls: list[dict[str, object]] = []
        self.acquire_lease_seconds_override: int | None = None
        self.release_calls: list[SessionOperationContext] = []
        self.archive_delete_calls: list[SessionOperationContext] = []
        self.reconcile_archive_delete_calls: list[SessionOperationContext] = []
        self.classify_archive_manifest_calls: list[tuple[SessionOperationContext, object, int]] = []
        self.compare_and_swap_calls: list[SessionOperationContext] = []
        self.events: list[str] = []
        self.archive_delete_error: BaseException | None = None
        self.reconcile_archive_delete_error: BaseException | None = None
        self.compare_and_swap_errors: dict[int, BaseException] = {}
        self.renew_error: BaseException | None = None
        self.renew_wait_for: threading.Event | None = None
        self.renew_started = threading.Event()
        self.mutate_blocked = False
        self.mutate_started = threading.Event()
        self.mutate_allowed = threading.Event()
        self.mutate_allowed.set()
        self.mutate_finished = threading.Event()

    def create_session_with_initial_fence(self, **kwargs):
        return self._delegate.create_session_with_initial_fence(**kwargs)

    def acquire(self, **kwargs) -> SessionOperationContext:
        self.acquire_calls.append(dict(kwargs))
        if self.acquire_lease_seconds_override is not None:
            kwargs = {**kwargs, "lease_seconds": self.acquire_lease_seconds_override}
        return self._delegate.acquire(**kwargs)

    def renew(self, context: SessionOperationContext, *, lease_seconds: int) -> SessionOperationContext:
        if self.renew_wait_for is not None:
            assert self.renew_wait_for.wait(timeout=5)
        self.renew_started.set()
        if self.renew_error is not None:
            raise self.renew_error
        return self._delegate.renew(context, lease_seconds=lease_seconds)

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        self.compare_and_swap_calls.append(context)
        self.events.append("compare_and_swap")
        error = self.compare_and_swap_errors.get(len(self.compare_and_swap_calls))
        if error is not None:
            raise error
        self._delegate.compare_and_swap(context)

    def mutate(self, context: SessionOperationContext, mutation: Callable[[Any], Any]) -> Any:
        self.events.append("mutate")
        if not self.mutate_blocked:
            return self._delegate.mutate(context, mutation)
        self.mutate_started.set()
        try:
            assert self.mutate_allowed.wait(timeout=5)
            return self._delegate.mutate(context, mutation)
        finally:
            self.mutate_finished.set()

    def release(self, context: SessionOperationContext) -> None:
        self.release_calls.append(context)
        self.events.append("release")
        self._delegate.release(context)

    def archive_delete(self, context: SessionOperationContext) -> None:
        self.archive_delete_calls.append(context)
        self.events.append("archive_delete")
        if self.archive_delete_error is not None:
            raise self.archive_delete_error
        self._delegate.archive_delete(context)

    def reconcile_archive_delete(self, context: SessionOperationContext) -> ArchiveDeleteReconciliation:
        self.reconcile_archive_delete_calls.append(context)
        self.events.append("reconcile_archive_delete")
        if self.reconcile_archive_delete_error is not None:
            raise self.reconcile_archive_delete_error
        return self._delegate.reconcile_archive_delete(context)

    def classify_archive_manifest(
        self,
        current_context: SessionOperationContext,
        *,
        manifest_operation_id: object,
        manifest_operation_epoch: int,
    ) -> ArchiveManifestRelation:
        self.classify_archive_manifest_calls.append((current_context, manifest_operation_id, manifest_operation_epoch))
        self.events.append("classify_archive_manifest")
        return self._delegate.classify_archive_manifest(
            current_context,
            manifest_operation_id=manifest_operation_id,  # type: ignore[arg-type]
            manifest_operation_epoch=manifest_operation_epoch,
        )

    def mutate_fork_creation(self, **kwargs):
        return self._delegate.mutate_fork_creation(**kwargs)


def _service_with_recording_authority(
    engine,
    *,
    data_dir=None,
    lease_seconds: int = 30,
) -> tuple[SessionServiceImpl, _RecordingSessionOperationAuthority]:
    authority = _RecordingSessionOperationAuthority(SQLiteLocalSessionOperationAuthority(engine))
    return (
        SessionServiceImpl(
            engine,
            data_dir=data_dir,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.archive-authority"),
            session_operation_authority=authority,
            owner_instance_id="test-archive-owner",
            session_operation_lease_seconds=lease_seconds,
        ),
        authority,
    )


class _SessionOperationContexts:
    """Acquire real per-session operation contexts and release test-held leases."""

    def __init__(self) -> None:
        self._held: list[tuple[SessionServiceImpl, SessionOperationContext]] = []

    def acquire(
        self,
        service: SessionServiceImpl,
        session_id: uuid.UUID,
        operation_kind: SessionOperationKind = SessionOperationKind.EXECUTE,
    ) -> SessionOperationContext:
        context = service.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=operation_kind,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
        self._held.append((service, context))
        return context

    def release(self, service: SessionServiceImpl, context: SessionOperationContext) -> None:
        service.session_operation_authority.release(context)
        self._held.remove((service, context))

    def close(self) -> None:
        for service, context in reversed(self._held):
            service.session_operation_authority.release(context)
        self._held.clear()


@pytest.fixture
def engine():
    """Create an in-memory SQLite engine with all tables.

    Uses StaticPool so that run_in_executor threads share the same
    in-memory database connection.
    """
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def session_operation_contexts():
    contexts = _SessionOperationContexts()
    try:
        yield contexts
    finally:
        contexts.close()


@pytest.fixture
def service(engine):
    """Create a SessionServiceImpl backed by the in-memory engine."""
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


class TestSessionCRUD:
    """Tests for session create, get, list, and archive."""

    @pytest.mark.asyncio
    async def test_create_session(self, service) -> None:
        session = await service.create_session("alice", "My Session", "local")
        assert isinstance(session, SessionRecord)
        assert session.user_id == "alice"
        assert session.auth_provider_type == "local"
        assert session.title == "My Session"
        assert isinstance(session.id, uuid.UUID)
        assert isinstance(session.created_at, datetime)

    @pytest.mark.asyncio
    async def test_get_session(self, service) -> None:
        created = await service.create_session("alice", "Test", "local")
        fetched = await service.get_session(created.id)
        assert fetched.id == created.id
        assert fetched.user_id == "alice"
        assert fetched.title == "Test"

    @pytest.mark.asyncio
    async def test_update_session_title_persists_and_refreshes_timestamp(self, service) -> None:
        created = await service.create_session("alice", "Test", "local")

        updated = await service.update_session_title(created.id, "Renamed pipeline")

        assert updated.id == created.id
        assert updated.title == "Renamed pipeline"
        assert updated.updated_at >= created.updated_at
        fetched = await service.get_session(created.id)
        assert fetched.title == "Renamed pipeline"

    @pytest.mark.asyncio
    async def test_get_session_not_found_raises(self, service) -> None:
        with pytest.raises(ValueError, match="not found"):
            await service.get_session(uuid.uuid4())

    @pytest.mark.asyncio
    async def test_list_sessions_user_scoped(self, service) -> None:
        await service.create_session("alice", "Session A", "local")
        await service.create_session("alice", "Session B", "local")
        await service.create_session("bob", "Session C", "local")

        alice_sessions = await service.list_sessions("alice", "local")
        assert len(alice_sessions) == 2
        assert all(s.user_id == "alice" for s in alice_sessions)

        bob_sessions = await service.list_sessions("bob", "local")
        assert len(bob_sessions) == 1

    @pytest.mark.asyncio
    async def test_list_sessions_ordered_by_updated_at_desc(self, service) -> None:
        s1 = await service.create_session("alice", "First", "local")
        await service.create_session("alice", "Second", "local")
        # Add a message to s1 to update its updated_at
        await service.add_message(s1.id, "user", "hello", writer_principal="route_user_message")

        sessions = await service.list_sessions("alice", "local")
        # s1 should be first (most recently updated)
        assert sessions[0].id == s1.id

    @pytest.mark.asyncio
    async def test_archive_session_deletes_unrun_session(self, service) -> None:
        session = await service.create_session("alice", "To Archive", "local")
        await service.add_message(session.id, "user", "hello", writer_principal="route_user_message")
        await service.archive_session(session.id)

        with pytest.raises(ValueError):
            await service.get_session(session.id)

        messages = await service.get_messages(session.id)
        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_physical_archive_consumes_exact_archive_context_without_release(self, engine) -> None:
        service, authority = _service_with_recording_authority(engine)
        session = await service.create_session("alice", "Consume Archive", "local")

        await service.archive_session(session.id)

        archive_acquire_calls = [call for call in authority.acquire_calls if call["operation_kind"] is SessionOperationKind.ARCHIVE]
        assert archive_acquire_calls == [
            {
                "session_id": session.id,
                "operation_kind": SessionOperationKind.ARCHIVE,
                "owner_instance_id": "test-archive-owner",
                "lease_seconds": 30,
            }
        ]
        assert len(authority.archive_delete_calls) == 1
        consumed_context = authority.archive_delete_calls[0]
        assert consumed_context.operation_kind is SessionOperationKind.ARCHIVE
        assert consumed_context.fence.session_id == str(session.id)
        assert authority.release_calls == []
        with engine.connect() as conn:
            assert conn.execute(select(sessions_table.c.id).where(sessions_table.c.id == str(session.id))).first() is None
            assert (
                conn.execute(
                    select(session_operation_fences_table.c.session_id).where(
                        session_operation_fences_table.c.session_id == str(session.id)
                    )
                ).first()
                is None
            )

    @pytest.mark.asyncio
    async def test_archive_missing_session_uses_public_not_found_contract(self, service) -> None:
        missing = uuid.uuid4()

        with pytest.raises(ValueError, match=f"Session not found: {missing}"):
            await service.archive_session(missing)

    @pytest.mark.asyncio
    async def test_soft_archive_releases_exact_archive_context_and_retains_session_and_fence(
        self,
        engine,
        session_operation_contexts,
    ) -> None:
        service, authority = _service_with_recording_authority(engine)
        session = await service.create_session("alice", "Soft Archive", "local")
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        execute_context = session_operation_contexts.acquire(service, session.id)
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        await service.archive_session(session.id)

        archive_acquire_calls = [call for call in authority.acquire_calls if call["operation_kind"] is SessionOperationKind.ARCHIVE]
        assert len(archive_acquire_calls) == 1
        assert authority.archive_delete_calls == []
        archive_release_calls = [context for context in authority.release_calls if context.operation_kind is SessionOperationKind.ARCHIVE]
        assert len(archive_release_calls) == 1
        released_context = archive_release_calls[0]
        assert released_context.operation_kind is SessionOperationKind.ARCHIVE
        assert released_context.fence.session_id == str(session.id)
        with engine.connect() as conn:
            retained = conn.execute(select(sessions_table).where(sessions_table.c.id == str(session.id))).one()
            fence = conn.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
            ).one()
        assert retained.archived_at is not None
        assert fence.released_at is not None

    @pytest.mark.asyncio
    async def test_active_guided_archive_guard_releases_current_archive_context(self, engine) -> None:
        service, authority = _service_with_recording_authority(engine)
        session = await service.create_session("alice", "Active Guided", "local")
        compose_context = authority._delegate.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="test-guided-owner",
            lease_seconds=300,
        )
        await service.reserve_guided_operation(
            session_id=session.id,
            operation_id=str(uuid.uuid4()),
            kind="guided_start",
            request_hash="a" * 64,
            actor="composer_route",
            lease_seconds=300,
            session_operation_context=compose_context,
        )
        authority._delegate.release(compose_context)

        with pytest.raises(SessionGuidedOperationInProgressError):
            await service.archive_session(session.id)

        assert len(authority.acquire_calls) == 1
        assert authority.acquire_calls[0]["operation_kind"] is SessionOperationKind.ARCHIVE
        assert authority.archive_delete_calls == []
        assert len([context for context in authority.release_calls if context.operation_kind is SessionOperationKind.ARCHIVE]) == 1
        assert authority.release_calls[0].operation_kind is SessionOperationKind.ARCHIVE
        assert authority.release_calls[0].fence.session_id == str(session.id)
        with engine.connect() as conn:
            fence = conn.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session.id))
            ).one()
        assert fence.released_at is not None

    @pytest.mark.asyncio
    async def test_archive_session_hides_session_with_durable_completion_history(self, engine, service) -> None:
        session = await service.create_session("alice", "To Archive", "local")
        await service.add_message(session.id, "user", "hello", writer_principal="route_user_message")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        with engine.begin() as conn:
            conn.execute(
                insert(composer_completion_events_table).values(
                    id=str(uuid.uuid4()),
                    session_id=str(session.id),
                    composition_state_id=str(state.id),
                    event_type="export_yaml",
                    actor="user:alice",
                    created_at=datetime.now(UTC),
                )
            )

        await service.archive_session(session.id)

        archived = await service.get_session(session.id)
        assert archived.archived_at is not None

        visible_sessions = await service.list_sessions("alice", "local")
        assert [s.id for s in visible_sessions] == []

        archived_sessions = await service.list_sessions("alice", "local", include_archived=True)
        assert [s.id for s in archived_sessions] == [session.id]

        messages = await service.get_messages(session.id)
        assert len(messages) == 1
        with engine.begin() as conn:
            remaining_completion_events = conn.execute(
                select(composer_completion_events_table).where(composer_completion_events_table.c.session_id == str(session.id))
            ).all()
        assert len(remaining_completion_events) == 1

    @pytest.mark.asyncio
    async def test_soft_archive_with_data_dir_preserves_blob_bytes_and_creates_no_quarantine(
        self,
        engine,
        tmp_path,
        session_operation_contexts,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Soft archive with blobs", "local")
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        execute_context = session_operation_contexts.acquire(service, session.id)
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "keep.csv"
        blob.write_bytes(b"value\n")

        await service.archive_session(session.id)

        assert blob.read_bytes() == b"value\n"
        assert not (data_dir / ".archive_quarantine").exists()
        assert authority.archive_delete_calls == []
        assert len([context for context in authority.release_calls if context.operation_kind is SessionOperationKind.ARCHIVE]) == 1

    @pytest.mark.asyncio
    async def test_physical_archive_uses_manifest_and_exact_authority_checkpoints(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Durable archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        (blob_dir / "payload.csv").write_bytes(b"row\n")

        def record_prepare(*args: Any, **kwargs: Any):
            authority.events.append("prepare")
            return prepare_archive_quarantine(*args, **kwargs)

        def record_stage(*args: Any, **kwargs: Any) -> None:
            identity = args[1]
            assert archive_quarantine_paths(data_dir, identity).manifest.is_file()
            authority.events.append("stage")
            stage_archive_quarantine(*args, **kwargs)

        def record_purge(*args: Any, **kwargs: Any) -> None:
            authority.events.append("purge")
            purge_archive_quarantine(*args, **kwargs)

        def record_retire(*args: Any, **kwargs: Any) -> None:
            authority.events.append("retire")
            retire_archive_quarantine(*args, **kwargs)

        monkeypatch.setattr(service_module, "prepare_archive_quarantine", record_prepare, raising=False)
        monkeypatch.setattr(service_module, "stage_archive_quarantine", record_stage, raising=False)
        monkeypatch.setattr(service_module, "purge_archive_quarantine", record_purge, raising=False)
        monkeypatch.setattr(service_module, "retire_archive_quarantine", record_retire, raising=False)

        await service.archive_session(session.id)

        assert authority.events == [
            "mutate",
            "compare_and_swap",
            "prepare",
            "compare_and_swap",
            "compare_and_swap",
            "stage",
            "compare_and_swap",
            "compare_and_swap",
            "archive_delete",
            "purge",
            "retire",
        ]
        assert not blob_dir.exists()
        assert list_archive_quarantine_manifests(data_dir, session.id) == ()
        assert authority.release_calls == []

    @pytest.mark.asyncio
    async def test_physical_archive_prepare_failure_does_not_delete_or_touch_canonical(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Prepare failure", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "payload.csv"
        blob.write_bytes(b"row\n")

        def fail_prepare(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("injected prepare failure")

        monkeypatch.setattr(service_module, "prepare_archive_quarantine", fail_prepare, raising=False)

        with pytest.raises(OSError, match="injected prepare failure"):
            await service.archive_session(session.id)

        assert blob.read_bytes() == b"row\n"
        assert authority.archive_delete_calls == []
        assert len(authority.release_calls) == 1

    @pytest.mark.asyncio
    async def test_physical_archive_current_reconciliation_restores_and_retires_before_release(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        authority.archive_delete_error = RuntimeError("injected database failure")
        session = await service.create_session("alice", "Rollback archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "payload.csv"
        blob.write_bytes(b"row\n")

        def record_restore(*args: Any, **kwargs: Any) -> None:
            authority.events.append("restore")
            restore_archive_quarantine(*args, **kwargs)

        def record_retire(*args: Any, **kwargs: Any) -> None:
            authority.events.append("retire")
            retire_archive_quarantine(*args, **kwargs)

        monkeypatch.setattr(service_module, "restore_archive_quarantine", record_restore, raising=False)
        monkeypatch.setattr(service_module, "retire_archive_quarantine", record_retire, raising=False)

        with pytest.raises(RuntimeError, match="injected database failure"):
            await service.archive_session(session.id)

        assert blob.read_bytes() == b"row\n"
        assert list_archive_quarantine_manifests(data_dir, session.id) == ()
        assert authority.events[-6:] == [
            "reconcile_archive_delete",
            "compare_and_swap",
            "restore",
            "compare_and_swap",
            "retire",
            "release",
        ]

    @pytest.mark.asyncio
    async def test_unknown_archive_outcome_preserves_exact_quarantine_obligation(
        self,
        engine,
        tmp_path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        authority.archive_delete_error = RuntimeError("secret primary detail")
        authority.reconcile_archive_delete_error = OSError("secret reconciliation detail")
        session = await service.create_session("alice", "Unknown archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        (blob_dir / "payload.csv").write_bytes(b"row\n")

        with pytest.raises(SessionOperationTerminalOutcomeUnknown) as exc_info:
            await service.archive_session(session.id)

        assert "secret" not in str(exc_info.value)
        manifests = list_archive_quarantine_manifests(data_dir, session.id)
        assert len(manifests) == 1
        paths = archive_quarantine_paths(data_dir, manifests[0].identity)
        assert paths.payload.is_dir()
        assert (paths.payload / "payload.csv").read_bytes() == b"row\n"
        assert not blob_dir.exists()
        assert authority.release_calls == []

    @pytest.mark.asyncio
    async def test_physical_archive_adopts_single_stale_payload_before_new_archive(
        self,
        engine,
        tmp_path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Adopt archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        (blob_dir / "payload.csv").write_bytes(b"row\n")
        stale_identity = ArchiveQuarantineIdentity(
            session_id=session.id,
            operation_id=uuid.uuid4(),
            operation_epoch=1,
        )
        prepare_archive_quarantine(data_dir, stale_identity, source_present=True)
        stage_archive_quarantine(data_dir, stale_identity, blob_dir)

        await service.archive_session(session.id)

        assert len(authority.classify_archive_manifest_calls) == 1
        assert authority.classify_archive_manifest_calls[0][1:] == (
            stale_identity.operation_id,
            stale_identity.operation_epoch,
        )
        assert list_archive_quarantine_manifests(data_dir, session.id) == ()
        assert not blob_dir.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("residue", ["prepared-source", "absent-source"])
    async def test_physical_archive_retires_safe_stale_manifest_without_payload(
        self,
        engine,
        tmp_path,
        residue: str,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Retire stale archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        source_present = residue == "prepared-source"
        if source_present:
            blob_dir.mkdir(parents=True)
            (blob_dir / "payload.csv").write_bytes(b"row\n")
        stale_identity = ArchiveQuarantineIdentity(
            session_id=session.id,
            operation_id=uuid.uuid4(),
            operation_epoch=1,
        )
        prepare_archive_quarantine(
            data_dir,
            stale_identity,
            source_present=source_present,
        )

        await service.archive_session(session.id)

        assert len(authority.classify_archive_manifest_calls) == 1
        assert list_archive_quarantine_manifests(data_dir, session.id) == ()
        assert not blob_dir.exists()
        assert len(authority.archive_delete_calls) == 1

    @pytest.mark.asyncio
    async def test_physical_archive_fails_closed_on_stale_payload_and_canonical_collision(
        self,
        engine,
        tmp_path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Collision archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        (blob_dir / "canonical.csv").write_bytes(b"canonical\n")
        stale_identity = ArchiveQuarantineIdentity(
            session_id=session.id,
            operation_id=uuid.uuid4(),
            operation_epoch=1,
        )
        prepare_archive_quarantine(data_dir, stale_identity, source_present=True)
        stale_paths = archive_quarantine_paths(data_dir, stale_identity)
        stale_paths.payload.mkdir()
        (stale_paths.payload / "stale.csv").write_bytes(b"stale\n")

        with pytest.raises(ArchiveQuarantineCollisionError):
            await service.archive_session(session.id)

        assert (blob_dir / "canonical.csv").read_bytes() == b"canonical\n"
        assert (stale_paths.payload / "stale.csv").read_bytes() == b"stale\n"
        assert authority.archive_delete_calls == []
        assert len(authority.release_calls) == 1

    @pytest.mark.asyncio
    async def test_physical_archive_cancellation_joins_stage_then_restores_before_release(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Cancelled archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "payload.csv"
        blob.write_bytes(b"row\n")
        stage_started = threading.Event()
        stage_allowed = threading.Event()
        stage_finished = threading.Event()

        def blocked_stage(*args: Any, **kwargs: Any) -> None:
            stage_archive_quarantine(*args, **kwargs)
            stage_started.set()
            try:
                assert stage_allowed.wait(timeout=5)
            finally:
                stage_finished.set()

        monkeypatch.setattr(service_module, "stage_archive_quarantine", blocked_stage)

        archive_task = asyncio.create_task(service.archive_session(session.id))
        assert await asyncio.to_thread(stage_started.wait, 5)
        archive_task.cancel("client disconnected")
        stage_allowed.set()

        with pytest.raises(asyncio.CancelledError, match="client disconnected"):
            await archive_task

        assert stage_finished.is_set()
        assert blob.read_bytes() == b"row\n"
        assert list_archive_quarantine_manifests(data_dir, session.id) == ()
        assert authority.archive_delete_calls == []
        assert len(authority.release_calls) == 1
        assert authority.events[-1] == "release"

    @pytest.mark.asyncio
    async def test_repeated_cancellation_joins_stage_before_restore_and_preserves_first_cancel(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Repeatedly cancelled archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "payload.csv"
        blob.write_bytes(b"row\n")
        stage_started = threading.Event()
        stage_allowed = threading.Event()
        stage_finished = threading.Event()
        restore_started = threading.Event()
        restore_after_stage: list[bool] = []

        def blocked_stage(*args: Any, **kwargs: Any) -> None:
            stage_archive_quarantine(*args, **kwargs)
            stage_started.set()
            try:
                assert stage_allowed.wait(timeout=5)
            finally:
                stage_finished.set()

        def record_restore(*args: Any, **kwargs: Any) -> None:
            restore_after_stage.append(stage_finished.is_set())
            restore_started.set()
            restore_archive_quarantine(*args, **kwargs)

        monkeypatch.setattr(service_module, "stage_archive_quarantine", blocked_stage)
        monkeypatch.setattr(service_module, "restore_archive_quarantine", record_restore)

        archive_task = asyncio.create_task(service.archive_session(session.id))
        assert await asyncio.to_thread(stage_started.wait, 5)
        assert archive_task.cancel("first exact cancellation")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert archive_task.cancel("second cancellation")
        restore_overlapped_stage = await asyncio.to_thread(restore_started.wait, 0.25)
        stage_allowed.set()

        with pytest.raises(asyncio.CancelledError, match="first exact cancellation"):
            await archive_task

        assert not restore_overlapped_stage
        assert stage_finished.is_set()
        assert restore_after_stage == [True]
        assert not any(task.get_name().startswith("session-archive-quarantine-stage") and not task.done() for task in asyncio.all_tasks())
        assert blob.read_bytes() == b"row\n"
        assert list_archive_quarantine_manifests(data_dir, session.id) == ()
        assert authority.archive_delete_calls == []
        assert len(authority.release_calls) == 1
        assert authority.events[-1] == "release"

    @pytest.mark.asyncio
    async def test_renewal_loss_joins_stage_worker_and_surfaces_exact_lease_error(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(
            engine,
            data_dir=data_dir,
            lease_seconds=1,
        )
        renewal_error = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
        authority.renew_error = renewal_error
        authority.acquire_lease_seconds_override = 30
        session = await service.create_session("alice", "Renewal-lost archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        (blob_dir / "payload.csv").write_bytes(b"row\n")
        stage_started = threading.Event()
        stage_allowed = threading.Event()
        stage_finished = threading.Event()
        archive_returned = threading.Event()
        cancelling_at_return: list[int] = []
        stage_finished_at_return: list[bool] = []

        def blocked_stage(*args: Any, **kwargs: Any) -> None:
            stage_archive_quarantine(*args, **kwargs)
            stage_started.set()
            try:
                assert stage_allowed.wait(timeout=5)
            finally:
                stage_finished.set()

        authority.renew_wait_for = stage_started
        monkeypatch.setattr(service_module, "stage_archive_quarantine", blocked_stage)

        archive_task = asyncio.create_task(service.archive_session(session.id))

        def record_return(task: asyncio.Task[None]) -> None:
            cancelling_at_return.append(task.cancelling())
            stage_finished_at_return.append(stage_finished.is_set())
            archive_returned.set()

        archive_task.add_done_callback(record_return)
        assert await asyncio.to_thread(stage_started.wait, 5)
        assert await asyncio.to_thread(authority.renew_started.wait, 5)
        returned_before_stage_release = await asyncio.to_thread(archive_returned.wait, 0.25)
        stage_allowed.set()
        try:
            await archive_task
        except BaseException as error:
            observed_error = error
        else:
            pytest.fail("archive unexpectedly succeeded after renewal loss")

        assert not returned_before_stage_release
        assert stage_finished.is_set()
        assert stage_finished_at_return == [True]
        assert cancelling_at_return == [0]
        assert observed_error is renewal_error
        assert type(observed_error) is SessionOperationFenceLost
        assert observed_error.reason is FenceLossReason.LEASE_EXPIRED
        assert not isinstance(observed_error, asyncio.CancelledError)
        assert not any(task.get_name().startswith("session-archive-quarantine-stage") and not task.done() for task in asyncio.all_tasks())
        assert not blob_dir.exists()
        manifests = list_archive_quarantine_manifests(data_dir, session.id)
        assert len(manifests) == 1
        obligation = archive_quarantine_paths(data_dir, manifests[0].identity)
        assert (obligation.payload / "payload.csv").read_bytes() == b"row\n"
        assert authority.archive_delete_calls == []

    @pytest.mark.asyncio
    async def test_renewal_loss_joins_decision_worker_and_surfaces_exact_lease_error(
        self,
        engine,
        tmp_path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(
            engine,
            data_dir=data_dir,
            lease_seconds=1,
        )
        renewal_error = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
        authority.renew_error = renewal_error
        authority.acquire_lease_seconds_override = 30
        authority.renew_wait_for = authority.mutate_started
        authority.mutate_blocked = True
        authority.mutate_allowed.clear()
        session = await service.create_session("alice", "Renewal-lost decision", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "payload.csv"
        blob.write_bytes(b"row\n")
        archive_returned = threading.Event()
        cancelling_at_return: list[int] = []
        worker_finished_at_return: list[bool] = []

        archive_task = asyncio.create_task(service.archive_session(session.id))

        def record_return(task: asyncio.Task[None]) -> None:
            cancelling_at_return.append(task.cancelling())
            worker_finished_at_return.append(authority.mutate_finished.is_set())
            archive_returned.set()

        archive_task.add_done_callback(record_return)
        assert await asyncio.to_thread(authority.mutate_started.wait, 5)
        assert await asyncio.to_thread(authority.renew_started.wait, 5)
        returned_before_worker_release = await asyncio.to_thread(archive_returned.wait, 0.25)
        authority.mutate_allowed.set()
        try:
            await archive_task
        except BaseException as error:
            observed_error = error
        else:
            pytest.fail("archive unexpectedly succeeded after decision renewal loss")

        assert not returned_before_worker_release
        assert authority.mutate_finished.is_set()
        assert worker_finished_at_return == [True]
        assert cancelling_at_return == [0]
        assert observed_error is renewal_error
        assert type(observed_error) is SessionOperationFenceLost
        assert observed_error.reason is FenceLossReason.LEASE_EXPIRED
        assert not isinstance(observed_error, asyncio.CancelledError)
        assert not any(task.get_name().startswith("session-archive-decision") and not task.done() for task in asyncio.all_tasks())
        assert blob.read_bytes() == b"row\n"
        assert not (data_dir / ".archive_quarantine").exists()
        assert authority.archive_delete_calls == []

    @pytest.mark.asyncio
    async def test_lease_loss_before_owned_phase_start_surfaces_exact_error_without_coroutine_leak(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        session = await service.create_session("alice", "Pre-start lease loss", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "payload.csv"
        blob.write_bytes(b"row\n")
        renewal_error = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
        original_create_task = service_module.SessionOperationLease.create_task

        def cancel_reconcile_before_start(
            lease: Any,
            coroutine: Any,
            *,
            name: str | None = None,
        ) -> asyncio.Task[Any]:
            task = original_create_task(lease, coroutine, name=name)
            if name == "session-archive-quarantine-reconcile-prior":
                lease._record_renewal_error(renewal_error)
            return task

        monkeypatch.setattr(service_module.SessionOperationLease, "create_task", cancel_reconcile_before_start)

        with warnings.catch_warnings(record=True) as captured_warnings:
            warnings.simplefilter("always", RuntimeWarning)
            try:
                await service.archive_session(session.id)
            except BaseException as error:
                observed_error = error
            else:
                pytest.fail("archive unexpectedly succeeded after pre-start lease loss")
            await asyncio.sleep(0)
            gc.collect()

        assert observed_error is renewal_error
        assert type(observed_error) is SessionOperationFenceLost
        assert observed_error.reason is FenceLossReason.LEASE_EXPIRED
        assert not isinstance(observed_error, asyncio.CancelledError)
        assert not any("was never awaited" in str(warning.message) for warning in captured_warnings)
        assert not any(
            task.get_name().startswith("session-archive-quarantine-reconcile-prior") and not task.done() for task in asyncio.all_tasks()
        )
        assert blob.read_bytes() == b"row\n"
        assert not (data_dir / ".archive_quarantine").exists()
        assert authority.archive_delete_calls == []

    @pytest.mark.asyncio
    async def test_post_stage_fence_loss_preserves_payload_obligation_without_cleanup(
        self,
        engine,
        tmp_path,
    ) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        service, authority = _service_with_recording_authority(engine, data_dir=data_dir)
        authority.compare_and_swap_errors[4] = SessionOperationFenceLost(FenceLossReason.STALE_EPOCH)
        session = await service.create_session("alice", "Lost archive", "local")
        blob_dir = data_dir / "blobs" / str(session.id)
        blob_dir.mkdir(parents=True)
        (blob_dir / "payload.csv").write_bytes(b"row\n")

        with pytest.raises(SessionOperationFenceLost) as exc_info:
            await service.archive_session(session.id)

        assert exc_info.value.reason is FenceLossReason.STALE_EPOCH
        assert authority.archive_delete_calls == []
        manifests = list_archive_quarantine_manifests(data_dir, session.id)
        assert len(manifests) == 1
        obligation = archive_quarantine_paths(data_dir, manifests[0].identity)
        assert not blob_dir.exists()
        assert (obligation.payload / "payload.csv").read_bytes() == b"row\n"

    @pytest.mark.asyncio
    async def test_archive_session_deletes_blob_directory(self, engine, tmp_path) -> None:
        """Archiving a session removes its blob directory from the filesystem."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        service_with_dir = SessionServiceImpl(
            engine,
            data_dir=data_dir,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test"),
        )

        session = await service_with_dir.create_session("alice", "Blob Session", "local")
        sid = str(session.id)

        # Create blob directory with a file (simulating stored blobs)
        blob_dir = data_dir / "blobs" / sid
        blob_dir.mkdir(parents=True)
        (blob_dir / "some-blob_data.csv").write_text("col1\nval1")
        assert blob_dir.is_dir()

        await service_with_dir.archive_session(session.id)

        # Blob directory should be cleaned up
        assert not blob_dir.exists()

        # Session should be gone
        with pytest.raises(ValueError):
            await service_with_dir.get_session(session.id)

    @pytest.mark.asyncio
    async def test_archive_session_preserves_quarantine_and_raises_when_post_commit_purge_fails(
        self,
        engine,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Post-commit purge failure preserves one sanitized obligation."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        service_with_dir, authority = _service_with_recording_authority(engine, data_dir=data_dir)

        session = await service_with_dir.create_session("alice", "Blob Session", "local")
        sid = str(session.id)
        blob_dir = data_dir / "blobs" / sid
        blob_dir.mkdir(parents=True)
        blob_file = blob_dir / "some-blob_data.csv"
        blob_file.write_text("col1\nval1")

        def fail_purge(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("permission denied removing staged blob directory")

        monkeypatch.setattr(service_module, "purge_archive_quarantine", fail_purge, raising=False)

        with pytest.raises(QuarantineCleanupError) as exc_info:
            await service_with_dir.archive_session(session.id)
        rendered = "\n".join(
            [
                str(exc_info.value),
                *(exc_info.value.__notes__ if hasattr(exc_info.value, "__notes__") else []),
                "".join(traceback.format_exception(exc_info.value)),
            ]
        )
        assert "permission denied" not in rendered
        assert str(data_dir) not in rendered
        assert exc_info.value.__cause__ is None

        with pytest.raises(ValueError):
            await service_with_dir.get_session(session.id)

        assert not blob_dir.exists()
        manifests = list_archive_quarantine_manifests(data_dir, session.id)
        assert len(manifests) == 1
        obligation = archive_quarantine_paths(data_dir, manifests[0].identity)
        assert obligation.payload.is_dir()
        assert (obligation.payload / blob_file.name).read_text() == "col1\nval1"
        assert authority.release_calls == []


class TestRunEvents:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_run_id_kind", ("canonical_string", "bool", "object"))
    async def test_append_run_event_rejects_non_exact_uuid_before_database_work(
        self,
        service,
        engine,
        session_operation_contexts,
        invalid_run_id_kind: str,
    ) -> None:
        session = await service.create_session("alice", "Invalid event run id", "local")
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(
            session.id,
            state.id,
            session_operation_context=execute_context,
        )
        invalid_run_id: object = {
            "canonical_string": str(run.id),
            "bool": True,
            "object": object(),
        }[invalid_run_id_kind]
        statements: list[str] = []

        def capture_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            with pytest.raises(TypeError, match="run_id must be an exact UUID"):
                await service.append_run_event(
                    run_id=invalid_run_id,  # type: ignore[arg-type]
                    timestamp=datetime.now(UTC),
                    event_type="progress",
                    data={},
                    session_operation_context=execute_context,
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

        assert statements == []
        with engine.connect() as conn:
            event_count = conn.execute(select(func.count()).select_from(run_events_table)).scalar_one()
        assert event_count == 0

    @pytest.mark.asyncio
    async def test_append_run_event_rejects_naive_timestamp_before_write(
        self,
        service,
        engine,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Naive event", "local")
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(
            session.id,
            state.id,
            session_operation_context=execute_context,
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            await service.append_run_event(
                run_id=run.id,
                timestamp=datetime.now(UTC).replace(tzinfo=None),
                event_type="progress",
                data={},
                session_operation_context=execute_context,
            )

        with engine.connect() as conn:
            assert conn.execute(select(run_events_table.c.id).where(run_events_table.c.run_id == str(run.id))).first() is None

    @pytest.mark.asyncio
    async def test_append_and_list_run_events_preserves_order_and_payload(
        self,
        service,
        engine,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Run events", "local")
        session_id = session.id
        state_id = uuid.uuid4()
        run_id = uuid.uuid4()
        created_at = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                insert(composition_states_table).values(
                    id=str(state_id),
                    session_id=str(session_id),
                    version=1,
                    is_valid=True,
                    provenance="session_seed",
                    created_at=created_at,
                )
            )
            conn.execute(
                insert(runs_table).values(
                    id=str(run_id),
                    session_id=str(session_id),
                    state_id=str(state_id),
                    status="running",
                    started_at=created_at,
                    rows_processed=0,
                    rows_failed=0,
                )
            )

        execute_context = session_operation_contexts.acquire(service, session_id)
        await service.append_run_event(
            run_id=run_id,
            timestamp=created_at,
            event_type="progress",
            data={"source_rows_processed": 1},
            session_operation_context=execute_context,
        )
        await service.append_run_event(
            run_id=run_id,
            timestamp=created_at,
            event_type="error",
            data={"message": "bad row", "node_id": None, "row_id": None},
            session_operation_context=execute_context,
        )
        await service.append_run_event(
            run_id=run_id,
            timestamp=created_at,
            event_type="failed",
            data={"status": "failed", "detail": "boom", "node_id": None},
            session_operation_context=execute_context,
        )

        records = await service.list_run_events(run_id)

        assert [record.event_type for record in records] == ["progress", "error", "failed"]
        assert [record.sequence for record in records] == [1, 2, 3]
        assert records[0].data == {"source_rows_processed": 1}
        assert records[1].data["message"] == "bad row"
        assert records[2].data["detail"] == "boom"
        with engine.connect() as conn:
            assert conn.execute(select(run_events_table.c.id)).fetchall()


class TestMessagePersistence:
    """Tests for chat message add and retrieval."""

    @pytest.mark.asyncio
    async def test_add_and_get_messages(self, service) -> None:
        session = await service.create_session("alice", "Chat", "local")
        msg1 = await service.add_message(session.id, "user", "Hello", writer_principal="route_user_message")
        await service.add_message(session.id, "assistant", "Hi there", writer_principal="compose_loop")

        assert isinstance(msg1, ChatMessageRecord)
        assert msg1.role == "user"
        assert msg1.content == "Hello"

        messages = await service.get_messages(session.id)
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_messages_ordered_by_created_at_asc(self, service) -> None:
        session = await service.create_session("alice", "Chat", "local")
        await service.add_message(session.id, "user", "First", writer_principal="route_user_message")
        await service.add_message(session.id, "assistant", "Second", writer_principal="compose_loop")
        await service.add_message(session.id, "user", "Third", writer_principal="route_user_message")

        messages = await service.get_messages(session.id)
        assert [m.content for m in messages] == ["First", "Second", "Third"]

    @pytest.mark.asyncio
    async def test_add_message_with_tool_calls(self, service) -> None:
        session = await service.create_session("alice", "Chat", "local")
        tool_calls_data = [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "set_source",
                    "arguments": '{"type":"csv"}',
                },
            }
        ]
        msg = await service.add_message(
            session.id,
            "assistant",
            "Setting source",
            tool_calls=tool_calls_data,
            writer_principal="compose_loop",
        )
        assert msg.tool_calls is not None

    @pytest.mark.asyncio
    async def test_add_message_updates_session_updated_at(self, service) -> None:
        session = await service.create_session("alice", "Chat", "local")
        original_updated = session.updated_at.replace(tzinfo=None)
        await service.add_message(session.id, "user", "hello", writer_principal="route_user_message")
        refreshed = await service.get_session(session.id)
        # SQLite strips timezone info; compare naive datetimes (both are UTC)
        refreshed_updated = refreshed.updated_at.replace(tzinfo=None)
        assert refreshed_updated >= original_updated


class TestCompositionStateVersioning:
    """Tests for immutable state snapshots with monotonic versioning."""

    @pytest.mark.asyncio
    async def test_first_state_version_is_1(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state_data = CompositionStateData(is_valid=False)
        state = await service.save_composition_state(session.id, state_data, provenance="session_seed")
        assert isinstance(state, CompositionStateRecord)
        assert state.version == 1
        # New states (not reverts) have no lineage (D2/D7)
        assert state.derived_from_state_id is None

    @pytest.mark.asyncio
    async def test_version_increments_monotonically(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        s1 = await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        s2 = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        assert s1.version == 1
        assert s2.version == 2

    @pytest.mark.asyncio
    async def test_get_current_state_returns_highest_version(
        self,
        service,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        await service.save_composition_state(
            session.id,
            CompositionStateData(
                source={"type": "csv", "path": "old.csv"},
                is_valid=False,
            ),
            provenance="session_seed",
        )
        await service.save_composition_state(
            session.id,
            CompositionStateData(
                source={"type": "csv", "path": "new.csv"},
                is_valid=True,
            ),
            provenance="session_seed",
        )
        current = await service.get_current_state(session.id)
        assert current is not None
        assert current.version == 2
        assert current.is_valid is True

    @pytest.mark.asyncio
    async def test_named_sources_round_trip_through_session_state(self, service) -> None:
        session = await service.create_session("alice", "Multi-source", "local")
        sources = {
            "orders": {
                "plugin": "csv",
                "on_success": "orders_out",
                "options": {"path": "orders.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_out",
                "options": {"path": "refunds.csv", "schema": {"mode": "observed"}},
                "on_validation_failure": "quarantine",
            },
        }

        await service.save_composition_state(
            session.id,
            CompositionStateData(
                sources=sources,
                outputs=[
                    {"name": "orders_out", "plugin": "json", "options": {"path": "orders.jsonl"}, "on_write_failure": "discard"},
                    {"name": "refunds_out", "plugin": "json", "options": {"path": "refunds.jsonl"}, "on_write_failure": "discard"},
                ],
                metadata_={"name": "Multi-source", "description": ""},
                is_valid=True,
            ),
            provenance="session_seed",
        )

        current = await service.get_current_state(session.id)

        assert current is not None
        assert current.sources == sources

    @pytest.mark.asyncio
    async def test_get_current_state_returns_none_when_empty(
        self,
        service,
    ) -> None:
        session = await service.create_session("alice", "Empty", "local")
        current = await service.get_current_state(session.id)
        assert current is None

    @pytest.mark.asyncio
    async def test_composer_meta_roundtrips_through_persistence(
        self,
        service,
    ) -> None:
        """``composer_meta`` survives DB roundtrip and reaches state record.

        Regression for the ``state.composer_meta.repair_turns_used`` surface
        — the convergence-suite eval scorer reads this field via
        ``GET /api/sessions/{id}/state``. If the DB column is dropped or the
        envelope wrap/unwrap is misaligned, scoring silently ambers.
        """
        session = await service.create_session("alice", "Pipeline", "local")
        state_data = CompositionStateData(
            is_valid=True,
            composer_meta={"repair_turns_used": 1},
        )
        saved = await service.save_composition_state(session.id, state_data, provenance="session_seed")
        assert saved.composer_meta is not None
        assert saved.composer_meta["repair_turns_used"] == 1

        # Load via a different code path (get_current_state hits
        # _row_to_state_record / _unwrap_envelope) to prove the value survives
        # the JSON envelope wrap and unwrap.
        loaded = await service.get_current_state(session.id)
        assert loaded is not None
        assert loaded.composer_meta is not None
        assert loaded.composer_meta["repair_turns_used"] == 1

    @pytest.mark.asyncio
    async def test_composer_meta_absent_persists_as_none(
        self,
        service,
    ) -> None:
        """``composer_meta`` defaulting to ``None`` round-trips as ``None``.

        Honest absence: revert/fork paths and historical pre-plumbing rows
        must not synthesise a fake ``repair_turns_used: 0``. The eval scorer
        relies on this distinction (absent => AMBER with explanation, not
        silent pass).
        """
        session = await service.create_session("alice", "Pipeline", "local")
        state_data = CompositionStateData(is_valid=True)
        saved = await service.save_composition_state(session.id, state_data, provenance="session_seed")
        assert saved.composer_meta is None

        loaded = await service.get_current_state(session.id)
        assert loaded is not None
        assert loaded.composer_meta is None

    @pytest.mark.asyncio
    async def test_get_state_versions_returns_all_ascending(
        self,
        service,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        versions = await service.get_state_versions(session.id)
        assert len(versions) == 3
        assert [v.version for v in versions] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_state_preserves_pipeline_data(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state_data = CompositionStateData(
            source={"type": "csv", "path": "/data/input.csv"},
            nodes=[{"name": "classify", "type": "transform"}],
            edges=[{"from": "source", "to": "classify"}],
            outputs=[{"name": "results", "type": "csv_sink"}],
            metadata_={"pipeline_name": "Test Pipeline"},
            is_valid=True,
            validation_errors=None,
        )
        state = await service.save_composition_state(session.id, state_data, provenance="session_seed")
        assert state.is_valid is True


class TestOneActiveRunEnforcement:
    """Tests for B6 -- one active run per session."""

    @pytest.mark.asyncio
    async def test_second_pending_run_raises(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        # First run should succeed
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        # Second run should fail
        with pytest.raises(RunAlreadyActiveError):
            await service.create_run(session.id, state.id, session_operation_context=execute_context)

    @pytest.mark.asyncio
    async def test_create_run_returns_run_record(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        assert isinstance(run, RunRecord)
        assert run.status == "pending"
        assert run.session_id == session.id
        assert run.state_id == state.id
        assert run.pipeline_yaml is None

    @pytest.mark.asyncio
    async def test_create_run_with_pipeline_yaml(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(
            session.id,
            state.id,
            pipeline_yaml="source:\n  type: csv",
            session_operation_context=execute_context,
        )
        assert run.pipeline_yaml == "source:\n  type: csv"

    @pytest.mark.asyncio
    async def test_completed_run_allows_new_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        # Transition through legal path: pending -> running -> completed
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-complete-1",
            session_operation_context=execute_context,
        )
        # New run should succeed
        run2 = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        assert run2.status == "pending"

    @pytest.mark.asyncio
    async def test_failed_run_allows_new_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        # Transition through legal path: pending -> running -> failed
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(run.id, "failed", error="boom", session_operation_context=execute_context)
        run2 = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        assert run2.status == "pending"

    @pytest.mark.asyncio
    async def test_running_run_blocks_new_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        with pytest.raises(RunAlreadyActiveError):
            await service.create_run(session.id, state.id, session_operation_context=execute_context)


class TestGetState:
    """Tests for get_state -- fetch a specific CompositionStateRecord by UUID."""

    @pytest.mark.asyncio
    async def test_get_state_by_id(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        saved = await service.save_composition_state(
            session.id,
            CompositionStateData(
                source={"type": "csv"},
                is_valid=True,
            ),
            provenance="session_seed",
        )
        fetched = await service.get_state(saved.id)
        assert fetched.id == saved.id
        assert fetched.version == saved.version

    @pytest.mark.asyncio
    async def test_get_state_not_found_raises(self, service) -> None:
        with pytest.raises(ValueError, match="not found"):
            await service.get_state(uuid.uuid4())


class TestGetStateInSession:
    """Tests for get_state_in_session -- scoped read with Tier 1 invariant check.

    Regression guard (P2f): list_session_runs resolves each run's
    state_id without a session-scope check. Migration 007's composite FK
    prevents future cross-session state refs at the schema layer, but
    pre-007 orphans repaired with Variant-A (delete orphans) have no
    runtime defense-in-depth. ``get_state_in_session`` is that
    defense-in-depth.
    """

    @pytest.mark.asyncio
    async def test_returns_record_when_session_matches(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        saved = await service.save_composition_state(
            session.id, CompositionStateData(source={"type": "csv"}, is_valid=True), provenance="session_seed"
        )
        fetched = await service.get_state_in_session(saved.id, session.id)
        assert fetched.id == saved.id
        assert fetched.session_id == session.id

    @pytest.mark.asyncio
    async def test_raises_audit_integrity_error_on_session_mismatch(self, service) -> None:
        """State belongs to session A, caller says it's in session B — Tier 1."""
        from elspeth.contracts.errors import AuditIntegrityError

        session_a = await service.create_session("alice", "Pipeline A", "local")
        session_b = await service.create_session("alice", "Pipeline B", "local")
        state_in_a = await service.save_composition_state(
            session_a.id, CompositionStateData(source={"type": "csv"}, is_valid=True), provenance="session_seed"
        )
        with pytest.raises(AuditIntegrityError, match="Tier 1 audit anomaly"):
            await service.get_state_in_session(state_in_a.id, session_b.id)

    @pytest.mark.asyncio
    async def test_raises_value_error_when_state_missing(self, service) -> None:
        """Nonexistent state_id must still raise ValueError, not AuditIntegrityError.

        Absence is distinguishable from corruption — callers that map to
        404 rely on the exception class to know which is which.
        """
        session = await service.create_session("alice", "Pipeline", "local")
        with pytest.raises(ValueError, match="not found"):
            await service.get_state_in_session(uuid.uuid4(), session.id)


class TestSetActiveState:
    """Tests for set_active_state -- revert by copying a prior version."""

    @pytest.mark.asyncio
    async def test_revert_creates_new_version(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        v1 = await service.save_composition_state(
            session.id, CompositionStateData(source={"type": "csv"}, is_valid=True), provenance="session_seed"
        )
        await service.save_composition_state(
            session.id, CompositionStateData(source={"type": "api"}, is_valid=True), provenance="session_seed"
        )
        # Revert to v1 -- should create v3 as a copy of v1
        reverted = await service.set_active_state(session.id, v1.id)
        assert reverted.version == 3
        # Content should match v1, not v2
        assert reverted.sources == v1.sources
        # Lineage: reverted state records where it came from (D6)
        assert reverted.derived_from_state_id == v1.id

    @pytest.mark.asyncio
    async def test_revert_preserves_named_sources(self, service) -> None:
        session = await service.create_session("alice", "Multi-source", "local")
        sources = {
            "orders": {"plugin": "csv", "on_success": "orders_rows", "on_validation_failure": "discard", "options": {"path": "orders.csv"}},
            "refunds": {
                "plugin": "csv",
                "on_success": "refunds_rows",
                "on_validation_failure": "discard",
                "options": {"path": "refunds.csv"},
            },
        }
        v1 = await service.save_composition_state(
            session.id,
            CompositionStateData(sources=sources, is_valid=True),
            provenance="session_seed",
        )
        await service.save_composition_state(
            session.id,
            CompositionStateData(source={"plugin": "json", "on_success": "rows", "on_validation_failure": "discard", "options": {}}),
            provenance="session_seed",
        )

        reverted = await service.set_active_state(session.id, v1.id)

        assert reverted.sources == sources

    @pytest.mark.asyncio
    async def test_revert_preserves_history(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        v2 = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        await service.set_active_state(session.id, v2.id)
        versions = await service.get_state_versions(session.id)
        # All three versions should exist (v1, v2, v3)
        assert len(versions) == 3
        assert [v.version for v in versions] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_revert_state_not_found_raises(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        with pytest.raises(ValueError, match="not found"):
            await service.set_active_state(session.id, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_revert_state_wrong_session_raises(self, service) -> None:
        s1 = await service.create_session("alice", "Session 1", "local")
        s2 = await service.create_session("alice", "Session 2", "local")
        state = await service.save_composition_state(s1.id, CompositionStateData(is_valid=True), provenance="session_seed")
        with pytest.raises(ValueError, match="does not belong"):
            await service.set_active_state(s2.id, state.id)


class TestGetRun:
    """Tests for get_run -- fetch a RunRecord by UUID."""

    @pytest.mark.asyncio
    async def test_get_run_returns_record(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        created = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        fetched = await service.get_run(created.id)
        assert isinstance(fetched, RunRecord)
        assert fetched.id == created.id
        assert fetched.status == "pending"

    @pytest.mark.asyncio
    async def test_get_run_not_found_raises(self, service) -> None:
        with pytest.raises(ValueError, match="not found"):
            await service.get_run(uuid.uuid4())


class TestGetActiveRun:
    """Tests for get_active_run -- pending/running run for a session."""

    @pytest.mark.asyncio
    async def test_returns_active_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        active = await service.get_active_run(session.id)
        assert active is not None
        assert active.id == run.id

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_run(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        active = await service.get_active_run(session.id)
        assert active is None

    @pytest.mark.asyncio
    async def test_returns_none_after_completion(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-active-none",
            session_operation_context=execute_context,
        )
        active = await service.get_active_run(session.id)
        assert active is None


class TestUpdateRunStatusExpanded:
    """Tests for expanded update_run_status signature (R6)."""

    @pytest.mark.asyncio
    async def test_update_with_error(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "failed",
            error="Source file not found",
            session_operation_context=execute_context,
        )
        fetched = await service.get_run(run.id)
        assert fetched.status == "failed"
        assert fetched.error == "Source file not found"
        assert fetched.finished_at is not None

    @pytest.mark.asyncio
    async def test_update_with_landscape_run_id(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed_with_failures",
            landscape_run_id="lscp-abc-123",
            rows_processed=100,
            rows_succeeded=4,
            rows_routed_success=4,
            rows_routed_failure=0,
            rows_failed=3,
            session_operation_context=execute_context,
        )
        fetched = await service.get_run(run.id)
        assert fetched.status == "completed_with_failures"
        assert fetched.landscape_run_id == "lscp-abc-123"
        assert fetched.rows_processed == 100
        assert fetched.rows_succeeded == 4
        assert fetched.rows_routed_success == 4
        assert fetched.rows_routed_failure == 0
        assert fetched.rows_failed == 3


class TestAdr019LegacyCounterReadCompatibility:
    """Pre-ADR-019 sessions.db rows used disjoint routed/quarantine counters."""

    @staticmethod
    def _accounting_from_run(run: RunRecord) -> RunAccounting:
        return RunAccounting(
            source=RunAccountingSource(rows_processed=run.rows_processed),
            tokens=RunAccountingTokens(
                emitted=run.rows_succeeded + run.rows_failed,
                terminal=run.rows_succeeded + run.rows_failed,
                succeeded=run.rows_succeeded,
                failed=run.rows_failed,
                structural=0,
                pending=0,
            ),
            routing=RunAccountingRouting(
                routed_success=run.rows_routed_success,
                routed_failure=run.rows_routed_failure,
                quarantined=run.rows_quarantined,
                discarded=0,
            ),
            integrity=RunAccountingIntegrity(
                closure="closed",
                missing_terminal_outcomes=0,
                duplicate_terminal_outcomes=0,
            ),
        )

    @staticmethod
    def _status_response_from_run(run: RunRecord) -> RunStatusResponse:
        return RunStatusResponse(
            run_id=str(run.id),
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            accounting=TestAdr019LegacyCounterReadCompatibility._accounting_from_run(run),
            error=run.error,
            landscape_run_id=run.landscape_run_id,
        )

    @pytest.mark.asyncio
    async def test_get_run_normalizes_legacy_gate_routed_success_counter(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-legacy-gate",
            rows_processed=4,
            rows_succeeded=0,
            rows_failed=0,
            rows_routed_success=4,
            rows_routed_failure=0,
            rows_quarantined=0,
            session_operation_context=execute_context,
        )

        fetched = await service.get_run(run.id)

        assert fetched.rows_succeeded == 4
        assert fetched.rows_routed_success == 4
        response = self._status_response_from_run(fetched)
        assert response.status == "completed"

    @pytest.mark.asyncio
    async def test_get_run_normalizes_legacy_quarantine_failure_counter(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed_with_failures",
            landscape_run_id="lscp-legacy-quarantine",
            rows_processed=3,
            rows_succeeded=1,
            rows_failed=0,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=2,
            session_operation_context=execute_context,
        )

        fetched = await service.get_run(run.id)

        assert fetched.rows_failed == 2
        assert fetched.rows_quarantined == 2
        response = self._status_response_from_run(fetched)
        assert response.status == "completed_with_failures"

    @pytest.mark.asyncio
    async def test_get_run_leaves_current_subset_counters_unchanged(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-current-gate",
            rows_processed=4,
            rows_succeeded=4,
            rows_failed=0,
            rows_routed_success=2,
            rows_routed_failure=0,
            rows_quarantined=0,
            session_operation_context=execute_context,
        )

        fetched = await service.get_run(run.id)

        assert fetched.rows_succeeded == 4
        assert fetched.rows_routed_success == 2

    @pytest.mark.asyncio
    async def test_update_not_found_raises(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Missing run", "local")
        execute_context = session_operation_contexts.acquire(service, session.id)
        with pytest.raises(ValueError, match="not found"):
            await service.update_run_status(
                uuid.uuid4(),
                "completed",
                session_operation_context=execute_context,
            )

    @pytest.mark.asyncio
    async def test_completed_requires_landscape_run_id(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        with pytest.raises(ValueError, match="landscape_run_id"):
            await service.update_run_status(run.id, "completed", session_operation_context=execute_context)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed", "completed_with_failures", "empty"])
    async def test_operator_completion_status_requires_landscape_run_id(
        self,
        service,
        session_operation_contexts,
        status,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        with pytest.raises(ValueError, match="landscape_run_id"):
            await service.update_run_status(run.id, status, session_operation_context=execute_context)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["completed_with_failures", "empty"])
    async def test_widened_operator_completion_status_stamps_finished_at(
        self,
        service,
        session_operation_contexts,
        status,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            status,
            landscape_run_id=f"lscp-{status}",
            session_operation_context=execute_context,
        )

        fetched = await service.get_run(run.id)
        assert fetched.status == status
        assert fetched.finished_at is not None
        assert fetched.landscape_run_id == f"lscp-{status}"

    @pytest.mark.asyncio
    async def test_failed_requires_error(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        with pytest.raises(ValueError, match="requires error"):
            await service.update_run_status(run.id, "failed", session_operation_context=execute_context)


class TestRunTransitionEnforcement:
    """Tests for D3 -- LEGAL_RUN_TRANSITIONS enforcement."""

    @pytest.mark.asyncio
    async def test_legal_transition_pending_to_running(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        fetched = await service.get_run(run.id)
        assert fetched.status == "running"

    @pytest.mark.asyncio
    async def test_legal_transition_pending_to_cancelled(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "cancelled", session_operation_context=execute_context)
        fetched = await service.get_run(run.id)
        assert fetched.status == "cancelled"
        assert fetched.finished_at is not None

    @pytest.mark.asyncio
    async def test_illegal_transition_pending_to_completed_raises(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        with pytest.raises(ValueError, match=r"Illegal.*transition"):
            await service.update_run_status(
                run.id,
                "completed",
                landscape_run_id="lscp-illegal",
                session_operation_context=execute_context,
            )

    @pytest.mark.asyncio
    async def test_illegal_transition_completed_to_running_raises(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-finished",
            session_operation_context=execute_context,
        )
        with pytest.raises(ValueError, match=r"Illegal.*transition"):
            await service.update_run_status(run.id, "running", session_operation_context=execute_context)


class TestLandscapeRunIdWriteOnce:
    """Tests for D4 -- landscape_run_id is write-once."""

    @pytest.mark.asyncio
    async def test_set_landscape_run_id(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "running",
            landscape_run_id="lscp-001",
            session_operation_context=execute_context,
        )
        fetched = await service.get_run(run.id)
        assert fetched.landscape_run_id == "lscp-001"

    @pytest.mark.asyncio
    async def test_overwrite_landscape_run_id_raises(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "running",
            landscape_run_id="lscp-001",
            session_operation_context=execute_context,
        )
        with pytest.raises(ValueError, match=r"landscape_run_id.*already set"):
            await service.update_run_status(
                run.id,
                "completed",
                landscape_run_id="lscp-002",
                session_operation_context=execute_context,
            )

    @pytest.mark.asyncio
    async def test_none_landscape_run_id_does_not_overwrite(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "running",
            landscape_run_id="lscp-001",
            session_operation_context=execute_context,
        )
        # Passing None (default) should not trigger the write-once guard
        await service.update_run_status(run.id, "completed", session_operation_context=execute_context)
        fetched = await service.get_run(run.id)
        assert fetched.landscape_run_id == "lscp-001"


class TestCancelOrphanedRuns:
    """Tests for D5 -- cancel_orphaned_runs."""

    @pytest.mark.asyncio
    async def test_cancels_stale_running_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)
        # Cancel with max_age_seconds=0 so ANY running run is considered stale
        cancelled = await service.cancel_orphaned_runs(
            session.id,
            max_age_seconds=0,
        )
        assert len(cancelled) == 1
        assert cancelled[0].id == run.id
        assert cancelled[0].status == "cancelled"

    @pytest.mark.asyncio
    async def test_does_not_cancel_recent_running_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)
        # max_age_seconds=3600 -- run was just created, so not stale
        cancelled = await service.cancel_orphaned_runs(
            session.id,
            max_age_seconds=3600,
        )
        assert len(cancelled) == 0

    @pytest.mark.asyncio
    async def test_does_not_cancel_completed_runs(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-orphan-1",
            session_operation_context=execute_context,
        )
        session_operation_contexts.release(service, execute_context)
        cancelled = await service.cancel_orphaned_runs(
            session.id,
            max_age_seconds=0,
        )
        assert len(cancelled) == 0

    @pytest.mark.asyncio
    async def test_cancel_unblocks_session_for_new_run(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)
        await service.cancel_orphaned_runs(session.id, max_age_seconds=0)
        # Session should now accept a new run
        successor_context = session_operation_contexts.acquire(service, session.id)
        run2 = await service.create_run(session.id, state.id, session_operation_context=successor_context)
        assert run2.status == "pending"

    @pytest.mark.asyncio
    async def test_cancel_includes_pending_orphans(self, service, session_operation_contexts) -> None:
        """A run stuck in 'pending' (crash before transition to running) is also cleaned."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        # Create run that stays in pending (simulates crash before running transition)
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)
        cancelled = await service.cancel_orphaned_runs(session.id, max_age_seconds=0)
        assert len(cancelled) == 1
        assert cancelled[0].status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_does_not_touch_completed_runs(self, service, session_operation_contexts) -> None:
        """Completed runs are never cancelled regardless of age."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-orphan-2",
            session_operation_context=execute_context,
        )
        session_operation_contexts.release(service, execute_context)
        cancelled = await service.cancel_orphaned_runs(session.id, max_age_seconds=0)
        assert len(cancelled) == 0


class TestCancelAllOrphanedRuns:
    """Tests for cancel_all_orphaned_runs (global startup cleanup)."""

    @pytest.mark.asyncio
    async def test_cancels_all_non_terminal_runs_without_age_filter(self, service, session_operation_contexts) -> None:
        """Default (max_age_seconds=None) cancels ALL pending/running runs,
        not just old ones. Critical for single-process server restarts."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        # Create a fresh run (just created, zero age)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        # No age filter — should cancel even a brand-new run
        cancelled = await service.cancel_all_orphaned_runs()
        assert cancelled == 1

        updated = await service.get_run(run.id)
        assert updated.status == "cancelled"

    @pytest.mark.asyncio
    async def test_record_returning_cleanup_preserves_landscape_run_id(self, service, session_operation_contexts) -> None:
        """Startup reconciliation needs cancelled run records to update Landscape."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "running",
            landscape_run_id="lscp-orphan-1",
            session_operation_context=execute_context,
        )
        session_operation_contexts.release(service, execute_context)

        cancelled = await service.cancel_all_orphaned_run_records(
            reason="Orphaned by server restart - no active process",
        )

        assert len(cancelled) == 1
        cancelled_run = cancelled[0]
        assert cancelled_run.id == run.id
        assert cancelled_run.status == "cancelled"
        assert cancelled_run.finished_at is not None
        assert cancelled_run.error == "Orphaned by server restart - no active process"
        assert cancelled_run.landscape_run_id == "lscp-orphan-1"

    @pytest.mark.asyncio
    async def test_cancels_pending_runs_without_age_filter(self, service, session_operation_contexts) -> None:
        """Pending runs (never transitioned to running) are also cancelled."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        cancelled = await service.cancel_all_orphaned_runs()
        assert cancelled == 1

    @pytest.mark.asyncio
    async def test_does_not_cancel_terminal_runs(self, service, session_operation_contexts) -> None:
        """Completed/cancelled/failed runs are never touched."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        await service.update_run_status(
            run.id,
            "completed",
            landscape_run_id="lscp-global-1",
            session_operation_context=execute_context,
        )
        session_operation_contexts.release(service, execute_context)

        cancelled = await service.cancel_all_orphaned_runs()
        assert cancelled == 0

    @pytest.mark.asyncio
    async def test_age_filter_still_works_when_provided(self, service, session_operation_contexts) -> None:
        """When max_age_seconds is given, only old runs are cancelled."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        # Run was just created — 3600s filter should skip it
        cancelled = await service.cancel_all_orphaned_runs(max_age_seconds=3600)
        assert cancelled == 0

    @pytest.mark.asyncio
    async def test_unblocks_session_after_cancellation(self, service, session_operation_contexts) -> None:
        """After cancelling orphaned runs, session can accept new runs."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        await service.cancel_all_orphaned_runs()

        # Session should now be unblocked
        successor_context = session_operation_contexts.acquire(service, session.id)
        run2 = await service.create_run(session.id, state.id, session_operation_context=successor_context)
        assert run2.status == "pending"


class TestLandscapeReconciliationMarkers:
    @staticmethod
    async def _cancelled_run(
        service,
        session_operation_contexts,
        *,
        reason: str,
        landscape_run_id: str | None,
    ) -> RunRecord:
        session = await service.create_session(str(uuid.uuid4()), "Pipeline", "local")
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
        )
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        if landscape_run_id is not None:
            await service.update_run_status(
                run.id,
                "running",
                landscape_run_id=landscape_run_id,
                session_operation_context=execute_context,
            )
        session_operation_contexts.release(service, execute_context)
        cancelled = await service.cancel_all_orphaned_run_records(reason=reason)
        assert len(cancelled) == 1
        return cancelled[0]

    @pytest.mark.asyncio
    async def test_exact_pending_suffix_selection_includes_null_anchor_and_excludes_other_errors(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        pending_null = await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"startup reason {LANDSCAPE_RECONCILIATION_PENDING_SUFFIX}",
            landscape_run_id=None,
        )
        pending_anchor = await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"periodic reason {LANDSCAPE_RECONCILIATION_PENDING_SUFFIX}",
            landscape_run_id="landscape-1",
        )
        await self._cancelled_run(
            service,
            session_operation_contexts,
            reason="ordinary user error",
            landscape_run_id="landscape-2",
        )
        await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"embedded {LANDSCAPE_RECONCILIATION_PENDING_SUFFIX} trailing text",
            landscape_run_id="landscape-3",
        )
        await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"startup reason {LANDSCAPE_RECONCILIATION_COMPLETE_SUFFIX}",
            landscape_run_id="landscape-4",
        )
        await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"startup reason {LANDSCAPE_RECONCILIATION_ABSENT_SUFFIX}",
            landscape_run_id="landscape-5",
        )

        candidates = await service.list_pending_landscape_reconciliations()

        assert {candidate.id for candidate in candidates} == {pending_null.id, pending_anchor.id}
        assert {candidate.landscape_run_id for candidate in candidates} == {None, "landscape-1"}

    @pytest.mark.asyncio
    async def test_outcome_update_is_atomic_exact_and_preserves_reason(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        complete = await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"human readable startup reason {LANDSCAPE_RECONCILIATION_PENDING_SUFFIX}",
            landscape_run_id=None,
        )
        absent = await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"human readable periodic reason {LANDSCAPE_RECONCILIATION_PENDING_SUFFIX}",
            landscape_run_id="missing-landscape",
        )

        await service.mark_landscape_reconciliation_outcomes(
            complete_run_ids=frozenset({complete.id}),
            absent_run_ids=frozenset({absent.id}),
        )

        complete_row = await service.get_run(complete.id)
        absent_row = await service.get_run(absent.id)
        assert complete_row.error == f"human readable startup reason {LANDSCAPE_RECONCILIATION_COMPLETE_SUFFIX}"
        assert absent_row.error == f"human readable periodic reason {LANDSCAPE_RECONCILIATION_ABSENT_SUFFIX}"
        assert await service.list_pending_landscape_reconciliations() == []

    @pytest.mark.asyncio
    async def test_outcome_update_rejects_overlap_without_mutation(
        self,
        service,
        session_operation_contexts,
    ) -> None:
        candidate = await self._cancelled_run(
            service,
            session_operation_contexts,
            reason=f"startup reason {LANDSCAPE_RECONCILIATION_PENDING_SUFFIX}",
            landscape_run_id="landscape-1",
        )
        with pytest.raises(ValueError, match="overlap"):
            await service.mark_landscape_reconciliation_outcomes(
                complete_run_ids=frozenset({candidate.id}),
                absent_run_ids=frozenset({candidate.id}),
            )
        assert [row.id for row in await service.list_pending_landscape_reconciliations()] == [candidate.id]


class TestCancelAllOrphanedRunsExcludeRunIds:
    """Tests for exclude_run_ids — liveness-aware orphan cleanup."""

    @pytest.mark.asyncio
    async def test_excludes_live_run_ids_from_cancellation(self, service, session_operation_contexts) -> None:
        """Runs with IDs in exclude_run_ids are skipped even if they exceed max_age."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        # Exclude this run's ID — it should NOT be cancelled
        cancelled = await service.cancel_all_orphaned_runs(
            max_age_seconds=0,
            exclude_run_ids=frozenset({str(run.id)}),
        )
        assert cancelled == 0

        # Run should still be running
        fetched = await service.get_run(run.id)
        assert fetched.status == "running"

    @pytest.mark.asyncio
    async def test_cancels_non_excluded_runs(self, service, session_operation_contexts) -> None:
        """Runs NOT in exclude_run_ids are still cancelled normally."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        # Exclude a different run ID — this run should be cancelled
        cancelled = await service.cancel_all_orphaned_runs(
            max_age_seconds=0,
            exclude_run_ids=frozenset({"not-this-run-id"}),
        )
        assert cancelled == 1

        fetched = await service.get_run(run.id)
        assert fetched.status == "cancelled"

    @pytest.mark.asyncio
    async def test_empty_exclude_set_cancels_all(self, service, session_operation_contexts) -> None:
        """Empty exclude_run_ids (default) does not change behaviour."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        cancelled = await service.cancel_all_orphaned_runs(
            max_age_seconds=0,
            exclude_run_ids=frozenset(),
        )
        assert cancelled == 1


class TestCancelAllOrphanedRunsReason:
    """Tests for reason parameter — error provenance on orphan cancellation."""

    @pytest.mark.asyncio
    async def test_reason_written_to_error_column(self, service, session_operation_contexts) -> None:
        """When reason is provided, it's stored in the run's error field."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        await service.cancel_all_orphaned_runs(
            max_age_seconds=0,
            reason="Orphaned by server restart — no active process",
        )

        fetched = await service.get_run(run.id)
        assert fetched.status == "cancelled"
        assert fetched.error == "Orphaned by server restart — no active process"

    @pytest.mark.asyncio
    async def test_no_reason_leaves_error_null(self, service, session_operation_contexts) -> None:
        """When reason is None (default), error field stays unset."""
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "running", session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        await service.cancel_all_orphaned_runs(max_age_seconds=0)

        fetched = await service.get_run(run.id)
        assert fetched.status == "cancelled"
        assert fetched.error is None


class TestCancelledTerminalTransitions:
    """Tests for cancelled as a terminal state — no outgoing transitions.

    These transitions are the exact paths triggered when the orphan cleanup
    cancels a run in the DB while the executor thread is still running.
    The executor then tries cancelled→completed or cancelled→failed,
    both of which must be rejected.
    """

    @pytest.mark.asyncio
    async def test_illegal_transition_cancelled_to_completed_raises(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "cancelled", session_operation_context=execute_context)
        with pytest.raises(ValueError, match=r"Illegal.*transition"):
            await service.update_run_status(
                run.id,
                "completed",
                landscape_run_id="lscp-cancelled",
                session_operation_context=execute_context,
            )

    @pytest.mark.asyncio
    async def test_illegal_transition_cancelled_to_failed_raises(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        run = await service.create_run(session.id, state.id, session_operation_context=execute_context)
        await service.update_run_status(run.id, "cancelled", session_operation_context=execute_context)
        with pytest.raises(ValueError, match=r"Illegal.*transition"):
            await service.update_run_status(
                run.id,
                "failed",
                error="boom",
                session_operation_context=execute_context,
            )


class TestArchiveSessionWithActiveRun:
    """Tests for archive_session when a run is active."""

    @pytest.mark.asyncio
    async def test_archive_soft_hides_session_with_active_run(self, service, session_operation_contexts) -> None:
        """A session with a durable run is soft-archived, not deleted.

        Commit 4c3e81182 ("Polish RC5 composer UX and archive behavior")
        defined the contract: ``archive_session`` physically deletes
        sessions with no durable history (no runs, no composer
        completion events) and soft-hides sessions that have either.
        An active run counts as durable history — the row remains, an
        ``archived_at`` timestamp is set, and the session is hidden
        from the default list but visible when ``include_archived``
        is requested. Preserving the row keeps the run's audit
        lineage queryable.
        """
        session = await service.create_session("alice", "Pipeline", "local")
        state = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        execute_context = session_operation_contexts.acquire(service, session.id)
        await service.create_run(session.id, state.id, session_operation_context=execute_context)
        session_operation_contexts.release(service, execute_context)

        await service.archive_session(session.id)

        archived = await service.get_session(session.id)
        assert archived.archived_at is not None, "Soft-archive should populate archived_at, not delete the row"

        default_listing = await service.list_sessions("alice", "local")
        assert session.id not in [s.id for s in default_listing], "Soft-archived session must be hidden from default listing"

        with_archived = await service.list_sessions("alice", "local", include_archived=True)
        assert session.id in [s.id for s in with_archived], "Soft-archived session must be retrievable via include_archived"


class TestGetMessagesNonexistentSession:
    """Tests for get_messages behavior with a nonexistent session."""

    @pytest.mark.asyncio
    async def test_get_messages_returns_empty_for_nonexistent_session(self, service) -> None:
        """get_messages silently returns [] for a nonexistent session_id.

        This is by design — the WHERE clause filters by session_id and
        returns no rows. Callers (routes) should verify session existence
        via _verify_session_ownership before calling get_messages.
        """
        import uuid

        result = await service.get_messages(uuid.uuid4())
        assert result == []


class TestPagination:
    """Tests for limit/offset pagination on list endpoints."""

    @pytest.mark.asyncio
    async def test_list_sessions_limit(self, service) -> None:
        for i in range(5):
            await service.create_session("alice", f"Session {i}", "local")
        sessions = await service.list_sessions("alice", "local", limit=2)
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_list_sessions_offset(self, service) -> None:
        for i in range(5):
            await service.create_session("alice", f"Session {i}", "local")
        all_sessions = await service.list_sessions("alice", "local")
        offset_sessions = await service.list_sessions("alice", "local", limit=2, offset=2)
        assert len(offset_sessions) == 2
        assert offset_sessions[0].id == all_sessions[2].id
        assert offset_sessions[1].id == all_sessions[3].id

    @pytest.mark.asyncio
    async def test_list_sessions_offset_past_end(self, service) -> None:
        await service.create_session("alice", "Only One", "local")
        sessions = await service.list_sessions("alice", "local", offset=10)
        assert sessions == []

    @pytest.mark.asyncio
    async def test_get_messages_limit(self, service) -> None:
        session = await service.create_session("alice", "Chat", "local")
        for i in range(5):
            await service.add_message(session.id, "user", f"Message {i}", writer_principal="route_user_message")
        messages = await service.get_messages(session.id, limit=3)
        assert len(messages) == 3
        assert messages[0].content == "Message 0"

    @pytest.mark.asyncio
    async def test_get_messages_offset(self, service) -> None:
        session = await service.create_session("alice", "Chat", "local")
        for i in range(5):
            await service.add_message(session.id, "user", f"Message {i}", writer_principal="route_user_message")
        messages = await service.get_messages(session.id, limit=2, offset=3)
        assert len(messages) == 2
        assert messages[0].content == "Message 3"
        assert messages[1].content == "Message 4"

    @pytest.mark.asyncio
    async def test_get_state_versions_limit(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        for _ in range(5):
            await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        versions = await service.get_state_versions(session.id, limit=2)
        assert len(versions) == 2
        assert versions[0].version == 1
        assert versions[1].version == 2

    @pytest.mark.asyncio
    async def test_get_state_versions_offset(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        for _ in range(5):
            await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        versions = await service.get_state_versions(session.id, limit=2, offset=3)
        assert len(versions) == 2
        assert versions[0].version == 4
        assert versions[1].version == 5


class TestPruneStateVersions:
    """Tests for prune_state_versions -- delete old versions, preserve recent and run-referenced."""

    @pytest.mark.asyncio
    async def test_prune_deletes_old_versions(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        for _ in range(5):
            await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")

        deleted = await service.prune_state_versions(session.id, keep_latest=2)
        assert deleted == 3

        remaining = await service.get_state_versions(session.id)
        assert len(remaining) == 2
        assert [v.version for v in remaining] == [4, 5]

    @pytest.mark.asyncio
    async def test_prune_preserves_run_referenced_versions(self, service, session_operation_contexts) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        v1 = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")

        # Create a run referencing v1
        execute_context = session_operation_contexts.acquire(service, session.id)
        await service.create_run(session.id, v1.id, session_operation_context=execute_context)

        # Prune keeping only latest 1 -- v1 should survive (run-referenced), v2 deleted
        deleted = await service.prune_state_versions(session.id, keep_latest=1)
        assert deleted == 1  # only v2 deleted

        remaining = await service.get_state_versions(session.id)
        remaining_versions = [v.version for v in remaining]
        assert 1 in remaining_versions  # preserved by run reference
        assert 2 not in remaining_versions  # deleted
        assert 3 in remaining_versions  # kept as latest

    @pytest.mark.asyncio
    async def test_prune_returns_zero_when_nothing_to_prune(self, service) -> None:
        session = await service.create_session("alice", "Pipeline", "local")
        for _ in range(2):
            await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")

        deleted = await service.prune_state_versions(session.id, keep_latest=5)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_prune_preserves_derived_from_lineage(self, service) -> None:
        """States referenced via derived_from_state_id must survive pruning.

        Scenario: v1 (normal), v2 (normal), v3 (revert to v1).
        Prune with keep_latest=1 keeps v3 (latest).  v1 must survive
        because v3.derived_from_state_id points at it.  v2 can be deleted.
        """
        session = await service.create_session("alice", "Pipeline", "local")
        v1 = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        # Revert to v1 — creates v3 with derived_from_state_id = v1.id
        v3 = await service.set_active_state(session.id, v1.id)
        assert v3.derived_from_state_id == v1.id

        deleted = await service.prune_state_versions(session.id, keep_latest=1)
        assert deleted == 1  # only v2 deleted

        remaining = await service.get_state_versions(session.id)
        remaining_ids = {v.id for v in remaining}
        assert v1.id in remaining_ids, "v1 must survive — referenced by v3.derived_from_state_id"
        assert v3.id in remaining_ids, "v3 must survive — it is the latest version"

    @pytest.mark.asyncio
    async def test_prune_preserves_transitive_derived_lineage(self, service) -> None:
        """Transitive derived_from chains must be fully preserved.

        Scenario: v1, v2, v3 (revert→v1), v4, v5 (revert→v3).
        Prune with keep_latest=1 keeps v5.  v3 must survive (v5 points
        at it), and v1 must survive (v3 points at it).  v2 and v4 can go.
        """
        session = await service.create_session("alice", "Pipeline", "local")
        v1 = await service.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        # v3: revert to v1
        v3 = await service.set_active_state(session.id, v1.id)
        await service.save_composition_state(session.id, CompositionStateData(is_valid=False), provenance="session_seed")
        # v5: revert to v3
        v5 = await service.set_active_state(session.id, v3.id)

        deleted = await service.prune_state_versions(session.id, keep_latest=1)
        assert deleted == 2  # v2 and v4 deleted

        remaining = await service.get_state_versions(session.id)
        remaining_ids = {v.id for v in remaining}
        assert v1.id in remaining_ids, "v1 must survive — v3.derived_from_state_id"
        assert v3.id in remaining_ids, "v3 must survive — v5.derived_from_state_id"
        assert v5.id in remaining_ids, "v5 must survive — latest version"
