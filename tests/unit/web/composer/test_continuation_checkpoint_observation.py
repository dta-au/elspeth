"""Dormant Review and EARLY checkpoint provider-output DATA controls."""

from __future__ import annotations

from dataclasses import replace

import litellm
import pytest

from elspeth.contracts.composer_audit import PrivateReceivedDiagnostic
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolBatch
from elspeth.web.composer.advisor_invocation_outcome import AdvisorInvocationReturned, observe_advisor_invocation
from elspeth.web.composer.advisor_output import AdvisorCheckpointResponse, AdvisorEndResponse, parse_advisor_checkpoint_response
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointCompleteResponseObservation,
    CheckpointIncompleteResponseObservation,
    observe_checkpoint_provider_response,
)
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_retained_physical_audit_call,
    measure_continuation_response_size,
)
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_advisor_checkpoint import _checkpoint_reply
from tests.unit.web.composer.test_advisor_invocation_outcome import USER, advisor_case, reply
from tests.unit.web.composer.test_advisor_invocation_outcome import operation_store as _operation_store

operation_store = _operation_store


async def _actual_checkpoint(advisor, provider, monkeypatch, *, phase, verdict="CLEAN", publication_verdict=None):
    content = _checkpoint_reply(
        verdict=verdict,
        findings="A concrete field contract gap remains." if verdict == "FLAGGED" else "",
        publication_verdict=(publication_verdict or "ADMIT") if phase == "end" else None,
    )
    trigger = ADVISOR_TRIGGER_DETERMINISTIC_END if phase == "end" else ADVISOR_TRIGGER_DETERMINISTIC_EARLY
    arguments = {"trigger": trigger, "problem_summary": "Review the current graph", "recent_errors": [], "attempted_actions": []}

    async def completion(**kwargs):
        return reply(content)

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(USER):
        returned = await observe_advisor_invocation(
            advisor,
            arguments,
            recorder=BufferingRecorder(),
            provider_owner=provider,
            structured_output=True,
        )
    assert type(returned) is AdvisorInvocationReturned
    parsed = parse_advisor_checkpoint_response(content, phase=phase).response
    assert parsed is not None
    return returned.observation, parsed, trigger, content


def _observe(physical, parsed, trigger, context, *, phase):
    assert physical.attempt is not None
    return observe_checkpoint_provider_response(
        physical,
        phase=phase,
        trigger=trigger,
        attempt=physical.attempt,
        decision=parsed,
        context=context,
        env_ref_names=frozenset(),
    )


def _large_audit(physical):
    assert physical.audit_call is not None
    payload = canonical_json({"payload": "x" * 220_000})
    receipts = tuple(
        PrivateReceivedDiagnostic(f"diagnostic-{index}", f"call-{index}", "list_sources", payload, '{"status":"received"}', None)
        for index in range(5)
    )
    return replace(physical, audit_call=replace(physical.audit_call, private_diagnostics=receipts))


def _with_usage(physical, usage: TokenUsage, *, model_returned: str | None):
    assert physical.provider_metadata is not None and physical.audit_call is not None and physical.metadata is not None
    provider_metadata = replace(physical.provider_metadata, usage=usage, model_returned=model_returned)
    audit = replace(
        physical.audit_call,
        model_returned=model_returned,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cached_prompt_tokens=usage.cached_prompt_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        reasoning_tokens=usage.reasoning_tokens,
    )
    summary = dict(physical.metadata)
    summary.update(
        model=model_returned or audit.model_requested,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_prompt_tokens=usage.cached_prompt_tokens,
    )
    return replace(physical, provider_metadata=provider_metadata, audit_call=audit, metadata=summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_complete_checkpoint_keeps_original_guidance_and_audit(operation_store, tmp_path, monkeypatch, phase) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, content = await _actual_checkpoint(advisor, provider, monkeypatch, phase=phase)
        observed = _observe(physical, parsed, trigger, context, phase=phase)
        assert isinstance(observed, CheckpointCompleteResponseObservation)
        assert observed.message is physical.message and observed.decision is parsed
        assert decode_continuation_response(observed.response).message.content == content
        assert decode_retained_physical_audit_call(observed.response) == physical.audit_call
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_flagged_checkpoint_and_end_withhold_are_accepted_data(operation_store, tmp_path, monkeypatch, phase) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, content = await _actual_checkpoint(
            advisor, provider, monkeypatch, phase=phase, verdict="FLAGGED", publication_verdict="WITHHOLD"
        )
        observed = _observe(physical, parsed, trigger, context, phase=phase)
        assert isinstance(observed, CheckpointCompleteResponseObservation)
        assert observed.decision.verdict == "FLAGGED"
        if phase == "end":
            assert isinstance(observed.decision, AdvisorEndResponse)
            assert observed.decision.publication_verdict == "WITHHOLD"
        assert decode_continuation_response(observed.response).message.content == content
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_large_checkpoint_audit_is_incomplete_data_without_decision(operation_store, tmp_path, monkeypatch, phase) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, content = await _actual_checkpoint(advisor, provider, monkeypatch, phase=phase)
        large = _large_audit(physical)
        observed = _observe(large, parsed, trigger, context, phase=phase)
        assert isinstance(observed, CheckpointIncompleteResponseObservation)
        assert (observed.representation, observed.document_codec, observed.measurement) == (
            "physical_audit",
            "composer.physical-audit.v1",
            "at_least",
        )
        assert observed.observed_bytes == observed.limit_bytes + 1
        assert observed.evidence_status == "incomplete" and observed.retryable is False
        assert "decision" not in observed.__dataclass_fields__ and "response" not in observed.__dataclass_fields__
        assert content not in repr(observed)
        assert large.provider_metadata is not None and large.audit_call is not None
        unknown = replace(
            large,
            provider_metadata=replace(large.provider_metadata, provider_cost=None, provider_cost_source="not_available"),
            audit_call=replace(large.audit_call, provider_cost=None, provider_cost_source="not_available"),
        )
        unknown_observed = _observe(unknown, parsed, trigger, context, phase=phase)
        assert isinstance(unknown_observed, CheckpointIncompleteResponseObservation)
        assert unknown_observed.cost.status == "unknown" and unknown_observed.cost.amount is None
        for amount, expected in ((0.0, "0"), (0.25, "0.25"), (5e-324, "5e-324"), (1e308, "1e308")):
            costed = replace(
                large,
                provider_metadata=replace(large.provider_metadata, provider_cost=amount, provider_cost_source="response_usage.cost"),
                audit_call=replace(large.audit_call, provider_cost=amount, provider_cost_source="response_usage.cost"),
            )
            cost_observed = _observe(costed, parsed, trigger, context, phase=phase)
            assert isinstance(cost_observed, CheckpointIncompleteResponseObservation)
            assert (cost_observed.cost.status, cost_observed.cost.amount) == ("known", expected)
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
async def test_response_only_checkpoint_excess_keeps_valid_end_decision(operation_store, tmp_path, monkeypatch) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, _content = await _actual_checkpoint(advisor, provider, monkeypatch, phase="end")
        assert physical.message is not None and physical.provider_metadata is not None and physical.audit_call is not None
        metadata = replace(
            physical.provider_metadata,
            reasoning_content="x" * 220_000,
            reasoning_details=[{"text": "y" * 220_000}, {"text": "z" * 150_000}],
        )
        call = replace(physical.audit_call, reasoning_content=metadata.reasoning_content, reasoning_details=metadata.reasoning_details)
        completion = _AdmittedLLMCompletion(physical.message, _AdmittedToolBatch((), frozenset()), metadata)
        assert measure_physical_audit_call_size(call).admitted_bytes is not None
        assert measure_continuation_response_size(completion, audit_call=call).exceeded
        observed = _observe(replace(physical, provider_metadata=metadata, audit_call=call), parsed, trigger, context, phase="end")
        assert isinstance(observed, CheckpointIncompleteResponseObservation)
        assert (observed.representation, observed.document_codec) == ("response", "composer.response.v1")
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_checkpoint_identity_and_decision_mismatch_precede_size(operation_store, tmp_path, monkeypatch, phase) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, _content = await _actual_checkpoint(advisor, provider, monkeypatch, phase=phase)
        large = _large_audit(physical)
        assert large.attempt is not None and large.audit_call is not None and large.provider_metadata is not None
        with pytest.raises(AuditIntegrityError, match="original typed phase and paid DATA"):
            observe_checkpoint_provider_response(
                large,
                phase=phase,
                trigger=trigger,
                attempt=replace(large.attempt, attempt_id="foreign-attempt"),
                decision=parsed,
                context=context,
                env_ref_names=frozenset(),
            )
        for call in (
            replace(large.audit_call, call_id="foreign-attempt"),
            replace(
                large.audit_call,
                status=ComposerLLMCallStatus.API_ERROR,
                error_class="SyntheticAuditError",
                error_message="synthetic_audit_error",
            ),
        ):
            with pytest.raises(AuditIntegrityError, match="changed its actual physical identity"):
                _observe(replace(large, audit_call=call), parsed, trigger, context, phase=phase)
        wrong_cost = replace(large.provider_metadata, provider_cost=123.456, provider_cost_source="response_usage.cost")
        with pytest.raises(AuditIntegrityError, match="changed its actual physical identity"):
            _observe(replace(large, provider_metadata=wrong_cost), parsed, trigger, context, phase=phase)
        changed_usage = replace(large.provider_metadata.usage, prompt_tokens=999)
        assert large.metadata is not None
        changed_summary = dict(large.metadata) | {"prompt_tokens": 999}
        with pytest.raises(AuditIntegrityError, match="changed its actual physical identity"):
            _observe(
                replace(large, provider_metadata=replace(large.provider_metadata, usage=changed_usage), metadata=changed_summary),
                parsed,
                trigger,
                context,
                phase=phase,
            )
        changed_decision = parsed.model_copy(update={"category": "schema_mismatch"})
        with pytest.raises(AuditIntegrityError, match="closed original decision"):
            _observe(large, changed_decision, trigger, context, phase=phase)
        wrong_class = (
            AdvisorEndResponse.model_validate(parsed.model_dump(mode="json") | {"publication_verdict": "ADMIT"})
            if phase == "early"
            else AdvisorCheckpointResponse.model_validate(parsed.model_dump(mode="json", exclude={"publication_verdict"}))
        )
        with pytest.raises(AuditIntegrityError, match="original typed phase and paid DATA"):
            _observe(large, wrong_class, trigger, context, phase=phase)
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
async def test_checkpoint_overflow_usage_keeps_actual_unknown_zero_and_bounds(operation_store, tmp_path, monkeypatch) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, _content = await _actual_checkpoint(advisor, provider, monkeypatch, phase="early")
        large = _large_audit(physical)
        assert large.provider_metadata is not None and large.audit_call is not None
        original = _observe(large, parsed, trigger, context, phase="early")
        assert isinstance(original, CheckpointIncompleteResponseObservation)
        assert original.usage.status == "observed"
        assert original.usage.model == large.provider_metadata.model_returned
        fallback = _observe(_with_usage(large, large.provider_metadata.usage, model_returned=None), parsed, trigger, context, phase="early")
        assert isinstance(fallback, CheckpointIncompleteResponseObservation)
        assert fallback.usage.status == "observed" and fallback.usage.model == large.audit_call.model_requested
        for usage, expected in ((TokenUsage(), None), (TokenUsage(prompt_tokens=0, completion_tokens=0), 0)):
            observed = _observe(_with_usage(large, usage, model_returned="test/model"), parsed, trigger, context, phase="early")
            assert isinstance(observed, CheckpointIncompleteResponseObservation)
            assert observed.usage.status == "observed" and observed.usage.prompt_tokens is expected
            assert observed.usage.completion_tokens is expected
        for length, status in ((256, "observed"), (257, "unavailable")):
            observed = _observe(
                _with_usage(large, large.provider_metadata.usage, model_returned="m" * length),
                parsed,
                trigger,
                context,
                phase="early",
            )
            assert isinstance(observed, CheckpointIncompleteResponseObservation)
            assert observed.usage.status == status
            assert observed.usage.model == ("m" * length if status == "observed" else None)
        for tokens, status in ((2**31 - 1, "observed"), (2**31, "unavailable")):
            usage = TokenUsage(prompt_tokens=tokens, completion_tokens=0)
            observed = _observe(_with_usage(large, usage, model_returned="test/model"), parsed, trigger, context, phase="early")
            assert isinstance(observed, CheckpointIncompleteResponseObservation)
            assert observed.usage.status == status
            assert observed.usage.prompt_tokens == (tokens if status == "observed" else None)
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
async def test_invalid_checkpoint_is_not_reclassified_by_large_audit(operation_store, tmp_path, monkeypatch) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, _content = await _actual_checkpoint(advisor, provider, monkeypatch, phase="early")
        oversized = _large_audit(physical)
        assert oversized.message is not None
        invalid = replace(oversized, message=replace(oversized.message, content="not checkpoint JSON"), guidance="not checkpoint JSON")
        with pytest.raises(AuditIntegrityError, match="closed original decision"):
            _observe(invalid, parsed, trigger, context, phase="early")
        with pytest.raises(AuditIntegrityError, match="typed phase"):
            _observe(oversized, parsed, ADVISOR_TRIGGER_DETERMINISTIC_END, context, phase="early")
        assert oversized.provider_metadata is not None and oversized.audit_call is not None
        metadata = replace(oversized.provider_metadata, reasoning_content="x" * 262_145)
        traversal = replace(
            oversized,
            provider_metadata=metadata,
            audit_call=replace(oversized.audit_call, reasoning_content=metadata.reasoning_content),
        )
        with pytest.raises(CredentialMaterialRefused):
            _observe(traversal, parsed, trigger, context, phase="early")
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("large_audit", (False, True))
async def test_audit_only_credential_refuses_before_checkpoint_size(operation_store, tmp_path, monkeypatch, large_audit) -> None:
    advisor, provider, context, coordinator, _requests, _custodies, _audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    try:
        physical, parsed, trigger, _content = await _actual_checkpoint(advisor, provider, monkeypatch, phase="early")
        physical = _large_audit(physical) if large_audit else physical
        assert physical.audit_call is not None
        call = replace(
            physical.audit_call,
            model_requested="postgresql://example:secret@db.example/database",  # secret-scan: allow-this-line
        )
        with pytest.raises(CredentialMaterialRefused):
            _observe(replace(physical, audit_call=call), parsed, trigger, context, phase="early")
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)
