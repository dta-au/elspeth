"""Standalone creation recovery must not consume pipeline-output custody."""

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blob_run_links_table, blobs_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.helpers.session_fences import seed_live_operation_context
from tests.unit.web.blobs.test_service import _seed_active_run
from tests.unit.web.blobs.test_service_fencing import _insert_session, _reserve_output_blob


@pytest.fixture
def db_engine() -> Engine:
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    return engine


@pytest.fixture
def session_id(db_engine: Engine) -> UUID:
    return _insert_session(db_engine)


@pytest.fixture
def blob_service(db_engine: Engine, tmp_path: Path) -> BlobServiceImpl:
    return BlobServiceImpl(db_engine, tmp_path)


@pytest.fixture
def compose_context(db_engine: Engine, session_id: UUID) -> SessionOperationContext:
    return seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.COMPOSE)


@pytest.mark.asyncio
async def test_create_preserves_predecessor_execute_output_reservation(blob_service, db_engine, session_id, compose_context) -> None:
    run_id = UUID(
        await _seed_active_run(
            db_engine,
            session_id,
            session_operation_context=compose_context,
            source={
                "plugin": "csv",
                "on_success": "rows",
                "options": {"path": "input.csv"},
                "on_validation_failure": "discard",
            },
        )
    )
    execute_context = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    output = _reserve_output_blob(blob_service, execute_context, run_id)
    output_path = Path(output.storage_path)
    output_bytes = b"partial pipeline output\n"
    output_path.write_bytes(output_bytes)
    with db_engine.connect() as connection:
        before = connection.execute(select(blobs_table).where(blobs_table.c.id == str(output.id))).one()

    successor = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.COMPOSE)
    created = await blob_service.create_blob(session_id, "next.csv", b"a\n1\n", "text/csv", session_operation_context=successor)

    assert created.status == "ready"
    assert Path(created.storage_path).read_bytes() == b"a\n1\n"
    assert output_path.read_bytes() == output_bytes
    with db_engine.connect() as connection:
        after = connection.execute(select(blobs_table).where(blobs_table.c.id == str(output.id))).one()
        links = connection.execute(select(blob_run_links_table).where(blob_run_links_table.c.blob_id == str(output.id))).all()
    assert after == before
    assert [(link.run_id, link.direction) for link in links] == [(str(run_id), "output")]


@pytest.mark.asyncio
async def test_creation_recovery_still_refuses_corrupt_predecessor_storage(
    blob_service, db_engine, session_id, compose_context, tmp_path
) -> None:
    predecessor = await blob_service.create_blob(
        session_id, "earlier.csv", b"a\n1\n", "text/csv", session_operation_context=compose_context
    )
    original_path = Path(predecessor.storage_path)
    foreign_path = tmp_path / "not-owned-by-the-reservation.csv"
    foreign_bytes = b"must not be removed\n"
    foreign_path.write_bytes(foreign_bytes)
    # Deliberately corrupt owned metadata after admission. The recovery reader
    # must still check standalone creation evidence, not skip all predecessor rows.
    with db_engine.begin() as connection:
        connection.execute(
            update(blobs_table)
            .where(blobs_table.c.id == str(predecessor.id))
            .values(
                status="pending",
                storage_path=str(foreign_path),
                custody_operation_id=compose_context.fence.operation_id,
                custody_operation_epoch=compose_context.fence.operation_epoch,
                custody_operation_kind=compose_context.operation_kind.value,
            )
        )
    successor = seed_live_operation_context(db_engine, session_id, operation_kind=SessionOperationKind.COMPOSE)

    with pytest.raises(AuditIntegrityError, match="abandoned blob reservation storage escaped exact custody"):
        await blob_service.create_blob(session_id, "next.csv", b"a\n2\n", "text/csv", session_operation_context=successor)

    assert original_path.read_bytes() == b"a\n1\n"
    assert foreign_path.read_bytes() == foreign_bytes
    with db_engine.connect() as connection:
        retained = connection.execute(select(blobs_table.c.id).where(blobs_table.c.session_id == str(session_id))).scalars().all()
    assert retained == [str(predecessor.id)]
