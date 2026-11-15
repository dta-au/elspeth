"""Stored validation verdict shared by history presentation and baseline semantics."""

from elspeth.web.sessions.protocol import CompositionStateRecord


def is_validation_milestone(record: CompositionStateRecord) -> bool:
    """Match last-validated selection, including valid pre-marker snapshots."""
    meta = record.composer_meta
    return record.is_valid and (meta is None or "validation_lane" not in meta or meta["validation_lane"] == "strict")
