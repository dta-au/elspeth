"""Proposal, error, continuation-budget and conversation-context carriers.

These types outlived the empty-state pipeline planner, whose route and escape
hatch were retired (U-30, H2): every request is now authored by the compose
loop. The module keeps its historical name because the session service,
routes and tool batch import these carriers from here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, NotRequired, TypedDict, cast, final

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.pipeline_proposal import PipelineProposal
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.turn_effects import AdvisorPublicationReview, TurnEvidence


class PlannerPriorUserRequestDict(TypedDict):
    """Provider projection of one authoritative earlier user request."""

    history_index: int
    content: str


class PlannerConversationContextDict(TypedDict):
    """Earlier requests and separately attributed assistant context."""

    prior_user_requests: list[PlannerPriorUserRequestDict]
    additional_prior_user_requests_omitted: int
    prior_assistant_messages: NotRequired[list[PlannerPriorAssistantMessageDict]]


@dataclass(frozen=True, slots=True)
class PlannerPriorUserRequest:
    """One user-authored history entry retained for planner intent custody."""

    history_index: int
    content: str

    def __post_init__(self) -> None:
        if type(self.history_index) is not int or self.history_index < 0:
            raise ValueError("history_index must be a non-negative exact integer")
        if type(self.content) is not str or not self.content.strip():
            raise ValueError("content must be a non-empty exact string")

    def to_dict(self) -> PlannerPriorUserRequestDict:
        return PlannerPriorUserRequestDict(history_index=self.history_index, content=self.content)


@dataclass(frozen=True, slots=True)
class PlannerPriorAssistantMessage:
    """Earlier assistant prose supplies context, never user-request authority."""

    history_index: int
    content: str

    def __post_init__(self) -> None:
        if type(self.history_index) is not int or self.history_index < 0:
            raise ValueError("history_index must be a non-negative exact integer")
        if type(self.content) is not str or not self.content.strip():
            raise ValueError("content must be a non-empty exact string")

    def to_dict(self) -> PlannerPriorAssistantMessageDict:
        return PlannerPriorAssistantMessageDict(history_index=self.history_index, content=self.content)


class PlannerPriorAssistantMessageDict(TypedDict):
    """Provider projection of assistant prose, separate from human requests."""

    history_index: int
    content: str


@dataclass(frozen=True, slots=True)
class PlannerConversationContext:
    """Owned, bounded context for resolving a referential current request."""

    prior_user_requests: tuple[PlannerPriorUserRequest, ...]
    additional_prior_user_requests_omitted: int = 0
    prior_assistant_messages: tuple[PlannerPriorAssistantMessage, ...] = ()

    def __post_init__(self) -> None:
        if type(self.prior_user_requests) is not tuple or any(
            type(request) is not PlannerPriorUserRequest for request in self.prior_user_requests
        ):
            raise TypeError("prior_user_requests must be an exact PlannerPriorUserRequest tuple")
        if type(self.prior_assistant_messages) is not tuple or any(
            type(message) is not PlannerPriorAssistantMessage for message in self.prior_assistant_messages
        ):
            raise TypeError("prior_assistant_messages must be an exact PlannerPriorAssistantMessage tuple")
        if not self.prior_user_requests and not self.prior_assistant_messages:
            raise ValueError("planner conversation context must not be empty")
        history_indices = tuple(request.history_index for request in self.prior_user_requests)
        if history_indices != tuple(sorted(set(history_indices))):
            raise ValueError("prior_user_requests history_index values must be unique and increasing")
        assistant_indices = tuple(message.history_index for message in self.prior_assistant_messages)
        if assistant_indices != tuple(sorted(set(assistant_indices))):
            raise ValueError("prior_assistant_messages history_index values must be unique and increasing")
        if set(history_indices) & set(assistant_indices):
            raise ValueError("user and assistant history_index values must be distinct")
        if type(self.additional_prior_user_requests_omitted) is not int or self.additional_prior_user_requests_omitted < 0:
            raise ValueError("additional_prior_user_requests_omitted must be a non-negative exact integer")
        if self.additional_prior_user_requests_omitted > 0 and len(self.prior_user_requests) < 2:
            raise ValueError("omitted prior requests require both an anchor and a retained recent request")

    def to_dict(self) -> PlannerConversationContextDict:
        result = PlannerConversationContextDict(
            prior_user_requests=[request.to_dict() for request in self.prior_user_requests],
            additional_prior_user_requests_omitted=self.additional_prior_user_requests_omitted,
        )
        if self.prior_assistant_messages:
            result["prior_assistant_messages"] = [message.to_dict() for message in self.prior_assistant_messages]
        return result


class PipelinePlannerError(RuntimeError):
    """Leak-safe failure raised when the bounded planner cannot continue.

    ``detail_codes`` carries the closed, leak-safe validation error codes of
    the last candidate rejection when the failure is a repair/composition
    exhaustion — the discriminant a live 5xx investigation needs, recorded on
    the durable failure disposition so it never requires a temp diagnostic.
    Empty for non-rejection failures (timeout, provider error, ...).

    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        detail_codes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail_codes = detail_codes


class PlannerContinuationUnavailable(Exception):
    """Internal admission signal, never a provider outage or model verdict."""


@final
@dataclass(slots=True)
class PlannerContinuationBudget:
    """Continue this exact declined request's existing reservations and cost cap."""

    policy: PlannerBudgetPolicy
    session_id: str
    request_id: str | None
    request_hash: str
    provider_owner: ProviderInvocationOwner | None
    evidence_owner: TurnEvidence | None
    deadline: float
    reserved_calls: int
    validated_cost: Decimal
    cost_known: bool
    _charged_call_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _publication_binding: str | None = field(default=None, init=False, repr=False)

    def validate_for(
        self,
        *,
        session_id: str,
        request_id: str,
        request: str,
        provider_owner: ProviderInvocationOwner | None,
        evidence_owner: TurnEvidence,
    ) -> None:
        if (
            type(self.policy) is not PlannerBudgetPolicy
            or type(self.request_id) is not str
            or not self.request_id
            or self.session_id != session_id
            or self.request_id != request_id
            or self.request_hash != stable_hash(request)
            or self.provider_owner is not provider_owner
            or self.evidence_owner is not evidence_owner
            or type(evidence_owner) is not TurnEvidence
            or type(self.reserved_calls) is not int
            or not 0 <= self.reserved_calls <= self.policy.max_total_provider_calls
            or type(self.validated_cost) is not Decimal
            or not self.validated_cost.is_finite()
            or self.validated_cost < 0
            or type(self.cost_known) is not bool
            or type(self.deadline) is not float
            or not math.isfinite(self.deadline)
        ):
            raise AuditIntegrityError("Planner continuation belongs to another request or has invalid accounting")

    def has_cost_headroom(self) -> bool:
        return self.cost_known and self.validated_cost < self.policy.max_cumulative_provider_cost

    def within_cost_cap(self) -> bool:
        return self.cost_known and self.validated_cost <= self.policy.max_cumulative_provider_cost

    def bind_publication(self, review: AdvisorPublicationReview) -> None:
        if type(review) is not AdvisorPublicationReview or (
            self._publication_binding is not None and self._publication_binding != review.binding
        ):
            raise AuditIntegrityError("Planner continuation changed its exact candidate review")
        self._publication_binding = review.binding

    def require_publication(self, review: AdvisorPublicationReview | None) -> None:
        if type(review) is not AdvisorPublicationReview or self._publication_binding is None or review.binding != self._publication_binding:
            raise AuditIntegrityError("Planner continuation lacks its exact bound candidate review")

    def reserve_attempt(self, *, now: float) -> bool:
        if now >= self.deadline or self.reserved_calls >= self.policy.max_total_provider_calls or not self.has_cost_headroom():
            return False
        # Matches original planner semantics: reserved before quota, distinct
        # from the number of actual physical SDK dispatches.
        self.reserved_calls += 1
        return True

    def check_dispatch(self, *, now: float) -> None:
        if now >= self.deadline or self.reserved_calls > self.policy.max_total_provider_calls or not self.has_cost_headroom():
            raise PlannerContinuationUnavailable("Planner continuation has no existing admission headroom")

    def consume_call(self, call: ComposerLLMCall, *, retained: bool) -> None:
        if type(call) is not ComposerLLMCall or call.call_id is None or not retained:
            raise AuditIntegrityError("Planner continuation lacks its actual retained advisor audit")
        if call.call_id in self._charged_call_ids:
            raise AuditIntegrityError("Planner continuation attempted to charge the same advisor call twice")
        self._charged_call_ids.add(call.call_id)
        if call.provider_cost is None:
            self.cost_known = False
            return
        cost = Decimal(str(call.provider_cost))
        if not cost.is_finite() or cost < 0:
            self.cost_known = False
            return
        self.validated_cost += cost


@dataclass(frozen=True, slots=True)
class PlannerBudgetPolicy:
    """Request-wide hard bounds, except cost which is a continuation cap.

    ``max_request_bytes`` covers the exact canonical UTF-8 bytes of the full
    post-cache-marker ``{messages, tools}`` request payload.  Provider cost is
    necessarily known only after a call: the call is audited first, then a
    missing/malformed value or cumulative overage prevents all response
    parsing, dispatch, custody, and proposal construction.  The final call may
    therefore overshoot the configured amount; this is not a pre-spend cap.
    """

    max_total_provider_calls: int
    max_request_bytes: int
    max_completion_tokens: int
    max_cumulative_provider_cost: Decimal

    def __post_init__(self) -> None:
        for name, value in (
            ("max_total_provider_calls", self.max_total_provider_calls),
            ("max_request_bytes", self.max_request_bytes),
            ("max_completion_tokens", self.max_completion_tokens),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        if type(self.max_cumulative_provider_cost) is not Decimal:
            raise TypeError("max_cumulative_provider_cost must be Decimal")
        if not self.max_cumulative_provider_cost.is_finite() or self.max_cumulative_provider_cost < 0:
            raise ValueError("max_cumulative_provider_cost must be finite and non-negative")

    @property
    def audit_hash(self) -> str:
        return stable_hash(
            {
                "schema": "composer.planner-budget.v1",
                "max_total_provider_calls": self.max_total_provider_calls,
                "max_request_bytes": self.max_request_bytes,
                "max_completion_tokens": self.max_completion_tokens,
                "max_cumulative_provider_cost": str(self.max_cumulative_provider_cost),
            }
        )


PipelineCustodyResult = Literal["not_required", "ready"]


@dataclass(frozen=True, slots=True)
class PipelinePlanResult:
    """A staged proposal plus transport/custody facts kept outside the draft hash."""

    proposal: PipelineProposal
    tool_call_id: str
    custody_result: PipelineCustodyResult
    model_identifier: str
    model_version: str
    provider: str

    def __post_init__(self) -> None:
        if type(self.proposal) is not PipelineProposal:
            raise TypeError("proposal must be an exact PipelineProposal")
        if type(self.tool_call_id) is not str or not self.tool_call_id.strip():
            raise ValueError("tool_call_id must be a non-empty exact string")
        custody_result = cast(Any, self.custody_result)
        if type(custody_result) is not str or custody_result not in {"not_required", "ready"}:
            raise ValueError("custody_result must be 'not_required' or 'ready'")
        for name, value in (
            ("model_identifier", self.model_identifier),
            ("model_version", self.model_version),
            ("provider", self.provider),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a non-empty exact string")


__all__ = [
    "PipelineCustodyResult",
    "PipelinePlanResult",
    "PipelinePlannerError",
    "PlannerBudgetPolicy",
    "PlannerContinuationBudget",
    "PlannerContinuationUnavailable",
    "PlannerConversationContext",
    "PlannerPriorAssistantMessage",
    "PlannerPriorUserRequest",
]
