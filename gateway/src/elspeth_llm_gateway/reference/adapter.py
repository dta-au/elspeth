"""The fictional ``reference_v1_invoke`` adapter.

This adapter targets a wire schema that does not correspond to any real LLM
provider. It exists as the exemplar every real agency adapter is copied
from, so its shape is deliberately, visibly non-agency: no vendor-specific
field names, no vendor-specific quirks to accidentally imitate. Read this
module alongside ``elspeth_llm_gateway.sdk.protocol.AdapterProtocol`` — every
place a real adapter's mapping will differ from this one is marked below
with a ``# TRANSLATION POINT`` comment.

The fictional upstream schema
------------------------------

Request body POSTed to ``v1/invoke``::

    {
        "conversation": [
            {"speaker": "<role>", "text": "<content>"},
            {"speaker": "assistant", "operations": [
                {"ref": "<call id>", "operation": "<tool name>", "payload": {...}}
            ]},
            {"speaker": "tool", "text": "<result>", "operation_ref": "<call id>"}
        ],
        "generation": {
            "target": "<model_target['target']>",
            "temperature": <float>?,
            "seed": <int>?,
            "max_output": <int>?
        },
        "directives": [
            {"operation": "<name>", "about": "<description>", "payload_schema": {...}}
        ]?,
        "format": {"kind": "object"} | {"kind": "schema", "schema": {...}}?
    }

A conversation entry carries ``text`` when the canonical message has
content, ``operations`` when it is an assistant message requesting tool
calls, and ``operation_ref`` when it is a tool-result message answering a
prior call. ``directives`` is present only when the request declares tools;
``format`` is present only when the request declares a response format.

Success response body::

    {
        "result": {"text": "..."} | {"invocations": [
            {"ref": "<call id>", "operation": "<name>", "payload": {...}}
        ]},
        "halt": "complete" | "truncated" | "operations" | "screened",
        "accounting": {"input_units": <int>, "output_units": <int>}?
    }

Failure response body (paired with a non-2xx HTTP status)::

    {"fault": {"kind": "throttle" | "overloaded" | "screening" | "too_long" | ...}}

Design note on the ``throttle`` -> ``upstream_rate_limited`` mapping: the
gateway's transport short-circuits any HTTP 429 before an adapter's
``classify_error`` ever runs, so this branch is not reachable via a real
429 response today. It is kept anyway for two reasons: a fictional upstream
could still describe a throttle fault inside a 2xx-status body (robustness
against that shape), and this module is the template every agency adapter
copies — the mapping documents the expected shape even where this
particular reference upstream cannot currently trigger it.
"""

import json

from elspeth_llm_gateway.sdk.protocol import (
    AdapterDescriptor,
    ErrorClassification,
    InvokePlan,
    UpstreamFailure,
)
from elspeth_llm_gateway.sdk.types import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalToolCall,
    CanonicalUsage,
    Capability,
    FinishReason,
)

_HALT_TO_FINISH_REASON: dict[str, FinishReason] = {
    "complete": FinishReason.STOP,
    "truncated": FinishReason.LENGTH,
    "operations": FinishReason.TOOL_CALLS,
    "screened": FinishReason.CONTENT_FILTER,
}

# TRANSLATION POINT: a real agency's fault-kind vocabulary will differ from
# this fictional one; only the *codes* on the right (from
# ``elspeth_llm_gateway.sdk.protocol.CLASSIFIABLE_CODES``) are fixed.
_FAULT_KIND_TO_CLASSIFICATION: dict[str, ErrorClassification] = {
    "throttle": ErrorClassification(code="upstream_rate_limited", retryable=True),
    "overloaded": ErrorClassification(code="upstream_unavailable", retryable=True),
    "screening": ErrorClassification(code="content_policy_rejected", retryable=False),
    "too_long": ErrorClassification(code="context_length_exceeded", retryable=False),
}

_FALLBACK_CLASSIFICATION = ErrorClassification(code="upstream_response_invalid", retryable=False)


def _conversation_entry(message: CanonicalMessage) -> dict:
    # TRANSLATION POINT: a real agency's message-entry shape (role names,
    # field names for text/tool-calls/tool-results) will differ from this
    # fictional speaker/text/operations/operation_ref shape.
    entry: dict = {"speaker": message.role}
    if message.content is not None:
        entry["text"] = message.content
    if message.tool_calls:
        entry["operations"] = [
            {"ref": call.call_id, "operation": call.name, "payload": json.loads(call.arguments_json)} for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        entry["operation_ref"] = message.tool_call_id
    return entry


class ReferenceV1InvokeAdapter:
    """Fictional reference adapter for the ``reference_v1_invoke`` schema."""

    def descriptor(self) -> AdapterDescriptor:
        return AdapterDescriptor(
            name="reference_v1_invoke",
            version="0.1.0",
            adapter_api_major=1,
            capabilities=frozenset(
                {
                    Capability.TEXT,
                    Capability.TOOLS,
                    Capability.JSON_OBJECT,
                    Capability.JSON_SCHEMA,
                    Capability.SEED,
                    Capability.USAGE,
                }
            ),
        )

    def validate_configuration(self, options: dict) -> None:
        # TRANSLATION POINT: a real agency adapter typically accepts
        # deployment-specific options here (e.g. a region or deployment id).
        # This reference adapter accepts none.
        if options:
            raise ValueError(f"reference_v1_invoke accepts no configuration options, got: {sorted(options)}")

    def build_invoke(self, request: CanonicalRequest) -> InvokePlan:
        conversation = [_conversation_entry(message) for message in request.messages]

        # TRANSLATION POINT: a real agency's generation-parameters field
        # name(s) and shape will differ from this fictional
        # target/temperature/seed/max_output shape.
        generation: dict = {"target": request.model_target["target"]}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.seed is not None:
            generation["seed"] = request.seed
        if request.max_tokens is not None:
            generation["max_output"] = request.max_tokens

        body: dict = {"conversation": conversation, "generation": generation}

        if request.tools:
            # TRANSLATION POINT: a real agency's tool-declaration field
            # names will differ from this fictional
            # operation/about/payload_schema shape.
            body["directives"] = [
                {"operation": tool.name, "about": tool.description, "payload_schema": tool.parameters_schema} for tool in request.tools
            ]

        if request.response_format is not None:
            # TRANSLATION POINT: a real agency's structured-output request
            # shape will differ from this fictional kind/schema shape.
            if request.response_format.kind == "json_object":
                body["format"] = {"kind": "object"}
            else:
                body["format"] = {"kind": "schema", "schema": request.response_format.json_schema}

        return InvokePlan(path="v1/invoke", body=body)

    def parse_success(self, body: dict) -> CanonicalResponse:
        halt = body["halt"]
        if halt not in _HALT_TO_FINISH_REASON:
            raise ValueError(f"reference_v1_invoke: unknown halt value {halt!r}")
        finish_reason = _HALT_TO_FINISH_REASON[halt]

        result: dict = body["result"] if "result" in body else {}

        if halt == "operations":
            # TRANSLATION POINT: a real agency's tool-invocation result
            # shape will differ from this fictional invocations/ref/
            # operation/payload shape.
            text = None
            tool_calls = tuple(
                CanonicalToolCall(
                    call_id=invocation["ref"],
                    name=invocation["operation"],
                    arguments_json=json.dumps(invocation["payload"]),
                )
                for invocation in result["invocations"]
            )
        else:
            tool_calls = ()
            if halt == "screened" and "text" not in result:
                # RULING: a halt that salvages no text still needs
                # text-present (never None) on CanonicalResponse.
                text = ""
            else:
                # Missing "text" here is a malformed upstream contract for a
                # non-screened halt: fail closed with the natural KeyError
                # rather than fabricating a default.
                text = result["text"]

        usage: CanonicalUsage | None = None
        if "accounting" in body:
            accounting = body["accounting"]
            input_units = accounting["input_units"]
            output_units = accounting["output_units"]
            usage = CanonicalUsage(
                prompt_tokens=input_units,
                completion_tokens=output_units,
                total_tokens=input_units + output_units,
            )

        return CanonicalResponse(text=text, tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)

    def classify_error(self, failure: UpstreamFailure) -> ErrorClassification:
        if failure.body is None or "fault" not in failure.body:
            return _FALLBACK_CLASSIFICATION
        fault = failure.body["fault"]
        if "kind" not in fault:
            return _FALLBACK_CLASSIFICATION
        kind = fault["kind"]
        if kind not in _FAULT_KIND_TO_CLASSIFICATION:
            return _FALLBACK_CLASSIFICATION
        return _FAULT_KIND_TO_CLASSIFICATION[kind]
