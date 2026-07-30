import logging

import httpx
import pytest
import respx
from elspeth_llm_gateway.core.config import load_config
from elspeth_llm_gateway.core.contract import ChatRequest
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.core.oauth import TokenManager
from elspeth_llm_gateway.core.service import CompletionService
from elspeth_llm_gateway.core.transport import UpstreamClient
from elspeth_llm_gateway.sdk.protocol import (
    AdapterDescriptor,
    ErrorClassification,
    InvokePlan,
    UpstreamFailure,
)
from elspeth_llm_gateway.sdk.types import CanonicalResponse, CanonicalToolCall, CanonicalUsage, Capability, FinishReason

TOKEN_URL = "https://auth.example.com/token"
UPSTREAM_ORIGIN = "https://upstream.example.com"
UPSTREAM_URL = f"{UPSTREAM_ORIGIN}/v1/invoke"

BASE_ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": "b" * 40,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "fake_adapter",
    "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": UPSTREAM_ORIGIN,
    "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": TOKEN_URL,
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": "client-id-value",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": "c" * 40,
    "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
    "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
    "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
    "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "10000",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
    "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": '{"gpt-4o": {"target": "backend-a"}}',
}


def _config(**overrides):
    env = dict(BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return load_config(env)


def _chat_request_dict(**overrides):
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi there"}]}
    body.update(overrides)
    return body


def _chat_request(**overrides) -> ChatRequest:
    return ChatRequest(**_chat_request_dict(**overrides))


class FakeAdapter:
    """A minimal, fully-controllable ``AdapterProtocol`` implementation for unit tests."""

    def __init__(
        self,
        *,
        capabilities=frozenset({Capability.TEXT}),
        build_invoke_error: Exception | None = None,
        parse_success_result: CanonicalResponse | None = None,
        parse_success_error: Exception | None = None,
        classify_result: ErrorClassification | None = None,
        classify_error_raiser: Exception | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._build_invoke_error = build_invoke_error
        self._parse_success_result = parse_success_result
        self._parse_success_error = parse_success_error
        self._classify_result = classify_result or ErrorClassification(code="upstream_response_invalid", retryable=False)
        self._classify_error_raiser = classify_error_raiser

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(name="fake_adapter", version="0.0.1", adapter_api_major=1, capabilities=self._capabilities)

    def validate_configuration(self, options: dict) -> None:
        return None

    def build_invoke(self, request):
        if self._build_invoke_error is not None:
            raise self._build_invoke_error
        return InvokePlan(path="v1/invoke", body={"echo": True})

    def parse_success(self, body: dict) -> CanonicalResponse:
        if self._parse_success_error is not None:
            raise self._parse_success_error
        assert self._parse_success_result is not None
        return self._parse_success_result

    def classify_error(self, failure: UpstreamFailure) -> ErrorClassification:
        if self._classify_error_raiser is not None:
            raise self._classify_error_raiser
        return self._classify_result


def _mock_token(token: str = "tok-1"):
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": token, "token_type": "bearer", "expires_in": 300})
    )


def _service(config, adapter, client) -> CompletionService:
    token_manager = TokenManager(config, client)
    upstream = UpstreamClient(config, token_manager, client)
    logger = logging.getLogger("test.service")
    return CompletionService(config, adapter, upstream, logger)


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http_client:
        yield http_client


# --- happy path ---------------------------------------------------------------


@respx.mock
async def test_happy_text_path_returns_openai_shape(client):
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    adapter = FakeAdapter(parse_success_result=CanonicalResponse(text="hello there", finish_reason=FinishReason.STOP))
    config = _config()
    service = _service(config, adapter, client)

    response = await service.complete(_chat_request(), "req-123")

    assert response["id"] == "gwcmpl-req-123"
    assert response["object"] == "chat.completion"
    assert response["model"] == "gpt-4o"
    assert response["choices"][0]["message"] == {"role": "assistant", "content": "hello there"}
    assert response["choices"][0]["finish_reason"] == "stop"
    assert "usage" not in response


@respx.mock
async def test_usage_present_when_adapter_reports_it(client):
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    adapter = FakeAdapter(
        parse_success_result=CanonicalResponse(
            text="hi",
            finish_reason=FinishReason.STOP,
            usage=CanonicalUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )
    )
    config = _config()
    service = _service(config, adapter, client)

    response = await service.complete(_chat_request(), "req-usage")

    assert response["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


# --- step 1: capability check, before any upstream call ------------------------


@respx.mock
async def test_tools_without_capability_raises_capability_unsupported_before_upstream_call(client):
    adapter = FakeAdapter(capabilities=frozenset({Capability.TEXT}))
    config = _config()
    service = _service(config, adapter, client)
    request = _chat_request(tools=[{"type": "function", "function": {"name": "lookup"}}])

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(request, "req-cap")

    assert exc_info.value.code == GatewayErrorCode.CAPABILITY_UNSUPPORTED
    assert exc_info.value.status == 422


@respx.mock
async def test_json_object_response_format_without_capability_raises_capability_unsupported(client):
    adapter = FakeAdapter(capabilities=frozenset({Capability.TEXT}))
    config = _config()
    service = _service(config, adapter, client)
    request = _chat_request(response_format={"type": "json_object"})

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(request, "req-cap-2")

    assert exc_info.value.code == GatewayErrorCode.CAPABILITY_UNSUPPORTED


@respx.mock
async def test_seed_without_capability_raises_capability_unsupported(client):
    adapter = FakeAdapter(capabilities=frozenset({Capability.TEXT}))
    config = _config()
    service = _service(config, adapter, client)
    request = _chat_request(seed=42)

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(request, "req-cap-3")

    assert exc_info.value.code == GatewayErrorCode.CAPABILITY_UNSUPPORTED


# --- step 2: model-alias lookup -------------------------------------------------


@respx.mock
async def test_unknown_model_alias_raises_model_not_allowed(client):
    adapter = FakeAdapter()
    config = _config()
    service = _service(config, adapter, client)
    request = _chat_request(model="not-a-configured-alias")

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(request, "req-model")

    assert exc_info.value.code == GatewayErrorCode.MODEL_NOT_ALLOWED


# --- step 3: bounds check --------------------------------------------------------


@respx.mock
async def test_bounds_violation_raises_invalid_request(client):
    config = _config(ELSPETH_LLM_GATEWAY_MAX_MESSAGES="1")
    adapter = FakeAdapter()
    service = _service(config, adapter, client)
    request = _chat_request(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
        ]
    )

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(request, "req-bounds")

    assert exc_info.value.code == GatewayErrorCode.INVALID_REQUEST


# --- step 4: adapter build_invoke exception -> internal_error -------------------


@respx.mock
async def test_build_invoke_exception_raises_internal_error(client):
    adapter = FakeAdapter(build_invoke_error=ValueError("adapter bug: malformed canonical input"))
    config = _config()
    service = _service(config, adapter, client)

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_chat_request(), "req-build-invoke")

    assert exc_info.value.code == GatewayErrorCode.INTERNAL_ERROR


# --- step 5: parse_success exception / response validation ----------------------


@respx.mock
async def test_parse_success_exception_raises_upstream_response_invalid(client):
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    adapter = FakeAdapter(parse_success_error=ValueError("adapter bug: unexpected shape"))
    config = _config()
    service = _service(config, adapter, client)

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_chat_request(), "req-parse")

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_RESPONSE_INVALID


@respx.mock
async def test_tool_call_response_without_tools_requested_raises_upstream_response_invalid(client):
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    adapter = FakeAdapter(
        capabilities=frozenset({Capability.TEXT, Capability.TOOLS}),
        parse_success_result=CanonicalResponse(
            text=None,
            tool_calls=(CanonicalToolCall(call_id="c1", name="lookup", arguments_json="{}"),),
            finish_reason=FinishReason.TOOL_CALLS,
        ),
    )
    config = _config()
    service = _service(config, adapter, client)

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_chat_request(), "req-toolcall")

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_RESPONSE_INVALID


# --- step 6: classify_error / non-2xx -------------------------------------------


@respx.mock
async def test_non_2xx_classified_error_raises_matching_gateway_error(client):
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(500, json={"fault": {"kind": "overloaded"}}))
    adapter = FakeAdapter(classify_result=ErrorClassification(code="upstream_unavailable", retryable=True))
    config = _config()
    service = _service(config, adapter, client)

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_chat_request(), "req-classify")

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_UNAVAILABLE


@respx.mock
async def test_classify_error_exception_raises_internal_error(client):
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(500, json={"fault": {"kind": "overloaded"}}))
    adapter = FakeAdapter(classify_error_raiser=ValueError("adapter bug in classify_error"))
    config = _config()
    service = _service(config, adapter, client)

    with pytest.raises(GatewayError) as exc_info:
        await service.complete(_chat_request(), "req-classify-exc")

    assert exc_info.value.code == GatewayErrorCode.INTERNAL_ERROR
