"""Build the original END input from issued caller locals and runtime capture."""

from __future__ import annotations

from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.readonly_json import JsonValue
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_checkpoint_end_input import (
    _GATES,
    _MESSAGE,
    _ORPHANS,
    _PUBLICATION,
    _REVIEW,
    _VALIDATION,
    CapturedCheckpointEndInput,
    CheckpointEndInputSnapshot,
    CheckpointIterationEndCaller,
    CheckpointOrdinaryEndCaller,
    CheckpointSourceProofEndCaller,
    capture_checkpoint_end_input,
)
from elspeth.web.composer.continuation_checkpoint_end_locals import (
    CheckpointEndGateLocals,
    IterationEndCallerLocals,
    OrdinaryEndCallerLocals,
    SourceProofEndCallerLocals,
    require_original_checkpoint_end_gate_locals,
)
from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
from elspeth.web.composer.continuation_classifier_handoff import encode_classify_post_primary
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_tool_batch import _dump_owned
from elspeth.web.execution.completion_gates import completion_gate_fingerprint


def checkpoint_end_request_identity(options: dict[str, JsonValue]) -> str:
    """Bind the actual request after the gateway's idempotent usage opt-in."""
    from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash
    from elspeth.web.composer.provider_gateway import _apply_openrouter_usage_accounting

    _apply_openrouter_usage_accounting(options)
    return continuation_provider_input_hash(options)


def build_checkpoint_end_input(
    *,
    phase_owner: CheckpointPhaseOwner,
    gate_locals: CheckpointEndGateLocals,
    captured: CapturedContinuationRuntime,
    retry_ordinal: Literal[0, 1] = 0,
) -> CapturedCheckpointEndInput:
    """Retain the actual gate input; grant neither SQL nor SDK admission.

    The caller supplies the capture made at this original prepared attempt.
    The codec rechecks the source, complete runtime closure, retained PRIMARY,
    publication packet and every issued gate/caller field before issuance.
    """
    require_original_checkpoint_end_gate_locals(gate_locals, phase_owner=phase_owner)
    if type(captured) is not CapturedContinuationRuntime:
        raise TypeError("END producer requires its original runtime capture")
    frames = tuple(frame for frame in captured.stack.frames if type(frame) is ComposeFrame)
    if len(frames) != 1:
        raise AuditIntegrityError("END producer lost its exact original compose frame")
    frame = frames[0]
    original = gate_locals.caller
    caller: CheckpointOrdinaryEndCaller | CheckpointSourceProofEndCaller | CheckpointIterationEndCaller
    if type(original) is OrdinaryEndCallerLocals:
        caller = CheckpointOrdinaryEndCaller(
            proof_runtime_preflight_json=None
            if original.proof_runtime_preflight is None
            else _dump_owned(_VALIDATION, original.proof_runtime_preflight),
            proof_repair_action=original.proof_repair_action,
            proof_repair_diagnostics_json=None
            if original.proof_repair_diagnostics is None
            else canonical_json(owned_json(deep_thaw(original.proof_repair_diagnostics))),
            confirmed_stop=original.confirmed_stop,
            confirmed_stop_evidence_sha256=frame.evidence.digest if original.confirmed_stop else None,
        )
    elif type(original) is SourceProofEndCallerLocals:
        caller = CheckpointSourceProofEndCaller(
            proof_kind=original.proof_kind,
            diagnostics_json=None if original.diagnostics is None else canonical_json(owned_json(deep_thaw(original.diagnostics))),
            source_name=original.source_name,
            reason=original.reason,
            composed_stop_validation_json=_dump_owned(_VALIDATION, original.composed_stop_validation),
        )
    elif type(original) is IterationEndCallerLocals:
        caller = CheckpointIterationEndCaller(
            pending_source_sha256=stable_hash(original.pending.model_dump(mode="json")),
            primary_effect_key=original.pending.primary_effect_key,
            pending_json=encode_classify_post_primary(original.pending),
        )
    else:
        raise AuditIntegrityError("END producer requires the original finite caller")
    source = phase_owner.phase_source
    runtime = phase_owner.runtime
    index = phase_owner.superseded_reply_index
    if index is not None and not 0 <= index < len(runtime.live.llm_messages):
        raise AuditIntegrityError("END producer lost its original superseded reply position")
    snapshot = CheckpointEndInputSnapshot(
        session_id=captured.stack.logical.session_id,
        operation_id=captured.stack.logical.operation_id,
        outer_effect_key=source.outer_effect_key,
        original_checkpoint_id=source.current.checkpoint_id,
        original_cursor=source.current.cursor,
        captured_runtime_sha256=stable_hash(captured.stack.model_dump(mode="json")),
        runtime_chunks=tuple(chunk.reference for chunk in captured.chunks),
        state_sha256=frame.state.digest,
        graph_fingerprint=completion_gate_fingerprint(runtime.live.state),
        assistant_message_json=_dump_owned(_MESSAGE, gate_locals.assistant_message),
        runtime_preflight_json=None if gate_locals.runtime_preflight is None else _dump_owned(_VALIDATION, gate_locals.runtime_preflight),
        review_state_json=_dump_owned(_REVIEW, gate_locals.review_state),
        initial_review_state_json=_dump_owned(_REVIEW, gate_locals.initial_review_state),
        publication_review_json=_dump_owned(_PUBLICATION, gate_locals.publication_review),
        completion_gates_json=None if gate_locals.completion_gates is None else _dump_owned(_GATES, gate_locals.completion_gates),
        advisor_checkpoint_passes_used=gate_locals.advisor_checkpoint_passes_used,
        passes_delta=gate_locals.passes_delta,
        pass_index=gate_locals.pass_index,
        max_passes=gate_locals.max_passes,
        retry_ordinal=retry_ordinal,
        allow_repair_continue=gate_locals.allow_repair_continue,
        orphaned_precheck=gate_locals.orphan_observation.missing_sites,
        orphan_events_json=_dump_owned(_ORPHANS, gate_locals.orphan_observation.events),
        orphan_source_sha256=gate_locals.orphan_observation.source_sha256,
        caller=caller,
        superseded_reply_index=index,
        superseded_reply_sha256=None if index is None else stable_hash(runtime.live.llm_messages[index]),
        elide_advisor_exchange=phase_owner.elide_advisor_exchange,
    )
    return capture_checkpoint_end_input(
        snapshot,
        phase_source=source,
        runtime=runtime,
        captured=captured,
        orphan_observation=gate_locals.orphan_observation,
        gate_locals=gate_locals,
        phase_owner=phase_owner,
    )
