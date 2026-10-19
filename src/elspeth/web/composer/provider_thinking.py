"""Exact native thinking replay, separate from visible prose and audit summaries."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PromptGuardContentPart,
    PromptGuardThinkingParts,
)
from elspeth.web.composer.prompt_guard_rendering import JsonPath, json_content_parts
from elspeth.web.composer.protocol import ComposerAdmissionRefused
from elspeth.web.credential_guard import require_no_credential_material

_INSPECTION_UNIT_CHARACTERS = 8_000
_INSPECTION_OVERLAP_CHARACTERS = 1_000


def parse_native_thinking_blocks(value: object) -> list[dict[str, str]]:
    """Parse the closed native shape without imposing diagnostic JSON limits.

    Provider thinking has no documented per-field character ceiling. Its
    grammar has fixed depth, so it needs no generic recursive artifact walk.
    """
    if type(value) not in (list, tuple):
        raise ComposerAdmissionRefused("Provider thinking state is not a complete native block list.")
    result: list[dict[str, str]] = []
    for block in value:
        if not isinstance(block, Mapping):
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
                block[key].encode("utf-8")
        except UnicodeEncodeError:
            raise ComposerAdmissionRefused("Provider thinking state contains invalid UTF-8 text.") from None
        opaque_key = "signature" if block["type"] == "thinking" else "data"
        if not block[opaque_key]:
            raise ComposerAdmissionRefused("Provider thinking state is missing its opaque replay data.")
        result.append({key: cast(str, block[key]) for key in block})
    return result


def native_thinking_blocks(value: object) -> list[dict[str, str]]:
    """Admit complete blocks and credential-scan each complete native field."""
    blocks = parse_native_thinking_blocks(value)
    for block in blocks:
        # The schema is already closed. Scan complete strings, including token
        # and URL seams, with the same detector rather than a local size cap.
        require_no_credential_material(
            block,
            surface="composer_provider_thinking",
            max_string_bytes=max(len(text.encode("utf-8")) for text in block.values()),
        )
    return blocks


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
        for key in ("thinking", "signature") if block["type"] == "thinking" else ("data",):
            text = block[key]
            # Only the inspection projection is windowed. Exact native bytes
            # remain intact; overlap exposes attacks crossing a window seam.
            string_parts[(index, key)] = tuple(
                PromptGuardContentPart(
                    text[start : start + _INSPECTION_UNIT_CHARACTERS],
                    inspection_text=text[max(0, start - _INSPECTION_OVERLAP_CHARACTERS) : start + _INSPECTION_UNIT_CHARACTERS],
                )
                for start in range(0, len(text), _INSPECTION_UNIT_CHARACTERS)
            )
    parts = json_content_parts(serialized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), string_parts=string_parts)
    return {**message, "thinking_blocks": blocks, PROMPT_GUARD_THINKING_PARTS_KEY: PromptGuardThinkingParts(parts)}
