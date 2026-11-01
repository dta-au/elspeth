"""Original semantic Advisor outcomes through real guard/quota/audit owners.

These are physical-result DATA proofs, not live semantic SQL/action admission.
The existing continuation fixture supplies the paid source and refused tool.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import litellm
import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_semantic_advisor as semantic
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_retained_physical_audit_call,
    encode_continuation_text_response,
)
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_semantic_advisor import (
    _answer,
    _case,
    _reply,
)
from tests.unit.web.composer.test_continuation_semantic_advisor import operation_store as _operation_store
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions.test_token_usage_adapters import _ledger

operation_store = _operation_store


def _contains_original(error: BaseException, original: BaseException) -> bool:
    if error is original:
        return True
    return isinstance(error, BaseExceptionGroup) and any(_contains_original(child, original) for child in error.exceptions)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", ["success", "malformed", "wrong_call"])
async def test_semantic_observation_retains_original_paid_response_and_attempt(operation_store, tmp_path, monkeypatch, reply_kind):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
        SemanticAdvisorInvocationReturned,
        observe_semantic_advisor_invocation,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = semantic.prepare_semantic_advisor_request(**inputs)
        answer = _answer(request)
        if reply_kind == "wrong_call":
            answer["decision_id"] = "a-different-physical-call"
        original = "\nordinary non-JSON paid reply\n" if reply_kind == "malformed" else "\n" + json.dumps(answer, indent=2) + "\n"
        recorder, physical, admitted = BufferingRecorder(), [], []
        actual_capture = semantic._capture_composer_llm_completion_fields

        def capture(*args, **kwargs):
            value = actual_capture(*args, **kwargs)
            admitted.append(value)
            return value

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            assert physical[-1] is not None
            return _reply(original)

        monkeypatch.setattr(semantic, "_capture_composer_llm_completion_fields", capture)
        monkeypatch.setattr(litellm, "acompletion", completion)
        before = provider.cost_budget.snapshot()
        outcome = await observe_semantic_advisor_invocation(request, provider_owner=provider, recorder=recorder, timeout=5)
        observed = outcome.observation
        assert observed.source is request.source and observed.request_sha256 == request.request_sha256
        assert observed.message is admitted[0][0] and observed.provider_metadata is admitted[0][2]
        assert observed.message.content == original
        assert observed.attempt is physical[0] and observed.audit_call is recorder.llm_calls[0]
        assert observed.sdk_entered is observed.recorder_completed is True
        assert observed.audit_call.call_id == physical[0].attempt_id
        assert custodies[0].attempt is custodies[0].call is None
        retained = encode_continuation_text_response(
            content=observed.message.content, provider_metadata=observed.provider_metadata, audit_call=observed.audit_call
        )
        assert decode_continuation_response(retained).message.content == original
        assert decode_retained_physical_audit_call(retained) == observed.audit_call
        if reply_kind == "success":
            assert type(outcome) is SemanticAdvisorInvocationReturned
            assert outcome.result.decision is observed.decision
            assert outcome.result.audit_call is observed.audit_call and outcome.result.response == retained
            assert observed.original_failure is None
            assert observed.audit_call.status is ComposerLLMCallStatus.SUCCESS
        else:
            assert type(outcome) is SemanticAdvisorInvocationRaised
            assert isinstance(outcome.error, _MalformedLLMResponseError)
            assert observed.original_failure is outcome.error and observed.decision is None
            assert observed.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        after = provider.cost_budget.snapshot()
        assert after.physical_calls == before.physical_calls + 1 and len(after.charges) == len(before.charges) + 1
        assert after.charges[-1].response_accountable is True
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert inputs["runtime"].source.selected.stack.active_action_json is None
        assert request.frontier.action_receipts == ()
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_observation_withholds_credential_response_without_losing_original_failure(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
        observe_semantic_advisor_invocation,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = semantic.prepare_semantic_advisor_request(**inputs)
        recorder, physical = BufferingRecorder(), []
        candidate = "postgresql://alice:" + "private-value@example.test/db"

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            return _reply(candidate)

        monkeypatch.setattr(litellm, "acompletion", completion)
        outcome = await observe_semantic_advisor_invocation(request, provider_owner=provider, recorder=recorder, timeout=5)
        observed = outcome.observation
        assert type(outcome) is SemanticAdvisorInvocationRaised
        assert isinstance(observed.original_failure, CredentialMaterialRefused)
        assert _contains_original(outcome.error, observed.original_failure)
        assert not _contains_original(outcome.error, AuditIntegrityError("distinct exception control"))
        assert observed.message is observed.provider_metadata is observed.decision is None
        assert observed.attempt is physical[0] and observed.sdk_entered is True
        assert observed.audit_call is recorder.llm_calls[0] and observed.recorder_completed is True
        assert observed.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        assert observed.audit_call.error_message == "credential_material_rejected"
        assert candidate not in repr(observed) and candidate not in str(outcome.error)
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["audit", "recorder", "settlement"])
async def test_semantic_observation_preserves_parse_failure_and_actual_cleanup_failure(
    operation_store, tmp_path, monkeypatch, failure_site
):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
        observe_semantic_advisor_invocation,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = semantic.prepare_semantic_advisor_request(**inputs)
        recorder, physical = BufferingRecorder(), []
        secondary = AuditIntegrityError("original semantic cleanup fault")
        body = "original allowed paid malformed decision"
        if failure_site == "audit":

            def failed_builder(*args, **kwargs):
                raise secondary

            monkeypatch.setattr(semantic, "build_llm_call_record", failed_builder)
        elif failure_site == "recorder":
            actual_record = recorder.record_llm_call

            def failed_record(*args, **kwargs):
                actual_record(*args, **kwargs)
                raise secondary

            monkeypatch.setattr(recorder, "record_llm_call", failed_record)
        else:
            actual_finish = provider.service.finish_provider_attempt

            async def failed_finish(**kwargs):
                await actual_finish(**kwargs)
                raise secondary

            monkeypatch.setattr(provider.service, "finish_provider_attempt", failed_finish)

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            return _reply(body)

        monkeypatch.setattr(litellm, "acompletion", completion)
        outcome = await observe_semantic_advisor_invocation(request, provider_owner=provider, recorder=recorder, timeout=5)
        observed = outcome.observation
        assert type(outcome) is SemanticAdvisorInvocationRaised
        assert isinstance(observed.original_failure, _MalformedLLMResponseError)
        assert _contains_original(outcome.error, observed.original_failure) and _contains_original(outcome.error, secondary)
        assert not _contains_original(outcome.error, AuditIntegrityError("distinct exception control"))
        assert observed.message.content == body and observed.provider_metadata is not None and observed.decision is None
        assert observed.attempt is physical[0] and observed.sdk_entered is True
        if failure_site == "audit":
            assert observed.audit_call is None and observed.recorder_completed is False
            assert recorder.llm_calls == () and len(_ledger(operation_store[0])) == baseline["ledger"]
        else:
            assert observed.audit_call is recorder.llm_calls[0]
            assert observed.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
            assert observed.recorder_completed is (failure_site == "settlement")
            assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_observation_keeps_owned_guard_refusal_without_sdk_or_paid_result(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
        observe_semantic_advisor_invocation,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        owner = inputs["owner"]
        configuration = owner._settings.composer_advisor_prompt_guard
        assert configuration is not None
        originals, physical = [], []

        def blocked(request):
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "userPromptAnalysis": {"attackDetected": True},
                    "documentsAnalysis": [{"attackDetected": False} for _ in payload["documents"]],
                },
            )

        monkeypatch.setattr(
            owner,
            "_prompt_guard_mount",
            PromptGuardMount(AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(blocked))),
        )
        request = semantic.prepare_semantic_advisor_request(**inputs)
        actual_guard = provider_gateway.enforce_prompt_guard

        async def guard(**kwargs):
            try:
                return await actual_guard(**kwargs)
            except PromptGuardRefused as error:
                originals.append(error)
                raise

        async def completion(**kwargs):
            physical.append(kwargs)
            return _reply(json.dumps(_answer(request)))

        monkeypatch.setattr(provider_gateway, "enforce_prompt_guard", guard)
        monkeypatch.setattr(litellm, "acompletion", completion)
        recorder = BufferingRecorder()
        outcome = await observe_semantic_advisor_invocation(request, provider_owner=provider, recorder=recorder, timeout=5)
        observed = outcome.observation
        assert type(outcome) is SemanticAdvisorInvocationRaised and outcome.error is originals[0]
        assert observed.original_failure is outcome.error
        assert observed.message is observed.provider_metadata is observed.attempt is observed.audit_call is observed.decision is None
        assert observed.sdk_entered is observed.recorder_completed is False
        assert physical == [] and recorder.llm_calls == () and outcome.error.llm_calls == ()
        assert custodies[0].attempt is None and len(_ledger(operation_store[0])) == baseline["ledger"]
        checks = _checks(operation_store[0])
        assert len(checks) == baseline["checks"] + 1
        assert checks[-1]["audit_id"] == outcome.error.audit_id and checks[-1]["disposition"] == "blocked"
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_observation_preserves_owner_cancellation_and_actual_paid_audit(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
        observe_semantic_advisor_invocation,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = semantic.prepare_semantic_advisor_request(**inputs)
        recorder, entered, physical = BufferingRecorder(), asyncio.Event(), []

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(litellm, "acompletion", completion)
        task = asyncio.create_task(observe_semantic_advisor_invocation(request, provider_owner=provider, recorder=recorder, timeout=5))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel("original semantic owner cancellation")
            outcome = await task
            observed = outcome.observation
            assert type(outcome) is SemanticAdvisorInvocationRaised and isinstance(observed.original_failure, asyncio.CancelledError)
            assert observed.original_failure.args == ("original semantic owner cancellation",)
            assert _contains_original(outcome.error, observed.original_failure) and task.cancelling() == 1
            assert observed.attempt is physical[0] and observed.sdk_entered is True
            assert observed.message is observed.provider_metadata is observed.decision is None
            assert observed.audit_call is recorder.llm_calls[0] and observed.recorder_completed is True
            assert observed.audit_call.status is ComposerLLMCallStatus.CANCELLED
            assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
            coordinator.assert_completed()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
