"""Fail-closed server invariant error for Composer-owned state."""

from __future__ import annotations


class InvariantError(Exception):
    """A first-party Composer invariant was violated, not a client request."""
