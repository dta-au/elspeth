"""Closed rejection/creation handoffs and pure required rejection projection."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_proposal import PipelineProposal
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RejectionProjectionUnusedMetadata
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected, PipelineRejectionSQLResult
from elspeth.web.sessions.proposal_authority import _pipeline_rejected_payload
from elspeth.web.sessions.protocol import (
    AuthoritativePipelineProposal,
    CompositionProposalRecord,
    PipelineProposalPublicMetadata,
    ProposalEventRecord,
)


@dataclass(frozen=True, slots=True)
class PipelineRejectionReturned:
    sql_outcome: RequiredSQLReturned[PipelineRejectionSQLResult]
    post_sql_failures: tuple[BaseException, ...] = ()


@dataclass(frozen=True, slots=True)
class PipelineRejectionRaised:
    sql_outcome: RequiredSQLRaised
    projection_unused: RejectionProjectionUnusedMetadata


type PipelineRejectionFinishOnce = PipelineRejectionReturned | PipelineRejectionRaised


@dataclass(frozen=True, slots=True)
class PipelineCreationReturned:
    sql_outcome: RequiredSQLReturned[CompositionProposalRecord]
    post_sql_failures: tuple[BaseException, ...] = ()


@dataclass(frozen=True, slots=True)
class PipelineCreationRaised:
    sql_outcome: RequiredSQLRaised


type PipelineCreationFinishOnce = PipelineCreationReturned | PipelineCreationRaised


def decode_pipeline_rejection_result(result: PipelineRejectionSQLResult, expected: PipelineRejectionExpected) -> CompositionProposalRecord:
    """Validate actual physical SQL material; never read, repair or write."""
    if type(result) is not PipelineRejectionSQLResult or type(expected) is not PipelineRejectionExpected:
        raise AuditIntegrityError("rejection projection requires nominal SQL result and expectation")
    if type(expected.authority) is not AuthoritativePipelineProposal or result.expected is not expected:
        raise AuditIntegrityError("rejection projection replaced its independent expectation")
    if (
        type(expected.context) is not SessionOperationContext
        or type(expected.context.fence) is not SessionOperationFence
        or type(expected.authority.proposal) is not PipelineProposal
        or type(expected.actor) is not str
        or not expected.actor
        or type(expected.actor_user_id) is not str
        or not expected.actor_user_id
        or (expected.dispatch is not None and type(expected.dispatch) is not PipelineDispatchAuditBinding)
    ):
        raise AuditIntegrityError("rejection expectation has foreign authority domains")
    record, event = result.record, result.event
    original = expected.authority.row
    if (
        type(record) is not CompositionProposalRecord
        or type(original) is not CompositionProposalRecord
        or type(event) is not ProposalEventRecord
        or type(record.pipeline_metadata) is not PipelineProposalPublicMetadata
        or type(original.pipeline_metadata) is not PipelineProposalPublicMetadata
        or type(result.transitioned) is not bool
    ):
        raise AuditIntegrityError("rejection projection contains foreign owned material")
    metadata = record.pipeline_metadata
    if (
        any(not is_lower_sha256_hex(value) for value in (metadata.draft_hash, metadata.skill_hash, metadata.audit_payload_hash))
        or type(metadata.repair_count) is not int
        or metadata.repair_count < 0
        or metadata.custody_result not in ("ready", "not_required")
    ):
        raise AuditIntegrityError("rejection metadata has an invalid owned domain")
    for uuid_value in (record.id, record.session_id, event.id, event.session_id, event.proposal_id, record.audit_event_id):
        if type(uuid_value) is not UUID:
            raise AuditIntegrityError("rejection projection has a noncanonical owned UUID")
    for optional_uuid in (record.user_message_id, record.base_state_id):
        if optional_uuid is not None and type(optional_uuid) is not UUID:
            raise AuditIntegrityError("rejection projection has a foreign optional owned UUID")
    if type(expected.authority.creation_event_id) is not UUID or expected.context.fence.session_id != str(record.session_id):
        raise AuditIntegrityError("rejection expectation belongs to another session or creation")
    if type(record.tool_call_id) is not str or not record.tool_call_id or record.tool_name != "set_pipeline":
        raise AuditIntegrityError("rejection projection has a foreign tool domain")
    for timestamp in (record.created_at, record.updated_at, event.created_at):
        if type(timestamp) is not datetime or timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise AuditIntegrityError("rejection projection has an unowned timestamp")
    try:
        checked_record = replace(record)
        checked_event = replace(event)
        checked_metadata = replace(record.pipeline_metadata)
        expected_payload = _pipeline_rejected_payload(authority=expected.authority, reason=expected.reason, dispatch=expected.dispatch)
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("rejection projection failed owned constructor validation") from error
    if (
        record.status != "rejected"
        or record.committed_state_id is not None
        or event.event_type != "proposal.rejected"
        or event.session_id != record.session_id
        or event.proposal_id != record.id
        or record.audit_event_id != event.id
        or event.actor != expected.actor
        or event.payload != expected_payload
        or checked_record != record
        or checked_event != event
        or checked_metadata != record.pipeline_metadata
        or replace(record, status=original.status, audit_event_id=original.audit_event_id, updated_at=original.updated_at) != original
        or (result.transitioned and (original.status != "pending" or record.updated_at != event.created_at))
        or (not result.transitioned and (original.status != "rejected" or original.audit_event_id != event.id))
    ):
        raise AuditIntegrityError("rejection projection differs from its immutable proposal/event binding")
    return record
