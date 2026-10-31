"""Ordinary selected-current read and complete pre-P4 evidence graph restore.

This facet never uses owed-result cancellation/expiry allowances. DATA carries
historical physical identity; only this actual SQL call supplies current context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_snapshot, verify_private_chunk
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, decode_turn_evidence, require_admitted_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence, _restore_current_evidence
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.converters import state_from_record, state_record_from_row
from elspeth.web.sessions.models import (
    chat_messages_table as messages,
)
from elspeth.web.sessions.models import (
    composer_async_operations_table as jobs,
)
from elspeth.web.sessions.models import (
    composer_segment_attempts_table as attempts,
)
from elspeth.web.sessions.models import (
    composition_states_table as states,
)
from elspeth.web.sessions.models import (
    sessions_table as sessions,
)
from elspeth.web.sessions.transcript_reads import validate_transcript_private_custody

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


def read_turn_evidence_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> AdmittedTurnEvidence:
    if type(selected) is not SelectedContinuationState or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("cold evidence requires its actual selected/current owners")
    selected.__post_init__()
    names = require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
    )
    if inventory.selection.snapshot_json != encode_continuation_snapshot(selected.stack):
        raise AuditIntegrityError("cold evidence changed the complete selected root")
    root = selected.stack
    if root.pending_owner_results:
        raise AuditIntegrityError("cold pre-P4 evidence cannot consume a pending owner result")
    snapshot = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=names)
    if snapshot.snapshot.application_entry is not None:
        from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

        require_p4_application_journal_on_connection(conn, running, selected=selected, evidence=snapshot, env_ref_names=names)
    return snapshot


def read_selected_turn_evidence_graph_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    names: frozenset[str],
) -> AdmittedTurnEvidence:
    """Validate selected evidence DATA after the caller proves current admission."""
    principal = conn.execute(select(sessions.c.archived_at).where(sessions.c.id == str(running.claim.session_id))).one()
    if principal.archived_at is not None:
        raise AuditIntegrityError("cold evidence changed its principal or committed ingress")
    return read_selected_turn_evidence_graph_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected,
        names=names,
    )


def read_selected_turn_evidence_graph_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    names: frozenset[str],
) -> AdmittedTurnEvidence:
    """Immutable graph/principal DATA after a separate locked admission proof.

    Archivedness is an admission concern: current readers refuse it in their
    wrapper; lost-owner settlement proves it in its own repository transaction.
    Every original graph, transcript, base, creator and application check stays.
    """
    root = selected.stack
    compose = tuple(frame for frame in root.frames if type(frame) is ComposeFrame)
    if len(compose) != 1:
        raise AuditIntegrityError("cold evidence requires exactly one actual Compose frame")
    frame = compose[0]
    if frame.initial_state.codec != "composer.state.v1" or frame.audit_buffer.codec != "composer.audit-buffer.v1":
        raise AuditIntegrityError("cold evidence changed its selected state/recorder format")
    sid = str(session_id)
    if (root.logical.session_id, root.logical.operation_id) != (sid, operation_id):
        raise AuditIntegrityError("cold evidence changed its logical SQL identity")

    def actual_chunk(reference: PrivateChunkRef) -> PrivateChunk:
        candidates = tuple(item for item in selected.chunks if item.reference == reference)
        if len(candidates) != 1:
            raise AuditIntegrityError("cold evidence lost its selected chunk")
        raw = read_chunk(conn, session_id=sid, operation_id=operation_id, key=f"value:{reference.codec}:{reference.digest}")
        if raw != candidates[0].canonical.encode("utf-8"):
            raise AuditIntegrityError("cold evidence storage differs from its selected bytes")
        verify_private_chunk(reference, candidates[0].canonical)
        return candidates[0]

    admitted = decode_turn_evidence(actual_chunk(frame.evidence), env_ref_names=names, source_chunks=selected.chunks)
    snapshot = admitted.snapshot
    if snapshot.logical != root.logical:
        raise AuditIntegrityError("cold evidence changed its complete logical binding")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at).where(sessions.c.id == sid)).one()
    if (
        parent.lifecycle_version != 2
        or parent.actor_user_id != snapshot.actor_user_id
        or principal.user_id != snapshot.actor_user_id
        or parent.request_hash != root.logical.request_hash
        or parent.kind != root.logical.kind
        or parent.base_state_id != root.logical.base_state_id
        or parent.user_message_id != snapshot.request_id
        or parent.ingress_phase != "committed"
    ):
        raise AuditIntegrityError("cold evidence changed its principal or committed ingress")
    validate_transcript_private_custody(conn, sid)
    ingress = conn.execute(select(messages).where(messages.c.session_id == sid, messages.c.id == snapshot.request_id)).one_or_none()
    if (
        ingress is None
        or ingress.role != "user"
        or ingress.writer_principal not in ("route_user_message", "session_fork")
        or ingress.content != snapshot.transcript.request
        or stable_hash(ingress.content) != snapshot.request_content_hash
    ):
        raise AuditIntegrityError("cold evidence changed its authenticated original request")
    original = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.session_operation_id == snapshot.physical_operation_id,
            attempts.c.session_operation_epoch == snapshot.physical_operation_epoch,
        )
    ).one_or_none()
    if original is None:
        raise AuditIntegrityError("cold evidence creator is outside its logical operation")
    retained = snapshot.effects
    if root.logical.base_state_id is None:
        initial = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    else:
        row = conn.execute(select(states).where(states.c.session_id == sid, states.c.id == root.logical.base_state_id)).one_or_none()
        if row is None:
            raise AuditIntegrityError("cold evidence lost its original same-session base")
        initial = state_from_record(state_record_from_row(row))
    if (
        canonical_json(initial.to_dict()) != retained.initial_state_json
        or actual_chunk(frame.initial_state).canonical != retained.initial_state_json
        or frame.initial_version != initial.version
        or frame.current_state_id != retained.latest_committed_state_id
    ):
        raise AuditIntegrityError("cold evidence changed its actual initial state/frontier")
    if (
        snapshot.recorder != frame.audit_buffer
        or actual_chunk(frame.audit_buffer) != admitted.recorder_chunk
        or actual_chunk(frame.transcript) != encode_message_shelf(snapshot.transcript)
        or frame.messages.codec != "composer.messages.v1"
        or actual_chunk(frame.messages).canonical
        != canonical_json(
            {
                "messages_json": snapshot.transcript.messages_json,
                "current_indexes": snapshot.transcript.current_indexes,
            }
        )
    ):
        raise AuditIntegrityError("cold evidence changed its selected recorder/transcript/messages graph")
    # Decode one actual shared graph; never substitute an equal second batch.
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_selected_batch_graph import require_selected_active_batch_graph

    for batch_frame in root.frames:
        if type(batch_frame) is ToolBatchFrame:
            actual_chunk(batch_frame.accumulator)
            actual_chunk(batch_frame.response)
    require_selected_active_batch_graph(selected, admitted, env_ref_names=names)
    from elspeth.web.sessions.continuation_attachment_application import require_attachment_applications_data_on_connection

    require_attachment_applications_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=selected, evidence=admitted, env_ref_names=names
    )
    return admitted


async def restore_turn_evidence(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    service: ComposerServiceImpl | None = None,
) -> RestoredTurnEvidence:
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(selected) is not SelectedContinuationState
        or type(required_work) is not RequiredWorkBinding
        or type(required_work.running) is not ComposerOperationRunning
    ):
        raise AuditIntegrityError("cold evidence requires actual current read custody")
    if service is not None:
        _require_current_reader_service(service, authority)
    running = required_work.running
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("cold evidence changed its required-work logical claim")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql, authority.read_continuation_turn_evidence, running, selected=selected, inventory=inventory
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    result: RestoredTurnEvidence | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not AdmittedTurnEvidence:
                raise AuditIntegrityError("cold evidence read replaced its owned DATA result")
            admitted = require_admitted_turn_evidence(outcome.value, env_ref_names=inventory.names)
            reader = None
            if admitted.snapshot.requires_blob_reader:
                if service is None:
                    raise AuditIntegrityError("cold evidence requires its actual current reader service")
                _require_current_reader_service(service, authority)
                from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader

                reader = CurrentTranscriptBlobReader.from_service(
                    service, session_id=str(running.claim.session_id), context=running.session_operation_context
                )
            result = _restore_current_evidence(
                admitted, context=running.session_operation_context, reader=reader, env_ref_names=inventory.names
            )
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("cold evidence read returned an unknown outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Cold evidence original outcomes", errors)
    if result is None:
        raise AuditIntegrityError("cold evidence lost its projected complete graph")
    return result


def _require_current_reader_service(service: ComposerServiceImpl, authority: ComposerAsyncOperationAuthority) -> None:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not ComposerServiceImpl or service._session_engine is not authority._engine:
        raise AuditIntegrityError("cold reader service and selected SQL require the same actual store")
    sessions = service._require_sessions_service()
    if not isinstance(sessions, SessionServiceImpl) or sessions._engine is not authority._engine:
        raise AuditIntegrityError("cold reader session authority belongs to another actual store")
