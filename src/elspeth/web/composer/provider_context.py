"""Capture exact safe logical messages/tools, never transport kwargs or images.

The finite credential detector is not general DLP. This initial version
withholds the complete context when any slot cannot safely be retained. It
never labels a filtered prefix as a reconstructable request.
"""

from __future__ import annotations

import json
import math
import re

from pydantic import JsonValue, ValidationError
from rfc8785 import CanonicalizationError

from elspeth.contracts.composer_provider_context import PrivateProviderContext, ProviderContextUnavailableReason
from elspeth.contracts.credential_material import find_credential_material, is_secret_field
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES, JsonBoundaryError, JsonTraversalBudget
from elspeth.web.composer.continuation_controls import (
    CONTINUATION_CALL_NOT_ALONE,
    CONTINUATION_CONTROL_RESULT,
    CONTINUATION_CONTROL_TOOL,
    CONTINUATION_NOTES_MAX_BYTES,
    ContinuationControlRefused,
    ContinueArguments,
    _argument_refusal,
)
from elspeth.web.composer.diagnostic_evidence import _POLICY, _decode, _DuplicateKeys, _project
from elspeth.web.composer.protocol import ComposerAdmissionRefused
from elspeth.web.composer.provider_thinking import native_thinking_blocks
from elspeth.web.composer.redaction import MANIFEST

_DATA_URL = re.compile(r"\bdata:[^,\s]*,", re.IGNORECASE)


class _UnavailableContext(ValueError):
    """Closed reason without untrusted content or exception stringification."""

    def __init__(self, reason: ProviderContextUnavailableReason) -> None:
        super().__init__(reason)
        self.reason = reason


def _copy_json(value: object, budget: JsonTraversalBudget, depth: int = 0) -> JsonValue:
    """Detach exact finite JSON under one aggregate input/decoded-slot budget."""
    budget.check_depth(depth, label="provider context")
    if type(value) is dict:
        budget.consume_items(len(value), label="provider context")
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if type(key) is not str:
                raise _UnavailableContext("unsupported")
            budget.consume_text(key, label="provider context")
            result[key] = _copy_json(child, budget, depth + 1)
        return result
    if type(value) is list:
        budget.consume_items(len(value), label="provider context")
        return [_copy_json(child, budget, depth + 1) for child in value]
    if type(value) is str:
        # Full Composer prompts exceed the ordinary leaf cap. The shared
        # aggregate byte budget still bounds the complete retained request.
        budget.consume_text(value, label="provider context", enforce_string_limit=False)
        # Image data URLs are unsafe even when embedded in ordinary text or
        # an encoded argument. No decoding/fetching or raw fallback occurs.
        if _DATA_URL.search(value) is not None:
            raise _UnavailableContext("unsupported")
        return value
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise _UnavailableContext("unsupported")


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise _UnavailableContext("unsupported")
    return value


def _text(value: JsonValue) -> str:
    if type(value) is not str:
        raise _UnavailableContext("unsupported")
    if find_credential_material(value, _POLICY) is not None:
        raise _UnavailableContext("recognized_credential")
    return value


def _safe_data(value: JsonValue, *, tool_name: str = "") -> None:
    reasons: set[str] = set()
    _project(value, tool_name=tool_name, additional_fields=(), reasons=reasons, root=True)
    if reasons:
        reason: ProviderContextUnavailableReason = "credential_field" if "credential_field" in reasons else "recognized_credential"
        raise _UnavailableContext(reason)


def _schema(value: JsonValue, *, credential_property: bool = False) -> None:
    """Preserve schema property names; inspect descriptions and authored values."""
    if type(value) is dict:
        for key, child in value.items():
            _text(key)
            if key in {"default", "example", "examples", "const", "enum"}:
                if credential_property and child is not None:
                    raise _UnavailableContext("credential_field")
                _safe_data(child)
            elif key in {"properties", "$defs", "definitions", "patternProperties"}:
                for property_name, property_schema in _object(child).items():
                    _text(property_name)
                    _schema(property_schema, credential_property=key == "properties" and is_secret_field(property_name))
            else:
                _schema(child, credential_property=credential_property)
    elif type(value) is list:
        for item in value:
            _schema(item, credential_property=credential_property)
    elif type(value) is str:
        _text(value)


def _encoded_tool_json(value: JsonValue, *, tool_name: str, budget: JsonTraversalBudget) -> None:
    if tool_name not in MANIFEST and tool_name != "emit_pipeline_proposal":
        raise _UnavailableContext("unsupported")
    if type(value) is not str:
        raise _UnavailableContext("unsupported")
    try:
        decoded = _decode(value)
    except _DuplicateKeys:
        raise _UnavailableContext("duplicate_keys") from None
    except JsonBoundaryError:
        raise
    except ValueError:
        raise _UnavailableContext("malformed_json") from None
    decoded = _copy_json(decoded, budget)
    _safe_data(decoded, tool_name=tool_name)


def _refused_control_text(raw: str, budget: JsonTraversalBudget) -> None:
    """Scan every original scalar, including shadowed/escaped malformed keys.

    Only a paired exact control refusal may retain this original argument
    text. No malformed ordinary-tool JSON or arbitrary result gains a raw
    fallback. Unreadable string escapes still refuse the complete context.
    """
    decoder = json.JSONDecoder()
    index = 0
    while index < len(raw):
        if raw[index] in "{}[],: \t\r\n":
            index += 1
            continue
        if raw[index] == '"':
            try:
                value, end = decoder.raw_decode(raw, index)
            except ValueError:
                raise _UnavailableContext("malformed_json") from None
            if type(value) is not str:
                raise _UnavailableContext("unsupported")
            index = end
        else:
            end = index + 1
            while end < len(raw) and raw[end] not in '{}[],: \t\r\n"':
                end += 1
            value, index = raw[index:end], end
        budget.consume_items(1, label="refused control text")
        _copy_json(value, budget)
        _text(value)
        following = index
        while following < len(raw) and raw[following] in " \t\r\n":
            following += 1
        if following < len(raw) and raw[following] == ":" and is_secret_field(value):
            raise _UnavailableContext("credential_field")


def _control_history_result(raw: JsonValue, *, alone: bool, budget: JsonTraversalBudget) -> str:
    """Closed historical control semantics, never a dispatch capability."""
    arguments = _text(raw)
    refused = False
    try:
        value = _decode(arguments)
    except JsonBoundaryError:
        raise
    except (_DuplicateKeys, ValueError):
        _refused_control_text(arguments, budget)
        refused = True
    else:
        decoded = _copy_json(value, budget)
        _safe_data(decoded)
        try:
            parsed = ContinueArguments.model_validate(decoded)
        except ValidationError:
            refused = True
        else:
            notes = parsed.notes
            if notes is not None and (not notes.strip() or len(notes.encode("utf-8")) > CONTINUATION_NOTES_MAX_BYTES):
                refused = True
    if not alone:
        return canonical_json(
            ContinuationControlRefused(
                CONTINUATION_CALL_NOT_ALONE,
                "checkpoint_and_continue must be the only tool call in its batch; every call in this batch was refused before effects.",
            ).to_tool_payload()
        )
    return canonical_json(_argument_refusal().to_tool_payload()) if refused else CONTINUATION_CONTROL_RESULT


def _message_content(content: JsonValue) -> None:
    if content is None:
        return
    if type(content) is str:
        _text(content)
        return
    if type(content) is not list:
        raise _UnavailableContext("unsupported")
    for block_value in content:
        block = _object(block_value)
        if set(block) - {"type", "text", "cache_control"} or "type" not in block or "text" not in block:
            raise _UnavailableContext("unsupported")
        if block["type"] != "text":
            raise _UnavailableContext("unsupported")
        _text(block["text"])
        if "cache_control" in block:
            _safe_data(block["cache_control"])


def _check_messages(messages: JsonValue, budget: JsonTraversalBudget) -> None:
    if type(messages) is not list:
        raise _UnavailableContext("unsupported")
    calls: dict[str, tuple[str, str | None]] = {}
    for message_value in messages:
        message = _object(message_value)
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "cache_control", "thinking_blocks"}:
            raise _UnavailableContext("unsupported")
        if "role" not in message:
            raise _UnavailableContext("unsupported")
        role = message["role"]
        if type(role) is not str or role not in {"system", "developer", "user", "assistant", "tool"}:
            raise _UnavailableContext("unsupported")
        if "thinking_blocks" in message:
            if role != "assistant":
                raise _UnavailableContext("unsupported")
            try:
                native_thinking_blocks(message["thinking_blocks"])
            except ComposerAdmissionRefused:
                raise _UnavailableContext("unsupported") from None
        if role != "tool" and "tool_call_id" in message:
            raise _UnavailableContext("unsupported")
        if "name" in message:
            _text(message["name"])
        if "cache_control" in message:
            _safe_data(message["cache_control"])
        if role == "tool":
            if "tool_call_id" not in message or "content" not in message:
                raise _UnavailableContext("unsupported")
            call_id = _text(message["tool_call_id"])
            if call_id not in calls:
                raise _UnavailableContext("unsupported")
            name, control_result = calls.pop(call_id)
            if control_result is not None:
                if message["content"] != control_result:
                    raise _UnavailableContext("unsupported")
            else:
                _encoded_tool_json(message["content"], tool_name=name, budget=budget)
        elif "content" in message:
            _message_content(message["content"])
        if "tool_calls" in message:
            if role != "assistant" or type(message["tool_calls"]) is not list:
                raise _UnavailableContext("unsupported")
            batch_ids: list[str] = []
            for call_value in message["tool_calls"]:
                call = _object(call_value)
                if set(call) != {"id", "type", "function"} or call["type"] != "function":
                    raise _UnavailableContext("unsupported")
                call_id = _text(call["id"])
                function = _object(call["function"])
                if set(function) != {"name", "arguments"} or call_id in calls:
                    raise _UnavailableContext("unsupported")
                name = _text(function["name"])
                if name == CONTINUATION_CONTROL_TOOL:
                    result = _control_history_result(function["arguments"], alone=len(message["tool_calls"]) == 1, budget=budget)
                else:
                    _encoded_tool_json(function["arguments"], tool_name=name, budget=budget)
                    result = None
                calls[call_id] = name, result
                batch_ids.append(call_id)
            if len(batch_ids) > 1:
                mixed_refusal = next((calls[key][1] for key in batch_ids if calls[key][0] == CONTINUATION_CONTROL_TOOL), None)
                if mixed_refusal is not None:
                    # The control producer refuses the whole batch before
                    # effects. A peer success is not this closed history.
                    for key in batch_ids:
                        name, _result = calls[key]
                        calls[key] = name, mixed_refusal
    if calls:
        raise _UnavailableContext("unsupported")


def _check_tools(tools: JsonValue) -> None:
    if tools is None:
        return
    if type(tools) is not list:
        raise _UnavailableContext("unsupported")
    for tool_value in tools:
        tool = _object(tool_value)
        if set(tool) - {"type", "function", "cache_control"} or "type" not in tool or "function" not in tool:
            raise _UnavailableContext("unsupported")
        if tool["type"] != "function":
            raise _UnavailableContext("unsupported")
        function = _object(tool["function"])
        if set(function) - {"name", "description", "parameters", "strict"} or "name" not in function:
            raise _UnavailableContext("unsupported")
        _text(function["name"])
        if "description" in function:
            _text(function["description"])
        if "parameters" in function:
            _schema(function["parameters"])
        if "strict" in function and type(function["strict"]) is not bool:
            raise _UnavailableContext("unsupported")
        if "cache_control" in tool:
            _safe_data(tool["cache_control"])


def capture_provider_context(*, messages: object, tools: object) -> PrivateProviderContext:
    """Snapshot only explicitly supplied ordered messages/tools before await.

    ``None`` means tools omitted; an empty list means an explicitly empty list.
    This preparation receipt never asserts that the SDK or remote provider
    received the request. Physical dispatch remains the call owner's fact.
    """
    try:
        budget = JsonTraversalBudget()
        context = _object(
            _copy_json(
                {
                    "schema_version": "composer.provider-context.v1",
                    "capture_boundary": "prepared_litellm_messages_and_tools",
                    "messages": messages,
                    "tools": tools,
                },
                budget,
            )
        )
        _check_messages(context["messages"], budget)
        _check_tools(context["tools"])
        content = canonical_json(context)
        if len(content.encode("utf-8")) > JSON_MAX_TOTAL_UTF8_BYTES:
            raise JsonBoundaryError("provider context exceeds encoded bounds")
        return PrivateProviderContext(disposition="retained", canonical=content)
    except JsonBoundaryError:
        return PrivateProviderContext(disposition="unavailable", canonical=None, reasons=("bounds",))
    except _UnavailableContext as exc:
        return PrivateProviderContext(disposition="unavailable", canonical=None, reasons=(exc.reason,))
    except CanonicalizationError:
        return PrivateProviderContext(disposition="unavailable", canonical=None, reasons=("unsupported",))
