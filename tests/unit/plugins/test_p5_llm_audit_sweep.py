"""Regression tests for the p5 LLM-audit sweep.

#123 (maintainer ruling 2026-11-14, option a): an empty or whitespace-only
completion is recorded on the LLM call row with its real classification
(content filter / output-token budget / unexplained), never as SUCCESS, for
every provider that records through ``AuditedLLMClient`` (Azure, Bedrock) and
for the HTTP providers (OpenRouter, Gateway). Replay follows the same rule.

#128 + systems sweep F2: no credential-bearing plugin config field shows its
value in ``repr()``/``str()``, and a config validation failure does not carry
the authored input (and so a secret prefix) in its chained ``__cause__``.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import re
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel, SecretStr

import elspeth.plugins
import elspeth.plugins.infrastructure.clients.llm as llm_module
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.enums import RunMode
from elspeth.plugins.infrastructure.clients.llm import AuditedLLMClient, ContentPolicyError, LLMClientError
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchProviderConfig
from elspeth.plugins.infrastructure.config_base import PluginConfig
from elspeth.plugins.sinks.azure_blob_sink import AzureBlobSinkConfig
from elspeth.plugins.sources.azure_blob_source import AzureBlobSourceConfig
from elspeth.plugins.sources.llm.config import (
    AzureOpenAILLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
    OpenRouterLLMSourceConfig,
)
from elspeth.plugins.transforms.azure.ai_search import AzureAISearchConfig
from elspeth.plugins.transforms.azure.content_safety import AzureContentSafetyConfig
from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShieldConfig
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers.azure import AzureOpenAIConfig
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockConfig
from elspeth.plugins.transforms.llm.providers.gateway import GatewayConfig, _validate_gateway_success_response
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig, _validate_chat_completion_response
from elspeth.plugins.transforms.llm.tracing import AzureAITracingConfig, LangfuseTracingConfig
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.llm.test_provider_azure import _LEADER_TOKEN as _AZURE_LEADER_TOKEN
from tests.unit.plugins.llm.test_provider_azure import FakeAuditRecorder as AzureFakeAuditRecorder
from tests.unit.plugins.llm.test_provider_azure import _make_provider as _make_azure_provider
from tests.unit.plugins.llm.test_provider_bedrock import _LEADER_TOKEN as _BEDROCK_LEADER_TOKEN
from tests.unit.plugins.llm.test_provider_bedrock import MODEL as _BEDROCK_MODEL
from tests.unit.plugins.llm.test_provider_bedrock import FakeAuditRecorder as BedrockFakeAuditRecorder
from tests.unit.plugins.llm.test_provider_bedrock import _provider as _bedrock_provider
from tests.unit.plugins.llm.test_provider_bedrock import _response as _bedrock_response
from tests.unit.plugins.llm.test_provider_gateway import _completion_body, _gateway_response

_BUDGET_TEXT = "output token budget"


def _raised_by(call: Callable[[], object]) -> BaseException | None:
    """Return what ``call`` raised (or None) so every outcome lands on an assertion."""
    try:
        call()
    except Exception as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# #123: AuditedLLMClient records the classification, not SUCCESS
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    content: object


@dataclass
class _Choice:
    message: object
    finish_reason: str | None


@dataclass
class _Response:
    content: object
    finish_reason: str | None
    refusal: str | None = None
    model: str = "gpt-4o"
    usage: object = None

    @property
    def choices(self) -> list[_Choice]:
        return [_Choice(message=_Message(self.content), finish_reason=self.finish_reason)]

    def model_dump(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.refusal is not None:
            message["refusal"] = self.refusal
        return {"model": self.model, "choices": [{"index": 0, "message": message, "finish_reason": self.finish_reason}]}


@dataclass
class _Recorder:
    calls: list[dict[str, Any]] = field(default_factory=list)
    next_index: int = 0

    def allocate_call_index(self, _state_id: str, **_kwargs: Any) -> int:
        index = self.next_index
        self.next_index += 1
        return index

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(**{**kwargs, "call_id": f"call-{len(self.calls)}"})


def _sdk(response: object) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response)))


def _live_client(response: object, recorder: _Recorder, events: list[Any]) -> AuditedLLMClient:
    return AuditedLLMClient(
        **mock_audit_authority(),
        execution=recorder,
        state_id="state-p5",
        run_id="run-p5",
        telemetry_emit=events.append,
        underlying_client=_sdk(response),
    )


def _complete(client: AuditedLLMClient) -> BaseException | None:
    return _raised_by(lambda: client.chat_completion(model="gpt-4o", messages=[ChatMessage(role="user", content="hi")]))


def _error_dict(call: Mapping[str, Any]) -> dict[str, Any]:
    error = call["error"]
    assert error is not None, f"call recorded without an error payload: status={call['status']!r}"
    return error.to_dict()


class TestEmptyCompletionIsRecordedWithItsClassification:
    @pytest.mark.parametrize(
        ("content", "finish_reason", "refusal", "category", "text"),
        [
            pytest.param("", "content_filter", None, "content_policy", "filtered", id="empty-content-filter"),
            pytest.param("   ", "content_filter", None, "content_policy", "filtered", id="whitespace-content-filter"),
            pytest.param(None, "content_filter", None, "content_policy", "filtered", id="null-content-filter"),
            pytest.param("", "stop", "I can't help with that.", "content_policy", "filtered", id="empty-refusal"),
            pytest.param("", "length", None, "client", _BUDGET_TEXT, id="empty-length"),
            pytest.param(" \n", "length", None, "client", _BUDGET_TEXT, id="whitespace-length"),
            pytest.param(None, "length", None, "client", _BUDGET_TEXT, id="null-length"),
            pytest.param("", "stop", None, "client", "without an explicit refusal", id="empty-stop"),
            pytest.param("\t", None, None, "client", "without an explicit refusal", id="whitespace-no-finish-reason"),
            pytest.param(None, "stop", None, "client", "without an explicit refusal", id="null-stop"),
        ],
    )
    def test_call_row_status_and_error_match_the_raised_classification(
        self, content: str | None, finish_reason: str | None, refusal: str | None, category: str, text: str
    ) -> None:
        recorder = _Recorder()
        events: list[Any] = []
        raised = _complete(_live_client(_Response(content, finish_reason, refusal), recorder, events))

        assert isinstance(raised, LLMClientError), f"empty completion returned normally (raised={raised!r})"
        assert raised.category == category
        assert raised.retryable is False
        assert text in str(raised)
        assert isinstance(raised, ContentPolicyError) is (category == "content_policy")

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["status"] is CallStatus.ERROR, "an empty completion must never be recorded as SUCCESS"
        error = _error_dict(call)
        assert error["category"] == category
        assert error["type"] == type(raised).__name__
        assert error["message"] == str(raised)
        assert error["retryable"] is False
        # The provider's response is evidence: it is kept on the ERROR row.
        assert call["response_data"] is not None
        assert call["response_data"].to_dict()["content"].strip() == ""
        assert [event.status for event in events] == [CallStatus.ERROR]

    @pytest.mark.parametrize("content", ["", "  ", None])
    def test_tool_calls_with_empty_content_is_an_unsupported_response(self, content: str | None) -> None:
        recorder = _Recorder()
        raised = _complete(_live_client(_Response(content, "tool_calls"), recorder, []))

        assert type(raised) is LLMClientError
        assert "tool_calls" in str(raised)
        assert recorder.calls[0]["status"] is CallStatus.ERROR
        assert _error_dict(recorder.calls[0])["category"] == "response_processing"

    def test_non_empty_completion_is_still_success(self) -> None:
        recorder = _Recorder()
        client = _live_client(_Response("an answer", "stop"), recorder, [])
        response = client.chat_completion(model="gpt-4o", messages=[ChatMessage(role="user", content="hi")])

        assert response.content == "an answer"
        assert recorder.calls[0]["status"] is CallStatus.SUCCESS

    def test_shared_empty_completion_errors_are_owned_types(self) -> None:
        assert "EmptyCompletionError" in vars(llm_module)
        assert "FilteredCompletionError" in vars(llm_module)
        assert "classify_empty_completion" in vars(llm_module)
        empty_error = vars(llm_module)["EmptyCompletionError"]
        filtered_error = vars(llm_module)["FilteredCompletionError"]
        assert issubclass(empty_error, LLMClientError)
        assert issubclass(filtered_error, ContentPolicyError)


# ---------------------------------------------------------------------------
# #123: replay follows the live rule
# ---------------------------------------------------------------------------


def _replay_client(evidence: SimpleNamespace, recorder: _Recorder, events: list[Any]) -> AuditedLLMClient:
    class _ReplaySession:
        mode = RunMode.REPLAY

        def replay_call(self, **kwargs: Any) -> SimpleNamespace:
            assert kwargs["call_type"] is CallType.LLM
            return evidence

    return AuditedLLMClient(
        **mock_audit_authority(),
        execution=recorder,
        state_id="state-p5",
        run_id="run-p5-replay",
        telemetry_emit=events.append,
        underlying_client=_sdk(AssertionError("replay must not dispatch")),
        call_mode_session=_ReplaySession(),
    )


def _recorded_response(content: str, finish_reason: str) -> dict[str, Any]:
    return {
        "content": content,
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 9, "completion_tokens": 0},
        "raw_response": {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]},
        "pricing_model": "gpt-4o",
        "provider_cost": None,
        "provider_cost_source": "not_available",
    }


def _live_empty_record(content: str | None, finish_reason: str) -> tuple[BaseException | None, dict[str, Any]]:
    recorder = _Recorder()
    raised = _complete(_live_client(_Response(content, finish_reason), recorder, []))
    call = recorder.calls[0]
    response_data = call["response_data"]
    return raised, {
        "status": call["status"],
        "error": None if call["error"] is None else call["error"].to_dict(),
        "response": None if response_data is None else response_data.to_dict(),
    }


class TestReplayFollowsTheEmptyCompletionRule:
    @pytest.mark.parametrize(
        ("content", "finish_reason"),
        [
            pytest.param("", "content_filter", id="filtered"),
            pytest.param(None, "content_filter", id="null-filtered"),
            pytest.param("", "length", id="length"),
            pytest.param("  ", "stop", id="unexplained"),
        ],
    )
    def test_replay_reproduces_the_live_error_and_its_evidence(self, content: str | None, finish_reason: str) -> None:
        live_raised, live = _live_empty_record(content, finish_reason)
        assert live["status"] is CallStatus.ERROR, f"live run recorded {live['status']!r} for an empty completion"
        assert live["response"] is not None

        recorder = _Recorder()
        events: list[Any] = []
        evidence = SimpleNamespace(
            source_call_id="source-empty",
            status=CallStatus.ERROR,
            response_data=live["response"],
            error_data=live["error"],
            latency_ms=4.0,
        )
        replay_raised = _raised_by(
            lambda: _replay_client(evidence, recorder, events).chat_completion(
                model="gpt-4o", messages=[ChatMessage(role="user", content="hi")]
            )
        )

        assert type(replay_raised) is type(live_raised), f"replay raised {replay_raised!r}, live raised {live_raised!r}"
        assert str(replay_raised) == str(live_raised)
        assert len(recorder.calls) == 1
        replayed = recorder.calls[0]
        assert replayed["status"] is CallStatus.ERROR
        assert replayed["source_call_id"] == "source-empty"
        assert replayed["error"].to_dict() == live["error"]
        assert replayed["response_data"].to_dict() == live["response"]
        assert [event.status for event in events] == [CallStatus.ERROR]

    def test_replay_refuses_a_success_record_with_empty_content(self) -> None:
        recorder = _Recorder()
        evidence = SimpleNamespace(
            source_call_id="legacy-success",
            status=CallStatus.SUCCESS,
            response_data=_recorded_response("   ", "stop"),
            error_data=None,
            latency_ms=1.0,
        )
        raised = _raised_by(
            lambda: _replay_client(evidence, recorder, []).chat_completion(
                model="gpt-4o", messages=[ChatMessage(role="user", content="hi")]
            )
        )

        assert isinstance(raised, ValueError), f"replay accepted a SUCCESS record with empty content: {raised!r}"
        assert recorder.calls == []

    def test_replay_refuses_an_empty_completion_error_with_non_empty_content(self) -> None:
        recorder = _Recorder()
        evidence = SimpleNamespace(
            source_call_id="tampered",
            status=CallStatus.ERROR,
            response_data=_recorded_response("real text", "length"),
            error_data={
                "type": "EmptyCompletionError",
                "message": "LLM exhausted the output token budget before returning content",
                "retryable": False,
                "pricing_model": "gpt-4o",
                "provider_cost": None,
                "provider_cost_source": "not_available",
                "category": "client",
            },
            latency_ms=1.0,
        )
        raised = _raised_by(
            lambda: _replay_client(evidence, recorder, []).chat_completion(
                model="gpt-4o", messages=[ChatMessage(role="user", content="hi")]
            )
        )

        assert isinstance(raised, ValueError)
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# #123: Azure and Bedrock execute_query go through the classified record
# ---------------------------------------------------------------------------


def _azure_execute(response: _Response) -> tuple[BaseException | None, AzureFakeAuditRecorder]:
    recorder = AzureFakeAuditRecorder()
    provider = _make_azure_provider(recorder)
    provider._underlying_client = _sdk(response)
    raised = _raised_by(
        lambda: provider.execute_query(
            messages=[ChatMessage(role="user", content="hi")],
            model="gpt-4o",
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_operation(operation_id="op-1", coordination_token=_AZURE_LEADER_TOKEN),
        )
    )
    return raised, recorder


def _bedrock_execute(content: str, finish_reason: str) -> tuple[BaseException | None, BedrockFakeAuditRecorder]:
    recorder = BedrockFakeAuditRecorder()
    provider = _bedrock_provider(recorder=recorder)
    with patch("litellm.completion", return_value=_bedrock_response(content=content, finish_reason=finish_reason)):
        raised = _raised_by(
            lambda: provider.execute_query(
                messages=[ChatMessage(role="user", content="hi")],
                model=_BEDROCK_MODEL,
                temperature=0.0,
                max_tokens=64,
                audit_parent=LLMAuditParent.for_operation(operation_id="op-1", coordination_token=_BEDROCK_LEADER_TOKEN),
            )
        )
    return raised, recorder


_PROVIDER_CASES = [
    pytest.param("", "length", "client", _BUDGET_TEXT, id="length"),
    pytest.param("  ", "stop", "client", "without an explicit refusal", id="unexplained"),
    pytest.param("", "content_filter", "content_policy", "filtered", id="filtered"),
]


class TestProvidersRecordTheClassification:
    @pytest.mark.parametrize(("content", "finish_reason", "category", "text"), _PROVIDER_CASES)
    def test_azure(self, content: str, finish_reason: str, category: str, text: str) -> None:
        raised, recorder = _azure_execute(_Response(content, finish_reason))

        assert isinstance(raised, LLMClientError), f"azure accepted an empty completion: {raised!r}"
        assert raised.category == category
        assert text in str(raised)
        assert len(recorder.operation_calls) == 1
        call = recorder.operation_calls[0]
        assert call["status"] is CallStatus.ERROR
        assert _error_dict(call)["category"] == category

    @pytest.mark.parametrize(("content", "finish_reason", "category", "text"), _PROVIDER_CASES)
    def test_bedrock(self, content: str, finish_reason: str, category: str, text: str) -> None:
        raised, recorder = _bedrock_execute(content, finish_reason)

        assert isinstance(raised, LLMClientError), f"bedrock accepted an empty completion: {raised!r}"
        assert raised.category == category
        assert text in str(raised)
        assert len(recorder.operation_calls) == 1
        call = recorder.operation_calls[0]
        assert call["status"] is CallStatus.ERROR
        assert _error_dict(call)["category"] == category

    @pytest.mark.parametrize(("content", "finish_reason", "category", "text"), _PROVIDER_CASES)
    def test_gateway_uses_the_same_classification(self, content: str, finish_reason: str, category: str, text: str) -> None:
        body = _completion_body(content=content, finish_reason=finish_reason)
        raised = _raised_by(lambda: _validate_gateway_success_response(_gateway_response(body), usage_required=False))

        assert isinstance(raised, LLMClientError)
        assert raised.category == category
        assert text in str(raised)

    @pytest.mark.parametrize(("content", "finish_reason", "category", "text"), _PROVIDER_CASES)
    def test_openrouter_uses_the_same_classification(self, content: str, finish_reason: str, category: str, text: str) -> None:
        body = _completion_body(content=content, finish_reason=finish_reason)
        raised = _raised_by(lambda: _validate_chat_completion_response(_gateway_response(body, contract_header=None)))

        assert isinstance(raised, LLMClientError)
        assert raised.category == category
        assert text in str(raised)
        assert type(raised).__name__ in {"EmptyCompletionError", "FilteredCompletionError"}


# ---------------------------------------------------------------------------
# #128 / F2: credentials never appear in repr() or str()
# ---------------------------------------------------------------------------

_SENTINEL = "SENTINELSECRET-p5-7f3a91"  # secret-scan: allow-this-line

_CREDENTIAL_FIELDS: list[tuple[type[BaseModel], str]] = [
    (OpenRouterConfig, "api_key"),
    (GatewayConfig, "api_key"),
    (AzureBlobSourceConfig, "connection_string"),
    (AzureBlobSourceConfig, "sas_token"),
    (AzureBlobSourceConfig, "client_secret"),
    (AzureBlobSinkConfig, "connection_string"),
    (AzureBlobSinkConfig, "sas_token"),
    (AzureBlobSinkConfig, "client_secret"),
    (AzureContentSafetyConfig, "api_key"),
    (AzurePromptShieldConfig, "api_key"),
    (AzureAISearchConfig, "api_key"),
    (AzureSearchProviderConfig, "api_key"),
]

# Tier 2 tracing options are a free-form dict that carries a Langfuse
# secret_key or an Application Insights connection string.
_TRACING_CARRIERS: list[type[BaseModel]] = [
    AzureOpenAIConfig,
    OpenRouterConfig,
    BedrockConfig,
    GatewayConfig,
    AzureOpenAILLMSourceConfig,
    OpenRouterLLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
]


class TestCredentialsAreHiddenFromRepr:
    @pytest.mark.parametrize(("model", "name"), _CREDENTIAL_FIELDS, ids=lambda v: v.__name__ if isinstance(v, type) else v)
    def test_credential_field_value_not_in_repr_or_str(self, model: type[BaseModel], name: str) -> None:
        instance = model.model_construct(**{name: _SENTINEL})
        assert _SENTINEL not in repr(instance), f"{model.__name__}.{name} shows its value in repr()"
        assert _SENTINEL not in str(instance), f"{model.__name__}.{name} shows its value in str()"
        # Positive control: the field is really set, so the absence is the repr's doing.
        assert instance.model_dump()[name] == _SENTINEL

    @pytest.mark.parametrize("model", _TRACING_CARRIERS, ids=lambda m: m.__name__)
    def test_tracing_options_not_in_repr_or_str(self, model: type[BaseModel]) -> None:
        tracing = {"provider": "langfuse", "public_key": "pk-public", "secret_key": _SENTINEL, "host": "https://cloud.langfuse.com"}
        instance = model.model_construct(tracing=tracing)
        assert _SENTINEL not in repr(instance), f"{model.__name__}.tracing shows its secret_key in repr()"
        assert _SENTINEL not in str(instance)

    def test_validated_openrouter_config_hides_api_key(self) -> None:
        config = OpenRouterConfig.from_dict(
            {
                "model": "openai/gpt-4o-mini",
                "api_key": _SENTINEL,
                "prompt_template": "Analyze: {{ row.text }}",
                "schema": {"mode": "observed"},
                "required_input_fields": [],
            }
        )
        assert config.api_key == _SENTINEL
        assert _SENTINEL not in repr(config)
        assert _SENTINEL not in str(config)

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(
                lambda: LangfuseTracingConfig(public_key="pk", secret_key=_SENTINEL, host="https://cloud.langfuse.com"), id="langfuse"
            ),
            pytest.param(lambda: AzureAITracingConfig(connection_string=_SENTINEL), id="azure-ai"),
        ],
    )
    def test_tracing_dataclasses_hide_credentials(self, build: Callable[[], object]) -> None:
        config = build()
        assert _SENTINEL not in repr(config)
        assert _SENTINEL not in str(config)


_SECRETISH = re.compile(
    r"(api_key|apikey|secret|password|passwd|connection_string|sas_token|access_key|session_token|bearer|^token$|_token$|credential|private_key)",
    re.IGNORECASE,
)
_NOT_SECRET = re.compile(r"(_env$|_ref$|_name$|_field|max_|_fingerprint|_id$|auth_mode|_mode$|^token_)", re.IGNORECASE)


def _plugin_modules() -> list[Any]:
    modules = []
    for info in pkgutil.walk_packages(elspeth.plugins.__path__, "elspeth.plugins."):
        modules.append(importlib.import_module(info.name))
    return modules


def test_every_plugin_credential_field_hides_its_value() -> None:
    """Whole-tree census: a secret-named field is repr=False or SecretStr."""
    leaking: list[str] = []
    checked = 0
    seen: set[type] = set()
    for module in _plugin_modules():
        for value in list(vars(module).values()):
            if not isinstance(value, type) or value in seen or not value.__module__.startswith("elspeth."):
                continue
            seen.add(value)
            if issubclass(value, BaseModel):
                for name, info in value.model_fields.items():
                    if _SECRETISH.search(name) and not _NOT_SECRET.search(name):
                        checked += 1
                        if info.repr and SecretStr not in _annotation_members(info.annotation):
                            leaking.append(f"{value.__module__}.{value.__qualname__}.{name}")
            elif dataclasses.is_dataclass(value):
                for dc_field in dataclasses.fields(value):
                    if _SECRETISH.search(dc_field.name) and not _NOT_SECRET.search(dc_field.name):
                        checked += 1
                        if dc_field.repr:
                            leaking.append(f"{value.__module__}.{value.__qualname__}.{dc_field.name}")
    # Control: the walk must actually reach the known credential fields.
    assert checked >= 30, f"census found only {checked} credential fields; the walk is broken"
    assert leaking == []


def _annotation_members(annotation: object) -> set[object]:
    import typing

    members = {annotation}
    for arg in typing.get_args(annotation):
        members |= _annotation_members(arg)
    return members


def test_census_detects_a_planted_leak() -> None:
    """Negative control for the census predicate."""

    class _Planted(BaseModel):
        api_key: str = "x"

    info = _Planted.model_fields["api_key"]
    assert _SECRETISH.search("api_key") and not _NOT_SECRET.search("api_key")
    assert info.repr and SecretStr not in _annotation_members(info.annotation)


# ---------------------------------------------------------------------------
# F2: the chained ValidationError of from_dict carries no secret prefix
# ---------------------------------------------------------------------------


class TestConfigErrorsCarryNoAuthoredInput:
    def test_missing_field_error_traceback_does_not_show_a_leading_secret(self) -> None:
        options = {
            "api_key": _SENTINEL,
            "endpoint": "https://x.cognitiveservices.azure.com",
            "fields": ["a"],
            "schema": {"mode": "observed"},
        }  # "thresholds" is missing -> a model-level error whose input is the whole dict
        raised = _raised_by(lambda: AzureContentSafetyConfig.from_dict(options))

        assert raised is not None
        rendered = "".join(traceback.format_exception(raised))
        assert "thresholds" in rendered  # positive control: the error is the one we built
        assert _SENTINEL[:8] not in rendered, "a secret prefix reached the rendered traceback"

    def test_plugin_config_base_hides_input_in_errors(self) -> None:
        assert PluginConfig.model_config.get("hide_input_in_errors") is True

    def test_azure_search_provider_config_error_hides_input(self) -> None:
        raised = _raised_by(lambda: AzureSearchProviderConfig.model_validate({"api_key": _SENTINEL}))

        assert raised is not None
        assert _SENTINEL[:8] not in "".join(traceback.format_exception(raised))
