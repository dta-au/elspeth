"""Original END source joined to one existing Advisor physical invocation.

This dormant adapter supplies custody and raw-result observation only. It does
not return a gate verdict, complete an owed END tail, or authorize a retry.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from typing import Literal
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.advisor_invocation_outcome import _AdvisorInvocationCapture
from elspeth.web.composer.continuation_checkpoint_end_input import CapturedCheckpointEndInput, require_original_checkpoint_end_input
from elspeth.web.composer.continuation_checkpoint_end_locals import CheckpointEndGateLocals, require_original_checkpoint_end_gate_locals
from elspeth.web.composer.continuation_checkpoint_end_producer import build_checkpoint_end_input
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointPaidIncompleteTerminal
from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkIssuer
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointReturnedResponseObservation,
    observe_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    capture_continuation_runtime_owners,
)
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_quota import ProviderCallCustody, V4PaidIncompleteCommitted
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation
from elspeth.web.composer.provider_sdk_terminal import capture_sdk_terminal_failure
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce
from elspeth.web.required_work import RequiredWorkRole, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPreparedSource, PreparedCheckpointProvider, PreparedCheckpointSource
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, VerifiedSdkTerminal
from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEndPhysicalSource:
    phase_owner: CheckpointPhaseOwner
    gate_locals: CheckpointEndGateLocals
    captured: CapturedContinuationRuntime
    end_input: CapturedCheckpointEndInput
    prepared: CheckpointPreparedSource
    capture: _AdvisorInvocationCapture = field(repr=False)
    detached: RestoredContinuationRuntime = field(repr=False)
    clock: ContinuationActionClock = field(repr=False)


@dataclass(slots=True)
class _OriginalPhysicalSource:
    phase_owner: CheckpointPhaseOwner
    gate_locals: CheckpointEndGateLocals
    captured: CapturedContinuationRuntime
    end_input: CapturedCheckpointEndInput
    prepared: CheckpointPreparedSource
    capture: _AdvisorInvocationCapture
    detached: RestoredContinuationRuntime
    detached_capture: CapturedContinuationRuntime
    clock: ContinuationActionClock
    prepared_request: PreparedCheckpointProvider | None = None
    custody: ProviderCallCustody | None = None
    issuer: CheckpointNoSdkIssuer | None = None
    unbound_refusal: ReferenceType[CheckpointUnboundRefusalPermit] | None = None
    unbound_failure: ReferenceType[ComposerContextUnsupported] | None = None


_issued: WeakKeyDictionary[CheckpointEndPhysicalSource, _OriginalPhysicalSource] = WeakKeyDictionary()
_gate_sources: WeakKeyDictionary[CheckpointEndGateLocals, ReferenceType[CheckpointEndPhysicalSource]] = WeakKeyDictionary()
_UNBOUND_REFUSAL_ISSUER = object()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointUnboundRefusalPermit:
    source: CheckpointEndPhysicalSource
    failure: ComposerContextUnsupported
    _issue_key: InitVar[object]

    def __post_init__(self, _issue_key: object) -> None:
        if _issue_key is not _UNBOUND_REFUSAL_ISSUER:
            raise TypeError("Checkpoint source refusal requires its original private issuer")


class _CheckpointUnboundRefusalObserved(Exception):
    def __init__(self, permit: CheckpointUnboundRefusalPermit) -> None:
        require_original_checkpoint_unbound_refusal(permit)
        super().__init__("checkpoint_source_preparation_refused")
        self.permit = permit


def observe_checkpoint_end_unbound_refusal(
    source: CheckpointEndPhysicalSource, *, failure: ComposerContextUnsupported
) -> CheckpointUnboundRefusalPermit:
    """Issue only at the original request-preparation refusal, before binding."""
    require_original_checkpoint_end_physical_source(source)
    original = _issued[source]
    if (
        type(failure) is not ComposerContextUnsupported
        or failure.reason != "prepared_request_bytes"
        or not source.capture.claimed
        or original.custody is not None
        or original.issuer is not None
        or original.unbound_refusal is not None
    ):
        raise AuditIntegrityError("Checkpoint unbound refusal changed its original pre-admission boundary")
    permit = CheckpointUnboundRefusalPermit(source, failure, _UNBOUND_REFUSAL_ISSUER)
    original.unbound_refusal = ref(permit)
    original.unbound_failure = ref(failure)
    return permit


def require_original_checkpoint_unbound_refusal(permit: CheckpointUnboundRefusalPermit) -> None:
    if type(permit) is not CheckpointUnboundRefusalPermit:
        raise AuditIntegrityError("Checkpoint unbound refusal requires its original nominal permit")
    require_original_checkpoint_end_physical_source(permit.source)
    original = _issued[permit.source]
    if (
        original.unbound_refusal is None
        or original.unbound_refusal() is not permit
        or original.unbound_failure is None
        or original.unbound_failure() is not permit.failure
        or original.custody is not None
        or original.issuer is not None
        or type(permit.failure) is not ComposerContextUnsupported
        or permit.failure.reason != "prepared_request_bytes"
    ):
        raise AuditIntegrityError("Checkpoint unbound refusal lost its original source or failure")


def require_original_checkpoint_end_physical_source(source: CheckpointEndPhysicalSource) -> None:
    original = _issued.get(source) if type(source) is CheckpointEndPhysicalSource else None
    if original is None or any(
        actual is not expected
        for actual, expected in (
            (source.phase_owner, original.phase_owner),
            (source.gate_locals, original.gate_locals),
            (source.captured, original.captured),
            (source.end_input, original.end_input),
            (source.prepared, original.prepared),
            (source.capture, original.capture),
            (source.detached, original.detached),
            (source.clock, original.clock),
        )
    ):
        raise AuditIntegrityError("END physical invocation lost its original source producer")
    require_original_checkpoint_end_gate_locals(source.gate_locals, phase_owner=source.phase_owner)
    chunk = require_original_checkpoint_end_input(source.end_input, phase_source=source.phase_owner.phase_source, captured=source.captured)
    if source.prepared.end_input_ref != chunk.reference:
        raise AuditIntegrityError("END physical source changed its retained gate input")


def require_checkpoint_end_prepared_clock(prepared: PreparedCheckpointProvider) -> ContinuationActionClock:
    """Reprove the original live request and driver clock before SDK admission."""
    if type(prepared) is not PreparedCheckpointProvider:
        raise AuditIntegrityError("END timing requires its original nominal prepared request")
    if type(prepared.physical_source) is not ReferenceType:
        raise AuditIntegrityError("END timing requires its original physical source reference")
    source = prepared.physical_source()
    if source is None:
        raise AuditIntegrityError("END timing lost its original live physical source")
    require_original_checkpoint_end_physical_source(source)
    original = _issued[source]
    phases = source.phase_owner.phases
    clock = original.clock
    if (
        original.prepared_request is not prepared
        or prepared.clock is not clock
        or type(clock) is not ContinuationActionClock
        or phases.action_clock is not clock
        or prepared.running is not phases.work.running
        or prepared.prepared_source is not source.prepared
        or prepared.work.coordinator is not phases.work.coordinator
        or prepared.composer is not phases.service
        or prepared.authority is not phases.authority
        or clock.policy != source.phase_owner.runtime.source.policy.actions.advisor
    ):
        raise AuditIntegrityError("END timing replaced its original request, phase owner or driver clock")
    sql_deadline = phases.work.coordinator.sql_deadline_owner
    if sql_deadline is not None:
        if sql_deadline.running is not prepared.running:
            raise AuditIntegrityError("END timing changed its original SQL owner")
        sql_deadline.require_action_clock(clock)
    return clock


def require_checkpoint_end_detached_source(source: CheckpointEndPhysicalSource) -> None:
    from elspeth.web.composer.continuation_checkpoint_end_detached import require_detached_end_capture

    require_original_checkpoint_end_physical_source(source)
    original = _issued[source]
    if source.detached.source.selected.checkpoint_id != source.prepared.prepared_selected.checkpoint_id:
        raise AuditIntegrityError("END detached source changed its pre-admission selected head")
    require_detached_end_capture(source.detached, expected=original.detached_capture, phase_owner=source.phase_owner)


async def prepare_checkpoint_end_physical_source(
    *, phase_owner: CheckpointPhaseOwner, gate_locals: CheckpointEndGateLocals
) -> CheckpointEndPhysicalSource:
    """Capture the original gate and retain source before its physical attempt."""
    require_original_checkpoint_end_gate_locals(gate_locals, phase_owner=phase_owner)
    prior = _gate_sources.get(gate_locals)
    if prior is not None and prior() is not None:
        raise AuditIntegrityError("END gate already has its original physical source")
    phases = phase_owner.phases
    running = phases.work.running
    sessions = phases.service._sessions_service
    if running is None or type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("END source requires the original running session SQL owner")
    clock = phases.action_clock
    if type(clock) is not ContinuationActionClock or clock.policy != phase_owner.runtime.source.policy.actions.advisor:
        raise AuditIntegrityError("END source lost its original driver action clock")
    sql_deadline = phases.work.coordinator.sql_deadline_owner
    if sql_deadline is not None:
        if sql_deadline.running is not running:
            raise AuditIntegrityError("END source changed its original SQL timing owner")
        sql_deadline.require_action_clock(clock)
    captured = await capture_continuation_runtime_owners(
        phase_owner.runtime, selected=phase_owner.selected, inventory=phase_owner.inventory
    )
    if len(phase_owner.phase_source.completed_children) > 1:
        raise AuditIntegrityError("END physical source exceeded its one original format retry")
    retry_ordinal: Literal[0, 1] = 1 if phase_owner.phase_source.completed_children else 0
    end_input = build_checkpoint_end_input(phase_owner=phase_owner, gate_locals=gate_locals, captured=captured, retry_ordinal=retry_ordinal)
    preparation = PreparedCheckpointSource(
        running=running,
        selected=phase_owner.selected,
        captured_before=captured,
        phase="end",
        pass_index=gate_locals.pass_index,
        work=phases.work,
        composer=phases.service,
        authority=phases.authority,
        end_input=end_input,
    )
    prepared = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: sessions.prepare_v4_checkpoint_source_sync(prepared=preparation),
        expected=CheckpointPreparedSource,
    )
    from elspeth.web.composer.continuation_checkpoint_end_detached import restore_prepared_end_runtime

    detached, detached_capture = await restore_prepared_end_runtime(phase_owner=phase_owner, prepared=prepared, captured=captured)
    capture = _AdvisorInvocationCapture()
    source = CheckpointEndPhysicalSource(phase_owner, gate_locals, captured, end_input, prepared, capture, detached, clock)
    _issued[source] = _OriginalPhysicalSource(
        phase_owner, gate_locals, captured, end_input, prepared, capture, detached, detached_capture, clock
    )
    _gate_sources[gate_locals] = ref(source)
    require_original_checkpoint_end_physical_source(source)
    return source


def bind_checkpoint_end_physical_request(
    source: CheckpointEndPhysicalSource, *, custody: ProviderCallCustody, prepared_input: PreparedProviderInput
) -> None:
    """Bind actual prepared request bytes/model to the original unentered SDK."""
    require_original_checkpoint_end_physical_source(source)
    original = _issued[source]
    owner = source.phase_owner
    running = owner.phases.work.running
    if (
        type(custody) is not ProviderCallCustody
        or type(prepared_input) is not PreparedProviderInput
        or prepared_input.role != "advisor"
        or running is None
        or original.custody is not None
        or original.prepared_request is not None
        or original.unbound_refusal is not None
        or not source.capture.claimed
        or custody.required_work.coordinator is not owner.phases.work.coordinator
        or custody.required_work.transition_ordinal != owner.phases.work.transition_ordinal
        or custody.required_work.role is not RequiredWorkRole.TURN
        or custody.context is not owner.runtime.bindings.session_operation_context
    ):
        raise AuditIntegrityError("END physical request changed its original prepared invocation")
    prepared = PreparedCheckpointProvider(
        running=running,
        prepared_source=source.prepared,
        work=custody.required_work,
        composer=owner.phases.service,
        authority=owner.phases.authority,
        retry_ordinal=1 if owner.phase_source.completed_children else 0,
        request_sha256=stable_hash(prepared_input.evidence.messages),
        requested_model=prepared_input.model,
        env_ref_names=owner.runtime.env_ref_names,
        publication_binding=source.gate_locals.publication_review.binding,
        clock=source.clock,
        physical_source=ref(source),
    )
    original.prepared_request = prepared
    clock = require_checkpoint_end_prepared_clock(prepared)
    issuer = CheckpointNoSdkIssuer(prepared, clock=clock)
    custody.bind_continuation_checkpoint_v4(prepared, issuer)
    original.custody, original.issuer = custody, issuer

    async def finalize_incomplete(
        incomplete: V4ProviderResultIncomplete, required_work: RequiredWorkTicket
    ) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:
        require_original_checkpoint_end_physical_source(source)
        if original.custody is not custody or custody.continuation_checkpoint_v4 is not prepared:
            raise AuditIntegrityError("END incomplete finalizer lost its original physical source")
        admission = custody.continuation_checkpoint_v4_admission
        if admission is None:
            raise AuditIntegrityError("END incomplete finalizer has no original admitted attempt")
        sessions = owner.phases.service._sessions_service
        if type(sessions) is not SessionServiceImpl:
            raise AuditIntegrityError("END incomplete finalizer lost its original SQL owner")
        return await sessions.finalize_v4_continuation_checkpoint_incomplete(
            prepared=prepared,
            admission=admission,
            custody=custody,
            incomplete=incomplete,
            required_work=required_work,
        )

    custody.bind_v4_returned_incomplete_finalizer(finalize_incomplete)

    async def finalize_sdk(
        observation: V4SdkFailureObservation, required_work: RequiredWorkTicket
    ) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:
        require_original_checkpoint_end_physical_source(source)
        if original.custody is not custody or custody.continuation_checkpoint_v4 is not prepared:
            raise AuditIntegrityError("END SDK finalizer lost its original physical source")
        admission = custody.continuation_checkpoint_v4_admission
        if admission is None or custody.attempt is not admission.attempt or observation.attempt is not admission.attempt:
            raise AuditIntegrityError("END SDK finalizer lost its original admitted paid attempt")
        sessions = owner.phases.service._sessions_service
        if type(sessions) is not SessionServiceImpl:
            raise AuditIntegrityError("END SDK finalizer lost its original SQL owner")
        deadline_owner = owner.phases.work.coordinator.sql_deadline_owner
        if deadline_owner is not None:
            deadline_owner.begin_settlement()
        outcome = await sessions.finalize_v4_sdk_terminal(observation=observation, custody=custody, required_work=required_work)
        owner.phases.work.coordinator.validate_provider_settlement_sql_outcome(ticket=required_work, actual_outcome=outcome)
        return outcome

    custody.bind_v4_sdk_finalizer(finalize_sdk)
    from elspeth.web.continuation_retention_memo import finish_v4_preparation_memo

    try:
        clock.finish_preparation()
    finally:
        finish_v4_preparation_memo(clock)


def checkpoint_end_physical_custody(source: CheckpointEndPhysicalSource) -> ProviderCallCustody:
    require_original_checkpoint_end_physical_source(source)
    custody = _issued[source].custody
    if custody is None:
        raise AuditIntegrityError("END physical source has no original prepared custody")
    return custody


def observe_checkpoint_end_sdk_companion(source: CheckpointEndPhysicalSource, failure: BaseException) -> SdkTerminalFailure | None:
    """Use only this physical source's retained custody for exceptional SDK results."""
    require_original_checkpoint_end_physical_source(source)
    custody = _issued[source].custody
    if custody is None:
        return None
    return capture_sdk_terminal_failure(failure, custody=custody)


def observe_checkpoint_end_paid_incomplete_terminal(
    control: V4PaidIncompleteCommitted, *, phase_owner: CheckpointPhaseOwner, gate_locals: CheckpointEndGateLocals
) -> EndCheckpointPaidIncompleteTerminal:
    """Use the exact gate's original custody, never the exception DATA alone."""
    reference = _gate_sources.get(gate_locals)
    source = None if reference is None else reference()
    if source is None or source.phase_owner is not phase_owner or source.gate_locals is not gate_locals:
        raise AuditIntegrityError("END paid-incomplete control lost its original gate source")
    custody = checkpoint_end_physical_custody(source)
    finalized = custody.require_v4_paid_incomplete_committed(control)
    return EndCheckpointPaidIncompleteTerminal(finalized.verified, finalized.deferred_cancellations)


def note_checkpoint_end_physical_failure(source: CheckpointEndPhysicalSource, error: BaseException) -> None:
    """Keep the original failure available to the separately admitted no-SDK owner."""
    require_original_checkpoint_end_physical_source(source)
    original = _issued[source]
    if original.issuer is not None and original.custody is not None:
        original.issuer.note_original_failure(original.custody, error)


def checkpoint_end_physical_no_sdk_issuer(source: CheckpointEndPhysicalSource) -> CheckpointNoSdkIssuer:
    require_original_checkpoint_end_physical_source(source)
    issuer = _issued[source].issuer
    if issuer is None:
        raise AuditIntegrityError("END physical source has no original no-SDK issuer")
    return issuer


def observe_checkpoint_end_physical_return(
    source: CheckpointEndPhysicalSource,
) -> CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation:
    """Observe the original complete result; SQL still owes its full disposition."""
    custody = checkpoint_end_physical_custody(source)
    context = source.phase_owner.runtime.bindings.session_operation_context
    policy = source.phase_owner.runtime.source.policy
    if custody.attempt is None or context is None or type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("END physical return lost its original attempt or operation")
    return observe_v4_checkpoint_returned_response(
        source.capture.snapshot(),
        custody=custody,
        policy=policy,
        phase="end",
        trigger=ADVISOR_TRIGGER_DETERMINISTIC_END,
        attempt=custody.attempt,
        context=context,
        env_ref_names=source.phase_owner.runtime.env_ref_names,
    )
