"""Explicit V4 representation checks and a real paid native-source reconstruction.

The pure comparison controls grant no SDK/SQL custody. The native control uses
the existing Composer owner harness and actual provider publication/cold source.
"""

import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_THINKING_PARTS_KEY, detach_prompt_guard_evidence
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation_codec import decode_continuation_response
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.continuation_provider_outcomes import (
    require_successful_response_audit,
    require_v4_successful_response_audit,
)
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.provider_result_admission import project_v4_provider_diagnostic_metadata
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation import private_chunk_key
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.helpers.continuation_policy import continuation_test_settings, minimal_continuation_policy
from tests.unit.web.composer.test_v4_provider_result_admission import _admit, _response
from tests.unit.web.sessions import test_continuation_cold_restore as cold_restore
from tests.unit.web.sessions import test_continuation_message_families as native
from tests.unit.web.sessions import test_continuation_runtime_provider_history as history

operation_store = native.operation_store
_REASONING = "r" * 100_000


def v4_policy():
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert type(policy) is ContinuationAdmissionPolicyV4
    return policy


def original_and_audit():
    admitted = _admit(_response(reasoning=_REASONING))
    metadata = admitted.metadata
    witness = metadata.v4_completeness
    assert witness is not None
    witness.require_metadata(metadata)
    call = build_llm_call_record(
        model_requested="model-original",
        messages=[{"role": "user", "content": "Original planner input"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response_metadata=metadata,
    )
    return metadata, replace(call, call_id=admitted.sdk_return.attempt_id)


def test_v4_full_original_reasoning_compares_to_its_bounded_audit_without_changing_legacy():
    metadata, call = original_and_audit()
    assert metadata.reasoning_content == _REASONING
    assert call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
    original = metadata.reasoning_content
    require_v4_successful_response_audit(
        call,
        metadata,
        policy=v4_policy(),
        attempt_id=call.call_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )
    assert metadata.reasoning_content is original
    with pytest.raises(AuditIntegrityError, match="actual physical identity"):
        require_successful_response_audit(call, metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at)


@pytest.mark.parametrize("field", ("reasoning_content", "provider_request_id", "provider_cost", "prompt_tokens"))
def test_v4_comparison_refuses_changed_bounded_audit(field):
    metadata, call = original_and_audit()
    changed = replace(
        call,
        **{
            field: {"reasoning_content": "other diagnostic", "provider_request_id": "other-id", "provider_cost": 0.25, "prompt_tokens": 4}[
                field
            ]
        },
    )
    with pytest.raises(AuditIntegrityError, match="actual physical identity"):
        require_v4_successful_response_audit(
            changed,
            metadata,
            policy=v4_policy(),
            attempt_id=call.call_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )


def test_equal_diagnostic_projection_cannot_authorize_changed_original_metadata():
    metadata, _call = original_and_audit()
    changed = replace(metadata, reasoning_content="s" * 100_000)
    # Equality after diagnostic loss cannot prove equality of private originals.
    assert project_v4_provider_diagnostic_metadata(changed) == project_v4_provider_diagnostic_metadata(metadata)
    assert metadata.v4_completeness is not None
    with pytest.raises(AuditIntegrityError, match="original issued metadata"):
        metadata.v4_completeness.require_metadata(changed)


def test_v4_comparison_refuses_legacy_policy_instead_of_selecting_projection():
    metadata, call = original_and_audit()
    with pytest.raises(AuditIntegrityError, match="explicit admitted policy"):
        require_v4_successful_response_audit(
            call,
            metadata,
            policy=cast(ContinuationAdmissionPolicyV4, minimal_continuation_policy()),
            attempt_id=call.call_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )


@pytest.mark.asyncio
async def test_actual_v4_native_reconstruction_proves_full_original_before_diagnostic_comparison(operation_store, tmp_path, monkeypatch):
    policy = v4_policy()
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    monkeypatch.setattr(cold_restore, "minimal_continuation_policy", lambda: policy)
    prepare_original = history.prepared_runtime
    sdk_responses = []

    async def prepare_with_full_original(*args, **kwargs):
        arguments, runtime, calls = await prepare_original(*args, **kwargs)
        original_sdk = litellm.acompletion

        async def returned_response(**sdk_kwargs):
            response = await original_sdk(**sdk_kwargs)
            response.choices[0].message.reasoning_content = _REASONING
            sdk_responses.append(response)
            return response

        monkeypatch.setattr(litellm, "acompletion", returned_response)
        return arguments, runtime, calls

    # Control only the actual SDK response used by the existing harness;
    # preparation, dispatch, physical audit, ledger and receipt stay real.
    monkeypatch.setattr(native, "prepared_runtime", prepare_with_full_original)
    case = await native.native_case(store, tmp_path, monkeypatch)
    running = case["kwargs"]["inventory"].running
    try:
        assert case["kwargs"]["selected"].stack.policy_hash == policy.policy_hash
        cold = decode_continuation_response(case["response"])
        assert cold.provider_metadata.v4_completeness is None
        assert cold.provider_metadata.reasoning_content == _REASONING
        assert case["recorder"].llm_calls[0].reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
        actual = await native.restore_case(store, case)
        assert detach_prompt_guard_evidence(actual.messages).messages == detach_prompt_guard_evidence(case["original_messages"]).messages
        assert actual.messages[-1][PROMPT_GUARD_THINKING_PARTS_KEY] == case["original_messages"][-1][PROMPT_GUARD_THINKING_PARTS_KEY]
        assert len(sdk_responses) == 1
        key = private_chunk_key(case["response"])
        with store[0].begin() as conn:
            row = conn.execute(select(chunks).where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == key)).one()
            original = row.payload
            changed = original.replace(_REASONING.encode("utf-8"), ("s" * 100_000).encode("utf-8"))
            assert changed != original and len(changed) == len(original)
            conn.execute(update(chunks).where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == key).values(payload=changed))
        try:
            # Both private strings yield the same bounded diagnostic marker.
            # Actual retained-byte/source proof must reject before reconstruction.
            with pytest.raises(AuditIntegrityError, match=r"chunk.*(binding|digest)"):
                await native.restore_case(store, case)
            assert len(sdk_responses) == 1
        finally:
            with store[0].begin() as conn:
                conn.execute(update(chunks).where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == key).values(payload=original))
        await native.restore_case(store, case)
        assert len(sdk_responses) == 1
    finally:
        store[1].release(running.session_operation_context)
