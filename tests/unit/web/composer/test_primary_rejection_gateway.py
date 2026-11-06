"""Primary rejection evidence must survive physical dispatch and real settlement.

The response-byte cases preserve the existing parser; they do not assert a
currently broken byte limit. All outbound SDK errors below are genuine LiteLLM
errors raised at ``acompletion``, after the normal custody/guard admission.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import httpx
import litellm
import pytest
from litellm import ModelResponse
from litellm.exceptions import BadRequestError
from openai import BadRequestError as OpenAIBadRequestError
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND, ComposerDiagnosticEvidence
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_errors import classify_provider_failure
from elspeth.web.composer.provider_gateway import ProviderGateway, _BadRequestLLMError
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted

pytest_plugins = ("tests.unit.web.composer.test_original_primary_audit_carry",)

_PRIVATE = "PRIVATE_PRIMARY_REJECTION_BODY_CANARY"
_PROMPT = "Explain why this ordinary primary request was rejected."


def _sdk_rejection(*, content: bytes | None = None, response: httpx.Response | None = None, message: str = _PRIVATE) -> BadRequestError:
    if response is None:
        response = httpx.Response(
            400,
            request=httpx.Request("POST", "https://provider.example.invalid/completion"),
            content=content
            if content is not None
            else b'{"error":{"code":"context_length_exceeded","message":"' + _PRIVATE.encode() + b'"}}',
        )
    original = BadRequestError(message, model="test/model", llm_provider="openai", response=response)
    assert original.body is None and original.code is None
    return original


async def _invoke(gateway, entry, custody, recorder):
    messages = [{"role": "user", "content": _PROMPT}]
    if entry == "text":
        return await gateway._call_text_llm_with_audit(messages, timeout=5, recorder=recorder, provider_custody=custody)
    if entry == "tool-outcome":
        return await gateway._call_llm_outcome_with_audit(messages, [], timeout=5, recorder=recorder, provider_custody=custody)
    assert entry == "tool"
    return await gateway._call_llm_with_audit(messages, [], timeout=5, recorder=recorder, provider_custody=custody)


def _descriptor(wrapper: _BadRequestLLMError) -> SafeRequestRejection:
    # Missing evidence is an assertion failure after dispatch/settlement, not a
    # collection failure caused by importing a symbol that does not exist yet.
    assert "request_rejection" in vars(wrapper), "Primary bad-request wrapper discarded safe rejection evidence"
    descriptor = vars(wrapper)["request_rejection"]
    assert isinstance(descriptor, SafeRequestRejection)
    return descriptor


async def _settled_failure(engine, service, context, call):
    assert call.call_id is not None
    with engine.connect() as connection:
        attempt = connection.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id == call.call_id)
        ).one()
        usage = connection.execute(
            select(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == attempt.ledger_entry_id)
        ).one()
    assert attempt.settled_at is not None
    assert attempt.session_id == context.fence.session_id
    assert attempt.operation_id == context.fence.operation_id
    assert attempt.operation_epoch == context.fence.operation_epoch
    assert usage.session_id == context.fence.session_id
    assert usage.prompt_tokens is None and usage.completion_tokens is None
    assert call.prompt_tokens is None and call.completion_tokens is None
    rows = await service.get_messages(UUID(context.fence.session_id), limit=None, include_private_evidence=True)
    evidence_rows = []
    for row in rows:
        for envelope in row.tool_calls or ():
            if envelope.get("_kind") != DIAGNOSTIC_EVIDENCE_KIND or envelope["tool_call_id"] != call.call_id:
                continue
            evidence = ComposerDiagnosticEvidence.from_envelope(
                envelope,
                expected_session_id=context.fence.session_id,
                expected_operation_id=context.fence.operation_id,
                expected_tool_call_id=attempt.attempt_id,
            )
            if evidence.status == "provider_failure":
                evidence_rows.append((row, evidence))
    ((row, evidence),) = evidence_rows
    assert evidence.operation_epoch == context.fence.operation_epoch
    assert evidence.tool_name == "composer_failure"
    assert row.writer_principal == "compose_loop"
    visible = await service.get_messages(UUID(context.fence.session_id), limit=None, include_private_evidence=False)
    assert row.id not in {message.id for message in visible}
    return evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ("tool", "tool-outcome", "text"))
async def test_primary_gateway_retains_rejection_and_exact_cause(primary_case, monkeypatch, entry):
    settings, requests, engine, context, service, coordinator, owner = primary_case
    recorder = BufferingRecorder()
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    original = _sdk_rejection()
    physical = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(engine, context, requests, role="composer", user_prompt="" if entry == "text" else _PROMPT)
        assert custody.attempt is not None
        physical.append(custody.attempt.attempt_id)
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope(_PROMPT), pytest.raises(_BadRequestLLMError) as caught:
        await _invoke(gateway, entry, custody, recorder)
    wrapper = caught.value
    assert wrapper.__cause__ is original
    assert str(wrapper) == "LLM request rejected (BadRequestError)"
    assert _PRIVATE not in str(wrapper)
    assert wrapper.provider_detail == str(original)
    assert wrapper.provider_status_code == 400
    assert len(physical) == len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.call_id == physical[0]
    assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert call.error_class == "BadRequestError"
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()
    evidence = await _settled_failure(engine, service, context, call)
    descriptor = _descriptor(wrapper)
    assert descriptor.reason_code == "context_length_exceeded"
    assert descriptor.reason_disposition == "retained"
    assert descriptor.reason_source == "http_response"
    assert descriptor.retryable is False
    assert descriptor.diagnostic_reference == evidence.evidence_id
    assert descriptor.diagnostic_reference is not None
    assert _PRIVATE not in descriptor.model_dump_json() + descriptor.explanation


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ("tool", "tool-outcome", "text"))
async def test_primary_rejection_reference_names_settled_private_failure_not_previous_call(primary_case, monkeypatch, entry):
    settings, _requests, engine, context, service, coordinator, owner = primary_case
    recorder = BufferingRecorder()
    original = _sdk_rejection()
    physical = []

    async def completion(**kwargs):
        physical.append(kwargs)
        if len(physical) == 1:
            return ModelResponse(
                model="test/model",
                choices=[{"message": {"role": "assistant", "content": "A genuinely earlier reply.", "tool_calls": None}}],
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )
        assert len(physical) == 2, "A rejected unchanged request must not be replayed"
        raise original

    monkeypatch.setattr(litellm, "acompletion", completion)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    with prompt_guard_user_scope(_PROMPT):
        await _invoke(gateway, entry, owner.mint(ProviderInvocationFamily.PRIMARY), recorder)
        before_rejection = len(physical)
        with pytest.raises(_BadRequestLLMError) as caught:
            await _invoke(gateway, entry, owner.mint(ProviderInvocationFamily.PRIMARY), recorder)
    assert len(physical) - before_rejection == 1
    assert len(recorder.llm_calls) == 2
    earlier, rejected = recorder.llm_calls
    assert earlier.status is ComposerLLMCallStatus.SUCCESS
    assert rejected.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert earlier.call_id != rejected.call_id
    assert caught.value.__cause__ is original
    evidence = await _settled_failure(engine, service, context, rejected)
    descriptor = _descriptor(caught.value)
    assert descriptor.diagnostic_reference == evidence.evidence_id
    assert evidence.tool_call_id == rejected.call_id
    assert evidence.tool_call_id != earlier.call_id
    coordinator.assert_completed()


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for child in error.exceptions for leaf in _leaves(child))
    return (error,)


@pytest.mark.asyncio
async def test_rejection_settlement_failure_preserves_original_and_blocks_normal_projection(primary_case, monkeypatch):
    settings, requests, engine, context, service, coordinator, owner = primary_case
    original = _sdk_rejection()
    settlement_failure = RuntimeError("required rejection settlement failed")
    physical = []
    writer = service._write_audit_cohort_on_connection

    async def completion(**kwargs):
        _assert_required_guard_admitted(engine, context, requests, role="composer", user_prompt=_PROMPT)
        physical.append(kwargs)
        raise original

    def fail_actual_settlement(*args, **kwargs):
        if any(envelope.get("_kind") == "llm_call_audit" for draft in kwargs["drafts"] for envelope in draft.tool_calls or ()):
            assert len(physical) == 1
            raise settlement_failure
        return writer(*args, **kwargs)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(service, "_write_audit_cohort_on_connection", fail_actual_settlement)
    gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    recorder = BufferingRecorder()
    with prompt_guard_user_scope(_PROMPT), pytest.raises(BaseExceptionGroup) as caught:
        await _invoke(gateway, "tool-outcome", owner.mint(ProviderInvocationFamily.PRIMARY), recorder)
    leaves = _leaves(caught.value)
    (wrapper,) = [leaf for leaf in leaves if isinstance(leaf, _BadRequestLLMError)]
    assert wrapper.__cause__ is original
    assert any(leaf is settlement_failure or leaf.__cause__ is settlement_failure for leaf in leaves)
    assert len(physical) == len(recorder.llm_calls) == 1
    with engine.connect() as connection:
        attempt = connection.execute(select(quota_provider_attempts_table)).one()
        assert attempt.settled_at is None
        assert connection.execute(select(token_usage_ledger_table)).all() == []
    assert not isinstance(caught.value, _BadRequestLLMError)
    assert coordinator.all_completed


@pytest.mark.parametrize("size", (128, 65_536, 65_537))
def test_litellm_buffered_response_exact_byte_boundary(size):
    base = b'{"error":{"code":"context_length_exceeded"}}'
    payload = base + b" " * (size - len(base))
    assert len(payload) == size
    original = _sdk_rejection(content=payload)
    assert original.body is None and original.code is None
    assert original.response.content == payload
    assert len(original.response.content) == size
    result = classify_provider_failure(original)
    assert result is not None and result.request_rejection is not None
    descriptor = result.request_rejection
    assert result.retryable is False
    if size <= 65_536:
        assert descriptor.reason_code == "context_length_exceeded"
        assert descriptor.reason_source == "http_response"
        assert descriptor.reason_disposition == "retained"
    else:
        assert descriptor.reason_code is None
        assert descriptor.reason_source == "unavailable"
        assert descriptor.reason_disposition == "unavailable"


@pytest.mark.parametrize(
    ("content", "disposition", "source"),
    [
        pytest.param(b"not JSON", "unavailable", "unavailable", id="malformed-json"),
        pytest.param(
            b'{"error":{"code":"invalid_request","code":"context_length_exceeded"}}', "unavailable", "unavailable", id="duplicate-json"
        ),
        pytest.param(b"\xff", "unavailable", "unavailable", id="invalid-utf8"),
        pytest.param(b'{"error":{"code":"PRIVATE_UNKNOWN_CODE"}}', "unrecognized", "http_response", id="unknown-code"),
    ],
)
def test_litellm_buffered_response_parser_neighbors(content, disposition, source):
    original = _sdk_rejection(content=content)
    result = classify_provider_failure(original)
    assert result is not None and result.request_rejection is not None
    assert result.request_rejection.reason_code is None
    assert result.request_rejection.reason_disposition == disposition
    assert result.request_rejection.reason_source == source
    assert "PRIVATE_UNKNOWN_CODE" not in result.request_rejection.model_dump_json()


def test_conflicting_sdk_response_facts_remain_unrecognized():
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://provider.example.invalid"), json={"error": {"code": "unsupported_parameter"}}
    )
    original = OpenAIBadRequestError(_PRIVATE, response=response, body={"code": "context_length_exceeded"})
    result = classify_provider_failure(original)
    assert result is not None and result.request_rejection is not None
    assert result.request_rejection.reason_code is None
    assert result.request_rejection.reason_disposition == "unrecognized"
    assert result.request_rejection.reason_source == "unavailable"


def test_litellm_unread_response_is_not_consumed_for_rejection_evidence():
    class ForbiddenStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            raise AssertionError("Rejection classification must not consume a provider stream")

    response = httpx.Response(400, request=httpx.Request("POST", "https://provider.example.invalid"), stream=ForbiddenStream())
    original = _sdk_rejection(response=response)
    result = classify_provider_failure(original)
    assert result is not None and result.request_rejection is not None
    assert result.request_rejection.reason_code is None
    assert result.request_rejection.reason_disposition == "unavailable"
    assert result.request_rejection.reason_source == "unavailable"
    assert response.is_stream_consumed is False
