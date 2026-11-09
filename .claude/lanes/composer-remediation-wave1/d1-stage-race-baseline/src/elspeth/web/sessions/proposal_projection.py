"""Strict public projection of persisted composition proposals."""

from __future__ import annotations

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.protocol import CompositionProposalRecord
from elspeth.web.sessions.schemas import CompositionProposalResponse, PipelineProposalMetadataResponse


def project_composition_proposal(record: CompositionProposalRecord) -> CompositionProposalResponse:
    """Project one immutable proposal record to its ordinary wire body."""
    metadata = record.pipeline_metadata
    return CompositionProposalResponse(
        id=str(record.id),
        session_id=str(record.session_id),
        tool_call_id=record.tool_call_id,
        tool_name=record.tool_name,
        status=record.status,
        summary=record.summary,
        rationale=record.rationale,
        affects=list(record.affects),
        arguments_redacted_json=deep_thaw(record.arguments_redacted_json),
        base_state_id=str(record.base_state_id) if record.base_state_id is not None else None,
        committed_state_id=str(record.committed_state_id) if record.committed_state_id is not None else None,
        audit_event_id=str(record.audit_event_id) if record.audit_event_id is not None else None,
        pipeline_metadata=(
            PipelineProposalMetadataResponse(
                draft_hash=metadata.draft_hash,
                base=deep_thaw(metadata.base),
                repair_count=metadata.repair_count,
                skill_hash=metadata.skill_hash,
                audit_payload_hash=metadata.audit_payload_hash,
                custody_result=metadata.custody_result,
            )
            if metadata is not None
            else None
        ),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
