"""Pin record_blob_inline_resolutions audit writes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import insert, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blob_inline_resolutions_table,
    blobs_table,
    composition_states_table,
    runs_table,
    session_operation_fences_table,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.fixture
def engine():
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def service(engine):
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.record-blob-inline-resolutions"),
    )


@pytest.mark.asyncio
async def test_record_blob_inline_resolutions_writes_one_row_per_resolution(service, engine) -> None:
    session = await service.create_session("writer-test-user", "Writer test", "local")
    run_id = uuid4()
    blob_id = uuid4()
    with engine.begin() as conn:
        _seed_run_and_blob(conn, session_id=session.id, run_id=run_id, blob_id=blob_id)

    execute_context = service.session_operation_authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    try:
        await service.record_blob_inline_resolutions(
            run_id=run_id,
            resolutions=[
                ResolvedBlobContent(
                    field_path="source.options.system_prompt",
                    blob_id=blob_id,
                    content_hash="a" * 64,
                    byte_length=42,
                    mime_type="text/plain",
                    encoding="utf-8",
                )
            ],
            attempt=1,
            session_operation_context=execute_context,
        )
    finally:
        service.session_operation_authority.release(execute_context)

    with engine.connect() as conn:
        rows = conn.execute(select(blob_inline_resolutions_table)).fetchall()

    assert len(rows) == 1
    assert rows[0].run_id == str(run_id)
    assert rows[0].blob_id == str(blob_id)
    assert rows[0].field_path == "source.options.system_prompt"
    assert rows[0].byte_length == 42


@pytest.mark.asyncio
async def test_record_blob_inline_resolutions_skips_empty_batch(service, engine) -> None:
    session = await service.create_session("writer-test-user", "Empty inline resolution batch", "local")
    execute_context = service.session_operation_authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    try:
        await service.record_blob_inline_resolutions(
            run_id=uuid4(),
            resolutions=[],
            attempt=1,
            session_operation_context=execute_context,
        )
    finally:
        service.session_operation_authority.release(execute_context)

    with engine.connect() as conn:
        rows = conn.execute(select(blob_inline_resolutions_table)).fetchall()

    assert rows == []


@pytest.mark.asyncio
async def test_record_blob_inline_resolutions_raises_audit_integrity_error_on_db_failure() -> None:
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    service = SessionServiceImpl(
        eng,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.record-blob-inline-resolutions"),
    )
    initialize_session_schema(eng)
    session = await service.create_session("writer-test-user", "Writer test", "local")
    run_id = uuid4()
    blob_id = uuid4()
    with eng.begin() as conn:
        _seed_run_and_blob(conn, session_id=session.id, run_id=run_id, blob_id=blob_id)
    execute_context = service.session_operation_authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    blob_inline_resolutions_table.drop(eng)

    try:
        with pytest.raises(AuditIntegrityError, match="failed to record blob_inline_resolutions"):
            await service.record_blob_inline_resolutions(
                run_id=run_id,
                resolutions=[
                    ResolvedBlobContent(
                        field_path="source.options.system_prompt",
                        blob_id=blob_id,
                        content_hash="a" * 64,
                        byte_length=42,
                        mime_type="text/plain",
                        encoding="utf-8",
                    )
                ],
                attempt=1,
                session_operation_context=execute_context,
            )
    finally:
        service.session_operation_authority.release(execute_context)


@pytest.mark.asyncio
async def test_record_blob_inline_resolutions_empty_batch_wraps_cas_database_failure() -> None:
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    service = SessionServiceImpl(
        eng,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.record-blob-inline-resolutions"),
    )
    initialize_session_schema(eng)
    session = await service.create_session("writer-test-user", "Empty batch database failure", "local")
    execute_context = service.session_operation_authority.acquire(
        session_id=session.id,
        operation_kind=SessionOperationKind.EXECUTE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    session_operation_fences_table.drop(eng)

    with pytest.raises(AuditIntegrityError, match="failed to record blob_inline_resolutions"):
        await service.record_blob_inline_resolutions(
            run_id=uuid4(),
            resolutions=[],
            attempt=1,
            session_operation_context=execute_context,
        )


def _seed_run_and_blob(conn, *, session_id: UUID, run_id: UUID, blob_id: UUID) -> None:
    state_id = str(uuid4())
    now = datetime.now(UTC)
    conn.execute(
        insert(composition_states_table).values(
            id=state_id,
            session_id=str(session_id),
            version=1,
            source=None,
            nodes=None,
            edges=None,
            outputs=None,
            metadata_=None,
            is_valid=True,
            validation_errors=None,
            composer_meta=None,
            created_at=now,
            derived_from_state_id=None,
            provenance="session_seed",
        )
    )
    conn.execute(
        insert(runs_table).values(
            id=str(run_id),
            session_id=str(session_id),
            state_id=state_id,
            status="pending",
            started_at=now,
            finished_at=None,
            rows_processed=0,
            rows_succeeded=0,
            rows_failed=0,
            rows_routed_success=0,
            rows_routed_failure=0,
            rows_quarantined=0,
            error=None,
            landscape_run_id=None,
            pipeline_yaml=None,
        )
    )
    conn.execute(
        insert(blobs_table).values(
            id=str(blob_id),
            session_id=str(session_id),
            filename="prompt.txt",
            mime_type="text/plain",
            size_bytes=42,
            content_hash="a" * 64,
            storage_path="/tmp/prompt.txt",
            created_at=now,
            created_by="user",
            source_description=None,
            status="ready",
            creation_modality="verbatim",
        )
    )
