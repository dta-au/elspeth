"""Original V4 SDK admission plus separate accidental DATA-misuse controls."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import litellm
import pytest
from litellm import ModelResponse
from litellm.types.utils import Usage

from elspeth.contracts.composer_audit import PrivateReceivedDiagnostic
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_semantic_advisor as semantic
from elspeth.web.composer.continuation_semantic_advisor_outcome import (
    SemanticAdvisorInvocationRaised,
    SemanticAdvisorInvocationV4Finalized,
)
from elspeth.web.composer.continuation_semantic_advisor_phase import (
    SemanticAdvisorPaidIncompleteTerminal,
    SemanticAdvisorPaidInvalidTerminal,
    SemanticAdvisorPaidTerminal,
)
from elspeth.web.composer.continuation_semantic_observation import (
    SemanticCompleteResponseObservation,
    SemanticIncompleteResponseObservation,
    observe_semantic_provider_response,
)
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_semantic_advisor import _answer, _case, _observe_case, _reply
from tests.unit.web.composer.test_continuation_semantic_advisor import operation_store as _operation_store

operation_store = _operation_store


async def _actual_response(inputs, phases, monkeypatch, transform=None):
    request = semantic.prepare_semantic_advisor_request(**inputs)
    original = "\n" + json.dumps(_answer(request), indent=2) + "\n"
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        response = _reply(original)
        return response if transform is None else transform(response)

    monkeypatch.setattr(litellm, "acompletion", completion)
    observed, terminal, error, actual_request = await _observe_case(phases, monkeypatch)
    assert len(calls) == 1
    assert actual_request.request_sha256 == request.request_sha256
    return actual_request, observed, terminal, error, original


async def _actual_success(inputs, phases, monkeypatch, transform=None):
    actual_request, observed, terminal, error, original = await _actual_response(inputs, phases, monkeypatch, transform)
    assert error is None and terminal is None
    assert type(observed) is SemanticAdvisorInvocationV4Finalized
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
        require_v4_completeness=True,
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
    def escaped_reasoning(response):
        response.choices[0].message.reasoning_content = "\x00" * 180_000
        return response

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, _baseline, _custodies):
        request, returned, terminal, error, _original = await _actual_response(inputs, _baseline["phases"], monkeypatch, escaped_reasoning)
        assert error is None and type(returned) is SemanticAdvisorInvocationV4Finalized
        assert type(terminal) is SemanticAdvisorPaidTerminal
        assert returned.observation.decision is not None
        observed = _observe(request, returned, context, inputs)
        assert isinstance(observed, SemanticIncompleteResponseObservation)
        assert (observed.representation, observed.document_codec) == ("response", "composer.response.v1")
        assert observed.observed_bytes == observed.limit_bytes + 1
        assert observed.retryable is False and observed.evidence_status == "incomplete"
        assert terminal.verified.hold_state == "consumed"
        assert terminal.verified.receipt.cost == observed.cost
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
@pytest.mark.parametrize("kind", ("malformed", "tool"))
async def test_actual_invalid_semantic_reply_retains_paid_result_and_closes_custody(operation_store, tmp_path, monkeypatch, kind) -> None:
    def invalid_reply(response):
        if kind == "malformed":
            response.choices[0].message.content = "not a closed semantic decision"
            return response
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "unexpected-semantic-call",
                                "type": "function",
                                "function": {"name": "set_pipeline", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        _request, returned, terminal, error, _original = await _actual_response(inputs, baseline["phases"], monkeypatch, invalid_reply)
        assert error is None and type(returned) is SemanticAdvisorInvocationV4Finalized
        assert type(terminal) is SemanticAdvisorPaidInvalidTerminal
        assert type(terminal.original_error) is _MalformedLLMResponseError
        assert terminal.original_error is returned.observation.original_failure
        assert terminal.paid is returned.finalization.result and terminal.paid.decision is None
        assert returned.observation.tool_calls_present is (kind == "tool")
        assert all(custody.attempt is None and custody.call is None for custody in custodies)
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("admitted", "credential", "over_limit"))
async def test_provider_field_traversal_refusal_is_preserved(operation_store, tmp_path, monkeypatch, kind) -> None:
    reasoning = "x" * (1_048_577 if kind == "over_limit" else 262_145)
    if kind == "credential":
        reasoning += " password=original-secret"  # secret-scan: allow-this-line

    def provider_reasoning(response):
        response.choices[0].message.reasoning_content = reasoning
        return response

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, _custodies):
        _request, returned, terminal, error, _original = await _actual_response(inputs, baseline["phases"], monkeypatch, provider_reasoning)
        if kind == "credential":
            assert type(returned) is SemanticAdvisorInvocationRaised
            assert isinstance(error, CredentialMaterialRefused)
            assert returned.observation.original_failure is error
            assert returned.observation.decision is None and terminal is None
        elif kind == "over_limit":
            assert error is None and type(terminal) is SemanticAdvisorPaidIncompleteTerminal
            assert terminal.verified.hold_state == "consumed"
            assert terminal.verified.receipt.retryable is False
            assert terminal.verified.receipt.evidence_status == "incomplete"
        else:
            assert error is None and terminal is None and type(returned) is SemanticAdvisorInvocationV4Finalized
            metadata = returned.observation.provider_metadata
            assert metadata is not None and metadata.reasoning_content == reasoning
            assert metadata.v4_completeness is not None
            metadata.v4_completeness.require_metadata(metadata)
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "expected", "refusal"),
    [
        ("unavailable", None, "COST_UNAVAILABLE"),
        (0.0, "0", None),
        (0.25, "0.25", None),
        (5e-324, "5e-324", None),
        (1e308, None, "COST_CAP_EXCEEDED"),
    ],
)
async def test_cost_and_usage_preserve_known_unknown_and_unavailable(
    operation_store, tmp_path, monkeypatch, amount, expected, refusal
) -> None:
    def reported_cost(response):
        assert response.usage is not None
        # A present malformed external cost cannot fall back to catalogue pricing.
        response.usage = Usage(**(response.usage.model_dump() | {"cost": amount}))
        return response

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, context, coordinator, baseline, _custodies):
        request, returned, terminal, error, _original = await _actual_response(inputs, baseline["phases"], monkeypatch, reported_cost)
        call = returned.observation.audit_call
        assert call is not None
        assert error is None and terminal is None and type(returned) is SemanticAdvisorInvocationV4Finalized
        assert returned.finalization.result.audit_call == call
        if refusal is not None:
            assert call.provider_cost == (None if amount == "unavailable" else amount)
            source = "not_available" if amount == "unavailable" else "response_usage.cost"
            assert call.provider_cost_source == source and call.cost_response_accountable is True
            budget = provider.cost_budget
            assert budget is not None
            settled = budget.snapshot()
            assert settled.refusal_code == refusal
            assert settled.physical_calls == len(settled.charges)
            (charge,) = tuple(charge for charge in settled.charges if charge.call_id == call.call_id)
            assert charge.cost == (None if amount == "unavailable" else Decimal(str(amount)))
            assert charge.cost_source == source and charge.response_accountable is True
            for require_allowed in (budget.require_output, budget.require_dispatch):
                with pytest.raises(CommonProviderCostRefused) as refused:
                    require_allowed()
                assert refused.value.code == refusal and refused.value.call_id == call.call_id
                assert budget.snapshot() == settled
        else:
            observed = _observe(request, replace(returned, observation=_with_large_audit(returned)), context, inputs)
            assert isinstance(observed, SemanticIncompleteResponseObservation)
            assert observed.cost.amount == expected and observed.cost.status == "known"
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", (None, "m" * 257), ids=("requested-fallback", "returned-unavailable"))
async def test_actual_returned_model_controls_overflow_usage(operation_store, tmp_path, monkeypatch, model) -> None:
    def reported_model(response):
        response.model = model
        return response

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, context, coordinator, baseline, _custodies):
        request, returned, _original = await _actual_success(inputs, baseline["phases"], monkeypatch, reported_model)
        observed = _observe(request, replace(returned, observation=_with_large_audit(returned)), context, inputs)
        assert isinstance(observed, SemanticIncompleteResponseObservation)
        call = returned.observation.audit_call
        assert call is not None
        if model is None:
            assert observed.usage.status == "observed" and observed.usage.model == call.model_requested
        else:
            assert call.model_returned is not None and len(call.model_returned) > 256
            assert observed.usage.status == "unavailable" and observed.usage.model is None
    coordinator.assert_completed()
