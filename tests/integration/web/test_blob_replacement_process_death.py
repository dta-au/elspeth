"""A successor repairs durable replacement phases after actual process death."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
import structlog
from sqlalchemy import func, select

from elspeth.contracts.blobs import BlobReplacementPlan
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.replacement import BlobReplacementCoordinator
from elspeth.web.blobs.service import BlobServiceImpl, content_hash
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blob_replacement_cleanups_table, blobs_table
from elspeth.web.sessions.protocol import SessionOperationMutationTransaction
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity


def _die_during_replacement(database_url: str, storage_root: str, blob_id: UUID, context: SessionOperationContext, phase: str) -> None:
    engine = create_session_engine(database_url)
    service = BlobServiceImpl(engine, Path(storage_root))
    authority = service._session_operation_authority
    original_mutate = authority.mutate
    original_replace = os.replace
    old = authority.mutate(context, lambda transaction: transaction.blobs.read_blob(blob_id=blob_id))

    def mutate_then_die[T](operation: SessionOperationContext, mutation: Callable[[SessionOperationMutationTransaction], T]) -> T:
        result = original_mutate(operation, mutation)
        if isinstance(result, BlobReplacementPlan) and result.phase == phase:
            os._exit(42)
        return result

    def rename_then_die(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if phase == "temporary" and str(source).endswith(".custody.tmp"):
            os._exit(42)
        original_replace(source, target, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if (phase == "backup" and str(target).endswith(".backup")) or (phase == "published" and str(source).endswith(".stage")):
            os.fsync(dst_dir_fd) if dst_dir_fd is not None else None
            os._exit(42)

    with patch.object(authority, "mutate", mutate_then_die), patch("elspeth.web.blobs.replacement.os.replace", rename_then_die):
        BlobReplacementCoordinator(engine=engine, data_dir=Path(storage_root), session_operation_authority=authority).replace_blob(
            expected=old,
            replacement=replace(old, content_hash=content_hash(b"new")),
            content=b"new",
            context=context,
            max_storage_per_session=1000,
            accepting_proposal_id=None,
        )
    raise AssertionError("replacement missed the requested process-death phase")


@pytest.mark.parametrize("phase", ["intent", "temporary", "swap_pending", "backup", "published", "purge_pending"])
def test_process_death_keeps_exact_version_recoverable(tmp_path: Path, phase: str) -> None:
    database_url = f"sqlite:///{tmp_path / 'sessions.db'}"
    engine = create_session_engine(database_url)
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="process-death")
    sessions = SessionServiceImpl(engine, tmp_path, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.process-death"))
    session = asyncio.run(sessions.create_session("process-death", "Replacement", "local"))
    authority = sessions.session_operation_authority
    context = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=120,
    )
    service = BlobServiceImpl(engine, tmp_path, session_operation_authority=authority)
    old = asyncio.run(service.create_blob(session.id, "content.txt", b"old", "text/plain", session_operation_context=context))
    neighbor = asyncio.run(service.create_blob(session.id, "neighbor.txt", b"safe", "text/plain", session_operation_context=context))
    process = multiprocessing.get_context("spawn").Process(
        target=_die_during_replacement,
        args=(database_url, str(tmp_path), old.id, context, phase),
    )
    process.start()
    try:
        process.join(timeout=30)
        assert not process.is_alive(), "replacement child did not reach its durable phase"
        assert process.exitcode == 42
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)
        process.close()
    with engine.connect() as connection:
        assert connection.execute(select(blob_replacement_cleanups_table.c.phase)).scalar_one() == (
            "intent" if phase in {"intent", "temporary"} else "purge_pending" if phase == "purge_pending" else "swap_pending"
        )
    # Explicitly settle the dead owner's operation, then acquire a new epoch.
    authority.release(context)
    successor = authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=120,
    )
    try:
        BlobReplacementCoordinator(engine=engine, data_dir=tmp_path, session_operation_authority=authority).reconcile(context=successor)
        expected = replace(old, content_hash=content_hash(b"new")) if phase == "purge_pending" else old
        assert authority.mutate(successor, lambda transaction: transaction.blobs.read_blob(blob_id=old.id)) == expected
        assert Path(old.storage_path).read_bytes() == (b"new" if phase == "purge_pending" else b"old")
        assert Path(neighbor.storage_path).read_bytes() == b"safe"
        assert set(Path(old.storage_path).parent.iterdir()) == {Path(old.storage_path), Path(neighbor.storage_path)}
        with engine.connect() as connection:
            assert connection.execute(select(blob_replacement_cleanups_table)).all() == []
            assert connection.execute(select(func.sum(blobs_table.c.size_bytes))).scalar_one() == 7
    finally:
        authority.release(successor)
        engine.dispose()
