"""LLM response validation utilities.

Per ELSPETH's Three-Tier Trust Model:
- LLM responses are Tier 3 (external data) - zero trust
- Validation must happen IMMEDIATELY at the boundary
- Invalid responses must be caught, not silently coerced

This module extracts the common validation pattern from LLM transforms
so it can be:
1. Reused across all LLM plugin implementations
2. Property-tested with Hypothesis

Shared helpers:
- strip_markdown_fences: Strip markdown code block wrappers from LLM output
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from elspeth.contracts.errors import TransformErrorReason
from elspeth.contracts.freeze import deep_freeze
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, OutputFieldType, ResponseFormat


def reject_nonfinite_constant(value: str) -> None:
    """Reject non-standard JSON constants (NaN, Infinity, -Infinity).

    Used as ``parse_constant`` argument to ``json.loads`` at every Tier 3
    boundary where LLM JSON responses are parsed.
    """
    raise ValueError(f"Non-standard JSON constant '{value}' not allowed")


def validate_field_value(
    value: Any,
    field_config: OutputFieldConfig,
) -> str | None:
    """Validate a parsed JSON value against its declared output field type.

    Tier 3 boundary enforcement: LLM responses may contain values that parse
    as valid JSON but violate the declared schema (e.g., string where integer
    expected, boolean where number expected, non-finite floats).

    Args:
        value: The parsed JSON value from the LLM response
        field_config: Expected type configuration from output_fields

    Returns:
        Error message string if validation fails, None if valid
    """
    expected_type = field_config.type

    if expected_type == OutputFieldType.STRING:
        if not isinstance(value, str):
            return f"expected string, got {type(value).__name__}"

    elif expected_type == OutputFieldType.INTEGER:
        # bool is subclass of int in Python — reject explicitly
        if isinstance(value, bool):
            return "expected integer, got boolean"
        if isinstance(value, float) and not math.isfinite(value):
            return "expected finite integer, got non-finite float"
        if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
            pass
        else:
            return f"expected integer, got {type(value).__name__}"

    elif expected_type == OutputFieldType.NUMBER:
        if isinstance(value, bool):
            return "expected number, got boolean"
        if not isinstance(value, (int, float)):
            return f"expected number, got {type(value).__name__}"
        if isinstance(value, float) and not math.isfinite(value):
            return "expected finite number, got non-finite float"

    elif expected_type == OutputFieldType.BOOLEAN:
        if not isinstance(value, bool):
            return f"expected boolean, got {type(value).__name__}"

    elif expected_type == OutputFieldType.ENUM:
        if not isinstance(value, str):
            return f"expected string (enum), got {type(value).__name__}"
        if field_config.values and value not in field_config.values:
            return f"value '{value}' not in allowed values: {field_config.values}"

    return None


@dataclass(frozen=True, slots=True)
class ValidationSuccess:
    """Successful validation result containing parsed data."""

    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.data, MappingProxyType):
            object.__setattr__(self, "data", deep_freeze(self.data))


@dataclass(frozen=True, slots=True)
class ValidationError:
    """Failed validation result with error details."""

    reason: str
    detail: str | None = None
    expected: str | None = None
    actual: str | None = None


ValidationResult = ValidationSuccess | ValidationError


def validate_json_object_response(content: str) -> ValidationResult:
    """Validate LLM response content is a JSON object.

    This is the standard validation for ELSPETH LLM transforms:
    1. Parse JSON (catch JSONDecodeError)
    2. Verify type is dict (not array, null, or primitive)
    3. Return validated dict or structured error

    Args:
        content: Raw response content from LLM API

    Returns:
        ValidationSuccess with parsed dict, or ValidationError with details
    """
    # Step 1: Parse JSON
    try:
        parsed = json.loads(content, parse_constant=reject_nonfinite_constant)
    except (json.JSONDecodeError, ValueError) as e:
        return ValidationError(
            reason="invalid_json",
            detail=str(e),
        )

    # Step 2: Verify type is dict
    if not isinstance(parsed, dict):
        return ValidationError(
            reason="invalid_json_type",
            expected="object",
            actual=type(parsed).__name__,
        )

    # Success
    return ValidationSuccess(data=parsed)


def strip_markdown_fences(content: str) -> str:
    """Strip markdown code block fences from LLM response content.

    LLMs sometimes wrap JSON responses in ```json ... ``` blocks even in
    JSON mode. This strips them so JSON parsing succeeds.

    Consolidates identical logic from azure_multi_query.py and
    openrouter_multi_query.py.
    """
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped

    first_newline = stripped.find("\n")
    if first_newline == -1:
        # No newline after opening fence — no body to extract
        return stripped

    stripped = stripped[first_newline + 1 :]
    # Handle trailing whitespace before closing fence (e.g. "``` \n")
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()
        stripped = stripped[:-3].strip()
    return stripped


def build_structured_response_directive(
    *,
    schema_name: str,
    output_fields: tuple[OutputFieldConfig, ...],
    response_format: ResponseFormat,
    prompt: str,
) -> tuple[dict[str, Any] | None, str]:
    """Build the provider response constraint and exact prompt for a query.

    Shared by every LLM plugin surface that declares ``output_fields`` (the
    multi-query per-query pair, the single-prompt top-level pair, and the LLM
    source). Structured mode sends the schema through the API-native
    ``response_format`` contract. Standard JSON mode guarantees only a JSON
    object at the API boundary, so the declared field contract must also be
    present in the prompt before runtime validation can reasonably enforce
    it. Canonical JSON keeps field names and enum values escaped and
    deterministic rather than interpolating them as free-form prose.

    Args:
        schema_name: json_schema name advertised to the API (e.g. "q1_output").
        output_fields: Declared typed output fields (empty = no constraint).
        response_format: STRUCTURED (API-enforced json_schema) or STANDARD.
        prompt: The rendered prompt before any contract suffix.

    Returns:
        (response_format dict or None, provider prompt actually sent).
    """
    from elspeth.contracts.hashing import canonical_json

    if not output_fields:
        return None, prompt
    properties = {field.suffix: field.to_json_schema() for field in output_fields}
    output_schema = {
        "type": "object",
        "properties": properties,
        "required": list(properties),
    }
    if response_format == ResponseFormat.STRUCTURED:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": output_schema,
            },
        }, prompt
    return {"type": "json_object"}, (
        f"{prompt}\n\nReturn exactly one JSON object matching this required output contract (JSON Schema):\n{canonical_json(output_schema)}"
    )


def extract_structured_fields(
    content: str,
    output_fields: tuple[OutputFieldConfig, ...],
) -> tuple[dict[str, Any], TransformErrorReason | None]:
    """Parse and validate an LLM JSON response against declared output fields.

    Tier 3 boundary: the response is external data — parse immediately,
    reject non-object payloads, missing declared fields, and type
    mismatches instead of coercing. Callers wrap the returned error reason
    with their own context (query name/index where one exists).

    Returns:
        (fields keyed by suffix, None) on success;
        ({}, error reason dict carrying at least "reason") on failure.
    """
    try:
        parsed = json.loads(content, parse_constant=reject_nonfinite_constant)
    except (json.JSONDecodeError, ValueError) as e:
        return {}, {
            "reason": "json_parse_failed",
            "error": str(e),
            "raw_response_preview": content[:500],
        }
    if not isinstance(parsed, dict):
        return {}, {
            "reason": "invalid_json_type",
            "expected": "object",
            "actual": type(parsed).__name__,
        }
    extracted: dict[str, Any] = {}
    for field in output_fields:
        if field.suffix not in parsed:
            return {}, {
                "reason": "missing_output_field",
                "field": field.suffix,
                "available_fields": list(parsed.keys()),
            }
        type_error = validate_field_value(parsed[field.suffix], field)
        if type_error is not None:
            return {}, {
                "reason": "field_type_mismatch",
                "field": field.suffix,
                "error": type_error,
                "value": repr(parsed[field.suffix])[:200],
            }
        extracted[field.suffix] = parsed[field.suffix]
    return extracted, None
