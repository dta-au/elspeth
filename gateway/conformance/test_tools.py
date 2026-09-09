"""Tool call + tool result round trip: end-to-end coverage of the ``tools``
capability.

When the target adapter does not declare ``Capability.TOOLS``, the first
request must be rejected with ``capability_unsupported`` (see
``test_capabilities.py`` for the general declared/undeclared sweep); this
file additionally proves the full round trip when it IS declared: the
gateway must both deliver a tool call the client can act on, and accept a
follow-up turn carrying that tool call plus its result and produce a normal
completion.
"""

import json

_CHAT_URL = "/v1/chat/completions"

_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "search_catalog",
        "description": "Search the product catalog",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        },
    },
}


async def test_tool_call_and_tool_result_round_trip(gateway_client, chat_headers, model_alias, declared_capabilities):
    payload = {"query": "widgets", "limit": 3}
    use_tool_text = f"USE_TOOL search_catalog {json.dumps(payload)}"

    first_body = {
        "model": model_alias,
        "messages": [{"role": "user", "content": use_tool_text}],
        "tools": [_TOOL_DEF],
    }
    first = await gateway_client.post(_CHAT_URL, json=first_body, headers=chat_headers)

    if "tools" not in declared_capabilities:
        assert first.status_code == 422
        assert first.json()["error"]["code"] == "capability_unsupported"
        return

    assert first.status_code == 200
    first_choice = first.json()["choices"][0]
    assert first_choice["finish_reason"] == "tool_calls"
    first_message = first_choice["message"]
    assert first_message["content"] is None

    tool_calls = first_message["tool_calls"]
    assert len(tool_calls) == 1
    call = tool_calls[0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "search_catalog"
    assert json.loads(call["function"]["arguments"]) == payload

    second_body = {
        "model": model_alias,
        "messages": [
            {"role": "user", "content": use_tool_text},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call["id"], "content": "3 widgets in stock"},
        ],
        "tools": [_TOOL_DEF],
    }
    second = await gateway_client.post(_CHAT_URL, json=second_body, headers=chat_headers)

    assert second.status_code == 200
    second_choice = second.json()["choices"][0]
    assert second_choice["finish_reason"] == "stop"
    assert second_choice["message"]["content"] == "MOCK:3 widgets in stock"


async def test_malformed_tool_call_arguments_rejected_before_upstream(gateway_client, chat_headers, model_alias):
    """A tool call whose ``arguments`` string is not valid JSON must be
    rejected as ``invalid_request`` at request-validation time -- before
    the capability check, the adapter, or the upstream ever run -- so this
    holds regardless of whether the target adapter declares ``tools``."""
    body = {
        "model": model_alias,
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_catalog", "arguments": "NOT JSON"}}],
            }
        ],
        "tools": [_TOOL_DEF],
    }
    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
