"""The shared row writers and the public message/title/resolve entry points require a proved operation (P4-D6 family A2b)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import select, update

from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, composition_states_table, session_operation_fences_table, sessions_table
from elspeth.web.sessions.protocol import CompositionStateData, InterpretationChoice
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
    return SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.writer-boundaries"))


async def _create_session(service: SessionServiceImpl) -> UUID:
    return (await service.create_session("alice", "Writer boundaries", "local")).id


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


def _rows(engine, table, session_id: str) -> int:
    with engine.connect() as conn:
        return len(conn.execute(select(table.c.id).where(table.c.session_id == session_id)).all())


@pytest.mark.asyncio
async def test_public_message_and_title_writers_refuse_a_missing_operation(file_engine) -> None:
    """No ``None`` default survives: each entry point rejects an absent context
    before opening a transaction, and nothing is written."""
    service = _service(file_engine)
    session_id = await _create_session(service)
    sid = str(session_id)
    before_messages = _rows(file_engine, chat_messages_table, sid)
    with pytest.raises(TypeError):
        await service.add_message(
            session_id, "user", "hello", writer_principal="route_user_message", session_operation_context=cast(Any, None)
        )
    with pytest.raises(TypeError):
        await service.add_messages_atomic(session_id, (), writer_principal="compose_loop", session_operation_context=cast(Any, None))
    with pytest.raises(TypeError):
        await service.update_session_title(session_id, "renamed", session_operation_context=cast(Any, None))
    with pytest.raises(TypeError):
        await service.resolve_interpretation_event(
            session_id=session_id,
            event_id=uuid4(),
            choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
            amended_value=None,
            actor="user:alice",
            session_operation_context=cast(Any, None),
        )
    assert _rows(file_engine, chat_messages_table, sid) == before_messages
    with file_engine.connect() as conn:
        assert conn.execute(select(sessions_table.c.title).where(sessions_table.c.id == sid)).scalar_one() == "Writer boundaries"


@pytest.mark.asyncio
async def test_public_message_and_title_writers_land_under_a_live_compose_operation(file_engine) -> None:
    service = _service(file_engine)
    session_id = await _create_session(service)
    sid = str(session_id)
    compose = await _acquire_compose_context(service, session_id)
    message = await service.add_message(
        session_id, "user", "hello", writer_principal="route_user_message", session_operation_context=compose
    )
    assert message.content == "hello"
    renamed = await service.update_session_title(session_id, "renamed", session_operation_context=compose)
    assert renamed.title == "renamed"
    assert _rows(file_engine, chat_messages_table, sid) == 1


@pytest.mark.asyncio
async def test_shared_row_writers_prove_the_operation_on_the_connection_before_each_insert(file_engine) -> None:
    """``_insert_chat_message`` / ``_insert_composition_state`` are the
    SessionMutationAuthority boundaries: with the live COMPOSE fence the row
    lands; once that fence row is released the same call, in the same
    transaction, raises and inserts nothing."""
    service = _service(file_engine)
    session_id = await _create_session(service)
    sid = str(session_id)
    compose = await _acquire_compose_context(service, session_id)
    now = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    state = CompositionStateData(sources={}, nodes={}, edges=[], outputs={}, metadata_={}, is_valid=True, validation_errors=None)

    def _write_then_lose_authority() -> None:
        with service._session_process_locked_begin(sid) as conn, service._session_write_lock(conn, sid):
            sequence_no = service._reserve_sequence_range(conn, sid, count=2)
            service._insert_chat_message(
                conn,
                session_id=sid,
                role="user",
                content="landed",
                raw_content=None,
                tool_calls=None,
                sequence_no=sequence_no,
                writer_principal="route_user_message",
                composition_state_id=None,
                tool_call_id=None,
                parent_assistant_id=None,
                created_at=now,
                session_operation_context=compose,
            )
            service._insert_composition_state(
                conn,
                session_id=sid,
                payload=StatePayload(data=state, derived_from_state_id=None),
                provenance="session_seed",
                created_at=now,
                session_operation_context=compose,
            )
            conn.execute(
                update(session_operation_fences_table)
                .where(
                    session_operation_fences_table.c.session_id == sid,
                    session_operation_fences_table.c.operation_id == compose.fence.operation_id,
                )
                .values(released_at=datetime.now(UTC))
            )
            with pytest.raises(SessionOperationFenceLost):
                service._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="user",
                    content="refused",
                    raw_content=None,
                    tool_calls=None,
                    sequence_no=sequence_no + 1,
                    writer_principal="route_user_message",
                    composition_state_id=None,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                    session_operation_context=compose,
                )
            with pytest.raises(SessionOperationFenceLost):
                service._insert_composition_state(
                    conn,
                    session_id=sid,
                    payload=StatePayload(data=state, derived_from_state_id=None),
                    provenance="session_seed",
                    created_at=now,
                    session_operation_context=compose,
                )
            foreign = SessionOperationContext(fence=compose.fence, operation_kind=SessionOperationKind.BLOB_READ)
            with pytest.raises(SessionOperationFenceLost):
                service._insert_chat_message(
                    conn,
                    session_id=sid,
                    role="user",
                    content="refused",
                    raw_content=None,
                    tool_calls=None,
                    sequence_no=sequence_no + 1,
                    writer_principal="route_user_message",
                    composition_state_id=None,
                    tool_call_id=None,
                    parent_assistant_id=None,
                    created_at=now,
                    session_operation_context=foreign,
                )

    await service._run_sync(_write_then_lose_authority)
    assert _rows(file_engine, chat_messages_table, sid) == 1
    assert _rows(file_engine, composition_states_table, sid) == 1
