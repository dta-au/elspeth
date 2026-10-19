"""Real settlement failures retain the selected legacy or required identity."""

import time
from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import (
    ProviderInvocationFamily,
    admit_provider_attempt,
    composer_quota_scope,
    quota_provider_calls,
)
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table, token_usage_ledger_table
from tests.helpers.quota_guard import guarded_quota_turn
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
    failure = failure_type("actual settlement writer fault")
    physical = []
    writes = []
    messages = [{"role": "user", "content": "Build the requested pipeline."}]
    recorder = BufferingRecorder()
    write_cohort = service._write_audit_cohort_on_connection

    async def completion(**kwargs):
        physical.append(kwargs)
        return ModelResponse(
            model="test/model",
            choices=[{"message": {"role": "assistant", "content": "Actual provider prose.", "tool_calls": None}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    def fail_settlement_cohort(*args, **kwargs):
        drafts = kwargs["drafts"]
        assert all(len(draft.tool_calls) == 1 for draft in drafts)
        kinds = [draft.tool_calls[0]["_kind"] for draft in drafts]
        if kinds == ["prompt_guard_audit"]:
            assert required and physical == [] and writes == []
            return write_cohort(*args, **kwargs)
        assert kinds == ["llm_call_audit", DIAGNOSTIC_EVIDENCE_KIND]
        assert len(physical) == 1
        assert drafts[0].tool_calls[0]["call"]["status"] == "success"
        writes.append(True)
        raise failure

    @quota_provider_calls
    async def legacy_settlement() -> None:
        # The legacy span is still supported below the guarded SDK boundary.
        # This callback is synthetic; it makes no physical-provider claim.
        started_at = datetime.now(UTC)
        started_ns = time.monotonic_ns()
        await admit_provider_attempt(model="test/model")
        response = await completion(model="test/model", messages=messages)
        recorder.record_llm_call(
            build_llm_call_record(
                model_requested="test/model",
                messages=messages,
                tools=None,
                status=ComposerLLMCallStatus.SUCCESS,
                started_at=started_at,
                started_ns=started_ns,
                temperature=None,
                seed=None,
                response=response,
                private_request=capture_provider_context(messages=messages, tools=None),
            )
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", fail_settlement_cohort)
    try:
        with guarded_quota_turn(engine, service, context) if required else nullcontext(None) as turn:
            with (
                nullcontext() if required else composer_quota_scope(service, context),
                pytest.raises(ComposerOwnedSettlementFailure if required else failure_type) as caught,
            ):
                if required:
                    assert turn is not None
                    gateway = ProviderGateway(
                        model="test/model",
                        settings=_settings(tmp_path, composer_prompt_guard_mode="required"),
                        endpoint_base_url=None,
                        endpoint_api_key=None,
                    )
                    # Install the actual Azure client with its HTTP transport;
                    # no inspect/enforcer/parser/decision function is replaced.
                    monkeypatch.setattr(gateway, "_prompt_guard_mount", turn.mount)
                    custody = turn.owner.mint(ProviderInvocationFamily.PRIMARY)
                    await gateway._call_llm_with_audit(messages, [], timeout=5, recorder=recorder, provider_custody=custody)
                else:
                    await legacy_settlement()
            assert len(physical) == 1 and writes == [True]
            if required:
                assert caught.value.__cause__ is failure
                assert turn is not None and turn.coordinator.all_completed
                assert any(failure is error for ticket in turn.coordinator.tickets for error in ticket.errors)
                turn.assert_audit(guards=1, calls=0, private=0)
                assert len(turn.requests) == 1
            else:
                assert caught.value is failure
            assert len(recorder.llm_calls) == 1
            with engine.connect() as connection:
                attempt = connection.execute(select(quota_provider_attempts_table)).one()
                assert attempt.settled_at is None
                assert connection.execute(select(token_usage_ledger_table)).all() == []
                if not required:
                    assert connection.execute(select(chat_messages_table)).all() == []
            assert recorder.llm_calls[0].call_id == attempt.attempt_id
    finally:
        authority.release(context)
        engine.dispose()
