"""Strict parsing helpers for content a plugin binds as configuration.

A plugin that takes file content as a config option (a reference table, an
injected document) parses it at config load, where the result becomes part of
node identity. These helpers refuse the inputs that would make that identity
ambiguous or unbounded: duplicate JSON object members, the non-finite JSON
constants, and values whose canonical size exceeds a caller-chosen limit.

The errors raised here are deliberately content-neutral. A caller that wants
its own wording (naming its option, entry or limit) catches them and re-raises.
"""

from __future__ import annotations

from collections.abc import Mapping


class DuplicateJsonObjectMemberError(ValueError):
    """A JSON object repeated a member name, so its meaning is ambiguous."""

    def __init__(self, member_name: str) -> None:
        super().__init__(f"duplicate JSON object member {member_name!r}")
        self.member_name = member_name


class CanonicalSizeLimitExceededError(ValueError):
    """A value's canonical-JSON size exceeds the limit the caller allowed."""

    def __init__(self) -> None:
        super().__init__("canonical JSON size exceeds the limit")


class NonStringObjectKeyError(TypeError):
    """A mapping being sized has a key that is not a ``str``."""

    def __init__(self) -> None:
        super().__init__("objects must use string keys")


class NonCanonicalValueTypeError(TypeError):
    """A value being sized has a type canonical JSON cannot represent."""

    def __init__(self, type_name: str) -> None:
        super().__init__(f"value of type {type_name} is not canonical JSON")
        self.type_name = type_name


def reject_duplicate_json_object_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Preserve each JSON object's member names only when they are unambiguous.

    Pass as ``object_pairs_hook`` to ``json.loads`` / ``json.load``.

    Raises:
        DuplicateJsonObjectMemberError: an object repeats a member name.
    """
    members: dict[str, object] = {}
    for name, value in pairs:
        if name in members:
            raise DuplicateJsonObjectMemberError(name)
        members[name] = value
    return members


def reject_nonfinite_constant(value: str) -> None:
    """Reject non-standard JSON constants (NaN, Infinity, -Infinity).

    Python's json module accepts these by default, but they violate:
    1. RFC 8259 (JSON standard) - only allows null, true, false
    2. Canonical JSON policy - non-finite floats crash hashing

    This function is passed to json.loads/json.load via parse_constant
    parameter to reject these values at parse time.

    Args:
        value: The constant string (NaN, Infinity, or -Infinity)

    Raises:
        ValueError: Always - these constants are not allowed
    """
    raise ValueError(f"Non-standard JSON constant '{value}' not allowed. Use null for missing values, not NaN/Infinity.")


def bounded_canonical_size(value: object, *, limit: int) -> int:
    """Return canonical-JSON byte size while stopping at ``limit``.

    The walk stops as soon as the running size passes ``limit``, so a hostile
    or oversized value is never serialized to measure it. Every float is
    budgeted at a fixed 32 bytes, an upper bound on its canonical form.

    Raises:
        CanonicalSizeLimitExceededError: the size exceeds ``limit``.
        NonStringObjectKeyError: a mapping has a non-``str`` key.
        NonCanonicalValueTypeError: a value is not None, bool, str, int,
            float, a mapping, a list or a tuple.
    """

    def add(total: int, increment: int) -> int:
        total += increment
        if total > limit:
            raise CanonicalSizeLimitExceededError
        return total

    def string_size(text: str) -> int:
        size = add(0, 2)
        for character in text:
            codepoint = ord(character)
            if character in ('"', "\\"):
                width = 2
            elif codepoint <= 0x1F:
                width = 6
            elif codepoint <= 0x7F:
                width = 1
            elif codepoint <= 0x7FF:
                width = 2
            elif codepoint <= 0xFFFF:
                width = 3
            else:
                width = 4
            size = add(size, width)
        return size

    if value is None:
        return add(0, 4)
    if type(value) is bool:
        return add(0, 4 if value else 5)
    if type(value) is str:
        return string_size(value)
    if type(value) is int:
        return add(0, len(str(value)))
    if type(value) is float:
        return add(0, 32)
    if isinstance(value, Mapping):
        total = add(0, 2)
        for index, (key, item) in enumerate(value.items()):
            if type(key) is not str:
                raise NonStringObjectKeyError
            if index:
                total = add(total, 1)
            total = add(total, string_size(key))
            total = add(total, 1)
            total = add(total, bounded_canonical_size(item, limit=limit - total))
        return total
    if isinstance(value, (list, tuple)):
        total = add(0, 2)
        for index, item in enumerate(value):
            if index:
                total = add(total, 1)
            total = add(total, bounded_canonical_size(item, limit=limit - total))
        return total
    raise NonCanonicalValueTypeError(type(value).__name__)
