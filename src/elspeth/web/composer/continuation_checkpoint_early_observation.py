"""Original complete EARLY returns, including unsupported complete reply shapes.

The SDK witness establishes a complete returned result. A malformed checkpoint
shape keeps its actual MALFORMED_RESPONSE audit; no success is manufactured.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal
from weakref import ref

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolBatch
from elspeth.web.composer.advisor_invocation_outcome import AdvisorInvocationObservation
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointReturnedResponseObservation,
    _cost,
    _OriginalV4CheckpointReturn,
    _usage,
    _v4_returns,
)
from elspeth.web.composer.continuation_codec import encode_continuation_response, measure_continuation_response_size, owned_json
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_provider_outcomes import (
    _physical_audit_json_value,
    measure_physical_audit_call_size,
    require_v4_successful_response_audit,
)
from elspeth.web.composer.provider_audit_source import audit_drafts_json, capture_provider_audit_source, provider_attempt_audit_drafts
from elspeth.web.composer.provider_gateway import _require_no_credential_material_in_completion_fields
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, project_v4_provider_diagnostic_metadata
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.credential_guard import require_no_credential_material

if TYPE_CHECKING:
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict


def early_returned_checkpoint_verdict(completion: _AdmittedLLMCompletion) -> AdvisorCheckpointVerdict:
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointVerdict, _parse_advisor_checkpoint_guidance
    from elspeth.web.composer.advisor_policy import ADVISOR_MALFORMED_USER_DETAIL

    content = completion.message.content
    if completion.tool_batch.calls or content is None or not content.strip():
        return AdvisorCheckpointVerdict(
            ok=False,
            blocking=False,
            failure_class="malformed",
            findings_text=ADVISOR_MALFORMED_USER_DETAIL,
            response_schema_valid=False if type(content) is str else None,
        )
    return _parse_advisor_checkpoint_guidance(content, phase="early")


def early_format_reprompt_required(completion: _AdmittedLLMCompletion) -> bool:
    return type(completion.message.content) is str and not early_returned_checkpoint_verdict(completion).ok


def require_v4_checkpoint_early_returned_audit(
    call: ComposerLLMCall,
    completion: _AdmittedLLMCompletion,
    *,
    policy: ContinuationAdmissionPolicyV4,
    attempt_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    if type(completion) is not _AdmittedLLMCompletion or type(call) is not ComposerLLMCall:
        raise AuditIntegrityError("EARLY returned audit requires its actual complete owned response")
    if call.status is ComposerLLMCallStatus.SUCCESS:
        if completion.tool_batch.calls or completion.message.content is None or not completion.message.content.strip():
            raise AuditIntegrityError("EARLY successful audit cannot describe an unsupported complete response shape")
        require_v4_successful_response_audit(
            call,
            completion.provider_metadata,
            policy=policy,
            attempt_id=attempt_id,
            started_at=started_at,
            finished_at=finished_at,
        )
        return
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or call.error_class != "_MalformedLLMResponseError"
        or call.error_message != "malformed_response"
        or not (completion.tool_batch.calls or completion.message.content is None or not completion.message.content.strip())
    ):
        raise AuditIntegrityError("EARLY complete malformed audit lost its actual unsupported response shape")
    metadata = project_v4_provider_diagnostic_metadata(completion.provider_metadata)
    usage = metadata.usage
    if (
        call.call_id != attempt_id
        or call.started_at != started_at
        or call.finished_at != finished_at
        or (call.model_returned, call.provider_request_id, call.finish_reason, call.provider_served)
        != (metadata.model_returned, metadata.provider_request_id, metadata.finish_reason, metadata.provider_served)
        or (
            call.prompt_tokens,
            call.completion_tokens,
            call.total_tokens,
            call.cached_prompt_tokens,
            call.cache_creation_input_tokens,
            call.cache_read_input_tokens,
            call.reasoning_tokens,
        )
        != (
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cached_prompt_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
            usage.reasoning_tokens,
        )
        or (call.provider_cost, call.provider_cost_source) != (metadata.provider_cost, metadata.provider_cost_source)
        or canonical_json(owned_json((call.reasoning_content, call.reasoning_details, call.thinking_blocks)))
        != canonical_json(owned_json((metadata.reasoning_content, metadata.reasoning_details, metadata.thinking_blocks)))
    ):
        raise AuditIntegrityError("EARLY malformed response changed its actual complete physical accounting")


def observe_v4_early_checkpoint_returned_response(
    observation: AdvisorInvocationObservation,
    *,
    custody: ProviderCallCustody,
    policy: ContinuationAdmissionPolicyV4,
    attempt: ProviderAttempt,
    context: SessionOperationContext,
    env_ref_names: frozenset[str],
) -> CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation:
    admission = custody.continuation_checkpoint_v4_admission
    if (
        type(observation) is not AdvisorInvocationObservation
        or type(custody) is not ProviderCallCustody
        or type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or custody.context is not context
        or type(env_ref_names) is not frozenset
        or admission is None
        or custody.attempt is not attempt
        or observation.attempt is not attempt
        or custody.call is not observation.audit_call
        or observation.audit_call is None
        or observation.message is None
        or observation.provider_metadata is None
        or type(observation.tool_batch) is not _AdmittedToolBatch
        or observation.audit_call.messages_hash != admission.hold.source_input_sha256
        or admission.original_source.phase != "early"
        or admission.original_source.early_input_ref is None
    ):
        raise AuditIntegrityError("EARLY returned response lost its original admitted physical capture")
    metadata = observation.provider_metadata
    witness = metadata.v4_completeness
    if type(witness) is not V4ProviderResultCompleteness:
        raise AuditIntegrityError("EARLY returned response lost its original complete SDK witness")
    custody.require_v4_result_completeness(witness)
    witness.require_result(metadata, content=observation.message.content, tool_calls=observation.tool_batch.calls)
    completion = _AdmittedLLMCompletion(observation.message, observation.tool_batch, metadata)
    call = observation.audit_call
    _require_no_credential_material_in_completion_fields(
        content=completion.message.content,
        tool_calls=tuple((item.function.name, item.function.arguments) for item in completion.tool_batch.calls),
        provider_metadata=metadata,
        surface="composer_advisor_response",
    )
    require_no_credential_material(
        _physical_audit_json_value(call),
        surface="composer_checkpoint_overflow_physical_audit",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    drafts, error = parse_json_strict(audit_drafts_json(provider_attempt_audit_drafts(call, context)))
    if error is not None:
        raise AuditIntegrityError("EARLY response lost its original private audit drafts")
    require_no_credential_material(
        drafts,
        surface="composer_checkpoint_overflow_audit_drafts",
        env_ref_names=env_ref_names,
        max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
    )
    require_v4_checkpoint_early_returned_audit(
        call,
        completion,
        policy=policy,
        attempt_id=attempt.attempt_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )
    # Both original complete response and physical audit keep the unchanged
    # private-document bound. Excess is a distinct returned-overflow outcome.
    physical_excess = measure_physical_audit_call_size(call).exceeded
    response_excess = measure_continuation_response_size(completion, audit_call=call).exceeded
    result: CheckpointReturnedResponseObservation | CheckpointIncompleteResponseObservation
    if physical_excess or response_excess:
        representation: Literal["physical_audit", "response"] = "physical_audit" if physical_excess else "response"
        result = CheckpointIncompleteResponseObservation(
            phase="early",
            source=capture_provider_audit_source(context),
            request_sha256=call.messages_hash,
            attempt_id=attempt.attempt_id,
            representation=representation,
            document_codec="composer.physical-audit.v1" if physical_excess else "composer.response.v1",
            measurement="at_least",
            observed_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1,
            limit_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
            evidence_status="incomplete",
            retryable=False,
            cost=_cost(call),
            usage=_usage(call),
        )
    else:
        response = encode_continuation_response(completion, audit_call=call)
        result = CheckpointReturnedResponseObservation(
            "early", attempt, observation.message, metadata, call, response, observation.tool_batch
        )
    source = _OriginalV4CheckpointReturn(
        custody,
        witness,
        observation,
        attempt.attempt_id,
        call.messages_hash,
        result.response.reference.digest if type(result) is CheckpointReturnedResponseObservation else None,
    )
    object.__setattr__(result, "_issued_record", source)
    _v4_returns[result] = ref(source)
    return result
