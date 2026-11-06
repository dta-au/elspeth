"""Dormant TITLE result observation over the real complete codec projections."""

from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import PrivateReceivedDiagnostic
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.composer_provider_context import PrivateProviderContext
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import measure_continuation_response_size
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call, measure_physical_audit_call_size
from elspeth.web.composer.continuation_title_observation import (
    TitleCompleteObservation,
    TitleOverflowObservation,
    observe_title_provider_result,
)
from elspeth.web.composer.continuation_title_values import capture_title_provider_result, decode_title_provider_result
from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_title_values import title_result_inputs


def _with_diagnostics(values: dict, *, count: int, size: int = 220_000) -> None:
    payload = canonical_json({"payload": "x" * size})
    receipts = tuple(
        PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", payload, '{"status":"received"}', None)
        for index in range(count)
    )
    values["audit_call"] = replace(values["audit_call"], private_diagnostics=receipts)
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])


def test_complete_title_observation_preserves_original_result() -> None:
    values = title_result_inputs()
    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleCompleteObservation)
    assert observed.captured.completion is values["completion"]
    assert observed.captured.audit_call is values["audit_call"]
    decoded = decode_title_provider_result(observed.captured.chunk, env_ref_names=values["env_ref_names"])
    assert decoded.snapshot == observed.captured.snapshot
    assert encode_physical_audit_call(decoded.audit_call) == encode_physical_audit_call(values["audit_call"])


def test_v4_live_title_observation_refuses_completion_without_original_issued_return() -> None:
    values = title_result_inputs()
    with pytest.raises(AuditIntegrityError, match="source-issued complete return"):
        observe_title_provider_result(**values, require_source_issued_v4=True)
    with pytest.raises(AuditIntegrityError, match="exact boolean"):
        observe_title_provider_result(**values, require_source_issued_v4=1)


def test_retainable_large_audit_only_field_matches_complete_capture() -> None:
    values = title_result_inputs()
    values["audit_call"] = replace(values["audit_call"], model_requested="m" * 270_000)
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])
    expected = capture_title_provider_result(**values)

    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleCompleteObservation)
    assert observed.captured.chunk == expected.chunk


def test_large_audit_only_field_with_credential_keeps_credential_refusal() -> None:
    values = title_result_inputs()
    values["audit_call"] = replace(
        values["audit_call"],
        model_requested="m" * 270_000 + " postgresql://example:secret@db.example/database",  # secret-scan: allow-this-line
    )
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])
    with pytest.raises(CredentialMaterialRefused):
        capture_title_provider_result(**values)
    with pytest.raises(CredentialMaterialRefused):
        observe_title_provider_result(**values)


def test_large_physical_audit_is_bounded_paid_observation_with_original_accounting() -> None:
    values = title_result_inputs()
    _with_diagnostics(values, count=5)
    metadata = replace(values["completion"].provider_metadata, provider_cost=0.25, provider_cost_source="response_usage.cost")
    values["completion"] = replace(values["completion"], provider_metadata=metadata)
    values["audit_call"] = replace(values["audit_call"], provider_cost=0.25, provider_cost_source="response_usage.cost")
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])

    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("physical_audit", "composer.physical-audit.v1")
    assert (observed.measurement, observed.observed_bytes, observed.limit_bytes) == (
        "at_least",
        CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1,
        CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    assert observed.attempt_id == values["audit_call"].call_id
    assert (observed.cost.status, observed.cost.amount) == ("known", "0.25")
    assert observed.usage.model == "model-actual"
    assert "x" * 220_000 not in repr(observed)


def test_extreme_finite_known_cost_stays_known_after_response_overflow() -> None:
    values = title_result_inputs()
    _with_diagnostics(values, count=5)
    completion = values["completion"]
    metadata = replace(completion.provider_metadata, provider_cost=1e308, provider_cost_source="response_usage.cost")
    values["completion"] = replace(
        completion,
        provider_metadata=metadata,
    )
    values["audit_call"] = replace(values["audit_call"], provider_cost=1e308, provider_cost_source="response_usage.cost")
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])

    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleOverflowObservation)
    assert (observed.cost.status, observed.cost.amount, observed.cost.source) == ("known", "1e308", "response_usage.cost")


def test_large_response_with_small_audit_is_distinct_excess() -> None:
    values = title_result_inputs()
    completion = values["completion"]
    metadata = replace(completion.provider_metadata, reasoning_content="x" * 230_000, reasoning_details=[{"text": "y" * 230_000}])
    values["completion"] = replace(completion, message=replace(completion.message, content="z" * 230_000), provider_metadata=metadata)
    values["audit_call"] = replace(
        values["audit_call"], reasoning_content=metadata.reasoning_content, reasoning_details=metadata.reasoning_details
    )
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])
    assert measure_physical_audit_call_size(values["audit_call"]).admitted_bytes is not None
    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("response", "composer.response.v1")
    assert observed.cost.status == "unknown"


def test_outer_title_document_counts_escaping_after_inner_fits() -> None:
    values = title_result_inputs()
    completion = values["completion"]
    values["completion"] = replace(completion, message=replace(completion.message, content="\\" * 245_000))
    values["audit_call"] = replace(
        values["audit_call"],
        private_request=PrivateProviderContext("retained", canonical_json({"messages": [{"role": "user", "content": "x" * 120_000}]})),
    )
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])
    assert measure_continuation_response_size(values["completion"], audit_call=values["audit_call"]).admitted_bytes is not None
    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleOverflowObservation)
    assert (observed.representation, observed.document_codec) == ("response", "composer.title-provider-result.v1")


def test_draft_heavy_outer_document_overflows_after_inner_and_audit_fit() -> None:
    values = title_result_inputs()
    _with_diagnostics(values, count=3)
    assert measure_physical_audit_call_size(values["audit_call"]).admitted_bytes is not None
    assert measure_continuation_response_size(values["completion"], audit_call=values["audit_call"]).admitted_bytes is not None
    observed = observe_title_provider_result(**values)
    assert isinstance(observed, TitleOverflowObservation)
    assert observed.document_codec == "composer.title-provider-result.v1"


def test_invalid_audit_is_never_reclassified_as_size() -> None:
    values = title_result_inputs()
    values["audit_call"] = replace(
        values["audit_call"],
        status=ComposerLLMCallStatus.API_ERROR,
        error_class="SyntheticAuditError",
        error_message="synthetic_audit_error",
        reasoning_content="x" * 1_100_000,
    )
    with pytest.raises(AuditIntegrityError, match="changed its actual physical identity"):
        observe_title_provider_result(**values)


def test_changed_draft_cohort_is_never_reclassified_as_size() -> None:
    values = title_result_inputs()
    values["audit_drafts"] = ()
    with pytest.raises(AuditIntegrityError, match="changed its original audit draft cohort"):
        observe_title_provider_result(**values)


def test_credential_in_identity_is_never_reclassified_as_size() -> None:
    values = title_result_inputs()
    values["actor_user_id"] = "postgresql://example:secret@db.example/database"  # secret-scan: allow-this-line
    _with_diagnostics(values, count=5)
    with pytest.raises(CredentialMaterialRefused):
        observe_title_provider_result(**values)


@pytest.mark.parametrize("oversized", (False, True))
def test_audit_only_credential_refuses_before_size(oversized: bool) -> None:
    values = title_result_inputs()
    if oversized:
        _with_diagnostics(values, count=5)
    values["audit_call"] = replace(
        values["audit_call"],
        model_requested="postgresql://example:secret@db.example/database",  # secret-scan: allow-this-line
    )
    values["audit_drafts"] = provider_attempt_audit_drafts(values["audit_call"], values["context"])
    with pytest.raises(CredentialMaterialRefused):
        observe_title_provider_result(**values)


@pytest.mark.parametrize("oversized", (False, True))
def test_malformed_response_refuses_before_size(oversized: bool) -> None:
    values = title_result_inputs()
    if oversized:
        _with_diagnostics(values, count=5)
    completion = values["completion"]
    values["completion"] = replace(completion, tool_batch=replace(completion.tool_batch, call_ids=frozenset({"foreign"})))
    with pytest.raises(AuditIntegrityError, match="inconsistent tool identities"):
        observe_title_provider_result(**values)
