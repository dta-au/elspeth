"""The original upload authority must survive both storage and ready commit."""

from pathlib import Path

import pytest
from sqlalchemy import select

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
@pytest.mark.parametrize("phase", ["before_write", "before_ready"])
async def test_upload_revalidates_actual_authority_at_each_durable_phase(
    blob_service,
    compose_context,
    db_engine,
    session_id,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    neighbor = await blob_service.create_blob(
        session_id,
        "neighbor.txt",
        b"keep",
        "text/plain",
        session_operation_context=compose_context,
    )
    authority = blob_service._session_operation_authority
    original_write = service_module._atomic_write_blob
    revoked = False

    def revoke_at_phase(*args, **kwargs) -> None:
        nonlocal revoked
        if phase == "before_ready":
            original_write(*args, **kwargs)
        authority.release(compose_context)
        revoked = True
        with pytest.raises(SessionOperationFenceLost):
            authority.compare_and_swap(compose_context)
        if phase == "before_write":
            original_write(*args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(service_module, "_atomic_write_blob", revoke_at_phase)
        with pytest.raises(SessionOperationFenceLost):
            await blob_service.create_blob(
                session_id,
                "revoked.txt",
                b"uncommitted",
                "text/plain",
                session_operation_context=compose_context,
            )
    assert revoked
    with db_engine.connect() as connection:
        pending = connection.execute(select(blobs_table).where(blobs_table.c.filename == "revoked.txt")).one()
    assert pending.status == "pending"
    assert pending.custody_operation_id == compose_context.fence.operation_id
    assert Path(neighbor.storage_path).read_bytes() == b"keep"
    successor = authority.acquire(
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="creation-successor",
        lease_seconds=60,
    )
    try:
        created = await blob_service.create_blob(
            session_id,
            "successor.txt",
            b"ok",
            "text/plain",
            session_operation_context=successor,
        )
        assert Path(created.storage_path).read_bytes() == b"ok"
        assert Path(neighbor.storage_path).read_bytes() == b"keep"
        with db_engine.connect() as connection:
            assert connection.execute(select(blobs_table).where(blobs_table.c.id == pending.id)).first() is None
        assert not Path(pending.storage_path).exists()
        assert set((tmp_path / "blobs" / str(session_id)).iterdir()) == {Path(created.storage_path), Path(neighbor.storage_path)}
    finally:
        authority.release(successor)
