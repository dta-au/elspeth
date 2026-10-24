"""Actual native producer plus source-checked untrusted inspection rebuilding.

These functions do not admit a current SQL selection or grant tool authority.
The current family owner separately checks the selected shelf and source rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardThinkingParts,
    PromptGuardToolArgumentParts,
    detach_prompt_guard_evidence,
)
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolCall
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response
from elspeth.web.composer.continuation_message_family_recipes import (
    AttachmentResultMessageRecipe,
    FailureSchemaMessageRecipe,
    NativeThinkingMessageRecipe,
)
from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json
from elspeth.web.composer.discovery_cache import serialize_tool_result, tool_result_content_parts
from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts
from elspeth.web.composer.provider_thinking import attach_provider_thinking, native_thinking_blocks
from elspeth.web.composer.tools._common import ToolResult, build_plugin_schemas_for_failure
from elspeth.web.composer.tools._registry import should_augment_with_plugin_schemas

_GUARD_KEYS = frozenset(
    {
        PROMPT_GUARD_CONTENT_PARTS_KEY,
        PROMPT_GUARD_THINKING_PARTS_KEY,
        PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
        PROMPT_GUARD_TRUST_KEY,
    }
)


def native_assistant_message(completion: _AdmittedLLMCompletion) -> dict[str, Any]:
    """The existing actual tool-batch assistant construction, unchanged."""
    if type(completion) is not _AdmittedLLMCompletion:
        raise TypeError("native assistant construction requires its actual admitted completion")
    return attach_provider_thinking(
        {
            "role": "assistant",
            "content": completion.message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in completion.tool_batch.calls
            ],
        },
        completion.provider_metadata.thinking_blocks,
    )


def family_message_wire(message: Mapping[str, Any]) -> str:
    return encode_transcript_object_json(detach_prompt_guard_evidence([message]).messages[0])


def retained_family_wire(text: str) -> dict[str, Any]:
    value, error = parse_json_strict(text)
    if error is not None or type(value) is not dict or _GUARD_KEYS.intersection(value) or encode_transcript_object_json(value) != text:
        raise AuditIntegrityError("family message lost its complete ordered marker-free wire")
    return value


def _require_native_source(completion: _AdmittedLLMCompletion, wire: dict[str, Any]) -> None:
    if set(wire) != {"role", "content", "tool_calls", "thinking_blocks"} or wire["role"] != "assistant":
        raise AuditIntegrityError("native message changed its actual complete assistant envelope")
    if wire["content"] != completion.message.content:
        raise AuditIntegrityError("native message changed its actual provider content")
    calls = wire["tool_calls"]
    if type(calls) is not list or len(calls) != len(completion.tool_batch.calls):
        raise AuditIntegrityError("native message changed its actual issued call cohort")
    for call, original in zip(calls, completion.tool_batch.calls, strict=True):
        if (
            type(call) is not dict
            or set(call) != {"id", "type", "function"}
            or call["id"] != original.id
            or call["type"] != "function"
            or type(call["function"]) is not dict
            or set(call["function"]) != {"name", "arguments"}
            or call["function"]["name"] != original.function.name
            or type(call["function"]["arguments"]) is not str
        ):
            raise AuditIntegrityError("native message changed its actual issued call identity/order")
    # Native JSON is closed and complete. Compare the selected shelf against
    # the response's separately retained original ordered native input.
    # Rewritten argument strings also come from that selected shelf, after
    # the real ordinary argument-admission/custody producer ran.
    retained = native_thinking_blocks(wire["thinking_blocks"])
    actual = native_thinking_blocks(completion.provider_metadata.thinking_blocks)
    if not retained or json.dumps(retained, ensure_ascii=False, separators=(",", ":")) != json.dumps(
        actual, ensure_ascii=False, separators=(",", ":")
    ):
        raise AuditIntegrityError("native message changed its complete actual provider thinking")


def _native_message_from_wire(
    completion: _AdmittedLLMCompletion, wire: dict[str, Any], argument_indexes: tuple[int, ...]
) -> dict[str, Any]:
    _require_native_source(completion, wire)
    calls = wire["tool_calls"]
    if (
        type(argument_indexes) is not tuple
        or any(type(index) is not int or not 0 <= index < len(calls) for index in argument_indexes)
        or argument_indexes != tuple(sorted(set(argument_indexes)))
    ):
        raise AuditIntegrityError("native message lost its actual admitted argument positions")
    for index, (call, original) in enumerate(zip(calls, completion.tool_batch.calls, strict=True)):
        if index not in argument_indexes and call["function"]["arguments"] != original.function.arguments:
            raise AuditIntegrityError("native message changed unadmitted original argument bytes")
    result = attach_provider_thinking({"role": "assistant", "content": wire["content"], "tool_calls": calls}, wire["thinking_blocks"])
    if argument_indexes:
        result[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY] = tuple(
            PromptGuardToolArgumentParts(index, json_argument_content_parts(calls[index]["function"]["arguments"]))
            for index in argument_indexes
        )
    if family_message_wire(result) != encode_transcript_object_json(wire):
        raise AuditIntegrityError("native producer changed the complete selected ordered wire")
    return result


def capture_native_message_recipe(
    *, response: PrivateChunk, completion: _AdmittedLLMCompletion, message: Mapping[str, Any]
) -> NativeThinkingMessageRecipe:
    """Bind a live actual producer object; the recipe saves no trust object."""
    if type(response) is not PrivateChunk or type(completion) is not _AdmittedLLMCompletion or type(message) is not dict:
        raise TypeError("native capture requires actual response/completion owners")
    if decode_continuation_response(response) != completion:
        raise AuditIntegrityError("native capture changed its complete original provider response")
    wire = retained_family_wire(family_message_wire(message))
    indexes: tuple[int, ...] = ()
    if PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message:
        entries = message[PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY]
        if type(entries) is not tuple or not entries or any(type(entry) is not PromptGuardToolArgumentParts for entry in entries):
            raise AuditIntegrityError("native capture replaced its actual tool argument partitions")
        indexes = tuple(entry.tool_call_index for entry in entries)
    rebuilt = _native_message_from_wire(completion, wire, indexes)
    actual_parts = message[PROMPT_GUARD_THINKING_PARTS_KEY] if PROMPT_GUARD_THINKING_PARTS_KEY in message else None
    if type(actual_parts) is not PromptGuardThinkingParts or actual_parts != rebuilt[PROMPT_GUARD_THINKING_PARTS_KEY]:
        raise AuditIntegrityError("native capture changed its actual complete inspection partitions")
    if (
        PROMPT_GUARD_CONTENT_PARTS_KEY in message
        or PROMPT_GUARD_TRUST_KEY in message
        or message.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY) != rebuilt.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY)
    ):
        raise AuditIntegrityError("native capture changed its actual message/argument provenance")
    return NativeThinkingMessageRecipe(response=response.reference, argument_indexes=indexes)


def require_native_message_refresh(
    *,
    previous: NativeThinkingMessageRecipe,
    previous_wire: str,
    recipe: NativeThinkingMessageRecipe,
    message: dict[str, Any],
) -> None:
    """Admit only the existing ordinary argument rewrite on the same message."""
    if previous.response != recipe.response or not set(previous.argument_indexes).issubset(recipe.argument_indexes):
        raise AuditIntegrityError("native refresh changed its source or removed an admitted argument")
    before = retained_family_wire(previous_wire)
    after = retained_family_wire(family_message_wire(message))
    if type(before.get("tool_calls")) is not list or len(before["tool_calls"]) != len(after["tool_calls"]):
        raise AuditIntegrityError("native refresh changed its actual call cohort")
    for old_call, new_call in zip(before["tool_calls"], after["tool_calls"], strict=True):
        new_call["function"]["arguments"] = old_call["function"]["arguments"]
    if encode_transcript_object_json(after) != previous_wire:
        raise AuditIntegrityError("native refresh changed bytes outside the ordinary argument rewrite")


def rebuild_native_message(*, response: PrivateChunk, recipe: NativeThinkingMessageRecipe, selected_message_json: str) -> dict[str, Any]:
    """Pure reconstruction after the actual current owner verifies SQL/shelf."""
    if type(response) is not PrivateChunk or type(recipe) is not NativeThinkingMessageRecipe or response.reference != recipe.response:
        raise AuditIntegrityError("native recipe changed its actual selected response")
    return _native_message_from_wire(
        decode_continuation_response(response), retained_family_wire(selected_message_json), recipe.argument_indexes
    )


def readmit_failure_schema_result(*, call: _AdmittedToolCall, result: ToolResult, catalog: PolicyCatalogView) -> ToolResult:
    """Run only the actual current schema augmentation, never its mutation."""
    if type(call) is not _AdmittedToolCall or type(result) is not ToolResult or type(catalog) is not PolicyCatalogView:
        raise TypeError("failure schema rebuilding requires its actual call/result/policy owners")
    if result.success or result.plugin_schemas is None or not should_augment_with_plugin_schemas(call.function.name):
        raise AuditIntegrityError("failure schema has no eligible actual failed augmentation")
    bare = replace(result, plugin_schemas=None)
    schemas = build_plugin_schemas_for_failure(bare, catalog, schema_unavailable_message=lambda _schema: None)
    if schemas is None or canonical_json(deep_thaw(schemas)) != canonical_json(deep_thaw(result.plugin_schemas)):
        raise AuditIntegrityError("failure schemas differ from their actual current policy producer")
    return replace(result, plugin_schemas=schemas)


def failure_schema_message(*, call: _AdmittedToolCall, result: ToolResult, catalog: PolicyCatalogView) -> dict[str, Any]:
    current = readmit_failure_schema_result(call=call, result=result, catalog=catalog)
    serialized = serialize_tool_result(current)
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": serialized,
        PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(current, serialized),
    }


def capture_failure_schema_recipe(
    *, response: PrivateChunk, call: _AdmittedToolCall, result: ToolResult, catalog: PolicyCatalogView, message: Mapping[str, Any]
) -> FailureSchemaMessageRecipe:
    if type(response) is not PrivateChunk or type(message) is not dict:
        raise TypeError("failure capture requires its actual response/message owners")
    completion = decode_continuation_response(response)
    if len(tuple(original for original in completion.tool_batch.calls if original == call)) != 1:
        raise AuditIntegrityError("failure schema capture changed its original admitted call")
    rebuilt = failure_schema_message(call=call, result=result, catalog=catalog)
    if (
        family_message_wire(message) != family_message_wire(rebuilt)
        or message.get(PROMPT_GUARD_CONTENT_PARTS_KEY) != rebuilt[PROMPT_GUARD_CONTENT_PARTS_KEY]
        or PROMPT_GUARD_THINKING_PARTS_KEY in message
        or PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY in message
        or PROMPT_GUARD_TRUST_KEY in message
    ):
        raise AuditIntegrityError("failure schema capture changed its actual complete producer message")
    return FailureSchemaMessageRecipe(response=response.reference, tool_call_id=call.id)


def attachment_result_message(*, call: _AdmittedToolCall, result: ToolResult) -> dict[str, Any]:
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolFunction
    from elspeth.web.composer.continuation_message_builders import tool_result_message
    from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

    if type(call) is not _AdmittedToolCall or type(call.function) is not _AdmittedToolFunction or type(result) is not ToolResult:
        raise AuditIntegrityError("attachment message requires its actual issued call and complete result")
    if call.function.name not in ("read_attachment", "get_blob_content"):
        raise AuditIntegrityError("attachment message selected another actual response domain")
    admitted = admit_discovery_result(call.function.name, result)
    return tool_result_message(call_id=call.id, result=admitted, serialized=serialize_admitted_discovery_result(admitted))


def capture_attachment_result_recipe(
    *, response: PrivateChunk, call: _AdmittedToolCall, result: ToolResult, message: dict[str, Any]
) -> AttachmentResultMessageRecipe:
    from elspeth.web.composer._compose_loop_carriers import _AdmittedToolFunction

    if (
        type(response) is not PrivateChunk
        or type(call) is not _AdmittedToolCall
        or type(call.function) is not _AdmittedToolFunction
        or type(result) is not ToolResult
        or type(message) is not dict
    ):
        raise AuditIntegrityError("attachment capture requires its actual original response/call/result/message owners")
    completion = decode_continuation_response(response)
    found = tuple(item for item in completion.tool_batch.calls if item.id == call.id)
    if len(found) != 1 or found[0] != call:
        raise AuditIntegrityError("attachment message changed its actual original issued call")
    expected = attachment_result_message(call=call, result=result)
    if family_message_wire(message) != family_message_wire(expected):
        raise AuditIntegrityError("attachment message changed its actual complete response wire")
    # Compare detached guard provenance as well as complete wire. A caller may
    # not retain a different trust partition for the same visible JSON string.
    if detach_prompt_guard_evidence([message]) != detach_prompt_guard_evidence([expected]):
        raise AuditIntegrityError("attachment message changed its actual response trust partition")
    return AttachmentResultMessageRecipe(response=response.reference, tool_call_id=call.id)
