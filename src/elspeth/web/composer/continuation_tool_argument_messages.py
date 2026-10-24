"""Exact source-bound ordinary assistant messages from actual B2 admission.

The recipe retains source and untrusted argument positions, never saved trust.
Current SQL source admission remains with CurrentContinuationMessageFamilies.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardToolArgumentParts,
)
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response
from elspeth.web.composer.continuation_message_family_builders import (
    capture_native_message_recipe,
    family_message_wire,
    native_assistant_message,
    retained_family_wire,
)
from elspeth.web.composer.continuation_message_family_recipes import NativeThinkingMessageRecipe, ToolCallArgumentsMessageRecipe
from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json
from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts
from elspeth.web.composer.provider_thinking import native_thinking_blocks


def _indexes(message: Mapping[str, Any]) -> tuple[int, ...]:
    if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY not in message:
        return ()
    entries = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
    if type(entries) is not tuple or not entries or any(type(entry) is not PromptGuardToolArgumentParts for entry in entries):
        raise AuditIntegrityError("ordinary message lost its actual argument partitions")
    return tuple(entry.tool_call_index for entry in entries)


def _ordinary_message(wire: dict[str, Any], indexes: tuple[int, ...]) -> dict[str, Any]:
    if set(wire) != {"role", "content", "tool_calls"} or wire["role"] != "assistant":
        raise AuditIntegrityError("ordinary message changed its complete assistant envelope")
    calls = wire["tool_calls"]
    if type(calls) is not list or not calls:
        raise AuditIntegrityError("ordinary message lost its issued call cohort")
    if (
        type(indexes) is not tuple
        or any(type(index) is not int or not 0 <= index < len(calls) for index in indexes)
        or indexes != tuple(sorted(set(indexes)))
    ):
        raise AuditIntegrityError("ordinary message lost its unique admitted argument positions")
    for call in calls:
        if (
            type(call) is not dict
            or set(call) != {"id", "type", "function"}
            or type(call["id"]) is not str
            or not call["id"]
            or call["type"] != "function"
            or type(call["function"]) is not dict
            or set(call["function"]) != {"name", "arguments"}
            or type(call["function"]["name"]) is not str
            or not call["function"]["name"]
            or type(call["function"]["arguments"]) is not str
        ):
            raise AuditIntegrityError("ordinary message changed its complete function envelope")
    result = dict(wire)
    if indexes:
        result[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = tuple(
            PromptGuardToolArgumentParts(index, json_argument_content_parts(calls[index]["function"]["arguments"])) for index in indexes
        )
    return result


def require_ordinary_argument_parts(message: Mapping[str, Any], indexes: tuple[int, ...]) -> None:
    wire = retained_family_wire(family_message_wire(message))
    rebuilt = _ordinary_message(wire, indexes)
    if any(
        key in message for key in (PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY, PROMPT_GUARD_THINKING_PARTS_KEY)
    ) or message.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY) != rebuilt.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY):
        raise AuditIntegrityError("ordinary message changed its actual all-untrusted argument provenance")


def _require_ordinary_source(
    completion: _AdmittedLLMCompletion,
    wire: dict[str, Any],
    argument_indexes: tuple[int, ...],
) -> None:
    expected = retained_family_wire(family_message_wire(native_assistant_message(completion)))
    native_blocks = completion.provider_metadata.thinking_blocks
    if native_blocks is not None and native_thinking_blocks(native_blocks):
        raise AuditIntegrityError("ordinary message cannot discard actual native thinking")
    actual = retained_family_wire(encode_transcript_object_json(wire))
    _ordinary_message(actual, ())
    if len(actual["tool_calls"]) != len(expected["tool_calls"]):
        raise AuditIntegrityError("ordinary message changed its actual issued call cohort")
    for index, (current, original) in enumerate(zip(actual["tool_calls"], expected["tool_calls"], strict=True)):
        if index not in argument_indexes and current["function"]["arguments"] != original["function"]["arguments"]:
            raise AuditIntegrityError("ordinary message changed unadmitted original argument bytes")
        current["function"]["arguments"] = original["function"]["arguments"]
    if encode_transcript_object_json(actual) != encode_transcript_object_json(expected):
        raise AuditIntegrityError("ordinary message changed its original provider envelope or call identity")


def capture_tool_call_message_recipe(
    *,
    response: PrivateChunk,
    completion: _AdmittedLLMCompletion,
    message: Mapping[str, Any],
) -> NativeThinkingMessageRecipe | ToolCallArgumentsMessageRecipe:
    if type(response) is not PrivateChunk or type(completion) is not _AdmittedLLMCompletion or type(message) is not dict:
        raise TypeError("tool message capture requires its actual response/completion/message owners")
    if decode_continuation_response(response) != completion:
        raise AuditIntegrityError("tool message changed its complete original response")
    native_blocks = completion.provider_metadata.thinking_blocks
    if native_blocks is not None and native_thinking_blocks(native_blocks):
        return capture_native_message_recipe(response=response, completion=completion, message=message)
    indexes = _indexes(message)
    require_ordinary_argument_parts(message, indexes)
    _require_ordinary_source(completion, retained_family_wire(family_message_wire(message)), indexes)
    return ToolCallArgumentsMessageRecipe(response=response.reference, argument_indexes=indexes)


def require_tool_call_message_refresh(
    *,
    previous: object,
    previous_wire: str | None,
    recipe: NativeThinkingMessageRecipe | ToolCallArgumentsMessageRecipe,
    completion: _AdmittedLLMCompletion,
    message: Mapping[str, Any],
    tool_call_id: str | None,
) -> None:
    if previous is None:
        if (
            previous_wire is not None
            or tool_call_id is not None
            or recipe.argument_indexes
            or family_message_wire(message) != family_message_wire(native_assistant_message(completion))
        ):
            raise AuditIntegrityError("tool message initialization differs from the actual original producer")
        return
    if (
        type(previous) not in (NativeThinkingMessageRecipe, ToolCallArgumentsMessageRecipe)
        or type(previous) is not type(recipe)
        or previous_wire is None
    ):
        raise AuditIntegrityError("tool message refresh replaced its actual family")
    if not isinstance(previous, (NativeThinkingMessageRecipe, ToolCallArgumentsMessageRecipe)):
        raise AuditIntegrityError("tool message refresh lacks its actual nominal recipe")
    positions = tuple(index for index, call in enumerate(completion.tool_batch.calls) if call.id == tool_call_id)
    if (
        len(positions) != 1
        or previous.response != recipe.response
        or recipe.argument_indexes != tuple(sorted({*previous.argument_indexes, *positions}))
    ):
        raise AuditIntegrityError("tool message refresh changed its source or admitted call position")
    before = retained_family_wire(previous_wire)
    after = retained_family_wire(family_message_wire(message))
    index = positions[0]
    if (
        type(before.get("tool_calls")) is not list
        or type(after.get("tool_calls")) is not list
        or len(before["tool_calls"]) != len(after["tool_calls"])
    ):
        raise AuditIntegrityError("tool message refresh changed its actual call cohort")
    after["tool_calls"][index]["function"]["arguments"] = before["tool_calls"][index]["function"]["arguments"]
    if encode_transcript_object_json(after) != previous_wire:
        raise AuditIntegrityError("tool message refresh changed bytes outside its one admitted argument")


def rebuild_tool_argument_message(
    *,
    response: PrivateChunk,
    recipe: ToolCallArgumentsMessageRecipe,
    selected_message_json: str,
) -> dict[str, Any]:
    if type(response) is not PrivateChunk or type(recipe) is not ToolCallArgumentsMessageRecipe or response.reference != recipe.response:
        raise AuditIntegrityError("ordinary message changed its selected response")
    completion = decode_continuation_response(response)
    wire = retained_family_wire(selected_message_json)
    _require_ordinary_source(completion, wire, recipe.argument_indexes)
    result = _ordinary_message(wire, recipe.argument_indexes)
    if family_message_wire(result) != selected_message_json:
        raise AuditIntegrityError("ordinary message rebuilding changed complete ordered wire")
    return result
