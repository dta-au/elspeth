"""Canonical SDK types and the closed capability/finish-reason vocabularies.

These models are the adapter SDK's public vocabulary: the core service and
adapters exchange requests/responses exclusively through them. Every model is
frozen and rejects unknown fields so that the wire contract between core and
adapters cannot silently drift.

This module must not import anything from ``elspeth_llm_gateway.core`` or from
the surrounding ELSPETH repository.
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


class Capability(StrEnum):
    """Closed vocabulary of adapter-declarable capabilities."""

    TEXT = "text"
    TOOLS = "tools"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"
    SEED = "seed"
    USAGE = "usage"


class FinishReason(StrEnum):
    """Closed vocabulary of canonical response finish reasons."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"


class CanonicalToolDef(BaseModel):
    """A tool definition offered to the model, in canonical form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str | None
    parameters_schema: dict


class CanonicalToolCall(BaseModel):
    """A single tool invocation requested by the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name: str
    arguments_json: str


class CanonicalMessage(BaseModel):
    """A single chat message in canonical form."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None
    tool_calls: tuple[CanonicalToolCall, ...] = ()
    tool_call_id: str | None = None


class ResponseFormatSpec(BaseModel):
    """A requested structured-output format.

    ``strict`` mirrors the inbound ``response_format.json_schema.strict``
    flag (``core.contract.JsonSchemaFormat``): ``None`` when the caller did
    not set it, otherwise the caller's own ``True``/``False``. Only
    meaningful when ``kind`` is ``"json_schema"`` -- adapters that support
    strict schema adherence use it to request that behavior from upstream;
    it is never populated for ``"json_object"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["json_object", "json_schema"]
    schema_name: str | None = None
    json_schema: dict | None = None
    strict: bool | None = None


class CanonicalRequest(BaseModel):
    """A canonical chat-completion request bound for an adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_target: dict
    model_alias: str
    messages: tuple[CanonicalMessage, ...]
    temperature: float | None
    seed: int | None
    max_tokens: int | None
    tools: tuple[CanonicalToolDef, ...] = ()
    tool_choice: str | None = None
    tool_choice_function: str | None = None
    response_format: ResponseFormatSpec | None = None


class CanonicalUsage(BaseModel):
    """Token usage accounting for a canonical response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @model_validator(mode="after")
    def _check_arithmetic(self) -> Self:
        if self.prompt_tokens < 0 or self.completion_tokens < 0 or self.total_tokens < 0:
            raise ValueError("usage token counts must be >= 0")
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
        return self


class CanonicalResponse(BaseModel):
    """A canonical chat-completion response from an adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None
    tool_calls: tuple[CanonicalToolCall, ...] = ()
    finish_reason: FinishReason
    usage: CanonicalUsage | None = None

    @model_validator(mode="after")
    def _check_content_xor(self) -> Self:
        has_text = self.text is not None
        has_tool_calls = len(self.tool_calls) > 0
        if has_text == has_tool_calls:
            raise ValueError("exactly one of text or tool_calls must be set")
        return self
