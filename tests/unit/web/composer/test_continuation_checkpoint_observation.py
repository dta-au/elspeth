"""Current checkpoint SDK custody and explicitly separate pure DATA controls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

import litellm
import pytest
from litellm import ModelResponse

from elspeth.contracts.composer_audit import PrivateReceivedDiagnostic
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _AdmittedToolBatch
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_checkpoint_telemetry import AdvisorCheckpointPhase
from elspeth.web.composer.advisor_invocation_outcome import AdvisorInvocationObservation
from elspeth.web.composer.advisor_output import AdvisorCheckpointResponse, AdvisorEndResponse, parse_advisor_checkpoint_response
from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response
from elspeth.web.composer.continuation_checkpoint_early_physical import checkpoint_early_physical_custody
from elspeth.web.composer.continuation_checkpoint_end_physical import checkpoint_end_physical_custody
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointReturnedResponseObservation,
    _observe_checkpoint_returned_response,
    _usage,
    observe_v4_checkpoint_returned_response,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_retained_physical_audit_call,
    measure_continuation_response_size,
)
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_provider_outcomes import measure_physical_audit_call_size
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.tools.sessions import ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.helpers.continuation_app import sdk_reply
from tests.unit.web.composer.test_advisor_checkpoint import _checkpoint_reply
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store

operation_store = _operation_store


@dataclass(frozen=True, slots=True)
class _ActualCheckpointCase:
    physical: AdvisorInvocationObservation
    policy: ContinuationAdmissionPolicyV4
    context: SessionOperationContext
    custody: ProviderCallCustody
    env_ref_names: frozenset[str]
    parsed: AdvisorCheckpointResponse | None
    trigger: str
    content: str
    phase: AdvisorCheckpointPhase
    failure: _MalformedLLMResponseError | None


def _captured_case(
    source, *, phase: AdvisorCheckpointPhase, content: str, failure: _MalformedLLMResponseError | None = None
) -> _ActualCheckpointCase:
    runtime = source.phase_owner.runtime
    physical = source.capture.snapshot()
    custody = checkpoint_early_physical_custody(source) if phase == "early" else checkpoint_end_physical_custody(source)
    if failure is not None and (
        physical.message is None
        or physical.audit_call is None
        or physical.provider_metadata is None
        or physical.attempt is None
        or physical.message.content != content
    ):
        raise failure
    assert physical.attempt is not None and physical.attempt is custody.attempt
    assert physical.audit_call is custody.call and physical.message is not None and physical.message.content == content
    policy = runtime.source.policy
    assert type(policy) is ContinuationAdmissionPolicyV4
    context = runtime.bindings.session_operation_context
    assert type(context) is SessionOperationContext
    parsed = parse_advisor_checkpoint_response(content, phase=phase).response
    trigger = ADVISOR_TRIGGER_DETERMINISTIC_EARLY if phase == "early" else ADVISOR_TRIGGER_DETERMINISTIC_END
    return _ActualCheckpointCase(physical, policy, context, custody, runtime.env_ref_names, parsed, trigger, content, phase, failure)


@asynccontextmanager
async def _actual_checkpoint_case(
    store,
    tmp_path,
    monkeypatch,
    *,
    phase: AdvisorCheckpointPhase,
    verdict="CLEAN",
    publication_verdict=None,
    response_transform: Callable[[ModelResponse], ModelResponse] | None = None,
    raw_content: str | None = None,
) -> AsyncIterator[_ActualCheckpointCase]:
    """Borrow a real paid response before its original caller completes retention.

    The optional transform configures external SDK DATA before admission. It
    never changes an issued witness, source, clock or current SQL premise.
    """
    content = (
        raw_content
        if raw_content is not None
        else _checkpoint_reply(
            verdict=verdict,
            findings="A concrete field contract gap remains." if verdict == "FLAGGED" else "",
            publication_verdict=(publication_verdict or "ADMIT") if phase == "end" else None,
        )
    )
    if phase == "early":
        async with _early_case(store, tmp_path, monkeypatch) as (_store, owner, _running, selected, prior_sdk):
            calls = []

            async def completion(**arguments):
                calls.append(arguments)
                response = sdk_reply(content, model="test/model")
                return response if response_transform is None else response_transform(response)

            monkeypatch.setattr(litellm, "acompletion", completion)
            async with _early_attempt(owner, selected) as source:
                early_failure = None
                try:
                    await _invoke_early(source)
                except _MalformedLLMResponseError as error:
                    if raw_content is None:
                        raise
                    early_failure = error
                assert len(calls) == len(prior_sdk) == 1
                yield _captured_case(source, phase=phase, content=content, failure=early_failure)
        return

    assert phase == "end"
    captured = []
    ready, release = asyncio.Event(), asyncio.Event()
    original_call = AdvisorCheckpointOwner._call_advisor_with_audit
    if raw_content is None:
        monkeypatch.setattr(shared_fixture, "_end_admit", lambda: json.loads(content))

    async def checkpoint_call(advisor, *args, **kwargs):
        if "checkpoint_end_source" not in kwargs or kwargs["checkpoint_end_source"] is None:
            return await original_call(advisor, *args, **kwargs)
        original_sdk = litellm.acompletion

        async def completion(**arguments):
            response = await original_sdk(**arguments)
            if raw_content is not None:
                response.choices[0].message.content = raw_content
            return response if response_transform is None else response_transform(response)

        failure = None
        with monkeypatch.context() as transport:
            transport.setattr(litellm, "acompletion", completion)
            try:
                result = await original_call(advisor, *args, **kwargs)
            except _MalformedLLMResponseError as error:
                if raw_content is None:
                    raise
                failure = error
        if not captured:
            captured.append(_captured_case(kwargs["checkpoint_end_source"], phase=phase, content=content, failure=failure))
            ready.set()
            await release.wait()
        if failure is not None:
            raise failure
        return result

    monkeypatch.setattr(AdvisorCheckpointOwner, "_call_advisor_with_audit", checkpoint_call)

    async def original_end():
        async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch, timed_read=True):
            pass

    original = asyncio.create_task(original_end())
    reached = asyncio.create_task(ready.wait())
    failure = None
    try:
        await asyncio.wait((original, reached), return_when=asyncio.FIRST_COMPLETED)
        if original.done():
            await original
        assert captured, "original END did not reach its actual complete paid response"
        yield captured[0]
    except BaseException as error:
        failure = error
    finally:
        release.set()
        reached.cancel()
        await asyncio.gather(reached, return_exceptions=True)
        try:
            await original
        except BaseException as error:
            if failure is not None and error is not failure:
                raise BaseExceptionGroup("Checkpoint observation and original END completion", [failure, error]) from None
            raise
    if failure is not None:
        raise failure


def _observe(case: _ActualCheckpointCase, physical: AdvisorInvocationObservation | None = None):
    actual = case.physical if physical is None else physical
    assert actual.attempt is not None
    if case.phase == "early":
        return observe_v4_early_checkpoint_returned_response(
            actual,
            custody=case.custody,
            policy=case.policy,
            attempt=actual.attempt,
            context=case.context,
            env_ref_names=case.env_ref_names,
        )
    assert case.phase == "end"
    return observe_v4_checkpoint_returned_response(
        actual,
        custody=case.custody,
        policy=case.policy,
        phase=case.phase,
        trigger=case.trigger,
        attempt=actual.attempt,
        context=case.context,
        env_ref_names=case.env_ref_names,
    )


def _custody_refusal(case: _ActualCheckpointCase) -> str:
    return "original admitted physical capture" if case.phase == "early" else "actual physical custody"


def _observe_data(case: _ActualCheckpointCase, physical: AdvisorInvocationObservation):
    """Exercise the current V4 pure projector; this does not issue live custody."""
    assert physical.attempt is not None
    return _observe_checkpoint_returned_response(
        physical,
        phase=case.phase,
        trigger=case.trigger,
        attempt=physical.attempt,
        context=case.context,
        env_ref_names=case.env_ref_names,
        v4_policy=case.policy,
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
    provider_metadata = replace(physical.provider_metadata, usage=usage, model_returned=model_returned, v4_completeness=None)
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
async def test_complete_checkpoint_keeps_original_guidance_and_audit(operation_store, tmp_path, monkeypatch, phase):
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase=phase) as case:
        observed = _observe(case)
        assert type(observed) is CheckpointReturnedResponseObservation
        assert observed.message is case.physical.message and observed.audit_call is case.physical.audit_call
        assert decode_continuation_response(observed.response).message.content == case.content
        assert decode_retained_physical_audit_call(observed.response) == case.physical.audit_call
        require_original_v4_checkpoint_returned_response(
            observed, attempt_id=observed.attempt.attempt_id, request_sha256=observed.audit_call.messages_hash
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_flagged_checkpoint_and_end_withhold_are_accepted_data(operation_store, tmp_path, monkeypatch, phase):
    async with _actual_checkpoint_case(
        operation_store, tmp_path, monkeypatch, phase=phase, verdict="FLAGGED", publication_verdict="WITHHOLD"
    ) as case:
        observed = _observe(case)
        assert type(observed) is CheckpointReturnedResponseObservation
        assert case.parsed is not None
        assert case.parsed.verdict == "FLAGGED"
        if phase == "end":
            assert type(case.parsed) is AdvisorEndResponse and case.parsed.publication_verdict == "WITHHOLD"
        assert decode_continuation_response(observed.response).message.content == case.content


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_large_checkpoint_audit_is_incomplete_data_without_decision(operation_store, tmp_path, monkeypatch, phase):
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase=phase) as case:
        assert type(_observe(case)) is CheckpointReturnedResponseObservation
        large = _large_audit(case.physical)
        observed = _observe_data(case, large)
        assert type(observed) is CheckpointIncompleteResponseObservation
        assert (observed.representation, observed.document_codec, observed.measurement) == (
            "physical_audit",
            "composer.physical-audit.v1",
            "at_least",
        )
        assert observed.observed_bytes == observed.limit_bytes + 1
        assert observed.evidence_status == "incomplete" and observed.retryable is False
        assert "decision" not in observed.__dataclass_fields__ and "response" not in observed.__dataclass_fields__
        assert case.content not in repr(observed)
        with pytest.raises(AuditIntegrityError, match="original physical return producer"):
            require_original_v4_checkpoint_returned_response(
                observed, attempt_id=observed.attempt_id, request_sha256=observed.request_sha256
            )
        with pytest.raises(AuditIntegrityError, match=_custody_refusal(case)):
            _observe(case, large)
        assert large.provider_metadata is not None and large.audit_call is not None
        unknown = replace(
            large,
            provider_metadata=replace(
                large.provider_metadata, provider_cost=None, provider_cost_source="not_available", v4_completeness=None
            ),
            audit_call=replace(large.audit_call, provider_cost=None, provider_cost_source="not_available"),
        )
        unknown_observed = _observe_data(case, unknown)
        assert type(unknown_observed) is CheckpointIncompleteResponseObservation
        assert unknown_observed.cost.status == "unknown" and unknown_observed.cost.amount is None
        for amount, expected in ((0.0, "0"), (0.25, "0.25"), (5e-324, "5e-324"), (1e308, "1e308")):
            costed = replace(
                large,
                provider_metadata=replace(
                    large.provider_metadata, provider_cost=amount, provider_cost_source="response_usage.cost", v4_completeness=None
                ),
                audit_call=replace(large.audit_call, provider_cost=amount, provider_cost_source="response_usage.cost"),
            )
            cost_observed = _observe_data(case, costed)
            assert type(cost_observed) is CheckpointIncompleteResponseObservation
            assert (cost_observed.cost.status, cost_observed.cost.amount) == ("known", expected)


@pytest.mark.asyncio
async def test_response_only_checkpoint_size_kernel_keeps_independent_end_grammar(operation_store, tmp_path, monkeypatch):
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase="end") as case:
        physical = case.physical
        assert physical.message is not None and physical.provider_metadata is not None and physical.audit_call is not None
        metadata = replace(
            physical.provider_metadata,
            reasoning_content="x" * 220_000,
            reasoning_details=[{"text": "y" * 220_000}, {"text": "z" * 150_000}],
            v4_completeness=None,
        )
        call = replace(physical.audit_call, reasoning_content=metadata.reasoning_content, reasoning_details=metadata.reasoning_details)
        completion = _AdmittedLLMCompletion(physical.message, _AdmittedToolBatch((), frozenset()), metadata)
        assert measure_physical_audit_call_size(call).admitted_bytes is not None
        assert measure_continuation_response_size(completion, audit_call=call).exceeded
        assert parse_advisor_checkpoint_response(case.content, phase="end").response == case.parsed
        with pytest.raises(AuditIntegrityError, match=_custody_refusal(case)):
            _observe(case, replace(physical, provider_metadata=metadata, audit_call=call))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_checkpoint_identity_and_raw_field_mismatch_precede_size(operation_store, tmp_path, monkeypatch, phase):
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase=phase) as case:
        physical = case.physical
        assert physical.attempt is not None and physical.audit_call is not None and physical.provider_metadata is not None
        assert type(_observe(case)) is CheckpointReturnedResponseObservation
        with pytest.raises(AuditIntegrityError, match=_custody_refusal(case)):
            _observe(case, replace(physical, attempt=replace(physical.attempt, attempt_id="foreign-attempt")))
        for call in (
            replace(physical.audit_call, call_id="foreign-attempt"),
            replace(
                physical.audit_call,
                status=ComposerLLMCallStatus.API_ERROR,
                error_class="SyntheticAuditError",
                error_message="synthetic_audit_error",
            ),
        ):
            with pytest.raises(AuditIntegrityError, match=_custody_refusal(case)):
                _observe(case, replace(physical, audit_call=call))
        for metadata in (
            replace(physical.provider_metadata, provider_cost=123.456, provider_cost_source="response_usage.cost"),
            replace(physical.provider_metadata, usage=replace(physical.provider_metadata.usage, prompt_tokens=999)),
        ):
            with pytest.raises(AuditIntegrityError):
                _observe(case, replace(physical, provider_metadata=metadata))
        assert physical.message is not None
        with pytest.raises(AuditIntegrityError):
            _observe(
                case,
                replace(physical, message=replace(physical.message, content="not checkpoint JSON"), guidance="not checkpoint JSON"),
            )
        large = _large_audit(physical)
        with pytest.raises(AuditIntegrityError, match=_custody_refusal(case)):
            _observe(case, large)


@pytest.mark.asyncio
async def test_checkpoint_overflow_usage_keeps_actual_unknown_zero_and_bounds(operation_store, tmp_path, monkeypatch):
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase="early") as case:
        large = _large_audit(case.physical)
        assert large.provider_metadata is not None and large.audit_call is not None
        original = _observe_data(case, large)
        assert type(original) is CheckpointIncompleteResponseObservation
        assert original.usage.status == "observed" and original.usage.model == large.provider_metadata.model_returned
        fallback = _observe_data(case, _with_usage(large, large.provider_metadata.usage, model_returned=None))
        assert type(fallback) is CheckpointIncompleteResponseObservation
        assert fallback.usage.status == "observed" and fallback.usage.model == large.audit_call.model_requested
        for usage, expected in ((TokenUsage(), None), (TokenUsage(prompt_tokens=0, completion_tokens=0), 0)):
            observed = _observe_data(case, _with_usage(large, usage, model_returned="test/model"))
            assert type(observed) is CheckpointIncompleteResponseObservation
            assert observed.usage.status == "observed" and observed.usage.prompt_tokens is expected
            assert observed.usage.completion_tokens is expected
        for length, status in ((256, "observed"), (257, "unavailable")):
            # V4 bounds the diagnostic model before comparing metadata. Check
            # this receipt-usage kernel's own limit without fabricating that
            # earlier projection or claiming an issued provider response.
            usage = _usage(replace(large.audit_call, model_returned="m" * length))
            assert usage.status == status and usage.model == ("m" * length if status == "observed" else None)
        for tokens, status in ((2**31 - 1, "observed"), (2**31, "unavailable")):
            observed = _observe_data(
                case, _with_usage(large, TokenUsage(prompt_tokens=tokens, completion_tokens=0), model_returned="test/model")
            )
            assert type(observed) is CheckpointIncompleteResponseObservation
            assert observed.usage.status == status and observed.usage.prompt_tokens == (tokens if status == "observed" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ("early", "end"))
async def test_actual_checkpoint_keeps_full_original_reasoning_and_refuses_changed_witness_data(
    operation_store, tmp_path, monkeypatch, phase
):
    reasoning = "r" * 100_000

    def response_with_reasoning(response):
        response.choices[0].message.reasoning_content = reasoning
        return response

    async with _actual_checkpoint_case(
        operation_store, tmp_path, monkeypatch, phase=phase, response_transform=response_with_reasoning
    ) as case:
        physical = case.physical
        assert physical.provider_metadata is not None and physical.audit_call is not None
        observed = _observe(case)
        assert type(observed) is CheckpointReturnedResponseObservation
        assert observed.provider_metadata.reasoning_content == reasoning
        assert decode_continuation_response(observed.response).provider_metadata.reasoning_content == reasoning
        with pytest.raises(AuditIntegrityError):
            _observe(case, replace(physical, provider_metadata=replace(physical.provider_metadata, reasoning_content="changed original")))


@pytest.mark.asyncio
@pytest.mark.parametrize("large_audit", (False, True))
async def test_audit_only_credential_refuses_before_checkpoint_size(operation_store, tmp_path, monkeypatch, large_audit):
    async with _actual_checkpoint_case(operation_store, tmp_path, monkeypatch, phase="early") as case:
        physical = _large_audit(case.physical) if large_audit else case.physical
        assert physical.audit_call is not None
        call = replace(
            physical.audit_call,
            model_requested="postgresql://example:secret@db.example/database",  # secret-scan: allow-this-line
        )
        with pytest.raises(CredentialMaterialRefused):
            _observe_data(case, replace(physical, audit_call=call))
        assert physical.provider_metadata is not None
        traversal = replace(physical.provider_metadata, reasoning_content="x" * 262_145, v4_completeness=None)
        with pytest.raises(CredentialMaterialRefused):
            _observe_data(case, replace(physical, provider_metadata=traversal))
