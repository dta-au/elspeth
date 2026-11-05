"""Atomic paid V4 END Review retention before a later semantic Stop.

This is a Review action consequence.  It does not publish an assistant or
complete the operation.  The original semantic Stop and its cold reader keep
sole authority to publish a reviewed graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import Connection, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, EndFrame
from elspeth.web.composer.continuation_checkpoint_end_cold_review import ColdProjectedEndReview, require_original_cold_end_review
from elspeth.web.composer.continuation_checkpoint_end_complete_clean import ProjectedEndCompleteClean
from elspeth.web.composer.continuation_checkpoint_end_complete_review import (
    ProjectedEndCompleteReview,
    require_original_end_complete_review,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
)
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
)
from elspeth.web.composer.continuation_end_values import encode_end_candidate
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.visible_tool_event import validate_visible_audit_row
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    TokenUsageEntry,
    settle_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    _require_current_continuation_running,
    append_complete_snapshot,
    continuation_unpaid_terminal_reserve_on_connection,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_end_cold_pending import require_end_pending_cohort_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_action import (
    EndCompleteActionPreview,
    consume_end_complete_action_preview_on_connection,
    prepare_cold_end_complete_action_preview_on_connection,
    prepare_end_complete_action_preview_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import (
    PreparedEndCompletePass,
    prepare_end_complete_pass,
    prepare_first_cold_end_complete_pass,
    write_end_complete_pass_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal_prepare import (
    PreparedEndTerminalAuditRow,
    _prefix_rows,
    _prefix_rows_for_result,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    VerifiedCheckpointEndOwed,
    read_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    CheckpointPaidAuditCohort,
    admit_checkpoint_paid_audit_source_on_connection,
    checkpoint_paid_audit_cohort_for_hold,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
)
from elspeth.web.sessions.continuation_semantic_actions import (
    SemanticActionAdmission,
    installed_action_json,
    read_original_dispatched_semantic_review_on_connection,
)
from elspeth.web.sessions.continuation_semantic_review_result import (
    RetainedSemanticReviewResult,
    SemanticReviewCommitted,
    V4ReviewResultCommit,
    encode_semantic_review_result,
    read_original_semantic_review_result_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects


@dataclass(frozen=True, slots=True)
class EndCompleteReviewBatch:
    owed: VerifiedCheckpointEndOwed
    projected: ProjectedEndCompleteReview | ColdProjectedEndReview
    admission: SemanticActionAdmission
    captured: CapturedContinuationRuntime
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]
    child_receipt: str
    response_chunk_key: str
    original_call: ComposerLLMCall
    quota_entry: TokenUsageEntry
    pass_audit: PreparedEndCompletePass
    paid_audit: CheckpointPaidAuditCohort
    prefix_rows: tuple[PreparedEndTerminalAuditRow, ...]
    result_chunk_key: str
    result_canonical: str
    rebound_canonical: str
    rebound_key: str
    rebound_codec: str
    action_preview: EndCompleteActionPreview
    after_base_json: str
    observed_at: datetime


def prepare_end_complete_review_on_connection(
    conn: Connection,
    *,
    physical: CheckpointEndPhysicalSource | None,
    prepared: PreparedCheckpointProvider | None,
    owed_admission: CheckpointEndOwedAdmission | None,
    owed: VerifiedCheckpointEndOwed,
    selected: SelectedContinuationState | None,
    returned: CheckpointReturnedResponseObservation | None,
    projected: ProjectedEndCompleteReview | ColdProjectedEndReview,
    observed_at: datetime,
) -> EndCompleteReviewBatch:
    """Freeze all retained Review DATA and its next unpaid terminal reserve."""
    if type(projected) is ColdProjectedEndReview:
        require_original_cold_end_review(projected)
        source = projected.source
        work = source.runtime.bindings.required_work
        running = None if work is None else work.running
        if running is None or any(item is not None for item in (physical, prepared, owed_admission, selected, returned)):
            raise AuditIntegrityError("cold END Review mixed warm paid authority")
        if (
            source.owed != owed
            or source.selected.checkpoint_id != owed.selected.checkpoint_id
            or source.selected.stack != decode_continuation_snapshot(owed.selected.payload_json)
        ):
            raise AuditIntegrityError("cold END Review changed its original current H")
        source_selected = source.selected
        original_call = decode_retained_physical_audit_call(owed.response)
        pass_audit = prepare_first_cold_end_complete_pass(gate=projected.gate)
        prefix_rows = (
            ()
            if projected.gate.computation.action == "fallthrough"
            else _prefix_rows_for_result(projected.withheld_candidate, projected.execution.public_result)
        )
        dispatch_checkpoint_id = None if owed.phase_source is None else owed.phase_source.checkpoint_id
        dispatch_cursor = None if owed.phase_source is None else owed.phase_source.cursor
    else:
        if (
            type(projected) is not ProjectedEndCompleteReview
            or type(physical) is not CheckpointEndPhysicalSource
            or type(prepared) is not PreparedCheckpointProvider
            or type(owed_admission) is not CheckpointEndOwedAdmission
            or type(selected) is not SelectedContinuationState
            or type(returned) is not CheckpointReturnedResponseObservation
        ):
            raise AuditIntegrityError("warm END Review lost its original paid issuer")
        require_original_end_complete_review(projected, physical=physical, owed=owed_admission, selected=selected, returned=returned)
        running = prepared.running
        source_selected = selected
        original_call = original_v4_checkpoint_returned_audit_call(
            returned, attempt_id=owed.attempt_id, request_sha256=prepared.request_sha256
        )
        pass_audit = prepare_end_complete_pass(physical=physical, computation=projected.terminal.computation, owed=owed)
        prefix_rows = () if isinstance(projected.terminal, ProjectedEndCompleteClean) else _prefix_rows(projected.terminal)
        if owed.phase_source is None:
            raise AuditIntegrityError("END Review lost its original semantic dispatch D")
        if prepared.retry_ordinal == 0:
            if owed.retry_history is not None or owed.phase_source != prepared.prepared_source.source:
                raise AuditIntegrityError("END Review changed its original first physical source")
        elif prepared.retry_ordinal == 1:
            if owed.retry_history is None or owed.retry_history.selected != prepared.prepared_source.source:
                raise AuditIntegrityError("END Review changed its original retry-ready physical source")
        else:
            raise AuditIntegrityError("END Review exceeded its one bounded format retry")
        dispatch_checkpoint_id = owed.phase_source.checkpoint_id
        dispatch_cursor = owed.phase_source.cursor
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if dispatch_checkpoint_id is None or dispatch_cursor is None or original_call.messages_hash != owed.request_sha256:
        raise AuditIntegrityError("END Review lost its exact original dispatch and request identity")
    execution, captured = projected.execution, projected.captured
    before = decode_continuation_snapshot(owed.selected.payload_json)
    if (
        (owed_admission is not None and owed.selected != owed_admission.owed_selected)
        or execution.current is not source_selected
        or execution.runtime.source.selected is not source_selected
        or captured.selected is not source_selected
        or source_selected.stack != before
        or captured.stack.model_copy(update={"frames": before.frames}) != before
        or before.pending_settlement_ids != (owed.attempt_id,)
        or before.pending_owner_results
        or len(before.frames) != 2
        or type(before.frames[0]) is not ComposeFrame
        or type(before.frames[1]) is not EndFrame
    ):
        raise AuditIntegrityError("END complete Review changed its owed action or captured runtime")
    if original_call.status is not ComposerLLMCallStatus.SUCCESS or type(original_call.call_id) is not str or not original_call.call_id:
        raise AuditIntegrityError("END complete Review cannot retain an incomplete paid result")
    quota_entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(original_call))
    if quota_entry is None or quota_entry.call_id != owed.attempt_id:
        raise AuditIntegrityError("END complete Review lost original provider accounting")
    paid_audit = checkpoint_paid_audit_cohort_for_hold(original_call, owed.new_hold)
    semantic_source = before.active_action_source
    if semantic_source is None:
        raise AuditIntegrityError("END complete Review lost its original paid semantic action")
    admission = read_original_dispatched_semantic_review_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation,
        dispatch_checkpoint_id=dispatch_checkpoint_id,
        semantic_effect_key=semantic_source.effect_key,
    )
    candidate, owner_key = admission.review_candidate, admission.owner_effect_key
    if (
        candidate is None
        or owner_key != owed.outer_effect_key
        or admission.action_key is None
        or before.active_action_json != installed_action_json(admission)
        or before.active_action_source != admission.paid.source
    ):
        raise AuditIntegrityError("END complete Review lost its original paid semantic action")
    compose, end = captured.stack.frames
    if (
        type(compose) is not ComposeFrame
        or type(end) is not EndFrame
        or end != before.frames[-1]
        or compose.pending_response != candidate.response
    ):
        raise AuditIntegrityError("END complete Review changed its captured Compose/END pair")
    state = next((chunk for chunk in (*captured.chunks, *source_selected.chunks) if chunk.reference == compose.state), None)
    if state is None or state.canonical != canonical_json(execution.public_result.state.to_dict()):
        raise AuditIntegrityError("END complete Review changed its original shared gate state")
    rebound = candidate.model_copy(update={"state": compose.state, "evidence": compose.evidence, "iteration": compose.iteration})
    rebound_chunk = encode_end_candidate(rebound)
    result_json = encode_semantic_review_result(RetainedSemanticReviewResult(execution.public_result, execution.publication_binding))
    child_receipt = str(uuid4())
    response_key = private_chunk_key(owed.response)
    commit = V4ReviewResultCommit(
        session_id=running.claim.session_id,
        operation_id=operation,
        semantic_effect_key=admission.paid.source.effect_key,
        owner_effect_key=owner_key,
        dispatch_checkpoint_id=dispatch_checkpoint_id,
        dispatch_cursor=dispatch_cursor,
        action_key=admission.action_key,
        candidate_digest=candidate.response.digest,
        result_json=result_json,
        outcome="clean" if execution.disposition.value == "clean" else "blocked",
        pass_audit_message_id=pass_audit.message_id,
        pass_index=pass_audit.record.pass_index,
        owed_checkpoint_id=owed.selected.checkpoint_id,
        owed_cursor=owed.selected.cursor,
        provider_attempt_id=owed.attempt_id,
        physical_effect_key=owed.effect_key,
        checkpoint_call_id=original_call.call_id,
        private_audit_sha256=paid_audit.private_sha256,
    )
    canonical = canonical_json(commit.model_dump(mode="json"))
    if len(canonical.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("END complete Review result exceeds its unchanged private document bound")
    require_no_credential_material(
        canonical,
        surface="composer_continuation_semantic_review_result",
        env_ref_names=execution.runtime.env_ref_names,
        max_string_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    result_key = "semantic-review-result:" + sha256(canonical.encode("utf-8")).hexdigest()
    frontier = tuple(
        EffectFrontierEntry(
            effect_id=item.effect_id,
            disposition="committed",
            receipt_id=child_receipt if item.effect_id == owed.effect_key else owner_key,
        )
        if item.effect_id in (owed.effect_key, owner_key)
        else item
        for item in before.effect_frontier
    )
    published = end.model_copy(
        update={
            "phase": "publication_ready" if commit.outcome == "clean" else "repair_needed",
            "state": compose.state,
            "candidate": rebound_chunk.reference,
            "publication_binding": execution.publication_binding if commit.outcome == "clean" else None,
        }
    )
    base = captured.stack.model_copy(update={"effect_frontier": frontier, "pending_settlement_ids": (), "frames": (compose, published)})
    if prepared is None:
        action = prepare_cold_end_complete_action_preview_on_connection(conn, running=running, owed=owed, after_base=base)
    else:
        action = prepare_end_complete_action_preview_on_connection(conn, prepared=prepared, owed=owed, after_base=base)
    if action is None:
        raise AuditIntegrityError("END complete Review lost its prescribed action receipt")
    event = ContinuationEvent(
        "checkpoint",
        canonical_json(
            {
                "owner": "composer_semantic_review",
                "transition": "review_result_retained",
                "semantic_effect_key": commit.semantic_effect_key,
                "owner_effect_key": owner_key,
                "result_chunk_key": result_key,
                "dispatch_checkpoint_id": commit.dispatch_checkpoint_id,
                "dispatch_cursor": commit.dispatch_cursor,
                "action_key": commit.action_key,
                "outcome": commit.outcome,
                "owed_checkpoint_id": commit.owed_checkpoint_id,
                "owed_cursor": commit.owed_cursor,
                "provider_attempt_id": commit.provider_attempt_id,
            }
        ),
    )
    append = prepare_snapshot_append(owed.selected, snapshot_json=action.successor_json, events=(event,), published_at=observed_at)
    chunks = tuple(
        ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
        for chunk in captured.chunks
    )
    prospective = (
        *chunks,
        ProspectiveContinuationChunk(
            private_chunk_key(rebound_chunk), rebound_chunk.canonical.encode("utf-8"), rebound_chunk.reference.codec
        ),
        ProspectiveContinuationChunk(result_key, canonical.encode("utf-8"), commit.schema_version),
        ProspectiveContinuationChunk(
            private_chunk_key(action.frontier), action.frontier.canonical.encode("utf-8"), action.frontier.reference.codec
        ),
        ProspectiveContinuationChunk(f"event:{owed.selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
        *continuation_unpaid_terminal_reserve_on_connection(
            conn, session_id=sid, operation_id=operation, post_root_json=append.snapshot_json, post_cursor=owed.selected.cursor + 1
        ),
    )
    return EndCompleteReviewBatch(
        owed,
        projected,
        admission,
        captured,
        append,
        prospective,
        child_receipt,
        response_key,
        original_call,
        quota_entry,
        pass_audit,
        paid_audit,
        prefix_rows,
        result_key,
        canonical,
        rebound_chunk.canonical,
        private_chunk_key(rebound_chunk),
        rebound_chunk.reference.codec,
        action,
        encode_continuation_snapshot(base),
        observed_at,
    )


def _write_prefix_row(
    conn: Connection, *, service: object, running: object, prepared: PreparedEndTerminalAuditRow, created_at: datetime
) -> int:
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("END Review prefix lost its original fenced writer")
    draft = prepared.draft
    if draft.role != "audit" or draft.tool_calls is None or len(draft.tool_calls) != 1:
        raise AuditIntegrityError("END Review prefix lost its original audit envelope")
    sid = str(running.claim.session_id)
    context = running.session_operation_context
    validate_visible_audit_row(
        envelopes=deep_thaw(draft.tool_calls),
        content=draft.content,
        role="audit",
        writer_principal="compose_loop",
        session_id=sid,
        context=context,
    )
    sequence = service._reserve_sequence_range(conn, sid, count=1)
    service._insert_chat_message(
        conn,
        session_id=sid,
        role="audit",
        content=draft.content,
        raw_content=None,
        tool_calls=deep_thaw(draft.tool_calls),
        sequence_no=sequence,
        writer_principal="compose_loop",
        composition_state_id=draft.effective_composition_state_id(None),
        tool_call_id=None,
        parent_assistant_id=None,
        created_at=created_at,
        session_operation_context=context,
        message_id=prepared.message_id,
    )
    return sequence


def persist_end_complete_review_on_connection(
    conn: Connection,
    *,
    service: object,
    physical: CheckpointEndPhysicalSource | None,
    prepared: PreparedCheckpointProvider | None,
    owed_admission: CheckpointEndOwedAdmission | None,
    selected: SelectedContinuationState | None,
    returned: CheckpointReturnedResponseObservation | None,
    projected: ProjectedEndCompleteReview | ColdProjectedEndReview,
) -> SemanticReviewCommitted:
    """Commit complete paid Review DATA, pass and action in one transaction."""
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or not conn.in_transaction():
        raise AuditIntegrityError("END complete Review requires its original session transaction")
    if type(projected) is ColdProjectedEndReview:
        require_original_cold_end_review(projected)
        work = projected.source.runtime.bindings.required_work
        running = None if work is None else work.running
        if running is None or any(item is not None for item in (physical, prepared, owed_admission, selected, returned)):
            raise AuditIntegrityError("cold END Review mixed warm paid authority")
        attempt_id = projected.source.owed.attempt_id
        names = projected.source.runtime.env_ref_names
    else:
        if (
            type(projected) is not ProjectedEndCompleteReview
            or type(physical) is not CheckpointEndPhysicalSource
            or type(prepared) is not PreparedCheckpointProvider
            or type(owed_admission) is not CheckpointEndOwedAdmission
            or type(selected) is not SelectedContinuationState
            or type(returned) is not CheckpointReturnedResponseObservation
        ):
            raise AuditIntegrityError("warm END Review lost its original paid issuer")
        running = prepared.running
        attempt_id = owed_admission.original.attempt.attempt_id
        names = prepared.env_ref_names
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with service._session_write_lock(conn, sid):
        _require_current_continuation_running(conn, running)
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=attempt_id,
            names=names,
        )
        if (
            owed is None
            or (type(projected) is ColdProjectedEndReview and owed != projected.source.owed)
            or (type(projected) is ProjectedEndCompleteReview and (owed_admission is None or owed.selected != owed_admission.owed_selected))
            or load_snapshot(conn, session_id=sid, operation_id=operation) != owed.selected
        ):
            raise AuditIntegrityError("END complete Review lost its rebound original paid H")
        require_end_pending_cohort_on_connection(conn, owed=owed)
        batch = prepare_end_complete_review_on_connection(
            conn,
            physical=physical,
            prepared=prepared,
            owed_admission=owed_admission,
            owed=owed,
            selected=selected,
            returned=returned,
            projected=projected,
            observed_at=database_now(conn),
        )
        admitted_audit = admit_checkpoint_paid_audit_source_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            owed=owed,
            call=batch.original_call,
            cohort=batch.paid_audit,
            names=names,
        )
        admit_retained_payload_chunks(
            conn,
            session_id=sid,
            operation_id=operation,
            prospective=batch.prospective,
            retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
            consuming_hold=owed.new_hold,
        )
        for chunk in batch.captured.chunks:
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=batch.rebound_key,
            payload=batch.rebound_canonical.encode("utf-8"),
            codec=batch.rebound_codec,
        )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=batch.result_chunk_key,
            payload=batch.result_canonical.encode("utf-8"),
            codec="composer.semantic-review-result.v2",
        )
        child = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owed.effect_key,
                effects.c.owner == "provider",
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == owed.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="committed",
                receipt_key=batch.child_receipt,
                result_chunk_key=batch.response_chunk_key,
                response_finished_at=batch.original_call.finished_at,
            )
        )
        outer = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owed.outer_effect_key,
                effects.c.owner == "composer_end",
                effects.c.disposition == "dispatched",
                effects.c.receipt_key.is_(None),
                effects.c.action_json == installed_action_json(batch.admission),
            )
            .values(disposition="committed", receipt_key=owed.outer_effect_key, result_chunk_key=batch.result_chunk_key)
        )
        if child.rowcount != 1 or outer.rowcount != 1:
            raise AuditIntegrityError("END complete Review lost its original child or action effect CAS")
        consume_end_complete_action_preview_on_connection(
            conn, owed=owed, after_base=decode_continuation_snapshot(batch.after_base_json), preview=batch.action_preview
        )
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=owed.selected.checkpoint_id,
            expected_cursor=owed.selected.cursor,
            snapshot_json=batch.append.snapshot_json,
            event_batch=batch.append.events,
            handoff=False,
            prepared_append=batch.append,
        )
        if stored.checkpoint_id != batch.append.checkpoint_id or stored.payload_json != batch.append.snapshot_json:
            raise AuditIntegrityError("END complete Review changed its exact prepared successor")
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=owed.attempt_id, entry=batch.quota_entry)
        write_end_complete_pass_on_connection(
            conn, service=service, context=running.session_operation_context, prepared=batch.pass_audit, created_at=batch.observed_at
        )
        write_checkpoint_paid_audit_cohort_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            cohort=batch.paid_audit,
            observed_at=batch.observed_at,
            admitted=admitted_audit,
        )
        for row in batch.prefix_rows:
            _write_prefix_row(conn, service=service, running=running, prepared=row, created_at=batch.observed_at)
        consume_provider_terminal_hold_after_complete_result(
            conn, source=owed.new_hold, provider_attempt_id=owed.attempt_id, consumed_at=batch.observed_at
        )
        cold = read_original_semantic_review_result_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, review_effect_key=owed.outer_effect_key
        )
        if (
            cold.completed != stored
            or cold.result != projected.execution.public_result
            or cold.outcome != projected.execution.disposition.value
        ):
            raise AuditIntegrityError("END complete Review lost its actual retained result")
        return cold
