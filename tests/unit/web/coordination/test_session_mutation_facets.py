"""``_RepositorySessionMutations`` message/state facets: exact COMPOSE-or-PROPOSAL custody (P4-D6 family A2a)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import select

from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.repository import SessionDerivedCustodyError, _RepositoryMutationState, _RepositorySessionMutations
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import composition_rejection_events_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry


@pytest.fixture
def file_engine(tmp_path: Path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _service(engine) -> SessionServiceImpl:
    return SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.session-facets"))


async def _create_session(service: SessionServiceImpl) -> UUID:
    return (await service.create_session("alice", "Facet custody", "local")).id


async def _acquire_compose_context(service: SessionServiceImpl, session_id: UUID) -> SessionOperationContext:
    return cast(
        SessionOperationContext,
        await service._run_sync(
            lambda: service.session_operation_authority.acquire(
                session_id=session_id,
                operation_kind=SessionOperationKind.COMPOSE,
                owner_instance_id=service.session_operation_owner_instance_id,
                lease_seconds=service.session_operation_lease_seconds,
            )
        ),
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _updated_at(engine, session_id: str) -> datetime:
    with engine.connect() as conn:
        return _utc(conn.execute(select(sessions_table.c.updated_at).where(sessions_table.c.id == session_id)).scalar_one())


def _run_facet(service: SessionServiceImpl, session_id: str, context: SessionOperationContext, action) -> None:
    with service._session_process_locked_begin(session_id) as conn, service._session_write_lock(conn, session_id):
        state = _RepositoryMutationState(conn, session_id=session_id, database_now=datetime.now(UTC), operation_context=context)
        try:
            action(_RepositorySessionMutations(state))
        finally:
            state._close()


@pytest.mark.asyncio
async def test_mark_session_updated_lands_under_compose_or_proposal_and_refuses_other_custody(file_engine) -> None:
    """The facet checks the exact operation shape before its UPDATE: COMPOSE or
    PROPOSAL over the bound session lands the caller's stamp; any other kind,
    or a context for another session, is refused and writes nothing. (The
    live fence row is the enclosing mutation transaction's proof, as for
    ``record_plugin_crash_breadcrumb``.)"""
    service = _service(file_engine)
    session_id = await _create_session(service)
    sid = str(session_id)
    compose = await _acquire_compose_context(service, session_id)
    landed = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    proposal_landed = datetime(2031, 6, 7, 8, 9, 10, tzinfo=UTC)
    refused = datetime(2032, 1, 2, 3, 4, 5, tzinfo=UTC)

    await service._run_sync(lambda: _run_facet(service, sid, compose, lambda facet: facet.mark_session_updated(updated_at=landed)))
    assert _updated_at(file_engine, sid) == landed

    proposal = SessionOperationContext(fence=compose.fence, operation_kind=SessionOperationKind.PROPOSAL)
    await service._run_sync(
        lambda: _run_facet(service, sid, proposal, lambda facet: facet.mark_session_updated(updated_at=proposal_landed))
    )
    assert _updated_at(file_engine, sid) == proposal_landed

    blob_read = SessionOperationContext(fence=compose.fence, operation_kind=SessionOperationKind.BLOB_READ)
    with pytest.raises(SessionOperationFenceLost):
        await service._run_sync(lambda: _run_facet(service, sid, blob_read, lambda facet: facet.mark_session_updated(updated_at=refused)))
    assert _updated_at(file_engine, sid) == proposal_landed

    foreign = SessionOperationContext(fence=replace(compose.fence, session_id=str(uuid4())), operation_kind=SessionOperationKind.COMPOSE)
    with pytest.raises(SessionOperationFenceLost):
        await service._run_sync(lambda: _run_facet(service, sid, foreign, lambda facet: facet.mark_session_updated(updated_at=refused)))
    assert _updated_at(file_engine, sid) == proposal_landed

    with pytest.raises(TypeError):
        await service._run_sync(
            lambda: _run_facet(service, sid, compose, lambda facet: facet.mark_session_updated(updated_at=cast(datetime, "2031")))
        )
    assert _updated_at(file_engine, sid) == proposal_landed


@pytest.mark.asyncio
async def test_mark_session_updated_refuses_a_session_row_that_is_not_there(file_engine) -> None:
    service = _service(file_engine)
    session_id = await _create_session(service)
    compose = await _acquire_compose_context(service, session_id)
    ghost = str(uuid4())
    ghost_context = SessionOperationContext(fence=replace(compose.fence, session_id=ghost), operation_kind=SessionOperationKind.COMPOSE)
    with pytest.raises(SessionDerivedCustodyError):
        await service._run_sync(
            lambda: _run_facet(
                service,
                ghost,
                ghost_context,
                lambda facet: facet.mark_session_updated(updated_at=datetime(2031, 1, 1, tzinfo=UTC)),
            )
        )


@pytest.mark.asyncio
async def test_record_composition_rejection_persists_the_unredacted_reason_under_compose_only(file_engine) -> None:
    service = _service(file_engine)
    session_id = await _create_session(service)
    sid = str(session_id)
    compose = await _acquire_compose_context(service, session_id)
    created_at = datetime(2031, 3, 4, 5, 6, 7, tzinfo=UTC)

    def _record(facet: _RepositorySessionMutations) -> None:
        facet.record_composition_rejection(
            tool_call_id="call-1",
            tool_name="upsert_node",
            error_code="E_SCHEMA",
            message="node options rejected",
            planner_payload='{"raw": "planner text"}',
            composition_state_id=None,
            created_at=created_at,
        )

    await service._run_sync(lambda: _run_facet(service, sid, compose, _record))
    with file_engine.connect() as conn:
        rows = conn.execute(select(composition_rejection_events_table).where(composition_rejection_events_table.c.session_id == sid)).all()
    assert len(rows) == 1
    row = rows[0]
    assert (row.tool_call_id, row.tool_name, row.error_code, row.message, row.planner_payload, row.composition_state_id) == (
        "call-1",
        "upsert_node",
        "E_SCHEMA",
        "node options rejected",
        '{"raw": "planner text"}',
        None,
    )
    assert _utc(row.created_at) == created_at

    proposal = SessionOperationContext(fence=compose.fence, operation_kind=SessionOperationKind.PROPOSAL)
    with pytest.raises(SessionOperationFenceLost):
        await service._run_sync(lambda: _run_facet(service, sid, proposal, _record))
    foreign = SessionOperationContext(fence=replace(compose.fence, session_id=str(uuid4())), operation_kind=SessionOperationKind.COMPOSE)
    with pytest.raises(SessionOperationFenceLost):
        await service._run_sync(lambda: _run_facet(service, sid, foreign, _record))
    with file_engine.connect() as conn:
        assert conn.execute(select(composition_rejection_events_table.c.id)).all().__len__() == 1
