"""Decode persisted composition-state rows into owned session records."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from elspeth.web.sessions.content_publication import read_content_publication
from elspeth.web.sessions.protocol import CompositionStateRecord, decode_stored_composition_validation_errors
from elspeth.web.sessions.state_envelope import unwrap_state_column
from elspeth.web.sessions.time_normalization import restore_utc


def state_record_from_row(row: Any) -> CompositionStateRecord:
    """Convert a SQLAlchemy row to a CompositionStateRecord.

    Seam contract A: metadata_ maps DB column metadata_ back to the
    dataclass field. JSON columns are unwrapped from their _version envelope.
    """
    row_mapping = row._mapping
    record = CompositionStateRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        version=row.version,
        source=unwrap_state_column(row.source),
        sources=unwrap_state_column(row_mapping["sources"] if "sources" in row_mapping else None),
        nodes=unwrap_state_column(row.nodes),
        edges=unwrap_state_column(row.edges),
        outputs=unwrap_state_column(row.outputs),
        metadata_=unwrap_state_column(row.metadata_),
        is_valid=row.is_valid,
        validation_errors=decode_stored_composition_validation_errors(row.validation_errors),
        created_at=restore_utc(row.created_at),
        derived_from_state_id=(UUID(row.derived_from_state_id) if row.derived_from_state_id is not None else None),
        composer_meta=unwrap_state_column(row.composer_meta),
    )
    read_content_publication(record)
    return record
