"""Original site-free END fallthrough before semantic Review retention.

This is a value projection.  The paid response still belongs to its owed H
until the SQL Review consequence admits and consumes every retained effect.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _EndCompletionDisposition
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGatePassed
from elspeth.web.composer.composer_preflight import _state_contains_blob_ref
from elspeth.web.composer.composition_completion import (
    _end_disposition_after_terminal_barriers,
    _TerminalNoToolAdvisorGateOutcome,
    _with_advisor_gate_decision,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import (
    EndGateComputation,
    require_original_checkpoint_end_computation,
)
from elspeth.web.composer.continuation_checkpoint_end_locals import OrdinaryEndCallerLocals
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    CheckpointEndPhysicalSource,
    checkpoint_end_physical_custody,
    require_checkpoint_end_detached_source,
)
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.execution.completion_gates import completion_gate_fingerprint
from elspeth.web.interpretation_state import interpretation_sites


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ProjectedEndCompleteClean:
    computation: EndGateComputation
    result: ComposerResult
    disposition: _EndCompletionDisposition
    superseded_reply_index: int | None


@dataclass(frozen=True, slots=True)
class _Original:
    physical: CheckpointEndPhysicalSource
    returned: CheckpointReturnedResponseObservation
    computation: EndGateComputation
    result: ComposerResult
    result_snapshot: ComposerResult
    state_json: str
    preflight_json: str | None


_issued: WeakKeyDictionary[ProjectedEndCompleteClean, _Original] = WeakKeyDictionary()


async def project_end_complete_clean(
    *, physical: CheckpointEndPhysicalSource, returned: CheckpointReturnedResponseObservation, computation: EndGateComputation
) -> ProjectedEndCompleteClean:
    """Run the original pure no-site tail with its preflight reuse proved first."""
    require_checkpoint_end_detached_source(physical)
    require_original_checkpoint_end_computation(computation, source=physical, returned=returned)
    gate, owner, live = physical.gate_locals, physical.phase_owner, physical.detached.live
    bindings = physical.detached.bindings
    prior = live.last_runtime_preflight
    no_recompute = (
        live.state.version == gate.initial_version
        and not _state_contains_blob_ref(live.state)
        and (prior is not None or live.state.validate().is_valid)
        and gate.runtime_preflight == prior
    )
    graph_blocked = (
        gate.completion_gates is not None
        and gate.completion_gates.advisor_signoff is not None
        and gate.completion_gates.advisor_signoff.for_graph == completion_gate_fingerprint(live.state)
        and gate.completion_gates.advisor_signoff.cause is AdvisorBlockCause.GRAPH_REJECTED
    )
    if (
        type(gate.caller) is not OrdinaryEndCallerLocals
        or gate.caller.proof_runtime_preflight is not None
        or computation.action != "fallthrough"
        or not computation.publication_admitted
        or not no_recompute
        or graph_blocked
        or interpretation_sites(live.state)
        or gate.orphan_observation.events
        or gate.orphan_observation.missing_sites
        or live.advisor_repair_context_introduced
    ):
        raise AuditIntegrityError("END clean projection requires the unchanged qualified no-site ordinary branch")
    index = owner.superseded_reply_index
    if index is not None and (
        not 0 <= index < len(live.llm_messages) or live.llm_messages[index] != {"role": "assistant", "content": computation.candidate}
    ):
        raise AuditIntegrityError("END clean projection changed its original superseded reply")
    custody = checkpoint_end_physical_custody(physical)
    if any(call.call_id == returned.audit_call.call_id for call in live.recorder.llm_calls):
        raise AuditIntegrityError("END clean projection duplicated its original paid call")
    live.recorder.record_llm_call(returned.audit_call, provider_custody=custody)
    completion = owner.phases.service._completion
    result = await completion._finalize_no_tool_response(
        content=computation.candidate,
        state=live.state,
        initial_version=gate.initial_version,
        user_id=bindings.user_id,
        session_id=bindings.session_id,
        last_runtime_preflight=prior,
        runtime_preflight_cache=live.runtime_preflight_cache,
        session_scope=bindings.session_scope,
        user_message=bindings.message,
        mutation_success_seen=live.mutation_success_seen,
        tool_invocations=live.recorder.invocations,
        llm_calls=live.recorder.llm_calls,
        plugin_snapshot=bindings.plugin_snapshot,
        session_operation_context=bindings.session_operation_context,
    )
    decision = AdvisorGatePassed(for_graph=completion_gate_fingerprint(live.state)) if prior is not None and prior.is_valid else None
    result = _with_advisor_gate_decision(result, gate.completion_gates, decision, computation.review_identity)
    if index is not None:
        del live.llm_messages[index]
    live.advisor_review_state = computation.review_state
    live.advisor_checkpoint_passes_used += computation.passes_delta
    result = replace(
        result,
        repair_turns_used=gate.repair_turns_used,
        persisted_assistant_message_id=gate.persisted_assistant_message_id,
        persisted_assistant_content=gate.persisted_assistant_content,
        persisted_tool_call_turn=gate.persisted_tool_call_turn,
    )
    result = completion._append_checkpoint_receipt(result, turn_evidence=live.turn_evidence)
    disposition = _end_disposition_after_terminal_barriers(
        result,
        _TerminalNoToolAdvisorGateOutcome(
            action="fall_through",
            advisor_gate_decision=decision,
            publication_binding=computation.publication_review.binding,
            graph_review_identity=computation.review_identity,
        ),
    )
    if result.pipeline_commit_intent is not None or result.graph_recovery_intent is not None or result.recovery_audit_drafts:
        raise AuditIntegrityError("END clean projection unexpectedly requested a graph write")
    projected = ProjectedEndCompleteClean(computation, result, disposition, index)
    _issued[projected] = _Original(
        physical,
        returned,
        computation,
        result,
        replace(result),
        canonical_json(result.state.to_dict()),
        None if result.runtime_preflight is None else result.runtime_preflight.model_dump_json(),
    )
    return projected


def require_original_end_complete_clean(
    projected: ProjectedEndCompleteClean, *, physical: CheckpointEndPhysicalSource, returned: CheckpointReturnedResponseObservation
) -> None:
    original = _issued.get(projected) if type(projected) is ProjectedEndCompleteClean else None
    if (
        original is None
        or original.physical is not physical
        or original.returned is not returned
        or original.computation is not projected.computation
        or original.result is not projected.result
        or original.result_snapshot != projected.result
        or original.state_json != canonical_json(projected.result.state.to_dict())
        or original.preflight_json
        != (None if projected.result.runtime_preflight is None else projected.result.runtime_preflight.model_dump_json())
    ):
        raise AuditIntegrityError("END clean result changed after its original projection")
    require_original_checkpoint_end_computation(projected.computation, source=physical, returned=returned)
