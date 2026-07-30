import asyncio
import base64
from urllib.parse import parse_qs, quote_plus

import httpx
import pytest
import respx
from elspeth_llm_gateway.core.config import load_config
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.core.oauth import TokenManager

TOKEN_URL = "https://auth.example.com/token"
CLIENT_ID = "client-id-value"
CLIENT_SECRET = "c" * 40
BODY_SENTINEL = "BODY-SENTINEL-9f3a2c"  # planted in every failure body; must never reach str(exc)

BASE_ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": "b" * 40,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "example_adapter",
    "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": "https://upstream.example.com",
    "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": TOKEN_URL,
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": CLIENT_ID,
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
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


def _form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


class FakeClock:
    """A settable stand-in for ``time.monotonic`` used to pin cache-expiry math."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http_client:
        yield http_client


def _ok_response(*, access_token="tok", token_type="bearer", expires_in=300, sentinel=False):
    body = {"access_token": access_token, "token_type": token_type, "expires_in": expires_in}
    if sentinel:
        body["sentinel"] = BODY_SENTINEL
    return httpx.Response(200, json=body)


# --- constructor guard ---------------------------------------------------------


async def test_constructor_accepts_https_token_url(client):
    config = _config()
    TokenManager(config, client)  # must not raise


async def test_constructor_accepts_loopback_http_token_url(client):
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL="http://127.0.0.1:8080/token")
    TokenManager(config, client)  # must not raise


async def test_constructor_rejects_plain_http_token_url(client):
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL="http://auth.example.com/token")
    with pytest.raises(ValueError):
        TokenManager(config, client)


async def test_constructor_rejects_loopback_lookalike_host(client):
    """``http://127.0.0.1.evil.com`` must not pass a naive ``startswith`` guard."""
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL="http://127.0.0.1.evil.com/token")
    with pytest.raises(ValueError):
        TokenManager(config, client)


async def test_constructor_rejects_bare_https_scheme_with_no_host(client):
    """A parsed check (not a prefix match) must reject a URL with no host at all."""
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL="https://")
    with pytest.raises(ValueError):
        TokenManager(config, client)


async def test_constructor_rejects_embedded_userinfo(client):
    """httpx applies URL-embedded userinfo as its own Basic auth header, silently
    overriding the header this module constructs for client_secret_basic — so a
    token URL carrying a username/password must be rejected at construction."""
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL="https://user:pass@auth.example.com/token")
    with pytest.raises(ValueError):
        TokenManager(config, client)


# --- client authentication methods ---------------------------------------------


@respx.mock
async def test_client_secret_basic_sends_basic_header_and_no_secret_in_form(client):
    route = respx.post(TOKEN_URL).mock(return_value=_ok_response(access_token="tok-1"))
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD="client_secret_basic")
    manager = TokenManager(config, client)

    token = await manager.get_token()

    assert token == "tok-1"
    assert route.call_count == 1
    sent = route.calls[0].request
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert sent.headers["Authorization"] == f"Basic {expected_basic}"
    form = _form(sent)
    assert "client_id" not in form
    assert "client_secret" not in form
    assert "scope" not in form
    assert form["grant_type"] == "client_credentials"


@respx.mock
async def test_client_secret_basic_form_urlencodes_id_and_secret_before_joining(client):
    """RFC 6749 §2.3.1 / Appendix B: client_id and client_secret are each
    application/x-www-form-urlencoded *before* being joined with ':' and
    base64'd. A raw ':' or '%' inside either value must not corrupt the
    username:password split a compliant server performs after decoding."""
    route = respx.post(TOKEN_URL).mock(return_value=_ok_response(access_token="tok-encoded"))
    raw_client_id = "id:with:colons"
    raw_client_secret = "secret%with%percent"
    config = _config(
        ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD="client_secret_basic",
        ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID=raw_client_id,
        ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET=raw_client_secret,
    )
    manager = TokenManager(config, client)

    token = await manager.get_token()

    assert token == "tok-encoded"
    sent = route.calls[0].request
    auth_header = sent.headers["Authorization"]
    assert auth_header.startswith("Basic ")
    decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode("utf-8")
    expected_username = quote_plus(raw_client_id, safe="")
    expected_password = quote_plus(raw_client_secret, safe="")
    assert decoded == f"{expected_username}:{expected_password}"
    # The encoded username/password must each still split unambiguously on
    # the join separator -- the whole point of encoding before joining.
    username, _, password = decoded.partition(":")
    assert username == expected_username
    assert password == expected_password


@respx.mock
async def test_client_secret_basic_plain_ascii_credentials_unchanged(client):
    """quote_plus is a no-op on unreserved characters, so the plain-ASCII
    BASE_ENV credentials must still produce exactly the same header as
    a raw (unencoded) colon-join would."""
    route = respx.post(TOKEN_URL).mock(return_value=_ok_response())
    config = _config()
    manager = TokenManager(config, client)

    await manager.get_token()

    sent = route.calls[0].request
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert sent.headers["Authorization"] == f"Basic {expected_basic}"


@respx.mock
async def test_client_secret_post_sends_form_fields_and_no_basic_header(client):
    route = respx.post(TOKEN_URL).mock(return_value=_ok_response(access_token="tok-2"))
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD="client_secret_post")
    manager = TokenManager(config, client)

    token = await manager.get_token()

    assert token == "tok-2"
    sent = route.calls[0].request
    assert "Authorization" not in sent.headers
    form = _form(sent)
    assert form["client_id"] == CLIENT_ID
    assert form["client_secret"] == CLIENT_SECRET
    assert form["grant_type"] == "client_credentials"


@respx.mock
async def test_scope_sent_space_joined_when_configured(client):
    route = respx.post(TOKEN_URL).mock(return_value=_ok_response())
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_SCOPES="read write")
    manager = TokenManager(config, client)

    await manager.get_token()

    form = _form(route.calls[0].request)
    assert form["scope"] == "read write"


# --- caching / single-flight ----------------------------------------------------


@respx.mock
async def test_second_call_uses_cache_no_network_call(client):
    route = respx.post(TOKEN_URL).mock(return_value=_ok_response())
    config = _config()
    manager = TokenManager(config, client)

    first = await manager.get_token()
    second = await manager.get_token()

    assert first == second == "tok"
    assert route.call_count == 1


@respx.mock
async def test_expiry_and_skew_boundary_forces_refresh(client):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            _ok_response(access_token="tok-1", expires_in=300),
            _ok_response(access_token="tok-2", expires_in=300),
        ]
    )
    clock = FakeClock(0.0)
    config = _config(ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS="60")
    manager = TokenManager(config, client, clock=clock)

    token = await manager.get_token()
    assert token == "tok-1"

    clock.advance(239.0)  # now=239, expires_at - skew = 300 - 60 = 240; 239 < 240 -> still cached
    token = await manager.get_token()
    assert token == "tok-1"
    assert route.call_count == 1

    clock.advance(1.0)  # now=240; 240 < 240 is False -> refetch
    token = await manager.get_token()
    assert token == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_concurrent_cold_start_single_network_call(client):
    async def handler(request):
        await asyncio.sleep(0.01)
        return _ok_response()

    route = respx.post(TOKEN_URL).mock(side_effect=handler)
    config = _config()
    manager = TokenManager(config, client)

    tokens = await asyncio.gather(*(manager.get_token() for _ in range(10)))

    assert all(token == "tok" for token in tokens)
    assert route.call_count == 1


@respx.mock
async def test_invalidate_forces_refetch(client):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            _ok_response(access_token="tok-1"),
            _ok_response(access_token="tok-2"),
        ]
    )
    config = _config()
    manager = TokenManager(config, client)

    first = await manager.get_token()
    manager.invalidate()
    second = await manager.get_token()

    assert first == "tok-1"
    assert second == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_failure_does_not_poison_cache_subsequent_call_retries(client):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"token_type": "bearer", "expires_in": 300}),  # missing access_token
            _ok_response(access_token="tok-recovered"),
        ]
    )
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError):
        await manager.get_token()

    token = await manager.get_token()

    assert token == "tok-recovered"
    assert route.call_count == 2


@respx.mock(assert_all_called=False)
async def test_follow_redirects_is_disabled_target_never_called(client, respx_mock):
    target = respx_mock.get("https://auth.example.com/other").mock(return_value=httpx.Response(200))
    respx_mock.post(TOKEN_URL).mock(return_value=httpx.Response(302, headers={"Location": "https://auth.example.com/other"}))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    assert exc_info.value.code == GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE
    assert target.call_count == 0


# --- failure classes -------------------------------------------------------------


def _assert_leak_free(exc: GatewayError) -> None:
    text = str(exc)
    assert exc.code == GatewayErrorCode.OAUTH_TOKEN_UNAVAILABLE
    assert CLIENT_ID not in text
    assert CLIENT_SECRET not in text
    assert BODY_SENTINEL not in text


@respx.mock
async def test_non_200_status_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(500, text=f"upstream on fire, secret={CLIENT_SECRET} {BODY_SENTINEL}"))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_non_json_body_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, text=f"not json, client_secret={CLIENT_SECRET} {BODY_SENTINEL}"))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_non_object_json_body_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=["not", "an", "object", BODY_SENTINEL]))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_missing_access_token_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token_type": "bearer", "expires_in": 300, "sentinel": BODY_SENTINEL})
    )
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_empty_access_token_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(access_token="", sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_wrong_token_type_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(token_type="mac", sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_token_type_is_case_insensitive_bearer_accepted(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(token_type="BEARER"))
    config = _config()
    manager = TokenManager(config, client)

    token = await manager.get_token()

    assert token == "tok"


@respx.mock
async def test_missing_token_type_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 300, "sentinel": BODY_SENTINEL}))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_zero_expires_in_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(expires_in=0, sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_negative_expires_in_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(expires_in=-5, sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_non_int_expires_in_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(expires_in="300", sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_bool_expires_in_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(expires_in=True, sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_float_expires_in_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(expires_in=300.5, sentinel=True))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_transport_error_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError(f"connection refused {BODY_SENTINEL}"))
    config = _config()
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)
    assert "connection refused" not in str(exc_info.value)


# --- fixed-lifetime fallback -----------------------------------------------------


@respx.mock
async def test_expires_in_absent_uses_fixed_lifetime_when_configured(client):
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-1", "token_type": "bearer"}),
            httpx.Response(200, json={"access_token": "tok-2", "token_type": "bearer"}),
        ]
    )
    clock = FakeClock(0.0)
    config = _config(
        ELSPETH_LLM_GATEWAY_OAUTH_FIXED_LIFETIME_SECONDS="300",
        ELSPETH_LLM_GATEWAY_REFRESH_SKEW_SECONDS="60",
    )
    manager = TokenManager(config, client, clock=clock)

    token = await manager.get_token()
    assert token == "tok-1"

    clock.advance(239.0)
    token = await manager.get_token()
    assert token == "tok-1"
    assert route.call_count == 1

    clock.advance(1.0)
    token = await manager.get_token()
    assert token == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_expires_in_absent_and_no_fixed_lifetime_raises_oauth_token_unavailable(client):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "token_type": "bearer", "sentinel": BODY_SENTINEL})
    )
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_FIXED_LIFETIME_SECONDS=None)
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)


@respx.mock
async def test_expires_in_present_invalid_never_falls_back_to_fixed_lifetime(client):
    respx.post(TOKEN_URL).mock(return_value=_ok_response(expires_in=0, sentinel=True))
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_FIXED_LIFETIME_SECONDS="300")
    manager = TokenManager(config, client)

    with pytest.raises(GatewayError) as exc_info:
        await manager.get_token()

    _assert_leak_free(exc_info.value)
