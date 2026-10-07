"""Concrete planner/canonical callers and mounted manual proposal routes."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.auth.middleware import get_current_user
from elspeth.web.auth.models import UserIdentity
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.pipeline_commit import PipelineCommitError
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.planning_application import PlanningApplication
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy
from elspeth.web.sessions.composer_app_services import composer_app_services
from elspeth.web.sessions.manual_proposal_failure import ComposerManualProposalFailure, consume_manual_proposal_failure
from elspeth.web.sessions.models import composition_proposals_table, proposal_events_table
from elspeth.web.sessions.routes._helpers import _initial_composition_state
from elspeth.web.sessions.routes.composer import pipeline_settlement
from tests.fixtures.composer_fakes import install_restricted_plugin_policy, interpretation_surfacing_stub
from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


@pytest.mark.asyncio
@pytest.mark.timeout(30, method="thread")
@pytest.mark.parametrize("rejection_outcome", ["returned", "raised", "unknown"])
async def test_actual_manual_rejection_keeps_authority_read_outer_cancellations_on_later_outcome(
    operation_store, tmp_path, monkeypatch, rejection_outcome
):
    from uuid import uuid4

    from starlette.requests import Request

    from elspeth.web import async_workers
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.sessions.routes.composer import proposals
    from elspeth.web.sessions.schemas import RejectProposalRequest

    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    entered, release = threading.Event(), threading.Event()
    deliveries = []
    bound = []
    actual_shield = asyncio.shield
    actual_bind = SessionOperationLease.bind_required_work
    sql_original = OperationalError("actual rejection after successful parked authority read", {}, RuntimeError("actual driver"))
    unknown_original = RuntimeError("actual rejection submission custody unavailable after authority read")

    def observe_shield(awaitable):
        async def observe():
            try:
                return await actual_shield(awaitable)
            except asyncio.CancelledError as delivery:
                deliveries.append(delivery)
                raise

        return observe()

    def observe_bind(lease, coordinator):
        actual_bind(lease, coordinator)
        bound.append((lease, coordinator))

    def park_read_then_fail_rejection(_conn, _cursor, statement, _parameters, _context, _many):
        if not entered.is_set() and statement.lstrip().startswith("SELECT") and "FROM composition_proposals" in statement:
            entered.set()
            assert release.wait(5)
        if rejection_outcome == "raised" and statement.startswith("UPDATE composition_proposals"):
            raise sql_original

    actual_submit = async_workers._submit_shared

    async def refuse_unknown_rejection(callable_, ticket=None, finalizer=None, *, deferred_cancellations=None):
        if ticket is not None and ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL:
            ticket.observe_submission_unknown(unknown_original)
            raise unknown_original
        return await actual_submit(callable_, ticket, finalizer, deferred_cancellations=deferred_cancellations)

    monkeypatch.setattr(asyncio, "shield", observe_shield)
    monkeypatch.setattr(SessionOperationLease, "bind_required_work", observe_bind)
    if rejection_outcome == "unknown":
        monkeypatch.setattr(async_workers, "_submit_shared", refuse_unknown_rejection)
    event.listen(engine, "before_cursor_execute", park_read_then_fail_rejection)
    task = asyncio.create_task(
        proposals.reject_composition_proposal(
            session_id=sid,
            proposal_id=expected.authority.row.id,
            body=RejectProposalRequest(),
            request=Request({"type": "http", "app": app, "state": {"request_id": str(uuid4())}}),
            user=UserIdentity(user_id="alice", username="alice"),
        )
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        for index in range(3):
            task.cancel(f"actual authority-read outer cancellation {index}")
            async with asyncio.timeout(5):
                while len(deliveries) != index + 1:
                    await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(BaseException) as caught:
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park_read_then_fail_rejection)

    assert len(bound) == 1
    lease, coordinator = bound[0]
    producer = next(ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER)
    child = coordinator._child_registrations[producer]
    sql = next(ticket for ticket in child.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL)
    projection = next(ticket for ticket in child.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION)
    read_sql = next(ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.PREPARATION_READ_SQL)
    assert read_sql._finish_once_handoff is not None
    assert read_sql._finish_once_handoff.deferred_cancellations == ()
    assert [delivery.args for delivery in deliveries] == [(f"actual authority-read outer cancellation {index}",) for index in range(3)]
    if rejection_outcome == "raised":
        assert type(caught.value) is ComposerManualProposalFailure
        observation = consume_manual_proposal_failure(caught.value)
        assert observation.selected_original is sql_original
        assert observation.project(request_id="manual-cancellation-proof", timeout_seconds=5).http_status == 503
        if any(all(leaf is not delivery for leaf in originals(observation.original_root)) for delivery in deliveries):
            pytest.fail("Consumed manual rejection omitted an exact delivered authority-read cancellation")
        assert producer._child_outcome is not None
        assert all(any(leaf is delivery for leaf in originals(producer._child_outcome.original_root)) for delivery in deliveries)
        assert any(error is sql_original for error in sql.errors)
        coordinator.assert_completed()
        assert lease.closed and projection.complete
        assert proposal_events(engine, expected.authority.row.id) == []
    elif rejection_outcome == "returned":
        assert type(caught.value) is not ComposerManualProposalFailure
        assert all(any(leaf is delivery for leaf in originals(caught.value)) for delivery in deliveries)
        assert child.failure_receipts() == ()
        assert producer._child_outcome is None
        assert producer not in coordinator._child_outcomes
        parent_roots = producer.errors
        assert len(parent_roots) == 1
        assert all(any(leaf is delivery for leaf in originals(parent_roots[0])) for delivery in deliveries)
        parent_receipts = producer.receipts()
        assert len(parent_receipts) == 1
        assert parent_receipts[0].original_root is parent_roots[0]
        assert parent_receipts[0].child_outcome is None
        assert all(any(leaf is delivery for leaf in originals(parent_receipts[0].original_root)) for delivery in deliveries)
        coordinator.assert_completed()
        assert lease.closed and sql.complete and projection.complete
        assert len(proposal_events(engine, expected.authority.row.id)) == 1
    else:
        assert type(caught.value) is not ComposerManualProposalFailure
        assert any(leaf is unknown_original for leaf in originals(caught.value))
        assert all(any(leaf is delivery for leaf in originals(caught.value)) for delivery in deliveries)
        assert coordinator._manual_proposal_carrier is None
        assert not sql.complete and not projection.complete
        assert projection._unused_metadata is None
        assert not coordinator.all_completed
        repository.release(lease.context)


def mounted_app(store, tmp_path):
    engine, _repository, _authority, service, _sid = store
    from elspeth.web.app import create_app
    from tests.unit.web.test_app import _settings

    app = create_app(_settings(tmp_path, composer_boot_probe_enabled=False))
    app.state.session_engine.dispose()
    app.state.session_service = service
    app.state.session_engine = engine
    from elspeth.web.coordination.approval_lifecycle_authority import RepositoryApprovalLifecycleAuthority
    from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
    from tests.fixtures.identities import grant_test_pipeline_user

    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    app.state.identity_authority = RepositoryIdentityAuthority(engine, lifecycle_effect=RepositoryApprovalLifecycleAuthority().apply)
    app.state.settings = _settings(tmp_path, auth_provider="local", operator_metrics_bearer_token=None)
    app.state.composer_service = None
    app.state.scoped_secret_resolver = None
    app.state.interpretation_surfacing = interpretation_surfacing_stub()
    from elspeth.web.composer.progress import ComposerProgressRegistry

    app.state.composer_progress_registry = ComposerProgressRegistry()
    install_restricted_plugin_policy(app)

    async def actual_fixture_user():
        return UserIdentity(user_id="alice", username="alice")

    app.dependency_overrides[get_current_user] = actual_fixture_user
    return app


def originals(root):
    if isinstance(root, BaseExceptionGroup):
        return tuple(leaf for child in root.exceptions for leaf in originals(child))
    return (root,)


def proposal_events(engine, proposal_id):
    with engine.connect() as conn:
        return conn.execute(
            select(proposal_events_table).where(
                proposal_events_table.c.proposal_id == str(proposal_id), proposal_events_table.c.event_type == "proposal.rejected"
            )
        ).all()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_actual_canonical_failure_rejects_under_registered_child_and_keeps_body(operation_store, tmp_path, monkeypatch, manual):
    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("actual controlled validation failure", code="VALIDATION_FAILED")

    async def fail_candidate(**kwargs):
        raise body

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", fail_candidate)
    if manual:
        repository.release(running.session_operation_context)
        from elspeth.web.coordination.lifecycle import SessionOperationLease

        bound = []
        original_bind = SessionOperationLease.bind_required_work

        def observe_bind(lease, coordinator):
            original_bind(lease, coordinator)
            bound.append(coordinator)

        monkeypatch.setattr(SessionOperationLease, "bind_required_work", observe_bind)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/sessions/{sid}/proposals/{expected.authority.row.id}/accept",
                json={"draft_hash": expected.authority.proposal.draft_hash},
            )
        assert response.status_code == 422
        assert len(bound) == 1
        bound[0].assert_completed()
        receipts = bound[0].failure_receipts()
        assert any(
            receipt.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER
            and any(leaf is body for leaf in originals(receipt.original_root))
            for receipt in receipts
        )
        assert all(receipt.child_outcome is None for receipt in receipts)
    else:
        parent = RequiredWorkCoordinator(
            RequiredWorkAuthority(
                RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
            )
        )
        binding = RequiredWorkBinding(parent, 9, 13, RequiredWorkRole.TURN, running)
        try:
            with pytest.raises(BaseExceptionGroup) as caught:
                await pipeline_settlement.settle_pipeline_proposal_under_compose_lock(
                    services=composer_app_services(app),
                    user_id="alice",
                    authority=expected.authority,
                    draft_hash=expected.authority.proposal.draft_hash,
                    session_operation_context=running.session_operation_context,
                    commit_timeout_seconds=5,
                    running=running,
                    required_work=parent,
                    required_binding=binding,
                )
            assert any(leaf is body for leaf in originals(caught.value))
            parent.assert_completed()
            receipts = parent.failure_receipts()
            producer = next(receipt for receipt in receipts if receipt.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER)
            assert any(leaf is body for leaf in originals(producer.original_root))
            assert producer.child_outcome is None
            assert producer.key.transition_ordinal == 9
            assert producer.key.semantic_ordinal == 13
        finally:
            repository.release(running.session_operation_context)
    events = proposal_events(engine, expected.authority.row.id)
    assert len(events) == 1
    assert events[0].actor == "system:pipeline_commit:user:alice"
    assert events[0].payload["reason_code"] == "validation_failed"


@pytest.mark.asyncio
async def test_mounted_manual_reject_binds_real_lease_before_sql_and_projects_before_release(operation_store, tmp_path, monkeypatch):
    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    from elspeth.web.coordination.lifecycle import SessionOperationLease

    bound = []
    original_bind = SessionOperationLease.bind_required_work

    def observe_bind(lease, coordinator):
        original_bind(lease, coordinator)
        bound.append((lease, coordinator))

    monkeypatch.setattr(SessionOperationLease, "bind_required_work", observe_bind)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/sessions/{sid}/proposals/{expected.authority.row.id}/reject", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert len(bound) == 1
    lease, coordinator = bound[0]
    assert lease.context.operation_kind is SessionOperationKind.PROPOSAL
    assert coordinator.authority.authority_kind is RequiredAuthorityKind.MANUAL_PROPOSAL
    assert coordinator.authority.durable_operation_id is None and coordinator.authority.claim_attempt is None
    coordinator.assert_completed()
    assert len(proposal_events(engine, expected.authority.row.id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("telemetry_failure", [False, True])
async def test_actual_planner_creation_cancel_retains_nonzero_child_rejection_scope(
    operation_store, tmp_path, monkeypatch, telemetry_failure
):
    engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    from tests.unit.web.test_app import _settings

    settings = _settings(tmp_path, operator_metrics_bearer_token=None)
    planner = PlanningApplication(
        sessions_service=service,
        policy_context=MagicMock(),
        chargeable_admission=MagicMock(),
        preflight=MagicMock(),
        schema_disclosure=SchemaDisclosureTracker(),
        settings=settings,
        availability=ComposerAvailability(True, "test-model", "test"),
        planner_dialect=ToolContractDialect.NONE,
        hatch_dialect=ToolContractDialect.NONE,
        advisor_provider="test",
        composer_skill_text="fixture skill",
        session_engine=engine,
        secret_service=None,
        secret_wiring_policy=SecretWiringPolicy(()),
        endpoint_base_url=None,
        endpoint_api_key=None,
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
    )
    telemetry_original = RuntimeError("actual creation postSQL telemetry failure")
    if telemetry_failure:
        from elspeth.web.sessions import service as service_module

        class FailedCounter:
            def add(self, amount, attributes):
                raise telemetry_original

        monkeypatch.setattr(service_module, "_PIPELINE_PLANNER_COUNTER", FailedCounter())
    preferences = await service.get_composer_preferences(sid)
    assert preferences.trust_mode == "auto_commit"
    parent = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
        )
    )
    binding = RequiredWorkBinding(parent, 7, 12, RequiredWorkRole.TURN, running)
    plan = PipelinePlanResult(
        proposal=expected.authority.proposal,
        tool_call_id="actual-new-planner-tool-ID",
        custody_result="not_required",
        model_identifier="model",
        model_version="version",
        provider="test",
    )
    entered, release = threading.Event(), threading.Event()

    def park_creation(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO composition_proposals"):
            entered.set()
            assert release.wait(5)

    event.listen(engine, "before_cursor_execute", park_creation)
    task = asyncio.create_task(
        planner._stage_pipeline_plan(
            plan=plan,
            state=_initial_composition_state(),
            session_id=sid,
            current_state_id=None,
            user_message_id=expected.authority.row.user_message_id,
            user_id="alice",
            session_operation_context=running.session_operation_context,
            preferences=preferences,
            recorder=BufferingRecorder(),
            planner_llm_calls=(),
            planner_attempts=(),
            planner_invocations=(),
            planner_withheld_replies=(),
            required_work=binding,
        )
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        for index in range(3):
            task.cancel(f"actual planner cancellation {index}")
            await asyncio.sleep(0.03)
            assert not task.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await task
        cancellations = tuple(leaf for leaf in originals(caught.value) if isinstance(leaf, asyncio.CancelledError))
        assert [leaf.args for leaf in cancellations] == [(f"actual planner cancellation {index}",) for index in range(3)]
        parent.assert_completed()
        if telemetry_failure:
            assert any(leaf is telemetry_original for leaf in originals(caught.value))
            assert any(receipt.key.source is RequiredWorkSource.PROPOSAL_CREATION_PROJECTION for receipt in parent.failure_receipts())
        with engine.connect() as conn:
            row = conn.execute(
                select(composition_proposals_table).where(composition_proposals_table.c.tool_call_id == plan.tool_call_id)
            ).one()
        events = proposal_events(engine, row.id)
        assert len(events) == 1 and events[0].payload["reason_code"] == "request_cancelled"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park_creation)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_mounted_manual_body_sql_and_close_failures_retain_originals_and_reduce_priority(operation_store, tmp_path, monkeypatch):
    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("actual validation body", code="VALIDATION_FAILED")
    sql_error = OperationalError("actual rejection UPDATE", {}, RuntimeError("actual driver"))
    close_error = AuditIntegrityError("actual required release integrity failure")
    from elspeth.web.coordination.lifecycle import SessionOperationLease

    bound = []
    body_entered = threading.Event()
    rejection_update_attempted = threading.Event()
    release_faults = []
    actual_bind = SessionOperationLease.bind_required_work

    def observe_bind(lease, coordinator):
        actual_bind(lease, coordinator)
        bound.append((lease, coordinator))

    monkeypatch.setattr(SessionOperationLease, "bind_required_work", observe_bind)

    async def fail_candidate(**kwargs):
        body_entered.set()
        raise body

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", fail_candidate)

    def fail_owned_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if body_entered.is_set() and statement.startswith("UPDATE composition_proposals"):
            rejection_update_attempted.set()
            raise sql_error
        if (
            body_entered.is_set()
            and rejection_update_attempted.is_set()
            and len(bound) == 1
            and statement.startswith("UPDATE session_operation_fences")
        ):
            assignments = tuple(
                part.strip().partition("=")[0].strip() for part in statement.partition(" WHERE ")[0].partition(" SET ")[2].split(",")
            )
            fence = bound[0][0].context.fence
            parameters = _parameters.values() if isinstance(_parameters, dict) else _parameters
            if isinstance(_parameters, dict):
                expiry_value = _parameters.get("lease_expires_at")
                release_value = _parameters.get("released_at")
            elif len(_parameters) >= 2:
                expiry_value, release_value = _parameters[:2]
            else:
                expiry_value = release_value = None
            if (
                assignments == ("lease_expires_at", "released_at")
                and release_value is not None
                and release_value == expiry_value
                and all(value in parameters for value in (fence.session_id, fence.operation_id, fence.lease_token))
            ):
                release_faults.append(statement)
                raise close_error

    from elspeth.web import app as app_module

    consumed = []
    actual_consume = app_module.consume_manual_proposal_failure

    def retain_consumed(carrier):
        observation = actual_consume(carrier)
        consumed.append(observation)
        return observation

    monkeypatch.setattr(app_module, "consume_manual_proposal_failure", retain_consumed)
    event.listen(engine, "before_cursor_execute", fail_owned_sql)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/sessions/{sid}/proposals/{expected.authority.row.id}/accept",
                json={"draft_hash": expected.authority.proposal.draft_hash},
            )
        assert response.status_code == 500
        assert response.json()["error_type"] == "audit_integrity_error"
        assert body_entered.is_set() and rejection_update_attempted.is_set()
        assert len(release_faults) == 1
        assert len(consumed) == 1 and consumed[0].selected_original is close_error
        assert len(bound) == 1
        retained = originals(consumed[0].original_root)
        assert any(leaf is body for leaf in retained)
        assert any(leaf is sql_error for leaf in retained)
        assert any(leaf is close_error for leaf in retained)
        assert proposal_events(engine, expected.authority.row.id) == []
    finally:
        event.remove(engine, "before_cursor_execute", fail_owned_sql)
        # The actual failed release does not prove the live fence ended.
        if bound:
            repository.release(bound[0][0].context)
