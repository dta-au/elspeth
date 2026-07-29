import json
import math
from typing import Any


class StrictJsonError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"strict json rejection: {reason}")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJsonError("duplicate_key")
        obj[key] = value
    return obj


def _reject_constant(_value: str) -> Any:
    raise StrictJsonError("non_finite")


def _check_finite(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StrictJsonError("non_finite")
    elif isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
    elif isinstance(value, list):
        for item in value:
            _check_finite(item)


def parse_strict_json(raw: bytes, *, max_bytes: int) -> Any:
    if len(raw) > max_bytes:
        raise StrictJsonError("too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJsonError("invalid_utf8") from exc
    try:
        result = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
    except StrictJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJsonError("invalid_json") from exc
    _check_finite(result)
    return result
