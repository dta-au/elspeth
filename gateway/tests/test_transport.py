import json

import httpx
import pytest
import respx
from elspeth_llm_gateway.core.config import load_config
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.core.oauth import TokenManager
from elspeth_llm_gateway.core.transport import UpstreamClient, UpstreamResult
from elspeth_llm_gateway.sdk.protocol import InvokePlan

TOKEN_URL = "https://auth.example.com/token"
UPSTREAM_ORIGIN = "https://upstream.example.com"
UPSTREAM_URL = f"{UPSTREAM_ORIGIN}/v1/chat"

BASE_ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": "b" * 40,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "example_adapter",
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


def _plan(**overrides) -> InvokePlan:
    kwargs = {"path": "v1/chat", "headers": {}, "body": {"model": "gpt-4o"}}
    kwargs.update(overrides)
    return InvokePlan(**kwargs)


def _token_response(token: str) -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "token_type": "bearer", "expires_in": 300})


def _mock_tokens(*tokens: str):
    return respx.post(TOKEN_URL).mock(side_effect=[_token_response(t) for t in tokens])


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http_client:
        yield http_client


def _transport(config, client_) -> UpstreamClient:
    token_manager = TokenManager(config, client_)
    return UpstreamClient(config, token_manager, client_)


class _BoundedRaisingStream(httpx.AsyncByteStream):
    """Yields bytes in small pieces; raises if more than ``limit`` bytes are ever pulled.

    Stands in for an oversized upstream body: if ``UpstreamClient`` ever fully
    buffers the response (e.g. via ``.aread()``/``.content``) rather than
    reading a bounded number of streamed bytes, this raises instead of
    silently succeeding -- turning a memory-safety regression into a test
    failure rather than a passing-by-accident test.
    """

    def __init__(self, *, total_size: int, limit: int, piece: int = 64) -> None:
        self._total_size = total_size
        self._limit = limit
        self._piece = piece

    async def __aiter__(self):
        produced = 0
        while produced < self._total_size:
            if produced >= self._limit:
                raise AssertionError(f"stream consumed past cap: {produced} >= {self._limit}")
            chunk_len = min(self._piece, self._total_size - produced)
            produced += chunk_len
            yield b"a" * chunk_len

    async def aclose(self) -> None:
        return None


# --- origin-pinned URL join / send-time header re-validation -------------------


@respx.mock
@pytest.mark.parametrize("forbidden_name", ["authorization", "Authorization", "host", "cookie", "x-forwarded-for"])
async def test_mutated_forbidden_header_raises_internal_error_with_zero_http_calls(client, forbidden_name):
    """``InvokePlan.headers`` is a mutable dict even though the model is frozen;
    a caller mutating it after construction (e.g. an adapter bug, or a
    concurrently-shared plan) must be caught at send time, before any network
    call -- not just at ``InvokePlan`` construction. No route is registered at
    all: if the implementation ever reached the network here, respx itself
    would raise (unmocked request) rather than this test silently passing."""
    config = _config()
    transport = _transport(config, client)
    plan = _plan()
    plan.headers[forbidden_name] = "smuggled-value"

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(plan)

    assert exc_info.value.code == GatewayErrorCode.INTERNAL_ERROR


@respx.mock
async def test_mutated_oversized_header_value_raises_internal_error_with_zero_http_calls(client):
    config = _config()
    transport = _transport(config, client)
    plan = _plan(headers={"x-custom": "a"})
    plan.headers["x-custom"] = "a" * 1025

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(plan)

    assert exc_info.value.code == GatewayErrorCode.INTERNAL_ERROR


# --- 401 replay semantics --------------------------------------------------------


@respx.mock
async def test_401_then_200_replays_once_with_fresh_token(client):
    token_route = _mock_tokens("tok-1", "tok-2")
    upstream_route = respx.post(UPSTREAM_URL).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"result": "ok"}),
        ]
    )
    config = _config()
    transport = _transport(config, client)

    result = await transport.invoke(_plan(headers={"x-passthrough": "v"}))

    assert result == UpstreamResult(status=200, body={"result": "ok"})
    assert upstream_route.call_count == 2
    assert token_route.call_count == 2

    first_request = upstream_route.calls[0].request
    second_request = upstream_route.calls[1].request
    assert first_request.headers["Authorization"] == "Bearer tok-1"
    assert second_request.headers["Authorization"] == "Bearer tok-2"
    assert first_request.headers["Authorization"] != second_request.headers["Authorization"]
    assert first_request.headers["Content-Type"] == "application/json"
    assert first_request.headers["x-passthrough"] == "v"


@respx.mock
async def test_adapter_content_type_header_is_overridden_not_duplicated(client):
    """InvokePlan lower-cases header names at construction, so an
    adapter-supplied ``content-type`` would otherwise collide, case-only,
    with the ``Content-Type`` the transport adds -- httpx would then send
    both as separate wire headers instead of one replacing the other."""
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={}))
    config = _config()
    transport = _transport(config, client)

    await transport.invoke(_plan(headers={"content-type": "application/vnd.custom+json"}))

    sent_headers = upstream_route.calls[0].request.headers
    assert sent_headers.get_list("content-type") == ["application/json"]


@respx.mock
async def test_401_then_401_raises_upstream_unauthorized_exactly_two_calls(client):
    _mock_tokens("tok-1", "tok-2")
    upstream_route = respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(401))
    config = _config()
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_UNAUTHORIZED
    assert upstream_route.call_count == 2


# --- statuses the transport owns outright (no adapter, no replay) ---------------


@respx.mock
async def test_429_raises_upstream_rate_limited_single_call_no_replay(client):
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(429))
    config = _config()
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_RATE_LIMITED
    assert upstream_route.call_count == 1


@respx.mock(assert_all_called=False)
async def test_302_redirect_raises_upstream_response_invalid_and_is_never_followed(client, respx_mock):
    respx_mock.post(TOKEN_URL).mock(return_value=_token_response("tok-1"))
    upstream_route = respx_mock.post(UPSTREAM_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://evil.example.com/steal"})
    )
    target = respx_mock.get("https://evil.example.com/steal").mock(return_value=httpx.Response(200))
    config = _config()
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_RESPONSE_INVALID
    assert upstream_route.call_count == 1
    assert target.call_count == 0


@respx.mock
async def test_timeout_raises_upstream_timeout_single_call_no_replay(client):
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    config = _config()
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_TIMEOUT
    assert upstream_route.call_count == 1


@respx.mock
async def test_connect_error_raises_upstream_unavailable_single_call_no_replay(client):
    """``httpx.TimeoutException`` is a subclass of ``httpx.TransportError`` --
    a non-timeout transport error (here, connection refused) must still map
    to ``upstream_unavailable``, proving timeout is checked first without
    swallowing every other transport error into the timeout bucket."""
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    config = _config()
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_UNAVAILABLE
    assert upstream_route.call_count == 1


# --- pass-through statuses: adapter classifies later, transport never raises ----


@respx.mock
async def test_500_returns_upstream_result_without_exception_or_replay(client):
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    config = _config()
    transport = _transport(config, client)

    result = await transport.invoke(_plan())

    assert result == UpstreamResult(status=500, body={"error": "boom"})
    assert upstream_route.call_count == 1


@respx.mock
async def test_400_returns_upstream_result_without_exception_or_replay(client):
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(400, json={"error": "bad_request"}))
    config = _config()
    transport = _transport(config, client)

    result = await transport.invoke(_plan())

    assert result == UpstreamResult(status=400, body={"error": "bad_request"})
    assert upstream_route.call_count == 1


@respx.mock
async def test_non_2xx_body_that_fails_parse_returns_none_body(client):
    _mock_tokens("tok-1")
    upstream_route = respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(500, text="not json at all"))
    config = _config()
    transport = _transport(config, client)

    result = await transport.invoke(_plan())

    assert result == UpstreamResult(status=500, body=None)
    assert upstream_route.call_count == 1


# --- 2xx body parsing / streaming size cap --------------------------------------


@respx.mock
async def test_2xx_body_that_fails_strict_parse_raises_upstream_response_invalid(client):
    _mock_tokens("tok-1")
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, text="not json at all"))
    config = _config()
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_RESPONSE_INVALID


@respx.mock
async def test_oversized_2xx_body_raises_response_invalid_without_full_buffering(client):
    """Response body exceeding max_response_bytes (here 2x the cap) must never
    be fully buffered: the custom stream raises if more bytes than a small
    overshoot past the cap are ever pulled, which only happens if the
    implementation reads via an unbounded ``.aread()``/``.content`` call
    instead of a capped streaming read."""
    _mock_tokens("tok-1")
    cap = 1024
    config = _config(ELSPETH_LLM_GATEWAY_MAX_RESPONSE_BYTES=str(cap))
    stream = _BoundedRaisingStream(total_size=cap * 2, limit=cap + 512, piece=64)
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, stream=stream))
    transport = _transport(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await transport.invoke(_plan())

    assert exc_info.value.code == GatewayErrorCode.UPSTREAM_RESPONSE_INVALID


@respx.mock
async def test_body_exactly_at_cap_still_parses_successfully(client):
    """Boundary check against an off-by-one in the streaming cap: a 2xx body
    whose exact byte size equals ``max_response_bytes`` must still parse.

    ``max_response_bytes`` also bounds the OAuth token response (see
    ``core/oauth.py``), so the cap here is padded well above the token
    JSON's own size -- otherwise shrinking it to fit the upstream payload
    exactly would starve the token fetch itself.
    """
    _mock_tokens("tok-1")
    target_size = 256
    payload = {"result": "x", "pad": ""}
    base_length = len(json.dumps(payload).encode("utf-8"))
    payload["pad"] = "p" * (target_size - base_length)
    encoded = json.dumps(payload).encode("utf-8")
    cap = len(encoded)
    assert cap == target_size

    config = _config(ELSPETH_LLM_GATEWAY_MAX_RESPONSE_BYTES=str(cap))
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, content=encoded, headers={"Content-Type": "application/json"}))
    transport = _transport(config, client)

    result = await transport.invoke(_plan())

    assert result == UpstreamResult(status=200, body=payload)
