"""Exact durable binding for applied ordinary blob proposals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.sessions.proposal_blob_refs import proposal_blob_reference_ids

APPLIED_BLOB_PROPOSAL_TOOLS = frozenset({"update_blob", "delete_blob"})


class BlobSnapshotPayload(TypedDict):
    """The exact persisted blob fields a durable receipt or replacement ledger binds.

    Every value is JSON-ready (ids and timestamps as strings, enums as their
    values) so the same shape round-trips through the receipt columns and
    compares byte-for-byte against a live row projection.
    """

    id: str
    session_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str | None
    storage_path: str
    created_at: str
    created_by: str
    source_description: str | None
    status: str
    creation_modality: str
    created_from_message_id: str | None
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None


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


def blob_record_snapshot_payload(record: BlobRecord) -> BlobSnapshotPayload:
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


def blob_row_snapshot_payload(row: Any) -> BlobSnapshotPayload:
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


def record_applied_blob_proposal_effect(
    conn: Any,
    *,
    session_id: str,
    accepting_proposal_id: str,
    tool_name: str,
    blob_id: str,
    result_row: Any,
    now: datetime,
) -> None:
    """Write the durable applied-effect receipt in the effect's own commit.

    Mirrors the repository blob-mutation receipt exactly: the accepting
    proposal must be the exact pending mutation for this tool, at most one
    receipt may exist, and the receipt binds the arguments hash and the
    committed result-row snapshot so acceptance can later verify the effect
    it is crediting (``_validated_blob_effect_receipt``).
    """
    from sqlalchemy import insert, select

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.contracts.hashing import stable_hash
    from elspeth.web.sessions.models import composition_proposals_table, proposal_blob_effect_receipts_table

    proposal = conn.execute(
        select(composition_proposals_table).where(
            composition_proposals_table.c.id == accepting_proposal_id,
            composition_proposals_table.c.session_id == session_id,
        )
    ).one_or_none()
    if proposal is None:
        raise AuditIntegrityError("Tier 1: blob effect receipt proposal is missing or cross-session")
    if proposal.status != "pending" or proposal.tool_name != tool_name:
        raise AuditIntegrityError("Tier 1: blob effect receipt proposal is not the exact pending mutation")
    arguments_hash = proposal_blob_arguments_hash(
        tool_name=tool_name,
        arguments=proposal.arguments_json,
        blob_id=blob_id,
    )
    existing = conn.execute(
        select(proposal_blob_effect_receipts_table.c.proposal_id).where(
            proposal_blob_effect_receipts_table.c.proposal_id == accepting_proposal_id,
            proposal_blob_effect_receipts_table.c.session_id == session_id,
        )
    ).one_or_none()
    if existing is not None:
        raise AuditIntegrityError("Tier 1: blob proposal effect already has a durable receipt")
    result_blob_snapshot = blob_row_snapshot_payload(result_row)
    conn.execute(
        insert(proposal_blob_effect_receipts_table).values(
            proposal_id=accepting_proposal_id,
            session_id=session_id,
            tool_name=tool_name,
            blob_id=blob_id,
            arguments_hash=arguments_hash,
            result_blob_snapshot=result_blob_snapshot,
            result_blob_snapshot_hash=stable_hash(result_blob_snapshot),
            accepted_event_id=None,
            created_at=now,
            accepted_at=None,
        )
    )
