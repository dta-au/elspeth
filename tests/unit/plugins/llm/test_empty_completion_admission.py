"""Ordinary in-memory SDK controls; no external transport/provider dispatch."""

import base64
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from elspeth.contracts import CallStatus, CallType, RunMode
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.call_mode import ReplayCallEvidence, VerificationDecision
from elspeth.contracts.chat_parts import ChatMessage, ImagePart
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient, ContentPolicyError, LLMClientError, classify_unusable_content
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers.azure import AzureLLMProvider
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockCredentials, BedrockLLMProvider
from elspeth.plugins.transforms.llm.providers.gateway import _validate_gateway_success_response
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.clients.test_audited_llm_client import PNG_BYTES, FakeExecutionRepository, FakeOpenAIClient, provider_response
from tests.unit.plugins.llm.test_provider_azure import _LEADER_TOKEN, FakeAuditRecorder, FakeTelemetryEmit

MESSAGES = [ChatMessage(role="user", content="local ordinary control")]


@pytest.mark.parametrize(
    "finish,refusal,expected",
    [
        ("stop", None, LLMClientError),
        ("length", None, LLMClientError),
        ("unexpected", None, LLMClientError),
        ("tool_calls", None, LLMClientError),
        ("content_filter", None, ContentPolicyError),
        ("stop", "explicit provider refusal", ContentPolicyError),
        ("stop", {"untrusted": "truthy"}, LLMClientError),
        ("stop", "   ", LLMClientError),
    ],
)
@pytest.mark.parametrize("content", [None, "", "   "])
def test_unusable_completion_requires_affirmative_policy_evidence(
    finish: str, refusal: object, expected: type[LLMClientError], content: str | None
) -> None:
    raw = {"choices": [{"finish_reason": finish, "message": {"content": content, "refusal": refusal}}]}
    sdk = FakeOpenAIClient(
        response=provider_response(
            content=content,
            finish_reason=finish,
            raw_response=raw,
            message=SimpleNamespace(content=content, refusal=refusal),
        )
    )
    execution = FakeExecutionRepository()
    telemetry = []
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=telemetry.append,
        underlying_client=sdk,
        require_nonempty_content=content is not None,
    )
    with pytest.raises(expected) as error:
        client.chat_completion(model="gpt-4", messages=MESSAGES)
    assert type(error.value) is expected
    assert error.value.retryable is False
    assert len(sdk.create_calls) == 1
    execution.assert_recorded_once()
    assert execution.last_record_call_kwargs["status"] is CallStatus.ERROR
    assert execution.last_record_call_kwargs["response_data"].raw_response["choices"][0]["message"]["content"] == content
    audit_error = execution.last_record_call_kwargs["error"]
    assert audit_error.type == (
        "ContentPolicyError"
        if expected is ContentPolicyError
        else "UnsupportedResponseError"
        if finish == "tool_calls"
        else "LLMClientError"
    )
    assert audit_error.category == (
        "content_policy" if expected is ContentPolicyError else "response_processing" if finish == "tool_calls" else "client"
    )
    assert len(telemetry) == 1
    assert telemetry[0].status is CallStatus.ERROR


@pytest.mark.parametrize("content", ["", "   "])
def test_low_level_empty_string_compatibility_is_preserved(content: str) -> None:
    sdk = FakeOpenAIClient(response=provider_response(content=content))
    execution = FakeExecutionRepository()
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda event: None,
        underlying_client=sdk,
    )
    assert client.chat_completion(model="gpt-4", messages=MESSAGES).content == content
    assert execution.last_record_call_kwargs["status"] is CallStatus.SUCCESS


@pytest.mark.parametrize("provider_name", ["azure", "bedrock"])
@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("content", [None, "", "   "])
def test_pipeline_providers_audit_unusable_blank_as_client_error(
    monkeypatch: pytest.MonkeyPatch, provider_name: str, preflight: bool, content: str | None
) -> None:
    recorder = FakeAuditRecorder()
    telemetry = FakeTelemetryEmit()
    sdk = FakeOpenAIClient(
        response=provider_response(
            content=content,
            finish_reason="length",
            raw_response={"choices": [{"finish_reason": "length", "message": {"content": content}}]},
        )
    )
    if provider_name == "azure":
        provider = AzureLLMProvider(
            endpoint="https://ordinary.invalid/",
            api_key="local-placeholder",
            api_version="2024-10-21",
            deployment_name="gpt-4",
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=telemetry,
        )
    else:
        provider = BedrockLLMProvider(
            region_name=None,
            credentials=BedrockCredentials(),
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=telemetry,
        )
    monkeypatch.setattr(provider, "_get_underlying_client", lambda: sdk)
    closed: list[AuditedLLMClient] = []
    original_close = AuditedLLMClient.close

    def close_client(client: AuditedLLMClient) -> None:
        closed.append(client)
        original_close(client)

    monkeypatch.setattr(AuditedLLMClient, "close", close_client)
    try:
        with pytest.raises(LLMClientError) as error:
            if preflight:
                provider.runtime_preflight(operation_id="op-1", model="gpt-4", temperature=None, coordination_token=_LEADER_TOKEN)
            else:
                provider.execute_query(
                    MESSAGES,
                    model="gpt-4",
                    temperature=None,
                    max_tokens=32,
                    audit_parent=LLMAuditParent.for_operation(operation_id="op-1", coordination_token=_LEADER_TOKEN),
                )
        assert type(error.value) is LLMClientError
        assert error.value.retryable is False
        assert len(sdk.create_calls) == 1
        assert len(recorder.operation_calls) == 1
        assert recorder.operation_calls[0]["status"] is CallStatus.ERROR
        assert len(telemetry.events) == 1
        assert telemetry.events[0].status is CallStatus.ERROR
        assert provider._llm_clients == {}
        assert len(closed) == int(preflight)
    finally:
        provider.close()


@pytest.mark.parametrize(
    "content,finish,raw,expected",
    [
        (None, "content_filter", {}, ContentPolicyError),
        (None, "content_filter", {"choices": [{"message": {"content": None}}]}, ContentPolicyError),
        (None, None, {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}, LLMClientError),
        (None, "stop", {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}, LLMClientError),
        (None, "content_filter", {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}, LLMClientError),
        (None, "content_filter", {"choices": [{"message": {"content": "", "refusal": "private-sentinel"}}]}, LLMClientError),
        ("", "content_filter", {"choices": [{"message": {"content": None, "refusal": "private-sentinel"}}]}, LLMClientError),
        (None, "stop", {"choices": [{"message": {"content": "private-sentinel", "refusal": "refused"}}]}, LLMClientError),
        (None, "content_filter", {"choices": "private-sentinel"}, LLMClientError),
        (None, "content_filter", {"choices": []}, LLMClientError),
        (None, "content_filter", {"choices": ["private-sentinel"]}, LLMClientError),
        (None, "content_filter", {"choices": [{"message": None}]}, LLMClientError),
        (None, "content_filter", {"choices": [{"message": {"content": 42}}]}, LLMClientError),
        (None, "content_filter", {"choices": [{"finish_reason": {"private-sentinel": 1}}]}, LLMClientError),
        (None, {"private-sentinel": 1}, {}, LLMClientError),
        (None, "stop", {"choices": [{"message": {"refusal": "private-sentinel"}}]}, LLMClientError),
        (
            None,
            "stop",
            {"choices": [{"message": {"content": None}}, {"message": {"content": None, "refusal": "private-sentinel"}}]},
            LLMClientError,
        ),
        (None, "tool_calls", {"choices": [{"message": {"content": None, "refusal": "private-sentinel"}}]}, ContentPolicyError),
    ],
)
def test_sdk_and_captured_evidence_cannot_borrow_a_different_refusal(
    content: str | None, finish: Any, raw: Mapping[str, Any], expected: type[LLMClientError]
) -> None:
    execution = FakeExecutionRepository()
    telemetry = []
    sdk = FakeOpenAIClient(response=provider_response(content=content, finish_reason=finish, raw_response=raw))
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=telemetry.append,
        underlying_client=sdk,
        require_nonempty_content=True,
    )
    with pytest.raises(expected) as error:
        client.chat_completion(model="gpt-4", messages=MESSAGES)
    assert type(error.value) is expected
    assert not error.value.retryable
    assert "private-sentinel" not in str(error.value)
    execution.assert_recorded_once()
    recorded = execution.last_record_call_kwargs
    assert recorded["status"] is CallStatus.ERROR
    assert recorded["response_data"].raw_response == deep_freeze(raw)
    assert "private-sentinel" not in recorded["error"].message
    assert recorded["error"].category == ("content_policy" if expected is ContentPolicyError else "client")
    assert len(sdk.create_calls) == len(telemetry) == 1


@pytest.mark.parametrize("frozen", [False, True])
def test_shared_classifier_accepts_frozen_matching_first_message(frozen: bool) -> None:
    raw = {"choices": [{"finish_reason": "stop", "message": {"content": " ", "refusal": "private-sentinel"}}]}
    evidence = deep_freeze(raw) if frozen else raw
    verdict = classify_unusable_content(content=" ", raw_response=evidence)
    assert type(verdict.error) is ContentPolicyError
    assert verdict.audit_type == "ContentPolicyError"
    assert verdict.audit_category == "content_policy"
    assert "private-sentinel" not in str(verdict.error)


@pytest.mark.parametrize("choices", [b"private-sentinel", bytearray(b"private-sentinel")])
def test_text_bytes_are_not_choice_arrays(choices: object) -> None:
    verdict = classify_unusable_content(content=None, raw_response={"choices": choices}, sdk_finish_reason="content_filter")
    assert type(verdict.error) is LLMClientError
    assert "private-sentinel" not in str(verdict.error)


@pytest.mark.parametrize("content", [None, "", "   "])
@pytest.mark.parametrize(
    "finish,refusal,expected",
    [
        ("stop", None, LLMClientError),
        ("length", None, LLMClientError),
        ("tool_calls", None, LLMClientError),
        ("content_filter", None, ContentPolicyError),
        ("stop", "private-sentinel", ContentPolicyError),
    ],
)
def test_gateway_content_classification_precedes_required_usage(
    content: str | None, finish: str, refusal: str | None, expected: type[LLMClientError]
) -> None:
    response = httpx.Response(
        200, json={"choices": [{"finish_reason": finish, "message": {"content": content, "refusal": refusal}}], "model": "gpt-4"}
    )
    with pytest.raises(expected) as error:
        _validate_gateway_success_response(response, usage_required=True)
    assert type(error.value) is expected
    assert not error.value.retryable
    assert "usage" not in str(error.value)
    assert "private-sentinel" not in str(error.value)


def _pipeline_provider(
    name: str, recorder: FakeAuditRecorder, telemetry: FakeTelemetryEmit, **options: Any
) -> AzureLLMProvider | BedrockLLMProvider:
    if name == "azure":
        return AzureLLMProvider(
            endpoint="https://ordinary.invalid/",
            api_key="local-placeholder",
            api_version="2024-10-21",
            deployment_name="gpt-4",
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=telemetry,
            **options,
        )
    assert name == "bedrock"
    return BedrockLLMProvider(
        region_name=None,
        credentials=BedrockCredentials(),
        recorder=recorder,
        run_id="run-1",
        telemetry_emit=telemetry,
        **options,
    )


@pytest.mark.parametrize("provider_name", ["azure", "bedrock"])
def test_unusable_row_call_preserves_its_actual_audit_parent(provider_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = FakeAuditRecorder()
    telemetry = FakeTelemetryEmit()
    provider = _pipeline_provider(provider_name, recorder, telemetry, approved_prompt_artifact_hash="b" * 64)
    sdk = FakeOpenAIClient(response=provider_response(content=" ", finish_reason="length"))
    monkeypatch.setattr(provider, "_get_underlying_client", lambda: sdk)
    authority = mock_audit_authority("run-1", token_id="token-row")
    parent = LLMAuditParent.for_row(
        member_token=authority["member_token"], work_item=authority["work_item"], state_id="state-row", token_id="token-row"
    )
    try:
        with pytest.raises(LLMClientError) as error:
            provider.execute_query(MESSAGES, model="gpt-4", temperature=None, max_tokens=32, audit_parent=parent)
        assert type(error.value) is LLMClientError
        assert recorder.allocated_state_ids == ["state-row"]
        assert recorder.operation_calls == []
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["state_id"] == "state-row"
        assert call["member_token"] == authority["member_token"]
        assert call["work_item"] is authority["work_item"]
        assert call["call_index"] == 0
        assert call["status"] is CallStatus.ERROR
        assert call["error"].category == "client"
        assert call["approved_prompt_artifact_hash"] == "b" * 64
        assert "require_nonempty_content" not in sdk.single_create_kwargs()
        assert provider._llm_clients == {}
        assert telemetry.events[0].token_id == "token-row"
        assert telemetry.events[0].state_id == "state-row"
        assert telemetry.events[0].operation_id is None
    finally:
        provider.close()


@pytest.mark.parametrize("content", [None, " "])
@pytest.mark.parametrize("audit_fails", [False, True])
def test_unusable_response_preserves_cost_governance_and_audit_before_telemetry(
    content: str | None, audit_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    failure = AuditIntegrityError("controlled audit failure")

    class Execution(FakeExecutionRepository):
        def record_call(self, **kwargs: Any) -> SimpleNamespace:
            events.append("audit")
            if audit_fails:
                raise failure
            return super().record_call(**kwargs)

    execution = Execution()
    emitted = []
    sdk = FakeOpenAIClient(
        response=provider_response(
            content=content,
            finish_reason="length",
            usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10, "cost": 0.125},
            raw_response={"choices": [{"message": {"content": content}, "finish_reason": "length"}]},
        )
    )
    original_create = sdk.chat.completions.create

    def create(**kwargs: Any) -> object:
        events.append("sdk")
        return original_create(**kwargs)

    def before() -> str:
        events.append("before")
        return "attempt-1"

    def after(attempt_id: str, call_id: str) -> None:
        assert (attempt_id, call_id) == ("attempt-1", "call-1")
        assert len(execution.recorded_calls) == 1
        events.append("after")

    def emit(event: Any) -> None:
        events.append("telemetry")
        emitted.append(event)

    monkeypatch.setattr(sdk.chat.completions, "create", create)
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=emit,
        underlying_client=sdk,
        require_nonempty_content=True,
        llm_call_governance=LLMCallGovernance(before_call=before, after_call=after),
        pricing_model="operator-pricing-alias",
    )
    with pytest.raises(AuditIntegrityError if audit_fails else LLMClientError) as error:
        client.chat_completion(model="gpt-4", messages=MESSAGES, approved_prompt_artifact_hash="a" * 64)
    if audit_fails:
        assert error.value is failure
        assert execution.recorded_calls == emitted == []
        assert events == ["before", "sdk", "audit"]
    else:
        assert type(error.value) is LLMClientError
        execution.assert_recorded_once()
        recorded = execution.last_record_call_kwargs
        assert recorded["call_index"] == 0
        assert recorded["approved_prompt_artifact_hash"] == "a" * 64
        expected_usage = TokenUsage(prompt_tokens=10, completion_tokens=0, reported_total=10)
        assert recorded["token_usage"] == expected_usage
        assert recorded["error"].category == "client"
        assert recorded["error"].provider_cost == recorded["response_data"].provider_cost == 0.125
        assert recorded["error"].pricing_model == "operator-pricing-alias"
        assert recorded["error"].provider_cost_source == "response_usage.cost"
        assert emitted[0].status is CallStatus.ERROR
        assert emitted[0].token_usage == expected_usage
        assert events == ["before", "sdk", "audit", "after", "telemetry"]
    assert len(sdk.create_calls) == 1


@pytest.mark.parametrize("content", [None, " "])
def test_unusable_response_image_policy_remains_authoritative(content: str | None) -> None:
    image = ImagePart.from_bytes(format="png", data=PNG_BYTES, blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    raw = {"choices": [{"message": {"content": content}, "finish_reason": "content_filter"}], "debug": encoded}
    execution = FakeExecutionRepository()
    emitted = []
    sdk = FakeOpenAIClient(response=provider_response(content=content, finish_reason="content_filter", raw_response=raw))
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=emitted.append,
        underlying_client=sdk,
        require_nonempty_content=True,
    )
    with pytest.raises(LLMClientError) as error:
        client.chat_completion(model="gpt-4", messages=[ChatMessage(role="user", content=(image,))])
    assert type(error.value) is LLMClientError
    assert encoded not in str(error.value)
    execution.assert_recorded_once()
    recorded = execution.last_record_call_kwargs
    assert recorded["status"] is CallStatus.ERROR
    assert recorded["error"].type == "ImageResponsePolicyError"
    assert recorded["response_data"] is None
    assert len(emitted) == len(sdk.create_calls) == 1
    assert emitted[0].status is CallStatus.ERROR
    assert emitted[0].response_payload is None


@pytest.mark.parametrize("content", [12, [], {"private-sentinel": "value"}])
def test_nonempty_opt_in_preserves_malformed_content_admission(content: object) -> None:
    execution = FakeExecutionRepository()
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda event: None,
        underlying_client=FakeOpenAIClient(response=provider_response(content=content)),
        require_nonempty_content=True,
    )
    with pytest.raises(LLMClientError) as error:
        client.chat_completion(model="gpt-4", messages=MESSAGES)
    assert type(error.value) is LLMClientError
    assert execution.last_record_call_kwargs["error"].type == "MalformedResponseError"
    assert execution.last_record_call_kwargs["error"].category == "response_processing"


@pytest.mark.parametrize("content", [None, " "])
def test_verify_receives_the_authoritative_unusable_response_error(content: str | None) -> None:
    admissions: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    sdk = FakeOpenAIClient(response=provider_response(content=content, finish_reason="length"))

    class Session:
        mode = RunMode.VERIFY

        def admit_verify_call(self, **kwargs: Any) -> str:
            assert sdk.create_calls == []
            admissions.append(kwargs)
            return "source-call"

        def verify_call(self, **kwargs: Any) -> VerificationDecision:
            comparisons.append(kwargs)
            return VerificationDecision(current_call_id=kwargs["current_call_id"], source_call_id="source-call", is_match=True)

    execution = FakeExecutionRepository()
    emitted = []
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=emitted.append,
        underlying_client=sdk,
        require_nonempty_content=True,
        call_mode_session=Session(),
    )
    with pytest.raises(LLMClientError) as error:
        client.chat_completion(model="gpt-4", messages=MESSAGES)
    assert type(error.value) is LLMClientError
    assert len(admissions) == len(comparisons) == len(sdk.create_calls) == len(emitted) == 1
    comparison = comparisons[0]
    recorded = execution.last_record_call_kwargs
    assert comparison["live_status"] is CallStatus.ERROR
    assert comparison["current_call_index"] == recorded["call_index"] == 0
    assert comparison["live_response_data"] == recorded["response_data"].to_dict()
    assert comparison["live_error_data"] == recorded["error"].to_dict()
    assert comparison["live_error_data"]["category"] == "client"
    assert "require_nonempty_content" not in admissions[0]["request_data"]
    assert "require_nonempty_content" not in sdk.single_create_kwargs()


class _RecordedSession:
    """Replay a DTO actually captured by AuditedLLMClient, with no live I/O."""

    mode = RunMode.REPLAY

    def __init__(self, call: dict[str, Any]) -> None:
        self.call = call
        self.requests: list[dict[str, Any]] = []

    def replay_call(self, **kwargs: Any) -> ReplayCallEvidence:
        assert kwargs["call_type"] is CallType.LLM
        assert kwargs["request_data"] == self.call["request_data"].to_dict()
        assert kwargs["current_state_id"] is None
        assert kwargs["current_operation_id"] == "op-1"
        assert kwargs["current_call_index"] == 0
        self.requests.append(kwargs)
        return ReplayCallEvidence(
            source_call_id="retained-source-call",
            status=self.call["status"],
            response_data=self.call["response_data"].to_dict(),
            error_data=None if self.call["error"] is None else self.call["error"].to_dict(),
            latency_ms=self.call["latency_ms"],
        )


def _capture_source_call(provider_name: str, preflight: bool, content: str, finish: str, *, require_nonempty: bool) -> dict[str, Any]:
    recorder = FakeExecutionRepository()
    client = AuditedLLMClient(
        **mock_audit_authority(),
        execution=recorder,
        state_id="source-state",
        run_id="run-1",
        telemetry_emit=lambda event: None,
        underlying_client=FakeOpenAIClient(
            response=provider_response(
                content=content,
                finish_reason=finish,
                raw_response={"choices": [{"message": {"content": content}, "finish_reason": finish}]},
                usage={"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10, "cost": 0.125},
            )
        ),
        provider=provider_name,
        max_tokens_param="max_completion_tokens" if provider_name == "azure" else "max_tokens",
        require_nonempty_content=require_nonempty,
    )
    messages = [ChatMessage(role="user", content="This is a pre-flight smoke test. Please reply with ok.")] if preflight else MESSAGES
    budget = 2048 if preflight and provider_name == "azure" else 32
    # Query providers explicitly pass response_format=None; preflight omits it.
    # Preserve that request distinction in the source recording and exact match.
    request_options = {} if preflight else {"response_format": None}
    if require_nonempty:
        with pytest.raises(LLMClientError):
            client.chat_completion(model="gpt-4", messages=messages, temperature=None, max_tokens=budget, **request_options)
    else:
        assert (
            client.chat_completion(model="gpt-4", messages=messages, temperature=None, max_tokens=budget, **request_options).content
            == content
        )
    recorder.assert_recorded_once()
    return recorder.last_record_call_kwargs


class _NoLiveLimiter:
    def acquire(self) -> None:
        pytest.fail("replay acquired live dispatch capacity")


def _invoke_provider(provider: AzureLLMProvider | BedrockLLMProvider, preflight: bool) -> Any:
    if preflight:
        return provider.runtime_preflight(operation_id="op-1", model="gpt-4", temperature=None, coordination_token=_LEADER_TOKEN)
    return provider.execute_query(
        MESSAGES,
        model="gpt-4",
        temperature=None,
        max_tokens=32,
        audit_parent=LLMAuditParent.for_operation(operation_id="op-1", coordination_token=_LEADER_TOKEN),
    )


@pytest.mark.parametrize("provider_name", ["azure", "bedrock"])
@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("content,finish", [("", "stop"), ("   ", "stop"), ("", "content_filter"), ("answer", "stop")])
def test_historical_success_replay_retains_history_and_refuses_unusable_text_without_sdk(
    provider_name: str, preflight: bool, content: str, finish: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The generic client deliberately still admits blank strings. Its actual
    # recorded DTO supplies the historical SUCCESS shape; no database is forged.
    source_call = _capture_source_call(provider_name, preflight, content, finish, require_nonempty=False)
    assert source_call["status"] is CallStatus.SUCCESS
    session = _RecordedSession(source_call)
    recorder = FakeAuditRecorder()
    telemetry = FakeTelemetryEmit()
    provider = _pipeline_provider(
        provider_name,
        recorder,
        telemetry,
        call_mode_session=session,
        limiter=_NoLiveLimiter(),
        llm_call_governance=LLMCallGovernance(
            before_call=lambda: pytest.fail("replay requested a chargeable attempt"),
            after_call=lambda *_args: pytest.fail("replay settled a chargeable attempt"),
        ),
    )
    monkeypatch.setattr(provider, "_get_underlying_client", lambda: pytest.fail("replay constructed an SDK"))
    try:
        if content.strip():
            result = _invoke_provider(provider, preflight)
            if not preflight:
                assert result.content == content
        else:
            expected = ContentPolicyError if finish == "content_filter" else LLMClientError
            with pytest.raises(expected) as error:
                _invoke_provider(provider, preflight)
            assert type(error.value) is expected
            assert not error.value.retryable
        assert len(session.requests) == len(recorder.operation_calls) == len(telemetry.events) == 1
        copied = recorder.operation_calls[0]
        assert copied["status"] is CallStatus.SUCCESS
        assert copied["source_call_id"] == "retained-source-call"
        assert copied["response_data"].to_dict() == source_call["response_data"].to_dict()
        assert copied["token_usage"] == source_call["token_usage"]
        assert copied["error"] is None
        assert telemetry.events[0].status is CallStatus.SUCCESS
        assert source_call["status"] is CallStatus.SUCCESS
        assert provider._llm_clients == {}
    finally:
        provider.close()


@pytest.mark.parametrize("provider_name", ["azure", "bedrock"])
@pytest.mark.parametrize("preflight", [False, True])
def test_response_processing_error_replay_remains_refused(provider_name: str, preflight: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    source_call = _capture_source_call(provider_name, preflight, " ", "length", require_nonempty=True)
    assert source_call["status"] is CallStatus.ERROR
    session = _RecordedSession(source_call)
    recorder = FakeAuditRecorder()
    telemetry = FakeTelemetryEmit()
    provider = _pipeline_provider(provider_name, recorder, telemetry, call_mode_session=session, limiter=_NoLiveLimiter())
    monkeypatch.setattr(provider, "_get_underlying_client", lambda: pytest.fail("replay constructed an SDK"))
    try:
        with pytest.raises(ValueError, match="response-processing error cannot be reproduced exactly"):
            _invoke_provider(provider, preflight)
        assert len(session.requests) == 1
        assert recorder.operation_calls == telemetry.events == []
        assert provider._llm_clients == {}
    finally:
        provider.close()
