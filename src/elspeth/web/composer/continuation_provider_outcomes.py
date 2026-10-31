from __future__ import annotations

from datetime import datetime

from pydantic import TypeAdapter, ValidationError

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMProviderMetadata
from elspeth.web.composer.continuation_codec import owned_json

_PHYSICAL_AUDIT = TypeAdapter(ComposerLLMCall)


def encode_physical_audit_call(call: ComposerLLMCall) -> str:
    if type(call) is not ComposerLLMCall:
        raise TypeError("physical audit retention requires its complete actual nominal record")
    projected = _PHYSICAL_AUDIT.dump_python(call, mode="json", warnings="error", fallback=owned_json)
    return canonical_json(owned_json(projected))


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
    if type(call) is not ComposerLLMCall or type(metadata) is not _AdmittedLLMProviderMetadata:
        raise AuditIntegrityError("received response lacks its actual complete nominal audit/metadata")
    usage = metadata.usage
    if (
        call.status is not ComposerLLMCallStatus.SUCCESS
        or call.call_id != attempt_id
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
