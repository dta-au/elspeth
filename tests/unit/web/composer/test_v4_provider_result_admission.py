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
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolBatch
from elspeth.web.composer.continuation_codec import decode_continuation_response, encode_continuation_response
from elspeth.web.composer.llm_response_parsing import (
    PROVIDER_STRING_TRUNCATION_MARKER,
    admit_llm_provider_metadata,
    admit_v4_provider_result,
    build_llm_call_record,
)
from elspeth.web.composer.provider_result_admission import (
    V4ProviderResultIncomplete,
    V4ProviderSdkReturn,
    diagnostic_provider_metadata,
    project_v4_provider_diagnostic_metadata,
    require_issued_v4_incomplete,
    require_retained_v4_provider_metadata_credentials,
    require_v4_provider_metadata_credentials,
)
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_in_llm_metadata


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
    # The existing diagnostic bound caps the payload prefix; the explicit
    # original-length marker follows it and is part of the stored evidence.
    expected = tuple(
        f"{original[:limit]}{PROVIDER_STRING_TRUNCATION_MARKER}{len(original)}>"
        for original, limit in ((response.model, 256), (response.id, 256), (response.choices[0].finish_reason, 128))
    )
    assert (call.model_returned, call.provider_request_id, call.finish_reason) == expected
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


def _cold_original():
    response = _response(reasoning="r" * 300_000, details=[{"text": "d" * 70_000}])
    response.id = "original-id" * 40
    admitted = _admit(response)
    complete = _AdmittedLLMCompletion(
        admitted.message,
        _AdmittedToolBatch(admitted.tool_calls, frozenset(call.id for call in admitted.tool_calls)),
        admitted.metadata,
    )
    chunk = encode_continuation_response(complete)
    assert chunk.reference.byte_length <= 1_048_576
    return admitted, decode_continuation_response(chunk)


def test_cold_diagnostic_projection_retains_full_private_data_without_minting_witness():
    admitted, cold = _cold_original()
    private = cold.provider_metadata
    assert private.v4_completeness is None
    assert private.reasoning_content == "r" * 300_000
    assert deep_thaw(private.reasoning_details) == [{"text": "d" * 70_000}]
    assert diagnostic_provider_metadata(private) is private
    projected = project_v4_provider_diagnostic_metadata(private)
    assert projected == diagnostic_provider_metadata(admitted.metadata)
    assert projected.v4_completeness is None and private.v4_completeness is None
    assert private.reasoning_content == "r" * 300_000 and len(private.provider_request_id) > 256
    with pytest.raises(AuditIntegrityError, match="original issued"):
        admitted.metadata.v4_completeness.require_metadata(projected)


def test_cold_provider_validation_does_not_widen_generic_metadata_guard():
    _admitted, cold = _cold_original()
    metadata = cold.provider_metadata
    with pytest.raises(CredentialMaterialRefused) as generic:
        require_no_credential_material_in_llm_metadata(
            content=cold.message.content,
            tool_calls=(),
            reasoning_content=metadata.reasoning_content,
            reasoning_details=metadata.reasoning_details,
            thinking_blocks=metadata.thinking_blocks,
            model_returned=metadata.model_returned,
            provider_request_id=metadata.provider_request_id,
            finish_reason=metadata.finish_reason,
            provider_served=metadata.provider_served,
            surface="test-generic-metadata",
        )
    assert generic.value.finding.category == "traversal_limit"
    require_retained_v4_provider_metadata_credentials(cold, surface="test-v4-retained")
    assert metadata.v4_completeness is None


@pytest.mark.parametrize("mutation", ["credential", "private-limit"])
def test_cold_provider_validation_rechecks_original_fields_and_unchanged_limit(mutation):
    _admitted, cold = _cold_original()
    changed = "ordinary " * 34_000 + "password=" + "original-secret" if mutation == "credential" else "r" * 1_048_577
    substituted = replace(cold, provider_metadata=replace(cold.provider_metadata, reasoning_content=changed))
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_retained_v4_provider_metadata_credentials(substituted, surface="test-v4-retained")
    # Pattern names are internal diagnostics; the public detector maps both
    # connection-password and credential-assignment matches to token_shape.
    assert caught.value.finding.category == ("token_shape" if mutation == "credential" else "traversal_limit")
    assert caught.value.finding.location == ("mapping_value" if mutation == "credential" else "traversal_boundary")
    if mutation == "credential":
        neutral = changed.replace("password=", "ordinary=")
        assert len(neutral) == len(changed)
        ordinary = replace(cold, provider_metadata=replace(cold.provider_metadata, reasoning_content=neutral))
        require_retained_v4_provider_metadata_credentials(ordinary, surface="test-v4-retained")
    assert substituted.provider_metadata.v4_completeness is None


def _issued_incomplete():
    response = _response()
    response.choices[0].message.reasoning_details = object()
    with pytest.raises(V4ProviderResultIncomplete) as caught:
        _admit(response)
    return caught.value


def test_original_parser_incomplete_has_issued_loss_and_accounting_proof():
    incomplete = _issued_incomplete()
    require_issued_v4_incomplete(incomplete, sdk_return=incomplete.sdk_return)
    assert incomplete.losses[0].reason == "unsupported"
    assert incomplete.metadata.provider_cost == 0.125
    assert incomplete.metadata.usage.completion_tokens == 4


@pytest.mark.parametrize("forgery", ["field-copy", "invented-loss"])
def test_nominal_incomplete_cannot_borrow_original_sdk_return(forgery):
    original = _issued_incomplete()
    losses = original.losses if forgery == "field-copy" else (replace(original.losses[0], reason="invalid_type"),)
    fabricated = V4ProviderResultIncomplete(sdk_return=original.sdk_return, metadata=original.metadata, losses=losses)
    assert fabricated is not original and fabricated.sdk_return is original.sdk_return
    with pytest.raises(AuditIntegrityError, match="original parser issuance"):
        require_issued_v4_incomplete(fabricated, sdk_return=original.sdk_return)
    require_issued_v4_incomplete(original, sdk_return=original.sdk_return)


@pytest.mark.parametrize("mutation", ["loss-tuple", "loss-field", "sdk-copy", "sdk-field", "metadata-copy", "cost", "usage", "json"])
def test_parser_incomplete_proof_rejects_changed_original_fields(mutation):
    original = _issued_incomplete()
    returned = original.sdk_return
    if mutation == "loss-tuple":
        captured_losses = original.losses
        original.losses = (*captured_losses,)
        assert original.losses == captured_losses and original.losses is not captured_losses
    elif mutation == "loss-field":
        object.__setattr__(original.losses[0], "reason", "invalid_type")
    elif mutation == "sdk-copy":
        original.sdk_return = replace(returned)
    elif mutation == "sdk-field":
        object.__setattr__(returned, "attempt_id", "changed-attempt")
    elif mutation == "metadata-copy":
        original.metadata = replace(original.metadata)
    elif mutation == "cost":
        object.__setattr__(original.metadata, "provider_cost", 0.0)
    elif mutation == "usage":
        object.__setattr__(original.metadata.usage, "completion_tokens", 99)
    else:
        object.__setattr__(original.metadata, "reasoning_details", {"text": "substituted"})
    with pytest.raises(AuditIntegrityError, match="original parser-issued DATA"):
        require_issued_v4_incomplete(original, sdk_return=returned)


def test_parser_incomplete_proof_requires_same_custody_return_object():
    original = _issued_incomplete()
    with pytest.raises(AuditIntegrityError, match="original parser-issued DATA"):
        require_issued_v4_incomplete(original, sdk_return=replace(original.sdk_return))
    require_issued_v4_incomplete(original, sdk_return=original.sdk_return)
