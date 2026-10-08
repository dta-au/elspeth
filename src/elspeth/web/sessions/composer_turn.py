"""One detached freeform composer turn (spec §3; plan N11b).

``run_composer_turn`` rebuilds the bodies of ``POST /messages``
(``routes/messages.py`` ``send_message``) and ``POST /recompose``
(``routes/composer/compose.py`` ``recompose``) from the COMPOSE lease onward,
for the async worker (N12), which has no ``Request``:

* the per-session process lock, the COMPOSE ``SessionOperationLease`` (adopted
  from the start composite, R3) and the composer request lifecycle (E1) are the
  caller's;
* the start composite has already checked ownership, the bound base and, for
  recompose, the transcript (E5). The turn re-reads the head and the transcript
  only to assert that nothing moved under its own lease (E7): a difference is
  Tier 1, never a 4xx;
* success is published in one transaction with the assistant row, the turn
  audit cohort, the pending proposals and the terminal CAS (R2,
  ``complete_composer_async_operation``).

It keeps the routes' owned-child settlement custody. The send user-row insert
and the post-provider settlement each run as a lease-owned child joined through
``_join_freeform_owned_task``, so a cancel that lands while a child runs is
deferred until the child settled and then re-raised. A deferred cancel re-raised
after the success composite committed leaves ``request_lifecycle.durable_completed``
set: the job is ``completed`` and the worker writes no other terminal.

Success returns the settled ``ComposerOperationRecord``. Every failure
propagates for the worker to project (N10) and settle (N12):

- the typed ladder's ``HTTPException`` bodies, arm for arm with the routes;
- app-handler classes such as ``AuditIntegrityError`` or
  ``SessionOperationFenceLost``;
- ``ComposerOperationCancelledDuringTurn`` from a non-audit write or a terminal
  CAS that a committed cancel fenced (ruling 2, E10);
- ``ComposerTurnDeadlineExpired`` when the admitted deadline is spent before
  ``compose()`` could start (E14);
- ``asyncio.CancelledError`` after the cancelled arm joined its audit writes.

On every non-success exit the worker calls :func:`persist_cancelled_turn_audit`
with the job's :class:`ComposerTurnObservation` before any failure terminal, so
a result ``compose()`` already returned, or the exception-carried LLM calls of a
typed arm a committed cancel fenced, are never lost (F-B2/C2, B2-closure).
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from functools import partial
from typing import Final, Literal, TypedDict, final
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.composer_progress import ComposerProgressEvent, ComposerProgressSink
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord, compartment_ingress_record
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_planner import PipelinePlannerError
from elspeth.web.composer.protocol import (
    PIPELINE_STAGED_REVIEW_MESSAGE,
    ComposerAdmissionRefused,
    ComposerConvergenceError,
    ComposerPluginCrashError,
    ComposerResult,
    ComposerRuntimePreflightError,
    ComposerServiceError,
)
from elspeth.web.composer.provider_gateway import _BadRequestLLMError
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.execution.completion_gates import completion_gate_decision_changes, parse_completion_gates
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
    RequiredWorkSource,
)
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_app_services import ComposerAppServices
from elspeth.web.sessions.composer_operations import (
    COMPOSER_CANCEL_REQUESTED,
    COMPOSER_LEASE_LOST,
    COMPOSER_SHUTDOWN,
    ComposerOperationAssistantWrite,
    ComposerOperationCancel,
    ComposerOperationCancelledDuringTurn,
    ComposerOperationCancelReason,
    ComposerOperationFenceLost,
    ComposerOperationKind,
    ComposerOperationRecord,
    ComposerOperationRunning,
    ComposerOwnedSettlementFailure,
    ComposerRequiredRecoveryFailure,
    ComposerTurnDeadlineExpired,
)
from elspeth.web.sessions.protocol import (
    ChatMessageRecord,
    CompositionProposalRecord,
    CompositionStateRecord,
    MessageIngressFresh,
    SessionNotFoundError,
)
from elspeth.web.sessions.routes._helpers import (
    _COMPOSER_PERSIST_FAILED_DURING_UNWIND_COUNTER,
    _COMPOSER_REQUESTS_INFLIGHT,
    _COMPOSER_TIER1_VIOLATION_COUNTER,
    ComposerRequestLifecycle,
    _capture_freeform_child,
    _chat_ingress_inputs,
    _composer_chat_history,
    _composer_conversation_messages,
    _composer_heartbeat_cancel_of,
    _composer_heartbeat_failed_progress_event,
    _ComposerRequestTerminalStatus,
    _composition_proposal_response,
    _freeform_child_result,
    _handle_composer_chargeable_refusal,
    _handle_composer_provider_failure,
    _handle_convergence_error,
    _handle_planner_failure,
    _handle_plugin_crash,
    _handle_runtime_preflight_failure,
    _initial_composition_state,
    _join_freeform_owned_task,
    _join_shielded_task_after_cancellation,
    _litellm_error_detail,
    _llm_calls_from_exception,
    _message_response,
    _persist_llm_calls,
    _persist_turn_audit_cohort,
    _publish_progress,
    _record_composer_request_terminal,
    _record_composer_runtime_preflight_telemetry,
    _safe_frame_strings,
    _state_data_from_composer_state,
    _state_from_record,
    _state_response,
    client_cancelled_progress_event,
    composer_progress_sink_for_lease,
    composer_turn_end_assistant_row,
    convergence_progress_event,
    freeform_planner_progress_reason,
    llm_call_audit_envelope,
    llm_call_audit_summary,
    maybe_auto_title_session,
    merge_composer_meta_updates,
    slog,
)
from elspeth.web.sessions.routes.composer.pipeline_settlement import PipelineRouteSettlement, settle_auto_commit_intent
from elspeth.web.sessions.schemas import (
    CompositionStateResponse,
    MessageWithStateResponse,
    RecomposeRequest,
    SendMessageRequest,
)
from elspeth.web.sessions.service import ComposerRequiredAuditPersistenceError
from elspeth.web.sessions.titles import is_default_session_title

# The settlement's own TIMEOUT body (pipeline_settlement.py:283-287), pinned by
# test_settlement_timeout_detail_is_the_settlement_route_string.
_SETTLEMENT_TIMEOUT_DETAIL: Final = "Pipeline preparation timed out. Please retry this proposal."
_AUTO_TITLE_JOIN_SECONDS: Final = 2.0
_ADVISORY_PROGRESS_FAILURES: Final[tuple[type[Exception], ...]] = (
    ComposerRequestLeaseLost,
    PermissionError,
    SQLAlchemyError,
    AsyncWorkerAdmissionTimeoutError,
)


@final
@dataclass(frozen=True, slots=True)
class ComposerTurnInput:
    """The validated, request-free input of one detached turn (spec §3 typed worker context)."""

    session_id: UUID
    operation_id: str
    kind: ComposerOperationKind
    actor_user_id: str
    request: SendMessageRequest | RecomposeRequest
    request_id: str | None
    budget_seconds: float

    def __post_init__(self) -> None:
        if type(self.session_id) is not UUID:
            raise TypeError("ComposerTurnInput.session_id must be a UUID")
        if type(self.operation_id) is not str or str(UUID(self.operation_id)) != self.operation_id:
            raise ValueError("ComposerTurnInput.operation_id must be a canonical UUID string")
        expected_request: type[SendMessageRequest] | type[RecomposeRequest]
        if self.kind == "compose_message":
            expected_request = SendMessageRequest
        elif self.kind == "compose_recompose":
            expected_request = RecomposeRequest
        else:
            raise ValueError(f"ComposerTurnInput.kind {self.kind!r} is not a composer operation kind")
        if type(self.request) is not expected_request:
            raise TypeError(f"ComposerTurnInput.request for {self.kind} must be an exact {expected_request.__name__}")
        expected_request.model_validate(self.request.model_dump(mode="python"), strict=True)
        if self.request.operation_id != self.operation_id:
            raise ValueError("ComposerTurnInput.request.operation_id must be the turn's operation_id")
        if type(self.actor_user_id) is not str or not 1 <= len(self.actor_user_id) <= 128:
            raise ValueError("ComposerTurnInput.actor_user_id must be a bounded non-empty string")
        if self.request_id is not None and (type(self.request_id) is not str or not 1 <= len(self.request_id) <= 128):
            raise ValueError("ComposerTurnInput.request_id must be bounded or None")
        if type(self.budget_seconds) is not float or not math.isfinite(self.budget_seconds) or self.budget_seconds <= 0:
            raise ValueError("ComposerTurnInput.budget_seconds must be a positive finite float")


@final
@dataclass(frozen=True, slots=True)
class ComposerBudgetAnchor:
    """The job's remaining wall-clock budget, pinned at the ``running`` transition (E14; F-C5).

    The worker (N12) builds it from the start composite:
    ``remaining_at_running_seconds`` is ``deadline_at - started_at`` on the
    database clock, and ``monotonic_at_running`` is ``time.monotonic()`` taken
    when that composite returned. Setup after ``running`` (adopt, the request
    lifecycle, the preamble reads, the user-row insert) is therefore spent from
    the admitted deadline, never added to it.
    """

    remaining_at_running_seconds: float
    monotonic_at_running: float

    def __post_init__(self) -> None:
        if type(self.remaining_at_running_seconds) is not float or not math.isfinite(self.remaining_at_running_seconds):
            raise ValueError("ComposerBudgetAnchor.remaining_at_running_seconds must be a finite float")
        if type(self.monotonic_at_running) is not float or not math.isfinite(self.monotonic_at_running):
            raise ValueError("ComposerBudgetAnchor.monotonic_at_running must be a finite float")

    def remaining_seconds(self, *, monotonic_now: float) -> float:
        """The budget left at ``monotonic_now``; zero or negative once the deadline has passed."""
        if type(monotonic_now) is not float or not math.isfinite(monotonic_now) or monotonic_now < self.monotonic_at_running:
            raise ValueError("Composer budget observation must be finite and monotonic")
        return self.remaining_at_running_seconds - (monotonic_now - self.monotonic_at_running)


class ComposerPostComposeUpdates(TypedDict):
    repair_turns_used: int
    ingress: CompositionIngressRecord
    chat_ingress_inputs: list[ChatIngressInput]


@final
@dataclass(slots=True)
class ComposerTurnObservation:
    """What one running turn has produced so far, readable by the worker's job frame (F-B2/C2).

    Mutable by design; one per job. :func:`run_composer_turn` writes it at fixed
    points and the frame reads it after the turn ends by any route:

    - ``compose_result``: set on the first statement after ``compose()``
      returns, before any post-compose write, so a cancel anywhere in the tail
      (the title join, settlement, the state save, a composite that loses to a
      committed cancel) still leaves the provider evidence to
      :func:`persist_cancelled_turn_audit`;
    - ``pending_exception_llm_calls``: the LLM calls a typed arm holds on its
      exception, recorded as that arm's first statement, before its first
      session write (B2-closure). A committed cancel can fence the arm's first
      non-audit write (a handler's partial-state save) before its own cohort
      write runs;
    - ``audit_cohort_durable``: set on the line after each cohort persist the
      turn reaches returns: the success composite, each ``_handle_*`` handler
      whose last write is the cohort, each ladder ``_persist_llm_calls``, the
      cancelled arm's persist, and :func:`persist_cancelled_turn_audit`. The
      required detached-turn persists propagate storage failures and are
      joined to actual SQL completion, so this flag is a durability witness;
    - ``compose_base_state_id`` (CONTRACT DEVIATION D-N11b-1): the head the turn
      composes against, set immediately before ``compose()``. The frame's
      persist stamps LLM sidecars with it, as every other writer of those rows does.
    """

    compose_result: ComposerResult | None = None
    pending_exception_llm_calls: tuple[ComposerLLMCall, ...] = ()
    pending_exception_tool_invocations: tuple[ComposerToolInvocation, ...] = ()
    audit_cohort_durable: bool = False
    compose_base_state_id: UUID | None = None
    deferred_cancellation: asyncio.CancelledError | None = None
    required_work: RequiredWorkCoordinator | None = None


@final
@dataclass(frozen=True, slots=True)
class _ComposerTurnLabels:
    """Everything that differs between the two kinds besides the preamble; each is the route's exact string."""

    endpoint: Literal["send_message", "recompose"]
    telemetry_source: Literal["compose", "recompose"]
    provider_route: Literal["messages", "recompose"]
    convergence_log_prefix: Literal["convergence", "recompose_convergence"]
    handler_log_prefix: Literal["compose", "recompose"]
    llm_bad_request_event: Literal["compose_llm_bad_request", "recompose_llm_bad_request"]
    site: Literal["send_message", "recompose"]
    noun: Literal["request", "retry"]
    starting_headline: str
    starting_evidence: str
    settlement_task_name: str
    cancelled_llm_task_name: str
    cancelled_progress_task_name: str


_SEND_LABELS: Final = _ComposerTurnLabels(
    endpoint="send_message",
    telemetry_source="compose",
    provider_route="messages",
    convergence_log_prefix="convergence",
    handler_log_prefix="compose",
    llm_bad_request_event="compose_llm_bad_request",
    site="send_message",
    noun="request",
    starting_headline="I'm reading your request and current pipeline.",
    starting_evidence="The request was accepted for this session.",
    settlement_task_name="send-message-post-provider-settlement",
    cancelled_llm_task_name="send-message-cancelled-llm-call-persist",
    cancelled_progress_task_name="send-message-cancelled-progress-publish",
)
_RECOMPOSE_LABELS: Final = _ComposerTurnLabels(
    endpoint="recompose",
    telemetry_source="recompose",
    provider_route="recompose",
    convergence_log_prefix="recompose_convergence",
    handler_log_prefix="recompose",
    llm_bad_request_event="recompose_llm_bad_request",
    site="recompose",
    noun="retry",
    starting_headline="I'm rereading your request and current pipeline.",
    starting_evidence="The retry was accepted for this session.",
    settlement_task_name="recompose-post-provider-settlement",
    cancelled_llm_task_name="recompose-cancelled-llm-call-persist",
    cancelled_progress_task_name="recompose-cancelled-progress-publish",
)


_COMPOSER_OPERATION_CANCEL_REQUESTED: Final = COMPOSER_CANCEL_REQUESTED
_COMPOSER_OPERATION_LEASE_LOST: Final = COMPOSER_LEASE_LOST
_COMPOSER_OPERATION_SHUTDOWN: Final = COMPOSER_SHUTDOWN


def _composer_operation_cancel_of(exc: asyncio.CancelledError) -> ComposerOperationCancel | None:
    """Return the worker's cancel marker when ``exc`` was delivered with one."""
    if len(exc.args) != 1 or type(exc.args[0]) is not ComposerOperationCancel:
        return None
    return exc.args[0]


def _cancel_is_user_stop(exc: asyncio.CancelledError) -> bool:
    """E22: only the cancel endpoint's marker is a Stop; heartbeat, lease loss, shutdown and unmarked are server faults."""
    if _composer_heartbeat_cancel_of(exc) is not None:
        return False
    operation_cancel = _composer_operation_cancel_of(exc)
    return operation_cancel is not None and operation_cancel.reason is ComposerOperationCancelReason.CANCEL_REQUESTED


async def _discard_progress(event: ComposerProgressEvent) -> None:
    """The sink of a turn whose progress claim failed: advisory UX, never the outcome (E20)."""
    del event


async def _advisory_progress_sink(
    services: ComposerAppServices,
    *,
    request_lifecycle: ComposerRequestLifecycle,
    session_id: UUID,
    operation_id: str,
    running: ComposerOperationRunning,
    request_id: str,
    user_id: str,
) -> ComposerProgressSink:
    """E20: progress snapshots are advisory UX; the job row is the settlement authority (spec §2).

    Loss of the request lease, a revoked identity or session (the
    ``PermissionError`` family), a database fault or a worker-pool admission
    timeout on the claim or on a publish is logged and the turn continues. The
    lifecycle's heartbeat still cancels the turn if the request lease is truly
    gone. Any other exception is a defect and propagates.
    """
    try:
        sink = await composer_progress_sink_for_lease(
            services.progress_registry,
            lease=request_lifecycle.lease,
            session_id=str(session_id),
            request_id=request_id,
            user_id=user_id,
            operation_id=operation_id,
            session_operation_id=running.session_operation_context.fence.operation_id,
            session_operation_epoch=running.session_operation_context.fence.operation_epoch,
        )
    except _ADVISORY_PROGRESS_FAILURES as exc:
        slog.warning(
            "composer_operation.progress_claim_failed",
            session_id=str(session_id),
            operation_id=operation_id,
            exc_class=type(exc).__name__,
        )
        return _discard_progress

    async def publish(event: ComposerProgressEvent) -> None:
        try:
            await sink(event)
        except _ADVISORY_PROGRESS_FAILURES as exc:
            slog.warning(
                "composer_operation.progress_publish_failed",
                session_id=str(session_id),
                operation_id=operation_id,
                phase=event.phase,
                exc_class=type(exc).__name__,
            )

    return publish


def _message_with_state_response(
    assistant: ChatMessageRecord,
    proposals: tuple[CompositionProposalRecord, ...],
    current_state: CompositionStateRecord | None,
    *,
    state_response: CompositionStateResponse | None,
    live_validation: ValidationSummary | None,
) -> MessageWithStateResponse:
    """The route's success DTO (``messages.py:551-556``), built inside the terminal transaction."""
    if state_response is not None and (current_state is None or str(current_state.id) != state_response.id):
        raise AuditIntegrityError("Composer terminal state changed before validation projection")
    return MessageWithStateResponse(
        message=_message_response(assistant),
        state=_state_response(current_state, live_validation=live_validation)
        if state_response is not None and current_state is not None
        else None,
        proposals=[_composition_proposal_response(proposal) for proposal in proposals if proposal.status == "pending"],
    )


async def _join_auto_title(
    task: asyncio.Task[None],
    *,
    session_id: UUID,
    operation_id: str,
    cancellation_observations: list[asyncio.CancelledError],
) -> None:
    """Bounded, cancellation-safe join of the first-message auto-title child (E12; review m3).

    The child is lease-owned and writes the title under the COMPOSE fence, so it
    must finish before the terminal composite (ruling 2 refuses the write after
    it). It is finished before this returns or raises: done, or cancelled and
    given a bounded settle (its cancel arm settles the provider charge,
    ``_auto_title.py:435-446``, an audit-only write under E11).

    - Done with a result: ``task.result()`` re-raises a title failure into the
      turn, as ``messages.py:1039`` does today; the turn has not completed, so
      nothing completed is replaced.
    - Done cancelled: reject unknown accounting completion as an integrity failure.
    - Not done within ``_AUTO_TITLE_JOIN_SECONDS``: cancelled, settled and logged
      (``composer_operation.auto_title_join_timed_out``); the turn continues.
    - The turn itself cancelled while joining: the child is cancelled and
      settled before the ``CancelledError`` propagates, and that is logged
      (``composer_operation.auto_title_join_cancelled``).
    """
    try:
        done, _pending = await asyncio.wait({task}, timeout=_AUTO_TITLE_JOIN_SECONDS)
    except asyncio.CancelledError as cancellation:
        if all(cancellation is not earlier for earlier in cancellation_observations):
            cancellation_observations.append(cancellation)
        task.cancel()
        await _join_cancelled_auto_title(task, cancellation_observations=cancellation_observations)
        slog.warning(
            "composer_operation.auto_title_join_cancelled",
            session_id=str(session_id),
            operation_id=operation_id,
            child_settled=task.done(),
        )
        raise cancellation
    if task not in done:
        task.cancel()
        await _join_cancelled_auto_title(task, cancellation_observations=cancellation_observations)
        if cancellation_observations:
            slog.warning(
                "composer_operation.auto_title_join_cancelled",
                session_id=str(session_id),
                operation_id=operation_id,
                child_settled=task.done(),
            )
            raise cancellation_observations[0]
        slog.warning(
            "composer_operation.auto_title_join_timed_out",
            session_id=str(session_id),
            operation_id=operation_id,
            timeout_seconds=_AUTO_TITLE_JOIN_SECONDS,
            child_settled=task.done(),
        )
        return
    if task.cancelled():
        raise AuditIntegrityError("Auto-title child self-cancelled without accounting completion")
    _required_title_result(task)


def _required_title_result(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except (AuditIntegrityError, SQLAlchemyError):
        raise
    except Exception as exc:
        raise ComposerOwnedSettlementFailure() from exc


async def _join_cancelled_auto_title(task: asyncio.Task[None], *, cancellation_observations: list[asyncio.CancelledError]) -> None:
    """Join the physical child, retaining only cancellations delivered to this caller."""
    owner = asyncio.current_task()
    while not task.done():
        cancelling_before = owner.cancelling() if owner is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            if (not task.cancelled() or (owner is not None and owner.cancelling() > cancelling_before)) and all(
                cancellation is not earlier for earlier in cancellation_observations
            ):
                cancellation_observations.append(cancellation)
            continue
        except BaseException:
            if not task.done():
                raise
    try:
        if not task.cancelled():
            _required_title_result(task)
    except BaseException as child_failure:
        if cancellation_observations:
            raise BaseExceptionGroup(
                "Auto-title join retained child failure and caller cancellations", [child_failure, *cancellation_observations]
            ) from child_failure
        raise


async def _required_audit[T](observation: ComposerTurnObservation, work: Coroutine[object, object, T]) -> T:
    """Join required SQL fully and retain cancellation after its durable witness."""
    try:
        result, deferred = await _join_freeform_owned_task(asyncio.create_task(work, name="composer-required-audit"))
    except ComposerRequiredRecoveryFailure as exc:
        observation.audit_cohort_durable = exc.audit_cohort_durable
        raise
    except (
        AuditIntegrityError,
        SQLAlchemyError,
        HTTPException,
        ComposerOperationCancelledDuringTurn,
        ComposerOperationFenceLost,
        SessionOperationFenceLost,
        AsyncWorkerAdmissionTimeoutError,
    ):
        raise
    except Exception as exc:
        raise ComposerOwnedSettlementFailure() from exc
    observation.audit_cohort_durable = True
    if deferred is not None and observation.deferred_cancellation is None:
        observation.deferred_cancellation = deferred
    return result


def _turn_audit_cohort_drafts(
    tool_invocations: tuple[ComposerToolInvocation, ...],
    llm_calls: tuple[ComposerLLMCall, ...],
    *,
    tool_composition_state_id: UUID | None,
    llm_composition_state_id: UUID | None,
    parent_assistant_id: UUID | None,
) -> tuple[tuple[AuditMessageDraft, ...], tuple[PipelineDispatchAuditBinding, ...]]:
    """Preserve the production cohort redaction, locators and binding shapes."""
    drafts: list[AuditMessageDraft] = []
    bindings: list[PipelineDispatchAuditBinding] = []
    for invocation in tool_invocations:
        content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
        drafts.append(
            AuditMessageDraft(
                role="tool" if parent_assistant_id is not None else "audit",
                content=content,
                tool_calls=(envelope,),
                tool_call_id=invocation.tool_call_id if parent_assistant_id is not None else None,
                parent_assistant_id=str(parent_assistant_id) if parent_assistant_id is not None else None,
                composition_state_id=str(tool_composition_state_id) if tool_composition_state_id is not None else None,
            )
        )
        if invocation.tool_name == "set_pipeline" and invocation.status is ComposerToolStatus.SUCCESS:
            bindings.append(PipelineDispatchAuditBinding.from_persisted_envelope(envelope))
    for call in llm_calls:
        drafts.append(
            AuditMessageDraft(
                role="audit",
                content=llm_call_audit_summary(call),
                tool_calls=(llm_call_audit_envelope(call),),
                composition_state_id=str(llm_composition_state_id) if llm_composition_state_id is not None else None,
            )
        )
    return tuple(drafts), tuple(bindings)


async def persist_cancelled_turn_audit(
    services: ComposerAppServices,
    running: ComposerOperationRunning,
    observation: ComposerTurnObservation,
) -> None:
    """Persist a turn's not-yet-durable provider evidence before the worker's failure terminal (F-B2/C2; spec §4).

    What it writes is a precedence, never a union (B2-closure):

    - a compose result exists: that result's LLM calls, and its tool
      invocations unless the loop already persisted its tool-call turn;
    - otherwise ``observation.pending_exception_llm_calls``.

    Runtime-preflight path 2 has both sources, holding the same calls; the
    result wins, so they are never written twice. Tool rows are written with no
    parent, i.e. as ``role="audit"`` rows: the assistant row they would have
    parented never became durable, and a ``tool`` row must name one (Codex C1).
    LLM sidecars carry ``observation.compose_base_state_id``.

    A no-op when the cohort is already durable or when there is nothing to
    write. The write goes through ``add_messages_atomic(..., audit_only=True)``,
    which a committed cancel does not fence (E10/E11), and is joined through
    repeated cancellation of the caller. A database failure, a worker-pool
    admission timeout or a lapsed COMPOSE fence (``audit_only`` waives only the
    cancel predicate, never the fence) raises AuditIntegrityError. Required
    evidence cannot become an ordinary Stop outcome.
    """
    if observation.audit_cohort_durable:
        return
    result = observation.compose_result
    tool_invocations: tuple[ComposerToolInvocation, ...]
    llm_calls: tuple[ComposerLLMCall, ...]
    if result is not None:
        tool_invocations = result.tool_invocations if not result.persisted_tool_call_turn else ()
        llm_calls = result.llm_calls
    elif observation.pending_exception_llm_calls or observation.pending_exception_tool_invocations:
        tool_invocations = observation.pending_exception_tool_invocations
        llm_calls = observation.pending_exception_llm_calls
    else:
        return
    claim = running.claim
    rows = len(tool_invocations) + len(llm_calls)
    if not rows:
        observation.audit_cohort_durable = True
        return
    if observation.required_work is None:
        raise AuditIntegrityError("Cancelled-turn audit lacks its exact registered work owner")

    async def _persist() -> None:
        try:
            await _persist_turn_audit_cohort(
                services.session_service,
                claim.session_id,
                tool_invocations,
                llm_calls,
                tool_composition_state_id=observation.compose_base_state_id,
                llm_composition_state_id=observation.compose_base_state_id,
                plugin_crash_pending=True,
                required_audit=True,
                required_work=observation.required_work,
                session_operation_context=running.session_operation_context,
            )
        except (AuditIntegrityError, SQLAlchemyError, SessionOperationFenceLost, AsyncWorkerAdmissionTimeoutError) as exc:
            _COMPOSER_PERSIST_FAILED_DURING_UNWIND_COUNTER.add(rows, {"helper": "cancelled_turn_audit"})
            slog.error(
                "composer_operation.cancelled_turn_audit_persist_failed",
                session_id=str(claim.session_id),
                operation_id=claim.operation_id,
                rows=rows,
                calls=len(llm_calls),
                models_requested=[call.model_requested for call in llm_calls],
                exc_class=type(exc).__name__,
            )
            raise AuditIntegrityError("Required cancelled-turn evidence did not become durable") from exc
        observation.audit_cohort_durable = True

    await _join_shielded_task_after_cancellation(asyncio.create_task(_persist(), name="composer-operation-cancelled-turn-audit-persist"))


async def _run_composer_turn(
    services: ComposerAppServices,
    turn: ComposerTurnInput,
    *,
    lease: SessionOperationLease,
    running: ComposerOperationRunning,
    request_lifecycle: ComposerRequestLifecycle,
    observation: ComposerTurnObservation,
    budget_anchor: ComposerBudgetAnchor,
) -> ComposerOperationRecord:
    """Run one freeform send or recompose turn to its settled success; see the module docstring."""
    deferred_cancellation: asyncio.CancelledError | None = None
    continuation_cancellations: list[asyncio.CancelledError] = []
    auto_title_cancellations: list[asyncio.CancelledError] = []
    if running.claim.session_id != turn.session_id or running.claim.operation_id != turn.operation_id:
        raise AuditIntegrityError("composer turn input does not match its running operation")
    if lease.context != running.session_operation_context:
        raise AuditIntegrityError("composer turn lease is not the running operation's session operation context")
    if observation.required_work is None:
        required_work = lease.required_work
        observation.required_work = (
            RequiredWorkCoordinator(
                RequiredWorkAuthority(
                    RequiredAuthorityKind.DURABLE_COMPOSE, lease.context, running.claim.operation_id, running.claim.attempt
                )
            )
            if required_work is None
            else required_work
        )
    required_work = observation.required_work
    if type(required_work) is not RequiredWorkCoordinator:
        raise AuditIntegrityError("Composer turn requires an owned required-work coordinator")
    lease.bind_required_work(required_work)
    # E14: the worker fills both from the one start composite; a mismatch is a caller defect.
    if turn.budget_seconds != budget_anchor.remaining_at_running_seconds:
        raise ValueError("composer turn budget_seconds is not its budget anchor's remaining_at_running_seconds")
    if (
        observation.compose_result is not None
        or observation.pending_exception_llm_calls
        or observation.audit_cohort_durable
        or observation.compose_base_state_id is not None
    ):
        raise ValueError("composer turn observation must be fresh for each turn")
    if request_lifecycle.durable_completed:
        raise ValueError("composer turn request lifecycle is already durably completed")
    labels = _SEND_LABELS if turn.kind == "compose_message" else _RECOMPOSE_LABELS
    request = turn.request
    service = services.session_service
    settings = services.settings
    provider_binding = RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN, running)
    provider_owner = ProviderInvocationOwner(service=service, required_work=provider_binding)

    # The record is needed for its title (auto-title). Ownership is not
    # re-checked: E5 checked it in the start composite, under the fence this
    # turn now holds. A row gone since then reads as the same 404.
    try:
        session = await service.get_session(turn.session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail="Session not found") from None

    # E7: the start composite proved head == bound base under this fence, so the
    # loop seeds from the head exactly as the routes do (messages.py:206-218).
    state_record = await service.get_current_state(session.id)
    compose_base_state_id = state_record.id if state_record is not None else None
    if compose_base_state_id != request.state_id:
        raise AuditIntegrityError(
            f"composer operation {turn.operation_id}: the head under its adopted COMPOSE lease is not the "
            "bound base the start composite verified"
        )
    state = _initial_composition_state() if state_record is None else _state_from_record(state_record)
    # Tier 1, outside every try: a corrupt envelope must propagate (messages.py:219-223).
    prior_completion_gates_facts = parse_completion_gates(state_record.composer_meta) if state_record is not None else None
    plugin_snapshot = services.plugin_snapshot_for_user_id(turn.actor_user_id)
    profile_registry = services.operator_profile_registry

    # Per-kind preamble.
    if isinstance(request, SendMessageRequest):
        message_text = request.content
        # messages.py:236-259: user row + job user_message_id + ingress + transcript,
        # ONE transaction (E8), as an owned child joined through caller cancellation.
        ingress_sql, ingress_projection = observation.required_work.reserve_pair(
            RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.INGRESS_PROJECTION, transition_ordinal=0, semantic_ordinal=0
        )
        ingress = lease.create_task(
            _capture_freeform_child(
                service.add_message_with_transcript(
                    session.id,
                    "user",
                    request.content,
                    operation_id=UUID(turn.operation_id),
                    requested_state_id=request.state_id,
                    composition_state_id=compose_base_state_id,
                    writer_principal="route_user_message",
                    session_operation_context=lease.context,
                    running=running,
                    required_work=ingress_sql,
                )
            ),
            name="send-message-ingress",
        )
        try:
            ingress_outcome, ingress_cancellation = await _join_freeform_owned_task(ingress)
            ingress_result = _freeform_child_result(ingress_outcome)
        except BaseException:
            ingress_projection.complete_owned()
            raise
        try:
            if not isinstance(ingress_result, MessageIngressFresh):
                raise AuditIntegrityError(f"composer operation {turn.operation_id}: message ingress was not fresh for a running job")
            turn_user_message_id = ingress_result.message.id
            records = list(ingress_result.transcript)
        except BaseException as exc:
            ingress_projection.complete_owned(exc)
            raise
        ingress_projection.complete_owned()
        if ingress_cancellation is not None:
            raise ingress_cancellation
    else:
        # E5 verified exact latest-user and saved-outcome guards, including
        # interrupted tool prefixes. Assert that same target under our lease.
        records = await service.get_messages(session.id, limit=None)
        recompose_conversation = _composer_conversation_messages(records)
        latest_user = next((row for row in reversed(recompose_conversation) if row.role == "user"), None)
        if latest_user is None or latest_user.id != request.expected_user_message_id:
            raise AuditIntegrityError(
                f"composer operation {turn.operation_id}: the recompose transcript changed under its adopted "
                "COMPOSE lease after the start composite verified it"
            )
        latest_user_index = recompose_conversation.index(latest_user)
        if any(row.role == "assistant" and not row.tool_calls for row in recompose_conversation[latest_user_index + 1 :]):
            raise AuditIntegrityError("Recompose saved response appeared under its exact COMPOSE lease")
        proposals = await service.list_composition_proposals(session.id)
        if any(
            p.user_message_id == latest_user.id and p.pipeline_metadata is not None and p.status in ("pending", "committed")
            for p in proposals
        ):
            raise AuditIntegrityError("Recompose saved proposal appeared under its exact COMPOSE lease")
        message_text = latest_user.content
        turn_user_message_id = latest_user.id
    chat_ingress = compartment_ingress_record(message_text, own_compartment_id=settings.compartment_id)
    chat_ingress_inputs = _chat_ingress_inputs(records, own_compartment_id=settings.compartment_id)
    # E20: the progress request id is the persisted user-message id.
    progress_sink = await _advisory_progress_sink(
        services,
        request_lifecycle=request_lifecycle,
        session_id=session.id,
        operation_id=turn.operation_id,
        running=running,
        request_id=str(turn_user_message_id),
        user_id=turn.actor_user_id,
    )
    await _publish_progress(
        progress_sink,
        event=ComposerProgressEvent(
            phase="starting",
            headline=labels.starting_headline,
            evidence=(labels.starting_evidence,),
            likely_next="ELSPETH will prepare the composer prompt with the current pipeline.",
        ),
    )

    _COMPOSER_REQUESTS_INFLIGHT.add(1, {"endpoint": labels.endpoint})
    terminal_status: _ComposerRequestTerminalStatus = "failed"
    auto_title_task: asyncio.Task[None] | None = None
    try:
        if isinstance(request, SendMessageRequest):
            # messages.py:303-310: Tier-1 snapshot guard over CONVERSATION rows, inside the try.
            send_conversation = _composer_conversation_messages(records)
            if not send_conversation or send_conversation[-1].id != turn_user_message_id:
                raise AuditIntegrityError(
                    "Tier 1 audit anomaly: send_message transcript snapshot "
                    f"for session {session.id} does not end at inserted user "
                    f"message {turn_user_message_id}. Refusing to compose against "
                    "interleaved session history."
                )
        # Exclude only the turn's own user row; the history builder decodes the
        # full transcript's provider-control audit rows itself.
        has_partial_turn = isinstance(request, RecomposeRequest) and any(
            row.role == "assistant" for row in recompose_conversation[latest_user_index + 1 :]
        )
        chat_messages = _composer_chat_history(
            records if has_partial_turn else [record for record in records if record.id != turn_user_message_id]
        )

        # messages.py:335-355: first-message auto-title, a child of the lease.
        if isinstance(request, SendMessageRequest) and len(records) == 1 and is_default_session_title(session.title):
            title_custody = provider_owner.mint(ProviderInvocationFamily.TITLE)
            title_producer = required_work.reserve(
                RequiredWorkSource.TITLE_CONTINUATION_PRODUCER,
                transition_ordinal=title_custody.required_work.transition_ordinal,
                semantic_ordinal=title_custody.required_work.semantic_ordinal,
            )

            async def run_owned_title() -> None:
                try:
                    await maybe_auto_title_session(
                        service=service,
                        session_id=session.id,
                        user_message=request.content,
                        model=settings.composer_model,
                        temperature=settings.composer_temperature,
                        seed=settings.composer_seed,
                        session_operation_context=lease.context,
                        api_base=settings.composer_endpoint_base_url,
                        api_key=(
                            settings.composer_endpoint_api_key.get_secret_value()
                            if settings.composer_endpoint_api_key is not None
                            else None
                        ),
                        provider_custody=title_custody,
                    )
                except BaseException as failure:
                    title_producer.complete_owned(failure)
                    raise
                title_producer.complete_owned()

            try:
                auto_title_task = lease.create_task(run_owned_title())
            except BaseException as failure:
                title_producer.complete_owned(failure)
                raise

        composer = services.composer_service
        from openai import OpenAIError

        async def settle_post_provider(result: ComposerResult) -> ComposerOperationRecord:
            # messages.py:361-570 / compose.py:200-385, ending in the R2 composite.
            _post_compose_updates: ComposerPostComposeUpdates = {
                "repair_turns_used": result.repair_turns_used,
                "ingress": chat_ingress,
                "chat_ingress_inputs": chat_ingress_inputs,
            }
            _post_compose_meta = merge_composer_meta_updates(
                state_record.composer_meta if state_record is not None else None,
                _post_compose_updates,
            )
            state_response: CompositionStateResponse | None = None
            live_validation: ValidationSummary | None = None
            post_compose_state_id: UUID | None = compose_base_state_id
            assistant_record: ChatMessageRecord | None = None
            route_settlement: PipelineRouteSettlement | None = None
            if result.pipeline_commit_intent is not None:
                # E14: auto-commit settlement gets what is left of the job budget.
                commit_timeout_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())
                if commit_timeout_seconds <= 0:
                    raise HTTPException(status_code=504, detail=_SETTLEMENT_TIMEOUT_DETAIL)
                settlement_outcome = await settle_auto_commit_intent(
                    services=services,
                    user_id=turn.actor_user_id,
                    service=service,
                    session_id=session.id,
                    intent=result.pipeline_commit_intent,
                    composer_meta=_post_compose_meta,
                    telemetry_source=labels.telemetry_source,
                    session_operation_context=lease.context,
                    commit_timeout_seconds=commit_timeout_seconds,
                    running=running,
                    required_work=observation.required_work,
                    required_binding=provider_binding,
                )
                if type(settlement_outcome) is PipelineRouteSettlement:
                    route_settlement = settlement_outcome
                else:
                    # Auto-commit authority was durably revoked (elspeth-01d4c6e683).
                    result = replace(result, message=PIPELINE_STAGED_REVIEW_MESSAGE, pipeline_commit_intent=None)
            # Below the revoked branch on purpose (elspeth-d581b3da7f).
            turn_end = composer_turn_end_assistant_row(result)
            if route_settlement is not None:
                live_validation = route_settlement.validation
                state_response = _state_response(
                    route_settlement.settlement.state,
                    live_validation=route_settlement.validation,
                )
                post_compose_state_id = route_settlement.settlement.state.id
                assistant_record = route_settlement.settlement.transition_message
            elif result.state.version != state.version or completion_gate_decision_changes(
                prior_completion_gates_facts, result.advisor_gate_decision, result.state
            ):
                await _publish_progress(
                    progress_sink,
                    event=ComposerProgressEvent(
                        phase="validating",
                        headline="The composer is validating the pipeline and its review status.",
                        evidence=("The pipeline state and review outcome are being checked before persistence.",),
                        likely_next="ELSPETH will save the validated pipeline snapshot.",
                    ),
                )
                try:
                    state_data, validation = await _state_data_from_composer_state(
                        result.state,
                        settings=settings,
                        secret_service=services.scoped_secret_resolver,
                        user_id=turn.actor_user_id,
                        session_id=session.id,
                        plugin_snapshot=plugin_snapshot,
                        profile_registry=profile_registry,
                        catalog=services.catalog_service,
                        runtime_preflight=result.runtime_preflight,
                        preflight_exception_policy="raise",
                        initial_version=state.version,
                        telemetry_source=labels.telemetry_source,
                        composer_meta=_post_compose_meta,
                        advisor_gate_decision=result.advisor_gate_decision,
                    )
                except ComposerRuntimePreflightError as rpf_exc:
                    # Path 2 (post-compose runtime preflight).
                    rpf_exc = ComposerRuntimePreflightError(
                        original_exc=rpf_exc.original_exc,
                        partial_state=rpf_exc.partial_state,
                        tool_invocations=result.tool_invocations,
                        llm_calls=result.llm_calls,
                    )
                    # B2-closure: recorded before any write. These are
                    # result.llm_calls and the frame's persist prefers the
                    # result, so the two are never written together.
                    observation.pending_exception_llm_calls = _llm_calls_from_exception(rpf_exc)
                    await _publish_progress(
                        progress_sink,
                        event=ComposerProgressEvent(
                            phase="failed",
                            headline="The composer could not safely validate the pipeline update.",
                            evidence=("Runtime preflight failed during state persistence.",),
                            likely_next="Review the visible error message, then retry after the issue is resolved.",
                            reason="runtime_preflight_failed",
                        ),
                    )
                    response_body = await _required_audit(
                        observation,
                        _handle_runtime_preflight_failure(
                            rpf_exc,
                            service,
                            session.id,
                            turn.actor_user_id,
                            labels.handler_log_prefix,
                            compose_base_state_id,
                            settings=settings,
                            secret_service=services.scoped_secret_resolver,
                            plugin_snapshot=plugin_snapshot,
                            profile_registry=profile_registry,
                            catalog=services.catalog_service,
                            session_operation_context=lease.context,
                            ingress=chat_ingress,
                            chat_ingress_inputs=chat_ingress_inputs,
                            required_audit=True,
                            required_work=required_work,
                        ),
                    )
                    # VERIFY B2 gap: the handler just persisted result.llm_calls as
                    # its last write; without this the frame writes them twice.
                    observation.audit_cohort_durable = True
                    raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc
                await _publish_progress(
                    progress_sink,
                    event=ComposerProgressEvent(
                        phase="saving",
                        headline="ELSPETH is saving the pipeline and review status.",
                        evidence=("A new composition state version is being stored for this session.",),
                        likely_next="The assistant response will appear after the save completes.",
                    ),
                )
                checkpoint_sql, checkpoint_projection = required_work.reserve_pair(
                    RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,
                    RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION,
                    transition_ordinal=0,
                    semantic_ordinal=0,
                )
                try:
                    new_state_record = await service.save_composition_state(
                        session.id,
                        state_data,
                        provenance="post_compose",
                        session_operation_context=lease.context,
                        required_work=checkpoint_sql,
                    )
                except BaseException:
                    checkpoint_projection.complete_owned()
                    raise
                try:
                    state_response = _state_response(new_state_record, live_validation=validation)
                    live_validation = validation
                    post_compose_state_id = new_state_record.id
                except BaseException as exc:
                    checkpoint_projection.complete_owned(exc)
                    raise
                checkpoint_projection.complete_owned()
            # R2: the assistant row (unless the settlement wrote it), the turn audit
            # cohort, the pending proposals and the terminal CAS in ONE transaction.
            assistant_write: ComposerOperationAssistantWrite | None
            parent_assistant_id: UUID
            if assistant_record is None:
                parent_assistant_id = uuid4()
                assistant_write = ComposerOperationAssistantWrite(
                    message_id=parent_assistant_id,
                    content=turn_end.content,
                    raw_content=turn_end.raw_content,
                    composition_state_id=post_compose_state_id,
                )
            else:
                assistant_write = None
                parent_assistant_id = assistant_record.id
            audit_cohort, _pipeline_bindings = _turn_audit_cohort_drafts(
                result.tool_invocations if not result.persisted_tool_call_turn else (),
                result.llm_calls,
                tool_composition_state_id=post_compose_state_id,
                llm_composition_state_id=compose_base_state_id,
                parent_assistant_id=parent_assistant_id,
            )
            # Joined through cancellation: a lease-loss cancel of this child must not
            # leave the composite's worker thread committing while the frame decides
            # the terminal and persists the cancelled-turn audit. Its own outcome is
            # authoritative: it commits (completed wins), or raises
            # ComposerOperationCancelledDuringTurn / a fence loss having written nothing.
            record = await _join_shielded_task_after_cancellation(
                asyncio.create_task(
                    service.complete_composer_async_operation(
                        running,
                        assistant=assistant_write,
                        assistant_record=assistant_record,
                        audit_cohort=audit_cohort,
                        audit_composition_state_id=None,
                        required_work=observation.required_work,
                        build_response=partial(
                            _message_with_state_response, state_response=state_response, live_validation=live_validation
                        ),
                    ),
                    name="composer-operation-terminal-composite",
                )
            )
            observation.audit_cohort_durable = True
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="complete",
                    headline="The composer has updated the pipeline."
                    if result.state.version != state.version
                    else "The composer response is ready.",
                    evidence=("The assistant response has been saved for this session.",),
                    likely_next="Review the response and current pipeline.",
                    reason="composer_complete",
                ),
            )
            return record

        post_provider_error: BaseException | None = None
        settled_record: ComposerOperationRecord | None = None
        # E14/F-C5: what is left of the admitted deadline NOW, with no await between
        # this measurement and the provider call.
        compose_budget_seconds = budget_anchor.remaining_seconds(monotonic_now=time.monotonic())
        observation.compose_base_state_id = compose_base_state_id
        if compose_budget_seconds <= 0:
            terminal_status = "timed_out"
            await _publish_progress(progress_sink, event=convergence_progress_event(budget_exhausted="timeout"))
            raise ComposerTurnDeadlineExpired(
                session_id=turn.session_id,
                operation_id=turn.operation_id,
                remaining_seconds=compose_budget_seconds,
                budget_seconds_at_running=budget_anchor.remaining_at_running_seconds,
            )
        try:
            # Awaited inline in the turn task: attach_llm_calls rides on the
            # CancelledError instance and a task boundary would drop it.
            result = await composer.compose(
                message_text,
                chat_messages,
                state,
                session_id=str(turn.session_id),
                current_state_id=str(compose_base_state_id) if compose_base_state_id is not None else None,
                user_id=turn.actor_user_id,
                progress=progress_sink,
                session_operation_context=lease.context,
                user_message_id=str(turn_user_message_id),
                completion_gates=prior_completion_gates_facts,
                budget_seconds=compose_budget_seconds,
                required_work=provider_binding,
                provider_owner=provider_owner,
            )
            # F-B2/C2: first statement after compose() returns.
            observation.compose_result = result
            if auto_title_task is not None:
                # E12: joined before the terminal composite. The reference is dropped
                # only once the join finished by any route (m3), so the outer finally
                # never joins it twice and a cancel here never orphans the child.
                try:
                    await _join_auto_title(
                        auto_title_task,
                        session_id=turn.session_id,
                        operation_id=turn.operation_id,
                        cancellation_observations=auto_title_cancellations,
                    )
                finally:
                    auto_title_task = None
            continuation = lease.create_task(
                _capture_freeform_child(settle_post_provider(result)),
                name=labels.settlement_task_name,
            )
            try:
                outcome, deferred_cancellation = await _join_freeform_owned_task(
                    continuation, cancellation_observations=continuation_cancellations
                )
                receipt = _freeform_child_result(outcome)
            except (
                AuditIntegrityError,
                SQLAlchemyError,
                HTTPException,
                InvariantError,
                ComposerConvergenceError,
                ComposerRuntimePreflightError,
                ComposerPluginCrashError,
                ComposerServiceError,
                PipelinePlannerError,
                ChargeableAdmissionRefused,
                ComposerAdmissionRefused,
                OpenAIError,
                _BadRequestLLMError,
                ComposerOperationCancelledDuringTurn,
                ComposerOperationFenceLost,
                SessionOperationFenceLost,
                AsyncWorkerAdmissionTimeoutError,
                asyncio.CancelledError,
            ) as exc:
                post_provider_error = exc
            except Exception as exc:
                post_provider_error = ComposerOwnedSettlementFailure()
                post_provider_error.__cause__ = exc
            else:
                settled_record = receipt
                request_lifecycle.durable_completed = True
                terminal_status = "completed"
        except asyncio.CancelledError as turn_cancel:
            if post_provider_error is not None:
                raise post_provider_error from turn_cancel
            raise
        except ComposerConvergenceError as exc:
            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)
            terminal_status = "timed_out" if exc.budget_exhausted == "timeout" else "failed"
            await _publish_progress(
                progress_sink,
                event=convergence_progress_event(budget_exhausted=exc.budget_exhausted),
            )
            observation.pending_exception_tool_invocations = exc.tool_invocations if exc.failed_turn is None else ()
            response_body = await _required_audit(
                observation,
                _handle_convergence_error(
                    exc,
                    service,
                    session.id,
                    turn.actor_user_id,
                    labels.convergence_log_prefix,
                    compose_base_state_id,
                    settings=settings,
                    secret_service=services.scoped_secret_resolver,
                    plugin_snapshot=plugin_snapshot,
                    profile_registry=profile_registry,
                    catalog=services.catalog_service,
                    session_operation_context=lease.context,
                    ingress=chat_ingress,
                    chat_ingress_inputs=chat_ingress_inputs,
                    budget_seconds=compose_budget_seconds,  # D10: the budget compose() actually received
                    required_audit=True,
                    required_work=required_work,
                ),
            )
            observation.audit_cohort_durable = True
            raise HTTPException(status_code=422, detail=response_body) from exc
        except OpenAIError as exc:
            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)
            provider_failure = await _required_audit(
                observation,
                _handle_composer_provider_failure(
                    exc,
                    route=labels.provider_route,
                    service=service,
                    session_id=session.id,
                    composition_state_id=compose_base_state_id,
                    progress_sink=progress_sink,
                    session_operation_context=lease.context,
                    expose_provider_error=settings.composer_expose_provider_errors,
                    required_audit=True,
                    required_work=observation.required_work,
                ),
            )
            observation.audit_cohort_durable = True
            raise provider_failure from exc
        except _BadRequestLLMError as exc:
            llm_calls = _llm_calls_from_exception(exc)
            observation.pending_exception_llm_calls = llm_calls
            slog.error(
                labels.llm_bad_request_event,
                session_id=str(turn.session_id),
                exc_class=type(exc).__name__,
            )
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline=f"The composer model rejected this {labels.noun}.",
                    evidence=("The model provider rejected the composer request as invalid.",),
                    likely_next="Check the composer provider configuration and request options before retrying.",
                    reason="provider_unavailable",
                ),
            )
            if llm_calls:
                await _required_audit(
                    observation,
                    _persist_llm_calls(
                        service,
                        session.id,
                        llm_calls,
                        compose_base_state_id,
                        plugin_crash_pending=True,
                        session_operation_context=lease.context,
                        required_audit=True,
                        required_work=observation.required_work,
                    ),
                )
                observation.audit_cohort_durable = True
            raise HTTPException(
                status_code=502,
                detail=_litellm_error_detail(
                    "llm_unavailable",
                    exc,
                    expose_provider_error=settings.composer_expose_provider_errors,
                ),
            ) from exc
        except ComposerPluginCrashError as crash:
            # MUST precede the generic ComposerServiceError arm (messages.py:697-721).
            observation.pending_exception_llm_calls = _llm_calls_from_exception(crash)
            observation.pending_exception_tool_invocations = crash.tool_invocations if crash.failed_turn is None else ()
            response_body = await _required_audit(
                observation,
                _handle_plugin_crash(
                    crash,
                    service,
                    session.id,
                    turn.actor_user_id,
                    labels.handler_log_prefix,
                    compose_base_state_id,
                    settings=settings,
                    secret_service=services.scoped_secret_resolver,
                    plugin_snapshot=plugin_snapshot,
                    profile_registry=profile_registry,
                    catalog=services.catalog_service,
                    session_operation_context=lease.context,
                    ingress=chat_ingress,
                    chat_ingress_inputs=chat_ingress_inputs,
                    required_audit=True,
                    required_work=required_work,
                ),
            )
            observation.audit_cohort_durable = True
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline=f"The composer could not safely finish this {labels.noun}.",
                    evidence=("A pipeline tool failed on the server side.",),
                    likely_next="Review the visible error message, then retry after the issue is resolved.",
                    reason="plugin_crash",
                ),
            )
            raise HTTPException(status_code=500, detail=response_body) from crash.original_exc
        except ComposerRuntimePreflightError as rpf_exc:
            # Path 1 (cached preflight re-raised by compose()); telemetry primacy
            # as messages.py:773-777 (elspeth-0891e8da73).
            observation.pending_exception_llm_calls = _llm_calls_from_exception(rpf_exc)
            _record_composer_runtime_preflight_telemetry(
                "exception",
                source="cached_preflight",
                exception_class=rpf_exc.exc_class,
            )
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline=f"The composer could not safely finish this {labels.noun}.",
                    evidence=("Runtime preflight failed before the compose loop returned.",),
                    likely_next="Review the visible error message, then retry after the issue is resolved.",
                    reason="runtime_preflight_failed",
                ),
            )
            observation.pending_exception_tool_invocations = rpf_exc.tool_invocations
            response_body = await _required_audit(
                observation,
                _handle_runtime_preflight_failure(
                    rpf_exc,
                    service,
                    session.id,
                    turn.actor_user_id,
                    labels.handler_log_prefix,
                    compose_base_state_id,
                    settings=settings,
                    secret_service=services.scoped_secret_resolver,
                    plugin_snapshot=plugin_snapshot,
                    profile_registry=profile_registry,
                    catalog=services.catalog_service,
                    session_operation_context=lease.context,
                    ingress=chat_ingress,
                    chat_ingress_inputs=chat_ingress_inputs,
                    required_audit=True,
                    required_work=required_work,
                ),
            )
            observation.audit_cohort_durable = True
            raise HTTPException(status_code=500, detail=response_body) from rpf_exc.original_exc
        except PipelinePlannerError as exc:
            # elspeth-54c11243a3. D7 (visible recompose change): both kinds now use
            # send's COST_UNAVAILABLE copy and actor attribution (messages.py:819-837).
            # The planner's calls are already durable (llm_calls_durable), so this
            # records () and _handle_planner_failure persists none.
            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline=f"The composer could not build a pipeline for this {labels.noun}.",
                    evidence=(
                        "Cost accounting could not admit the model response."
                        if exc.code == "COST_UNAVAILABLE"
                        else "The composer model did not return a usable pipeline plan.",
                    ),
                    likely_next=(
                        "Ask an administrator to configure or correct model pricing before trying again."
                        if exc.code == "COST_UNAVAILABLE"
                        else "Retry the request; if it keeps failing, simplify it or check the composer provider."
                    ),
                    reason=freeform_planner_progress_reason(exc.code),
                ),
            )
            status_code, planner_response_body = await _required_audit(
                observation,
                _handle_planner_failure(
                    exc,
                    service,
                    session.id,
                    compose_base_state_id,
                    session_operation_context=lease.context,
                    required_audit=True,
                    required_work=observation.required_work,
                ),
            )
            raise HTTPException(status_code=status_code, detail=planner_response_body) from exc
        except ChargeableAdmissionRefused as exc:
            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)
            chargeable_refusal = await _required_audit(
                observation,
                _handle_composer_chargeable_refusal(
                    exc,
                    service=service,
                    session_id=session.id,
                    composition_state_id=compose_base_state_id,
                    progress_sink=progress_sink,
                    session_operation_context=lease.context,
                    required_audit=True,
                    required_work=observation.required_work,
                ),
            )
            observation.audit_cohort_durable = True
            raise chargeable_refusal from exc
        except ComposerAdmissionRefused as exc:
            observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline="This request was refused by the admission policy.",
                    evidence=(str(exc),),
                    likely_next="Ask an administrator to review your access and quota configuration.",
                    reason="admission_refused",
                ),
            )
            if isinstance(exc, CredentialMaterialRefused):
                raise HTTPException(status_code=422, detail=exc.to_payload()) from exc
            raise HTTPException(
                status_code=403,
                detail={"error_type": "composer_admission_refused", "failure_code": "admission_refused", "detail": str(exc)},
            ) from exc
        except ComposerServiceError as exc:
            llm_calls = _llm_calls_from_exception(exc)
            observation.pending_exception_llm_calls = llm_calls
            await _publish_progress(
                progress_sink,
                event=ComposerProgressEvent(
                    phase="failed",
                    headline=f"The composer could not finish this {labels.noun}.",
                    evidence=("Prompt preparation or composer service setup failed.",),
                    likely_next="Retry once the composer service is available.",
                    reason="service_setup_failed",
                ),
            )
            if llm_calls:
                await _required_audit(
                    observation,
                    _persist_llm_calls(
                        service,
                        session.id,
                        llm_calls,
                        compose_base_state_id,
                        plugin_crash_pending=True,
                        session_operation_context=lease.context,
                        required_audit=True,
                        required_work=observation.required_work,
                    ),
                )
                observation.audit_cohort_durable = True
            raise HTTPException(
                status_code=502,
                detail={"error_type": "composer_error", "detail": str(exc)},
            ) from exc
        finally:
            # Unknown first-party faults keep unwinding with their exact type; this
            # drains only their attached LLM sidecars (messages.py:896-918). A typed
            # arm above already raised a fresh HTTPException, which carries none.
            current_exc = sys.exception()
            drained_calls = (
                () if current_exc is None or isinstance(current_exc, asyncio.CancelledError) else _llm_calls_from_exception(current_exc)
            )
            if drained_calls:
                observation.pending_exception_llm_calls = drained_calls
                await _required_audit(
                    observation,
                    _persist_llm_calls(
                        service,
                        session.id,
                        drained_calls,
                        compose_base_state_id,
                        plugin_crash_pending=True,
                        session_operation_context=lease.context,
                        required_audit=True,
                        required_work=observation.required_work,
                    ),
                )
                observation.audit_cohort_durable = True
        if post_provider_error is not None:
            raise post_provider_error
        if settled_record is None:
            raise InvariantError("Provider returned without a freeform continuation receipt")
        if deferred_cancellation is not None:
            # Owned-child custody: the cancel was deferred past the committed
            # composite; durable_completed tells the worker completed won.
            raise deferred_cancellation
        if observation.deferred_cancellation is not None:
            raise observation.deferred_cancellation
        return settled_record
    except ComposerRequiredAuditPersistenceError as exc:
        _COMPOSER_TIER1_VIOLATION_COUNTER.add(1, {"helper": exc.helper})
        raise
    except InvariantError as exc:
        slog.error(
            "composer.invariant_violated",
            session_id=str(turn.session_id),
            user_id=turn.actor_user_id,
            exc_class=type(exc).__name__,
            site=labels.site,
            frames=_safe_frame_strings(exc),
        )
        original_outcomes: list[BaseException] = [exc]
        for cancelled in (*continuation_cancellations, deferred_cancellation, observation.deferred_cancellation):
            if cancelled is not None and all(cancelled is not original for original in original_outcomes):
                original_outcomes.append(cancelled)
        cause = (
            exc
            if len(original_outcomes) == 1
            else BaseExceptionGroup("Invariant failure retained original deferred cancellation", original_outcomes)
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_type": "server_invariant_violated",
                "detail": "Server invariant violated. See application audit log for diagnostic detail.",
            },
        ) from cause
    except asyncio.CancelledError as exc:
        if request_lifecycle.durable_completed:
            terminal_status = "completed"
            if len(auto_title_cancellations) > 1:
                raise BaseExceptionGroup("Auto-title join retained caller cancellations", auto_title_cancellations) from exc
            raise
        # messages.py:953-1017, both kinds: each write runs as its own task joined
        # through the shielded join, so it is durable before the re-raise and
        # therefore before the worker's terminal. The attached calls ride only on
        # compose()'s own CancelledError, when there is no compose result.
        llm_calls = _llm_calls_from_exception(exc)
        observation.pending_exception_llm_calls = llm_calls
        try:
            if llm_calls:
                await _join_shielded_task_after_cancellation(
                    asyncio.create_task(
                        _persist_llm_calls(
                            service,
                            session.id,
                            llm_calls,
                            compose_base_state_id,
                            plugin_crash_pending=True,
                            session_operation_context=lease.context,
                            required_audit=True,
                            required_work=observation.required_work,
                        ),
                        name=labels.cancelled_llm_task_name,
                    ),
                    primary_cancellation=exc,
                )
                observation.audit_cohort_durable = True
            user_stop = _cancel_is_user_stop(exc)
            await _join_shielded_task_after_cancellation(
                asyncio.create_task(
                    _publish_progress(
                        progress_sink,
                        event=client_cancelled_progress_event() if user_stop else _composer_heartbeat_failed_progress_event(),
                    ),
                    name=labels.cancelled_progress_task_name,
                ),
                primary_cancellation=exc,
            )
        except BaseException as cleanup_failure:
            if len(auto_title_cancellations) > 1:
                raise BaseExceptionGroup(
                    "Composer cancellation cleanup retained auto-title caller cancellations",
                    [cleanup_failure, *auto_title_cancellations],
                ) from cleanup_failure
            raise
        terminal_status = "cancelled" if user_stop else "failed"
        if len(auto_title_cancellations) > 1:
            raise BaseExceptionGroup("Auto-title join retained caller cancellations", auto_title_cancellations) from exc
        raise
    finally:
        _COMPOSER_REQUESTS_INFLIGHT.add(-1, {"endpoint": labels.endpoint})
        _record_composer_request_terminal(terminal_status, endpoint=labels.endpoint)
        if auto_title_task is not None:
            # Every exit that did not reach the pre-terminal join (m3).
            original = sys.exception()
            try:
                await _join_auto_title(
                    auto_title_task,
                    session_id=turn.session_id,
                    operation_id=turn.operation_id,
                    cancellation_observations=auto_title_cancellations,
                )
            except BaseException as title_failure:
                retained: list[BaseException] = []
                for retained_error in (original, title_failure, *auto_title_cancellations):
                    if retained_error is not None and all(retained_error is not earlier for earlier in retained):
                        retained.append(retained_error)
                if len(retained) > 1:
                    raise BaseExceptionGroup("Composer turn retained original and auto-title join failures", retained) from title_failure
                raise


async def run_composer_turn(
    services: ComposerAppServices,
    turn: ComposerTurnInput,
    *,
    lease: SessionOperationLease,
    running: ComposerOperationRunning,
    request_lifecycle: ComposerRequestLifecycle,
    observation: ComposerTurnObservation,
    budget_anchor: ComposerBudgetAnchor,
) -> ComposerOperationRecord:
    if observation.required_work is None:
        required_work = lease.required_work
        observation.required_work = (
            RequiredWorkCoordinator(
                RequiredWorkAuthority(
                    RequiredAuthorityKind.DURABLE_COMPOSE, lease.context, running.claim.operation_id, running.claim.attempt
                )
            )
            if required_work is None
            else required_work
        )
    required_work = observation.required_work
    if type(required_work) is not RequiredWorkCoordinator:
        raise AuditIntegrityError("Composer turn requires an owned required-work coordinator")
    lease.bind_required_work(required_work)
    return await _run_composer_turn(
        services,
        turn,
        lease=lease,
        running=running,
        request_lifecycle=request_lifecycle,
        observation=observation,
        budget_anchor=budget_anchor,
    )
