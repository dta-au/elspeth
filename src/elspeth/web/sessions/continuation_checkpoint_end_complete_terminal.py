"""Atomic first-slice END paid consequence and shared terminal publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial

from sqlalchemy import Connection, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.web.composer.continuation_checkpoint_end_complete_terminal import ProjectedEndCompleteTerminal
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_finalization import ComposerFinalizationInputs, _message_with_state_response
from elspeth.web.sessions.composer_operations import ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, append_complete_snapshot, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import write_end_complete_pass_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal_prepare import (
    EndCompleteTerminalBatch,
    PreparedEndTerminalAuditRow,
    prepare_end_complete_terminal_batch_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    read_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import write_checkpoint_paid_audit_cohort_on_connection
from elspeth.web.sessions.continuation_retained_capacity import (
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects


@dataclass(frozen=True, slots=True)
class EndCompleteTerminalOutcome:
    batch: EndCompleteTerminalBatch
    record: ComposerOperationRecord
    deferred_telemetry: tuple[AuditMessageDraft, ...]

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_telemetry")


def _write_prefix_row(
    conn: Connection,
    *,
    service: object,
    running: ComposerOperationRunning,
    prepared: PreparedEndTerminalAuditRow,
    created_at: datetime,
) -> int:
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("END terminal prefix lost its original writer or running owner")
    sid = str(running.claim.session_id)
    context = running.session_operation_context
    draft = prepared.draft
    if draft.role != "audit" or draft.tool_calls is None or len(draft.tool_calls) != 1:
        raise AuditIntegrityError("END terminal prefix changed its original audit shape")
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


async def persist_end_complete_terminal_on_connection(
    conn: Connection,
    *,
    service: object,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed_admission: CheckpointEndOwedAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEndCompleteTerminal,
    inputs: ComposerFinalizationInputs,
) -> EndCompleteTerminalOutcome:
    """Consume original END H and the complete shared finalizer in one lock.

    This coroutine does not suspend after entering the supplied transaction.
    The caller must emit deferred telemetry only after the enclosing commit.
    """
    from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal_cold import (
        require_end_complete_terminal_postwrite_on_connection,
    )
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or not conn.in_transaction():
        raise AuditIntegrityError("END terminal requires its original session SQL transaction")
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
            raise AuditIntegrityError("END terminal lost its rebound original paid head")
        batch = prepare_end_complete_terminal_batch_on_connection(
            conn,
            service=service,
            physical=physical,
            prepared=prepared,
            owed=owed,
            returned=returned,
            projected=projected,
            inputs=inputs,
        )
        admit_retained_payload_chunks(
            conn,
            session_id=sid,
            operation_id=operation,
            prospective=batch.prospective,
            retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
            consuming_hold=owed.new_hold,
        )
        changed = conn.execute(
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
        if changed.rowcount != 1:
            raise AuditIntegrityError("END terminal lost its original physical child effect CAS")
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=owed.attempt_id, entry=batch.quota_entry)
        pass_sequence = write_end_complete_pass_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            prepared=batch.pass_audit,
            created_at=batch.assistant_created_at,
        )
        prefix_sequences = tuple(
            _write_prefix_row(conn, service=service, running=running, prepared=row, created_at=batch.assistant_created_at)
            for row in batch.prefix_rows
        )
        write_checkpoint_paid_audit_cohort_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            cohort=batch.paid_audit,
            observed_at=batch.assistant_created_at,
        )
        if service._reserve_sequence_range(conn, sid, count=1) != batch.assistant.sequence_no:
            raise AuditIntegrityError("END terminal prefix changed its prepared assistant sequence")
        preterminal = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=owed.selected.checkpoint_id,
            expected_cursor=owed.selected.cursor,
            snapshot_json=batch.preterminal.snapshot_json,
            event_batch=batch.preterminal.events,
            handoff=False,
            prepared_append=batch.preterminal,
            consuming_hold=owed.new_hold,
        )
        if preterminal.checkpoint_id != batch.preterminal.checkpoint_id:
            raise AuditIntegrityError("END terminal changed its exact paid preterminal append")
        consume_provider_terminal_hold_after_complete_result(
            conn, source=owed.new_hold, provider_attempt_id=owed.attempt_id, consumed_at=batch.assistant_created_at
        )
        deferred: list[AuditMessageDraft] = []
        record = await service.complete_composer_async_operation(
            running,
            assistant=batch.finalization.assistant,
            assistant_record=None,
            audit_cohort=batch.finalization.audit_cohort,
            audit_composition_state_id=None,
            build_response=partial(_message_with_state_response, state_response=batch.finalization.state_response, live_validation=None),
            provider_audit_calls=projected.result.llm_calls,
            publication_result=projected.result,
            transaction_conn=conn,
            terminal_created_at=batch.assistant_created_at,
            audit_message_ids=batch.audit_message_ids,
            expected_provider_cohort=batch.provider_cohort,
            expected_message=batch.assistant,
            expected_response=batch.response,
            prepared_settlement=batch.terminal,
            deferred_telemetry=deferred,
        )
        if type(record) is not ComposerOperationRecord or record.status != "completed":
            raise AuditIntegrityError("END terminal lost its original completed operation")
        require_end_complete_terminal_postwrite_on_connection(
            conn,
            prepared=prepared,
            batch=batch,
            record=record,
            pass_sequence=pass_sequence,
            prefix_sequences=prefix_sequences,
        )
        return EndCompleteTerminalOutcome(batch, record, tuple(deferred))
