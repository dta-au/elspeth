"""Real LiteLLM ingress shapes; these pure tests grant no physical SDK custody."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from litellm.types.utils import ModelResponse

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.llm_response_parsing import (
    admit_llm_provider_metadata,
    admit_v4_provider_result,
    build_llm_call_record,
)
from elspeth.web.composer.provider_result_admission import (
    V4ProviderResultIncomplete,
    V4ProviderSdkReturn,
    diagnostic_provider_metadata,
    require_v4_provider_metadata_credentials,
)
from elspeth.web.credential_guard import CredentialMaterialRefused


def _response(*, reasoning="ordinary reasoning", details=None):
    return ModelResponse(
        id="response-original",
        model="model-original",
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "original answer",
                    "tool_calls": [],
                    "reasoning_content": reasoning,
                    "reasoning_details": details,
                },
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "cost": 0.125},
    )


def _admit(response):
    choice = response.choices[0]
    # The gateway controls actual mint/claim identity. This is only a pure
    # parser fixture and does not assert that a provider call occurred.
    returned = V4ProviderSdkReturn("attempt-parser-fixture", id(response))
    return admit_v4_provider_result(response, choice=choice, message=choice.message, sdk_return=returned)


@pytest.mark.parametrize("size", [65_536, 65_537, 262_145, 300_000])
def test_original_reasoning_survives_legacy_diagnostic_limits(size):
    original = "r" * size
    response = _response(reasoning=original, details=[{"text": "original detail"}])
    admitted = _admit(response)
    metadata = admitted.metadata
    assert metadata.reasoning_content == original
    assert deep_thaw(metadata.reasoning_details) == [{"text": "original detail"}]
    assert metadata.provider_cost == 0.125 and metadata.usage.prompt_tokens == 3
    witness = metadata.v4_completeness
    assert witness is not None and witness.sdk_return is admitted.sdk_return
    witness.require_result(metadata, content=admitted.message.content, tool_calls=admitted.tool_calls)
    require_v4_provider_metadata_credentials(metadata, content=admitted.message.content, tool_calls=(), surface="test-v4")
    diagnostic = diagnostic_provider_metadata(metadata)
    assert diagnostic.reasoning_content == (original if size <= 65_536 else "<provider-artifact-unavailable>")


def test_full_private_identifiers_keep_existing_bounded_audit_projection():
    response = _response(reasoning="r" * 70_000)
    response.id = "id" * 300
    response.model = "model" * 100
    response.choices[0].finish_reason = "finish" * 50
    admitted = _admit(response)
    metadata = admitted.metadata
    assert (metadata.model_returned, metadata.provider_request_id, metadata.finish_reason) == (
        response.model,
        response.id,
        response.choices[0].finish_reason,
    )
    call = build_llm_call_record(
        model_requested="requested-model",
        messages=[],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response_metadata=metadata,
    )
    diagnostic = diagnostic_provider_metadata(metadata)
    assert (call.model_returned, call.provider_request_id, call.finish_reason) == (
        diagnostic.model_returned,
        diagnostic.provider_request_id,
        diagnostic.finish_reason,
    )
    assert len(call.model_returned) <= 256 and len(call.provider_request_id) <= 256 and len(call.finish_reason) <= 128
    assert call.reasoning_content == "<provider-artifact-unavailable>" and call.provider_cost == 0.125


def test_source_sdk_mutation_cannot_change_the_owned_full_result():
    response = _response(details=[{"text": "original"}])
    admitted = _admit(response)
    response.choices[0].message.reasoning_details[0]["text"] = "changed SDK value"
    assert deep_thaw(admitted.metadata.reasoning_details) == [{"text": "original"}]
    witness = admitted.metadata.v4_completeness
    assert witness is not None
    with pytest.raises(AuditIntegrityError, match="original issued"):
        diagnostic_provider_metadata(replace(admitted.metadata))
    with pytest.raises(AuditIntegrityError, match="original selected content"):
        witness.require_result(admitted.metadata, content="changed", tool_calls=())


@pytest.mark.parametrize("literal", ["<provider-artifact-unavailable>", "original<truncated:900000>"])
def test_literal_diagnostic_markers_are_preserved_without_inferred_loss(literal):
    admitted = _admit(_response(reasoning=literal, details={"text": literal}))
    assert admitted.metadata.reasoning_content == literal
    assert deep_thaw(admitted.metadata.reasoning_details) == {"text": literal}
    assert admitted.metadata.v4_completeness is not None


@pytest.mark.parametrize("kind", ["opaque", "recursive", "nonfinite", "depth", "invalid-utf8", "scalar-excess"])
def test_unavailable_original_is_explicit_and_keeps_honest_monetary_facts(kind):
    class Opaque:
        __slots__ = ()

        def model_dump(self):
            raise AssertionError("provider serializer must never execute")

    details = {"text": "ordinary"}
    reasoning = "ordinary"
    if kind == "opaque":
        details = Opaque()
    elif kind == "recursive":
        details = []
        details.append(details)
    elif kind == "nonfinite":
        details = {"number": float("inf")}
    elif kind == "depth":
        for _ in range(70):
            details = [details]
    elif kind == "invalid-utf8":
        reasoning = "\ud800"
    else:
        reasoning = "r" * 1_048_577
    response = _response()
    response.choices[0].message.reasoning_content = reasoning
    response.choices[0].message.reasoning_details = details
    with pytest.raises(V4ProviderResultIncomplete) as caught:
        _admit(response)
    incomplete = caught.value
    assert incomplete.sdk_return.response_identity == id(response)
    assert incomplete.metadata.provider_cost == 0.125 and incomplete.metadata.usage.completion_tokens == 4
    assert incomplete.metadata.v4_completeness is None
    assert incomplete.losses
    if kind == "scalar-excess":
        assert any(loss.reason == "scalar_utf8_limit" and loss.scalar_utf8_bytes == 1_048_577 for loss in incomplete.losses)
    else:
        assert all(loss.scalar_utf8_bytes is None for loss in incomplete.losses)
    assert "original answer" not in str(incomplete)


def test_credential_after_old_cutoff_keeps_native_refusal():
    response = _response(reasoning="ordinary " * 34_000 + "password=" + "original-secret")
    with pytest.raises(CredentialMaterialRefused):
        _admit(response)


def test_real_litellm_legacy_projection_keeps_original_lossy_contract():
    response = _response(reasoning="r" * 70_000, details=[{"text": "d" * 70_000}])
    choice = response.choices[0]
    metadata = admit_llm_provider_metadata(response, choice=choice, message=choice.message)
    assert metadata.reasoning_content == metadata.reasoning_details == "<provider-artifact-unavailable>"
    assert metadata.v4_completeness is None and metadata.provider_cost == 0.125


def test_foreign_successful_return_cannot_admit_another_sdk_object():
    response = _response()
    choice = response.choices[0]
    with pytest.raises(AuditIntegrityError, match="exact successful SDK-return"):
        admit_v4_provider_result(
            response,
            choice=choice,
            message=choice.message,
            sdk_return=V4ProviderSdkReturn("attempt-other", id(_response())),
        )
