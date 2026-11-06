"""Original iteration EARLY source and one driver-clock-bound physical request.

Issuance requires the actual empty-to-nonempty iteration observation. Immutable
request DATA cannot recreate this weak-issued live authority after a restart.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.readonly_json import JsonValue
from elspeth.web.composer.advisor_invocation_outcome import _AdvisorInvocationCapture
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkIssuer
from elspeth.web.composer.continuation_checkpoint_phase import CheckpointPhaseOwner, prepare_checkpoint_phase_owner
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    _CapturedRuntimeData,
    capture_continuation_runtime_data,
)
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.required_work import RequiredWorkRole, RequiredWorkSource
from elspeth.web.sessions.continuation_checkpoint_early_retry import VerifiedEarlyFormatRetry
from elspeth.web.sessions.continuation_checkpoint_early_source import (
    CapturedCheckpointEarlyInput,
    CheckpointEarlyGuardCommitment,
    CheckpointEarlyInput,
    encode_checkpoint_early_input,
)
from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPreparedSource, PreparedCheckpointProvider, PreparedCheckpointSource
from elspeth.web.sessions.service import SessionServiceImpl

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _DispatchOutcome, _PersistOutcome
    from elspeth.web.composer.continuation_checkpoint_early_adoption import _SettledEarly
    from elspeth.web.composer.continuation_checkpoint_observation import (
        CheckpointIncompleteResponseObservation,
        CheckpointReturnedResponseObservation,
    )
    from elspeth.web.composer.continuation_finished_batch_data import ColdPostBatchPersistData
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEarlyPhysicalSource:
    phase_owner: CheckpointPhaseOwner
    dispatch: _DispatchOutcome = field(repr=False)
    persist: _PersistOutcome | ColdPostBatchPersistData = field(repr=False)
    pre_batch_state: CompositionState = field(repr=False)
    captured: CapturedContinuationRuntime = field(repr=False)
    clock: ContinuationActionClock = field(repr=False)
    capture: _AdvisorInvocationCapture = field(repr=False)
    _issued_record: _OriginalEarlySource | None = field(default=None, init=False, repr=False, compare=False)
    _settled_record: _SettledEarly | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(slots=True, weakref_slot=True)
class _OriginalEarlySource:
    phase_owner: CheckpointPhaseOwner
    dispatch: _DispatchOutcome
    persist: _PersistOutcome | ColdPostBatchPersistData
    pre_batch_state: CompositionState
    captured: CapturedContinuationRuntime
    clock: ContinuationActionClock
    capture: _AdvisorInvocationCapture
    capture_data: _CapturedRuntimeData
    prepared_input: PreparedProviderInput | None = None
    prepared: PreparedCheckpointProvider | None = None
    custody: ProviderCallCustody | None = None
    issuer: CheckpointNoSdkIssuer | None = None
    request_identity: str | None = None
    early_input: CapturedCheckpointEarlyInput | None = None
    retry: VerifiedEarlyFormatRetry | None = None
    detached: RestoredContinuationRuntime | None = None
    detached_capture: CapturedContinuationRuntime | None = None


_issued: WeakKeyDictionary[CheckpointEarlyPhysicalSource, ReferenceType[_OriginalEarlySource]] = WeakKeyDictionary()


def _original_source(source: CheckpointEarlyPhysicalSource) -> _OriginalEarlySource:
    reference = _issued.get(source) if type(source) is CheckpointEarlyPhysicalSource else None
    original = None if reference is None else reference()
    if original is None or original is not source._issued_record:
        raise AuditIntegrityError("EARLY physical request lost its original issued observation")
    return original


@asynccontextmanager
async def checkpoint_early_physical_preparation(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
    pre_batch_state: CompositionState,
) -> AsyncIterator[CheckpointEarlyPhysicalSource]:
    """Own one original EARLY action's text parent before its first capture.

    The driver has already admitted and bound this clock. Source SQL and each
    worker borrow text proofs while keeping their own fresh authority checks.
    The physical binder seals preparation before guard/quota/SDK stages; this
    lexical owner must exit before a later driver-admitted retry action starts.
    """
    from elspeth.web.continuation_retention_memo import v4_preparation_memo_scope

    clock = phases.action_clock
    deadline = phases.work.coordinator.sql_deadline_owner
    if type(clock) is not ContinuationActionClock or deadline is None:
        raise AuditIntegrityError("EARLY preparation requires its original driver clock and SQL timing owner")
    deadline.require_action_clock(clock)
    with v4_preparation_memo_scope(clock):
        source = await prepare_checkpoint_early_physical_source(
            phases,
            runtime,
            selected=selected,
            dispatch=dispatch,
            persist=persist,
            pre_batch_state=pre_batch_state,
        )
        yield source


def _prepared_guard_commitment(supplied: PreparedProviderInput) -> CheckpointEarlyGuardCommitment:
    return CheckpointEarlyGuardCommitment(
        provenance_sha256=supplied._seal,
        role="advisor",
        mode=supplied.mode,
        adapter=supplied._guard_adapter,
        policy_fingerprint=supplied._guard_fingerprint,
        validity_fingerprint=supplied._guard_validity_fingerprint,
        timeout_seconds=supplied._guard_timeout,
    )


def require_original_checkpoint_early_physical_source(source: CheckpointEarlyPhysicalSource) -> None:
    from elspeth.web.composer.continuation_semantic_advisor_return import _live_source
    from elspeth.web.composer.no_tool_policy import state_is_structurally_empty

    original = _original_source(source)
    if any(
        actual is not expected
        for actual, expected in (
            (source.phase_owner, original.phase_owner),
            (source.dispatch, original.dispatch),
            (source.persist, original.persist),
            (source.pre_batch_state, original.pre_batch_state),
            (source.captured, original.captured),
            (source.clock, original.clock),
            (source.capture, original.capture),
        )
    ):
        raise AuditIntegrityError("EARLY physical request lost its original issued observation")
    owner = source.phase_owner
    _live_source(owner.phases, owner.runtime, owner.selected, source.dispatch, source.persist)
    finished = owner.phases.finished_batch
    if (
        finished is None
        or finished[2] is not source.pre_batch_state
        or not state_is_structurally_empty(source.pre_batch_state)
        or state_is_structurally_empty(source.dispatch.state)
        or source.dispatch.state is not owner.runtime.live.state
        or owner.phase_source.phase != "early"
        or owner.phase_source.kind != "iteration_early"
        or source.captured.selected is not owner.selected
        or original.capture_data.captured is not source.captured
        or owner.phases.action_clock is not source.clock
        or source.clock.policy != owner.runtime.source.policy.actions.advisor
    ):
        raise AuditIntegrityError("EARLY physical source changed its actual graph transition or driver clock")
    deadline = owner.phases.work.coordinator.sql_deadline_owner
    if deadline is None or deadline.running is not owner.phases.work.running:
        raise AuditIntegrityError("EARLY physical source lost its original SQL timing owner")
    deadline.require_action_clock(source.clock)


async def prepare_checkpoint_early_physical_source(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
    pre_batch_state: CompositionState,
) -> CheckpointEarlyPhysicalSource:
    from elspeth.web.composer.continuation_semantic_advisor_return import _live_source
    from elspeth.web.composer.no_tool_policy import state_is_structurally_empty

    _live_source(phases, runtime, selected, dispatch, persist)
    finished = phases.finished_batch
    if (
        type(runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or finished is None
        or finished[2] is not pre_batch_state
        or not state_is_structurally_empty(pre_batch_state)
        or state_is_structurally_empty(dispatch.state)
        or type(phases.action_clock) is not ContinuationActionClock
    ):
        raise AuditIntegrityError("EARLY issuance requires its original paid iteration transition")
    phases.action_clock.require_preparation_time()
    owner = await prepare_checkpoint_phase_owner(phases, runtime, selected, superseded_reply_index=None, elide_advisor_exchange=False)
    if len(owner.phase_source.completed_children) > 1:
        raise AuditIntegrityError("EARLY source exceeded its one original format retry")
    phase = owner.phase_source
    retry = phase.early_retry
    if retry is None:
        if phase.completed_children:
            raise AuditIntegrityError("EARLY retry lost its original consumed first complete child")
    elif (
        type(retry) is not VerifiedEarlyFormatRetry
        or phase.phase != "early"
        or phase.kind != "iteration_early"
        or retry.selected is not phase.current
        or retry.child.original_stage is not phase.source
        or retry.marker.outer_effect_key != phase.outer_effect_key
        or phase.completed_children != (retry.marker.effect_key,)
    ):
        raise AuditIntegrityError("EARLY retry changed its acquired phase DATA")
    phases.action_clock.require_preparation_time()
    capture_data = await capture_continuation_runtime_data(runtime, selected=selected, inventory=runtime.materialization.inventory)
    phases.action_clock.require_preparation_time()
    captured = capture_data.captured
    capture = _AdvisorInvocationCapture()
    source = CheckpointEarlyPhysicalSource(owner, dispatch, persist, pre_batch_state, captured, phases.action_clock, capture)
    original = _OriginalEarlySource(
        owner, dispatch, persist, pre_batch_state, captured, phases.action_clock, capture, capture_data, retry=retry
    )
    object.__setattr__(source, "_issued_record", original)
    _issued[source] = ref(original)
    require_original_checkpoint_early_physical_source(source)
    return source


def require_checkpoint_early_prepared_clock(prepared: PreparedCheckpointProvider) -> ContinuationActionClock:
    if type(prepared) is not PreparedCheckpointProvider or type(prepared.physical_source) is not ReferenceType:
        raise AuditIntegrityError("EARLY timing requires its original nominal prepared request")
    source = prepared.physical_source()
    if not isinstance(source, CheckpointEarlyPhysicalSource):
        raise AuditIntegrityError("EARLY timing lost its original live source")
    require_original_checkpoint_early_physical_source(source)
    original = _original_source(source)
    owner = source.phase_owner
    if (
        original.prepared is not prepared
        or original.prepared_input is None
        or prepared.clock is not source.clock
        or prepared.running is not owner.phases.work.running
        or prepared.work.coordinator is not owner.phases.work.coordinator
        or prepared.composer is not owner.phases.service
        or prepared.authority is not owner.phases.authority
        or prepared.prepared_source.phase != "early"
        or prepared.prepared_source.early_input_ref is None
        or prepared.env_ref_names != owner.runtime.env_ref_names
        or prepared.publication_binding is not None
    ):
        raise AuditIntegrityError("EARLY timing replaced its original prepared request or operation")
    supplied = original.prepared_input
    supplied.verify(model=supplied.model, role="advisor", mount=supplied.mount, mode=supplied.mode, user_prompt=supplied.genuine_user)
    return source.clock


async def bind_checkpoint_early_physical_request(
    source: CheckpointEarlyPhysicalSource,
    *,
    custody: ProviderCallCustody,
    prepared_input: PreparedProviderInput,
    request_options: dict[str, JsonValue],
) -> None:
    """Retain exact request/inventory before quota; do not create another clock."""
    from elspeth.web.composer.provider_gateway import _apply_openrouter_usage_accounting

    require_original_checkpoint_early_physical_source(source)
    original = _original_source(source)
    owner = source.phase_owner
    running = owner.phases.work.running
    if (
        type(custody) is not ProviderCallCustody
        or type(prepared_input) is not PreparedProviderInput
        or prepared_input.role != "advisor"
        or running is None
        or original.prepared is not None
        or original.custody is not None
        or not source.capture.claimed
        or custody.required_work.coordinator is not owner.phases.work.coordinator
        or custody.required_work.transition_ordinal != owner.phases.work.transition_ordinal
        or custody.required_work.role is not RequiredWorkRole.TURN
        or custody.context is not owner.runtime.bindings.session_operation_context
        or request_options["messages"] is not prepared_input.evidence.messages
        or request_options["model"] != prepared_input.model
    ):
        raise AuditIntegrityError("EARLY physical binding changed its actual prepared invocation")
    source.clock.require_preparation_time()
    prepared_input.verify(
        model=prepared_input.model,
        role="advisor",
        mount=prepared_input.mount,
        mode=prepared_input.mode,
        user_prompt=prepared_input.genuine_user,
    )
    # ELSPETH authors these wire options. Validate their complete JSON value
    # algebra before hashing; the original prepared-message identity stays intact.
    owned_json(request_options)
    _apply_openrouter_usage_accounting(request_options)
    identity = continuation_provider_input_hash(request_options)
    compose = source.captured.stack.frames[0]
    if not isinstance(compose, ComposeFrame):
        raise AuditIntegrityError("EARLY preparation lost its original composition frame")
    guard = _prepared_guard_commitment(prepared_input)
    snapshot = CheckpointEarlyInput(
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        original_checkpoint_id=owner.selected.checkpoint_id,
        original_cursor=owner.selected.stack.cursor,
        outer_effect_key=owner.phase_source.outer_effect_key,
        retry_ordinal=1 if original.retry is not None else 0,
        captured_runtime_sha256=stable_hash(source.captured.stack.model_dump(mode="json")),
        runtime_chunks=tuple(chunk.reference for chunk in source.captured.chunks),
        state_sha256=compose.state.digest,
        transcript_sha256=compose.transcript.digest,
        request_identity=identity,
        messages_sha256=stable_hash(prepared_input.evidence.messages),
        prepared_guard_sha256=stable_hash(guard.model_dump(mode="json")),
        guard=guard,
        requested_model=prepared_input.model,
        env_ref_names=tuple(sorted(owner.runtime.env_ref_names)),
    )
    early_input = CapturedCheckpointEarlyInput(snapshot, encode_checkpoint_early_input(snapshot), ref(source))
    original.prepared_input, original.request_identity, original.early_input = prepared_input, identity, early_input
    sessions = owner.phases.service._sessions_service
    if type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("EARLY physical binding lost its original session SQL owner")
    preparation = PreparedCheckpointSource(
        running=running,
        selected=owner.selected,
        captured_before=source.captured,
        phase="early",
        pass_index=0,
        work=owner.phases.work,
        composer=owner.phases.service,
        authority=owner.phases.authority,
        early_input=early_input,
    )

    def prepare_source() -> CheckpointPreparedSource:
        source.clock.require_preparation_time()
        return sessions.prepare_v4_checkpoint_source_sync(prepared=preparation)

    source.clock.require_preparation_time()
    prepared_source = await _registered(
        work=owner.phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=prepare_source,
        expected=CheckpointPreparedSource,
    )
    source.clock.require_preparation_time()
    # Join this attempt's actual captured admission to the fresh prepared
    # closure and construct independent owners before quota/SDK admission.
    from elspeth.web.composer.continuation_checkpoint_end_detached import restore_captured_checkpoint_runtime

    detached, detached_capture = await restore_captured_checkpoint_runtime(
        phase_owner=owner,
        prepared=prepared_source,
        capture_data=original.capture_data,
    )
    original.detached, original.detached_capture = detached, detached_capture
    prepared = PreparedCheckpointProvider(
        running=running,
        prepared_source=prepared_source,
        work=custody.required_work,
        composer=owner.phases.service,
        authority=owner.phases.authority,
        retry_ordinal=1 if original.retry is not None else 0,
        request_sha256=snapshot.messages_sha256,
        requested_model=prepared_input.model,
        env_ref_names=owner.runtime.env_ref_names,
        publication_binding=None,
        clock=source.clock,
        physical_source=ref(source),
    )
    original.prepared_input, original.prepared, original.request_identity = prepared_input, prepared, identity
    clock = require_checkpoint_early_prepared_clock(prepared)
    issuer = CheckpointNoSdkIssuer(prepared, clock=clock)
    custody.bind_continuation_checkpoint_v4(prepared, issuer)
    original.custody, original.issuer = custody, issuer
    request_options["continuation_input_sha256"] = identity
    _bind_early_finalizers(source, custody=custody, prepared=prepared, sessions=sessions)
    from elspeth.web.continuation_retention_memo import finish_v4_preparation_memo

    try:
        clock.finish_preparation()
    finally:
        finish_v4_preparation_memo(clock)


def require_original_checkpoint_early_input(
    value: CapturedCheckpointEarlyInput,
    *,
    captured: CapturedContinuationRuntime,
) -> None:
    if type(value) is not CapturedCheckpointEarlyInput or type(value.physical_source) is not ReferenceType:
        raise AuditIntegrityError("EARLY preparation requires its original issued request input")
    source = value.physical_source()
    if not isinstance(source, CheckpointEarlyPhysicalSource):
        raise AuditIntegrityError("EARLY preparation lost its original live source")
    require_original_checkpoint_early_physical_source(source)
    original = _original_source(source)
    if original.early_input is not value or source.captured is not captured or original.prepared_input is None:
        raise AuditIntegrityError("EARLY preparation replaced its original prepared request")
    supplied = original.prepared_input
    supplied.verify(model=supplied.model, role="advisor", mount=supplied.mount, mode=supplied.mode, user_prompt=supplied.genuine_user)
    if (
        encode_checkpoint_early_input(value.snapshot) != value.chunk
        or value.snapshot.guard != _prepared_guard_commitment(supplied)
        or value.snapshot.request_identity != original.request_identity
        or value.snapshot.messages_sha256 != stable_hash(supplied.evidence.messages)
        or value.snapshot.env_ref_names != tuple(sorted(source.phase_owner.runtime.env_ref_names))
    ):
        raise AuditIntegrityError("EARLY preparation changed its request or guard inventory commitment")


def checkpoint_early_physical_custody(source: CheckpointEarlyPhysicalSource) -> ProviderCallCustody:
    require_original_checkpoint_early_physical_source(source)
    custody = _original_source(source).custody
    if custody is None:
        raise AuditIntegrityError("EARLY physical source has no original bound custody")
    return custody


def checkpoint_early_format_retry(source: CheckpointEarlyPhysicalSource) -> VerifiedEarlyFormatRetry | None:
    require_original_checkpoint_early_physical_source(source)
    return _original_source(source).retry


def require_checkpoint_early_detached_source(
    source: CheckpointEarlyPhysicalSource,
    *,
    detached: RestoredContinuationRuntime,
    original: RestoredContinuationRuntime,
) -> None:
    """Validate the complete original graph in the caller's registered worker."""
    from elspeth.web.composer.continuation_checkpoint_end_detached import require_detached_end_capture

    expected = _require_checkpoint_early_detached_owners(source, detached=detached, original=original)
    require_detached_end_capture(detached, expected=expected, phase_owner=source.phase_owner)


def _require_checkpoint_early_detached_owners(
    source: CheckpointEarlyPhysicalSource,
    *,
    detached: RestoredContinuationRuntime,
    original: RestoredContinuationRuntime,
) -> CapturedContinuationRuntime:
    from elspeth.web.composer.continuation_checkpoint_end_detached import require_detached_end_owners

    require_original_checkpoint_early_physical_source(source)
    issued = _original_source(source)
    if (
        issued.detached is not detached
        or source.phase_owner.runtime is not original
        or issued.detached_capture is None
        or issued.prepared is None
        or detached.source.selected.checkpoint_id != issued.prepared.prepared_source.prepared_selected.checkpoint_id
    ):
        raise AuditIntegrityError("EARLY projection replaced its original independent pre-call graph")
    require_detached_end_owners(detached, original)
    return issued.detached_capture


def checkpoint_early_detached_runtime(source: CheckpointEarlyPhysicalSource) -> RestoredContinuationRuntime:
    require_original_checkpoint_early_physical_source(source)
    detached = _original_source(source).detached
    if detached is None:
        raise AuditIntegrityError("EARLY physical source has no original retained detached graph")
    # Only hand off the exact original owners here. The projector performs the
    # full capture/compare immediately before applying its result in one worker.
    _require_checkpoint_early_detached_owners(source, detached=detached, original=source.phase_owner.runtime)
    return detached


def note_checkpoint_early_physical_failure(source: CheckpointEarlyPhysicalSource, error: BaseException) -> None:
    require_original_checkpoint_early_physical_source(source)
    original = _original_source(source)
    if original.issuer is not None and original.custody is not None:
        original.issuer.note_original_failure(original.custody, error)


def checkpoint_early_physical_no_sdk_issuer(source: CheckpointEarlyPhysicalSource) -> CheckpointNoSdkIssuer:
    require_original_checkpoint_early_physical_source(source)
    issuer = _original_source(source).issuer
    if issuer is None:
        raise AuditIntegrityError("EARLY physical source has no original no-SDK issuer")
    return issuer


def observe_checkpoint_early_physical_return(
    source: CheckpointEarlyPhysicalSource,
) -> CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation:
    from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response

    custody = checkpoint_early_physical_custody(source)
    owner = source.phase_owner
    context, policy = owner.runtime.bindings.session_operation_context, owner.runtime.source.policy
    if custody.attempt is None or context is None or type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("EARLY physical return lost its original paid attempt or operation")
    return observe_v4_early_checkpoint_returned_response(
        source.capture.snapshot(),
        custody=custody,
        policy=policy,
        attempt=custody.attempt,
        context=context,
        env_ref_names=owner.runtime.env_ref_names,
    )


def observe_checkpoint_early_sdk_companion(
    source: CheckpointEarlyPhysicalSource,
    failure: BaseException,
) -> SdkTerminalFailure | None:
    from elspeth.web.composer.provider_sdk_terminal import capture_sdk_terminal_failure

    require_original_checkpoint_early_physical_source(source)
    custody = _original_source(source).custody
    return None if custody is None else capture_sdk_terminal_failure(failure, custody=custody)


def _bind_early_finalizers(
    source: CheckpointEarlyPhysicalSource,
    *,
    custody: ProviderCallCustody,
    prepared: PreparedCheckpointProvider,
    sessions: SessionServiceImpl,
) -> None:
    from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
    from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation
    from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce
    from elspeth.web.required_work import RequiredWorkTicket
    from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal
    from elspeth.web.sessions.continuation_sdk_terminal_result import VerifiedSdkTerminal

    def require_admission() -> None:
        require_original_checkpoint_early_physical_source(source)
        if (
            _original_source(source).custody is not custody
            or custody.continuation_checkpoint_v4 is not prepared
            or custody.continuation_checkpoint_v4_admission is None
            or source.phase_owner.phases.service._sessions_service is not sessions
        ):
            raise AuditIntegrityError("EARLY terminal finalizer lost its original admitted physical source")

    async def finalize_incomplete(
        incomplete: V4ProviderResultIncomplete,
        required_work: RequiredWorkTicket,
    ) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:
        require_admission()
        admission = custody.continuation_checkpoint_v4_admission
        if admission is None:
            raise AuditIntegrityError("EARLY incomplete finalizer lost its original admission")
        return await sessions.finalize_v4_continuation_checkpoint_incomplete(
            prepared=prepared,
            admission=admission,
            custody=custody,
            incomplete=incomplete,
            required_work=required_work,
        )

    async def finalize_sdk(
        observation: V4SdkFailureObservation,
        required_work: RequiredWorkTicket,
    ) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:
        require_admission()
        admission = custody.continuation_checkpoint_v4_admission
        if admission is None or custody.attempt is not admission.attempt or observation.attempt is not admission.attempt:
            raise AuditIntegrityError("EARLY SDK finalizer changed its original physical attempt")
        coordinator = source.phase_owner.phases.work.coordinator
        deadline = coordinator.sql_deadline_owner
        if deadline is None:
            raise AuditIntegrityError("EARLY SDK finalizer lost its original timing owner")
        deadline.begin_settlement()
        outcome = await sessions.finalize_v4_sdk_terminal(observation=observation, custody=custody, required_work=required_work)
        coordinator.validate_provider_settlement_sql_outcome(ticket=required_work, actual_outcome=outcome)
        return outcome

    custody.bind_v4_returned_incomplete_finalizer(finalize_incomplete)
    custody.bind_v4_sdk_finalizer(finalize_sdk)
