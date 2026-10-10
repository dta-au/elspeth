"""Real settlement failures retain the selected legacy or required identity."""

from contextlib import nullcontext
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner, composer_quota_scope
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table, token_usage_ledger_table
from tests.unit.web.composer.test_llm_sampling_config import _settings
from tests.unit.web.sessions.test_token_usage_adapters import _quota_service


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("failure_type", [RuntimeError, TimeoutError])
async def test_actual_settlement_writer_failure_only_wraps_required_scope(tmp_path, monkeypatch, required, failure_type):
    import litellm
    from litellm import ModelResponse

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="Settlement identity", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = (
        RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4())))
        if required
        else None
    )
    custody = None
    if coordinator is not None:
        owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    failure = failure_type("actual settlement writer fault")
    physical = []
    writes = []

    async def completion(**kwargs):
        physical.append(kwargs)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "Actual provider prose.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    def fail_cohort(*args, **kwargs):
        writes.append(True)
        raise failure

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", fail_cohort)
    recorder = BufferingRecorder()
    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    try:
        with (
            nullcontext() if required else composer_quota_scope(service, context),
            pytest.raises(ComposerOwnedSettlementFailure if required else failure_type) as caught,
        ):
            await gateway._call_llm_with_audit([], [], timeout=5, recorder=recorder, provider_custody=custody)
        assert len(physical) == 1 and writes == [True]
        if required:
            assert caught.value.__cause__ is failure
            assert coordinator is not None and coordinator.all_completed
            assert any(failure is error for ticket in coordinator.tickets for error in ticket.errors)
        else:
            assert caught.value is failure
        assert len(recorder.llm_calls) == 1
        with engine.connect() as connection:
            attempt = connection.execute(select(quota_provider_attempts_table)).one()
            assert attempt.settled_at is None
            assert connection.execute(select(token_usage_ledger_table)).all() == []
            assert connection.execute(select(chat_messages_table)).all() == []
        assert recorder.llm_calls[0].call_id == attempt.attempt_id
    finally:
        authority.release(context)
        engine.dispose()
