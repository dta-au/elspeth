"""Exact native thinking replay, separate from visible prose and audit summaries."""

from __future__ import annotations

import json
from typing import Any, cast

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PromptGuardContentPart,
    PromptGuardThinkingParts,
)
from elspeth.web.composer.bounded_json import JsonBoundaryError, bounded_json_loads, require_bounded_text
from elspeth.web.composer.prompt_guard_rendering import JsonPath, json_content_parts
from elspeth.web.composer.protocol import ComposerAdmissionRefused
from elspeth.web.credential_guard import require_no_credential_material

_OPAQUE_UNIT_CHARACTERS = 8_000


def native_thinking_blocks(value: object) -> list[dict[str, str]]:
    """Admit complete, finite provider blocks without rewriting any field."""
    try:
        detached = deep_thaw(value)
        serialized = json.dumps(detached, ensure_ascii=False, allow_nan=False)
        decoded = bounded_json_loads(serialized, label="provider native thinking")
    except (JsonBoundaryError, TypeError, ValueError):
        raise ComposerAdmissionRefused("Provider thinking state cannot be replayed completely within supported limits.") from None
    if type(decoded) is not list:
        raise ComposerAdmissionRefused("Provider thinking state is not a complete native block list.")
    result: list[dict[str, str]] = []
    for block in decoded:
        if type(block) is not dict:
            raise ComposerAdmissionRefused("Provider thinking state contains an invalid block.")
        if block.get("type") == "thinking":
            fields = {"type", "thinking", "signature"}
        elif block.get("type") == "redacted_thinking":
            fields = {"type", "data"}
        else:
            raise ComposerAdmissionRefused("Provider thinking state contains an unsupported native block.")
        if set(block) != fields or any(type(block[key]) is not str for key in fields):
            raise ComposerAdmissionRefused("Provider thinking state has incomplete native fields.")
        try:
            for key in fields:
                require_bounded_text(block[key], label="provider thinking field")
        except JsonBoundaryError:
            raise ComposerAdmissionRefused("Provider thinking state exceeds the supported complete-field limit.") from None
        opaque_key = "signature" if block["type"] == "thinking" else "data"
        if not block[opaque_key]:
            raise ComposerAdmissionRefused("Provider thinking state is missing its opaque replay data.")
        result.append({key: cast(str, block[key]) for key in block})
    require_no_credential_material(result, surface="composer_provider_thinking")
    return result


def attach_provider_thinking(message: dict[str, Any], value: object) -> dict[str, Any]:
    """Attach exact blocks and owned untrusted inspection evidence to a tool turn."""
    if value is None:
        return message
    blocks = native_thinking_blocks(value)
    if not blocks:
        return message
    serialized = json.dumps(blocks, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    string_parts: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    for index, block in enumerate(blocks):
        opaque_key = "signature" if block["type"] == "thinking" else "data"
        opaque = block[opaque_key]
        string_parts[(index, opaque_key)] = tuple(
            PromptGuardContentPart(opaque[start : start + _OPAQUE_UNIT_CHARACTERS])
            for start in range(0, len(opaque), _OPAQUE_UNIT_CHARACTERS)
        )
    parts = json_content_parts(serialized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), string_parts=string_parts)
    return {**message, "thinking_blocks": blocks, PROMPT_GUARD_THINKING_PARTS_KEY: PromptGuardThinkingParts(parts)}
