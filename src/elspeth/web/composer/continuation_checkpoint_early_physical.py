"""Original iteration EARLY source and one driver-clock-bound physical request.

Issuance requires the actual empty-to-nonempty iteration observation. Immutable
request DATA cannot recreate this weak-issued live authority after a restart.
"""

from __future__ import annotations

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
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, capture_continuation_runtime_owners
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.required_work import RequiredWorkRole, RequiredWorkSource
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
    from elspeth.web.composer.continuation_finished_batch_data import ColdPostBatchPersistData
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.state import CompositionState


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointEarlyPhysicalSource:
    phase_owner: CheckpointPhaseOwner
    dispatch: _DispatchOutcome = field(repr=False)
    persist: _PersistOutcome | ColdPostBatchPersistData = field(repr=False)
    pre_batch_state: CompositionState = field(repr=False)
    captured: CapturedContinuationRuntime = field(repr=False)
    clock: ContinuationActionClock = field(repr=False)
    capture: _AdvisorInvocationCapture = field(repr=False)


@dataclass(slots=True)
class _OriginalEarlySource:
    phase_owner: CheckpointPhaseOwner
    dispatch: _DispatchOutcome
    persist: _PersistOutcome | ColdPostBatchPersistData
    pre_batch_state: CompositionState
    captured: CapturedContinuationRuntime
    clock: ContinuationActionClock
    capture: _AdvisorInvocationCapture
    prepared_input: PreparedProviderInput | None = None
    prepared: PreparedCheckpointProvider | None = None
    custody: ProviderCallCustody | None = None
    issuer: CheckpointNoSdkIssuer | None = None
    request_identity: str | None = None
    early_input: CapturedCheckpointEarlyInput | None = None


_issued: WeakKeyDictionary[CheckpointEarlyPhysicalSource, _OriginalEarlySource] = WeakKeyDictionary()


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

    original = _issued.get(source) if type(source) is CheckpointEarlyPhysicalSource else None
    if original is None or any(
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
    owner = await prepare_checkpoint_phase_owner(phases, runtime, selected, superseded_reply_index=None, elide_advisor_exchange=False)
    captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=runtime.materialization.inventory)
    capture = _AdvisorInvocationCapture()
    source = CheckpointEarlyPhysicalSource(owner, dispatch, persist, pre_batch_state, captured, phases.action_clock, capture)
    _issued[source] = _OriginalEarlySource(owner, dispatch, persist, pre_batch_state, captured, phases.action_clock, capture)
    require_original_checkpoint_early_physical_source(source)
    return source


def require_checkpoint_early_prepared_clock(prepared: PreparedCheckpointProvider) -> ContinuationActionClock:
    if type(prepared) is not PreparedCheckpointProvider or type(prepared.physical_source) is not ReferenceType:
        raise AuditIntegrityError("EARLY timing requires its original nominal prepared request")
    source = prepared.physical_source()
    if not isinstance(source, CheckpointEarlyPhysicalSource):
        raise AuditIntegrityError("EARLY timing lost its original live source")
    require_original_checkpoint_early_physical_source(source)
    original = _issued[source]
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
    original = _issued[source]
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
        retry_ordinal=0,
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
    prepared_source = await _registered(
        work=owner.phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=lambda: sessions.prepare_v4_checkpoint_source_sync(prepared=preparation),
        expected=CheckpointPreparedSource,
    )
    prepared = PreparedCheckpointProvider(
        running=running,
        prepared_source=prepared_source,
        work=custody.required_work,
        composer=owner.phases.service,
        authority=owner.phases.authority,
        retry_ordinal=0,
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
    original = _issued[source]
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
    custody = _issued[source].custody
    if custody is None:
        raise AuditIntegrityError("EARLY physical source has no original bound custody")
    return custody
