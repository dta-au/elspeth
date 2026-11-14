"""Strict parsing helpers shared by plugins that bind content as configuration."""

from __future__ import annotations

import json

import pytest

from elspeth.plugins.infrastructure.config_content import (
    CanonicalSizeLimitExceededError,
    DuplicateJsonObjectMemberError,
    NonCanonicalValueTypeError,
    NonStringObjectKeyError,
    bounded_canonical_size,
    reject_duplicate_json_object_members,
    reject_nonfinite_constant,
)


class TestRejectDuplicateJsonObjectMembers:
    def test_duplicate_members_are_refused_naming_the_member(self) -> None:
        with pytest.raises(DuplicateJsonObjectMemberError, match="duplicate JSON object member 'a'") as exc:
            json.loads('{"a": 1, "a": 2}', object_pairs_hook=reject_duplicate_json_object_members)
        assert exc.value.member_name == "a"

    def test_refusal_is_a_value_error(self) -> None:
        assert issubclass(DuplicateJsonObjectMemberError, ValueError)

    def test_nested_duplicate_members_are_refused(self) -> None:
        with pytest.raises(DuplicateJsonObjectMemberError) as exc:
            json.loads('{"outer": {"b": 1, "b": 1}}', object_pairs_hook=reject_duplicate_json_object_members)
        assert exc.value.member_name == "b"

    def test_unambiguous_members_are_preserved_in_order(self) -> None:
        loaded = json.loads('{"b": 1, "a": {"c": null}}', object_pairs_hook=reject_duplicate_json_object_members)
        assert loaded == {"b": 1, "a": {"c": None}}
        assert list(loaded) == ["b", "a"]


class TestRejectNonfiniteConstant:
    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_nonfinite_constant_is_refused(self, constant: str) -> None:
        with pytest.raises(ValueError, match=f"Non-standard JSON constant '{constant}' not allowed"):
            json.loads(f"[{constant}]", parse_constant=reject_nonfinite_constant)

    def test_standard_constants_still_parse(self) -> None:
        assert json.loads("[null, true, false, 1.5]", parse_constant=reject_nonfinite_constant) == [None, True, False, 1.5]


class TestBoundedCanonicalSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, len("null")),
            (True, len("true")),
            (False, len("false")),
            (12345, len("12345")),
            (-7, len("-7")),
            ("abc", len('"abc"')),
            ({"a": 1}, len('{"a":1}')),
            ({"a": 1, "b": [None, "x"]}, len('{"a":1,"b":[null,"x"]}')),
            ([], len("[]")),
            ({}, len("{}")),
            ((1, 2), len("[1,2]")),
        ],
    )
    def test_size_is_the_canonical_json_byte_count(self, value: object, expected: int) -> None:
        assert bounded_canonical_size(value, limit=1000) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('"', 2 + 2),  # escaped quote
            ("\\", 2 + 2),  # escaped backslash
            ("\n", 2 + 6),  # control characters are budgeted as \uXXXX
            ("é", 2 + 2),
            ("€", 2 + 3),
            ("😀", 2 + 4),
        ],
    )
    def test_string_size_counts_escapes_and_utf8_width(self, text: str, expected: int) -> None:
        assert bounded_canonical_size(text, limit=1000) == expected

    def test_every_float_is_budgeted_at_a_fixed_upper_bound(self) -> None:
        assert bounded_canonical_size(1.5, limit=1000) == 32

    def test_a_value_exactly_at_the_limit_is_admitted(self) -> None:
        assert bounded_canonical_size({"a": 1}, limit=7) == 7

    def test_a_value_one_byte_over_the_limit_is_refused(self) -> None:
        with pytest.raises(CanonicalSizeLimitExceededError):
            bounded_canonical_size({"a": 1}, limit=6)

    def test_scalar_over_the_limit_is_refused(self) -> None:
        with pytest.raises(CanonicalSizeLimitExceededError, match="canonical JSON size exceeds the limit"):
            bounded_canonical_size("x" * 100, limit=10)

    def test_nested_values_share_the_callers_limit(self) -> None:
        value = [["x" * 10], ["x" * 10]]
        size = bounded_canonical_size(value, limit=1000)
        assert size == len('[["xxxxxxxxxx"],["xxxxxxxxxx"]]')
        with pytest.raises(CanonicalSizeLimitExceededError):
            bounded_canonical_size(value, limit=size - 1)

    def test_refusal_is_a_value_error(self) -> None:
        assert issubclass(CanonicalSizeLimitExceededError, ValueError)

    def test_non_string_object_key_is_a_value_error(self) -> None:
        with pytest.raises(NonStringObjectKeyError, match="objects must use string keys"):
            bounded_canonical_size({1: "a"}, limit=1000)
        assert issubclass(NonStringObjectKeyError, ValueError)
        assert not issubclass(NonStringObjectKeyError, TypeError)

    def test_non_canonical_value_is_a_value_error_naming_the_type(self) -> None:
        with pytest.raises(NonCanonicalValueTypeError, match="value of type set is not canonical JSON") as exc:
            bounded_canonical_size({"a": {1}}, limit=1000)
        assert exc.value.type_name == "set"
        assert issubclass(NonCanonicalValueTypeError, ValueError)
        assert not issubclass(NonCanonicalValueTypeError, TypeError)

    @pytest.mark.parametrize("value", ["x" * 100, {1: "a"}, [{1, 2}]])
    def test_every_refusal_is_caught_as_value_error(self, value: object) -> None:
        with pytest.raises(ValueError):
            bounded_canonical_size(value, limit=10)
