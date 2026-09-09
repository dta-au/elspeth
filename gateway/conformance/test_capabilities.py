"""Auth strictness, unknown-field rejection, the text happy path, and the
capability-declared/undeclared sweep.

Every capability-gated test here first reads ``/readyz`` (via the
``declared_capabilities`` fixture): claiming a capability and then failing to
honor it is the only failure this file recognizes for the gated capabilities
(``tools``, ``json_object``, ``json_schema``, ``seed``) -- when the target
adapter does not declare one, requesting it must be rejected with
``capability_unsupported``, never silently ignored or 500'd. Deeper coverage
of the *declared* behavior for ``tools``/``json_object``/``json_schema``
lives in ``test_tools.py``/``test_structured.py``; this file only asserts
that requesting an undeclared one is rejected (or, when declared, that it is
not).
"""

import pytest

_CHAT_URL = "/v1/chat/completions"

_GATED_CAPABILITY_EXTRA = {
    "tools": {"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]},
    "json_object": {"response_format": {"type": "json_object"}},
    "json_schema": {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer_schema", "schema": {"type": "object", "properties": {"answer": {"type": "string"}}}},
        }
    },
    "seed": {"seed": 7},
}


# --- auth strictness, including credential-channel rejection ------------------


async def test_missing_bearer_returns_401(gateway_client, chat_headers, chat_body_factory):
    headers = dict(chat_headers)
    del headers["Authorization"]

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"


async def test_wrong_bearer_returns_401(gateway_client, chat_headers, chat_body_factory):
    headers = dict(chat_headers, Authorization="Bearer not-the-right-token-value")

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"


async def test_query_param_credential_channel_is_rejected(gateway_client, chat_headers, chat_body_factory, bearer):
    """``?access_token=<bearer>`` with NO ``Authorization`` header must not
    authenticate: the gateway's only inbound credential channel is the
    ``Authorization`` header (see ``core.auth.check_bearer``)."""
    headers = dict(chat_headers)
    del headers["Authorization"]

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers, params={"access_token": bearer})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"


async def test_cookie_credential_channel_is_rejected(gateway_client, chat_headers, chat_body_factory, bearer):
    """``Cookie: token=<bearer>`` with NO ``Authorization`` header must not
    authenticate either -- there is no cookie credential path anywhere in
    this app."""
    headers = dict(chat_headers)
    del headers["Authorization"]
    headers["Cookie"] = f"token={bearer}"

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "inbound_authentication_failed"


# --- unknown-field rejection ---------------------------------------------------


async def test_unknown_top_level_field_is_rejected(gateway_client, chat_headers, chat_body_factory):
    body = chat_body_factory("hello", stream=True)

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# --- max_completion_tokens: the modern spelling of max_tokens -------------------
#
# A qualifying agency needs to see this explicitly, because it is the one
# key outside the declared field list that a conforming gateway must
# accept: OpenAI's current Chat Completions API uses
# ``max_completion_tokens``, and LiteLLM's ``openai`` provider path
# *translates* a caller's ``max_tokens`` into it -- the original key is
# dropped, so a plain LiteLLM client with any token cap sends only this
# spelling. A derived image that rejects it cannot serve LiteLLM callers at
# all (it is boot-fatal for ELSPETH's own composer boot probe).
#
# ``test_unknown_top_level_field_is_rejected`` immediately above is the
# companion proof that accepting this one key did NOT relax the
# strict-unknown-field posture generally; it is not duplicated here.


async def test_max_completion_tokens_is_accepted_as_the_max_tokens_spelling(gateway_client, chat_headers, chat_body_factory):
    body = chat_body_factory("hello", max_completion_tokens=16)

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "MOCK:hello"


async def test_supplying_both_max_tokens_spellings_is_rejected(gateway_client, chat_headers, chat_body_factory):
    """Two spellings of one cap is an ambiguity the gateway must not resolve
    by silently picking a winner. Rejection is by key presence, so this
    holds regardless of whether the two values agree."""
    body = chat_body_factory("hello", max_tokens=16, max_completion_tokens=16)

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_configured_max_tokens_bound_is_enforced_on_the_alias_spelling(gateway_client, chat_headers, chat_body_factory):
    """The cap must not be escapable by choosing the other spelling.

    ``32768`` is the ``max_max_tokens`` default (``Bounds``); the
    conformance stack does not override it, so ``32769`` is over the bound
    without needing any deployment-specific fixture. A derived image that
    lowers the bound rejects this too, so the assertion holds either way.
    """
    body = chat_body_factory("hello", max_completion_tokens=32769)

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# --- text completion happy path -------------------------------------------------


async def test_text_completion_happy_path(gateway_client, chat_headers, chat_body_factory, declared_capabilities):
    assert "text" in declared_capabilities

    response = await gateway_client.post(_CHAT_URL, json=chat_body_factory("hello"), headers=chat_headers)

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["message"]["content"] == "MOCK:hello"
    assert choice["finish_reason"] == "stop"


# --- capability sweep: undeclared capability must be rejected -------------------


@pytest.mark.parametrize("capability", sorted(_GATED_CAPABILITY_EXTRA))
async def test_capability_declared_or_rejected(gateway_client, chat_headers, chat_body_factory, declared_capabilities, capability):
    body = chat_body_factory("hello", **_GATED_CAPABILITY_EXTRA[capability])

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    if capability in declared_capabilities:
        # Claiming-and-failing is the only failure this sweep exists to
        # catch: a declared capability must actually work end to end
        # against the deterministic mock, not merely avoid 422 (a 500/502
        # here would be exactly that failure, and `!= 422` would miss it).
        assert response.status_code == 200
    else:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "capability_unsupported"
