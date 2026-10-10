"""Explicit provider boundary accounting uses the real session authority and SQL."""

from contextlib import suppress
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from tests.unit.web.composer.test_llm_sampling_config import _settings
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service


@pytest.mark.asyncio
@pytest.mark.parametrize("family", tuple(ProviderInvocationFamily))
async def test_required_gateway_without_recorder_retains_and_settles_actual_audit(tmp_path, monkeypatch, family):
    import litellm
    from litellm import ModelResponse

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="explicit", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(family)
    physical_calls = []

    async def completion(**kwargs):
        assert "provider_custody" not in kwargs
        assert custody.attempt is not None
        physical_calls.append(custody.attempt)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "An ordinary provider reply.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    result = await gateway._call_llm_with_audit([], [], timeout=5, recorder=None, provider_custody=custody)
    assert result.message.content == "An ordinary provider reply."
    assert len(physical_calls) == 1
    assert len(_ledger(engine)) == 1
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()
    sources = {ticket.key.source for ticket in coordinator._tickets.values()}
    assert sources == (
        {
            RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL,
            RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION,
            RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
        }
        if family is ProviderInvocationFamily.TITLE
        else {
            RequiredWorkSource.PROVIDER_ADMISSION_SQL,
            RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,
            RequiredWorkSource.PROVIDER_SETTLEMENT_SQL,
            RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION,
        }
    )
    authority.release(context)
    engine.dispose()


def test_family_mint_order_is_independent_and_binding_is_immutable(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="mint", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    binding = RequiredWorkBinding(coordinator, 4, 0, RequiredWorkRole.TURN)
    first = ProviderInvocationOwner(service=service, required_work=binding)
    second = ProviderInvocationOwner(service=service, required_work=binding)
    first.mint(ProviderInvocationFamily.TITLE)
    primary = first.mint(ProviderInvocationFamily.PRIMARY)
    other_primary = second.mint(ProviderInvocationFamily.PRIMARY)
    second.mint(ProviderInvocationFamily.TITLE)
    assert primary.required_work == other_primary.required_work
    assert primary.required_work.transition_ordinal == 4
    with pytest.raises(AttributeError):
        primary.required_work = binding
    with pytest.raises(AuditIntegrityError):
        first.mint(0)
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
async def test_sdk_and_audit_construction_failure_retain_originals_and_unknown_attempt(tmp_path, monkeypatch):
    import litellm

    from elspeth.web.composer import provider_gateway

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="audit failure", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    sdk_error = ValueError("SDK-only error canary")
    audit_error = RuntimeError("audit-construction error canary")

    async def completion(**kwargs):
        raise sdk_error

    def broken_audit(**kwargs):
        raise audit_error

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(provider_gateway, "build_llm_call_record", broken_audit)
    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    with pytest.raises(BaseExceptionGroup) as caught:
        await gateway._call_llm_with_audit([], [], timeout=5, recorder=None, provider_custody=custody)

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return tuple(leaf for child in error.exceptions for leaf in leaves(child))
        return (error,)

    original_leaves = leaves(caught.value)
    assert any(error is sdk_error for error in original_leaves)
    assert any(error is audit_error for error in original_leaves)
    assert any(isinstance(error, AuditIntegrityError) for error in original_leaves)
    assert custody.attempt is not None and custody.call is None
    assert _ledger(engine) == []
    assert all(ticket.complete for ticket in coordinator._tickets.values())
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
async def test_actual_title_producer_settles_provider_then_owned_title_sql(tmp_path, monkeypatch):
    import litellm
    from litellm import ModelResponse

    from elspeth.web.sessions._auto_title import _maybe_auto_title_session_required

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="New session", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.TITLE)
    dispatched = []

    async def completion(**kwargs):
        assert custody.attempt is not None
        dispatched.append(custody.attempt)
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Reviewed provider title"}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    audit_messages = [{"role": "user", "content": "Name this session"}]
    await _maybe_auto_title_session_required(
        service=service,
        session_id=session.id,
        model="test/model",
        temperature=None,
        seed=None,
        session_operation_context=context,
        kwargs={"model": "test/model", "messages": audit_messages},
        audit_messages=audit_messages,
        provider_custody=custody,
    )
    assert len(dispatched) == 1
    assert (await service.get_session(session.id)).title == "Reviewed provider title"
    assert len(_ledger(engine)) == 1
    coordinator.assert_completed()
    assert {ticket.key.source for ticket in coordinator._tickets.values()} == {
        RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL,
        RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION,
        RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
        RequiredWorkSource.TITLE_ACCOUNTING_SQL,
        RequiredWorkSource.TITLE_ACCOUNTING_PROJECTION,
    }
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_sdk", [False, True])
async def test_actual_advisor_producer_retains_complete_audit_without_recorder(tmp_path, monkeypatch, cancel_sdk):
    import asyncio

    import litellm
    from litellm import ModelResponse

    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Advisor", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    advisor = AdvisorCheckpointOwner(
        settings=_settings(tmp_path),
        composer_skill_text="Ordinary composer instructions",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=service,
        chargeable_admission=ComposerChargeableAdmission(service),
    )
    entered = asyncio.Event()
    cancellations = []

    async def completion(**kwargs):
        entered.set()
        if cancel_sdk:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise
        return ModelResponse(
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": "Review the declared field contracts.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    task = asyncio.create_task(
        advisor._call_advisor_with_audit(
            {"trigger": "reactive", "problem_summary": "Review source fields", "recent_errors": [], "attempted_actions": []},
            recorder=None,
            timeout=5,
            provider_owner=owner,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
    except TimeoutError:
        if task.done():
            task.result()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise
    if cancel_sdk:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(cancellations) == 1
    else:
        guidance, _ = await task
        assert guidance == "Review the declared field contracts."
    assert len(_ledger(engine)) == 1
    coordinator.assert_completed()
    assert {ticket.key.source for ticket in coordinator._tickets.values()} == {
        RequiredWorkSource.PROVIDER_ADMISSION_SQL,
        RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,
        RequiredWorkSource.PROVIDER_SETTLEMENT_SQL,
        RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION,
    }
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
async def test_actual_planner_producer_uses_physical_sdk_and_settles_failed_call(tmp_path, monkeypatch):
    from dataclasses import replace

    import litellm

    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.capability_skill import load_pipeline_capability_core
    from elspeth.web.composer.pipeline_planner import PlannerOriginatingMessage, plan_pipeline
    from elspeth.web.composer.pipeline_proposal import AbsentBase
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
    from tests.unit.web.composer.test_pipeline_planner import (
        _budget,
        _custody,
        _empty_state,
        _lifecycle,
        _model,
        _ScriptedCompletion,
    )

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Planner", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    recorder = BufferingRecorder()
    sdk_error = ValueError("local physical planner failure")
    physical_calls = []

    async def completion(**kwargs):
        physical_calls.append(kwargs)
        raise sdk_error

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(ValueError) as caught:
        await plan_pipeline(
            intent="Build the requested pipeline.",
            current_state=_empty_state(),
            provider_current_state=_empty_state().to_dict(),
            schemas_loaded=frozenset(),
            mark_schema_loaded=None,
            policy_catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot),
            plugin_snapshot=snapshot,
            originating_message=PlannerOriginatingMessage(
                session_id=str(session.id), message_id=str(uuid4()), content="Build the requested pipeline.", user_id="alice"
            ),
            base=AbsentBase(),
            model_config=_model(_ScriptedCompletion([])),
            rendered_skill=load_pipeline_capability_core() + "\nOrdinary bounded planner instructions.",
            repair_budget=0,
            budget_policy=_budget(),
            custody_config=replace(
                _custody(tmp_path), session_engine=engine, session_operation_context=context, session_operation_authority=authority
            ),
            lifecycle=_lifecycle(),
            recorder=recorder,
            candidate_finalizer=lambda candidate: candidate,
            provider_service=service,
            provider_owner=owner,
        )
    assert caught.value is sdk_error
    assert len(physical_calls) == 1
    assert len(recorder.llm_calls) == 1
    assert len(_ledger(engine)) == 1
    coordinator.assert_completed()
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
async def test_callback_failure_is_actual_undispatched_disposition_and_never_sdk(tmp_path, monkeypatch):
    import litellm
    from sqlalchemy import select

    from elspeth.web.composer.provider_gateway import _litellm_acompletion
    from elspeth.web.composer.provider_quota import provider_call_scope
    from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Callback", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    callback_error = ValueError("owned callback failure")
    sdk_entries = []

    async def rejecting_sdk(**kwargs):
        sdk_entries.append(kwargs)
        raise AssertionError("Physical SDK must not begin after callback failure")

    def callback():
        assert custody.attempt is not None
        assert not custody.needs_terminal_audit()
        raise callback_error

    monkeypatch.setattr(litellm, "acompletion", rejecting_sdk)
    with pytest.raises(AssertionError):
        await litellm.acompletion(model="test/model", messages=[])
    sdk_entries.clear()
    with pytest.raises(ValueError) as caught:
        async with provider_call_scope(custody):
            await _litellm_acompletion(provider_custody=custody, on_provider_dispatch=callback, model="test/model", messages=[])
    assert caught.value is callback_error
    assert sdk_entries == []
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        messages = connection.execute(select(chat_messages_table)).all()
    assert attempt.settled_at is not None
    assert len(messages) == 1
    assert "provider_attempt.cancelled_before_dispatch" in messages[0].content
    assert {ticket.key.source for ticket in coordinator._tickets.values()} == {
        RequiredWorkSource.PROVIDER_ADMISSION_SQL,
        RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,
        RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,
    }
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_outer", [False, True])
async def test_owned_self_cancel_retains_exact_child_cause_and_waits_for_child(tmp_path, cancel_outer):
    import asyncio

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Child", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    child_error = asyncio.CancelledError("actual owned child")
    cause = RuntimeError("unclassified child cause")

    async def child():
        entered.set()
        try:
            await release.wait()
            raise child_error from cause
        finally:
            completed.set()

    task = asyncio.create_task(custody._join(child()))
    await asyncio.wait_for(entered.wait(), timeout=5)
    if cancel_outer:
        task.cancel("outer one")
        await asyncio.sleep(0)
        task.cancel("outer two")
        await asyncio.sleep(0)
        assert not task.done()
        assert not completed.is_set()
    release.set()
    with pytest.raises((AuditIntegrityError, BaseExceptionGroup)) as caught:
        await task
    assert completed.is_set()

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return tuple(leaf for nested in error.exceptions for leaf in leaves(nested))
        return (error,)

    retained = leaves(caught.value)
    markers = [error for error in retained if isinstance(error, AuditIntegrityError)]
    assert len(markers) == 1
    assert markers[0].__cause__ is child_error
    assert child_error.__cause__ is cause
    if cancel_outer:
        assert any(error is child_error for error in retained)
        assert {str(error) for error in retained if isinstance(error, asyncio.CancelledError)} == {
            "actual owned child",
            "outer one",
            "outer two",
            "",
        }
    authority.release(context)
    engine.dispose()
