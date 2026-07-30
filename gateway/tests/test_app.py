import json
import logging

import httpx
import pytest
import respx
from elspeth_llm_gateway.core.app import CONTRACT_HEADER, REQUEST_ID_HEADER, create_app
from elspeth_llm_gateway.core.config import ConfigError, load_config
from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor
from elspeth_llm_gateway.sdk.types import Capability

TOKEN_URL = "https://auth.example.com/token"
UPSTREAM_ORIGIN = "https://upstream.example.com"
UPSTREAM_URL = f"{UPSTREAM_ORIGIN}/v1/invoke"
BEARER = "b" * 40
CLIENT_SECRET = "c" * 40

BASE_ENV = {
    "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": BEARER,
    "ELSPETH_LLM_GATEWAY_ADAPTER": "reference_v1_invoke",
    "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": UPSTREAM_ORIGIN,
    "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": TOKEN_URL,
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": "client-id-value",
    "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
    "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
    "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
    "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
    "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "10000",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
    "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
    "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": '{"gpt-4o": {"target": "backend-a"}}',
}

CHAT_BODY = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}


def _config(**overrides):
    env = dict(BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return load_config(env)


def _headers(**overrides) -> dict:
    base = {
        "Authorization": f"Bearer {BEARER}",
        CONTRACT_HEADER: "1",
        "Content-Type": "application/json",
    }
    base.update(overrides)
    return base


def _client_for(config, *, root_path: str = "", **create_app_kwargs) -> httpx.AsyncClient:
    app = create_app(config, **create_app_kwargs)
    transport = httpx.ASGITransport(app=app, root_path=root_path)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _mock_token(token: str = "tok-1"):
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": token, "token_type": "bearer", "expires_in": 300})
    )


# --- auth --------------------------------------------------------------------


@respx.mock
async def test_missing_bearer_returns_401_envelope_with_headers():
    async with _client_for(_config()) as client:
        headers = _headers()
        del headers["Authorization"]
        response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"
    assert response.headers[REQUEST_ID_HEADER]
    assert response.headers[CONTRACT_HEADER] == "1"


@respx.mock
async def test_wrong_bearer_returns_401_envelope_with_headers():
    async with _client_for(_config()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=CHAT_BODY,
            headers=_headers(Authorization="Bearer wrong-token-value", **{REQUEST_ID_HEADER: "req-wrong-bearer"}),
        )

    assert response.status_code == 401
    envelope = response.json()
    assert envelope["error"]["code"] == "inbound_authentication_failed"
    assert response.headers[REQUEST_ID_HEADER] == "req-wrong-bearer"
    assert response.headers[CONTRACT_HEADER] == "1"
    assert envelope["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


# --- contract header -----------------------------------------------------------


@respx.mock
async def test_missing_contract_header_returns_400_contract_mismatch():
    """Also sends an inbound X-Request-ID and asserts it's echoed back, proving
    RequestIDMiddleware (outermost) still ran and stamped its header even
    though ContractHeaderMiddleware (nested inside it) short-circuited the
    request before it ever reached auth or the route."""
    async with _client_for(_config()) as client:
        headers = _headers(**{REQUEST_ID_HEADER: "req-missing-contract"})
        del headers[CONTRACT_HEADER]
        response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "contract_mismatch"
    assert response.headers[REQUEST_ID_HEADER] == "req-missing-contract"
    assert response.headers[CONTRACT_HEADER] == "1"


@respx.mock
async def test_wrong_contract_header_value_returns_400_contract_mismatch():
    async with _client_for(_config()) as client:
        response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=_headers(**{CONTRACT_HEADER: "2"}))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "contract_mismatch"


@respx.mock
async def test_contract_check_runs_before_auth():
    """Bad contract header AND bad bearer together must surface contract_mismatch,
    proving contract-header enforcement executes before the auth layer."""
    async with _client_for(_config()) as client:
        headers = _headers(Authorization="Bearer wrong", **{CONTRACT_HEADER: "2"})
        response = await client.post("/v1/chat/completions", json=CHAT_BODY, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "contract_mismatch"


# --- body parsing ----------------------------------------------------------------


@respx.mock
async def test_unknown_json_field_returns_400_invalid_request():
    """Also proves headers reach a response built by the GatewayError exception
    handler (deep inside the middleware stack), and that the request id in
    the error envelope body matches the one echoed in the header."""
    body = dict(CHAT_BODY, stream=True)
    async with _client_for(_config()) as client:
        response = await client.post("/v1/chat/completions", json=body, headers=_headers(**{REQUEST_ID_HEADER: "req-invalid-body"}))

    assert response.status_code == 400
    envelope = response.json()
    assert envelope["error"]["code"] == "invalid_request"
    assert response.headers[REQUEST_ID_HEADER] == "req-invalid-body"
    assert response.headers[CONTRACT_HEADER] == "1"
    assert envelope["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


@respx.mock
async def test_deeply_nested_body_recursion_error_returns_400_invalid_request():
    payload = b"[" * 50000 + b"]" * 50000
    async with _client_for(_config()) as client:
        response = await client.post("/v1/chat/completions", content=payload, headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@respx.mock
async def test_oversized_body_returns_400_invalid_request():
    config = _config(ELSPETH_LLM_GATEWAY_MAX_BODY_BYTES="64")
    big_body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x" * 500}]}
    async with _client_for(config) as client:
        response = await client.post("/v1/chat/completions", json=big_body, headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def _tool_call_body(arguments: str) -> dict:
    return {
        "model": "gpt-4o",
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": arguments}}],
            }
        ],
    }


@respx.mock
async def test_malformed_tool_call_arguments_returns_400_invalid_request():
    """A tool call whose ``arguments`` string is not valid JSON must be
    rejected as a 400 before the adapter (and therefore the upstream) is
    ever reached -- previously this reached the reference adapter's own
    ``json.loads(call.arguments_json)`` inside ``build_invoke`` and
    surfaced as a 500 ``internal_error``."""
    async with _client_for(_config()) as client:
        response = await client.post("/v1/chat/completions", json=_tool_call_body("NOT JSON"), headers=_headers())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@respx.mock
async def test_valid_tool_call_arguments_returns_200():
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"result": {"text": "hello"}, "halt": "complete"}))

    async with _client_for(_config()) as client:
        response = await client.post("/v1/chat/completions", json=_tool_call_body('{"a": 1}'), headers=_headers())

    assert response.status_code == 200


# --- capability check ------------------------------------------------------------


class _TextOnlyAdapter:
    """Declares only ``Capability.TEXT`` -- the default reference adapter
    supports every capability, so this is needed to exercise
    ``capability_unsupported`` at the app level at all."""

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(name="text_only_adapter", version="0.0.1", adapter_api_major=1, capabilities=frozenset({Capability.TEXT}))

    def validate_configuration(self, options: dict) -> None:
        return None

    def build_invoke(self, request):
        raise NotImplementedError

    def parse_success(self, body):
        raise NotImplementedError

    def classify_error(self, failure):
        raise NotImplementedError


@respx.mock
async def test_capability_unsupported_returns_422_with_both_headers():
    """No respx routes are registered: if the capability check ever let the
    request reach the upstream call (or even the OAuth token endpoint),
    respx would raise instead of this test's assertions ever running."""
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }
    async with _client_for(_config(), adapter=_TextOnlyAdapter()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=body,
            headers=_headers(**{REQUEST_ID_HEADER: "req-capability"}),
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "capability_unsupported"
    assert response.headers[REQUEST_ID_HEADER] == "req-capability"
    assert response.headers[CONTRACT_HEADER] == "1"


# --- happy path ------------------------------------------------------------------


@respx.mock
async def test_happy_path_echoes_request_id_and_contract_header():
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"result": {"text": "hello"}, "halt": "complete"}))

    async with _client_for(_config()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=CHAT_BODY,
            headers=_headers(**{REQUEST_ID_HEADER: "my-request-id"}),
        )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "my-request-id"
    assert response.headers[CONTRACT_HEADER] == "1"
    body = response.json()
    assert body["id"] == "gwcmpl-my-request-id"
    assert body["choices"][0]["message"]["content"] == "hello"


@respx.mock
async def test_invalid_inbound_request_id_is_replaced_with_generated_one():
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"result": {"text": "hello"}, "halt": "complete"}))

    async with _client_for(_config()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=CHAT_BODY,
            headers=_headers(**{REQUEST_ID_HEADER: "has a space/invalid!"}),
        )

    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] != "has a space/invalid!"
    assert response.headers[REQUEST_ID_HEADER]


# --- healthz / readyz --------------------------------------------------------------


@respx.mock
async def test_healthz_requires_no_auth_and_returns_status_ok():
    async with _client_for(_config()) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@respx.mock
async def test_readyz_exact_key_set_and_no_secret_material():
    async with _client_for(_config()) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "ready",
        "contract_major",
        "adapter",
        "capabilities",
        "model_aliases",
        "mapping_generation",
        "oauth_fixed_lifetime",
        "errors",
    }
    assert set(body["adapter"].keys()) == {"name", "version", "adapter_api_major", "fingerprint"}
    assert body["ready"] is True
    assert body["errors"] == []
    assert body["adapter"]["name"] == "reference_v1_invoke"
    assert body["model_aliases"] == ["gpt-4o"]
    assert body["oauth_fixed_lifetime"] is False

    response_text = response.text
    assert BEARER not in response_text
    assert CLIENT_SECRET not in response_text


@respx.mock
async def test_readyz_requires_no_auth():
    async with _client_for(_config()) as client:
        response = await client.get("/readyz")
    assert response.status_code != 401


@respx.mock
async def test_readyz_oauth_fixed_lifetime_true_when_configured():
    config = _config(ELSPETH_LLM_GATEWAY_OAUTH_FIXED_LIFETIME_SECONDS="600")
    async with _client_for(config) as client:
        response = await client.get("/readyz")
    assert response.json()["oauth_fixed_lifetime"] is True


@respx.mock
async def test_readyz_reports_adapter_api_incompatible():
    class BadAdapter:
        def descriptor(self) -> AdapterDescriptor:
            return AdapterDescriptor(name="bad_adapter", version="0.0.1", adapter_api_major=999, capabilities=frozenset({Capability.TEXT}))

        def validate_configuration(self, options: dict) -> None:
            return None

        def build_invoke(self, request):
            raise NotImplementedError

        def parse_success(self, body):
            raise NotImplementedError

        def classify_error(self, failure):
            raise NotImplementedError

    async with _client_for(_config(), adapter=BadAdapter()) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert "adapter_api_incompatible" in body["errors"]


@respx.mock
async def test_readyz_reports_adapter_configuration_invalid():
    class MisconfiguredAdapter:
        def descriptor(self) -> AdapterDescriptor:
            return AdapterDescriptor(
                name="misconfigured_adapter", version="0.0.1", adapter_api_major=1, capabilities=frozenset({Capability.TEXT})
            )

        def validate_configuration(self, options: dict) -> None:
            raise ValueError("requires deployment-specific options")

        def build_invoke(self, request):
            raise NotImplementedError

        def parse_success(self, body):
            raise NotImplementedError

        def classify_error(self, failure):
            raise NotImplementedError

    async with _client_for(_config(), adapter=MisconfiguredAdapter()) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert "adapter_configuration_invalid" in body["errors"]


@respx.mock
async def test_readyz_never_calls_oauth_or_upstream():
    # No routes are mocked at all: if readyz ever called the token endpoint or
    # the upstream origin, respx would raise (no matching mock) rather than
    # this test's assertions ever running.
    async with _client_for(_config()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200


# --- unhandled exception -----------------------------------------------------------


class _BoomAdapter:
    """An adapter whose ``descriptor()`` itself raises -- simulates a genuine,
    unanticipated bug reaching the request pipeline (as opposed to a
    ``GatewayError``, which every other error-path test exercises)."""

    def descriptor(self) -> AdapterDescriptor:
        raise RuntimeError("boom: adapter bug unrelated to any GatewayError code")

    def validate_configuration(self, options: dict) -> None:
        return None

    def build_invoke(self, request):
        raise NotImplementedError

    def parse_success(self, body):
        raise NotImplementedError

    def classify_error(self, failure):
        raise NotImplementedError


@respx.mock
async def test_unhandled_exception_returns_500_internal_error_with_headers():
    """Regression test: a non-GatewayError exception must be converted to the
    internal_error envelope *and* still carry both headers. FastAPI's
    ``@app.exception_handler(Exception)`` is hoisted by Starlette onto
    ServerErrorMiddleware, which sits outside every ``add_middleware`` layer
    (including RequestIDMiddleware) and re-raises after responding -- with
    ASGITransport's default ``raise_app_exceptions=True`` that would surface
    as a raised exception here, not a 500 response. This is why the
    catch-all lives inside RequestIDMiddleware's own dispatch instead."""
    async with _client_for(_config(), adapter=_BoomAdapter()) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=CHAT_BODY,
            headers=_headers(**{REQUEST_ID_HEADER: "req-unhandled"}),
        )

    assert response.status_code == 500
    envelope = response.json()
    assert envelope["error"]["code"] == "internal_error"
    assert response.headers[REQUEST_ID_HEADER] == "req-unhandled"
    assert response.headers[CONTRACT_HEADER] == "1"
    assert envelope["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


# --- adapter resolution --------------------------------------------------------------


def test_unknown_adapter_name_raises_config_error():
    with pytest.raises(ConfigError):
        create_app(_config(ELSPETH_LLM_GATEWAY_ADAPTER="no_such_adapter_anywhere"))


# --- root_path auth+contract bypass (CRITICAL regression) ------------------------------


@respx.mock
async def test_root_path_prefixed_request_without_bearer_returns_401_not_bypassed():
    """The CRITICAL regression: both middlewares used to gate on
    ``request.url.path`` (== ``scope["path"]``, which still includes
    ``root_path``), while the router matches routes on
    ``get_route_path(scope)`` (``root_path`` stripped). Under
    ``root_path="/gw"``, ``"/gw/v1/chat/completions"`` does not start with
    ``"/v1/"``, so both middlewares used to skip their check entirely while
    the router still resolved and served the route underneath -- a full
    auth+contract bypass. No respx routes are registered here: if the
    request ever reached CompletionService (and so the OAuth token
    endpoint), respx would raise instead of this test's assertions ever
    running.
    """
    async with _client_for(_config(), root_path="/gw") as client:
        response = await client.post(
            "/gw/v1/chat/completions",
            json=CHAT_BODY,
            headers={CONTRACT_HEADER: "1", "Content-Type": "application/json"},  # no Authorization at all
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"


@respx.mock
async def test_root_path_prefixed_request_without_contract_header_returns_400_not_bypassed():
    async with _client_for(_config(), root_path="/gw") as client:
        headers = _headers()
        del headers[CONTRACT_HEADER]
        response = await client.post("/gw/v1/chat/completions", json=CHAT_BODY, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "contract_mismatch"


@respx.mock
async def test_root_path_prefixed_request_with_valid_credentials_still_works():
    """The fix must not break the legitimate case: valid bearer + contract
    header under a non-empty root_path still reaches the route and
    completes normally."""
    _mock_token()
    respx.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json={"result": {"text": "hello"}, "halt": "complete"}))

    async with _client_for(_config(), root_path="/gw") as client:
        response = await client.post("/gw/v1/chat/completions", json=CHAT_BODY, headers=_headers())

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"


# --- route surface (I1: no /docs, /redoc, /openapi.json) -------------------------------


def test_exact_route_set_excludes_docs_redoc_openapi():
    app = create_app(_config())
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert paths == {"/healthz", "/readyz", "/v1/chat/completions"}


# --- caplog sweep: no leaked secrets or content ------------------------------------


@respx.mock
async def test_caplog_sweep_never_leaks_prompt_bearer_secret_or_upstream_body(caplog):
    caplog.set_level(logging.INFO, logger="elspeth_llm_gateway")
    prompt_marker = "PROMPT-MARKER-9f3a2c1d"
    upstream_marker = "UPSTREAM-BODY-MARKER-7e21bc"

    _mock_token()
    respx.post(UPSTREAM_URL).mock(
        side_effect=[
            httpx.Response(200, json={"result": {"text": "hello"}, "halt": "complete"}),
            httpx.Response(500, json={"fault": {"kind": "unrecognized_kind", "detail": upstream_marker}}),
        ]
    )

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": prompt_marker}]}

    async with _client_for(_config()) as client:
        happy = await client.post("/v1/chat/completions", json=body, headers=_headers())
        assert happy.status_code == 200

        errored = await client.post("/v1/chat/completions", json=body, headers=_headers())
        assert errored.status_code == 502

    # A caplog sweep that finds no records proves nothing: assert the
    # "completion" event actually fired for both the success and the error
    # path before checking that neither one leaked anything unsafe.
    assert caplog.records
    completion_records = [json.loads(record.getMessage()) for record in caplog.records if record.getMessage().startswith("{")]
    statuses = {record["status"] for record in completion_records if "status" in record}
    assert "success" in statuses
    assert "error" in statuses
    error_records = [record for record in completion_records if record.get("status") == "error"]
    assert any(record.get("error_code") == "upstream_response_invalid" for record in error_records)

    all_messages = "\n".join(record.getMessage() for record in caplog.records)
    assert prompt_marker not in all_messages
    assert BEARER not in all_messages
    assert CLIENT_SECRET not in all_messages
    assert upstream_marker not in all_messages
