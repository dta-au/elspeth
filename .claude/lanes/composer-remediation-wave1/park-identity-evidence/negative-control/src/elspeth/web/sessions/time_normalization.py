"""Restore UTC to timestamps read from session persistence."""

from __future__ import annotations

from datetime import UTC, datetime


def restore_utc(value: datetime) -> datetime:
    """SQLite strips timezone information from stored UTC timestamps."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
