"""Source-bound preview of an END semantic action receipt before paid commit."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ContinuationFrameStack
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt,
    continuation_action_key,
    decode_continuation_frontier,
    decode_prescribed_continuation_action,
    encode_continuation_frontier,
    record_action_receipt,
)
from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.continuation_semantic_actions import (
    installed_action_json,
    read_original_dispatched_semantic_review_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


@dataclass(frozen=True, slots=True)
class EndCompleteActionPreview:
    """One exact action-frontier chunk and already-sized successor root."""

    effect_key: str
    action_json: str
    frontier: PrivateChunk
    successor_json: str


def prepare_end_complete_action_preview_on_connection(
    conn: Connection,
    *,
    prepared: PreparedCheckpointProvider,
    owed: VerifiedCheckpointEndOwed,
    after_base: ContinuationFrameStack,
) -> EndCompleteActionPreview | None:
    """Prove the original Review action and encode its committed receipt."""
    before = decode_continuation_snapshot(owed.selected.payload_json)
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(owed) is not VerifiedCheckpointEndOwed
        or type(after_base) is not ContinuationFrameStack
        or after_base.active_action_json != before.active_action_json
        or after_base.active_action_source != before.active_action_source
        or after_base.semantic_frontier != before.semantic_frontier
    ):
        raise AuditIntegrityError("END action preview changed the original owed action source")
    if before.active_action_json is None:
        if before.active_action_source is not None:
            raise AuditIntegrityError("END action preview lost its original action source")
        return None
    if before.active_action_source is None or before.semantic_frontier is None:
        raise AuditIntegrityError("END action preview lost its bound semantic frontier")
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    admission = read_original_dispatched_semantic_review_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation,
        dispatch_checkpoint_id=prepared.prepared_source.source.checkpoint_id,
        semantic_effect_key=owed.outer_effect_key,
    )
    if (
        admission.action_key is None
        or admission.paid.source != before.active_action_source
        or installed_action_json(admission) != before.active_action_json
    ):
        raise AuditIntegrityError("END action preview changed its original dispatched Review")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == owed.outer_effect_key
        )
    ).one_or_none()
    if effect is None:
        raise AuditIntegrityError("END action preview lost its original outer effect")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one_or_none()
    if (
        creator is None
        or creator.operation_id != operation
        or effect.owner != "composer_end"
        or effect.disposition != "dispatched"
        or effect.action_json != before.active_action_json
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or effect.receipt_key is not None
    ):
        raise AuditIntegrityError("END action preview lost its original dispatched effect/creator")
    ref = before.semantic_frontier
    chunks = load_private_chunks(conn, session_id=sid, operation_id=operation, stack=before)
    matching = tuple(chunk for chunk in chunks if chunk.reference == ref)
    if len(matching) != 1:
        raise AuditIntegrityError("END action preview lost its bounded semantic frontier")
    frontier = decode_continuation_frontier(matching[0].canonical)
    action = decode_prescribed_continuation_action(before.active_action_json)
    receipt = ActionExecutionReceipt(
        continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence}),
        owed.outer_effect_key,
        "committed",
    )
    updated = record_action_receipt(frontier, receipt, actual_owner_receipt_id=owed.outer_effect_key)
    chunk = encode_private_chunk(codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(updated))
    after = after_base.model_copy(update={"semantic_frontier": chunk.reference, "active_action_json": None, "active_action_source": None})
    return EndCompleteActionPreview(owed.outer_effect_key, before.active_action_json, chunk, encode_continuation_snapshot(after))


def consume_end_complete_action_preview_on_connection(
    conn: Connection,
    *,
    owed: VerifiedCheckpointEndOwed,
    after_base: ContinuationFrameStack,
    preview: EndCompleteActionPreview,
) -> str:
    """After outer CAS, let the old writer reproduce only the admitted bytes."""
    if (
        type(preview) is not EndCompleteActionPreview
        or preview.effect_key != owed.outer_effect_key
        or decode_continuation_snapshot(preview.successor_json).semantic_frontier != preview.frontier.reference
    ):
        raise AuditIntegrityError("END action consume changed its pre-admitted frontier")
    actual = retain_action_execution_receipt_on_connection(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        effect_key=owed.outer_effect_key,
        disposition="committed",
        snapshot_json=encode_continuation_snapshot(after_base),
    )
    if actual != preview.successor_json:
        raise AuditIntegrityError("END action receipt differs from its exact prewrite capacity preview")
    return actual
