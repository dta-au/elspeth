"""Size existing standard-JSON framing without constructing its whole text.

Reply framing uses ensure_ascii=False, sort_keys=True, default spaces and
allow_nan=True. This is distinct from RFC 8785 request encoding. Key order
changes no size; payloads and complete escaped scalar strings are never copied
or serialized by this traversal.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.readonly_json import ReadonlyJsonObject


@dataclass(frozen=True, slots=True)
class JsonTextExtent:
    utf8_bytes: int
    escaped_utf8_bytes: int
    unicode_error: UnicodeEncodeError | None = field(default=None, repr=False)


def _integer_characters(value: int) -> int:
    magnitude = abs(value)
    if magnitude == 0:
        return 1
    # A lower estimate avoids allocating a decimal encoding, including when
    # the existing Python conversion limit already proves inadmissibility.
    digits = max(1, ((magnitude.bit_length() - 1) * 1233 >> 12) + 1)
    configured = sys.get_int_max_str_digits()
    if configured and digits > configured:
        raise ValueError("Integer exceeds the existing standard JSON decimal conversion limit")
    while magnitude >= 10**digits:
        digits += 1
    while digits > 1 and magnitude < 10 ** (digits - 1):
        digits -= 1
    if configured and digits > configured:
        raise ValueError("Integer exceeds the existing standard JSON decimal conversion limit")
    return digits + int(value < 0)


def standard_json_text_extent(value: object, *, require_string_keys: bool = False) -> JsonTextExtent:
    """Measure inner JSON UTF-8 and its escaped outer-string content.

    Raw byte size preserves the credential detector's replacement convention.
    Surrogates remain legal inner JSON text; retain their original UTF-8
    failure for the later outer RFC8785 encoding instead of changing grammar.
    """
    raw = escaped = 0
    unicode_error: UnicodeEncodeError | None = None
    active: set[int] = set()

    def ascii_token(size: int) -> None:
        nonlocal raw, escaped
        raw += size
        escaped += size

    def text(source: str) -> None:
        nonlocal raw, escaped, unicode_error
        try:
            base = utf8_size(source)
        except UnicodeEncodeError as error:
            if unicode_error is None:
                unicode_error = error
            base = utf8_size(source, errors="replace")
        raw += 2 + base
        escaped += 4 + base
        for start in range(0, str.__len__(source), 4096):
            chunk = str.__getitem__(source, slice(start, start + 4096))
            quotes = chunk.count('"') + chunk.count("\\")
            short = sum(chunk.count(character) for character in "\b\f\n\r\t")
            long = sum(chunk.count(chr(number)) for number in range(32) if chr(number) not in "\b\f\n\r\t")
            raw += quotes + short + 5 * long
            escaped += 3 * quotes + 2 * short + 6 * long

    def number(source: int | float) -> int:
        if isinstance(source, int):
            return _integer_characters(source)
        # Float spelling is bounded and deliberately keeps stdlib NaN/Inf.
        return len(json.dumps(source, allow_nan=True))

    def visit(node: object) -> None:
        nonlocal raw, escaped
        if node is None:
            ascii_token(4)
        elif isinstance(node, bool):
            ascii_token(4 if node else 5)
        elif isinstance(node, str):
            text(node)
        elif isinstance(node, (int, float)):
            ascii_token(number(node))
        elif isinstance(node, (Mapping, ReadonlyJsonObject, list, tuple)):
            identity = id(node)
            if identity in active:
                raise RecursionError("Circular source in standard JSON framing")
            active.add(identity)
            try:
                ascii_token(2)
                if isinstance(node, (Mapping, ReadonlyJsonObject)):
                    group: str | None = None
                    for key in node:
                        if require_string_keys and not isinstance(key, str):
                            raise AuditIntegrityError("Owned reply framing requires string JSON object keys")
                        current = (
                            "string"
                            if isinstance(key, str)
                            else "number"
                            if isinstance(key, (int, float))
                            else "none"
                            if key is None
                            else "unsupported"
                        )
                        if current == "unsupported" or (group is not None and (current != group or current == "none")):
                            raise TypeError("Source JSON object keys cannot be sorted or encoded")
                        group = current
                    for index, (key, child) in enumerate(node.items()):
                        if index:
                            ascii_token(2)
                        if isinstance(key, str):
                            text(key)
                        else:
                            length = 4 if key is None else (4 if key else 5) if isinstance(key, bool) else number(key)
                            raw += length + 2
                            escaped += length + 4
                        ascii_token(2)
                        visit(child)
                else:
                    for index, child in enumerate(node):
                        if index:
                            ascii_token(2)
                        visit(child)
            finally:
                active.remove(identity)
        else:
            raise TypeError("Source JSON framing contains an unsupported value")

    visit(value)
    return JsonTextExtent(raw, escaped, unicode_error)
