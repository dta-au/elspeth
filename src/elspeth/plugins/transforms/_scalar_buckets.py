"""Type-aware bucket helpers for batch transform category values."""

from __future__ import annotations

import math
from collections.abc import Iterable

from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

type ScalarBucketKey = tuple[type[object], object]

# A configured category label (a baseline variant, a positive class) is matched
# against row values with ``same_scalar_bucket_value``, which is type-strict.
# So the config must keep the type the operator wrote: strict members stop
# pydantic's lax union from turning 1.0 into int 1 (a bucket no float variant
# can ever match) and admit a float label at all. Every scalar a YAML or JSON
# config can carry is admitted; Decimal and datetime group keys have no config
# literal, so they are not.
ConfiguredScalarLabel = StrictStr | StrictInt | StrictFloat | StrictBool


def require_matchable_configured_label(value: str | int | float | bool | None, *, field_name: str) -> str | int | float | bool | None:
    """Refuse a configured label that no row value could ever bucket-match.

    A blank string and a non-finite float are refused: batch plugins reject
    non-finite category values in the data, so such a label can never match.
    """
    if type(value) is str and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if type(value) is float and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def scalar_bucket_key(value: object) -> ScalarBucketKey:
    """Return a hashable key that keeps equal cross-type scalars separate."""
    return (type(value), value)


def same_scalar_bucket_value(left: object, right: object) -> bool:
    """Compare bucket values without merging ``True``/``1`` or ``False``/``0``."""
    return type(left) is type(right) and left == right


def scalar_bucket_contains(values: Iterable[object], candidate: object) -> bool:
    """Return whether ``candidate`` is already present under type-aware equality."""
    return any(same_scalar_bucket_value(value, candidate) for value in values)


def append_unique_bucket_value[T](values: list[T], candidate: T) -> None:
    """Append ``candidate`` only if no type-aware bucket match already exists."""
    if not scalar_bucket_contains(values, candidate):
        values.append(candidate)
