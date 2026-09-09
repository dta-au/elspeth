"""Clean fixture: assert [not] hasattr(...) presence-as-subject in tests.

Amnesty (b): the ``hasattr`` call is the direct operand of ``assert`` (or
``assert not``) inside the ``tests`` root, where presence/absence IS the
property under test, not a branch condition or a duck-typed adapter.
"""

from __future__ import annotations


def test_row_has_slots_not_dict() -> None:
    row = object()
    assert hasattr(row, "__class__")
    assert not hasattr(row, "__dict__")
