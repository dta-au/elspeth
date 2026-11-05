"""Pure first-slice END terminal consequence from one original paid checkpoint.

This projection deliberately grants no SQL or dispatch authority.  The SQL
writer must reprove the same owed physical source and every finalizer effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.composer_preflight import _state_contains_blob_ref
from elspeth.web.composer.continuation_checkpoint_end_computation import (
    EndGateComputation,
    require_original_checkpoint_end_computation,
)
from elspeth.web.composer.continuation_checkpoint_end_locals import OrdinaryEndCallerLocals
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    CheckpointEndPhysicalSource,
    checkpoint_end_physical_custody,
    require_checkpoint_end_detached_source,
    require_original_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff, state_is_structurally_empty
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.state import CompositionState
from elspeth.web.interpretation_state import interpretation_sites


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ProjectedEndCompleteTerminal:
    """The exact original blocked result before the shared finalizer writes."""

    computation: EndGateComputation
    result: ComposerResult
    withheld_candidate: str | None
    superseded_reply_index: int | None
    _issued_record: _TerminalSource | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _TerminalSource:
    physical: CheckpointEndPhysicalSource
    returned: CheckpointReturnedResponseObservation
    computation: EndGateComputation
    result: ComposerResult
    result_snapshot: ComposerResult
    state_json: str
    preflight_json: str | None
    withheld_candidate: str | None
    superseded_reply_index: int | None


_issued: WeakKeyDictionary[ProjectedEndCompleteTerminal, ReferenceType[_TerminalSource]] = WeakKeyDictionary()


def _original_absent_preflight_is_qualified(state: CompositionState, *, initial_version: int, last_preflight: object) -> bool:
    """Match the unchanged, non-blob, statically valid source of original None.

    The shared preflight owner intentionally returns None for this case; no
    synthetic validation result may be inserted into the original END gate.
    """
    return (
        last_preflight is None
        and state.version == initial_version
        and bool(state.sources)
        and bool(state.outputs)
        and not _state_contains_blob_ref(state)
        and state.validate().is_valid
        and not interpretation_sites(state)
    )


def _original_blocked_absent_preflight_is_qualified(state: CompositionState, *, initial_version: int, last_preflight: object) -> bool:
    """An unchanged empty candidate may be withheld without graph validation.

    The shared preflight intentionally returns None for empty state. A blocked
    Review retains that absence; it never treats the candidate as a valid graph
    or grants the clean publication branch its stronger qualification.
    """
    return _original_absent_preflight_is_qualified(state, initial_version=initial_version, last_preflight=last_preflight) or (
        last_preflight is None
        and state.version == initial_version
        and state_is_structurally_empty(state)
        and not _state_contains_blob_ref(state)
        and not interpretation_sites(state)
    )


def project_end_complete_terminal(
    *,
    physical: CheckpointEndPhysicalSource,
    returned: CheckpointReturnedResponseObservation,
    computation: EndGateComputation,
) -> ProjectedEndCompleteTerminal:
    """Continue the first blocked ordinary END branch on detached original owners.

    An original valid or source-qualified absent preflight, zero interpretation
    sites, no earlier repair context and one final FLAG avoid effectful reads.
    Unsupported branches retain the complete END-owed response and H.
    """
    require_checkpoint_end_detached_source(physical)
    require_original_checkpoint_end_computation(computation, source=physical, returned=returned)
    gate = physical.gate_locals
    owner = physical.phase_owner
    detached = physical.detached
    live = detached.live
    caller = gate.caller
    malformed_final = (
        not computation.verdict.ok
        and computation.verdict.failure_class == "malformed"
        and bool(owner.phase_source.completed_children)
        and computation.is_last_pass
    )
    flagged_final = (
        computation.verdict.ok
        and computation.verdict.blocking
        and computation.verdict.publication_verdict == "WITHHOLD"
        and computation.is_last_pass
    )
    missed = tuple(
        name
        for name, failed in (
            ("ordinary_caller", type(caller) is not OrdinaryEndCallerLocals),
            ("proof_override", type(caller) is OrdinaryEndCallerLocals and caller.proof_runtime_preflight is not None),
            ("blocked_action", computation.action != "blocked_or_orphan_terminal"),
            ("completed_verdict", not (flagged_final or malformed_final)),
            ("publication_admitted", computation.publication_admitted),
            ("last_pass", not computation.is_last_pass),
            ("genuine_orphans", bool(computation.genuine_orphans)),
            ("observed_orphans", bool(gate.orphan_observation.missing_sites)),
            (
                "original_absent_preflight",
                gate.runtime_preflight is None
                and not _original_blocked_absent_preflight_is_qualified(
                    live.state, initial_version=gate.initial_version, last_preflight=live.last_runtime_preflight
                ),
            ),
            ("valid_preflight", gate.runtime_preflight is not None and not gate.runtime_preflight.is_valid),
            ("handoff_preflight", gate.runtime_preflight is not None and is_pending_interpretation_handoff(gate.runtime_preflight)),
            ("interpretation_sites", bool(interpretation_sites(live.state))),
            ("repair_context", live.advisor_repair_context_introduced),
            ("pass_owner", live.advisor_checkpoint_passes_used != gate.advisor_checkpoint_passes_used),
        )
        if failed
    )
    if missed:
        raise AuditIntegrityError(f"END terminal projection requires the qualified first blocked ordinary branch: {', '.join(missed)}")
    index = owner.superseded_reply_index
    if index is not None and (
        not 0 <= index < len(live.llm_messages) or live.llm_messages[index] != {"role": "assistant", "content": computation.candidate}
    ):
        raise AuditIntegrityError("END terminal changed its original superseded reply")
    advisor = owner.phases.service._advisor_checkpoint
    if type(advisor) is not AdvisorCheckpointOwner:
        raise AuditIntegrityError("END terminal lost its original advisor owner")
    custody = checkpoint_end_physical_custody(physical)
    if any(call.call_id == returned.audit_call.call_id for call in live.recorder.llm_calls):
        raise AuditIntegrityError("END terminal duplicated its original paid call")
    live.recorder.record_llm_call(returned.audit_call, provider_custody=custody)
    saved_scope = live.turn_evidence.render_saved_scope()
    withheld = computation.candidate if computation.candidate.strip() else None
    blocked = advisor._advisor_blocked_result(
        reason="malformed" if malformed_final else "flagged_final_pass",
        verdict=computation.verdict,
        state=live.state,
        assistant_message=_AdmittedAssistantMessage(content=saved_scope),
        recorder=live.recorder,
        repair_turns_used=gate.repair_turns_used,
        persisted_assistant_message_id=gate.persisted_assistant_message_id,
        persisted_assistant_content=gate.persisted_assistant_content,
        persisted_tool_call_turn=gate.persisted_tool_call_turn,
        runtime_preflight=gate.runtime_preflight,
        outstanding_findings=None,
        publication_binding=computation.publication_review.binding,
        saved_scope=saved_scope,
    )
    blocked = replace(blocked, graph_review_identity=computation.review_identity)
    if index is not None:
        del live.llm_messages[index]
    live.advisor_review_state = computation.review_state
    live.advisor_checkpoint_passes_used += computation.passes_delta
    result = owner.phases.service._completion._append_checkpoint_receipt(blocked, turn_evidence=live.turn_evidence)
    if (
        result.pipeline_commit_intent is not None
        or result.graph_recovery_intent is not None
        or result.recovery_audit_drafts
        or result.tool_invocations
        or result.advisor_terminal_publication is None
        or result.graph_review_identity != computation.review_identity
        or result.llm_calls[-1:] != (returned.audit_call,)
    ):
        raise AuditIntegrityError("END terminal lost its complete original blocked result")
    projected = ProjectedEndCompleteTerminal(computation, result, withheld, index)
    original = _TerminalSource(
        physical,
        returned,
        computation,
        result,
        replace(result),
        canonical_json(result.state.to_dict()),
        None if result.runtime_preflight is None else result.runtime_preflight.model_dump_json(),
        withheld,
        index,
    )
    object.__setattr__(projected, "_issued_record", original)
    _issued[projected] = ref(original)
    return projected


def require_original_end_complete_terminal(
    projected: ProjectedEndCompleteTerminal,
    *,
    physical: CheckpointEndPhysicalSource,
    returned: CheckpointReturnedResponseObservation,
) -> None:
    """Reject a substituted blocked result or borrowed physical source."""
    registered = _issued.get(projected) if type(projected) is ProjectedEndCompleteTerminal else None
    original = None if registered is None else registered()
    if (
        original is None
        or original is not projected._issued_record
        or original.physical is not physical
        or original.returned is not returned
        or original.computation is not projected.computation
        or original.result is not projected.result
        or original.result_snapshot != projected.result
        or original.state_json != canonical_json(projected.result.state.to_dict())
        or original.preflight_json
        != (None if projected.result.runtime_preflight is None else projected.result.runtime_preflight.model_dump_json())
        or original.withheld_candidate != projected.withheld_candidate
        or original.superseded_reply_index != projected.superseded_reply_index
    ):
        raise AuditIntegrityError("END terminal changed its original blocked projection")
    require_original_checkpoint_end_physical_source(physical)
    require_original_checkpoint_end_computation(projected.computation, source=physical, returned=returned)
