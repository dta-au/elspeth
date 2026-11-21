"""Bound state inspection at the producing tool, without changing stored content."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from hashlib import sha256
from typing import Any, Literal

from pydantic import JsonValue

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.tools._common import ToolContext, ToolResult, _failure_result, normalize_tool_result_validation
from elspeth.web.composer.tools.state_responses import StateInspectionPage, state_inspection_page_payload

STATE_INSPECTION_MAX_BYTES = 32_768


def _disclosure_bytes(result: ToolResult) -> int:
    # Count the result and its escaping inside a provider message. ASCII JSON
    # is conservative for the production UTF-8 serializer, including Unicode.
    content = json.dumps(result.to_dict(), ensure_ascii=True)
    return len(json.dumps(content, ensure_ascii=True).encode("utf-8"))


def _structure(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Keep routing verbatim; omit options and prose from component summaries."""

    def component(item: JsonValue) -> dict[str, JsonValue]:
        assert type(item) is dict
        return {key: value for key, value in item.items() if key not in ("options", "description")}

    result = dict(data)
    for key in ("source", "node", "output"):
        if key in result and result[key] is not None:
            result[key] = component(result[key])
    if "sources" in result:
        sources = result["sources"]
        assert type(sources) is dict
        result["sources"] = {name: component(source) for name, source in sources.items()}
    for key in ("nodes", "outputs"):
        if key in result:
            items = result[key]
            assert type(items) is list
            result[key] = [component(item) for item in items]
    if "metadata" in result:
        result["metadata"] = component(result["metadata"])
    result["inspection_note"] = "Structural overview: option values and descriptions are excluded. Use field_path for exact values."
    return result


def _select(data: Any, pointer: str) -> Any:
    """Resolve an exact RFC 6901 path into this requested inspection view."""
    if pointer == "":
        return data
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid field_path JSON pointer.")
    value = data
    segments = pointer[1:].split("/")
    # A selected component accepts both its documented response wrapper and
    # a pointer directly into that component. Never choose among sources.
    if type(data) is dict and len(data) == 1:
        if "node" in data and segments[0] != "node":
            value = data["node"]
        elif "output" in data and segments[0] != "output":
            value = data["output"]
        elif "sources" in data and segments[0] != "sources":
            sources = data["sources"]
            assert type(sources) is dict
            if len(sources) == 1:
                value = next(iter(sources.values()))
    for segment in segments:
        key = segment.replace("~1", "/").replace("~0", "~")
        if type(value) is dict and key in value:
            value = value[key]
        elif type(value) is list and re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError(
                "field_path does not identify a value. Use /node/<field> for a node or /sources/<name>/<field> for a source; "
                "a selected node, output, or single source also accepts /<field>."
            )
    return value


def bounded_state_inspection(
    result: ToolResult,
    context: ToolContext,
    *,
    field_path: str | None,
    offset_chars: int,
    expected_sha256: str | None,
    since_start: bool,
) -> ToolResult:
    result = normalize_tool_result_validation(result, context.catalog)
    state = result.updated_state
    data = deep_thaw(result.data)
    assert type(data) is dict
    if field_path is None and offset_chars == 0 and expected_sha256 is None and _disclosure_bytes(result) <= STATE_INSPECTION_MAX_BYTES:
        return result

    view: Literal["structure", "field", "changes"]
    try:
        if field_path is not None:
            selected = _select(data, field_path)
            view = "field"
        elif since_start:
            selected = data
            view = "changes"
        else:
            selected = _structure(data)
            view = "structure"
    except ValueError as exc:
        return _failure_result(state, str(exc), error_code="argument_error", with_state_validation=False)
    encoding: Literal["text", "json"] = "text" if type(selected) is str else "json"
    content = selected if type(selected) is str else canonical_json(selected)
    digest = sha256(content.encode("utf-8")).hexdigest()
    if (offset_chars > 0 and expected_sha256 is None) or (expected_sha256 is not None and expected_sha256 != digest):
        return _failure_result(
            state,
            "Inspection content changed or its hash is missing. Read from offset_chars=0 and use the returned sha256 for continuation.",
            error_code="argument_error",
            with_state_validation=False,
        )
    if offset_chars > len(content):
        return _failure_result(
            state, "offset_chars exceeds the selected content length.", error_code="argument_error", with_state_validation=False
        )

    def _state_inspection_page_result(length: int) -> ToolResult:
        end = offset_chars + length
        payload = StateInspectionPage(
            view,
            field_path,
            encoding,
            content[offset_chars:end],
            offset_chars,
            len(content),
            end if end < len(content) else None,
            digest,
        )
        return replace(result, data=state_inspection_page_payload(payload))

    low, high = 0, min(len(content) - offset_chars, STATE_INSPECTION_MAX_BYTES)
    while low < high:
        middle = (low + high + 1) // 2
        if _disclosure_bytes(_state_inspection_page_result(middle)) <= STATE_INSPECTION_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    bounded = _state_inspection_page_result(low)
    if _disclosure_bytes(bounded) > STATE_INSPECTION_MAX_BYTES or (low == 0 and offset_chars < len(content)):
        return _failure_result(
            state,
            "Validation diagnostics exceed the inspection response budget. Call preview_pipeline to inspect validation.",
            error_code="inspection_response_too_large",
            with_state_validation=False,
        )
    return bounded


def enforce_state_inspection_limit(result: ToolResult, context: ToolContext) -> ToolResult:
    """Bound early refusals too; never attach an unlimited standing-error list."""
    result = normalize_tool_result_validation(result, context.catalog)
    if _disclosure_bytes(result) <= STATE_INSPECTION_MAX_BYTES:
        return result
    if not result.success and result.validation.errors:
        primary = result.validation.errors[0]
        refusal = _failure_result(
            result.updated_state,
            primary.message,
            error_code=primary.error_code,
            with_state_validation=False,
        )
        if _disclosure_bytes(refusal) <= STATE_INSPECTION_MAX_BYTES:
            return refusal
    return _failure_result(
        result.updated_state,
        "Inspection diagnostics exceed the response budget. Call preview_pipeline to inspect validation.",
        error_code="inspection_response_too_large",
        with_state_validation=False,
    )
