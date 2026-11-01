"""Semantic physical-call contract; these are not live K-7 worker proofs.

Use the existing real fenced Advisor/required Azure guard fixture and replace
only external SDK I/O. Historical source refs below are DATA, not current SQL
source clearance; the phase/source integration remains a separate obligation.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID

import httpx
import litellm
import pytest
from litellm import ModelResponse
from litellm.types.utils import Usage

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_advisor import ContinuationAdvisorInvocation, ContinuationAdvisorPremise, nearest_compose_frame
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_retained_physical_audit_call,
    encode_frontier_chunk,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_progress import initial_continuation_frontier
from elspeth.web.composer.continuation_semantic_advisor import prepare_semantic_advisor_request, run_semantic_advisor
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import ToolResult
from tests.unit.web.composer.test_advisor_invocation_outcome import operation_store as _operation_store
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions.test_continuation_tool_failure import _advance, _advisor_case
from tests.unit.web.sessions.test_token_usage_adapters import _ledger

operation_store = _operation_store


@asynccontextmanager
async def _case(store, tmp_path, monkeypatch, *, cost_limits=None):
    if cost_limits is not None:
        from tests.unit.web.composer import test_explicit_provider_custody as guard_fixture

        actual_settings = guard_fixture._required_guard_settings

        def costed_settings(*args, **kwargs):
            settings, requests = actual_settings(*args, **kwargs)
            return settings.model_copy(update=cost_limits), requests

        monkeypatch.setattr(guard_fixture, "_required_guard_settings", costed_settings)
        from tests.helpers import continuation_policy as policy_fixture

        actual_admission_settings = policy_fixture.continuation_test_settings

        def admitted_cost_settings(**overrides):
            return actual_admission_settings(**(cost_limits | overrides))

        monkeypatch.setattr(policy_fixture, "continuation_test_settings", admitted_cost_settings)
    # The existing worker fixture supplies the actual paid PRIMARY and tool
    # execution, but its external SDK script predates admitted cost policy.
    # Add ordinary provider-reported cost there, not in admission/accounting.
    actual_setattr = monkeypatch.setattr
    preparing_fixture = True

    def set_external_boundary(*args, **kwargs):
        if preparing_fixture and len(args) == 3 and args[0] is litellm and args[1] == "acompletion":
            scripted_sdk = args[2]

            async def accounted_sdk(**options):
                response = await scripted_sdk(**options)
                assert isinstance(response, ModelResponse) and response.usage is not None
                response.usage = Usage(**(response.usage.model_dump() | {"cost": 0.01}))
                return response

            return actual_setattr(litellm, "acompletion", accounted_sdk, **kwargs)
        return actual_setattr(*args, **kwargs)

    actual_setattr(monkeypatch, "setattr", set_external_boundary)
    async with _advisor_case(
        store,
        tmp_path,
        monkeypatch,
        tool_name="get_plugin_schema",
        tool_arguments={"plugin_type": "source", "name": "K7_REFUSED_PLUGIN"},
    ) as (phases, running, paid):
        preparing_fixture = False
        await _advance(phases)
        runtime = phases.runtime
        assert runtime is not None
        state = runtime.live.state
        provider = runtime.bindings.provider_owner
        advisor = phases.service._advisor_checkpoint
        context = running.session_operation_context
        coordinator = phases.work.coordinator
        user = runtime.bindings.message
        frame = nearest_compose_frame(runtime.source.selected.stack)
        frontier = initial_continuation_frontier(
            request=user, original_message_id=runtime.source.evidence.request_id, graph_hash=composition_content_hash(state)
        )
        source = ContinuationAdvisorInvocation(
            kind="semantic",
            state=frame.state,
            evidence=frame.evidence,
            transcript=frame.transcript,
            iteration=frame.iteration,
            coverage_binding=stable_hash({"request": user, "graph": frontier.graph_hash}),
            manual_arguments_json=None,
            manual_tool_call_id=None,
            candidate="The current draft still needs work.",
            premise=ContinuationAdvisorPremise(
                input_hash=stable_hash({"request": user}),
                policy_hash=runtime.source.selected.stack.policy_hash,
                tool_manifest_hash=stable_hash(runtime.live.tools),
                frontier=encode_frontier_chunk(frontier).reference,
            ),
        )
        inputs = {
            "owner": advisor,
            "provider_owner": provider,
            "source": source,
            "frontier": frontier,
            "state": state,
            "content_scope": ComposerContentScope(UUID(context.fence.session_id), composition_content_hash(state)),
            "user_message": user,
            "runtime": runtime,
            "env_ref_names": runtime.env_ref_names,
        }
        baseline = {"ledger": len(_ledger(store[0])), "checks": len(_checks(store[0])), "paid": len(paid)}
        assert baseline["paid"] == 1
        assert runtime.graph.batch is not None
        outcome = runtime.graph.batch.batch.tool_outcomes[-1]
        assert outcome.call.function.name == "get_plugin_schema"
        assert outcome.error_class is not None or (type(outcome.response) is ToolResult and outcome.response.success is False)
        tool_message = next(message for message in runtime.live.llm_messages if message.get("tool_call_id") == "actual_advisor")
        baseline["tool_feedback"] = json.loads(tool_message["content"])
        assert _contains_json_value([tool_message], baseline["tool_feedback"])
        assert not _contains_json_value([{"role": "user", "content": user}], baseline["tool_feedback"])
        custodies = []
        actual_mint = provider.mint

        def mint(family):
            actual = actual_mint(family)
            custodies.append(actual)
            return actual

        monkeypatch.setattr(provider, "mint", mint)
        yield inputs, provider, context, coordinator, baseline, custodies


def _contains_json_value(value, expected):
    """Locate complete owned feedback through its actual JSON string channels."""
    if value == expected:
        return True
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
        return parsed != value and _contains_json_value(parsed, expected)
    if isinstance(value, dict):
        return any(_contains_json_value(entry, expected) for entry in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_json_value(entry, expected) for entry in value)
    return False


def _answer(request):
    premise = request.source.premise
    assert premise is not None
    return {
        "schema_version": "composer.continuation-decision.v1",
        "decision": "CONTINUE",
        "scope": "whole_request",
        "obligation_ids": ["request"],
        "obligations": [],
        "evidence": [],
        "reason": "A concrete authoring step remains.",
        "remaining_actionable_ids": ["request"],
        "prescribed_action": {
            "kind": "primary_turn",
            "obligation_ids": ["request"],
            "evidence_ids": [],
            "input_hash": premise.input_hash,
            "graph_hash": request.frontier.graph_hash,
            "policy_hash": premise.policy_hash,
            "tool_manifest_hash": premise.tool_manifest_hash,
            "instruction": "Use the next PRIMARY call to address the open request.",
        },
    }


def _reply(content):
    return ModelResponse(
        model="test/model",
        choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": content, "tool_calls": None}}],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("router", [False, True])
async def test_semantic_call_uses_real_guard_quota_audit_and_exact_schema(operation_store, tmp_path, monkeypatch, router):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, context, coordinator, baseline, custodies):
        if router:
            monkeypatch.setattr(inputs["owner"], "_advisor_endpoint_base_url", "https://openrouter.ai/api/v1")
        request = prepare_semantic_advisor_request(**inputs)
        recorder = BufferingRecorder()
        physical, dispatched = [], []

        async def completion(**kwargs):
            checks = _checks(operation_store[0])
            assert len(checks) == baseline["checks"] + 1
            assert (checks[-1]["role"], checks[-1]["disposition"]) == ("advisor", "allow")
            assert checks[-1]["operation_id"] == context.fence.operation_id
            assert custodies[0].attempt is not None
            physical.append(custodies[0].attempt)
            assert kwargs["response_format"] == deep_thaw(request.options["response_format"])
            assert (
                kwargs["response_format"]["json_schema"]["schema"]["properties"]["schema_version"]["const"]
                == "composer.continuation-decision.v1"
            )
            assert kwargs["num_retries"] == kwargs["max_retries"] == 0
            assert ("provider" in kwargs) is router
            if router:
                assert kwargs["provider"] == {"require_parameters": True}
            assert "tools" not in kwargs
            complete = "\n".join(message["content"] for message in kwargs["messages"])
            assert "K7_REFUSED_PLUGIN" in complete and "get_plugin_schema" in complete
            assert inputs["user_message"] in complete
            assert _contains_json_value(kwargs["messages"], baseline["tool_feedback"])
            return _reply(json.dumps(_answer(request)))

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await run_semantic_advisor(
            request, provider_owner=provider, recorder=recorder, timeout=5, on_provider_dispatch=lambda: dispatched.append(True)
        )
        assert len(physical) == len(dispatched) == len(recorder.llm_calls) == 1
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert result.source is request.source and result.request_sha256 == request.request_sha256
        assert result.audit_call is recorder.llm_calls[0]
        assert result.decision.decision_id == result.audit_call.call_id == physical[0].attempt_id
        assert result.audit_call.status is ComposerLLMCallStatus.SUCCESS
        assert custodies[0].attempt is custodies[0].call is None
        # The module returned historical DATA, without executing the advice.
        assert request.frontier.action_receipts == ()
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_result_retains_exact_original_response_and_physical_audit(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, _custodies):
        request = prepare_semantic_advisor_request(**inputs)
        original = "\n" + json.dumps(_answer(request), indent=2, ensure_ascii=False) + "\n"

        async def completion(**kwargs):
            return _reply(original)

        monkeypatch.setattr(litellm, "acompletion", completion)
        recorder = BufferingRecorder()
        result = await run_semantic_advisor(request, provider_owner=provider, recorder=recorder, timeout=5)
        assert result.response.reference.codec == "composer.response.v1"
        retained = decode_continuation_response(result.response)
        assert retained.message.content == original
        assert json.loads(original).get("decision_id") is None
        assert decode_retained_physical_audit_call(result.response) == result.audit_call
        assert result.audit_call is recorder.llm_calls[0]
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", ["not JSON", '{"schema_version":"composer.continuation-decision.v1","decision":"CONTINUE","decision":"STOP_CONFIRMED"}']
)
async def test_invalid_semantic_decision_is_malformed_actual_audit(operation_store, tmp_path, monkeypatch, body):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = prepare_semantic_advisor_request(**inputs)
        recorder = BufferingRecorder()

        async def completion(**kwargs):
            return _reply(body)

        monkeypatch.setattr(litellm, "acompletion", completion)
        with pytest.raises(_MalformedLLMResponseError):
            await run_semantic_advisor(request, provider_owner=provider, recorder=recorder, timeout=5)
        assert len(recorder.llm_calls) == 1 and len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert recorder.llm_calls[0].status is ComposerLLMCallStatus.MALFORMED_RESPONSE
        assert custodies[0].attempt is custodies[0].call is None
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_advisor_preserves_actual_cost_cap_and_completion_clip(operation_store, tmp_path, monkeypatch):
    async with _case(
        operation_store,
        tmp_path,
        monkeypatch,
        cost_limits={
            "composer_planner_max_provider_calls": 2,
            "composer_planner_max_completion_tokens": 23,
        },
    ) as (inputs, provider, _context, coordinator, baseline, _custodies):
        budget = provider.cost_budget
        assert budget is not None
        before = budget.snapshot()
        assert before.policy.max_provider_calls == 2 and before.policy.max_completion_tokens == 23
        request = prepare_semantic_advisor_request(**inputs)
        recorder, physical = BufferingRecorder(), []

        async def completion(**kwargs):
            physical.append(kwargs)
            assert kwargs["max_tokens"] == 23
            return _reply(json.dumps(_answer(request)))

        monkeypatch.setattr(litellm, "acompletion", completion)
        result = await run_semantic_advisor(request, provider_owner=provider, recorder=recorder, timeout=5)
        assert result.audit_call.max_completion_tokens_requested == 23
        assert result.audit_call.provider_cost == 0.01
        assert budget.snapshot().physical_calls == before.physical_calls + 1
        assert len(budget.snapshot().charges) == len(before.charges) + 1
        assert budget.snapshot().charges[-1].response_accountable is True
        with pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
            await run_semantic_advisor(prepare_semantic_advisor_request(**inputs), provider_owner=provider, recorder=recorder, timeout=5)
        assert len(physical) == len(recorder.llm_calls) == 1
        assert len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_cancellation_keeps_original_and_one_cancelled_audit(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = prepare_semantic_advisor_request(**inputs)
        recorder, entered, pending, originals = BufferingRecorder(), asyncio.Event(), asyncio.Event(), []

        async def completion(**kwargs):
            entered.set()
            try:
                await pending.wait()
            except asyncio.CancelledError as original:
                originals.append(original)
                raise

        monkeypatch.setattr(litellm, "acompletion", completion)
        task = asyncio.create_task(run_semantic_advisor(request, provider_owner=provider, recorder=recorder, timeout=5))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel("original semantic cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        # wait_for cancels its separate SDK child; retain the owner's actual
        # original cancellation, rather than requiring both Tasks' objects to
        # have the same identity.
        assert len(originals) == 1 and caught.value.args == ("original semantic cancellation",) and task.cancelling() == 1
        assert len(recorder.llm_calls) == 1 and len(_ledger(operation_store[0])) == baseline["ledger"] + 1
        assert recorder.llm_calls[0].status is ComposerLLMCallStatus.CANCELLED
        assert custodies[0].attempt is custodies[0].call is None
        coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["frontier", "graph", "premise", "evidence", "transcript", "iteration", "missing_chunk", "changed_body"])
async def test_semantic_source_preparation_refuses_changed_complete_data_after_positive(operation_store, tmp_path, monkeypatch, change):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, _provider, _context, coordinator, baseline, custodies):
        actual = prepare_semantic_advisor_request(**inputs)
        assert actual.source == inputs["source"]
        source = inputs["source"]
        with pytest.raises(AuditIntegrityError):
            if change == "frontier":
                premise = source.premise.model_copy(
                    update={"frontier": encode_private_chunk(codec="composer.semantic-frontier.v1", canonical="{}").reference}
                )
                inputs["source"] = source.model_copy(update={"premise": premise})
            elif change == "graph":
                inputs["state"] = CompositionState(
                    nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Changed actual graph"), version=1
                )
            elif change == "premise":
                inputs["source"] = source.model_copy(update={"premise": source.premise.model_copy(update={"input_hash": "not-a-hash"})})
            elif change in ("evidence", "transcript", "iteration"):
                original = {"evidence": source.evidence, "transcript": source.transcript, "iteration": source.iteration}[change]
                inputs["source"] = source.model_copy(update={change: encode_private_chunk(codec=original.codec, canonical="{}").reference})
            else:
                runtime = inputs["runtime"]
                selected = runtime.source.selected
                chunks = tuple(chunk for chunk in selected.chunks if chunk.reference != source.evidence)
                if change == "changed_body":
                    from elspeth.web.composer.continuation_codec import PrivateChunk

                    original = next(chunk for chunk in selected.chunks if chunk.reference == source.evidence)
                    chunks = (*chunks, PrivateChunk(original.reference, original.canonical + " "))
                else:
                    assert change == "missing_chunk"
                inputs["runtime"] = replace(runtime, source=replace(runtime.source, selected=replace(selected, chunks=chunks)))
            prepare_semantic_advisor_request(**inputs)
        assert len(_checks(operation_store[0])) == baseline["checks"] and custodies == []
        coordinator.assert_completed()


@pytest.mark.asyncio
async def test_semantic_advisor_refuses_distinct_actual_service_before_quota_or_sdk(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from tests.unit.web.sessions.test_token_usage_adapters import _quota_service

    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        request = prepare_semantic_advisor_request(**inputs)
        foreign_dir = tmp_path / "foreign-service"
        foreign_dir.mkdir()
        foreign_engine, _authority, foreign_service = _quota_service(foreign_dir)
        calls = []

        async def completion(**kwargs):
            calls.append(kwargs)
            return _reply(json.dumps(_answer(request)))

        monkeypatch.setattr(litellm, "acompletion", completion)
        try:
            foreign = ProviderInvocationOwner(service=foreign_service, required_work=provider.required_work)
            changed = replace(request, provider_owner=foreign)
            with pytest.raises(AuditIntegrityError, match="another exact service"):
                await run_semantic_advisor(changed, provider_owner=foreign, recorder=BufferingRecorder(), timeout=5)
            assert calls == [] and custodies == []
            assert len(_ledger(operation_store[0])) == baseline["ledger"]
            assert len(_checks(operation_store[0])) == baseline["checks"]
            coordinator.assert_completed()
        finally:
            foreign_engine.dispose()


@pytest.mark.asyncio
async def test_semantic_guard_refusal_keeps_original_owned_audit_without_sdk_or_attachment(operation_store, tmp_path, monkeypatch):
    async with _case(operation_store, tmp_path, monkeypatch) as (inputs, provider, _context, coordinator, baseline, custodies):
        owner = inputs["owner"]
        configuration = owner._settings.composer_advisor_prompt_guard
        assert configuration is not None
        guard_requests, sdk, originals = [], [], []

        def blocked(request):
            guard_requests.append(request)
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "userPromptAnalysis": {"attackDetected": True},
                    "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
                },
            )

        monkeypatch.setattr(
            owner,
            "_prompt_guard_mount",
            PromptGuardMount(AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(blocked))),
        )
        request = prepare_semantic_advisor_request(**inputs)
        actual_guard = provider_gateway.enforce_prompt_guard

        async def observe_guard(**kwargs):
            try:
                return await actual_guard(**kwargs)
            except PromptGuardRefused as original:
                originals.append(original)
                raise

        async def completion(**kwargs):
            sdk.append(kwargs)
            return _reply(json.dumps(_answer(request)))

        monkeypatch.setattr(provider_gateway, "enforce_prompt_guard", observe_guard)
        monkeypatch.setattr(litellm, "acompletion", completion)
        recorder = BufferingRecorder()
        with pytest.raises(PromptGuardRefused) as caught:
            await run_semantic_advisor(request, provider_owner=provider, recorder=recorder, timeout=5)
        assert len(originals) == 1 and caught.value is originals[0]
        assert guard_requests and caught.value.disposition == "blocked"
        assert caught.value.llm_calls == () and recorder.llm_calls == ()
        assert sdk == [] and custodies[0].attempt is None
        checks = _checks(operation_store[0])
        assert len(checks) == baseline["checks"] + 1
        assert checks[-1]["audit_id"] == caught.value.audit_id and checks[-1]["disposition"] == "blocked"
        assert len(_ledger(operation_store[0])) == baseline["ledger"]
        coordinator.assert_completed()
