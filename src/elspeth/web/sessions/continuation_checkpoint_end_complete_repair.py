"""Exact prospective END repair successor from one paid V4 owed head.

The batch includes the detached runtime, original paid audit and the complete
post-root unpaid terminal floor. Construction grants no SQL write authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Connection, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, EffectFrontierEntry, EndFrame
from elspeth.web.composer.continuation_checkpoint_end_complete_repair import (
    ProjectedEndCompleteRepair,
    require_original_end_complete_repair,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    TokenUsageEntry,
    settle_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
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
from elspeth.web.sessions.continuation_checkpoint_end_complete_action import (
    EndCompleteActionPreview,
    consume_end_complete_action_preview_on_connection,
    prepare_end_complete_action_preview_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import (
    PreparedEndCompletePass,
    prepare_end_complete_pass,
    write_end_complete_pass_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair_cold import require_end_complete_repair_postwrite_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_withheld import write_end_repair_withheld_reply_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_owed import CheckpointEndOwedAdmission, VerifiedCheckpointEndOwed
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    CheckpointPaidAuditCohort,
    checkpoint_paid_audit_cohort,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments

_OWNER = "composer_checkpoint_end_complete"


@dataclass(frozen=True, slots=True)
class EndCompleteRepairBatch:
    projected: ProjectedEndCompleteRepair
    owed: VerifiedCheckpointEndOwed
    append: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]
    unpaid_reserve: tuple[ProspectiveContinuationChunk, ...]
    child_receipt: str
    outer_receipt: str
    response_chunk_key: str
    next_action_json: str
    original_call: ComposerLLMCall
    quota_entry: TokenUsageEntry
    audit_cohort: CheckpointPaidAuditCohort
    pass_audit: PreparedEndCompletePass
    after_base: ContinuationFrameStack
    action_preview: EndCompleteActionPreview | None


def prepare_end_complete_repair_batch_on_connection(
    conn: Connection,
    *,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed_admission: CheckpointEndOwedAdmission,
    owed: VerifiedCheckpointEndOwed,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEndCompleteRepair,
    published_at: datetime,
) -> EndCompleteRepairBatch:
    """Measure every retained byte against the rebound original paid H."""
    require_original_end_complete_repair(
        projected,
        physical=physical,
        prepared=prepared,
        owed=owed_admission,
        selected=selected,
        returned=returned,
    )
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    before = decode_continuation_snapshot(owed.selected.payload_json)
    captured = projected.captured
    if (
        type(owed) is not VerifiedCheckpointEndOwed
        or owed.session_id != sid
        or owed.operation_id != operation
        or owed.attempt_id != owed_admission.original.attempt.attempt_id
        or owed.selected != owed_admission.owed_selected
        or owed.new_hold != owed_admission.owed_hold
        or owed.response != returned.response
        or captured.selected is not selected
        or selected.stack != before
        or captured.stack.model_copy(update={"frames": before.frames}) != before
        or len(before.frames) != 2
        or type(before.frames[0]) is not ComposeFrame
        or type(before.frames[1]) is not EndFrame
        or len(captured.stack.frames) != 2
        or captured.stack.frames[1] != before.frames[1]
        or type(captured.stack.frames[0]) is not ComposeFrame
        or before.pending_settlement_ids != (owed.attempt_id,)
        or before.pending_owner_results
    ):
        raise AuditIntegrityError("END complete repair changed its exact owed runtime or retained scope")
    compose = before.frames[0]
    changed_compose = captured.stack.frames[0]
    if type(compose) is not ComposeFrame or type(changed_compose) is not ComposeFrame:
        raise AuditIntegrityError("END complete repair lost its single Compose frame")
    if (
        compose.pending_response is None
        or changed_compose.pending_response != compose.pending_response
        or changed_compose.initial_version != compose.initial_version
        or changed_compose.slice_sequence != compose.slice_sequence
        or changed_compose.advisor_checkpoint_passes - compose.advisor_checkpoint_passes != projected.computation.passes_delta
        or changed_compose.repair_turns != compose.repair_turns
        or changed_compose.advisor_repair_context_introduced is not True
    ):
        raise AuditIntegrityError("END complete repair did not apply its original paid pass exactly once")
    open_effects = tuple(item.effect_id for item in before.effect_frontier if item.disposition == "dispatched")
    if set(open_effects) != {owed.effect_key, owed.outer_effect_key} or len(open_effects) != 2:
        raise AuditIntegrityError("END complete repair found another open effect")
    following = changed_compose.model_copy(
        update={"phase": "action_ready", "pending_response": None, "provider_effect_key": None, "provider_input_sha256": None}
    )
    child_receipt = str(uuid4())
    outer_receipt = f"repair:{following.iteration.digest}"
    frontier = tuple(
        EffectFrontierEntry(
            effect_id=item.effect_id,
            disposition="committed",
            receipt_id=child_receipt if item.effect_id == owed.effect_key else outer_receipt,
        )
        if item.effect_id in (owed.effect_key, owed.outer_effect_key)
        else item
        for item in before.effect_frontier
    )
    after_base = captured.stack.model_copy(update={"frames": (following,), "effect_frontier": frontier, "pending_settlement_ids": ()})
    action_preview = prepare_end_complete_action_preview_on_connection(conn, prepared=prepared, owed=owed, after_base=after_base)
    after = decode_continuation_snapshot(action_preview.successor_json) if action_preview is not None else after_base
    policy = decode_continuation_policy(before.admission_policy_json, before.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("END complete repair lost its original V4 next-action policy")
    original_call = original_v4_checkpoint_returned_audit_call(returned, attempt_id=owed.attempt_id, request_sha256=prepared.request_sha256)
    if original_call.status is not ComposerLLMCallStatus.SUCCESS:
        raise AuditIntegrityError("END complete repair cannot settle an incomplete original response")
    quota_entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(original_call))
    if quota_entry is None or quota_entry.call_id != owed.attempt_id:
        raise AuditIntegrityError("END complete repair lost original paid accounting")
    cohort = checkpoint_paid_audit_cohort(original_call, running.session_operation_context)
    pass_audit = prepare_end_complete_pass(physical=physical, computation=projected.computation)
    response_key = private_chunk_key(owed.response)
    event = ContinuationEvent(
        "checkpoint",
        canonical_json(
            {
                "owner": _OWNER,
                "version": 1,
                "transition": "end_repair_continued",
                "attempt_id": owed.attempt_id,
                "effect_key": owed.effect_key,
                "outer_effect_key": owed.outer_effect_key,
                "child_receipt": child_receipt,
                "outer_receipt": outer_receipt,
                "response_chunk_key": response_key,
                "response_sha256": owed.response.reference.digest,
                "original_call_id": original_call.call_id,
                "original_request_sha256": owed.request_sha256,
                "private_audit_sha256": cohort.private_sha256,
                "pass_audit_id": pass_audit.message_id,
                "withheld_reply_id": projected.withheld.message_id if projected.withheld is not None else None,
                "iteration_sha256": following.iteration.digest,
                "action_frontier_sha256": action_preview.frontier.reference.digest if action_preview is not None else None,
            }
        ),
    )
    append = prepare_snapshot_append(
        owed.selected, snapshot_json=encode_continuation_snapshot(after), events=(event,), published_at=published_at
    )
    chunks = tuple(
        ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
        for chunk in captured.chunks
    )
    prospective = (
        *chunks,
        *(
            (
                ProspectiveContinuationChunk(
                    private_chunk_key(action_preview.frontier),
                    action_preview.frontier.canonical.encode("utf-8"),
                    action_preview.frontier.reference.codec,
                ),
            )
            if action_preview is not None
            else ()
        ),
        ProspectiveContinuationChunk(f"event:{owed.selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    reserve = continuation_unpaid_terminal_reserve_on_connection(
        conn, session_id=sid, operation_id=operation, post_root_json=append.snapshot_json, post_cursor=owed.selected.cursor + 1
    )
    return EndCompleteRepairBatch(
        projected,
        owed,
        append,
        prospective,
        reserve,
        child_receipt,
        outer_receipt,
        response_key,
        policy.actions.primary.model_dump_json(),
        original_call,
        quota_entry,
        cohort,
        pass_audit,
        after_base,
        action_preview,
    )


def persist_end_complete_repair_on_connection(
    conn: Connection,
    *,
    service: object,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed_admission: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEndCompleteRepair,
) -> EndCompleteRepairBatch:
    """Settle one original call and repair successor in one locked transaction."""
    from elspeth.web.sessions.continuation_checkpoint_end_owed import read_v4_checkpoint_end_owed_on_connection
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or not conn.in_transaction():
        raise AuditIntegrityError("END complete repair requires its original session SQL transaction")
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with service._session_write_lock(conn, sid):
        _require_current_continuation_running(conn, running)
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=owed_admission.original.attempt.attempt_id,
            names=prepared.env_ref_names,
        )
        if owed is None or current != owed.selected or owed.selected != owed_admission.owed_selected:
            raise AuditIntegrityError("END complete repair lost its rebound original paid head")
        now = database_now(conn)
        batch = prepare_end_complete_repair_batch_on_connection(
            conn,
            physical=physical,
            prepared=prepared,
            owed_admission=owed_admission,
            owed=owed,
            selected=selected,
            returned=returned,
            projected=projected,
            published_at=now,
        )
        admit_retained_payload_chunks(
            conn,
            session_id=sid,
            operation_id=operation,
            prospective=(*batch.prospective, *batch.unpaid_reserve),
            retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
            consuming_hold=owed.new_hold,
        )
        for chunk in projected.captured.chunks:
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
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
        captured_compose = projected.captured.stack.frames[0]
        if type(captured_compose) is not ComposeFrame:
            raise AuditIntegrityError("END complete repair changed its nominal captured Compose frame")
        outer = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owed.outer_effect_key,
                effects.c.owner == "composer_end",
                effects.c.disposition == "dispatched",
                effects.c.creator_sequence == running.claim.segment_sequence,
                effects.c.creator_generation == running.claim.ownership_generation,
                effects.c.receipt_key.is_(None),
            )
            .values(
                disposition="committed",
                receipt_key=batch.outer_receipt,
                result_chunk_key=f"value:{captured_compose.iteration.codec}:{captured_compose.iteration.digest}",
            )
        )
        if child.rowcount != 1 or outer.rowcount != 1:
            raise AuditIntegrityError("END complete repair lost its original child or outer effect CAS")
        if batch.action_preview is not None:
            consume_end_complete_action_preview_on_connection(
                conn,
                owed=owed,
                after_base=batch.after_base,
                preview=batch.action_preview,
            )
        changed_segment = conn.execute(
            update(segments)
            .where(
                segments.c.session_id == sid,
                segments.c.operation_id == operation,
                segments.c.sequence == running.claim.segment_sequence,
                segments.c.generation == running.claim.ownership_generation,
                segments.c.state == "running",
            )
            .values(next_action_json=batch.next_action_json)
        )
        if changed_segment.rowcount != 1:
            raise AuditIntegrityError("END complete repair lost its original segment next action")
        retained = append_complete_snapshot(
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
        if retained.checkpoint_id != batch.append.checkpoint_id or retained.payload_json != batch.append.snapshot_json:
            raise AuditIntegrityError("END complete repair changed its exact prepared successor")
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=owed.attempt_id, entry=batch.quota_entry)
        pass_sequence = write_end_complete_pass_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            prepared=batch.pass_audit,
            created_at=now,
        )
        write_checkpoint_paid_audit_cohort_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            cohort=batch.audit_cohort,
            observed_at=now,
        )
        withheld_sequence = (
            write_end_repair_withheld_reply_on_connection(
                conn,
                service=service,
                context=running.session_operation_context,
                prepared=projected.withheld,
                created_at=now,
            )
            if projected.withheld is not None
            else None
        )
        require_end_complete_repair_postwrite_on_connection(
            conn,
            prepared=prepared,
            batch=batch,
            pass_sequence=pass_sequence,
            withheld_sequence=withheld_sequence,
            observed_at=now,
            hold_state="active",
        )
        consume_provider_terminal_hold_after_complete_result(
            conn, source=owed.new_hold, provider_attempt_id=owed.attempt_id, consumed_at=database_now(conn)
        )
        require_end_complete_repair_postwrite_on_connection(
            conn,
            prepared=prepared,
            batch=batch,
            pass_sequence=pass_sequence,
            withheld_sequence=withheld_sequence,
            observed_at=now,
            hold_state="consumed",
        )
        return batch
