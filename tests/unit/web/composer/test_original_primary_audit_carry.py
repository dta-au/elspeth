"""The existing physical primary audit survives its existing owned settlement."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus, ToolContractDialect
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer import provider_gateway
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _CallModelOutcome
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_codec import encode_continuation_response
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.recovery_controls import recovery_tool_definitions
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service


@pytest.fixture
def primary_case(tmp_path, monkeypatch):
    settings, guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    settings = settings.model_copy(update={"composer_model": "test/model"})
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="original audit", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    try:
        yield settings, guard_requests, engine, context, service, coordinator, owner
    finally:
        authority.release(context)
        engine.dispose()


def _reply():
    return ModelResponse(
        model="test/model",
        choices=[
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "The original provider reply.", "tool_calls": None},
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ("gateway", "gateway_recovery", "model_turn"))
async def test_original_primary_call_reaches_outcome_after_one_actual_settlement(primary_case, monkeypatch, entry):
    settings, requests, engine, context, service, coordinator, owner = primary_case
    user_prompt = "Retain the ordinary primary reply."
    physical = []
    created = []
    settled = []
    original_build = provider_gateway.build_llm_call_record
    original_settle = type(service).finish_provider_attempt

    def build(**kwargs):
        call = original_build(**kwargs)
        created.append(call)
        return call

    async def settle(self, **kwargs):
        settled.append(kwargs["call"])
        return await original_settle(self, **kwargs)

    async def completion(**kwargs):
        _assert_required_guard_admitted(engine, context, requests, role="composer", user_prompt=user_prompt)
        physical.append(kwargs)
        if entry == "gateway_recovery":
            return ModelResponse(
                model="test/model",
                choices=[
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "The original provider reply.",
                            "tool_calls": [
                                {
                                    "id": "original-recovery",
                                    "type": "function",
                                    "function": {"name": "handoff_to_user", "arguments": '{"action":"park","message":"Review the draft."}'},
                                }
                            ],
                        },
                    }
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )
        return _reply()

    monkeypatch.setattr(provider_gateway, "build_llm_call_record", build)
    monkeypatch.setattr(type(service), "finish_provider_attempt", settle)
    monkeypatch.setattr(litellm, "acompletion", completion)
    recorder = BufferingRecorder()
    messages = [{"role": "user", "content": user_prompt}]
    with prompt_guard_user_scope(user_prompt):
        if entry in {"gateway", "gateway_recovery"}:
            custody = owner.mint(ProviderInvocationFamily.PRIMARY)
            gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
            tools = recovery_tool_definitions(ToolContractDialect.NONE) if entry == "gateway_recovery" else []
            outcome = await gateway._call_llm_outcome_with_audit(messages, tools, timeout=5, recorder=recorder, provider_custody=custody)
            assert custody.attempt is None and custody.call is None
        else:
            composer = ComposerServiceImpl.for_trained_operator(
                catalog=create_catalog_service(), settings=settings, sessions_service=service
            )
            state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
            outcome = await composer._call_model_turn(
                llm_messages=messages,
                tools=[],
                state=state,
                initial_version=state.version,
                deadline=asyncio.get_event_loop().time() + 10,
                recorder=recorder,
                progress=None,
                message=user_prompt,
                composition_turns_used=0,
                discovery_turns_used=0,
                failed_turn=None,
                provider_owner=owner,
            )
    assert type(outcome) is _CallModelOutcome and type(outcome.completion) is _AdmittedLLMCompletion
    assert len(physical) == len(created) == len(settled) == len(recorder.llm_calls) == len(_ledger(engine)) == 1
    assert outcome.audit_call is not None
    assert outcome.audit_call is settled[0] is recorder.llm_calls[0]
    if entry == "gateway_recovery":
        assert outcome.audit_call is not created[0] and outcome.audit_call.private_diagnostics
        assert replace(outcome.audit_call, private_diagnostics=created[0].private_diagnostics) == created[0]
    else:
        assert outcome.audit_call is created[0]
    assert outcome.audit_call.status is ComposerLLMCallStatus.SUCCESS
    assert outcome.completion.message.content == "The original provider reply."
    assert physical[0]["messages"] == messages
    # The ordinary retained response receives the actual original object.
    retained = encode_continuation_response(outcome.completion, audit_call=outcome.audit_call)
    assert outcome.audit_call.call_id is not None
    assert outcome.audit_call.call_id in retained.canonical
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("exception", "cancelled"))
async def test_outcome_path_preserves_original_exception_and_settles_once(primary_case, monkeypatch, failure_kind):
    settings, requests, engine, context, _service, coordinator, owner = primary_case
    user_prompt = "Exercise the original primary exception."
    error = ValueError("physical provider error") if failure_kind == "exception" else asyncio.CancelledError("physical cancellation")
    physical = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(engine, context, requests, role="composer", user_prompt=user_prompt)
        physical.append(kwargs)
        raise error

    monkeypatch.setattr(litellm, "acompletion", completion)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope(user_prompt), pytest.raises(type(error)) as caught:
        await gateway._call_llm_outcome_with_audit(
            [{"role": "user", "content": user_prompt}], [], timeout=5, recorder=recorder, provider_custody=custody
        )
    assert caught.value is error
    assert len(physical) == len(recorder.llm_calls) == len(_ledger(engine)) == 1
    expected = ComposerLLMCallStatus.API_ERROR if failure_kind == "exception" else ComposerLLMCallStatus.CANCELLED
    assert recorder.llm_calls[0].status is expected
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_outcome_path_keeps_sdk_and_audit_failures_as_original_leaves(primary_case, monkeypatch):
    settings, requests, engine, context, _service, coordinator, owner = primary_case
    user_prompt = "Keep both original failure objects."
    sdk_error, audit_error = ValueError("physical error"), RuntimeError("audit error")

    async def completion(**kwargs):
        _assert_required_guard_admitted(engine, context, requests, role="composer", user_prompt=user_prompt)
        raise sdk_error

    def broken_audit(**kwargs):
        raise audit_error

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(provider_gateway, "build_llm_call_record", broken_audit)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope(user_prompt), pytest.raises(BaseExceptionGroup) as caught:
        await gateway._call_llm_outcome_with_audit(
            [{"role": "user", "content": user_prompt}], [], timeout=5, recorder=None, provider_custody=custody
        )

    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return tuple(leaf for child in error.exceptions for leaf in leaves(child))
        return (error,)

    actual = leaves(caught.value)
    assert any(error is sdk_error for error in actual)
    assert any(error is audit_error for error in actual)
    assert custody.attempt is not None and custody.call is None
    assert _ledger(engine) == []
    assert all(ticket.complete for ticket in coordinator._tickets.values())


@pytest.mark.asyncio
async def test_legacy_primary_body_and_gateway_keep_the_completion_return_shape(tmp_path, monkeypatch):
    from tests.unit.web.composer.test_llm_sampling_config import _settings

    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    gateway_calls = []
    legacy_completion = provider_gateway._admit_composer_llm_completion(_reply())

    async def admitted_completion(messages, tools, **kwargs):
        gateway_calls.append((messages, tools, kwargs))
        return legacy_completion

    # Isolate the legacy wrappers' return shape. Actual physical calls and
    # settlement use the required-custody tests above.
    monkeypatch.setattr(gateway, "_call_llm", admitted_completion)
    messages = [{"role": "user", "content": "Ordinary legacy completion."}]
    first = await gateway._call_llm_with_audit_body(messages, [], timeout=5, recorder=None)
    second = await gateway._call_llm_with_audit(messages, [], timeout=5, recorder=None)
    assert type(first) is type(second) is _AdmittedLLMCompletion
    assert first is second is legacy_completion
    assert len(gateway_calls) == 2 and all(call[0] == messages for call in gateway_calls)

    old_hook_calls = []

    async def old_completion_hook(self, *args, **kwargs):
        old_hook_calls.append((args, kwargs))
        return first

    async def unexpected_outcome_hook(self, *args, **kwargs):
        raise AssertionError("Legacy model turn bypassed its existing completion hook")

    monkeypatch.setattr(ComposerServiceImpl, "_call_llm_before_deadline", old_completion_hook)
    monkeypatch.setattr(ComposerServiceImpl, "_call_llm_outcome_before_deadline", unexpected_outcome_hook)
    composer = ComposerServiceImpl.for_trained_operator(catalog=create_catalog_service(), settings=_settings(tmp_path))
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    outcome = await composer._call_model_turn(
        llm_messages=messages,
        tools=[],
        state=state,
        initial_version=state.version,
        deadline=asyncio.get_event_loop().time() + 10,
        recorder=BufferingRecorder(),
        progress=None,
        message="Ordinary legacy completion.",
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
    )
    assert len(old_hook_calls) == 1 and len(gateway_calls) == 2
    assert outcome.completion is first and outcome.audit_call is None
