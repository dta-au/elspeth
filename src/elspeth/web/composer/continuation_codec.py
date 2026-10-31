from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, cast

from pydantic import Field, JsonValue, ValidationError

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMProviderCostSource
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
    _AdmittedToolBatch,
    _AdmittedToolCall,
    _AdmittedToolFunction,
)
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationFrameStack,
    ContinuationValue,
    InitializeFrame,
    PrivateChunkRef,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.continuation_retention_memo import PrivateChunkVerificationKey, SnapshotDecodeKey, current_retention_stage_memo

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_progress import ContinuationFrontier


def encode_continuation_snapshot(frame_stack: ContinuationFrameStack) -> str:
    from elspeth.web.composer.continuation_policy import decode_continuation_policy

    if type(frame_stack) is not ContinuationFrameStack:
        raise TypeError("continuation codec requires its owned frame stack")
    decode_continuation_policy(frame_stack.admission_policy_json, frame_stack.policy_hash)
    return canonical_json(frame_stack.model_dump(mode="json"))


def decode_continuation_snapshot(encoded: str) -> ContinuationFrameStack:
    memo = current_retention_stage_memo()
    if memo is None or type(encoded) is not str:
        return _decode_continuation_snapshot_uncached(encoded)
    return memo.snapshot(SnapshotDecodeKey(encoded), _decode_continuation_snapshot_uncached)


def _decode_continuation_snapshot_uncached(encoded: str) -> ContinuationFrameStack:
    from elspeth.web.composer.continuation_policy import decode_continuation_policy

    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation snapshot is not strict JSON")
    try:
        # JSON mode admits JSON arrays as tuples while retaining scalar strictness.
        stack = ContinuationFrameStack.model_validate_json(canonical_json(value))
    except ValidationError as exc:
        raise AuditIntegrityError("continuation snapshot violates its owned codec") from exc
    decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if stack.active_action_json is not None:
        from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action

        decode_prescribed_continuation_action(stack.active_action_json)
    return stack


@dataclass(frozen=True, slots=True)
class PrivateChunk:
    reference: PrivateChunkRef
    canonical: str


def encode_mandatory_private_chunk(*, codec: str, value: object, env_ref_names: frozenset[str] = frozenset()) -> PrivateChunk:
    """Encode a fresh owner's complete value after its local admission checks.

    The default string codec below remains the integrity-only path used for
    retained-data verification. Fresh callers must establish their original
    graph/source/credential authority before invoking this resource obligation.
    """
    from elspeth.web.composer.continuation_retention import bind_retained_json_encoding, preflight_retained_json

    expected = preflight_retained_json(value, reason="retained_chunk_bytes")
    canonical = bind_retained_json_encoding(canonical_json(value), expected_bytes=expected)
    return encode_private_chunk(codec=codec, canonical=canonical, env_ref_names=env_ref_names)


def encode_private_chunk(
    *,
    codec: str,
    canonical: str,
    env_ref_names: frozenset[str] = frozenset(),
) -> PrivateChunk:
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.credential_guard import require_no_credential_material, require_no_credential_material_in_state

    if utf8_size(canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("private chunk exceeds the complete JSON admission bound")
    encoded = canonical.encode("utf-8")
    value, error = parse_json_strict(canonical)
    if error is not None or canonical_json(value) != canonical:
        raise AuditIntegrityError("private chunk must retain canonical strict JSON")
    if codec == "composer.state.v1":
        if type(value) is not dict:
            raise AuditIntegrityError("state chunk is not an owned state object")
        state = CompositionState.from_dict(value)
        require_no_credential_material_in_state(
            state,
            surface="composer_continuation_state",
            env_ref_names=env_ref_names,
        )
    else:
        require_no_credential_material(
            canonical,
            surface="composer_continuation_private_chunk",
            env_ref_names=env_ref_names,
            max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
        )
    reference = PrivateChunkRef.model_validate(
        {
            "codec": codec,
            "digest": sha256(encoded).hexdigest(),
            "byte_length": len(encoded),
        }
    )
    return PrivateChunk(reference, canonical)


def verify_private_chunk(reference: PrivateChunkRef, canonical: str) -> None:
    memo = current_retention_stage_memo()
    if memo is None or type(canonical) is not str or not _private_chunk_memo_reference(reference):
        _verify_private_chunk_canonical(canonical)
    else:
        memo.private_chunk(PrivateChunkVerificationKey(reference, canonical), _verify_private_chunk_canonical)
    encoded = canonical.encode("utf-8")
    if len(encoded) != reference.byte_length or sha256(encoded).hexdigest() != reference.digest:
        raise AuditIntegrityError("private chunk bytes differ from their binding")


def _private_chunk_memo_reference(reference: PrivateChunkRef) -> bool:
    if type(reference) is not PrivateChunkRef:
        return False
    try:
        # model_copy/model_construct can retain malformed nominal fields. Do
        # not hash them or move their original refusal ahead of canonical JSON.
        return all(
            actual is expected
            for actual, expected in zip(
                (type(reference.codec), type(reference.digest), type(reference.byte_length)), (str, str, int), strict=True
            )
        )
    except AttributeError:
        return False


def _verify_private_chunk_canonical(canonical: str) -> None:
    value, error = parse_json_strict(canonical)
    if error is not None or canonical_json(value) != canonical:
        raise AuditIntegrityError("retained private chunk is not canonical strict JSON")


def encode_frontier_chunk(frontier: ContinuationFrontier) -> PrivateChunk:
    from elspeth.web.composer.continuation_progress import encode_continuation_frontier

    return encode_private_chunk(codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(frontier))


def decode_frontier_chunk(chunk: PrivateChunk) -> ContinuationFrontier:
    from elspeth.web.composer.continuation_progress import decode_continuation_frontier

    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.semantic-frontier.v1":
        raise AuditIntegrityError("semantic frontier has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    return decode_continuation_frontier(chunk.canonical)


def owned_json(value: object) -> JsonValue:
    if value is None or type(value) in (str, bool, int):
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise AuditIntegrityError("continuation value is not finite JSON")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise AuditIntegrityError("continuation object has a non-string key")
        return {cast(str, key): owned_json(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [owned_json(child) for child in value]
    raise AuditIntegrityError("continuation value is outside the owned JSON algebra")


class RetainedToolCall(ContinuationValue):
    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: str


class RetainedUsage(ContinuationValue):
    prompt_tokens: int | None
    completion_tokens: int | None
    reported_total: int | None
    cached_prompt_tokens: int | None
    cache_creation_input_tokens: int | None
    cache_read_input_tokens: int | None
    reasoning_tokens: int | None


class RetainedProviderMetadata(ContinuationValue):
    model_returned: str | None
    finish_reason: str | None
    usage: RetainedUsage
    provider_cost: float | None
    provider_cost_source: ComposerLLMProviderCostSource
    provider_request_id: str | None
    reasoning_content: str | None
    reasoning_details: JsonValue
    thinking_blocks: JsonValue
    provider_served: str | None


class RetainedAdvisorResultMetadata(ContinuationValue):
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    latency_ms: int = Field(ge=0)


class RetainedCompletion(ContinuationValue):
    schema_version: Literal["composer.response.v1"] = "composer.response.v1"
    content: str | None
    calls: tuple[RetainedToolCall, ...]
    metadata: RetainedProviderMetadata
    advisor_result_metadata: RetainedAdvisorResultMetadata | None = None
    audit_call_json: str | None = None
    native_thinking_json: str | None = None


def _ordered_native_thinking(value: object) -> str | None:
    if value is None:
        return None
    from elspeth.web.composer.provider_thinking import native_thinking_blocks

    blocks = native_thinking_blocks(value)
    return json.dumps(blocks, ensure_ascii=False, separators=(",", ":"), allow_nan=False) if blocks else None


def _restore_ordered_native_thinking(value: JsonValue, ordered: str | None) -> JsonValue:
    if ordered is None:
        # Legacy non-native records retain their previous reader. Current
        # native message reconstruction separately requires the new source.
        return value
    from elspeth.web.composer.protocol import ComposerAdmissionRefused

    parsed, error = parse_json_strict(ordered)
    try:
        actual_ordered = _ordered_native_thinking(parsed) if error is None and type(parsed) is list else None
    except ComposerAdmissionRefused as cause:
        raise AuditIntegrityError("retained native thinking violates its closed provider shape") from cause
    if error is not None or type(parsed) is not list or actual_ordered != ordered:
        raise AuditIntegrityError("retained native thinking lost its complete ordered source")
    if canonical_json(owned_json(parsed)) != canonical_json(value):
        raise AuditIntegrityError("retained native thinking differs from its complete response metadata")
    return owned_json(parsed)


def require_continuation_request_admission(request_json: str) -> None:
    from elspeth.web.credential_guard import require_no_credential_material
    from elspeth.web.sessions.composer_operations import COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH

    if type(request_json) is not str or len(request_json.encode("utf-8")) > COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH:
        raise AuditIntegrityError("continuation request exceeds its admitted byte bound")
    value, error = parse_json_strict(request_json)
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("continuation request is not one strict JSON object")
    require_no_credential_material(
        value,
        surface="composer_continuation_initializer",
        max_string_bytes=COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH,
    )


def encode_continuation_response(
    completion: _AdmittedLLMCompletion,
    *,
    advisor_result_metadata: RetainedAdvisorResultMetadata | None = None,
    audit_call: ComposerLLMCall | None = None,
) -> PrivateChunk:
    if type(completion) is not _AdmittedLLMCompletion:
        raise TypeError("response retention requires an admitted completion")
    audit_json = None
    if audit_call is not None:
        from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call

        audit_json = encode_physical_audit_call(audit_call)
    calls = completion.tool_batch.calls
    if len({call.id for call in calls}) != len(calls) or completion.tool_batch.call_ids != frozenset(call.id for call in calls):
        raise AuditIntegrityError("admitted response has inconsistent tool identities")
    metadata = completion.provider_metadata
    usage = metadata.usage
    if advisor_result_metadata is not None and type(advisor_result_metadata) is not RetainedAdvisorResultMetadata:
        raise TypeError("Advisor result metadata requires its complete actual owned value")
    retained = RetainedCompletion(
        native_thinking_json=_ordered_native_thinking(metadata.thinking_blocks),
        audit_call_json=audit_json,
        advisor_result_metadata=advisor_result_metadata,
        content=completion.message.content,
        calls=tuple(
            RetainedToolCall(
                call_id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in calls
        ),
        metadata=RetainedProviderMetadata(
            model_returned=metadata.model_returned,
            finish_reason=metadata.finish_reason,
            usage=RetainedUsage(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                reported_total=usage.reported_total,
                cached_prompt_tokens=usage.cached_prompt_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            ),
            provider_cost=metadata.provider_cost,
            provider_cost_source=metadata.provider_cost_source,
            provider_request_id=metadata.provider_request_id,
            reasoning_content=metadata.reasoning_content,
            reasoning_details=owned_json(metadata.reasoning_details),
            thinking_blocks=owned_json(metadata.thinking_blocks),
            provider_served=metadata.provider_served,
        ),
    )
    return encode_private_chunk(
        codec="composer.response.v1",
        canonical=canonical_json(
            retained.model_dump(
                mode="json",
                exclude={"native_thinking_json"} if retained.native_thinking_json is None else None,
            )
        ),
    )


def decode_continuation_response(chunk: PrivateChunk) -> _AdmittedLLMCompletion:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.response.v1":
        raise AuditIntegrityError("continuation response has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        retained = RetainedCompletion.model_validate_json(chunk.canonical)
    except ValidationError as exc:
        raise AuditIntegrityError("retained response violates its owned shape") from exc
    if "native_thinking_json" in retained.model_fields_set and retained.native_thinking_json is None:
        raise AuditIntegrityError("retained native thinking field must be absent or complete ordered text")
    if len({call.call_id for call in retained.calls}) != len(retained.calls):
        raise AuditIntegrityError("retained response repeats a tool identity")
    if retained.audit_call_json is not None:
        from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call

        decode_physical_audit_call(retained.audit_call_json)
    metadata = retained.metadata
    usage = metadata.usage
    calls = tuple(
        _AdmittedToolCall(
            call.call_id,
            _AdmittedToolFunction(call.name, call.arguments),
        )
        for call in retained.calls
    )
    return _AdmittedLLMCompletion(
        message=_AdmittedAssistantMessage(retained.content),
        tool_batch=_AdmittedToolBatch(calls, frozenset(call.id for call in calls)),
        provider_metadata=_AdmittedLLMProviderMetadata(
            model_returned=metadata.model_returned,
            finish_reason=metadata.finish_reason,
            usage=TokenUsage(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                reported_total=usage.reported_total,
                cached_prompt_tokens=usage.cached_prompt_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            ),
            provider_cost=metadata.provider_cost,
            provider_cost_source=metadata.provider_cost_source,
            provider_request_id=metadata.provider_request_id,
            reasoning_content=metadata.reasoning_content,
            reasoning_details=metadata.reasoning_details,
            thinking_blocks=_restore_ordered_native_thinking(metadata.thinking_blocks, retained.native_thinking_json),
            provider_served=metadata.provider_served,
        ),
    )


def encode_continuation_text_response(
    *,
    content: str | None,
    provider_metadata: _AdmittedLLMProviderMetadata,
    advisor_result_metadata: RetainedAdvisorResultMetadata | None = None,
    audit_call: ComposerLLMCall | None = None,
) -> PrivateChunk:
    if type(provider_metadata) is not _AdmittedLLMProviderMetadata or (content is not None and type(content) is not str):
        raise TypeError("text response retention requires complete admitted content and provider metadata")
    return encode_continuation_response(
        _AdmittedLLMCompletion(
            message=_AdmittedAssistantMessage(content),
            tool_batch=_AdmittedToolBatch((), frozenset()),
            provider_metadata=provider_metadata,
        ),
        advisor_result_metadata=advisor_result_metadata,
        audit_call=audit_call,
    )


def decode_retained_provider_metadata(chunk: PrivateChunk) -> _AdmittedLLMProviderMetadata:
    return decode_continuation_response(chunk).provider_metadata


def decode_retained_advisor_result_metadata(chunk: PrivateChunk) -> RetainedAdvisorResultMetadata:
    decode_continuation_response(chunk)
    retained = RetainedCompletion.model_validate_json(chunk.canonical)
    if retained.advisor_result_metadata is None:
        raise AuditIntegrityError("manual Advisor response lost its actual result metadata")
    return retained.advisor_result_metadata


def decode_retained_physical_audit_call(chunk: PrivateChunk) -> ComposerLLMCall:
    from elspeth.web.composer.continuation_provider_outcomes import decode_physical_audit_call

    decode_continuation_response(chunk)
    retained = RetainedCompletion.model_validate_json(chunk.canonical)
    if retained.audit_call_json is None:
        raise AuditIntegrityError("production response lost its actual complete physical audit call")
    return decode_physical_audit_call(retained.audit_call_json)


def snapshot_after_provider_response(
    stack: ContinuationFrameStack,
    response_ref: PrivateChunkRef,
) -> ContinuationFrameStack:
    if response_ref.codec != "composer.response.v1":
        raise AuditIntegrityError("provider response reference has the wrong codec")
    top = stack.frames[-1]
    changed: ComposeFrame | AdvisorFrame | InitializeFrame
    if isinstance(top, ComposeFrame) and top.phase == "awaiting_response":
        changed = top.model_copy(
            update={
                "phase": "response_retained",
                "pending_response": response_ref,
            }
        )
    elif isinstance(top, AdvisorFrame) and top.phase == "awaiting_response":
        changed = top.model_copy(
            update={
                "phase": "response_retained",
                "response": response_ref,
            }
        )
    elif isinstance(top, InitializeFrame) and top.ingress_phase == "title_pending":
        if top.title_response is not None:
            raise AuditIntegrityError("title response was already retained")
        changed = top.model_copy(update={"title_response": response_ref})
    else:
        raise AuditIntegrityError("provider response has no awaiting owner frame")
    return stack.model_copy(update={"frames": (*stack.frames[:-1], changed)})
