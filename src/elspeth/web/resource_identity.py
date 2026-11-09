"""Canonical identity for accepted external resource UUID selectors."""

from uuid import UUID


def canonical_resource_uuid(value: str) -> str | None:
    """Project a UUID selector without trimming it or repairing owned evidence."""
    try:
        return str(UUID(value))
    except ValueError:
        return None
