"""Carry the actual END dispatch into its unchanged warm runtime owners.

The dispatch writer proves the paid sources. This adjacent handoff refreshes
current custody and source metadata without reconstructing those same historical
inputs. Later capture, source preparation and effect owners keep their proofs.
"""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import EffectFrontierEntry, EndFrame
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_end_transitions import EndReviewDispatchData, retain_end_review_dispatch
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, materialize_continuation_env_inventory
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_prompt_producers import _CurrentProducerInputs
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _CurrentPhaseRead, _registered_read
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.continuation_retention_memo import borrow_v4_preparation_memo_scope
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseSource, _effect_inventory, _event_text
from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource, _runtime_source_from_inputs_on_connection
from elspeth.web.sessions.locking import session_data_read_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases


def _read_dispatched_end_source(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    dispatched: EndReviewDispatchData,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    inputs: _CurrentProducerInputs,
) -> ContinuationRuntimeSource:
    """Refresh current facts around the writer's unchanged historical DATA."""
    _require_current_continuation_running(conn, running)
    predecessor = dispatched.predecessor
    before, after = predecessor.stack, selected.stack
    if (
        runtime.source.selected is not predecessor
        or runtime.materialization.selected is not predecessor
        or runtime.materialization.inputs is not inputs
        or inventory.running is not running
        or runtime.materialization.inventory.running is not running
        or inventory.names != runtime.env_ref_names
        or inputs.bootstrap != runtime.source.bootstrap
        or inputs.values != runtime.source.original
        or inputs.evidence != runtime.source.evidence
        or inputs.admitted_evidence.source_chunk != runtime.source.admitted_evidence.source_chunk
        or inputs.producer_inputs != runtime.source.iteration.producer_inputs
        or selected.checkpoint_id != dispatched.selected.checkpoint_id
        or encode_continuation_snapshot(after) != dispatched.selected.payload_json
        or after.cursor != dispatched.selected.cursor
        or after.cursor != before.cursor + 1
        or after.model_copy(
            update={"cursor": before.cursor, "effect_frontier": before.effect_frontier, "semantic_frontier": before.semantic_frontier}
        )
        != before
        or after.effect_frontier
        != (*before.effect_frontier, EffectFrontierEntry(effect_id=dispatched.effect_key, disposition="dispatched", receipt_id=None))
        or (before.active_action_source is None and after.semantic_frontier != before.semantic_frontier)
    ):
        raise AuditIntegrityError("END dispatch handoff changed its actual predecessor or exact writer delta")
    current_chunks = {chunk.reference: chunk for chunk in selected.chunks}
    for chunk in predecessor.chunks:
        if chunk.reference != before.semantic_frontier and current_chunks.get(chunk.reference) != chunk:
            raise AuditIntegrityError("END dispatch handoff changed its unchanged complete source DATA")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != dispatched.selected:
        raise AuditIntegrityError("END dispatch handoff lost its actual current successor")
    _effect_inventory(conn, sid, operation, dispatched.selected)
    end = after.frames[-1]
    if type(end) is not EndFrame or end.phase != "review_needed":
        raise AuditIntegrityError("END dispatch handoff lost its actual review cut")
    input_sha = stable_hash({"schema": "composer.end-review.v1", "candidate": end.candidate.digest})
    if _event_text(conn, sid, operation, after.cursor) != (
        "may_dispatch",
        canonical_json(
            {
                "owner": "composer_end",
                "transition": "end_review_dispatched",
                "effect_key": dispatched.effect_key,
                "input_sha256": input_sha,
            }
        ),
    ):
        raise AuditIntegrityError("END dispatch handoff changed its actual dispatch event")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == dispatched.effect_key
        )
    ).one_or_none()
    if (
        effect is None
        or effect.owner != "composer_end"
        or effect.input_sha256 != input_sha
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id is not None
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.action_json != before.active_action_json
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise AuditIntegrityError("END dispatch handoff changed its actual unpaid outer effect")
    # This owner still checks current env/head, the complete SQL private cohort,
    # cost accounting, attachments and the original segment clock. The carried
    # graph/principal/tool-history DATA is not described as a new historical proof.
    return _runtime_source_from_inputs_on_connection(conn, running, selected=selected, inventory=inventory, inputs=inputs)


async def dispatch_and_adopt_end_review(
    phases: ContinuationLivePhases, selected: SelectedContinuationState, runtime: RestoredContinuationRuntime
) -> tuple[_CurrentPhaseRead, CheckpointPhaseSource]:
    """Dispatch with full proofs, then adopt only that writer's adjacent result."""
    inventory = runtime.materialization.inventory
    initial = _CurrentPhaseRead(runtime, selected, inventory, None, phases.lease)
    phases._require_phase_read(selected, initial)
    inputs = runtime.materialization.inputs
    if type(inputs) is not _CurrentProducerInputs:
        raise AuditIntegrityError("END dispatch requires its actual admitted predecessor inputs")
    running = inventory.running
    dispatched = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=partial(retain_end_review_dispatch, phases.authority, running, selected=selected, inventory=inventory),
        expected=EndReviewDispatchData,
    )
    following = await load_selected_continuation(
        authority=phases.authority, running=running, selected=dispatched.selected, required_work=phases.work
    )
    current_inventory = await materialize_continuation_env_inventory(
        advisor=phases.service._advisor_checkpoint, authority=phases.authority, selected=following, required_work=phases.work
    )

    def adopt() -> RestoredContinuationRuntime:
        with (
            borrow_v4_preparation_memo_scope(phases.action_clock),
            session_data_read_transaction(phases.authority._engine, str(running.claim.session_id)) as conn,
        ):
            fresh = _read_dispatched_end_source(
                conn,
                running,
                dispatched=dispatched,
                runtime=runtime,
                selected=following,
                inventory=current_inventory,
                inputs=inputs,
            )
        runtime.materialization.advance_current_from_source(following, current_inventory, fresh)
        advanced = replace(runtime, source=fresh)
        # Install the actual successor before the registered bridge delivers
        # deferred cancellation, as the ordinary phase owner already does.
        phases.runtime = advanced
        return advanced

    advanced = await _registered_read(phases.work, RestoredContinuationRuntime, adopt)
    phase_read = _CurrentPhaseRead(advanced, following, current_inventory, None, phases.lease)
    phases._require_phase_read(following, phase_read)
    source = CheckpointPhaseSource("end", "end_review", dispatched.selected, dispatched.selected, dispatched.effect_key)
    return phase_read, source
