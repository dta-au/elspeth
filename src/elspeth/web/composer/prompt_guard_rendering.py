"""Lossless semantic parts for JSON serialized by owned prompt producers."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust

type JsonPath = tuple[str | int, ...]

# Keep small complete objects/arrays together rather than paying a separate
# service evidence envelope for each punctuation token. Larger containers
# recurse at structural boundaries; scalar strings are never cut.
_COMPLETE_CONTAINER_CHARACTERS = 2_048


def json_content_parts(
    serialized: str,
    *,
    reference_paths: frozenset[JsonPath] = frozenset(),
    ensure_ascii: bool = True,
    sort_keys: bool = False,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
) -> tuple[PromptGuardContentPart, ...]:
    """Retain complete scalar tokens and exact original serializer bytes.

    Callers supply their own serialized JSON and explicit paths whose values
    they obtained from the current registered catalog. No key or decoded value
    grants trust. A formatting mismatch is an owned producer error.
    """
    value: JsonValue = json.loads(serialized)
    item_separator, key_separator = separators if separators is not None else ((",", ": ") if indent is not None else (", ", ": "))

    def encode(item: JsonValue) -> str:
        return json.dumps(item, ensure_ascii=ensure_ascii, sort_keys=sort_keys, indent=indent, separators=separators)

    def render(item: JsonValue, path: JsonPath, depth: int) -> Iterator[PromptGuardContentPart]:
        encoded = encode(item)
        if indent is not None:
            encoded = encoded.replace("\n", "\n" + " " * (indent * depth))
        if path in reference_paths:
            yield PromptGuardContentPart(encoded, PromptGuardTrust.REFERENCE)
        elif (
            isinstance(item, dict | list)
            and len(encoded) <= _COMPLETE_CONTAINER_CHARACTERS
            and not any(reference[: len(path)] == path for reference in reference_paths)
        ):
            yield PromptGuardContentPart(encoded)
        elif isinstance(item, dict | list) and item:
            is_object = isinstance(item, dict)
            yield PromptGuardContentPart("{" if is_object else "[")
            entries: Iterable[tuple[str | int, JsonValue]] = (
                (sorted(item.items()) if sort_keys else item.items()) if isinstance(item, dict) else enumerate(item)
            )
            for index, (key, child) in enumerate(entries):
                if index:
                    yield PromptGuardContentPart(item_separator)
                if indent is not None:
                    yield PromptGuardContentPart("\n" + " " * (indent * (depth + 1)))
                if is_object:
                    yield PromptGuardContentPart(json.dumps(key, ensure_ascii=ensure_ascii) + key_separator)
                yield from render(child, (*path, key), depth + 1)
            if indent is not None:
                yield PromptGuardContentPart("\n" + " " * (indent * depth))
            yield PromptGuardContentPart("}" if is_object else "]")
        else:
            yield PromptGuardContentPart(encoded)

    parts = tuple(render(value, (), 0))
    if "".join(part.text for part in parts) != serialized:
        raise AuditIntegrityError("Prompt producer JSON partition changed its exact wire serialization")
    return parts


def json_argument_content_parts(serialized: str) -> tuple[PromptGuardContentPart, ...]:
    """Retain complete decoded argument values in supported wire renderings.

    Tool admission has already parsed this argument object. Accept the common
    JSON encoder renderings only when reconstruction is byte-identical. Any
    other formatting remains one complete untrusted string; never normalize
    or cut provider-authored bytes to obtain admission.
    """
    for ensure_ascii in (True, False):
        for separators in (None, (",", ":")):
            try:
                return json_content_parts(serialized, ensure_ascii=ensure_ascii, separators=separators)
            except AuditIntegrityError:
                continue
    return (PromptGuardContentPart(serialized),)
