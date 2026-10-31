"""Explicit provider boundary accounting uses the real session authority and SQL."""

import json
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import prompt_guard_clients
from elspeth.web.composer.prompt_guard import build_prompt_guard_mount, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from tests.unit.web.composer.test_llm_sampling_config import _settings
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service


def _required_guard_settings(tmp_path, monkeypatch):
    """Keep real required admission and replace only the external Azure HTTP boundary."""
    requests = []

    def serve(request):
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
            },
        )

    def client_factory(settings):
        return AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(serve))

    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", client_factory)
    guard = PromptGuardSettings(endpoint="https://guard.example.test", subscription_key="fixture-guard-credential")
    settings = _settings(
        tmp_path,
        composer_prompt_guard=guard,
        composer_advisor_prompt_guard=guard,
        composer_prompt_guard_mode="required",
    )
    return settings, requests


def _assert_required_guard_admitted(engine, context, requests, *, role, user_prompt, expected_count=1):
    checks = _checks(engine)
    assert len(checks) == expected_count
    for check in checks:
        assert check["operation_id"] == context.fence.operation_id
        assert check["operation_epoch"] == context.fence.operation_epoch
        assert check["role"] == role
        assert check["adapter"] == "azure_prompt_shields"
        assert check["mode"] == "required"
        assert (check["disposition"], check["reason"]) == ("allow", "clean")
    assert requests
    assert all(json.loads(request.content)["userPrompt"] == user_prompt for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", tuple(ProviderInvocationFamily))
async def test_required_gateway_without_recorder_retains_and_settles_actual_audit(tmp_path, monkeypatch, family):
    import litellm
    from litellm import ModelResponse

    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    user_prompt = "Reply to this ordinary custody request."
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
        _assert_required_guard_admitted(engine, context, guard_requests, role="composer", user_prompt=user_prompt)
        assert "provider_custody" not in kwargs
        assert custody.attempt is not None
        physical_calls.append(custody.attempt)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "An ordinary provider reply.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope(user_prompt):
        result = await gateway._call_llm_with_audit(
            [{"role": "user", "content": user_prompt}], [], timeout=5, recorder=None, provider_custody=custody
        )
    assert result.message.content == "An ordinary provider reply."
    assert len(physical_calls) == 1
    assert len(_ledger(engine)) == 1
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()
    sources = {ticket.key.source for ticket in coordinator._tickets.values()}
    assert sources == {RequiredWorkSource.DISPATCH_AUDIT_SQL, RequiredWorkSource.DISPATCH_AUDIT_PROJECTION} | (
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

    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    user_prompt = "Reply before the deliberate SDK and audit faults."
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
        _assert_required_guard_admitted(engine, context, guard_requests, role="composer", user_prompt=user_prompt)
        raise sdk_error

    def broken_audit(**kwargs):
        raise audit_error

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(provider_gateway, "build_llm_call_record", broken_audit)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope(user_prompt), pytest.raises(BaseExceptionGroup) as caught:
        await gateway._call_llm_with_audit(
            [{"role": "user", "content": user_prompt}], [], timeout=5, recorder=None, provider_custody=custody
        )

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

    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    user_prompt = "Name this session"
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
        _assert_required_guard_admitted(engine, context, guard_requests, role="composer", user_prompt=user_prompt)
        assert custody.attempt is not None
        dispatched.append(custody.attempt)
        return ModelResponse(
            model="test/model",
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Reviewed provider title"}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    audit_messages = [{"role": "user", "content": user_prompt}]
    with prompt_guard_user_scope(user_prompt):
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
            prompt_guard_mount=build_prompt_guard_mount(settings.composer_prompt_guard),
            prompt_guard_mode=settings.composer_prompt_guard_mode,
            user_message=user_prompt,
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
        RequiredWorkSource.DISPATCH_AUDIT_SQL,
        RequiredWorkSource.DISPATCH_AUDIT_PROJECTION,
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

    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    user_prompt = "Review the source fields for this ordinary request."
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
        settings=settings,
        composer_skill_text="Ordinary composer instructions",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=service,
        chargeable_admission=ComposerChargeableAdmission(service),
    )
    entered = asyncio.Event()
    cancellations = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(engine, context, guard_requests, role="advisor", user_prompt=user_prompt)
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
    with prompt_guard_user_scope(user_prompt):
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
        RequiredWorkSource.DISPATCH_AUDIT_SQL,
        RequiredWorkSource.DISPATCH_AUDIT_PROJECTION,
    }
    authority.release(context)
    engine.dispose()


@pytest.mark.asyncio
async def test_callback_failure_is_actual_undispatched_disposition_and_never_sdk(tmp_path, monkeypatch):
    import litellm
    from sqlalchemy import select

    from elspeth.web.composer.provider_gateway import _litellm_acompletion
    from elspeth.web.composer.provider_quota import provider_call_scope
    from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table

    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    user_prompt = "Exercise the owned pre-dispatch callback."
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
        _assert_required_guard_admitted(engine, context, guard_requests, role="composer", user_prompt=user_prompt)
        assert custody.attempt is not None
        assert not custody.needs_terminal_audit()
        raise callback_error

    monkeypatch.setattr(litellm, "acompletion", rejecting_sdk)
    with pytest.raises(AssertionError):
        await litellm.acompletion(model="test/model", messages=[])
    sdk_entries.clear()
    with prompt_guard_user_scope(user_prompt), pytest.raises(ValueError) as caught:
        async with provider_call_scope(custody):
            await _litellm_acompletion(
                provider_custody=custody,
                on_provider_dispatch=callback,
                prompt_guard_mount=build_prompt_guard_mount(settings.composer_prompt_guard),
                prompt_guard_mode=settings.composer_prompt_guard_mode,
                prompt_guard_role="composer",
                prompt_guard_user_prompt=user_prompt,
                model="test/model",
                messages=[{"role": "user", "content": user_prompt}],
            )
    assert caught.value is callback_error
    assert sdk_entries == []
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        messages = connection.execute(select(chat_messages_table)).all()
    assert attempt.settled_at is not None
    assert len(messages) == 2
    cancelled_messages = [row for row in messages if row.tool_calls is None]
    assert len(cancelled_messages) == 1
    assert "provider_attempt.cancelled_before_dispatch" in cancelled_messages[0].content
    cancellation = json.loads(cancelled_messages[0].content)
    assert cancellation["kind"] == "provider_attempt.cancelled_before_dispatch"
    assert cancellation["attempt_id"] == attempt.attempt_id
    assert cancellation["operation_id"] == context.fence.operation_id
    assert cancellation["operation_epoch"] == context.fence.operation_epoch
    assert {ticket.key.source for ticket in coordinator._tickets.values()} == {
        RequiredWorkSource.PROVIDER_ADMISSION_SQL,
        RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,
        RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,
        RequiredWorkSource.DISPATCH_AUDIT_SQL,
        RequiredWorkSource.DISPATCH_AUDIT_PROJECTION,
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
