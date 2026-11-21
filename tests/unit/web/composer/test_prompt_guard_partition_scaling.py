"""Partition checks in ``json_content_parts`` are linear and keep the base semantics.

The Advisor renders a session's whole visible transcript as prompt-guard parts
before the request is shaped by age. On the gold-3 battery session that
transcript held 64,863 string parts by edit 44, and the pairwise overlap check
took about 14 s of synchronous work on the event loop, long enough to starve
the 30 s session-operation lease of its 10 s renewals.

``_base_json_content_parts`` below is the implementation at 21e0c91ab, kept
verbatim as the oracle: the linear checks must raise the same error for the
same input and return the same parts otherwise.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust
from elspeth.web.composer import prompt_guard_rendering
from elspeth.web.composer.prompt_guard_rendering import JsonPath, _json_inspection_text, json_content_parts

_COMPLETE_CONTAINER_CHARACTERS = prompt_guard_rendering._COMPLETE_CONTAINER_CHARACTERS


def _base_json_content_parts(
    serialized: str,
    *,
    reference_paths: frozenset[JsonPath] = frozenset(),
    ensure_ascii: bool = True,
    sort_keys: bool = False,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
    string_parts: Mapping[JsonPath, tuple[PromptGuardContentPart, ...]] | None = None,
    encoder: Callable[[JsonValue], str] | None = None,
    decoder: Callable[[str], JsonValue] = json.loads,
) -> tuple[PromptGuardContentPart, ...]:
    """The pairwise implementation at 21e0c91ab, unchanged."""
    value = decoder(serialized)
    partitions = {} if string_parts is None else dict(string_parts)
    for path, units in partitions.items():
        if type(path) is not tuple or type(units) is not tuple or any(type(unit) is not PromptGuardContentPart for unit in units):
            raise AuditIntegrityError("Prompt producer string partition lacks owned semantic units")
        if any(path[: len(reference)] == reference or reference[: len(path)] == path for reference in reference_paths):
            raise AuditIntegrityError("Prompt producer string partition overlaps a reference value")
        if any(other != path and (path[: len(other)] == other or other[: len(path)] == path) for other in partitions):
            raise AuditIntegrityError("Prompt producer string partitions overlap")
    visited: set[JsonPath] = set()
    item_separator, key_separator = separators if separators is not None else ((",", ": ") if indent is not None else (", ", ": "))

    def encode(item: JsonValue) -> str:
        if encoder is not None:
            return encoder(item)
        return json.dumps(item, ensure_ascii=ensure_ascii, sort_keys=sort_keys, indent=indent, separators=separators)

    def render(item: JsonValue, path: JsonPath, depth: int) -> Iterator[PromptGuardContentPart]:
        encoded = encode(item)
        if indent is not None:
            encoded = encoded.replace("\n", "\n" + " " * (indent * depth))
        if path in partitions:
            units = partitions[path]
            if type(item) is not str or "".join(unit.text for unit in units) != item:
                raise AuditIntegrityError("Prompt producer string partition does not cover its complete value")
            visited.add(path)
            yield PromptGuardContentPart('"')
            for unit in units:
                yield PromptGuardContentPart(
                    encode(unit.text)[1:-1],
                    unit.trust,
                    inspection_text=unit.inspection_text if unit.inspection_text is not None else unit.text,
                )
            yield PromptGuardContentPart('"')
        elif path in reference_paths:
            yield PromptGuardContentPart(encoded, PromptGuardTrust.REFERENCE)
        elif (
            isinstance(item, dict | list)
            and len(encoded) <= _COMPLETE_CONTAINER_CHARACTERS
            and not any(reference[: len(path)] == path for reference in (*reference_paths, *partitions))
        ):
            yield PromptGuardContentPart(encoded, inspection_text=_json_inspection_text(encoded))
        elif isinstance(item, dict | list) and item:
            is_object = isinstance(item, dict)
            yield PromptGuardContentPart("{" if is_object else "[")
            entries: Iterable[tuple[str | int, JsonValue]] = (
                (sorted(item.items()) if sort_keys and encoder is None else item.items()) if isinstance(item, dict) else enumerate(item)
            )
            for index, (key, child) in enumerate(entries):
                if index:
                    yield PromptGuardContentPart(item_separator)
                if indent is not None:
                    yield PromptGuardContentPart("\n" + " " * (indent * (depth + 1)))
                if is_object:
                    yield PromptGuardContentPart(json.dumps(key, ensure_ascii=ensure_ascii) + key_separator, inspection_text=str(key))
                yield from render(child, (*path, key), depth + 1)
            if indent is not None:
                yield PromptGuardContentPart("\n" + " " * (indent * depth))
            yield PromptGuardContentPart("}" if is_object else "]")
        else:
            yield PromptGuardContentPart(encoded, inspection_text=item if type(item) is str else None)

    parts = tuple(render(value, (), 0))
    if visited != set(partitions) or "".join(part.text for part in parts) != serialized:
        raise AuditIntegrityError("Prompt producer JSON partition changed its exact wire serialization")
    return parts


def _units(text: str) -> tuple[PromptGuardContentPart, ...]:
    return (PromptGuardContentPart(text),)


def _random_value(rng: random.Random, depth: int) -> JsonValue:
    roll = rng.random()
    if depth >= 4 or roll < 0.35:
        return rng.choice(("alpha", "beta words", "", "é ü", "x" * rng.randint(0, 3_000), 7, None, True))
    if roll < 0.7:
        return {rng.choice(("a", "b", "c", "aa", "0")): _random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}
    return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]


def _paths(value: JsonValue, path: JsonPath = ()) -> Iterator[tuple[JsonPath, JsonValue]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _paths(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _paths(child, (*path, index))


def _case(seed: int) -> tuple[str, dict[JsonPath, tuple[PromptGuardContentPart, ...]], frozenset[JsonPath], dict[str, Any]]:
    rng = random.Random(seed)
    value = _random_value(rng, 0)
    every = list(_paths(value))
    strings = [(path, item) for path, item in every if type(item) is str]
    partitions: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    for path, item in rng.sample(strings, rng.randint(0, len(strings))):
        cut = rng.randint(0, len(item))
        units = tuple(
            PromptGuardContentPart(text, rng.choice((None, PromptGuardTrust.REFERENCE))) for text in (item[:cut], item[cut:]) if text
        )
        partitions[path] = units or _units(item)
    # Sometimes partition a container, a missing path or the root, so prefixes overlap.
    for _ in range(rng.choice((0, 0, 1, 2))):
        partitions[rng.choice([path for path, _item in every] + [("missing",), ()])] = _units("x")
    references = frozenset(rng.sample([path for path, _item in every] + [("missing", 0)], rng.choice((0, 0, 1, 2))))
    options: dict[str, Any] = rng.choice(({}, {"ensure_ascii": False}, {"indent": 2}, {"separators": (",", ":")}, {"sort_keys": True}))
    serialized = json.dumps(value, **options)
    return serialized, partitions, references, options


def _outcome(function: Callable[..., tuple[PromptGuardContentPart, ...]], serialized, partitions, references, options):
    try:
        return ("parts", function(serialized, string_parts=partitions, reference_paths=references, **options))
    except AuditIntegrityError as exc:
        return ("refused", str(exc))


@settings(max_examples=400, deadline=None)
@given(st.integers(min_value=0, max_value=2**32))
def test_linear_checks_match_the_base_on_random_paths(seed: int) -> None:
    serialized, partitions, references, options = _case(seed)
    assert _outcome(json_content_parts, serialized, partitions, references, options) == _outcome(
        _base_json_content_parts, serialized, partitions, references, options
    )


def test_random_cases_reach_every_outcome() -> None:
    """Control for the property test: the generator produces parts and both refusals."""
    seen = set()
    for seed in range(400):
        kind, detail = _outcome(_base_json_content_parts, *_case(seed))
        seen.add(kind if kind == "parts" else detail)
    assert {
        "parts",
        "Prompt producer string partition overlaps a reference value",
        "Prompt producer string partitions overlap",
    } <= seen, seen


@pytest.mark.parametrize(
    ("string_parts", "reference_paths", "message"),
    (
        # A partition below a reference, at it, and above it; an empty reference path.
        ({("a", "b", "c"): _units("x")}, frozenset({("a", "b")}), "overlaps a reference value"),
        ({("a", "b", "c"): _units("x")}, frozenset({("a", "b", "c")}), "overlaps a reference value"),
        ({("a", "b", "c"): _units("x")}, frozenset({("a", "b", "c", "d")}), "overlaps a reference value"),
        ({("a", "b", "c"): _units("x")}, frozenset({()}), "overlaps a reference value"),
        # Nested partitions, whichever comes first; the root partition.
        ({("a", "b"): _units("x"), ("a", "b", "c"): _units("x")}, frozenset(), "partitions overlap"),
        ({("a", "b", "c"): _units("x"), ("a", "b"): _units("x")}, frozenset(), "partitions overlap"),
        ({(): _units("x"), ("a", "b", "c"): _units("x")}, frozenset(), "partitions overlap"),
    ),
)
def test_overlapping_paths_are_still_refused(string_parts, reference_paths, message) -> None:
    serialized = json.dumps({"a": {"b": {"c": "x"}}})
    for function in (json_content_parts, _base_json_content_parts):
        with pytest.raises(AuditIntegrityError, match=message):
            function(serialized, string_parts=string_parts, reference_paths=reference_paths)


def test_sibling_paths_sharing_a_prefix_are_not_an_overlap() -> None:
    serialized = json.dumps({"a": {"b": "x", "bb": "y"}})
    parts = json_content_parts(serialized, string_parts={("a", "b"): _units("x"), ("a", "bb"): _units("y")})
    assert "".join(part.text for part in parts) == serialized


def _wide_case(events: int) -> tuple[str, dict[JsonPath, tuple[PromptGuardContentPart, ...]]]:
    rows = [{"role": "tool", "content": f"result {index}", "tool_call_id": f"call-{index}"} for index in range(events)]
    serialized = json.dumps({"events": rows}, ensure_ascii=False)
    string_parts: dict[JsonPath, tuple[PromptGuardContentPart, ...]] = {}
    for index, row in enumerate(rows):
        string_parts[("events", index, "content")] = _units(row["content"])
        string_parts[("events", index, "tool_call_id")] = _units(row["tool_call_id"])
    return serialized, string_parts


def _best_of(function: Callable[..., object], serialized: str, string_parts, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        function(serialized, ensure_ascii=False, string_parts=string_parts)
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.timing_sensitive
def test_work_grows_about_linearly_with_the_number_of_partitions() -> None:
    """Four times the partitions costs about four times the work, not sixteen.

    A ratio of two runs of the same function on the same host, not a
    wall-clock deadline. On the base the 2,000 → 8,000 partition ratio is
    about 14; linear work gives about 4.
    """
    small = _wide_case(1_000)
    large = _wide_case(4_000)
    ratio = _best_of(json_content_parts, *large, repeats=3) / _best_of(json_content_parts, *small, repeats=5)
    assert ratio < 8.0, f"2,000 → 8,000 partitions cost ratio {ratio:.1f} (linear ≈ 4, pairwise ≈ 16)"
