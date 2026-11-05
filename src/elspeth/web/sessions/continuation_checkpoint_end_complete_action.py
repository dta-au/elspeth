"""Source-bound preview of an END semantic action receipt before paid commit."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, EndFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_end_effects import end_effect_key
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt,
    continuation_action_key,
    decode_continuation_frontier,
    decode_prescribed_continuation_action,
    encode_continuation_frontier,
    record_action_receipt,
)
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, retain_action_execution_receipt_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair_history import (
    VerifiedEndCompleteRepairHistory,
    read_original_end_complete_repair_history_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    VerifiedCheckpointPaidOverflowTerminal,
    read_v4_checkpoint_end_owed_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.continuation_semantic_actions import (
    SemanticActionAdmission,
    installed_action_json,
    read_original_dispatched_semantic_review_after_end_overflow_on_connection,
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


def require_original_end_outer_creator_on_connection(
    conn: Connection, *, owed: VerifiedCheckpointEndOwed
) -> tuple[int, int, SemanticActionAdmission | None]:
    """Bind the historical D effect/creator separately from the current writer."""
    return _require_original_end_outer_creator_on_connection(conn, owed=owed, terminal=None, terminal_names=None, committed_repair=None)


def require_original_end_outer_creator_after_complete_repair_on_connection(
    conn: Connection, *, owed: VerifiedCheckpointEndOwed, names: frozenset[str]
) -> tuple[int, int, SemanticActionAdmission | None]:
    """Admit historical committed D only after its complete durable repair."""
    history = read_original_end_complete_repair_history_on_connection(conn, owed=owed, names=names)
    return _require_original_end_outer_creator_on_connection(conn, owed=owed, terminal=None, terminal_names=None, committed_repair=history)


def require_original_end_outer_creator_after_paid_overflow_on_connection(
    conn: Connection, *, owed: VerifiedCheckpointEndOwed, names: frozenset[str]
) -> tuple[int, int, SemanticActionAdmission | None]:
    """Prove original D after its outer effect became terminal-unknown."""
    terminal = read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
        conn,
        session_id=UUID(owed.session_id),
        operation_id=owed.operation_id,
        effect_key=owed.effect_key,
        names=names,
    )
    if (
        type(terminal) is not VerifiedCheckpointPaidOverflowTerminal
        or terminal.source != owed.new_hold
        or terminal.provider_attempt_id != owed.attempt_id
        or terminal.outer_effect_key != owed.outer_effect_key
        or terminal.hold_state != "consumed"
        or terminal.phase != "end"
        or terminal.receipt.representation != "retained_tail"
    ):
        raise AuditIntegrityError("END outer creator lacks its exact paid-overflow terminal")
    return _require_original_end_outer_creator_on_connection(
        conn, owed=owed, terminal=terminal, terminal_names=names, committed_repair=None
    )


def _require_original_end_outer_creator_on_connection(
    conn: Connection,
    *,
    owed: VerifiedCheckpointEndOwed,
    terminal: VerifiedCheckpointPaidOverflowTerminal | None,
    terminal_names: frozenset[str] | None,
    committed_repair: VerifiedEndCompleteRepairHistory | None,
) -> tuple[int, int, SemanticActionAdmission | None]:
    """Reprove immutable D lineage and its exact live or terminal SQL effect."""
    if (
        type(owed) is not VerifiedCheckpointEndOwed
        or owed.phase_source is None
        or owed.prepared_selected is None
        or owed.end_input is None
        or (
            committed_repair is not None
            and (type(committed_repair) is not VerifiedEndCompleteRepairHistory or committed_repair.owed != owed)
        )
        or (terminal is not None and committed_repair is not None)
    ):
        raise AuditIntegrityError("END outer creator lacks its original semantic dispatch D")
    sid, operation = owed.session_id, owed.operation_id
    original_owed = owed.retry_history.owed if owed.retry_history is not None else owed
    if (
        original_owed.phase_source is None
        or original_owed.prepared_selected is None
        or original_owed.end_input is None
        or original_owed.retry_ordinal != 0
        or original_owed.retry_history is not None
        or (owed.retry_history is None and owed.retry_ordinal != 0)
        or (
            owed.retry_history is not None
            and (
                owed.retry_ordinal != 1
                or original_owed.selected.checkpoint_id != owed.retry_history.marker.owed_checkpoint_id
                or original_owed.selected.cursor != owed.retry_history.marker.owed_cursor
            )
        )
        or original_owed.phase_source != owed.phase_source
        or original_owed.outer_effect_key != owed.outer_effect_key
        or original_owed.session_id != sid
        or original_owed.operation_id != operation
    ):
        raise AuditIntegrityError("END outer creator lost its original first paid source")
    original = decode_continuation_snapshot(original_owed.phase_source.payload_json)
    prepared = decode_continuation_snapshot(original_owed.prepared_selected.payload_json)
    dispatched = decode_continuation_snapshot(original_owed.original_selected.payload_json)
    retained = decode_continuation_snapshot(original_owed.selected.payload_json)
    outer_entries = tuple(item for item in original.effect_frontier if item.effect_id == owed.outer_effect_key)
    if (
        len(original.frames) != 2
        # Actual stored D→P capture changes only Compose's cache, evidence,
        # iteration, messages and transcript refs; P→A adds the physical
        # child/frontier, and A→H advances the cursor. The deep H reader
        # verifies the captured refs and every intervening event separately.
        or prepared.model_copy(update={"frames": original.frames, "cursor": original.cursor}) != original
        or prepared.frames != dispatched.frames
        or dispatched.frames != retained.frames
        or original_owed.end_input.original_checkpoint_id != original_owed.phase_source.checkpoint_id
        or len(outer_entries) != 1
        or outer_entries[0].disposition != "dispatched"
        or outer_entries[0].receipt_id is not None
    ):
        raise AuditIntegrityError("END outer creator changed its original dispatched D root")
    compose, end = original.frames
    if type(compose) is not ComposeFrame or type(end) is not EndFrame or end.phase != "review_needed":
        raise AuditIntegrityError("END outer creator changed its original Review frame")
    prepared_compose, prepared_end = prepared.frames
    if (
        type(prepared_compose) is not ComposeFrame
        or type(prepared_end) is not EndFrame
        or prepared_end != end
        or prepared_compose.pending_response != compose.pending_response
        or prepared_compose.provider_effect_key != compose.provider_effect_key
        or prepared_compose.provider_input_sha256 != compose.provider_input_sha256
    ):
        raise AuditIntegrityError("END outer creator changed its original paid candidate across capture")
    admission: SemanticActionAdmission | None = None
    if original.active_action_json is None:
        if (
            original.active_action_source is not None
            or compose.pending_response is None
            or end_effect_key(compose.pending_response) != owed.outer_effect_key
        ):
            raise AuditIntegrityError("END outer creator changed its original paid PRIMARY identity")
    else:
        semantic_source = original.active_action_source
        if semantic_source is None:
            raise AuditIntegrityError("END outer creator changed its original paid semantic Review")
        if terminal is None:
            admission = read_original_dispatched_semantic_review_on_connection(
                conn,
                session_id=UUID(sid),
                operation_id=operation,
                dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
                semantic_effect_key=semantic_source.effect_key,
            )
        else:
            if terminal_names is None:
                raise AuditIntegrityError("END outer creator lost its terminal credential inventory")
            admission = read_original_dispatched_semantic_review_after_end_overflow_on_connection(
                conn,
                session_id=UUID(sid),
                operation_id=operation,
                dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
                semantic_effect_key=semantic_source.effect_key,
                names=terminal_names,
                terminal=terminal,
            )
        if admission.owner_effect_key != owed.outer_effect_key or installed_action_json(admission) != original.active_action_json:
            raise AuditIntegrityError("END outer creator changed its original paid semantic Review")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == owed.outer_effect_key
        )
    ).one_or_none()
    expected_input = stable_hash({"schema": "composer.end-review.v1", "candidate": end.candidate.digest})
    expected_receipt: str | None = None
    expected_result_chunk_key: str | None = None
    if committed_repair is not None:
        repaired = decode_continuation_snapshot(committed_repair.successor.payload_json)
        if len(repaired.frames) != 1 or type(repaired.frames[0]) is not ComposeFrame:
            raise AuditIntegrityError("END outer creator lost its original committed repair frame")
        iteration = repaired.frames[0].iteration
        expected_receipt = f"repair:{iteration.digest}"
        expected_result_chunk_key = f"value:{iteration.codec}:{iteration.digest}"
        if committed_repair.outer_receipt != expected_receipt:
            raise AuditIntegrityError("END outer creator changed its original committed repair receipt")
    if (
        effect is None
        or effect.owner != "composer_end"
        or effect.disposition != ("unknown" if terminal is not None else "committed" if committed_repair is not None else "dispatched")
        or effect.input_sha256 != expected_input
        or effect.action_json != original.active_action_json
        or effect.receipt_key != expected_receipt
        or effect.result_chunk_key != expected_result_chunk_key
        or effect.provider_attempt_id is not None
    ):
        raise AuditIntegrityError("END outer creator lost its original dispatched effect")
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
        or creator.session_id != sid
        or creator.operation_id != operation
        or (creator.sequence, creator.generation) != (effect.creator_sequence, effect.creator_generation)
        or type(creator.session_operation_epoch) is not int
        or creator.session_operation_epoch < 1
        or type(creator.claim_attempt) is not int
        or creator.claim_attempt < 1
    ):
        raise AuditIntegrityError("END outer creator lost its original immutable attempt")
    return effect.creator_sequence, effect.creator_generation, admission


def prepare_end_complete_action_preview_on_connection(
    conn: Connection,
    *,
    prepared: PreparedCheckpointProvider,
    owed: VerifiedCheckpointEndOwed,
    after_base: ContinuationFrameStack,
) -> EndCompleteActionPreview | None:
    """Prove the original Review action and encode its committed receipt."""
    if type(prepared) is not PreparedCheckpointProvider or owed.phase_source is None:
        raise AuditIntegrityError("END action preview lost its original warm provider source")
    if prepared.retry_ordinal == 0:
        if owed.retry_history is not None or owed.phase_source != prepared.prepared_source.source:
            raise AuditIntegrityError("END first action preview changed its original semantic dispatch")
    elif prepared.retry_ordinal == 1:
        if owed.retry_history is None or prepared.prepared_source.source != owed.retry_history.selected:
            raise AuditIntegrityError("END retry action preview lost its original marker source")
    else:
        raise AuditIntegrityError("END action preview exceeded its bounded physical retry")
    return _prepare_end_complete_action_preview_on_connection(
        conn,
        running=prepared.running,
        dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
        owed=owed,
        after_base=after_base,
    )


def prepare_cold_end_complete_action_preview_on_connection(
    conn: Connection,
    *,
    running: ComposerOperationRunning,
    owed: VerifiedCheckpointEndOwed,
    after_base: ContinuationFrameStack,
) -> EndCompleteActionPreview | None:
    """Use deep-H-proved D without reconstructing a warm provider owner."""
    if type(owed) is not VerifiedCheckpointEndOwed or owed.phase_source is None:
        raise AuditIntegrityError("cold END action preview lacks its original phase dispatch")
    return _prepare_end_complete_action_preview_on_connection(
        conn,
        running=running,
        dispatch_checkpoint_id=owed.phase_source.checkpoint_id,
        owed=owed,
        after_base=after_base,
    )


def _prepare_end_complete_action_preview_on_connection(
    conn: Connection,
    *,
    running: ComposerOperationRunning,
    dispatch_checkpoint_id: str,
    owed: VerifiedCheckpointEndOwed,
    after_base: ContinuationFrameStack,
) -> EndCompleteActionPreview | None:
    before = decode_continuation_snapshot(owed.selected.payload_json)
    if (
        type(running) is not ComposerOperationRunning
        or type(owed) is not VerifiedCheckpointEndOwed
        or type(after_base) is not ContinuationFrameStack
        or (str(running.claim.session_id), running.claim.operation_id) != (owed.session_id, owed.operation_id)
        or type(dispatch_checkpoint_id) is not str
        or not dispatch_checkpoint_id
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
    # D's outer effect belongs to its historical creator. A replacement owner
    # fences this write with its current claim, without becoming that creator.
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if owed.phase_source is None or owed.phase_source.checkpoint_id != dispatch_checkpoint_id:
        raise AuditIntegrityError("END action preview substituted its original semantic dispatch D")
    _creator_sequence, _creator_generation, admission = require_original_end_outer_creator_on_connection(conn, owed=owed)
    if (
        admission is None
        or admission.action_key is None
        or admission.paid.source != before.active_action_source
        or installed_action_json(admission) != before.active_action_json
    ):
        raise AuditIntegrityError("END action preview changed its original dispatched Review")
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
