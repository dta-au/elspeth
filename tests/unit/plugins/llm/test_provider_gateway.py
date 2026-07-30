# tests/unit/plugins/llm/test_provider_gateway.py
"""Tests for GatewayLLMProvider — the ELSPETH pipeline provider that talks to
the Phase 1 compatibility gateway (contract major 1).

Uses respx to intercept real HTTP traffic through the genuine
``AuditedHTTPClient`` the provider constructs internally (unlike the
OpenRouter provider tests, which monkeypatch ``_get_http_client`` with a
fake). This exercises the real transport-row recording path so the
two-audit-row assertion is meaningful.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from elspeth.contracts import CallStatus, CallType
from elspeth.plugins.infrastructure.clients.llm import (
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider

_ENDPOINT = "https://gateway.example.com/v1"
_READYZ_ROOT = "https://gateway.example.com"
_CONTRACT_HEADER = "X-ELSPETH-LLM-Gateway-Contract"
_BODY_SENTINEL = "SENTINEL-do-not-leak-92f1a3"


@dataclass
class FakeAuditRecorder:
    call_indexes: list[int] = field(default_factory=list)
    allocated_state_ids: list[str | None] = field(default_factory=list)
    allocated_operation_ids: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    operation_calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str | None) -> int:
        self.allocated_state_ids.append(state_id)
        return len(self.allocated_state_ids) - 1

    def allocate_operation_call_index(self, operation_id: str) -> int:
        self.allocated_operation_ids.append(operation_id)
        return len(self.allocated_operation_ids) - 1

    def record_call(self, **call: Any) -> SimpleNamespace:
        self.calls.append(call)
        return SimpleNamespace(request_ref=f"request-{len(self.calls)}", response_ref=f"response-{len(self.calls)}")

    def record_operation_call(self, **call: Any) -> SimpleNamespace:
        self.operation_calls.append(call)
        return SimpleNamespace(
            request_ref=f"operation-request-{len(self.operation_calls)}",
            response_ref=f"operation-response-{len(self.operation_calls)}",
        )


@dataclass
class FakeTelemetryEmit:
    events: list[Any] = field(default_factory=list)

    def __call__(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture()
def audit_recorder() -> FakeAuditRecorder:
    return FakeAuditRecorder()


@pytest.fixture()
def telemetry_emit() -> FakeTelemetryEmit:
    return FakeTelemetryEmit()


@pytest.fixture()
def provider(audit_recorder: FakeAuditRecorder, telemetry_emit: FakeTelemetryEmit) -> GatewayLLMProvider:
    return GatewayLLMProvider(
        endpoint=_ENDPOINT,
        api_key="test-bearer-token",
        contract_major=1,
        required_capabilities=(),
        timeout_seconds=30.0,
        recorder=audit_recorder,
        run_id="run-1",
        telemetry_emit=telemetry_emit,
    )


def _completion_body(
    *,
    content: str = "Hello",
    model: str = "standard",
    finish_reason: str | None = "stop",
    usage: dict[str, int] | None = None,
    response_format_echo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "gwcmpl-req-1",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    if response_format_echo is not None:
        body["_response_format_echo"] = response_format_echo
    return body


def _gateway_response(
    body: dict[str, Any],
    *,
    status_code: int = 200,
    contract_header: str | None = "1",
) -> httpx.Response:
    headers = {"content-type": "application/json"}
    if contract_header is not None:
        headers[_CONTRACT_HEADER] = contract_header
    return httpx.Response(status_code=status_code, json=body, headers=headers)


def _error_body(code: str, *, message: str = "sanitized", retryable: bool = False, sentinel: bool = False) -> dict[str, Any]:
    error: dict[str, Any] = {
        "message": message if not sentinel else _BODY_SENTINEL,
        "type": "gateway_error",
        "code": code,
        "retryable": retryable,
        "request_id": "req-1",
    }
    return {"error": error}


def _readyz_body(
    *,
    ready: bool = True,
    contract_major: int = 1,
    model_aliases: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ready": ready,
        "contract_major": contract_major,
        "adapter": {"name": "reference_v1_invoke", "version": "1.0.0", "adapter_api_major": 1, "fingerprint": "abc123"},
        "capabilities": capabilities if capabilities is not None else ["text", "usage"],
        "model_aliases": model_aliases if model_aliases is not None else ["standard"],
        "mapping_generation": "gen-1",
        "oauth_fixed_lifetime": False,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestExecuteQueryHappyPath:
    @respx.mock
    def test_parses_success_response(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(
                _completion_body(content="Hi there", model="standard", usage={"prompt_tokens": 10, "completion_tokens": 5})
            )
        )
        result = provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=100,
            state_id="state-1",
            token_id="tok-1",
        )

        assert isinstance(result, LLMQueryResult)
        assert result.content == "Hi there"
        assert result.model == "standard"
        assert result.finish_reason is FinishReason.STOP
        assert result.usage.prompt_tokens == 10
        assert result.usage.completion_tokens == 5

    @respx.mock
    def test_sends_contract_header_and_bearer(self, provider: GatewayLLMProvider) -> None:
        route = respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body()))
        provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=100,
            state_id="state-1",
            token_id="tok-1",
        )
        sent = route.calls.last.request
        assert sent.headers["authorization"] == "Bearer test-bearer-token"
        assert sent.headers[_CONTRACT_HEADER] == "1"

    @respx.mock
    def test_max_tokens_none_omitted_from_body(self, provider: GatewayLLMProvider) -> None:
        route = respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body()))
        provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=None,
            state_id="state-1",
            token_id="tok-1",
        )
        sent_body = json.loads(route.calls.last.request.content)
        assert "max_tokens" not in sent_body

    @respx.mock
    @pytest.mark.parametrize(
        "response_format",
        [
            {"type": "json_object"},
            {"type": "json_schema", "json_schema": {"name": "Answer", "schema": {"type": "object"}}},
        ],
    )
    def test_response_format_forwarded_unchanged(self, provider: GatewayLLMProvider, response_format: dict[str, Any]) -> None:
        route = respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body()))
        provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=100,
            state_id="state-1",
            token_id="tok-1",
            response_format=response_format,
        )
        sent_body = json.loads(route.calls.last.request.content)
        assert sent_body["response_format"] == response_format

    @respx.mock
    def test_exactly_one_http_call_per_execute_query(self, provider: GatewayLLMProvider) -> None:
        """ELSPETH owns all retry policy — the provider must contain no retry loop."""
        route = respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body()))
        provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=100,
            state_id="state-1",
            token_id="tok-1",
        )
        assert route.call_count == 1


# ---------------------------------------------------------------------------
# Contract header verification
# ---------------------------------------------------------------------------


class TestContractHeader:
    @respx.mock
    def test_missing_contract_header_rejected_non_retryably(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(), contract_header=None))
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False

    @respx.mock
    def test_mismatched_contract_header_rejected_non_retryably(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(), contract_header="2"))
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False


# ---------------------------------------------------------------------------
# Error-code mapping — every gateway code, mapped by code ONLY
# ---------------------------------------------------------------------------


class TestErrorCodeMapping:
    @respx.mock
    @pytest.mark.parametrize(
        ("code", "expected_exc", "expected_retryable"),
        [
            ("invalid_request", LLMClientError, False),
            ("contract_mismatch", LLMClientError, False),
            ("model_not_allowed", LLMClientError, False),
            ("capability_unsupported", LLMClientError, False),
            ("context_length_exceeded", ContextLengthError, False),
            ("content_policy_rejected", ContentPolicyError, False),
            ("upstream_rate_limited", RateLimitError, True),
            ("upstream_timeout", NetworkError, True),
            ("upstream_unavailable", ServerError, True),
            ("oauth_token_unavailable", ServerError, True),
            ("upstream_unauthorized", LLMClientError, False),
            ("upstream_response_invalid", LLMClientError, False),
            ("internal_error", LLMClientError, False),
        ],
    )
    def test_maps_each_gateway_code(
        self,
        provider: GatewayLLMProvider,
        code: str,
        expected_exc: type[LLMClientError],
        expected_retryable: bool,
    ) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_error_body(code), status_code=400))
        with pytest.raises(expected_exc) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is expected_retryable

    @respx.mock
    def test_maps_on_code_only_ignoring_http_status(self, provider: GatewayLLMProvider) -> None:
        """A rate-limit code delivered with an unrelated HTTP status must
        still classify as RateLimitError — ELSPETH maps on code, not status."""
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(_error_body("upstream_rate_limited"), status_code=500)
        )
        with pytest.raises(RateLimitError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )

    @respx.mock
    def test_unknown_code_is_non_retryable(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(_error_body("some_future_code_we_dont_know"), status_code=400)
        )
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False

    @respx.mock
    def test_missing_code_is_non_retryable(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response({"error": {"message": "oops"}}, status_code=400))
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False

    @respx.mock
    def test_malformed_error_body_is_non_retryable(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=httpx.Response(
                status_code=500,
                content=b"not json at all",
                headers={"content-type": "text/plain", _CONTRACT_HEADER: "1"},
            )
        )
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False

    @respx.mock
    def test_error_body_sentinel_never_leaks_into_exception_message(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(_error_body("internal_error", sentinel=True), status_code=500)
        )
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert _BODY_SENTINEL not in str(exc_info.value)

    @respx.mock
    def test_sentinel_in_code_field_never_leaks(self, provider: GatewayLLMProvider) -> None:
        """Even if a malicious/buggy gateway places the sentinel in the
        ``code`` field itself, it must never appear in the raised message —
        it simply falls through as an unrecognized code."""
        body = {"error": {"message": "x", "type": "gateway_error", "code": _BODY_SENTINEL, "retryable": False, "request_id": "r"}}
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(body, status_code=400))
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert _BODY_SENTINEL not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Transport-level failures
# ---------------------------------------------------------------------------


class TestTransportErrors:
    @respx.mock
    def test_connect_error_raises_network_error(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NetworkError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )

    @respx.mock
    def test_timeout_raises_network_error(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(side_effect=httpx.ReadTimeout("timed out"))
        with pytest.raises(NetworkError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )


# ---------------------------------------------------------------------------
# Usage handling — required vs not, never zeros
# ---------------------------------------------------------------------------


class TestUsageHandling:
    @respx.mock
    def test_usage_required_but_absent_is_non_retryable(self, audit_recorder: FakeAuditRecorder, telemetry_emit: FakeTelemetryEmit) -> None:
        provider = GatewayLLMProvider(
            endpoint=_ENDPOINT,
            api_key="test-bearer-token",
            contract_major=1,
            required_capabilities=("usage",),
            recorder=audit_recorder,
            run_id="run-1",
            telemetry_emit=telemetry_emit,
        )
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(usage=None)))
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False

    @respx.mock
    def test_usage_absent_and_not_required_is_unknown_never_zeros(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(usage=None)))
        result = provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=100,
            state_id="state-1",
            token_id="tok-1",
        )
        assert result.usage.prompt_tokens is None
        assert result.usage.completion_tokens is None
        assert result.usage.reported_total is None


# ---------------------------------------------------------------------------
# Blank content
# ---------------------------------------------------------------------------


class TestBlankContent:
    @respx.mock
    def test_blank_content_with_tool_calls_finish_reason_is_non_retryable(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(_completion_body(content="", finish_reason="tool_calls"))
        )
        with pytest.raises(LLMClientError) as exc_info:
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert exc_info.value.retryable is False

    @respx.mock
    def test_blank_content_otherwise_is_content_policy_error(self, provider: GatewayLLMProvider) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(_completion_body(content="   ", finish_reason="stop"))
        )
        with pytest.raises(ContentPolicyError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )


# ---------------------------------------------------------------------------
# model field validation
# ---------------------------------------------------------------------------


class TestModelValidation:
    @respx.mock
    def test_non_string_model_rejected(self, provider: GatewayLLMProvider) -> None:
        body = _completion_body()
        body["model"] = 42
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(body))
        with pytest.raises(LLMClientError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )

    @respx.mock
    def test_empty_model_rejected(self, provider: GatewayLLMProvider) -> None:
        body = _completion_body()
        body["model"] = ""
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(body))
        with pytest.raises(LLMClientError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )


# ---------------------------------------------------------------------------
# Two audit rows: transport (HTTP) + semantic (LLM)
# ---------------------------------------------------------------------------


class TestAuditRows:
    @respx.mock
    def test_success_records_two_rows(self, provider: GatewayLLMProvider, audit_recorder: FakeAuditRecorder) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body()))
        provider.execute_query(
            messages=[{"role": "user", "content": "hi"}],
            model="standard",
            temperature=0.0,
            max_tokens=100,
            state_id="state-1",
            token_id="tok-1",
        )
        assert len(audit_recorder.calls) == 2
        call_types = [call["call_type"] for call in audit_recorder.calls]
        assert CallType.HTTP in call_types
        assert CallType.LLM in call_types

        llm_call = next(call for call in audit_recorder.calls if call["call_type"] == CallType.LLM)
        assert llm_call["status"] == CallStatus.SUCCESS

    def test_semantic_row_carries_resolved_prompt_template_hash(
        self, audit_recorder: FakeAuditRecorder, telemetry_emit: FakeTelemetryEmit
    ) -> None:
        provider = GatewayLLMProvider(
            endpoint=_ENDPOINT,
            api_key="test-bearer-token",
            contract_major=1,
            recorder=audit_recorder,
            run_id="run-1",
            telemetry_emit=telemetry_emit,
            resolved_prompt_template_hash="sha256:abc123",
        )
        with respx.mock:
            respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body()))
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        llm_call = next(call for call in audit_recorder.calls if call["call_type"] == CallType.LLM)
        assert llm_call["resolved_prompt_template_hash"] == "sha256:abc123"

    @respx.mock
    def test_error_records_two_rows(self, provider: GatewayLLMProvider, audit_recorder: FakeAuditRecorder) -> None:
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_error_body("internal_error"), status_code=500))
        with pytest.raises(LLMClientError):
            provider.execute_query(
                messages=[{"role": "user", "content": "hi"}],
                model="standard",
                temperature=0.0,
                max_tokens=100,
                state_id="state-1",
                token_id="tok-1",
            )
        assert len(audit_recorder.calls) == 2
        llm_call = next(call for call in audit_recorder.calls if call["call_type"] == CallType.LLM)
        assert llm_call["status"] == CallStatus.ERROR


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_clears_clients(self, provider: GatewayLLMProvider) -> None:
        provider._get_http_client("state-1", token_id="tok-1")
        assert len(provider._http_clients) == 1
        provider.close()
        assert len(provider._http_clients) == 0

    def test_satisfies_llm_provider_protocol(self, provider: GatewayLLMProvider) -> None:
        assert isinstance(provider, LLMProvider)


# ---------------------------------------------------------------------------
# runtime_preflight — both halves required
# ---------------------------------------------------------------------------


class TestRuntimePreflight:
    @respx.mock
    def test_preflight_succeeds_when_ready_and_completion_works(self, provider: GatewayLLMProvider) -> None:
        respx.get(f"{_READYZ_ROOT}/readyz").mock(return_value=httpx.Response(200, json=_readyz_body(), headers={_CONTRACT_HEADER: "1"}))
        respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(content="ok")))
        provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_fails_when_readyz_reports_not_ready(self, provider: GatewayLLMProvider) -> None:
        respx.get(f"{_READYZ_ROOT}/readyz").mock(
            return_value=httpx.Response(503, json=_readyz_body(ready=False), headers={_CONTRACT_HEADER: "1"})
        )
        with pytest.raises(LLMClientError):
            provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_fails_when_contract_major_mismatched(self, provider: GatewayLLMProvider) -> None:
        respx.get(f"{_READYZ_ROOT}/readyz").mock(
            return_value=httpx.Response(200, json=_readyz_body(contract_major=2), headers={_CONTRACT_HEADER: "1"})
        )
        with pytest.raises(LLMClientError):
            provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_fails_when_model_alias_absent(self, provider: GatewayLLMProvider) -> None:
        respx.get(f"{_READYZ_ROOT}/readyz").mock(
            return_value=httpx.Response(200, json=_readyz_body(model_aliases=["other-model"]), headers={_CONTRACT_HEADER: "1"})
        )
        with pytest.raises(LLMClientError):
            provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_fails_when_required_capability_missing(
        self, audit_recorder: FakeAuditRecorder, telemetry_emit: FakeTelemetryEmit
    ) -> None:
        provider = GatewayLLMProvider(
            endpoint=_ENDPOINT,
            api_key="test-bearer-token",
            contract_major=1,
            required_capabilities=("json_schema",),
            recorder=audit_recorder,
            run_id="run-1",
            telemetry_emit=telemetry_emit,
        )
        respx.get(f"{_READYZ_ROOT}/readyz").mock(
            return_value=httpx.Response(200, json=_readyz_body(capabilities=["text", "usage"]), headers={_CONTRACT_HEADER: "1"})
        )
        with pytest.raises(LLMClientError):
            provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_fails_when_readyz_ok_but_completion_fails(self, provider: GatewayLLMProvider) -> None:
        """A readiness document alone is NOT accepted as health — the
        completion call must also succeed."""
        respx.get(f"{_READYZ_ROOT}/readyz").mock(return_value=httpx.Response(200, json=_readyz_body(), headers={_CONTRACT_HEADER: "1"}))
        respx.post(f"{_ENDPOINT}/chat/completions").mock(
            return_value=_gateway_response(_error_body("upstream_unavailable"), status_code=503)
        )
        with pytest.raises(ServerError):
            provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_readyz_transport_error_raises_network_error(self, provider: GatewayLLMProvider) -> None:
        respx.get(f"{_READYZ_ROOT}/readyz").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(NetworkError):
            provider.runtime_preflight(operation_id="op-1", model="standard")

    @respx.mock
    def test_preflight_smoke_test_request_shape(self, provider: GatewayLLMProvider) -> None:
        respx.get(f"{_READYZ_ROOT}/readyz").mock(return_value=httpx.Response(200, json=_readyz_body(), headers={_CONTRACT_HEADER: "1"}))
        route = respx.post(f"{_ENDPOINT}/chat/completions").mock(return_value=_gateway_response(_completion_body(content="ok")))
        provider.runtime_preflight(operation_id="op-1", model="standard")
        sent_body = json.loads(route.calls.last.request.content)
        assert sent_body["model"] == "standard"
        assert sent_body["max_tokens"] == 32
