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
- ``"TRIGGER_HALT <kind>"`` -> a *success* body (HTTP 200) whose ``halt``
  is exactly ``<kind>`` (``truncated``, ``screened``, or ``complete``),
  reaching the three halt values ``TRIGGER_FAULT``/the default text-echo
  path don't:
    - ``truncated`` -> ``result.text = "MOCK:truncated"``, halt
      ``"truncated"`` -> the adapter maps this to ``FinishReason.LENGTH``.
    - ``screened`` -> **no** ``result.text`` at all (the salvage-less
      branch the reference adapter's docstring calls out: a halt that
      salvages no text still needs ``CanonicalResponse.text=""``, never
      ``None``) -> the adapter maps this to
      ``FinishReason.CONTENT_FILTER``.
    - ``complete`` -> ``result.text = "MOCK:complete"``, halt
      ``"complete"`` (accepted for symmetry with the other two).
  A ``<kind>`` outside this set is a malformed trigger (see below).
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

Two notes for whoever writes the Task 13 conformance kit against this mock:

1. ``TRIGGER_FAULT screening`` (a 400 *fault* body -> the gateway's
   ``classify_error`` maps it to ``content_policy_rejected``) is a
   *different code path* from ``TRIGGER_HALT screened`` (a 200 *success*
   body with halt ``"screened"`` -> ``FinishReason.CONTENT_FILTER``). They
   share the word "screen[ed]" but exercise different adapter methods
   (``classify_error`` vs ``parse_success``) and land on different
   ``elspeth_llm_gateway.sdk.types`` enums (``ErrorClassification.code`` vs
   ``FinishReason``). Do not treat one as covering the other.
2. ``TRIGGER_FAULT throttle`` returns HTTP 429 with a fault body, but the
   gateway's transport short-circuits *any* HTTP 429 into
   ``upstream_rate_limited`` before the adapter's ``classify_error`` ever
   runs (see the "Design note on the throttle -> upstream_rate_limited
   mapping" section of ``reference.adapter``'s module docstring). A
   conformance test that fires this trigger exercises the transport's 429
   short-circuit, *not* the adapter's ``"throttle"`` fault-kind branch --
   that branch is reachable only via a direct unit test of
   ``classify_error``, never through this mock over HTTP.

A malformed trigger -- ``"USE_TOOL <op>"`` with no JSON payload at all, or
a payload that isn't valid JSON -- returns a clean, deterministic HTTP 400
with ``{"error": "malformed_trigger"}`` rather than crashing. This shape is
a mock-only diagnostic (it is not part of the fictional
``reference_v1_invoke`` fault contract -- it is not a ``{"fault": {...}}``
body and carries no ``kind``); it exists purely so a malformed trigger in a
test fixture fails loudly and cleanly instead of surfacing as an opaque
500.
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

_VALID_HALT_TRIGGER_KINDS = frozenset({"truncated", "screened", "complete"})

# A single, fixed call ref: deterministic, and this mock never issues more
# than one invocation per USE_TOOL trigger.
_MOCK_TOOL_CALL_REF = "mock-call-ref-1"


def _malformed_trigger_response() -> JSONResponse:
    """A clean, deterministic 400 for any unparsable trigger text.

    See the module docstring's final paragraph: this shape is a mock-only
    diagnostic, not part of the fictional ``reference_v1_invoke`` fault
    contract.
    """
    return JSONResponse(status_code=400, content={"error": "malformed_trigger"})


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
            trigger_parts = last_text.split(" ", 2)
            if len(trigger_parts) < 3:
                return _malformed_trigger_response()
            _, operation, payload_json = trigger_parts
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                return _malformed_trigger_response()
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
            trigger_parts = last_text.split(" ", 1)
            if len(trigger_parts) < 2:
                return _malformed_trigger_response()
            _, kind = trigger_parts
            status = _FAULT_STATUS.get(kind, 400)
            return JSONResponse(status_code=status, content={"fault": {"kind": kind}})

        if last_text.startswith("TRIGGER_HALT "):
            trigger_parts = last_text.split(" ", 1)
            if len(trigger_parts) < 2:
                return _malformed_trigger_response()
            _, kind = trigger_parts
            if kind not in _VALID_HALT_TRIGGER_KINDS:
                return _malformed_trigger_response()
            if kind == "screened":
                # The salvage-less branch: no result.text at all. The
                # reference adapter's parse_success treats a missing
                # "text" under halt="screened" as text="" (never None),
                # never a KeyError -- see reference.adapter's docstring.
                return JSONResponse(
                    status_code=200,
                    content={"halt": "screened", "accounting": {"input_units": input_units, "output_units": 0}},
                )
            text = f"MOCK:{kind}"
            return JSONResponse(
                status_code=200,
                content={
                    "result": {"text": text},
                    "halt": kind,
                    "accounting": {"input_units": input_units, "output_units": len(text) // 4},
                },
            )

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
