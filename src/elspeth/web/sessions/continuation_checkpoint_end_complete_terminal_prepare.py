"""Exact first-slice END terminal proposal before any paid consequence write."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Connection, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.advisor_audit import advisor_terminal_publication_audit_envelope
from elspeth.web.composer.advisor_policy import ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_checkpoint_end_complete_terminal import (
    ProjectedEndCompleteTerminal,
    require_original_end_complete_terminal,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.control_messages import advisor_signoff_withheld_control_envelope
from elspeth.web.composer.withheld_replies import withheld_reply_envelope
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import TokenUsageEntry, token_usage_entry_from_llm_call_envelope
from elspeth.web.execution.completion_gates import project_committed_graph_review
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_finalization import (
    ComposerFinalizationInputs,
    ContinuationFinalizationBudget,
    PreparedReviewOnlyFinalization,
    _message_with_state_response,
    prepare_review_only_finalization,
)
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    PreparedSegmentSettlement,
    prepare_segment_settlement_on_connection,
    prepare_snapshot_append,
    private_chunk_key,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import PreparedEndCompletePass, prepare_end_complete_pass
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import CheckpointPaidAuditCohort, checkpoint_paid_audit_cohort
from elspeth.web.sessions.continuation_provider_audit import continuation_provider_audit_drafts_on_connection
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import interpretation_events_table as interpretation_events
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.schemas import MessageWithStateResponse


@dataclass(frozen=True, slots=True)
class PreparedEndTerminalAuditRow:
    message_id: str
    draft: AuditMessageDraft


@dataclass(frozen=True, slots=True)
class EndCompleteTerminalBatch:
    projected: ProjectedEndCompleteTerminal
    owed: VerifiedCheckpointEndOwed
    preterminal: PreparedContinuationAppend
    terminal: PreparedSegmentSettlement
    prospective: tuple[ProspectiveContinuationChunk, ...]
    child_receipt: str
    response_chunk_key: str
    original_call: ComposerLLMCall
    quota_entry: TokenUsageEntry
    paid_audit: CheckpointPaidAuditCohort
    pass_audit: PreparedEndCompletePass
    prefix_rows: tuple[PreparedEndTerminalAuditRow, ...]
    finalization: PreparedReviewOnlyFinalization
    provider_cohort: tuple[AuditMessageDraft, ...]
    audit_message_ids: tuple[str, ...]
    assistant: ChatMessageRecord
    response: MessageWithStateResponse
    assistant_created_at: datetime

    def __post_init__(self) -> None:
        freeze_fields(self, "prospective", "prefix_rows", "provider_cohort", "audit_message_ids")


def _prefix_rows(projected: ProjectedEndCompleteTerminal) -> tuple[PreparedEndTerminalAuditRow, ...]:
    rows: list[PreparedEndTerminalAuditRow] = []
    if projected.withheld_candidate is not None:
        content = projected.withheld_candidate
        rows.append(
            PreparedEndTerminalAuditRow(
                str(uuid4()),
                AuditMessageDraft(
                    role="audit", content=content, tool_calls=(withheld_reply_envelope("advisor_publication_withheld", content),)
                ),
            )
        )
    disclosure = ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE
    rows.append(
        PreparedEndTerminalAuditRow(
            str(uuid4()),
            AuditMessageDraft(role="audit", content=disclosure, tool_calls=(advisor_signoff_withheld_control_envelope(disclosure),)),
        )
    )
    publication = projected.result.advisor_terminal_publication
    if publication is None:
        raise AuditIntegrityError("END terminal lost its original blocked publication")
    envelope = advisor_terminal_publication_audit_envelope(publication)
    rows.append(
        PreparedEndTerminalAuditRow(
            str(uuid4()),
            AuditMessageDraft(role="audit", content=json.dumps(envelope, sort_keys=True), tool_calls=(envelope,)),
        )
    )
    return tuple(rows)


def _prospective(append: PreparedContinuationAppend) -> tuple[ProspectiveContinuationChunk, ...]:
    return (
        *(
            ProspectiveContinuationChunk(f"event:{append.expected_cursor + index}", event.payload_json.encode("utf-8"), "event.v2")
            for index, event in enumerate(append.events, start=1)
        ),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )


def prepare_end_complete_terminal_batch_on_connection(
    conn: Connection,
    *,
    service: object,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed: VerifiedCheckpointEndOwed,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEndCompleteTerminal,
    inputs: ComposerFinalizationInputs,
) -> EndCompleteTerminalBatch:
    """Freeze the complete blocked outcome and both exact Continuation appends.

    The caller owns the current session write lock. No DML occurs here; all
    finalizer dependencies are selected on the same connection and rechecked
    when the batch is consumed.
    """
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or not conn.in_transaction():
        raise AuditIntegrityError("END terminal proposal requires its original locked SQL connection")
    require_original_end_complete_terminal(projected, physical=physical, returned=returned)
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    service._assert_session_write_lock_held(conn, sid, caller="prepare_end_complete_terminal")
    if (
        type(inputs) is not ComposerFinalizationInputs
        or type(inputs.budget_anchor) is not ContinuationFinalizationBudget
        or inputs.service is not service
        or inputs.running is not running
        or inputs.lease.context is not running.session_operation_context
        or inputs.state is None
        or inputs.state_record is None
        or inputs.compose_base_state_id is None
        or inputs.state_record.id != inputs.compose_base_state_id
        or inputs.state.version != projected.result.state.version
        or projected.result.graph_recovery_intent is not None
        or projected.result.pipeline_commit_intent is not None
    ):
        raise AuditIntegrityError("END terminal finalizer changed its ordinary no-save live owners")
    if (
        conn.execute(
            select(interpretation_events.c.id).where(interpretation_events.c.session_id == sid, interpretation_events.c.choice == "pending")
        ).first()
        is not None
    ):
        raise AuditIntegrityError("END first terminal slice found pending interpretation consequences")
    root = decode_continuation_snapshot(owed.selected.payload_json)
    if root.pending_settlement_ids != (owed.attempt_id,) or root.pending_owner_results:
        raise AuditIntegrityError("END terminal lost its single original pending paid result")
    child = tuple(entry for entry in root.effect_frontier if entry.effect_id == owed.effect_key)
    outer = tuple(entry for entry in root.effect_frontier if entry.effect_id == owed.outer_effect_key)
    if (
        len(child) != 1
        or len(outer) != 1
        or child[0].disposition != "dispatched"
        or outer[0].disposition != "dispatched"
        or sum(entry.disposition == "dispatched" for entry in root.effect_frontier) != 2
    ):
        raise AuditIntegrityError("END terminal lost its original child and outer effect owners")
    original_call = original_v4_checkpoint_returned_audit_call(returned, attempt_id=owed.attempt_id, request_sha256=prepared.request_sha256)
    if original_call.status is not ComposerLLMCallStatus.SUCCESS:
        raise AuditIntegrityError("END terminal cannot settle an incomplete original paid result")
    quota_entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(original_call))
    if quota_entry is None or quota_entry.call_id != owed.attempt_id:
        raise AuditIntegrityError("END terminal lost its original provider accounting")
    paid_audit = checkpoint_paid_audit_cohort(original_call, running.session_operation_context)
    pass_audit = prepare_end_complete_pass(physical=physical, computation=projected.computation)
    prefix_rows = _prefix_rows(projected)
    child_receipt = str(uuid4())
    response_key = private_chunk_key(owed.response)
    frontier = tuple(
        EffectFrontierEntry(effect_id=entry.effect_id, disposition="committed", receipt_id=child_receipt)
        if entry.effect_id == owed.effect_key
        else entry
        for entry in root.effect_frontier
    )
    after = root.model_copy(update={"effect_frontier": frontier, "pending_settlement_ids": ()})
    event = ContinuationEvent(
        "checkpoint",
        canonical_json(
            {
                "owner": "composer_checkpoint_end_complete_terminal",
                "version": 1,
                "transition": "end_terminal_prepared",
                "attempt_id": owed.attempt_id,
                "effect_key": owed.effect_key,
                "outer_effect_key": owed.outer_effect_key,
                "child_receipt": child_receipt,
                "response_chunk_key": response_key,
                "response_sha256": owed.response.reference.digest,
                "original_call_id": original_call.call_id,
                "original_request_sha256": owed.request_sha256,
                "private_audit_sha256": paid_audit.private_sha256,
                "pass_audit_id": pass_audit.message_id,
                "withheld_id": prefix_rows[0].message_id if projected.withheld_candidate is not None else None,
                "disclosure_id": prefix_rows[-2].message_id,
                "publication_id": prefix_rows[-1].message_id,
            }
        ),
    )
    created_at = database_now(conn)
    preterminal = prepare_snapshot_append(
        owed.selected, snapshot_json=encode_continuation_snapshot(after), events=(event,), published_at=created_at
    )
    finalization = prepare_review_only_finalization(inputs, projected.result, cohort_calls=())
    if projected.result.tool_invocations or len(finalization.audit_cohort) != 1:
        raise AuditIntegrityError("END first terminal slice requires one unmixed graph-review finalizer audit")
    provider_cohort = continuation_provider_audit_drafts_on_connection(conn, running, calls=projected.result.llm_calls)
    if provider_cohort[-len(paid_audit.drafts) :] != paid_audit.drafts:
        # Earlier PRIMARY calls in this same segment remain in the ordinary
        # finalizer's current-source cohort and are rechecked/deduplicated there.
        raise AuditIntegrityError("END terminal finalizer lost its original paid END cohort suffix")
    active_paid = service._deduplicate_private_audit_drafts(
        conn,
        session_id=sid,
        drafts=paid_audit.drafts,
        writer_principal="compose_loop",
        session_operation_context=running.session_operation_context,
        composition_state_id=None,
    )
    if active_paid != paid_audit.drafts:
        raise AuditIntegrityError("END terminal source already published a paid provider audit row")
    prefix_start = service._reserve_sequence_range(conn, sid, count=1)
    assistant_sequence = prefix_start + 1 + len(prefix_rows) + len(paid_audit.drafts)
    assistant = ChatMessageRecord(
        id=finalization.assistant.message_id,
        session_id=running.claim.session_id,
        role="assistant",
        content=finalization.assistant.content,
        created_at=created_at,
        writer_principal="compose_loop",
        sequence_no=assistant_sequence,
        raw_content=finalization.assistant.raw_content,
        composition_state_id=finalization.assistant.composition_state_id,
    )
    complete_audit_cohort = (*finalization.audit_cohort, *provider_cohort)
    terminal_review = service._validate_terminal_graph_review(
        conn,
        running=running,
        drafts=complete_audit_cohort,
        audit_state_id=None,
        prospective_model_pass=pass_audit.record,
    )
    proposals = service._list_composition_proposals_on_connection(conn, session_id=running.claim.session_id)
    state_row = conn.execute(select(states).where(states.c.session_id == sid).order_by(states.c.version.desc()).limit(1)).one_or_none()
    current_state = service._row_to_state_record(state_row) if state_row is not None else None
    if current_state is None or current_state.id != inputs.compose_base_state_id:
        raise AuditIntegrityError("END terminal finalizer lost the original current graph head")
    stored_input = (
        inputs.state_record.review_projection.stored if inputs.state_record.review_projection is not None else inputs.state_record
    )
    if current_state != stored_input:
        raise AuditIntegrityError("END terminal finalizer changed its original graph input")
    review = terminal_review or service._graph_review_on_connection(conn, session_id=current_state.session_id, stored=current_state)
    if review is not None:
        current_state = project_committed_graph_review(current_state, review)
    response = _message_with_state_response(
        assistant, proposals, current_state, state_response=finalization.state_response, live_validation=None
    )
    response = MessageWithStateResponse.model_validate(response.model_dump(mode="python"), strict=True)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == running.claim.segment_sequence
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == running.claim.segment_sequence,
            attempts.c.generation == running.claim.ownership_generation,
        )
    ).one_or_none()
    terminal = prepare_segment_settlement_on_connection(
        conn,
        session_id=sid,
        operation_id=operation,
        parent=parent,
        segment=segment,
        history=history,
        outcome=response,
        settled_by="owner_terminal",
        attempt_outcome="settled",
        prospective_append=preterminal,
    )
    audit_ids = tuple(str(uuid4()) for _draft in complete_audit_cohort)
    return EndCompleteTerminalBatch(
        projected,
        owed,
        preterminal,
        terminal,
        (*_prospective(preterminal), *_prospective(terminal.append)),
        child_receipt,
        response_key,
        original_call,
        quota_entry,
        paid_audit,
        pass_audit,
        prefix_rows,
        finalization,
        provider_cohort,
        audit_ids,
        assistant,
        response,
        created_at,
    )
