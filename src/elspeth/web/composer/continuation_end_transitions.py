"""SQL owners for the Continuation END phase (U-40).

END begins after an actual, already committed PRIMARY response that issued no
tool calls. Its candidate is the provider's own words; nothing here authors a
reply, a graph or a proposal. Each transition is one locked session
transaction that re-proves the current owner, the selected predecessor and the
paid response before it writes.

Two transitions exist:

* ``end_entered``: Compose ``response_retained`` gains an ``EndFrame`` at
  ``review_needed``, retaining the exact candidate. The cut is handoff-safe and
  is sized for the END Advisor review.
* ``end_review_dispatched``: the END review is about to make provider calls.
  One ``end:<response digest>`` effect is recorded ``dispatched`` first, so a
  lost owner settles it as unknown instead of silently re-running the review.
  A successful publication commits that same effect inside its terminal
  transaction (``sessions/continuation.py`` ``_settlement_transition``).
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, EndFrame, PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
)
from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, decode_end_candidate
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import decode_continuation_policy
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
from elspeth.web.sessions.continuation_primary import read_committed_primary_response_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments

END_EFFECT_OWNER = "composer_end"

type EndTransition = Literal["end_entered", "end_review_dispatched"]


def end_effect_key(response: PrivateChunkRef) -> str:
    """The one END review effect of one paid PRIMARY response."""
    if type(response) is not PrivateChunkRef or response.codec != "composer.response.v1":
        raise AuditIntegrityError("END effect requires its actual paid PRIMARY response")
    return f"end:{response.digest}"


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


def retain_end_transition(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    transition: EndTransition,
    candidate: PrivateChunk | None = None,
) -> StoredContinuationSnapshot:
    if transition not in ("end_entered", "end_review_dispatched"):
        raise ValueError("END transition has no implemented source owner")
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
    ):
        raise AuditIntegrityError("END transition lost its actual current source/claim owners")
    if (transition == "end_entered") is (candidate is None):
        raise AuditIntegrityError("only END entry retains a new candidate")
    before = selected.stack
    sid, operation_id = before.logical.session_id, before.logical.operation_id
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
        _require_current_continuation_running(conn, running)
        require_current_env_inventory_on_connection(
            conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=before.cursor
        )
        stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
        if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(before):
            raise AuditIntegrityError("END transition replaced its actual selected predecessor")
        if before.pending_owner_results or before.pending_settlement_ids or before.active_action_json is not None:
            raise AuditIntegrityError("END transition requires a quiescent selected cut")
        if any(item.disposition not in ("committed", "refused") for item in before.effect_frontier):
            raise AuditIntegrityError("END transition cannot start over an open effect")
        compose = _quiescent_compose(before.frames)
        paid = compose.pending_response
        if paid is None:
            raise AuditIntegrityError("END lost its actual paid PRIMARY response")
        response = decode_continuation_response(
            read_committed_primary_response_on_connection(conn, running, selected=selected, reference=paid)
        )
        if response.tool_batch.calls:
            raise AuditIntegrityError("END requires a PRIMARY response that issued no tool calls")
        policy = decode_continuation_policy(before.admission_policy_json, before.policy_hash)
        key = end_effect_key(paid)
        if transition == "end_entered":
            if len(before.frames) != 1 or candidate is None:
                raise AuditIntegrityError("END entry requires its exact single Compose predecessor")
            value = decode_end_candidate(candidate)
            if value != end_candidate_for(compose, candidate_text=response.message.content or ""):
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
            if value != end_candidate_for(compose, candidate_text=response.message.content or ""):
                raise AuditIntegrityError("END review changed its retained candidate")
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
        if decode_continuation_snapshot(retained.payload_json).frames[-1] != end:
            raise AuditIntegrityError("END transition lost its retained frame")
        return retained
