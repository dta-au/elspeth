"""Strict JSON decoding for integrity-bearing ELSPETH artifacts."""

from __future__ import annotations

import json
from typing import Any, Never


class StrictJSONError(ValueError):
    """JSON is malformed or contains an ambiguous duplicate object key."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _reject_non_json_numeric_constant(constant: str) -> Never:
    raise StrictJSONError(f"non-JSON numeric constant {constant!r}")


def strict_json_loads(payload: str | bytes) -> Any:
    """Decode JSON while rejecting duplicate keys and non-JSON constants."""
    try:
        return json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_numeric_constant,
        )
    except json.JSONDecodeError as exc:
        raise StrictJSONError(str(exc)) from exc
