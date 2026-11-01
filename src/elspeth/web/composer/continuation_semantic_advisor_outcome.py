"""Original semantic Advisor physical observations, without action authority.

Only already admitted, credential-safe response fields may enter this DATA.
The observation does not prove durable effect retention, grant a retry, or
authorize installing a decision. Those require the current SQL/CAS owner.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdmittedLLMProviderMetadata
from elspeth.web.composer.advisor_output import ContinuationAdvisorDecision
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_advisor import ContinuationAdvisorInvocation
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.quota_authority import ProviderAttempt

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_semantic_advisor import PreparedSemanticAdvisorRequest, SemanticAdvisorResult
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer


@dataclass(frozen=True, slots=True)
class SemanticAdvisorInvocationObservation:
    source: ContinuationAdvisorInvocation
    request_sha256: str
    message: _AdmittedAssistantMessage | None
    provider_metadata: _AdmittedLLMProviderMetadata | None
    # Raw SDK tool objects are not admitted or retained by this observer.
    tool_calls_present: bool | None
    decision: ContinuationAdvisorDecision | None
    attempt: ProviderAttempt | None
    audit_call: ComposerLLMCall | None
    sdk_entered: bool
    recorder_completed: bool
    # The body failure precedes audit/settlement. When no body failed, the
    # observer preserves the actual outward error here as well as on Raised.
    original_failure: BaseException | None = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SemanticAdvisorInvocationReturned:
    result: SemanticAdvisorResult
    observation: SemanticAdvisorInvocationObservation


@dataclass(frozen=True, slots=True)
class SemanticAdvisorInvocationRaised:
    observation: SemanticAdvisorInvocationObservation
    error: BaseException


@dataclass(slots=True)
class _SemanticAdvisorInvocationCapture:
    source: ContinuationAdvisorInvocation
    request_sha256: str
    claimed: bool = False
    message: _AdmittedAssistantMessage | None = None
    provider_metadata: _AdmittedLLMProviderMetadata | None = None
    tool_calls_present: bool | None = None
    decision: ContinuationAdvisorDecision | None = None
    attempt: ProviderAttempt | None = None
    audit_call: ComposerLLMCall | None = None
    sdk_entered: bool = False
    recorder_completed: bool = False
    original_failure: BaseException | None = None

    def snapshot(self) -> SemanticAdvisorInvocationObservation:
        return SemanticAdvisorInvocationObservation(
            source=self.source,
            request_sha256=self.request_sha256,
            message=self.message,
            provider_metadata=self.provider_metadata,
            tool_calls_present=self.tool_calls_present,
            decision=self.decision,
            attempt=self.attempt,
            audit_call=self.audit_call,
            sdk_entered=self.sdk_entered,
            recorder_completed=self.recorder_completed,
            original_failure=self.original_failure,
        )


async def observe_semantic_advisor_invocation(
    request: PreparedSemanticAdvisorRequest,
    *,
    provider_owner: ProviderInvocationOwner,
    recorder: BufferingRecorder,
    timeout: float,
    on_provider_dispatch: Callable[[], None] | None = None,
    no_sdk_issuer: SemanticAdvisorNoSdkIssuer | None = None,
) -> SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised:
    """Observe one original throwing invocation, including paid invalid output.

    A raised outcome preserves the original body error separately from the
    actual outward error, which may include required audit/settlement or
    deferred cancellation failures. Catching it does not clear cancellation.
    The existing throwing invocation remains unchanged for its callers.
    """
    from elspeth.web.composer.continuation_semantic_advisor import PreparedSemanticAdvisorRequest, run_semantic_advisor

    if (
        type(request) is not PreparedSemanticAdvisorRequest
        or type(provider_owner) is not ProviderInvocationOwner
        or type(recorder) is not BufferingRecorder
    ):
        raise TypeError("Semantic Advisor observation requires its actual live owners")
    capture = _SemanticAdvisorInvocationCapture(request.source, request.request_sha256)
    try:
        result = await run_semantic_advisor(
            request,
            provider_owner=provider_owner,
            recorder=recorder,
            timeout=timeout,
            on_provider_dispatch=on_provider_dispatch,
            no_sdk_issuer=no_sdk_issuer,
            _outcome=capture,
        )
    except BaseException as error:
        if capture.original_failure is None:
            capture.original_failure = error
        raised = SemanticAdvisorInvocationRaised(capture.snapshot(), error)
        if no_sdk_issuer is not None:
            no_sdk_issuer.seal_original_outcome(capture, raised)
        return raised
    if (
        result.source is not capture.source
        or result.request_sha256 != capture.request_sha256
        or result.decision is not capture.decision
        or result.audit_call is not capture.audit_call
        or not capture.recorder_completed
    ):
        raise AuditIntegrityError("Semantic Advisor observation lost its original successful result")
    returned = SemanticAdvisorInvocationReturned(result, capture.snapshot())
    if no_sdk_issuer is not None:
        no_sdk_issuer.seal_original_outcome(capture, returned)
    return returned
