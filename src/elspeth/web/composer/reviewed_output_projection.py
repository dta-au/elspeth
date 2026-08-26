"""Compatibility between an exact retained-field projection and reviewed fields.

Extracted from the retired composer recipe scaffolding, whose only surviving
consumer was the guided planner. The check is independent of how a projection
was authored: it compares two declared field sets and nothing else.
"""

from __future__ import annotations

from typing import Literal, final


@final
class ReviewedOutputProjectionConflict(Exception):
    """Closed conflict between an exact projection and reviewed fields."""

    error_code: Literal["reviewed_output_projection_conflict"] = "reviewed_output_projection_conflict"

    def __init__(self, missing_fields: tuple[str, ...]) -> None:
        if type(missing_fields) is not tuple:
            raise TypeError("ReviewedOutputProjectionConflict.missing_fields must be an exact tuple")
        if not missing_fields:
            raise ValueError("ReviewedOutputProjectionConflict.missing_fields must not be empty")
        if any(type(field) is not str or not field for field in missing_fields):
            raise TypeError("ReviewedOutputProjectionConflict.missing_fields must contain non-empty exact strings")
        if len(set(missing_fields)) != len(missing_fields):
            raise ValueError("ReviewedOutputProjectionConflict.missing_fields must be unique")
        super().__init__("an exact retained-field projection omits reviewed output fields")
        self.missing_fields = missing_fields


def reviewed_output_projection_conflict(
    *,
    retained_fields: tuple[str, ...],
    required_fields: tuple[str, ...],
) -> ReviewedOutputProjectionConflict | None:
    """Return the ordered missing-field conflict for one exact projection.

    The caller owns the association between this projection and this reviewed
    output. Compatibility is set inclusion only: projection order and
    additional retained fields are permitted, and this helper never mutates
    either side of the reviewed contract.
    """

    if type(retained_fields) is not tuple or type(required_fields) is not tuple:
        raise TypeError("projection field sets must be exact tuples")
    if any(type(field) is not str or not field for field in (*retained_fields, *required_fields)):
        raise TypeError("projection field sets must contain non-empty exact strings")
    retained = set(retained_fields)
    missing = tuple(dict.fromkeys(field for field in required_fields if field not in retained))
    if not missing:
        return None
    return ReviewedOutputProjectionConflict(missing)
