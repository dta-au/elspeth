"""Shared Composer turn carriers used by finalization and Continuation.

The kept types describe one admitted request and the observation that its
finalization (``composer_finalization.settle_composer_result``) publishes.
``_read_retained_operation_context`` reads the retained operation history for
Continuation initialization.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Final, Literal, NotRequired, TypedDict, final
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError, run_required_sql_finish_once
from elspeth.web.compartments import ChatIngressInput, CompositionIngressRecord
from elspeth.web.composer.protocol import (
    ComposerResult,
    GraphRecoveryResponse,
    RetainedOperationContext,
)
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.execution.completion_gates import (
    CompletionGatesDict,
)
from elspeth.web.required_sql_outcomes import RequiredSQLRaised
from elspeth.web.required_work import (
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkSource,
)
from elspeth.web.sessions.composer_finalization import (
    _message_with_state_response as _message_with_state_response,
)
from elspeth.web.sessions.composer_finalization import (
    _turn_audit_cohort_drafts as _turn_audit_cohort_drafts,
)
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationFenceLost,
    ComposerOperationKind,
    ComposerOwnedSettlementFailure,
    ComposerRequiredRecoveryFailure,
)
from elspeth.web.sessions.protocol import (
    ChatMessageRecord,
    CompositionProposalRecord,
    CompositionStateRecord,
)
from elspeth.web.sessions.routes._helpers import (
    _composition_proposal_response,
    _join_freeform_owned_task,
    _message_response,
    _state_response,
)
from elspeth.web.sessions.schemas import (
    MessageWithStateResponse,
    RecomposeRequest,
    SendMessageRequest,
)

# The settlement's own TIMEOUT body (pipeline_settlement.py:283-287), pinned by
# test_settlement_timeout_detail_is_the_settlement_route_string.
_SETTLEMENT_TIMEOUT_DETAIL: Final = "Pipeline preparation timed out. Please retry this proposal."


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
    completion_gates: NotRequired[CompletionGatesDict]


@final
@dataclass(slots=True)
class ComposerTurnObservation:
    """What one running turn has produced so far, readable by the worker's job frame (F-B2/C2).

    Mutable by design; one per job. :func:`run_composer_turn` writes it at fixed
    points and the frame reads it after the turn ends by any route:

    - ``compose_result``: set on the first statement after ``compose()``
      returns, before any post-compose write, so a cancel anywhere in the tail
      (settlement, the state save, a composite that loses to a
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

    compose_result: ComposerResult | GraphRecoveryResponse | None = None
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




def _graph_recovery_response(
    assistant: ChatMessageRecord,
    proposals: tuple[CompositionProposalRecord, ...],
    current_state: CompositionStateRecord | None,
) -> MessageWithStateResponse:
    """Publish editable recovery without executing construction validation."""
    return MessageWithStateResponse(
        message=_message_response(assistant),
        state=_state_response(current_state) if current_state is not None else None,
        proposals=[_composition_proposal_response(proposal) for proposal in proposals if proposal.status == "pending"],
    )


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


async def _read_retained_operation_context(
    *,
    authority: ComposerAsyncOperationAuthority,
    binding: RequiredWorkBinding,
    session_id: UUID,
    actor_user_id: str,
) -> RetainedOperationContext:
    """Hand actual required history SQL/projection outcomes to the owning turn."""
    sql, projection = binding.reserve_pair(
        RequiredWorkSource.RETAINED_OPERATION_READ_SQL, RequiredWorkSource.RETAINED_OPERATION_READ_PROJECTION
    )
    try:
        outcome = await run_required_sql_finish_once(
            sql, authority.list_failure_outcomes, session_id=session_id, actor_user_id=actor_user_id, limit=3
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    context: RetainedOperationContext | None = None
    if isinstance(outcome, RequiredSQLRaised):
        projection.complete_without_submission()
        errors.append(outcome.error)
    else:
        projection.begin_projection()
        try:
            outcomes, cursor = outcome.value
            context = RetainedOperationContext(
                session_id=session_id,
                actor_user_id=actor_user_id,
                availability="available",
                outcomes=outcomes,
                has_more=cursor is not None,
            )
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    errors.extend(outcome.deferred_cancellations)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Retained operation history original outcomes", errors)
    if context is None:
        raise AuditIntegrityError("Retained operation history lost its projected outcome")
    return context
