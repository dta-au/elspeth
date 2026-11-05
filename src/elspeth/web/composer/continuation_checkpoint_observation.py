"""Dormant provider-output observation for Review and EARLY checkpoints.

These are structured checkpoint calls, distinct from semantic assessments.
The values here grant no retry, verdict application, SQL settlement or tail
retention authority. A later owner must authenticate original phase and source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from weakref import WeakKeyDictionary

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
    _AdmittedToolBatch,
)
from elspeth.web.composer.advisor_checkpoint_telemetry import AdvisorCheckpointPhase
from elspeth.web.composer.advisor_invocation_outcome import AdvisorInvocationObservation
from elspeth.web.composer.advisor_output import AdvisorCheckpointResponse, AdvisorEndResponse, parse_advisor_checkpoint_response
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_text_response, measure_continuation_response_size
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_provider_outcomes import (
    _physical_audit_json_value,
    measure_physical_audit_call_size,
    require_successful_response_audit,
    require_v4_successful_response_audit,
)
from elspeth.web.composer.provider_audit_source import (
    ProviderAuditSource,
    audit_drafts_json,
    capture_provider_audit_source,
    provider_attempt_audit_drafts,
)
from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion_fields
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, encode_paid_overflow_cost_amount

_LIMIT = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
_MAX_LEDGER_TOKENS = 2**31 - 1


@dataclass(frozen=True, slots=True)
class CheckpointCompleteResponseObservation:
    phase: AdvisorCheckpointPhase
    attempt: ProviderAttempt
    message: _AdmittedAssistantMessage
    provider_metadata: _AdmittedLLMProviderMetadata
    decision: AdvisorCheckpointResponse
    audit_call: ComposerLLMCall
    response: PrivateChunk


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointReturnedResponseObservation:
    """Complete successful transport DATA, before checkpoint grammar admission.

    A fitting malformed JSON answer can consume the existing format retry.
    Its bytes must first be retained by the original paid-attempt owner. This
    value grants neither a retry nor a model verdict.
    """

    phase: AdvisorCheckpointPhase
    attempt: ProviderAttempt
    message: _AdmittedAssistantMessage
    provider_metadata: _AdmittedLLMProviderMetadata
    audit_call: ComposerLLMCall
    response: PrivateChunk
    tool_batch: _AdmittedToolBatch = field(default_factory=lambda: _AdmittedToolBatch((), frozenset()))


@dataclass(frozen=True, slots=True)
class CheckpointOverflowUsageObservation:
    status: Literal["observed", "unavailable"]
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class CheckpointIncompleteResponseObservation:
    phase: AdvisorCheckpointPhase
    source: ProviderAuditSource
    request_sha256: str
    attempt_id: str
    representation: Literal["physical_audit", "response"]
    document_codec: Literal["composer.physical-audit.v1", "composer.response.v1"]
    measurement: Literal["at_least"]
    observed_bytes: int
    limit_bytes: int
    evidence_status: Literal["incomplete"]
    retryable: Literal[False]
    cost: PaidOverflowCost
    usage: CheckpointOverflowUsageObservation


type CheckpointProviderResponseObservation = CheckpointCompleteResponseObservation | CheckpointIncompleteResponseObservation


@dataclass(frozen=True, slots=True)
class _OriginalV4CheckpointReturn:
    custody: ProviderCallCustody
    witness: V4ProviderResultCompleteness
    original: AdvisorInvocationObservation
    attempt_id: str
    request_sha256: str
    response_digest: str | None


_v4_returns: WeakKeyDictionary[
    CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation, _OriginalV4CheckpointReturn
] = WeakKeyDictionary()


def _cost(call: ComposerLLMCall) -> PaidOverflowCost:
    if call.provider_cost is None:
        return PaidOverflowCost(status="unknown", amount=None, source="not_available")
    return PaidOverflowCost(status="known", amount=encode_paid_overflow_cost_amount(call.provider_cost), source=call.provider_cost_source)


def _usage(call: ComposerLLMCall) -> CheckpointOverflowUsageObservation:
    model = call.model_returned if call.model_returned is not None else call.model_requested
    tokens = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
    if (
        not model
        or len(model) > 256
        or any(value is not None and (type(value) is not int or value < 0 or value > _MAX_LEDGER_TOKENS) for value in tokens)
    ):
        return CheckpointOverflowUsageObservation("unavailable", None, None, None, None, None)
    return CheckpointOverflowUsageObservation("observed", model, *tokens)


def observe_checkpoint_returned_response(
    observation: AdvisorInvocationObservation,
    *,
    phase: AdvisorCheckpointPhase,
    trigger: str,
    attempt: ProviderAttempt,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
) -> CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation:
    return _observe_checkpoint_returned_response(
        observation, phase=phase, trigger=trigger, attempt=attempt, context=context, env_ref_names=env_ref_names, v4_policy=None
    )


def _observe_checkpoint_returned_response(
    observation: AdvisorInvocationObservation,
    *,
    phase: AdvisorCheckpointPhase,
    trigger: str,
    attempt: ProviderAttempt,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
    v4_policy: ContinuationAdmissionPolicyV4 | None,
) -> CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation:
    """Measure a real returned text response before scheduling a format retry.

    Provider errors, absent content and rejected SDK frames are deliberately
    outside this successful-return contract. Their settlement policy belongs
    to a separate owner; they cannot become paid-overflow by this function.
    """
    expected_trigger = ADVISOR_TRIGGER_DETERMINISTIC_EARLY if phase == "early" else ADVISOR_TRIGGER_DETERMINISTIC_END
    if (
        type(observation) is not AdvisorInvocationObservation
        or phase not in ("early", "end")
        or trigger != expected_trigger
        or type(attempt) is not ProviderAttempt
        or observation.attempt is not attempt
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or type(env_ref_names) is not frozenset
        or type(observation.message) is not _AdmittedAssistantMessage
        or type(observation.provider_metadata) is not _AdmittedLLMProviderMetadata
        or type(observation.audit_call) is not ComposerLLMCall
    ):
        raise AuditIntegrityError("checkpoint response requires its original typed phase and paid DATA")
    message = observation.message
    metadata = observation.provider_metadata
    call = observation.audit_call
    if (
        type(message.content) is not str
        or not message.content.strip()
        or observation.guidance != message.content
        or observation.metadata is None
        or set(observation.metadata) != {"model", "prompt_tokens", "completion_tokens", "cached_prompt_tokens", "latency_ms"}
        or observation.metadata["model"] != (metadata.model_returned or call.model_requested)
        or observation.metadata["prompt_tokens"] != metadata.usage.prompt_tokens
        or observation.metadata["completion_tokens"] != metadata.usage.completion_tokens
        or observation.metadata["cached_prompt_tokens"] != metadata.usage.cached_prompt_tokens
        or type(observation.metadata["latency_ms"]) is not int
        or observation.metadata["latency_ms"] < 0
    ):
        raise AuditIntegrityError("checkpoint response changed its original guidance or accounting projection")
    _require_no_credential_material_in_completion_fields(
        content=message.content, tool_calls=(), provider_metadata=metadata, surface="composer_advisor_response"
    )
    # The quota attempt begins on the database clock; this call has its own
    # worker timestamp. Bind their shared attempt ID without equating clocks.
    if v4_policy is None:
        require_successful_response_audit(
            call, metadata, attempt_id=attempt.attempt_id, started_at=call.started_at, finished_at=call.finished_at
        )
    else:
        require_v4_successful_response_audit(
            call, metadata, policy=v4_policy, attempt_id=attempt.attempt_id, started_at=call.started_at, finished_at=call.finished_at
        )
    completion = _AdmittedLLMCompletion(message, _AdmittedToolBatch((), frozenset()), metadata)
    measure_continuation_response_size(completion)
    source = capture_provider_audit_source(context)
    require_no_credential_material(
        _physical_audit_json_value(call),
        surface="composer_checkpoint_overflow_physical_audit",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    drafts_json = audit_drafts_json(provider_attempt_audit_drafts(call, context))
    drafts, drafts_error = parse_json_strict(drafts_json)
    if drafts_error is not None:
        raise AuditIntegrityError("checkpoint response lost its original audit draft JSON")
    require_no_credential_material(
        drafts,
        surface="composer_checkpoint_overflow_audit_drafts",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )

    def excess(representation: Literal["physical_audit", "response"]) -> CheckpointIncompleteResponseObservation:
        return CheckpointIncompleteResponseObservation(
            phase=phase,
            source=source,
            request_sha256=call.messages_hash,
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
    response = encode_continuation_text_response(content=message.content, provider_metadata=metadata, audit_call=call)
    return CheckpointReturnedResponseObservation(phase, attempt, message, metadata, call, response)


def observe_checkpoint_provider_response(
    observation: AdvisorInvocationObservation,
    *,
    phase: AdvisorCheckpointPhase,
    trigger: str,
    attempt: ProviderAttempt,
    decision: AdvisorCheckpointResponse,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
) -> CheckpointProviderResponseObservation:
    """Classify an already admitted decision without changing its grammar gate."""
    if (
        type(observation) is not AdvisorInvocationObservation
        or type(observation.message) is not _AdmittedAssistantMessage
        or type(observation.message.content) is not str
        or phase not in ("early", "end")
        or (phase == "early" and type(decision) is not AdvisorCheckpointResponse)
        or (phase == "end" and type(decision) is not AdvisorEndResponse)
    ):
        raise AuditIntegrityError("checkpoint response requires its original typed phase and paid DATA")
    admission = parse_advisor_checkpoint_response(observation.message.content, phase=phase)
    if admission.response is None or type(admission.response) is not type(decision) or admission.response != decision:
        raise AuditIntegrityError("checkpoint response changed its closed original decision")
    returned = observe_checkpoint_returned_response(
        observation,
        phase=phase,
        trigger=trigger,
        attempt=attempt,
        context=context,
        env_ref_names=env_ref_names,
    )
    if isinstance(returned, CheckpointIncompleteResponseObservation):
        return returned
    return CheckpointCompleteResponseObservation(
        returned.phase,
        returned.attempt,
        returned.message,
        returned.provider_metadata,
        decision,
        returned.audit_call,
        returned.response,
    )


def observe_v4_checkpoint_returned_response(
    observation: AdvisorInvocationObservation,
    *,
    custody: ProviderCallCustody,
    policy: ContinuationAdmissionPolicyV4,
    phase: AdvisorCheckpointPhase,
    trigger: str,
    attempt: ProviderAttempt,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
) -> CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation:
    """Require original raw-field completeness before any V4 success decision."""
    if (
        type(custody) is not ProviderCallCustody
        or custody.continuation_checkpoint_v4_admission is None
        or custody.attempt is not attempt
        or type(observation) is not AdvisorInvocationObservation
        or type(observation.audit_call) is not ComposerLLMCall
        or custody.call is not observation.audit_call
        or type(observation.provider_metadata) is not _AdmittedLLMProviderMetadata
        or type(observation.message) is not _AdmittedAssistantMessage
    ):
        raise AuditIntegrityError("V4 checkpoint response lost its actual physical custody")
    witness = observation.provider_metadata.v4_completeness
    if type(witness) is not V4ProviderResultCompleteness:
        raise AuditIntegrityError("V4 checkpoint response lacks original raw-result completeness")
    custody.require_v4_result_completeness(witness)
    witness.require_result(observation.provider_metadata, content=observation.message.content, tool_calls=())
    result = _observe_checkpoint_returned_response(
        observation, phase=phase, trigger=trigger, attempt=attempt, context=context, env_ref_names=env_ref_names, v4_policy=policy
    )
    if observation.audit_call.messages_hash != custody.continuation_checkpoint_v4_admission.hold.source_input_sha256:
        raise AuditIntegrityError("V4 checkpoint result changed its original admitted request")
    _v4_returns[result] = _OriginalV4CheckpointReturn(
        custody,
        witness,
        observation,
        attempt.attempt_id,
        observation.audit_call.messages_hash,
        result.response.reference.digest if type(result) is CheckpointReturnedResponseObservation else None,
    )
    return result


def require_original_v4_checkpoint_returned_response(
    result: CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation,
    *,
    attempt_id: str,
    request_sha256: str,
) -> None:
    """Require the original raw SDK-result producer before SQL can mark completeness."""
    source = (
        _v4_returns.get(result)
        if type(result) in (CheckpointReturnedResponseObservation, CheckpointIncompleteResponseObservation)
        else None
    )
    if source is None or source.attempt_id != attempt_id or source.request_sha256 != request_sha256:
        raise AuditIntegrityError("V4 checkpoint result lacks its original physical return producer")
    source.custody.require_v4_result_completeness(source.witness)
    if type(result) is CheckpointReturnedResponseObservation:
        if (
            result.attempt is not source.custody.attempt
            or result.audit_call is not source.original.audit_call
            or result.provider_metadata is not source.original.provider_metadata
            or result.message is not source.original.message
            or result.response.reference.digest != source.response_digest
        ):
            raise AuditIntegrityError("V4 checkpoint result substituted original complete DATA")
        source.witness.require_result(result.provider_metadata, content=result.message.content, tool_calls=result.tool_batch.calls)
        if source.original.tool_batch is not None and result.tool_batch is not source.original.tool_batch:
            raise AuditIntegrityError("V4 checkpoint result replaced its original complete tool batch")
    elif type(result) is CheckpointIncompleteResponseObservation:
        if result.attempt_id != attempt_id or result.request_sha256 != request_sha256:
            raise AuditIntegrityError("V4 checkpoint overflow substituted its original physical source")
    else:
        raise AuditIntegrityError("V4 checkpoint result lost its nominal returned type")


def original_v4_checkpoint_returned_audit_call(
    result: CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation,
    *,
    attempt_id: str,
    request_sha256: str,
) -> ComposerLLMCall:
    """Recover the issued audit call for SQL retention, never reconstruct a receipt.

    Overflow observations deliberately contain no private audit payload. Their
    original call remains available only through the live SDK-return issuer.
    """
    require_original_v4_checkpoint_returned_response(result, attempt_id=attempt_id, request_sha256=request_sha256)
    source = _v4_returns[result]
    call = source.original.audit_call
    message = source.original.message
    metadata = source.original.provider_metadata
    if (
        type(call) is not ComposerLLMCall
        or type(message) is not _AdmittedAssistantMessage
        or type(metadata) is not _AdmittedLLMProviderMetadata
        or source.custody.call is not call
        or call.call_id != attempt_id
        or call.messages_hash != request_sha256
    ):
        raise AuditIntegrityError("V4 checkpoint audit call lost its original physical return custody")
    source.witness.require_result(
        metadata,
        content=message.content,
        tool_calls=source.original.tool_batch.calls if source.original.tool_batch is not None else (),
    )
    return call
