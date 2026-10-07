"""UNRUN exact physical51/result52 witnesses complement retained V7 tests."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import SOURCE_MAPPING, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.models import composition_proposals_table, proposal_events_table
from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineRejectionRaised, PipelineRejectionReturned
from tests.unit.web.sessions.test_pipeline_rejection_required import (
    invoke,
    prepared_rejection,
    project_returned,
    rejection_work,
)

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
async def test_native_source51_actual_future_result52_event_and_once_only_pair(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    try:
        assert type(coordinator) is RequiredWorkCoordinator
        assert type(expected.context) is SessionOperationContext
        assert coordinator.authority.context is expected.context
        assert sql.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL
        assert projection.key.source is RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION
        assert int(sql.key.source) == 51 and int(projection.key.source) == 52
        assert tuple(int(value) for value in SOURCE_MAPPING[sql.key.source]) == (7, 2)
        assert tuple(int(value) for value in SOURCE_MAPPING[projection.key.source]) == (7, 5)
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionReturned
        assert type(handoff.sql_outcome) is RequiredSQLReturned
        future = sql._future
        assert type(future) is Future and future.done() and not future.cancelled()
        assert future.result() is handoff.sql_outcome.value
        assert sql._finish_once_handoff is handoff.sql_outcome
        assert sql.complete and not projection.complete
        assert projection._future is None and projection._unused_metadata is None
        row = project_returned(coordinator, sql, projection, handoff, expected)
        coordinator.assert_completed()
        assert projection.complete and sql._future is future
        assert coordinator.failure_receipts() == ()
        with engine.connect() as conn:
            durable = conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == str(row.audit_event_id))).one()
        assert durable.proposal_id == str(expected.authority.row.id)
        assert durable.actor == expected.actor and durable.event_type == "proposal.rejected"
        assert durable.payload["reason_code"] == expected.reason
        with pytest.raises(AuditIntegrityError):
            await invoke(service, expected, coordinator, sql, projection)
        assert sql._future is future and sql._finish_once_handoff is handoff.sql_outcome
        with engine.connect() as conn:
            events = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
        assert len(events) == 1
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_native_source51_original_future_exception_issued_unused52_and_actual_rollback(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    original = OperationalError("native exact rejection failure", {}, RuntimeError("native exact driver"))

    def fail_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise original

    event.listen(engine, "before_cursor_execute", fail_sql)
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionRaised and type(handoff.sql_outcome) is RequiredSQLRaised
        future = sql._future
        assert type(future) is Future and future.done() and not future.cancelled()
        assert future.exception() is original and handoff.sql_outcome.error is original
        cause, context = original.__cause__, original.__context__
        assert sql._finish_once_handoff is handoff.sql_outcome
        assert any(error is original for error in sql.errors)
        assert sql.complete and not projection.complete
        assert projection._future is None
        with pytest.raises(AuditIntegrityError):
            projection.complete_unused(replace(handoff.projection_unused))
        assert not projection.complete
        projection.complete_unused(handoff.projection_unused)
        assert projection._unused_metadata is handoff.projection_unused
        coordinator.assert_completed()
        assert original.__cause__ is cause and original.__context__ is context
        with engine.connect() as conn:
            status = conn.execute(
                select(composition_proposals_table.c.status).where(composition_proposals_table.c.id == str(expected.authority.row.id))
            ).scalar_one()
            events = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
        assert status == "pending" and events == []
        with pytest.raises(AuditIntegrityError):
            await invoke(service, expected, coordinator, sql, projection)
        assert sql._future is future and sql._finish_once_handoff is handoff.sql_outcome
    finally:
        event.remove(engine, "before_cursor_execute", fail_sql)
        repository.release(running.session_operation_context)
