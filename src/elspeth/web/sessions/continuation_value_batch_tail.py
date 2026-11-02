"""Reserve and consume the entire actual V4 value batch in one SQL transaction.

The caller owns the session write lock and transaction. No provider hold is
consumed here: the original PRIMARY result was already paid and committed.
Every issue/result, complete P4 input/result/application and final unpaid floor
is admitted before the first tool effect or public row is inserted.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _selected_chunks
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import PreparedContinuationAppend, StoredContinuationSnapshot, private_chunk_key
from elspeth.web.sessions.continuation_p4 import (
    planned_p4_chat_hash,
    planned_p4_rejection_hash,
    planned_p4_state_hash,
    prepare_p4_on_connection,
    prepare_p4_preparation_tail,
    prepare_p4_receipt_data,
    prepare_p4_result_tail,
    retain_p4_result_on_connection,
)
from elspeth.web.sessions.continuation_p4_application_store import (
    StoredP4Application,
    apply_committing_p4_application_on_connection,
    prepare_p4_application_tail,
)
from elspeth.web.sessions.continuation_prepared_tail import PreparedTailPrefix, admit_prepared_complete_tail_on_connection
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.continuation_value_batch_store import (
    consume_value_batch_publication_on_connection,
    plan_value_batch_publication_on_connection,
)
from elspeth.web.sessions.continuation_value_batch_transitions import (
    consume_value_batch_transitions_on_connection,
    prepare_value_batch_transitions_on_connection,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatch
    from elspeth.web.sessions.service import SessionServiceImpl


def _future(
    append: PreparedContinuationAppend, previous: SelectedContinuationState, chunks: tuple[PrivateChunk, ...]
) -> SelectedContinuationState:
    root = decode_continuation_snapshot(append.snapshot_json)
    return SelectedContinuationState(append.checkpoint_id, root, _selected_chunks(root, previous.chunks, chunks))


def _prefix(append: PreparedContinuationAppend, chunks: tuple[PrivateChunk, ...]) -> PreparedTailPrefix:
    return PreparedTailPrefix(
        append,
        tuple(
            ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
            for chunk in chunks
        ),
    )


def retain_value_batch_tail_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    service: SessionServiceImpl,
    batch: PreparedValueBatch,
    published_at: datetime,
) -> StoredP4Application:
    """Plan all finite prefixes, then commit only the exact original batch.

    This returns an in-transaction value. The service must commit and separately
    cold reread before treating it as delivered or a physical SQL completion.
    """
    from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_value_batch

    require_prepared_value_batch(batch)
    names, selected = batch.inventory.names, batch.selected
    service._assert_session_write_lock_held(conn, str(running.claim.session_id), caller="retain_value_batch_tail_on_connection")
    transitions = prepare_value_batch_transitions_on_connection(conn, running, prepared=batch, published_at=published_at)
    pending_root = transitions.completed.stack.model_copy(
        update={"frames": (batch.pending_capture.stack.frames[0], *transitions.completed.stack.frames[1:])}
    )
    captured = CapturedContinuationRuntime(
        transitions.completed, pending_root, _selected_chunks(pending_root, transitions.completed.chunks, batch.pending_capture.chunks)
    )
    preparation = prepare_p4_preparation_tail(
        running,
        selected=transitions.completed,
        pending_batch=batch.pending_batch,
        captured=captured,
        published_at=published_at,
        env_ref_names=names,
    )
    publication = plan_value_batch_publication_on_connection(
        conn, running, service=service, batch=batch, transitions=transitions, created_at=published_at
    )
    p4_selected = _future(preparation.append, transitions.completed, preparation.chunks)
    ordered_rejections = tuple(sorted(publication.rejections, key=lambda rejection: str(rejection.id)))
    receipt = prepare_p4_receipt_data(
        effect_key=preparation.effect_key,
        input_sha256=preparation.input_sha256,
        prepared=batch.prepared,
        outcome=publication.retained_outcome,
        chat_message_ids=tuple(message.id for message in publication.messages),
        chat_message_hashes=tuple(planned_p4_chat_hash(message) for message in publication.messages),
        state_row_hashes=tuple(
            planned_p4_state_hash(state, creator_operation_id=publication.creator_operation_id) for state in publication.states
        ),
        rejection_ids=tuple(str(rejection.id) for rejection in ordered_rejections),
        rejection_hashes=tuple(
            planned_p4_rejection_hash(
                rejection_id=str(rejection.id),
                session_id=str(running.claim.session_id),
                composition_state_id=rejection.composition_state_id,
                tool_call_id=rejection.record.tool_call_id,
                tool_name=rejection.record.tool_name,
                error_code=rejection.record.error_code,
                message=rejection.record.message,
                planner_payload=rejection.record.planner_payload,
                created_at=rejection.created_at,
            )
            for rejection in ordered_rejections
        ),
        env_ref_names=names,
    )
    result = prepare_p4_result_tail(selected=p4_selected, receipt=receipt, published_at=published_at, env_ref_names=names)
    result_selected = _future(result.append, p4_selected, (result.chunk,))
    compose = result_selected.stack.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("value-batch P4 application lost its original compose source")
    evidence_chunk = next(chunk for chunk in result_selected.chunks if chunk.reference == compose.evidence)
    evidence = decode_turn_evidence(evidence_chunk, env_ref_names=names, source_chunks=result_selected.chunks)
    application = prepare_p4_application_tail(
        running,
        selected=result_selected,
        evidence=evidence,
        pending_batch=batch.pending_batch,
        result=result.result,
        result_chunk=result.chunk,
        published_at=published_at,
        env_ref_names=names,
    )
    original = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    prefixes = (
        *transitions.prefixes,
        _prefix(preparation.append, preparation.chunks),
        _prefix(result.append, (result.chunk,)),
        _prefix(application.append, application.chunks),
    )
    with admit_prepared_complete_tail_on_connection(conn, original=original, prefixes=prefixes):
        consume_value_batch_transitions_on_connection(conn, running, plan=transitions, env_ref_names=names)
        commit = prepare_p4_on_connection(
            conn,
            running,
            expected_checkpoint_id=transitions.completed.checkpoint_id,
            expected_cursor=transitions.completed.stack.cursor,
            pending_batch=batch.pending_batch,
            captured=captured,
            env_ref_names=names,
            prepared_tail=preparation,
        )
        outcome = consume_value_batch_publication_on_connection(
            conn, running, service=service, publication=publication, commit=commit, publication_append=result.append
        )
        retain_p4_result_on_connection(
            conn,
            commit,
            prepared=batch.prepared,
            outcome=outcome,
            base_sequence=publication.base_sequence,
            created_at=publication.created_at,
            env_ref_names=names,
            prepared_tail=result,
        )
        stored = apply_committing_p4_application_on_connection(conn, commit, env_ref_names=names, prepared_tail=application)
        if type(stored) is not StoredP4Application:
            raise AuditIntegrityError("value batch lost its exact actual P4 application")
    return stored
