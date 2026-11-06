"""Original provider-response observation for semantic Advisor assessments.

This module classifies complete valid or invalid returned semantic responses.
Its values grant no decision application, SQL settlement, retry or retention
authority. Review and EARLY checkpoint calls are separate provider producers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
    _AdmittedToolBatch,
    _AdmittedToolCall,
)
from elspeth.web.composer.advisor_output import ContinuationAdvisorDecision, parse_continuation_advisor_decision
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_advisor import ContinuationAdvisorInvocation
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_response, measure_continuation_response_size
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_provider_outcomes import (
    _physical_audit_json_value,
    measure_physical_audit_call_size,
    require_malformed_response_audit,
    require_successful_response_audit,
)
from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationObservation
from elspeth.web.composer.provider_audit_source import (
    ProviderAuditSource,
    audit_drafts_json,
    capture_provider_audit_source,
    provider_attempt_audit_drafts,
)
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError, _require_no_credential_material_in_completion
from elspeth.web.composer.provider_result_admission import diagnostic_provider_metadata
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, encode_paid_overflow_cost_amount

_LIMIT = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
_MAX_LEDGER_TOKENS = 2**31 - 1


@dataclass(frozen=True, slots=True)
class SemanticCompleteResponseObservation:
    source: ContinuationAdvisorInvocation
    request_sha256: str
    attempt: ProviderAttempt
    message: _AdmittedAssistantMessage
    provider_metadata: _AdmittedLLMProviderMetadata
    decision: ContinuationAdvisorDecision
    audit_call: ComposerLLMCall
    response: PrivateChunk


@dataclass(frozen=True, slots=True)
class SemanticInvalidResponseObservation:
    """Complete actual SDK DATA whose closed semantic decision was refused."""

    source: ContinuationAdvisorInvocation
    request_sha256: str
    attempt: ProviderAttempt
    message: _AdmittedAssistantMessage
    provider_metadata: _AdmittedLLMProviderMetadata
    audit_call: ComposerLLMCall
    response: PrivateChunk
    original_failure: _MalformedLLMResponseError


@dataclass(frozen=True, slots=True)
class SemanticOverflowUsageObservation:
    status: Literal["observed", "unavailable"]
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True, slots=True)
class SemanticIncompleteResponseObservation:
    source: ProviderAuditSource
    source_sha256: str
    request_sha256: str
    attempt_id: str
    representation: Literal["physical_audit", "response", "recorder", "evidence_pair", "retained_history"]
    document_codec: Literal[
        "composer.physical-audit.v1",
        "composer.response.v1",
        "composer.audit-buffer.v2",
        "composer.evidence.v2",
        "composer.continuation.history.v4",
    ]
    measurement: Literal["at_least"]
    observed_bytes: int
    limit_bytes: int
    evidence_status: Literal["incomplete"]
    retryable: Literal[False]
    cost: PaidOverflowCost
    usage: SemanticOverflowUsageObservation


type SemanticProviderResponseObservation = (
    SemanticCompleteResponseObservation | SemanticInvalidResponseObservation | SemanticIncompleteResponseObservation
)


def observe_semantic_retained_tail_overflow(
    complete: SemanticCompleteResponseObservation | SemanticInvalidResponseObservation,
    *,
    context: SessionOperationContext,
    representation: Literal["recorder", "evidence_pair", "retained_history"],
    limit_bytes: int,
) -> SemanticIncompleteResponseObservation:
    """Classify only a measured mandatory tail of an already genuine response."""
    if (
        type(complete) not in (SemanticCompleteResponseObservation, SemanticInvalidResponseObservation)
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or complete.attempt.attempt_id != complete.audit_call.call_id
        or type(limit_bytes) is not int
        or limit_bytes <= 0
    ):
        raise AuditIntegrityError("semantic tail overflow requires its original complete paid response")
    codec: Literal["composer.audit-buffer.v2", "composer.evidence.v2", "composer.continuation.history.v4"]
    if representation == "retained_history":
        codec = "composer.continuation.history.v4"
    elif representation == "evidence_pair":
        codec = "composer.evidence.v2"
    elif representation == "recorder":
        codec = "composer.audit-buffer.v2"
    else:
        raise AuditIntegrityError("semantic tail overflow changed its mandatory representation")
    return SemanticIncompleteResponseObservation(
        source=capture_provider_audit_source(context),
        source_sha256=stable_hash(complete.source.model_dump(mode="json")),
        request_sha256=complete.request_sha256,
        attempt_id=complete.attempt.attempt_id,
        representation=representation,
        document_codec=codec,
        measurement="at_least",
        observed_bytes=limit_bytes + 1,
        limit_bytes=limit_bytes,
        evidence_status="incomplete",
        retryable=False,
        cost=_cost(complete.audit_call),
        usage=_usage(complete.audit_call),
    )


def _cost(call: ComposerLLMCall) -> PaidOverflowCost:
    if call.provider_cost is None:
        return PaidOverflowCost(status="unknown", amount=None, source="not_available")
    return PaidOverflowCost(status="known", amount=encode_paid_overflow_cost_amount(call.provider_cost), source=call.provider_cost_source)


def _usage(call: ComposerLLMCall) -> SemanticOverflowUsageObservation:
    model = call.model_returned if call.model_returned is not None else call.model_requested
    tokens = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
    if (
        not model
        or len(model) > 256
        or any(value is not None and (type(value) is not int or value < 0 or value > _MAX_LEDGER_TOKENS) for value in tokens)
    ):
        return SemanticOverflowUsageObservation("unavailable", None, None, None, None, None)
    return SemanticOverflowUsageObservation("observed", model, *tokens)


def observe_semantic_provider_response(
    observation: SemanticAdvisorInvocationObservation,
    *,
    source: ContinuationAdvisorInvocation,
    request_sha256: str,
    attempt: ProviderAttempt,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
    require_v4_completeness: bool = False,
) -> SemanticProviderResponseObservation:
    """Measure actual semantic provider representations after all validity gates.

    An excess probe saturates at limit + 1, giving a lower bound only. The
    resulting DATA must never be installed as an executable Advisor decision.
    """
    if (
        type(observation) is not SemanticAdvisorInvocationObservation
        or type(source) is not ContinuationAdvisorInvocation
        or source.kind != "semantic"
        or observation.source is not source
        or type(request_sha256) is not str
        or not is_lower_sha256_hex(request_sha256)
        or observation.request_sha256 != request_sha256
        or type(attempt) is not ProviderAttempt
        or observation.attempt is not attempt
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or type(env_ref_names) is not frozenset
        or type(require_v4_completeness) is not bool
        or observation.sdk_entered is not True
        or type(observation.message) is not _AdmittedAssistantMessage
        or type(observation.provider_metadata) is not _AdmittedLLMProviderMetadata
        or type(observation.audit_call) is not ComposerLLMCall
    ):
        raise AuditIntegrityError("semantic response observation requires original valid paid DATA")
    message = observation.message
    metadata = observation.provider_metadata
    decision = observation.decision
    call = observation.audit_call
    calls = observation.admitted_tool_calls
    if (
        type(calls) is not tuple
        or any(type(tool_call) is not _AdmittedToolCall for tool_call in calls)
        or observation.tool_calls_present is not bool(calls)
    ):
        raise AuditIntegrityError("semantic response changed its complete admitted tool call observation")
    original_failure = observation.original_failure
    invalid = type(original_failure) is _MalformedLLMResponseError
    if type(original_failure) is _MalformedLLMResponseError:
        if decision is not None or original_failure.provider_metadata is not metadata:
            raise AuditIntegrityError("invalid semantic response lost its original parse failure and metadata")
    elif original_failure is not None or type(decision) is not ContinuationAdvisorDecision or calls:
        raise AuditIntegrityError("semantic response lacks its original successful decision")
    completeness = metadata.v4_completeness
    if require_v4_completeness and completeness is None:
        raise AuditIntegrityError("V4 semantic paid observation lacks its original complete SDK return witness")
    if completeness is not None:
        completeness.require_result(metadata, content=message.content, tool_calls=calls)
    require_no_credential_material(
        source.model_dump(mode="json"),
        surface="composer_semantic_overflow_source",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    completion = _AdmittedLLMCompletion(message, _AdmittedToolBatch(calls, frozenset(call.id for call in calls)), metadata)
    _require_no_credential_material_in_completion(completion, surface="composer_advisor_response")
    if require_v4_completeness:
        audit_metadata = diagnostic_provider_metadata(metadata)
    else:
        audit_metadata = metadata
    parsed = (
        parse_continuation_advisor_decision(message.content, actual_advisor_call_id=attempt.attempt_id)
        if type(message.content) is str and message.content.strip() and not calls
        else None
    )
    if invalid:
        if parsed is not None:
            raise AuditIntegrityError("invalid semantic response contains a valid original decision")
    elif (
        type(decision) is not ContinuationAdvisorDecision
        or parsed is None
        or parsed != decision
        or decision.decision_id != attempt.attempt_id
    ):
        raise AuditIntegrityError("semantic response changed its actual closed decision or attempt")
    # The quota attempt starts on the database clock at admission; the physical
    # call starts later on the worker clock. Their timestamps are distinct.
    require_audit = require_malformed_response_audit if invalid else require_successful_response_audit
    require_audit(call, audit_metadata, attempt_id=attempt.attempt_id, started_at=call.started_at, finished_at=call.finished_at)
    measure_continuation_response_size(completion)
    source_data = capture_provider_audit_source(context)
    require_no_credential_material(
        _physical_audit_json_value(call),
        surface="composer_semantic_overflow_physical_audit",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    drafts_json = audit_drafts_json(provider_attempt_audit_drafts(call, context))
    drafts, drafts_error = parse_json_strict(drafts_json)
    if drafts_error is not None:
        raise AuditIntegrityError("semantic response lost its original audit draft JSON")
    require_no_credential_material(
        drafts,
        surface="composer_semantic_overflow_audit_drafts",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )

    def excess(representation: Literal["physical_audit", "response"]) -> SemanticIncompleteResponseObservation:
        return SemanticIncompleteResponseObservation(
            source=source_data,
            source_sha256=stable_hash(source.model_dump(mode="json")),
            request_sha256=request_sha256,
            attempt_id=attempt.attempt_id,
            representation=representation,
            document_codec="composer.physical-audit.v1" if representation == "physical_audit" else "composer.response.v1",
            measurement="at_least",
            observed_bytes=_LIMIT + 1,
            limit_bytes=_LIMIT,
            evidence_status="incomplete",
            retryable=False,
            cost=_cost(call),
            usage=_usage(call),
        )

    if measure_physical_audit_call_size(call).exceeded:
        return excess("physical_audit")
    if measure_continuation_response_size(completion, audit_call=call).exceeded:
        return excess("response")
    response = encode_continuation_response(completion, audit_call=call)
    if type(original_failure) is _MalformedLLMResponseError:
        return SemanticInvalidResponseObservation(source, request_sha256, attempt, message, metadata, call, response, original_failure)
    if type(decision) is not ContinuationAdvisorDecision:
        raise AuditIntegrityError("semantic response lost its successful decision")
    return SemanticCompleteResponseObservation(source, request_sha256, attempt, message, metadata, decision, call, response)
