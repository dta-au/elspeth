"""Atomic V4 value-batch publication under the original complete-tail scope."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.continuation_p4_outcome import encode_audit_outcome_data
from elspeth.web.composer.continuation_prepared_p4 import prepared_p4_members
from elspeth.web.coordination.composer_mutation_guard import check_auto_mutation_on_connection
from elspeth.web.sessions._persist_payload import AuditOutcome, CommittedCheckpointWitness
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationOwnerCommit, PreparedContinuationAppend
from elspeth.web.sessions.continuation_p4 import require_selected_p4_input, selected_p4_drain_scope
from elspeth.web.sessions.continuation_prepared_tail import require_prepared_tail_append
from elspeth.web.sessions.continuation_value_batch_transitions import PreparedValueBatchTransitions
from elspeth.web.sessions.models import approvals_table, chat_messages_table, composition_states_table, interpretation_events_table
from elspeth.web.sessions.protocol import StaleComposeStateError

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatch
    from elspeth.web.required_work import RequiredPreviewProduced
    from elspeth.web.sessions.continuation_value_batch_publication import PreparedValueBatchPublication
    from elspeth.web.sessions.service import SessionServiceImpl


def plan_value_batch_publication_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    service: SessionServiceImpl,
    batch: PreparedValueBatch,
    transitions: PreparedValueBatchTransitions,
    created_at: datetime,
    message_ids: tuple[UUID, ...] | None = None,
    state_ids: tuple[UUID, ...] | None = None,
    rejection_ids: tuple[UUID, ...] | None = None,
    issued_preview: RequiredPreviewProduced | None = None,
) -> PreparedValueBatchPublication:
    """Prove the current row source before the pure planner selects any rows."""
    from elspeth.web.sessions.continuation_value_batch_publication import plan_value_batch_publication, value_batch_publication_shape

    transitions.require_original()
    if transitions.prepared is not batch or batch.inventory.running is not running:
        raise AuditIntegrityError("value publication changed its original prepared batch owner")
    sid, context = str(running.claim.session_id), running.session_operation_context
    service._assert_session_write_lock_held(conn, sid, caller="plan_value_batch_publication_on_connection")
    service._require_session_write_authority_on_connection(conn, context, session_id=sid)
    members = prepared_p4_members(batch.prepared, env_ref_names=batch.inventory.names)
    if issued_preview is not None:
        from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_issued_preview_batch

        require_prepared_issued_preview_batch(batch, issued_preview)
        if (
            len(batch.requested_calls) != 1
            or batch.requested_calls[0].function.name != "preview_pipeline"
            or len(members.invocations) != 1
            or members.invocations[0].tool_name != "preview_pipeline"
            or members.invocations[0].version_after != members.invocations[0].version_before
            or any(row.composition_state_payload is not None for row in members.redacted_rows)
        ):
            raise AuditIntegrityError("issued preview publication cannot change graph or authored state")
    # This atomic value path cannot retire a review or approval as an incidental
    # effect of publishing a detached tool result.
    pending = conn.execute(
        select(interpretation_events_table.c.id)
        .where(
            interpretation_events_table.c.session_id == sid,
            interpretation_events_table.c.choice == "pending",
        )
        .limit(1)
    ).first()
    approval = conn.execute(
        select(approvals_table.c.approval_id)
        .where(
            approvals_table.c.session_id == sid,
            approvals_table.c.decision.is_(None),
        )
        .limit(1)
    ).first()
    if issued_preview is None and (pending is not None or approval is not None):
        raise AuditIntegrityError("value publication cannot silently retire pending reviews or approvals")
    current = conn.execute(
        select(composition_states_table)
        .where(composition_states_table.c.session_id == sid)
        .order_by(composition_states_table.c.version.desc())
        .limit(1)
    ).one_or_none()
    current_state = None if current is None else service._row_to_state_record(current)
    current_id = None if current is None else current.id
    if current_id != batch.prepared.expected_current_state_id:
        raise StaleComposeStateError("value publication changed its original current composition state")
    if batch.prepared.parent_composition_state_id != current_id:
        raise AuditIntegrityError("value publication changed its original parent composition state")
    locked_refusal = members.authority_revocation
    if any(row.composition_state_payload is not None for row in members.redacted_rows):
        if members.mutation_ceiling is None:
            raise AuditIntegrityError("value publication lost its actual graph mutation ceiling")
        members.mutation_ceiling.validate_context(context)
        current_refusal = check_auto_mutation_on_connection(conn, context=context, ceiling=members.mutation_ceiling)
        if locked_refusal is None:
            locked_refusal = current_refusal
    shape = value_batch_publication_shape(batch, locked_refusal=locked_refusal, env_ref_names=batch.inventory.names)
    if issued_preview is not None and shape.state_count != 0:
        raise AuditIntegrityError("issued preview publication cannot retire a review through authored state")
    base = service._reserve_sequence_range(conn, sid, count=shape.message_count)
    return plan_value_batch_publication(
        batch=batch,
        transitions=transitions,
        context=context,
        current_state=current_state,
        created_at=created_at,
        base_sequence=base,
        message_ids=tuple(uuid4() for _ in range(shape.message_count)) if message_ids is None else message_ids,
        state_ids=tuple(uuid4() for _ in range(shape.state_count)) if state_ids is None else state_ids,
        rejection_ids=tuple(uuid4() for _ in range(shape.rejection_count)) if rejection_ids is None else rejection_ids,
        locked_refusal=locked_refusal,
        env_ref_names=batch.inventory.names,
    )


def consume_value_batch_publication_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    service: SessionServiceImpl,
    publication: PreparedValueBatchPublication,
    commit: ContinuationOwnerCommit,
    publication_append: PreparedContinuationAppend,
    issued_preview: RequiredPreviewProduced | None = None,
) -> AuditOutcome:
    """Insert exact preplanned rows inside original P4 and complete-tail guards."""
    from elspeth.web.sessions.continuation_value_batch_publication import PreparedValueBatchPublication

    if type(publication) is not PreparedValueBatchPublication or commit.running is not running:
        raise AuditIntegrityError("value publication changed its exact P4 SQL owner")
    publication.require_original()
    batch, context = publication.batch, running.session_operation_context
    prepared, names = batch.prepared, batch.inventory.names
    members = prepared_p4_members(prepared, env_ref_names=names)
    sid = str(running.claim.session_id)
    service._assert_session_write_lock_held(conn, sid, caller="consume_value_batch_publication_on_connection")
    if not require_prepared_tail_append(conn, session_id=sid, operation_id=running.claim.operation_id, append=publication_append):
        raise AuditIntegrityError("value publication has no complete pre-issue tail reservation")
    with (
        selected_p4_drain_scope(conn, commit, prepared, env_ref_names=names),
        service._session_composer_mutation_transaction(
            conn,
            session_id=sid,
            session_operation_context=context,
            expected_kind=SessionOperationKind.COMPOSE,
        ),
    ):
        require_selected_p4_input(
            conn,
            commit,
            prepared=prepared,
            context=context,
            assistant_content=prepared.assistant_content,
            raw_content=prepared.raw_content,
            redacted_assistant_tool_calls=members.redacted_calls,
            redacted_tool_rows=members.redacted_rows,
            revoked_tool_rows=members.revoked_rows,
            mutation_ceiling=members.mutation_ceiling,
            authority_revocation=members.authority_revocation,
            private_tool_evidence=members.private_tool_evidence,
            private_tool_failures=members.private_tool_failures,
            rejection_records=members.rejection_records,
            parent_composition_state_id=prepared.parent_composition_state_id,
            expected_current_state_id=prepared.expected_current_state_id,
            plugin_crash_pending=prepared.crash_pending,
            env_ref_names=names,
        )
        expected = plan_value_batch_publication_on_connection(
            conn,
            running,
            service=service,
            batch=batch,
            transitions=publication.transitions,
            created_at=publication.created_at,
            message_ids=tuple(row.id for row in publication.messages),
            state_ids=tuple(row.id for row in publication.states),
            rejection_ids=publication.rejection_ids,
            issued_preview=issued_preview,
        )
        if expected != publication:
            raise AuditIntegrityError("value publication changed its original complete row plan")
        state_inputs = dict(zip((state.id for state in publication.states), publication.state_inputs, strict=True))
        inserted: list[UUID] = []
        for message in publication.messages:
            if message.composition_state_id in state_inputs and message.composition_state_id not in inserted:
                state = publication.states[len(inserted)]
                if state.id != message.composition_state_id:
                    raise AuditIntegrityError("value publication changed its actual graph checkpoint order")
                service._insert_composition_checkpoint(
                    conn,
                    session_id=sid,
                    state=state_inputs[state.id],
                    provenance="tool_call",
                    created_at=publication.created_at,
                    session_operation_context=context,
                    state_id=state.id,
                    derived_from_state_id=None if state.derived_from_state_id is None else str(state.derived_from_state_id),
                )
                actual = conn.execute(
                    select(composition_states_table).where(
                        composition_states_table.c.id == str(state.id), composition_states_table.c.session_id == sid
                    )
                ).one()
                if service._row_to_state_record(actual) != state or actual.creator_operation_id != publication.creator_operation_id:
                    raise AuditIntegrityError("value publication inserted a changed original checkpoint")
                inserted.append(state.id)
            if message.sequence_no is None:
                raise AuditIntegrityError("value publication lost its exact preplanned message sequence")
            service._insert_chat_message(
                conn,
                session_id=sid,
                role=message.role,
                content=message.content,
                raw_content=message.raw_content,
                tool_calls=deep_thaw(message.tool_calls),
                sequence_no=message.sequence_no,
                writer_principal=message.writer_principal,
                composition_state_id=None if message.composition_state_id is None else str(message.composition_state_id),
                tool_call_id=message.tool_call_id,
                parent_assistant_id=None if message.parent_assistant_id is None else str(message.parent_assistant_id),
                created_at=message.created_at,
                session_operation_context=context,
                message_id=str(message.id),
            )
        if tuple(inserted) != tuple(state.id for state in publication.states):
            raise AuditIntegrityError("value publication omitted an original checkpoint")
        for rejection in publication.rejections:
            record = rejection.record
            with service._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:
                mutations.record_composition_rejection(
                    rejection_id=rejection.id,
                    tool_call_id=record.tool_call_id,
                    tool_name=record.tool_name,
                    error_code=record.error_code,
                    message=record.message,
                    planner_payload=record.planner_payload,
                    composition_state_id=rejection.composition_state_id,
                    created_at=rejection.created_at,
                )
        rows = conn.execute(
            select(chat_messages_table)
            .where(
                chat_messages_table.c.session_id == sid,
                chat_messages_table.c.sequence_no >= publication.base_sequence,
                chat_messages_table.c.sequence_no < publication.base_sequence + len(publication.messages),
            )
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
        if tuple(chat_message_record_from_row(row) for row in rows) != publication.messages:
            raise AuditIntegrityError("value publication changed its original complete transcript")
    checkpoint = (
        None
        if not publication.states
        else CommittedCheckpointWitness(
            state=publication.states[-1],
            creator_operation_id=publication.creator_operation_id,
            inserted_state_ids=tuple(inserted),
        )
    )
    outcome = AuditOutcome(
        assistant_id=str(publication.messages[0].id),
        unwind_audit_failed=False,
        current_state_id=publication.retained_outcome.current_state_id,
        checkpoint_disposition=publication.retained_outcome.checkpoint_disposition,
        checkpoint=checkpoint,
        authority_revocation=publication.effective_refusal,
        published_tool_rows=publication.published_rows,
        published_assistant_content=publication.published_assistant_content,
    )
    if encode_audit_outcome_data(outcome, prepared=prepared, env_ref_names=names) != canonical_json(
        publication.retained_outcome.model_dump(mode="json")
    ):
        raise AuditIntegrityError("value publication changed its complete preplanned P4 outcome")
    return outcome
