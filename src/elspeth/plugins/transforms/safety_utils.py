"""Shared utilities for security transforms (keyword filter, Azure safety).

Provides common functions for field selection and validation used across
all security-oriented transforms.
"""

from __future__ import annotations

from types import MappingProxyType

from elspeth.contracts.freeze import FrozenJsonArray
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


def unscanned_field_warnings(fields_config: str | list[str], row: PipelineRow) -> list[str]:
    """Name every field that ``fields: all`` leaves unscanned, for the audit trail.

    'all' mode scans only top-level string values, so a field holding a
    dict, list or other non-string value — including text nested inside
    a container — is never scanned. A security transform reporting success
    must say so, or the audit record reads as though the whole row was
    checked. Each warning carries the field name and value type only, never
    the value. Explicitly named fields fail closed instead, so they produce
    no warnings here.
    """
    if fields_config != "all":
        return []
    scanned = frozenset(get_fields_to_scan(fields_config, row))
    return [
        f"field {field_name!r} not scanned: fields='all' scans only top-level string values "
        f"(actual_type={_row_value_type_name(row[field_name])})"
        for field_name in row
        if field_name not in scanned
    ]


# deep_freeze (contracts/freeze.py) turns an authored dict into a
# MappingProxyType and an authored list into a tuple or FrozenJsonArray;
# the warning names the authored shape, not the freeze wrapper.
_AUTHORED_TYPE_NAMES: dict[type, str] = {
    MappingProxyType: "dict",
    tuple: "list",
    FrozenJsonArray: "list",
}


def _row_value_type_name(value: object) -> str:
    """Name a row value's type as the row's author wrote it."""
    value_type = type(value)
    if value_type in _AUTHORED_TYPE_NAMES:
        return _AUTHORED_TYPE_NAMES[value_type]
    return value_type.__name__


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
