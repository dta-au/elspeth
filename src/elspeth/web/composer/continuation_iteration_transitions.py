"""SQL owners for the Continuation compose iteration after an applied batch (U-40).

After an issued batch's P4 publication is applied, the shared loop runs its
post-batch phase (early Advisor checkpoint, anti-anchor hint and turn
accounting) and then asks the provider again. These transitions make that
boundary durable. They author no message, graph or proposal: the next action
is always the provider's next PRIMARY call.

* ``iteration_review_dispatched``: the post-batch phase may make Advisor
  provider calls, so one ``advance:<batch response digest>`` effect is
  recorded ``dispatched`` before it runs. A lost owner settles it as unknown
  rather than re-running those calls.
* ``iteration_advanced``: the applied ToolBatch is retired and the Compose
  frame returns to ``action_ready`` for the next PRIMARY call, carrying the
  captured live counters and context. The ``advance`` effect is committed in
  the same transaction.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments

ITERATION_EFFECT_OWNER = "composer_iteration"
APPLIED_BATCH_CODEC = "composer.applied-p4-batch.v2"

type IterationTransition = Literal["iteration_review_dispatched", "iteration_advanced"]


def advance_effect_key(response: PrivateChunkRef) -> str:
    """The one post-batch phase effect of one applied batch's paid response."""
    if type(response) is not PrivateChunkRef or response.codec != "composer.response.v1":
        raise AuditIntegrityError("post-batch effect requires its actual paid PRIMARY response")
    return f"advance:{response.digest}"


def applied_batch_cut(frames: tuple[object, ...]) -> tuple[ComposeFrame, ToolBatchFrame]:
    """The exact applied-batch cut: one Compose under one settled ToolBatch."""
    if len(frames) != 2:
        raise AuditIntegrityError("post-batch phase requires its exact Compose/ToolBatch pair")
    compose, batch = frames
    if (
        type(compose) is not ComposeFrame
        or type(batch) is not ToolBatchFrame
        or batch.phase != "result_retained"
        or batch.accumulator.codec != APPLIED_BATCH_CODEC
        or f"batch:{batch.response.digest}:p4" not in batch.settled_effect_ids
        or compose.pending_response != batch.response
    ):
        raise AuditIntegrityError("post-batch phase requires an applied P4 batch of its own paid response")
    return compose, batch


def retain_iteration_transition(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    transition: IterationTransition,
    captured: CapturedContinuationRuntime | None = None,
) -> StoredContinuationSnapshot:
    if transition not in ("iteration_review_dispatched", "iteration_advanced"):
        raise ValueError("iteration transition has no implemented source owner")
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("iteration transition lost its actual current source/claim owners")
    if (transition == "iteration_advanced") is (captured is None):
        raise AuditIntegrityError("only the iteration advance retains captured live owners")
    before = selected.stack
    sid, operation_id = before.logical.session_id, before.logical.operation_id
    compose, batch = applied_batch_cut(before.frames)
    key = advance_effect_key(batch.response)
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        _require_current_continuation_running(conn, running)
        require_current_env_inventory_on_connection(
            conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=before.cursor
        )
        stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
        if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(before):
            raise AuditIntegrityError("iteration transition replaced its actual selected predecessor")
        if before.pending_owner_results or before.pending_settlement_ids or before.active_action_json is not None:
            raise AuditIntegrityError("iteration transition requires a quiescent applied cut")
        policy = decode_continuation_policy(before.admission_policy_json, before.policy_hash)
        if transition == "iteration_review_dispatched":
            if any(item.disposition not in ("committed", "refused") for item in before.effect_frontier):
                raise AuditIntegrityError("post-batch phase cannot start over an open effect")
            if any(item.effect_id == key for item in before.effect_frontier):
                raise AuditIntegrityError("post-batch phase already recorded its single effect")
            input_sha256 = stable_hash({"schema": "composer.post-batch.v1", "batch": batch.accumulator.digest})
            conn.execute(
                insert(effects).values(
                    session_id=sid,
                    operation_id=operation_id,
                    effect_key=key,
                    input_sha256=input_sha256,
                    owner=ITERATION_EFFECT_OWNER,
                    disposition="dispatched",
                    creator_sequence=running.claim.segment_sequence,
                    creator_generation=running.claim.ownership_generation,
                )
            )
            after = before.model_copy(
                update={
                    "effect_frontier": (
                        *before.effect_frontier,
                        EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),
                    )
                }
            )
            event = ContinuationEvent(
                "may_dispatch",
                canonical_json(
                    {"owner": ITERATION_EFFECT_OWNER, "transition": transition, "effect_key": key, "input_sha256": input_sha256}
                ),
            )
            next_action = policy.actions.advisor
        else:
            if captured is None or captured.selected is not selected:
                raise AuditIntegrityError("iteration advance changed its actual selected capture")
            open_effects = tuple(item for item in before.effect_frontier if item.disposition not in ("committed", "refused"))
            if open_effects != (EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),):
                raise AuditIntegrityError("iteration advance requires exactly its own dispatched post-batch effect")
            captured_compose, captured_batch = applied_batch_cut(captured.stack.frames)
            if (
                captured.stack.model_copy(update={"frames": before.frames}) != before
                or captured_batch.model_copy(update={"accumulator": batch.accumulator}) != batch
                or captured_batch.accumulator.codec != batch.accumulator.codec
                or captured_compose.initial_version != compose.initial_version
                or captured_compose.slice_sequence != compose.slice_sequence
                or captured_compose.composition_slice < compose.composition_slice
                or captured_compose.discovery_slice < compose.discovery_slice
                or captured_compose.composition_total < compose.composition_total
                or captured_compose.discovery_total < compose.discovery_total
                or captured_compose.repair_turns < compose.repair_turns
                or captured_compose.advisor_checkpoint_passes < compose.advisor_checkpoint_passes
            ):
                raise AuditIntegrityError("iteration advance changed its immutable request, batch or counters")
            for chunk in captured.chunks:
                put_chunk(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                    key=private_chunk_key(chunk),
                    payload=chunk.canonical.encode("utf-8"),
                    codec=chunk.reference.codec,
                )
            # A completed batch may have a new graph chunk after capture.
            # Reprove that exact full graph and every original P4 application
            # before retiring its current frame; the old chunk stays intact.
            from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
            from elspeth.web.composer.continuation_runtime import _selected_chunks
            from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

            captured_selected = SelectedContinuationState(
                selected.checkpoint_id, captured.stack, _selected_chunks(captured.stack, captured.chunks, selected.chunks)
            )
            admitted = read_selected_turn_evidence_graph_on_connection(conn, running, selected=captured_selected, names=inventory.names)
            require_p4_application_journal_on_connection(
                conn, running, selected=captured_selected, evidence=admitted, env_ref_names=inventory.names
            )
            following = captured_compose.model_copy(
                update={"phase": "action_ready", "pending_response": None, "provider_effect_key": None, "provider_input_sha256": None}
            )
            result_key = f"value:{following.iteration.codec}:{following.iteration.digest}"
            receipt = f"iteration:{following.iteration.digest}"
            changed = conn.execute(
                update(effects)
                .where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == operation_id,
                    effects.c.effect_key == key,
                    effects.c.owner == ITERATION_EFFECT_OWNER,
                    effects.c.disposition == "dispatched",
                    effects.c.creator_sequence == running.claim.segment_sequence,
                    effects.c.creator_generation == running.claim.ownership_generation,
                )
                .values(disposition="committed", receipt_key=receipt, result_chunk_key=result_key)
            )
            if changed.rowcount != 1:
                raise AuditIntegrityError("iteration advance lost its exact dispatched post-batch effect")
            after = captured.stack.model_copy(
                update={
                    "frames": (following,),
                    "effect_frontier": tuple(
                        EffectFrontierEntry(effect_id=item.effect_id, disposition="committed", receipt_id=receipt)
                        if item.effect_id == key
                        else item
                        for item in before.effect_frontier
                    ),
                }
            )
            event = ContinuationEvent(
                "checkpoint",
                canonical_json(
                    {
                        "owner": ITERATION_EFFECT_OWNER,
                        "transition": transition,
                        "effect_key": key,
                        "batch": batch.accumulator.digest,
                        "iteration": following.iteration.digest,
                    }
                ),
            )
            next_action = policy.actions.primary
        conn.execute(
            update(segments)
            .where(
                segments.c.session_id == sid,
                segments.c.operation_id == operation_id,
                segments.c.sequence == running.claim.segment_sequence,
                segments.c.generation == running.claim.ownership_generation,
            )
            .values(next_action_json=next_action.model_dump_json())
        )
        retained = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id,
            expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(after),
            event_batch=(event,),
            handoff=False,
        )
        if decode_continuation_snapshot(retained.payload_json).frames != after.frames:
            raise AuditIntegrityError("iteration transition lost its retained frames")
        if captured is not None:
            from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
            from elspeth.web.composer.continuation_runtime import _selected_chunks

            root = decode_continuation_snapshot(retained.payload_json)
            current = SelectedContinuationState(retained.checkpoint_id, root, _selected_chunks(root, captured.chunks, ()))
            read_selected_turn_evidence_graph_on_connection(conn, running, selected=current, names=inventory.names)
        return retained
