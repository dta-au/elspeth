"""PostgreSQL proofs for run-diagnostics audit authority ordering."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import Engine, event, func, select

from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    RunDiagnosticsAuditAuthority,
    RunDiagnosticsAuthorityLostError,
    RunRecord,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture()
def deployment(
    external_deployment_postgres_url: str,
) -> Iterator[tuple[Engine, Engine, SessionServiceImpl, SessionServiceImpl]]:
    diagnostics_engine = create_session_engine(external_deployment_postgres_url)
    archive_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(diagnostics_engine)
    diagnostics = SessionServiceImpl(
        diagnostics_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-run-diagnostics"),
        owner_instance_id=f"run-diagnostics-{uuid4()}",
    )
    archive = SessionServiceImpl(
        archive_engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.pg-run-diagnostics-archive"),
        owner_instance_id=f"run-diagnostics-archive-{uuid4()}",
    )
    try:
        yield diagnostics_engine, archive_engine, diagnostics, archive
    finally:
        diagnostics_engine.dispose()
        archive_engine.dispose()


async def _create_pending_run(service: SessionServiceImpl) -> RunRecord:
    session = await service.create_session(str(uuid4()), "Pipeline", "local")
    compose_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        state = await service.save_composition_state(
            session.id,
            CompositionStateData(is_valid=True),
            provenance="session_seed",
            session_operation_context=compose_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, compose_context)
    execute_context = await service._run_sync(
        lambda: service.session_operation_authority.acquire(
            session_id=session.id,
            operation_kind=SessionOperationKind.EXECUTE,
            owner_instance_id=service.session_operation_owner_instance_id,
            lease_seconds=service.session_operation_lease_seconds,
        )
    )
    try:
        return await service.create_run(
            session.id,
            state.id,
            session_operation_context=execute_context,
        )
    finally:
        await service._run_sync(service.session_operation_authority.release, execute_context)


def _message_count(engine: Engine, session_id: UUID) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(session_id))
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_archive_winning_advisory_lock_forces_diagnostics_recheck_and_refusal(deployment) -> None:
    diagnostics_engine, archive_engine, diagnostics, archive = deployment
    run = await _create_pending_run(diagnostics)
    authority = RunDiagnosticsAuditAuthority(
        run_id=run.id,
        session_id=run.session_id,
        state_id=run.state_id,
    )
    archive_has_lock = threading.Event()
    allow_archive_commit = threading.Event()
    diagnostics_attempted_lock = threading.Event()

    def pause_archive_after_lock(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update sessions set") and "archived_at" in normalized:
            archive_has_lock.set()
            if not allow_archive_commit.wait(timeout=10):
                raise TimeoutError("archive test barrier timed out")

    def observe_diagnostics_lock_attempt(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "pg_catalog.pg_advisory_xact_lock" in " ".join(statement.lower().split()):
            diagnostics_attempted_lock.set()

    event.listen(archive_engine, "before_cursor_execute", pause_archive_after_lock)
    event.listen(diagnostics_engine, "before_cursor_execute", observe_diagnostics_lock_attempt)
    try:
        archive_task = asyncio.create_task(archive.archive_session(run.session_id))
        assert await asyncio.to_thread(archive_has_lock.wait, 10), "archive never reached its locked update"
        diagnostics_task = asyncio.create_task(
            diagnostics.add_run_diagnostics_audit_message(
                authority,
                "must not survive archive",
                tool_calls=[{"_kind": "llm_call_audit", "run_id": str(run.id)}],
            )
        )
        assert await asyncio.to_thread(diagnostics_attempted_lock.wait, 10), "diagnostics never attempted the session lock"
        allow_archive_commit.set()
        await archive_task
        with pytest.raises(RunDiagnosticsAuthorityLostError) as exc_info:
            await diagnostics_task
    finally:
        allow_archive_commit.set()
        event.remove(archive_engine, "before_cursor_execute", pause_archive_after_lock)
        event.remove(diagnostics_engine, "before_cursor_execute", observe_diagnostics_lock_attempt)

    assert exc_info.value.reason == "session_archived"
    assert _message_count(diagnostics_engine, run.session_id) == 0, "refusal must not consume/persist a chat sequence"


@pytest.mark.asyncio
async def test_diagnostics_winning_advisory_lock_commits_before_archive(deployment) -> None:
    diagnostics_engine, archive_engine, diagnostics, archive = deployment
    run = await _create_pending_run(diagnostics)
    authority = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=run.session_id, state_id=run.state_id)
    diagnostics_has_lock = threading.Event()
    allow_diagnostics_commit = threading.Event()
    archive_attempted_lock = threading.Event()
    archive_reached_update = threading.Event()

    def pause_diagnostics_insert(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("insert into ") and chat_messages_table.name in normalized:
            diagnostics_has_lock.set()
            if not allow_diagnostics_commit.wait(timeout=10):
                raise TimeoutError("diagnostics test barrier timed out")

    def observe_archive(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if "pg_catalog.pg_advisory_xact_lock" in normalized:
            archive_attempted_lock.set()
        if normalized.startswith("update sessions set") and "archived_at" in normalized:
            archive_reached_update.set()

    event.listen(diagnostics_engine, "before_cursor_execute", pause_diagnostics_insert)
    event.listen(archive_engine, "before_cursor_execute", observe_archive)
    try:
        diagnostics_task = asyncio.create_task(
            diagnostics.add_run_diagnostics_audit_message(
                authority,
                "commits before archive",
                tool_calls=[{"_kind": "llm_call_audit", "run_id": str(run.id)}],
            )
        )
        assert await asyncio.to_thread(diagnostics_has_lock.wait, 10), "diagnostics never reached its locked insert"
        archive_task = asyncio.create_task(archive.archive_session(run.session_id))
        assert await asyncio.to_thread(archive_attempted_lock.wait, 10), "archive never attempted the session lock"
        assert not await asyncio.to_thread(archive_reached_update.wait, 0.2), "archive update bypassed diagnostics lock"
        allow_diagnostics_commit.set()
        record = await diagnostics_task
        await archive_task
    finally:
        allow_diagnostics_commit.set()
        event.remove(diagnostics_engine, "before_cursor_execute", pause_diagnostics_insert)
        event.remove(archive_engine, "before_cursor_execute", observe_archive)

    assert record.writer_principal == "run_diagnostics"
    assert _message_count(diagnostics_engine, run.session_id) == 1
    assert (await archive.get_session(run.session_id)).archived_at is not None
