"""Tests for the LiteLLM-backed AWS Bedrock pipeline provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from litellm.exceptions import (
    ContextWindowExceededError,
    ServiceUnavailableError,
)
from litellm.exceptions import (
    RateLimitError as LiteLLMRateLimitError,
)
from litellm.exceptions import (
    Timeout as LiteLLMTimeout,
)
from litellm.llms.bedrock.base_aws_llm import BaseAWSLLM
from litellm.types.utils import ModelResponse, Usage

from elspeth.contracts import CallType
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.plugins.infrastructure.clients.llm import (
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMAuditParent, LLMProvider
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockConfig, BedrockCredentials, BedrockLLMProvider


def test_replay_client_does_not_construct_bedrock_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = BedrockLLMProvider(
        region_name="ap-southeast-2",
        credentials=BedrockCredentials(),
        recorder=FakeAuditRecorder(),
        run_id="run-1",
        telemetry_emit=FakeTelemetryEmit(),
        call_mode_session=SimpleNamespace(mode=RunMode.REPLAY),
    )
    monkeypatch.setattr(provider, "_get_underlying_client", lambda: pytest.fail("Bedrock SDK constructed during replay"))

    client = provider._get_llm_client(LLMAuditParent.for_operation(operation_id="op-1", coordination_token=_LEADER_TOKEN))

    assert client._client is None


@pytest.mark.parametrize("source_problem", ["missing", "ambiguous"])
@pytest.mark.parametrize("preflight", [False, True])
def test_verify_source_request_refusal_precedes_bedrock_sdk_construction(
    source_problem: str,
    preflight: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class VerifySession:
        mode = RunMode.VERIFY

        def preflight_verify_request(self, **kwargs: Any) -> str:
            assert kwargs["call_type"] is CallType.LLM
            assert kwargs["current_operation_id"] == "op-1"
            assert kwargs["request_data"]["provider"] == "bedrock"
            raise AuditIntegrityError(f"Source request is {source_problem}")

    provider = BedrockLLMProvider(
        region_name="ap-southeast-2",
        credentials=BedrockCredentials(),
        recorder=FakeAuditRecorder(),
        run_id="run-1",
        telemetry_emit=FakeTelemetryEmit(),
        call_mode_session=VerifySession(),
    )
    monkeypatch.setattr(provider, "_get_underlying_client", lambda: pytest.fail("Bedrock SDK constructed before source admission"))

    with pytest.raises(AuditIntegrityError, match=source_problem):
        if preflight:
            provider.runtime_preflight(operation_id="op-1", model=MODEL, coordination_token=_LEADER_TOKEN)
        else:
            provider.execute_query(
                [ChatMessage(role="user", content="hello")],
                model=MODEL,
                temperature=0.0,
                max_tokens=32,
                audit_parent=LLMAuditParent.for_operation(operation_id="op-1", coordination_token=_LEADER_TOKEN),
            )


MODEL = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
DEFAULT_CHAIN = BedrockCredentials()
# Distinctive sentinels: the audit-leak proofs search recorded calls for them.
API_KEY = "bedrock-api-key-sentinel"  # secret-scan: allow-this-line
ACCESS_KEY_ID = "access-key-id-sentinel"  # secret-scan: allow-this-line
SECRET_ACCESS_KEY = "secret-access-key-sentinel"  # secret-scan: allow-this-line
SESSION_TOKEN = "session-token-sentinel"  # secret-scan: allow-this-line
DYNAMIC_SCHEMA = {"mode": "observed"}


# Mock-only authority: these providers use FakeAuditRecorder, never a database.
_LEADER_TOKEN = CoordinationToken(run_id="run-1", worker_id="leader-1", leader_epoch=1)
_MEMBER_TOKEN = _LEADER_TOKEN.membership
_WORK_ITEM = Mock(spec=TokenWorkItem)


@dataclass
class FakeAuditRecorder:
    allocated_state_ids: list[str | None] = field(default_factory=list)
    allocated_operation_ids: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    operation_calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str | None, *, member_token: WorkerMembershipToken, work_item: TokenWorkItem) -> int:
        self.allocated_state_ids.append(state_id)
        return len(self.allocated_state_ids) - 1

    def allocate_operation_call_index(self, operation_id: str, *, coordination_token: CoordinationToken) -> int:
        self.allocated_operation_ids.append(operation_id)
        return len(self.allocated_operation_ids) - 1

    def record_call(self, **call: Any) -> SimpleNamespace:
        self.calls.append(call)
        return SimpleNamespace(request_ref=f"request-{len(self.calls)}", response_ref=f"response-{len(self.calls)}")

    def record_operation_call(self, **call: Any) -> SimpleNamespace:
        self.operation_calls.append(call)
        return SimpleNamespace(
            call_id=f"operation-call-{len(self.operation_calls)}",
            request_ref=f"operation-request-{len(self.operation_calls)}",
            response_ref=f"operation-response-{len(self.operation_calls)}",
        )


@pytest.mark.parametrize("preflight", [False, True])
@pytest.mark.parametrize("refused", [False, True])
def test_governance_guards_bedrock_and_settles_once(preflight: bool, refused: bool) -> None:
    recorder = FakeAuditRecorder()
    events: list[str] = []

    def before() -> str:
        events.append("before")
        if refused:
            raise RuntimeError("quota refused")
        return "attempt-bedrock"

    def after(attempt_id: str, call_id: str) -> None:
        assert attempt_id == "attempt-bedrock"
        assert call_id == "operation-call-1"
        assert len(recorder.operation_calls) == 1
        events.append("after")

    provider = BedrockLLMProvider(
        region_name=None,
        credentials=DEFAULT_CHAIN,
        recorder=recorder,
        run_id="run-1",
        telemetry_emit=FakeTelemetryEmit(),
        llm_call_governance=LLMCallGovernance(before_call=before, after_call=after),
    )
    with patch("litellm.completion", return_value=_response()) as completion:
        if refused:
            with pytest.raises(RuntimeError, match="quota refused"):
                if preflight:
                    provider.runtime_preflight(operation_id="op-1", model=MODEL, coordination_token=_LEADER_TOKEN)
                else:
                    _execute_for_operation(provider)
        elif preflight:
            provider.runtime_preflight(operation_id="op-1", model=MODEL, coordination_token=_LEADER_TOKEN)
        else:
            _execute_for_operation(provider)
        assert completion.call_count == (0 if refused else 1)
    assert events == (["before"] if refused else ["before", "after"])


@dataclass
class FakeTelemetryEmit:
    events: list[Any] = field(default_factory=list)

    def __call__(self, event: Any) -> None:
        self.events.append(event)


def _config(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "provider": "bedrock",
        "model": MODEL,
        "prompt_template": "Classify: {{ row.text }}",
        "schema": DYNAMIC_SCHEMA,
        "required_input_fields": ["text"],
    }
    raw.update(overrides)
    return raw


def _provider(
    *,
    region_name: str | None = None,
    credentials: BedrockCredentials = DEFAULT_CHAIN,
    recorder: FakeAuditRecorder | None = None,
    telemetry: FakeTelemetryEmit | None = None,
) -> BedrockLLMProvider:
    return BedrockLLMProvider(
        region_name=region_name,
        credentials=credentials,
        recorder=recorder if recorder is not None else FakeAuditRecorder(),
        run_id="run-1",
        telemetry_emit=telemetry if telemetry is not None else FakeTelemetryEmit(),
    )


def _response(*, content: str = "Hello", finish_reason: str = "stop") -> ModelResponse:
    return ModelResponse(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        model=MODEL,
        usage=Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


def _execute(provider: BedrockLLMProvider) -> Any:
    return provider.execute_query(
        messages=[ChatMessage(role="user", content="hi")],
        model=MODEL,
        temperature=0.25,
        max_tokens=64,
        audit_parent=LLMAuditParent.for_row(
            member_token=_MEMBER_TOKEN,
            work_item=_WORK_ITEM,
            state_id="state-1",
            token_id="token-1",
        ),
    )


def _execute_for_operation(provider: BedrockLLMProvider) -> Any:
    return provider.execute_query(
        messages=[ChatMessage(role="user", content="hi")],
        model=MODEL,
        temperature=0.25,
        max_tokens=64,
        audit_parent=LLMAuditParent.for_operation(coordination_token=_LEADER_TOKEN, operation_id="operation-1"),
    )


class TestBedrockConfig:
    def test_valid_config_uses_default_credential_chain(self) -> None:
        config = BedrockConfig.from_dict(_config())

        assert config.provider == "bedrock"
        assert config.model == MODEL
        assert config.region_name is None
        # Credentials are optional: none configured means the default chain.
        assert config.credentials() == DEFAULT_CHAIN
        assert config.credentials().litellm_kwargs() == {}

    def test_api_key_is_accepted_as_the_only_credential(self) -> None:
        config = BedrockConfig.from_dict(_config(api_key=API_KEY))

        assert config.credentials().litellm_kwargs() == {"api_key": API_KEY}

    @pytest.mark.parametrize("session_token", [None, SESSION_TOKEN])
    def test_static_credential_pair_is_accepted_with_optional_session_token(self, session_token: str | None) -> None:
        overrides: dict[str, object] = {"aws_access_key_id": ACCESS_KEY_ID, "aws_secret_access_key": SECRET_ACCESS_KEY}
        expected = {"aws_access_key_id": ACCESS_KEY_ID, "aws_secret_access_key": SECRET_ACCESS_KEY, "aws_session_token": ""}
        if session_token is not None:
            overrides["aws_session_token"] = session_token
            expected["aws_session_token"] = session_token

        assert BedrockConfig.from_dict(_config(**overrides)).credentials().litellm_kwargs() == expected

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"api_key": API_KEY, "aws_access_key_id": ACCESS_KEY_ID, "aws_secret_access_key": SECRET_ACCESS_KEY}, "mutually exclusive"),
            ({"aws_access_key_id": ACCESS_KEY_ID}, "required together"),
            ({"aws_secret_access_key": SECRET_ACCESS_KEY}, "required together"),
            ({"aws_session_token": SESSION_TOKEN}, "aws_session_token requires"),
            ({"api_key": ""}, "api_key"),
        ],
    )
    def test_inconsistent_credentials_are_rejected(self, overrides: dict[str, object], message: str) -> None:
        with pytest.raises(PluginConfigError, match=message):
            BedrockConfig.from_dict(_config(**overrides))

    def test_credential_values_never_appear_in_reprs_or_validation_errors(self) -> None:
        config = BedrockConfig.from_dict(
            _config(aws_access_key_id=ACCESS_KEY_ID, aws_secret_access_key=SECRET_ACCESS_KEY, aws_session_token=SESSION_TOKEN)
        )
        rendered = repr(config) + repr(config.credentials()) + repr(BedrockCredentials(api_key=API_KEY))
        # Positive control: the repr is a real rendering, not an empty string.
        assert MODEL in rendered
        for sentinel in (API_KEY, ACCESS_KEY_ID, SECRET_ACCESS_KEY, SESSION_TOKEN):
            assert sentinel not in rendered

        with pytest.raises(PluginConfigError) as excinfo:
            BedrockConfig.from_dict(_config(api_key=API_KEY, aws_access_key_id=ACCESS_KEY_ID, aws_secret_access_key=SECRET_ACCESS_KEY))
        for sentinel in (API_KEY, ACCESS_KEY_ID, SECRET_ACCESS_KEY):
            assert sentinel not in str(excinfo.value)

    def test_explicit_region_is_accepted(self) -> None:
        assert BedrockConfig.from_dict(_config(region_name="us-gov-west-1")).region_name == "us-gov-west-1"

    @pytest.mark.parametrize(
        "model",
        [
            "anthropic.claude-v2",
            "bedrock/",
            " bedrock/model",
            "bedrock/model ",
            "bedrock/model\n",
            "bedrock/\x7fmodel",
        ],
    )
    def test_invalid_model_identifier_is_rejected(self, model: str) -> None:
        with pytest.raises(PluginConfigError, match="model"):
            BedrockConfig.from_dict(_config(model=model))

    def test_model_bound_accepts_512_characters(self) -> None:
        model = "bedrock/" + "x" * (512 - len("bedrock/"))

        assert BedrockConfig.from_dict(_config(model=model)).model == model

    def test_model_bound_rejects_513_characters(self) -> None:
        model = "bedrock/" + "x" * (513 - len("bedrock/"))

        with pytest.raises(PluginConfigError, match="model"):
            BedrockConfig.from_dict(_config(model=model))

    @pytest.mark.parametrize("region_name", ["", "US-EAST-1", "us_east_1", "x" * 65])
    def test_invalid_region_is_rejected(self, region_name: str) -> None:
        with pytest.raises(PluginConfigError, match="region_name"):
            BedrockConfig.from_dict(_config(region_name=region_name))

    @pytest.mark.parametrize(
        "field_name",
        [
            "profile",
            "profile_name",
            "role",
            "role_arn",
            "endpoint",
            "endpoint_url",
        ],
    )
    def test_ambient_identity_and_endpoint_fields_are_rejected(self, field_name: str) -> None:
        with pytest.raises(PluginConfigError, match=field_name):
            BedrockConfig.from_dict(_config(**{field_name: "forbidden"}))


class TestBedrockAdapter:
    @pytest.mark.parametrize("ambient_token", [None, "ambient-session-sentinel"])
    @pytest.mark.parametrize("credential_mode", ["long-lived", "temporary", "default-chain"])
    def test_litellm_resolves_and_signs_with_one_credential_identity(
        self, monkeypatch: pytest.MonkeyPatch, ambient_token: str | None, credential_mode: str
    ) -> None:
        resolver = BaseAWSLLM()
        for name in resolver.aws_authentication_params:
            monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-access-sentinel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret-sentinel")
        if ambient_token is not None:
            monkeypatch.setenv("AWS_SESSION_TOKEN", ambient_token)

        if credential_mode == "default-chain":
            credentials = DEFAULT_CHAIN
            expected_access = "ambient-access-sentinel"
            expected_secret = "ambient-secret-sentinel"
            expected_token = ambient_token
        else:
            expected_access = ACCESS_KEY_ID
            expected_secret = SECRET_ACCESS_KEY
            expected_token = SESSION_TOKEN if credential_mode == "temporary" else None
            credentials = BedrockCredentials(
                aws_access_key_id=ACCESS_KEY_ID,
                aws_secret_access_key=SECRET_ACCESS_KEY,
                aws_session_token=expected_token,
            )

        # Exercise the real SDK resolver and signer, without a network request.
        resolved = resolver.get_credentials(aws_region_name="us-east-1", **credentials.litellm_kwargs())
        assert resolved.access_key == expected_access
        assert resolved.secret_key == expected_secret
        assert (resolved.token or None) == expected_token
        request = AWSRequest(method="POST", url="https://bedrock-runtime.us-east-1.amazonaws.com/")
        SigV4Auth(resolved, "bedrock", "us-east-1").add_auth(request)
        assert f"Credential={expected_access}/" in request.headers["Authorization"]
        assert request.headers.get("X-Amz-Security-Token") == expected_token

    @pytest.mark.parametrize("region_name", [None, "ap-southeast-2"])
    def test_forwards_only_normal_request_fields_and_optional_region(self, region_name: str | None) -> None:
        provider = _provider(region_name=region_name)
        response = _response()

        with patch("litellm.completion", return_value=response) as completion:
            result = _execute(provider)

        kwargs = completion.call_args.kwargs
        assert kwargs["model"] == MODEL
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["temperature"] == 0.25
        assert kwargs["max_tokens"] == 64
        if region_name is None:
            assert "aws_region_name" not in kwargs
        else:
            assert kwargs["aws_region_name"] == region_name
        for forbidden in (
            "api_key",
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
            "aws_profile_name",
            "aws_role_name",
            "endpoint_url",
        ):
            assert forbidden not in kwargs
        assert result.content == "Hello"

    def test_operation_parent_constructs_audited_client_and_records_operation_call(self) -> None:
        recorder = FakeAuditRecorder()
        provider = _provider(recorder=recorder)

        with patch("litellm.completion", return_value=_response()):
            result = _execute_for_operation(provider)

        assert result.content == "Hello"
        assert recorder.calls == []
        assert recorder.allocated_state_ids == []
        assert recorder.allocated_operation_ids == ["operation-1"]
        assert [call["operation_id"] for call in recorder.operation_calls] == ["operation-1"]
        assert provider._llm_clients == {}

    def test_region_does_not_override_an_explicit_call_kwarg(self) -> None:
        from elspeth.plugins.transforms.llm.providers.bedrock import _LiteLLMSDKAdapter

        adapter = _LiteLLMSDKAdapter(region_name="ap-southeast-2", credentials=DEFAULT_CHAIN)
        with patch("litellm.completion", return_value=_response()) as completion:
            adapter.create(model=MODEL, messages=[], aws_region_name="us-east-1")

        assert completion.call_args.kwargs["aws_region_name"] == "us-east-1"

    @pytest.mark.parametrize(
        "credentials",
        [
            BedrockCredentials(api_key=API_KEY),
            BedrockCredentials(aws_access_key_id=ACCESS_KEY_ID, aws_secret_access_key=SECRET_ACCESS_KEY),
            BedrockCredentials(aws_access_key_id=ACCESS_KEY_ID, aws_secret_access_key=SECRET_ACCESS_KEY, aws_session_token=SESSION_TOKEN),
        ],
        ids=["api-key", "static-pair", "static-pair-with-session"],
    )
    def test_configured_credentials_reach_litellm_but_never_the_audit_record(self, credentials: BedrockCredentials) -> None:
        recorder = FakeAuditRecorder()
        provider = _provider(credentials=credentials, recorder=recorder)

        with patch("litellm.completion", return_value=_response()) as completion:
            _execute(provider)

        kwargs = completion.call_args.kwargs
        expected = credentials.litellm_kwargs()
        assert expected  # positive control: this case really carries a credential
        assert {name: kwargs[name] for name in expected} == expected
        for name in ("api_key", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
            assert (name in kwargs) == (name in expected)

        # The credential is injected below the audited client, so the recorded
        # call must not carry it. Positive control: the model IS recorded.
        assert len(recorder.calls) == 1
        recorded = repr(recorder.calls)
        assert MODEL in recorded
        for secret_value in expected.values():
            if secret_value:
                assert secret_value not in recorded

    def test_configured_credentials_override_a_caller_supplied_identity(self) -> None:
        from elspeth.plugins.transforms.llm.providers.bedrock import _LiteLLMSDKAdapter

        adapter = _LiteLLMSDKAdapter(region_name=None, credentials=BedrockCredentials(api_key=API_KEY))
        with patch("litellm.completion", return_value=_response()) as completion:
            adapter.create(model=MODEL, messages=[], api_key="caller-supplied")

        assert completion.call_args.kwargs["api_key"] == API_KEY


class TestBedrockProvider:
    def test_call_preserves_approved_prompt_artifact(self) -> None:
        recorder = FakeAuditRecorder()
        provider = BedrockLLMProvider(
            region_name=None,
            credentials=DEFAULT_CHAIN,
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=FakeTelemetryEmit(),
            approved_prompt_artifact_hash="b" * 64,
        )
        with patch("litellm.completion", return_value=_response()):
            _execute(provider)
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["approved_prompt_artifact_hash"] == "b" * 64

    def test_satisfies_llm_provider_protocol(self) -> None:
        assert isinstance(_provider(), LLMProvider)

    def test_execute_query_returns_normalized_result(self) -> None:
        with patch("litellm.completion", return_value=_response()):
            result = _execute(_provider())

        assert result.content == "Hello"
        assert result.model == MODEL
        assert result.usage.prompt_tokens == 11
        assert result.usage.completion_tokens == 7
        assert result.finish_reason is FinishReason.STOP

    def test_execute_query_content_filter_raises_content_policy_error(self) -> None:
        with (
            patch("litellm.completion", return_value=_response(content="", finish_reason="content_filter")),
            pytest.raises(ContentPolicyError, match="empty content"),
        ):
            _execute(_provider())

    def test_empty_content_error_does_not_expose_unknown_finish_reason(self) -> None:
        sentinel = "provider-private-finish-reason"
        provider = _provider()
        response = SimpleNamespace(
            content="",
            raw_response={"choices": [{"finish_reason": sentinel}]},
            usage=Usage(prompt_tokens=11, completion_tokens=0, total_tokens=11),
            model=MODEL,
        )
        client = SimpleNamespace(chat_completion=lambda **_kwargs: response)

        with patch.object(provider, "_get_llm_client", return_value=client), pytest.raises(ContentPolicyError) as exc_info:
            _execute(provider)

        assert str(exc_info.value) == "Bedrock LLM returned empty content (finish_reason=unrecognized)"
        assert sentinel not in str(exc_info.value)
        assert sentinel not in repr(exc_info.value)

    @pytest.mark.parametrize(
        ("provider_error", "expected_type", "retryable"),
        [
            (
                LiteLLMRateLimitError(
                    message="BedrockException: Rate Limit Error - ThrottlingException: Rate exceeded",
                    llm_provider="bedrock",
                    model=MODEL,
                ),
                RateLimitError,
                True,
            ),
            (
                ContextWindowExceededError(
                    message="BedrockException: Context Window Error - Input is too long for requested model.",
                    model=MODEL,
                    llm_provider="bedrock",
                ),
                ContextLengthError,
                False,
            ),
            (
                ServiceUnavailableError(
                    message="BedrockException - Internal server error",
                    llm_provider="bedrock",
                    model=MODEL,
                    response=httpx.Response(
                        status_code=500,
                        request=httpx.Request("POST", "https://example.invalid/"),
                    ),
                ),
                ServerError,
                True,
            ),
            (
                LiteLLMTimeout(
                    message="BedrockException: Timeout Error - Connect timeout on endpoint URL",
                    model=MODEL,
                    llm_provider="bedrock",
                ),
                NetworkError,
                True,
            ),
        ],
    )
    def test_execute_query_preserves_typed_category_but_redacts_provider_detail(
        self,
        provider_error: Exception,
        expected_type: type[LLMClientError],
        retryable: bool,
    ) -> None:
        recorder = FakeAuditRecorder()
        provider = _provider(recorder=recorder)

        with patch("litellm.completion", side_effect=provider_error), pytest.raises(expected_type) as exc_info:
            _execute(provider)

        escaping = exc_info.value
        assert str(escaping) == "Bedrock LLM request failed"
        assert repr(escaping) == f"{expected_type.__name__}('Bedrock LLM request failed')"
        assert escaping.retryable is retryable
        assert escaping.__cause__ is None
        assert escaping.__context__ is None
        assert recorder.calls[0]["error"].message == "LLM provider request failed"
        assert str(provider_error) not in recorder.calls[0]["error"].message
        assert str(provider_error) not in str(escaping)

    def test_runtime_preflight_redacts_raw_error_and_preserves_retryability(self) -> None:
        sentinel = "arn:aws:bedrock:ap-southeast-2:123456789012:raw-sentinel-request-id"
        recorder = FakeAuditRecorder()
        provider = _provider(recorder=recorder)
        provider_error = LiteLLMRateLimitError(
            message=f"BedrockException: Rate Limit Error - {sentinel}",
            llm_provider="bedrock",
            model=MODEL,
        )

        with patch("litellm.completion", side_effect=provider_error), pytest.raises(RateLimitError) as exc_info:
            provider.runtime_preflight(coordination_token=_LEADER_TOKEN, operation_id="operation-1", model=MODEL)

        escaping = exc_info.value
        assert str(escaping) == "Bedrock LLM request failed"
        assert sentinel not in str(escaping)
        assert sentinel not in repr(escaping)
        assert escaping.retryable is True
        assert escaping.__cause__ is None
        assert escaping.__context__ is None
        assert recorder.operation_calls[0]["error"].message == "LLM provider request failed"
        assert sentinel not in recorder.operation_calls[0]["error"].message

    def test_close_closes_underlying_adapter_once(self) -> None:
        provider = _provider()
        adapter = provider._get_underlying_client()
        close = Mock(wraps=adapter.close)
        adapter.close = close

        provider.close()
        provider.close()

        close.assert_called_once_with()
