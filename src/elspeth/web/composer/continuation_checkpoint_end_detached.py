"""Restore and prove an isolated END graph before its physical admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_caches import capture_continuation_caches, decode_continuation_caches, encode_continuation_caches
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_iteration import capture_continuation_iteration, encode_continuation_iteration
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    _capture_owners,
    _compose,
    _selected_chunks,
    capture_continuation_runtime_owners,
    restore_checkpoint_projection_runtime,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.sessions.continuation_runtime import runtime_chunk

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPreparedSource


def require_detached_end_owners(detached: RestoredContinuationRuntime, original: RestoredContinuationRuntime) -> None:
    """Shared service/work authority is intentional; mutable graph owners are not."""
    if type(detached) is not RestoredContinuationRuntime or type(original) is not RestoredContinuationRuntime:
        raise AuditIntegrityError("END detached source lost its nominal restored owners")
    if (
        detached is original
        or detached.live is original.live
        or detached.graph is original.graph
        or detached.materialization is original.materialization
        or detached.live.state is original.live.state
        or detached.live.recorder is original.live.recorder
        or detached.live.anti_anchor is original.live.anti_anchor
        or detached.live.turn_evidence is original.live.turn_evidence
        or detached.live.turn_evidence.transcript is original.live.turn_evidence.transcript
        or detached.live.llm_messages is original.live.llm_messages
        or not {id(message) for message in detached.live.llm_messages}.isdisjoint(id(other) for other in original.live.llm_messages)
        or type(detached.detached_disclosure) is not SchemaDisclosureTracker
        or detached.detached_disclosure is original.materialization.service._schema_disclosure
        or detached.bindings.required_work is not original.bindings.required_work
        or detached.bindings.provider_owner is not original.bindings.provider_owner
        or detached.bindings.session_operation_context is not original.bindings.session_operation_context
        or detached.env_ref_names != original.env_ref_names
    ):
        raise AuditIntegrityError("END detached source reused a live mutable owner or changed authority")
    detached_batch, original_batch = detached.graph.batch, original.graph.batch
    if (
        detached_batch is not None
        and original_batch is not None
        and (
            detached_batch is original_batch
            or detached_batch.batch is original_batch.batch
            or detached_batch.accumulator is original_batch.accumulator
            or detached_batch.batch.tool_outcomes is original_batch.batch.tool_outcomes
            or detached_batch.batch.private_authorings_by_call is original_batch.batch.private_authorings_by_call
            or detached_batch.batch.decoded_args_by_call_id is original_batch.batch.decoded_args_by_call_id
            or detached_batch.batch.refused_resources is original_batch.batch.refused_resources
            or detached_batch.batch.promises_by_call is original_batch.batch.promises_by_call
        )
    ):
        raise AuditIntegrityError("END detached source reused a live mutable batch")


def require_detached_end_capture(
    detached: RestoredContinuationRuntime, *, expected: CapturedContinuationRuntime, phase_owner: CheckpointPhaseOwner
) -> None:
    """Effectful synchronous validation; callers must own a registered worker.

    Capture reacquires policy and the live operation fence through SQL. The
    immediate comparison below consumes that actual DATA without another read.
    """
    require_detached_end_owners(detached, phase_owner.runtime)
    observed = _capture_owners(detached, source=detached.source, inventory=detached.materialization.inventory)
    require_detached_capture_matches(observed, expected=expected)


def require_detached_capture_matches(observed: CapturedContinuationRuntime, *, expected: CapturedContinuationRuntime) -> None:
    """Compare the actual capture DATA; this function opens no authority scope."""
    if observed.stack != expected.stack or sorted(
        (chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in observed.chunks
    ) != sorted((chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in expected.chunks):
        raise AuditIntegrityError("END detached graph changed its complete pre-admission capture")


def expected_prepared_end_capture(*, phase_owner: CheckpointPhaseOwner, selected: SelectedContinuationState) -> CapturedContinuationRuntime:
    """Account for the exact cache observation and current validation refresh.

    Runtime capture appends cache history even without graph changes. Runtime
    restore independently refreshes policy validation. Neither makes the full
    capture idempotent; all other selected fields and bytes must remain exact.
    """
    original = phase_owner.runtime
    live, bindings = original.live, original.bindings
    index, frame = _compose(selected)
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("END detached expectation lost its original capture context")
    names = original.env_ref_names
    caches = encode_continuation_caches(
        capture_continuation_caches(
            discovery=live.discovery_cache,
            preflight=live.runtime_preflight_cache,
            last_preflight=live.last_runtime_preflight,
            creator=context,
            prior=decode_continuation_caches(runtime_chunk(selected, frame.caches), env_ref_names=names),
            env_ref_names=names,
        ),
        env_ref_names=names,
    )
    snapshot, catalog = original.materialization.service._policy_context.build(bindings.user_id)
    if snapshot.snapshot_hash != bindings.plugin_snapshot.snapshot_hash or snapshot.policy_hash != bindings.plugin_snapshot.policy_hash:
        raise AuditIntegrityError("END detached expectation changed its actual current policy")
    current = catalog.validate_composition_state(live.state)
    iteration = encode_continuation_iteration(
        capture_continuation_iteration(
            producer_inputs=original.materialization.snapshot_inputs(),
            last_validation=current.validation,
            advisor_review=live.advisor_review_state,
            failed_turn=live.failed_turn,
            anti_anchor=live.anti_anchor.snapshot_for_continuation(),
            disclosure=original.materialization.service._schema_disclosure.snapshot_for_continuation(
                session_id=selected.stack.logical.session_id,
                catalog_hash=snapshot.snapshot_hash,
                policy_hash=snapshot.policy_hash,
            ),
            env_ref_names=names,
        ),
        env_ref_names=names,
    )
    frames = list(selected.stack.frames)
    frames[index] = frame.model_copy(update={"caches": caches.reference, "iteration": iteration.reference})
    root = selected.stack.model_copy(update={"frames": tuple(frames)})
    return CapturedContinuationRuntime(selected, root, _selected_chunks(root, selected.chunks, (caches, iteration)))


async def restore_prepared_end_runtime(
    *, phase_owner: CheckpointPhaseOwner, prepared: CheckpointPreparedSource, captured: CapturedContinuationRuntime
) -> tuple[RestoredContinuationRuntime, CapturedContinuationRuntime]:
    """Read the retained warm source, before any physical binding/attempt/H."""
    phases = phase_owner.phases
    running = phases.work.running
    if running is None or prepared.source.checkpoint_id != phase_owner.selected.checkpoint_id:
        raise AuditIntegrityError("END detached restore changed its original source-prepared transition")
    selected = await load_selected_continuation(
        authority=phases.authority, running=running, selected=prepared.prepared_selected, required_work=phases.work
    )
    if selected.stack != decode_continuation_snapshot(
        prepared.prepared_selected.payload_json
    ) or selected.stack != captured.stack.model_copy(update={"cursor": selected.stack.cursor}):
        raise AuditIntegrityError("END prepared root is not its exact captured warm source")
    inventory = await materialize_continuation_env_inventory(
        advisor=phases.service._advisor_checkpoint,
        authority=phases.authority,
        selected=selected,
        required_work=phases.work,
    )
    if inventory.names != phase_owner.inventory.names:
        raise AuditIntegrityError("END prepared source changed its current credential-reference inventory")
    detached = await restore_checkpoint_projection_runtime(
        service=phases.service,
        authority=phases.authority,
        selected=selected,
        inventory=inventory,
        required_work=phases.work,
        provider_owner=phases.provider_owner,
    )
    require_detached_end_owners(detached, phase_owner.runtime)
    detached_capture = await capture_continuation_runtime_owners(detached, selected=selected, inventory=inventory)
    expected = expected_prepared_end_capture(phase_owner=phase_owner, selected=selected)
    if detached_capture.stack != expected.stack or sorted(
        (chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in detached_capture.chunks
    ) != sorted((chunk.reference.codec, chunk.reference.digest, chunk.canonical) for chunk in expected.chunks):
        raise AuditIntegrityError("END detached restore changed the retained warm graph")
    return detached, detached_capture
