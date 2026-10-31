"""Original live Advisor observations; no durable review or dispatch authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMProviderMetadata,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.quota_authority import ProviderAttempt

if TYPE_CHECKING:
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.pipeline_planner import PlannerContinuationBudget


@dataclass(frozen=True, slots=True)
class AdvisorInvocationObservation:
    # These original owned projections are exposed only after the existing
    # response admission. Ignored raw SDK fields are never newly retained.
    message: _AdmittedAssistantMessage | None
    provider_metadata: _AdmittedLLMProviderMetadata | None
    guidance: str | None
    metadata: Mapping[str, Any] | None
    attempt: ProviderAttempt | None
    audit_call: ComposerLLMCall | None
    recorder_completed: bool
    continuation_budget_completed: bool

    def __post_init__(self) -> None:
        freeze_fields(self, "metadata")


@dataclass(frozen=True, slots=True)
class AdvisorInvocationReturned:
    observation: AdvisorInvocationObservation

    @property
    def result(self) -> tuple[str, Mapping[str, Any]]:
        guidance, metadata = self.observation.guidance, self.observation.metadata
        if guidance is None or metadata is None:
            raise AuditIntegrityError("Returned Advisor observation lacks its original successful values")
        return guidance, metadata


@dataclass(frozen=True, slots=True)
class AdvisorInvocationRaised:
    observation: AdvisorInvocationObservation
    error: BaseException


@dataclass(slots=True)
class _AdvisorInvocationCapture:
    claimed: bool = False
    message: _AdmittedAssistantMessage | None = None
    provider_metadata: _AdmittedLLMProviderMetadata | None = None
    guidance: str | None = None
    metadata: dict[str, Any] | None = None
    attempt: ProviderAttempt | None = None
    audit_call: ComposerLLMCall | None = None
    recorder_completed: bool = False
    continuation_budget_completed: bool = False

    def snapshot(self) -> AdvisorInvocationObservation:
        return AdvisorInvocationObservation(
            self.message,
            self.provider_metadata,
            self.guidance,
            self.metadata,
            self.attempt,
            self.audit_call,
            self.recorder_completed,
            self.continuation_budget_completed,
        )


async def observe_advisor_invocation(
    owner: AdvisorCheckpointOwner,
    arguments: Mapping[str, Any],
    *,
    recorder: BufferingRecorder | None,
    provider_owner: ProviderInvocationOwner,
    timeout: float | None = None,
    structured_output: bool = False,
    on_provider_dispatch: Callable[[], None] | None = None,
    continuation_budget: PlannerContinuationBudget | None = None,
) -> AdvisorInvocationReturned | AdvisorInvocationRaised:
    """Observe one original wrapper, preserving original failures as live data.

    This is not a review acceptance, restart decoder, retry or current grant.
    Existing callers continue using the original tuple-returning/raising owner.
    """
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner

    if type(owner) is not AdvisorCheckpointOwner:
        raise AuditIntegrityError("Advisor observation requires the actual current Advisor owner")
    if type(provider_owner) is not ProviderInvocationOwner:
        raise AuditIntegrityError("Advisor observation requires its actual explicit provider owner")
    capture = _AdvisorInvocationCapture()
    try:
        result = await owner._call_advisor_with_audit(
            arguments,
            recorder=recorder,
            timeout=timeout,
            structured_output=structured_output,
            on_provider_dispatch=on_provider_dispatch,
            provider_owner=provider_owner,
            continuation_budget=continuation_budget,
            _outcome=capture,
        )
    except BaseException as error:
        return AdvisorInvocationRaised(capture.snapshot(), error)
    if capture.guidance is None or capture.metadata is None or result[0] is not capture.guidance or result[1] is not capture.metadata:
        raise AuditIntegrityError("Advisor observation lost its original successful result")
    return AdvisorInvocationReturned(capture.snapshot())
