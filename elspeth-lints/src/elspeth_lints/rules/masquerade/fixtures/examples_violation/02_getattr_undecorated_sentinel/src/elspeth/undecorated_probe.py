"""Violation fixture: 3-arg literal-name getattr with no @trust_boundary decorator.

Same arity/literal-name shape ADR-032 prescribes at an external boundary,
but the function is not decorated, so the receiver's trust domain is
unproven. This is exactly the axis the gate must discriminate on: arity
and literal-name alone do not amnesty a site.
"""

from __future__ import annotations


def read_status(response: object) -> str | None:
    return getattr(response, "status", None)
