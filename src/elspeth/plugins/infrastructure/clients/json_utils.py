"""Checks for nonfinite numbers in external JSON values."""

from __future__ import annotations

import math
from typing import Any


def contains_non_finite(obj: Any) -> bool:
    """Recursively check if object contains NaN or Infinity float values.

    This is a Tier 3 boundary check: external JSON may contain non-finite values
    (Python's json module accepts them), but canonicalization rejects them. We
    detect these at the HTTP boundary to record as parse failure rather than
    crashing during audit recording.

    Args:
        obj: Any JSON-parsed value (dict, list, or primitive)

    Returns:
        True if any float value is NaN or Infinity
    """
    if isinstance(obj, float):
        return math.isnan(obj) or math.isinf(obj)
    if isinstance(obj, dict):
        return any(contains_non_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return any(contains_non_finite(v) for v in obj)
    return False
