"""PostgreSQL lease renewal remains live during shared Composer blob I/O."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Connection, event
from tests.fixtures.identities import ensure_test_identity
from tests.testcontainer.web import test_blob_custody_lock_isolation_postgres as custody_fixtures

from elspeth.contracts.advisory_locks import ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, ELSPETH_SESSIONS_LOCK_CLASSID
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs import replacement as replacement_module
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.composer.tools.blobs import _locked_read_ready_blob

pytestmark = pytest.mark.testcontainer
deployment = custody_fixtures.deployment


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "replace"])
async def test_composer_storage_pause_does_not_hold_sessions_lock(deployment, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    session_engine, blob_engine, sessions, shared = deployment
    custody_fixtures._register_instance(session_engine, sessions.session_operation_owner_instance_id)
    with session_engine.begin() as conn:
        ensure_test_identity(conn, identity_id="lease-progress")
    session = await sessions.create_session("lease-progress", "Composer", "local")
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=60,
    )
    service = BlobServiceImpl(blob_engine, shared, session_operation_authority=authority)
    old = await service.create_blob(session.id, "source.txt", b"old", "text/plain", session_operation_context=context)
    entered = threading.Event()
    resume = threading.Event()
    holder_pids: set[int] = set()

    def observe_custody(conn: Connection, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool) -> None:
        if "pg_advisory_lock(" in statement:
            holder_pids.add(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())

    def pause() -> None:
        entered.set()
        assert resume.wait(timeout=15), "storage pause was not released"

    original_read = Path.read_bytes
    original_write = replacement_module._atomic_write_blob

    def paused_read(path: Path) -> bytes:
        if path == Path(old.storage_path):
            pause()
        return original_read(path)

    def paused_write(*args, **kwargs) -> None:
        pause()
        original_write(*args, **kwargs)

    if operation == "read":
        monkeypatch.setattr(Path, "read_bytes", paused_read)
    else:
        monkeypatch.setattr(replacement_module, "_atomic_write_blob", paused_write)

    def perform() -> None:
        if operation == "read":
            record, data = _locked_read_ready_blob(
                blob_engine,
                str(session.id),
                str(old.id),
                data_dir=str(shared),
                session_operation_context=context,
                session_operation_authority=authority,
            )
            assert record is not None and record["id"] == str(old.id)
            assert data == b"old"
        else:
            updated = replace(old, content_hash=content_hash(b"new"))
            assert (
                BlobReplacementCoordinator(
                    engine=blob_engine,
                    data_dir=shared,
                    session_operation_authority=authority,
                ).replace_blob(
                    expected=old,
                    replacement=updated,
                    content=b"new",
                    context=context,
                    max_storage_per_session=1000,
                    accepting_proposal_id=None,
                )
                == updated
            )

    event.listen(blob_engine, "before_cursor_execute", observe_custody)
    task = asyncio.create_task(asyncio.to_thread(perform))
    renewal = None
    try:
        assert await asyncio.to_thread(entered.wait, 10), "blob operation did not reach storage"
        assert len(holder_pids) == 1
        with session_engine.connect() as observer:
            held = (
                observer.exec_driver_sql(
                    "SELECT classid FROM pg_locks WHERE pid = ANY(%s) AND locktype = 'advisory' AND granted",
                    (list(holder_pids),),
                )
                .scalars()
                .all()
            )
        assert ELSPETH_BLOB_CUSTODY_LOCK_CLASSID in held
        assert ELSPETH_SESSIONS_LOCK_CLASSID not in held
        renewal = asyncio.create_task(asyncio.to_thread(authority.renew, context, lease_seconds=60))
        renewed = await asyncio.wait_for(asyncio.shield(renewal), timeout=2)
        assert renewed.fence.operation_id == context.fence.operation_id
    finally:
        resume.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=15)
            if renewal is not None:
                await asyncio.wait_for(asyncio.shield(renewal), timeout=15)
        finally:
            event.remove(blob_engine, "before_cursor_execute", observe_custody)
            authority.release(context)
    assert original_read(Path(old.storage_path)) == (b"old" if operation == "read" else b"new")
