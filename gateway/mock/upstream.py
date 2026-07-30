"""Deterministic mock upstream for the fictional ``reference_v1_invoke`` schema.

Mirrors the wire schema documented in
``elspeth_llm_gateway.reference.adapter`` -- read that module's docstring
for the full request/response shape this mock must stay byte-compatible
with. Every response is a pure function of the request body: no
randomness, no wall-clock dependence, so the same request always produces
the same reply.

Behavior is keyed on the *last* ``conversation`` entry's ``text``:

- ``"USE_TOOL <op> <json>"`` -> an ``invocations`` response, halt
  ``"operations"``.
- ``"TRIGGER_FAULT <kind>"`` -> the matching fault body and HTTP status
  (``throttle`` 429, ``overloaded`` 503, ``screening``/``too_long`` 400).
- otherwise, keyed on the request's ``format.kind``:
    - ``"schema"`` -> the last text, JSON-encoded under the response
      schema's first declared property name (``"echo"`` when the schema
      declares none): ``{first_property: last_text}``, *not*
      ``{first_property: {"echo": last_text}}`` -- the flat form is the one
      that can actually validate against a schema whose first property is
      typed as a string (e.g. ``{"answer": {"type": "string"}}``); nesting
      an object under it would fail that property's own type check.
    - ``"object"`` -> ``json.dumps({"echo": <last text>})``, so
      ``json_object`` conformance can assert ``json.loads`` succeeds
      against the reply text.
    - absent -> ``"MOCK:" + <last text>``.

``directives`` / ``directive_policy`` (present only when the request
declares tools / sets ``tool_choice``) are accepted but otherwise ignored --
this mock's behavior never depends on them.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_DEFAULT_BEARER_PREFIX = "mock-token-"
_BEARER_PREFIX = "Bearer "

_FAULT_STATUS: dict[str, int] = {
    "throttle": 429,
    "overloaded": 503,
    "screening": 400,
    "too_long": 400,
}

# A single, fixed call ref: deterministic, and this mock never issues more
# than one invocation per USE_TOOL trigger.
_MOCK_TOOL_CALL_REF = "mock-call-ref-1"


def _last_text(conversation: list[dict]) -> str:
    if not conversation:
        return ""
    return conversation[-1].get("text") or ""


def _total_conversation_chars(conversation: list[dict]) -> int:
    return sum(len(entry.get("text") or "") for entry in conversation)


def create_mock_upstream_app(*, require_bearer_prefix: str = _DEFAULT_BEARER_PREFIX) -> FastAPI:
    """Build the mock fictional-upstream app; ``POST /v1/invoke`` is its only route."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/invoke")
    async def invoke(request: Request) -> JSONResponse:
        auth_header = request.headers.get("authorization")
        if auth_header is None or not auth_header.startswith(_BEARER_PREFIX):
            return JSONResponse(status_code=401, content={"error": "invalid_token"})
        token = auth_header[len(_BEARER_PREFIX) :]
        if not token.startswith(require_bearer_prefix):
            return JSONResponse(status_code=401, content={"error": "invalid_token"})

        body = await request.json()
        conversation = body.get("conversation") or []
        last_text = _last_text(conversation)
        input_units = _total_conversation_chars(conversation) // 4

        if last_text.startswith("USE_TOOL "):
            _, operation, payload_json = last_text.split(" ", 2)
            payload = json.loads(payload_json)
            reply = json.dumps(payload)
            return JSONResponse(
                status_code=200,
                content={
                    "result": {"invocations": [{"ref": _MOCK_TOOL_CALL_REF, "operation": operation, "payload": payload}]},
                    "halt": "operations",
                    "accounting": {"input_units": input_units, "output_units": len(reply) // 4},
                },
            )

        if last_text.startswith("TRIGGER_FAULT "):
            _, kind = last_text.split(" ", 1)
            status = _FAULT_STATUS.get(kind, 400)
            return JSONResponse(status_code=status, content={"fault": {"kind": kind}})

        response_format = body.get("format")
        if response_format is not None and response_format.get("kind") == "schema":
            schema = response_format.get("schema") or {}
            properties = schema.get("properties") or {}
            first_property = next(iter(properties), "echo")
            text = json.dumps({first_property: last_text})
        elif response_format is not None and response_format.get("kind") == "object":
            text = json.dumps({"echo": last_text})
        else:
            text = "MOCK:" + last_text

        return JSONResponse(
            status_code=200,
            content={
                "result": {"text": text},
                "halt": "complete",
                "accounting": {"input_units": input_units, "output_units": len(text) // 4},
            },
        )

    return app
