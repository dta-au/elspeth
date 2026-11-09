"""Violation fixture: inspect.getattr_static used as a presence probe.

``getattr_static`` has no structural amnesty in this gate — every use is
baseline-required (PLAN.md: "any getattr_static not matching the two
[identity/MRO] recognizers -> ERROR until a human adjudicates it"). This
shape consumes the result as a boolean presence check, not an identity
comparison, so it would not qualify even under a narrower recognizer.
"""

from __future__ import annotations

import inspect


def has_static_attr(obj: object, name: str) -> bool:
    sentinel = object()
    return inspect.getattr_static(obj, name, sentinel) is not sentinel
