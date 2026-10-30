"""Measure JSON and UTF-8 without allocating an encoding of the complete input.

The canonical counter accepts the same primitive and frozen-container domain as
``contracts.hashing.canonical_json``.  An excessive size is an observation, not a
serialization error: the remaining input is still validated.  Callers decide
when that observation may become a resource refusal.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import rfc8785

from elspeth.contracts.readonly_json import ReadonlyJsonArray, ReadonlyJsonObject, ReadonlyJsonString

_CHUNK_CHARACTERS = 4096
_SURROGATE = re.compile(r"[\ud800-\udfff]")
_SAFE_INTEGER_MAX = 2**53 - 1
_SHORT_ESCAPES = '"\\\b\f\n\r\t'
_LONG_ESCAPES = tuple(chr(value) for value in range(32) if chr(value) not in _SHORT_ESCAPES)


class _CanonicalIntegerDomainError(rfc8785.IntegerDomainError):
    """The existing integer-domain family without formatting a huge integer."""

    def __init__(self) -> None:
        rfc8785.CanonicalizationError.__init__(self, "integer exceeds the canonical JSON safe domain")


def _require_utf8(text: str, *, object_key: bool = False) -> None:
    match = _SURROGATE.search(text)
    if match is not None:
        # RFC 8785 sorts object keys using UTF-16BE before encoding their text.
        # Keep that failure's encoding, including its original input/offset.
        encoding = "utf-16-be" if object_key else "utf-8"
        raise UnicodeEncodeError(encoding, text, match.start(), match.start() + 1, "surrogates not allowed")


def utf8_size(text: str, *, errors: Literal["strict", "replace"] = "strict", limit: int | None = None) -> int:
    """Return exact UTF-8 size, or ``limit + 1`` after a supplied limit is exceeded.

    Strict mode validates the complete string even when its size already proves
    excess.  Replacement mode preserves ``str.encode(errors="replace")``'s
    behavior, including surrogate replacement.  No encoded chunk exceeds 16 KiB.
    """
    if errors == "strict":
        _require_utf8(text)
    elif errors != "replace":
        raise ValueError("UTF-8 measurement requires strict or replace errors")
    length = str.__len__(text)
    if limit is not None and length > limit:
        return limit + 1
    if str.isascii(text):
        return length
    size = 0
    for start in range(0, length, _CHUNK_CHARACTERS):
        chunk = str.__getitem__(text, slice(start, start + _CHUNK_CHARACTERS))
        size += len(chunk.encode("utf-8", errors=errors))
        if limit is not None and size > limit:
            return limit + 1
    return size


@dataclass(frozen=True, slots=True)
class BoundedCanonicalSize:
    """Exact admitted size; ``None`` records excess without inventing a size."""

    admitted_bytes: int | None
    limit: int

    @property
    def exceeded(self) -> bool:
        return self.admitted_bytes is None


def bounded_canonical_json_size(value: object, *, limit: int) -> BoundedCanonicalSize:
    """Measure RFC 8785 JSON without normalizing or serializing the whole input.

    Object key order changes no byte count.  Strings use bounded chunks; only
    finite numeric scalars are delegated to the existing RFC 8785 encoder.
    Cycles and invalid primitives still fail after the counter saturates.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("Canonical JSON byte limit must be a positive integer")
    size = 0
    active: set[int] = set()

    def add(amount: int) -> None:
        nonlocal size
        size = min(limit + 1, size + amount)

    def string(text: str, *, object_key: bool = False) -> None:
        try:
            _require_utf8(text, object_key=object_key)
        except UnicodeEncodeError as exc:
            if object_key:
                raise
            raise rfc8785.CanonicalizationError("input contains non-UTF-8 codepoints") from exc
        add(2)
        length = str.__len__(text)
        if size + length > limit:
            add(length)
            return
        for start in range(0, length, _CHUNK_CHARACTERS):
            chunk = str.__getitem__(text, slice(start, start + _CHUNK_CHARACTERS))
            extra = sum(chunk.count(character) for character in _SHORT_ESCAPES)
            extra += 5 * sum(chunk.count(character) for character in _LONG_ESCAPES)
            add(len(chunk.encode("utf-8")) + extra)
            if size > limit:
                return

    def visit(node: object) -> None:
        if node is None:
            add(4)
        elif isinstance(node, bool):
            add(4 if node else 5)
        elif isinstance(node, str):
            string(node)
        elif isinstance(node, ReadonlyJsonString):
            measured = node.canonical_string_bytes(limit=limit)
            if type(measured) is not int or not 2 <= measured <= limit + 1:
                raise ValueError("Owned JSON text projection returned an invalid bounded size")
            add(measured)
        elif isinstance(node, int):
            # IntegerDomainError formats its argument.  Reject before that
            # formatting can allocate a decimal representation of a huge int.
            if node < -_SAFE_INTEGER_MAX or node > _SAFE_INTEGER_MAX:
                raise _CanonicalIntegerDomainError()
            add(len(rfc8785.dumps(node)))
        elif isinstance(node, float):
            if math.isnan(node):
                raise ValueError(f"Cannot canonicalize NaN. Use None for missing values, not NaN. Got: {node!r}")
            if math.isinf(node):
                raise ValueError(f"Cannot canonicalize Infinity. Use None for missing values, not Infinity. Got: {node!r}")
            add(len(rfc8785.dumps(node)))
        elif isinstance(node, frozenset):
            raise TypeError("frozenset is not JSON-serializable and has no canonical ordering. Use list or tuple for ordered collections.")
        elif isinstance(node, (Mapping, list, tuple, ReadonlyJsonObject, ReadonlyJsonArray)):
            identity = id(node)
            if identity in active:
                raise RecursionError("Circular reference in canonical JSON input")
            active.add(identity)
            try:
                add(2)
                first = True
                if isinstance(node, (Mapping, ReadonlyJsonObject)):
                    for key, item in node.items():
                        if not isinstance(key, str):
                            raise rfc8785.CanonicalizationError("object keys must be strings")
                        if not first:
                            add(1)
                        first = False
                        string(key, object_key=True)
                        add(1)
                        visit(item)
                else:
                    for item in node:
                        if not first:
                            add(1)
                        first = False
                        visit(item)
            finally:
                active.remove(identity)
        else:
            raise rfc8785.CanonicalizationError(f"unsupported type: {type(node)}")

    visit(value)
    return BoundedCanonicalSize(admitted_bytes=size if size <= limit else None, limit=limit)
