"""Strict JSON parsing shared by audit contracts and external clients."""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Any


class DuplicateJSONKeyError(ValueError):
    """A JSON object contains duplicate keys at one nesting level."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject lossy last-wins parsing of duplicate external object keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJSONKeyError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_non_finite(constant: str) -> None:
    """Reject nonfinite literals during parsing, before recursive traversal."""
    raise ValueError(f"JSON contains non-finite value: {constant}")


def parse_json_strict(text: str) -> tuple[Any, str | None]:
    """Parse JSON with unique keys and finite constants; retain parse errors."""
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite)
    except (JSONDecodeError, DuplicateJSONKeyError, ValueError, RecursionError) as error:
        return None, str(error)
    return parsed, None


def check_json_depth(text: str, *, max_depth: int) -> None:
    """Bound nesting before decoding; the strict parser validates grammar."""
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError("JSON nesting exceeds configured bound")
        elif char in "]}":
            depth -= 1
