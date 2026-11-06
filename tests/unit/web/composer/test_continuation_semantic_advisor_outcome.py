"""Original semantic Advisor outcomes through real guard/quota/audit owners.

The actual V4 phase supplies admission and completion owners; these tests
inspect its physical observations without installing the returned decision.
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
    _observe_case,
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
        SemanticAdvisorInvocationV4Finalized,
    )
    from elspeth.web.composer.continuation_semantic_advisor_phase import SemanticAdvisorPaidInvalidTerminal

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = semantic.prepare_semantic_advisor_request(**inputs)
        answer = _answer(request)
        if reply_kind == "wrong_call":
            answer["decision_id"] = "a-different-physical-call"
        original = "\nordinary non-JSON paid reply\n" if reply_kind == "malformed" else "\n" + json.dumps(answer, indent=2) + "\n"
        recorder, physical, admitted = inputs["runtime"].live.recorder, [], []
        recorder_before = len(recorder.llm_calls)
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
        outcome, terminal, error, actual_request = await _observe_case(baseline["phases"], monkeypatch)
        observed = outcome.observation
        assert observed.source is actual_request.source and observed.request_sha256 == request.request_sha256
        assert observed.message is admitted[0][0] and observed.provider_metadata is admitted[0][2]
        assert observed.message.content == original
        assert observed.attempt is physical[0] and observed.audit_call is recorder.llm_calls[-1]
        assert len(recorder.llm_calls) == recorder_before + 1
        assert observed.sdk_entered is observed.recorder_completed is True
        assert observed.audit_call.call_id == physical[0].attempt_id
        retained = encode_continuation_text_response(
            content=observed.message.content, provider_metadata=observed.provider_metadata, audit_call=observed.audit_call
        )
        assert decode_continuation_response(retained).message.content == original
        assert decode_retained_physical_audit_call(retained) == observed.audit_call
        assert type(outcome) is SemanticAdvisorInvocationV4Finalized and error is None
        paid = outcome.finalization.result
        assert paid.decision == observed.decision
        assert paid.audit_call == observed.audit_call and paid.response == retained
        assert custodies[0].attempt is custodies[0].call is None
        if reply_kind == "success":
            assert terminal is error is None
            assert outcome.finalization.result.decision == observed.decision
            assert outcome.finalization.result.audit_call == observed.audit_call and outcome.finalization.result.response == retained
            assert custodies[0].attempt is custodies[0].call is None
            assert observed.original_failure is None
            assert observed.audit_call.status is ComposerLLMCallStatus.SUCCESS
        else:
            assert type(terminal) is SemanticAdvisorPaidInvalidTerminal and terminal.paid is paid
            assert paid.outcome == "paid_invalid" and isinstance(paid.original_error, _MalformedLLMResponseError)
            assert terminal.original_error is paid.original_error is observed.original_failure
            assert observed.decision is None
            assert observed.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        after = provider.cost_budget.snapshot()
        assert after.physical_calls == before.physical_calls + 1
        assert len(after.charges) == len(before.charges) + 1
        assert after.charges[-1].response_accountable is True
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert inputs["runtime"].source.selected.stack.active_action_json is None
        assert request.frontier.action_receipts == ()
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_observation_withholds_credential_response_without_losing_original_failure(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        recorder, physical = inputs["runtime"].live.recorder, []
        recorder_before = len(recorder.llm_calls)
        candidate = "postgresql://alice:" + "private-value@example.test/db"

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            return _reply(candidate)

        monkeypatch.setattr(litellm, "acompletion", completion)
        outcome, terminal, error, _request = await _observe_case(baseline["phases"], monkeypatch)
        observed = outcome.observation
        assert type(outcome) is SemanticAdvisorInvocationRaised
        assert error is outcome.error and terminal is None
        assert isinstance(observed.original_failure, CredentialMaterialRefused)
        assert _contains_original(outcome.error, observed.original_failure)
        assert not _contains_original(outcome.error, AuditIntegrityError("distinct exception control"))
        assert observed.message is observed.provider_metadata is observed.decision is None
        assert observed.attempt is physical[0] and observed.sdk_entered is True
        assert observed.audit_call is recorder.llm_calls[-1] and observed.recorder_completed is True
        assert len(recorder.llm_calls) == recorder_before + 1
        assert observed.audit_call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        assert observed.audit_call.error_message == "credential_material_rejected"
        assert candidate not in repr(observed) and candidate not in str(outcome.error)
        assert len(_ledger(operation_store[0])) == baseline["ledger"]
        assert custodies[0].attempt is physical[0]
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["audit", "recorder", "settlement"])
async def test_semantic_observation_preserves_original_failure_and_actual_cleanup_failure(
    operation_store, tmp_path, monkeypatch, failure_site
):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        recorder, physical = inputs["runtime"].live.recorder, []
        recorder_before = len(recorder.llm_calls)
        secondary = AuditIntegrityError("original semantic cleanup fault")
        body = "original allowed paid malformed decision"
        reached = []
        if failure_site == "audit":

            def failed_builder(*args, **kwargs):
                reached.append("audit")
                raise secondary

            monkeypatch.setattr(semantic, "build_llm_call_record", failed_builder)
        elif failure_site == "recorder":
            actual_record = recorder.record_llm_call

            def failed_record(*args, **kwargs):
                actual_record(*args, **kwargs)
                reached.append("recorder")
                raise secondary

            monkeypatch.setattr(recorder, "record_llm_call", failed_record)
        else:
            from elspeth.web.sessions.service import SessionServiceImpl

            actual_finish = SessionServiceImpl.finalize_v4_sdk_terminal

            async def failed_finish(self, **kwargs):
                await actual_finish(self, **kwargs)
                reached.append("settlement")
                raise secondary

            monkeypatch.setattr(SessionServiceImpl, "finalize_v4_sdk_terminal", failed_finish)

        original_sdk_failure = TimeoutError("original semantic SDK failure before required settlement")

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            if failure_site == "settlement":
                raise original_sdk_failure
            return _reply(body)

        monkeypatch.setattr(litellm, "acompletion", completion)
        outcome, _terminal, error, _request = await _observe_case(baseline["phases"], monkeypatch)
        observed = outcome.observation
        assert type(outcome) is SemanticAdvisorInvocationRaised
        assert reached == [failure_site] and error is outcome.error
        if failure_site == "settlement":
            assert observed.original_failure is original_sdk_failure
        else:
            assert isinstance(observed.original_failure, _MalformedLLMResponseError)
        assert _contains_original(outcome.error, secondary)
        if failure_site != "settlement":
            assert _contains_original(outcome.error, observed.original_failure)
        assert not _contains_original(outcome.error, AuditIntegrityError("distinct exception control"))
        assert observed.decision is None
        if failure_site == "settlement":
            assert observed.message is observed.provider_metadata is None
        else:
            assert observed.message.content == body and observed.provider_metadata is not None
        assert observed.attempt is physical[0] and observed.sdk_entered is True
        if failure_site == "audit":
            assert observed.audit_call is None and observed.recorder_completed is False
            assert len(recorder.llm_calls) == recorder_before and len(_ledger(operation_store[0])) == baseline["ledger"]
        else:
            assert observed.audit_call is recorder.llm_calls[-1]
            assert observed.audit_call.status is (
                ComposerLLMCallStatus.TIMEOUT if failure_site == "settlement" else ComposerLLMCallStatus.MALFORMED_RESPONSE
            )
            assert observed.recorder_completed is (failure_site == "settlement")
            assert len(_ledger(operation_store[0])) == baseline["ledger"] + (failure_site == "settlement")
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_observation_keeps_owned_guard_refusal_without_sdk_or_paid_result(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
    )

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
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
        recorder = inputs["runtime"].live.recorder
        recorder_before = len(recorder.llm_calls)
        outcome, terminal, error, _request = await _observe_case(baseline["phases"], monkeypatch)
        observed = outcome.observation
        assert type(outcome) is SemanticAdvisorInvocationRaised and outcome.error is originals[0]
        assert terminal is None and error is outcome.error
        assert observed.original_failure is outcome.error
        assert observed.message is observed.provider_metadata is observed.attempt is observed.audit_call is observed.decision is None
        assert observed.sdk_entered is observed.recorder_completed is False
        assert physical == [] and len(recorder.llm_calls) == recorder_before and outcome.error.llm_calls == ()
        assert custodies[0].attempt is None and len(_ledger(operation_store[0])) == baseline["ledger"]
        checks = _checks(operation_store[0])
        assert len(checks) == baseline["checks"] + 1
        assert checks[-1]["audit_id"] == outcome.error.audit_id and checks[-1]["disposition"] == "blocked"
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_observation_preserves_owner_cancellation_and_actual_paid_audit(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationRaised,
    )
    from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        recorder, entered, physical = inputs["runtime"].live.recorder, asyncio.Event(), []
        recorder_before = len(recorder.llm_calls)

        async def completion(**kwargs):
            physical.append(custodies[0].attempt)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(litellm, "acompletion", completion)
        task = asyncio.create_task(_observe_case(baseline["phases"], monkeypatch))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel("original semantic owner cancellation")
            outcome, terminal, error, _request = await task
            observed = outcome.observation
            assert type(outcome) is SemanticAdvisorInvocationRaised and isinstance(observed.original_failure, asyncio.CancelledError)
            assert observed.original_failure.args == ("original semantic owner cancellation",)
            assert error is None and type(terminal) in (SdkTerminalResult, SdkTerminalFailure)
            if type(terminal) is SdkTerminalFailure:
                assert terminal.original is outcome.error and _contains_original(outcome.error, observed.original_failure)
            else:
                assert outcome.error.__cause__ is observed.original_failure
            assert task.cancelling() == 1
            assert terminal.verified.provider_attempt_id == physical[0].attempt_id and terminal.verified.hold_state == "consumed"
            assert observed.attempt is physical[0] and observed.sdk_entered is True
            assert observed.message is observed.provider_metadata is observed.decision is None
            assert observed.audit_call is recorder.llm_calls[-1] and observed.recorder_completed is True
            assert len(recorder.llm_calls) == recorder_before + 1
            assert observed.audit_call.status is ComposerLLMCallStatus.CANCELLED
            assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
            coordinator.assert_completed()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
