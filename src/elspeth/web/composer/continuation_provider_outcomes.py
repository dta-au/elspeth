from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter, ValidationError

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, bounded_canonical_json_size
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES

_PHYSICAL_AUDIT = TypeAdapter(ComposerLLMCall)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4


def _physical_audit_json_value(call: ComposerLLMCall) -> JsonValue:
    if type(call) is not ComposerLLMCall:
        raise TypeError("physical audit retention requires its complete actual nominal record")
    projected = _PHYSICAL_AUDIT.dump_python(call, mode="json", warnings="error", fallback=owned_json)
    if projected["cost_response_accountable"] is None:
        del projected["cost_response_accountable"]
    return owned_json(projected)


def measure_physical_audit_call_size(call: ComposerLLMCall) -> BoundedCanonicalSize:
    """Measure the exact normal audit projection; excess is only a size observation.

    A caller must establish semantic and credential validity before treating an
    excess as a paid-overflow outcome. This helper grants no settlement authority.
    """
    return bounded_canonical_json_size(_physical_audit_json_value(call), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)


def encode_physical_audit_call(call: ComposerLLMCall) -> str:
    return canonical_json(_physical_audit_json_value(call))


def decode_physical_audit_call(canonical: str) -> ComposerLLMCall:
    if type(canonical) is not str:
        raise AuditIntegrityError("physical audit lost its complete canonical JSON")
    value, error = parse_json_strict(canonical)
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("physical audit is not one strict JSON object")
    try:
        call = _PHYSICAL_AUDIT.validate_json(canonical, strict=True, extra="forbid")
    except (ValidationError, ValueError, TypeError) as exc:
        raise AuditIntegrityError("physical audit violates its owned complete contract") from exc
    if type(call) is not ComposerLLMCall or encode_physical_audit_call(call) != canonical:
        raise AuditIntegrityError("physical audit omitted, changed or normalized retained fields")
    return call


def require_successful_response_audit(
    call: ComposerLLMCall,
    metadata: _AdmittedLLMProviderMetadata,
    *,
    attempt_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    if type(call) is not ComposerLLMCall or call.status is not ComposerLLMCallStatus.SUCCESS:
        raise AuditIntegrityError("received response lacks its original successful audit")
    _require_response_audit_fields(call, metadata, attempt_id=attempt_id, started_at=started_at, finished_at=finished_at)


def require_malformed_response_audit(
    call: ComposerLLMCall,
    metadata: _AdmittedLLMProviderMetadata,
    *,
    attempt_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Match a captured invalid decision to its actual unsuccessful paid audit."""
    if (
        type(call) is not ComposerLLMCall
        or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or call.error_class != "_MalformedLLMResponseError"
        or call.error_message != "malformed_response"
    ):
        raise AuditIntegrityError("invalid semantic response lacks its original malformed audit")
    _require_response_audit_fields(call, metadata, attempt_id=attempt_id, started_at=started_at, finished_at=finished_at)


def _require_response_audit_fields(
    call: ComposerLLMCall,
    metadata: _AdmittedLLMProviderMetadata,
    *,
    attempt_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    if type(call) is not ComposerLLMCall or type(metadata) is not _AdmittedLLMProviderMetadata:
        raise AuditIntegrityError("received response lacks its actual complete nominal audit/metadata")
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
        raise AuditIntegrityError("received response changed its actual physical identity, finish, usage or native audit")


def require_v4_successful_response_audit(
    call: ComposerLLMCall,
    metadata: _AdmittedLLMProviderMetadata,
    *,
    policy: ContinuationAdmissionPolicyV4,
    attempt_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Compare V4 original DATA to its bounded diagnostic audit projection.

    Policy selects representation only. The caller must first prove the live
    issued result or actual retained V4 source/receipt; neither projection nor
    nominal policy grants custody, SDK origin or SQL/application permission.
    Full original metadata remains the credential/retention/replay input.
    """
    from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
    from elspeth.web.composer.provider_result_admission import project_v4_provider_diagnostic_metadata

    if type(policy) is not ContinuationAdmissionPolicyV4:
        raise AuditIntegrityError("V4 audit comparison requires its explicit admitted policy")
    require_successful_response_audit(
        call,
        project_v4_provider_diagnostic_metadata(metadata),
        attempt_id=attempt_id,
        started_at=started_at,
        finished_at=finished_at,
    )
