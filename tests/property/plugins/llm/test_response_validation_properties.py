# tests/property/plugins/llm/test_response_validation_properties.py
"""Property-based tests for LLM response validation.

Per ELSPETH's Three-Tier Trust Model:
- LLM responses are Tier 3 (external data) - zero trust
- Validation must happen IMMEDIATELY at the boundary
- Invalid responses must be caught, not silently coerced

These tests exercise the PRODUCTION validation code in:
    src/elspeth/plugins/transforms/llm/validation.py (extract_structured_fields,
    the parser the LLM transform and the LLM source run)

They verify that validation correctly handles:
- Non-JSON responses
- Wrong JSON types (array when object expected)
- Truncated/partial JSON
- Valid responses (positive cases)
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.contracts.errors import TransformErrorReason
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, OutputFieldType
from elspeth.plugins.transforms.llm.validation import extract_structured_fields
from tests.strategies.json import json_primitives

# =============================================================================
# Strategies for generating LLM-like responses
# =============================================================================

# Valid JSON object responses
valid_json_objects = st.dictionaries(
    keys=st.text(min_size=1, max_size=20, alphabet=st.characters(whitelist_categories=["L"])),
    values=json_primitives,
    min_size=0,  # Empty object {} is valid
    max_size=10,
)

# Non-JSON strings - use explicit patterns instead of filter() for efficiency
# (Reviewer fix: avoid .filter(not _is_valid_json) which rejects >99% of inputs)
non_json_strings = st.one_of(
    st.just(""),
    st.just("{"),
    st.just('{"incomplete": '),
    st.just("[1, 2, 3"),
    st.just("{invalid}"),
    st.just("{{double braces}}"),
    st.sampled_from(
        [
            "This is not JSON",
            "Error: API rate limit exceeded",
            "<html>Error</html>",
            "None",
            "undefined",
            "{'single': 'quotes'}",  # Python dict, not JSON
        ]
    ),
)

# JSON that parses but is wrong type (array, not object)
wrong_type_json = st.one_of(
    st.lists(json_primitives, min_size=0, max_size=5).map(json.dumps),
    st.just("null"),
    st.just("true"),
    st.just("false"),
    st.integers().map(str),
    st.text(max_size=50).map(lambda s: json.dumps(s)),
)


# =============================================================================
# Property Tests: JSON Parse Boundary (Testing PRODUCTION code)
# =============================================================================


_ANSWER = (OutputFieldConfig(suffix="answer", type=OutputFieldType.STRING),)


def _parse_object(content: str) -> TransformErrorReason | None:
    """Parse with no declared fields: only the JSON-object boundary applies."""
    extracted, reason = extract_structured_fields(content, ())
    assert extracted == {}
    return reason


class TestLLMResponseParsingProperties:
    """Property tests for LLM response JSON parsing."""

    @given(response=valid_json_objects)
    @settings(max_examples=100)
    def test_valid_json_object_succeeds(self, response: dict[str, Any]) -> None:
        """Property: Valid JSON objects are accepted."""
        assert _parse_object(json.dumps(response)) is None

    @given(response=valid_json_objects, answer=st.text(max_size=50))
    @settings(max_examples=100)
    def test_declared_field_round_trips(self, response: dict[str, Any], answer: str) -> None:
        """Property: A declared string field is extracted exactly as sent."""
        content = json.dumps({**response, "answer": answer})
        assert extract_structured_fields(content, _ANSWER) == ({"answer": answer}, None)

    @given(response=non_json_strings)
    @settings(max_examples=100)
    def test_non_json_rejected(self, response: str) -> None:
        """Property: Non-JSON strings are rejected with clear error."""
        reason = _parse_object(response)

        assert reason is not None
        assert reason["reason"] == "json_parse_failed"

    @given(response=wrong_type_json)
    @settings(max_examples=100)
    def test_wrong_json_type_rejected(self, response: str) -> None:
        """Property: JSON that isn't an object is rejected.

        LLM transforms expect {"field": value} responses, not arrays,
        primitives, or null.
        """
        reason = _parse_object(response)

        assert reason is not None
        assert reason["reason"] == "invalid_json_type"
        assert reason["expected"] == "object"


class TestLLMResponseEdgeCases:
    """Property tests for LLM response edge cases."""

    def test_empty_object_accepted(self) -> None:
        """Property: Empty object {} is valid when nothing is declared."""
        assert _parse_object("{}") is None

    def test_deeply_nested_accepted(self) -> None:
        """Property: Nested objects within the depth bound are accepted."""
        deep = {"a": {"b": {"c": {"d": {"e": "value"}}}}}
        assert _parse_object(json.dumps(deep)) is None

    @given(whitespace=st.sampled_from([" ", "\n", "\t", "\r\n"]))
    @settings(max_examples=20)
    def test_whitespace_padded_json_accepted(self, whitespace: str) -> None:
        """Property: JSON with leading/trailing whitespace is accepted."""
        content = f'{whitespace}{{"answer": "value"}}{whitespace}'
        assert extract_structured_fields(content, _ANSWER) == ({"answer": "value"}, None)

    def test_null_json_rejected(self) -> None:
        """Property: JSON null is rejected (not an object)."""
        reason = _parse_object("null")

        assert reason is not None
        assert reason["reason"] == "invalid_json_type"

    def test_array_json_rejected(self) -> None:
        """Property: JSON array is rejected (not an object)."""
        reason = _parse_object("[1, 2, 3]")

        assert reason is not None
        assert reason["reason"] == "invalid_json_type"
        assert reason["actual"] == "list"

    @pytest.mark.parametrize(
        "content",
        [
            '{"score": NaN}',
            '{"score": Infinity}',
            '{"score": -Infinity}',
        ],
    )
    def test_non_finite_constants_rejected(self, content: str) -> None:
        """Non-standard JSON constants are rejected at parse boundary."""
        reason = _parse_object(content)

        assert reason is not None
        assert reason["reason"] == "json_parse_failed"


class TestLLMResponseDeterminism:
    """Property tests for validation determinism."""

    @given(content=st.text(max_size=200))
    @settings(max_examples=100)
    def test_validation_is_deterministic(self, content: str) -> None:
        """Property: Same input always produces same validation result."""
        assert extract_structured_fields(content, _ANSWER) == extract_structured_fields(content, _ANSWER)
