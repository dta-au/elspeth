"""Reprove an owed END result from durable DATA after owner loss.

This read grants no SDK call or SQL mutation. Its source can feed a separately
admitted cold consequence writer; warm weak issuers are deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from weakref import WeakKeyDictionary

from pydantic import TypeAdapter

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import advisor_policy
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict, _parse_advisor_checkpoint_guidance
from elspeth.web.composer.composition_completion import _FINALIZATION_AUTO_SURFACEABLE_KINDS, _advance_advisor_review_state
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
from elspeth.web.composer.continuation_checkpoint_end_computation import EndGateComputation, compute_end_gate
from elspeth.web.composer.continuation_checkpoint_end_input import CheckpointEndInputSnapshot, CheckpointOrdinaryEndCaller
from elspeth.web.composer.continuation_codec import decode_retained_physical_audit_call, encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, restore_checkpoint_projection_runtime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import _decode_owned
from elspeth.web.composer.turn_effects import AdvisorPublicationReview
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_owed import VerifiedCheckpointEndOwed, read_v4_checkpoint_end_owed_on_connection
from elspeth.web.sessions.locking import locked_session_transaction


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdCheckpointEndConsequenceSource:
    """One original H, unchanged pre-call graph and restored complete audit."""

    owed: VerifiedCheckpointEndOwed
    end_input: CheckpointEndInputSnapshot
    selected: SelectedContinuationState
    runtime: RestoredContinuationRuntime


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdCheckpointEndGate:
    source: ColdCheckpointEndConsequenceSource
    computation: EndGateComputation


_issued_sources: WeakKeyDictionary[ColdCheckpointEndConsequenceSource, tuple[object, ...]] = WeakKeyDictionary()
_issued_gates: WeakKeyDictionary[ColdCheckpointEndGate, ColdCheckpointEndConsequenceSource] = WeakKeyDictionary()


def require_original_cold_checkpoint_end_source(source: ColdCheckpointEndConsequenceSource) -> None:
    original = _issued_sources.get(source) if type(source) is ColdCheckpointEndConsequenceSource else None
    if original is None or any(
        actual is not expected
        for actual, expected in zip((source.owed, source.end_input, source.selected, source.runtime), original, strict=True)
    ):
        raise AuditIntegrityError("cold END source lacks its original current H read")


def require_original_cold_checkpoint_end_gate(gate: ColdCheckpointEndGate) -> None:
    source = _issued_gates.get(gate) if type(gate) is ColdCheckpointEndGate else None
    if source is None or source is not gate.source or type(gate.computation) is not EndGateComputation:
        raise AuditIntegrityError("cold END gate lacks its original current H computation")
    require_original_cold_checkpoint_end_source(source)


_MESSAGE = TypeAdapter(_AdmittedAssistantMessage)
_REVIEW = TypeAdapter(_AdvisorReviewState)
_PUBLICATION = TypeAdapter(AdvisorPublicationReview)


def _read_current_owed(*, phases: object, selected: SelectedContinuationState, names: frozenset[str]) -> VerifiedCheckpointEndOwed:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("cold END read requires its current actual phase owner")
    running = phases.work.running
    if running is None:
        raise AuditIntegrityError("cold END read lost its claimed operation")
    root = selected.stack
    if len(root.pending_settlement_ids) != 1:
        raise AuditIntegrityError("cold END has no unique outstanding physical attempt")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(phases.authority._engine, sid) as conn:
        current = load_snapshot(conn, session_id=sid, operation_id=operation)
        if current.checkpoint_id != selected.checkpoint_id or current.payload_json != encode_continuation_snapshot(root):
            raise AuditIntegrityError("cold END selected a changed current H")
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=root.pending_settlement_ids[0],
            names=names,
        )
        if owed is None or owed.selected != current:
            raise AuditIntegrityError("cold END lost its original complete pending H")
        return owed


async def read_cold_checkpoint_end_consequence_source(
    *, phases: object, selected: SelectedContinuationState
) -> ColdCheckpointEndConsequenceSource:
    """Restore H's pre-call owners, then append only its proved received audit."""
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("cold END source requires its actual phase and selected owners")
    running = phases.work.running
    if running is None:
        raise AuditIntegrityError("cold END source lost its original claimed operation")
    inventory = await materialize_continuation_env_inventory(
        advisor=phases.service._advisor_checkpoint,
        authority=phases.authority,
        selected=selected,
        required_work=phases.work,
    )
    owed = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=partial(_read_current_owed, phases=phases, selected=selected, names=inventory.names),
        expected=VerifiedCheckpointEndOwed,
    )
    end_input = owed.end_input
    if end_input is None or owed.phase_source is None or end_input.retry_ordinal != owed.retry_ordinal:
        raise AuditIntegrityError("cold END H lacks its exact retained original gate DATA")
    runtime = await restore_checkpoint_projection_runtime(
        service=phases.service,
        authority=phases.authority,
        selected=selected,
        inventory=inventory,
        required_work=phases.work,
        provider_owner=phases.provider_owner,
    )
    frame = selected.stack.frames[0]
    audit_chunk = tuple(chunk for chunk in selected.chunks if type(frame) is ComposeFrame and chunk.reference == frame.audit_buffer)
    if (
        type(frame) is not ComposeFrame
        or frame.state.digest != end_input.state_sha256
        or len(audit_chunk) != 1
        or runtime.live.recorder.snapshot_for_continuation(drained=runtime.drained) != decode_recorder_snapshot(audit_chunk[0])
        or stable_hash(runtime.live.state.to_dict()) != end_input.state_sha256
        or runtime.source.selected != selected
    ):
        raise AuditIntegrityError("cold END restored graph or recorder differs from its retained H codec state")
    call = decode_retained_physical_audit_call(owed.response)
    if call.call_id != owed.attempt_id or call.messages_hash != owed.request_sha256:
        raise AuditIntegrityError("cold END restored another physical result")
    runtime.live.recorder.restore_received_llm_call(call)
    source = ColdCheckpointEndConsequenceSource(owed, end_input, selected, runtime)
    _issued_sources[source] = (owed, end_input, selected, runtime)
    return source


async def compute_first_cold_checkpoint_end_gate(*, source: ColdCheckpointEndConsequenceSource) -> ColdCheckpointEndGate:
    """Recompute a first usable or final complete END result from H."""
    from elspeth.web.composer.continuation_codec import decode_continuation_response

    require_original_cold_checkpoint_end_source(source)
    if source.owed.retry_ordinal not in (0, 1) or (source.owed.retry_ordinal == 1) != (source.owed.retry_history is not None):
        raise AuditIntegrityError("cold END gate lost its bounded original retry chain")
    gate = source.end_input
    response = decode_continuation_response(source.owed.response)
    content = response.message.content
    if type(content) is not str:
        raise AuditIntegrityError("cold END first gate lost its complete returned text")
    verdict = _parse_advisor_checkpoint_guidance(content, phase="end")
    if not verdict.ok:
        if source.owed.retry_ordinal == 0:
            raise AuditIntegrityError("cold END malformed first result needs its bounded retry")
        verdict = AdvisorCheckpointVerdict(
            ok=False,
            blocking=False,
            failure_class="malformed",
            findings_text=advisor_policy.ADVISOR_MALFORMED_USER_DETAIL,
        )
    runtime = source.runtime
    assistant = _decode_owned(_MESSAGE, gate.assistant_message_json)
    review_state = _decode_owned(_REVIEW, gate.review_state_json)
    initial_review_state = _decode_owned(_REVIEW, gate.initial_review_state_json)
    publication = _decode_owned(_PUBLICATION, gate.publication_review_json)
    source_operation_id = (
        source.owed.retry_history.owed.new_hold.session_operation_id
        if source.owed.retry_history is not None
        else source.owed.new_hold.session_operation_id
    )
    observed = await runtime.materialization.service._completion._interpretation_surfacing.observe_pending_interpretation_review_sites(
        runtime.live.state, session_id=runtime.bindings.session_id
    )
    if (
        observed.source_sha256 != gate.orphan_source_sha256
        or observed.missing_sites != gate.orphaned_precheck
        or runtime.live.turn_evidence.review_candidate_for_source_operation(
            source_operation_id=source_operation_id,
            candidate=assistant.content or "",
            request=runtime.bindings.message,
            current_state=runtime.live.state,
        )
        != publication
    ):
        raise AuditIntegrityError("cold END gate changed its original pending review or publication source")
    if verdict.ok:
        verdict = replace(verdict, publication_binding=publication.binding)
    advanced = _advance_advisor_review_state(
        review_state,
        verdict=verdict,
        evidence_hash=publication.semantic_repair_key,
        pass_index=gate.pass_index,
    )
    stalled = (
        initial_review_state.completed_passes > 0
        and not initial_review_state.successful_mutating_actions
        and initial_review_state.previous_evidence_hash == publication.semantic_repair_key
    )
    computation = compute_end_gate(
        verdict=verdict,
        publication_review=publication,
        passes_delta=gate.passes_delta + 1,
        review_state=advanced,
        stalled_repair=stalled,
        orphaned_precheck=observed.missing_sites,
        auto_surfaceable_kinds=_FINALIZATION_AUTO_SURFACEABLE_KINDS,
        state=runtime.live.state,
        turn_evidence=runtime.live.turn_evidence,
        assistant_message=assistant,
        user_message=runtime.bindings.message,
        advisor_checkpoint_passes_used=gate.advisor_checkpoint_passes_used,
        max_passes=gate.max_passes,
        allow_repair_continue=gate.allow_repair_continue,
        dialogue_length=len(runtime.live.llm_messages) if type(gate.caller) is CheckpointOrdinaryEndCaller else None,
        original_source_operation_id=source_operation_id,
    )
    issued = ColdCheckpointEndGate(source, computation)
    _issued_gates[issued] = source
    return issued
