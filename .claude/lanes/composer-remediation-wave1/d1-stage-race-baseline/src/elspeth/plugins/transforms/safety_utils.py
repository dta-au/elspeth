"""Shared utilities for security transforms (keyword filter, Azure safety).

Provides common functions for field selection and validation used across
all security-oriented transforms.
"""

from __future__ import annotations

from elspeth.contracts.schema_contract import PipelineRow


def get_fields_to_scan(fields_config: str | list[str], row: PipelineRow) -> list[str]:
    """Determine which fields to scan based on config.

    Args:
        fields_config: 'all' for all string fields, a single field name,
            or a list of field names.
        row: The pipeline row to inspect.

    Returns:
        List of field names to scan.
    """
    if fields_config == "all":
        return [field_name for field_name in row if isinstance(row[field_name], str)]
    elif isinstance(fields_config, str):
        return [fields_config]
    else:
        return fields_config


def validate_fields_not_empty(v: str | list[str]) -> str | list[str]:
    """Reject empty fields -- security transform must scan at least one field.

    Use as the implementation body for Pydantic field_validator on 'fields'.
    """
    if isinstance(v, str):
        if not v.strip():
            raise ValueError("fields cannot be empty")
        return v
    if len(v) == 0:
        raise ValueError("fields list cannot be empty — security transform must scan at least one field")
    for i, name in enumerate(v):
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"fields[{i}] cannot be empty")
    return v
