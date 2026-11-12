"""Regression tests for the 20261111-plugins-review P3 LLM-provider sweep findings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from elspeth.contracts.chat_parts import ChatMessage
from elspeth.plugins.infrastructure.clients.llm import LLMClientError, NetworkError
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers.gateway import _validate_gateway_success_response, _validate_readyz_payload
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterLLMProvider
from tests.unit.plugins.llm.test_provider_bedrock import _LEADER_TOKEN as _BEDROCK_LEADER_TOKEN
from tests.unit.plugins.llm.test_provider_bedrock import MODEL as _BEDROCK_MODEL
from tests.unit.plugins.llm.test_provider_bedrock import _provider as _bedrock_provider
from tests.unit.plugins.llm.test_provider_bedrock import _response as _bedrock_response
from tests.unit.plugins.llm.test_provider_gateway import _CONTRACT_HEADER, _completion_body, _gateway_response, _readyz_body
from tests.unit.plugins.llm.test_provider_openrouter import _MEMBER_TOKEN as _OPENROUTER_MEMBER_TOKEN
from tests.unit.plugins.llm.test_provider_openrouter import _WORK_ITEM as _OPENROUTER_WORK_ITEM
from tests.unit.plugins.llm.test_provider_openrouter import FakeAuditRecorder as OpenRouterFakeAuditRecorder
from tests.unit.plugins.llm.test_provider_openrouter import FakeHTTPClient, _provider_http_client
from tests.unit.plugins.llm.test_provider_openrouter import FakeTelemetryEmit as OpenRouterFakeTelemetryEmit


def _raised_by(call: Callable[[], object]) -> BaseException | None:
    """Return what ``call`` raised (or None) so every outcome lands on an assertion."""
    try:
        call()
    except Exception as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# Finding #62: a required 'usage' capability must fail closed on malformed usage
# ---------------------------------------------------------------------------


def _completion_with_usage(usage: object) -> dict[str, Any]:
    body = _completion_body()
    body["usage"] = usage
    return body


class TestGatewayRequiredUsageFailsClosed:
    @pytest.mark.parametrize(
        "usage",
        [
            pytest.param({}, id="empty-object"),
            pytest.param("junk", id="string"),
            pytest.param({"prompt_tokens": "10", "completion_tokens": "5"}, id="string-counts"),
            pytest.param(5, id="int"),
            pytest.param({"prompt_tokens": 10}, id="completion-missing"),
            pytest.param({"completion_tokens": 5}, id="prompt-missing"),
            pytest.param({"prompt_tokens": True, "completion_tokens": 5}, id="bool-count"),
        ],
    )
    def test_malformed_usage_is_rejected_when_required(self, usage: object) -> None:
        response = _gateway_response(_completion_with_usage(usage))
        raised = _raised_by(lambda: _validate_gateway_success_response(response, usage_required=True))
        assert isinstance(raised, LLMClientError), f"malformed usage {usage!r} was accepted: {raised!r}"
        assert raised.retryable is False
        assert "usage" in str(raised)

    def test_well_formed_usage_is_accepted_when_required(self) -> None:
        response = _gateway_response(_completion_with_usage({"prompt_tokens": 10, "completion_tokens": 5}))
        _, _, usage, _, _ = _validate_gateway_success_response(response, usage_required=True)
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 5

    def test_malformed_usage_stays_unknown_when_not_required(self) -> None:
        response = _gateway_response(_completion_with_usage("junk"))
        _, _, usage, _, _ = _validate_gateway_success_response(response, usage_required=False)
        assert usage.prompt_tokens is None
        assert usage.completion_tokens is None


# ---------------------------------------------------------------------------
# Finding #63: readyz capabilities elements and contract_major are typed
# ---------------------------------------------------------------------------


def _readyz_response(**overrides: object) -> httpx.Response:
    body: dict[str, Any] = _readyz_body()
    body.update(overrides)
    return httpx.Response(200, json=body, headers={_CONTRACT_HEADER: "1"})


def _validate_readyz(response: httpx.Response, *, required_capabilities: tuple[str, ...] = ("usage",)) -> object:
    return _validate_readyz_payload(response, contract_major=1, model="standard", required_capabilities=required_capabilities)


class TestReadyzPayloadTyping:
    @pytest.mark.parametrize(
        "capabilities",
        [
            pytest.param([{"x": 1}], id="unhashable-dict"),
            pytest.param([["usage"]], id="unhashable-list"),
            pytest.param(["usage", 5], id="int-element"),
            pytest.param(["usage", None], id="null-element"),
        ],
    )
    def test_non_str_capability_element_raises_typed_error(self, capabilities: list[object]) -> None:
        raised = _raised_by(lambda: _validate_readyz(_readyz_response(capabilities=capabilities)))
        assert isinstance(raised, LLMClientError), f"capabilities {capabilities!r} produced {raised!r}"
        assert raised.retryable is False

    @pytest.mark.parametrize(
        "contract_major",
        [
            pytest.param(True, id="bool"),
            pytest.param(1.0, id="float"),
            pytest.param("1", id="str"),
        ],
    )
    def test_non_int_contract_major_is_rejected(self, contract_major: object) -> None:
        raised = _raised_by(lambda: _validate_readyz(_readyz_response(contract_major=contract_major)))
        assert isinstance(raised, LLMClientError), f"contract_major {contract_major!r} produced {raised!r}"
        assert raised.retryable is False

    def test_well_formed_readyz_document_is_accepted(self) -> None:
        identity = _validate_readyz(_readyz_response())
        assert identity == {"name": "reference_v1_invoke", "version": "1.0.0", "adapter_api_major": 1, "fingerprint": "abc123"}


# ---------------------------------------------------------------------------
# Finding #97: OpenRouter transport errors never carry the raw httpx text
# ---------------------------------------------------------------------------

_TRANSPORT_SENTINEL = "SENTINEL-transport-text-7c1e"


class TestOpenRouterTransportErrorRedaction:
    @pytest.mark.parametrize(
        "transport_error",
        [
            pytest.param(httpx.RemoteProtocolError(f"illegal header line: b'X-Leak: {_TRANSPORT_SENTINEL}'"), id="remote-protocol"),
            pytest.param(httpx.ConnectError(f"connect to https://{_TRANSPORT_SENTINEL}.example failed"), id="connect"),
        ],
    )
    def test_network_error_message_and_audit_row_use_fixed_text(self, transport_error: httpx.RequestError) -> None:
        recorder = OpenRouterFakeAuditRecorder()
        provider = OpenRouterLLMProvider(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            timeout_seconds=30.0,
            recorder=recorder,
            run_id="run-1",
            telemetry_emit=OpenRouterFakeTelemetryEmit(),
        )
        http_client = FakeHTTPClient(error=transport_error)
        with _provider_http_client(provider, http_client):
            raised = _raised_by(
                lambda: provider.execute_query(
                    messages=[ChatMessage(role="user", content="hi")],
                    model="gpt-4o",
                    temperature=0.0,
                    max_tokens=100,
                    audit_parent=LLMAuditParent.for_row(
                        member_token=_OPENROUTER_MEMBER_TOKEN,
                        work_item=_OPENROUTER_WORK_ITEM,
                        state_id="state-1",
                        token_id="tok-1",
                    ),
                )
            )

        assert isinstance(raised, NetworkError), f"expected NetworkError, got {raised!r}"
        assert raised.retryable is True
        assert _TRANSPORT_SENTINEL not in str(raised)
        assert _TRANSPORT_SENTINEL not in repr(raised)
        assert raised.__cause__ is transport_error
        assert recorder.calls, "the logical LLM error row was not recorded"
        assert _TRANSPORT_SENTINEL not in repr(recorder.calls)


# ---------------------------------------------------------------------------
# Finding #125: Bedrock readiness requires a usable completion
# ---------------------------------------------------------------------------


class TestBedrockPreflightRequiresUsableCompletion:
    @pytest.mark.parametrize(
        ("content", "finish_reason"),
        [
            pytest.param("", "stop", id="empty-stop"),
            pytest.param("   ", "stop", id="whitespace-stop"),
            pytest.param("ok", "length", id="length"),
            pytest.param("ok", "content_filter", id="content-filter"),
            pytest.param("ok", "tool_calls", id="tool-calls"),
        ],
    )
    def test_unusable_preflight_completion_is_not_readiness(self, content: str, finish_reason: str) -> None:
        provider = _bedrock_provider()
        with patch("litellm.completion", return_value=_bedrock_response(content=content, finish_reason=finish_reason)):
            raised = _raised_by(
                lambda: provider.runtime_preflight(
                    operation_id="op-1", model=_BEDROCK_MODEL, temperature=0.0, coordination_token=_BEDROCK_LEADER_TOKEN
                )
            )
        assert isinstance(raised, LLMClientError), f"preflight accepted content={content!r} finish_reason={finish_reason!r}"
        assert raised.retryable is False
        assert content.strip() == "" or content not in str(raised)

    def test_usable_preflight_completion_is_readiness(self) -> None:
        provider = _bedrock_provider()
        with patch("litellm.completion", return_value=_bedrock_response(content="ok", finish_reason="stop")):
            provider.runtime_preflight(operation_id="op-1", model=_BEDROCK_MODEL, temperature=0.0, coordination_token=_BEDROCK_LEADER_TOKEN)
