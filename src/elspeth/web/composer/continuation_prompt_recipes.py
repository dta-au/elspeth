"""Closed historical producer selectors; no saved prompt trust or live resources."""

from typing import Annotated, Literal

from pydantic import Field

from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_message_family_recipes import (
    AttachmentResultMessageRecipe,
    FailureSchemaMessageRecipe,
    NativeThinkingMessageRecipe,
    ToolCallArgumentsMessageRecipe,
)


class InitialMessageRecipe(ContinuationValue):
    kind: Literal["initial"] = "initial"
    index: int = Field(ge=0)


class RecoveryInstructionRecipe(ContinuationValue):
    kind: Literal["recovery_instruction"] = "recovery_instruction"


class ReplyInstructionRecipe(ContinuationValue):
    kind: Literal["reply_instruction"] = "reply_instruction"


class ToolArgumentsRecipe(ContinuationValue):
    kind: Literal["tool_arguments"] = "tool_arguments"


class PluginSchemaMessageRecipe(ContinuationValue):
    kind: Literal["plugin_schema"] = "plugin_schema"
    input_index: int = Field(ge=0)


ContinuationMessageRecipe = Annotated[
    InitialMessageRecipe
    | RecoveryInstructionRecipe
    | ReplyInstructionRecipe
    | ToolArgumentsRecipe
    | PluginSchemaMessageRecipe
    | NativeThinkingMessageRecipe
    | ToolCallArgumentsMessageRecipe
    | FailureSchemaMessageRecipe
    | AttachmentResultMessageRecipe,
    Field(discriminator="kind"),
]
