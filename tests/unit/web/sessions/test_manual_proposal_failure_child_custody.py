"""Actual manual parent31/child51 custody remains bound through presentation."""

from __future__ import annotations

import asyncio
import errno
from concurrent.futures import Future
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.pipeline_commit import PipelineCommitError
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkIncomplete, RequiredWorkSource
from elspeth.web.sessions import pipeline_rejection_custody as custody
from elspeth.web.sessions.manual_proposal_failure import ComposerManualProposalFailure, consume_manual_proposal_failure
from elspeth.web.sessions.pipeline_rejection_custody import original_outcome_group
from elspeth.web.sessions.routes.composer import pipeline_settlement, proposals
from elspeth.web.sessions.schemas import AcceptProposalRequest
from tests.unit.web.sessions.test_pipeline_rejection_callers import mounted_app, originals
from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)
pytestmark = pytest.mark.timeout(30, method="thread")


async def actual_canonical_failed_manual_carrier(store, tmp_path, monkeypatch, *, sql_original=None, require_carrier=True):
    engine, repository, _authority, _service, sid = store
    expected, running = await prepared_rejection(store)
    repository.release(running.session_operation_context)
    app = mounted_app(store, tmp_path)
    body = PipelineCommitError("actual canonical body", code="VALIDATION_FAILED")
    if sql_original is None:
        sql_original = OperationalError("actual rejected CAS", {}, RuntimeError("actual driver"))
    actual_issue = custody.issue_manual_proposal_failure
    issued = []

    def capture_issue(coordinator, lease, original_root):
        issued.append((coordinator, lease, original_root))
        return actual_issue(coordinator, lease, original_root)

    monkeypatch.setattr(custody, "issue_manual_proposal_failure", capture_issue)

    async def fail_candidate(**kwargs):
        raise body

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", fail_candidate)

    def fail_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE composition_proposals"):
            raise sql_original

    event.listen(engine, "before_cursor_execute", fail_sql)
    try:
        with pytest.raises(BaseException) as caught:
            await proposals.accept_composition_proposal(
                session_id=sid,
                proposal_id=expected.authority.row.id,
                request=Request({"type": "http", "app": app, "state": {"request_id": str(uuid4())}}),
                user=UserIdentity(user_id="alice", username="alice"),
                body=AcceptProposalRequest(draft_hash=expected.authority.proposal.draft_hash),
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_sql)
    carrier = caught.value
    assert len(issued) == 1
    coordinator, _lease, _original_root = issued[0]
    if require_carrier:
        assert type(carrier) is ComposerManualProposalFailure
        assert carrier._coordinator is coordinator
    producer = next(ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER)
    child = coordinator._child_registrations[producer]
    sql = next(ticket for ticket in child.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL)
    assert producer._child_outcome is not None
    assert any(error is sql_original for error in sql.errors)
    assert any(leaf is body for leaf in originals(_original_root))
    if type(carrier) is ComposerManualProposalFailure:
        assert any(leaf is body for leaf in originals(carrier.__cause__))
    return carrier, coordinator, child, sql, sql_original


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["original", "custody", "pending", "observation_copy", "registration", "future"])
async def test_actual_nested_manual_carrier_refuses_changed_child_custody(operation_store, tmp_path, monkeypatch, mutation):
    from dataclasses import replace

    carrier, coordinator, child, sql, sql_original = await actual_canonical_failed_manual_carrier(operation_store, tmp_path, monkeypatch)
    producer = next(ticket for ticket, registered in coordinator._child_registrations.items() if registered is child)
    retained_errors = tuple(sql._errors)
    retained_custody = tuple(sql._custody_errors)
    retained_observation = carrier._observation
    retained_future = sql._future
    pending = None
    try:
        if mutation == "original":
            sql._errors[:] = [OperationalError("substituted child CAS", {}, RuntimeError("another driver"))]
        elif mutation == "custody":
            sql.observe_custody_failure(AuditIntegrityError("actual late child custody failure"))
        elif mutation == "pending":
            pending = child.reserve(RequiredWorkSource.POSTCOMMIT_REVIEW_PROJECTION)
        elif mutation == "observation_copy":
            assert retained_observation is not None
            carrier._observation = replace(retained_observation)
        elif mutation == "registration":
            coordinator._child_registrations[producer] = RequiredWorkCoordinator(child.authority)
        else:
            copied_completion = Future()
            copied_completion.set_exception(sql_original)
            sql._future = copied_completion
        with pytest.raises((AuditIntegrityError, RequiredWorkIncomplete)):
            consume_manual_proposal_failure(carrier)
    finally:
        sql._errors[:] = retained_errors
        sql._custody_errors[:] = retained_custody
        carrier._observation = retained_observation
        coordinator._child_registrations[producer] = child
        sql._future = retained_future
        if pending is not None:
            pending.complete_owned(AuditIntegrityError("controlled unlaunched child refusal"))
    if pending is None:
        assert consume_manual_proposal_failure(carrier).selected_original is sql_original


@pytest.mark.asyncio
async def test_actual_nested_manual_carrier_accepts_regenerated_dtos_with_unchanged_originals(operation_store, tmp_path, monkeypatch):
    carrier, coordinator, child, _sql, sql_original = await actual_canonical_failed_manual_carrier(operation_store, tmp_path, monkeypatch)
    coordinator.failure_receipts()
    child.failure_receipts()
    observation = consume_manual_proposal_failure(carrier)
    assert observation.selected_original is sql_original


@pytest.mark.asyncio
@pytest.mark.parametrize("change_child", [False, True])
async def test_actual_nested_manual_custody_change_before_issue_cannot_authorize_old_child_snapshot(
    operation_store, tmp_path, monkeypatch, change_child
):
    actual_issue = custody.issue_manual_proposal_failure
    late = OSError(errno.EIO, "actual late child storage custody original before presentation")
    captured = []

    def observe_issue(coordinator, lease, original_root):
        producer = next(ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER)
        child = coordinator._child_registrations[producer]
        sql = next(ticket for ticket in child.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL)
        if change_child:
            sql.observe_custody_failure(late)
        captured.append((coordinator, child, sql))
        supplied = original_outcome_group("Complete actual stale child originals", original_root, late) if change_child else original_root
        return actual_issue(coordinator, lease, supplied)

    monkeypatch.setattr(custody, "issue_manual_proposal_failure", observe_issue)
    outcome, coordinator, child, _sql, sql_original = await actual_canonical_failed_manual_carrier(
        operation_store,
        tmp_path,
        monkeypatch,
        require_carrier=False,
    )
    assert len(captured) == 1
    if not change_child:
        assert type(outcome) is ComposerManualProposalFailure
        assert consume_manual_proposal_failure(outcome).selected_original is sql_original
        return
    assert any(leaf is late for receipt in child.failure_receipts() for leaf in receipt.original_category_witnesses)
    if type(outcome) is not ComposerManualProposalFailure:
        assert coordinator._manual_proposal_carrier is None
        return
    assert any(leaf is late for leaf in originals(outcome.__cause__))
    try:
        consume_manual_proposal_failure(outcome)
    except (AuditIntegrityError, RequiredWorkIncomplete):
        return
    pytest.fail("Changed actual child receipts obtained consumed manual presentation authority")


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_wrapper", [False, True])
async def test_actual_nested_registered_caused_cancellation_requires_complete_explicit_root(
    operation_store, tmp_path, monkeypatch, omit_wrapper
):
    sql_cause = OperationalError("actual nested SQL cause", {}, RuntimeError("actual nested driver"))
    wrapper = asyncio.CancelledError("actual nested SQL51 caused cancellation")
    wrapper.__cause__ = sql_cause
    actual_issue = custody.issue_manual_proposal_failure
    supplied_roots = []

    def supply_explicit_root(coordinator, lease, original_root):
        roots = tuple(leaf for leaf in originals(original_root) if not omit_wrapper or leaf is not wrapper)
        supplied = original_outcome_group("Actual nested wrapper raw coverage pair", *roots, sql_cause)
        supplied_roots.append(supplied)
        return actual_issue(coordinator, lease, supplied)

    monkeypatch.setattr(custody, "issue_manual_proposal_failure", supply_explicit_root)
    outcome, coordinator, child, sql, retained = await actual_canonical_failed_manual_carrier(
        operation_store,
        tmp_path,
        monkeypatch,
        sql_original=wrapper,
        require_carrier=False,
    )
    assert retained is wrapper and any(error is wrapper for error in sql.errors)
    producer = next(ticket for ticket, registered in coordinator._child_registrations.items() if registered is child)
    assert producer.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER
    assert sql.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL
    assert producer._child_outcome is not None
    assert any(leaf is wrapper for leaf in originals(producer._child_outcome.original_root))
    assert len(supplied_roots) == 1
    if not omit_wrapper:
        assert type(outcome) is ComposerManualProposalFailure
        observation = consume_manual_proposal_failure(outcome)
        assert observation.original_root is supplied_roots[0]
        assert observation.selected_original is sql_cause
        assert any(leaf is wrapper for leaf in originals(observation.original_root))
        assert observation.project(request_id="nested-complete", timeout_seconds=5).http_status == 503
        return
    assert all(leaf is not wrapper for leaf in originals(supplied_roots[0]))
    if type(outcome) is not ComposerManualProposalFailure:
        assert coordinator._manual_proposal_carrier is None
        return
    try:
        consume_manual_proposal_failure(outcome)
    except (AuditIntegrityError, RequiredWorkIncomplete):
        return
    pytest.fail("Nested actual registered caused cancellation omission obtained consumed manual presentation authority")
