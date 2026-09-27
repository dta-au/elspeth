"""Factory for creating Pydantic schemas from configuration.

This module creates runtime Pydantic models based on SchemaConfig,
enabling config-driven schema validation for plugins.

CRITICAL: The `allow_coercion` parameter enforces the three-tier trust model:
- Sources (allow_coercion=True): May coerce "42" -> 42
- Transforms/Sinks (allow_coercion=False): Reject wrong types (upstream bug)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import numpy as np
from pydantic import ConfigDict, create_model, model_validator

from elspeth.contracts import PluginSchema
from elspeth.contracts.schema import FIELD_TYPE_MAP as TYPE_MAP
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.core.canonical import is_non_canonical_number

# Type alias for extra field handling modes
ExtraMode = Literal["allow", "forbid"]


def _find_non_canonical_number_path(value: Any, path: str = "$") -> str | None:
    """Find the first path holding a number canonical JSON refuses by value.

    ``is_non_canonical_number`` decides (NaN/Infinity, an integer outside the
    JSON safe range ±(2**53-1), a NumPy value beyond the double range); this
    walker only finds where it is. Such a value cannot be hashed into the audit
    trail, so a source must quarantine the row that carries it.
    """
    # isinstance (not exact-type) so container/scalar subclasses cannot fail open:
    # a value nested in an OrderedDict/Mapping subclass or a namedtuple (tuple
    # subclass) must still be caught at the source boundary. str/bytes are
    # excluded from the Sequence arm.
    if is_non_canonical_number(value):
        return path

    if isinstance(value, Mapping):
        for key, nested in value.items():
            nested_path = _find_non_canonical_number_path(nested, f"{path}.{key!s}")
            if nested_path is not None:
                return nested_path
        return None

    if isinstance(value, (list, tuple)):
        for idx, nested in enumerate(value):
            nested_path = _find_non_canonical_number_path(nested, f"{path}[{idx}]")
            if nested_path is not None:
                return nested_path
        return None

    # NumPy arrays: scan integer and floating elements and report a useful path.
    # Other dtypes (string/object/bool/complex) hold no value the number rule
    # judges, so they are skipped.
    if (
        isinstance(value, np.ndarray)
        and value.size > 0
        and (np.issubdtype(value.dtype, np.floating) or np.issubdtype(value.dtype, np.integer))
    ):
        for idx, elem in enumerate(value.flat):
            if is_non_canonical_number(elem):
                indices = np.unravel_index(idx, value.shape)
                index_str = "][".join(str(i) for i in indices)
                return f"{path}[{index_str}]"

    return None


def _reject_non_canonical_numbers(data: Any) -> Any:
    """Reject a number canonical JSON refuses by value (NaN/Infinity, unsafe integer) at the source boundary."""
    offending_path = _find_non_canonical_number_path(data)
    if offending_path is not None:
        raise ValueError(
            f"Non-finite or out-of-range number at {offending_path}: canonical JSON admits finite numbers and integers "
            "within ±(2**53-1). Use null/None for missing values, not NaN/Infinity."
        )
    return data


class _ObservedPluginSchema(PluginSchema):
    """PluginSchema base for the source boundary: rejects non-canonical numbers.

    Runs AFTER field validation, so it judges the values the row will carry —
    including a typed ``int`` field coerced from text (a CSV cell), which a
    before-validator would only see as a string.
    """

    @model_validator(mode="after")
    def _validate_canonical_numbers(self) -> _ObservedPluginSchema:
        _reject_non_canonical_numbers(dict(self))
        return self


def create_schema_from_config(
    config: SchemaConfig,
    name: str,
    allow_coercion: bool = True,
) -> type[PluginSchema]:
    """Create a Pydantic schema class from configuration.

    Args:
        config: Schema configuration specifying fields and mode
        name: Name for the generated schema class
        allow_coercion: If True, coerce types (e.g., "42" -> 42). Default True.
            - Sources should use True (normalize external data)
            - Transforms/Sinks should use False (wrong types = upstream bug)

    Returns:
        A PluginSchema subclass with the specified fields and validation

    The generated schema:
    - Observed mode: extra="allow", accepts any fields (no type checking)
    - Fixed mode: extra="forbid", rejects unknown fields
    - Flexible mode: extra="allow", requires specified fields, allows extras

    Examples:
        # Source - coerces external data
        source_schema = create_schema_from_config(config, "CSVRow", allow_coercion=True)

        # Transform - expects clean data from upstream
        transform_schema = create_schema_from_config(config, "Input", allow_coercion=False)
    """
    if config.is_observed:
        # Observed schema - accept anything (no type validation either way)
        return _create_dynamic_schema(name)

    # Explicit schema - fixed or flexible mode
    return _create_explicit_schema(config, name, allow_coercion)


def _create_dynamic_schema(name: str) -> type[PluginSchema]:
    """Create a schema that accepts any fields.

    Note: Dynamic schemas don't do type checking, so coercion is irrelevant.
    """
    return create_model(
        name,
        __base__=_ObservedPluginSchema,
        __module__=__name__,
        __config__=ConfigDict(
            extra="allow",
            # No strict setting needed - no fields to validate types against
        ),
    )


def _create_explicit_schema(
    config: SchemaConfig,
    name: str,
    allow_coercion: bool,
) -> type[PluginSchema]:
    """Create a schema with explicit field definitions."""
    if config.fields is None or config.mode == "observed":
        raise ValueError("_create_explicit_schema requires fields and non-observed mode")

    # Build field definitions for create_model
    # Format: field_name=(type, default) or field_name=(type, ...)
    field_definitions: dict[str, Any] = {}

    for field_def in config.fields:
        python_type = _get_python_type(field_def)

        if field_def.required:
            # Required field - use ... (Ellipsis) as default
            field_definitions[field_def.name] = (python_type, ...)
        else:
            # Optional field - default to None
            field_definitions[field_def.name] = (python_type, None)

    # Determine extra field handling
    extra_mode: ExtraMode = "allow" if config.mode == "flexible" else "forbid"

    # Coercion control: strict=True means NO coercion (Pydantic's semantics)
    # allow_coercion=True  -> strict=False (coerce)
    # allow_coercion=False -> strict=True  (reject wrong types)
    use_strict = not allow_coercion

    # At source boundary (allow_coercion=True), use _ObservedPluginSchema base
    # to reject numbers canonical JSON refuses (NaN/Infinity, an integer outside
    # ±(2**53-1)) in every field: typed (after coercion), 'any', and
    # flexible-mode extras. FiniteFloat still rejects a non-finite typed float
    # first, with its own field-level error.
    base_class = _ObservedPluginSchema if allow_coercion else PluginSchema

    return create_model(
        name,
        __base__=base_class,
        __module__=__name__,
        __config__=ConfigDict(
            extra=extra_mode,
            strict=use_strict,
        ),
        **field_definitions,
    )


def _get_python_type(field_def: FieldDefinition) -> Any:
    """Convert field definition to Python type annotation.

    Nullable or optional fields return a Union type (base_type | None).
    Required non-nullable fields return the base type directly.

    Returns Any to satisfy mypy - the actual return is a type or UnionType.
    """
    base_type = TYPE_MAP[field_def.field_type]

    if field_def.nullable or not field_def.required:
        return base_type | None
    return base_type
