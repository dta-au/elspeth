"""The strict inbound Chat Completions subset and the response builder.

This is the wire boundary: every model here rejects unknown fields
(``extra="forbid"``) so that a client cannot silently smuggle an unsupported
OpenAI feature (``stream``, ``n``, ``logprobs``, ...) past validation. Only
the fields declared below are accepted; everything else is a hard rejection,
not a warning.

``Bounds`` and ``bounds_check`` enforce the gateway's configured limits
(message count, tool count, string length, schema size/depth, temperature
range, ``max_tokens`` cap) after pydantic's structural validation has
already passed. A bounds failure always raises
``GatewayError(GatewayErrorCode.INVALID_REQUEST)`` — never a raw
``ValidationError`` or free-text message.

``build_completion_response`` renders a ``CanonicalResponse`` into the
OpenAI Chat Completions response shape.
"""

import json
import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.sdk.types import CanonicalResponse

_VALID_STRING_TOOL_CHOICES = frozenset({"auto", "none", "required"})


class ChatFunctionDef(BaseModel):
    """A single function tool's declared name, description, and parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str | None = None
    parameters: dict | None = None


class ChatTool(BaseModel):
    """A single tool offered to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function"]
    function: ChatFunctionDef


class ChatToolCallFunction(BaseModel):
    """The function name and JSON-encoded arguments of a requested tool call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    arguments: str


class ChatToolCall(BaseModel):
    """A single tool call requested by an assistant message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    type: Literal["function"]
    function: ChatToolCallFunction


class ChatMessage(BaseModel):
    """A single chat message in the inbound Chat Completions subset.

    Fail-closed cross-field rules: ``tool_calls`` may only be set on an
    ``assistant`` message; ``tool_call_id`` is required exactly when the
    role is ``tool``; ``content`` must be an actual string (not ``None``)
    for every role except ``assistant``, where a tool-calls-only message
    may omit it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ChatToolCall] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check_tool_calls_only_on_assistant(self) -> Self:
        if self.role != "assistant" and self.tool_calls is not None:
            raise ValueError("tool_calls is only permitted on an assistant message")
        return self

    @model_validator(mode="after")
    def _check_tool_call_id_matches_role(self) -> Self:
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("tool_call_id is required on a tool message")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("tool_call_id is only permitted on a tool message")
        return self

    @model_validator(mode="after")
    def _check_content_is_string_for_non_assistant(self) -> Self:
        if self.role != "assistant" and self.content is None:
            raise ValueError("content must be a string for non-assistant roles")
        return self


class NamedToolChoiceFunction(BaseModel):
    """The declared function name a named ``tool_choice`` selects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str


class NamedToolChoice(BaseModel):
    """A ``tool_choice`` that names one explicit function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["function"]
    function: NamedToolChoiceFunction


class JsonSchemaFormat(BaseModel):
    """A named JSON Schema for structured-output responses.

    ``schema_definition`` is aliased to the wire key ``schema``. It cannot
    be named literally ``schema`` on the model: that shadows
    ``BaseModel.schema`` (pydantic v1's deprecated compatibility method)
    and emits a ``UserWarning`` on every access. ``populate_by_name=True``
    lets callers also construct this model with the Python-side keyword
    ``schema_definition=...``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    name: str
    schema_definition: dict = Field(alias="schema")
    strict: bool | None = None


class ResponseFormat(BaseModel):
    """A requested response format: plain JSON object or a named JSON Schema."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["json_object", "json_schema"]
    json_schema: JsonSchemaFormat | None = None

    @model_validator(mode="after")
    def _check_json_schema_required_iff_json_schema_type(self) -> Self:
        if self.type == "json_schema" and self.json_schema is None:
            raise ValueError("json_schema is required when type is 'json_schema'")
        if self.type == "json_object" and self.json_schema is not None:
            raise ValueError("json_schema must not be set when type is 'json_object'")
        return self


class ChatRequest(BaseModel):
    """The strict inbound Chat Completions request subset.

    Anything outside this shape — ``stream``, ``n``, ``logprobs``, or any
    other unsupported OpenAI field, at any nesting level — is rejected by
    ``extra="forbid"`` on every model in this chain, not merely ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    seed: int | None = None
    max_tokens: int | None = None
    tools: list[ChatTool] | None = None
    tool_choice: str | NamedToolChoice | None = None
    response_format: ResponseFormat | None = None

    @model_validator(mode="after")
    def _check_messages_non_empty(self) -> Self:
        if not self.messages:
            raise ValueError("messages must not be empty")
        return self

    @model_validator(mode="after")
    def _check_string_tool_choice(self) -> Self:
        if isinstance(self.tool_choice, str) and self.tool_choice not in _VALID_STRING_TOOL_CHOICES:
            raise ValueError(f"tool_choice must be one of {sorted(_VALID_STRING_TOOL_CHOICES)}")
        return self

    @model_validator(mode="after")
    def _check_temperature_finite(self) -> Self:
        if self.temperature is not None and not _is_finite(self.temperature):
            raise ValueError("temperature must be finite")
        return self

    @model_validator(mode="after")
    def _check_named_tool_choice_references_declared_tool(self) -> Self:
        if isinstance(self.tool_choice, NamedToolChoice):
            declared_names = {tool.function.name for tool in (self.tools or [])}
            if self.tool_choice.function.name not in declared_names:
                raise ValueError(f"tool_choice names function {self.tool_choice.function.name!r}, which is not declared in tools")
        return self


def _is_finite(value: float) -> bool:
    return math.isfinite(value)


class Bounds(BaseModel):
    """The gateway's configured request-size and value-range limits.

    Declared here so both this module's ``bounds_check`` and the runtime
    configuration loader (a later task) share one definition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_messages: int
    max_tools: int
    max_string_chars: int
    max_schema_bytes: int
    max_schema_depth: int
    temperature_min: float = 0.0
    temperature_max: float = 2.0
    max_max_tokens: int = 32768


def _schema_exceeds_depth(root: object, max_depth: int) -> bool:
    """Whether a JSON-Schema-shaped dict/list tree nests deeper than ``max_depth``.

    Walked iteratively with an explicit stack (never recursion): a client
    controls this structure, and an adversarially deep — but individually
    tiny — nesting must not be able to blow the interpreter's call stack
    before the bound it is supposed to be checked against is even applied.
    """
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            return True
        if isinstance(node, dict):
            stack.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth + 1) for value in node)
    return False


def _check_schema_bounds(schema: dict, bounds: Bounds) -> None:
    """Enforce ``max_schema_depth``/``max_schema_bytes`` on a client-supplied schema.

    Depth is checked first, with the iterative (non-recursive) walk above.
    ``json.dumps`` — used below to measure serialized size — recurses in
    Python once per nesting level, so it must never run before nesting
    depth is known to be within bounds: an adversarially deep, individually
    tiny payload would otherwise blow the interpreter's recursion limit
    during the byte-size check itself, escaping as an unhandled
    ``RecursionError`` instead of the intended ``GatewayError``.
    """
    if _schema_exceeds_depth(schema, bounds.max_schema_depth):
        raise GatewayError(GatewayErrorCode.INVALID_REQUEST)
    schema_bytes = len(json.dumps(schema).encode("utf-8"))
    if schema_bytes > bounds.max_schema_bytes:
        raise GatewayError(GatewayErrorCode.INVALID_REQUEST)


def _iter_message_strings(message: ChatMessage) -> list[str]:
    strings: list[str] = []
    if message.content is not None:
        strings.append(message.content)
    if message.tool_calls:
        for tool_call in message.tool_calls:
            strings.append(tool_call.function.arguments)
    return strings


def bounds_check(request: ChatRequest, bounds: Bounds) -> None:
    """Enforce the gateway's configured limits on an already-valid request.

    Raises ``GatewayError(GatewayErrorCode.INVALID_REQUEST)`` (never a raw
    exception with request-derived text) on the first violated bound:
    message count, per-message string length, tool count, structured-output
    schema size/depth, temperature range, or the ``max_tokens`` cap.
    """
    if len(request.messages) > bounds.max_messages:
        raise GatewayError(GatewayErrorCode.INVALID_REQUEST)

    for message in request.messages:
        for value in _iter_message_strings(message):
            if len(value) > bounds.max_string_chars:
                raise GatewayError(GatewayErrorCode.INVALID_REQUEST)

    if request.tools is not None:
        if len(request.tools) > bounds.max_tools:
            raise GatewayError(GatewayErrorCode.INVALID_REQUEST)
        for tool in request.tools:
            if tool.function.parameters is not None:
                _check_schema_bounds(tool.function.parameters, bounds)

    if request.response_format is not None and request.response_format.json_schema is not None:
        _check_schema_bounds(request.response_format.json_schema.schema_definition, bounds)

    if request.temperature is not None and not (bounds.temperature_min <= request.temperature <= bounds.temperature_max):
        raise GatewayError(GatewayErrorCode.INVALID_REQUEST)

    if request.max_tokens is not None and (request.max_tokens <= 0 or request.max_tokens > bounds.max_max_tokens):
        raise GatewayError(GatewayErrorCode.INVALID_REQUEST)


def build_completion_response(*, response_id: str, created: int, model_alias: str, canonical: CanonicalResponse) -> dict:
    """Render a ``CanonicalResponse`` into the OpenAI Chat Completions shape.

    Always exactly one choice at index 0. ``usage`` is present in the
    returned dict only when ``canonical.usage`` is not ``None``.
    """
    message: dict = {"role": "assistant", "content": canonical.text}
    if canonical.tool_calls:
        message["tool_calls"] = [
            {
                "id": tool_call.call_id,
                "type": "function",
                "function": {"name": tool_call.name, "arguments": tool_call.arguments_json},
            }
            for tool_call in canonical.tool_calls
        ]

    response: dict = {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model_alias,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": canonical.finish_reason.value,
            }
        ],
    }

    if canonical.usage is not None:
        response["usage"] = {
            "prompt_tokens": canonical.usage.prompt_tokens,
            "completion_tokens": canonical.usage.completion_tokens,
            "total_tokens": canonical.usage.total_tokens,
        }

    return response
