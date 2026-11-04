"""SQL owners for the Continuation END phase (U-40).

END begins after an actual, already committed PRIMARY response that issued no
tool calls, or a sole provider-authored recovery control. Its candidate is the
provider's own words; nothing here authors a reply, a graph or a proposal. Each transition is one locked session
transaction that re-proves the current owner, the selected predecessor and the
paid response before it writes.

The entry transitions are:

* ``end_entered``: Compose ``response_retained`` gains an ``EndFrame`` at
  ``review_needed``, retaining the exact candidate. The cut is handoff-safe and
  is sized for the END Advisor review.
* ``recovery_entered``: the same cut retains one sole paid recovery control
  without sending it through the graph tool dispatcher or shared END Review.
* ``end_review_dispatched``: the END review is about to make provider calls.
  One ``end:<response digest>`` effect is recorded ``dispatched`` first, so a
  lost owner settles it as unknown instead of silently re-running the review.
  A successful publication commits that same effect inside its terminal
  transaction (``sessions/continuation.py`` ``_settlement_transition``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sqlalchemy import insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, EndFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
)
from elspeth.web.composer.continuation_end_effects import end_effect_key, semantic_end_effect_key
from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, decode_end_candidate
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.recovery_controls import parse_recovery_tool_batch
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    bound_action_json,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
    retain_action_execution_receipt_on_connection,
)
from elspeth.web.sessions.continuation_primary import read_committed_primary_response_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_semantic_actions import SemanticActionAdmission

END_EFFECT_OWNER = "composer_end"

type EndTransition = Literal["end_entered", "recovery_entered", "end_review_dispatched", "end_repair_continued"]


def _quiescent_compose(stack_frames: tuple[object, ...]) -> ComposeFrame:
    compose = stack_frames[0]
    if type(compose) is not ComposeFrame or compose.phase != "response_retained" or compose.pending_response is None:
        raise AuditIntegrityError("END requires its actual retained PRIMARY response")
    if compose.provider_effect_key is None or compose.provider_input_sha256 is None:
        raise AuditIntegrityError("END lost its actual PRIMARY provider locator")
    return compose


def end_candidate_for(compose: ComposeFrame, *, candidate_text: str) -> EndContinuationCandidate:
    """The retained END candidate: references to actual DATA and the model's words."""
    if compose.pending_response is None or compose.provider_effect_key is None or compose.provider_input_sha256 is None:
        raise AuditIntegrityError("END candidate lost its actual paid response")
    return EndContinuationCandidate(
        kind="reply",
        response=compose.pending_response,
        primary_effect_key=compose.provider_effect_key,
        primary_input_sha256=compose.provider_input_sha256,
        candidate_text=candidate_text,
        state=compose.state,
        evidence=compose.evidence,
        iteration=compose.iteration,
    )


def recovery_candidate_for(compose: ComposeFrame, *, message: str) -> EndContinuationCandidate:
    """The provider's paid recovery intent, retained without tool dispatch."""
    return end_candidate_for(compose, candidate_text=message).model_copy(update={"kind": "recovery"})


def retain_end_transition(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    transition: EndTransition,
    candidate: PrivateChunk | None = None,
    captured: CapturedContinuationRuntime | None = None,
) -> StoredContinuationSnapshot:
    from elspeth.web.continuation_retention_memo import borrow_v4_preparation_memo_scope

    if transition not in ("end_entered", "recovery_entered", "end_review_dispatched", "end_repair_continued"):
        raise ValueError("END transition has no implemented source owner")
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("END transition lost its actual current source/claim owners")
    if (transition in ("end_entered", "recovery_entered")) is (candidate is None):
        raise AuditIntegrityError("only END entry retains a new candidate")
    if (transition == "end_repair_continued") is (captured is None):
        raise AuditIntegrityError("only END repair continuation retains captured live owners")
    before = selected.stack
    sid, operation_id = before.logical.session_id, before.logical.operation_id
    with borrow_v4_preparation_memo_scope(), locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        _require_current_continuation_running(conn, running)
        require_current_env_inventory_on_connection(
            conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=before.cursor
        )
        stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
        if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(before):
            raise AuditIntegrityError("END transition replaced its actual selected predecessor")
        if before.pending_owner_results or before.pending_settlement_ids:
            raise AuditIntegrityError("END transition requires a quiescent selected cut")
        review_admission: SemanticActionAdmission | None = None
        action_json = None
        if before.active_action_json is not None:
            from elspeth.web.sessions.continuation_semantic_actions import (
                installed_action_json,
                require_consumed_semantic_review_on_connection,
                require_dispatched_semantic_review_on_connection,
            )

            source = before.active_action_source
            if source is None or transition in ("end_entered", "recovery_entered"):
                raise AuditIntegrityError("END transition requires a quiescent selected cut")
            if transition == "end_review_dispatched":
                review_admission = require_consumed_semantic_review_on_connection(
                    conn, running, selected=stored, effect_key=source.effect_key
                )
            else:
                review_admission = require_dispatched_semantic_review_on_connection(
                    conn, running, selected=stored, effect_key=source.effect_key
                )
            action_json = bound_action_json(before, action_owner="review", read_tool_name=None, read_tool_arguments_json=None)
            if action_json != installed_action_json(review_admission) or source != review_admission.paid.source:
                raise AuditIntegrityError("END review changed its original paid action/source")
        elif before.active_action_source is not None:
            raise AuditIntegrityError("END transition lost its actual prescribed action")
        compose = _quiescent_compose(before.frames)
        paid = compose.pending_response
        if paid is None:
            raise AuditIntegrityError("END lost its actual paid PRIMARY response")
        key = end_effect_key(paid)
        if review_admission is not None:
            if review_admission.action_key is None:
                raise AuditIntegrityError("semantic END review lost its original action key")
            key = semantic_end_effect_key(paid, review_admission.action_key)
        open_effects = tuple(item for item in before.effect_frontier if item.disposition not in ("committed", "refused"))
        expected_open = (
            (EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),) if transition == "end_repair_continued" else ()
        )
        if open_effects != expected_open:
            raise AuditIntegrityError("END transition found another open effect")
        response = decode_continuation_response(
            read_committed_primary_response_on_connection(conn, running, selected=selected, reference=paid)
        )
        if response.tool_batch.calls and transition != "recovery_entered":
            raise AuditIntegrityError("END requires a PRIMARY response that issued no tool calls")
        policy = decode_continuation_policy(before.admission_policy_json, before.policy_hash)
        if transition in ("end_entered", "recovery_entered"):
            if len(before.frames) != 1 or candidate is None:
                raise AuditIntegrityError("END entry requires its exact single Compose predecessor")
            value = decode_end_candidate(candidate)
            if transition == "recovery_entered":
                try:
                    intent = parse_recovery_tool_batch(response.tool_batch)
                except (TypeError, ValueError) as error:
                    raise AuditIntegrityError("recovery END lost its sole provider-authored control") from error
                if intent is None:
                    raise AuditIntegrityError("recovery END has no original provider-authored intent")
                expected = recovery_candidate_for(compose, message=intent.message)
            else:
                expected = end_candidate_for(compose, candidate_text=response.message.content or "")
            if value != expected:
                raise AuditIntegrityError("END candidate differs from the actual paid response and its sources")
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation_id,
                key=private_chunk_key(candidate),
                payload=candidate.canonical.encode("utf-8"),
                codec=candidate.reference.codec,
            )
            end = EndFrame(phase="review_needed", state=compose.state, candidate=candidate.reference, publication_binding=None)
            after = before.model_copy(update={"frames": (compose, end)})
            event = ContinuationEvent(
                "checkpoint",
                canonical_json({"owner": END_EFFECT_OWNER, "transition": transition, "response": paid.digest}),
            )
            next_action = policy.actions.advisor
        elif transition == "end_repair_continued":
            selected_end = before.frames[-1]
            if captured is None or captured.selected is not selected or type(selected_end) is not EndFrame or len(before.frames) != 2:
                raise AuditIntegrityError("END repair continuation changed its actual selected capture")
            captured_compose = captured.stack.frames[0]
            if (
                type(captured_compose) is not ComposeFrame
                or len(captured.stack.frames) != 2
                or captured.stack.frames[-1] != selected_end
                or captured.stack.model_copy(update={"frames": before.frames}) != before
                or captured_compose.pending_response != paid
                or captured_compose.initial_version != compose.initial_version
                or captured_compose.slice_sequence != compose.slice_sequence
                or captured_compose.repair_turns + captured_compose.advisor_checkpoint_passes
                <= compose.repair_turns + compose.advisor_checkpoint_passes
            ):
                raise AuditIntegrityError("END repair continuation must spend a repair or review pass on its own cut")
            for chunk in captured.chunks:
                put_chunk(
                    conn,
                    session_id=sid,
                    operation_id=operation_id,
                    key=private_chunk_key(chunk),
                    payload=chunk.canonical.encode("utf-8"),
                    codec=chunk.reference.codec,
                )
            following = captured_compose.model_copy(
                update={"phase": "action_ready", "pending_response": None, "provider_effect_key": None, "provider_input_sha256": None}
            )
            receipt = f"repair:{following.iteration.digest}"
            changed = conn.execute(
                update(effects)
                .where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == operation_id,
                    effects.c.effect_key == key,
                    effects.c.owner == END_EFFECT_OWNER,
                    effects.c.disposition == "dispatched",
                    effects.c.creator_sequence == running.claim.segment_sequence,
                    effects.c.creator_generation == running.claim.ownership_generation,
                )
                .values(
                    disposition="committed",
                    receipt_key=receipt,
                    result_chunk_key=f"value:{following.iteration.codec}:{following.iteration.digest}",
                )
            )
            if changed.rowcount != 1:
                raise AuditIntegrityError("END repair continuation lost its exact dispatched review effect")
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
            if review_admission is not None:
                after = decode_continuation_snapshot(
                    retain_action_execution_receipt_on_connection(
                        conn,
                        session_id=sid,
                        operation_id=operation_id,
                        effect_key=key,
                        disposition="committed",
                        snapshot_json=encode_continuation_snapshot(after),
                    )
                )
            event = ContinuationEvent(
                "checkpoint",
                canonical_json(
                    {"owner": END_EFFECT_OWNER, "transition": transition, "effect_key": key, "iteration": following.iteration.digest}
                ),
            )
            next_action = policy.actions.primary
        else:
            selected_end = before.frames[-1]
            if (
                len(before.frames) != 2
                or type(selected_end) is not EndFrame
                or selected_end.phase != "review_needed"
                or selected_end.state != compose.state
            ):
                raise AuditIntegrityError("END review requires its exact retained review cut")
            end = selected_end
            value = decode_end_candidate(next(chunk for chunk in selected.chunks if chunk.reference == end.candidate))
            original_candidate = end_candidate_for(compose, candidate_text=response.message.content or "")
            if review_admission is None:
                if value != original_candidate:
                    raise AuditIntegrityError("END review changed its retained candidate")
            else:
                original = review_admission.evidence.coverage.end_candidate
                if (
                    original is None
                    or original.model_copy(update={"state": compose.state, "evidence": compose.evidence, "iteration": compose.iteration})
                    != original_candidate
                ):
                    raise AuditIntegrityError("semantic END review changed its original paid PRIMARY candidate")
                if value != review_admission.review_candidate or end.candidate != review_admission.review_candidate_source:
                    raise AuditIntegrityError("semantic END review changed its checked candidate rebinding")
            if any(item.effect_id == key for item in before.effect_frontier):
                raise AuditIntegrityError("END review already recorded its single provider effect")
            input_sha256 = stable_hash({"schema": "composer.end-review.v1", "candidate": end.candidate.digest})
            conn.execute(
                insert(effects).values(
                    session_id=sid,
                    operation_id=operation_id,
                    effect_key=key,
                    input_sha256=input_sha256,
                    owner=END_EFFECT_OWNER,
                    disposition="dispatched",
                    creator_sequence=running.claim.segment_sequence,
                    creator_generation=running.claim.ownership_generation,
                    action_json=action_json,
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
            if review_admission is not None:
                after = decode_continuation_snapshot(
                    retain_action_execution_receipt_on_connection(
                        conn,
                        session_id=sid,
                        operation_id=operation_id,
                        effect_key=key,
                        disposition="dispatched",
                        snapshot_json=encode_continuation_snapshot(after),
                    )
                )
            event = ContinuationEvent(
                "may_dispatch",
                canonical_json({"owner": END_EFFECT_OWNER, "transition": transition, "effect_key": key, "input_sha256": input_sha256}),
            )
            next_action = policy.actions.advisor
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
            raise AuditIntegrityError("END transition lost its retained frames")
        if captured is not None:
            from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
            from elspeth.web.composer.continuation_runtime import _selected_chunks

            root = decode_continuation_snapshot(retained.payload_json)
            added: tuple[PrivateChunk, ...] = ()
            if review_admission is not None:
                frontier = root.semantic_frontier
                if frontier is None:
                    raise AuditIntegrityError("semantic END repair lost its committed action frontier")
                canonical = read_chunk(
                    conn, session_id=sid, operation_id=operation_id, key=f"value:{frontier.codec}:{frontier.digest}"
                ).decode("utf-8", errors="strict")
                added = (PrivateChunk(frontier, canonical),)
            current = SelectedContinuationState(retained.checkpoint_id, root, _selected_chunks(root, captured.chunks, added))
            read_selected_turn_evidence_graph_on_connection(conn, running, selected=current, names=inventory.names)
        return retained
