"""Blob custody locking never queues a session-operation lease behind file I/O.

PostgreSQL only. The blob custody lock is a SESSION-level advisory lock
held on a dedicated connection across reserve, file write (including the
``.custody.tmp`` rename) and finalize. Session-operation fence operations
(acquire, renew, release) take a transaction-scoped advisory lock on the
same session id. If the two shared one classid, every fence operation on a
session would wait behind that session's filesystem persistence: on the
multi-replica target a renew starved past the lease window becomes a
spurious takeover. This module pins that the custody lock lives in its own
classid namespace (``ELSPETH_BLOB_CUSTODY_LOCK_CLASSID``): a renew completes
while a blob write for the same session is paused inside its rename.

SQLite cannot express the property -- its process lock serialises every
writer of a session, fence operations included -- so there is no SQLite
sibling.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import Engine, insert
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.sessions.test_archive_in_place import unsupported_rename

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions import archive_quarantine as quarantine
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.locking import ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, _blob_custody_session_lock, _postgres_advisory_session_lock
from elspeth.web.sessions.models import web_instances_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.mark.asyncio
async def test_in_place_archive_drains_a_writer_paused_after_authority_validation(deployment, monkeypatch) -> None:
    session_engine, blob_engine, sessions, shared = deployment
    _register_instance(session_engine, sessions.session_operation_owner_instance_id)
    unsupported_rename(monkeypatch)
    blobs = BlobServiceImpl(blob_engine, shared)
    owner_id = f"late-archive-writer-{uuid4()}"
    with session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id=owner_id)
    session = await sessions.create_session(owner_id, "Late writer", "local")
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.CREATE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    entered = threading.Event()
    resume = threading.Event()
    original_replace = os.replace
    blob_dir = shared / "blobs" / str(session.id)

    def pause_before_publish(source, target):
        if Path(target).parent == blob_dir and Path(source).name.endswith(".custody.tmp"):
            entered.set()
            assert resume.wait(timeout=15)
        original_replace(source, target)

    monkeypatch.setattr("elspeth.web.blobs.service.os.replace", pause_before_publish)
    write_task = asyncio.create_task(blobs.create_blob(session.id, "source.csv", b"x\n1\n", "text/csv", session_operation_context=context))
    archive_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        # The writer already passed its authority check and retains custody.
        # Releasing its fence models a predecessor resuming after takeover.
        await asyncio.to_thread(authority.release, context)
        archive_task = asyncio.create_task(sessions.archive_session(session.id))
        deadline = asyncio.get_running_loop().time() + 5
        waiting = False
        while asyncio.get_running_loop().time() < deadline and not archive_task.done():
            with session_engine.connect() as observer:
                waiting = observer.exec_driver_sql(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND classid = %s AND NOT granted)",
                    (ELSPETH_BLOB_CUSTODY_LOCK_CLASSID,),
                ).scalar_one()
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting, "archive did not wait for the predecessor's custody"
        assert not authority.archive_cleanup_is_consumed(session.id)
        assert not archive_task.done()
    finally:
        resume.set()
        with pytest.raises(SessionOperationFenceLost):
            await asyncio.wait_for(write_task, timeout=10)
        if archive_task is not None:
            await asyncio.wait_for(archive_task, timeout=10)
    assert authority.archive_cleanup_is_consumed(session.id)
    assert not blob_dir.exists()
    assert quarantine.archive_quarantine_operation_ids(shared, session.id) == ()
    with pytest.raises(SessionOperationFenceLost):
        await blobs.create_blob(session.id, "late.csv", b"x\n2\n", "text/csv", session_operation_context=context)
    assert not blob_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [False, True])
@pytest.mark.parametrize("failure_phase", ["acquire_ack", "acquire_commit", "release_rollback", "release_sql", "release_commit"])
async def test_postgres_uncertain_lock_connections_are_invalidated(deployment, monkeypatch, blocking, failure_phase) -> None:
    session_engine, blob_engine, _sessions, _shared = deployment
    key = f"uncertain-custody-{uuid4()}"
    with blob_engine.connect() as connection:
        original_exec = connection.exec_driver_sql
        original_commit = connection.commit
        original_rollback = connection.rollback
        commit_count = 0

        def fail_exec(statement, *args, **kwargs):
            acquisition = "pg_advisory_lock(" in statement or "pg_try_advisory_lock(" in statement
            if failure_phase == "release_sql" and "pg_advisory_unlock(" in statement:
                raise OSError("private unlock detail")
            result = original_exec(statement, *args, **kwargs)
            if failure_phase == "acquire_ack" and acquisition:
                raise OSError("private acquisition acknowledgement")
            return result

        def fail_commit():
            nonlocal commit_count
            commit_count += 1
            if (failure_phase == "acquire_commit" and commit_count == 1) or (failure_phase == "release_commit" and commit_count == 2):
                raise OSError("private commit acknowledgement")
            original_commit()

        def fail_rollback():
            if failure_phase == "release_rollback":
                raise OSError("private transaction reset")
            original_rollback()

        monkeypatch.setattr(connection, "exec_driver_sql", fail_exec)
        monkeypatch.setattr(connection, "commit", fail_commit)
        monkeypatch.setattr(connection, "rollback", fail_rollback)
        with (
            pytest.raises(OSError),
            _postgres_advisory_session_lock(
                connection, ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, key, label="test custody", blocking=blocking
            ) as acquired,
        ):
            assert acquired
            if failure_phase == "release_rollback":
                connection.exec_driver_sql("SELECT 1")
        assert connection.invalidated
        # Invalidation closes the actual backend; a fresh connection can
        # acquire the same lock rather than inheriting a pooled session lock.
        deadline = asyncio.get_running_loop().time() + 2
        reacquired = False
        while asyncio.get_running_loop().time() < deadline:
            with (
                session_engine.connect() as fresh,
                _postgres_advisory_session_lock(
                    fresh, ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, key, label="test fresh custody", blocking=False
                ) as lock_available,
            ):
                reacquired = lock_available
                if lock_available:
                    break
            await asyncio.sleep(0.01)
        assert reacquired


@pytest.mark.asyncio
async def test_postgres_recovery_try_lock_skips_live_and_consumed_contended_candidates(deployment, monkeypatch) -> None:
    session_engine, blob_engine, sessions, shared = deployment
    _register_instance(session_engine, sessions.session_operation_owner_instance_id)
    unsupported_rename(monkeypatch)
    owner_id = f"archive-sweep-{uuid4()}"
    with session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id=owner_id)
    session = await sessions.create_session(owner_id, "Recovery skip", "local")
    canonical = shared / "blobs" / str(session.id)
    canonical.mkdir(parents=True)
    (canonical / "source.csv").write_bytes(b"x\n1\n")
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    from uuid import UUID

    identity = quarantine.ArchiveQuarantineIdentity(session.id, UUID(context.fence.operation_id), context.fence.operation_epoch)
    quarantine.prepare_archive_quarantine(shared, identity, source_present=True)
    quarantine.stage_archive_quarantine(shared, identity, canonical)
    held = threading.Event()
    release = threading.Event()

    def hold_custody():
        with _blob_custody_session_lock(blob_engine, str(session.id)):
            held.set()
            assert release.wait(timeout=10)

    holder = threading.Thread(target=hold_custody)
    holder.start()
    try:
        assert await asyncio.to_thread(held.wait, 5)
        assert await asyncio.wait_for(sessions.reconcile_consumed_archives(), timeout=2) == 0
        authority.archive_delete(context)
        assert await asyncio.wait_for(sessions.reconcile_consumed_archives(), timeout=2) == 0
        assert (canonical / "source.csv").read_bytes() == b"x\n1\n"
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
    assert not holder.is_alive()
    assert await sessions.reconcile_consumed_archives() == 1
    assert not canonical.exists()


@pytest.fixture()
def deployment(
    external_deployment_postgres_url: str,
    tmp_path: Path,
) -> Iterator[tuple[Engine, Engine, SessionServiceImpl, Path]]:
    """One session service on its own engine; the blob service gets a second engine.

    Two engines make the pool counts attributable: a connection held by the
    blob write shows on the blob engine only.
    """
    session_engine = create_session_engine(external_deployment_postgres_url)
    blob_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(session_engine)
    shared = tmp_path / "shared-blobs"
    sessions = SessionServiceImpl(
        session_engine,
        shared,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-custody-lock"),
        owner_instance_id=f"custody-{uuid4()}",
        session_operation_lease_seconds=30,
    )
    try:
        yield session_engine, blob_engine, sessions, shared
    finally:
        session_engine.dispose()
        blob_engine.dispose()


def _register_instance(engine: Engine, instance_id: str) -> None:
    with engine.begin() as conn:
        now = conn.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        conn.execute(
            insert(web_instances_table).values(
                instance_id=instance_id,
                deployment_target="testcontainer",
                deployment_generation="custody-lock",
                session_epoch=37,
                landscape_epoch=29,
                coordination_protocol=1,
                image_digest="sha256:custody-lock",
                revision_label="custody-lock",
                state="active",
                started_at=now,
                last_heartbeat_at=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
        )


@pytest.mark.asyncio
async def test_postgres_lease_renew_is_not_blocked_by_an_in_flight_blob_persist(deployment) -> None:
    session_engine, blob_engine, sessions, shared = deployment
    _register_instance(session_engine, sessions.session_operation_owner_instance_id)
    blobs = BlobServiceImpl(blob_engine, shared)
    owner_id = f"pg-custody-{uuid4()}"
    with session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id=owner_id)
    session = await sessions.create_session(owner_id, "Custody", "local")
    lease = await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.CREATE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=sessions.session_operation_lease_seconds,
    )

    entered = threading.Event()
    resume_rename = threading.Event()
    original_replace = os.replace
    blob_dir = shared / "blobs" / str(session.id)

    def paused_custody_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(target).parent == blob_dir and Path(source).name.endswith(".custody.tmp"):
            entered.set()
            assert resume_rename.wait(timeout=10)
        original_replace(source, target)

    renew_task: asyncio.Task[object] | None = None
    try:
        with patch("elspeth.web.blobs.service.os.replace", paused_custody_replace):
            write_task = asyncio.create_task(
                blobs.create_blob(
                    session.id,
                    "source.csv",
                    b"x\n1\n",
                    "text/csv",
                    session_operation_context=lease.context,
                )
            )
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                # The blob engine holds exactly the custody-lock connection;
                # the session engine holds nothing.
                assert blob_engine.pool.checkedout() == 1
                assert session_engine.pool.checkedout() == 0

                # A real fence operation on the same session, issued while
                # the write is paused inside its rename. It must complete
                # without waiting for the filesystem.
                renew_task = asyncio.create_task(
                    asyncio.to_thread(
                        sessions.session_operation_authority.renew,
                        lease.context,
                        lease_seconds=sessions.session_operation_lease_seconds,
                    )
                )
                renewed = await asyncio.wait_for(asyncio.shield(renew_task), timeout=2)
                assert renewed.fence.session_id == lease.context.fence.session_id
            finally:
                resume_rename.set()
                if renew_task is not None and not renew_task.done():
                    await asyncio.wait_for(asyncio.shield(renew_task), timeout=10)

            record = await asyncio.wait_for(write_task, timeout=10)
        assert record.session_id == session.id
        assert record.size_bytes == 4
        assert blob_engine.pool.checkedout() == 0
        assert session_engine.pool.checkedout() == 0
    finally:
        await lease.close()
