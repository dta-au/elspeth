"""Contract-header enforcement: optional inbound negotiation, mandatory outbound echo.

``X-ELSPETH-LLM-Gateway-Contract: 1`` is *optional* on every ``/v1/*``
request -- Phase 3 relaxed this from a Phase 1 mandatory requirement so a
plain OpenAI-compatible client (the OpenAI SDK, LiteLLM, ELSPETH's own
OpenRouter-style provider) can talk to this gateway with only a bearer
token, no gateway-specific header at all. The gateway is an affordance over
a trusted, operator-chosen upstream, not a required protocol. Three-way
behaviour: absent -> proceed; present and ``"1"`` -> proceed; present and
anything else -> reject ``contract_mismatch`` (400). The header is still
stamped onto every response this gateway ever produces -- success or error,
regardless of which layer produced the error (auth, contract check, or the
route itself) -- unconditionally, whether or not the inbound request sent
one. See ``elspeth_llm_gateway.core.app``'s
``RequestIDMiddleware``/``ContractHeaderMiddleware`` docstrings for why:
this file re-verifies that contract at the wire level, with no internals
import.
"""

_CONTRACT_HEADER = "X-ELSPETH-LLM-Gateway-Contract"
_CHAT_URL = "/v1/chat/completions"


async def test_valid_contract_header_reaches_success_and_is_echoed(gateway_client, chat_headers, chat_body_factory):
    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=chat_headers)

    assert response.status_code == 200
    assert response.headers[_CONTRACT_HEADER] == "1"


async def test_missing_contract_header_with_valid_bearer_reaches_success_and_is_still_echoed(
    gateway_client, chat_headers, chat_body_factory
):
    """The plain-OpenAI-client path: no contract header sent at all, valid
    bearer only. Must succeed, and the outbound header is stamped
    regardless."""
    headers = dict(chat_headers)
    del headers[_CONTRACT_HEADER]

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 200
    assert response.headers[_CONTRACT_HEADER] == "1"


async def test_wrong_contract_header_value_returns_400_and_still_carries_response_header(gateway_client, chat_headers, chat_body_factory):
    """A header that IS present but wrong is still rejected -- the
    relaxation widens "absent", never "wrong"."""
    headers = dict(chat_headers, **{_CONTRACT_HEADER: "2"})

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "contract_mismatch"
    assert response.headers[_CONTRACT_HEADER] == "1"


async def test_contract_header_present_on_auth_error_response(gateway_client, chat_headers, chat_body_factory):
    headers = dict(chat_headers)
    del headers["Authorization"]

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 401
    assert response.headers[_CONTRACT_HEADER] == "1"


async def test_no_bearer_and_no_contract_header_returns_401_not_bypassed(gateway_client, chat_headers, chat_body_factory):
    """Security-critical: the contract-header relaxation must not become an
    auth bypass. With neither header sent, auth still fires independently
    and the request is still rejected -- 401, never 200."""
    headers = dict(chat_headers)
    del headers["Authorization"]
    del headers[_CONTRACT_HEADER]

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"
    assert response.headers[_CONTRACT_HEADER] == "1"


async def test_contract_header_present_on_healthz_and_readyz_though_neither_requires_it(gateway_client):
    """``/healthz`` and ``/readyz`` accept requests with no contract header at
    all (they are not under ``/v1/*``), but the response header is stamped
    unconditionally by the outermost middleware regardless."""
    for path in ("/healthz", "/readyz"):
        response = await gateway_client.get(path)
        assert response.headers[_CONTRACT_HEADER] == "1"
