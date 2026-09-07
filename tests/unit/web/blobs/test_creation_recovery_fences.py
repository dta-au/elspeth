"""Abandoned upload recovery preserves evidence when authority or artifacts fail."""

from pathlib import Path

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs import service as service_module
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.blobs import test_service as fixtures

blob_service = fixtures.blob_service
compose_context = fixtures.compose_context
db_engine = fixtures.db_engine
session_id = fixtures.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["lost_during_hash", "symlink_temp"])
async def test_abandoned_creation_preserves_exact_obligation_on_recovery_refusal(
    blob_service, compose_context, db_engine, session_id, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    authority = blob_service._session_operation_authority
    original_write = service_module._atomic_write_blob

    def release_after_write(*args, **kwargs):
        original_write(*args, **kwargs)
        authority.release(compose_context)

    with monkeypatch.context() as injection:
        injection.setattr(service_module, "_atomic_write_blob", release_after_write)
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.create_blob(
                session_id, "abandoned.txt", b"retained evidence", "text/plain", session_operation_context=compose_context
            )
    with db_engine.connect() as conn:
        pending = conn.execute(select(blobs_table)).one()
    storage = Path(pending.storage_path)
    assert storage.read_bytes() == b"retained evidence"
    temp = storage.with_name(f".{storage.name}.custody.tmp")
    neighbor = storage.with_name(f".{storage.name}.neighbor.tmp")
    neighbor.write_bytes(b"unrelated neighbor")
    successor = authority.acquire(
        session_id=session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="recovery-successor", lease_seconds=60
    )
    try:
        if fault == "symlink_temp":
            outside = tmp_path / "outside.txt"
            outside.write_bytes(b"outside evidence")
            temp.symlink_to(outside)
            expected_error = AuditIntegrityError
        else:
            temp.write_bytes(b"partial upload")
            original_hash = service_module._content_hash_from_file

            def release_after_hash(path, **kwargs):
                result = original_hash(path, **kwargs)
                if path == storage:
                    authority.release(successor)
                return result

            monkeypatch.setattr(service_module, "_content_hash_from_file", release_after_hash)
            expected_error = SessionOperationFenceLost
        with pytest.raises(expected_error):
            await blob_service.create_blob(session_id, "successor.txt", b"new bytes", "text/plain", session_operation_context=successor)
        assert storage.read_bytes() == b"retained evidence"
        assert neighbor.read_bytes() == b"unrelated neighbor"
        if fault == "symlink_temp":
            assert temp.is_symlink()
            assert temp.read_bytes() == b"outside evidence"
        else:
            assert temp.read_bytes() == b"partial upload"
        with db_engine.connect() as conn:
            remaining = conn.execute(select(blobs_table)).one()
            assert remaining == pending
    finally:
        if fault == "symlink_temp":
            authority.release(successor)
