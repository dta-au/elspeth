import json
import math
from typing import Any

# The CPython json module's C-accelerated scanner does not support a custom
# ``object_pairs_hook`` (needed below for duplicate-key rejection), so
# ``json.loads`` falls back to its pure-Python decoder, which recurses once
# per nesting level -- empirically this raises a raw ``RecursionError``
# somewhere between depths of 5,000 and 10,000 with an otherwise-empty call
# stack, and considerably sooner in a real deployment where the ASGI
# middleware stack, the event loop, and any test harness have already
# consumed part of the interpreter's recursion budget (see
# ``test_deeply_nested_but_small_body_rejected_as_too_deep_not_recursion_error``,
# which demonstrates the second-pass ``_check_finite`` walk below hitting
# exactly this at a depth as small as 2,000). 500 leaves a wide safety
# margin under either failure mode while comfortably exceeding any
# realistic JSON payload's real nesting (request bodies, tool-call
# arguments, and schemas in this codebase rarely exceed depth 20).
_MAX_JSON_DEPTH = 500


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
    """Reject any non-finite float anywhere in an already-parsed JSON value tree.

    Walked iteratively with an explicit stack (never recursion): the value
    handed in was itself already depth-bounded by ``_text_exceeds_depth``
    below before ``json.loads`` ever ran, but this is a second, independent
    pass over the resulting structure, so it must not reintroduce the same
    call-stack risk the depth pre-scan exists to avoid.
    """
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float):
            if not math.isfinite(current):
                raise StrictJsonError("non_finite")
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _text_exceeds_depth(text: str, max_depth: int) -> bool:
    """Whether ``text`` (already UTF-8-decoded JSON) nests brackets deeper than ``max_depth``.

    A character-level, iterative (never recursive) pre-scan run *before*
    ``json.loads`` ever sees this text -- see ``_MAX_JSON_DEPTH``'s comment
    above for why the parse itself cannot be trusted to fail safely on an
    adversarially deep input. Nesting is tracked with a plain counter;
    characters inside a JSON string literal (respecting ``\\"`` escapes) are
    skipped so a brace or bracket quoted inside a string value is never
    mistaken for real structural nesting.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > max_depth:
                return True
        elif ch in "}]":
            depth -= 1
    return False


def parse_strict_json(raw: bytes, *, max_bytes: int) -> Any:
    if len(raw) > max_bytes:
        raise StrictJsonError("too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJsonError("invalid_utf8") from exc
    if _text_exceeds_depth(text, _MAX_JSON_DEPTH):
        raise StrictJsonError("too_deep")
    try:
        result = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_reject_constant)
    except StrictJsonError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJsonError("invalid_json") from exc
    _check_finite(result)
    return result
