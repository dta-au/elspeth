"""reference_join restates the shared config-content errors in its own terms.

The parsing helpers live in ``plugins/infrastructure/config_content.py`` and
raise content-neutral errors. These tests pin that reference_join's own
exception types and messages are what a caller sees.
"""

from __future__ import annotations

import json

import pytest

from elspeth.plugins.transforms import reference_join
from elspeth.plugins.transforms.reference_join import (
    ReferenceTableError,
    _bounded_canonical_size,
    _reject_duplicate_json_object_members,
)


def test_size_limit_is_a_reference_table_error_naming_the_index_limit() -> None:
    with pytest.raises(ReferenceTableError) as exc:
        _bounded_canonical_size("x" * 100, limit=10)
    assert str(exc.value) == (
        f"reference table outputs exceed the {reference_join.MAX_REFERENCE_INDEX_BYTES}-byte materialized index limit. "
        "Reduce the table, output map, or retained value sizes."
    )


def test_size_limit_inside_a_container_is_a_reference_table_error() -> None:
    with pytest.raises(ReferenceTableError, match="materialized index limit"):
        _bounded_canonical_size({"a": ["x" * 100]}, limit=10)


def test_size_within_limit_is_returned() -> None:
    assert _bounded_canonical_size({"a": 1}, limit=7) == 7


def test_non_string_object_key_keeps_its_type_error_text() -> None:
    with pytest.raises(TypeError) as exc:
        _bounded_canonical_size({1: "a"}, limit=1000)
    assert type(exc.value) is TypeError
    assert str(exc.value) == "reference output objects must use string keys"


def test_non_canonical_value_keeps_its_type_error_text() -> None:
    with pytest.raises(TypeError) as exc:
        _bounded_canonical_size([{1, 2}], limit=1000)
    assert type(exc.value) is TypeError
    assert str(exc.value) == "reference output value of type set is not canonical JSON"


def test_duplicate_member_is_a_reference_table_error() -> None:
    with pytest.raises(ReferenceTableError) as exc:
        json.loads('{"a": 1, "a": 2}', object_pairs_hook=_reject_duplicate_json_object_members)
    assert type(exc.value) is ReferenceTableError
    assert str(exc.value) == "reference table has a duplicate JSON object member 'a'"
