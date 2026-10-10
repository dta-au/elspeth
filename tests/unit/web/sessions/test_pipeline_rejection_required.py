"""Actual rejection SQL/result/required projection custody controls."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.models import composition_proposals_table, proposal_events_table
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
from elspeth.web.sessions.pipeline_rejection_finish_once import (
    PipelineRejectionRaised,
    PipelineRejectionReturned,
    decode_pipeline_rejection_result,
)
from elspeth.web.sessions.protocol import RedactedPipelineArguments
from tests.unit.web.sessions.test_atomic_pipeline_review_evidence import _prepared_pipeline

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


async def prepared_rejection(store):
    _engine, _repository, _authority, service, sid = store
    row, _arguments, _drafts, _record, running = await _prepared_pipeline(store)
    authority = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    expected = PipelineRejectionExpected(
        authority, "operator_rejected", None, "user:alice", "alice", running.session_operation_context, running
    )
    return expected, running


def rejection_work(expected):
    running = expected.running
    assert running is not None
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            expected.context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
            proposal_id=str(expected.authority.row.id),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    sql, projection = coordinator.reserve_pair(
        RequiredWorkSource.PROPOSAL_REJECTION_SQL,
        RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION,
        transition_ordinal=4,
        semantic_ordinal=7,
    )
    return coordinator, sql, projection


async def invoke(service, expected, coordinator, sql, projection):
    return await service.reject_pipeline_composition_proposal_finish_once(
        expected=expected,
        coordinator=coordinator,
        rejection_work=sql,
        rejection_projection_work=projection,
        transition_ordinal=4,
        semantic_ordinal=7,
    )


def project_returned(coordinator, sql, projection, handoff, expected):
    assert type(handoff) is PipelineRejectionReturned
    coordinator.verify_rejection_returned(rejection_ticket=sql, actual_outcome=handoff.sql_outcome)
    projection.begin_projection()
    try:
        row = decode_pipeline_rejection_result(handoff.sql_outcome.value, expected)
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    return row


@pytest.mark.asyncio
async def test_rejection_actual_event_and_authorized_immutable_reuse(operation_store):
    engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    try:
        coordinator, sql, projection = rejection_work(expected)
        row = project_returned(coordinator, sql, projection, await invoke(service, expected, coordinator, sql, projection), expected)
        coordinator.assert_completed()
        with engine.connect() as conn:
            first = conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == str(row.audit_event_id))).one()
        repeated_authority = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
        repeated = replace(expected, authority=repeated_authority)
        next_coordinator, next_sql, next_projection = rejection_work(repeated)
        statements = []

        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", observe)
        try:
            handoff = await invoke(service, repeated, next_coordinator, next_sql, next_projection)
            reused = project_returned(next_coordinator, next_sql, next_projection, handoff, repeated)
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert reused == row
        assert not handoff.sql_outcome.value.transitioned
        assert not any(statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
        with engine.connect() as conn:
            second = conn.execute(select(proposal_events_table).where(proposal_events_table.c.id == str(row.audit_event_id))).one()
        assert second == first
        next_coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_rejection_physical_failure_rolls_back_and_issues_only_failure_unused(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    fault = OperationalError("controlled actual rejection CAS", {}, RuntimeError("controlled"))

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise fault

    event.listen(engine, "before_cursor_execute", fail)
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionRaised
        assert handoff.sql_outcome.error is fault
        projection.complete_unused(handoff.projection_unused)
        coordinator.assert_completed()
        with engine.connect() as conn:
            status = conn.execute(
                select(composition_proposals_table.c.status).where(composition_proposals_table.c.id == str(expected.authority.row.id))
            ).scalar_one()
            rejected = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
        assert status == "pending" and rejected == []
    finally:
        event.remove(engine, "before_cursor_execute", fail)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_foreign_rejection_pair_does_not_complete_foreign_work(operation_store):
    _engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    foreign, _foreign_sql, foreign_projection = rejection_work(expected)
    try:
        with pytest.raises(AuditIntegrityError):
            await invoke(service, expected, coordinator, sql, foreign_projection)
        assert not sql.complete and not projection.complete and not foreign_projection.complete
        assert not foreign.all_completed
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_rejection_result_survives_repeated_outer_cancellation_until_pure_projection(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    entered, release = threading.Event(), threading.Event()

    def park(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            entered.set()
            assert release.wait(5)

    event.listen(engine, "before_cursor_execute", park)
    task = asyncio.create_task(invoke(service, expected, coordinator, sql, projection))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        for index in range(3):
            task.cancel(f"actual-cancel-{index}")
            await asyncio.sleep(0.03)
            assert not task.done()
        assert not sql.complete and not projection.complete
        release.set()
        handoff = await task
        assert type(handoff) is PipelineRejectionReturned
        assert [cancel.args for cancel in handoff.sql_outcome.deferred_cancellations] == [(f"actual-cancel-{index}",) for index in range(3)]
        assert sql.complete and not projection.complete
        assert len({id(cancel) for cancel in handoff.sql_outcome.deferred_cancellations}) == 3
        project_returned(coordinator, sql, projection, handoff, expected)
        coordinator.assert_completed()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["actor", "payload", "tool", "committed_pointer", "metadata"])
async def test_rejection_pure_projection_rejects_corrupt_actual_material(operation_store, corruption):
    _engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionReturned
        result = handoff.sql_outcome.value
        if corruption == "actor":
            result = replace(result, event=replace(result.event, actor="user:foreign"))
        elif corruption == "payload":
            result = replace(result, event=replace(result.event, payload={**result.event.payload, "reason_code": "superseded"}))
        elif corruption == "tool":
            result = replace(result, record=replace(result.record, tool_call_id="foreign-provider-string"))
        elif corruption == "committed_pointer":
            result = replace(result, record=replace(result.record, committed_state_id=result.record.id))
        else:
            result = replace(result, record=replace(result.record, pipeline_metadata=None))
        projection.begin_projection()
        with pytest.raises(AuditIntegrityError) as failure:
            decode_pipeline_rejection_result(result, expected)
        projection.complete_owned(failure.value)
        coordinator.assert_completed()
        assert coordinator.failure_receipts()[0].key.source is RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_rejection_success_plus_telemetry_failure_still_requires_actual_projection(operation_store, monkeypatch):
    from elspeth.web.sessions import service as service_module

    _engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    original = RuntimeError("controlled owned post-SQL telemetry failure")

    class FailedCounter:
        def add(self, amount, attributes):
            raise original

    monkeypatch.setattr(service_module, "_PIPELINE_SETTLEMENT_COUNTER", FailedCounter())
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionReturned
        assert handoff.post_sql_failures == (original,)
        assert sql.complete and not projection.complete
        assert (
            coordinator.issue_rejection_projection_unused(
                rejection_ticket=sql,
                projection_ticket=projection,
                actual_outcome=handoff.sql_outcome,
            )
            is None
        )
        projected = project_returned(coordinator, sql, projection, handoff, expected)
        assert projected.status == "rejected"
        coordinator.assert_completed()
        assert sql.receipts() == ()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_rejection_foreign_independent_principal_is_a_known_physical_refusal(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    foreign = replace(expected, actor="user:mallory", actor_user_id="mallory")
    try:
        handoff = await invoke(service, foreign, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionRaised
        assert type(handoff.sql_outcome.error) is AuditIntegrityError
        assert sql.complete
        projection.complete_unused(handoff.projection_unused)
        coordinator.assert_completed()
        with engine.connect() as conn:
            rejected = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
        assert rejected == []
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_creation_finish_once_preserves_actual_row_and_all_cancellations(operation_store):
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineCreationReturned

    engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    parent = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    creation, projected = parent.reserve_pair(
        RequiredWorkSource.PROPOSAL_CREATION_SQL,
        RequiredWorkSource.PROPOSAL_CREATION_PROJECTION,
        transition_ordinal=6,
        semantic_ordinal=9,
    )
    original = expected.authority.row
    plan = PipelinePlanResult(
        proposal=expected.authority.proposal,
        tool_call_id="actual-next-provider-string",
        custody_result="not_required",
        model_identifier="model",
        model_version="version",
        provider="test",
    )
    entered, release = threading.Event(), threading.Event()

    def park(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO composition_proposals"):
            entered.set()
            assert release.wait(5)

    event.listen(engine, "before_cursor_execute", park)
    task = asyncio.create_task(
        service.create_pipeline_composition_proposal_finish_once(
            session_id=sid,
            plan=plan,
            summary=original.summary,
            rationale=original.rationale,
            affects=original.affects,
            arguments_redacted_json=RedactedPipelineArguments(original.arguments_redacted_json),
            actor="composer-web:user:alice",
            composer_model_identifier="model",
            composer_model_version="version",
            composer_provider="test",
            user_message_id=original.user_message_id,
            session_operation_context=running.session_operation_context,
            required_work=creation,
            running=running,
        )
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        for index in range(3):
            task.cancel(f"creation-original-{index}")
            await asyncio.sleep(0.03)
            assert not task.done()
        release.set()
        handoff = await task
        assert type(handoff) is PipelineCreationReturned
        assert [cancel.args for cancel in handoff.sql_outcome.deferred_cancellations] == [
            (f"creation-original-{index}",) for index in range(3)
        ]
        parent.verify_creation_outcome(creation_ticket=creation, actual_outcome=handoff.sql_outcome)
        projected.begin_projection()
        assert handoff.sql_outcome.value.tool_call_id == plan.tool_call_id
        assert handoff.sql_outcome.value.id != original.id
        assert handoff.sql_outcome.value.status == "pending"
        projected.complete_owned()
        parent.assert_completed()
        with engine.connect() as conn:
            persisted = conn.execute(
                select(composition_proposals_table).where(composition_proposals_table.c.id == str(handoff.sql_outcome.value.id))
            ).one()
        assert persisted.tool_call_id == plan.tool_call_id
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", ["stop", "deadline"])
async def test_rejection_business_cannot_cross_durable_stop_or_database_deadline(operation_store, monkeypatch, signal):
    from datetime import timedelta

    from elspeth.web.coordination import composer_operation_authority as authority_module

    engine, repository, authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    if signal == "stop":
        authority.request_cancel(
            session_id=sid,
            operation_id=running.claim.operation_id,
            cancelled_failure=lambda request_id: pytest.fail("running cancellation must not settle queued work"),
        )
    else:
        current = authority.get(session_id=sid, operation_id=running.claim.operation_id)
        assert current is not None
        monkeypatch.setattr(authority_module, "database_now", lambda conn: current.deadline_at + timedelta(microseconds=1))
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionRaised
        projection.complete_unused(handoff.projection_unused)
        coordinator.assert_completed()
        with engine.connect() as conn:
            status = conn.execute(
                select(composition_proposals_table.c.status).where(composition_proposals_table.c.id == str(expected.authority.row.id))
            ).scalar_one()
            rejected = conn.execute(
                select(proposal_events_table).where(
                    proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                    proposal_events_table.c.event_type == "proposal.rejected",
                )
            ).all()
        assert status == "pending" and rejected == []
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_rejection_forged_same_value_carrier_cannot_project_or_issue_unused(operation_store):
    from elspeth.web.required_sql_outcomes import RequiredSQLReturned

    _engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionReturned
        forged = RequiredSQLReturned(handoff.sql_outcome.value, handoff.sql_outcome.deferred_cancellations)
        assert forged is not handoff.sql_outcome
        with pytest.raises(AuditIntegrityError):
            coordinator.verify_rejection_returned(rejection_ticket=sql, actual_outcome=forged)
        with pytest.raises(AuditIntegrityError):
            coordinator.issue_rejection_projection_unused(rejection_ticket=sql, projection_ticket=projection, actual_outcome=forged)
        assert not projection.complete
        project_returned(coordinator, sql, projection, handoff, expected)
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_rejection_wrong_semantic_pair_refuses_before_physical_dispatch(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(AuditIntegrityError):
            await service.reject_pipeline_composition_proposal_finish_once(
                expected=expected,
                coordinator=coordinator,
                rejection_work=sql,
                rejection_projection_work=projection,
                transition_ordinal=4,
                semantic_ordinal=8,
            )
        assert statements == []
        assert not sql.complete and not projection.complete
    finally:
        event.remove(engine, "before_cursor_execute", observe)
        repository.release(running.session_operation_context)


def test_explicit_body_cleanup_and_cancellation_objects_are_retained():
    from elspeth.web.sessions.pipeline_rejection_custody import original_outcome_group

    body = RuntimeError("actual body")
    cleanup = OperationalError("actual cleanup", {}, RuntimeError("actual driver"))
    cancellation = asyncio.CancelledError("actual original")
    grouped = original_outcome_group("actual originals", body, BaseExceptionGroup("cleanup", [cleanup, cancellation]), body)
    assert isinstance(grouped, BaseExceptionGroup)
    assert grouped.exceptions == (body, cleanup, cancellation)
    reverse = original_outcome_group("reverse arrival", cancellation, cleanup, body)
    assert reverse.exceptions == (cancellation, cleanup, body)
