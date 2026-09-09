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

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import web_instances_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


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
    session = await sessions.create_session(f"pg-custody-{uuid4()}", "Custody", "local")
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
