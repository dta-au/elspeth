"""Physical generation, original-child and manual lease custody controls."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_sql_outcomes import RequiredSQLRaised
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from elspeth.web.sessions.models import composition_proposals_table, proposal_events_table
from elspeth.web.sessions.pipeline_rejection_custody import (
    await_retained_originals,
    close_required_proposal_lease,
    reject_pipeline_with_required_custody,
)
from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineRejectionRaised
from tests.unit.web.sessions.test_pipeline_rejection_required import invoke, prepared_rejection, rejection_work
from tests.unit.web.test_required_executor_custody import QueueThenRaiseExecutor

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
async def test_owned_cancelled_child_retains_actual_original_identity_and_arguments():
    original = asyncio.CancelledError("actual child original")
    entered = asyncio.Event()

    async def child():
        entered.set()
        raise original

    with pytest.raises(asyncio.CancelledError) as caught:
        await await_retained_originals(child())
    assert entered.is_set()
    assert caught.value is original
    assert caught.value.args == ("actual child original",)


@pytest.mark.asyncio
async def test_actual_rejection_postqueue_no_return_waits_for_generation_proof(operation_store, monkeypatch):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    executor = QueueThenRaiseExecutor(max_workers=1)
    parked, release = threading.Event(), threading.Event()
    # Populate the physical queue before installing the controlled submitter.
    blocker = ThreadPoolExecutor.submit(executor, lambda: (parked.set(), release.wait(5)))
    async with asyncio.timeout(5):
        while not parked.is_set():
            await asyncio.sleep(0.01)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    task = asyncio.create_task(invoke(service, expected, coordinator, sql, projection))
    try:
        generation = None
        async with asyncio.timeout(5):
            while generation is None:
                candidate = async_workers._GENERATION_CUSTODIAN
                generation = candidate if candidate is not None and candidate.executor is executor else None
                if generation is None:
                    await asyncio.sleep(0.01)
        assert generation is not None
        assert not generation.joined.is_set()
        assert not task.done() and not sql.complete and not projection.complete
        with pytest.raises(AuditIntegrityError):
            coordinator.prepare_lease_release()
        release.set()
        blocker.result(timeout=5)
        handoff = await task
        assert type(handoff) is PipelineRejectionRaised
        projection.complete_unused(handoff.projection_unused)
        coordinator.assert_completed()
        async with asyncio.timeout(5):
            while not generation.joined.is_set():
                await asyncio.sleep(0.01)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(composition_proposals_table.c.status).where(composition_proposals_table.c.id == str(expected.authority.row.id))
                ).scalar_one()
                == "pending"
            )
            assert (
                conn.execute(
                    select(proposal_events_table.c.id).where(
                        proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                        proposal_events_table.c.event_type == "proposal.rejected",
                    )
                ).all()
                == []
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await async_workers.shutdown_async_workers()
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_unknown_bridge_has_no_failure_arm_or_projection_waiver(operation_store, monkeypatch):
    _engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    original = RuntimeError("actual bridge completion proof unavailable")

    async def unknown_submission(*args, **kwargs):
        sql.observe_submission_unknown(original)
        raise original

    monkeypatch.setattr(async_workers, "_submit_shared", unknown_submission)
    try:
        with pytest.raises(RuntimeError) as caught:
            await invoke(service, expected, coordinator, sql, projection)
        assert caught.value is original
        assert not sql.complete and not projection.complete
        with pytest.raises(AuditIntegrityError):
            coordinator.issue_rejection_projection_unused(
                rejection_ticket=sql, projection_ticket=projection, actual_outcome=RequiredSQLRaised(original, ())
            )
        with pytest.raises(AuditIntegrityError):
            coordinator.prepare_lease_release()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_consumed_actual_rejection_pair_cannot_submit_again(operation_store):
    _engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    try:
        from tests.unit.web.sessions.test_pipeline_rejection_required import project_returned

        handoff = await invoke(service, expected, coordinator, sql, projection)
        project_returned(coordinator, sql, projection, handoff, expected)
        with pytest.raises(AuditIntegrityError):
            await invoke(service, expected, coordinator, sql, projection)
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_real_manual_lease_close_failure_keeps_actual_sql_original(operation_store):
    engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    lease = await SessionOperationLease.acquire(
        repository,
        session_id=sid,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.MANUAL_PROPOSAL,
            lease.context,
            proposal_id=str(expected.authority.row.id),
            invocation_id=str(expected.authority.row.id),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    lease.bind_required_work(coordinator)
    original = OperationalError("actual lease release SQL", {}, RuntimeError("controlled driver"))

    def fail_release(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE session_operation_fences") and "released_at" in statement:
            raise original

    event.listen(engine, "before_cursor_execute", fail_release)
    try:
        errors = await close_required_proposal_lease(lease, coordinator=coordinator)
        leaves = tuple(leaf for error in errors for leaf in _explicit_leaves(error))
        assert any(leaf is original for leaf in leaves)
        assert any(
            any(witness is original for witness in receipt.original_category_witnesses) for receipt in coordinator.failure_receipts()
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_release)
        repository.release(lease.context)


def _explicit_leaves(root):
    if isinstance(root, BaseExceptionGroup):
        return tuple(leaf for child in root.exceptions for leaf in _explicit_leaves(child))
    return (root,)


@pytest.mark.asyncio
async def test_nonzero_two_proposal_children_have_distinct_real_parent_recurrence(operation_store):
    _engine, repository, _authority, _service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    parent = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
        )
    )
    from uuid import uuid4

    try:
        first, first_ticket = parent.begin_proposal_child(
            str(expected.authority.row.id), expected.authority.row.tool_call_id, transition_ordinal=8, semantic_ordinal=11
        )
        second, second_ticket = parent.begin_proposal_child(str(uuid4()), "second-provider-ID", transition_ordinal=8, semantic_ordinal=11)
        assert first_ticket.key != second_ticket.key
        assert first_ticket.key.transition_ordinal == second_ticket.key.transition_ordinal == 8
        assert first_ticket.key.semantic_ordinal == second_ticket.key.semantic_ordinal == 11
        assert first.authority.claim_attempt == second.authority.claim_attempt == running.claim.attempt
        parent.complete_proposal_child(first, first_ticket)
        parent.complete_proposal_child(second, second_ticket)
        parent.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["claim_token", "attempt", "operation_id"])
async def test_rejection_requires_actual_running_claim_not_a_reconstructed_binding(operation_store, changed):
    from uuid import uuid4

    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    if changed == "attempt":
        claim = replace(running.claim, attempt=running.claim.attempt + 1)
    elif changed == "claim_token":
        claim = replace(running.claim, claim_token=str(uuid4()))
    else:
        claim = replace(running.claim, operation_id=str(uuid4()))
    foreign = replace(expected, running=replace(running, claim=claim))
    try:
        handoff = await invoke(service, foreign, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionRaised
        assert isinstance(handoff.sql_outcome.error, AuditIntegrityError)
        projection.complete_unused(handoff.projection_unused)
        coordinator.assert_completed()
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(proposal_events_table.c.id).where(
                        proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                        proposal_events_table.c.event_type == "proposal.rejected",
                    )
                ).all()
                == []
            )
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_raised_consumer_refuses_wrapper_with_genuine_metadata_but_forged_error(operation_store, monkeypatch):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            running.claim.operation_id,
            running.claim.attempt,
            proposal_id=str(expected.authority.row.id),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    binding = RequiredWorkBinding(coordinator, 4, 7, RequiredWorkRole.TURN, running)
    real_facade = type(service).reject_pipeline_composition_proposal_finish_once
    actual = OperationalError("actual rejection SQL failure", {}, RuntimeError("actual driver"))
    forged_error = OperationalError("forged error carrier", {}, RuntimeError("foreign driver"))

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise actual

    async def forge(self, **kwargs):
        handoff = await real_facade(self, **kwargs)
        assert type(handoff) is PipelineRejectionRaised
        assert handoff.sql_outcome.error is actual
        return replace(handoff, sql_outcome=RequiredSQLRaised(forged_error, handoff.sql_outcome.deferred_cancellations))

    monkeypatch.setattr(type(service), "reject_pipeline_composition_proposal_finish_once", forge)
    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AuditIntegrityError):
            await reject_pipeline_with_required_custody(service, expected=expected, binding=binding)
        assert not coordinator.all_completed
        with pytest.raises(AuditIntegrityError):
            coordinator.prepare_lease_release()
    finally:
        event.remove(engine, "before_cursor_execute", fail)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_successful_rejection_postsql_fault_is_owned_by_projection_without_erasing_event(operation_store, monkeypatch):
    from elspeth.web.sessions import service as service_module

    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            running.claim.operation_id,
            running.claim.attempt,
            proposal_id=str(expected.authority.row.id),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    binding = RequiredWorkBinding(coordinator, 3, 6, RequiredWorkRole.TURN, running)
    original = RuntimeError("actual owned postSQL counter failure")

    class FailedCounter:
        def add(self, amount, attributes):
            raise original

    monkeypatch.setattr(service_module, "_PIPELINE_SETTLEMENT_COUNTER", FailedCounter())
    try:
        with pytest.raises(BaseExceptionGroup) as caught:
            await reject_pipeline_with_required_custody(service, expected=expected, binding=binding)
        assert any(leaf is original for leaf in _explicit_leaves(caught.value))
        coordinator.assert_completed()
        assert any(receipt.key.source is RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION for receipt in coordinator.failure_receipts())
        with engine.connect() as conn:
            assert (
                len(
                    conn.execute(
                        select(proposal_events_table.c.id).where(
                            proposal_events_table.c.proposal_id == str(expected.authority.row.id),
                            proposal_events_table.c.event_type == "proposal.rejected",
                        )
                    ).all()
                )
                == 1
            )
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_real_lease_closed_child_cancellation_keeps_original_identity(operation_store, monkeypatch):
    _engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    lease = await SessionOperationLease.acquire(
        repository,
        session_id=sid,
        operation_kind=SessionOperationKind.PROPOSAL,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=30,
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.MANUAL_PROPOSAL,
            lease.context,
            proposal_id=str(expected.authority.row.id),
            invocation_id=str(expected.authority.row.id),
            tool_call_id=expected.authority.row.tool_call_id,
        )
    )
    lease.bind_required_work(coordinator)
    original = asyncio.CancelledError("actual closed-child cancellation")
    actual_close = SessionOperationLease.close

    async def close_then_cancel(self):
        await actual_close(self)
        raise original

    monkeypatch.setattr(SessionOperationLease, "close", close_then_cancel)
    errors = await close_required_proposal_lease(lease, coordinator=coordinator)
    assert errors == (original,)
    assert errors[0] is original
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_copied_unused_projection_metadata_is_not_an_issued_capability(operation_store):
    engine, repository, _authority, service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    coordinator, sql, projection = rejection_work(expected)
    fault = OperationalError("actual controlled failure", {}, RuntimeError("driver"))

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise fault

    event.listen(engine, "before_cursor_execute", fail)
    try:
        handoff = await invoke(service, expected, coordinator, sql, projection)
        assert type(handoff) is PipelineRejectionRaised
        copied = replace(handoff.projection_unused)
        assert copied is not handoff.projection_unused
        with pytest.raises(AuditIntegrityError):
            projection.complete_unused(copied)
        assert not projection.complete
        projection.complete_unused(handoff.projection_unused)
        coordinator.assert_completed()
    finally:
        event.remove(engine, "before_cursor_execute", fail)
        repository.release(running.session_operation_context)
