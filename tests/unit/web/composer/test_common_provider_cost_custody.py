"""Request cost custody: real SQL units and local physical SDK-boundary controls.

The directly driven custody tests exercise the protocol, not an SDK call.
Physical tests replace only LiteLLM's external completion boundary.
"""

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import admit_llm_provider_metadata, build_llm_call_record
from elspeth.web.composer.provider_cost import (
    CommonProviderCostBudget,
    CommonProviderCostPolicy,
    CommonProviderCostRefused,
    CommonProviderCostSnapshot,
)
from elspeth.web.composer.provider_gateway import _litellm_acompletion, _MalformedLLMResponseError
from elspeth.web.composer.provider_quota import (
    ProviderCallCustody,
    ProviderInvocationFamily,
    ProviderInvocationOwner,
    provider_call_scope,
)
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions.models import quota_provider_attempts_table
from tests.unit.web.sessions.test_token_usage_adapters import _ledger, _quota_service

MODEL = "test/unknown-cost-model"


@pytest.fixture
def owned(tmp_path):
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="cost custody", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60
    )
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    )
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    try:
        yield engine, service, context, coordinator, owner
    finally:
        authority.release(context)
        engine.dispose()


def _budget(owned, *, calls=75, request_bytes=2097152, completion=16384, cost="5.00", applicable=True):
    policy = CommonProviderCostPolicy(calls, request_bytes, completion, Decimal(cost), 2) if applicable else None
    return CommonProviderCostBudget(
        CommonProviderCostSnapshot(owned[2].fence.session_id, "request", 1, "legacy_empty_v1" if applicable else "not_applicable", policy)
    )


def _response(*, cost=0.01):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    if cost is not None:
        usage["cost"] = cost
    return SimpleNamespace(usage=usage)


def _record(custody, recorder, *, response=None, status=ComposerLLMCallStatus.SUCCESS, failure=None):
    recorder.record_llm_call(
        build_llm_call_record(
            model_requested=MODEL,
            messages=[],
            tools=None,
            status=status,
            started_at=datetime.now(UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            response=response,
            error_class=type(failure).__name__ if failure is not None else None,
            error_message=str(failure) if failure is not None else None,
            provider_custody=custody,
        ),
        provider_custody=custody,
    )


async def _protocol_call(custody, recorder, *, cost=0.01, accountable=True):
    """Drive the explicit custody protocol; this helper makes no physical SDK claim."""
    await custody.admit(model=MODEL)
    custody.mark_sdk_entered()
    custody.mark_cost_response_accountable(admitted_metadata=accountable)
    _record(custody, recorder, response=_response(cost=cost))


async def _physical_call(custody, recorder, **kwargs):
    """Use the actual gateway boundary and retain a terminal audit on every outcome."""
    content = kwargs["messages"][0]["content"]
    user_prompt = content if type(content) is str else "".join(part["text"] for part in content)
    async with provider_call_scope(custody):
        response = None
        status = ComposerLLMCallStatus.API_ERROR
        failure = None
        try:
            response = await _litellm_acompletion(
                prompt_guard_mount=None,
                prompt_guard_mode="debug-when-unconfigured",
                prompt_guard_role="composer",
                prompt_guard_user_prompt=user_prompt,
                provider_custody=custody,
                model=MODEL,
                **kwargs,
            )
            admit_llm_provider_metadata(response, choice=None, message=None, pricing_model=MODEL)
            custody.mark_cost_response_accountable(admitted_metadata=True)
            status = ComposerLLMCallStatus.SUCCESS
            return response
        except BaseException as error:
            failure = error
            if isinstance(error, asyncio.CancelledError):
                status = ComposerLLMCallStatus.CANCELLED
            elif isinstance(error, TimeoutError):
                status = ComposerLLMCallStatus.TIMEOUT
            raise
        finally:
            if custody.needs_terminal_audit():
                _record(custody, recorder, response=response, status=status, failure=failure)


def test_owner_binding_is_nominal_idempotent_and_immutable(owned):
    owner = owned[4]
    budget = _budget(owned)
    assert owner.cost_budget is None
    owner.bind_cost_budget(budget)
    owner.bind_cost_budget(budget)
    assert owner.cost_budget is budget
    with pytest.raises(AuditIntegrityError, match="replace"):
        owner.bind_cost_budget(_budget(owned))

    class ForeignBudget(CommonProviderCostBudget):
        pass

    with pytest.raises(AuditIntegrityError, match="nominal"):
        owner.bind_cost_budget(ForeignBudget(budget.snapshot()))
    with pytest.raises(AuditIntegrityError, match="foreign cost policy"):
        ProviderCallCustody(
            service=owned[1], context=owned[2], required_work=owner.required_work, cost_budget=ForeignBudget(budget.snapshot())
        )


@pytest.mark.parametrize("family", [ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR])
def test_owner_rejects_binding_after_covered_mint(owned, family):
    owner = owned[4]
    owner.mint(family)
    with pytest.raises(AuditIntegrityError, match="after covered"):
        owner.bind_cost_budget(_budget(owned))


@pytest.mark.asyncio
async def test_protocol_primary_and_advisor_share_settled_cost_and_sql_precedes_charge(owned, monkeypatch):
    engine, _, _, coordinator, owner = owned
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    recorder = BufferingRecorder()
    consume = budget.consume_settled

    def verify_durable_then_consume(call, *, response_accountable):
        assert len(_ledger(engine)) == len(budget.snapshot().charges) + 1
        consume(call, response_accountable=response_accountable)

    monkeypatch.setattr(budget, "consume_settled", verify_durable_then_consume)
    for family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR):
        custody = owner.mint(family)
        async with provider_call_scope(custody):
            await _protocol_call(custody, recorder)
        await custody.settle()
    assert budget.snapshot().physical_calls == 2
    assert budget.snapshot().known_cost == Decimal("0.02")
    assert tuple(charge.call_id for charge in budget.snapshot().charges) == tuple(call.call_id for call in recorder.llm_calls)
    assert len(_ledger(engine)) == 2
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", [ProviderInvocationFamily.PLANNER, ProviderInvocationFamily.TITLE])
async def test_protocol_planner_and_title_are_outside_common_budget(owned, family):
    owner = owned[4]
    # Excluded-family mints do not prevent a later request-policy binding.
    custody = owner.mint(family)
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    async with provider_call_scope(custody):
        await _protocol_call(custody, BufferingRecorder())
    assert budget.snapshot().physical_calls == 0
    assert budget.snapshot().charges == ()
    assert len(_ledger(owned[0])) == 1
    owned[3].assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("family", [ProviderInvocationFamily.PLANNER, ProviderInvocationFamily.TITLE])
async def test_protocol_excluded_families_minted_after_binding_do_not_charge_common_budget(owned, family):
    """Direct required custody protocol and real SQL settlement, without SDK dispatch."""
    owner = owned[4]
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    custody = owner.mint(family)
    async with provider_call_scope(custody):
        await _protocol_call(custody, BufferingRecorder())
    assert budget.snapshot().physical_calls == 0
    assert budget.snapshot().charges == ()
    assert len(_ledger(owned[0])) == 1
    owned[3].assert_completed()


@pytest.mark.asyncio
async def test_protocol_response_accountability_requires_dispatched_exact_boolean(owned):
    custody = owned[4].mint(ProviderInvocationFamily.PRIMARY)
    with pytest.raises(AuditIntegrityError, match="actual dispatched"):
        custody.mark_cost_response_accountable(admitted_metadata=True)
    await custody.admit(model=MODEL)
    with pytest.raises(AuditIntegrityError, match="actual dispatched"):
        custody.mark_cost_response_accountable(admitted_metadata=True)
    custody.mark_sdk_entered()
    with pytest.raises(AuditIntegrityError, match="actual dispatched"):
        custody.mark_cost_response_accountable(admitted_metadata=1)
    _record(custody, BufferingRecorder(), response=_response())
    await custody.settle()


@pytest.mark.asyncio
async def test_cost_charge_does_not_precede_failed_required_settlement(owned, monkeypatch):
    engine, service, _, _, owner = owned
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    failure = AuditIntegrityError("injected required settlement failure")

    async def fail_settlement(**kwargs):
        assert kwargs["call"] is custody.call
        raise failure

    async def sdk(**kwargs):
        return _response()

    monkeypatch.setattr(service, "finish_provider_attempt", fail_settlement)
    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(AuditIntegrityError) as caught:
        await _physical_call(custody, recorder, messages=[{"role": "user", "content": "Settlement failure control."}])
    assert caught.value is failure
    assert custody.attempt is not None and custody.call is recorder.llm_calls[0]
    assert _ledger(engine) == []
    assert budget.snapshot().physical_calls == 1
    assert budget.snapshot().charges == ()
    assert budget.snapshot().cost_known is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [None, RuntimeError("first-party body"), AuditIntegrityError("first-party integrity"), asyncio.CancelledError("body cancel")]
)
async def test_protocol_cost_refusal_preserves_original_body_outcomes(owned, body):
    owner = owned[4]
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    expected = CommonProviderCostRefused if body is None else BaseExceptionGroup
    with pytest.raises(expected) as caught:
        async with provider_call_scope(custody):
            await _protocol_call(custody, BufferingRecorder(), cost=None)
            if body is not None:
                raise body
    if body is None:
        refusal = caught.value
    else:
        assert caught.value.exceptions[0] is body
        refusal = caught.value.exceptions[1]
    assert type(refusal) is CommonProviderCostRefused
    assert refusal.code == "COST_UNAVAILABLE"
    assert len(_ledger(owned[0])) == 1


@pytest.mark.asyncio
async def test_protocol_malformed_semantics_retains_cost_precedence_and_cause(owned):
    owner = owned[4]
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    malformed = _MalformedLLMResponseError(
        "malformed", provider_metadata=admit_llm_provider_metadata(_response(cost=None), choice=None, message=None)
    )
    with pytest.raises(CommonProviderCostRefused) as caught:
        async with provider_call_scope(owner.mint(ProviderInvocationFamily.PRIMARY)) as custody:
            await _protocol_call(custody, BufferingRecorder(), cost=None)
            raise malformed
    assert caught.value.code == "COST_UNAVAILABLE"
    assert caught.value.__cause__ is malformed


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [TimeoutError("timeout"), asyncio.CancelledError("SDK cancelled"), RuntimeError("SDK fault")])
async def test_physical_failed_sdk_keeps_original_outcome_without_cost_refusal(owned, monkeypatch, body):
    engine, _, _, coordinator, owner = owned
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    entries = []

    async def sdk(**kwargs):
        entries.append(kwargs)
        if len(entries) == 1:
            raise body
        return _response()

    monkeypatch.setattr("litellm.acompletion", sdk)
    recorder = BufferingRecorder()
    with pytest.raises(type(body)) as caught:
        await _physical_call(owner.mint(ProviderInvocationFamily.PRIMARY), recorder, messages=[{"role": "user", "content": "Retry."}])
    assert caught.value is body
    assert budget.snapshot().charges[0].response_accountable is False
    assert budget.snapshot().refusal_code is None
    # Monetary policy permits retry. Independent SQL token admission still
    # refuses unknown usage; this test does not invent usage to bypass it.
    budget.require_dispatch()
    assert len(entries) == budget.snapshot().physical_calls == len(_ledger(engine)) == 1
    assert budget.snapshot().validated_cost == Decimal("0")
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("applicable", [False, True])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_physical_policy_view_preserves_detached_actual_fields_and_key_absence(owned, monkeypatch, applicable, with_tools):
    owner = owned[4]
    budget = _budget(owned, completion=31, applicable=applicable)
    owner.bind_cost_budget(budget)
    views = []
    prepare = budget.prepare_request
    messages = [{"role": "user", "content": [{"type": "text", "text": "Complete actual message.", "cache_control": {"type": "ephemeral"}}]}]
    tools = [
        {
            "type": "function",
            "function": {"name": "review", "description": "Actual full tool", "parameters": {"type": "object", "properties": {}}},
        }
    ]

    def capture_actual_view(view):
        assert ("tools" in view) is with_tools
        assert "max_tokens" not in view
        views.append(view)
        prepare(view)

    async def sdk(**kwargs):
        view = views[0]
        assert kwargs["messages"] is view["messages"]
        assert kwargs["messages"] == messages and kwargs["messages"] is not messages
        if with_tools:
            assert kwargs["tools"] is view["tools"]
            assert kwargs["tools"] == tools and kwargs["tools"] is not tools
        else:
            assert "tools" not in kwargs
        if applicable:
            assert kwargs["max_tokens"] == 31
        else:
            assert "max_tokens" not in kwargs
        return _response()

    monkeypatch.setattr(budget, "prepare_request", capture_actual_view)
    monkeypatch.setattr("litellm.acompletion", sdk)
    kwargs = {"messages": messages}
    if with_tools:
        kwargs["tools"] = tools
    await _physical_call(owner.mint(ProviderInvocationFamily.PRIMARY), BufferingRecorder(), **kwargs)
    assert len(views) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens, expected", [(20, 20), (100, 31)])
async def test_physical_completion_allowance_copies_back_to_actual_sdk(owned, monkeypatch, max_tokens, expected):
    owner = owned[4]
    owner.bind_cost_budget(_budget(owned, completion=31))
    entries = []

    async def sdk(**kwargs):
        entries.append(kwargs)
        return _response()

    monkeypatch.setattr("litellm.acompletion", sdk)
    await _physical_call(
        owner.mint(ProviderInvocationFamily.PRIMARY),
        BufferingRecorder(),
        messages=[{"role": "user", "content": "Allowance."}],
        max_tokens=max_tokens,
    )
    assert entries[0]["max_tokens"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, True, 0, "10"])
async def test_physical_invalid_explicit_completion_is_refused_before_admission(owned, monkeypatch, invalid):
    owner = owned[4]
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    entries = []

    async def sdk(**kwargs):
        entries.append(kwargs)
        return _response()

    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(AuditIntegrityError, match="completion allowance"):
        await _physical_call(
            owner.mint(ProviderInvocationFamily.PRIMARY),
            BufferingRecorder(),
            messages=[{"role": "user", "content": "Invalid."}],
            max_tokens=invalid,
        )
    assert entries == []
    assert budget.snapshot().physical_calls == 0
    with owned[0].connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []


@pytest.mark.asyncio
async def test_physical_complete_wire_bytes_refused_without_truncation_or_admission(owned, monkeypatch):
    owner = owned[4]
    budget = _budget(owned, request_bytes=50)
    owner.bind_cost_budget(budget)
    messages = [{"role": "user", "content": "Complete content " * 50}]
    entries = []

    async def sdk(**kwargs):
        entries.append(kwargs)
        return _response()

    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(CommonProviderCostRefused, match="REQUEST_BYTES_EXHAUSTED"):
        await _physical_call(owner.mint(ProviderInvocationFamily.PRIMARY), BufferingRecorder(), messages=messages)
    assert messages == [{"role": "user", "content": "Complete content " * 50}]
    assert entries == []
    assert budget.snapshot().physical_calls == 0
    with owned[0].connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table)).all() == []


@pytest.mark.asyncio
async def test_physical_dispatch_recheck_disposes_admitted_attempt_without_sdk_or_charge(owned, monkeypatch):
    engine, _, _, coordinator, owner = owned
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    require_dispatch = ProviderCallCustody.require_cost_dispatch
    refusal = CommonProviderCostRefused("PROVIDER_CALLS_EXHAUSTED")
    entries = []

    def fail_only_after_admission(current):
        require_dispatch(current)
        if current.attempt is not None:
            raise refusal

    async def sdk(**kwargs):
        entries.append(kwargs)
        return _response()

    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", fail_only_after_admission)
    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(CommonProviderCostRefused) as caught:
        await _physical_call(custody, BufferingRecorder(), messages=[{"role": "user", "content": "Recheck."}])
    assert caught.value is refusal
    assert entries == []
    assert custody.attempt is None and custody.call is None
    assert budget.snapshot().physical_calls == 0 and budget.snapshot().charges == ()
    assert _ledger(engine)[0][-2:] == (0, 0)
    with engine.connect() as connection:
        assert connection.execute(select(quota_provider_attempts_table.c.settled_at)).scalar_one() is not None
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [RuntimeError("body fault"), asyncio.CancelledError("body cancellation")])
async def test_protocol_required_settlement_failure_keeps_original_body_and_pending_custody(owned, monkeypatch, body):
    engine, service, _, _, owner = owned
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    failure = AuditIntegrityError("required SQL failure")

    async def fail_settlement(**kwargs):
        raise failure

    monkeypatch.setattr(service, "finish_provider_attempt", fail_settlement)
    with pytest.raises(BaseExceptionGroup) as caught:
        async with provider_call_scope(custody):
            await _protocol_call(custody, BufferingRecorder())
            raise body
    assert caught.value.exceptions == (body, failure)
    assert custody.attempt is not None and custody.call is not None
    assert _ledger(engine) == []
    assert budget.snapshot().charges == ()


@pytest.mark.asyncio
async def test_protocol_deferred_cancellation_charges_only_after_real_sql_then_preserves_cancel(owned, monkeypatch):
    engine, service, _, coordinator, owner = owned
    budget = _budget(owned)
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    entered = asyncio.Event()
    release = asyncio.Event()
    finish = service.finish_provider_attempt

    async def delayed_settlement(**kwargs):
        entered.set()
        await release.wait()
        return await finish(**kwargs)

    monkeypatch.setattr(service, "finish_provider_attempt", delayed_settlement)

    async def operation():
        async with provider_call_scope(custody):
            await _protocol_call(custody, BufferingRecorder())

    task = asyncio.create_task(operation())
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert _ledger(engine) == []
    assert budget.snapshot().charges == ()
    task.cancel("original deferred cancellation")
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError, match="original deferred cancellation"):
        await asyncio.wait_for(task, timeout=5)
    assert len(_ledger(engine)) == 1
    assert len(budget.snapshot().charges) == 1
    assert custody.attempt is None and custody.call is None
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_gateway_caller_has_shared_physical_limit_and_admitted_metadata_accountability(owned, tmp_path, monkeypatch):
    from litellm import ModelResponse

    from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
    from elspeth.web.composer.provider_gateway import ProviderGateway
    from tests.unit.web.composer.test_llm_sampling_config import _settings

    engine, _, _, coordinator, owner = owned
    budget = _budget(owned, calls=1, completion=31)
    owner.bind_cost_budget(budget)
    settings = _settings(tmp_path, composer_prompt_guard_mode="debug-when-unconfigured")
    gateway = ProviderGateway(model=MODEL, settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    recorder = BufferingRecorder()
    entries = []

    async def sdk(**kwargs):
        entries.append(kwargs)
        assert kwargs["max_tokens"] == 31
        return ModelResponse(
            model=MODEL,
            choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "Actual gateway reply.", "tool_calls": None}}],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
        )

    monkeypatch.setattr("litellm.acompletion", sdk)
    with prompt_guard_user_scope("Actual gateway request."):
        result = await gateway._call_llm_with_audit(
            [{"role": "user", "content": "Actual gateway request."}],
            [],
            timeout=5,
            recorder=recorder,
            provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        )
        assert result.message.content == "Actual gateway reply."
        with pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
            await gateway._call_llm_with_audit(
                [{"role": "user", "content": "Actual gateway request."}],
                [],
                timeout=5,
                recorder=recorder,
                provider_custody=owner.mint(ProviderInvocationFamily.ADVISOR),
            )
    assert len(entries) == budget.snapshot().physical_calls == len(_ledger(engine)) == 1
    assert budget.snapshot().charges[0].response_accountable is True
    assert len(budget.snapshot().charges) == len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.max_completion_tokens_requested == 31
    assert call.provider_cost == 0.01
    assert call.planner_policy_hash is None and call.planner_call_ordinal is None
    coordinator.assert_completed()
