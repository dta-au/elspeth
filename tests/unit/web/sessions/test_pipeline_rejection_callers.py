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
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.application_policy import PluginPolicyContextFactory
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.composition_completion import CompositionCompletion
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
@pytest.mark.parametrize("owned", [False, True], ids=["public", "owned"])
@pytest.mark.parametrize(
    "recovered,argument_site",
    [(False, None), (True, None), (False, "build"), (False, "executor")],
    ids=["fresh", "persisted-success", "build-argument", "executor-argument"],
)
async def test_real_canonical_terminal_refusal_http_rejects_once_without_state_publication(
    operation_store, tmp_path, monkeypatch, owned, recovered, argument_site
):
    from dataclasses import replace

    from sqlalchemy import func

    from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
    from elspeth.core.canonical import stable_hash
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.audit import begin_dispatch, dispatch_with_audit
    from elspeth.web.composer.pipeline_commit import _bind_executor_content_hash
    from elspeth.web.composer.pipeline_proposal import (
        PipelineProposal,
        PresentBase,
        composition_content_hash,
        owned_composition_state_authority,
        owned_composition_state_review_arguments,
    )
    from elspeth.web.composer.protocol import ToolArgumentError
    from elspeth.web.composer.redaction import redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
    from elspeth.web.composer.tools import ToolContext, build_set_pipeline_candidate, execute_tool
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from elspeth.web.sessions.models import chat_messages_table, composition_states_table
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.sessions.routes._helpers import _persist_tool_invocations
    from tests.integration.web.composer.test_pipeline_proposal_lifecycle import _runnable_pipeline
    from tests.unit.web.coordination.test_composer_operation_authority import start

    engine, repository, _authority, service, sid = operation_store
    record, running = start(operation_store)
    context = running.session_operation_context
    from uuid import UUID

    user = await service.add_message_with_transcript(
        sid,
        "user",
        "Keep the current pipeline",
        operation_id=UUID(record.operation_id),
        requested_state_id=None,
        writer_principal="route_user_message",
        session_operation_context=context,
        running=running,
    )
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    public = _runnable_pipeline(tmp_path, sid)
    empty = _initial_composition_state()
    candidate = build_set_pipeline_candidate(
        public,
        empty,
        ToolContext(
            catalog=policy,
            plugin_snapshot=snapshot,
            data_dir=str(tmp_path),
            session_id=str(sid),
            session_engine=engine,
            session_operation_context=context,
            session_operation_authority=service.session_operation_authority,
            user_id="alice",
            require_data_dir_for_paths=True,
        ),
    )
    assert candidate.acceptable
    state = replace(candidate.result.updated_state, version=7)
    payload = state.to_dict()
    saved = await service.save_composition_state(
        sid,
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
        ),
        provenance="tool_call",
        session_operation_context=context,
    )
    proposed = state
    if argument_site == "executor":
        proposed = replace(state, metadata=replace(state.metadata, name="Changed candidate"))
        public["metadata"] = {"name": proposed.metadata.name, "description": proposed.metadata.description}
    arguments = owned_composition_state_authority(proposed) if owned else public
    plan = PipelinePlanResult(
        proposal=PipelineProposal.create(
            pipeline=arguments, base=PresentBase(saved.id, composition_content_hash(state)), repair_count=0, skill_hash=stable_hash("skill")
        ),
        tool_call_id="equal-terminal",
        custody_result="not_required",
        model_identifier="model",
        model_version="v1",
        provider="test",
    )
    row = await service.create_pipeline_composition_proposal(
        session_id=sid,
        plan=plan,
        summary="Keep pipeline",
        rationale="Requested",
        affects=("graph",),
        actor="composer-web:user:alice",
        arguments_redacted_json=redact_tool_call_arguments(
            "set_pipeline", owned_composition_state_review_arguments(arguments) if owned else arguments, telemetry=NoopRedactionTelemetry()
        ),
        composer_model_identifier="model",
        composer_model_version="v1",
        composer_provider="test",
        session_operation_context=context,
        user_message_id=user.message.id,
    )
    binding = None
    if recovered:
        recorder = BufferingRecorder()

        async def dispatch():
            return execute_tool(
                "set_pipeline",
                public,
                state,
                policy,
                plugin_snapshot=snapshot,
                data_dir=str(tmp_path),
                session_id=str(sid),
                session_engine=engine,
                session_operation_context=context,
                session_operation_authority=service.session_operation_authority,
                user_id="alice",
            )

        outcome = await dispatch_with_audit(
            recorder=recorder,
            audit=begin_dispatch(row.tool_call_id, "set_pipeline", arguments, version_before=state.version, actor="user:alice"),
            do_dispatch=dispatch,
            version_after_provider=lambda result: result.updated_state.version,
            arg_error_payload_factory=lambda exc: {"error_class": "ToolArgumentError", "error_code": exc.category.value},
        )
        assert outcome.result.success
        invocation = _bind_executor_content_hash(
            recorder.invocations[0], executor_content_hash=composition_content_hash(outcome.result.updated_state)
        )
        bindings = await _persist_tool_invocations(
            service, sid, (invocation,), None, plugin_crash_pending=False, session_operation_context=context
        )
        (binding,) = bindings
    repository.release(context)
    app = mounted_app(operation_store, tmp_path)
    import elspeth.web.composer.pipeline_commit as commit

    def forbid_execution(*args, **kwargs):
        pytest.fail("equal pending acceptance dispatched the executor")

    argument_error = ToolArgumentError(
        argument="set_pipeline arguments",
        expected="object conforming to SetPipelineArgumentsModel",
        actual_type="invalid_schema",
        code="SCHEMA_VALIDATION",
        category=ToolArgumentErrorCategory.SCHEMA_SHAPE,
    )

    def refuse_argument(*args, **kwargs):
        raise argument_error

    monkeypatch.setattr(commit, "execute_tool", refuse_argument if argument_site == "executor" else forbid_execution)
    if argument_site == "build":
        monkeypatch.setattr(commit, "build_set_pipeline_candidate", refuse_argument)
    with engine.connect() as conn:
        before = conn.execute(select(func.count()).select_from(chat_messages_table)).scalar_one()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/sessions/{sid}/proposals/{row.id}/accept"
        response = await client.post(url, json={"draft_hash": plan.proposal.draft_hash})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error_type"] == ("proposal_argument_invalid" if argument_site else "proposal_no_change")
        diagnosis = (
            f"The composer's proposed change has invalid arguments: {argument_error.safe_message} "
            if argument_site
            else "The composer's proposed change does not change the current composition. "
        )
        assert response.json()["detail"]["detail"] == (
            diagnosis + "The proposal has been automatically rejected. Ask the composer to revise and resubmit."
        )
        repeat = await client.post(url, json={"draft_hash": plan.proposal.draft_hash})
        assert repeat.status_code == 409
    reloaded = await service.get_authoritative_pipeline_proposal(session_id=sid, proposal_id=row.id)
    assert reloaded.row.status == "rejected"
    assert reloaded.row.committed_state_id is None
    assert (await service.get_current_state(sid)).id == saved.id
    events = proposal_events(engine, row.id)
    assert len(events) == 1
    assert events[0].actor == "system:pipeline_commit:user:alice"
    assert events[0].payload["reason_code"] == ("validation_failed" if argument_site else "no_change")
    assert events[0].payload["dispatch"] == (binding.to_dict() if binding is not None else None)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(composition_states_table)).scalar_one() == 1
        if argument_site != "executor":
            assert conn.execute(select(func.count()).select_from(chat_messages_table)).scalar_one() == before
        else:
            audit_rows = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.role == "audit")).scalars().all()
            invocations = [
                item["invocation"]
                for calls in audit_rows
                if calls
                for item in calls
                if item["_kind"] == "audit" and item["invocation"]["tool_call_id"] == row.tool_call_id
            ]
            assert len(invocations) == 1
            assert invocations[0]["status"] == "arg_error"


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
@pytest.mark.parametrize(
    "code,reason", [("VALIDATION_FAILED", "validation_failed"), ("NO_CHANGE", "no_change"), ("ARGUMENT_ERROR", "validation_failed")]
)
async def test_actual_canonical_failure_rejects_under_registered_child_and_keeps_body(
    operation_store, tmp_path, monkeypatch, manual, code, reason
):
    engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("actual controlled validation failure", code=code)

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
        if code in {"NO_CHANGE", "ARGUMENT_ERROR"}:
            payload = response.json()["detail"]
            assert "error_type" in payload, payload
            assert payload["error_type"] == ("proposal_no_change" if code == "NO_CHANGE" else "proposal_argument_invalid")
            assert payload["tool_name"] == "set_pipeline"
            assert "automatically rejected" in payload["detail"]
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
    assert events[0].payload["reason_code"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_sql", "after_sql"])
async def test_no_change_required_rejection_keeps_exact_cancellations_and_projects_actual_sql(
    operation_store, tmp_path, monkeypatch, boundary
):
    from elspeth.web.sessions.service import SessionServiceImpl

    engine, repository, _authority, _service, _sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("pipeline proposal does not change the current composition", code="NO_CHANGE")

    async def no_change(**kwargs):
        raise body

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", no_change)
    parent = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            running.session_operation_context,
            running.claim.operation_id,
            running.claim.attempt,
        )
    )
    binding = RequiredWorkBinding(parent, 9, 13, RequiredWorkRole.TURN, running)
    entered, release = threading.Event(), threading.Event()
    handoffs = []
    after_sql_cancel = asyncio.CancelledError("exact owned postSQL cancellation")
    if boundary == "after_sql":
        from elspeth.web.sessions import service as service_module

        class CancelledCounter:
            def add(self, amount, attributes):
                raise after_sql_cancel

        monkeypatch.setattr(service_module, "_PIPELINE_SETTLEMENT_COUNTER", CancelledCounter())

    def park_sql(_conn, _cursor, statement, _parameters, _context, _many):
        if boundary == "before_sql" and statement.startswith("UPDATE composition_proposals"):
            entered.set()
            assert release.wait(5)

    actual_finish = SessionServiceImpl.reject_pipeline_composition_proposal_finish_once

    async def observe_return(self, **kwargs):
        handoff = await actual_finish(self, **kwargs)
        handoffs.append(handoff)
        return handoff

    monkeypatch.setattr(SessionServiceImpl, "reject_pipeline_composition_proposal_finish_once", observe_return)
    event.listen(engine, "before_cursor_execute", park_sql)
    task = asyncio.create_task(
        pipeline_settlement.settle_pipeline_proposal_under_compose_lock(
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
    )
    try:
        if boundary == "before_sql":
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            for index in range(2):
                task.cancel(f"no-change-{boundary}-{index}")
                await asyncio.sleep(0.03)
                assert not task.done()
        release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await task
        leaves = originals(caught.value)
        assert any(leaf is body for leaf in leaves)
        assert len(handoffs) == 1
        originals_delivered = handoffs[0].sql_outcome.deferred_cancellations if boundary == "before_sql" else handoffs[0].post_sql_failures
        assert len(originals_delivered) == (2 if boundary == "before_sql" else 1)
        if boundary == "after_sql":
            assert originals_delivered[0] is after_sql_cancel
        assert all(any(leaf is delivery for leaf in leaves) for delivery in originals_delivered)
        parent.assert_completed()
        producer = next(ticket for ticket in parent.tickets if ticket.key.source is RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER)
        child = parent._child_registrations[producer]
        sql = next(ticket for ticket in child.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL)
        projection = next(ticket for ticket in child.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION)
        assert sql.complete and projection.complete and projection._projection_started
        assert sql._finish_once_handoff is not None
        assert producer.key.transition_ordinal == 9 and producer.key.semantic_ordinal == 13
        events = proposal_events(engine, expected.authority.row.id)
        assert len(events) == 1
        assert events[0].actor == "system:pipeline_commit:user:alice"
        assert events[0].payload["reason_code"] == "no_change"
        assert events[0].payload["dispatch"] is not None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park_sql)
        repository.release(running.session_operation_context)


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
    advisor_checkpoint = MagicMock(spec=AdvisorCheckpointOwner)
    completion = MagicMock(spec=CompositionCompletion)
    planner = PlanningApplication(
        sessions_service=service,
        policy_context=MagicMock(spec=PluginPolicyContextFactory),
        chargeable_admission=MagicMock(spec=ComposerChargeableAdmission),
        preflight=MagicMock(spec=ComposerPreflight),
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
        advisor_checkpoint=advisor_checkpoint,
        completion=completion,
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
            planner_private_candidates=(),
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
        assert advisor_checkpoint.mock_calls == []
        assert completion.mock_calls == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park_creation)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "required,trust_modes,cancel_creation,fault",
    [
        (required, modes, cancel, "pending")
        for required in (False, True)
        for modes in (
            ("explicit_approve", "explicit_approve"),
            ("auto_commit", "auto_commit"),
            ("auto_commit", "explicit_approve"),
            ("explicit_approve", "auto_commit"),
        )
        for cancel in (False, True)
    ]
    + [
        (required, ("auto_commit", "auto_commit"), False, fault)
        for required in (False, True)
        for fault in ("missing", "error", "corrupt", "ValueError", "SQL", "group", "telemetry")
    ],
)
async def test_actual_planner_resource_race_retains_receipts_and_original_cancellation(
    operation_store, tmp_path, monkeypatch, required, trust_modes, cancel_creation, fault
):
    from dataclasses import replace

    from sqlalchemy import delete, update

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.composer.pipeline_proposal import PipelineProposal
    from elspeth.web.sessions.models import blobs_table, composition_states_table, sessions_table
    from tests.integration.web.composer.test_freeform_proposal_prevalidation import _harness, _valid_pipeline_args
    from tests.unit.web.composer.test_audit_cohort_settlement import _chat_rows, _discovery_invocation, _invocation_occurrences

    engine, repository, _authority, service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    planner = _harness(tmp_path / "planner").service._planning_application
    planner._sessions_service_optional = service
    if trust_modes[1] == "explicit_approve":
        # Explicit approval keeps one composition proposal outstanding, so the
        # planner refuses to publish beside the fixture's undecided proposal
        # and would never reach the creation race under test. Decide it first.
        await service.reject_pipeline_composition_proposal(
            session_id=sid,
            proposal_id=expected.authority.row.id,
            draft_hash=expected.authority.proposal.draft_hash,
            reason="operator_rejected",
            dispatch=None,
            actor="user:alice",
            session_operation_context=running.session_operation_context,
        )
    prior_occurrences = _invocation_occurrences(_chat_rows(service, str(sid)))
    assert prior_occurrences == {expected.authority.row.tool_call_id: 1}
    blob = await BlobServiceImpl(engine, tmp_path).create_blob(
        sid, "input.csv", b"value\n1\n", "text/csv", session_operation_context=running.session_operation_context
    )
    pipeline = _valid_pipeline_args(tmp_path)
    pipeline["source"]["blob_id"] = str(blob.id)
    proposal = PipelineProposal.create(pipeline=pipeline, base=expected.authority.proposal.base, repair_count=0, skill_hash="a" * 64)
    plan = PipelinePlanResult(
        proposal=proposal,
        tool_call_id="raced-planner",
        custody_result="not_required",
        model_identifier="model",
        model_version="version",
        provider="test",
    )
    # The same authority passed admission; the resource changes only afterward.
    from elspeth.web.sessions.proposal_blob_refs import validate_proposal_blob_references

    with engine.begin() as conn:
        validate_proposal_blob_references(conn, session_id=str(sid), tool_name="set_pipeline", arguments=pipeline)
        if fault == "missing":
            conn.execute(delete(blobs_table).where(blobs_table.c.id == str(blob.id)))
        elif fault in {"pending", "error", "corrupt"}:
            if fault == "corrupt":
                conn.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            conn.execute(
                update(blobs_table).where(blobs_table.c.id == str(blob.id)).values(status="unknown" if fault == "corrupt" else fault)
            )
            if fault == "corrupt":
                conn.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(trust_mode=trust_modes[1]))
    preferences = replace(await service.get_composer_preferences(sid), trust_mode=trust_modes[0])
    parent = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
        )
    )
    binding = RequiredWorkBinding(parent, 7, 12, RequiredWorkRole.TURN, running) if required else None
    entered, release = threading.Event(), threading.Event()
    deliveries = []
    actual_shield = asyncio.shield
    injected = {
        "ValueError": ValueError("private parser detail"),
        "SQL": OperationalError("private SQL detail", {}, RuntimeError("driver detail")),
        "group": ExceptionGroup("private grouped failures", [ValueError("parser"), RuntimeError("other")]),
        "telemetry": RuntimeError("post-SQL telemetry detail"),
    }
    if fault == "telemetry":
        from elspeth.web.sessions import service as service_module

        class FailedCounter:
            def add(self, amount, attributes):
                raise injected["telemetry"]

        monkeypatch.setattr(service_module, "_PIPELINE_PLANNER_COUNTER", FailedCounter())

    async def observe_shield(awaitable):
        try:
            return await actual_shield(awaitable)
        except asyncio.CancelledError as delivery:
            deliveries.append(delivery)
            raise

    from elspeth.web import async_workers

    actual_retain = async_workers._retain_cancellation

    def observe_retain(cancellations, cancellation):
        deliveries.append(cancellation)
        actual_retain(cancellations, cancellation)

    def park_validator(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("SELECT blobs.session_id, blobs.status"):
            entered.set()
            assert release.wait(5)
            if fault in {"ValueError", "SQL", "group"}:
                raise injected[fault]

    monkeypatch.setattr(asyncio, "shield", observe_shield)
    monkeypatch.setattr(async_workers, "_retain_cancellation", observe_retain)
    event.listen(engine, "before_cursor_execute", park_validator)
    state = _initial_composition_state()
    planner_invocation = _discovery_invocation("completed-planner-evidence")
    task = asyncio.create_task(
        planner._stage_pipeline_plan(
            plan=plan,
            state=state,
            session_id=sid,
            current_state_id=None,
            user_message_id=expected.authority.row.user_message_id,
            user_id="alice",
            session_operation_context=running.session_operation_context,
            preferences=preferences,
            recorder=BufferingRecorder(),
            planner_llm_calls=(),
            planner_private_candidates=(),
            planner_attempts=(),
            planner_invocations=(planner_invocation,),
            planner_withheld_replies=(),
            required_work=binding,
        )
    )
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        if cancel_creation:
            task.cancel("original failed creation cancellation")
            async with asyncio.timeout(5):
                while not deliveries:
                    await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel_creation:
            with pytest.raises(BaseException) as caught:
                await task
            leaves = originals(caught.value)
            assert any(leaf is deliveries[0] for leaf in leaves)
            if not required:
                assert caught.value is deliveries[0]
                assert type(caught.value.__cause__).__name__ == "ProposalResourceUnavailableError"
        elif fault in {"corrupt", "ValueError", "SQL", "group", "telemetry"}:
            with pytest.raises(BaseException) as caught:
                await task
            if fault == "corrupt":
                assert type(caught.value) is AuditIntegrityError
            elif fault == "group":
                assert originals(caught.value) == originals(injected[fault])
            elif fault == "telemetry" and required:
                assert any(leaf is injected[fault] for leaf in originals(caught.value))
            else:
                assert caught.value is injected[fault]
        else:
            result = await task
            assert result.state is state
            assert result.raw_assistant_content == ""
            assert result.pipeline_commit_intent is None and result.runtime_preflight is None
            assert result.tool_invocations == () and result.llm_calls == ()
            assert "no longer available" in result.message
            assert str(blob.id) not in result.message
        if required:
            parent.assert_completed()
            sql = next(ticket for ticket in parent.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_CREATION_SQL)
            projection = next(ticket for ticket in parent.tickets if ticket.key.source is RequiredWorkSource.PROPOSAL_CREATION_PROJECTION)
            assert sql.complete and projection.complete
            if fault == "telemetry":
                assert sql.errors == ()
                assert any(
                    any(leaf is injected[fault] for leaf in originals(receipt.original_root)) for receipt in parent.failure_receipts()
                )
            else:
                assert len(sql.errors) == 1
                if fault in {"pending", "missing", "error"}:
                    assert type(sql.errors[0]).__name__ == "ProposalResourceUnavailableError"
                elif fault != "corrupt":
                    assert sql.errors[0] is injected[fault]
                assert any(receipt.original_root is sql.errors[0] for receipt in parent.failure_receipts())
            assert not parent._child_registrations
            if cancel_creation:
                assert any(leaf is sql.errors[0] for leaf in originals(caught.value))
        with engine.connect() as conn:
            assert len(
                conn.execute(
                    select(composition_proposals_table.c.id).where(composition_proposals_table.c.tool_call_id == plan.tool_call_id)
                ).all()
            ) == int(fault == "telemetry")
            assert conn.execute(select(composition_states_table.c.id)).all() == []
            assert len(
                conn.execute(
                    select(proposal_events_table.c.id).where(proposal_events_table.c.proposal_id != str(expected.authority.row.id))
                ).all()
            ) == int(fault == "telemetry")
        assert _invocation_occurrences(_chat_rows(service, str(sid))) == {**prior_occurrences, planner_invocation.tool_call_id: 1}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", park_validator)
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
