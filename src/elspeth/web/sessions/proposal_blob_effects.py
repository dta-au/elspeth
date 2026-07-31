"""Exact durable binding for applied ordinary blob proposals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.sessions.proposal_blob_refs import proposal_blob_reference_ids

APPLIED_BLOB_PROPOSAL_TOOLS = frozenset({"update_blob", "delete_blob"})


def proposal_blob_arguments_hash(
    *,
    tool_name: str,
    arguments: object,
    blob_id: str,
) -> str:
    """Validate one exact blob authority and hash its private arguments."""
    if tool_name not in APPLIED_BLOB_PROPOSAL_TOOLS:
        raise AuditIntegrityError("Tier 1: blob effect receipt tool is not an approved blob mutation")
    if type(arguments) is not dict:
        raise AuditIntegrityError("Tier 1: blob effect proposal arguments are not an exact dict")
    try:
        references = proposal_blob_reference_ids(tool_name, arguments)
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditIntegrityError("Tier 1: blob effect proposal has malformed blob authority") from exc
    if references != (blob_id,):
        raise AuditIntegrityError("Tier 1: blob effect receipt does not bind the proposal's exact blob")
    return stable_hash(arguments)


def blob_record_snapshot_payload(record: BlobRecord) -> dict[str, Any]:
    """Project the exact persisted BlobRecord fields bound by a receipt."""
    if type(record) is not BlobRecord:
        raise TypeError("record must be an exact BlobRecord")
    return {
        "id": str(record.id),
        "session_id": str(record.session_id),
        "filename": record.filename,
        "mime_type": record.mime_type,
        "size_bytes": record.size_bytes,
        "content_hash": record.content_hash,
        "storage_path": record.storage_path,
        "created_at": record.created_at.isoformat(),
        "created_by": record.created_by,
        "source_description": record.source_description,
        "status": record.status,
        "creation_modality": record.creation_modality.value,
        "created_from_message_id": record.created_from_message_id,
        "creating_model_identifier": record.creating_model_identifier,
        "creating_model_version": record.creating_model_version,
        "creating_provider": record.creating_provider,
        "creating_composer_skill_hash": record.creating_composer_skill_hash,
        "creating_arguments_hash": record.creating_arguments_hash,
    }


def blob_row_snapshot_payload(row: Any) -> dict[str, Any]:
    """Project a live blob row with the canonical BlobRecord snapshot shape."""
    created_at = row.created_at
    if type(created_at) is not datetime:
        raise AuditIntegrityError("Tier 1: blob effect result created_at is malformed")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    else:
        created_at = created_at.astimezone(UTC)
    return {
        "id": row.id,
        "session_id": row.session_id,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "content_hash": row.content_hash,
        "storage_path": row.storage_path,
        "created_at": created_at.isoformat(),
        "created_by": row.created_by,
        "source_description": row.source_description,
        "status": row.status,
        "creation_modality": row.creation_modality,
        "created_from_message_id": row.created_from_message_id,
        "creating_model_identifier": row.creating_model_identifier,
        "creating_model_version": row.creating_model_version,
        "creating_provider": row.creating_provider,
        "creating_composer_skill_hash": row.creating_composer_skill_hash,
        "creating_arguments_hash": row.creating_arguments_hash,
    }
