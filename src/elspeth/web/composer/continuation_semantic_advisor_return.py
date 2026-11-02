"""Original post-batch calls and captures retained for semantic assessment.

An equal result or capture is DATA, not evidence that this live owner produced
it. Only these observers register the original objects. The SQL iteration
owner separately replays the paid PRIMARY/P4 source, current fence and stage
effect before accepting an observation; none of these carriers grants replay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer._compose_loop_carriers import (
    _AdvisorReviewState,
    _ClassifyOutcome,
    _DispatchOutcome,
    _PersistOutcome,
    _TerminateOutcome,
)
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointNoSdkTerminal,
    EndCheckpointPaidOverflowTerminal,
    EndCheckpointUnboundRefusalTerminal,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointPaidIncompleteTerminal, observe_end_checkpoint_terminal
from elspeth.web.composer.continuation_classifier_handoff import (
    ClassifyPostPrimary,
    ClassifySourceProofStop,
    encode_classify_post_primary,
    encode_classify_source_proof_stop,
)
from elspeth.web.composer.continuation_classifier_owned_source import (
    ClassifierOwnedSourceObservation,
    ClassifyReplyRefusalReady,
    decode_classifier_owned_source,
    encode_classifier_owned_source,
)
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_finished_batch_data import ColdPostBatchPersistData
from elspeth.web.composer.continuation_result_codec import RetainedComposerResult, encode_retained_composer_result
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    capture_continuation_runtime_owners,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.no_tool_policy import state_is_structurally_empty
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.protocol import ComposerResult
from elspeth.web.composer.provider_quota import composer_quota_scope
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.composer_operations import ComposerOperationRunning

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.sessions.continuation_classifier_primary import (
        ClassifierPrimaryIssuer,
        ClassifierPrimaryResult,
        ClassifierReplyDeadline,
        VerifiedClassifierPaidIncompleteTerminal,
        VerifiedClassifierPaidOverflowTerminal,
    )
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ObservedIterationEarly:
    """Opaque original EARLY completion; constructor access issues nothing."""


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ObservedClassification:
    outcome: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ObservedIterationReturn:
    """Opaque original completed return; constructor access issues nothing."""


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ObservedIterationEndInput:
    """Opaque original pre-END input; constructor access issues no binding."""


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ObservedClassifierReplyDeadline:
    """Opaque reached no-SDK boundary; fields cannot recreate its live source."""


@dataclass(frozen=True, slots=True)
class OriginalClassifierReplyDeadlineData:
    """Verified source DATA; the ready SQL owner must reverify the marker."""

    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory
    token: ClassifierReplyDeadline


@dataclass(frozen=True, slots=True)
class ObservedIterationReturnData:
    result: ComposerResult
    publication_binding: str | None
    result_json: str
    recorder_source: PrivateChunk
    terminal_kind: Literal["model_end", "owned_refusal", "owned_control", "owned_error", "owned_recovery"] | None = None
    end_disposition: Literal["clean", "blocked"] | None = None
    primary_effect_key: str | None = None
    primary_input_sha256: str | None = None
    primary_response: PrivateChunkRef | None = None
    owned_source_json: str | None = None


@dataclass(slots=True)
class _ClassifyEndInvocationCapture:
    """Private invocation observation, never a caller-issued publication grant.

    The resumed service records its actual shared END outcome before returning
    the classifier result. Its observer must seal both exact object identities
    and original paid PRIMARY source; the SQL writer still authenticates the
    original persisted model-pass record. An unissued capture has no authority.
    """

    original_terminal: _TerminateOutcome | None = None
    original_outcome: _ClassifyOutcome | None = None
    terminal_kind: Literal["model_end", "owned_refusal", "owned_control", "owned_error", "owned_recovery"] | None = None
    original_owned_source_json: str | None = None

    def record_original_owned_source(self, observation: ClassifierOwnedSourceObservation) -> None:
        if self.original_owned_source_json is not None:
            raise AuditIntegrityError("classifier refusal source observation can only be recorded once")
        self.original_owned_source_json = encode_classifier_owned_source(observation)

    def record_original_model_end(self, terminal: _TerminateOutcome, outcome: _ClassifyOutcome) -> None:
        if (
            self.original_outcome is not None
            or type(terminal) is not _TerminateOutcome
            or type(outcome) is not _ClassifyOutcome
            or terminal.action != "return"
            or outcome.action != "return"
            or terminal.publication_binding != outcome.publication_binding
        ):
            raise AuditIntegrityError("classifier END capture changed its one original terminal outcome")
        self.original_terminal, self.original_outcome = terminal, outcome
        self.terminal_kind = "model_end"

    def record_original_owned_terminal(
        self,
        kind: Literal["owned_refusal", "owned_control", "owned_error", "owned_recovery"],
        outcome: _ClassifyOutcome,
        *,
        terminal: _TerminateOutcome | None = None,
    ) -> None:
        if (
            self.original_outcome is not None
            or kind not in ("owned_refusal", "owned_control", "owned_error", "owned_recovery")
            or type(outcome) is not _ClassifyOutcome
            or outcome.action != "return"
            or (
                terminal is not None
                and (
                    type(terminal) is not _TerminateOutcome
                    or terminal.action != "return"
                    or terminal.publication_binding != outcome.publication_binding
                    or terminal.end_disposition.value == "clean"
                )
            )
        ):
            raise AuditIntegrityError("classifier END capture changed its original owned terminal source")
        self.original_outcome, self.terminal_kind = outcome, kind
        self.original_terminal = terminal


@dataclass(frozen=True, slots=True)
class _LiveSource:
    phases: ContinuationLivePhases
    runtime: RestoredContinuationRuntime
    running: ComposerOperationRunning
    selected: SelectedContinuationState
    dispatch: _DispatchOutcome
    persist: _PersistOutcome | ColdPostBatchPersistData


@dataclass(slots=True)
class _ClassificationSource:
    live: _LiveSource
    original: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop
    original_result: ComposerResult | None
    counters: tuple[int, int, int, int]
    publication_binding: str | None
    return_started: bool = False
    issuer: ClassifierPrimaryIssuer | None = None
    original_primary: ClassifierPrimaryResult | None = None
    physical_source_refreshed: bool = False
    pending_json: str | None = None
    pending_capture: CapturedContinuationRuntime | None = None
    pending_capture_started: bool = False
    completion_capture: _ClassifyEndInvocationCapture | None = None
    original_terminal: _TerminateOutcome | None = None
    terminal_kind: Literal["model_end", "owned_refusal", "owned_control", "owned_error", "owned_recovery"] | None = None
    resumed_from: ClassifyPostPrimary | ClassifySourceProofStop | ClassifyReplyRefusalReady | None = None
    owned_source_json: str | None = None


@dataclass(frozen=True, slots=True)
class _EarlySource:
    live: _LiveSource
    captured: CapturedContinuationRuntime


@dataclass(frozen=True, slots=True)
class _ReturnSource:
    classification: ObservedClassification
    live: _LiveSource
    captured: CapturedContinuationRuntime
    data: ObservedIterationReturnData


@dataclass(frozen=True, slots=True)
class _EndInputSource:
    live: _LiveSource
    pending: ClassifyPostPrimary | ClassifySourceProofStop
    pending_json: str
    response: PrivateChunk | None
    evidence: TurnEvidence
    state: CompositionState
    request: str
    clock: ContinuationActionClock
    binding: str | None


@dataclass(frozen=True, slots=True)
class _ReplyDeadlineSource:
    live: _LiveSource
    inventory: CurrentContinuationEnvInventory
    issuer: ClassifierPrimaryIssuer
    token: ClassifierReplyDeadline
    clock: ContinuationActionClock
    observation_json: str


_early: WeakKeyDictionary[ObservedIterationEarly, _EarlySource] = WeakKeyDictionary()
_classifications: WeakKeyDictionary[ObservedClassification, _ClassificationSource] = WeakKeyDictionary()
_returns: WeakKeyDictionary[ObservedIterationReturn, _ReturnSource] = WeakKeyDictionary()
_end_inputs: WeakKeyDictionary[ObservedIterationEndInput, _EndInputSource] = WeakKeyDictionary()
_reply_deadlines: WeakKeyDictionary[ObservedClassifierReplyDeadline, _ReplyDeadlineSource] = WeakKeyDictionary()


def _counters(outcome: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop) -> tuple[int, int, int, int]:
    if type(outcome) in (ClassifyPostPrimary, ClassifySourceProofStop):
        return outcome.composition_turns_delta, outcome.discovery_turns_delta, 0, 0
    if type(outcome) is not _ClassifyOutcome:
        raise AuditIntegrityError("iteration observation lost its original nominal outcome")
    return (
        outcome.composition_turns_delta,
        outcome.discovery_turns_delta,
        outcome.advisor_passes_delta,
        outcome.repair_turns_delta,
    )


def _pending_json(outcome: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop) -> str | None:
    if type(outcome) is ClassifyPostPrimary:
        return encode_classify_post_primary(outcome)
    if type(outcome) is ClassifySourceProofStop:
        return encode_classify_source_proof_stop(outcome)
    return None


def _original_result(outcome: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop) -> ComposerResult | None:
    return outcome.result if type(outcome) is _ClassifyOutcome else None


def _original_binding(outcome: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop) -> str | None:
    return outcome.publication_binding if type(outcome) is _ClassifyOutcome else None


def _original_action_deadline(
    phases: ContinuationLivePhases, runtime: RestoredContinuationRuntime, clock: ContinuationActionClock
) -> float:
    from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner

    owner = phases.work.coordinator.sql_deadline_owner
    if (
        type(owner) is not ContinuationSQLDeadlineOwner
        or owner.running is not phases.work.running
        or runtime.bindings.required_work is not phases.work
        or phases.action_clock is not clock
        or owner.policy != runtime.source.policy
    ):
        raise AuditIntegrityError("classifier action lost its original deadline owner and bound clock")
    now = time.monotonic()
    remaining = owner.remaining_dispatch(monotonic_now=now)
    if remaining <= 0:
        raise TimeoutError("classifier action exhausted its original dispatch allowance")
    return min(runtime.bindings.deadline, now + remaining, clock.started_monotonic + clock.policy.total)


def _live_source(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
) -> _LiveSource:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(runtime) is not RestoredContinuationRuntime:
        raise AuditIntegrityError("iteration observation requires its original live owners")
    running = phases.work.running
    finished = phases.finished_batch
    if (
        type(running) is not ComposerOperationRunning
        or type(selected) is not SelectedContinuationState
        or phases.runtime is not runtime
        or runtime.source.selected is not selected
        or runtime.materialization.service is not phases.service
        or runtime.materialization.authority is not phases.authority
        or runtime.materialization.inventory.running is not running
        or runtime.bindings.required_work is not phases.work
        or runtime.bindings.provider_owner is not phases.provider_owner
        or runtime.bindings.session_operation_context is not running.session_operation_context
        or phases.lease.context is not running.session_operation_context
        or finished is None
        or finished[0] is not dispatch
        or finished[1] is not persist
    ):
        raise AuditIntegrityError("iteration observation changed its original runtime, work, lease or batch")
    phases.work.validate_context(running.session_operation_context)
    return _LiveSource(phases, runtime, running, selected, dispatch, persist)


def _require_current_capture(
    live: _LiveSource,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    captured: CapturedContinuationRuntime,
    original_capture: CapturedContinuationRuntime,
) -> None:
    current = _live_source(live.phases, live.runtime, live.selected, live.dispatch, live.persist)
    if (
        current.running is not running
        or selected is not live.selected
        or type(captured) is not CapturedContinuationRuntime
        or captured is not original_capture
        or captured.selected is not selected
    ):
        raise AuditIntegrityError("iteration observation changed its original selected capture")


async def observe_continuation_iteration_early(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
    pre_batch_state: CompositionState,
) -> tuple[ObservedIterationEarly, CapturedContinuationRuntime]:
    """Run the original EARLY checkpoint once, under its caller's action."""
    source = _live_source(phases, runtime, selected, dispatch, persist)
    finished = phases.finished_batch
    if (
        finished is None
        or finished[2] is not pre_batch_state
        or not state_is_structurally_empty(pre_batch_state)
        or state_is_structurally_empty(dispatch.state)
    ):
        raise AuditIntegrityError("iteration EARLY observation changed its original graph transition")
    service, live, bindings = phases.service, runtime.live, runtime.bindings
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("iteration EARLY observation lost its original operation context")
    before = len(live.llm_messages)
    with composer_quota_scope(service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
        await service._advisor_checkpoint._maybe_run_early_checkpoint(
            state=dispatch.state,
            prev_state=pre_batch_state,
            session_id=bindings.session_id,
            llm_messages=live.llm_messages,
            recorder=live.recorder,
            progress=bindings.progress,
            deadline=bindings.deadline,
            session_operation_context=context,
            provider_owner=bindings.provider_owner,
            turn_evidence=live.turn_evidence,
        )
    if len(live.llm_messages) > before:
        live.advisor_repair_context_introduced = True
    live.turn_evidence.transcript.observe_messages(live.llm_messages)
    captured = await capture_continuation_runtime_owners(runtime, selected=selected, inventory=runtime.materialization.inventory)
    observed = ObservedIterationEarly()
    _early[observed] = _EarlySource(source, captured)
    return observed, captured


def require_original_iteration_early(
    observed: ObservedIterationEarly,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    captured: CapturedContinuationRuntime,
) -> None:
    original = _early.get(observed) if type(observed) is ObservedIterationEarly else None
    if original is None:
        raise AuditIntegrityError("iteration EARLY requires its original issued observation")
    _require_current_capture(original.live, running, selected, captured, original.captured)


async def observe_continuation_iteration_classification(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
) -> (
    ObservedClassification
    | ObservedClassifierReplyDeadline
    | VerifiedClassifierPaidOverflowTerminal
    | VerifiedClassifierPaidIncompleteTerminal
    | ClassifierNoSdkRefusalReceipt
):
    """Observe the actual classifier; preserve its original thrown outcome."""
    from elspeth.web.sessions.continuation_classifier_primary import (
        ClassifierPrimaryIssuer,
        ClassifierReplyDeadline,
        VerifiedClassifierPaidIncompleteTerminal,
        VerifiedClassifierPaidOverflowTerminal,
    )
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt

    source = _live_source(phases, runtime, selected, dispatch, persist)
    service, live, bindings = phases.service, runtime.live, runtime.bindings
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("iteration classifier lost its original operation context")
    clock = phases.action_clock
    if type(clock) is not ContinuationActionClock or clock.policy != runtime.source.policy.actions.primary:
        raise AuditIntegrityError("iteration classifier lost its original driver PRIMARY clock")
    deadline = _original_action_deadline(phases, runtime, clock)
    inventory = runtime.materialization.inventory
    issuer = ClassifierPrimaryIssuer(
        service=service,
        authority=phases.authority,
        running=source.running,
        selected=selected,
        inventory=inventory,
        runtime=runtime,
        required_work=phases.work,
        provider_owner=phases.provider_owner,
        clock=clock,
    )
    issuer.bind_reply_deadline(deadline)
    completion_capture = _ClassifyEndInvocationCapture()
    with composer_quota_scope(service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
        outcome = await service._classify_and_budget_turn(
            dispatch=dispatch,
            turn_evidence=live.turn_evidence,
            session_operation_context=context,
            persist=persist,
            llm_messages=live.llm_messages,
            tools=live.tools,
            recorder=live.recorder,
            anti_anchor=live.anti_anchor,
            progress=bindings.progress,
            message=bindings.message,
            initial_version=live.initial_version,
            deadline=deadline,
            runtime_preflight_cache=live.runtime_preflight_cache,
            session_scope=bindings.session_scope,
            session_id=bindings.session_id,
            user_id=bindings.user_id,
            mutation_success_seen=live.mutation_success_seen,
            repair_turns_used=live.repair_turns_used,
            composition_turns_used=live.composition_turns_used,
            discovery_turns_used=live.discovery_turns_used,
            advisor_checkpoint_passes_used=live.advisor_checkpoint_passes_used,
            plugin_snapshot=bindings.plugin_snapshot,
            advisor_review_state=live.advisor_review_state,
            completion_gates=bindings.completion_gates,
            provider_owner=bindings.provider_owner,
            classifier_primary_issuer=issuer,
            completion_capture=completion_capture,
        )
    if isinstance(outcome, ClassifierReplyDeadline):
        observation = issuer.require_reply_deadline(outcome)
        observed_deadline = ObservedClassifierReplyDeadline()
        _reply_deadlines[observed_deadline] = _ReplyDeadlineSource(
            source,
            inventory,
            issuer,
            outcome,
            clock,
            encode_classifier_owned_source(observation),
        )
        # No await, source refresh, runtime capture or finalizer follows the
        # reached boundary. The driver owns original deadline disposition.
        return observed_deadline
    if isinstance(
        outcome, (VerifiedClassifierPaidOverflowTerminal, VerifiedClassifierPaidIncompleteTerminal, ClassifierNoSdkRefusalReceipt)
    ):
        if type(outcome) not in (
            VerifiedClassifierPaidOverflowTerminal,
            VerifiedClassifierPaidIncompleteTerminal,
            ClassifierNoSdkRefusalReceipt,
        ):
            raise AuditIntegrityError("iteration classifier returned a non-nominal terminal")
        if issuer.terminal is not outcome:
            raise AuditIntegrityError("iteration classifier lost its original terminal source")
        return outcome
    if not isinstance(outcome, (_ClassifyOutcome, ClassifyPostPrimary, ClassifySourceProofStop)) or type(outcome) not in (
        _ClassifyOutcome,
        ClassifyPostPrimary,
        ClassifySourceProofStop,
    ):
        raise AuditIntegrityError("iteration classifier lost its original nominal outcome")
    return _register_classification(source, outcome, completion_capture, issuer=issuer)


def require_original_classifier_reply_deadline(
    observed: ObservedClassifierReplyDeadline,
    *,
    phases: ContinuationLivePhases,
    running: ComposerOperationRunning,
    action_clock: ContinuationActionClock,
) -> OriginalClassifierReplyDeadlineData:
    """Revalidate the original uninvoked issuer, never its expired business clock."""
    if type(observed) is not ObservedClassifierReplyDeadline or observed not in _reply_deadlines:
        raise AuditIntegrityError("classifier reply deadline requires its original issued observation")
    source = _reply_deadlines[observed]
    live = source.live
    if (
        phases is not live.phases
        or running is not live.running
        or action_clock is not source.clock
        or phases.action_clock is not action_clock
        or type(action_clock) is not ContinuationActionClock
        or action_clock.policy != live.runtime.source.policy.actions.primary
        or live.runtime.materialization.inventory is not source.inventory
    ):
        raise AuditIntegrityError("classifier reply deadline changed its original source, actor or clock")
    _live_source(phases, live.runtime, live.selected, live.dispatch, live.persist)
    observation = source.issuer.require_reply_deadline(source.token)
    if encode_classifier_owned_source(observation) != source.observation_json:
        raise AuditIntegrityError("classifier reply deadline changed its original complete elapsed observation")
    return OriginalClassifierReplyDeadlineData(live.selected, source.inventory, source.token)


def _register_classification(
    source: _LiveSource,
    outcome: _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop,
    completion_capture: _ClassifyEndInvocationCapture,
    *,
    issuer: ClassifierPrimaryIssuer | None = None,
    resumed_from: ClassifyPostPrimary | ClassifySourceProofStop | ClassifyReplyRefusalReady | None = None,
) -> ObservedClassification:
    if type(outcome) is _ClassifyOutcome and outcome.action == "return" and completion_capture.original_outcome is not outcome:
        raise AuditIntegrityError("iteration return lost its actual original completion producer")
    observed = ObservedClassification(outcome)
    _classifications[observed] = _ClassificationSource(
        source,
        outcome,
        _original_result(outcome),
        _counters(outcome),
        _original_binding(outcome),
        issuer=issuer,
        original_primary=None if issuer is None else issuer.result,
        pending_json=_pending_json(outcome),
        completion_capture=completion_capture,
        original_terminal=completion_capture.original_terminal,
        terminal_kind=completion_capture.terminal_kind,
        resumed_from=resumed_from,
        owned_source_json=completion_capture.original_owned_source_json,
    )
    return observed


def _original_classification(observed: ObservedClassification) -> _ClassificationSource:
    source = _classifications.get(observed) if type(observed) is ObservedClassification else None
    if (
        source is None
        or observed.outcome is not source.original
        or _original_result(observed.outcome) is not source.original_result
        or _counters(observed.outcome) != source.counters
        or _original_binding(observed.outcome) != source.publication_binding
        or _pending_json(observed.outcome) != source.pending_json
        or (source.issuer is not None and source.issuer.result is not source.original_primary)
        or (
            source.completion_capture is not None
            and (
                source.completion_capture.original_terminal is not source.original_terminal
                or source.completion_capture.terminal_kind != source.terminal_kind
                or source.completion_capture.original_owned_source_json != source.owned_source_json
                or (
                    type(source.original) is _ClassifyOutcome
                    and source.original.action == "return"
                    and source.completion_capture.original_outcome is not source.original
                )
            )
        )
    ):
        raise AuditIntegrityError("iteration return requires its original issued classification observation")
    return source


def require_original_iteration_classification(
    observed: ObservedClassification,
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
) -> _ClassifyOutcome | ClassifyPostPrimary | ClassifySourceProofStop:
    """Return the actual outcome without granting SQL or provider admission."""
    source = _original_classification(observed)
    if source.live.phases is not phases or source.live.runtime is not runtime or source.live.selected is not selected:
        raise AuditIntegrityError("iteration classification changed its original live source")
    _live_source(phases, runtime, selected, source.live.dispatch, source.live.persist)
    return source.original


async def _refresh_original_classification_source(
    source: _ClassificationSource, phases: ContinuationLivePhases, runtime: RestoredContinuationRuntime
) -> tuple[RestoredContinuationRuntime, SelectedContinuationState]:
    if source.live.phases is not phases or source.live.runtime is not runtime:
        raise AuditIntegrityError("classifier capture changed its actual originating live owners")
    current = source.live.selected
    primary = source.original_primary
    if primary is not None and not source.physical_source_refreshed:
        if source.issuer is None or source.issuer.result is not primary or not source.issuer.sdk_entered:
            raise AuditIntegrityError("classifier capture lost its actually committed physical response")
        current = await load_selected_continuation(
            authority=phases.authority, running=source.live.running, selected=primary.snapshot, required_work=phases.work
        )
        phase_read = await phases._current_phase_read(current)
        phases._require_phase_read(current, phase_read)
        refreshed = phase_read.runtime
        if (
            refreshed.live is not runtime.live
            or refreshed.live.recorder is not runtime.live.recorder
            or refreshed.live.turn_evidence is not runtime.live.turn_evidence
            or refreshed.bindings.provider_owner is not runtime.bindings.provider_owner
            or refreshed.bindings.required_work is not runtime.bindings.required_work
        ):
            raise AuditIntegrityError("classifier capture replaced its actual live post-provider owners")
        runtime = refreshed
    finished = phases.finished_batch
    if finished is None:
        phase_read = await phases.restore_finished_batch(current)
        runtime = phase_read.runtime
        finished = phases.finished_batch
    if finished is None:
        raise AuditIntegrityError("classifier capture lost its original applied batch source")
    source.live = _live_source(phases, runtime, current, finished[0], finished[1])
    source.physical_source_refreshed = True
    return runtime, current


async def refresh_original_classification_source(
    observed: ObservedClassification, phases: ContinuationLivePhases, runtime: RestoredContinuationRuntime
) -> tuple[RestoredContinuationRuntime, SelectedContinuationState]:
    """Adopt the actual committed PRIMARY source before applying deltas."""
    source = _original_classification(observed)
    if type(source.original) is not _ClassifyOutcome:
        raise AuditIntegrityError("pending classifier requires its original separate capture")
    return await _refresh_original_classification_source(source, phases, runtime)


async def capture_original_classification_pending(
    observed: ObservedClassification, phases: ContinuationLivePhases, runtime: RestoredContinuationRuntime
) -> tuple[CapturedContinuationRuntime, SelectedContinuationState]:
    """Capture the actual committed provider source before a separate END."""
    source = _original_classification(observed)
    pending = source.original
    if not isinstance(pending, (ClassifyPostPrimary, ClassifySourceProofStop)) or source.pending_capture_started:
        raise AuditIntegrityError("classifier pending capture requires its one original source observation")
    origin = source.live.selected
    if (pending.source_checkpoint_id, pending.source_cursor) != (origin.checkpoint_id, origin.stack.cursor):
        raise AuditIntegrityError("classifier pending changed its original invocation checkpoint")
    if type(pending) is ClassifyPostPrimary:
        primary = source.original_primary
        if primary is None or (
            pending.primary_effect_key,
            pending.primary_input_sha256,
            pending.response,
        ) != (primary.effect_key, primary.input_sha256, primary.response.reference):
            raise AuditIntegrityError("classifier pending substituted its original paid response")
    elif source.original_primary is not None or (source.issuer is not None and source.issuer.sdk_entered):
        raise AuditIntegrityError("classifier source stop cannot hide a physical provider call")
    source.pending_capture_started = True
    runtime, current = await _refresh_original_classification_source(source, phases, runtime)
    runtime.live.turn_evidence.transcript.observe_messages(runtime.live.llm_messages)
    captured = await capture_continuation_runtime_owners(runtime, selected=current, inventory=runtime.materialization.inventory)
    source.pending_capture = captured
    return captured, current


def require_original_classification_pending(
    observed: ObservedClassification,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    captured: CapturedContinuationRuntime,
) -> ClassifyPostPrimary | ClassifySourceProofStop:
    source = _original_classification(observed)
    pending = source.original
    if not isinstance(pending, (ClassifyPostPrimary, ClassifySourceProofStop)) or source.pending_capture is None:
        raise AuditIntegrityError("classifier pending requires its original captured source observation")
    _require_current_capture(source.live, running, selected, captured, source.pending_capture)
    return pending


def require_original_classification_owned_source(
    observed: ObservedClassification,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    captured: CapturedContinuationRuntime,
) -> str | None:
    """Read source DATA only through the original pending-call capture."""
    require_original_classification_pending(observed, running, selected, captured)
    return _original_classification(observed).owned_source_json


async def observe_continuation_classifier_end(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    pending: ClassifyPostPrimary | ClassifySourceProofStop,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
    primary_response: PrivateChunk | None,
    owned_source_json: str | None = None,
) -> (
    ObservedClassification
    | EndCheckpointPaidIncompleteTerminal
    | EndCheckpointPaidOverflowTerminal
    | EndCheckpointNoSdkTerminal
    | EndCheckpointUnboundRefusalTerminal
):
    """Observe the actual resumed shared END under its new original action."""
    source = _live_source(phases, runtime, selected, dispatch, persist)
    clock = phases.action_clock
    if type(clock) is not ContinuationActionClock or clock.policy != runtime.source.policy.actions.advisor:
        raise AuditIntegrityError("classifier END lost its original driver Advisor clock")
    capture = _ClassifyEndInvocationCapture()
    if type(pending) is ClassifySourceProofStop:
        if owned_source_json is None:
            raise AuditIntegrityError("classifier source-stop END lost its original retained observation")
        observation = decode_classifier_owned_source(owned_source_json)
        if (
            observation.kind != "source_proof_refusal"
            or observation.source_checkpoint_id != pending.source_checkpoint_id
            or observation.source_cursor != pending.source_cursor
            or observation.proof_json != pending.proof_json
            or observation.state_sha256 != stable_hash(dispatch.state.to_dict())
        ):
            raise AuditIntegrityError("classifier source-stop END substituted its original pending proof or actual state")
        # The SQL owner authenticates the observation's original physical
        # creator separately from this actual END actor after any ready yield.
        capture.record_original_owned_source(observation)
    elif owned_source_json is not None:
        raise AuditIntegrityError("classifier PRIMARY END cannot borrow a source-stop observation")
    context = runtime.bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("classifier END lost its original current operation context")
    checkpoint_phase_owner = None
    from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4

    if type(runtime.source.policy) is ContinuationAdmissionPolicyV4:
        from elspeth.web.composer.continuation_checkpoint_phase import prepare_checkpoint_phase_owner
        from elspeth.web.composer.service import _ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE

        checkpoint_phase_owner = await prepare_checkpoint_phase_owner(
            phases,
            runtime,
            selected,
            superseded_reply_index=None,
            elide_advisor_exchange=_ELIDE_ADVISOR_EXCHANGE_AT_FINALIZE,
        )
    with composer_quota_scope(phases.service._require_sessions_service(), context), prompt_guard_user_scope(runtime.bindings.message):
        outcome = await observe_end_checkpoint_terminal(
            phases.service.resume_continuation_classifier_end(
                pending=pending,
                runtime=runtime,
                dispatch=dispatch,
                persist=persist,
                primary_response=primary_response,
                completion_capture=capture,
                deadline=_original_action_deadline(phases, runtime, clock),
                checkpoint_phase_owner=checkpoint_phase_owner,
            )
        )
    if isinstance(
        outcome,
        (
            EndCheckpointPaidIncompleteTerminal,
            EndCheckpointPaidOverflowTerminal,
            EndCheckpointNoSdkTerminal,
            EndCheckpointUnboundRefusalTerminal,
        ),
    ):
        return outcome
    if type(outcome) is not _ClassifyOutcome:
        raise AuditIntegrityError("classifier END lost its actual nominal outcome")
    return _register_classification(source, outcome, capture, resumed_from=pending)


async def observe_continuation_classifier_reply_refusal(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    pending: ClassifyReplyRefusalReady,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
) -> ObservedClassification:
    """Observe the retained unpaid reply tail under its own actual SQL action."""
    source = _live_source(phases, runtime, selected, dispatch, persist)
    clock = phases.action_clock
    if (
        type(pending) is not ClassifyReplyRefusalReady
        or type(clock) is not ContinuationActionClock
        or clock.policy != runtime.source.policy.actions.session_sql
    ):
        raise AuditIntegrityError("classifier reply refusal lost its original ready DATA or current SQL clock")
    observation = decode_classifier_owned_source(pending.observation_json)
    if (
        observation.kind != "reply_deadline_exhausted"
        or observation.source_checkpoint_id != pending.source_checkpoint_id
        or observation.source_cursor != pending.source_cursor
        or observation.state_sha256 != stable_hash(dispatch.state.to_dict())
    ):
        raise AuditIntegrityError("classifier reply refusal changed its retained original elapsed source")
    capture = _ClassifyEndInvocationCapture()
    capture.record_original_owned_source(observation)
    context = runtime.bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("classifier reply refusal lost its actual current operation context")
    deadline = _original_action_deadline(phases, runtime, clock)
    with composer_quota_scope(phases.service._require_sessions_service(), context), prompt_guard_user_scope(runtime.bindings.message):
        outcome = await phases.service.resume_continuation_classifier_end(
            pending=pending,
            runtime=runtime,
            dispatch=dispatch,
            persist=persist,
            primary_response=None,
            completion_capture=capture,
            deadline=deadline,
            checkpoint_phase_owner=None,
        )
    if (
        type(outcome) is not _ClassifyOutcome
        or outcome.action != "return"
        or capture.terminal_kind != "owned_refusal"
        or capture.original_terminal is not None
    ):
        raise AuditIntegrityError("classifier reply refusal lost its actual unpaid terminal outcome")
    return _register_classification(source, outcome, capture, resumed_from=pending)


def _actual_end_input_binding(
    live: _LiveSource, pending: ClassifyPostPrimary | ClassifySourceProofStop, primary_response: PrivateChunk | None
) -> str | None:
    if live.dispatch.state is not live.runtime.live.state:
        raise AuditIntegrityError("classifier END input replaced its actual dispatch state")
    if type(pending) is ClassifySourceProofStop:
        if primary_response is not None:
            raise AuditIntegrityError("classifier source stop cannot invent a paid PRIMARY input")
        return live.runtime.live.turn_evidence.review_candidate(
            candidate=live.dispatch.assistant_message.content or "",
            request=live.runtime.bindings.message,
            current_state=live.dispatch.state,
        ).binding
    if type(pending) is not ClassifyPostPrimary or type(primary_response) is not PrivateChunk:
        raise AuditIntegrityError("classifier END input requires its actual original pending response")
    if primary_response.reference != pending.response:
        raise AuditIntegrityError("classifier END input substituted its original response reference")
    response = decode_continuation_response(primary_response)
    if response.tool_batch.calls:
        raise AuditIntegrityError("classifier model END input requires its original no-tool response")
    return live.runtime.live.turn_evidence.review_candidate(
        candidate=response.message.content or "",
        request=live.runtime.bindings.message,
        current_state=live.dispatch.state,
    ).binding


def observe_continuation_classifier_end_input(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    *,
    selected: SelectedContinuationState,
    pending: ClassifyPostPrimary | ClassifySourceProofStop,
    dispatch: _DispatchOutcome,
    persist: _PersistOutcome | ColdPostBatchPersistData,
    primary_response: PrivateChunk | None,
) -> ObservedIterationEndInput:
    """Seal the actual live model candidate binding before END may dispatch."""
    live = _live_source(phases, runtime, selected, dispatch, persist)
    clock = phases.action_clock
    if type(clock) is not ContinuationActionClock or clock.policy != runtime.source.policy.actions.advisor:
        raise AuditIntegrityError("classifier END input lost its original driver Advisor clock")
    _original_action_deadline(phases, runtime, clock)
    encoded = _pending_json(pending)
    if encoded is None:
        raise AuditIntegrityError("classifier END input lost its original closed pending value")
    binding = _actual_end_input_binding(live, pending, primary_response)
    observed = ObservedIterationEndInput()
    _end_inputs[observed] = _EndInputSource(
        live,
        pending,
        encoded,
        primary_response,
        runtime.live.turn_evidence,
        dispatch.state,
        runtime.bindings.message,
        clock,
        binding,
    )
    return observed


def require_original_iteration_end_input(
    observed: ObservedIterationEndInput,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    *,
    pending_json: str,
) -> str | None:
    """Revalidate the original live input; SQL separately proves its owner."""
    if type(observed) is not ObservedIterationEndInput or observed not in _end_inputs:
        raise AuditIntegrityError("classifier END input requires its original issued observation")
    source = _end_inputs[observed]
    live = source.live
    current = _live_source(live.phases, live.runtime, live.selected, live.dispatch, live.persist)
    if (
        current.running is not running
        or selected is not live.selected
        or live.runtime.live.turn_evidence is not source.evidence
        or live.dispatch.state is not source.state
        or live.runtime.bindings.message != source.request
        or live.phases.action_clock is not source.clock
        or type(pending_json) is not str
        or source.pending_json != pending_json
        or _pending_json(source.pending) != source.pending_json
        or _actual_end_input_binding(live, source.pending, source.response) != source.binding
    ):
        raise AuditIntegrityError("classifier END input changed its original live source or complete binding")
    _original_action_deadline(live.phases, live.runtime, source.clock)
    return source.binding


def _recorder_source(captured: CapturedContinuationRuntime) -> PrivateChunk:
    parent = captured.stack.frames[0]
    if type(parent) is not ComposeFrame:
        raise AuditIntegrityError("iteration return lost its captured Compose owner")
    found = tuple(chunk for chunk in captured.chunks if chunk.reference == parent.audit_buffer)
    if len(found) != 1:
        raise AuditIntegrityError("iteration return lost its exact captured recorder source")
    return found[0]


async def complete_continuation_iteration_return(
    observed: ObservedClassification, phases: ContinuationLivePhases, runtime: RestoredContinuationRuntime
) -> tuple[ObservedIterationReturn, CapturedContinuationRuntime]:
    source = _original_classification(observed)
    outcome = source.original
    if (
        source.live.phases is not phases
        or source.live.runtime is not runtime
        or source.return_started
        or type(outcome) is not _ClassifyOutcome
        or outcome.action != "return"
        or type(outcome.result) is not ComposerResult
    ):
        raise AuditIntegrityError("iteration return changed its original completed classifier")
    runtime, _selected = await _refresh_original_classification_source(source, phases, runtime)
    source.return_started = True
    service, live, bindings = phases.service, runtime.live, runtime.bindings
    context = bindings.session_operation_context
    if context is None:
        raise AuditIntegrityError("iteration return lost its original operation context")
    public_result = outcome.result
    with composer_quota_scope(service._require_sessions_service(), context), prompt_guard_user_scope(bindings.message):
        if public_result.graph_recovery_intent is None:
            if live.advisor_repair_context_introduced:
                public_result = await service._completion._qualified_advisor_repair_public_result(
                    public_result,
                    user_id=bindings.user_id,
                    session_id=bindings.session_id,
                    session_operation_context=context,
                    cache=live.runtime_preflight_cache,
                    initial_version=live.initial_version,
                    session_scope=bindings.session_scope,
                    plugin_snapshot=bindings.plugin_snapshot,
                    publication_binding=outcome.publication_binding,
                    turn_evidence=live.turn_evidence,
                    user_message=bindings.message,
                )
            public_result = service._completion._append_checkpoint_receipt(public_result, turn_evidence=live.turn_evidence)
    terminal = source.original_terminal
    if terminal is not None and terminal.advisor_review_state is not None:
        review_state = terminal.advisor_review_state
        if type(review_state) is not _AdvisorReviewState or review_state.completed_passes != live.advisor_checkpoint_passes_used:
            raise AuditIntegrityError("iteration return changed its original completed END review state or pass count")
        live.advisor_review_state = review_state
    live.turn_evidence.transcript.observe_messages(live.llm_messages)
    captured = await capture_continuation_runtime_owners(
        runtime, selected=source.live.selected, inventory=runtime.materialization.inventory
    )
    recorder = _recorder_source(captured)
    encoded = encode_retained_composer_result(RetainedComposerResult(public_result, outcome.publication_binding), recorder_source=recorder)
    disposition: Literal["clean", "blocked"] | None = None
    if terminal is not None:
        disposition = "clean" if terminal.end_disposition.value == "clean" else "blocked"
    primary = source.original_primary
    pending = source.resumed_from
    effect_key = primary.effect_key if primary is not None else pending.primary_effect_key if type(pending) is ClassifyPostPrimary else None
    input_sha = (
        primary.input_sha256 if primary is not None else pending.primary_input_sha256 if type(pending) is ClassifyPostPrimary else None
    )
    response = primary.response.reference if primary is not None else pending.response if type(pending) is ClassifyPostPrimary else None
    data = ObservedIterationReturnData(
        public_result,
        outcome.publication_binding,
        encoded,
        recorder,
        source.terminal_kind,
        disposition,
        effect_key,
        input_sha,
        response,
        source.owned_source_json,
    )
    returned = ObservedIterationReturn()
    _returns[returned] = _ReturnSource(observed, source.live, captured, data)
    return returned, captured


def require_original_iteration_return(
    observed: ObservedIterationReturn,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    captured: CapturedContinuationRuntime,
) -> ObservedIterationReturnData:
    original = _returns.get(observed) if type(observed) is ObservedIterationReturn else None
    if original is None:
        raise AuditIntegrityError("iteration return requires its original issued observation")
    _original_classification(original.classification)
    _require_current_capture(original.live, running, selected, captured, original.captured)
    data = original.data
    if (
        _recorder_source(captured) is not data.recorder_source
        or encode_retained_composer_result(
            RetainedComposerResult(data.result, data.publication_binding), recorder_source=data.recorder_source
        )
        != data.result_json
    ):
        raise AuditIntegrityError("iteration return changed its original complete result or recorder")
    return data
