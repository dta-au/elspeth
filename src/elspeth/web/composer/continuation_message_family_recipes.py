"""Historical source locators for native and failure-schema message builders."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef


class NativeThinkingMessageRecipe(ContinuationValue):
    kind: Literal["native_thinking"] = "native_thinking"
    response: PrivateChunkRef
    argument_indexes: tuple[int, ...] = ()

    @model_validator(mode="after")
    def require_complete_source(self) -> NativeThinkingMessageRecipe:
        if self.response.codec != "composer.response.v1":
            raise ValueError("native thinking requires its original complete response")
        if self.argument_indexes != tuple(sorted(set(self.argument_indexes))) or any(index < 0 for index in self.argument_indexes):
            raise ValueError("native thinking argument positions must retain their original unique order")
        return self


class FailureSchemaMessageRecipe(ContinuationValue):
    kind: Literal["failure_schema"] = "failure_schema"
    response: PrivateChunkRef
    tool_call_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_complete_source(self) -> FailureSchemaMessageRecipe:
        if self.response.codec != "composer.response.v1":
            raise ValueError("failure schema requires its actual issued response")
        return self


class AttachmentResultMessageRecipe(ContinuationValue):
    """Original completed read result, never clearance for a later read."""

    kind: Literal["attachment_result"] = "attachment_result"
    response: PrivateChunkRef
    tool_call_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_complete_source(self) -> AttachmentResultMessageRecipe:
        if self.response.codec != "composer.response.v1":
            raise ValueError("attachment result requires its actual issued response")
        return self


class ToolCallArgumentsMessageRecipe(ContinuationValue):
    """Original ordinary assistant source and exact admitted argument positions."""

    kind: Literal["tool_call_arguments"] = "tool_call_arguments"
    response: PrivateChunkRef
    argument_indexes: tuple[int, ...] = ()

    @model_validator(mode="after")
    def require_complete_source(self) -> ToolCallArgumentsMessageRecipe:
        if self.response.codec != "composer.response.v1":
            raise ValueError("ordinary arguments require their original complete response")
        if self.argument_indexes != tuple(sorted(set(self.argument_indexes))) or any(index < 0 for index in self.argument_indexes):
            raise ValueError("ordinary argument positions must retain their original unique order")
        return self


class ToolResultMessageRecipe(ContinuationValue):
    """Original issued-call result selector; contains no saved prompt trust."""

    kind: Literal["tool_result"] = "tool_result"
    response: PrivateChunkRef
    tool_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_complete_source(self) -> ToolResultMessageRecipe:
        if self.response.codec != "composer.response.v1":
            raise ValueError("tool result requires its original complete paid response")
        return self


class ToolFeedbackMessageRecipe(ContinuationValue):
    """Issued feedback locator; the committed invocation supplies semantics."""

    kind: Literal["tool_feedback"] = "tool_feedback"
    response: PrivateChunkRef
    tool_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)
    source: Literal["arg_error", "advisor_success"]

    @model_validator(mode="after")
    def require_complete_source(self) -> ToolFeedbackMessageRecipe:
        if self.response.codec != "composer.response.v1":
            raise ValueError("issued feedback requires its original complete paid response")
        return self
