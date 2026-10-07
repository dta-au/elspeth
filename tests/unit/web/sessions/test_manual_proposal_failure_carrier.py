"""Actual completed manual custody, private issuance and original coverage."""

from __future__ import annotations

import asyncio
import copy
import gc
import threading
import weakref
from dataclasses import replace

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.session_operation import SessionOperationKind
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
from elspeth.web.sessions.manual_proposal_failure import (
    ComposerManualProposalFailure,
    consume_manual_proposal_failure,
    issue_manual_proposal_failure,
)
from elspeth.web.sessions.pipeline_rejection_custody import (
    close_required_proposal_lease,
    original_outcome_group,
    reject_pipeline_with_required_custody,
)
from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)
pytestmark = pytest.mark.timeout(30, method="thread")


def assert_omission_cannot_obtain_consumed_manual_presentation(carrier):
    if carrier is None:
        return
    try:
        consume_manual_proposal_failure(carrier)
    except AuditIntegrityError:
        return
    pytest.fail("An explicit original omission obtained consumed manual presentation authority")


async def actual_failed_manual_invocation(store):
    engine, repository, _authority, service, sid = store
    expected, running = await prepared_rejection(store)
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
    expected = replace(expected, context=lease.context, running=None)
    original = OperationalError("SECRET_SQL", {"secret": "SECRET_PARAMETER"}, RuntimeError("SECRET_DRIVER"))

    def fail_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise original

    event.listen(engine, "before_cursor_execute", fail_sql)
    try:
        with pytest.raises(OperationalError) as caught:
            await reject_pipeline_with_required_custody(
                service, expected=expected, binding=RequiredWorkBinding(coordinator, 3, 5, RequiredWorkRole.TURN)
            )
        assert caught.value is original
    finally:
        event.remove(engine, "before_cursor_execute", fail_sql)
    return lease, coordinator, original


@pytest.mark.asyncio
async def test_private_carrier_retains_lower_close_cancel_and_original_driver_without_mutation(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    engine = operation_store[0]
    entered, release = threading.Event(), threading.Event()

    def park_close(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE session_operation_fences") and "released_at" in statement:
            entered.set()
            assert release.wait(5)

    event.listen(engine, "before_cursor_execute", park_close)
    task = asyncio.create_task(close_required_proposal_lease(lease, coordinator=coordinator))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel("actual late close original")
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        close_originals = await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park_close)
    assert len(close_originals) == 1 and isinstance(close_originals[0], asyncio.CancelledError)
    cancellation = close_originals[0]
    root = original_outcome_group("full actual root", sql_original, cancellation)
    cause, context = sql_original.__cause__, sql_original.__context__
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    assert type(carrier) is ComposerManualProposalFailure
    # Genuine DTO regeneration is not an identity substitution of actual work.
    coordinator.failure_receipts()
    observation = consume_manual_proposal_failure(carrier)
    assert observation.original_root is root and observation.selected_original is sql_original
    assert observation.project(request_id="actual-request", timeout_seconds=5).http_status == 503
    assert sql_original.__cause__ is cause and sql_original.__context__ is context
    with pytest.raises(AuditIntegrityError):
        consume_manual_proposal_failure(carrier)


@pytest.mark.asyncio
async def test_unissued_equal_copy_foreign_owner_and_missing_original_coverage_cannot_render(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    await close_required_proposal_lease(lease, coordinator=coordinator)
    with pytest.raises(AuditIntegrityError):
        consume_manual_proposal_failure(ComposerManualProposalFailure())
    foreign = RequiredWorkCoordinator(coordinator.authority)
    assert issue_manual_proposal_failure(foreign, lease, sql_original) is None
    carrier = issue_manual_proposal_failure(coordinator, lease, sql_original)
    assert type(carrier) is ComposerManualProposalFailure
    copied = copy.copy(carrier)
    assert copied is not carrier
    with pytest.raises(AuditIntegrityError):
        consume_manual_proposal_failure(copied)
    assert consume_manual_proposal_failure(carrier).selected_original is sql_original


@pytest.mark.asyncio
async def test_missing_original_fallback_is_a_terminal_presentation_attempt(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    await close_required_proposal_lease(lease, coordinator=coordinator)
    assert issue_manual_proposal_failure(coordinator, lease, RuntimeError("omitted actual SQL")) is None
    with pytest.raises(AuditIntegrityError):
        issue_manual_proposal_failure(coordinator, lease, sql_original)


@pytest.mark.asyncio
async def test_consumed_actual_invocation_cannot_issue_again_or_reobserve_cached_close(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    await close_required_proposal_lease(lease, coordinator=coordinator)
    carrier = issue_manual_proposal_failure(coordinator, lease, sql_original)
    assert type(carrier) is ComposerManualProposalFailure
    consume_manual_proposal_failure(carrier)
    with pytest.raises(AuditIntegrityError):
        issue_manual_proposal_failure(coordinator, lease, sql_original)
    with pytest.raises(AuditIntegrityError):
        await close_required_proposal_lease(lease, coordinator=coordinator)
    with pytest.raises(AuditIntegrityError):
        consume_manual_proposal_failure(carrier)


@pytest.mark.asyncio
async def test_cached_close_cannot_mint_another_consumable_manual_presentation(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    await close_required_proposal_lease(lease, coordinator=coordinator)
    carrier = issue_manual_proposal_failure(coordinator, lease, sql_original)
    assert type(carrier) is ComposerManualProposalFailure
    assert consume_manual_proposal_failure(carrier).selected_original is sql_original
    try:
        await close_required_proposal_lease(lease, coordinator=coordinator)
        replacement = issue_manual_proposal_failure(coordinator, lease, sql_original)
        if replacement is None:
            return  # An alternative safe refusal is not a mutation success.
        consume_manual_proposal_failure(replacement)
    except AuditIntegrityError:
        return
    pytest.fail("Cached actual close minted a second consumable presentation capability")


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_close_original", [False, True])
async def test_actual_close_cancellation_with_known_cause_still_requires_explicit_original_coverage(
    operation_store, monkeypatch, omit_close_original
):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    actual_close = SessionOperationLease.close
    close_original = asyncio.CancelledError("actual close child original with known SQL cause")
    close_original.__cause__ = sql_original

    async def close_then_raise(self):
        await actual_close(self)
        raise close_original

    monkeypatch.setattr(SessionOperationLease, "close", close_then_raise)
    errors = await close_required_proposal_lease(lease, coordinator=coordinator)
    assert errors == (close_original,) and errors[0] is close_original
    root = (
        sql_original
        if omit_close_original
        else original_outcome_group("explicit close original and known cause", sql_original, close_original)
    )
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    if omit_close_original:
        assert_omission_cannot_obtain_consumed_manual_presentation(carrier)
    else:
        assert type(carrier) is ComposerManualProposalFailure
        assert consume_manual_proposal_failure(carrier).original_root is root
        assert any(leaf is close_original for leaf in root.exceptions)


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_deferred_original", [False, True])
async def test_actual_finish_once_deferred_cancellation_requires_explicit_manual_root(operation_store, omit_deferred_original):
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
    expected = replace(expected, context=lease.context, running=None)
    original = OperationalError("actual parked rejection SQL", {}, RuntimeError("actual parked driver"))
    entered, release = threading.Event(), threading.Event()

    def fail_parked_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            entered.set()
            assert release.wait(5)
            raise original

    event.listen(engine, "before_cursor_execute", fail_parked_sql)
    task = asyncio.create_task(
        reject_pipeline_with_required_custody(
            service, expected=expected, binding=RequiredWorkBinding(coordinator, 3, 5, RequiredWorkRole.TURN)
        )
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel("actual physical SQL join cancellation")
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", fail_parked_sql)
    sql = next(ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL)
    handoff = sql._finish_once_handoff
    assert isinstance(handoff, RequiredSQLRaised)
    assert handoff.error is original and len(handoff.deferred_cancellations) == 1
    cancellation = handoff.deferred_cancellations[0]
    assert cancellation.args == ("actual physical SQL join cancellation",)
    assert any(leaf is cancellation for leaf in caught.value.exceptions)
    assert await close_required_proposal_lease(lease, coordinator=coordinator) == ()
    root = original if omit_deferred_original else caught.value
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    if omit_deferred_original:
        assert_omission_cannot_obtain_consumed_manual_presentation(carrier)
    else:
        assert type(carrier) is ComposerManualProposalFailure
        observation = consume_manual_proposal_failure(carrier)
        assert observation.original_root is root and observation.selected_original is original


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["consumed", "unconsumed", "fallback", "release_failure"])
async def test_actual_invocation_traceback_cycles_have_no_process_global_root(operation_store, disposition):
    rooted = []

    async def complete_invocation():
        lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
        release_original = AuditIntegrityError("actual failed physical release lifetime")

        def fail_release(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("UPDATE session_operation_fences") and "released_at" in statement:
                raise release_original

        if disposition == "release_failure":
            event.listen(operation_store[0], "before_cursor_execute", fail_release)
        try:
            close_originals = await close_required_proposal_lease(lease, coordinator=coordinator)
        finally:
            if disposition == "release_failure":
                event.remove(operation_store[0], "before_cursor_execute", fail_release)
                operation_store[1].release(lease.context)
        root = sql_original
        if disposition == "fallback":
            root = original_outcome_group("higher real original", sql_original, AuditIntegrityError("no subset projection"))
        carrier = issue_manual_proposal_failure(coordinator, lease, root)
        if disposition in ("fallback", "release_failure"):
            assert carrier is None
            if disposition == "release_failure":
                assert any(error is release_original for error in close_originals)
        else:
            assert type(carrier) is ComposerManualProposalFailure
            if disposition == "consumed":
                consume_manual_proposal_failure(carrier)
        observed_original = release_original if disposition == "release_failure" else sql_original
        rooted.append(observed_original)
        return weakref.ref(observed_original)

    observed = await complete_invocation()
    gc.collect()
    assert observed() is rooted[0]  # Known retained original controls the probe.
    rooted.clear()
    gc.collect()
    assert observed() is None


@pytest.mark.asyncio
async def test_higher_unregistered_original_keeps_full_root_instead_of_old_subset_dto(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    await close_required_proposal_lease(lease, coordinator=coordinator)
    higher = AuditIntegrityError("actual independently retained integrity failure")
    root = original_outcome_group("complete explicit originals", sql_original, higher)
    assert issue_manual_proposal_failure(coordinator, lease, root) is None
    assert root.exceptions == (sql_original, higher)


@pytest.mark.asyncio
async def test_unknown_pending_projection_never_obtains_private_manual_carrier(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    pending = coordinator.reserve(RequiredWorkSource.POSTCOMMIT_REVIEW_PROJECTION)
    assert not pending.complete
    # This is a known pending-semantic negative, not a forged SQL completion.
    errors = await close_required_proposal_lease(lease, coordinator=coordinator)
    assert errors
    root = original_outcome_group("original plus unresolved close", sql_original, *errors)
    assert issue_manual_proposal_failure(coordinator, lease, root) is None
    pending.complete_without_submission(AuditIntegrityError("controlled unlaunched projection refusal"))
    operation_store[1].release(lease.context)


@pytest.mark.asyncio
async def test_equal_category_audit_conflict_uses_unchanged_shared_policy(operation_store):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    engine = operation_store[0]
    release_original = AuditIntegrityError("actual release audit", failed_turn=FailedTurnMetadata(None, 1, 0))

    def fail_release(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE session_operation_fences") and "released_at" in statement:
            raise release_original

    event.listen(engine, "before_cursor_execute", fail_release)
    try:
        close_originals = await close_required_proposal_lease(lease, coordinator=coordinator)
    finally:
        event.remove(engine, "before_cursor_execute", fail_release)
    extra_original = AuditIntegrityError("independent equal-category audit", failed_turn=FailedTurnMetadata(None, 2, 1))
    root = original_outcome_group("all actual retained audit originals", sql_original, *close_originals, extra_original)
    carrier = issue_manual_proposal_failure(coordinator, lease, root)
    assert type(carrier) is ComposerManualProposalFailure
    observation = consume_manual_proposal_failure(carrier)
    projected = observation.project(request_id=None, timeout_seconds=5)
    assert projected.http_status == 500
    assert projected.body["diagnostic"] == "failed_turn_metadata_conflict"
    assert "failed_turn" not in projected.body
    assert any(witness is extra_original for witness in observation.witnesses)
    operation_store[1].release(lease.context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["ticket_original", "selected_cause", "carrier_root"])
async def test_private_carrier_rejects_changed_actual_evidence_then_accepts_restored_identity(operation_store, mutation):
    lease, coordinator, sql_original = await actual_failed_manual_invocation(operation_store)
    await close_required_proposal_lease(lease, coordinator=coordinator)
    carrier = issue_manual_proposal_failure(coordinator, lease, sql_original)
    assert type(carrier) is ComposerManualProposalFailure
    cause = sql_original.__cause__
    root = carrier.__cause__
    ticket = next(ticket for ticket in coordinator.tickets if any(error is sql_original for error in ticket.errors))
    actual_errors = tuple(ticket._errors)
    try:
        if mutation == "ticket_original":
            ticket._errors[:] = [RuntimeError("same ticket, substituted original")]
        elif mutation == "selected_cause":
            sql_original.__cause__ = RuntimeError("substituted driver cause")
        else:
            carrier.__cause__ = RuntimeError("omitted full original root")
        with pytest.raises(AuditIntegrityError):
            consume_manual_proposal_failure(carrier)
    finally:
        ticket._errors[:] = actual_errors
        sql_original.__cause__ = cause
        carrier.__cause__ = root
    assert consume_manual_proposal_failure(carrier).selected_original is sql_original
