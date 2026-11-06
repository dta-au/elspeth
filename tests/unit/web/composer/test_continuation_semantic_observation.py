"""Provider-stage semantic Advisor DATA from the original V4 paid phase.

Copies below exercise DATA parsing only; they never supply SQL authority or
replace the original physical owner used to obtain the positive observation.
"""

from __future__ import annotations

import json
from dataclasses import replace

import litellm
import pytest

from elspeth.contracts.composer_audit import PrivateReceivedDiagnostic
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_semantic_advisor as semantic
from elspeth.web.composer.continuation_semantic_advisor_outcome import (
    SemanticAdvisorInvocationV4Finalized,
)
from elspeth.web.composer.continuation_semantic_observation import (
    SemanticCompleteResponseObservation,
    SemanticIncompleteResponseObservation,
    observe_semantic_provider_response,
)
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _case, _observe_case, _reply
from tests.unit.web.composer.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


async def _actual_success(inputs, phases, monkeypatch):
    request = semantic.prepare_semantic_advisor_request(**inputs)
    original = "\n" + json.dumps(_answer(request), indent=2) + "\n"

    async def completion(**kwargs):
        return _reply(original)

    monkeypatch.setattr(litellm, "acompletion", completion)
    observed, terminal, error, actual_request = await _observe_case(phases, monkeypatch)
    assert error is None and terminal is None
    assert type(observed) is SemanticAdvisorInvocationV4Finalized
    assert actual_request.request_sha256 == request.request_sha256
    return actual_request, observed, original


def _observe(request, returned, context, inputs):
    physical = returned.observation
    assert physical.attempt is not None
    return observe_semantic_provider_response(
        physical,
        source=request.source,
        request_sha256=request.request_sha256,
        attempt=physical.attempt,
        context=context,
        env_ref_names=inputs["env_ref_names"],
    )


def _with_large_audit(returned, *, count: int = 5):
    physical = returned.observation
    assert physical.audit_call is not None
    payload = canonical_json({"payload": "x" * 220_000})
    receipts = tuple(
        PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", payload, '{"status":"received"}', None)
        for index in range(count)
    )
    return replace(physical, audit_call=replace(physical.audit_call, private_diagnostics=receipts))


@pytest.mark.asyncio
async def test_complete_semantic_observation_preserves_original_decision_text_and_chunk(operation_store, tmp_path, monkeypatch) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        observed = _observe(request, returned, context, inputs)
        assert isinstance(observed, SemanticCompleteResponseObservation)
        assert observed.message.content == original
        assert observed.decision == returned.finalization.result.decision
        assert observed.audit_call == returned.finalization.result.audit_call
        assert observed.response == returned.finalization.result.response
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_valid_semantic_physical_audit_excess_has_only_bounded_paid_data(operation_store, tmp_path, monkeypatch) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        changed = replace(returned, observation=_with_large_audit(returned))
        observed = _observe(request, changed, context, inputs)
        assert isinstance(observed, SemanticIncompleteResponseObservation)
        assert (observed.representation, observed.document_codec, observed.measurement) == (
            "physical_audit",
            "composer.physical-audit.v1",
            "at_least",
        )
        assert observed.observed_bytes == observed.limit_bytes + 1
        assert observed.evidence_status == "incomplete" and observed.retryable is False
        assert observed.cost.status == "known"
        assert "response" not in observed.__dataclass_fields__ and "decision" not in observed.__dataclass_fields__
        assert original not in repr(observed)
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_escaped_semantic_response_excess_preserves_valid_decision(operation_store, tmp_path, monkeypatch) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        physical = returned.observation
        assert physical.provider_metadata is not None and physical.audit_call is not None
        metadata = replace(
            physical.provider_metadata,
            reasoning_content="x" * 220_000,
            reasoning_details=[{"text": "y" * 220_000}, {"text": "z" * 150_000}],
        )
        changed = replace(
            returned,
            observation=replace(
                physical,
                provider_metadata=metadata,
                audit_call=replace(
                    physical.audit_call,
                    reasoning_content=metadata.reasoning_content,
                    reasoning_details=metadata.reasoning_details,
                ),
            ),
        )
        observed = _observe(request, changed, context, inputs)
        assert isinstance(observed, SemanticIncompleteResponseObservation)
        assert (observed.representation, observed.document_codec) == ("response", "composer.response.v1")
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("large_audit", (False, True))
async def test_wrong_decision_id_is_integrity_error_before_size(operation_store, tmp_path, monkeypatch, large_audit: bool) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        physical = _with_large_audit(returned) if large_audit else returned.observation
        assert physical.decision is not None
        changed = replace(returned, observation=replace(physical, decision=physical.decision.model_copy(update={"decision_id": "foreign"})))
        with pytest.raises(AuditIntegrityError, match="closed decision or attempt"):
            _observe(request, changed, context, inputs)
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("large_audit", (False, True))
async def test_audit_only_credential_is_not_size(operation_store, tmp_path, monkeypatch, large_audit: bool) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        physical = _with_large_audit(returned) if large_audit else returned.observation
        assert physical.audit_call is not None
        audit = replace(
            physical.audit_call,
            model_requested="postgresql://example:secret@db.example/database",  # secret-scan: allow-this-line
        )
        changed = replace(returned, observation=replace(physical, audit_call=audit))
        with pytest.raises(CredentialMaterialRefused):
            _observe(request, changed, context, inputs)
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_malformed_or_tool_bearing_decision_is_not_size(operation_store, tmp_path, monkeypatch) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        physical = _with_large_audit(returned)
        assert physical.message is not None
        for changed in (
            replace(physical, message=replace(physical.message, content="not a closed semantic decision")),
            replace(physical, tool_calls_present=True),
        ):
            with pytest.raises(AuditIntegrityError):
                _observe(request, replace(returned, observation=changed), context, inputs)
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_provider_field_traversal_refusal_is_preserved(operation_store, tmp_path, monkeypatch) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        physical = _with_large_audit(returned)
        assert physical.provider_metadata is not None and physical.audit_call is not None
        metadata = replace(physical.provider_metadata, reasoning_content="x" * 262_145)
        changed = replace(
            returned,
            observation=replace(
                physical, provider_metadata=metadata, audit_call=replace(physical.audit_call, reasoning_content=metadata.reasoning_content)
            ),
        )
        with pytest.raises(CredentialMaterialRefused):
            _observe(request, changed, context, inputs)
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_cost_and_usage_preserve_known_unknown_and_unavailable(operation_store, tmp_path, monkeypatch) -> None:
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, _baseline["phases"], monkeypatch)
        physical = _with_large_audit(returned)
        assert physical.provider_metadata is not None and physical.audit_call is not None
        for amount, expected in ((None, None), (0.0, "0"), (0.25, "0.25"), (5e-324, "5e-324"), (1e308, "1e308")):
            source = "not_available" if amount is None else "response_usage.cost"
            metadata = replace(physical.provider_metadata, provider_cost=amount, provider_cost_source=source)
            audit = replace(physical.audit_call, provider_cost=amount, provider_cost_source=source)
            changed = replace(returned, observation=replace(physical, provider_metadata=metadata, audit_call=audit))
            observed = _observe(request, changed, context, inputs)
            assert isinstance(observed, SemanticIncompleteResponseObservation)
            assert observed.cost.amount == expected
            assert observed.cost.status == ("unknown" if amount is None else "known")
        metadata = replace(physical.provider_metadata, model_returned=None)
        audit = replace(physical.audit_call, model_returned=None)
        fallback = _observe(
            request, replace(returned, observation=replace(physical, provider_metadata=metadata, audit_call=audit)), context, inputs
        )
        assert isinstance(fallback, SemanticIncompleteResponseObservation)
        assert fallback.usage.status == "observed" and fallback.usage.model == audit.model_requested
        long_model = "m" * 257
        metadata = replace(physical.provider_metadata, model_returned=long_model)
        audit = replace(physical.audit_call, model_returned=long_model)
        unavailable = _observe(
            request, replace(returned, observation=replace(physical, provider_metadata=metadata, audit_call=audit)), context, inputs
        )
        assert isinstance(unavailable, SemanticIncompleteResponseObservation)
        assert unavailable.usage.status == "unavailable" and unavailable.usage.model is None
        coordinator.assert_completed()
