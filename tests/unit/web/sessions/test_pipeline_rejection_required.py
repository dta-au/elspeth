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
from elspeth.web.sessions.models import composition_proposals_table, composition_states_table, proposal_events_table
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
@pytest.mark.parametrize("telemetry_failure", [False, True])
@pytest.mark.parametrize(
    "rejection_fails",
    [False, True],
    ids=["rejection-commits", "rejection-rolls-back"],
)
async def test_creation_then_rejection_retains_actual_originals_at_nonzero_scope(
    operation_store, monkeypatch, telemetry_failure, rejection_fails
):
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
    from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkRole
    from elspeth.web.sessions import service as service_module
    from elspeth.web.sessions.pipeline_rejection import PipelineRejectionSQLResult
    from elspeth.web.sessions.pipeline_rejection_custody import (
        original_outcome_group,
        project_creation_handoff,
        reject_pipeline_with_required_custody,
    )
    from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineCreationReturned

    engine, repository, _authority, service, sid = operation_store
    existing, running = await prepared_rejection(operation_store)
    context = running.session_operation_context
    old = existing.authority.row
    assert old.user_message_id is not None
    creation_owner = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            context,
            durable_operation_id=running.claim.operation_id,
            claim_attempt=running.claim.attempt,
        )
    )
    creation_binding = RequiredWorkBinding(creation_owner, 7, 12, RequiredWorkRole.TURN, running)
    creation, creation_projection = creation_binding.reserve_pair(
        RequiredWorkSource.PROPOSAL_CREATION_SQL,
        RequiredWorkSource.PROPOSAL_CREATION_PROJECTION,
    )
    plan = PipelinePlanResult(
        proposal=existing.authority.proposal,
        tool_call_id="lower-level-new-provider-call",
        custody_result="not_required",
        model_identifier="model",
        model_version="version",
        provider="test",
    )
    telemetry_original = RuntimeError("actual creation post-SQL telemetry failure")
    if telemetry_failure:

        class FailedCounter:
            def add(self, amount, attributes):
                raise telemetry_original

        monkeypatch.setattr(service_module, "_PIPELINE_PLANNER_COUNTER", FailedCounter())

    create_entered, create_release = threading.Event(), threading.Event()
    reject_entered, reject_release = threading.Event(), threading.Event()
    sql_original = OperationalError("actual rejection SQL failure", {}, RuntimeError("actual driver"))
    created_cancellations = []
    rejected_cancellations = []
    creation_outcomes = []
    rejection_outcomes = []
    creation_handoffs = []
    creation_projection_failures = []
    observed_rejection = []
    rejection_owners = []
    actual_finish = service_module.run_required_sql_finish_once

    async def observe_finish(ticket, func):
        outcome = await actual_finish(ticket, func)
        if ticket.key.source is RequiredWorkSource.PROPOSAL_CREATION_SQL:
            creation_outcomes.append((ticket, outcome))
            created_cancellations.extend(outcome.deferred_cancellations)
        if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL:
            rejection_outcomes.append((ticket, outcome))
            rejected_cancellations.extend(outcome.deferred_cancellations)
        return outcome

    monkeypatch.setattr(service_module, "run_required_sql_finish_once", observe_finish)

    def hold_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO composition_proposals"):
            create_entered.set()
            assert create_release.wait(10)
        if statement.startswith("UPDATE composition_proposals"):
            reject_entered.set()
            assert reject_release.wait(10)
            if rejection_fails:
                raise sql_original

    event.listen(engine, "before_cursor_execute", hold_sql)

    async def workflow():
        handoff = await service.create_pipeline_composition_proposal_finish_once(
            session_id=sid,
            plan=plan,
            summary=old.summary,
            rationale=old.rationale,
            affects=old.affects,
            arguments_redacted_json=RedactedPipelineArguments(old.arguments_redacted_json),
            actor="composer-web:user:alice",
            composer_model_identifier="model",
            composer_model_version="version",
            composer_provider="test",
            user_message_id=old.user_message_id,
            session_operation_context=context,
            required_work=creation,
            running=running,
        )
        creation_handoffs.append(handoff)
        row, failures, cancellations = project_creation_handoff(creation_binding, creation, creation_projection, handoff)
        creation_projection_failures.extend(failures)
        assert row is not None and row.status == "pending"
        assert row.id != old.id and row.tool_call_id == plan.tool_call_id
        assert row.session_id == sid and row.user_message_id == old.user_message_id
        actual = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
        assert actual.row.audit_event_id == actual.creation_event_id
        expected = PipelineRejectionExpected(
            actual,
            "request_cancelled",
            None,
            "system:auto_reject_request_cancelled",
            "alice",
            context,
            running,
        )
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(
                RequiredAuthorityKind.DURABLE_COMPOSE,
                context,
                durable_operation_id=running.claim.operation_id,
                claim_attempt=running.claim.attempt,
                proposal_id=str(row.id),
                tool_call_id=row.tool_call_id,
            )
        )
        rejection_owners.append(coordinator)
        binding = RequiredWorkBinding(coordinator, 7, 12, RequiredWorkRole.TURN, running)
        observed_rejection.append(expected)
        try:
            await reject_pipeline_with_required_custody(service, expected=expected, binding=binding)
        except BaseException as rejection:
            raise original_outcome_group(
                "Actual creation and cancellation cleanup originals",
                *cancellations,
                *failures,
                rejection,
            ) from rejection
        raise AssertionError("actual rejection completion lost its deferred cancellation")

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return tuple(leaf for child in error.exceptions for leaf in leaves(child))
        return (error,)

    task = asyncio.create_task(workflow())
    try:
        assert await asyncio.to_thread(create_entered.wait, 10)
        task.cancel("initial creation cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        create_release.set()
        assert await asyncio.to_thread(reject_entered.wait, 10)
        task.cancel("later cleanup cancellation")
        await asyncio.sleep(0)
        assert not task.done()
        reject_release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await asyncio.wait_for(task, 10)
        assert type(caught.value) is BaseExceptionGroup
        originals = leaves(caught.value)
        assert len(created_cancellations) == len(rejected_cancellations) == 1
        assert type(created_cancellations[0]) is type(rejected_cancellations[0]) is asyncio.CancelledError
        assert created_cancellations[0].args == ("initial creation cancellation",)
        assert rejected_cancellations[0].args == ("later cleanup cancellation",)
        projected_originals = tuple(leaf for failure in creation_projection_failures for leaf in leaves(failure))
        expected_originals = (
            created_cancellations[0],
            *projected_originals,
            *((sql_original,) if rejection_fails else ()),
            rejected_cancellations[0],
        )
        assert len(originals) == len(expected_originals)
        assert all(actual is expected for actual, expected in zip(originals, expected_originals, strict=True))
        assert len({id(original) for original in originals}) == len(originals)

        ((creation_ticket, creation_outcome),) = creation_outcomes
        assert creation_ticket is creation
        assert type(creation_outcome) is RequiredSQLReturned
        (creation_handoff,) = creation_handoffs
        assert type(creation_handoff) is PipelineCreationReturned
        assert creation_handoff.sql_outcome is creation_outcome
        assert creation_outcome.value.tool_call_id == plan.tool_call_id
        assert creation_outcome.value.id != old.id
        if telemetry_failure:
            (post_sql_original,) = creation_handoff.post_sql_failures
            assert post_sql_original is telemetry_original
            assert any(original is telemetry_original for original in projected_originals)
        else:
            assert creation_handoff.post_sql_failures == ()
            assert creation_projection_failures == []
            assert len(originals) == (3 if rejection_fails else 2)

        creation_owner.assert_completed()
        assert len(creation_owner.tickets) == 2
        assert {ticket.key.source for ticket in creation_owner.tickets} == {
            RequiredWorkSource.PROPOSAL_CREATION_SQL,
            RequiredWorkSource.PROPOSAL_CREATION_PROJECTION,
        }
        (rejection_owner,) = rejection_owners
        rejection_owner.assert_completed()
        assert len(rejection_owner.tickets) == 2
        assert {ticket.key.source for ticket in rejection_owner.tickets} == {
            RequiredWorkSource.PROPOSAL_REJECTION_SQL,
            RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION,
        }
        for owner in (creation_owner, rejection_owner):
            for ticket in owner.tickets:
                assert ticket.complete
                assert ticket.key.transition_ordinal == 7 and ticket.key.semantic_ordinal == 12
                assert ticket.key.authority.context == context
                assert ticket.key.authority.durable_operation_id == running.claim.operation_id
                assert ticket.key.authority.claim_attempt == running.claim.attempt
        if telemetry_failure:
            receipts = creation_owner.failure_receipts()
            assert any(
                receipt.key.source is RequiredWorkSource.PROPOSAL_CREATION_PROJECTION
                and any(original is telemetry_original for original in leaves(receipt.original_root))
                for receipt in receipts
            )

        (expected,) = observed_rejection
        assert expected.reason == "request_cancelled" and expected.dispatch is None
        assert expected.actor == "system:auto_reject_request_cancelled"
        assert expected.actor_user_id == "alice"
        assert expected.context == context and expected.running is running
        assert rejection_owner.authority.proposal_id == str(expected.authority.row.id)
        assert rejection_owner.authority.tool_call_id == plan.tool_call_id
        ((rejection_ticket, rejection_outcome),) = rejection_outcomes
        assert rejection_ticket in rejection_owner.tickets
        assert rejection_ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL
        assert rejection_outcome.deferred_cancellations[0] is rejected_cancellations[0]

        with engine.connect() as conn:
            actual_row = (
                conn.execute(
                    select(composition_proposals_table).where(
                        composition_proposals_table.c.session_id == str(sid),
                        composition_proposals_table.c.id == str(expected.authority.row.id),
                        composition_proposals_table.c.tool_call_id == plan.tool_call_id,
                    )
                )
                .mappings()
                .one()
            )
            terminal_rows = (
                conn.execute(
                    select(proposal_events_table).where(
                        proposal_events_table.c.session_id == str(sid),
                        proposal_events_table.c.proposal_id == actual_row["id"],
                        proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")),
                    )
                )
                .mappings()
                .all()
            )
            assert conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == str(sid))).all() == []
        assert actual_row["committed_state_id"] is None
        assert actual_row["user_message_id"] == str(old.user_message_id)
        reloaded = await asyncio.wait_for(
            service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=expected.authority.row.id),
            2,
        )
        assert reloaded.proposal == expected.authority.proposal
        assert reloaded.creation_event_id == expected.authority.creation_event_id
        if rejection_fails:
            assert type(rejection_outcome) is RequiredSQLRaised
            assert rejection_outcome.error is sql_original
            assert actual_row["status"] == "pending" and terminal_rows == []
            assert actual_row["audit_event_id"] == str(expected.authority.creation_event_id)
            assert reloaded.row == expected.authority.row
        else:
            assert type(rejection_outcome) is RequiredSQLReturned
            result = rejection_outcome.value
            assert type(result) is PipelineRejectionSQLResult
            assert result.expected is expected and result.transitioned is True
            assert result.record == reloaded.row
            assert actual_row["status"] == reloaded.row.status == "rejected"
            (terminal,) = terminal_rows
            expected_payload = {
                "schema": "pipeline_proposal_rejected.v1",
                "tool_call_id": plan.tool_call_id,
                "tool_name": "set_pipeline",
                "status": "rejected",
                "outcome": "failed",
                "reason_code": "request_cancelled",
                "draft_hash": plan.proposal.draft_hash,
                "dispatch": None,
            }
            assert terminal["event_type"] == result.event.event_type == "proposal.rejected"
            assert terminal["actor"] == result.event.actor == "system:auto_reject_request_cancelled"
            assert terminal["payload"] == result.event.payload == expected_payload
            assert terminal["id"] == str(result.event.id) == actual_row["audit_event_id"]
            assert terminal["id"] == str(reloaded.row.audit_event_id)
            assert terminal["proposal_id"] == str(result.record.id) == str(expected.authority.row.id)
            assert terminal["session_id"] == str(result.record.session_id) == str(sid)
            assert (
                replace(
                    reloaded.row,
                    status=expected.authority.row.status,
                    audit_event_id=expected.authority.row.audit_event_id,
                    updated_at=expected.authority.row.updated_at,
                )
                == expected.authority.row
            )
    finally:
        create_release.set()
        reject_release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", hold_sql)
        repository.release(context)


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
