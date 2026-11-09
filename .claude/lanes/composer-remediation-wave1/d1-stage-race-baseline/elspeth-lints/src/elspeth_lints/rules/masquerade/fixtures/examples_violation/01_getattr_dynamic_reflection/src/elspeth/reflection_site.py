"""Violation fixture: reflection-style getattr with a dynamic, non-literal name.

Not covered by any amnesty: the name is not a string literal (so it is
not the sentinel-getattr shape), and this function is not
``@trust_boundary``/``@observation_boundary`` decorated in any case.
"""

from __future__ import annotations


def read_dynamic_field(row: object, field_name: str) -> object:
    return getattr(row, field_name, None)
