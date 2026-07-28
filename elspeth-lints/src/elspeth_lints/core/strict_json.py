"""Strict JSON decoding for integrity-bearing ELSPETH artifacts."""

from __future__ import annotations

import json
from typing import Any


class StrictJSONError(ValueError):
    """JSON is malformed or contains an ambiguous duplicate object key."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def strict_json_loads(payload: str | bytes) -> Any:
    """Decode JSON while rejecting duplicate keys at every object depth."""
    try:
        return json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise StrictJSONError(str(exc)) from exc
