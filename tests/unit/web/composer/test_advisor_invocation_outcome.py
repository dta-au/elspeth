"""Original Advisor invocation owners; only external transports are controlled."""

import asyncio
import time
from decimal import Decimal
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.core.canonical import stable_hash
from elspeth.web.composer import advisor_checkpoint
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_invocation_outcome import AdvisorInvocationRaised, AdvisorInvocationReturned, observe_advisor_invocation
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.pipeline_planner import PlannerBudgetPolicy, PlannerContinuationBudget
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_token_usage_adapters import _ledger

operation_store = operations.operation_store
USER = "Review the declared fields for this ordinary request."
ARGUMENTS = {"trigger": "reactive", "problem_summary": "Review source fields", "recent_errors": [], "attempted_actions": []}


def advisor_case(store, tmp_path, monkeypatch):
    engine, repository, _authority, service, sid = store
    with engine.begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    provider = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    advisor = AdvisorCheckpointOwner(
        settings=settings,
        composer_skill_text="Ordinary composer instructions",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=service,
        chargeable_admission=ComposerChargeableAdmission(service),
    )
    custodies, audits, projections = [], [], []
    original_mint = provider.mint

    def mint(family):
        assert family is ProviderInvocationFamily.ADVISOR
        result = original_mint(family)
        custodies.append(result)
        return result

    monkeypatch.setattr(provider, "mint", mint)
    original_builder = advisor_checkpoint.build_llm_call_record

    def build(*args, **kwargs):
        result = original_builder(*args, **kwargs)
        audits.append(result)
        return result

    monkeypatch.setattr(advisor_checkpoint, "build_llm_call_record", build)
    original_capture = advisor_checkpoint._capture_composer_llm_completion_fields

    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        projections.append(result)
        return result

    monkeypatch.setattr(advisor_checkpoint, "_capture_composer_llm_completion_fields", capture)
    return advisor, provider, context, coordinator, requests, custodies, audits, projections


def reply(content="Review the declared field contracts."):
    return ModelResponse(
        model="test/model",
        choices=[{"message": {"role": "assistant", "content": content, "tool_calls": None}}],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("success", "without_recorder", "legacy_tuple", "timeout", "malformed", "credential"))
async def test_original_advisor_outcome_identity_and_accounting(operation_store, tmp_path, monkeypatch, case):
    advisor, provider, context, coordinator, requests, custodies, audits, projections = advisor_case(operation_store, tmp_path, monkeypatch)
    recorder = None if case == "without_recorder" else BufferingRecorder()
    physical, dispatched = [], []
    timeout = TimeoutError("original Advisor transport timeout")

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], context, requests, role="advisor", user_prompt=USER)
        physical.append(custodies[0].attempt)
        assert physical[-1] is not None and kwargs["num_retries"] == kwargs["max_retries"] == 0
        if case == "timeout":
            raise timeout
        if case == "malformed":
            return reply("")
        if case == "credential":
            return reply("postgresql://alice:" + "private-value@example.test/db")
        return reply()

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with prompt_guard_user_scope(USER):
            if case == "legacy_tuple":
                result = await advisor._call_advisor_with_audit(
                    ARGUMENTS, recorder=recorder, provider_owner=provider, on_provider_dispatch=lambda: dispatched.append(True)
                )
                assert type(result) is tuple and result[0] == "Review the declared field contracts."
            else:
                result = await observe_advisor_invocation(
                    advisor, ARGUMENTS, recorder=recorder, provider_owner=provider, on_provider_dispatch=lambda: dispatched.append(True)
                )
                observed = result.observation
                assert observed.attempt is physical[0] and observed.audit_call is audits[0]
                assert observed.recorder_completed is (recorder is not None)
                assert observed.continuation_budget_completed is False
                if case in ("timeout", "malformed", "credential"):
                    assert type(result) is AdvisorInvocationRaised
                    assert observed.message is observed.provider_metadata is observed.guidance is observed.metadata is None
                    if case == "timeout":
                        assert result.error is timeout and audits[0].status is ComposerLLMCallStatus.TIMEOUT
                    else:
                        assert audits[0].status is ComposerLLMCallStatus.MALFORMED_RESPONSE
                else:
                    assert type(result) is AdvisorInvocationReturned
                    assert observed.message is projections[0][0] and observed.provider_metadata is projections[0][2]
                    assert result.result[0] is observed.guidance and result.result[1] is observed.metadata
                    assert audits[0].status is ComposerLLMCallStatus.SUCCESS
        assert len(physical) == len(audits) == len(dispatched) == len(_ledger(operation_store[0])) == 1
        assert audits[0].call_id == physical[0].attempt_id and audits[0].started_at == physical[0].started_at
        assert custodies[0].attempt is custodies[0].call is None
        if recorder is not None:
            assert recorder.llm_calls == (audits[0],) and recorder.llm_calls[0] is audits[0]
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
async def test_original_advisor_cancellation_identity_after_real_settlement(operation_store, tmp_path, monkeypatch):
    advisor, provider, context, coordinator, requests, custodies, audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    entered = asyncio.Event()
    cancellations, physical = [], []

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], context, requests, role="advisor", user_prompt=USER)
        physical.append(custodies[0].attempt)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    monkeypatch.setattr(litellm, "acompletion", completion)

    async def invoke():
        with prompt_guard_user_scope(USER):
            return await observe_advisor_invocation(advisor, ARGUMENTS, recorder=BufferingRecorder(), provider_owner=provider)

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel("original Advisor caller cancellation")
        result = await task
        assert type(result) is AdvisorInvocationRaised and result.error is cancellations[0]
        assert result.observation.attempt is physical[0] and result.observation.audit_call is audits[0]
        assert audits[0].status is ComposerLLMCallStatus.CANCELLED
        assert len(_ledger(operation_store[0])) == 1 and custodies[0].attempt is None
        coordinator.assert_completed()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        operation_store[1].release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ("audit", "recorder"))
async def test_original_advisor_partial_failure_retains_attempt_and_original_error(operation_store, tmp_path, monkeypatch, failure_site):
    advisor, provider, context, coordinator, requests, custodies, audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    recorder = BufferingRecorder()
    original = AuditIntegrityError("original Advisor consumer failure")
    if failure_site == "audit":

        def fail_build(*args, **kwargs):
            raise original

        monkeypatch.setattr(advisor_checkpoint, "build_llm_call_record", fail_build)
    else:
        original_record = recorder.record_llm_call

        def fail_after_record(*args, **kwargs):
            original_record(*args, **kwargs)
            raise original

        monkeypatch.setattr(recorder, "record_llm_call", fail_after_record)
    physical = []

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], context, requests, role="advisor", user_prompt=USER)
        physical.append(custodies[0].attempt)
        return reply()

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        with prompt_guard_user_scope(USER):
            result = await observe_advisor_invocation(advisor, ARGUMENTS, recorder=recorder, provider_owner=provider)
        assert type(result) is AdvisorInvocationRaised and result.observation.attempt is physical[0]
        assert result.observation.recorder_completed is False
        if failure_site == "audit":
            assert isinstance(result.error, BaseExceptionGroup) and result.error.exceptions[0] is original
            assert result.observation.audit_call is None and not audits and not _ledger(operation_store[0])
            assert custodies[0].attempt is physical[0]
        else:
            assert result.error is original and result.observation.audit_call is audits[0]
            assert recorder.llm_calls[0] is audits[0] and len(_ledger(operation_store[0])) == 1
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
async def test_original_advisor_continuation_budget_charges_same_retained_call_once(operation_store, tmp_path, monkeypatch):
    advisor, provider, context, coordinator, requests, _custodies, audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )
    evidence = TurnEvidence(
        session_id=str(operation_store[4]),
        session_operation_context=context,
        request_id="advisor-budget",
        request=USER,
        initial_state_id=None,
        initial_state=CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
    )
    budget = PlannerContinuationBudget(
        policy=PlannerBudgetPolicy(3, 100000, 1024, Decimal("1")),
        session_id=str(operation_store[4]),
        request_id="advisor-budget",
        request_hash=stable_hash(USER),
        provider_owner=provider,
        evidence_owner=evidence,
        deadline=time.perf_counter() + 20,
        reserved_calls=0,
        validated_cost=Decimal("0"),
        cost_known=True,
    )
    budget.validate_for(
        session_id=str(operation_store[4]), request_id="advisor-budget", request=USER, provider_owner=provider, evidence_owner=evidence
    )
    assert budget.reserve_attempt(now=time.perf_counter())

    async def completion(**kwargs):
        _assert_required_guard_admitted(operation_store[0], context, requests, role="advisor", user_prompt=USER)
        return reply()

    monkeypatch.setattr(litellm, "acompletion", completion)
    recorder = BufferingRecorder()
    try:
        with prompt_guard_user_scope(USER):
            result = await observe_advisor_invocation(
                advisor, ARGUMENTS, recorder=recorder, provider_owner=provider, continuation_budget=budget
            )
        # Unknown actual provider cost may refuse output after accounting; it must
        # still consume this exact retained call once, never substitute a new one.
        assert result.observation.continuation_budget_completed is True
        assert result.observation.audit_call is audits[0] and recorder.llm_calls[0] is audits[0]
        assert budget.reserved_calls == 1 and budget._charged_call_ids == {audits[0].call_id}
        with pytest.raises(AuditIntegrityError, match="same advisor call twice"):
            budget.consume_call(audits[0], retained=True)
        assert len(_ledger(operation_store[0])) == 1
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)


@pytest.mark.asyncio
async def test_advisor_observer_refuses_foreign_owner_before_any_effect(operation_store, tmp_path, monkeypatch):
    advisor, provider, context, coordinator, requests, custodies, audits, _projections = advisor_case(
        operation_store, tmp_path, monkeypatch
    )

    async def forbidden(**kwargs):
        raise AssertionError("Foreign observer owner must not dispatch")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    try:
        with pytest.raises(AuditIntegrityError, match="actual current Advisor owner"):
            await observe_advisor_invocation(object(), ARGUMENTS, recorder=None, provider_owner=provider)
        with pytest.raises(AuditIntegrityError, match="actual explicit provider owner"):
            await observe_advisor_invocation(advisor, ARGUMENTS, recorder=None, provider_owner=object())
        assert not requests and not custodies and not audits and not _ledger(operation_store[0])
        coordinator.assert_completed()
    finally:
        operation_store[1].release(context)
