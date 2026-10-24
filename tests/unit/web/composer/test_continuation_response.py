from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
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
    ContinuationFrameStack,
    ContinuationProviderCostPolicy,
    InitializeFrame,
    LogicalRequestBinding,
    MutationCeilingSnapshot,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    RetainedAdvisorResultMetadata,
    decode_continuation_response,
    decode_retained_advisor_result_metadata,
    decode_retained_provider_metadata,
    encode_continuation_response,
    encode_continuation_text_response,
    encode_private_chunk,
    require_continuation_request_admission,
    snapshot_after_provider_response,
)
from elspeth.web.composer.continuation_policy import (
    ContinuationActionBounds,
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    ContinuationSegmentPolicy,
)
from elspeth.web.credential_guard import CredentialMaterialRefused


def completion():
    calls = (
        _AdmittedToolCall("first", _AdmittedToolFunction("get_plugin_schema", '{"name":"csv","type":"source"}')),
        _AdmittedToolCall("second", _AdmittedToolFunction("list_sinks", "{}")),
    )
    return _AdmittedLLMCompletion(
        _AdmittedAssistantMessage("café"),
        _AdmittedToolBatch(calls, frozenset({"first", "second"})),
        _AdmittedLLMProviderMetadata(
            model_returned="test/model",
            finish_reason="tool_calls",
            usage=TokenUsage(reported_total=41, cached_prompt_tokens=0),
            provider_cost=None,
            provider_cost_source="not_available",
            provider_request_id="physical-1",
            reasoning_content=None,
            reasoning_details=[{"type": "reasoning.summary", "text": "native summary"}],
            thinking_blocks=[
                {"type": "thinking", "thinking": "first", "signature": "signature-1"},
                {"type": "redacted_thinking", "data": "opaque-second"},
            ],
            provider_served="Test Provider",
        ),
    )


def test_initializer_scans_full_large_original_before_private_retention():
    clean = {"content": "x" * 300_000}
    require_continuation_request_admission(canonical_json(clean))
    late = clean | {"password": "example-value"}  # secret-scan: allow-this-line
    with pytest.raises(CredentialMaterialRefused):
        require_continuation_request_admission(canonical_json(late))


def stack_for(top):
    action = ContinuationActionPolicy(
        preparation=1.0,
        guard=2.0,
        quota=3.0,
        physical=4.0,
        retention=5.0,
    )
    policy = ContinuationAdmissionPolicy(
        segment=ContinuationSegmentPolicy(
            nominal=300.0,
            watermark=120.0,
            quantum=180.0,
            reserve=30.0,
        ),
        actions=ContinuationActionBounds(
            primary=action,
            advisor=action,
            title=action,
            read=action,
            session_sql=action,
            blob=action,
            external=action,
        ),
    )
    return ContinuationFrameStack(
        logical=LogicalRequestBinding(
            session_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000002",
            original_user_message_id="original",
            request_hash="a" * 64,
            kind="compose_message",
            base_state_id=None,
        ),
        mutation_ceiling=MutationCeilingSnapshot(
            session_id="00000000-0000-4000-8000-000000000001",
            request_id="original",
            initial_trust_mode="auto_commit",
        ),
        provider_cost_policy=ContinuationProviderCostPolicy(
            applicability="not_applicable",
            lifecycle_version=2,
            accounting_owner="physical_audit_and_token_ledger",
        ),
        policy_hash=policy.policy_hash,
        admission_policy_json=canonical_json(policy.model_dump(mode="json")),
        cursor=7,
        frames=(top,),
        effect_frontier=(),
        pending_settlement_ids=("actual-attempt",),
        semantic_frontier=None,
    )


def test_response_cold_roundtrip_preserves_all_admitted_fields_and_native_order():
    original = completion()
    retained = encode_continuation_response(original)
    cold = PrivateChunk(retained.reference, retained.canonical.encode().decode())
    restored = decode_continuation_response(cold)
    assert restored == original
    assert restored is not original
    assert restored.tool_batch.calls[0].id == "first"
    assert restored.provider_metadata.usage.prompt_tokens is None
    assert restored.provider_metadata.usage.reported_total == 41
    assert encode_continuation_response(restored) == retained


def test_response_decoder_refuses_changed_bytes_and_duplicate_call_identity():
    retained = encode_continuation_response(completion())
    with pytest.raises(AuditIntegrityError, match="binding"):
        decode_continuation_response(
            PrivateChunk(
                retained.reference,
                retained.canonical.replace("café", "cafe"),
            )
        )
    original = completion()
    duplicate = replace(
        original,
        tool_batch=_AdmittedToolBatch(
            (original.tool_batch.calls[0], original.tool_batch.calls[0]),
            frozenset({"first"}),
        ),
    )
    with pytest.raises(AuditIntegrityError, match="identities"):
        encode_continuation_response(duplicate)


def test_advisor_response_is_retained_without_clearing_real_settlement():
    chunk = encode_continuation_response(completion())
    review = encode_private_chunk(codec="composer.advisor.v1", canonical="{}").reference
    before = stack_for(
        AdvisorFrame(
            phase="awaiting_response",
            review=review,
            response=None,
            coverage_binding="exact-coverage",
        )
    )
    after = snapshot_after_provider_response(before, chunk.reference)
    assert after.pending_settlement_ids == ("actual-attempt",)
    assert after.cursor == before.cursor
    assert after.frames[-1].phase == "response_retained"
    assert after.frames[-1].response == chunk.reference
    assert not after.is_handoff_safe


def test_title_response_does_not_republish_or_complete_title_phase():
    chunk = encode_continuation_response(completion())
    before = stack_for(
        InitializeFrame(
            request_json='{"content":"Build"}',
            ingress_phase="title_pending",
            title_attempt_id="actual-title-attempt",
            bootstrap=encode_private_chunk(codec="composer.bootstrap.v1", canonical="{}").reference,
        )
    )
    after = snapshot_after_provider_response(before, chunk.reference)
    top = after.frames[-1]
    assert top.ingress_phase == "title_pending"
    assert top.title_attempt_id == "actual-title-attempt"
    assert top.title_response == chunk.reference
    assert after.pending_settlement_ids == before.pending_settlement_ids
    with pytest.raises(AuditIntegrityError, match="already retained"):
        snapshot_after_provider_response(after, chunk.reference)


def test_response_cannot_be_attached_to_a_ready_initializer():
    chunk = encode_continuation_response(completion())
    before = stack_for(
        InitializeFrame(
            request_json="{}",
            ingress_phase="ready",
            title_attempt_id=None,
            bootstrap=encode_private_chunk(codec="composer.bootstrap.v1", canonical="{}").reference,
        )
    )
    with pytest.raises(AuditIntegrityError, match="awaiting owner"):
        snapshot_after_provider_response(before, chunk.reference)


def test_text_families_retain_complete_known_usage_and_native_metadata():
    actual = completion().provider_metadata
    chunk = encode_continuation_text_response(content="Complete owned reply", provider_metadata=actual)
    restored = decode_continuation_response(PrivateChunk(chunk.reference, chunk.canonical.encode().decode()))
    assert restored.message.content == "Complete owned reply"
    assert restored.tool_batch.calls == () and restored.tool_batch.call_ids == frozenset()
    assert restored.provider_metadata == actual
    assert decode_retained_provider_metadata(chunk) == actual
    assert restored.provider_metadata.usage.reported_total == 41
    assert restored.provider_metadata.usage.cached_prompt_tokens == 0
    assert restored.provider_metadata.thinking_blocks == actual.thinking_blocks


def test_manual_advisor_retains_exact_result_metadata_in_the_same_response():
    actual = completion().provider_metadata
    result = RetainedAdvisorResultMetadata(
        model="actual-served-model", prompt_tokens=27, completion_tokens=None, cached_prompt_tokens=0, latency_ms=1847
    )
    chunk = encode_continuation_text_response(
        content="Inspect the original source", provider_metadata=actual, advisor_result_metadata=result
    )
    cold = PrivateChunk(chunk.reference, chunk.canonical.encode().decode())
    assert decode_retained_advisor_result_metadata(cold).model_dump(mode="python") == {
        "model": "actual-served-model",
        "prompt_tokens": 27,
        "completion_tokens": None,
        "cached_prompt_tokens": 0,
        "latency_ms": 1847,
    }
    assert decode_retained_provider_metadata(cold) == actual
    absent = encode_continuation_text_response(content="Reply", provider_metadata=actual)
    with pytest.raises(AuditIntegrityError, match="actual result metadata"):
        decode_retained_advisor_result_metadata(absent)
