"""Tests for the deterministic mock OAuth + mock upstream stack (``mock/``).

Both mock apps are exercised directly over ``httpx.ASGITransport`` -- no
network, no uvicorn -- so these tests are as fast and hermetic as any other
unit test in this suite. ``mock/stack.py``'s uvicorn wiring itself (actually
binding three ports and serving forever) is not something a unit test should
start; instead its env-building and message-formatting helpers are imported
and asserted against directly, so a future required env var or a broken
curl example fails a test here rather than only showing up when someone
runs ``python -m mock.stack`` by hand.
"""

import base64
import json
from urllib.parse import quote_plus

import httpx
import mock.stack as mock_stack
import pytest
from mock.oauth import create_mock_oauth_app
from mock.upstream import create_mock_upstream_app

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret-value"  # secret-scan: allow-this-line


async def _post_token(app, *, data=None, headers=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://oauth.mock") as client:
        return await client.post("/token", data=data, headers=headers)


def _basic_header(client_id: str, client_secret: str) -> str:
    username = quote_plus(client_id, safe="")
    password = quote_plus(client_secret, safe="")
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


# --- mock oauth: token issuance --------------------------------------------------


async def test_sequential_tokens_are_deterministic_and_ordered():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    form = {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    first = await _post_token(app, data=form)
    second = await _post_token(app, data=form)
    third = await _post_token(app, data=form)

    assert first.status_code == 200
    assert first.json()["access_token"] == "mock-token-1"
    assert second.json()["access_token"] == "mock-token-2"
    assert third.json()["access_token"] == "mock-token-3"


async def test_default_expires_in_is_returned():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, expires_in=1234)
    form = {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    response = await _post_token(app, data=form)

    assert response.json()["expires_in"] == 1234
    assert response.json()["token_type"] == "bearer"


async def test_client_secret_post_accepted():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    form = {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    response = await _post_token(app, data=form)

    assert response.status_code == 200
    assert response.json()["access_token"] == "mock-token-1"


async def test_client_secret_basic_accepted_with_rfc6749_form_urlencoding():
    """Credentials containing reserved characters must still round-trip: the
    gateway's own TokenManager form-urlencodes each of client_id/client_secret
    (via quote_plus) before joining with ':' and base64-encoding, so the mock
    must undo that same encoding -- not a raw base64(id:secret) decode."""
    raw_id = "id:with:colons"
    raw_secret = "secret%with%percent"  # secret-scan: allow-this-line
    app = create_mock_oauth_app(client_id=raw_id, client_secret=raw_secret)

    response = await _post_token(
        app,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": _basic_header(raw_id, raw_secret)},
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "mock-token-1"


async def test_client_secret_basic_plain_ascii_matches_naive_join():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    response = await _post_token(
        app,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": _basic_header(CLIENT_ID, CLIENT_SECRET)},
    )

    assert response.status_code == 200
    assert response.json()["access_token"] == "mock-token-1"


async def test_wrong_grant_type_rejected():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    response = await _post_token(app, data={"grant_type": "authorization_code", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})

    assert response.status_code == 400


async def test_wrong_credentials_rejected():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)

    response = await _post_token(app, data={"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": "wrong"})

    assert response.status_code == 401


async def test_fail_next_fails_exactly_once_then_resets():
    app = create_mock_oauth_app(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, fail_next={"status": 500})
    form = {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    first = await _post_token(app, data=form)
    second = await _post_token(app, data=form)
    third = await _post_token(app, data=form)

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json()["access_token"] == "mock-token-1"  # the failed call never advanced the counter
    assert third.status_code == 200
    assert third.json()["access_token"] == "mock-token-2"


# --- mock upstream: /v1/invoke ---------------------------------------------------


def _conversation(text: str, *, extra: dict | None = None) -> dict:
    body = {
        "conversation": [{"speaker": "user", "text": text}],
        "generation": {"target": "mock-target"},
    }
    if extra:
        body.update(extra)
    return body


async def _invoke(app, body: dict, *, token: str | None = "mock-token-1") -> httpx.Response:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://upstream.mock") as client:
        return await client.post("/v1/invoke", json=body, headers=headers)


async def test_text_echo_default_behavior():
    app = create_mock_upstream_app()

    response = await _invoke(app, _conversation("hello there"))

    assert response.status_code == 200
    body = response.json()
    assert body["result"]["text"] == "MOCK:hello there"
    assert body["halt"] == "complete"
    assert body["accounting"]["input_units"] == len("hello there") // 4
    assert body["accounting"]["output_units"] == len("MOCK:hello there") // 4


async def test_text_echo_is_pure_function_of_request():
    """Same request in, byte-identical response out -- no randomness, no clock."""
    app = create_mock_upstream_app()
    body = _conversation("determinism check")

    first = await _invoke(app, body)
    second = await _invoke(app, body)

    assert first.json() == second.json()


async def test_use_tool_trigger_returns_invocation_and_halts_operations():
    app = create_mock_upstream_app()
    payload = {"query": "widgets", "limit": 3}
    text = f"USE_TOOL search_catalog {json.dumps(payload)}"

    response = await _invoke(app, _conversation(text))

    assert response.status_code == 200
    body = response.json()
    assert body["halt"] == "operations"
    invocations = body["result"]["invocations"]
    assert len(invocations) == 1
    assert invocations[0]["operation"] == "search_catalog"
    assert invocations[0]["payload"] == payload
    assert isinstance(invocations[0]["ref"], str) and invocations[0]["ref"]


@pytest.mark.parametrize(
    "kind,expected_status",
    [
        ("throttle", 429),
        ("overloaded", 503),
        ("screening", 400),
        ("too_long", 400),
    ],
)
async def test_trigger_fault_returns_matching_status_and_body(kind, expected_status):
    app = create_mock_upstream_app()

    response = await _invoke(app, _conversation(f"TRIGGER_FAULT {kind}"))

    assert response.status_code == expected_status
    assert response.json() == {"fault": {"kind": kind}}


async def test_schema_format_echoes_under_first_declared_property():
    app = create_mock_upstream_app()
    schema = {"type": "object", "properties": {"answer": {"type": "string"}, "confidence": {"type": "number"}}}
    body = _conversation("what is the answer", extra={"format": {"kind": "schema", "schema": schema}})

    response = await _invoke(app, body)

    assert response.status_code == 200
    result = response.json()["result"]
    parsed = json.loads(result["text"])
    assert parsed == {"answer": "what is the answer"}


async def test_object_format_produces_text_that_is_valid_json():
    app = create_mock_upstream_app()
    body = _conversation("give me json", extra={"format": {"kind": "object"}})

    response = await _invoke(app, body)

    assert response.status_code == 200
    result = response.json()["result"]
    parsed = json.loads(result["text"])
    assert parsed == {"echo": "give me json"}


async def test_directive_policy_key_is_tolerated_and_ignored():
    """When tool_choice is set, the real adapter sends a directive_policy key
    the mock must not choke on -- it may simply ignore it."""
    app = create_mock_upstream_app()
    body = _conversation("hello", extra={"directive_policy": {"mode": "required"}})

    response = await _invoke(app, body)

    assert response.status_code == 200
    assert response.json()["result"]["text"] == "MOCK:hello"


async def test_missing_bearer_returns_401():
    app = create_mock_upstream_app()

    response = await _invoke(app, _conversation("hi"), token=None)

    assert response.status_code == 401


async def test_foreign_bearer_prefix_returns_401():
    app = create_mock_upstream_app()

    response = await _invoke(app, _conversation("hi"), token="some-other-token-1")

    assert response.status_code == 401


async def test_custom_bearer_prefix_is_honored():
    app = create_mock_upstream_app(require_bearer_prefix="custom-prefix-")

    rejected = await _invoke(app, _conversation("hi"), token="mock-token-1")
    accepted = await _invoke(app, _conversation("hi"), token="custom-prefix-1")

    assert rejected.status_code == 401
    assert accepted.status_code == 200


# --- mock/stack.py: env wiring + curl example ------------------------------------


def test_stack_env_satisfies_the_real_gateway_config_loader():
    """``_build_gateway_env`` must stay a valid ``load_config`` input: if a
    future task adds a required ``ELSPETH_LLM_GATEWAY_*`` var and this
    helper isn't updated, ``load_config`` fails closed on ``missing_env:*``
    and the documented quick-start dies silently until someone runs it by
    hand. This test fails loudly instead."""
    env = mock_stack._build_gateway_env(oauth_port=18788, upstream_port=18789)

    config = mock_stack.load_config(env)

    assert config.adapter_name == "reference_v1_invoke"
    assert config.upstream_origin == "http://127.0.0.1:18789"
    assert config.oauth_token_url == "http://127.0.0.1:18788/token"
    assert config.oauth_client_id.get_secret_value() == mock_stack.MOCK_CLIENT_ID
    assert config.oauth_client_secret.get_secret_value() == mock_stack.MOCK_CLIENT_SECRET
    assert mock_stack._MODEL_ALIAS in config.model_mappings


def test_stack_builds_a_real_gateway_app_from_its_own_env():
    """End-to-end wiring check, short of actually binding a port: the env
    this module builds must produce an app ``create_app`` accepts."""
    env = mock_stack._build_gateway_env(oauth_port=18788, upstream_port=18789)
    config = mock_stack.load_config(env)

    app = mock_stack.create_app(config)

    assert app is not None


def test_curl_example_is_pasteable_and_matches_the_wired_env():
    example = mock_stack._curl_example(8787)

    assert "http://127.0.0.1:8787/v1/chat/completions" in example
    assert f"Bearer {mock_stack.MOCK_INBOUND_BEARER}" in example
    assert "X-ELSPETH-LLM-Gateway-Contract: 1" in example
    assert f'"model": "{mock_stack._MODEL_ALIAS}"' in example
