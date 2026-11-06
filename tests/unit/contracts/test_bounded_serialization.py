"""Exact aggregate byte counting and bounded UTF-8 measurement contracts."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest
import rfc8785
from hypothesis import given
from hypothesis import strategies as st

from elspeth.contracts.bounded_serialization import bounded_canonical_json_size, utf8_size
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_text_measure import standard_json_text_extent


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        -(2**53 - 1),
        2**53 - 1,
        -0.0,
        1e-7,
        1e17,
        1e30,
        'quote" backslash\\ newline\n controls\x00\x1f',
        {"é": ["🧵", "\b\f\r\t", 1.0], "a": {}},
        MappingProxyType({"messages": (MappingProxyType({"role": "user", "content": "λ"}),), "tools": ()}),
    ],
)
def test_exact_boundary_matches_declared_canonical_bytes(value: object) -> None:
    expected = len(canonical_json(value).encode("utf-8"))
    admitted = bounded_canonical_json_size(value, limit=expected)
    assert admitted.admitted_bytes == expected
    assert admitted.limit == expected
    assert not admitted.exceeded
    if expected > 1:
        excess = bounded_canonical_json_size(value, limit=expected - 1)
        assert excess.exceeded
        assert excess.admitted_bytes is None


_JSON_SCALARS = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(st.characters(exclude_categories=("Cs",)), max_size=150)
)
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda child: (
        st.lists(child, max_size=5) | st.dictionaries(st.text(st.characters(exclude_categories=("Cs",)), max_size=20), child, max_size=5)
    ),
    max_leaves=30,
)


@given(_JSON_VALUES)
def test_counter_matches_canonical_encoder_over_json_domain(value: object) -> None:
    expected = len(canonical_json(value).encode("utf-8"))
    assert bounded_canonical_json_size(value, limit=expected).admitted_bytes == expected
    assert bounded_canonical_json_size(value, limit=max(1, expected - 1)).exceeded is (expected > 1)


def test_oversized_strings_are_not_passed_to_whole_value_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    original = rfc8785.dumps
    calls: list[object] = []

    def numeric_scalar(value: object) -> bytes:
        assert type(value) in (int, float), "whole string/container encoding preceded aggregate admission"
        calls.append(value)
        return original(value)

    monkeypatch.setattr(rfc8785, "dumps", numeric_scalar)
    assert rfc8785.dumps(1) == b"1"
    with pytest.raises(AssertionError, match="whole string/container"):
        rfc8785.dumps({"instrument_control": "must fail"})
    calls.clear()
    value = {"messages": [{"role": "user", "content": "é" * 600_000}], "tools": [], "numbers": [-0.0, 1e30]}
    assert bounded_canonical_json_size(value, limit=64).exceeded
    assert calls == [-0.0, 1e30]


@pytest.mark.parametrize(
    ("invalid", "error"),
    [
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (float("-inf"), ValueError),
        (2**53, rfc8785.IntegerDomainError),
        pytest.param(1 << 100_000, rfc8785.IntegerDomainError, id="huge-integer"),
        (frozenset({1}), TypeError),
        (object(), rfc8785.CanonicalizationError),
        ("late\ud800", rfc8785.CanonicalizationError),
        ({1: "not a string key"}, rfc8785.CanonicalizationError),
        ({"late\ud800": "invalid key"}, UnicodeEncodeError),
    ],
)
def test_excess_does_not_hide_later_serialization_failure(invalid: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        bounded_canonical_json_size(["x" * 100_000, invalid], limit=16)


def test_cycles_remain_serialization_failure_after_counter_saturates() -> None:
    value: list[object] = ["x" * 100_000]
    value.append(value)
    with pytest.raises(RecursionError, match="Circular reference"):
        bounded_canonical_json_size(value, limit=16)


def test_repeated_frozen_alias_is_not_a_cycle() -> None:
    shared = MappingProxyType({"text": "é"})
    value = (shared, shared)
    expected = len(canonical_json(value).encode("utf-8"))
    assert bounded_canonical_json_size(value, limit=expected).admitted_bytes == expected


@pytest.mark.parametrize("limit", [0, -1, True, False])
def test_counter_limit_requires_exact_positive_integer(limit: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        bounded_canonical_json_size({}, limit=limit)


@pytest.mark.parametrize("text", ["", "ascii", "éλ🧵", "x" * 4095 + "🧵é", "\ud800x\udfff"])
def test_utf8_replacement_preserves_original_bytes_and_threshold(text: str) -> None:
    expected = len(text.encode("utf-8", errors="replace"))
    assert utf8_size(text, errors="replace") == expected
    assert utf8_size(text, errors="replace", limit=expected) == expected
    assert utf8_size(text, errors="replace", limit=expected - 1) == expected
    assert utf8_size(text, errors="replace", limit=0) == min(expected, 1)


def test_utf8_strict_excess_still_validates_complete_late_text() -> None:
    with pytest.raises(UnicodeEncodeError):
        utf8_size("x" * 100_000 + "\ud800", limit=1)


def test_utf8_strict_measures_complete_multibyte_string() -> None:
    text = "🧵é" * 100_000
    assert utf8_size(text) == len(text.encode("utf-8"))
    assert utf8_size(text, limit=599_999) == 600_000


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        1.0,
        -0.0,
        1e30,
        2**64,
        float("nan"),
        float("inf"),
        float("-inf"),
        'quote" slash\\\n\b\f\r\t\x00\x1féλ🧵',
        {"z": ["\x00", -0.0, 1.0, 2**64], "a": {"text": "🧵"}},
        {2: "two", 10: "ten"},
        {False: "false", True: "true"},
    ],
)
def test_reply_inner_json_extent_matches_stdlib_and_outer_string_escaping(value: object) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    expected_raw = len(serialized.encode("utf-8"))
    expected_escaped = len(json.dumps(serialized, ensure_ascii=False).encode("utf-8")) - 2
    extent = standard_json_text_extent(value)
    assert extent.utf8_bytes == expected_raw
    assert extent.escaped_utf8_bytes == expected_escaped
    assert extent.unicode_error is None


@given(_JSON_VALUES)
def test_reply_inner_json_extent_matches_independent_encoder_over_nested_json(value: object) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    extent = standard_json_text_extent(value)
    assert extent.utf8_bytes == len(serialized.encode("utf-8"))
    assert extent.escaped_utf8_bytes == len(json.dumps(serialized, ensure_ascii=False).encode("utf-8")) - 2


def test_reply_extent_never_encodes_an_entire_string_or_container(monkeypatch: pytest.MonkeyPatch) -> None:
    original = json.dumps
    encoded: list[object] = []

    def scalar_only(value: object, **kwargs: object) -> str:
        assert type(value) is float, "whole reply framing was serialized before admission"
        encoded.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(json, "dumps", scalar_only)
    assert json.dumps(1.0) == "1.0"
    with pytest.raises(AssertionError, match="whole reply framing"):
        json.dumps({"instrument_control": "must fail"})
    encoded.clear()
    source = {"content": 'é\\"\n' * 200_000, "number": -0.0}
    extent = standard_json_text_extent(source)
    assert extent.utf8_bytes > 1_000_000
    assert encoded == [-0.0]


def test_reply_extent_preserves_surrogate_failure_for_outer_utf8_after_inner_framing() -> None:
    source = {"content": "prefix\ud800suffix"}
    serialized = json.dumps(source, ensure_ascii=False, sort_keys=True)
    extent = standard_json_text_extent(source)
    assert extent.utf8_bytes == len(serialized.encode("utf-8", errors="replace"))
    assert isinstance(extent.unicode_error, UnicodeEncodeError)
    with pytest.raises(UnicodeEncodeError):
        serialized.encode("utf-8")


@pytest.mark.parametrize("source", [{"string": 1, 2: "numeric"}, {object(): "unsupported"}, {"late": frozenset({1})}])
def test_reply_extent_retains_inner_serialization_errors(source: object) -> None:
    with pytest.raises(TypeError):
        json.dumps(source, ensure_ascii=False, sort_keys=True)
    with pytest.raises(TypeError):
        standard_json_text_extent(source)
