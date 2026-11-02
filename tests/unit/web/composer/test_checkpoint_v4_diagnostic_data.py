"""Original-parser DATA controls; these do not claim V4 physical/SQL custody."""

import time
from dataclasses import replace

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    _observe_checkpoint_returned_response,
    observe_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import decode_continuation_response
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.llm_response_parsing import admit_v4_provider_result, build_llm_call_record
from elspeth.web.composer.provider_result_admission import V4ProviderSdkReturn
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_advisor_invocation_outcome import advisor_case, reply
from tests.unit.web.composer.test_continuation_checkpoint_observation import _actual_checkpoint
from tests.unit.web.composer.test_continuation_checkpoint_observation import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_checkpoint_original_100k_metadata_matches_diagnostic_only_after_original_witness(operation_store, tmp_path, monkeypatch):
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, _parsed, trigger, content = await _actual_checkpoint(advisor, provider, monkeypatch, phase="end")
        assert physical.attempt is not None and physical.audit_call is not None
        raw = reply(content)
        raw.choices[0].message.reasoning_content = "r" * 100_000
        admitted = admit_v4_provider_result(
            raw,
            choice=raw.choices[0],
            message=raw.choices[0].message,
            sdk_return=V4ProviderSdkReturn(physical.attempt.attempt_id, id(raw)),
        )
        metadata = admitted.metadata
        witness = metadata.v4_completeness
        assert witness is not None
        diagnostic = build_llm_call_record(
            model_requested=physical.audit_call.model_requested,
            messages=[],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=physical.audit_call.started_at,
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            response_metadata=metadata,
        )
        original = replace(
            physical,
            message=admitted.message,
            provider_metadata=metadata,
            guidance=content,
            metadata={
                "model": metadata.model_returned or diagnostic.model_requested,
                "prompt_tokens": metadata.usage.prompt_tokens,
                "completion_tokens": metadata.usage.completion_tokens,
                "cached_prompt_tokens": metadata.usage.cached_prompt_tokens,
                "latency_ms": 0,
            },
            audit_call=replace(diagnostic, call_id=physical.attempt.attempt_id),
        )
        policy = continuation_policy_v4_from_settings(
            continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
        )
        assert policy is not None

        def observe(value):
            # Pure parser witness only. Production additionally requires the
            # real custody before it can reach this internal DATA comparator.
            witness.require_result(value.provider_metadata, content=value.message.content, tool_calls=())
            return _observe_checkpoint_returned_response(
                value,
                phase="end",
                trigger=trigger,
                attempt=physical.attempt,
                context=context,
                env_ref_names=frozenset(),
                v4_policy=policy,
            )

        result = observe(original)
        assert type(result) is CheckpointReturnedResponseObservation
        assert decode_continuation_response(result.response).provider_metadata.reasoning_content == "r" * 100_000
        assert result.audit_call.reasoning_content == "<provider-artifact-unavailable>"
        with pytest.raises(AuditIntegrityError):
            observe(replace(original, provider_metadata=replace(metadata, reasoning_content="s" * 100_000)))
        with pytest.raises(AuditIntegrityError, match="physical identity"):
            observe(replace(original, audit_call=replace(original.audit_call, reasoning_content="changed audit")))
        with pytest.raises(AuditIntegrityError, match="physical identity"):
            observe_checkpoint_returned_response(
                original,
                phase="end",
                trigger=trigger,
                attempt=physical.attempt,
                context=context,
                env_ref_names=frozenset(),
            )
    finally:
        coordinator.assert_completed()
