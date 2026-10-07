"""Real archive API, terminal database truth, and replica recovery on EFS-like mounts."""

from __future__ import annotations

import errno
import threading
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.progress import ComposerProgressRegistry
from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost, SessionOperationTerminalOutcomeUnknown
from elspeth.web.sessions import archive_quarantine as quarantine
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.locking import _blob_custody_session_lock
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.protocol import SessionNotFoundError
from elspeth.web.sessions.service import SessionServiceImpl
from tests.unit.web.blobs.test_routes import _make_app
from tests.unit.web.sessions.test_archive_in_place import unsupported_rename
from tests.unit.web.sessions.test_routes import _ExecutionServiceStub


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    unsupported_rename(monkeypatch)
    app, sessions, blobs = _make_app(tmp_path)
    # The shared route helper normally tests DB-only session deletion. Here
    # both services must use the actual uploaded bytes on the same mount.
    sessions._data_dir = tmp_path
    app.state.execution_service = _ExecutionServiceStub()
    app.state.composer_progress_registry = ComposerProgressRegistry()
    try:
        yield app, sessions, blobs, tmp_path
    finally:
        sessions._engine.dispose()


async def upload(client: httpx.AsyncClient) -> tuple[UUID, UUID]:
    created = await client.post("/api/sessions", json={"title": "Archive uploaded evidence"})
    assert created.status_code == 201
    session_id = UUID(created.json()["id"])
    uploaded = await client.post(
        f"/api/sessions/{session_id}/blobs",
        files={"file": ("source.csv", b"x\n1\n", "text/csv")},
    )
    assert uploaded.status_code == 201
    return session_id, UUID(uploaded.json()["id"])


@pytest.mark.asyncio
async def test_delete_uploaded_session_succeeds_without_native_noreplace(deployment):
    app, sessions, _blobs, data_dir = deployment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id, blob_id = await upload(client)
        assert (data_dir / "blobs" / str(session_id)).is_dir()
        deleted = await client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 204
        assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
        assert (await client.get(f"/api/sessions/{session_id}/blobs/{blob_id}")).status_code == 404
    assert not (data_dir / "blobs" / str(session_id)).exists()
    assert quarantine.archive_quarantine_operation_ids(data_dir, session_id) == ()
    assert sessions.session_operation_authority.archive_cleanup_is_consumed(session_id)
    assert await sessions.reconcile_consumed_archives() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_outcome", ["current", "committed", "unknown", "fence_lost"])
async def test_failed_delete_restores_only_current_truth(deployment, monkeypatch, delete_outcome):
    app, sessions, _blobs, data_dir = deployment
    authority = sessions.session_operation_authority
    real_delete = authority.archive_delete
    injected_storage_errors: list[OSError] = []

    def fail_delete(context):
        if delete_outcome == "committed":
            real_delete(context)
        if delete_outcome == "fence_lost":
            raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
        error = OSError(errno.EIO, "ambiguous database acknowledgement")
        injected_storage_errors.append(error)
        raise error

    monkeypatch.setattr(authority, "archive_delete", fail_delete)
    if delete_outcome in {"unknown", "fence_lost"}:

        def fail_reconciliation(_context):
            if delete_outcome == "fence_lost":
                raise SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)
            error = OSError(errno.EIO, "terminal truth unavailable")
            injected_storage_errors.append(error)
            raise error

        monkeypatch.setattr(authority, "reconcile_archive_delete", fail_reconciliation)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id, blob_id = await upload(client)
        if delete_outcome == "committed":
            await sessions.archive_session(session_id)
            with pytest.raises(SessionNotFoundError):
                await sessions.get_session(session_id)
            assert not (data_dir / "blobs" / str(session_id)).exists()
            assert quarantine.archive_quarantine_operation_ids(data_dir, session_id) == ()
            return
        expected_error = {
            "current": OSError,
            "unknown": ExceptionGroup,
            "fence_lost": SessionOperationFenceLost,
        }[delete_outcome]
        with pytest.raises(expected_error) as captured:
            await sessions.archive_session(session_id)
        if delete_outcome == "unknown":
            assert isinstance(captured.value, ExceptionGroup)
            terminal, delete_error, reconciliation_error = captured.value.exceptions
            assert isinstance(terminal, SessionOperationTerminalOutcomeUnknown)
            assert len(injected_storage_errors) == 2
            assert delete_error is injected_storage_errors[0]
            assert reconciliation_error is injected_storage_errors[1]
            assert all(error.errno == errno.EIO for error in injected_storage_errors)
        await sessions.get_session(session_id)
        root = data_dir / "blobs" / str(session_id)
        assert next(root.glob(f"{blob_id}_*")).read_bytes() == b"x\n1\n"
        operations = quarantine.archive_quarantine_operation_ids(data_dir, session_id)
        if delete_outcome == "current":
            assert operations == ()
            assert not tuple(root.glob(".archive-in-place-*"))
        else:
            assert len(operations) == 1
            assert quarantine.archive_quarantine_paths(data_dir, operations[0]).in_place.is_file()
            # Ordinary startup/periodic recovery never interprets filesystem
            # metadata as permission to restore or delete a live parent.
            assert await sessions.reconcile_consumed_archives() == 0
            assert quarantine.archive_quarantine_operation_ids(data_dir, session_id) == operations


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_phase", ["purge", "retire"])
async def test_consumed_cleanup_failure_survives_and_is_recovered_by_another_replica(deployment, monkeypatch, cleanup_phase):
    app, sessions, _blobs, data_dir = deployment
    real_purge = service_module.purge_archive_quarantine
    real_retire = service_module.retire_archive_quarantine

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EACCES, "mount temporarily unavailable")

    function_name = "purge_archive_quarantine" if cleanup_phase == "purge" else "retire_archive_quarantine"
    monkeypatch.setattr(service_module, function_name, unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id, _blob_id = await upload(client)
        with pytest.raises(service_module.QuarantineCleanupError):
            await sessions.archive_session(session_id)
    assert sessions.session_operation_authority.archive_cleanup_is_consumed(session_id)
    assert len(quarantine.archive_quarantine_operation_ids(data_dir, session_id)) == 1
    monkeypatch.setattr(service_module, "purge_archive_quarantine", real_purge)
    monkeypatch.setattr(service_module, "retire_archive_quarantine", real_retire)
    replica = SessionServiceImpl(
        sessions._engine,
        data_dir,
        telemetry=sessions._telemetry,
        log=sessions._log,
        owner_instance_id="recovery-replica",
    )
    assert await replica.reconcile_consumed_archives() == 1
    assert await sessions.reconcile_consumed_archives() == 0
    assert not (data_dir / "blobs" / str(session_id)).exists()
    assert quarantine.archive_quarantine_operation_ids(data_dir, session_id) == ()


@pytest.mark.asyncio
async def test_half_present_authority_aborts_cleanup_without_touching_bytes(deployment):
    app, sessions, _blobs, data_dir = deployment
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id, blob_id = await upload(client)
    identity = quarantine.ArchiveQuarantineIdentity(session_id, UUID("22222222-2222-4c22-8d22-222222222222"), 7)
    canonical = data_dir / "blobs" / str(session_id)
    quarantine.prepare_archive_quarantine(data_dir, identity, source_present=True)
    quarantine.stage_archive_quarantine(data_dir, identity, canonical)
    with sessions._engine.begin() as conn:
        conn.execute(delete(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id)))
    with pytest.raises(AuditIntegrityError, match="half-present"):
        await sessions.reconcile_consumed_archives()
    assert next(canonical.glob(f"{blob_id}_*")).read_bytes() == b"x\n1\n"
    assert quarantine.archive_quarantine_paths(data_dir, identity).in_place.is_file()


@pytest.mark.asyncio
async def test_consumed_scanner_rejects_unexpected_control_before_deleting_bytes(deployment, monkeypatch):
    app, sessions, _blobs, data_dir = deployment
    real_purge = service_module.purge_archive_quarantine

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EACCES, "mount unavailable")

    monkeypatch.setattr(service_module, "purge_archive_quarantine", unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id, blob_id = await upload(client)
        with pytest.raises(service_module.QuarantineCleanupError):
            await sessions.archive_session(session_id)
    (identity,) = quarantine.archive_quarantine_operation_ids(data_dir, session_id)
    paths = quarantine.archive_quarantine_paths(data_dir, identity)
    (paths.operation_dir / "unexpected.control").write_bytes(b"preserve")
    monkeypatch.setattr(service_module, "purge_archive_quarantine", real_purge)
    with pytest.raises(quarantine.ArchiveQuarantineIntegrityError, match="invalid entry set"):
        await sessions.reconcile_consumed_archives()
    assert next((data_dir / "blobs" / str(session_id)).glob(f"{blob_id}_*")).read_bytes() == b"x\n1\n"
    assert paths.manifest.is_file()
    assert paths.in_place.is_file()


@pytest.mark.asyncio
async def test_recovery_skips_busy_custody_and_retries_consumed_work(deployment, monkeypatch):
    app, sessions, _blobs, data_dir = deployment
    real_purge = service_module.purge_archive_quarantine

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EACCES, "mount unavailable")

    monkeypatch.setattr(service_module, "purge_archive_quarantine", unavailable)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session_id, _blob_id = await upload(client)
        with pytest.raises(service_module.QuarantineCleanupError):
            await sessions.archive_session(session_id)
    monkeypatch.setattr(service_module, "purge_archive_quarantine", real_purge)
    held = threading.Event()
    release = threading.Event()

    def hold_custody():
        with _blob_custody_session_lock(sessions._engine, str(session_id)):
            held.set()
            assert release.wait(timeout=10)

    owner = threading.Thread(target=hold_custody)
    owner.start()
    try:
        assert held.wait(timeout=5)
        assert await sessions.reconcile_consumed_archives() == 0
        assert (data_dir / "blobs" / str(session_id)).is_dir()
    finally:
        release.set()
        owner.join(timeout=5)
    assert not owner.is_alive()
    assert await sessions.reconcile_consumed_archives() == 1
