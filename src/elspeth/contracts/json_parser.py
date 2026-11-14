"""Strict JSON parsing shared by audit contracts and external clients."""

from __future__ import annotations

import json
import math
import sys
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
    """Parse JSON with unique keys and finite constants; retain parse errors.

    Nesting is bounded by the interpreter recursion limit. Before Python 3.14
    the C decoder counted its recursion against that limit and raised
    ``RecursionError``; from 3.14 it guards only the C stack and decodes far
    deeper documents, which Python-level walkers of the result then cannot
    traverse. The explicit bound keeps the parse-error contract.
    """
    max_depth = sys.getrecursionlimit()
    # Counting openers is C-speed; only a text with enough of them to exceed
    # the bound pays for the character scan.
    if text.count("[") + text.count("{") > max_depth:
        try:
            check_json_depth(text, max_depth=max_depth)
        except ValueError as error:
            return None, str(error)
    try:
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite)
    except (JSONDecodeError, DuplicateJSONKeyError, ValueError, RecursionError) as error:
        return None, str(error)
    return parsed, None


def _reject_overflow_float(literal: str) -> float:
    """Reject numeric literals that overflow to ``inf`` (e.g. ``1e999``)."""
    parsed = float(literal)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON contains non-finite number: {literal[:64]}")
    return parsed


def parse_json_bounded(text: str, *, max_depth: int) -> tuple[Any, str | None]:
    """Parse external JSON with a nesting bound, unique keys and finite numbers.

    Unlike :func:`parse_json_strict`, this also refuses nesting deeper than
    ``max_depth`` (checked before decoding, so the decoder never recurses on
    hostile input) and numeric literals that overflow to infinity. It does not
    check the canonical integer domain: callers that hash the result must
    still run ``canonical_json`` over it.
    """
    try:
        check_json_depth(text, max_depth=max_depth)
    except ValueError as error:
        return None, str(error)
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
            parse_float=_reject_overflow_float,
        )
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
