"""Structured-output modes: ``json_object`` and ``json_schema``.

When the target adapter does not declare the matching capability, the
request must be rejected with ``capability_unsupported`` (see
``test_capabilities.py`` for the general sweep); when it is declared, the
response content must be valid JSON in the shape the mock upstream is
documented to produce (see ``mock/upstream.py``'s module docstring).
"""

import json

_CHAT_URL = "/v1/chat/completions"


async def test_json_object_mode_produces_parseable_content(gateway_client, chat_headers, model_alias, declared_capabilities):
    body = {
        "model": model_alias,
        "messages": [{"role": "user", "content": "give me json"}],
        "response_format": {"type": "json_object"},
    }
    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    if "json_object" not in declared_capabilities:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "capability_unsupported"
        return

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)  # must not raise
    assert parsed == {"echo": "give me json"}


async def test_json_schema_mode_echoes_under_first_declared_property(gateway_client, chat_headers, model_alias, declared_capabilities):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    body = {
        "model": model_alias,
        "messages": [{"role": "user", "content": "what is the meaning of life"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer_schema", "schema": schema}},
    }
    response = await gateway_client.post(_CHAT_URL, json=body, headers=chat_headers)

    if "json_schema" not in declared_capabilities:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "capability_unsupported"
        return

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    parsed = json.loads(content)  # must not raise
    assert parsed == {"answer": "what is the meaning of life"}
