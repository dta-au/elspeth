"""Error-envelope shape, plus the mock upstream's halt/fault behaviors.

Error envelope shape is asserted for ``model_not_allowed``, ``invalid_request``
(both always reachable regardless of the target adapter's declared
capabilities), and ``capability_unsupported``. The real reference adapter
declares every gated capability, so ``capability_unsupported`` is otherwise
unreachable via HTTP against it; the ``capability_unsupported`` test below
uses ``limited_gateway_client`` (see ``conftest.py``'s ``_TextOnlyAdapter``)
-- a second, independently-built in-process gateway app whose adapter
declares only ``text`` -- so this file asserts all three envelope shapes for
real rather than skipping one.

The halt/fault sections drive the mock upstream's documented triggers (see
``mock/upstream.py``'s module docstring) and assert the resulting
``finish_reason`` / ``GatewayErrorCode`` mapping. Two notes that module's
docstring calls out, preserved here:

1. ``TRIGGER_HALT screened`` (a 200 success body -> ``finish_reason``
   ``content_filter``) is a *different* path from ``TRIGGER_FAULT
   screening`` (a 400 fault body -> error code ``content_policy_rejected``).
   Both are asserted separately below -- neither covers the other.
2. ``TRIGGER_FAULT throttle`` exercises the gateway transport's ``429``
   short-circuit (``upstream_rate_limited``), never the adapter's own
   ``classify_error`` mapping for ``"throttle"`` -- the transport reports
   ``upstream_rate_limited`` on any ``429`` before an adapter ever sees the
   response. The test below documents this instead of claiming
   adapter-mapping coverage it does not have.
"""

import pytest

_CHAT_URL = "/v1/chat/completions"
_ENVELOPE_KEYS = {"message", "type", "code", "retryable", "request_id"}


def _assert_envelope_shape(payload: dict, *, code: str) -> None:
    error = payload["error"]
    assert set(error.keys()) == _ENVELOPE_KEYS
    assert error["type"] == "gateway_error"
    assert error["code"] == code
    assert isinstance(error["retryable"], bool)
    assert isinstance(error["request_id"], str) and error["request_id"]


# --- error envelope shape -------------------------------------------------------


async def test_model_not_allowed_envelope_shape(gateway_client, chat_headers):
    body = {"model": "no-such-model-anywhere-conformance", "messages": [{"role": "user", "content": "hi"}]}

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 404
    _assert_envelope_shape(response.json(), code="model_not_allowed")


async def test_invalid_request_envelope_shape(gateway_client, chat_headers, chat_body_factory):
    body = chat_body_factory("hi", stream=True)

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 400
    _assert_envelope_shape(response.json(), code="invalid_request")


async def test_capability_unsupported_envelope_shape(limited_gateway_client, chat_headers, chat_body_factory):
    """Uses ``limited_gateway_client`` (a TEXT-only adapter -- see
    ``conftest.py``'s ``_TextOnlyAdapter``): the real reference adapter
    declares every gated capability, so this error is otherwise unreachable
    via HTTP against it. Requesting ``tools`` is enough to trigger the
    rejection; any of the four gated capabilities would do."""
    body = chat_body_factory(
        "hi", tools=[{"type": "function", "function": {"name": "x", "parameters": {"type": "object", "properties": {}}}}]
    )

    response = await limited_gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 422
    _assert_envelope_shape(response.json(), code="capability_unsupported")


# --- halts: finish_reason coverage ----------------------------------------------


@pytest.mark.parametrize(
    "kind,expected_finish_reason,expected_content",
    [
        ("truncated", "length", "MOCK:truncated"),
        ("screened", "content_filter", ""),
        ("complete", "stop", "MOCK:complete"),
    ],
)
async def test_trigger_halt_finish_reasons(gateway_client, chat_headers, chat_body_factory, kind, expected_finish_reason, expected_content):
    body = chat_body_factory(f"TRIGGER_HALT {kind}")

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == expected_finish_reason
    assert choice["message"]["content"] == expected_content


# --- faults: gateway error-code mapping -----------------------------------------


@pytest.mark.parametrize(
    "kind,expected_status,expected_code",
    [
        ("overloaded", 503, "upstream_unavailable"),
        ("screening", 400, "content_policy_rejected"),
        ("too_long", 400, "context_length_exceeded"),
    ],
)
async def test_trigger_fault_maps_to_gateway_error_code(
    gateway_client, chat_headers, chat_body_factory, kind, expected_status, expected_code
):
    body = chat_body_factory(f"TRIGGER_FAULT {kind}")

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


async def test_trigger_fault_throttle_hits_transport_429_short_circuit_not_adapter_mapping(gateway_client, chat_headers, chat_body_factory):
    """Documents, rather than claims more than it proves: this exercises the
    transport's 429 short-circuit, not the reference adapter's own
    "throttle" fault-kind branch (unreachable via HTTP -- see this module's
    docstring)."""
    body = chat_body_factory("TRIGGER_FAULT throttle")

    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_rate_limited"
