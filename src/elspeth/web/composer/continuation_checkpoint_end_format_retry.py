"""One format retry after the complete original END result is durable.

The original malformed call is settled and retained before a fresh phase/gate
is captured. Only this issued source selects the existing format re-prompt;
neither a decoded receipt nor an SDK failure grants another paid call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from pydantic import TypeAdapter

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.continuation_audit_buffer import RECORDER_CODEC
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness, ContinuationMandatoryCaptureOverflow
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    read_cold_checkpoint_end_consequence_source,
)
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointPaidOverflowTerminal
from elspeth.web.composer.continuation_checkpoint_end_input import (
    _GATES,
    _MESSAGE,
    _ORPHANS,
    _PUBLICATION,
    _REVIEW,
    _VALIDATION,
    CheckpointOrdinaryEndCaller,
)
from elspeth.web.composer.continuation_checkpoint_end_locals import (
    CheckpointEndGateLocals,
    EndProofDiagnostic,
    OrdinaryEndCallerLocals,
    SourceProofEndCallerLocals,
    capture_checkpoint_end_gate_locals,
    require_original_checkpoint_end_gate_locals,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import require_original_checkpoint_end_physical_source
from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner, prepare_checkpoint_phase_owner
from elspeth.web.composer.continuation_codec import decode_retained_physical_audit_call, encode_continuation_snapshot
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_runtime import _registered_read
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.continuation_tool_batch import _decode_owned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import _require_current_continuation_running, load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_retry import (
    ColdProjectedEndFormatRetry,
    ColdProjectedEndFormatRetryOverflow,
    VerifiedEndFormatRetry,
    project_cold_end_format_retry_on_connection,
    read_end_format_retry_on_connection,
    read_retry_ready_action_source_on_connection,
    retain_cold_end_format_retry_on_connection,
    retain_end_format_retry_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEndFormatRetrySource:
    proof: VerifiedEndFormatRetry
    phase_owner: CheckpointPhaseOwner
    gate_locals: CheckpointEndGateLocals


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEndRetryPublicationSource:
    proof: VerifiedEndFormatRetry
    phase_owner: CheckpointPhaseOwner


_issued: WeakKeyDictionary[CheckpointEndFormatRetrySource, tuple[VerifiedEndFormatRetry, CheckpointPhaseOwner, CheckpointEndGateLocals]] = (
    WeakKeyDictionary()
)
_issued_publications: WeakKeyDictionary[CheckpointEndRetryPublicationSource, tuple[VerifiedEndFormatRetry, CheckpointPhaseOwner]] = (
    WeakKeyDictionary()
)


@dataclass(frozen=True, slots=True)
class _RetryReadyProbe:
    proof: VerifiedEndFormatRetry | None


@dataclass(frozen=True, slots=True)
class _ColdRetryProjectionResult:
    value: ColdProjectedEndFormatRetry | ColdProjectedEndFormatRetryOverflow


_DIAGNOSTICS = TypeAdapter(tuple[EndProofDiagnostic, ...])


def require_original_checkpoint_end_format_retry(source: CheckpointEndFormatRetrySource) -> None:
    original = _issued.get(source) if type(source) is CheckpointEndFormatRetrySource else None
    if original is None or any(
        actual is not expected for actual, expected in zip((source.proof, source.phase_owner, source.gate_locals), original, strict=True)
    ):
        raise AuditIntegrityError("END format retry requires its original SQL and gate source")
    owner, proof = source.phase_owner, source.proof
    if (
        owner.phase_source.current != proof.selected
        or owner.phase_source.source != proof.owed.phase_source
        or owner.phase_source.completed_children != (proof.marker.effect_key,)
        or owner.runtime.env_ref_names != frozenset(proof.marker.end_env_ref_names)
        or source.gate_locals.pass_index != proof.marker.pass_index
    ):
        raise AuditIntegrityError("END format retry changed its original pass or retained phase")
    require_original_checkpoint_end_gate_locals(source.gate_locals, phase_owner=owner)


def require_original_checkpoint_end_retry_publication(
    source: CheckpointEndRetryPublicationSource, *, phase_owner: CheckpointPhaseOwner
) -> str:
    original = _issued_publications.get(source) if type(source) is CheckpointEndRetryPublicationSource else None
    if original is None or source.proof is not original[0] or source.phase_owner is not original[1] or phase_owner is not original[1]:
        raise AuditIntegrityError("END retry publication lacks its original issued SQL and phase owner")
    proof = source.proof
    owed = proof.owed
    if (
        phase_owner.phases.runtime is not phase_owner.runtime
        or phase_owner.runtime.source.selected != phase_owner.selected
        or phase_owner.phase_source.current != proof.selected
        or phase_owner.phase_source.source != owed.phase_source
        or phase_owner.phase_source.completed_children != (proof.marker.effect_key,)
        or phase_owner.selected.checkpoint_id != proof.selected.checkpoint_id
        or proof.marker.attempt_id != owed.attempt_id
        or proof.marker.effect_key != owed.effect_key
        or proof.marker.outer_effect_key != owed.outer_effect_key
        or proof.marker.end_input_ref != owed.end_input_ref
        or not owed.new_hold.session_operation_id
    ):
        raise AuditIntegrityError("END retry publication changed its original H or current phase")
    return owed.new_hold.session_operation_id


def _issue_retry_publication_source(proof: VerifiedEndFormatRetry, owner: CheckpointPhaseOwner) -> CheckpointEndRetryPublicationSource:
    value = CheckpointEndRetryPublicationSource(proof, owner)
    _issued_publications[value] = (proof, owner)
    require_original_checkpoint_end_retry_publication(value, phase_owner=owner)
    return value


def _project_first_retry_cost(*, phases: object, proof: VerifiedEndFormatRetry, call: object) -> None:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(call) is not ComposerLLMCall:
        raise AuditIntegrityError("END retry cost needs its exact original live owner and retained call")
    budget = phases.provider_owner.cost_budget
    accountable = call.cost_response_accountable
    if (
        budget is None
        or type(accountable) is not bool
        or proof.marker.attempt_id != call.call_id
        or budget.snapshot().physical_calls != len(budget.snapshot().charges) + 1
        or any(charge.call_id == call.call_id for charge in budget.snapshot().charges)
    ):
        raise AuditIntegrityError("END retry lost its original unsettled cost projection")
    budget.consume_settled(call, response_accountable=accountable)


async def restore_checkpoint_end_format_retry_source(
    *, phases: object, selected: SelectedContinuationState
) -> CheckpointEndFormatRetrySource | None:
    """Reissue one retry gate from the current retained marker and real runtime.

    A decoded marker alone grants no retry. The ordinary recovered-event chain,
    original paid consequence, current head, original gate input and actual
    restored phase owner are independently proved before the weak issue.
    """
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("END retry restore requires its current phase and selected owners")
    running = phases.work.running
    if running is None:
        raise AuditIntegrityError("END retry restore lost its claimed operation")
    sid, operation = str(running.claim.session_id), running.claim.operation_id

    def probe() -> _RetryReadyProbe:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            _require_current_continuation_running(conn, running)
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            if current.checkpoint_id != selected.checkpoint_id or current.payload_json != encode_continuation_snapshot(selected.stack):
                raise AuditIntegrityError("END retry restore selected another current checkpoint")
            return _RetryReadyProbe(
                read_retry_ready_action_source_on_connection(conn, session_id=sid, operation_id=operation, selected=current)
            )

    observed = await _registered_read(phases.work, _RetryReadyProbe, probe)
    proof = observed.proof
    if proof is None:
        return None
    gate_input = proof.owed.end_input
    if (
        gate_input is None
        or type(gate_input.caller) is not CheckpointOrdinaryEndCaller
        or gate_input.retry_ordinal != 0
        or gate_input.pass_index != proof.marker.pass_index
        or gate_input.outer_effect_key != proof.marker.outer_effect_key
        or proof.owed.phase_source is None
        or gate_input.original_checkpoint_id != proof.owed.phase_source.checkpoint_id
    ):
        raise AuditIntegrityError("END retry restore has no supported original ordinary gate")
    runtime, _inventory = await phases.current(selected)
    owner = await prepare_checkpoint_phase_owner(
        phases,
        runtime,
        selected,
        superseded_reply_index=gate_input.superseded_reply_index,
        elide_advisor_exchange=gate_input.elide_advisor_exchange,
    )
    if (
        owner.phase_source.current != proof.selected
        or owner.phase_source.completed_children != (proof.marker.effect_key,)
        or runtime.env_ref_names != frozenset(proof.marker.end_env_ref_names)
    ):
        raise AuditIntegrityError("END retry restore changed its original physical or credential phase")
    surfacing = phases.service._interpretation_surfacing
    orphan = await surfacing.observe_pending_interpretation_review_sites(runtime.live.state, session_id=sid)
    if (
        orphan.source_sha256 != gate_input.orphan_source_sha256
        or orphan.missing_sites != gate_input.orphaned_precheck
        or _ORPHANS.dump_python(orphan.events, mode="json")
        != _ORPHANS.dump_python(_decode_owned(_ORPHANS, gate_input.orphan_events_json), mode="json")
        or stable_hash(runtime.live.state.to_dict()) != gate_input.state_sha256
    ):
        raise AuditIntegrityError("END retry restore changed its original pending interpretation cohort")
    caller = gate_input.caller
    restored_caller = OrdinaryEndCallerLocals(
        proof_runtime_preflight=(
            _decode_owned(_VALIDATION, caller.proof_runtime_preflight_json) if caller.proof_runtime_preflight_json is not None else None
        ),
        proof_repair_action=caller.proof_repair_action,
        proof_repair_diagnostics=(
            _decode_owned(_DIAGNOSTICS, caller.proof_repair_diagnostics_json) if caller.proof_repair_diagnostics_json is not None else None
        ),
        confirmed_stop=caller.confirmed_stop,
    )
    gate = capture_checkpoint_end_gate_locals(
        CheckpointEndGateLocals(
            assistant_message=_decode_owned(_MESSAGE, gate_input.assistant_message_json),
            runtime_preflight=(
                _decode_owned(_VALIDATION, gate_input.runtime_preflight_json) if gate_input.runtime_preflight_json is not None else None
            ),
            review_state=_decode_owned(_REVIEW, gate_input.review_state_json),
            initial_review_state=_decode_owned(_REVIEW, gate_input.initial_review_state_json),
            publication_review=_decode_owned(_PUBLICATION, gate_input.publication_review_json),
            completion_gates=(
                _decode_owned(_GATES, gate_input.completion_gates_json) if gate_input.completion_gates_json is not None else None
            ),
            advisor_checkpoint_passes_used=gate_input.advisor_checkpoint_passes_used,
            passes_delta=gate_input.passes_delta,
            pass_index=gate_input.pass_index,
            max_passes=gate_input.max_passes,
            allow_repair_continue=gate_input.allow_repair_continue,
            orphan_observation=orphan,
            caller=restored_caller,
            current_state_id=runtime.live.current_state_id,
            initial_version=runtime.live.initial_version,
            repair_turns_used=runtime.live.repair_turns_used,
            persisted_assistant_message_id=runtime.live.persisted_assistant_message_id,
            persisted_assistant_content=runtime.live.persisted_assistant_content,
            persisted_tool_call_turn=runtime.live.persisted_tool_call_turn,
        ),
        phase_owner=owner,
        state=runtime.live.state,
        evidence=runtime.live.turn_evidence,
        surfacing=surfacing,
        session_id=sid,
        request=runtime.bindings.message,
        dialogue=runtime.live.llm_messages,
        format_retry_publication_source=_issue_retry_publication_source(proof, owner),
    )
    issued = CheckpointEndFormatRetrySource(proof, owner, gate)
    _issued[issued] = (proof, owner, gate)
    require_original_checkpoint_end_format_retry(issued)
    return issued


async def settle_cold_checkpoint_end_format_retry(
    *, phases: object, selected: SelectedContinuationState
) -> CheckpointEndFormatRetrySource | EndCheckpointPaidOverflowTerminal:
    """Close a current malformed H, then reissue the one next ordinal gate."""
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("cold END retry requires its current selected phase")
    running = phases.work.running
    sessions = phases.service._sessions_service
    if running is None or type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("cold END retry lost its original session owner")
    source = await read_cold_checkpoint_end_consequence_source(phases=phases, selected=selected)
    sid = str(running.claim.session_id)

    def project() -> _ColdRetryProjectionResult:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            return _ColdRetryProjectionResult(project_cold_end_format_retry_on_connection(conn, service=sessions, source=source))

    projected = (await _registered_read(phases.work, _ColdRetryProjectionResult, project)).value

    async def settle_overflow(
        failure: ContinuationRetainedCapacityExceeded | ColdProjectedEndFormatRetryOverflow,
        *,
        capacity_projection: ColdProjectedEndFormatRetry | None = None,
    ) -> EndCheckpointPaidOverflowTerminal:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=lambda: sessions.settle_cold_v4_checkpoint_end_owed_overflow_sync(
                cold_source=source, failure=failure, capacity_projection=capacity_projection
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        phases.latest_snapshot = settled.terminal
        return EndCheckpointPaidOverflowTerminal(settled)

    if type(projected) is ColdProjectedEndFormatRetryOverflow:
        return await settle_overflow(projected)
    if type(projected) is not ColdProjectedEndFormatRetry or projected.source is not source:
        raise AuditIntegrityError("cold END retry lost its issued exact prospective source")

    def retain() -> VerifiedEndFormatRetry:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            return retain_cold_end_format_retry_on_connection(conn, service=sessions, source=source)

    try:
        proof = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=retain,
            expected=VerifiedEndFormatRetry,
        )
    except ContinuationRetainedCapacityExceeded as excess:
        return await settle_overflow(excess, capacity_projection=projected)
    _project_first_retry_cost(
        phases=phases,
        proof=proof,
        call=decode_retained_physical_audit_call(proof.owed.response),
    )
    phases.latest_snapshot = proof.selected
    next_selected = await load_selected_continuation(
        authority=phases.authority, running=running, selected=proof.selected, required_work=phases.work
    )
    issued = await restore_checkpoint_end_format_retry_source(phases=phases, selected=next_selected)
    if issued is None:
        raise AuditIntegrityError("cold END retry lost its freshly committed marker")
    return issued


async def prepare_checkpoint_end_format_retry(
    first: V4EndFirstRetained,
) -> CheckpointEndFormatRetrySource | EndCheckpointPaidOverflowTerminal:
    """Close one malformed H, then capture the actual next gate on that cut."""
    from elspeth.web.composer.continuation_checkpoint_end_first_return import V4EndFirstRetained

    if type(first) is not V4EndFirstRetained or first.prepared.retry_ordinal != 0:
        raise AuditIntegrityError("END format retry requires its first original complete result")
    physical = first.physical
    require_original_checkpoint_end_physical_source(physical)
    before = physical.phase_owner
    phases = before.phases
    running = phases.work.running
    sessions = phases.service._sessions_service
    if (
        running is None
        or type(sessions) is not SessionServiceImpl
        or first.prepared.running is not running
        or phases.runtime is not before.runtime
    ):
        raise AuditIntegrityError("END format retry lost its original running runtime")
    sid, operation = str(running.claim.session_id), running.claim.operation_id

    def retain() -> VerifiedEndFormatRetry:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            return retain_end_format_retry_on_connection(
                conn,
                service=sessions,
                prepared=first.prepared,
                owed_admission=first.owed,
                returned=first.returned,
            )

    async def settle_overflow(
        failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
    ) -> EndCheckpointPaidOverflowTerminal:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=lambda: sessions.settle_v4_checkpoint_end_owed_overflow_sync(
                prepared=first.prepared, owed=first.owed, returned=first.returned, failure=failure
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        return EndCheckpointPaidOverflowTerminal(settled)

    try:
        proof = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=retain,
            expected=VerifiedEndFormatRetry,
        )
    except ContinuationRetainedCapacityExceeded as excess:
        return await settle_overflow(excess)
    except AuditIntegrityError as error:
        cause = error.__cause__
        if type(cause) is not ContinuationMandatoryCaptureOverflow or cause.witness.codec not in (RECORDER_CODEC, "composer.evidence.v2"):
            raise
        return await settle_overflow(cause.witness)
    # V4 custody deliberately defers ordinary cost settlement until its SQL
    # consequence is durable. Project this exact proved call before restoring
    # the next runtime; its cost owner must agree with the complete ledger.
    budget = phases.provider_owner.cost_budget
    call = first.returned.audit_call
    accountable = call.cost_response_accountable
    if (
        budget is None
        or type(accountable) is not bool
        or before.runtime.bindings.provider_owner is not phases.provider_owner
        or proof.marker.attempt_id != call.call_id
        or budget.snapshot().physical_calls != len(budget.snapshot().charges) + 1
        or any(charge.call_id == call.call_id for charge in budget.snapshot().charges)
    ):
        raise AuditIntegrityError("END retry lost its original unsettled cost projection")
    budget.consume_settled(call, response_accountable=accountable)
    phases.latest_snapshot = proof.selected
    selected = await load_selected_continuation(
        authority=phases.authority, running=running, selected=proof.selected, required_work=phases.work
    )
    runtime, _inventory = await phases.current(selected)
    owner = await prepare_checkpoint_phase_owner(
        phases,
        runtime,
        selected,
        superseded_reply_index=before.superseded_reply_index,
        elide_advisor_exchange=before.elide_advisor_exchange,
    )

    def reprove() -> VerifiedEndFormatRetry:
        with locked_session_transaction(phases.authority._engine, sid) as conn:
            _require_current_continuation_running(conn, running)
            current = load_snapshot(conn, session_id=sid, operation_id=operation)
            if current != proof.selected or current.payload_json != encode_continuation_snapshot(selected.stack):
                raise AuditIntegrityError("END format retry replaced its actual current checkpoint")
            return read_end_format_retry_on_connection(
                conn, session_id=sid, operation_id=operation, selected=current, names=runtime.env_ref_names
            )

    proof = await _registered_read(phases.work, VerifiedEndFormatRetry, reprove)
    surfacing = phases.service._interpretation_surfacing
    observation = await surfacing.observe_pending_interpretation_review_sites(runtime.live.state, session_id=sid)
    previous = physical.gate_locals
    if observation.source_sha256 != previous.orphan_observation.source_sha256:
        raise AuditIntegrityError("END format retry changed its original pending interpretation cohort")
    dialogue = None
    if type(previous.caller) is OrdinaryEndCallerLocals:
        dialogue = runtime.live.llm_messages
    elif type(previous.caller) is SourceProofEndCallerLocals:
        dialogue = []
    gate = capture_checkpoint_end_gate_locals(
        replace(previous, orphan_observation=observation),
        phase_owner=owner,
        state=runtime.live.state,
        evidence=runtime.live.turn_evidence,
        surfacing=surfacing,
        session_id=sid,
        request=runtime.bindings.message,
        dialogue=dialogue,
        format_retry_publication_source=_issue_retry_publication_source(proof, owner),
    )
    source = CheckpointEndFormatRetrySource(proof, owner, gate)
    _issued[source] = (proof, owner, gate)
    require_original_checkpoint_end_format_retry(source)
    return source
