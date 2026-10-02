"""Type normalization for schema contracts.

Converts numpy/pandas types to Python primitives for consistent
contract storage and validation.

Fast path: Uses type() with frozenset membership for standard Python types (performance).
Slow path: Uses isinstance() checks for numpy/pandas type hierarchies.
Avoids string matching on __name__: type identity is decided nominally,
against concrete classes, never by name (CONTRIBUTING.md §Convention:
validate by trust domain).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Final, cast

from elspeth.contracts.trust_boundary import trust_boundary

# NOTE: numpy and pandas are imported LAZILY inside normalize_type_for_contract()
# to avoid breaking the contracts leaf module boundary. Importing them at module
# level pulls in 400+ modules just for type normalization.

# Canonical type registry: string name → Python type.
# Single source of truth for all contract type maps in the codebase.
# Used for checkpoint serialization/deserialization, type validation,
# and annotation resolution across contracts/ modules.
CONTRACT_TYPE_MAP: dict[str, type] = {
    "int": int,
    "str": str,
    "float": float,
    "bool": bool,
    "NoneType": type(None),
    "datetime": datetime,
    "object": object,  # 'any' type for fields that accept any value
}

# Types that can be serialized in checkpoint and restored in from_checkpoint()
# Derived from CONTRACT_TYPE_MAP to stay in sync.
ALLOWED_CONTRACT_TYPES: frozenset[type] = frozenset(CONTRACT_TYPE_MAP.values())


class _UnsupportedContractTypeSignal:
    """Singleton signal for runtime values that cannot be checkpointed as field types."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSUPPORTED_CONTRACT_TYPE"


UNSUPPORTED_CONTRACT_TYPE: Final = _UnsupportedContractTypeSignal()
NormalizedContractType = type | _UnsupportedContractTypeSignal


def normalize_type_for_contract(value: Any) -> NormalizedContractType:
    """Convert value's type to Python primitive for contract storage.

    Args:
        value: Any Python value

    Returns:
        Python primitive type or UNSUPPORTED_CONTRACT_TYPE for unknowns

    Raises:
        ValueError: If value is NaN or Infinity (invalid for audit trail)

    Note:
        numpy and pandas are imported lazily inside this function to avoid
        pulling in heavy dependencies when the contracts package is imported.
    """
    if value is None:
        return type(None)

    # Fast path: standard Python types skip numpy/pandas imports entirely.
    # Exclude float — must fall through to NaN/Infinity rejection below.
    fast_type = type(value)
    if fast_type is not float and fast_type in ALLOWED_CONTRACT_TYPES:
        return fast_type

    # Lazy imports to maintain contracts as a leaf module
    import numpy as np
    import pandas as pd

    # Missing-value sentinels normalize to NoneType
    if value is pd.NA:
        return type(None)

    # pd.NaT = "Not a Time" (missing datetime), treat like None
    if value is pd.NaT:
        return type(None)

    # CRITICAL: Reject NaN/Infinity (Tier 1 audit integrity)
    # Must check before numpy type normalization to catch np.float64(nan) etc.
    if isinstance(value, (float, np.floating)) and (math.isnan(value) or math.isinf(value)):
        raise ValueError(f"Cannot infer type from non-finite float: {value}. NaN/Infinity are invalid in audit trail.")

    # Normalize numpy/pandas types to primitives
    if isinstance(value, np.integer):
        return int
    if isinstance(value, np.floating):
        return float
    if isinstance(value, np.bool_):
        return bool
    if isinstance(value, pd.Timestamp):
        return datetime
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return type(None)
        return datetime
    if isinstance(value, np.str_):
        return str
    # NOTE: np.bytes_ is NOT normalized to str (or bytes). It falls through
    # to the ALLOWED_CONTRACT_TYPES check and returns the unsupported signal. This prevents
    # silent misclassification of binary data as text in schema contracts.

    # Return an explicit unsupported signal instead of raising TypeError as a
    # protocol value. infer_field_type() maps it to object/any.
    final_type = type(value)
    if final_type not in ALLOWED_CONTRACT_TYPES:
        return UNSUPPORTED_CONTRACT_TYPE
    return final_type


def infer_field_type(value: Any) -> tuple[type, bool]:
    """Infer a field's contract ``(python_type, nullable)`` from one observed value.

    The single inference rule for every contract field built from a value —
    a source's first row (``ContractBuilder``), a transform's propagated or
    narrowed output (``contract_propagation``) and a field a transform adds
    or retypes one value at a time (``SchemaContract.with_field``,
    value_transform):

    * A value whose type has no contract type name — a nested object or an
      array, whether a ``dict``/``list`` or its frozen ``MappingProxyType``/
      ``tuple`` view — is typed ``object`` (``any``). The row still carries
      the value, so the contract must describe it rather than refuse it.
    * A null-like value (``None``, ``pd.NA``, ``pd.NaT``) is typed ``object``
      and nullable: one null says the field may be null, not that it is
      always ``NoneType``.

    Non-finite floats propagate ``ValueError`` from
    ``normalize_type_for_contract``: they are invalid audit material on every
    path.
    """
    normalized_type = normalize_type_for_contract(value)
    if normalized_type is UNSUPPORTED_CONTRACT_TYPE:
        return object, False
    python_type = cast(type, normalized_type)
    if python_type is type(None):
        return object, True
    return python_type, False


@trust_boundary(
    tier=3,
    source=(
        "one pipeline-row value during SchemaContract.validate() — Tier-3 external row data whose runtime "
        "type is being classified so exotic types quarantine instead of crashing the run"
    ),
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns an owned primitive type for recognized numpy/pandas/stdlib values and type(value) for "
        "everything else, so the caller records a TypeMismatchViolation; never raises on malformed input"
    ),
    non_raising=True,
)
def classify_runtime_type(value: Any) -> type:
    """Classify a value's type for runtime validation comparison.

    Unlike normalize_type_for_contract(), this function NEVER raises.
    It normalizes numpy/pandas wrappers to Python primitives (so that
    numpy.int64(42) compares equal to int), but returns type(value) for
    anything it doesn't recognize — letting the caller produce a
    TypeMismatchViolation instead of crashing the pipeline.

    This is the correct function for SchemaContract.validate(), where
    values come from Tier 3 external data and exotic types should be
    quarantined, not crash the run.

    Args:
        value: Any Python value from a pipeline row

    Returns:
        Python primitive type for known types, or type(value) for unknowns.
        Never raises.
    """
    if value is None:
        return type(None)

    # Fast path: standard Python types (including float — no NaN rejection
    # at validation time, that's a contract-inference concern)
    fast_type = type(value)
    if fast_type in ALLOWED_CONTRACT_TYPES:
        return fast_type

    # Lazy imports to maintain contracts as a leaf module
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        return fast_type

    # Missing-value sentinels
    if value is pd.NA or value is pd.NaT:
        return type(None)

    # Non-finite floats: return float (the type is correct even if the
    # value is problematic — validation compares types, not values)
    if isinstance(value, (float, np.floating)) and (math.isnan(value) or math.isinf(value)):
        return float

    # Normalize numpy/pandas types to primitives
    if isinstance(value, np.integer):
        return int
    if isinstance(value, np.floating):
        return float
    if isinstance(value, np.bool_):
        return bool
    if isinstance(value, pd.Timestamp):
        return datetime
    if isinstance(value, np.datetime64):
        if np.isnat(value):
            return type(None)
        return datetime
    if isinstance(value, np.str_):
        return str

    # Unknown type — return as-is for comparison. The caller will see
    # actual_type != expected_type and produce a TypeMismatchViolation.
    return fast_type
